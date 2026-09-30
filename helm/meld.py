#!/usr/bin/env python3
"""helm meld — the MINDMELD preset over the ONE comms primitive.

A mindmeld is hyper-speed a2a real-time convergence: both parties reply FAST
with what they ALREADY know; a fork that needs research is NOT a meld — it
falls to async (premise meld-discipline). The preset is THIN by law
(one-comms-primitive): a meld is just a fresh room (`meld-<epoch>-<slug>`)
plus a bounded synchronous discipline read over it — no new transport, no
daemon, no second store. The room IS the artifact.

LATENCY-PURE (premise comms-presets-optimize-their-novel-purity): every meld
post and typed lifecycle transition append in RAM — no signing leg, no node
round-trip, no disk write mid-meld. The out-of-band log-flush mirrors lifecycle
truth append-only before rendered room history; replay restores actor state with
transport cursors reset, without resurrecting dead meld rooms.

WAKE (premise a2a-wake-foolproof-layers — the verb owns it, never agent
discipline): (1) durable row — the invite/READY/DONE rows are ordinary room
rows; a TRACKED seat's delivery lane backfills a room born after its join
from offset 0, so the mention that created the meld MUST deliver (seats.py
multi-room law); (2) event path — @mention → PostToolUse boundary (busy) or
the join-mandated `wait --follow` beacon (idle); (3) self-timeout — recv's
blocking bound and the exchange cap are BEHAVIOR (exit 3), never advice. The
cap prints the fall-to-async instruction; the bound prints "recv again",
because many real peer replies take longer than the bound. Layers affect
latency, never delivery.

Mentions fire ONLY at act-moments — invite (@peer), READY (@convener),
DONE/ABORT (@peer). Mid-meld YIELD/HOLD chunks carry NO mention: both
parties sit inside recv polling the room directly at MELD_POLL, so a meld
never floods the peer's delivery cursor with stale nudges (post-meld
boundary spam / stop-guard blocks — the mention-backlog class).

Protocol (the predecessor's meld script lineage, scars kept):
  * every protocol row is epoch-fenced `[MELD e:<epoch>]` — a reused room
    can never replay a dead meld's leftovers into a new session;
  * the invite's protocol head (room + join command) comes FIRST so the
    200-byte delivery clip can never eat the join instruction (a
    predecessor's issue, inverted for a head-clip);
  * READY carries no floor marker — it is control-only; recv returns it
    exactly once (convener, invited status) and skips it everywhere else
    (codex F1: control echoes are not chunks);
  * recv accepts only PEER rows ending in a real floor marker; own rows and
    unattributable senders are skipped fail-closed (codex F2);
  * state is keyed room × ACTOR (`<room>.meld.<seat-key>.json`) — helm's two
    seats always share one chat dir, the exact host-global-state clobber the
    predecessor meld's live dogfood refuted (test-fixtures-isolated-what-production-shares).

Floor markers (the lineage vocabulary, verbatim): [YIELD] hands the floor,
[HOLD] more coming from the same speaker, [DONE] leaving the meld,
[ABORT] kills it fail-loud (exit 4).

v1 is 2-party (the predecessor meld's precedent); 3+ minds use a plain room + discipline.
NAMING (premise council-is-the-number-one-feature, owner canon 2026-07-23):
MELD is the GENUS — `meld` stays the primary verb; `standup` (informal 2+
convergence, includes this 2-party mindmeld) and `council` (the big FORMAL
convergence — agenda/quorum/recorded verdict, the 0.3 N-of-M machinery) are
SPECIES spellings routed to this same preset, never replacements. Every
spelling echoes itself back in the printed next-commands (`via`).

PINNED PAIR: a meld is its invited pair. The seed
carries `invited=<peer>`; join REFUSES any other seat; recv accepts READY
and chunks ONLY from state["peer"] — a third seat can no longer GO, hijack,
or kill a meld with a forged DONE/ABORT. Identity is env-first
(home.chat_name), aligned with chat.whoname — the roster-first order here
silently overrode HELM_CHAT_NAME for any roster-known session.
"""
import base64
import contextlib
import hashlib
import json
import os
import re
import sys
import time

from . import chat, eventledger, freetext, home, pk

MARKERS = ("YIELD", "HOLD", "DONE", "ABORT")
CAP = 5                # accepted inbound chunks before fall-to-async
RECV_TIMEOUT_S = 90.0  # recv blocking bound
MELD_POLL = 0.5        # latency-pure: 4× faster than the room's POLL_S
EXIT_BOUND, EXIT_ABORT = 3, 4

_MARKER_RE = re.compile(r"\[(YIELD|HOLD|DONE|ABORT)\]\s*$")
_EPOCH_RE = re.compile(r"\[MELD(?:-INVITE)? e:(\d+)\]")
_READY_RE = re.compile(r"\bREADY:(\d+)\b")
# the seed's pinned-SET fields — findall[-1] so a topic that EMBEDS a fake
# `convener=… invited=…` run can never outrank the real fields (they are
# appended AFTER the topic, so the last match is always the seed's own).
# `invited` is a comma-joined SET (standup = the informal 2+ species, owner
# canon 2026-07-23): a single `invited=a` still parses as a 1-element set, so
# the 2-party pinned-pair is the size-1 case — strict backward-compat. Split
# on comma at the read sites (`_invited_seats`); the SET is the anti-hijack
# allowlist, generalizing the 2026-07-23 single-peer pin, never loosening it.
_INVITED_RE = re.compile(r"convener=[\w.-]+ invited=([\w.,-]+) cap=\d")


def _invited_seats(seedtext):
    """The invited SET parsed from a seed (standup 2+; the pinned-pair is the
    size-1 case). findall[-1] defeats a topic-embedded spoof; split on comma,
    drop empties. Returns [] for a pre-pin (unpinned) seed — grandfathered."""
    fm = _INVITED_RE.findall(seedtext or "")
    if not fm:
        return []
    return [s for s in fm[-1].split(",") if s]


def _cap():
    try:
        return int(home.env("MELD_CAP") or CAP)
    except ValueError:
        return CAP


def _recv_timeout():
    try:
        return float(home.env("MELD_RECV_TIMEOUT_S") or RECV_TIMEOUT_S)
    except ValueError:
        return RECV_TIMEOUT_S


def _self_seat():
    from . import seats
    # ENV-FIRST, aligned with chat.whoname (identity
    # trap): the roster-first order here silently ignored HELM_CHAT_NAME for
    # any session already in the roster — the invite fired AND the peer
    # joined under the roster seat instead of the exported one, zero warning.
    # home.chat_name is THE validated seam (hostile names rejected at source).
    name = home.chat_name()
    if name:
        # A LIVE RENAME ALIAS NAMES THE RENAMED ROW, as it does for
        # chat.whoname and the actor layer (seats_common.declared_name). Read
        # raw, a seat renamed while its process ran looked its OLD name up as a
        # roster key, found nothing, and read as "no seat" — and `cell`'s
        # signing gate then let the owner's inherited profile stand, so its
        # rows went out SIGNED BY THE OWNER with `from` naming the new seat
        # (task/3049, measured on a live alias). The raw read above still runs
        # first, so a hostile name raises exactly as before.
        return seats.own_name() or name
    sid = home.session_id()
    # safe_cwd, not os.getcwd(): a bare getcwd here crashed ALL five meld
    # verbs from a deleted cwd (eager-getcwd class); derive_seat handles None.
    # RENDER PATH, derive_seat kept deliberately: home.chat_name() above is the
    # validated seam and answers for any declared seat; this floor only names a
    # meld participant for display.
    return seats.seat_for_session(sid) or \
        seats.derive_seat(sid, seats.safe_cwd())


LIFECYCLE_V = 1
_LIFECYCLE_DIR = "meld-lifecycle"
_DURABLE_FIELDS = ("role", "peer", "peers", "exchanges", "cap", "status",
                   "created", "done_peers", "spoke_peers")
_STATUSES = {"invited", "active", "done", "peer-done", "done-mutual", "aborted"}
_UNKNOWN = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_UNKNOWN": (
        "why a room's lifecycle read UNKNOWN, per lifecycle path; a replay "
        "clears it and test_meld clears it first"),
}


class LifecycleError(RuntimeError):
    pass


def state_path(room, seat):
    from . import seats
    return os.path.join(chat.chat_dir(),
                        "%s.meld.%s.json" % (pk.slug(room), seats._seat_key(seat)))


def _room_key(room):
    return base64.urlsafe_b64encode(room.encode("utf-8")).decode("ascii").rstrip("=")


def _room_from_key(key):
    try:
        raw = key + "=" * (-len(key) % 4)
        room = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        raise LifecycleError("invalid meld lifecycle room key %r" % key)
    if _room_key(room) != key:
        raise LifecycleError("non-canonical meld lifecycle room key %r" % key)
    return room


def lifecycle_path(room, durable=False):
    root = (os.path.join(chat.journal_dir(), "meld") if durable else
            os.path.join(chat.chat_dir(), _LIFECYCLE_DIR))
    return os.path.join(root, _room_key(room) + ".jsonl")


def _event_id(room, seq):
    return hashlib.sha256(("meld-lifecycle|%s|%d" % (room, seq)).encode()).hexdigest()[:24]


def _projection(st):
    peers = st.get("peers") or ([st["peer"]] if st.get("peer") else [])
    return {
        "role": st.get("role"), "peer": st.get("peer"), "peers": list(peers),
        "exchanges": st.get("exchanges", 0), "cap": st.get("cap", _cap()),
        "status": st.get("status"), "created": st.get("created"),
        "done_peers": sorted(set(st.get("done_peers") or [])),
        "spoke_peers": sorted(set(st.get("spoke_peers") or [])),
    }


def _valid_projection(p):
    if not isinstance(p, dict) or set(p) != set(_DURABLE_FIELDS):
        return False
    if p["role"] not in ("convener", "joiner") or p["status"] not in _STATUSES:
        return False
    if not isinstance(p["peer"], str) or not p["peer"]:
        return False
    peers = p["peers"]
    if (not isinstance(peers, list) or not peers
            or not all(isinstance(x, str) and x for x in peers)
            or len(peers) != len(set(peers))):
        return False
    if p["peer"] not in peers:
        return False
    if (isinstance(p["exchanges"], bool) or not isinstance(p["exchanges"], int)
            or p["exchanges"] < 0 or isinstance(p["cap"], bool)
            or not isinstance(p["cap"], int) or p["cap"] < 1
            or not isinstance(p["created"], str) or not p["created"]):
        return False
    for name in ("done_peers", "spoke_peers"):
        xs = p[name]
        if (not isinstance(xs, list) or xs != sorted(set(xs))
                or any(x not in peers for x in xs)):
            return False
    return True


def _same_static(before, after):
    return all(before[k] == after[k]
               for k in ("role", "peer", "peers", "cap", "created"))


def _valid_transition(kind, before, after, cause):
    if kind == "invited":
        return (before is None and after["role"] == "convener"
                and after["status"] == "invited" and after["exchanges"] == 0
                and not after["done_peers"] and not after["spoke_peers"])
    if kind == "accepted":
        return (before is None and after["role"] == "joiner"
                and after["status"] == "active" and after["exchanges"] == 0
                and not after["done_peers"] and not after["spoke_peers"])
    if kind == "legacy-snapshot":
        return before is None
    # provenance markers: room-level facts, never an exchange. They restate
    # the last actor's projection UNCHANGED (the restore/reopen mutates no
    # actor state — it annotates the room), so validation is identity. The
    # room-level ORDERING/CAUSALITY between them (a reopen needs a prior
    # restore AND live content between) is a _room_projection law — one
    # transition's identity check cannot see the stream (r4: presence
    # was treated as truth; three malformed histories reduced cleanly).
    if kind == "restored-from-journal":
        return before is not None and after == before
    if kind == "reopened-after-replay":
        return before is not None and after == before
    if before is None or not _same_static(before, after):
        return False
    if kind == "ready":
        want = dict(before, status="active")
        return before["status"] == "invited" and after == want
    if kind in ("local-yield", "local-hold"):
        want = dict(before, status="active" if before["status"] == "invited"
                    else before["status"])
        return before["status"] not in ("aborted", "done-mutual", "peer-done") \
            and after == want
    if kind == "local-done":
        if before["status"] in ("aborted", "done-mutual"):
            return False
        want = dict(before, status="done-mutual" if before["status"] == "peer-done"
                    else "done")
        return after == want
    if kind == "local-abort":
        return before["status"] not in ("aborted", "done-mutual") \
            and after == dict(before, status="aborted")
    if not isinstance(cause, str) or cause not in before["peers"]:
        return False
    spoke = sorted(set(before["spoke_peers"]) | {cause})
    if kind in ("peer-yield", "peer-hold"):
        want = dict(before, exchanges=before["exchanges"] + 1,
                    spoke_peers=spoke,
                    status="active" if before["status"] == "invited"
                    else before["status"])
        return before["status"] not in ("aborted", "done-mutual", "peer-done") \
            and after == want
    if kind in ("peer-abort", "peer-membership-mismatch"):
        want = dict(before, exchanges=before["exchanges"] + 1,
                    spoke_peers=spoke, status="aborted")
        return before["status"] not in ("aborted", "done-mutual") and after == want
    if kind == "peer-done":
        done = sorted(set(before["done_peers"]) | {cause})
        remaining = [x for x in before["peers"] if x not in done]
        status = before["status"] if remaining else (
            "done-mutual" if before["status"] == "done" else "peer-done")
        want = dict(before, exchanges=before["exchanges"] + 1,
                    spoke_peers=spoke, done_peers=done, status=status)
        return before["status"] not in ("aborted", "done-mutual", "peer-done") \
            and after == want
    return False


def _validate_event(ev, room):
    if not isinstance(ev, dict) or ev.get("v") != LIFECYCLE_V:
        return "unknown schema version"
    seq = ev.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        return "invalid sequence"
    if ev.get("id") != _event_id(room, seq) or ev.get("room") != room:
        return "identity mismatch"
    if not isinstance(ev.get("ts"), str) or not ev["ts"]:
        return "invalid timestamp"
    if not isinstance(ev.get("actor"), str) or not ev["actor"]:
        return "invalid actor"
    if isinstance(ev.get("epoch"), bool) or not isinstance(ev.get("epoch"), int):
        return "invalid epoch"
    if not isinstance(ev.get("transition"), str) or not ev["transition"]:
        return "invalid transition"
    if ev.get("cause") is not None and not isinstance(ev["cause"], str):
        return "invalid cause"
    if not _valid_projection(ev.get("projection")):
        return "invalid actor projection"
    return None


#: The two transitions that may start an actor's NEXT ROUND in a room it has
#: already spoken in: a persistent room (a task's pair meld) holds one round
#: per dispatch, each fenced by its own, strictly newer epoch.
_ROUND_OPENERS = ("invited", "accepted")


def _opens_round(kind, before_epoch, epoch):
    """Does a `kind` event at `epoch` open a new round over an actor whose
    last event was at `before_epoch`? Only an invite or a join, and only
    FORWARD: an older epoch can never reopen a round the room moved past."""
    return (kind in _ROUND_OPENERS and isinstance(before_epoch, int)
            and isinstance(epoch, int) and epoch > before_epoch)


def _reduce(events, room):
    """(states, unique, why, roomproj) — the OWNER layer: one fold over the
    validated event stream yields both the per-actor states AND the room-level
    provenance every actor must read identically. roomproj = {"last_activity",
    "restored", "reopened"}: last_activity is the max epoch-ts over REAL
    content events — legacy-snapshot (a migration marker, not an exchange) and
    the restored/reopened provenance markers (reconstruction artifacts) never
    count; restored/reopened record the typed markers' presence. Deriving
    these HERE, from the stream, is what makes them survive snapshot repair
    and a second wipe+restore — actor-local bits died with the snapshot
    (r3: the mixed history was one _repair_from_ram away from gone)."""
    states, unique, by_seq = {}, [], {}
    expected = 1
    for ev in events:
        why = _validate_event(ev, room)
        if why:
            return {}, [], why, _EMPTY_ROOMPROJ
        seq = ev["seq"]
        if seq in by_seq:
            if ev != by_seq[seq]:
                return {}, [], "conflicting duplicate sequence %d" % seq, \
                    _EMPTY_ROOMPROJ
            continue
        if seq != expected:
            return {}, [], "sequence gap: wanted %d, found %d" % (
                expected, seq), _EMPTY_ROOMPROJ
        actor = ev["actor"]
        before = states.get(actor)
        if before is not None and ev["epoch"] != before["epoch"]:
            if not _opens_round(ev["transition"], before["epoch"],
                                ev["epoch"]):
                return {}, [], "actor epoch changed at sequence %d" % seq, \
                    _EMPTY_ROOMPROJ
            before = None                 # a new round starts from nothing
        after = ev["projection"]
        if not _valid_transition(ev["transition"],
                                 before["projection"] if before else None,
                                 after, ev.get("cause")):
            return {}, [], "impossible transition %s at sequence %d" % (
                ev["transition"], seq), _EMPTY_ROOMPROJ
        states[actor] = {"epoch": ev["epoch"], "projection": after,
                         "journal_seq": seq}
        by_seq[seq] = ev
        unique.append(ev)
        expected += 1
    roomproj, pwhy = _room_projection(unique)
    if pwhy:
        return {}, [], pwhy, _EMPTY_ROOMPROJ
    return states, unique, None, roomproj


def _ts_epoch(s):
    """ISO-8601 wall clock -> epoch seconds, or None. The lifecycle event's
    `ts` is this format; the room's `epoch` is a creation marker, NOT a clock
    for activity, and must never be parsed here."""
    import calendar
    try:
        return calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def _room_projection(unique):
    """(proj, why) — room-level provenance from the validated stream, with
    the room-level CAUSALITY the per-transition check cannot see (r4).
    Pure: same events in, same projection out, for EVERY actor and reader.

    Marker laws, fail-closed (a malformed history is UNKNOWN, never truth):
      * reopened-after-replay requires a PRIOR restored-from-journal (a
        reopen with no restore, or one ordered before it, is impossible);
      * at least one REAL content event must sit between restore and reopen
        — the reopen marker exists to record that live activity RESUMED, so
        a restore immediately followed by a reopen claims a resumption the
        stream does not contain;
      * a restored-from-journal landing while the room is already restored
        is a RE-RESTORE (a second wipe+replay of the same history): legal.
        It resets the intervening-content count, so a reopen recorded for
        the earlier restore does not leak forward as content for the new
        one. Duplicate REOPEN markers are noise, never history.

    Clock laws (the r4 generated-clock class): an unparseable or FUTURE
    event ts (hard-rejected — ANY future epoch, no slack) POISONS the room
    clock: the activity record is no longer trustworthy, so last_activity is
    withheld no matter how clean a sibling ts reads. One bad event plus one
    good one still rendered a confident age when only the max was kept."""
    proj = {"last_activity": None, "restored": False, "reopened": False,
            "clock_unknown": False}
    now = int(time.time())
    live_content_since_restore = 0
    for ev in unique:
        kind = ev["transition"]
        if kind == "restored-from-journal":
            # a re-restore (second wipe+replay of the same history) lands
            # while already restored: legal — but it RESETS the intervening
            # count, so a reopen recorded for the earlier restore does not
            # leak forward as content for this one.
            proj["restored"] = True
            live_content_since_restore = 0
            continue                        # a marker is not an exchange
        if kind == "reopened-after-replay":
            if not proj["restored"]:
                return proj, "reopened-after-replay without a prior restore"
            if proj["reopened"]:
                return proj, "duplicate reopened-after-replay marker"
            if not live_content_since_restore:
                return proj, "reopened-after-replay with no intervening live content"
            proj["reopened"] = True
            continue                        # ditto
        if kind == "legacy-snapshot":
            continue                        # a migration marker, not an exchange
        live_content_since_restore += 1
        # ev["ts"] is the event's WALL CLOCK; ev["epoch"] is the room's
        # creation epoch, constant for every event in the room. Reading epoch
        # here made last_activity the creation time forever and hid every
        # later exchange — the same wrong-clock class the name-epoch fallback
        # had, one layer down. _ts_epoch parses the ISO wall clock.
        ep = _ts_epoch(ev.get("ts"))
        if ep is None:
            # an unparseable event clock is not "no activity" — it is UNKNOWN
            # activity, and it POISONS the room clock: the record of WHEN
            # things happened can no longer be trusted, so no sibling ts —
            # older OR newer — may stand in with a confident age.
            proj["clock_unknown"] = True
            continue
        if ep > now:
            # a FUTURE stream clock is impossible; the old +60s slack let
            # now+30 render age=0s as confident certainty (the generated
            # boundary probe caught it). Hard-reject ANY future epoch; poison
            # the room clock, never record a future activity time.
            proj["clock_unknown"] = True
            continue
        proj["last_activity"] = max(proj["last_activity"] or 0, ep)
    if proj["clock_unknown"]:
        # the poison law: a dirty clock withholds the age even when a clean
        # sibling parsed fine — age is a defensible number or ABSENT, never
        # certainty the inputs do not support.
        proj["last_activity"] = None
    elif not proj["last_activity"]:
        proj["last_activity"] = None
    return proj, None


_EMPTY_ROOMPROJ = {"last_activity": None, "restored": False,
                   "reopened": False, "clock_unknown": False}


def _events(path):
    events, unavailable = eventledger.checked_events(path, strict=True)
    return events, unavailable


def _cache_key(room):
    return os.path.abspath(lifecycle_path(room)), room


def converged_with(st, peer):
    """True iff actor snapshot `st` records a CLOSED meld that `peer` spoke in.

    Closed means `done` or `done-mutual`. Spoke means recv accepted at least
    one chunk from `peer` (its DONE counts): `exchanges >= 1` and `peer` in
    `spoke_peers`. One side's own DONE writes `done` whether or not the peer
    ever joined, so status alone reads a meld closed alone as converged.
    Measured on the live bus: 17 of 83 meld records were `done` with 0
    exchanges and no peer in spoke_peers, and the stop guard's spiral rung
    (seats_stop_signals._melded_with) took each one as the cure.

    Fails closed: a missing or malformed field is not convergence, and a bool
    is never an exchange count."""
    if not isinstance(st, dict) or str(st.get("status") or "") not in (
            "done", "done-mutual"):
        return False
    who = [st.get("peer")] + list(st.get("peers") or [])
    ex, spoke = st.get("exchanges"), st.get("spoke_peers")
    return (peer in [str(x) for x in who if x]
            and isinstance(ex, int) and not isinstance(ex, bool) and ex >= 1
            and isinstance(spoke, list) and peer in [str(x) for x in spoke])


def _unknown_state(room, seat, reason):
    _UNKNOWN[_cache_key(room)] = reason
    return {"room": room, "self": seat, "status": "UNKNOWN", "_unknown": reason}


def _snapshot(room, actor, folded):
    st = {"room": room, "epoch": folded["epoch"], "self": actor, "idx": 0,
          "journal_seq": folded["journal_seq"]}
    st.update(folded["projection"])
    return st


def _repair_from_ram(room, seat):
    path = lifecycle_path(room)
    events, unavailable = _events(path)
    if unavailable:
        return _unknown_state(room, seat, unavailable)
    if not events:
        return None
    states, unique, why, _rp = _reduce(events, room)
    if why:
        return _unknown_state(room, seat, why)
    if unique and not _lock_held(room):
        # r4 MIGRATION: a parent-shaped room (r2 actor bits on the
        # snapshots, a markerless stream) folds its bits into typed room
        # markers on this first journaled read — then the bits are retired
        # and never read again. Skipped when the caller already holds the
        # room lock: replay_room's no-restore path folds there instead.
        with chat._room_lock(room) as locked:
            if not locked:
                return _unknown_state(room, seat, "meld lifecycle room lock unavailable")
            why = _migrate_parent_bits_locked(room, seat, states, unique)
        # the helper's tri-state: "folded" is SUCCESS (markers appended), a
        # reason string is UNKNOWN, None is nothing-to-fold. r6b made it
        # tri-state and this caller still read every truthy return as a
        # reason — a first state() read appended the marker then returned
        # status=UNKNOWN reason=folded (r6b exact review).
        if why and why != "folded":
            return _unknown_state(room, seat, why)
        states, unique = _reload_after_append(room, seat)
        if unique is None:
            return _unknown_state(room, seat, states)
    folded = states.get(seat)
    if folded is None:
        return None
    current = pk.read_json(state_path(room, seat), None)
    if (isinstance(current, dict)
            and current.get("journal_seq") == folded["journal_seq"]):
        return current
    repaired = _snapshot(room, seat, folded)
    _write_state(room, seat, repaired)
    return repaired


def state(room, seat):
    if not os.path.exists(lifecycle_path(room)) and os.path.exists(lifecycle_path(room, True)):
        replay_room(room, apply=True)
    repaired = _repair_from_ram(room, seat)
    if repaired is not None:
        return repaired
    d = pk.read_json(state_path(room, seat), None)
    if (isinstance(d, dict) and d.get("journal_seq") is None
            and isinstance(d.get("epoch"), int) and not isinstance(d.get("epoch"), bool)
            and _valid_projection(_projection(d))):
        # A live pre-feature snapshot may be used before the next ordinary
        # flush. Seed its typed RAM migration event on first state read so the
        # next transition has an honest predecessor; disk remains untouched.
        return _transition(room, seat, "legacy-snapshot", d)
    return d if isinstance(d, dict) else None


_CLOSED_SIDE = ("done", "done-mutual", "aborted")


def finished(room):
    """True iff every member of the room's NEWEST round has closed its side.

    CLOSED, NEVER QUIET. A side is closed when its actor's lifecycle status
    is done, done-mutual or aborted; the meld is finished only when EVERY
    member of the newest round -- each actor at that epoch and every peer
    one of them pinned -- holds one. So a convener who closes an invite
    nobody joined has not finished a meld, a pair room whose next round has
    opened is a meld in progress again, and a room that is merely idle is
    never finished for being idle (task/3519: a standing or slow room must
    not read as over). The retirement that reads this adds its own quiet
    bound on top; this answers only whether the meld is over.

    A PURE READ OF THE LIFECYCLE STREAM, RAM first and then the durable
    copy: no snapshot repair, no migration and no lock, so a caller already
    holding the room lock may ask. A stream that is absent, unreadable or
    does not reduce is UNKNOWN, and UNKNOWN is not finished."""
    path = lifecycle_path(room)
    if not os.path.exists(path):
        path = lifecycle_path(room, durable=True)
    events, unavailable = _events(path)
    if unavailable or not events:
        return False
    states, _unique, why, _roomproj = _reduce(events, room)
    if why or not states:
        return False
    epoch = max(s["epoch"] for s in states.values())
    current = {actor: s["projection"] for actor, s in states.items()
               if s["epoch"] == epoch}
    members = set(current)
    for proj in current.values():
        members.update(proj.get("peers") or ())
    return all(m in current and current[m].get("status") in _CLOSED_SIDE
               for m in members)


def _write_state(room, seat, st):
    chat._ensure_dir()
    pk.write_json(state_path(room, seat), st)


def _lock_held(room):
    """True when THIS process already holds the room's flock. Checks the
    in-process re-entrancy tracker first, falling back to Linux /proc/locks."""
    if chat.is_room_locked(room):
        return True
    path = os.path.abspath(os.path.join(chat.chat_dir(), pk.slug(room) + ".lock"))
    try:
        st = os.stat(path)
        key = "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev),
                                st.st_ino)
    except (OSError, AttributeError):
        return False
    me = str(os.getpid())
    try:
        with open("/proc/locks", encoding="ascii") as f:
            for line in f:
                parts = line.split()
                if (len(parts) > 5 and parts[1] == "FLOCK"
                        and parts[3] == "WRITE" and parts[4] == me
                        and parts[5] == key):
                    return True
    except OSError:
        return False
    return False


def _parent_actor_bits(room):
    """The r2 parent format's actor-snapshot provenance bits for ONE room ->
    (restored, reopened, disagreement). r4 MIGRATION: the immediate
    parent wrote _replayed/_reopened on the ACTOR SNAPSHOTS and its stream
    has NO markers; a stream-only reader laundered every such room back to
    durable-live — the original bug reintroduced for all existing data. The
    bits are consulted EXACTLY ONCE (the first journaled read of a
    parent-shaped room), folded into typed stream markers, then retired from
    every snapshot — this function is the migration's input, never a live
    authority. FAIL-CLOSED on actor disagreement: two actors reporting
    different replay states is UNKNOWN, never a guess."""
    restored = reopened = disagreement = False
    seen_restored, seen_reopened = set(), set()
    for name in os.listdir(chat.chat_dir()):
        if ".meld." not in name or not name.endswith(".json"):
            continue
        st = pk.read_json(os.path.join(chat.chat_dir(), name), None)
        if not isinstance(st, dict) or st.get("room") != room:
            continue
        if "_replayed" in st:
            seen_restored.add(bool(st["_replayed"]))
        if "_reopened" in st:
            seen_reopened.add(bool(st["_reopened"]))
    if len(seen_restored) > 1 or len(seen_reopened) > 1:
        disagreement = True
    else:
        restored = seen_restored == {True}
        reopened = seen_reopened == {True}
    return restored, reopened, disagreement


def _migrate_parent_bits_locked(room, seat, states, unique):
    """Fold the r2 parent actor bits into typed ROOM markers — ONCE, with
    the room lock already held by the caller (the append must serialize with
    live transitions exactly like replay_room's restore does). Fires only on
    a parent-shaped room: a journaled stream with NO provenance markers
    whose snapshots still carry the bits. After the fold the stream owns
    the provenance and every snapshot for the room is rewritten WITHOUT the
    bits, so this can never fire twice and the bits are never consulted
    again. Actor disagreement is UNKNOWN — the reason is planted and the
    stream is left untouched (fail-closed, never a guess)."""
    if any(ev["transition"] in ("restored-from-journal",
                                "reopened-after-replay") for ev in unique):
        return None                       # already migrated: nothing to do
    restored, reopened, disagreement = _parent_actor_bits(room)
    if disagreement:
        reason = "parent-format actor snapshots disagree about replay state"
        _UNKNOWN[_cache_key(room)] = reason
        return reason
    if not (restored or reopened):
        return None                       # a live room, no bits: nothing folded
    path = lifecycle_path(room)
    last = unique[-1]

    def marker(seq, kind, at_event):
        return {"v": LIFECYCLE_V, "id": _event_id(room, seq), "room": room,
                "seq": seq, "ts": pk.now_ts(), "actor": at_event["actor"],
                "epoch": at_event["epoch"], "transition": kind,
                "projection": at_event["projection"]}

    pre_fold = list(unique)              # the markerless stream as read

    def mirror_durable(events):
        # the fold must live in the DURABLE journal too: replay_durable
        # (status's first act) rewrites RAM from durable, and a markerless
        # durable copy would erase the migration and launder the room back
        # to live on the very next status read. The guard compares the
        # durable copy against the PRE-FOLD stream (the fold's input), never
        # the post-fold one — the markers are exactly what durable lacks.
        dpath = lifecycle_path(room, True)
        if not os.path.exists(dpath) or chat.journal_write_refusal():
            return True          # an isolated namespace folds its RAM only
        durable, dunavailable = _events(dpath)
        if dunavailable:
            return False
        _ds, dunique, dwhy, _drp = _reduce(durable, room)
        if dwhy or dunique != pre_fold[:len(dunique)]:
            return False
        try:
            with open(dpath, "w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")
        except OSError:
            return False
        return True

    if restored and not reopened:
        # the FROZEN ghost: the restore belongs AFTER the frozen tail — the
        # parent restored a conversation whose last event predates the wipe,
        # and nothing live ever followed. Appending the marker at the tail
        # is the honest reconstruction.
        seq = last["seq"] + 1
        mk = marker(seq, "restored-from-journal", last)
        if not eventledger.append(path, mk):
            reason = "could not append migrated restored-from-journal marker"
            _UNKNOWN[_cache_key(room)] = reason
            return reason
        if not mirror_durable(unique + [mk]):
            reason = "could not mirror migrated marker to the durable journal"
            _UNKNOWN[_cache_key(room)] = reason
            return reason
        seq = mk["seq"]
    if reopened:
        # the MIXED ghost: _reopened=True certifies live activity RESUMED
        # after the restore, but the parent never recorded WHERE the restore
        # happened. Honest reconstruction is IMPOSSIBLE: the reopen marker
        # needs a restore before it AND a live content event between them,
        # and the bits name neither the restore point nor which live event
        # is the resume. Every candidate splice either restates the wrong
        # actor projection (an impossible transition) or places the markers
        # adjacent (a reopen with no intervening content). FAIL-CLOSED:
        # UNKNOWN, never a guessed splice and never a laundering to live.
        reason = ("parent bits claim a reopen the markerless stream cannot "
                  "place honestly (no recorded restore point; the resume "
                  "event is unidentifiable)")
        _UNKNOWN[_cache_key(room)] = reason
        return reason
    states, unique = _reload_after_append(room, seat)
    if unique is None:
        return states                # the reload reason (UNKNOWN planted)
    # the fold succeeded: retire the bits from EVERY snapshot of the room so
    # they are never read again — a marker-bearing stream is the sole
    # provenance authority from here on.
    for name in os.listdir(chat.chat_dir()):
        if ".meld." not in name or not name.endswith(".json"):
            continue
        sp = os.path.join(chat.chat_dir(), name)
        st = pk.read_json(sp, None)
        if not isinstance(st, dict) or st.get("room") != room:
            continue
        if "_replayed" in st or "_reopened" in st:
            st.pop("_replayed", None)
            st.pop("_reopened", None)
            if not isinstance(st.get("journal_seq"), int):
                # a pre-journal parent snapshot has no seq — stamp it so the
                # state-file fallback in state() never re-imports this room
                # as an unjournaled legacy snapshot after the very wipe that
                # motivated the restore.
                st["journal_seq"] = seq
                st.setdefault("idx", 0)
            pk.write_json(sp, st)
    return "folded"                      # markers were appended



def _reload_after_append(room, seat):
    """(states, unique) re-reduced after a migration append; (reason, None)
    with the UNKNOWN planted when the fresh stream refuses to reduce — a
    verb never reports from a stream it has not re-measured."""
    events, unavailable = _events(lifecycle_path(room))
    if unavailable:
        _UNKNOWN[_cache_key(room)] = unavailable
        return unavailable, None
    states, unique, why, _rp = _reduce(events, room)
    if why:
        _UNKNOWN[_cache_key(room)] = why
        return why, None
    return states, unique


def _adopt_durable_markers(room, path, unique):
    """Copy into RAM the restore markers the durable journal holds past RAM's
    tail, so the next live event takes a sequence durable has not spent. The
    caller holds the room lock (the flush reads durable under the same one).

    The flush accepts durable-ahead as the steady state, while the next
    sequence was counted from RAM alone. A live event then took the durable
    marker's seq and event id with different content, neither stream was a
    prefix of the other, and the flush quarantined the room and its chat
    rows. Adopting the markers makes RAM a prefix of durable again, so the
    live event flushes as an extension.

    Only the marker-only shape is adopted (_replay_divergence's rule). An
    isolated namespace or no durable file adopts nothing. A durable journal
    that cannot be read or reduced, or one ahead of RAM by anything but
    restore markers, or forked from it, REFUSES the transition before any
    append (LifecycleError naming why): allocating from RAM there reuses a
    durable seq and id, and the next flush quarantines the room for good.
    True when RAM was appended to."""
    why = _durable_refusal(room, unique)
    if why:
        raise LifecycleError(why)
    dpath = lifecycle_path(room, True)
    if chat.journal_write_refusal() or not os.path.exists(dpath):
        return False
    durable, _unavailable = _events(dpath)
    _ds, dunique, why, _rp = _reduce(durable, room)
    if (why or len(dunique) <= len(unique)
            or dunique[:len(unique)] != unique
            or _replay_divergence(unique, dunique) is not None):
        return False
    for ev in dunique[len(unique):]:
        if not eventledger.append(path, ev):
            raise LifecycleError("could not append meld lifecycle RAM journal")
    return True


def _durable_refusal(room, unique):
    """Why a live transition must not be allocated over `room`'s durable
    journal, or None. The caller holds the room lock and has reduced RAM to
    `unique`. None when there is no durable copy to collide with (an isolated
    namespace, no file), when durable is a prefix of RAM (the flush extends
    it), or when durable is ahead only by restore markers (adopted)."""
    dpath = lifecycle_path(room, True)
    if chat.journal_write_refusal() or not os.path.exists(dpath):
        return None
    durable, unavailable = _events(dpath)
    if unavailable:
        return ("meld lifecycle durable journal UNKNOWN for %s: %s — a live "
                "transition would reuse a sequence it may have spent"
                % (room, unavailable))
    _ds, dunique, why, _rp = _reduce(durable, room)
    if why:
        return ("meld lifecycle durable journal UNKNOWN for %s: %s — a live "
                "transition would reuse a sequence it may have spent"
                % (room, why))
    if dunique == unique[:len(dunique)]:
        return None
    why = _replay_divergence(unique, dunique)
    return ("refusing a live meld transition in %s: %s" % (room, why)) \
        if why else None


def _transition(room, seat, kind, st, cause=None, before_write=None):
    """Append one lifecycle event under the room lock. `before_write` runs
    after every check passed and before the append, still under the lock: a
    caller whose own write must stand with this one (say's chat row) refuses
    there, so a refusal on either side leaves both unwritten."""
    with chat._room_lock(room) as locked:
        if not locked:
            raise LifecycleError("meld lifecycle room lock unavailable for %s" % room)
        path = lifecycle_path(room)
        events, unavailable = _events(path)
        if unavailable:
            raise LifecycleError("meld lifecycle RAM journal unavailable: %s" % unavailable)
        states, unique, why, roomproj = _reduce(events, room)
        if why:
            raise LifecycleError("meld lifecycle RAM journal UNKNOWN: %s" % why)
        if _adopt_durable_markers(room, path, unique):
            events, unavailable = _events(path)
            if unavailable:
                raise LifecycleError("meld lifecycle RAM journal unavailable: %s"
                                     % unavailable)
            states, unique, why, roomproj = _reduce(events, room)
            if why:
                raise LifecycleError("meld lifecycle RAM journal UNKNOWN: %s" % why)
        before = states.get(seat)
        if before is not None and _opens_round(kind, before["epoch"],
                                               st["epoch"]):
            before = None                 # the actor's next round, see _reduce
        seq = len(unique) + 1
        ev = {"v": LIFECYCLE_V, "id": _event_id(room, seq), "room": room,
              "seq": seq, "ts": pk.now_ts(), "actor": seat,
              "epoch": st["epoch"], "transition": kind,
              "projection": _projection(st)}
        if cause is not None:
            ev["cause"] = cause
        why = _validate_event(ev, room)
        if why or not _valid_transition(kind,
                                        before["projection"] if before else None,
                                        ev["projection"], cause):
            raise LifecycleError("refusing impossible meld transition %s: %s" %
                                 (kind, why or "state mismatch"))
        if before_write is not None:
            before_write()
        if not eventledger.append(path, ev):
            raise LifecycleError("could not append meld lifecycle RAM journal")
        # Provenance is a STREAM fact, not a snapshot bit: the FIRST live
        # transition after a restore appends a typed reopened-after-replay
        # marker, so the mixed state (replayed origin + live resumption) is
        # durable ROOM provenance every actor derives identically — it
        # survives snapshot repair and a second wipe+restore, where the r2
        # actor-local _reopened bit died with the snapshot (r3 HIGH).
        # A live event on a REPLAYED meld never launders the ghost back to
        # plain active (meld-1785588958's exact shape); the marker records
        # that live activity RESUMED, and the renderer shows the mixed state.
        if (roomproj["restored"] and not roomproj["reopened"]
                and kind not in ("legacy-snapshot", "restored-from-journal",
                                 "reopened-after-replay")):
            mseq = seq + 1
            marker = {"v": LIFECYCLE_V, "id": _event_id(room, mseq),
                      "room": room, "seq": mseq, "ts": pk.now_ts(),
                      "actor": seat, "epoch": st["epoch"],
                      "transition": "reopened-after-replay",
                      "projection": ev["projection"]}
            if not eventledger.append(path, marker):
                raise LifecycleError("could not append meld lifecycle RAM journal")
        snap = dict(st, journal_seq=seq)
        snap.pop("_replayed", None)         # retired actor-local provenance
        snap.pop("_reopened", None)         # (stream-derived room facts now)
        _write_state(room, seat, snap)
        _UNKNOWN.pop(_cache_key(room), None)
        return snap


def room_name(topic, epoch):
    s = pk.slug(topic)[:24].strip("-") or "topic"
    return "meld-%d-%s" % (epoch, s)


_SEED_RE = re.compile(r"\A\s*\[MELD e:(\d+)\] PROBLEM: ")


def seeds(rows):
    """[(epoch, convener, seedtext, index)] — every round opened in a room, in
    room order. A seed is a PROBLEM row whose `convener=` field names the seat
    that posted it; a room made by `invite` into a fresh name holds one, a
    persistent room (a task's pair meld) holds one per round."""
    out = []
    for i, m in enumerate(rows or ()):
        text, frm = m.get("text") or "", str(m.get("from") or "")
        sm = _SEED_RE.match(text)
        if not sm or not frm or m.get("react"):
            continue
        conv = re.findall(r"convener=([\w.-]+) invited=", text)
        if conv and conv[-1] != frm:
            continue                      # a seed in another seat's name
        out.append((int(sm.group(1)), frm, text, i))
    return out


def latest_seed(rows):
    """(epoch, convener, seedtext) of the room's NEWEST round, or (None, None,
    None). A room with no PROBLEM-shaped row falls back to its first
    epoch-marked row, which is what every reader took before a room could
    hold more than one round."""
    found = seeds(rows)
    if found:
        epoch, conv, text, _i = max(found, key=lambda s: (s[0], s[3]))
        return epoch, conv, text
    for m in rows or ():
        em = _EPOCH_RE.search(m.get("text") or "")
        if em and m.get("from"):
            return int(em.group(1)), str(m["from"]), m.get("text") or ""
    return None, None, None


def _room_epoch_max(room):
    """The newest epoch any row or lifecycle event of `room` names, or 0."""
    best = 0
    rows, _total = chat.read(room)
    for m in rows:
        em = _EPOCH_RE.search(m.get("text") or "")
        if em:
            best = max(best, int(em.group(1)))
    events, _unavailable = _events(lifecycle_path(room))
    for ev in events or ():
        ep = ev.get("epoch") if isinstance(ev, dict) else None
        if isinstance(ep, int) and not isinstance(ep, bool):
            best = max(best, ep)
    return best


#: A joiner's share of carried history: this fraction of its context window,
#: at four bytes a token, clamped. A 115072-token seat gets 4602 bytes; a
#: 1M-token seat gets the ceiling; a seat with no stamp gets the floor. The
#: rest stays one `chat read` away.
DIGEST_WINDOW_FRACTION = 0.01
DIGEST_FLOOR, DIGEST_CEILING = 1024, 8192


def digest_budget(window=None):
    """(bytes, window_tokens | None) a joiner may take of a room's earlier
    rounds.

    The window is the joiner's OWN stamp (CLAUDE_CODE_MAX_CONTEXT_TOKENS, set
    at launch for every seat whose model is not the harness default). A seat
    with NO stamp, or an unreadable one, gets the FLOOR and a window of None:
    its size is unknown, and guessing it large is how a small-window seat
    would take a digest it cannot hold (the integrator's ruling). Carrying a
    whole room into a small window is how a pair meld would cause the
    compaction it exists to prevent, so no answer here is ever the log."""
    if window is None:
        try:
            window = int(os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
                         or 0)
        except ValueError:
            window = 0
        if window < 1:
            return DIGEST_FLOOR, None
    window = max(1, int(window))
    raw = int(window * 4 * DIGEST_WINDOW_FRACTION)
    return max(DIGEST_FLOOR, min(DIGEST_CEILING, raw)), window


def history_digest(room, rows, epoch, budget=None):
    """[lines] — the rounds of `room` before `epoch`, newest first, one line
    each, cut at `budget` bytes, and ALWAYS the pointer to the whole log.

    Each line names the round's pair, its problem head and how it ended (the
    parties' MELD OUTCOME, or open). It is a digest, never the log: the log
    is `helm chat read --room`, and a line that does not fit is counted, not
    carried."""
    from . import review_door
    window = True
    if budget is None:
        budget, window = digest_budget()
    prior = [s for s in seeds(rows) if s[0] < epoch]
    total = sum(len((m.get("text") or "").encode("utf-8")) for m in rows)
    pointer = "the whole log (%d rows, %d bytes): helm chat read --room %s" % (
        len(rows), total, room)
    if not prior:
        return [pointer]
    heading = "%d earlier round(s) in this room, newest first (a digest " \
        "bounded to %d bytes%s):" % (
            len(prior), budget,
            "" if window else "; no window stamp, so the smallest")
    omitted = "  (%d earlier round(s) not shown here)" % len(prior)
    reserve = sum(len(x.encode("utf-8")) + 1
                  for x in (heading, omitted, pointer))
    lines, used, cut = [], 0, 0
    for ep, conv, text, _i in sorted(prior, key=lambda s: -s[0]):
        got = review_door.room_outcome(room, rows=rows, epoch=ep)
        head = text.split("PROBLEM:", 1)[-1].split(" | convener=", 1)[0]
        head = " ".join(head.split())
        if len(head) > 110:
            head = head[:109] + "…"
        ended = got["outcome"] or "open"
        if got.get("tip"):
            ended += " tip " + got["tip"][:12]
        line = "  round e:%d %s: %s -> %s" % (
            ep, "+".join(chat._dsan(p) for p in got["parties"] or [conv]),
            head, ended)
        size = len(line.encode("utf-8")) + 1
        if reserve + used + size > budget:
            cut += 1
            continue
        lines.append(line)
        used += size
    out = [heading] + lines
    if cut:
        out.append("  (%d earlier round(s) not shown here)" % cut)
    out.append(pointer)
    return out


def _post(text, room, seat, marker=None):
    """Every meld row rides the v1 RAM append unsigned — the latency-pure
    path by law (a node round-trip mid-meld dilutes the preset's one axis)."""
    return chat.post(text, room=room, who=seat, sign=False,
                     meld_marker=marker)


BUSY_REVIEWS = 2       # open review rows that make an invitee a busy reader
BUSY_AGE_S = 900       # ... once each has waited this long (DESIGN 3.4.1)


def _reach(peers, now=None):
    """{peer: the line saying why this invite may never become its turn}.

    INVITE ONLY A LIVE SEAT (meld program A3, DESIGN 3.4.1). The wake line in
    `invite` promised every tracked peer "never lost, only delayed", and an
    IDLE seat with no live beacon never takes that turn: the spiral rung once
    prescribed a meld with gemini while the census read it DEAF-IN-EFFECT.

    A WARNING, NEVER A REFUSAL. A seat with no live beacon still receives the
    row at its next tool boundary when it is mid-turn (the hook path
    test_invite_delivers_to_tracked_peer drives), so a refusal would block a
    meld that can happen. The convener decides, with the verdict in hand.

    READ, NEVER RE-MEASURED. The verdict is the attendance register the
    beacons timer writes onto each roster row, and the owed rows are the
    stop-facts resident's frontier: a roster read and a facts read, measured
    at 1.5 ms and ~100 ms. The first cut ran a fresh census (0.25-2 s, a
    process-table sweep) and a ledger fold (55 s right after a land), and an
    invite is the moment a meld should be instant.

    UNKNOWN SAYS NOTHING. No register row, one older than two census
    intervals, an UNPROVEN verdict, or stop-facts that are not exact leave a
    peer out of the answer: an unreadable or stale reading never becomes a
    claim about a seat."""
    from . import beacons, seats, stopfacts
    now = time.time() if now is None else now
    out = {}
    try:
        roster = seats.roster() or {}
    except Exception:                        # noqa: BLE001 — an unreadable
        roster = {}                          # roster names no seat
    asleep = (beacons.DEAF, beacons.DEAF_IN_EFFECT, beacons.VACANT,
              beacons.MISROUTED, beacons.RESTING)
    for p in peers:
        att = (roster.get(p) or {}).get("attendance")
        at = att.get("at") if isinstance(att, dict) else None
        if not isinstance(at, (int, float)) \
                or now - at > 2 * beacons.INTERVAL_S:
            continue
        if att.get("state") in asleep:
            out[p] = ("WARNING: %s is %s (%s, read %ds ago), so nothing wakes "
                      "it: an idle seat never sees this invite, and a busy one "
                      "sees it only at its next tool boundary"
                      % (chat._dsan(p), att["state"],
                         att.get("why") or "no reason given",
                         max(0, int(now - at))))
    try:
        owed, why = stopfacts.read().owed_pair(peers[0])
    except Exception:                        # noqa: BLE001 — unreadable facts
        owed, why = {}, "unreadable"         # say nothing
    if why:
        return out
    stale = {}
    for r in owed.values():
        who = str(r.get("recipient") or "")
        if r.get("kind") != "review" or who not in peers or who in out:
            continue
        at = pk.parse_ts_epoch(r.get("ts"))
        if at is not None and now - at >= BUSY_AGE_S:
            stale[who] = stale.get(who, 0) + 1
    for who, n in stale.items():
        if n >= BUSY_REVIEWS:
            out[who] = ("WARNING: %s holds %d open review rows older than %d "
                        "min, so this meld queues behind them"
                        % (chat._dsan(who), n, BUSY_AGE_S // 60))
    return out


def _opening_lock_name(room):
    """A fixed-width sibling lock key that can never truncate to `room`."""
    return "meld-open-" + hashlib.blake2b(
        str(room).encode("utf-8"), digest_size=16).hexdigest()


def invite(peer, topic, seat=None, via="meld", room=None, ring=None,
           _round_topic=None, _opened=None, _round_marker=None):
    """(room, lines) — open a meld/standup: seed the problem ([HOLD], discipline
    included so joiners need no skill file), then the @peers invite HEAD-first
    (clip-proof). `peer` is ONE seat or a comma/space list (standup = the
    informal 2+ species, owner canon 2026-07-23): the seed carries
    `invited=<set>`, the pinned SET that is the anti-hijack allowlist — join
    refuses any seat not in it, recv accepts READY/chunks only from members. A
    1-element set IS the 2-party pinned pair (strict backward-compat). Each
    untracked peer gets its OWN loud no-delivery-lane warning.

    `room` NAMES A PERSISTENT ROOM (a task's pair meld): this opening is then
    the room's next ROUND, not a new room. Its epoch is strictly newer than
    every epoch the room has seen, so the fence retires the rounds before it,
    the exchange cap counts this round only, and the history stays in the one
    room. `ring` says what already woke the peers (a dispatch row's DM): the
    invite row then carries NO @mention, because one wake is the whole budget.
    `_round_topic` builds a persistent room's topic from the locked count of
    prior rounds; `_opened` receives that exact count and epoch before unlock.
    `_round_marker` makes that opening idempotent under the same lock: an exact
    marker immediately after `PROBLEM:` reuses its existing round instead of
    appending another one."""
    seat = seat or _self_seat()
    invited = []
    for raw in str(peer or "").replace(",", " ").split():
        p = raw.lstrip("@")
        if not p:
            continue
        try:  # THE arg-side ingestion seam — a hostile/garbled peer name never
            p = home.validate_seat_arg(p)  # becomes a seed field or mention
        except home.SeatNameError as e:
            raise SystemExit("helm chat %s: %s" % (via, e))
        if p == seat:
            raise SystemExit("helm chat %s: cannot invite yourself (%s) — you are "
                             "the convener, not an invitee" % (via, seat))
        if p not in invited:               # dedupe, order-preserving
            invited.append(p)
    if not invited:
        raise SystemExit("helm chat %s: invite wants at least one peer seat name" % via)
    members = set(invited) | {seat}
    epoch = int(time.time())
    prior_rounds = 0
    if room is not None and (not re.fullmatch(
            r"meld-\d+-[a-z0-9-]{1,64}", str(room)) or pk.slug(room) != room):
        raise SystemExit("helm chat %s: --room %r is not a meld room name"
                         % (via, room))
    if _round_topic is not None and (room is None or not callable(_round_topic)):
        raise SystemExit("helm chat %s: a round topic builder requires one "
                         "persistent room" % via)
    if _round_marker is not None and (room is None or _round_topic is None
                                      or not str(_round_marker).strip()):
        raise SystemExit("helm chat %s: a round marker requires one persistent "
                         "room and topic builder" % via)

    def checked_topic(value):
        value = str(value or "").strip()
        if not value:
            raise SystemExit(
                "helm chat %s: MELD-EMPTY-PROBLEM — invite wants a non-empty "
                "problem statement before it can create a room" % via)
        outside = _outside_member_mentions(value, members)
        if outside:
            addressed = ", ".join("@" + chat._dsan(x) for x in outside)
            pinned = ", ".join(sorted(chat._dsan(x) for x in members))
            raise SystemExit(
                "helm chat %s: MELD-MEMBERSHIP-MISMATCH addressed=%s "
                "pinned={%s} — invite every addressed participant, or use a "
                "plain name for a non-participant reference"
                % (via, addressed, pinned))
        return value

    if _round_topic is None:
        topic = checked_topic(topic)
    # BROADCAST TOKENS in invite topics wake nobody — refuse them outright.
    _broadcast_token_refusal(topic)
    # A PERSISTENT ROOM'S ROUND OPEN IS ONE CRITICAL SECTION. Each post takes
    # the room's chat lock itself and releases it, so a sibling lock held
    # across the whole open serializes epoch allocation, both protocol rows
    # and the lifecycle transition. Without it two concurrent dispatches
    # read the same maximum and minted the same epoch.
    lock_name = _opening_lock_name(room) if room is not None else None
    opening = chat._room_lock(lock_name) if lock_name is not None \
        else contextlib.nullcontext()
    reused = False
    d_peers = [chat._dsan(p) for p in invited]
    with opening as opening_locked:
        if lock_name is not None and not opening_locked:
            raise LifecycleError("persistent-room opening lock unavailable")
        if room is None:
            room = room_name(topic, epoch)
            if state(room, seat):
                raise SystemExit("helm chat %s: state already exists for room %s"
                                 % (via, room))
        else:
            room_rows, _total = chat.read(room)
            found = seeds(room_rows)
            marker = str(_round_marker or "")
            for n, (old_epoch, old_convener, old_seed, _index) in enumerate(found):
                start = _SEED_RE.match(old_seed)
                same_parties = old_convener == seat \
                    and set(_invited_seats(old_seed)) == set(invited)
                if marker and same_parties and start \
                        and old_seed[start.end():].startswith(marker):
                    prior_topic = old_seed[start.end():].rsplit(
                        " | convener=", 1)[0]
                    epoch, prior_rounds, topic, reused = \
                        old_epoch, n, prior_topic, True
                    break
            if not reused:
                epoch = max(epoch, _room_epoch_max(room) + 1)
                prior_rounds = len(found)
        if not reused and _round_topic is not None:
            topic = checked_topic(_round_topic(prior_rounds))
        if isinstance(_opened, dict):
            _opened.update(epoch=epoch, prior_rounds=prior_rounds, topic=topic,
                           reused=reused)
        seed = ("[MELD e:%d] PROBLEM: %s | convener=%s invited=%s cap=%d "
                "recv-timeout=%ds | MELD DISCIPLINE: reply FAST with what you "
                "already know; a fork that needs research is NOT a meld — close "
                "[DONE] with the async continuation. [HOLD]"
                % (epoch, topic, seat, ",".join(invited), _cap(),
                   int(_recv_timeout())))
        if not reused:
            _post(seed, room, seat)
        # peers validated above; laundering the EMITTED copies stays as
        # defense-in-depth beneath the seam. topic is CONTENT, left full-fidelity
        # by the class rule (only identity is laundered).
        # THE RING IS NOT REPEATED: with `ring` the invite head names the peers
        # bare, so the row is the room's record of how to join and wakes nobody.
        inv = ("%s [MELD-INVITE e:%d] room=%s JOIN: helm chat %s join %s "
               "THEN: helm chat %s recv %s || topic: %s"
               % (" ".join(("%s" if ring else "@%s") % d for d in d_peers),
                  epoch, room, via, room, via, room, topic))
        if ring:
            inv += " || woken by: %s" % " ".join(str(ring).split())[:160]
        invite_mark = "[MELD-INVITE e:%d] room=%s " % (epoch, room)
        if not reused or not any(
                str(message.get("from") or "") == seat
                and invite_mark in str(message.get("text") or "")
                and not message.get("react")
                for message in chat.read(room)[0]):
            _post(inv, room, seat)
        # A durable seed is only the first third of an opening. A retry repairs
        # an absent invite and lifecycle transition under this same lock; it does
        # not mistake the first append for a completed round.
        current_state = state(room, seat)
        current_epoch = current_state.get("epoch") \
            if isinstance(current_state, dict) else None
        if not isinstance(current_epoch, int) or current_epoch < epoch:
            # the convener's accept-set IS the invited set; `peer` kept = invited[0]
            # so any legacy single-peer reader still resolves (backward-compat).
            st = {
                "room": room, "epoch": epoch, "role": "convener", "self": seat,
                "peer": invited[0], "peers": list(invited), "idx": 0,
                "exchanges": 0, "cap": _cap(), "status": "invited",
                "created": pk.now_ts(), "done_peers": [], "spoke_peers": []}
            _transition(room, seat, "invited", st)
    from . import seats
    wakelines, tracked = [], []
    for p, d in zip(invited, d_peers):
        if ring:
            wakelines.append("no @mention posted: %s is woken by %s"
                             % (d, " ".join(str(ring).split())[:160]))
            continue
        try:  # the wake-truth line: honest per EACH peer's ACTUAL lane
            on = seats.seat_scope(p)["tracked"]
        except Exception:
            on = False
        if on:
            tracked.append(p)
        else:
            wakelines.append(
                "WARNING: %s is NOT on the chat roster — no delivery lane "
                "exists; nothing wakes it until it joins or reads %s itself"
                % (d, room))
    reach = _reach(tracked) if tracked else {}
    for p in tracked:
        wakelines.append(reach.get(p) or (
            "the invite is a durable row — %s wakes at its next tool boundary "
            "or beacon (never lost, only delayed)" % chat._dsan(p)))
    if reach:
        # THE COMMAND, NOT A NAME. `route.answer` folds the ledger, measured at
        # 54 s right after a land, and an invite must not pay that to print one
        # seat: the convener runs the verb when the warning is one it acts on.
        wakelines.append("find a live reader: helm route review")
    if prior_rounds:
        wakelines.append("round %d of this room: the %d before it stay in the "
                         "room (helm chat read --room %s)"
                         % (prior_rounds + 1, prior_rounds, room))
    return room, [
        "MELD-%s room=%s epoch=%d peers=%s"
        % ("REUSED" if reused else "INVITED", room, epoch,
           ",".join(d_peers))] + wakelines + [
        "next: helm chat %s recv %s   (returns on the FIRST READY; then speak "
        "the first chunk: helm chat %s say %s --marker YIELD \"...\")"
        % (via, room, via, room)]


def join(room, seat=None, via="meld"):
    """(lines) — join a meld: parse epoch + convener + invited from the seed,
    post the control-only READY (@convener — the wake-back; a READY that
    lands silently strands GO forever, a predecessor's live incident), state
    joiner/active with idx=0 so the seeded problem is the first recv chunk.
    REFUSES a seat the seed did not invite (a meld
    convened for one seat was consummated by another with zero warning —
    downstream, the convener's DONE @mentioned a ghost). Pre-pin seeds
    (no invited= field) grandfather in unpinned."""
    seat = seat or _self_seat()
    rows, _total = chat.read(room)
    # THE NEWEST ROUND. A persistent room holds one seed per round; the first
    # one is a round the fence already retired.
    epoch, convener, seedtext = latest_seed(rows)
    if epoch is None:
        raise SystemExit("helm chat %s: no meld seed in room %s — was it "
                         "invited? (helm chat read --room %s)" % (via, room, room))
    if convener == seat:
        raise SystemExit("helm chat %s: %s convened this meld — recv, don't "
                         "join" % (via, seat))
    invited = _invited_seats(seedtext)     # the pinned SET (grandfathers [])
    if invited and seat not in invited:
        raise SystemExit(
            "helm chat %s: room %s was convened for {%s}, not %s — join under an "
            "invited name (HELM_CHAT_NAME=%s or --seat <one of the invited>). "
            "A non-invited join is how melds got hijacked/mis-consummated "
            "(the invited-pair invariant)." % (via, room, ",".join(invited),
                                          chat._dsan(seat), chat._dsan(seat)))
    # convener is a SEED ROW's from-field — planted/foreign (a pre-fix or
    # foreign-node seed) it may carry ESC/bidi. Launder it before it enters
    # BOTH the MELD-JOINED display line AND the posted READY text (chat._fmt
    # renders posted text raw fleet-wide — the identity-into-text bypass). The
    # RAW convener stays in the accept-set for recv's matching, mirroring
    # recv's chat._dsan(frm) pattern: launder the EMIT, keep the KEY raw.
    # A joiner accepts from the convener AND every OTHER invited peer — a 2+
    # standup is a full mesh within the pinned set; a size-1 invited leaves
    # just the convener, the 2-party pair.
    peers = [convener] + [p for p in invited if p != seat]
    d_convener = chat._dsan(convener)
    _post("@%s [MELD e:%d] READY:%d (%s joined %s)"
          % (d_convener, epoch, epoch, seat, room), room, seat)
    st = {
        "room": room, "epoch": epoch, "role": "joiner", "self": seat,
        "peer": convener, "peers": peers, "idx": 0, "exchanges": 0,
        "cap": _cap(), "status": "active", "created": pk.now_ts(),
        "done_peers": [], "spoke_peers": []}
    _transition(room, seat, "accepted", st)
    # THE HISTORY IS IN THE ROOM, and a joiner gets it as a DIGEST bounded to
    # its own window plus the pointer to the whole log — never the log, which
    # in a long task would fill a small window on arrival.
    history = history_digest(room, rows, epoch) if len(seeds(rows)) > 1 \
        else []
    return ["MELD-JOINED room=%s epoch=%d convener=%s" % (room, epoch, d_convener),
            "next: helm chat %s recv %s   (the seeded problem statement "
            "is your first chunk)" % (via, room)] + history


def _fall_lines(room, reason, st, via="meld"):
    return ["MELD-BOUND room=%s reason=%s exchanges=%d/%d"
            % (room, reason, st.get("exchanges", 0), st.get("cap", _cap())),
            "the synchronous window is over — fall to async NOW: post your "
            "current state + the required next action as the closing chunk "
            "(it @mentions the peer; the durable row guarantees it lands):",
            "  helm chat %s say %s --marker DONE \"<state + next action>\""
            % (via, room)]


def _quiet_lines(room, bound, st, via="meld"):
    """The recv timeout on a meld that has RUN: no reply yet, so recv again.

    `_fall_lines` says "the synchronous window is over — fall to async
    NOW", then post DONE. That is wrong here: the bound is this verb's poll
    limit, not the peer leaving. Measured over 104 meld turns in 44 rooms,
    18% of peer replies took longer than 90 s, and the Claude-side 90th
    percentile was 533 s. That text told the waiting side to close a meld
    its peer was still answering.

    It stays EXIT_BOUND: the bound really did fire. Only the next action
    changes. The exchange CAP is a real end and keeps `_fall_lines`."""
    return [
        "MELD-BOUND room=%s reason=timeout waited=%ds exchanges=%d/%d"
        % (room, int(bound), st.get("exchanges", 0), st.get("cap", _cap())),
        "no reply yet: recv again. The meld is still open and nothing was "
        "consumed; a peer reply can take several minutes.",
        "    helm chat %s recv %s" % (via, room),
        "  only if the peer is genuinely out, close it as async:",
        "    helm chat %s say %s --marker DONE \"<state + next action>\""
        % (via, room),
    ]


def _nojoin_lines(room, bound, unjoined, st, via="meld"):
    """The timeout that fired while NOBODY had joined yet (task/2445).

    `_fall_lines` says "the synchronous window is over", and for a meld that
    RAN that is true and useful. Fired at a convener whose peer has not joined,
    it is false in the way that costs the most: there was no window. Not one
    chunk arrived, so there is nothing to fall FROM, and the instruction it
    gives — post your current state as the CLOSING chunk — closes a meld that
    never opened, against a peer who is about to walk into a sealed room.

    Reported by a seat USING helm, twice, against a codex peer that was still
    joining: a family wake is not bounded by this verb's poll bound, and the
    convener was told the peer had gone when the peer was on its way.

    The state already knew: a convener sits at status `invited` until the first
    READY flips it, and every joiner is recorded as it arrives, so the peers
    who are genuinely still out can be NAMED rather than summarised. Naming
    them is the difference between "retry" and "chase that one seat".

    This stays EXIT_BOUND. The bound really did fire and a caller that treats
    the code as "recv returned without a chunk" stays correct; what changes is
    that the seat is told which of the two very different things happened."""
    who = ", ".join(chat._dsan(p) for p in unjoined) or "the invited peer"
    return [
        "MELD-NOJOIN room=%s waited=%ds not-joined=%s"
        % (room, int(bound), who),
        "the synchronous window has NOT closed — it never opened: no chunk has "
        "arrived and %s has not joined yet. A cross-family wake can outlast "
        "this bound." % who,
        "  keep waiting (the state is intact, nothing was consumed):",
        "    helm chat %s recv %s" % (via, room),
        "  or check the invite actually woke them:",
        "    helm chat read --room %s" % room,
        "  only if they are genuinely out, close it as async:",
        "    helm chat %s say %s --marker DONE \"<state + next action>\""
        % (via, room),
    ]


def _floor_line(marker, frm, accept, st, room, via):
    """The floor, HONESTLY (task #21 — the first real-use finding of the 3-way
    standup). In a PAIR the floor is genuinely exclusive: the speaker yielded
    it to you, nobody else can take it, so the original wording stands. In a
    2+ standup it is NOT exclusive — 'floor: YOURS' claimed an exclusivity the
    protocol never had (any member may speak next), and a claim the substrate
    cannot keep is the bug. We do NOT mint a floor TOKEN to make the claim
    true: a token is a lock, and a lock needs lease/timeout/steal or one dead
    member wedges the room forever (the stuck-noun class). The value of a
    multi-party standup is that whoever HAS the answer speaks next, not whose
    turn it is — so the fix is to report the truth and name who is still out."""
    others = sorted(x for x in accept if x != frm)
    # "still to hear from" must exclude everyone who has SPOKEN, not only those
    # who have DONE (a re-gate): a member who already YIELDed their view
    # was being listed as unheard, so the line nagged for input already given.
    heard = set(st.get("done_peers") or []) | set(st.get("spoke_peers") or [])
    pending = [x for x in others if x not in heard]
    speak = ("helm chat %s say %s --marker YIELD|HOLD|DONE \"...\"" % (via, room))
    if len(accept) <= 1:                  # the pinned pair — exclusive, unchanged
        return ("floor: YOURS — reply fast with what you already know: %s" % speak
                if marker == "YIELD" else "floor: PEER'S — more coming; recv again")
    if marker != "YIELD":                 # HOLD: the speaker keeps going
        return ("floor: %s's — more coming; recv again (%d member(s) in this "
                "standup)" % (chat._dsan(frm), len(accept) + 1))
    who = ", ".join(chat._dsan(x) for x in pending) or "no one else"
    return ("floor: OPEN — %s yielded to the room (%d members). Speak if you "
            "have it, HOLD if more is coming, DONE when you are out; still to "
            "hear from: %s. %s" % (chat._dsan(frm), len(accept) + 1, who, speak))


def _ignored_note(ignored, peers):
    """One terse line when the loop skipped floor-shaped rows from OUTSIDE the
    pinned set — silence here is how the unpinned-peer hijack went unnoticed."""
    if not ignored:
        return []
    who = ", ".join(sorted({chat._dsan(x) for x in ignored}))
    members = ", ".join(sorted({chat._dsan(x) for x in peers}))
    return ["(pinned set: ignored %d floor/READY row(s) from non-member "
            "seat(s) %s — this meld speaks only with {%s})"
            % (len(ignored), who, members)]


def _broadcast_token_refusal(text):
    """Refuse exact broadcast @tokens (@all, @fleet, @everyone) in meld prose.

    They wake nobody in a meld context, so using them only misleads the writer.
    Prose like "@all-online" or "@no-one-sure" stays admitted (not exact match).
    """
    from . import seats
    for tok in seats._BROADCAST.findall(text or ""):
        raise SystemExit(
            "helm chat %s: REFUSING broadcast token @%s in meld prose — "
            "broadcast tokens wake nobody in a meld context; use a seat "
            "name or plain prose" % ("meld", tok))


def _outside_member_mentions(text, members):
    """Direct @addresses not named by this meld's immutable member set.

    Use the delivery lane's ONE mention grammar: an @name is an address, not
    prose. Outsider-authored rows stay ignored (the anti-hijack invariant), but
    a PINNED member cannot assign a load-bearing YIELD/HOLD role to somebody the
    decision transcript refuses to hear. Case-fold because delivery does too.

    Only an @token that can WAKE a seat is an address (task/3247). Prose like
    "@all-online" or "@no-one-sure" matches the token regex but names no
    seat, so it is not a mention. A token wakes what delivery's `seat_names`
    answers for: a roster key, OR a live rename alias of one — the renamed
    seat still wakes on its old name inside the window, so an alias of an
    outsider is an outside address and an alias of a member is the member.

    AN UNREADABLE ROSTER PROVES NO TOKEN IS PROSE. Delivery never consults
    the roster to wake a seat on its own name, so a roster this read cannot
    see (`roster_acquired` failed) would otherwise admit every outsider at
    once; there every non-member token counts, as before the roster gate. A
    MISSING roster is proven empty, and a different answer from unreadable.
    """
    from . import seats
    known = {str(x).casefold() for x in members if x}
    rows, failed = seats.roster_acquired()
    keys = {str(k).casefold() for k in rows}
    out = {}
    for tok in seats._MENTION_TOKEN.findall(text or ""):
        cf = tok.casefold()
        if cf in known or seats._BROADCAST.search("@" + tok):
            continue
        if not failed:
            alias, _until = seats.live_alias(tok, rows)
            seat = cf if cf in keys else str(alias or "").casefold()
            if not seat or seat in known:
                continue
        out.setdefault(cf, tok)
    return sorted(out.values(), key=str.casefold)


def _membership_mismatch_lines(room, names, members, via="meld"):
    addressed = ", ".join("@" + chat._dsan(x) for x in names)
    pinned = ", ".join(sorted(chat._dsan(x) for x in members))
    return [
        "MELD-MEMBERSHIP-MISMATCH room=%s addressed=%s pinned={%s}"
        % (room, addressed, pinned),
        "an accepted member assigned a floor role outside the pinned set — "
        "this room cannot converge over a transcript that refuses that role",
        "re-convene with every addressed participant in the invite set; use "
        "plain names for non-participant references (not @addresses)",
    ]


def recv(room, timeout=None, seat=None, poll=MELD_POLL, via="meld"):
    """(code, lines) — the blocking marker-aware read: return the next PEER
    chunk carrying a real floor marker; skip own/unattributable rows (F2),
    stale epochs (the fence), control echoes (F1), markerless chatter,
    reactions, and — the pinned-pair law — EVERY row from a seat that is not
    state["peer"] (recv accepted READY and chunks from
    ANY non-self sender; a meld convened for one seat was consummated by
    another, and a third seat could kill any meld with a forged DONE/ABORT).
    Bounds are behavior: cap/timeout → (EXIT_BOUND, fall-to-async
    instruction); [ABORT] → (EXIT_ABORT, loud). READY returns exactly once —
    to the invited convener, flipping it active (the GO moment). After your
    own [DONE] recv becomes the COUNTERSIGN WATCH (closer-went-blind,
    live-fire invariant): it returns the peer's closing DONE/ABORT — flipping
    done-mutual and counting the exchange — instead of refusing."""
    seat = seat or _self_seat()
    st = state(room, seat)
    if st is None:
        return 2, ["helm chat %s: no meld state for %s — invite or join first"
                   % (via, room)]
    if st.get("status") == "UNKNOWN":
        return 2, ["helm chat %s: meld state for %s is UNKNOWN — %s"
                   % (via, room, st.get("_unknown") or "lifecycle journal unreadable")]
    if st["status"] == "aborted":
        return 2, ["helm chat %s: room %s is ABORTED — the meld is dead" % (via, room)]
    if st["status"] == "done-mutual":
        return 2, ["helm chat %s: room %s is sealed (done-mutual) — both sides "
                   "closed" % (via, room)]
    closing = st["status"] == "done"      # countersign watch, not a refusal
    if st["status"] == "peer-done":
        return 2, ["helm chat %s: peer already left room %s — nothing further "
                   "arrives; close: helm chat %s say %s --marker DONE "
                   "\"<closing state>\"" % (via, room, via, room)]
    if not closing and st["exchanges"] >= st.get("cap", _cap()):
        return EXIT_BOUND, _fall_lines(room, "cap", st, via)
    # the pinned SET this seat accepts from — the 2026-07-23 hijack-guard,
    # widened to standup's 2+. `peers` is authoritative; fall back to the lone
    # `peer` for state written before the set field existed (backward-compat).
    accept = set(st.get("peers") or ([st["peer"]] if st.get("peer") else []))
    ignored, late = [], 0
    bound = _recv_timeout() if timeout is None else timeout
    deadline = time.time() + bound
    while True:
        rows, total = chat.read(room, since=st["idx"])
        base = st["idx"]                  # stable — idx mutates in the loop
        for i, m in enumerate(rows):
            here = base + i + 1
            text, frm = m.get("text") or "", str(m.get("from") or "")
            if m.get("react") or not text or not frm or frm == seat:
                st["idx"] = here          # own/unattributable/reaction: skip
                continue                  # fail-closed (F2)
            em = _EPOCH_RE.search(text)
            if em and int(em.group(1)) != st["epoch"]:
                st["idx"] = here          # stale epoch: a dead meld's row
                continue                  # can never replay (the fence)
            if accept and frm not in accept:  # outside the pinned SET — noted, never obeyed
                if _READY_RE.search(text) or _MARKER_RE.search(text):
                    ignored.append(frm)
                st["idx"] = here
                continue
            if _READY_RE.search(text) and not _MARKER_RE.search(text):
                st["idx"] = here
                # EVERY READY is recorded, not only the one that flips the
                # convener active. In a 2+ standup the second and later joins
                # were skipped as control echoes, so the state could not say
                # which members were still out — and the timeout line had to
                # summarise where it could have named.
                st["joined_peers"] = sorted(
                    set(st.get("joined_peers") or []) | {frm})
                if st["role"] == "convener" and st["status"] == "invited":
                    st["status"] = "active"
                    st = _transition(room, seat, "ready", st, cause=frm)
                    return 0, ["[meld %s e:%d] READY — %s is in. You have "
                               "the floor: helm chat %s say %s --marker "
                               "YIELD \"<first chunk>\""
                               % (room, st["epoch"], chat._dsan(frm), via, room)] \
                        + _ignored_note(ignored, accept)
                continue                  # control echo elsewhere (F1)
            mk = _MARKER_RE.search(text)
            if not mk:
                st["idx"] = here          # markerless chatter is not a chunk
                continue
            marker = mk.group(1)
            if closing and marker in ("YIELD", "HOLD"):
                st["idx"] = here          # late chunk after your DONE — the
                late += 1                 # watch wants only the countersign
                continue
            # THE PINNED-ROLE HOLE (live 2026-08-08): a convener invited only
            # one seat, then assigned a codex seat the load-bearing verifier role
            # in a YIELD chunk. recv ignored codex's answers as non-member rows
            # and still reported convergence. Outsider rows MUST remain unable
            # to abort (anti-hijack), so the discriminator is the AUTHOR: an
            # accepted member's own floor chunk cannot address a non-member.
            # DONE/ABORT may name an async handoff; only live floor roles bind.
            if marker in ("YIELD", "HOLD"):
                outside = _outside_member_mentions(text, accept | {seat})
                if outside:
                    st["idx"], st["exchanges"] = here, st["exchanges"] + 1
                    st["spoke_peers"] = sorted(
                        set(st.get("spoke_peers") or []) | {frm})
                    st["status"] = "aborted"
                    st = _transition(room, seat, "peer-membership-mismatch", st,
                                     cause=frm)
                    return EXIT_ABORT, _membership_mismatch_lines(
                        room, outside, accept | {seat}, via)
            st["idx"], st["exchanges"] = here, st["exchanges"] + 1
            # a real floor chunk = this member has now SPOKEN (feeds the
            # still-to-hear set, which must not nag for input already given)
            st["spoke_peers"] = sorted(set(st.get("spoke_peers") or []) | {frm})
            if st["status"] == "invited":
                st["status"] = "active"       # first accepted peer chunk activates
            if marker == "ABORT":
                st["status"] = "aborted"
                st = _transition(room, seat, "peer-abort", st, cause=frm)
                return EXIT_ABORT, ["[meld %s e:%d] ABORT from %s — the meld "
                                    "is dead, fail-loud:" % (room, st["epoch"], chat._dsan(frm)),
                                    "  %s" % text] + _ignored_note(ignored, accept)
            if marker == "DONE":
                # a 2+ standup does NOT seal on ONE member's DONE — record who
                # closed and keep the floor open until EVERY member has (or you
                # close). A size-1 accept-set seals at once = the 2-party pair.
                donep = set(st.get("done_peers") or [])
                donep.add(frm)
                st["done_peers"] = sorted(donep)
                remaining = [p for p in accept if p not in donep]
                if remaining:
                    st = _transition(room, seat, "peer-done", st, cause=frm)
                    tail = ("%s closed (%d of %d in) — %s; recv again for the "
                            "rest" % (chat._dsan(frm), len(donep), len(accept),
                                      "the standup continues" if not closing
                                      else "awaiting the remaining countersigns"))
                    return 0, ["[meld %s e:%d] %s: %s"
                               % (room, st["epoch"], chat._dsan(frm), text),
                               tail] + _ignored_note(ignored, accept)
                st["status"] = "done-mutual" if closing else "peer-done"
                st = _transition(room, seat, "peer-done", st, cause=frm)
                if closing:
                    tail = ("done-mutual — the meld is sealed; the room is the "
                            "durable record (log-flush out-of-band)")
                elif len(accept) == 1:        # the 2-party pair — unchanged UX
                    tail = ("peer left — close your side: helm chat %s say %s "
                            "--marker DONE \"<closing state>\"" % (via, room))
                else:
                    tail = ("all %d peers closed — close your side: helm chat "
                            "%s say %s --marker DONE \"<closing state>\""
                            % (len(accept), via, room))
                return 0, ["[meld %s e:%d] %s: %s"
                           % (room, st["epoch"], chat._dsan(frm), text),
                           tail] + _ignored_note(ignored, accept)
            st = _transition(room, seat, "peer-" + marker.lower(), st, cause=frm)
            floor = _floor_line(marker, frm, accept, st, room, via)
            return 0, ["[meld %s e:%d] %s: %s" % (room, st["epoch"], chat._dsan(frm), text),
                       floor] + _ignored_note(ignored, accept)
        if st["idx"] != total:
            st["idx"] = total
        _write_state(room, seat, st)      # consumed ground survives a re-run
        now = time.time()
        if now >= deadline:
            if closing:
                pending = sorted(accept - set(st.get("done_peers") or []))
                who = ", ".join(chat._dsan(p) for p in pending) or "?"
                return EXIT_BOUND, [
                    "MELD-CLOSING room=%s — no countersign from %s yet; "
                    "your side stays closed, the room stays readable "
                    "(helm chat read --room %s)"
                    % (room, who, room)] \
                    + (["(%d late chunk(s) after your DONE skipped)" % late]
                       if late else []) + _ignored_note(ignored, accept)
            # WHICH TIMEOUT IS THIS? A meld that exchanged chunks and went
            # quiet is a closed window; a meld nobody joined is not one, and
            # the two want opposite next actions.
            heard = set(st.get("joined_peers") or []) \
                | set(st.get("spoke_peers") or []) \
                | set(st.get("done_peers") or [])
            unjoined = sorted(accept - heard)
            if unjoined and not st.get("exchanges"):
                return EXIT_BOUND, _nojoin_lines(room, bound, unjoined, st, via) \
                    + _ignored_note(ignored, accept)
            return EXIT_BOUND, _quiet_lines(room, bound, st, via) \
                + _ignored_note(ignored, accept)
        time.sleep(min(poll, deadline - now))   # the last nap ends AT the bound


def _peers_closed(room, st, seat):
    """(members, closed): this meld's members and those whose [DONE] is
    already in the room. A DONE counts whether recv consumed it (done_peers)
    or it still sits unread past `idx`.

    The unread rows are filtered exactly as recv filters them: reactions,
    own rows, unattributed rows, stale epochs and non-members never count.
    A forged DONE from outside the pinned set cannot silence a member."""
    members = set(st.get("peers") or ([st["peer"]] if st.get("peer") else []))
    closed = set(st.get("done_peers") or []) & members
    rows, _total = chat.read(room, since=st.get("idx") or 0)
    for m in rows:
        text, frm = m.get("text") or "", str(m.get("from") or "")
        if m.get("react") or not text or not frm or frm == seat:
            continue
        if members and frm not in members:
            continue
        em = _EPOCH_RE.search(text)
        if em and int(em.group(1)) != st["epoch"]:
            continue
        mk = _MARKER_RE.search(text)
        if mk and mk.group(1) == "DONE":
            closed.add(frm)
    return members, closed


def say(room, marker, text, seat=None, via="meld"):
    """(lines) — append one bounded chunk: content + floor marker in the one
    text field (text-or-it-didn't-happen). The row also records which suffix
    say generated, so a diff's literal trailing [DONE] remains distinguishable.
    DONE/ABORT @mention the peer (the
    act-moments — a closing that lands silently strands the peer's bound);
    YIELD/HOLD stay mention-free (the peer is inside recv; no cursor spam)."""
    seat = seat or _self_seat()
    marker = (marker or "").upper()
    if marker not in MARKERS:
        raise SystemExit("helm chat %s: --marker wants one of %s" % (via, "|".join(MARKERS)))
    st = state(room, seat)
    if st is None:
        raise SystemExit("helm chat %s: no meld state for %s — invite or join "
                         "first" % (via, room))
    if st.get("status") == "UNKNOWN":
        raise SystemExit("helm chat %s: meld state for %s is UNKNOWN — %s" %
                         (via, room, st.get("_unknown") or
                          "lifecycle journal unreadable"))
    if st["status"] == "aborted":
        raise SystemExit("helm chat %s: room %s is ABORTED" % (via, room))
    if st["status"] == "done-mutual":
        # SEALED: recv refuses it too (both sides closed). Without this, a DONE
        # into a sealed meld re-posts an @peer mention AND regresses the status
        # done-mutual -> done (below), so `meld status` lies and the printed
        # hint drives a phantom 90s countersign watch for an already-consumed
        # countersign — the exact double-command an agent replays post-compaction.
        raise SystemExit("helm chat %s: room %s is sealed (done-mutual) — both "
                         "sides closed; nothing further posts" % (via, room))
    raw = text or ""
    if not raw.strip():
        raise SystemExit("helm chat %s: say wants text — a bare marker is not "
                         "a chunk (text-or-it-didn't-happen)" % via)
    # A trailing unified-diff context line is spaces only, and an added or
    # removed line may end in a significant space. strip() deletes both, so
    # the floor marker glues on and the posted hunk is a different patch.
    # Keep those bytes; still drop a leading empty margin (task/3774).
    kept = raw.rstrip("\n")
    tail = []
    while True:
        head, sep, last = kept.rpartition("\n")
        if not sep or last.strip():
            break
        tail.append(last)
        kept = head
    text = kept.lstrip()
    if tail:
        text += "\n" + "\n".join(reversed(tail))
        if raw.endswith("\n"):
            text += "\n"
    if marker in ("YIELD", "HOLD"):
        # THE MIRROR OF RECV'S LATE-CHUNK BRANCH. After my own DONE, recv
        # skips the peer's YIELD/HOLD. After the peer's DONE, my YIELD/HOLD
        # reaches nobody: the peer's closing recv skips it too. Measured on
        # the live bus: one seat posted 5 YIELDs over 24 minutes into a room
        # its codex peer had already left. The check reads the room, not only
        # my state, because that seat had not run recv since the peer's DONE.
        # Refused BEFORE posting: in peer-done, a post without this check
        # lands and the lifecycle transition then raises.
        members, closed = _peers_closed(room, st, seat)
        if members and members <= closed:
            who = ", ".join(sorted(chat._dsan(p) for p in closed))
            raise SystemExit(
                "helm chat %s: MELD-PEER-CLOSED room=%s — %s already said "
                "[DONE] in this room, so a %s reaches nobody. Close your side "
                "with your closing state:\n  helm chat %s say %s --marker "
                "DONE \"<closing state>\"" % (via, room, who, marker, via, room))
        members = set(members)
        members.add(seat)
        _broadcast_token_refusal(text)
        outside = _outside_member_mentions(text, members)
        if outside:
            raise SystemExit("helm chat %s: %s" % (
                via, "\n".join(_membership_mismatch_lines(
                    room, outside, members, via))))
    # st["peer"] is the RELOCATED convener/invitee from-field (join stored the
    # seed row's raw `from` here; invite stored the raw arg). Launder it before
    # it enters the DONE/ABORT posted text — chat._fmt renders posted text raw
    # fleet-wide, so a hostile peer would reshape every reader's terminal. Raw
    # stays in state (only this mention emits it; recv matches on frm==seat).
    mention = "@%s " % chat._dsan(st["peer"]) if marker in ("DONE", "ABORT") else ""
    row = "%s[MELD e:%d] %s [%s]" % (mention, st["epoch"], text, marker)
    if marker == "DONE":
        # THE DOOR'S WORD, SAID BEFORE THE SEAL (task/3223). A sealed room
        # takes nothing further, so a closing block the review door would
        # refuse, or a round it cannot bind to its row, is refused here by
        # the door's own predicates, on the exact row that would post.
        from . import review_door
        why = review_door.done_refusal(room, row, st["epoch"], seat)
        if why:
            raise SystemExit("helm chat %s: %s" % (via, why))
        st["status"] = "done" if st["status"] != "peer-done" else "done-mutual"
    elif marker == "ABORT":
        st["status"] = "aborted"
    elif st["status"] == "invited":
        st["status"] = "active"           # convener spoke GO
    # ONE LOCK SPAN, BOTH CHECKS BEFORE EITHER WRITE (task/2648). The
    # transition checks RAM and the durable journal under the room lock, then
    # asks the chat post's own pre-append refusal (a padded sha), and only
    # then appends; the row posts after, still under that lock (re-entrant in
    # this thread). A peer's abort or a replay cannot change the journal
    # between the checks and the writes, a refused transition posts no row,
    # and a refused post seals no transition, so a peer-done retry is not
    # refused as sealed. The event does not carry the row's id.
    def post_allowed():
        why = chat.post_refusal(row)
        if why:
            from . import friction
            friction.record("shaguard", reason="padded-sha")
            raise ValueError(why)

    # The post's rotation waits until the room lock is released
    # (chat.rotation_deferred): rotating takes the delivery-state guard, which
    # every other writer takes BEFORE the room lock.
    with chat.rotation_deferred(), chat._room_lock(room) as locked:
        if not locked:
            raise LifecycleError("meld lifecycle room lock unavailable for %s"
                                 % room)
        st = _transition(room, seat, "local-" + marker.lower(), st,
                         before_write=post_allowed)
        # Every refusal ran before the append, so a failure here is a write
        # fault, the one split the agreed bar admits: it must say so, never
        # read as a refusal that changed nothing.
        try:
            _post(row, room, seat, marker=marker)
        except Exception as exc:
            raise LifecycleError(
                "meld %s: the %s transition is RECORDED (epoch %d, now %s) "
                "but its chat row did NOT post (%s: %s), so the peer cannot "
                "see it; post the text to %s by hand"
                % (room, marker, st["epoch"], st["status"],
                   type(exc).__name__, exc, room)) from exc
    out = ["[meld %s e:%d] %s: … [%s]" % (room, st["epoch"], seat, marker)]
    if marker == "DONE":
        out.append("you left the meld — /premise anything durable; the room "
                   "log-flushes out-of-band like any room")
        if st["status"] == "done":  # peer not closed yet — the closer is not
            out.append("confirm the countersign: helm chat %s recv %s "
                       "(returns the peer's DONE → done-mutual)" % (via, room))
    return out


def _lifecycle_rooms(durable=False):
    root = (os.path.join(chat.journal_dir(), "meld") if durable else
            os.path.join(chat.chat_dir(), _LIFECYCLE_DIR))
    try:
        names = sorted(n for n in os.listdir(root) if n.endswith(".jsonl"))
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], "%s: %s" % (type(exc).__name__, exc)
    rooms = []
    try:
        rooms = [_room_from_key(n[:-6]) for n in names]
    except LifecycleError as exc:
        return [], str(exc)
    return rooms, None


def _legacy_snapshots(rooms=None):
    wanted = set(rooms) if rooms is not None else None
    try:
        names = os.listdir(chat.chat_dir())
    except OSError:
        return []
    out = []
    for name in names:
        if ".meld." not in name or not name.endswith(".json"):
            continue
        st = pk.read_json(os.path.join(chat.chat_dir(), name), None)
        if not isinstance(st, dict) or st.get("journal_seq") is not None:
            continue
        room, actor = st.get("room"), st.get("self")
        if (not isinstance(room, str) or not room or not isinstance(actor, str)
                or not actor or wanted is not None and room not in wanted
                or isinstance(st.get("epoch"), bool)
                or not isinstance(st.get("epoch"), int)
                or not _valid_projection(_projection(st))):
            continue
        out.append((room, actor, st))
    return sorted(out, key=lambda x: (x[0], x[1]))


def _import_legacy(rooms=None):
    imported = 0
    for room, actor, st in _legacy_snapshots(rooms):
        events, unavailable = _events(lifecycle_path(room))
        if unavailable:
            raise LifecycleError("legacy meld import cannot read RAM journal: %s" %
                                 unavailable)
        states, _unique, why, _rp = _reduce(events, room)
        if why:
            raise LifecycleError("legacy meld import found UNKNOWN RAM journal: %s" % why)
        if actor in states:
            continue
        _transition(room, actor, "legacy-snapshot", st)
        imported += 1
    return imported


def flush_lifecycle(rooms=None):
    """Mirror contiguous RAM lifecycle events to disk. Called only by log_flush
    (verified by grep, not by this sentence — the one caller is chat.log_flush).

    Returns (appended, quarantined): quarantined maps room -> reason for every
    meld whose lifecycle could NOT be mirrored.

    ONE DIVERGED MELD USED TO DISCARD EVERY LATER ONE. This loop raised on the
    first bad room, so rooms after it never ran — and because log_flush called
    this BEFORE its own room loop, that raise also discarded the rendered-row
    flush of all 156 chat rooms. Measured 2026-08-11: 8 hours of fleet chat
    lived in RAM only while a 3-minute timer fired ~160 times, because one meld
    room diverged. 110 of 156 rooms were flushable at every moment.

    THE INVARIANT THE OLD ORDERING PROTECTED IS KEPT: a meld whose lifecycle
    cannot flush must not get a partial rendered-row flush either, so its room
    is QUARANTINED rather than silently continued — log_flush skips exactly the
    rooms named here. What changes is the blast radius, never the coherence
    rule. A quarantine is reported, never swallowed: the divergence itself is a
    separate defect and resetting the cursor here would destroy its evidence.

    AN ISOLATED NAMESPACE RAISES before any work (chat.journal_write_refusal):
    its RAM holds copies of live rooms, and mirroring them would fork the live
    journal. log_flush refuses first; this guard is for any other caller."""
    isolated = chat.journal_write_refusal()
    if isolated:
        raise LifecycleError(isolated)
    _import_legacy(rooms)
    targets, unavailable = _lifecycle_rooms()
    if unavailable:
        raise LifecycleError("meld lifecycle RAM directory UNKNOWN: %s" % unavailable)
    if rooms is not None:
        wanted = set(rooms)
        targets = [r for r in targets if r in wanted]
    appended = 0
    quarantined = {}
    for room in targets:
        try:
            appended += _flush_one_lifecycle(room)
        except LifecycleError as exc:
            quarantined[room] = str(exc)
    return appended, quarantined


def _flush_one_lifecycle(room):
    """Mirror ONE meld room's lifecycle. Raises LifecycleError; the caller
    quarantines that room rather than abandoning its siblings."""
    appended = 0
    with chat._room_lock(room) as locked:
        if not locked:
            raise LifecycleError("meld lifecycle room lock unavailable for %s" % room)
        ram, unavailable = _events(lifecycle_path(room))
        if unavailable:
            raise LifecycleError("meld lifecycle RAM journal UNKNOWN for %s: %s" %
                                 (room, unavailable))
        _states, runique, why, _rp = _reduce(ram, room)
        if why:
            raise LifecycleError("meld lifecycle RAM journal UNKNOWN for %s: %s" %
                                 (room, why))
        durable, unavailable = _events(lifecycle_path(room, True))
        if unavailable:
            raise LifecycleError("meld lifecycle durable journal UNKNOWN for %s: %s" %
                                 (room, unavailable))
        _states, dunique, why, _rp2 = _reduce(durable, room)
        if why:
            raise LifecycleError("meld lifecycle durable journal UNKNOWN for %s: %s" %
                                 (room, why))
        if runique == dunique[:len(runique)] and len(dunique) > len(runique):
            # DURABLE AHEAD OF RAM IS THE CORRECT STEADY STATE, NOT A DIVERGENCE.
            # This predicate demanded durable be an exact PREFIX of RAM, so after
            # any tmpfs wipe without a restore-journal run — i.e. after every
            # reboot — durable is legitimately LONGER and the check could never
            # accept it again. Measured 2026-08-11 across all 248 lifecycle
            # journal pairs: 46 durable-ahead, 78 equal, ZERO with RAM ahead.
            # Not one was corrupt. The flush failed permanently because disk
            # remembered more than RAM, which for a WRITE-BEHIND log is exactly
            # what disk is for.
            #
            # RECONCILE BY ACCEPTING, NEVER BY TRUNCATING: disk is the
            # authoritative copy and RAM is the impoverished one, so there is
            # nothing to append and nothing to remove. Truncating durable to
            # match RAM would destroy the very history the journal exists to
            # hold.
            return appended
        if dunique != runique[:len(dunique)]:
            # NEITHER IS A PREFIX OF THE OTHER: a genuine fork, and the only
            # state this error was ever meant to name.
            raise LifecycleError("meld lifecycle durable journal diverges from RAM for %s" %
                                 room)
        for ev in runique[len(dunique):]:
            if not eventledger.append(lifecycle_path(room, True), ev):
                raise LifecycleError("could not append durable meld lifecycle for %s" % room)
            appended += 1
    return appended


def _replay_divergence(ram, durable):
    """Why a live RAM stream and its durable replay disagree, or None when
    durable is ahead only by `restored-from-journal` markers.

    None is the flush path's rule (_flush_one_lifecycle) applied to replay.
    A marker records that ANOTHER namespace restored the room. It carries no
    transition, so RAM is still this room's whole live state. The measured
    402 durable-ahead journals were all this shape, and each one read UNKNOWN
    here while the flush beside it accepted it.

    A real transition that RAM lacks is not that shape: RAM has lost live
    events or another writer moved the room. Each reason names the first
    event where the streams part."""
    if durable[:len(ram)] == ram:
        extra = durable[len(ram):]
        real = [e for e in extra if e["transition"] != "restored-from-journal"]
        if not real:
            return None
        return ("durable lifecycle journal is ahead of RAM by %d real "
                "transition(s) RAM never saw (first: %s at seq %d) — RAM lost "
                "live events or another writer moved this room"
                % (len(real), real[0]["transition"], real[0]["seq"]))
    at = next(i for i, (r, d) in enumerate(zip(ram, durable)) if r != d)
    return ("RAM and durable lifecycle journals fork at seq %d (RAM: %s, "
            "durable: %s) — neither is a prefix of the other"
            % (ram[at]["seq"], ram[at]["transition"],
               durable[at]["transition"]))


def replay_room(room, apply=True):
    with chat._room_lock(room) as locked:
        if not locked:
            reason = "meld lifecycle room lock unavailable for %s" % room
            _UNKNOWN[_cache_key(room)] = reason
            return {"state": "UNKNOWN", "reason": reason, "actors": 0}
        # r4 LOCK: _transition holds this SAME lock; a replay_room run
        # bare lets a live transition racing a restore interleave
        # after the durable copy but before the restore marker — both
        # allocate the same seq and the completed RAM stream reduces to
        # "conflicting duplicate sequence N", UNKNOWN on the next read. The
        # whole inspect/copy/marker/snapshot sequence is one critical
        # section, and the COMPLETED stream is re-reduced BEFORE ok is
        # reported — a verb that reports ok on a stream it has not reduced
        # is claiming an outcome it did not measure.
        durable, unavailable = _events(lifecycle_path(room, True))
        if unavailable:
            _UNKNOWN[_cache_key(room)] = unavailable
            return {"state": "UNKNOWN", "reason": unavailable, "actors": 0}
        if not durable:
            reason = "missing or empty durable lifecycle journal"
            _UNKNOWN[_cache_key(room)] = reason
            return {"state": "UNKNOWN", "reason": reason, "actors": 0}
        states, unique, why, _rp = _reduce(durable, room)
        if why:
            _UNKNOWN[_cache_key(room)] = why
            return {"state": "UNKNOWN", "reason": why, "actors": 0}
        if not apply:
            return {"state": "ok", "actors": len(states), "events": len(unique)}
        ram_path = lifecycle_path(room)
        ram, ram_unavailable = _events(ram_path)
        if ram_unavailable:
            _UNKNOWN[_cache_key(room)] = ram_unavailable
            return {"state": "UNKNOWN", "reason": ram_unavailable, "actors": 0}
        _ram_states, runique, ram_why, _rp2 = _reduce(ram, room)
        if ram_why:
            _UNKNOWN[_cache_key(room)] = ram_why
            return {"state": "UNKNOWN", "reason": ram_why, "actors": 0}
        if runique and runique[:len(unique)] != unique:
            reason = _replay_divergence(runique, unique)
            if reason:
                _UNKNOWN[_cache_key(room)] = reason
                return {"state": "UNKNOWN", "reason": reason, "actors": 0}
            # Durable is ahead only by restore markers another namespace
            # wrote. RAM is still the live room, so it is read as-is below.
        restored = not runique
        if restored:
            for ev in unique:
                if not eventledger.append(ram_path, ev):
                    reason = "could not rebuild RAM lifecycle journal"
                    _UNKNOWN[_cache_key(room)] = reason
                    return {"state": "UNKNOWN", "reason": reason, "actors": 0}
            # The restore is a ROOM fact, so it goes IN the stream: a typed
            # restored-from-journal marker, appended to RAM *and* mirrored to
            # the durable journal (flush_lifecycle's contiguous mirror
            # carries it). This is what makes the replayed provenance survive
            # a snapshot repair or a SECOND wipe+restore — the r1/r2
            # in-memory _REPLAYED set and snapshot _replayed bit both died
            # with the process/snapshot (r3 HIGH). The marker restates
            # the last event's projection unchanged: it annotates the room,
            # mutating no actor state.
            last = unique[-1]
            seq = last["seq"] + 1
            marker = {"v": LIFECYCLE_V, "id": _event_id(room, seq), "room": room,
                      "seq": seq, "ts": pk.now_ts(), "actor": last["actor"],
                      "epoch": last["epoch"], "transition": "restored-from-journal",
                      "projection": last["projection"]}
            if not eventledger.append(ram_path, marker):
                reason = "could not rebuild RAM lifecycle journal"
                _UNKNOWN[_cache_key(room)] = reason
                return {"state": "UNKNOWN", "reason": reason, "actors": 0}
            # An isolated namespace records its own restore in its own RAM
            # only. Mirrored, the marker takes the next sequence number of
            # the LIVE room's journal, and that room's next real transition
            # then forks from it (chat.journal_write_refusal).
            if (not chat.journal_write_refusal() and not eventledger.append(
                    lifecycle_path(room, True), marker)):
                reason = "could not mirror restore marker to the durable journal"
                _UNKNOWN[_cache_key(room)] = reason
                return {"state": "UNKNOWN", "reason": reason, "actors": 0}
            # Reduce the COMPLETED RAM stream before claiming the restore
            # worked: the copy + marker must read back as one coherent
            # history, not merely as appends that returned True.
            states, unique = _reload_after_append(room, None)
            if unique is None:
                return {"state": "UNKNOWN", "reason": states, "actors": 0}
            for actor, folded in states.items():
                _write_state(room, actor, _snapshot(room, actor, folded))
        else:
            # r4 MIGRATION: a parent-shaped room (r2 actor bits on the
            # snapshots, a MARKERLESS durable stream — the parent's replay
            # never wrote markers) folds its bits into typed room markers on
            # this first journaled read. replay_room is the durable-primary
            # path (status restores RAM from durable first), so the fold
            # must live HERE — a fold in _repair_from_ram would fire on a
            # RAM copy that replay_durable has ALREADY rewritten from the
            # markerless durable, erasing it. We hold the room lock, exactly
            # as the fold requires.
            states, unique = _reload_after_append(room, None)
            if unique is None:
                return {"state": "UNKNOWN", "reason": states, "actors": 0}
            mwhy = _migrate_parent_bits_locked(room, None, states, unique)
            if mwhy and mwhy != "folded":
                return {"state": "UNKNOWN", "reason": mwhy, "actors": 0}
            if mwhy == "folded":
                # the fold CHANGED the stream: `unique` above is the PRE-fold
                # stream while RAM and durable now carry the markers. Report
                # the POST-fold truth — re-reduce so events/replayed name
                # what the journals actually hold (r6). A migration
                # from durable bits IS a restore.
                states, unique = _reload_after_append(room, None)
                if unique is None:
                    return {"state": "UNKNOWN", "reason": states, "actors": 0}
                restored = True
            # mwhy is None: nothing folded — a live room, and `restored`
            # keeps its no-RAM flag value. A no-op migration must never
            # report replayed (r6 second probe: a normal live room
            # read replayed=true).
            for actor in states:
                _repair_from_ram(room, actor)
        _UNKNOWN.pop(_cache_key(room), None)
        return {"state": "ok", "actors": len(states), "events": len(unique),
                "replayed": restored}


def replay_durable(apply=True):
    rooms, unavailable = _lifecycle_rooms(durable=True)
    if unavailable:
        return {"state": "UNKNOWN", "rooms": {}, "reason": unavailable}
    # A RETIRED MELD IS NOT REPLAYED. `helm chat retire-rooms` archived its
    # room and actor snapshots; replaying its lifecycle would write the
    # snapshots back onto the bus after every reboot. The durable lifecycle
    # journal itself is untouched, so the history stays readable.
    from . import chatdebris
    retired = chatdebris.retired_through()
    rooms = [room for room in rooms if room not in retired]
    if not rooms:
        return {"state": "absent", "rooms": {}}
    report = {room: replay_room(room, apply=apply) for room in rooms}
    state_ = "UNKNOWN" if any(x["state"] == "UNKNOWN" for x in report.values()) else "ok"
    return {"state": state_, "rooms": report, "applied": bool(apply)}


def _room_name_epoch(room):
    """The room-name epoch as a MIGRATION VALIDATOR ONLY — never a clock.

    The name is a CLAIM, and a malformed one is not a clock: accept only a
    plausible epoch (at/after the lifecycle journal's founding, never in the
    future — hard-reject, no slack), else None. Its one legitimate use is
    aging a PRE-FEATURE room whose only stream event is a legacy-snapshot
    (excluded from last_activity, so the stream alone cannot date it). For
    any journaled room the event stream is the sole authority (r3 MED:
    three clocks certified three different impossible states — a name-epoch
    of 1750000000 read age=413d on a live meld, and a content-row ts can be
    a RESTORED marker)."""
    m = re.match(r"meld-(\d+)-", room or "")
    if not m:
        return None
    try:
        epoch = int(m.group(1))
    except ValueError:
        return None
    if 1784663952 <= epoch <= int(time.time()):
        return epoch
    return None


def _last_activity_epoch(room):
    """Epoch of the meld's last REAL exchange, from the ONE authority: the
    lifecycle event stream's room projection (_reduce). A room with no
    journaled content events falls back to the VALIDATED room-name epoch —
    the migration case only (a legacy-snapshot-only room)."""
    events, unavailable = _events(lifecycle_path(room))
    if not unavailable and events:
        _states, _unique, why, roomproj = _reduce(events, room)
        if why:
            return None                          # an unreadable journal is UNKNOWN
        if roomproj["last_activity"]:
            return roomproj["last_activity"]
        if roomproj["clock_unknown"]:
            # a journaled room whose activity clock is invalid/future is
            # UNKNOWN — the name-epoch fallback would render age=0s-confident
            # off a claim. Silence, never a guess (r3 MED).
            return None
        # a legacy-snapshot-only stream carries no content clock: the ONE case
        # the migration fallback exists for
        return _room_name_epoch(room)
    return _room_name_epoch(room)


def _age_suffix(room, st):
    """' age=<human>' for the status row, or '' when no clock reads.

    Reuses the fleet's compact-age idiom (s/m/h/d). Never asserts an age it
    cannot read — an unreadable clock is silence on the suffix, never a
    guessed number (the honest-UNKNOWN law)."""
    ep = _last_activity_epoch(room)
    if not ep:
        return ""
    delta = max(0, int(time.time()) - ep)
    for div, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if delta >= div:
            return " age=%d%s" % (delta // div, unit)
    return " age=%ds" % delta


def _durability(room, st):
    """Three-state ROOM provenance, derived from the event stream by _reduce
    and read IDENTICALLY by every actor: 'replayed' (restored, no later live
    content), 'replayed-reopened' (restored AND live activity resumed — the
    MIXED state; rendered distinctly so a ghost someone answered never reads
    as either a frozen archive or a plain live meld — it is both, and saying
    only one is the lie that cost the live exchange), or the live/durable
    pair when no restore ever happened."""
    events, unavailable = _events(lifecycle_path(room))
    if unavailable:
        return "UNKNOWN"
    _states, unique, why, roomproj = _reduce(events, room)
    if not why and not any(
            ev["transition"] in ("restored-from-journal",
                                 "reopened-after-replay") for ev in unique):
        # a markerless stream that a migration verdict (disagreement /
        # no-clean-splice) already condemned: re-deriving live from the
        # stream would launder the bits' testimony back to durable-live.
        # The markerless shape is the tell (replay_durable may clear the
        # ledger; the stream still has no marker), so the verdict re-fires.
        _r, _o, dis = _parent_actor_bits(room)
        if dis or _o:
            _UNKNOWN[_cache_key(room)] = (
                "parent-format actor snapshots disagree about replay state"
                if dis else
                "parent bits claim a reopen with no clean splice point")
            return "UNKNOWN"
    if why:
        return "UNKNOWN"
    if roomproj["restored"]:
        return "replayed-reopened" if roomproj["reopened"] else "replayed"
    seq = st.get("journal_seq")
    if not isinstance(seq, int):
        return "live-not-yet-durable"
    durable, unavailable = _events(lifecycle_path(room, True))
    if unavailable:
        return "UNKNOWN"
    _states, dunique, why, _rp = _reduce(durable, room)
    if why:
        return "UNKNOWN"
    return "durable-live" if dunique and dunique[-1]["seq"] >= seq \
        else "live-not-yet-durable"


def _member(room, seat):
    """True if `seat` is a party to `room`, False if the room's records name
    other parties only, None if no record can be read.

    This seat's own snapshot answers first. Otherwise any lifecycle event,
    in RAM or durable, that names the seat as its actor or as a peer
    answers. The events are read without the strict fold on purpose: a room
    is UNKNOWN exactly when its stream does not reduce, and a stream that
    does not reduce still names who wrote it."""
    if os.path.exists(state_path(room, seat)):
        return True
    want = str(seat).casefold()
    seen = False
    for durable in (False, True):
        events, _unavailable = eventledger.checked_events(
            lifecycle_path(room, durable))
        for ev in events:
            if not isinstance(ev, dict):
                continue
            proj = ev.get("projection")
            names = [ev.get("actor")]
            if isinstance(proj, dict):
                peers = proj.get("peers")
                names += [proj.get("peer")] + (
                    peers if isinstance(peers, list) else [])
            names = [str(x).casefold() for x in names if isinstance(x, str)]
            if not names:
                continue
            seen = True
            if want in names:
                return True
    return False if seen else None


def status(seat=None, via="meld"):
    """[lines] — this actor's live melds, with honest durability provenance."""
    from . import seats
    seat = seat or _self_seat()
    replay = replay_durable(apply=True)
    key = ".meld.%s.json" % seats._seat_key(seat)
    d = chat.chat_dir()
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(key))
    except OSError:
        names = []
    out = []
    ram_root = os.path.abspath(os.path.join(chat.chat_dir(), _LIFECYCLE_DIR))
    foreign = 0
    for (path, room), reason in sorted(_UNKNOWN.items()):
        if os.path.dirname(path) != ram_root:
            continue
        # THIS SEAT'S MELDS ONLY, like every other row here. The replay reads
        # every room on the bus, so an unfiltered list gave each seat the
        # whole fleet's UNKNOWN melds (one seat's status listed 13, including
        # melds between other seats it was never in). A room whose membership
        # cannot be read stays listed: it cannot be proven someone else's.
        if _member(room, seat) is False:
            foreign += 1
            continue
        out.append("  %s  status=UNKNOWN durability=UNKNOWN reason=%s" %
                   (room, reason))
    for n in names:
        raw = pk.read_json(os.path.join(d, n), None) or {}
        room = raw.get("room") or n.split(".meld.")[0]
        if any(line.startswith("  %s " % room) for line in out):
            # ONE ROW PER ROOM. The replay already said UNKNOWN; a second row
            # read from RAM alone printed durability=durable-live beside it.
            continue
        st = state(room, seat) or raw
        if st.get("status") == "UNKNOWN":
            if not any(line.startswith("  %s " % room) for line in out):
                out.append("  %s  status=UNKNOWN durability=UNKNOWN reason=%s" %
                           (room, st.get("_unknown") or "unreadable lifecycle"))
            continue
        durability = _durability(room, st)
        status_ = st.get("status")
        if status_ in ("active", "invited"):
            # THE THREE EXPLICIT STATUSES the sealed meld named, never two
            # collapsed into one label. A REPLAYED ghost is never AWAITING A
            # TURN: (a) FROZEN — restored, no later live content — reads
            # active-replayed; (b) MIXED — restored AND live activity resumed
            # — reads active-reopened, visibly resumed, so a ghost someone
            # answered is never mistaken for either a frozen archive or a
            # plain live meld. The status is TRUE (that is where the
            # conversation stopped) but the READING (turn-owed) is false in
            # both; the suffix says which. Branch on the EXACT durability —
            # startswith("replayed") would label the mixed room -replayed and
            # erase the reopened truth the third status exists to carry.
            if durability == "replayed":
                status_ = "%s-replayed" % status_
            elif durability == "replayed-reopened":
                status_ = "%s-reopened" % status_
        out.append("  %s  role=%s status=%s exchanges=%s/%s peer=%s "
                   "durability=%s%s"
                   % (room, st.get("role"), status_,
                      st.get("exchanges"), st.get("cap"),
                      chat._dsan(st.get("peer") or "?"),
                      durability, _age_suffix(room, st)))
    if out:
        return out
    if replay["state"] == "absent" or (replay["state"] == "UNKNOWN"
                                       and not foreign):
        return ["helm chat %s: meld state UNKNOWN for seat %s — no readable durable "
                "lifecycle journal proves an empty slate" % (via, seat)]
    if foreign:
        # Every UNKNOWN room was proven to be another seat's: this seat's
        # empty slate is measured, and the other rooms are not its to report.
        return ["helm chat %s: no live melds for seat %s (%d meld(s) of other "
                "seats read UNKNOWN; not yours)" % (via, seat, foreign)]
    return ["helm chat %s: no live melds for seat %s" % (via, seat)]


def _flag(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return default


def usage(via="meld"):
    from . import review_door
    return ("usage: helm chat %s invite <peer> <topic...> [--wait] "
            "[--into ROOM] | "
            "join <room> | recv <room> [--timeout S] | say <room> --marker "
            "YIELD|HOLD|DONE|ABORT [<text...>] | status | standing <peer> "
            "| pull <standing-room> <seat> <question...> | leave "
            "<standing-room>   [--seat S on any] "
            "(one preset, three spellings: meld = the genus, council/standup "
            "= species; say with NO text reads the chunk from stdin, which is "
            "the safe route for prose carrying backticks or $( ) — "
            "`helm chat %s say R --marker YIELD <<'EOF'`). A review or pair "
            "round closes with each side's [DONE] carrying one line: %s"
            % (via, via, review_door.OUTCOME_LINE))


def cmd(args, via="meld"):
    """helm chat meld|council|standup invite <peer> <topic...> [--wait] |
    join <room> | recv <room> [--timeout S] | say <room> --marker M
    [<text...>] | status — every verb takes --seat S (explicit per-command
    identity; ambient-only identity forced env -u gymnastics for
    one-off council seats). `via` is the spelling the operator typed — it
    echoes back in every printed next-command.

    SAY TAKES ITS CHUNK FROM STDIN WHEN NO TEXT IS GIVEN, the same door as
    `post` and `dm`. This verb is in chat._BODY_VERBS, so the argv guard
    already refuses a backticked body here and prescribes a quoted-delimiter
    heredoc; without the stdin leg that prescription named a route the door
    did not have."""
    args = list(args or [])
    seat = None
    if "--seat" in args:
        v = _flag(args, "--seat")
        try:
            seat = home.validate_seat_arg(v)
        except home.SeatNameError as e:
            print("helm chat %s: %s" % (via, e), file=sys.stderr)
            return 2
        if not seat:
            print("helm chat %s: --seat wants a seat name" % via, file=sys.stderr)
            return 2
    wait = "--wait" in args               # invite --wait = invite + first recv
    if wait:                              # (every convener's literal next call)
        args.remove("--wait")
    verb = args[0] if args else "status"
    from . import meld_standing
    # THE STANDING MODE (task/3560) shares these verbs and none of the
    # capped meld's bounds: a room named for a pair routes there.
    standing = len(args) >= 2 and meld_standing.is_standing_room(args[1])
    try:
        if verb == "standing":
            if len(args) >= 2 and args[1] in ("-h", "--help"):
                print(usage(via))
                return 0
            if len(args) >= 2:
                room, lines = meld_standing.open_room(
                    args[1], " ".join(args[2:]) or None, seat=seat, via=via)
                print("\n".join(lines))
                return 0
            print("helm chat %s: standing wants a peer to open a room with"
                  % via, file=sys.stderr)
            print(usage(via))
            return 2
        if verb == "pull" and len(args) >= 3:
            asked, _prc = freetext.tail("helm meld", "pull", args[3:],
                                        "the question")
            if _prc is not None:
                return _prc
            print("\n".join(meld_standing.pull(args[1], args[2], asked or "",
                                               seat=seat, via=via)))
            return 0
        if verb == "leave" and standing:
            print("\n".join(meld_standing.leave(args[1], seat=seat, via=via)))
            return 0
        if verb == "invite" and "--into" in args and \
                meld_standing.is_standing_room(
                    args[args.index("--into") + 1]
                    if args.index("--into") + 1 < len(args) else ""):
            into = _flag(args, "--into")
            for peer in args[1].replace(",", " ").split():
                print("\n".join(meld_standing.add(into, peer, seat=seat,
                                                  via=via)))
            return 0
        if verb == "join" and standing:
            print("\n".join(meld_standing.join(args[1], seat=seat, via=via)))
            return 0
        if verb == "recv" and standing:
            _flag(args, "--timeout")          # never waits: nothing to bound
            code, lines = meld_standing.recv(args[1], seat=seat, via=via)
            print("\n".join(lines))
            return code
        if verb == "invite" and len(args) >= 3:
            threshold = _flag(args, "--threshold")   # council only; popped here
            ctip = _flag(args, "--tip")              # ditto: the judged artifact
            # --question is the OTHER way to name a council's subject: a design
            # decision has no sha, and requiring one is what made `council`
            # unable to convene the exact conversations it exists for.
            cq = _flag(args, "--question")
            # --into ROOM: the next ROUND in a persistent room (a task's
            # pair meld) instead of a new room. Not --room: `helm chat`
            # consumes that flag for every verb before this one runs.
            into = _flag(args, "--into")
            subject, _irc = freetext.tail("helm meld", "invite", args[2:],
                                          "the subject")
            if _irc is not None:
                return _irc
            room, lines = invite(args[1], subject or "", seat=seat, via=via,
                                 room=into)
            print("\n".join(lines))
            if via == "council":
                # the FORMAL species convenes an embargoed quorum over the same
                # pinned set the invite just wrote (council.py owns the tally)
                from . import council
                st = state(room, seat or _self_seat())
                reg, cerr = council.convene(
                    room, st["peers"], st["epoch"], threshold=threshold,
                    convener=st["self"], tip=ctip, question=cq)
                if cerr:
                    print("helm chat council: " + cerr, file=sys.stderr)
                    return 2
                print("\n".join(council.status_lines(room, via)))
            elif threshold is not None:
                print("helm chat %s: --threshold is a COUNCIL flag (the formal "
                      "species with a quorum) — a %s has no vote to count"
                      % (via, via), file=sys.stderr)
                return 2
            if not wait:
                return 0
            code, lines = recv(room, seat=seat, via=via)
            print("\n".join(lines), file=sys.stderr if code else sys.stdout)
            return code
        if verb == "join" and len(args) >= 2:
            print("\n".join(join(args[1], seat=seat, via=via)))
            return 0
        if verb == "recv" and len(args) >= 2:
            t = _flag(args, "--timeout")
            code, lines = recv(args[1], timeout=float(t) if t else None,
                               seat=seat, via=via)
            print("\n".join(lines), file=sys.stderr if code else sys.stdout)
            return code
        if verb == "say" and len(args) >= 2:
            marker = _flag(args, "--marker")
            # THE SAFE ROUTE THE ARGV GUARD ALREADY NAMES FOR THIS VERB.
            # `chat meld say` sits in chat._BODY_VERBS, so helm refuses a
            # backticked argv body here and points the author at a
            # quoted-delimiter heredoc — and this door then took text from
            # argv ONLY, so the route the guard prescribes did not exist and
            # the author's next move was the dangerous one. A meld chunk is
            # prose written under time pressure, which is exactly where a
            # backtick or a $( ) gets executed by the shell before helm sees
            # the body. `resolve_one_body` is the same door `post` and `dm`
            # use: it reads stdin only when something is actually waiting, so
            # a scripted caller with no body still falls through to usage
            # rather than hanging on a socket that never reaches EOF, and it
            # REFUSES a positional body and a piped one together rather than
            # silently discarding one of them.
            said, _srrc = freetext.tail("helm meld", "say", args[2:],
                                        "the message")
            if _srrc is not None:
                return _srrc
            body, rc = chat.resolve_one_body(said or "", "%s say" % via)
            if rc is not None:
                return rc
            if standing:
                print("\n".join(meld_standing.say(
                    args[1], body, marker=marker, seat=seat, via=via)))
                return 0
            print("\n".join(say(args[1], marker, body, seat=seat, via=via)))
            return 0
        if verb == "status":
            print("\n".join(status(seat=seat, via=via)
                            + meld_standing.status_lines(
                                seat or _self_seat(), via)))
            return 0
    except (SystemExit, LifecycleError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(usage(via), file=sys.stderr)
    return 2
