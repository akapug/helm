#!/usr/bin/env python3
"""helm seats — stop signals: the two HARD blocks and the cheap probes.

THIS IS THE FIRST OF FOUR MODULES OUT OF ONE 3,143-LINE SECTION, and the cut
is at the point where a stop stops being a REFUSAL and becomes ADVICE.

TWO THINGS HERE ACTUALLY BLOCK AN IDLE STOP, and both are deliberate: an
un-armed beacon (_beacon_block) means the agent cannot be woken, so stopping
makes it unreachable rather than idle; and the review spiral (_spiral_gate)
means the same lane has been review-dispatched at three distinct tips, where
the cure is one live exchange rather than a fourth round. Everything else in
this file only SUPPLIES SIGNAL to the whisper ladder, which advises and never
refuses.

PRECISION IS THE WHOLE GAME for the blocking pair. A guard that blocks on a
bad signal is worse than no guard, because an agent that learns to work
around a false refusal has learned to work around the true one too. That is
why the latches are fingerprinted rather than counted: a latch keyed on a
count re-fires on the same unchanged state, and re-firing is how a guard
teaches itself to be ignored.

The candidate probes (_ask_candidate, _dispatch_candidate, _gate_candidate,
_staged_deletions, _unverified_candidate, _unbanked_candidate) are grouped
with them because each is a cheap per-turn read the ladder asks EVERY stop,
and their cost is the reason they are cheap: anything expensive here is paid
on every turn of every seat.
"""

import hashlib
import json
import os
import re
import time

from . import chat, home, pk, vcs
from .seats_common import (SEAT_BYTES, STATUS_BYTES, _clip, _scrub, _seat_key,
                           own_name, roster)
# Downward edges only: stop_signals -> delivery -> roster -> identity ->
# common. The stop ladder READS the world these modules maintain and writes
# none of it, which is why nothing here needs deferring.
from .seats_identity import acting_seat, deliverable, safe_cwd, seat_scope
from .seats_delivery import _cursor, _room_dirty, _scan_rooms, _sid8, _tail

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
def _pending_all(room, seat, session=None, scan_lane="pending"):
    """[(room, row)] pending across one lane's bounded room scan, cursors
    untouched. Delivery, stop-guard, and roster observation own independent
    identity queues so one consumer cannot steal another's eventual coverage.
    The tracked/backfill law and one-roster-read scope match deliver_any."""
    sc = seat_scope(seat)
    tracked = sc["tracked"] \
        or (_cursor(room, seat, session) or _cursor(room, seat)) is not None
    out = []
    for r in _scan_rooms(
            room, seat=seat, scope=sc, session=session,
            scan_lane=scan_lane):
        if not _room_dirty(r, seat, session):
            continue
        out.extend((r, row) for row in _pending_rows(
            r, seat, session, scope=sc,
            backfill=(tracked and r != room) or r.startswith(chat.DM_PREFIX)))
    return out
def _off(name):
    return (home.env(name) or "").lower() in ("0", "off", "no")
BEACON_LATCH = "stopbeacon"
def beacon_procs(seat, proc_dir="/proc", strict=False):
    """([pids], trouble) — live `helm chat wait` waiters attributable to
    `seat`. Argv shape comes from rearm.py (the same exact-shape gate the
    land-to-live pass signals on): a standalone `helm` token — or `-m helm` —
    followed by `chat wait`. A bash `-c 'helm chat wait …'` wrapper carries
    that text inside ONE argument with no standalone `helm` token, so the
    wrapper alone is never mistaken for the beacon; the wrapper execs the real
    process, which IS matched.

    Attribution, in order: the `--seat` flag (casefold), else the process's own
    HELM_CHAT_NAME/MELD_CHAT_NAME (a beacon armed without --seat resolves its
    seat from that env), else UNATTRIBUTABLE. The stop guard's default is
    deliberately lenient: unattributable counts as a hit because a wrong BLOCK
    stops a healthy seat.

    `strict=True` IS A POSITIVE CLAIM, AND A SHAPE CANNOT MAKE ONE. An argv
    shape says a `helm chat wait` process EXISTS; it says nothing about whether
    anything is still listening at the other end. An ORPHAN waiter left behind
    by a DEAD session satisfies the shape perfectly, and that is not a corner
    case — @kimi measured two of them on her own seat, detached shells whose
    claude sessions had exited (chat #helm row 623). While this function
    answered from shape alone, `resumeturn`'s recovery line — "It has an armed
    inbox beacon and will wake on its next @mention" — was satisfiable by a
    zombie, so a compacted seat covered only by an orphan stayed dark forever
    under an alert that said it was covered.

    So strict now requires a LIVE SESSION behind the shape (`beacons.classify`):
    a hit counts only when a live process still holds the session the waiter
    was armed under. The three outcomes stay distinct, because collapsing them
    is the whole bug — PROVEN LIVE returns pids; UNPROVABLE returns trouble (an
    alert must say UNKNOWN, never promise a wake); and a seat covered ONLY by
    proven ghosts returns a clean, deliberate EMPTY, which is the true and
    previously unsayable answer "nothing will wake this seat."

    FAIL-OPEN: trouble is a string and the caller must not block or claim armed
    on it."""
    from . import rearm
    needles = [("%s=%s" % (v, seat)).encode("utf-8") + b"\0"
               for v in ("HELM_CHAT_NAME", "MELD_CHAT_NAME")]
    want = str(seat or "").casefold()
    try:
        me = os.getuid()
        pids = [n for n in os.listdir(proc_dir) if n.isdigit()]
    except OSError as exc:
        return [], "process table unlistable (%s)" % exc
    hits, opaque, mypid = [], [], os.getpid()
    for pid in pids:
        pdir = os.path.join(proc_dir, pid)
        try:
            ipid = int(pid)
            if ipid == mypid or os.stat(pdir).st_uid != me:
                continue
        except OSError:
            continue                     # exited between listdir and stat
        try:
            with open(os.path.join(pdir, "cmdline"), "rb") as f:
                raw = f.read(64 * 1024)
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            continue                     # exited mid-scan: proven not-live
        except PermissionError:
            continue      # not-dumpable (systemd --user is uid-1000 and
                          # opaque): opaque by kernel design, and never a
                          # plain `helm chat wait` python process. Treating
                          # this as trouble would disable the guard on every
                          # real host — found on the first live run.
        except OSError as exc:
            return [], ("process %s cmdline unreadable (%s)"
                        % (pid, exc.__class__.__name__))
        argv = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
        sub = rearm._helm_subargv(argv) if argv else None
        if not sub or sub[:2] != ["chat", "wait"]:
            continue
        flag = rearm._flag(sub, "--seat")
        if flag is not None:
            if str(flag).casefold() == want:
                hits.append(ipid)
            continue
        try:                             # no --seat: its env names its seat
            with open(os.path.join(pdir, "environ"), "rb") as f:
                env = f.read(64 * 1024)
        except OSError:
            (opaque if strict else hits).append(ipid)
            continue
        named = b"HELM_CHAT_NAME=" in env or b"MELD_CHAT_NAME=" in env
        if any(n in env for n in needles):
            hits.append(ipid)
        elif not named:
            (opaque if strict else hits).append(ipid)
    if strict:
        return _strict_live(seat, hits, opaque, proc_dir)
    return hits, None
def _strict_live(seat, hits, opaque, proc_dir):
    """The positive half of `beacon_procs`: which of these shapes is a beacon
    somebody is still listening to. Every waiter is asserted, not the first
    match and not the newest — taking the newest arm time is exactly how a seat
    with five waiters, four of them stale, was read as 'current' (#helm 622)."""
    from . import beacons
    live = beacons.live_sessions()
    proven, ghosts, unproven = [], [], []
    for pid in hits:
        row = beacons.classify(pid, seat, None, live, proc_dir)
        if row["state"] == beacons.LIVE:
            proven.append(pid)
        elif row["state"] == beacons.GHOST:
            ghosts.append(row)
        else:
            unproven.append(row)
    if proven:
        return proven, None
    if opaque:
        return [], ("%d live beacon waiter%s could not be attributed to seat %s"
                    % (len(opaque), "" if len(opaque) == 1 else "s", seat))
    if unproven:
        return [], ("%d beacon waiter%s for seat %s could not be proven live "
                    "(%s)" % (len(unproven), "" if len(unproven) == 1 else "s",
                              seat, unproven[0]["why"]))
    # Zero proven, zero unprovable: either there is no waiter at all, or every
    # one of them is a proven ghost. Both are the same honest answer — nothing
    # will wake this seat — and it is the answer this function could not give
    # before, because a ghost used to satisfy it.
    return [], None
def owes_beacon(seat, session=None):
    """The VALIDATED seat name when this process owes an armed beacon, else
    None. Only a LAUNCHED fleet seat owes one: HELM_CHAT_NAME names this very
    seat (the launch seam's stamp — launch.build_env / seat.launch_line; an
    ad-hoc session that auto-named itself has none) AND the seat is
    roster-registered (its join actually ran, so a delivery lane exists to go
    dark). Fail-open to None on every uncertainty — never block on a guess.

    Returning the name (not a bool) is load-bearing for the display-launder
    law: home.chat_name() is THE validated ingestion seam ([A-Za-z0-9._-] only,
    SeatNameError on ESC/C0/bidi), so the name this returns is provably inert
    and can be interpolated into the block message — including into the
    copy-pasteable Monitor command, which laundering would corrupt. The roster
    read below is INTERNAL-MATCHING-ONLY: a membership test whose key never
    reaches a sink."""
    try:
        stamped = home.chat_name()
    except Exception:
        return None                      # a hostile name is not a seat at all
    if not stamped or not seat:
        return None
    if str(stamped).casefold() != str(seat).casefold():
        return None
    try:
        return stamped if seat in roster() else None
    except Exception:
        return None
def _beacon_block(session, room, seat):
    """The armed-beacon gate -> one block line, or None. Latches the OBSERVED
    state (armed|missing) per (seat, session): the transition into `missing`
    blocks once, a re-stop on the same state passes, and a beacon that later
    dies re-arms the block. Probe trouble touches nothing."""
    name = None if _off("STOP_GUARD_BEACON") else owes_beacon(seat, session)
    if not name:
        return None
    pids, trouble = beacon_procs(name)
    if trouble:
        return None                      # absence unproven -> never block
    path = _stop_fp_path(room, seat, session, kind=BEACON_LATCH)
    try:
        with open(path) as f:
            last = f.read().strip()
    except OSError:
        last = None
    state = "armed" if pids else "missing"
    if state != last:
        try:
            chat._ensure_dir()
            pk.atomic_write(path, state)
        except OSError:
            # An unlatchable gate would block EVERY stop forever — the wedge
            # this guard is forbidden to become. No latch, no block.
            return None
    if state == "armed" or last == "missing":
        return None                      # armed, or already pointed at once
    return (
        "[helm stop-guard] NO ARMED BEACON — seat '%s' has no live `helm chat "
        "wait --seat %s` process, so NOTHING can wake it: every row addressed "
        "to you piles up undelivered and you go dark (the restarted-integrator "
        "incident: 98 undelivered rows, ~1.5h blind, a cross-family gate "
        "verdict missed). Arm it BEFORE ending this turn:\n"
        "  Monitor(command: \"helm chat wait --seat %s --follow\", "
        "persistent: true)\n"
        "Monitor missing from your tools means DEFERRED, not absent: "
        "ToolSearch(query: \"select:Monitor\") first. A background shell is "
        "NOT a beacon — it cannot wake your turn loop. This blocks once per "
        "beacon-loss episode (a re-stop passes; HELM_STOP_GUARD_BEACON=0 "
        "disables it)." % (name, name, name))
SPIRAL_LATCH = "stopspiral"
# The message's whole point is a command the seat can PASTE, which rules out
# laundering the identities at the sink: mangling a name to make it safe also
# makes the command wrong. The beacon block above solves this the only honest
# way — validate at the seam, then interpolate something provably inert — and
# this rung copies it. `dispatches._TOKEN` is the seat-name shape the ledger
# already enforces on senders; lanes get the same treatment. A name that does
# not match is not scrubbed into something plausible, it yields NO FINDING:
# refusing to speak is always available, and a guard is allowed to be silent.
_SPIRAL_LANE = re.compile(r"[A-Za-z0-9._/-]{1,160}\Z")
def _spiral_meld(peer, lane):
    """The literal cure command, filled in with the real peer and lane."""
    return ('helm chat meld invite %s "%s: converge every open review finding '
            'in ONE exchange"' % (peer, lane))
def _melded_with(peer, span_h, now=None):
    """(True, room) iff this seat CONVERGED a meld with `peer` inside the
    spiral's own window, else (False, reason).

    #70, and it is the guard punishing a seat for taking the guard's own cure.
    review_spiral counts every distinct reviewed tip and consults NO meld
    state, and the latch fingerprint includes the round count — so the ONE
    post-meld round that IS the convergence re-arms the gate against the seat
    that just did what the block told it to do. The remedy becomes evidence.

    CONVERGED, NOT MERELY OPENED: an `active` meld is a conversation in
    progress and proves nothing about a cure; only a done/done-mutual state
    says the exchange closed. Opening a meld must never buy silence, or the
    gate is disarmed by the cheapest possible gesture.

    INSIDE THE WINDOW, because a meld from last week is not this spiral's cure.
    span_h is what review_spiral actually exposes — it carries no per-round
    timestamps, so "postdates the penultimate round" is not computable here
    without reaching into dispatches. This asks the question the rule exists
    to ask — did this seat meld DURING this spiral — and says so plainly
    rather than implying a precision it does not have.

    FAILS CLOSED TO NOT-MELDED. An unreadable meld directory suppresses
    nothing: a suppression that fires on absent evidence un-guards the spiral
    rung fleet-wide, which is the same failure the delegation exemption next
    door forbids by name."""
    import glob as _glob
    now = time.time() if now is None else now
    try:
        span_s = max(0.0, float(span_h or 0)) * 3600.0
    except (TypeError, ValueError):
        return False, "unusable span"
    floor = now - span_s
    try:
        paths = _glob.glob(os.path.join(chat.chat_dir(), "*.meld.*.json"))
    except OSError as e:
        return False, "meld state unreadable (%s)" % type(e).__name__
    for path in paths:
        st = pk.read_json(path, None)
        if not isinstance(st, dict):
            continue
        if str(st.get("status") or "") not in ("done", "done-mutual"):
            continue                      # active != converged
        who = [st.get("peer")] + list(st.get("peers") or [])
        if peer not in [str(x) for x in who if x]:
            continue
        try:
            when = float(st.get("epoch") or 0)
        except (TypeError, ValueError):
            continue
        # ONE SECOND OF SLACK, and it is a real boundary rather than a fudge.
        # `epoch` is integer seconds; `floor` is a float. When a spiral's
        # rounds land inside one second — which is exactly the shape of a fast
        # ping-pong, and of every test that stages rounds in a loop — span_h is
        # 0.0 and the window collapses to a POINT that integer truncation puts
        # the meld just outside. The cure would then be rejected for being
        # simultaneous with the disease.
        if when + 1.0 >= floor:
            # LAUNDERED AT THE READ, never at the emit. This room name comes
            # out of a meld file ANOTHER seat wrote, and _spiral_gate
            # interpolates it straight into displayed stop-guard text — so an
            # unscrubbed value here reshapes the frame it rides in. Scrubbing
            # at the read makes the contract "element two is always safe to
            # display", which a future caller cannot undo by forgetting;
            # laundering at the one emit site would have fixed one caller and
            # left the next exposed. SEAT_BYTES because a room name is a
            # glance, exactly like a seat label.
            return True, _clip(_scrub(str(st.get("room") or "?")), SEAT_BYTES)
    return False, "no converged meld with %s inside the window" % peer
def _spiral_gate(session, room, seat):
    """(block | None, warn | None) for the review-spiral rung."""
    if _off("STOP_GUARD_SPIRAL") or not seat:
        return None, None
    from . import dispatches
    try:                      # fail-open TOTAL — a Stop rung must never wedge
        info, err = dispatches.review_spiral(seat)
    except Exception:
        return None, None
    if err:
        # THE COMMENT WAS ALREADY RIGHT AND THE CODE DID NOT CARRY IT: "UNKNOWN
        # rounds are not zero rounds". Both returned silence, so a seat the
        # detector could not KEY ON looked exactly like a seat with nothing to
        # report — and one seat ran TEN rounds inside that silence. Still
        # fail-open (a Stop rung may never wedge a turn), now audible.
        return None, ("[helm stop-guard] review-spiral rung could not measure "
                      "this seat: %s — rounds are UNKNOWN, not zero."
                      % _clip(_scrub(str(err)).strip(), STATUS_BYTES))
    if not info:
        return None, None     # measured, and genuinely no spiral
    lane = str(info.get("lane") or "")
    peer = str(info.get("peer") or "")
    rounds = int(info.get("rounds") or 0)
    if not _SPIRAL_LANE.fullmatch(lane) or not dispatches._TOKEN.fullmatch(peer):
        return None, None     # cannot be quoted inertly -> not said at all
    if rounds < dispatches.SPIRAL_MELD_ROUNDS:
        return None, None
    cure = _spiral_meld(peer, lane)
    warn = (
        "[helm stop-guard] TWO REVIEW ROUNDS on lane '%s' (peer %s). helm's "
        "own `review-begins-with-cat-file`: at TWO rounds the cure is a MELD, "
        "never round three. If round two comes back with findings, converge "
        "them live instead of sending a third:\n  %s" % (lane, peer, cure))
    # THE LATCH KEYS ON THE CHAIN, not the lane string — the same defect this
    # rung's detector just stopped making. Two unrelated chains reusing one lane
    # name reach `rounds` independently, and a lane-keyed fingerprint let the
    # first one's latch swallow the second one's block. `chain` is opaque here
    # (an id, or "lane:<name>" for a legacy row) and is hashed, never printed,
    # so nothing needs to quote it.
    fp = hashlib.blake2b(("%s|%s|%d" % (info.get("chain") or lane, lane, rounds))
                         .encode("utf-8"), digest_size=8).hexdigest()
    path = _stop_fp_path(room, seat, session, kind=SPIRAL_LATCH)
    try:
        with open(path) as f:
            last = f.read().strip()
    except OSError:
        last = None
    if last == fp:
        return None, None                 # already pointed at this exact state
    latched = True
    try:
        chat._ensure_dir()
        pk.atomic_write(path, fp)
    except OSError:
        latched = False
    if rounds < dispatches.SPIRAL_BLOCK_ROUNDS:
        return None, warn
    # #70: THE CURE MUST NOT COUNT AS THE DISEASE. A seat that already
    # converged a meld with this peer inside this spiral's window has done
    # exactly what the block prescribes, and the ONE round that carries the
    # convergence is what re-arms the gate. Suppress the BLOCK, keep the WARN
    # — the seat still sees the state, it is simply not walled for complying.
    melded, why = _melded_with(peer, info.get("span_h"))
    if melded:
        return None, (warn + "\n  MELD ALREADY CONVERGED with %s (room %s) "
                      "inside this window — not blocking: this round is the "
                      "cure, not another spiral." % (peer, why))
    if not latched:
        return None, warn    # unlatchable -> degrade, never wall the fleet
    return (
        "[helm stop-guard] REVIEW SPIRAL — you have review-dispatched lane "
        "'%s' at %d DISTINCT tips in the last %dh (latest peer: %s). That is "
        "round %d, and helm's own `review-begins-with-cat-file` says: \"at TWO "
        "rounds the cure is a MELD, never round three. Live cost of getting "
        "this wrong: ~6 async rounds on one small lane, 2026-07-29.\" That "
        "rule was in context on every turn of the night it was broken, so this "
        "is a GATE, not another reminder. Do NOT idle waiting for the next "
        "verdict — converge every open finding in one live exchange:\n"
        "  %s\n"
        "One meld closed all three remaining questions on the lane that cost "
        "six rounds. Rounds are counted on the WORK CHAIN, so renaming the lane "
        "does not reset them. Blocks once per (chain, round-count) — a re-stop "
        "passes, a further round re-arms it; HELM_STOP_GUARD_SPIRAL=0 disables."
        % (lane, rounds, dispatches.SPIRAL_WINDOW_H, peer, rounds, cure)), None
STOP_WHISPER_CAP = 240   # one line's byte budget (inject.py WHISPER_CAP kin)
_WHISPER_FIRED_CAP = 20  # fired-set entries kept per (seat, session) latch
STUCK_AT = 3    # reflex.py stuck-commonsense threshold (re-fires per bucket)
DIRTY_AT = 8    # reflex.py uncommitted-drift threshold
SOLO_LOAD_AT = 3  # owner canon 2026-08-03: "when you mention 3 things in one
                  # sentence, 2 of them should go to SA or TLA". The structural
                  # analogue of holding: 3 leases at once is the width at which
                  # carrying them alone stops being a choice and starts being a
                  # backlog. Buckets like STUCK_AT/DIRTY_AT (N // SOLO_LOAD_AT).
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
        r, unavailable = ownerasks.stop_candidate()
        if unavailable:
            return ("ask:ledger-unavailable",
                    "owner-ask ledger UNAVAILABLE — owner debt is UNKNOWN, not "
                    "zero; repair/read `helm asks list` before stopping")
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
def _dispatch_candidate():
    """The DISPATCH rung — sits directly under the owner-ask rung: work you
    handed to another seat and have not checked on. One cheap local read of
    the dispatch ledger; whispers the OLDEST OVERDUE row, never the list.

    This is the durable half of the owner's ask (2026-07-21): "we definitely
    need some timer fallback for anything that is sent to them, to make sure
    it is remembered to check on their progress". Per-session Monitor
    watchdogs die at compaction; this rung re-fires from disk in whatever
    session is running.

    The whisper says CHECK IN, never reassign — an overdue row means the
    deadline passed, not that the seat is dead, and tonight a lane quiet 47
    minutes turned out to be a long turn. The fp carries status/delivery so a
    state transition re-fires exactly once. Fail-closed to None."""
    try:
        from . import dispatches
        # THIS seat's own identity, through the one law (own_name -> session
        # row -> derive). Without it the rung offered every seat the same
        # globally-oldest row — the docstring above says "work YOU handed",
        # and the filter that makes that true needs a seat to compare against.
        r, kind, unavailable = dispatches.stop_candidate(
            seat=acting_seat(cwd=safe_cwd()))
        if unavailable:
            return ("dispatch:ledger-unavailable",
                    "dispatch ledger UNAVAILABLE — obligations are UNKNOWN, not "
                    "zero; repair/read `helm dispatch list` before stopping")
        if not r:
            return None
        tip = str(r.get("tip") or r.get("ref") or "<reviewed-tip>")
        lane = _clip(_scrub(str(r.get("lane") or "")), 32)
        if kind == "redispatch":
            return ("dispatch:%s:needs-redispatch" % r.get("id"),
                    "dispatch %s to @%s (%s) NEEDS REDISPATCH — this historical "
                    "row lacks an exact tip so no verdict can ever close it; "
                    "redispatch the work with `helm dispatch send <recipient> <lane> "
                    "\"<message>\" --ref <tip> --kind <build|review> "
                    "--supersedes <old-id>` (the old row stays visible as history)"
                    % (r.get("id"), r.get("recipient"), lane))
        if kind == "confirm":
            return ("dispatch:%s:needs-confirmation" % r.get("id"),
                    "dispatch %s to @%s (%s) delivery is NEEDS CONFIRMATION — "
                    "confirm at @%s that the hand-off actually arrived; do NOT "
                    "resend automatically (one operation = at most one send)"
                    % (r.get("id"), r.get("recipient"), lane,
                       r.get("recipient")))
        return ("dispatch:%s:%s:%s" % (r.get("id"), r.get("status"), tip),
                "dispatch %s to @%s (%s) NEEDS CHECK-IN (OVERDUE) and is "
                "PENDING VERDICT — verify at the exact recipient; do NOT "
                "reassign on age alone. Close the exact reviewed tip with: "
                "helm dispatch verdict %s %s --approve|--fix|--supersede "
                "<evidence>   (the polarity flag is REQUIRED — without it "
                "the verb exits 2)"
                % (r.get("id"), r.get("recipient"), lane, r.get("id"),
                   _clip(_scrub(tip), 16)))
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
STAGED_DEL_NAMED = 2    # deleted paths NAMED in a whisper ahead of "+N more"
_STAGED_DEL_PATH_CLIP = 20   # per-path byte clip — measured with the templates
_STAGED_DEL_TOK_CLIP = 12    # token clip in the deletion wording — measured
def _staged_deletions(cwd):
    """(shared, deleted-paths) probed from the checkout at `cwd`, or None =
    UNKNOWN (no cwd, not a repo, git absent/failed/timed out). Two bounded git
    reads: rev-parse separates the MAIN checkout — git-dir IS the common dir,
    the tree the whole fleet shares — from a linked worktree (lane-owned,
    single-lease); `diff --cached` lists paths whose DELETION is already
    staged, `--no-renames` so a delete paired with a similar add elsewhere
    still surfaces as the delete it is. Tri-state on purpose (vcs.ancestry's
    law): a probe that cannot SEE returns UNKNOWN, never a verdict — and the
    callers SAY "UNKNOWN" on UNKNOWN, keeping the read-then-decide advice
    without asserting a venue the probe never measured."""
    if not cwd:
        return None
    try:  # through the vcs seam (probe: rc!=0/timeout/no-git → None), never a
        from . import vcs  # direct spawn — the spawn-audit pin holds at 20
        be = vcs.backend(cwd)
        rp = be.probe(cwd, "rev-parse", "--path-format=absolute",
                      "--git-dir", "--git-common-dir", timeout=3)
        if not rp:
            return None
        dirs = [ln.strip() for ln in rp.splitlines() if ln.strip()]
        if len(dirs) != 2:
            return None
        dz = be.probe(cwd, "diff", "--cached", "--name-only",
                      "--diff-filter=D", "--no-renames", "-z", timeout=3)
        if dz is None:  # '' is a LEGITIMATE clean answer, only None is UNKNOWN
            return None
        return (os.path.realpath(dirs[0]) == os.path.realpath(dirs[1]),
                [p for p in dz.split("\0") if p])
    except Exception:
        return None
def _deletions_named(paths):
    """The bounded name-what-is-at-stake clause: the first STAGED_DEL_NAMED
    paths (each byte-clipped) plus an EXPLICIT '+N more'. The omitted count is
    part of the clause and sits AHEAD of the advice in both templates — a
    truncation that hides its own truncation is the bug class this fix
    belongs to, so the tail clip may never be what discloses it."""
    named = ", ".join(_clip(_scrub(p), _STAGED_DEL_PATH_CLIP)
                      for p in paths[:STAGED_DEL_NAMED])
    more = len(paths) - STAGED_DEL_NAMED
    return named + (" +%d more" % more if more > 0 else "")
def _deletions_fp(paths):
    """8-hex fingerprint of the staged-deletion SET, so the latch re-arms when
    the set changes and the legacy wording (a different fp) still gets its one
    shot once the deletions resolve."""
    return hashlib.blake2b("\0".join(paths).encode("utf-8"),
                           digest_size=4).hexdigest()
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
def _unbanked_candidate(dirty, edits, latest, staged=None):
    """The UNBANKED-GREEN rung: edits landed, EVERY latest gate run is green,
    tree still dirty — the next step is unambiguous: commit. The sharper,
    earlier cousin of the dirty-streak rung (no eight-op wait when the state
    already reads 'proven green, unbanked'). fp = the newest green digest:
    each newly-proven green state whispers once. `staged` is the tri-state
    _staged_deletions probe (None = UNKNOWN): positive sight of staged
    deletions in a shared checkout swaps the advice for the refusal branch
    below."""
    if not (dirty and edits and latest):
        return None
    if any(not (isinstance(r.get("exit"), int) and r["exit"] == 0)
           for r in latest.values()):
        return None   # a red/no-verdict gate stands — the red rung owns this stop
    g = max(latest.values(), key=lambda x: x.get("ts") or 0)
    # THIS RUNG CANNOT KNOW WHOSE DIRT IT IS, so it must not say "yours".
    #
    # The dirty signal is a bare boolean — `backend.dirty()` returns True/False
    # with no file list — and the fleet shares one main checkout. Two facts are
    # known here: a green gate ran, and the tree is dirty. NEITHER implies the
    # dirty files were in that green, and neither implies they are this seat's.
    #
    # THE FIRST VERSION SAID `git add -u`, NOT `add -A`, for a reason that was
    # true but INCOMPLETE. `-A` was the danger because untracked files (another
    # lane's audit doc, live 2026-07-26) were never in the green, so `-u` was
    # called safe on the grounds that it stages tracked modifications only.
    #
    # IT IS NOT SAFE. Live 2026-07-27: another seat staged lane content into this
    # shared tree, REVERTING three pushed commits. Those were TRACKED
    # modifications, so `-u` stages them exactly as `-A` would, and this rung
    # fired telling the integrator to bank them as "the proven slice." Following
    # it would have silently reverted pushed work belonging to three seats. `-u`
    # narrows the blast radius from untracked to tracked; it does not make the
    # claim true.
    #
    # So the fix is at the CLAIM, not the flag — state what is known, and name the
    # check the reader must run. Same repair as `dispatch add`'s loud silence: a
    # surface that cannot know says so instead of guessing confidently.
    #
    # ORDER IS WHAT PROTECTS THE INSTRUCTION. STOP_WHISPER_CAP is 240 bytes for
    # the whole line and _clip truncates the TAIL, so whatever sits last is what
    # overflow eats. The first draft of this wording put the advice last and
    # shipped as `bank it with \`git ad…` — the reader got the alarm and not the
    # repair. Keeping the actionable text ahead of the trailing "holds once per
    # state" boilerplate means an overlong gate token spends the budget on the
    # note, never on the verb.
    #
    # The token clip is 16 rather than 40 to buy back room for the trailing
    # note. MEASURED, and narrower than I first claimed: at clip 40 this body
    # reached 247 bytes and lost 7, which fell in the boilerplate, not the
    # instruction. So 16 protected the NOTE; the ordering protects the ADVICE.
    # Stated precisely because the mutation that restored clip 40 left the test
    # green, and a comment calling the clip load-bearing would have been the
    # third documented-exclusion-untested that day.
    #
    # RE-MEASURED 2026-08-05, and the NOTE's protection is now SPENT: adding the
    # third ownership state costs 31 bytes, so this arm composes to 247 bytes
    # (UNKNOWN: 243) and loses its tail at ANY token — even a 6-byte `pytest`,
    # well under the clip. 16 no longer buys the note back; only the ORDERING
    # still holds, and it is now the sole thing keeping the verb on screen. Do
    # not read the surviving "…" in the pinned test as a typo. Budget is now
    # ZERO: any further wording growth eats `never commit` next, so a FOURTH
    # state cannot simply be appended — it costs a re-cut of the whole line.
    #
    # WHILE ANY STAGED DELETION EXISTS IN A SHARED CHECKOUT, `add -u` IS NOT
    # OFFERED AT ALL — the deleted paths are named instead. Do not "simplify"
    # this back into the one-branch advice: the ownership question the legacy
    # wording asks the reader to answer — "is this dirt yours?" — is PRECISELY
    # the question a seat cannot answer reliably in a shared tree, and that
    # unanswerable question is the whole reason this guard exists. A seat that
    # edited here all evening reasonably concludes the dirt is its own, follows
    # the `add -u` arm, and stages every tracked deletion with it. Live
    # 2026-07-29: eight owner-gated doc deletions (~1648 lines of landed
    # documentation, a deliberate public-readiness scrub) sat staged in the
    # shared checkout, worktree matching, while this whisper suggested exactly
    # that branch. The guard cannot distinguish a deliberate owner-gated scrub
    # from an accidental `git rm` — the check-that-cannot-see-a-case class
    # (return UNKNOWN, never a verdict: vcs.ancestry, seat.py proxy
    # liveness), except this instance laundered the UNKNOWN as an
    # INSTRUCTION rather than a verdict. So in the one state where a wrong
    # ownership guess is destructive, the guard refuses to name the destructive
    # branch and hands the reader the real information instead: a bounded list
    # of the paths at stake plus the exact count omitted. The fallback arms
    # below SAY WHAT WAS MEASURED, never more: the probe returned a bool, so
    # a tree it measured NOT shared (a lane worktree, single-lease — it owns
    # its own index) must not be called SHARED, and a probe that returned
    # None must say UNKNOWN rather than mint a venue from blindness — the
    # legacy wording asserted SHARED in both states, which was this same
    # laundered-verdict class one arm over.
    # MEASURED: token clip 12 + per-path clip 20 + 2 named
    # paths peaks this body at ~237 bytes, so count, first path, '+N more'
    # and the prohibition all clear the 240 cap; overflow can only eat the
    # trailing boilerplate.
    if staged and staged[0] and staged[1]:
        return ("unbanked-del:%s:%s" % (g.get("digest"),
                                        _deletions_fp(staged[1])),
                "gate GREEN (`%s`) + %d STAGED deletion(s) in this SHARED "
                "checkout: %s — scrub or accident? one seat can't tell: do "
                "NOT stage/commit; act per-path"
                % (_clip(_scrub(str(g.get("token") or "?")),
                         _STAGED_DEL_TOK_CLIP),
                   len(staged[1]), _deletions_named(staged[1])))
    tok = _clip(_scrub(str(g.get("token") or "?")), 16)
    if staged is not None and not staged[0]:
        return ("unbanked:%s" % g.get("digest"),
                "gate GREEN (`%s`) + dirty in this LANE room — a lane owns "
                "its own index: `git add -u` and commit in your room" % tok)
    if staged is None:
        return ("unbanked:%s" % g.get("digest"),
                "gate GREEN (`%s`) + dirty; shared or lane? UNKNOWN — read "
                "`git diff --stat HEAD`: yours -> add -u; a live DELEGATE's -> "
                "LEAVE IT; NOT yours -> stash create, never commit" % tok)
    # THREE STATES, NOT TWO. yours/not-yours was exhaustive while every agent
    # edited in a CLAIMED LANE — and the delegation machinery below is
    # lane-claim-scoped by construction (_delegated_build needs a lease;
    # _record_subagent_event requires "the exact canonical lane root whose
    # current claim binds this process's seat and session"). A Task SUBAGENT
    # working in the SHARED checkout holds neither, so it is invisible here BY
    # DESIGN, and the binary then prescribes confidently over a case it cannot
    # see: `add -u` commits a delegate's half-finished edit, `stash create`
    # steals the file out from under a live writer.
    # MEASURED 2026-08-05: this whisper fired three times at an integrator whose
    # subagent held the two dirty files; the correct action was NEITHER branch.
    # Naming the third state is the honest floor — the guard cannot detect it
    # (that needs lease-free subagent visibility, a real lane, not a string), so
    # it must not answer as though only two exist.
    return ("unbanked:%s" % g.get("digest"),
            "gate GREEN (`%s`) + dirty, but this checkout is SHARED — read "
            "`git diff --stat HEAD`: yours -> add -u; a live DELEGATE's -> "
            "LEAVE IT; NOT yours -> stash create, never commit" % tok)
