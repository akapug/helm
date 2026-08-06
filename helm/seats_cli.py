#!/usr/bin/env python3
"""helm seats — the CLI verbs and the hook legs, dispatched from chat.cmd_chat.

THE HOOK LEGS ARE WHY THIS IS NOT JUST ARGUMENT PARSING. A hook runs in a
process helm does not own, reading a payload on stdin from a harness that may
never send one, and the seat's whole liveness depends on it not hanging. So
_hook_stdin has a DEADLINE and a stall note rather than a blocking read: a
hook that waits forever on an empty pipe is indistinguishable from a wedged
agent, and the fleet has spent real hours on that confusion.

THIS MODULE IS THE TOP OF THE LAYERING and imports from nearly every sibling,
which is exactly right for a dispatcher — it is the one place that is allowed
to know about everything, because knowing about everything IS its job. The
direction matters: siblings must never import back from here. The one that
does — claims, for a single session-id read — takes a deferred import at its
call site rather than making this module a dependency of the layer below it.
"""

import json
import os
import sys
import time

from . import chat, home, pk
from .seats_common import (DEFAULT_TTL, STATUS_BYTES, _clip, _scrub, _seat_key,
                           _seat_label, dm_lane, own_name, recipient_matches,
                           roster)
from .seats_identity import (_assert_own_seat, _mention_re, acting_seat,
                             derive_seat, resolve_homing, safe_cwd)
from .seats_roster import (disown_session, last_seen, mutes, rehome_seat,
                           rename_seat, set_mute)
from .seats_delivery import _baseline_state, _write_cursor, deliver_any, dm
from .seats_join import join, wait
from .seats_ack import _ts_epoch, ack, pending
from .seats_delegation import (_clear_posttool_delegation,
                               _record_posttool_delegation)
from .seats_stop_signals import _pending_all
from .seats_claims import (_pub_res, claim, claim_liveness_mark, claims_list,
                           own_leases, release)
from .seats_report import (_claims_by_holder, _fmt_age, _status_age, _status_by,
                           gc_roster, presence_dot, presence_of, roster_report,
                           runtime_label, set_status, status_line)
from .seats_stop_guard import stop_guard

HOOK_STDIN_DEADLINE_S = 2.0   # generous: the harness writes its JSON at once
_HOOK_STDIN_STALL = "hook-stdin-stall"   # the receipt a silent pass never left
def _hook_stdin():
    """Hook JSON off stdin, bounded in TIME as well as bytes.

    THIS DOCSTRING USED TO SAY "bounded" AND MEAN THE WRONG DIMENSION. The old
    body was `sys.stdin.buffer.read(65536)`, which bounds BYTES: on a pipe that
    stays open and never delivers, a buffered read blocks until it can fill the
    buffer or sees EOF — forever. It answered "can this read too much?" and not
    "can this wait forever?", and the word `bounded` was true of the neighbour.

    WHAT THAT COST, measured against the owner's own observation
    that stop-hook errors kept appearing after they were "fixed":

      stdin closed            -> exit 2 in 0.3s   (the guard runs, blocks a stop)
      stdin open, no data     -> BLOCKS; killed by the hook's `timeout 5`
      real hook JSON piped    -> exit 2 in 0.3s   (fine)

    The hook wrapper is `timeout 5 helm chat stop-guard --hook-json; rc=$?;
    [ "$rc" = 2 ] && exit 2; exit 0`. A timeout kill is rc 124, which is not 2,
    so the wrapper returns 0 and THE STOP IS ALLOWED. Every one of those visible
    "Stop hook error" lines was a stop-guard that never ran at all: obligations
    unchecked, leases unreported, and no record anywhere that it had happened.
    Two rounds of fixes to what the guard DECIDES could never help, because it
    was dying before it decided anything.

    So: wait for readability with a deadline, drain what is actually there, and
    on a stall return {} FAST. The caller then runs with defaults — a guard that
    checks with less input is worth infinitely more than one killed mid-read.

    AND THE STALL LEAVES A RECEIPT. A silent degrade is what made this invisible
    for as long as it lasted; `_hook_stall_note` records it so the next question
    about a flaky stop hook is answerable from data instead of a live repro."""
    import select
    deadline = time.time() + HOOK_STDIN_DEADLINE_S
    buf = b""
    try:
        fd = sys.stdin.buffer.fileno()
    except Exception:                        # noqa: BLE001
        # NO REAL FD — an in-memory stdin (BytesIO in the suite, a harness that
        # substitutes a file object). Those cannot block on a pipe, so the
        # plain read is both correct and the only thing available. Returning {}
        # here instead COST A REAL REGRESSION: it silently dropped the payload
        # of every hook whose stdin was substituted, which the join hook-leg
        # test caught immediately. Never let a fix for the blocking path
        # swallow the non-blocking one.
        return _hook_stdin_plain()
    while True:
        left = deadline - time.time()
        if left <= 0:
            _hook_stall_note(len(buf))
            return {}
        try:
            ready, _w, _x = select.select([fd], [], [], left)
        except Exception:                    # noqa: BLE001 — unselectable stdin
            if buf:
                break                        # parse what we already drained
            return _hook_stdin_plain()       # nothing read yet: use the plain path
        if not ready:
            _hook_stall_note(len(buf))
            return {}
        try:
            chunk = os.read(fd, 65536)
        except Exception:                    # noqa: BLE001
            break
        if not chunk:                        # EOF: everything the harness sent
            break
        buf += chunk
        if len(buf) >= 65536:
            break
        try:                                 # a complete document ends the read
            d = json.loads(buf)
            return d if isinstance(d, dict) else {}
        except Exception:                    # noqa: BLE001 — partial, keep going
            continue
    try:
        d = json.loads(buf or b"{}")
        return d if isinstance(d, dict) else {}
    except Exception:                        # noqa: BLE001
        return {}
def _hook_stdin_plain():
    """The pre-deadline read, kept for stdin that has no file descriptor.
    Correct there precisely because an in-memory stream cannot block."""
    try:
        d = json.loads(sys.stdin.buffer.read(65536) or b"{}")
        return d if isinstance(d, dict) else {}
    except Exception:                        # noqa: BLE001
        return {}
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
def _flag(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default
_COUNCIL_FLAGS = ("--tip", "--evidence", "--seat", "--threshold")
def _council_positionals(args):
    """Positional args with every known flag AND its value removed — a bare
    split would read `--tip`'s value as the verdict."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in _COUNCIL_FLAGS:
            skip = True
            continue
        if a.startswith("--"):
            continue
        out.append(a)
    return out
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
    rows = claims_list()
    # join on the PUBLISHED key (_pub_res both sides) AND on holder: two raw
    # keys can scrub/clip down to one published string, and without the holder
    # test such a collision would stamp "(yours)" on a stranger's row. Only
    # ever this seat's own token either way.
    own = {_pub_res(r): v for r, v in own_leases(me).items()}
    for c in rows:                        # the holder's own token, handed back
        c["lease"] = own.get(c["resource"]) if c["holder"] == me else None
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
            claim_liveness_mark(c),
            ("  lease %s (yours)" % c["lease"]) if c["lease"] else ""))
    return 0
def _cmd_council(verb, args):
    """The council verbs — the FORMAL convergence species (0.3's N-of-M, now
    landed). Identity is AMBIENT (chat._seat_actor): a signal binds MEMBER
    IDENTITY, so a claimed --seat would let one seat cast another's sealed
    judgment — the exact footgun the dm/ack actor-binding rule ("--seat
    asserts ambient, never selects the signer") closed for signing."""
    from . import council
    rest = _council_positionals(args)
    room = rest[0] if rest else None
    if not room:
        print("helm chat %s: needs a council room (helm chat council invite "
              "<members> <topic>)" % verb, file=sys.stderr)
        return 2
    seat, serr = chat._seat_actor(args)
    if serr:
        print("helm chat %s: %s" % (verb, serr), file=sys.stderr)
        return 2
    if verb == "council-status":
        print("\n".join(council.status_lines(room)))
        return 0
    if verb == "council-abort":
        reason = " ".join(x for x in rest[1:] if not x.startswith("--"))
        reg, err = council.abort(room, seat, reason)
        if err:
            print("helm chat council-abort: " + err, file=sys.stderr)
            return 2
        chat.post("[COUNCIL %s] ABORTED by %s — %s. The embargo is permanent; "
                  "collaborate in a standup, then reconvene on the superseding "
                  "tip." % (room, chat._dsan(seat), reg.get("abort_reason")),
                  room=room, who=seat, sign=False)
        print("COUNCIL %s ABORTED — no reveal, ever (an aborted council's "
              "judgments were not formed independently)" % room)
        return 0
    if verb == "reveal":
        signals, err = council.reveal(room)
        if err:
            print("helm chat reveal: " + err, file=sys.stderr)
            return 2
        label, counts = council.outcome(signals)
        print("COUNCIL %s REVEALED — %d sealed judgment(s), embargo lifted:"
              % (room, len(signals)))
        for s in signals:
            print("  %s: %s @ %s — %s" % (chat._dsan(s["seat"]), s["verdict"],
                                          s["tip"][:12], s["evidence"] or "(no evidence ref)"))
        # quorum is a REVEAL bar, not a decision — say what the judgments add
        # up to rather than let "quorum reached" be misread as "ratified"
        print("OUTCOME: %s (%s)" % (label, ", ".join(
            "%s %d" % (v, counts[v]) for v in council.VERDICTS)))
        chat.post("[COUNCIL %s] QUORUM — embargo lifted, %d judgment(s) on the "
                  "record: %s" % (room, len(signals),
                                  ", ".join("%s %s" % (chat._dsan(s["seat"]), s["verdict"])
                                            for s in signals)),
                  room=room, who=seat, sign=False)
        return 0
    # verdict = SIGNAL (sealed)
    verdict = rest[1] if len(rest) > 1 else None
    tip = _flag(args, "--tip")
    evidence = _flag(args, "--evidence")
    reg, err = council.signal(room, seat, verdict, tip, evidence)
    if err:
        print("helm chat verdict: " + err, file=sys.stderr)
        return 2
    n, k, is_open = council.tally(room)
    print("SEALED — your judgment is recorded and EMBARGOED (%d of %d)" % (n, k))
    # NO PUBLIC PRE-QUORUM PROGRESS ROW — deleted, not patched (re-gated in
    # cross-family review).
    # Two rounds of trying to publish progress "safely" both leaked:
    #   r1: posted as who=signer — the row's own from field named the seat the
    #       text promised to hide.
    #   r2: posted as who=convener — better, but chat.post still touches the
    #       CALLING seat's presence cross-seat, and a public "1 of 2" identifies
    #       the other signer by elimination anyway.
    # The count was never worth it: `helm chat council-status <room>` already
    # serves the tally on demand to anyone entitled to ask. A guarantee you have
    # to keep narrowing is not a guarantee — so the embargo is now enforced by
    # NOT EMITTING, which is the only version of "zero WHO" that is true.
    # The signer still gets their private local confirmation above.
    if is_open:
        print("QUORUM REACHED — reveal: helm chat reveal %s" % room)
    return 0
def _env_session():
    return home.session_id()
def _hook_emit(event):
    """ONE unbuffered write of the whole hook response: no
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
    pass through untouched."""
    if not cwd or room_source != "derived":
        return room, room_source
    r2, s2 = resolve_homing(None, cwd)
    if not r2:
        return "main", None                     # payload cwd is project-less
    return r2, ("derived" if s2 == "derived" else None)
def _addressed(m, seat):
    """Is this row a DIRECT ask of `seat` — dm, @mention, or reply-to-me?
    Every direct ask must also be deliverable. Reaction additions deliberately
    sit outside this catchup/ACTED tier: they wake the target author as an
    attention signal, but never become work the author must acknowledge."""
    if m.get("dm"):
        return (recipient_matches(m["dm"], seat)
                or bool(seat) and str(m.get("room") or "") == dm_lane(seat))
    text = str(m.get("text") or "")
    if _mention_re(seat).search(text):
        return True
    return recipient_matches(m.get("rfrom"), seat)
def catchup(seat, room=None, apply=False, session=None,
            include_addressed=False):
    """Park the seat's OWN pending backlog, deliberately and accountably.

    Mute and catchup are different acts over one decision: MUTE silences the
    ambient wake (mentions pierce, by law) while the backlog stays pending —
    correctly, because a direct ask is an obligation. This verb is the
    missing decision: "seen, park it." Measured live: the integrator
    muted a flooded room and its stop-guard kept re-arming on the churning
    backlog; the mute worked, the guard worked, and the decision being made
    had no verb.

    THREE LAWS, each from a live incident:
      - SELF-ONLY: a seat parks its own backlog, never another seat's —
        parking someone else silently hides THEIR obligations.
      - ADDRESSED ROWS NEED THE FLAG (an affected-party verdict from a
        live meld): catchup is used reflexively, and
        ambient noise is what "I have seen this pile" means; a direct ask is
        precisely what it does not mean. Default parks ambient only; a room
        whose pending contains addressed rows is REFUSED in default mode —
        cursors are single offsets, so parking "around" an addressed row is
        positionally impossible, and pretending otherwise would park it.
      - PARKING AN ADDRESSED ROW LEAVES A DURABLE TRACE readable by its
        senders: a row posted INTO the parked room naming who parked how
        many asks from whom. Withholding never removes access — nor
        accountability. (Senders are named, not @mentioned: the trace must
        be readable, not a wake storm.)

    Parked rows are never deleted or hidden — they remain readable via
    `helm chat read`; only delivery cursors move. On apply, per room, one
    act: seat-level cursor to end-of-file, every session-scoped cursor of
    THIS seat to end-of-file, the stop-guard fingerprint latch cleared.
    """
    pend = _pending_all(room or "main", seat, session)
    if room:
        pend = [(r, m) for r, m in pend if r == room]
    by_room = {}
    for r, m in pend:
        by_room.setdefault(r, []).append(m)
    report = {"rooms": {}, "applied": bool(apply), "parked": 0, "held": 0}
    key = _seat_key(seat)
    for r in sorted(by_room):
        rows = by_room[r]
        addressed = [m for m in rows if _addressed(m, seat)]
        info = {"rows": [{"from": m.get("from"),
                          "text": str(m.get("text") or "")[:80],
                          "addressed": _addressed(m, seat)} for m in rows],
                "addressed": len(addressed)}
        report["rooms"][r] = info
        if addressed and not include_addressed:
            info["held"] = len(rows)     # positional truth: all or nothing
            report["held"] += len(rows)
            continue
        info["parking"] = len(rows)
        report["parked"] += len(rows)
        if not apply:
            continue
        if addressed:
            # the durable trace, posted BEFORE the cursors move so a crash
            # between the two leaves over-accounting, never a silent park
            senders = {}
            for m in addressed:
                frm = str(m.get("from") or "?")
                senders[frm] = senders.get(frm, 0) + 1
            named = ", ".join("%d from %s" % (n, chat._dsan(f))
                              for f, n in sorted(senders.items()))
            chat.post("catchup: %s parked %d addressed row%s without "
                      "answering (%s). The rows remain above, readable — "
                      "this trace is the accountability, not the answer."
                      % (chat._dsan(str(seat)), len(addressed),
                         "s"[:len(addressed) != 1], named),
                      room=r, who=seat)
        st = _baseline_state(r, at_start=False)
        _write_cursor(r, seat, *st, base=st[2])
        prefix = "%s.cursor.%s." % (pk.slug(r), key)
        latch = "%s.stopfp.%s" % (pk.slug(r), key)
        try:
            names = os.listdir(chat.chat_dir())
        except OSError:
            names = []
        for name in names:
            path = os.path.join(chat.chat_dir(), name)
            if name.startswith(prefix):        # session-scoped cursors, mine
                cur = pk.read_json(path, None)
                if isinstance(cur, dict) and isinstance(cur.get("off"), int):
                    pk.write_json(path, {
                        "dev": st[0], "ino": st[1], "off": st[2],
                        "rid": st[3], "active": bool(cur.get("active")),
                        "base": st[2]})
            elif name == latch or name.startswith(latch + "."):
                try:
                    os.remove(path)            # no stale block state survives
                except OSError:
                    pass
    return report
def cmd(verb, args, room="main", room_explicit=False, room_source=None):
    """The seats subverbs, reached through `helm chat <verb>`."""
    args = list(args or [])
    if verb == "join":
        try:
            session = cwd = session_source = None
            if "--hook-json" in args:
                d = _hook_stdin()
                session, cwd = d.get("session_id"), d.get("cwd")
                session_source = d.get("source")
                room, room_source = _payload_homing(cwd, room, room_source)
            seat, line = join(session=session, cwd=cwd or safe_cwd(),
                              seat=_flag(args, "--seat"), room=room,
                              room_explicit=room_explicit,
                              room_source=room_source,
                              session_source=session_source)
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
                room, _ = _payload_homing(cwd, room, room_source)
                # Producer-side delegation evidence: `agent_id` is the hook
                # contract's positive subagent discriminator. Its own fail-
                # closed path must never cost the ordinary delivery boundary.
                try:
                    _record_posttool_delegation(d)
                except Exception:
                    pass
            emit = _hook_emit("PostToolUse") if "--hook-json" in args else print
            claimed, err = _assert_own_seat(_flag(args, "--seat"))
            if err:
                print(err, file=sys.stderr)
                return 0                # fail-open: never hold a tool boundary
            deliver_any(session=session, room=room,   # every room, one nudge
                        seat=claimed, emit=emit, cwd=cwd)
            # THE PER-TOOLCALL STEER, and it rides HERE because `record` is
            # bound by its own no-output law ("a recorder must never block or
            # noise a turn") while deliver already speaks at this exact
            # boundary and owns the emit contract. record runs FIRST in the
            # hook order, so the edit destination it just logged is on disk
            # before this reads it. Walled in its own try: a steer must never
            # cost a delivery.
            try:
                from . import toolwhisper
                line = toolwhisper.for_hook(session)
                if line:
                    emit(line)
            except Exception:
                pass
        except Exception:
            pass                    # fail-open: never hold a tool boundary
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
        seat, err = _assert_own_seat(claimed)
        if err:
            print(err.replace("cannot receive for", "cannot catch up for"),
                  file=sys.stderr)
            return 2
        seat = seat or own_name()
        out = catchup(seat, room=room if room_explicit else None,
                      apply="--apply" in args,
                      include_addressed="--including-mentions" in args)
        if not out["parked"] and not out["held"]:
            print("helm chat catchup: nothing pending for %s — nothing to "
                  "park" % seat)
            return 0
        for r, info in sorted(out["rooms"].items()):
            state = ("HELD — %d addressed row(s); answer them or add "
                     "--including-mentions" % info["addressed"])                 if info.get("held") else "parking %d" % info.get("parking", 0)
            print("  [%s] %s" % (r, state))
            for m in info["rows"]:
                print("    %s%s: %s" % (
                    "@" if m["addressed"] else " ",
                    chat._dsan(str(m.get("from") or "?")),
                    str(m.get("text") or "")[:70]))
        if out["applied"] and out["parked"]:
            print("helm chat catchup: parked %d row%s for %s — cursors at "
                  "end-of-file, stop-guard latch cleared, addressed parks "
                  "traced in-room. The rows remain readable: helm chat read"
                  % (out["parked"], "s"[:out["parked"] != 1], seat))
        elif not out["applied"] and out["parked"]:
            print("helm chat catchup: DRY RUN — %d row%s would be parked"
                  "%s; add --apply."
                  % (out["parked"], "s"[:out["parked"] != 1],
                     (", %d held" % out["held"]) if out["held"] else ""))
        elif out["held"]:
            print("helm chat catchup: nothing parked — every pending room "
                  "holds addressed rows. Answer them, or park deliberately "
                  "with --including-mentions (leaves an in-room trace).")
        return 0
    if verb == "dm":
        sender, _serr = chat._seat_actor(args)
        if _serr:
            print("helm chat: " + _serr, file=sys.stderr)
            return 2
        to = args[0] if args else None
        text = " ".join(args[1:]).strip()
        if to and not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        if not (to and text):
            print("usage: helm chat dm <seat> <text...> [--seat S]  "
                  "(one private recipient — never a room)", file=sys.stderr)
            return 2
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
            note = " ".join(args[i + 1:]).strip() or None
            del args[i:]                 # --note eats the rest of the line
        tid = args[0] if args else None
        if not tid:
            print("usage: helm chat ack <id> [done|blocked] [--note ...] "
                  "[--seat S]", file=sys.stderr)
            return 2
        state = args[1] if len(args) > 1 else "done"
        res, err = ack(tid, state, note=note, who=seat,
                       session=_env_session())
        if err:
            print("helm chat: " + err, file=sys.stderr)
            return 1
        tgt = str(res["target"].get("id") or "")[:8]
        lbl = res["state"].upper()
        sender = chat._dsan(str(res["target"].get("from") or "?"))
        if res["dup"]:
            print("helm chat: %s already acked %s by %s — no new row "
                  "(idempotent)" % (tgt, lbl, _seat_label(seat)))
            return 0
        tail = (': "%s"' % _clip(_scrub(note), 80)) if note else ""
        print("helm chat: acked %s %s%s — @%s watches it leave `helm chat "
              "pending`" % (tgt, lbl, tail, sender))
        return 0
    if verb == "pending":
        seat = chat._seat_flag(args) or acting_seat(_env_session(), safe_cwd())
        if args:
            # FAIL LOUD on an unsupported selector. `helm chat pending
            # pane:console-design` used to drop the selector on the floor and
            # print the CALLER's rows — silently answering a DIFFERENT question
            # than the one asked, which is how a live investigation got misled
            # into trusting a wrong answer.
            print("helm chat pending: unsupported selector %r. `pending` is "
                  "always the OUTBOUND view of ONE sender — it takes no "
                  "positional selector. To see what is waiting FOR another "
                  "seat, read its pending column in `helm chat seats`."
                  % " ".join(args), file=sys.stderr)
            return 2
        items, total = pending(seat=seat, session=_env_session())
        if not items:
            print("helm chat: nothing outbound is waiting — every addressed "
                  "message you sent is consumed (acted) ✓")
            return 0
        now = time.time()
        n_sent = sum(1 for it in items if it["state"] == "sent")
        n_seen = sum(1 for it in items if it["state"] == "seen")
        n_unres = sum(1 for it in items if it["state"] == "unresolved")
        n_unk = sum(1 for it in items if it["state"] == "unknown")
        head = "%d SENT-not-SEEN, %d SEEN-not-ACTED" % (n_sent, n_seen)
        if n_unres:
            head += ", %d UNRESOLVED (no such seat)" % n_unres
        if n_unk:
            head += ", %d UNREADABLE lane%s" % (n_unk, "s"[:n_unk != 1])
        print("helm chat pending (as %s) — %d addressed row%s awaiting consume "
              "(%s):" % (_seat_label(seat), total, "s"[:total != 1], head))
        for it in items:
            where = ("dm" if it["dm"] else "main" if it["room"] == "main"
                     else "#" + it["room"])
            if it["state"] == "unknown":
                print("  ⚠ UNKN %-8s   %-14s %-7s        lane unreadable — "
                      "state UNKNOWN, NOT consumed" % ("", "?", where))
                continue
            glyph, st = (("⊘", "GONE") if it["state"] == "unresolved"
                         else ("○", "SENT") if it["state"] == "sent"
                         else ("◐", "SEEN"))
            age = _fmt_age(now - (_ts_epoch(it["ts"]) or now))
            print("  %s %-4s %-8s → %-14s %-7s %4s  %s"
                  % (glyph, st, str(it["id"] or "")[:8], _seat_label(it["to"]),
                     where, age, _clip(_scrub(it["text"]), 60)))
        print("  ○ SENT = never surfaced (recipient dead / away / wedged?)   "
              "◐ SEEN = surfaced, not acted   ⊘ GONE = no seat answers that "
              "@name (typo / never online)   ·   they close it: helm chat "
              "ack <id> done|blocked")
        return 0
    if verb == "seat":
        if args[:1] == ["rename"] and len(args) >= 3:
            whole = "--row" in args
            args = [a for a in args if a != "--row"]
            ok, msg = rename_seat(args[1], args[2], whole_row=whole)
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
        print("usage: helm chat seat rename <sid|oldname> <newname> | "
              "seat mute|unmute <room> [--seat S] | seat mutes [--seat S] | "
              "seat gc [--apply] | seat disown <seat> <sid> [--to <seat>] | "
              "rehome <sid|name> <room|main|none>   "
              "(rename is ROW-granular: a session-id target that names a row "
              "with other sessions needs --row)",
              file=sys.stderr)
        return 2
    if verb == "stop-guard":
        try:
            session, stop_active, transcript, cwd = None, False, None, None
            if "--hook-json" in args:
                d = _hook_stdin()
                session = d.get("session_id")
                stop_active = bool(d.get("stop_hook_active"))
                # the Stop payload carries the path to THIS session's
                # transcript — the only way a rung can read what the turn
                # actually promised the owner
                transcript = d.get("transcript_path")
                # ... and the session's cwd — the only checkout the whisper's
                # staged-deletion probe may read. PAYLOAD-ONLY on purpose: a
                # safe_cwd() fallback would let the guard process's own cwd
                # (the test runner's, a manual invoker's) leak real-repo
                # state into a session it does not belong to.
                cwd = d.get("cwd")
                room, _ = _payload_homing(d.get("cwd"), room, room_source)
            blocks, warns = stop_guard(session=session, room=room,
                                       seat=_flag(args, "--seat"),
                                       stop_active=stop_active,
                                       transcript=transcript, cwd=cwd)
        except Exception as exc:
            # FAIL-OPEN, AND LOUD. Never wedging a stop is right — a guard
            # that can wedge the fleet is worse than one that misses a row.
            # But returning 0 SILENTLY made a crashed guard indistinguishable
            # from a clean one: the seat reads an ordinary allow and believes
            # its inbox, leases and lanes were examined when nothing was.
            # That is the delivery promise's fifth escape, and
            # it is the same shape as the fourth — an error becoming silence,
            # silence reading as "nothing pending". The stop still passes; the
            # reader is simply told the check did not happen.
            print("[helm stop-guard] THE GUARD COULD NOT RUN (%s: %s) — this "
                  "stop is ALLOWED and UNCHECKED: your inbox, your claim "
                  "leases and your lanes were NOT examined. Nothing here says "
                  "they are clear."
                  % (type(exc).__name__, exc), file=sys.stderr)
            return 0
        # bug-class empty-reason-refusal: content and exit status are one
        # decision. An unexplained blocker fails open; a blank warning is noise.
        warns = [w for w in warns if isinstance(w, str) and w.strip()]
        blocks = [b for b in blocks if isinstance(b, str) and b.strip()]
        if blocks:                  # ALL blockers in ONE exit-2 (one-shot fix)
            # BLOCKS ONLY on this exit. Claude Code renders EVERY exit-2 stop
            # hook emission as a red "Stop hook error:", so an advisory
            # printed beside a block turns "stop allowed, lease retained" —
            # correct behavior — into error text (measured live:
            # the owner watched exactly that red line all evening). And
            # inside a block the reader's attention budget belongs to the
            # cure; reassurances about OTHER leases are noise there. The
            # advisories are recomputed on the re-stop that follows the
            # cure, so nothing is lost — it is deferred to the exit where
            # it is true.
            print("\n".join(blocks), file=sys.stderr)
            return 2
        for w in warns:             # advisories ride the ALLOW exit only
            print(w, file=sys.stderr)
        return 0
    if verb == "wait":
        timeout = _flag(args, "--timeout")
        follow = "--follow" in args
        claimed, err = _assert_own_seat(_flag(args, "--seat"))
        if err:
            print(err, file=sys.stderr)
            return 2
        # RE-ARM IS STOP-THEN-START, NEVER START. This leg IS the beacon, so
        # this is the one place that may reap: a seat stopping its OWN
        # superseded waiter, which is the designed lifecycle. Every re-arm used
        # to be a bare START, so each one left its predecessor running and the
        # fleet-wide count only ever climbed (48 the night this landed). Scoped
        # to `claimed` — the seat this process has PROVEN it may act as, via
        # _assert_own_seat above — so it can never reach another fleet's
        # waiters. Only the --follow shape registers: a single-shot `wait` is a
        # delivery, not a beacon, and must not stop the real one.
        armed = None
        if follow and claimed:
            try:
                from . import beacons
                armed = beacons.arm(claimed, session=_env_session())
            except Exception:               # noqa: BLE001
                armed = None                # a registry miss never stops a beacon
        try:
            line = wait(seat=claimed, room=room,
                        any_row="--any" in args,
                        timeout=float(timeout) if timeout else None,
                    # --follow (beacon) + --any-watch must use wait()'s FLUSHED
                    # sink (_emit_line) — passing bare print here overrode it and
                    # block-buffered every wake-line into oblivion on a Monitor
                    # pipe (the beacon-never-wakes bug). Only single-shot seat
                    # mode keeps print (deliver emits the one line + returns it).
                        # --follow (beacon) + --any-watch must use wait()'s
                        # FLUSHED sink (_emit_line) — passing bare print here
                        # overrode it and block-buffered every wake-line into
                        # oblivion on a Monitor pipe (the beacon-never-wakes
                        # bug). Only single-shot seat mode keeps print (deliver
                        # emits the one line + returns it).
                        emit=None if (follow or "--any" in args) else print,
                        follow=follow, session=_env_session(),
                        # --ambient opts the beacon back into home-room wakes
                        # (quiet rooms); absent, wait() resolves the shape
                        # default (--follow ⇒ mention-only, single-shot ⇒ full).
                        ambient=True if "--ambient" in args else None)
        finally:
            # The row names a PROCESS, so it must die with the process. A
            # beacon SIGTERMed mid-life never reaches here, which is why the
            # census prunes rows whose pid is proven gone rather than trusting
            # this to be the only drain.
            if armed and armed.get("registered"):
                try:
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
    if verb == "seats":
        rep = roster_report(room)
        rows = rep["seats"]
        hidden = 0
        if "--all" not in args:      # absent rows hide by default (junk rows
            shown = [s for s in rows if s["presence"] != "absent"]
            hidden = len(rows) - len(shown)          # leave via seat gc only)
            rows = shown
        if not rows and not hidden:
            print("helm chat: no seats yet — sessions join on their next start "
                  "(helm hooks install wires it)")
            return 0
        display_rows = []
        for s in rows:
            runtime = runtime_label(s)
            label = "%s [%s]" % (s["seat"], runtime) if runtime else s["seat"]
            display_rows.append((s, label))
        w = max([len(label) for _s, label in display_rows] or [0])
        for s, label in display_rows:
            scope = "#" + s["home_room"] if s.get("home_room") else "all"
            source = " (%s)" % s["home_room_source"] \
                if s.get("home_room_source") else ""
            # the todo cell rides the existing row (who is working on what) —
            # `helm todos --all` is the full pull surface
            t = s.get("todo") or {}
            task = (" · %s (%d/%d)" % (t["active"][:44], t["done"], t["total"])
                    if t.get("active") else
                    " · %d/%d done" % (t["done"], t["total"]) if t else "")
            # the same one-line status the web presence bar shows (fresh
            # explicit status > live claim > stale status > home) — home-tier
            # is already the row. An explicit line carries its age (a 2-day-
            # old away message must READ as 2 days old) + the cross-seat
            # writer where one was recorded.
            extra = ""
            if s.get("source") == "status":
                if s.get("status_age") is not None:
                    extra = " (%s)" % _fmt_age(s["status_age"])
                if s.get("status_by"):
                    extra += " (by %s)" % s["status_by"]
            line = (" ▸ %s%s" % (s["line"], extra)
                    if s.get("line") and s.get("source") != "home" else "")
            # the honest-presence surface: an UNVERIFIED row SAYS so, names the
            # other claimant, and names the one verb that clears it — the owner
            # is GUI/CLI-first, so a state only agents can compute is not done
            warn = (" ⚠ %s — `helm chat seat disown %s %s`"
                    % (s["warn"], s["seat"],
                       (s.get("unverified_session") or "")[:8])
                    if s.get("warn") else "")
            print("  %s %-*s  %-10s  pending %-3d %s · home %s%s%s%s%s" % (
                s.get("dot") or presence_dot(s["presence"]),
                w, label, s["presence"], s["pending"],
                (s.get("project") or ""), scope, source, line, task, warn))
        if hidden:
            print("  (%d absent seat%s hidden — --all shows them; `helm chat "
                  "seat gc` prunes evidence-free rows)"
                  % (hidden, "s"[:hidden != 1]))
        for c in rep["claims"]:
            # The roster is the surface the fleet actually reads, so the stale
            # verdict has to be HERE and not only in the claims data. A dead
            # holder's lock is indistinguishable from a working seat's until
            # something says so out loud.
            print("  claim: %s -> %s (%ds left, fence %s)%s" % (
                c["resource"], c["holder"], c["remaining"], c["fence"],
                claim_liveness_mark(c)))
        return 0
    if verb == "status":
        # `helm chat status <one-line>` sets, `--clear` clears, bare shows —
        # the ICQ away-message: one glanceable line on the seat's roster row
        clear = "--clear" in args
        if clear:
            args = [a for a in args if a != "--clear"]
        who = chat._seat_flag(args) or acting_seat(_env_session(), safe_cwd())
        text = " ".join(args).strip()
        if text and clear:
            print("usage: helm chat status [<one-line> | --clear] [--seat S]",
                  file=sys.stderr)
            return 2
        if not text and not clear:            # bare: show the current line
            row = roster().get(who)
            if row is None:
                print("helm chat: no roster row for %r yet" % who,
                      file=sys.stderr)
                return 1
            line, source = status_line(row, _claims_by_holder().get(who))
            extra = ""
            if source == "status":
                age, sb = _status_age(row), _status_by(row)
                if age is not None:
                    extra = " (%s)" % _fmt_age(age)
                if sb:
                    extra += " (by %s)" % sb
            print("helm chat: %s %s ▸ %s (%s)%s" % (
                presence_dot(presence_of(last_seen(who, row))),
                _seat_label(who), line or "—", source, extra))
            return 0
        # the WRITER is always the ambient identity — a cross-seat write
        # (--seat != self) is allowed but recorded (status_by, post parity)
        ok, msg = set_status(who, None if clear else text,
                             by=derive_seat(_env_session()))
        print("helm chat: " + msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if verb == "claim":
        if not args:
            print("usage: helm chat claim <resource> [--ttl SECONDS] [--seat S] "
                  "[--lease ID to extend]   (the printed lease id confirms "
                  "release; `helm chat claims` reprints your own)",
                  file=sys.stderr)
            return 2
        # session comes ONLY from the ambient harness env — never a flag: a
        # roster-visible SID must not be assertable through the CLI
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
        return _cmd_claims(args)
    if verb in ("verdict", "reveal", "council-status", "council-abort"):
        return _cmd_council(verb, args)
    print("helm chat: unknown subcommand '%s'" % verb, file=sys.stderr)
    return 2
