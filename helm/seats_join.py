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
import stat
import sys
import time

from . import chat, home, pk, seats_advice
from .seats_common import (BEACON_DRAIN_CAP, GUIDE_PATH, _clip, _flocked,
                           _scrub, declared_name, dm_lane, roster)
# join sits at the TOP of the layering — join -> delivery -> roster ->
# identity -> common — so every one of these is a downward edge and none
# needs deferring. That is what a leaf looks like when the floor came first.
from .seats_identity import (DERIVED, _dispute_sentence, acting_seat,
                             identity_disagreement, resolve_homing,
                             resolve_identity)
from .seats_address import seat_scope
from .seats_roster import nonpane_session, write_roster
from .seats_runtime import launch_runtime
from .seats_delivery import (_cursor, _init_cursor, _scan_rooms, cursor_path,
                             deliver_any)

def join_cli(args, room, room_explicit, room_source):
    """`helm chat join` typed by hand — the NON-hook leg, beside the verb it
    drives (code lives beside what it acts on, not in the dispatcher). Three
    things differ from the hook leg: a typo'd flag REFUSES instead of minting
    an auto-derived stranger (`--seet wanted` would otherwise exit 0 and mint
    a seat nobody meant); the session is THIS process's own, because None is
    the pre-launch child's shape and disarms every guard keyed on it; and a
    refusal exits 2 on stderr — a refusal that exits 0 is not a refusal to
    anything scripted around it. Only the HOOK leg (in seats_cli) stays
    fail-open: a session start is never shaped by a guard.

    It also lives here rather than in seats_cli.cmd for the reason
    _cmd_claims gives there: `cmd` is a DECLARED apply-reader exemption, and
    a guard_tail call in its body would satisfy the sweep for the whole
    dispatcher on the strength of a guard that covers neither --apply branch."""
    from .cli import guard_tail
    from .seats_identity import safe_cwd
    rc = guard_tail("helm chat join", args, valued=("--seat",),
                    usage="usage: helm chat join [--seat S]")
    if rc is not None:
        return rc
    flag = args[args.index("--seat") + 1] if "--seat" in args else None
    _seat, line = join(session=home.session_id(), cwd=safe_cwd(), seat=flag,
                       room=room, room_explicit=room_explicit,
                       room_source=room_source)
    refused = "JOIN REFUSED" in line
    print(line, file=sys.stderr if refused else sys.stdout)
    return 2 if refused else 0


# The session kinds Claude records in <home>/sessions/<pid>.json that make a
# pane: a `bg` job or a `fork` shares the pane's environment, ORCA_PANE_KEY
# included, and is not the pane's agent.
PANE_KINDS = ("interactive",)
_ANCESTOR_HOPS = 8


def _ancestor_pids(pid=None):
    """This process and its ancestors, nearest first, read from /proc stat."""
    from . import procid
    pid, out = pid or os.getpid(), []
    for _ in range(_ANCESTOR_HOPS):
        if pid <= 1:
            break
        out.append(pid)
        try:
            with open(os.path.join(procid.proc_root(), str(pid), "stat"),
                      "rb") as f:
                pid = int(f.read().decode("utf-8", "replace")
                          .rpartition(")")[2].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return out


def session_kind(session=None):
    """The `kind` Claude recorded for the claude process this hook runs under,
    or None when no record could be found.

    The record is `<home>/sessions/<pid>.json`, where pid is the nearest
    ancestor that has one; home is CLAUDE_CONFIG_DIR, then every credential
    home. A record whose sessionId names a different session is skipped, so
    a record left by an unrelated process on a recycled pid does not answer."""
    homes = [os.environ.get("CLAUDE_CONFIG_DIR")]
    try:
        from . import sessions
        homes += sessions.cred_homes()
    except Exception:                          # noqa: BLE001 — probe only
        pass
    homes = [h for h in dict.fromkeys(homes) if h]
    for pid in _ancestor_pids():
        for h in homes:
            try:
                with pk.open_regular(
                        os.path.join(h, "sessions", "%d.json" % pid)) as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            if session and rec.get("sessionId") \
                    and rec.get("sessionId") != str(session):
                continue
            return rec.get("kind")
    return None


def join(session=None, cwd=None, seat=None, room="main", room_explicit=False,
         room_source=None, session_source=None, runtime=None,
         require_pane=None, owe_banner=False):
    """The autojoin: roster row + cursor initialized HERE + the identity line
    the hook injects as session context. The line DIRECTS the agent to arm its
    idle-wake beacon as a mandatory FIRST action — a self-armed Monitor is the
    only thing that can wake an idle PTY agent (native-wake-only-agent-armed),
    so a SessionStart directive is the strongest enforcement available.
    Idempotent per seat. Baselines a cursor in every room `_scan_rooms` admits
    (all live rooms for legacy un-homed seats; {home, main} for homed seats),
    so pre-join backlog never floods and later admitted rooms can backfill.

    `require_pane` gates enrolment on Claude's own session record (see
    `session_kind`): None asks nothing (a hand join, a pre-launch mirror);
    False refuses only a record that says the session is not a pane; True
    enrols only a record that says it is. Refused, the return is (None, "")
    and nothing is written.

    A DEADLINE KILL MUST NOT HALF-JOIN (task/2924). The hook runs under
    `timeout 5`, whose SIGTERM kills Python where it stands. With the roster
    row written BEFORE the per-room cursor baseline -- the long step, one
    locked commit per room -- a kill in between leaves a rostered seat with
    no cursor: every backlog row reads as pending and the banner is never
    printed. So the order is: the owed record (when
    `owe_banner`), then every room's baseline, then the roster row, then the
    DM lane, which alone must wait for the write's `admitted` answer. A kill
    now leaves no row, or a row whose rooms are all baselined, and the owed
    record lets the next hook replay this join (`settle_owed_join`). Each
    step is idempotent, so a replay only finishes what the kill left."""
    inputs = {"cwd": cwd, "seat": seat, "room": room,
              "room_explicit": room_explicit, "room_source": room_source,
              "session_source": session_source, "require_pane": require_pane}
    if require_pane is not None:
        kind = session_kind(session)
        if kind not in PANE_KINDS and (require_pane or kind is not None):
            return None, ""
    dis = identity_disagreement(session)
    if dis:
        # REGISTRATION UNDER A DISPUTED IDENTITY IS REFUSED WHOLE: no roster
        # write, no cursor init — a half-join would mint split-brain presence,
        # and a full one is incident 1 (2026-08-02: an inherited env name
        # joined as the integrator and took over its row). The refusal is a
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
    # AN EXPLICIT --seat IS A THIRD IDENTITY SOURCE and must obey the same
    # law as the other two: `identity_disagreement` above compares the
    # DECLARED name against the SESSION's roster binding, but never sees an
    # argument, so `seat or acting_seat(...)` alone would silently prefer the
    # flag — the one thing seats_identity forbids ("AGREEMENT of sources is
    # the only clean state ... neither source is ever silently preferred").
    #
    # COUNTEREXAMPLE this guards against: a process whose declared or
    # rostered identity is seat-a runs `helm chat join --seat seat-b`. Without
    # the check the roster gains a seat-b row while `post`/`react` — which
    # resolve through the declared name first, by the contamination law —
    # keep stamping seat-a: one agent, two identities, and the half that
    # speaks is the half nobody watches.
    #
    # A JOIN CANNOT REBIND A RUNNING PROCESS. Its declared name lives in its
    # own environ and nothing outside can rewrite that, so honouring the flag
    # could only move the written-down half. Refuse, and point at
    # `helm chat seat rename` (moves the row and every keyed state file) or
    # a re-export + relaunch (actually becomes the other seat).
    #
    # THE FLAG IS COMPARED AGAINST WHAT THE LAW RESOLVES — resolve_identity,
    # all three sources in order — never against one source. Only a DECLARED
    # or ROSTERED answer can disagree; a DERIVED answer is a minted name, not
    # an identity, and an explicit --seat over it is a legitimate first bind.
    #
    # THE PRE-LAUNCH CHILD IS NOT THIS CASE. launch pre-joins the CHILD's seat
    # from the PARENT's process before the child's environ is built, so the
    # parent's declared name is a different agent's and disagreement is
    # expected. That call carries session=None because the child does not
    # exist yet — the discriminator: a process that IS the seat has a
    # session; one registering a seat it is about to become does not.
    if seat and session:
        state, who = resolve_identity(session, cwd)
        if state != DERIVED and str(seat).casefold() == str(who).casefold():
            # AGREEMENT ADOPTS THE LAW'S SPELLING. `who` is the declared or
            # rostered authority — no roster lookup here, so no race — and the
            # locked write below canonicalizes on top of it if a differently-
            # cased key already exists, returning the key it chose. BOTH
            # halves are load-bearing. Without this line, on an EMPTY roster
            # the writer has no key to preserve, so `--seat SEAT-A` over
            # declared seat-a would admit SEAT-A while acting_seat stays
            # seat-a — the split, on first admission. Without the writer's
            # half, a pre-write read of the key is a second snapshot that a
            # case-only change can desynchronise from the write.
            seat = who
        elif state != DERIVED:
            return who, (
                "[helm chat] JOIN REFUSED: --seat says %r but this process "
                "resolves to %r (%s), and a join cannot rebind a running "
                "process. Honouring the flag would move the ROSTER only, "
                "leaving `post` and `react` stamping %r: one agent with two "
                "identities. Fix: to rename the seat, `helm chat seat rename "
                "%s %s` (it moves the row and every keyed state file); to BE "
                "the other seat, re-export HELM_CHAT_NAME=%s and relaunch."
                % (seat, who, state, who, who, seat, seat))
        # A DERIVED CALLER ONTO AN OCCUPIED NAME IS NOT REFUSED HERE, by
        # measurement. A fresh session with no identity can pass --seat X
        # where X's row holds an older session, and at this layer that is
        # observationally identical to an honest RESUME (new sid, occupied
        # row, no declared identity) — every pane resume in the fleet has
        # this shape. Refusing on sid inequality alone breaks resume; telling
        # the two apart needs a LIVENESS oracle, which the law keeps in
        # identity_disagreement's claim-jump and deliberately does not extend
        # to a DERIVED caller. That distinction belongs to
        # seats_identity/presence, which holds the presence authority; join
        # does not have it and does not claim it.
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
    runtime = launch_runtime() if runtime is None else runtime
    # THE EFFECTIVE KEY COMES BACK FROM THE LOCKED WRITE. Reading a canonical
    # spelling before this call and writing under it after is two roster
    # snapshots, and a case-only change between them splits the durable key
    # from the returned one. One acquisition, one key, and that key is what
    # the return, banner, family lookup and spawn binding below all use.
    # THE PANE KEY IS THIS PROCESS'S OWN, so only a join that IS the seat
    # (it has a session) records it; the pre-launch mirror runs in the parent.
    if owe_banner and session:
        _owe_join(session, inputs)
    # THE ONE ROOM THE ROSTER WRITE STARTS AT ZERO: narrowing an existing home
    # to explicit main keeps pending main traffic (seats_mute
    # `_rooms_to_baseline`), so the first pass starts main where that would.
    narrowing = (source == "explicit" and home_room == "main"
                 and seat_scope(seat).get("home") not in (None, "main"))
    _baseline_rooms(room, seat, session, admitted=None,
                    from_start=("main",) if narrowing else ())
    seat, row, admitted = write_roster(
        seat, session=session, cwd=cwd, home_room=home_room,
        home_room_source=source, identity=not nonpane_session(session),
        runtime=runtime, keyed=True, admission=True,
        pane_key=os.environ.get("ORCA_PANE_KEY") if session else None)
    if session:
        try:
            from . import seat as _seat
            # HELM_CHAT_NAME is the canonical roster identity; a durable rename
            # need not remain family-shaped. HELM_SEAT_STORAGE is minted beside
            # it from the instance directory and selects only the config/register
            # path. The binder requires both values to match spawn.json.
            storage = os.environ.get("HELM_SEAT_STORAGE")
            if storage:
                bound = _seat._bind_spawn_session(
                    seat, session, source=session_source,
                    storage_seat=storage)
            elif _seat._seat_family(seat)[1] is None:
                bound = _seat._bind_spawn_session(
                    seat, session, source=session_source)
            else:
                bound = None
            if bound is False:
                print("helm chat join: WARN — could not bind session %s to "
                      "spawn register for %s; autocompact stays fail-closed"
                      % (session, seat), file=sys.stderr)
        except Exception as e:
            print("helm chat join: WARN — spawn-session bind failed for %s (%s); "
                  "autocompact stays fail-closed" % (seat, e), file=sys.stderr)
    effective_home = row.get("home_room")
    # THE SECOND PASS: the DM lane, plus any room that appeared since the
    # first. Every room the first pass reached is a read, not a write.
    _baseline_rooms(room, seat, session, admitted=admitted)
    scope = ("; your home room %s does NOT wake you row-by-row — catch up "
             "with helm chat read when you wake, or arm the beacon with "
             "--ambient if the room is quiet" % effective_home
             if effective_home and effective_home != "main" else "")
    display_room = effective_home or room
    # THE BEACON DIRECTIVE IS THE PAYLOAD, THE REST WAS RATIONALE. This banner
    # is a seat's first context, and every load-bearing token stays: identity,
    # the wake summary, MANDATORY, the exact copy-pasteable Monitor command
    # (--follow + the harness's deadline), its expiry rule, the
    # DEFERRED/ToolSearch fallback, the
    # not-a-background-shell caveat, the native-wake-only-agent-armed reason,
    # and the guide pointer — those are pinned by the guardian tests because
    # they are what makes the directive actionable. What was cut is the prose
    # that repeated the same physics three ways (task 692); the enforcement is
    # unchanged. `scope` (a homed seat's catch-up hint) rides in as before.
    # THE ADMISSION SAYS WHICH NAME IT ADMITTED THROUGH. A retained session
    # whose environ still spells the old name is this seat only while the
    # rename alias is live; the banner names the window and the one fix.
    _own, alias = declared_name()
    if alias and str(_own).casefold() == str(seat).casefold():
        scope += (". Your HELM_CHAT_NAME is still '%s' — a rename alias of "
                  "%s until %s; re-export HELM_CHAT_NAME=%s before then or "
                  "this process becomes a stranger again"
                  % (alias[0], seat, pk.epoch_ts(alias[1]), seat))
    # A SESSION ALREADY COVERED BY A LIVE BEACON IS NOT TOLD TO ARM ONE.
    # A COMPACTING SUBAGENT SHARES ITS MAIN SESSION, so it reaches this banner
    # holding main's live beacon and is handed MANDATORY FIRST ACTION anyway.
    # It cannot comply — the arm guard refuses a sidechain — so the directive
    # is noise at the top of the one context where noise is most expensive,
    # and it teaches a seat that helm's own MANDATORY can be ignored.
    #
    # THE SIDECHAIN BRANCH CANNOT ANSWER THIS. It keys on an agent id, and the
    # harness's SessionStart compact payload carries none, so it never fires
    # for the case it was written for. COVERAGE is readable where identity is
    # not: `_one_live_incumbent` asks whether ONE committed LIVE waiter already
    # serves this exact (seat, session), proving it across the registry AND
    # /proc rather than trusting a row.
    #
    # ITS FAIL DIRECTION IS THE ONE THIS NEEDS, and that is why it is the
    # instrument rather than a new predicate: an unlistable process table, a
    # GHOST or UNKNOWN waiter, a different session, or two live waiters all
    # return None. UNKNOWN therefore KEEPS the directive, because a seat wrongly
    # told to arm loses a tool call while a seat wrongly told it is covered
    # goes deaf — [[negative-and-unreadable-must-not-share-a-value]] decided in
    # the direction that costs least.
    #
    # ASKED ONLY ON A COMPACT, and that bound is a measurement rather than
    # caution. `_one_live_incumbent` probes /proc and the credential homes:
    # 43ms median against an EMPTY registry on this box, and every seat join
    # goes through this banner. A FRESH SessionStart cannot have a live beacon
    # for its own session by construction — the session is new — so paying it
    # there buys nothing. A COMPACT is the one arrival where the session is
    # OLD, which is exactly the case this branch exists for.
    # AN INCUMBENT IS NOT A BEACON, AND READING IT AS ONE IS FALSE ASSURANCE.
    # `_one_live_incumbent` answers "ONE live committed waiter serves this
    # (seat, session)" — it admits an UNREGISTERED SINGLE-SHOT `chat wait`
    # through its unregistered path, and production's own words are that a
    # single-shot is a DELIVERY, not a beacon. Suppressing the directive for
    # one tells a seat it is covered by something that stops waiting.
    #
    # SO THE SPEC IS READ, NOT THE TRUTHINESS. `waiter_spec` already carries
    # the distinction: `follow` is what makes a waiter keep waiting after it
    # delivers. TIMEOUT IS NOT CONSULTED — a finite followed beacon is
    # truthful point-in-time coverage, and every armed Monitor on this fleet
    # is finite and re-arms, so demanding an infinite one would tell every
    # seat to arm a beacon it already holds. ROOM IS NOT CONSULTED — a
    # seat-follow delivery wakes on DMs and mentions from ANY room, so a room
    # mismatch is not proof of non-coverage.
    #
    # `--any` IS EXCLUDED, and it is the one flag that looks like wider
    # coverage and is not. The wait loop says so twice in its own words:
    # "--any never filters at all", "--any stays one room's tap", and it
    # "WATCHES A ROOM AND TOUCHES NO CURSOR". A room watch is a different
    # instrument, not a superset of the seat's delivery.
    # AND THE SPEC CANNOT SEE WHERE THE WAKES GO. Every field above is
    # reconstructed from ARGV, and a redirection is not in argv — the shell
    # consumes `> file` before the exec — so a waiter can be LIVE, followed,
    # not `--any`, on the right seat and session, and have fd 1 pointed at a
    # file. It consumes addressed rows and produces no wake, and this banner
    # would tell the seat to "re-arm only if that waiter dies" about a process
    # that never dies. A cross-family read found it against the agreed
    # contract; that contract never looked below argv.
    #
    # THE SINK IS REQUIRED TO BE ADMISSIBLE, NOT MERELY NOT-REFUTING, because
    # UNKNOWN keeps the directive here like every other unknown on this path.
    # AND THE EVIDENCE IS NECESSARY, NOT SUFFICIENT: a regular file or
    # /dev/null proves nothing reads it, while a socket or pipe proves only
    # that the sink is not provably dead — this side cannot see whether
    # anything still holds the far end. The banner says so rather than
    # claiming more than was measured.
    covered = None
    if session and str(session_source or "") == "compact":
        try:
            from . import beacons
            incumbent = beacons._one_live_incumbent(seat, session)
            spec = (incumbent or {}).get("waiter")
            if isinstance(spec, dict) and spec.get("follow") \
                    and not spec.get("any") \
                    and (incumbent or {}).get("sink") == beacons.SINK_ADMISSIBLE:
                covered = incumbent
        except Exception:
            covered = None       # unreadable is not covered
    return seat, join_banner(seat, display_room, scope,
                             covered.get("pid") if covered else None)


def _baseline_rooms(room, seat, session, admitted, from_start=()):
    """Give (seat, session) a cursor pair in every room the join admits.

    `admitted is None` is the pass BEFORE the roster write, and it skips the
    seat's DM lane: that lane baselines at offset 0 and may inherit an older
    session's cursor, and whether it may is the roster write's `admitted`
    answer, which does not exist yet. Every other room is an EOF baseline
    that no roster fact changes. The cursor key is casefold-exact
    (`seats_common._seat_key`), so a spelling the locked write later
    canonicalizes names the same files. A room in `from_start` starts at
    offset 0, as the roster write would start it."""
    lane = dm_lane(seat)
    for r in _scan_rooms(
            room, seat=seat, session=session, bounded=False):
        # the seat's DM lane baselines at offset 0 — every row in it is
        # addressed to THIS seat by construction, so a DM sent before the
        # join must deliver, never vanish under an EOF baseline
        if r == lane and admitted is None:
            continue
        at0 = r == lane or r in from_start
        if _cursor(r, seat, session) is None \
                or _cursor(r, seat, session, beacon=True) is None:
            _init_cursor(
                r, seat, session, at_start=at0,
                inherit_existing=bool(admitted and r == lane and session))
        if session and (_cursor(r, seat) is None
                        or _cursor(r, seat, beacon=True) is None):
            # the seat-level pair too: sessionless callers (bare CLI wait/
            # deliver) must not start blind just because the join was hook-keyed
            _init_cursor(r, seat, at_start=at0)


# THE OWED JOIN. One small file per session, written before a hook join does
# anything durable and removed only after its banner has been written out, so
# its presence means exactly "this session's join may be unfinished and its
# banner unseen". It lives in its own state family, out of every room scan.
OWED_FAMILY = "join"


def _owed_path(session):
    return chat.state_path(OWED_FAMILY, "owed." + pk.slug(str(session)))


def _owe_join(session, inputs):
    os.makedirs(chat.state_family_dir(OWED_FAMILY), mode=0o700, exist_ok=True)
    pk.write_json(_owed_path(session), dict(inputs, session=session))


def owed_join(session):
    """The join inputs this session still owes, or None. A stat, then a small
    read only when owed: this runs on every PostToolUse delivery."""
    if not session:
        return None
    path = _owed_path(session)
    if not os.path.exists(path):
        return None
    d = pk.read_json(path, None)
    return d if isinstance(d, dict) and d.get("session") == session else None


def paid_join(session):
    """The banner reached the harness: nothing is owed any more."""
    if not session:
        return
    try:
        os.unlink(_owed_path(session))
    except FileNotFoundError:
        pass


def settle_owed_join(session):
    """Replay an owed join and return its line ("" when nothing is owed).

    The replay is the SAME join with the SAME inputs, and every step of it is
    idempotent, so it finishes whatever the kill left: rooms already
    baselined are reads, and the roster write re-admits the same session. The
    owed record stays until the caller has written the line and calls
    `paid_join`, so a replay killed in turn is replayed again by the next
    hook. A join that now refuses returns its refusal, which is owed too."""
    owed = owed_join(session)
    if owed is None:
        return ""
    kw = {k: owed.get(k) for k in ("cwd", "seat", "room_explicit",
                                   "room_source", "session_source",
                                   "require_pane")}
    _seat, line = join(session=session, room=owed.get("room") or "main", **kw)
    if not line:
        paid_join(session)          # refused as not-a-pane: nothing to say
    return line or ""


def owed_emitter(emit, session):
    """`emit`, carrying an owed join's banner when this session owes one.

    The PostToolUse delivery hook is the seat's next hook after a killed
    SessionStart join, so it replays the join here, BEFORE its identity check:
    a join killed before its roster write left no row to assert. The banner
    rides the FIRST response, because the installed pair runner refuses a
    second one; `flush_owed` sends it alone when delivery said nothing. The
    owed record is removed only after that write returns. Nothing owed, or a
    replay that fails, returns `emit` untouched: a delivery is never the
    price of a banner."""
    try:
        owed = settle_owed_join(session)
    except Exception:
        return emit
    if not owed:
        return emit

    def paying(line):
        if paying.paid:
            return emit(line)
        out = emit("\n\n".join(x for x in (owed, line) if x))
        paying.paid = True
        paid_join(session)
        return out
    paying.paid = False
    paying.flush_owed = lambda: None if paying.paid else paying(None)
    return paying


def join_banner(seat, display_room, scope, covered_pid=None):
    """The SessionStart banner for one seat — the armed form when a live
    waiter's pid is supplied, else the one that asks for the first action.

    A PURE RENDERER AT ITS OWN DOOR, because this is the highest-frequency
    message helm prints and a budget over it has to measure the artifact
    rather than a reconstruction of it. `join` decides coverage; this decides
    nothing and only writes words.

    THE ARMED FORM IS THE SHORTER ONE, which it was not: 771 characters to say
    that nothing is owed, four sentences of them re-arm caveats that matter
    only later. It states the fact and stops; the caveats are in the guide it
    already cites, which is where a seat that needs them is reading anyway.

    THE MONITOR ARGV IS THE PAYLOAD of the other form and stays verbatim, and
    so does the deferred-tool fallback, because a seat that cannot find the
    tool cannot perform the act. What is not here is the rationale — why a
    background shell cannot wake a PTY agent — which the guide carries whole.

    AND THE CITATION IT CARRIED WAS DEAD: it named premise
    `native-wake-only-agent-armed`, the store holds
    `native-wake-only-agent-armed-or-headless`, and `helm store get` on the
    cited spelling returns nothing. A dangling id printed at every session
    start is worse than no id — it teaches readers that helm's citations do
    not resolve. The pointer is the guide now, and it is a path that exists."""
    if covered_pid is not None:
        return ("[helm chat] seat '%s' in room %s. Your beacon is already "
                "armed for this session (waiter pid %s), so nothing is owed; "
                "catch up with `helm chat read`. If it dies, or you are "
                "addressed and do not wake, re-arm — see %s"
                % (seat, display_room, covered_pid, GUIDE_PATH))
    return ("[helm chat] seat '%s' in room %s — mentions, DMs and @all wake "
            "you between tool calls%s. First action: arm your "
            "beacon — %s; %s. No Monitor tool? "
            "DEFERRED: ToolSearch(query: "
            "\"select:Monitor\"). Bearings, and why a background shell will "
            "not do: %s"
            % (seat, display_room, scope, seats_advice.beacon_monitor(seat),
               seats_advice.BEACON_EXPIRY_TERSE, GUIDE_PATH))


def destination_usable(stream, follower=True):
    """False when `stream`'s bytes are PROVEN to reach no reader, else None.

    `follower=False` is a consumer that exits after its line: its exit flushes
    any filter it writes through, so only a follower asks who reads its pipe.

    IDENTITY RESOLVES THE OBJECT; THE OBJECT DECIDES. Asking WHICH FUNCTION
    was passed is never authority here -- it cannot be, because the same
    function writes to different places depending on what `sys.stdout` is
    bound to. What identity is good for is the one thing a callable cannot
    tell you: which object it writes to. So the table below maps the emitters
    this module KNOWS write to `sys.stdout` onto that object, and everything
    else resolves to None and stays UNKNOWN.

    THE SINGLE-SHOT PATH IS WHY THIS IS A FUNCTION AND NOT AN INLINE CHECK.
    The measurement lived inside `join` and tested `stream is _emit_line`, so
    every OTHER production caller -- the hook boundary and single-shot wait,
    which pass `emit=print` -- skipped it entirely and went on consuming
    addressed rows into a sink that wakes nobody. The defect was never about
    which function; it was about which callers got measured at all."""
    dest = sys.stdout if stream in (_emit_line, print) else None
    from . import beacons
    return beacons.sink_usable_for(dest, follower=follower)


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
def _beacon_identity_refusal(session, cwd=None, ambient_seat=True,
                             asserted=None):
    """The one-line refusal that must stop a beacon arming, or None.

    TWO CAUSES, not one. The dispute half is the 2026-08-02 theft vector. The
    other half is why this predicate grew an argument: when `wait` resolves
    its OWN seat, it is arming a wake path and consuming an inbox, and the
    ambient resolver's last rung MINTS a name — so a process nobody declared
    and the roster never bound would arm a stranger's beacon and drain rows
    into a pipe belonging to nobody. `wait` is a DELIVERY (its own docstring
    says so: it advances the cursor through deliver's at-most-once path), and
    a delivery that resolves its seat before calling deliver walks straight
    past the guard deliver makes for itself.

    `ambient_seat=False` says the caller was GIVEN a seat and is not resolving
    one, so only the dispute half applies. A seat DERIVED from the roster was
    not given: `_cmd_wait` passes ambient_seat=True for it, because a
    resolution owes the admission pass that a proof does not.

    `asserted` rides through to the admission pass so a stated `--seat` is
    checked AS an assertion (resolve_actor's MISASSERTED rung) instead of being
    invisible to the one door that can refuse it."""
    dis = identity_disagreement(session)
    if dis:
        return "[helm chat] REFUSING to arm: " + _dispute_sentence(
            dis[0], dis[1], session)
    if ambient_seat:
        from . import actors
        _actor, err = actors.resolve_actor(session, cwd, asserted=asserted,
                                           act="arm a beacon and consume an inbox")
        if err:
            return "[helm chat] REFUSING to arm: " + err
    return None


def wait(seat=None, room="main", any_row=False, timeout=None, poll=None,
         emit=None, follow=False, session=None, ambient=None):
    """Block until the next word arrives; returns the line or None on
    timeout. Seat mode IS a delivery (advances the cursor via deliver's
    at-most-once path); --any watches the room without touching cursors.
    Busy-turn parity comes from the PostToolUse hook; an IDLE seat gets
    woken only if it armed a Monitor on this — opt-in by design (M11).

    --follow (the idle-wake beacon) NEVER returns on a match: it streams each
    matching event as one emitted line — one Monitor line = one agent wake —
    and returns only on timeout (a Monitor passes none: the HARNESS ends the
    watch at its own deadline, and the seat re-arms). A contiguous burst of
    reactions to one row is ONE wake event: the shared delivery drain
    aggregates it before the cursor commits, while deliverable() stays a
    stateless routing rule. The beacon's
    DEFAULT scope is MENTION-ONLY: @mentions of the seat (any room), replies
    and reactions to its rows, DMs, and @all — a plain
    home-room row no longer wakes it. A wake-muted room gates this beacon only;
    its rows remain owed to hook delivery/pending/stop surfaces. Each ambient
    wake burns a full idle turn, and a seat in a busy home room trusts the
    mention culture (premise
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
    deadline = time.time() + timeout if timeout is not None else None
    # the ambient session (CLI leg passes _env_session()) keys the SAME
    # per-session cursor the boundary hook advances — one session, one
    # cursor, whichever channel fires first; co-named siblings unaffected.
    # `--any` WATCHES A ROOM AND TOUCHES NO CURSOR (this function's own
    # docstring says so), so it is not the delivery whose identity has to be
    # admissible — guarding it refused a plain room watch for having no name.
    ambient_seat = not seat and not any_row
    seat = seat or acting_seat(session)   # OWN identity first (acting_seat)
    refusal = _beacon_identity_refusal(session, ambient_seat=ambient_seat)
    if refusal:
        # THE EXACT ARM THAT DRAINED ~60 OF OPUS-INTEGRATOR'S DMS 2026-08-02:
        # a restarted pane wearing an inherited HELM_CHAT_NAME armed the named
        # seat's beacon and its polls consumed that seat's inbox. Armed-under-
        # dispute loses the wake path, but the wake path was the theft vector.
        # Printed through the FLUSHED stdout sink, not stderr: a Monitor pipe
        # reads stdout, so the refusal must ride the same channel a wake would.
        _emit_line(refusal)
        return None
    if follow and emit is None:
        # A BEACON WHOSE STDOUT IS A REGULAR FILE CAN WAKE NOBODY. Armed as a
        # background shell task, stdout is that task's output FILE: every
        # wake-line lands in a sink no reader is woken by, while the beacon
        # keeps consuming addressed rows — _beacon_orphaned's twin with a
        # live launcher instead of a dead one. Measured 2026-08-21: three
        # seats in another project's room armed exactly this shape and burned a room of
        # turns until a peer explained the Monitor form. REFUSE at arm
        # time, through stdout — the top of that very file is the one place
        # the mis-armed seat eventually looks. Rows stay PENDING, so the
        # absence watchdogs escalate instead of a healthy-looking sleep.
        # A deliberate file capture opts out: HELM_BEACON_FILE_SINK_OK=1.
        try:
            sink_is_file = stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)
        except (OSError, ValueError, AttributeError):
            sink_is_file = False        # unknowable stdout -> old behaviour
        from . import beacons as _beacons
        if sink_is_file and os.environ.get(_beacons.FILE_SINK_OK) != "1":
            _emit_line(
                "helm chat wait: REFUSING this beacon — stdout is a regular "
                "FILE, so it was armed as a background shell task and its "
                "wake-lines wake nobody. Arm it as a Monitor: "
                + seats_advice.beacon_monitor("<you>")
                + " — a Monitor's stdout is a pipe, one line "
                "= one wake. Deliberate file capture: "
                "HELM_BEACON_FILE_SINK_OK=1.")
            return None
        # A FILTER THAT HOLDS A LINE EATS THE WAKE (task/2920): this waiter
        # commits the row, then the line sits in the filter's buffer until
        # the Monitor deadline kills the pipeline. `cut -c1-500` is one: it
        # holds a line until its producer exits, and a live seat lost every
        # addressed row to it. Refusing makes this waiter exit, and EOF
        # flushes the refusal through the filter.
        holder = _beacons.stdout_line_holder()
        if holder:
            _emit_line(
                "helm chat wait: REFUSING this beacon — its stdout is piped "
                "into %s, which can hold a wake line until the Monitor kills "
                "it, so the row is consumed and never shown. Arm it bare: %s. "
                "A filter must pass each line at once: `command grep "
                "--line-buffered`, `sed -u`, cat, tee or tr."
                % (holder, seats_advice.beacon_monitor("<you>")))
            return None
    # single-shot keeps its contract: emit stays as passed (None ⇒ deliver
    # returns the line without emitting). --follow always needs a sink to stream
    # through, so it defaults to a PER-LINE-FLUSHED print: the beacon's reader
    # is a Monitor (a PIPE), and bare print() is block-buffered to a pipe — the
    # wake-line would sit unflushed and the agent would never wake (the beacon
    # worked in a tty, dead through Monitor). flush=True = one line, one wake.
    stream = emit or (_emit_line if follow else emit)
    # WHAT THIS CONSUMER CAN ACTUALLY REACH, and the question is about the
    # OBJECT, never about a function name.
    #
    # KNOWING WHICH FUNCTION RUNS IS NOT KNOWING WHERE ITS BYTES GO.
    # `_emit_line` calls `print(..., flush=True)`, so it writes to whatever
    # `sys.stdout` is BOUND TO at that moment — and that binding is not fd 1
    # by nature. Both directions were measured wrong when this keyed on the
    # function identity alone: under `redirect_stdout(/dev/null)` with a PIPE
    # on fd 1 the follower read itself usable and discarded the row, and under
    # `redirect_stdout(StringIO)` with /dev/null on fd 1 it withheld from a
    # perfectly usable collector. One check, wrong in opposite directions,
    # because it asked about the wrong object.
    #
    # SO RESOLVE THE DESTINATION AND CLASSIFY THAT. `sys.stdout.fileno()` is
    # the fd the bytes actually reach, whatever number it happens to be. An
    # object with NO fileno — a StringIO, any in-process collector — is not a
    # kernel sink this module can refute, so it is UNKNOWN and consumes
    # exactly as it always has. Refuse only what can be PROVEN to reach no
    # reader; never guess on a caller's behalf.
    #
    # AND TAKE THE READING BESIDE EACH DELIVERY, NEVER ONCE. This loop
    # waits without bound and fd 1 is not fixed for the life of the
    # process: the reader of a pipe can exit between two polls, and a
    # harness can dup2 a fresh pipe over the number. A reading taken before
    # the wait answers for the fd table of that moment only -- it would
    # keep consuming rows into a pipe that went deaf after it was taken,
    # and it would withhold forever from a sink that became usable after
    # it was taken. Both are the loss this classification exists to
    # refuse, so `destination_usable` is asked at the moment a row would
    # be spent, beside the call that spends it.
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
                                       ambient=ambient,
                                       channel="beacon" if follow else None,
                                       sink_usable=destination_usable(
                                           stream, follower=follow))
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
        # THE LAST NAP ENDS AT THE DEADLINE, NOT A WHOLE POLL PAST IT. An
        # unclamped sleep(poll) here made `--timeout 0.01` wait POLL_S (2 s)
        # and let a Monitor overshoot its own bound by up to a poll.
        now = time.time()
        if deadline and now >= deadline:
            return None
        time.sleep(min(poll, deadline - now) if deadline else poll)
