#!/usr/bin/env python3
"""helm seats — CLI verbs and hook legs dispatched from chat.cmd_chat.

A hook reads harness stdin the process does not own, so _hook_stdin has a
DEADLINE and a stall note rather than a blocking read: a
hook that waits forever on an empty pipe is indistinguishable from a wedged
agent, and the fleet has spent real hours on that confusion.

THIS MODULE IS THE TOP OF THE LAYERING and imports from nearly every sibling,
which is exactly right for a dispatcher — it is the one place that is allowed
to know about everything, because knowing about everything IS its job. The
siblings never import back; claims defers its one session-id read.
"""

import json
import os
import sys
import time
from . import actors, chat, freetext, home, hooks, pk
from .seats_common import (DEFAULT_TTL, STATUS_BYTES, _clip, _scrub,
                           _seat_label, own_name,
                           status_write_refused, ttl_flag)
from .seats_identity import (_assert_own_seat, acting_seat, resolve_homing,
                             safe_cwd)
from .seats_mute import mutes, set_mute
from .seats_roster import (disown_session, rehome_seat,
                           rename_seat, roster_checked)
from .seats_delivery import deliver_any, dm, receipt_cli
from .seats_roomscan import QuietRooms
from .seats_catchup import catchup, render_catchup
from .seats_join import (_beacon_identity_refusal, _emit_line, join, paid_join,
                         join_cli, wait)
from .seats_ack import _ts_epoch, render_ack, render_pending
from .seats_delegation import (_clear_posttool_delegation,
                               _record_posttool_delegation)
from .seats_claims import (claim, claim_marks, claims_list, own_leases,
                           release, stamp_claim_rows)
from .seats_gc import gc_roster
from .seats_report import render_roster, render_status, set_status
from .seats_stop_guard import stop_guard
from .hookstdin import HOOK_STDIN_MAX, _UNPARSED, _parsed  # noqa: F401
# THE COUNCIL VERBS AND `_flag` live in seats_cli_council (this file passed
# its 1000-line budget); every name stays importable from here, and
# `_OWNER_NAMES` declares the move so the retired-name rung reads it as one.
from .seats_cli_council import (  # noqa: F401
    _COUNCIL_FLAGS, _cmd_council, _council_positionals, _flag)
_OWNER_NAMES = (("seats_cli_council", (
    "_COUNCIL_FLAGS", "_cmd_council", "_council_positionals",
    "_flag")),)  # moved; bound here

HOOK_STDIN_DEADLINE_S = 2.0   # generous: the harness writes its JSON at once
_HOOK_STDIN_STALL = "hook-stdin-stall"   # the receipt a silent pass never left
def _hook_stdin():
    """Hook JSON off stdin, bounded in TIME, read WHOLE in bytes.

    A buffered read on a pipe that stays open and never delivers blocks until
    it fills its buffer or sees EOF, which is forever; the hook wrapper's
    timeout then kills the guard and the harness reads that as ALLOW, so a
    stop-guard that never ran looked like one that passed. So: wait for
    readability with a deadline, drain what is there, and on a stall return
    {} FAST, leaving a receipt (`_hook_stall_note`) so a flaky hook is
    answerable from data. `hooks.spec_command` owns the wrapper's budget.

    The byte bound is the payload's (HOOK_STDIN_MAX), never a buffer's: see
    helm/hookstdin.py for what a 64 KiB cut did, and for `_UNPARSED`, which
    tells a consumer that `{}` came from a payload it could not read."""
    import select
    _UNPARSED[0] = ""
    deadline = time.time() + HOOK_STDIN_DEADLINE_S
    buf = bytearray()
    try:
        fd = sys.stdin.buffer.fileno()
    except Exception:                        # noqa: BLE001
        # NO REAL FD — an in-memory stdin (BytesIO in the suite, a harness that
        # substitutes a file object). Those cannot block on a pipe, so the
        # plain read is both correct and the only thing available. Never let
        # a fix for the blocking path swallow the non-blocking one.
        return _hook_stdin_plain()
    while True:
        left = deadline - time.time()
        if left <= 0:
            _hook_stall_note(len(buf))
            _UNPARSED[0] = "stdin stalled with %d bytes" % len(buf)
            return {}
        try:
            ready, _w, _x = select.select([fd], [], [], left)
        except Exception:                    # noqa: BLE001 — unselectable stdin
            if buf:
                break                        # parse what we already drained
            return _hook_stdin_plain()       # nothing read yet: use the plain path
        if not ready:
            _hook_stall_note(len(buf))
            _UNPARSED[0] = "stdin stalled with %d bytes" % len(buf)
            return {}
        try:
            chunk = os.read(fd, 65536)
        except Exception:                    # noqa: BLE001
            break
        if not chunk:                        # EOF: everything the harness sent
            break
        buf += chunk
        if len(buf) >= HOOK_STDIN_MAX:
            break
        if not chunk.rstrip().endswith(b"}"):  # only a `}` can end a document
            continue
        try:
            d = json.loads(buf)
        except Exception:                    # noqa: BLE001 — partial, keep going
            continue
        return d if isinstance(d, dict) else _parsed(buf)
    return _parsed(buf)
def _hook_stdin_plain():
    """The read for a stdin with no descriptor, which cannot block. The
    installed PostToolUse pair hands every stage exactly this shape."""
    _UNPARSED[0] = ""
    try:
        raw = sys.stdin.buffer.read(HOOK_STDIN_MAX)
    except Exception:                        # noqa: BLE001
        return {}
    return _parsed(raw)
def _hook_stall_note(nbytes):
    """Record that a hook read stalled — never raise, never block the boundary.

    The whole defect was that a stalled hook looked exactly like a clean pass.
    One line, appended, best-effort: an unwritable state dir must not turn a
    degraded hook into a failed one."""
    try:
        p = os.path.join(home.helm_home(), home.GLOBAL, ".state", _HOOK_STDIN_STALL)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s stalled after %.1fs with %d bytes (seat=%s)\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       HOOK_STDIN_DEADLINE_S, nbytes, home.chat_name() or "?"))
    except Exception:                        # noqa: BLE001
        pass
def _cmd_claims(args):
    """`helm chat claims [--json]` — the live lease board, with THIS seat's own
    tokens joined in.

    A leaf function rather than an inline branch of `cmd` for one structural
    reason worth stating: the apply-reader sweep in
    tests/test_dispatch_honest.ApplyReadersAreGuarded flags any `cmd*` handler
    that mentions `--apply` without calling `guard_tail`, and `cmd` is a
    DECLARED exemption because its two `--apply` branches (`seat gc`,
    `catchup`) refuse junk through their own closed sets instead. Putting a
    guard_tail call in `cmd`'s own body would have satisfied that detector for
    the whole dispatcher on the strength of a guard that covers neither
    `--apply` branch — retiring a live exemption by accident. `_cmd_council`
    already sets this precedent; this leg follows it.

    Two defects closed here, both the lying-surface class:
      * `--json` was ACCEPTED AND DROPPED — the human table printed, rc 0, and
        the caller had no way to know. It is real now, and `guard_tail` refuses
        every OTHER unknown flag rather than eating it (`--seat` included: the
        own-token rule reads the AMBIENT identity, so a name out of argv here
        would be a second, wrong answer to 'which seat am I').
      * no read surface handed a holder their own lease token, so the `release`
        refusal stranded exactly the honest holder it was written to serve.

    `--room` never arrives — chat.main consumes it, and the claims ledger is
    one per chat dir, not per room."""
    from .cli import guard_tail
    grc = guard_tail("helm chat claims", args, flags=("--json",),
                     usage=("usage: helm chat claims [--json]  (the live "
                            "leases; the `lease` field is YOUR OWN token, "
                            "shown only for rows this seat holds)"))
    if grc is not None:
        return grc
    me = acting_seat(_env_session(), safe_cwd())
    # ONE READ OF THE LEDGER FOR THE ROWS AND THE TOKENS: two reads let a
    # release and a re-claim land between them, and the join then put a token
    # on a row it never belonged to (task/2541).
    swept = []
    rows = claims_list(swept=swept)
    # The STORED keys go in: stamp_claim_rows owns the join, because two
    # stored rows can publish one resource and one holder (task/2528).
    own = own_leases(me, swept=swept[0])
    # ONE ROSTER READ FOR THE WHOLE COMMAND, AND IT IS ABOVE THE --json BRANCH.
    # Below it, only the two terminal renderers carried listedness while every
    # PROGRAMMATIC consumer got the unmarked view (task/914 item 2).
    snap = roster_checked()
    stamp_claim_rows(rows, own, me, snap)   # lease + listedness ride the ROW
    if "--json" in args:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("helm chat: no live claims")
        return 0
    for c in rows:
        # STALE IS SAID OUT LOUD, not merely computed. claims_list() carries
        # the liveness verdict per claim; printing the row without it leaves
        # a dead holder's lock looking exactly like a working seat's, which
        # is the whole condition this lane exists to end. The remedy rides
        # with the diagnosis so nobody has to go looking for the verb.
        print("  %s -> %s (%ds left, fence %s)%s%s" % (
            c["resource"], c["holder"], c["remaining"], c["fence"],
            claim_marks(c, snap),
            ("  lease %s (yours)" % c["lease"]) if c["lease"] else ""))
    return 0
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
def _payload_homing(cwd, room, room_source):
    """The hook seam homes from the SESSION's payload cwd, not the hook
    PROCESS's. cmd_chat pre-resolves the default room from its own cwd —
    normally identical to the session's, but a metaharness may run hooks
    elsewhere (or the two may diverge), and a derived room from the WRONG
    cwd would home the seat to the wrong project. So: a DERIVED
    pre-resolution is re-resolved through THE one resolver against the
    payload cwd when one is present; explicit rooms (--room, operator env)
    pass through untouched. A payload cwd that IS the hook's own cwd, the
    ordinary case, was derived by that pre-resolution already, so it is not
    derived again: that cost two git processes and a registry read on every
    hook (task/3556)."""
    if not cwd or room_source != "derived" or cwd == safe_cwd():
        return room, room_source
    r2, s2 = resolve_homing(None, cwd)
    if not r2:
        return "main", None                     # payload cwd is project-less
    return r2, ("derived" if s2 == "derived" else None)
def _cmd_wait(args, room):
    """Validate and run one `helm chat wait` invocation.

    This stays outside the multiplexer so its `guard_tail` cannot make the
    structural apply-reader audit mistake unrelated `cmd()` branches for
    guarded ones — as for `_cmd_claims` and `_cmd_council`."""
    from .cli import guard_tail
    grc = guard_tail("helm chat wait", args, valued=("--seat", "--timeout"),
                     flags=("--any", "--follow", "--replace", "--ambient",
                            "--on-behalf", "--per-row", "--once"),
                     usage=chat.HELP["wait"].splitlines()[0])
    if grc is not None:
        return grc
    timeout_arg = _flag(args, "--timeout")
    try:
        timeout = float(timeout_arg) if timeout_arg is not None else None
    except (TypeError, ValueError):
        print("helm chat wait: --timeout wants a number, got %r" % timeout_arg,
              file=sys.stderr)
        return 2
    if timeout is not None:
        import math
        if not math.isfinite(timeout):
            print("helm chat wait: --timeout wants a finite number, got %r"
                  % timeout_arg, file=sys.stderr)
            return 2
    follow = "--follow" in args
    replace = "--replace" in args
    any_row = "--any" in args
    ambient = "--ambient" in args
    session = _env_session()
    named = _flag(args, "--seat")
    # ARG SHAPE FIRST, IDENTITY SECOND (cheap-first): "who are you?" when the
    # flag COMBINATION is wrong sends the caller to fix the wrong thing.
    if replace and not follow:
        print("helm chat wait: --replace requires --follow — a single-shot "
              "delivery has no beacon to rotate", file=sys.stderr)
        return 2
    if replace and not named:
        print("helm chat wait: --replace requires --seat S so the process "
              "proves whose beacon it may rotate", file=sys.stderr)
        return 2
    # THE ONE-SHOT BEACON ENDS AFTER ONE RING, so it is the doorbell shape
    # only: `--any` watches a room with no cursor and `--per-row` streams a
    # line per row, and neither has one ring to end on.
    once = "--once" in args
    if once and not follow:
        print("helm chat wait: --once requires --follow — it is the one-shot "
              "beacon; a bare wait is a delivery, not a beacon",
              file=sys.stderr)
        return 2
    for clash in ("--any", "--per-row"):
        if once and clash in args:
            print("helm chat wait: --once cannot take %s — the one-shot "
                  "beacon ends after one doorbell ring" % clash,
                  file=sys.stderr)
            return 2
    _behalf, _ = actors.grant_on_behalf("beacon", named,
                                        stated="--on-behalf" in args)
    # THE SESSION IS THE SECOND IDENTITY SOURCE AND IT WAS ALREADY IN HAND,
    # one line up: a pane that declares no HELM_CHAT_NAME is still a named
    # seat when the roster binds its session to exactly one row, and helm's
    # own join hook is what wrote that row.
    claimed, err = _assert_own_seat(named, on_behalf=_behalf, session=session)
    if err:
        print(err, file=sys.stderr)
        return 2
    if _behalf and claimed:      # LOUD: the defect was that this was silent
        print("helm chat wait: acting ON BEHALF OF %r — no identity of its "
              "own, draining that seat's inbox" % claimed, file=sys.stderr)
    # ROTATION NEEDS CALLER OWNERSHIP, AND A RESOLUTION IS NOT ONE. Everything
    # else below this line acts on the caller's OWN inbox; --replace reaches
    # ANOTHER PROCESS — it skips beacons' incumbent-idempotence rung, SIGTERMs
    # the live waiter serving this seat, and registers itself as the wake route
    # in its place. A roster-derived identity is reproducible by every nameless
    # child of the pane (the session id is inherited), so admitting it here let
    # a descendant kill its own parent's waiter and divert the inbox. BEFORE any
    # signal or registry write, and the door that decides is in the identity
    # layer so there is one definition of "may rotate".
    # AND THE REAP IS THE SAME ACT UNDER ANOTHER NAME, which is why this door is
    # asked for EVERY follow arm and not only for --replace. `beacons.arm`
    # defaults `reap=True`, and on that default an incumbent the census cannot
    # prove — UNKNOWN, ghost, duplicate — falls straight past the idempotence
    # rung into `stop_superseded`: the very SIGTERM --replace asks permission
    # for, taken without asking. So ONE authority answer decides BOTH legs, and
    # a derived identity gets `reap=False`: it arms alongside whatever it cannot
    # prove, and beacons names the way in rather than clearing the way out.
    may_reap = False
    if claimed and follow:
        _rep, rep_err = actors.replacement_authority(
            claimed, on_behalf=_behalf, session=session)
        if replace and rep_err:
            print(rep_err, file=sys.stderr)
            return 2
        may_reap = not rep_err
    # A ROSTER-DERIVED CLAIM IS A RESOLUTION, NOT A PROOF, so it owes the
    # admission pass. `ambient_seat=not claimed` switched `actors.resolve_actor`
    # OFF exactly when a seat was NAMED — the one door that consults the roster,
    # disabled by the flag that says "I was given a seat". A name proven by the
    # env (or covered by a stated on-behalf capability) was given; a name this
    # process resolved for itself was not.
    derived_claim = bool(claimed) and not own_name() and not _behalf
    refusal = _beacon_identity_refusal(
        session, ambient_seat=(not claimed) or derived_claim,
        asserted=named) if follow else None
    if refusal:
        _emit_line(refusal)               # validate before any signal/write
        return 0
    # ORDINARY ARM IS IDEMPOTENT; DELIBERATE ROTATION SAYS --replace. This
    # leg IS the beacon, so this is the one place that may reap: a seat
    # stopping its OWN superseded waiter. Bare re-arm used to SIGTERM the
    # healthy Monitor that had just woken the agent, surfacing
    # exit 143 and prompting another re-arm. One attributable LIVE waiter
    # for this exact seat+session is already success; different/ghost/
    # duplicate states still flow through stop-then-start — but only WITH
    # authority. Scoped to `claimed` — the seat this process has PROVEN it
    # may act as, via _assert_own_seat above — so it can never reach another
    # fleet's waiters, AND to `may_reap`, so a roster-DERIVED identity arms
    # its own beacon without ever stopping anybody's. Only the --follow
    # shape registers: a single-shot `wait` is a delivery, not a beacon, and
    # must not stop the real one — it never reaches this call at all, which
    # is why the reap class has exactly ONE door (`git grep -n beacons.arm`
    # is this line and nothing else; `deliver`, `prepare` and `catchup`
    # reach no beacon stop, and `stop_superseded` has one caller, `arm`).
    armed = None
    if follow and claimed:
        try:
            from . import beacons
            spec = beacons.requested_waiter_spec(
                room, any_row=any_row, ambient=ambient, timeout=timeout,
                per_row="--per-row" in args, once=once)
            armed = beacons.arm(claimed, session=session, replace=replace,
                                reap=may_reap, waiter=spec)
        except Exception:               # noqa: BLE001
            armed = None                # a registry miss never stops a beacon
    if armed and armed.get("error"):
        print("[helm chat] inbox beacon arm DEGRADED — %s; stopped=%s, "
              "registered=%s. The waiter will continue unless the result "
              "below identifies an incumbent or scope conflict."
              % (armed["error"], armed.get("stopped") or [],
                 bool(armed.get("registered"))), file=sys.stderr)
    if armed and armed.get("alongside"):
        print("[helm chat] inbox beacon %s" % armed["alongside"],
              file=sys.stderr)
    if armed and armed.get("nonwaking"):
        # THE RULES LIVE IN seats_advice, WHICH OWNS THEM; this facade holds
        # the CALL. Rules here would fill the split budget the extraction
        # exists to drain.
        from . import seats_advice
        print(seats_advice.nonwaking_message(armed, claimed), file=sys.stderr)
    if armed and armed.get("already_live"):
        incumbent = armed["already_live"]
        print("[helm chat] inbox beacon already armed for seat %r, session "
              "%s at pid %s — duplicate --follow exits cleanly; use "
              "--replace only for deliberate rotation."
              % (claimed, str(incumbent.get("session") or "?")[:8],
                 incumbent["pid"]), file=sys.stderr)
        return 0
    if armed and armed.get("conflict"):
        incumbent = armed["conflict"]
        print("[helm chat] inbox beacon at pid %s serves the same seat+session "
              "with different wait behavior — nothing was rotated; use "
              "--replace to accept that scope change."
              % incumbent["pid"], file=sys.stderr)
        return 2
    line = None
    try:
        line = wait(seat=claimed, room=room, any_row=any_row, timeout=timeout,
                    # --follow (beacon) + --any-watch must use wait()'s
                    # FLUSHED sink (_emit_line) — passing bare print here
                    # overrode it and block-buffered every wake-line into
                    # oblivion on a Monitor pipe. Only single-shot seat mode
                    # keeps print (deliver emits one line + returns it).
                    emit=None if (follow or any_row) else print,
                    follow=follow, session=session,
                    # --ambient opts the beacon back into home-room wakes
                    # (quiet rooms); absent, wait() resolves the shape
                    # default (--follow ⇒ mention-only, single-shot ⇒ full).
                    ambient=True if ambient else None,
                    doorbell="--per-row" not in args,   # beacon_doorbell
                    once=once)
    finally:
        # The row names a PROCESS, so it must die with the process. A beacon
        # SIGTERMed mid-life never reaches here, which is why the census prunes
        # rows whose pid is proven gone rather than trusting this as the only
        # drain. A ONE-SHOT THAT RANG is the exception: its row is stamped,
        # not dropped, because the seat is re-arming in the turn the ring
        # starts and the census reads that gap as WAKING (beacons.once_age).
        if armed and armed.get("registered"):
            try:
                if once and line:
                    beacons.mark_ended(claimed)
                else:
                    beacons.release(claimed)
            except Exception:           # noqa: BLE001
                pass
    if follow:               # --follow streams via emit; returns on timeout
        return 0
    if line is None:
        return 1
    if "--any" in args:
        print(line)
    return 0


def cmd(verb, args, room="main", room_explicit=False, room_source=None):
    """The seats subverbs, reached through `helm chat <verb>`."""
    args = list(args or [])
    from . import hooklatency
    pair = None
    if verb == "deliver" and "--hook-json" in args:
        from . import posttoolrun
        pair = posttoolrun.current()
    if pair and pair.phase == "prepare":
        # The installed preparatory pass crosses cli.main's SAME scope door,
        # but must not recover rename, home, delegate, deliver, or latch first.
        try:
            d = _hook_stdin()
            hooklatency.bind(d.get("session_id"))
            behalf, _ = actors.grant_on_behalf("delivery", _flag(args, "--seat"),
                                               stated="--on-behalf" in args)
            own, err = _assert_own_seat(_flag(args, "--seat"), on_behalf=behalf,
                                        session=d.get("session_id")
                                        or _env_session())
            if err:
                pair.failure("identity refused: %s" % err)
            else:
                pair.prepare(d.get("session_id"), own, d)
        except Exception as exc:
            pair.failure("whisper preparation failed (%s: %s)" % (type(exc).__name__, exc))
        return 0
    from .seats_rename import recover_seat_rename
    if not recover_seat_rename():
        print("helm chat: interrupted rename is not recoverable", file=sys.stderr)
        if pair:
            pair.failure("interrupted rename is not recoverable")
        return 1
    if verb == "join":
        if "--hook-json" not in args:
            return join_cli(args, room, room_explicit, room_source)
        try:                        # the hook path is fail-open, always
            d = _hook_stdin()
            # A SIDECHAIN IS NOT THE SEAT (task/2542). A subagent's compaction
            # fires SessionStart inside the subagent, and the banner below
            # told it "you are seat X ... MANDATORY FIRST ACTION: arm your
            # beacon" — so it did, and took the seat's wake route. It hears the
            # rule instead and joins nothing: its session is the main
            # thread's, whose own SessionStart already joined. Keyed on the
            # documented agent_id; see actors.SIDECHAIN_RULE for why that is
            # unmeasured on this event and why its absence changes nothing.
            if actors.sidechain_agent(d):
                _hook_emit("SessionStart")(
                    actors.sidechain_notice(_flag(args, "--seat")))
                return 0
            cwd = d.get("cwd")
            room, room_source = _payload_homing(cwd, room, room_source)
            # ONLY A PANE ENROLS. Outside helm's project the join ran because
            # Orca opened this pane (hooks.orca_admits), so enrolment needs
            # Claude's own record to say the session IS the pane; inside it,
            # only a record that says otherwise refuses (task/2673 PART C).
            from .hooks import outside_helm
            _seat, line = join(session=d.get("session_id"),
                               cwd=cwd or safe_cwd(),
                               seat=_flag(args, "--seat"), room=room,
                               room_explicit=room_explicit,
                               room_source=room_source,
                               session_source=d.get("source"),
                               require_pane=outside_helm() is True,
                               owe_banner=True)
            if line:
                _hook_emit("SessionStart")(line)
            paid_join(d.get("session_id"))    # only once it is written
        except Exception:
            pass                    # never shape a session start
        return 0
    if verb == "deliver":
        try:
            session = cwd = agent = None
            unread, foreign = "", {}    # outside helm: own mail only; inside: no new kwarg
            if "--hook-json" in args:
                _UNPARSED[0] = ""
                d = _hook_stdin()
                agent = actors.sidechain_agent(d)  # task/2929: a subagent reads no lead row
                # A PAYLOAD THAT DID NOT PARSE NAMES NO THREAD: no agent_id
                # for the fence above, no session_id for the cursor. Spend
                # nothing; the lead's next readable boundary delivers.
                unread = _UNPARSED[0]
                if unread:
                    print("[helm chat] delivery skipped: the hook payload is "
                          "unreadable (%s); nothing was consumed" % unread,
                          file=sys.stderr)
                session, cwd = d.get("session_id"), d.get("cwd")
                hooklatency.bind(session)
                room, _ = _payload_homing(cwd, room, room_source)
                # Producer-side delegation evidence (`agent_id` is the hook's
                # subagent discriminator), never at the boundary's own cost.
                foreign = {"project_only": True} if hooks.orca_foreign() else {}
                try:
                    if not foreign:     # only the scoped SubagentStop clears it
                        _record_posttool_delegation(d)
                except Exception as exc:
                    hooklatency.mark("exception")
                    if pair:
                        pair.failure("delegation evidence failed (%s: %s)" % (type(exc).__name__, exc))
            emit = pair.emit if pair else (_hook_emit("PostToolUse") if "--hook-json" in args else print)
            from . import seats_join as _seats_join
            if "--hook-json" in args and not agent and not unread:
                emit = _seats_join.owed_emitter(emit, session)  # a killed join ends here
            _b, _ = actors.grant_on_behalf("delivery", _flag(args, "--seat"),
                                           stated="--on-behalf" in args)
            claimed, err = _assert_own_seat(_flag(args, "--seat"), on_behalf=_b,
                                            session=session or _env_session())
            if err:
                hooklatency.mark("unchecked")
                print(err, file=sys.stderr)
                if pair:
                    pair.failure("identity refused: %s" % err)
                return 0                # fail-open: never hold a tool boundary
            if pair:
                pair.delivery_allowed = True
            # MEASURED HERE TOO, because this boundary never enters `join`.
            # The fence that stops a consumer spending an addressed row it
            # cannot deliver lived inside the follow path, so the hook and
            # single-shot callers -- which pass `emit=print` -- consumed rows
            # into a sink that wakes nobody and were never asked. The
            # destination classifier is the same one `join` uses; a caller
            # whose emitter cannot be resolved still answers UNKNOWN and
            # consumes exactly as before.
            # ONE BOUNDARY, ONE PASS, AND NO LISTING OF ITS OWN (task/3848):
            # this process ends with the boundary, so it keeps no quiet proof
            # (remember=False) and only reads the room listing the last pass
            # made, while the room set has not moved.
            deliver_any(session=session, room=room, seat=claimed, emit=emit,
                        cwd=cwd, channel="hook" if "--hook-json" in args else None,
                        **foreign, sink_usable=not (agent or unread)
                        and _seats_join.destination_usable(emit, follower=False),
                        quiet=QuietRooms(remember=False))
            getattr(emit, "flush_owed", lambda: None)()
            if pair:
                pair.finish()           # after receipt, still under delivery's timer
                return 0                # no legacy second whisper/output
            # THE PER-TOOLCALL STEER, and it rides HERE because `record` is
            # bound by its own no-output law ("a recorder must never block or
            # noise a turn") while deliver already speaks at this exact
            # boundary and owns the emit contract. record runs FIRST in the
            # hook order, so the edit destination it just logged is on disk.
            # Never outside helm (a helm steer), never at a delivery's cost.
            try:
                from . import stop_early, toolwhisper  # stop_early: the owed-row line
                line = None if foreign else (toolwhisper.for_hook(session) or stop_early.for_hook(session, claimed, agent))
                if line:
                    emit(line)
            except Exception:
                hooklatency.mark("exception")
        except Exception as exc:
            hooklatency.mark("exception")
            if pair:
                pair.failure("delivery failed (%s: %s)" % (type(exc).__name__, exc))
                # A receipt failure after complete publication is terminal for
                # delivery. The whisper WAS published; latch it once if time
                # remains, without output or replaying the owner operation.
                try:
                    pair.commit_published()
                except Exception as latch_exc:
                    pair.failure("whisper latch UNKNOWN (%s: %s)" % (type(latch_exc).__name__, latch_exc))
        return 0
    if verb == "delegation-stop":
        try:
            if "--hook-json" in args:
                _clear_posttool_delegation(_hook_stdin())
        except Exception:
            pass                    # fail-open: never hold subagent teardown
        return 0
    if verb == "catchup":
        claimed = _flag(args, "--seat")
        junk = [a for a in args
                if a not in ("--apply", "--including-mentions")
                and a != "--seat" and a != claimed]
        if junk:
            print("helm chat catchup: unknown argument%s %s — takes only "
                  "[--room R] [--seat S] [--including-mentions] [--apply]"
                  % ("s"[:len(junk) != 1], " ".join(junk)), file=sys.stderr)
            return 2
        # PARKING MOVES CURSORS, and two guards left a hole BETWEEN them:
        # fail-open `_assert_own_seat` passed `--seat <victim>` through and
        # the DERIVED check sat behind `if not seat`.
        actor, aerr = actors.resolve_actor(_env_session(), safe_cwd(),
                                           asserted=claimed,
                                           act="catch up")
        if aerr:
            # "REFUSING" stays SHOUTED: this verb's refusal is read by a seat
            # that believes it has a backlog to park, and the loud form is what
            # its own pinned arm asserts on.
            print("helm chat catchup: REFUSING — " + aerr, file=sys.stderr)
            return 2
        seat = actor.canonical_name
        out = catchup(seat, room=room if room_explicit else None,
                      apply="--apply" in args,
                      include_addressed="--including-mentions" in args)
        return render_catchup(out, seat)
    if verb == "dm":
        sender, _serr = chat._seat_actor(args)
        if _serr:
            print("helm chat: " + _serr, file=sys.stderr)
            return 2
        # The recipient is a positional, so the flag scan and the one-body
        # rule start AFTER it; both live in chat.dm_argv beside the doors
        # they compose.
        to, text, rc = chat.dm_argv(args)
        if rc is not None:
            return rc
        row, err = dm(to, text, who=sender, session=_env_session())
        if err:
            print("helm chat: " + err, file=sys.stderr)
            return 1
        print("helm chat [dm] %s" % chat._fmt(row))
        return 0
    if verb == "ack":
        seat, _serr = chat._seat_actor(args)
        if _serr:
            print("helm chat: " + _serr, file=sys.stderr)
            return 2
        note = None
        if "--note" in args:
            i = args.index("--note")
            note, _nrc = freetext.tail("helm chat", "ack", args[i + 1:],
                                       "note text")
            if _nrc is not None:
                return _nrc
            note = (note or "").strip() or None
            del args[i:]                 # --note eats the rest of the line
        return render_ack(args, seat, note, _env_session())
    if verb == "receipts":
        return receipt_cli(args)
    if verb == "pending":
        seat = chat._seat_flag(args) or acting_seat(_env_session(), safe_cwd())
        if args:
            # FAIL LOUD on an unsupported selector. A `helm chat pending
            # pane:<seat>` that drops the selector on the floor and prints
            # the CALLER's rows is silently answering a DIFFERENT question
            # than the one asked, which is how a live investigation got misled
            # into trusting a wrong answer (owner-adjacent, 2026-07-24).
            print("helm chat pending: unsupported selector %r. `pending` is "
                  "always the OUTBOUND view of ONE sender — it takes no "
                  "positional selector. To see what is waiting FOR another "
                  "seat, read its pending column in `helm chat seats`."
                  % " ".join(args), file=sys.stderr)
            return 2
        return render_pending(seat, _env_session())
    if verb == "seat":
        if args[:1] == ["rename"] and len(args) >= 3:
            whole = "--row" in args
            dry = "--dry-run" in args
            wants_hours = "--alias-hours" in args
            hours = _flag(args, "--alias-hours")
            if wants_hours and hours is None:      # `-1` reads as a flag,
                print("helm chat seat rename: --alias-hours wants a finite, "
                      "non-negative number of hours", file=sys.stderr)
                return 2                          # never as a silent default
            args = [a for a in args if a not in ("--row", "--dry-run")]
            if len(args) < 3:
                print(chat.HELP["seat"], file=sys.stderr)
                return 2
            kw = {"whole_row": whole, "dry_run": dry}
            if hours is not None:
                kw["alias_hours"] = hours
            ok, msg = rename_seat(args[1], args[2], **kw)
            print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args[:1] in (["mute"], ["unmute"], ["mutes"]):
            sub = args.pop(0)
            who = chat._seat_flag(args) or acting_seat(_env_session(),
                                                       safe_cwd())
            if sub == "mutes":
                got = mutes(who)
                print("helm chat: %s mutes %s" % (
                    _seat_label(who),
                    ", ".join(_seat_label(g) for g in got)
                    if got else "nothing"))
                return 0
            if not args:
                print("usage: helm chat seat %s <room> [--seat S]" % sub,
                      file=sys.stderr)
                return 2
            ok, msg = set_mute(who, args[0], on=sub == "mute")
            print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args[:1] == ["disown"] and len(args) >= 3:
            to = _flag(args, "--to")
            ok, msg = disown_session(args[1], args[2], to=to)
            print("helm chat: " + msg,
                  file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args[:1] == ["rehome"] and len(args) >= 3:
            ok, msg = rehome_seat(args[1], args[2])
            print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
            return 0 if ok else 1
        if args[:1] == ["gc"]:
            rest = args[1:]
            if any(a != "--apply" for a in rest):
                print("usage: helm chat seat gc [--apply]   (dry-run default; "
                      "prunes only roster rows with NO live evidence — no "
                      "transcript, no live process, no fresh presence)",
                      file=sys.stderr)
                return 2
            rows, pruned = gc_roster(apply="--apply" in rest)
            if not rows:
                print("helm chat: roster empty — nothing to gc")
                return 0
            # launder BOTH columns: the seat KEY (a hostile HELM_CHAT_NAME) and
            # the why (it interpolates that same key — "carries HELM_CHAT_NAME=%s")
            labels = {id(r): _seat_label(r["seat"]) for r in rows}
            w = max(len(labels[id(r)]) for r in rows)
            for r in rows:
                print("  %-5s %-*s  %s"
                      % (r["verdict"].upper(), w, labels[id(r)],
                         _clip(_scrub(str(r["why"])).strip(), STATUS_BYTES)))
            n = sum(r["verdict"] == "prune" for r in rows)
            if "--apply" in rest:
                print("helm chat: pruned %d roster row%s (+ derived seat "
                      "state); %d kept on live evidence"
                      % (len(pruned), "s"[:len(pruned) != 1],
                         len(rows) - len(pruned)))
            else:
                print("helm chat: %d row%s would be pruned — dry-run "
                      "(`helm chat seat gc --apply` prunes)"
                      % (n, "s"[:n != 1]))
            return 0
        print("usage: helm chat seat rename <sid|oldname> <newname> "
              "[--dry-run] [--alias-hours H] | "
              "seat mute|unmute <room> [--seat S] | seat mutes [--seat S] | "
              "seat gc [--apply] | seat disown <seat> <sid> [--to <seat>] | "
              "rehome <sid|name> <room|main|none>   "
              "(rename is ROW-granular: a session-id target that names a row "
              "with other sessions needs --row; --dry-run prints the five "
              "surfaces it would change and writes nothing; the old name "
              "stays reachable for --alias-hours, default 24)",
              file=sys.stderr)
        return 2
    if verb == "stop-guard":
        from . import (projscope, record, seats_stop_budget,
                       seats_stop_claims, seats_stop_response,
                       seats_stop_timing)
        from .seats_stop_signals import _off
        if _off("STOP_GUARD"):
            return 0
        if "--detail" in args and "--hook-json" not in args:  # a read, no ladder
            return seats_stop_claims.lease_detail(room, _flag(args, "--seat"))
        session, stop_active, transcript, cwd = None, False, None, None
        # THE LADDER IS ARMED HERE. `State()` constructs the RungTiming, which
        # emits its first line and schedules the streaming flush, so THIS door
        # owns disarming it. `RungTiming.finish` settles the path that reaches
        # the terminal; the `finally` below is the belt for every path that
        # does not — an EXPIRED ladder (which publishes through a fallback rung
        # and holds no run identity), a raise out of publication, and whatever
        # exit is added next. One door, because a cancel added per exit is a
        # list that a later exit is left off.
        budget = seats_stop_budget.State()
        # FROM THE HOOK'S START, NOT THIS LINE'S: the wrapper's `timeout` was
        # armed before this interpreter existed, and startup spends it too.
        from . import procage
        deadline = time.monotonic() + seats_stop_budget.BUDGET_S \
            - (procage.hook_elapsed() or 0.0)
        try:
            with projscope.scope(deadline=deadline):
                try:
                    if "--hook-json" in args:
                        d = _hook_stdin()
                        session = d.get("session_id")
                        stop_active = bool(d.get("stop_hook_active"))
                        transcript = d.get("transcript_path")
                        cwd = d.get("cwd")
                        room, _ = _payload_homing(cwd, room, room_source)
                    blocks, warns = stop_guard(
                        session=session, room=room, seat=_flag(args, "--seat"),
                        stop_active=stop_active, transcript=transcript, cwd=cwd,
                        budget=budget, detail="--detail" in args)
                except projscope.Expired:
                    raise
                except Exception as exc:
                    return record.turn_closed(
                        session, seats_stop_response.publish_failure(exc, budget))
                # bug-class empty-reason-refusal: content and exit status are
                # one decision. Blank blockers fail open; blank warns are noise.
                warns = [w for w in warns if isinstance(w, str) and w.strip()]
                blocks = [b for b in blocks if isinstance(b, str) and b.strip()]
                budget.blocks, budget.warns = blocks, warns
                if budget.expired:
                    raise projscope.Expired("Stop ladder coverage incomplete")
                # rc 0 ENDS THE TURN, so the door dates it (helm/seat_idle.py)
                return record.turn_closed(
                    session, seats_stop_response.publish(blocks, warns, budget))
        except projscope.Expired:
            blocks, warns = budget.expire()
            return record.turn_closed(session, seats_stop_response.publish_expired(
                blocks, warns, stop_active=stop_active))
        finally:
            seats_stop_timing.settle()
    if verb == "wait":
        return _cmd_wait(args, room)
    if verb == "seats":
        return render_roster(room, "--all" in args)
    if verb == "status":
        # `helm chat status <one-line>` sets, `--clear` clears, bare shows —
        # the ICQ away-message: one glanceable line on the seat's roster row
        clear = "--clear" in args
        if clear:
            args = [a for a in args if a != "--clear"]
        # READ BEFORE THE POP: `_seat_flag` DELETES the pair from argv, so a
        # second call answers None and a guard keyed on it never fires.
        asserted = "--seat" in args
        who = chat._seat_flag(args) or acting_seat(_env_session(), safe_cwd())
        # THE MEASURED VICTIM OF THIS HOLE. `helm chat status --help` STORED
        # the literal string "--help" as a seat's status line instead of
        # printing usage, and the row it wrote was read by another seat.
        text, _src = freetext.tail("helm chat", "status", args, "a status line")
        if _src is not None:
            return _src
        text = (text or "").strip()
        if text and clear:
            print("usage: helm chat status [<one-line> | --clear] [--seat S]",
                  file=sys.stderr)
            return 2
        if not text and not clear:            # bare: show the current line
            return render_status(who)
        # the WRITER is always the ambient identity — a cross-seat write
        # (--seat != self) is allowed but recorded (status_by, post parity)
        writer, werr, wreason = actors.resolve_actor_reason(
            _env_session(), safe_cwd(), act="record a status write")
        # THE PREDICATE AND ITS WHOLE CONTRACT LIVE IN
        # `seats_common.status_write_refused`, beside `recipient_matches`,
        # whose casefold relation it depends on. `acting_seat` is passed as
        # a THUNK because the laziness is load-bearing: the acting identity
        # is read only on the exempt tier.
        if status_write_refused(
                werr, wreason, asserted, who,
                lambda: acting_seat(_env_session(), safe_cwd())):
            print("helm chat: " + werr, file=sys.stderr)
            return 2
        ok, msg = set_status(who, None if clear else text, by=writer)
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "claim":
        if not args:
            print("usage: helm chat claim <resource> [--ttl SECONDS] [--seat S] "
                  "[--lease ID to extend]   (the printed lease id confirms "
                  "release; `helm chat claims` reprints your own)",
                  file=sys.stderr)
            return 2
        # A MALFORMED --ttl IS REFUSED BEFORE ANY IDENTITY IS RESOLVED.
        ttl, aerr = ttl_flag(args, DEFAULT_TTL)
        # session comes ONLY from the ambient harness env — never a flag: a
        # roster-visible SID must not be assertable through the CLI
        actor, aerr = (None, aerr) if aerr else actors.resolve_actor(
            _env_session(), safe_cwd(), asserted=_flag(args, "--seat"),
            act="claim a lease")
        if aerr:
            print("helm chat: " + aerr, file=sys.stderr)
            return 2
        try:
            ok, msg, _lease = claim(
                args[0], actor.canonical_name,
                ttl=ttl,
                lease=_flag(args, "--lease"), session=_env_session())
        except OSError as exc:      # an unreadable ledger: UNKNOWN, unwritten
            ok, msg = False, "REFUSED — %s; nothing claimed" % exc
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "release":
        if not args:
            print("usage: helm chat release <resource> --lease ID [--seat S]",
                  file=sys.stderr)
            return 2
        # RELEASE IS AUTHORIZED BY THE TOKEN, NOT BY A NAME: `_binding_ok`
        # requires {lease, holder, granting session} together and only the
        # holder was handed the nonce, so presenting it IS the proof.
        # `--seat` ADDRESSES the holder row.
        seat = _flag(args, "--seat") or acting_seat(_env_session(), safe_cwd())
        try:
            ok, msg = release(args[0], seat, lease=_flag(args, "--lease"),
                              session=_env_session())
        except OSError as exc:      # an unreadable ledger: UNKNOWN, unwritten
            ok, msg = False, "REFUSED — %s; nothing released" % exc
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "claims":
        return _cmd_claims(args)
    if verb in ("verdict", "reveal", "council-status", "council-abort"):
        return _cmd_council(verb, args)
    print("helm chat: unknown subcommand '%s'" % verb, file=sys.stderr)
    return 2
