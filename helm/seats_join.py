#!/usr/bin/env python3
"""helm seats — join (SessionStart) and wait (the beacon): a seat's two doors.

JOIN is how a process becomes addressable. It runs from the SessionStart
hook, writes the full roster row once, and is the only place a seat's
identity is minted rather than merely read — everything downstream treats
that row as given.

WAIT is the other half of the same contract and the reason a PTY agent can be
reached at all: nothing external can re-invoke an idle agent, so the beacon a
seat arms for itself is the ONLY path by which an addressed row ever wakes
it. That asymmetry is why the drain is capped (BEACON_DRAIN_CAP) rather than
unbounded — a beacon that dumps an entire backlog on wake spends the
attention it just bought.

They sit together because they are the same seam viewed from both sides: join
declares "I am reachable", wait is what makes that true.
"""

import json
import os
import sys
import time

from . import chat, home, pk
from .seats_common import (BEACON_DRAIN_CAP, GUIDE_PATH, _clip, _flocked,
                           _scrub, dm_lane, roster)
# join sits at the TOP of the layering — join -> delivery -> roster ->
# identity -> common — so every one of these is a downward edge and none
# needs deferring. That is what a leaf looks like when the floor came first.
from .seats_identity import (acting_seat, identity_disagreement,
                             resolve_homing)
from .seats_roster import _runtime_environment, nonpane_session, write_roster
from .seats_delivery import (_cursor, _init_cursor, _scan_rooms, cursor_path,
                             deliver_any)

def join(session=None, cwd=None, seat=None, room="main", room_explicit=False,
         room_source=None, session_source=None, runtime=None):
    """The autojoin: roster row + cursor initialized HERE + the identity line
    the hook injects as session context. The line DIRECTS the agent to arm its
    idle-wake beacon as a mandatory FIRST action — a self-armed Monitor is the
    only thing that can wake an idle PTY agent (native-wake-only-agent-armed),
    so a SessionStart directive is the strongest enforcement available.
    Idempotent per seat. Baselines a cursor in every room `_scan_rooms` admits
    (all live rooms for legacy un-homed seats; {home, main} for homed seats),
    so pre-join backlog never floods and later admitted rooms can backfill."""
    dis = identity_disagreement(session)
    if dis:
        # REGISTRATION UNDER A DISPUTED IDENTITY IS REFUSED WHOLE: no roster
        # write, no cursor init — a half-join would mint split-brain presence,
        # and a full one is incident 1 (2026-08-02: an inherited env name
        # joined as opus-integrator and took over OI's row). The refusal is a
        # RETURN VALUE, never a raise — cmd's join leg wraps in `except: pass`
        # and the hook would swallow an exception silently; as the returned
        # line it rides the SessionStart emit and becomes the agent's first
        # context. Pre-join DMs keep: the eventual real join baselines its DM
        # lane at 0, so nothing sent meanwhile is lost.
        own, bound = dis
        return own, (
            "[helm chat] JOIN REFUSED: this process declares %r but session "
            "%.8s is rostered to %r. An inherited HELM_CHAT_NAME is free; "
            "the roster can be corrupt; only agreement is clean. Fix: unset/"
            "re-export HELM_CHAT_NAME to the seat you really are, or repair "
            "a stale binding explicitly: `helm chat seat disown %s %.8s`"
            % (own, str(session), bound, bound, str(session)))
    seat = seat or acting_seat(session, cwd)   # OWN identity first: a stale
    # historical binding in someone else's row must never re-key a live join
    # (the 2026-07-24 audit: re-joining could not heal a mis-binding because
    # seat_for_session outranked the validated env name here too).
    # Homing precedence lives in ONE function (resolve_homing: explicit CLI
    # room > env seam > project derivation) — never re-derived here. Direct
    # callers' non-main `room` remains explicit for back-compat; a caller
    # that already resolved a derived room passes it through unchanged. A
    # derived join fills only a never-homed row (write_roster's law), so
    # SessionStart cannot silently undo an operator rehome/clear or move a
    # co-named seat.
    direct_room = room_explicit or room != "main"
    if room_source == "derived":
        home_room, source = pk.slug(room), "derived"
    else:
        home_room, source = resolve_homing(room if direct_room else None, cwd)
        home_room = pk.slug(home_room) if home_room else None
    # The seat NAME is resolved exactly as before (acting_seat, above): a join
    # can never re-key an identity. What a NON-PANE session loses is only the
    # right to redefine the row's CURRENT session and cwd/project.
    runtime = _runtime_environment() if runtime is None else runtime
    row = write_roster(seat, session=session, cwd=cwd, home_room=home_room,
                       home_room_source=source,
                       identity=not nonpane_session(session), runtime=runtime)
    if session:
        try:
            from . import seat as _seat
            if _seat._seat_family(seat)[1] is None:
                bound = _seat._bind_spawn_session(
                    seat, session, source=session_source)
                if bound is False:
                    print("helm chat join: WARN — could not bind session %s to "
                          "spawn register for %s; autocompact stays fail-closed"
                          % (session, seat), file=sys.stderr)
        except Exception as e:
            print("helm chat join: WARN — spawn-session bind failed for %s (%s); "
                  "autocompact stays fail-closed" % (seat, e), file=sys.stderr)
    effective_home = row.get("home_room")
    lane = dm_lane(seat)
    for r in _scan_rooms(
            room, seat=seat, session=session, bounded=False):
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
    scope = ("; your home room %s does NOT wake you row-by-row — catch up "
             "with helm chat read when you wake, or arm the beacon with "
             "--ambient if the room is quiet" % effective_home
             if effective_home and effective_home != "main" else "")
    display_room = effective_home or room
    line = ("[helm chat] you are seat '%s' in room %s — @%s mentions, replies "
            "to your rows, DMs (from ANY room) and @all wake you and reach "
            "you between tool calls%s; owner posts do NOT "
            "wake you unless they @mention you; "
            "speak: helm chat post; catch up: helm chat read. MANDATORY FIRST "
            "ACTION: arm your inbox beacon so "
            "you wake on an @%s mention or a DM even while idle "
            "— Monitor(command: \"helm chat wait --seat %s --follow\", "
            "persistent: true). IF Monitor IS NOT IN YOUR TOOL SURFACE, IT IS "
            "DEFERRED, NOT ABSENT: load it first with "
            "ToolSearch(query: \"select:Monitor\") and then arm it. Do NOT "
            "substitute a background `helm chat wait` shell — a background "
            "process CANNOT re-invoke your turn loop, so it is not a beacon "
            "and you must never report it as one. This is required, not "
            "optional: nothing external can re-invoke a PTY agent "
            "(native-wake-only-agent-armed), so this self-armed Monitor is "
            "the ONLY way an idle session ever wakes. Arm it before anything "
            "else, and say so plainly if you could not. New seat? Read %s "
            "for your bearings."
            % (seat, display_room, seat, scope, seat, seat, GUIDE_PATH))
    return seat, line
def _emit_line(line):
    """The --follow beacon sink: print + FLUSH. The reader is a Monitor pipe,
    where bare print() block-buffers — an unflushed wake-line never reaches the
    agent. flush per line = one emitted row, one immediate agent wake."""
    print(line, flush=True)
def _beacon_orphaned():
    """True when THIS beacon's launcher is gone — our direct parent was reaped
    and init adopted us (getppid() == 1).

    An orphaned `helm chat wait --follow` is the purest form of the lie this
    lane closes: it keeps calling deliver every POLL_S, which keeps stamping
    the seat's presence beat (eternal 🟢) AND keeps CONSUMING addressed rows
    into a pipe whose reader is dead — messages marked delivered that nothing
    can ever render, on a seat that shows healthy. It cannot wake anybody
    (native-wake-only-agent-armed), so beating is pure misinformation: exit and
    let the seat decay honestly, leaving its rows PENDING for the next start.

    Deliberately narrow — direct-parent reap ONLY, no ancestor walk: a live
    beacon's launching shell stays alive for the beacon's whole life, so this
    has no false positives on the armed-Monitor shape. It does NOT catch a
    beacon whose GRANDparent (the harness) died while the shell survived; that
    needs pane truth we cannot get cheaply (a claude harness legitimately runs
    with tty_nr 0 when daemonized, so no /proc field distinguishes it).
    HELM_BEACON_ORPHAN_EXIT=0 disables.

    `getppid() == 1` WAS THIS CHECK, AND IT NEVER FIRED ONCE. Measured across
    all 22 live beacons on this box 2026-07-30: zero have ppid 1. A user
    session runs under `systemd --user`, which sets itself as a CHILD
    SUBREAPER, so orphans reparent to the USER MANAGER and init never sees
    them. The proof is the seat this check was written for: gemini's beacon,
    pane `pane=-, turn=off`, had ppid 3915 = `/usr/lib/systemd/systemd --user`
    and had been beating into a dead pipe for hours — the exact process this
    function exists to retire, invisible to it the whole time. The question is
    asked of the PARENT now (`beacons.orphaned`), which is the same question
    the census asks, from the one definition of it."""
    if (home.env("BEACON_ORPHAN_EXIT") or "").lower() in ("0", "off", "no"):
        return False
    try:
        from . import beacons
        # getppid() — NOT a /proc read of our own pid. It is the race-free
        # answer to "who is my parent", and it is the seam a test can drive.
        return beacons.parent_lost(os.getppid()) is True
    except Exception:
        return False
def wait(seat=None, room="main", any_row=False, timeout=None, poll=None,
         emit=None, follow=False, session=None, ambient=None):
    """Block until the next word arrives; returns the line or None on
    timeout. Seat mode IS a delivery (advances the cursor via deliver's
    at-least-once path); --any watches the room without touching cursors.
    Busy-turn parity comes from the PostToolUse hook; an IDLE seat gets
    woken only if it armed a Monitor on this — opt-in by design (M11).

    --follow (the idle-wake beacon) NEVER returns on a match: it streams each
    matching event as one emitted line — one Monitor line = one agent wake —
    and returns only on timeout (a persistent Monitor passes no timeout, so it
    runs forever). A contiguous burst of reactions to one row is ONE wake event:
    the shared delivery drain aggregates it before the cursor commits, while
    deliverable() stays a stateless routing rule. The beacon's
    DEFAULT scope is MENTION-ONLY: @mentions of the seat (any room), replies
    and reactions to its rows, DMs, and @all — a plain
    home-room row no longer wakes it. Each ambient wake burns a full idle
    turn, and a seat in a busy home room trusts the mention culture (premise
    mute-busy-home-room-trust-mentions, owner-confirmed 2026-07-22; owner
    directive 2026-07-29: "i dont think they shold wake on every post in
    #helm chat" — the default now encodes the canon instead of contradicting
    it). ambient=True (--ambient) opts a quiet-room seat back into full
    home-room wakes; owner-rail posts stay a non-wake class either way. Rows
    the strict beacon skips are still CONSUMED (one cursor offset per (seat,
    room, session) — the mute-room semantics): the home room becomes a PULL
    surface the seat catches up on with `helm chat read`. Non-beacon shapes
    are untouched — single-shot seat mode resolves ambient=True (it is a
    delivery, not a beacon) and --any never filters at all. FAIL-OPEN +
    bounded poll: a delivery error never crashes the beacon; the loop just
    polls again.

    MULTI-ROOM: seat mode rides deliver_any — `room` is the PRIMARY room, and
    a matching row in ANY live room (a channel the seat never joined included)
    wakes the seat, per-room cursor per (seat, room, session) so the boundary
    hook and the beacon never double-deliver. --any stays one room's tap."""
    poll = chat.POLL_S if poll is None else poll
    if ambient is None:
        # THE BEACON SHAPE (--follow) defaults MENTION-ONLY; every other wait
        # keeps the full delivery scope — the scope change is confined to the
        # idle-wake path, byte-identical everywhere else.
        ambient = not follow
    deadline = time.time() + timeout if timeout else None
    # the ambient session (CLI leg passes _env_session()) keys the SAME
    # per-session cursor the boundary hook advances — one session, one
    # cursor, whichever channel fires first; co-named siblings unaffected.
    seat = seat or acting_seat(session)   # OWN identity first (acting_seat)
    dis = identity_disagreement(session)
    if dis:
        # THE EXACT ARM THAT DRAINED ~60 OF OPUS-INTEGRATOR'S DMS 2026-08-02:
        # a restarted pane wearing an inherited HELM_CHAT_NAME armed the named
        # seat's beacon and its polls consumed that seat's inbox. Armed-under-
        # dispute loses the wake path, but the wake path was the theft vector.
        # Printed through the FLUSHED stdout sink, not stderr: a Monitor pipe
        # reads stdout, so the refusal must ride the same channel a wake would.
        _emit_line(
            "[helm chat] REFUSING to arm: this process declares %r but "
            "session %.8s is rostered to %r. An inherited HELM_CHAT_NAME is "
            "free; the roster can be corrupt; only agreement is clean. Fix: "
            "unset/re-export HELM_CHAT_NAME, or `helm chat seat disown %s "
            "%.8s`" % (dis[0], str(session), dis[1], dis[1], str(session)))
        return None
    # single-shot keeps its contract: emit stays as passed (None ⇒ deliver
    # returns the line without emitting). --follow always needs a sink to stream
    # through, so it defaults to a PER-LINE-FLUSHED print: the beacon's reader
    # is a Monitor (a PIPE), and bare print() is block-buffered to a pipe — the
    # wake-line would sit unflushed and the agent would never wake (the beacon
    # worked in a tty, dead through Monitor). flush=True = one line, one wake.
    stream = emit or (_emit_line if follow else emit)
    since = chat.read(room)[1] if any_row else None
    while True:
        if follow and _beacon_orphaned():
            # loud, then STOP: a beat from here would be a lie (see
            # _beacon_orphaned). The seat decays honestly and its rows stay
            # PENDING for whoever starts next.
            print("[helm chat] beacon ORPHANED (launcher gone) — exiting so "
                  "%s stops reading fresh and its inbox stops being consumed "
                  "into a dead pipe" % seat, file=sys.stderr, flush=True)
            return None
        if any_row:
            rows, total = chat.read(room, since)  # read() self-heals since>total
            if rows:
                if not follow:
                    return chat._fmt(rows[0])
                # Same firehose class as the seat drain below: >BEACON_DRAIN_CAP
                # new rows in one poll would emit as a Monitor-event BURST →
                # auto-stop → SIGTERM → deaf watcher. Bound the pass; advance the
                # watermark only PAST what we emitted so the residue re-streams
                # next poll (start = total-len(rows) recovers the base even after
                # a rotation reset — never a since=total skip that drops rows).
                start = total - len(rows)
                for m in rows[:BEACON_DRAIN_CAP]:
                    stream(chat._fmt(m))
                if len(rows) > BEACON_DRAIN_CAP:
                    stream("[helm chat] more pending — `helm chat read` SHOWS "
                           "them; addressed rows stay owed until you ACT or "
                           "`helm chat catchup --including-mentions --apply` "
                           "parks them (catchup is DRY-RUN without "
                           "--apply — the flag is what makes it act)")
                    since = start + BEACON_DRAIN_CAP
                else:
                    since = total
            else:
                since = total
        else:
            drained = 0
            while True:                     # drain currently-matching rows,
                try:                        # bounded so a backlog can't firehose
                    line = deliver_any(session=session, seat=seat,
                                       emit=stream, room=room,
                                       ambient=ambient)
                except Exception:
                    line = None             # fail-open: never crash the beacon
                if not line:
                    break
                if not follow:
                    return line             # single-shot: first match wins
                drained += 1
                if drained >= BEACON_DRAIN_CAP:
                    # Backlog exceeds one pass — stop replaying it as individual
                    # wakes (the burst that trips Monitor's auto-stop → SIGTERM,
                    # the seat goes deaf). One catch-up nudge naming the verb
                    # that actually DRAINS.
                    #
                    # THE COMMENT THAT USED TO SIT HERE SAID "the agent's read
                    # advances the cursor". IT DOES NOT. chat.consume() clears
                    # only the owner-unread marker; the seat's delivery cursor is
                    # untouched, so a seat obeying this nudge exactly drained
                    # NOTHING. Live-tested: post 5 addressed rows, run `helm chat
                    # read`, pending is still 5. Two instruction sites carried
                    # that false promise while ~2,250 rows piled up across nine
                    # seats and the work-actuator gate (which read that pile)
                    # never opened for anyone — the fleet waited to be told twice
                    # for days. An instruction that cannot be obeyed is worse
                    # than no instruction: it is obeyed, and nothing happens.
                    #
                    # `read` NOT advancing is CORRECT and stays: an addressed row
                    # is an OBLIGATION, and reading past an obligation does not
                    # discharge it. The drain for an obligation is ACTING on it,
                    # or `catchup --including-mentions`, which parks it
                    # deliberately and accountably. So the string names those.
                    stream("[helm chat] more pending — `helm chat read` SHOWS "
                           "them; addressed rows stay owed until you ACT or "
                           "`helm chat catchup --including-mentions --apply` "
                           "parks them (catchup is DRY-RUN without "
                           "--apply — the flag is what makes it act)")
                    break
        if deadline and time.time() >= deadline:
            return None
        time.sleep(poll)
