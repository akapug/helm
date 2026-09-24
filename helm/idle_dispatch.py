"""helm.idle_dispatch — the stranded-obligation loud-fail rung.

THE GAP (owner catch, a seat idle-on-an-open-gate): the coordinator
ASSUMES a dispatched agent is BUSY, but a "quiet" presence dot means BOTH
"busy on a long turn" AND "idle-done on an open gate" — indistinguishable, so
only a human glance caught a seat sitting idle 68 minutes on an open review.
This rung turns idle-on-an-open-obligation into an OBSERVABLE SIGNAL: it crosses
the dispatch ledger (obligation + deadline, dispatches.py) against the
recipient's presence (seats.presence_of) — the one JOIN neither half computes
today. dispatches is sender-side + clock-only; presence is timestamp-only.

THE SIGNATURE (idle-on-open-dispatch):
    an OPEN dispatch row (not verdict, not cancelled — via dispatches._open,
    which open_rows() applies) whose RECIPIENT is quiet/absent
    (no recent tool boundary) AND the dispatch is older than IDLE_DISPATCH_S (a
    soft "should have been picked up" window, earlier than the hard deadline so
    an idle-on-gate surfaces BEFORE it is overdue — one seat was idle on a gate,
    possibly not yet past its deadline).

QUIET IS NOT UNCLAIMED (owner catch, a codex seat's near-miss). For its
first life this rung tested ONE claim key — `dispatch:<id8>` — and then the
alert said the recipient "holds no claim", a sentence about the SEAT. Those are
different facts. A `dispatch:<id8>` claim is written by exactly one path
(seats.py's idle-seat self-assign offer); a seat that works a dispatch the
NORMAL way — `helm work claim <lane>` — holds `worktree:<project>:<lane>`
instead. Measured on the live estate that day: 21 live claims, every one a
`worktree:…` key, ZERO `dispatch:…` keys. So the clause the alert leaned on was
false for essentially every seat actually working, and it was load-bearing: it
is what turned a quiet holder into a "STRANDED" row whose recommended action was
REASSIGNMENT. One seat was DMd as holding-no-claim while holding
worktree:helm:lr-close-delivered-report with 8917s left and 932 uncommitted
insertions in its room, including a test file that existed nowhere else. Acting
on that recommendation would have destroyed all of it.

The two states demand OPPOSITE actions, so they are now separate findings:
    quiet + NO live claim anywhere -> STRANDED. Re-check or reassign.
    quiet + HOLDING any live claim -> NOT stranded: a busy or WEDGED owner.
        RESCUE (preserve the room, recover the seat). Never reassign.
    claims ledger UNREADABLE       -> UNKNOWN, which is NOT "no claim". Never
        reassign on an unread fact; go read it.
The claim half is now a DIRECT lookup over every live resource keyed by holder,
which is the authoritative answer and was always one dict away from the wrong
one.

THE WIRE THAT WAS MISSING: it DMs the dispatch's SENDER (the coordinator),
never a room broadcast — a broadcast never lands in a parked coordinator's
`helm chat wait --follow` beacon, so it can't WAKE the one who must re-check.
That un-wired wake is precisely why that seat needed a human. Flag-only, never
auto-reassign (the codebase disclaims "reassign on age alone").

AND FOR ITS FIRST LIFE THAT WAS THE ONLY WIRE, which is detection without
ACTUATION. The rung did the hard half — it knows exactly WHICH SEAT owes the
row — and then told everyone except that seat. The sender's inbox filled with
STRANDED notices while the owing seats stayed quiet, and recovery required
someone to address the owing seat by hand. So there are now TWO
FIRST-CLASS LEGS (see check): the sender is told their obligation is idle, and
the recipient is WOKEN. They share a finding and nothing else — separate
admission, separate latch, separate failure boundary — because a second
addressee bolted onto filters and latches written for one is how an earlier
attempt at this collected most of its review.

THE WAKE'S PROOF IS A BEACON, NOT A PANE (see _wake_route). The obvious move
was to compose resumeturn.wake_alert, which owns waking; measured over the live
roster it would have refused ten of fourteen seats — every claude-family seat,
including the integrator — because its target resolver answers a RESUME-grade
question. This rung wants only to put a row in an inbox something is listening
to, and that is a different, cheaper, measurable fact.

COMPOSED, not net-new: reuses silent_drop.py's scan->fcntl-.state-latch->
one-alert-per-episode->re-arm skeleton + LATCH_TTL idiom; dispatches.open_rows/
_age_s/_is_overdue (the obligation+deadline half); seats.presence_of/last_seen/
_live_claims (the liveness half) + seats.dm (the beacon wake). It is the
deterministic INVERSE of seats._offer_rows: that offers UNOWNED work TO an idle
seat; this detects an OWNED obligation stranded ON an idle seat.
"""
import fcntl
import json
import os
import sys
import time

from . import dispatches, home, seats, seats_integrator
from .seats_identity import _warn_once
# ASKED, NEVER SPELLED. The seat that reads a row nobody else can is a ROLE,
# and seats_integrator is the one door that resolves it against the live
# roster. `dispatches._default_lander` is NOT that door and must not be
# borrowed for it: it is env-overridable and answers a DIFFERENT question —
# who FOLDS a lane, not who reads an orphan.

LATCH_TTL_S = 15 * 60          # FIRST alert wait; each repeat doubles it
# The RETRY clock, which is not the alert clock. A send that FAILED told
# nobody, so it must be retried far sooner than a delivered alert repeats —
# but it must still back off, or a permanently refusing transport becomes an
# every-pass loop.
RETRY_TTL_S = 60
# NOT-YET-ASKED, WHICH IS NOT None. `integrator_seat` ANSWERS None for "could
# not be resolved", so None cannot also mean "we have not looked" — the two
# would share a value and a sweep that failed to resolve would re-read the
# roster on every subsequent row.
_UNASKED = object()
# Ceiling on the doubling. A row stranded for days still speaks — roughly once
# per shift instead of 96 times a day — so the signal survives without the
# channel becoming noise the recipient learns to skip.
LATCH_BACKOFF_CAP_S = 4 * 60 * 60
IDLE_DISPATCH_S = 15 * 60      # soft "should-be-picked-up" window (== QUIET_S);
                               # earlier than the hard deadline so idle-on-gate
                               # surfaces BEFORE overdue
_STATE = "idle_dispatch.json"

_USAGE = """usage: helm seat idle-dispatch [--once] [--dry-run] [--quiet] [--json]
  One read-only pass: cross every OPEN dispatch against its recipient's
  presence AND that recipient's directly-looked-up claim state, then DM the
  dispatch's SENDER one latched alert. Three states, three actions:
    STRANDED      quiet + holds no live claim + NO measured live pane ->
                  re-check or reassign
    WORKING       quiet + holds no live claim but the pane is MEASURABLY LIVE
                  -> wake with an @mention, never reassign. Presence goes
                  quiet after 2min without a tool boundary while a whole-suite
                  gate runs ~14min, and reviewing takes no lease, so both
                  halves of STRANDED are the normal condition of a working
                  reviewer; a positive pane reading outranks that inference
    HOLDING       quiet + holds a live claim -> busy or WEDGED owner; RESCUE,
                  never reassign (their room may hold uncommitted work)
    CLAIM-UNKNOWN the claims ledger could not be read -> read it; never
                  reassign on an unread fact
  A DM, not a broadcast, so it wakes a parked coordinator's beacon. Latched:
  one alert per dispatch per episode. --dry-run reports without DMing;
  --quiet skips the DM; --once accepted for stability.

  SECOND LEG — it also WAKES THE RECIPIENT, the one seat that can discharge
  the row. Independently admitted (STRANDED only, never a HOLDING owner whose
  room may hold uncommitted work, never an unread ledger, never a seat its
  PROVIDER is walling) and independently latched, so a latched sender alert
  can never swallow a wake. The wake is proved by a MEASURED armed beacon
  (seats.beacon_procs), not by a resumable pane: a DM reaches a chat lane and
  any armed beacon takes it, so the pane ambiguity that must refuse a RESUME
  is irrelevant here. No beacon = queued bytes, and the leg says so rather
  than claiming a delivery. The claim state is re-read immediately before the
  send, and an unreadable ledger REFUSES the wake without burning its latch.

  THIRD LEG — it also reports the rows whose DELIVERY NEVER LANDED, because a
  seat asking "what do I owe" had no answer and a burn-down had no input.
  Reported, never sent: this leg enumerates and renders, it does not DM.

  ITS DOMAIN IS NARROWER THAN THE NAME SUGGESTS, and the render says so
  rather than letting the reader assume otherwise. It reads the DISPATCH
  ROW's delivery marker, which proves the assignment mention was observed. It
  is NOT obligation-level delivery: a row can be observed and later become
  AUTHOR/REVIEWER/LANDER owed, and this leg does not see that transition. The
  obligation seam that answers the wider question does not exist yet, so the
  count is a FLOOR on what is owed, never a total.

  UNREADABLE IS NOT EMPTY, all the way to the exit code. When the ledger
  cannot be read this prints UNKNOWN and exits non-zero, so a caller cannot
  read "nothing owed" out of a question that was never answered.
"""


# ---------------------------------------------------------------------------
# the detector — the open_rows x recipient-presence join
# ---------------------------------------------------------------------------

CLAIM_NONE = "none"           # proven: the recipient holds no live claim
CLAIM_HOLDING = "holding"     # proven: the recipient holds >=1 live claim
CLAIM_UNKNOWN = "unknown"     # the ledger could not be read — NOT "none"

# The WAKE leg's admission, which is a different question from the claim state
# and must never be folded into it: "is anything listening on this seat's
# inbox?" See _wake_route.
WAKE_ARMED = "armed"          # measured: a live beacon process is listening
WAKE_NONE = "none"            # measured: no live beacon — a DM is queued bytes
WAKE_UNKNOWN = "unknown"      # the probe could not tell — never "none"


def _recipient_key(value):
    canonical, _err = seats._canonical_recipient(value)
    return str(canonical or "")


def _claim_index(claims):
    """(holder-casefolded -> [resource, ...], {live resource keys}, anon-count)
    from the live-claim dict. The holder field is what makes the answer a DIRECT
    lookup about the SEAT rather than an inference from one guessed resource key.

    `anon` counts live rows whose holder is missing or unusable. Those are the
    same failure one level down: a claim we cannot ATTRIBUTE cannot be ruled out
    as this recipient's, so a seat with no attributed claim reads UNKNOWN rather
    than "holds nothing" while any such row exists. `claim()` always records a
    holder, so this is the corrupt/hand-edited path — and that is exactly the
    path on which an inversion would otherwise be silent."""
    by_holder, keys, anon = {}, set(), 0
    for res, v in claims.items():
        if res == "_fence":
            continue
        keys.add(res)
        holder = str(v.get("holder") or "").strip() if isinstance(v, dict) else ""
        holder_key = _recipient_key(holder)
        if holder_key:
            by_holder.setdefault(holder_key, []).append(res)
        else:
            anon += 1
    return by_holder, keys, anon


def _liveness(seat):
    try:
        from . import seat as _seat
        return _seat.seat_liveness(seat) or {}
    except Exception:
        return None


def _live_pane(seat, row=None):
    """"" or a short fact naming a LIVE pane — the second mirror of
    _provider_wall, for the failure that hit the fleet twice in one hour.

    ABSENT AND GONE ARE DIFFERENT FACTS AND THIS RUNG COULD NOT TELL THEM
    APART. `presence` measures TIME SINCE A TOOL BOUNDARY, which for a live
    seat sitting between turns grows without bound — so a working reviewer
    reads `absent` for the same reason a dead one does. Composed with "holds
    no claim", both land on STRANDED, whose text says the recipient "may be
    idle-done or parked WITHOUT reporting" and recommends "re-check or
    reassign". For a live seat every word of that is wrong, and the
    recommended action is the destructive one.

    MEASURED twice within an hour and on the SAME seat: a codex instance
    was DMd as absent-and-claimless while `seat_liveness` reported state IDLE
    on pane-tail evidence, i.e. a live pane between turns holding an open
    review. Two seats independently reported it STRANDED before either
    checked. For contrast another seat read presence FRESH with the SAME IDLE
    orca state, differing only because its last tool boundary was seconds
    rather than minutes old.

    THE CLASSIFIER WAS ALREADY HERE. `_provider_wall` above calls
    seat_liveness for the QUOTA question and its docstring says in as many
    words that idle_dispatch never asked it. This is that same miss, one
    question over: the rung had the authoritative answer in the same module
    and still composed liveness out of two proxies that cannot see a pane.

    A LIVE pane means WAKE, never reassign — an @mention is the wake path.
    Like the wall fact this never suppresses the alert and never changes the
    claim state: the sender still needs to know their obligation is idle,
    which is this rung's whole purpose. It only stops the alert recommending
    the one action that would destroy the work. Anything unexpected reads as
    no fact at all, because a liveness probe must never corrupt a
    stranded-work alert."""
    row = row if row is not None else _liveness(seat)
    if row is None:
        return ""
    state = row.get("state")
    # LIVE-PANE STATES ONLY, as an ALLOWLIST. A blocklist here would be
    # wrong in the dangerous direction: an unrecognised state would read
    # as live and mute a genuinely stranded row. GONE/UNKNOWN and every
    # future name fall through to no fact, leaving STRANDED intact.
    if state not in ("IDLE", "RUNNING", "LIVE"):
        return ""
    return " Pane: %s (%s) — the recipient is LIVE and between turns, " \
           "not parked-without-reporting. WAKE it with an @mention; " \
           "reassigning takes work from a seat that still holds it." \
           % (state, str(row.get("evidence") or "no evidence"))


def _wake_route(seat_key):
    """(WAKE_*, [pids]) — can a DM to this seat actually REACH something?

    THE WAKE LEG NEEDED A PROOF AND THE OBVIOUS ONE IS THE WRONG DOOR.
    resumeturn._dm_target is the module that owns waking, so composing it here
    was the first instinct and it would have shipped a mute switch. Measured
    across all 14 roster seats: _dm_target routes to FOUR, and every
    one of those four is proxy-family. It refuses TEN — every claude-family
    seat on the roster, the integrator and this rung's own author among them —
    and SEVEN of those ten had a live armed beacon at that same instant. Its own refusal strings say why, and they are right:
    "2 addressable processes still name <seat> after session evidence
    — a resume must never guess which one compacted". That is a RESUME-grade
    question. Not knowing which pane compacted is an excellent reason to refuse
    to inject keystrokes into one, and no reason at all to refuse a DM, which
    goes to the seat's chat lane where any armed beacon takes it.

    SO THE PROOF MATCHES THE ACT: seats.beacon_procs is already the exact-shape
    process instrument for "is anything listening", and resumeturn._wake_text
    already uses it to tell a HUMAN their recovery route. Same instrument, same
    three states, one door down. Measured on the same population: ARMED 11,
    no-beacon 3, UNKNOWN 0 — and the three without beacons are the two
    zz-synthetic test seats and one real seat, so this discriminates rather
    than admitting everyone.

    UNKNOWN IS NOT "NO BEACON", the same asymmetry the claim half already
    keeps: `strict=True` reports probe trouble separately, and trouble is
    checked BEFORE the pid list, so a probe that could not look never reads as
    a seat that is not listening. Collapsing those would make an unreadable
    /proc silently mean "do not wake", which is the failure mode this rung
    exists to end."""
    if not seat_key:
        return WAKE_UNKNOWN, []
    try:
        pids, trouble = seats.beacon_procs(seat_key, strict=True)
    except Exception:
        return WAKE_UNKNOWN, []
    if trouble:
        return WAKE_UNKNOWN, []
    pids = list(pids or [])
    return (WAKE_ARMED, pids) if pids else (WAKE_NONE, [])


def _provider_wall(seat, row=None):
    """"" or a short provider-wall fact — the mirror of _context_pressure,
    for the OPPOSITE claim state.

    THE BLAME THIS CORRECTS. A seat its PROVIDER is refusing has no recent
    activity and holds no claim, so it matches STRANDED exactly — and the
    STRANDED text then tells the sender their recipient "may be idle-done or
    parked WITHOUT reporting" and to "re-check or reassign". Every word of that
    is wrong about a walled seat: it is not parked, it did not fail to report,
    and reassigning punishes it for its provider's 429. Measured 2026-08-04:
    three codex seats sat RATE-LIMITED for five hours while every
    surface that could have said so stayed silent.

    THE CLASSIFIER ALREADY EXISTS — seat.py reports pane-tail
    BLOCKED_ON_QUOTA and composes cached proxywatch darkness as WALLED.
    idle_dispatch asks that one owner rather than growing a second matcher; a
    second detector would be the drift that let two surfaces disagree elsewhere
    tonight.

    ONE MORE FACT ON AN ALERT THAT ALREADY FIRED, exactly as _context_pressure
    is. It never suppresses the alert and never changes the claim state: a
    stranded row is still stranded, and the sender still needs to know. It only
    stops the alert from naming the wrong culprit. Anything unexpected reads as
    no fact at all, because a liveness probe must never be able to corrupt a
    stranded-work alert."""
    from . import seat as _seat
    row = row if row is not None else _liveness(seat)
    if row is None or row.get("state") not in ("BLOCKED_ON_QUOTA", "WALLED"):
        return ""
    why = str(row.get("blocked_on") or "").strip()
    action = _seat.remediation_text(row.get("remediation"))
    action += "; this recipient row cannot tell whether reassignment would help"
    return " Availability: %s%s — %s." % (
        row["state"], " (%s)" % why if why else "", action)


def _context_pressure(seat):
    """"" or a short ' Context: 100.1% of 320k window.' — the cheap read that
    names what that stranded case ACTUALLY was: a seat out of window with its pane
    still alive, which looks exactly like a quiet holder. Reuses the existing
    autocompact row (the same one `helm seat autocompact --dry-run` prints); it
    is NOT a new watchdog and it never decides anything — it is one more fact on
    an alert that already fired. Anything unexpected reads as no fact at all,
    because a context gauge must never be able to suppress or corrupt a
    stranded-work alert."""
    try:
        from . import autocompact
        row = autocompact.read(seat) or {}
        pct, win = row.get("pct"), row.get("window")
        if pct is None or not win:
            return ""
        return " Context: %.1f%% of %dk window." % (pct, int(win) // 1000)
    except Exception:
        return ""


def scan():
    """Read-only: every OPEN dispatch sitting on a quiet recipient, each tagged
    with the recipient's DIRECTLY-LOOKED-UP claim state (see CLAIM_*). A row is
    a finding when its recipient shows no recent tool boundary (presence
    quiet/absent) and it is older than the soft window (a fresh dispatch the
    seat has not reached yet is NOT a finding). Only the CLAIM_NONE rows are
    STRANDED; CLAIM_HOLDING is a busy-or-wedged owner and CLAIM_UNKNOWN is an
    unread fact, and neither may be reassigned. Never writes, never reassigns.

    An unreadable claims ledger no longer returns [] — that fail-closed silenced
    the whole rung on the one condition where the coordinator most needs to look
    (we cannot see who owns what). It now surfaces as CLAIM_UNKNOWN, whose alert
    text recommends reading, never reassigning: unknown is SAFE, not silent.

    ONE INSTANT FOR THE WHOLE SCAN. Every age question below — the freshness
    filter, the reported age_min, and the overdue flag — is answered against
    `read_now`, bound once here. They used to make THREE independent
    time.time() reads per row, so a row the FILTER admitted at 59s could be
    REPORTED as 2m old and overdue=True: the scan contradicted itself about
    one row, and the coordinator read an age that no decision had used."""
    read_now = time.time()
    try:
        rows = dispatches.open_rows()
    except Exception:
        return []
    try:
        claims = seats._live_claims()
    except Exception:
        claims = None
    unknown = claims is None      # unreadable ledger: report UNKNOWN, not "none"
    by_holder, claimed, anon = _claim_index(claims or {})
    # ONE ANSWER FOR THE WHOLE SWEEP, resolved on first need. Bound here rather
    # than inside the loop so a sweep that meets several self-addressed rows
    # reads the roster once and reports one reason, not one per row.
    integrator, integrator_why = _UNASKED, ""
    out = []
    for r in rows:
        rid = str(r.get("id") or "")
        recip = str(r.get("recipient") or "")
        recip_key = _recipient_key(recip)
        recip_display = str(r.get("recipient_display") or recip)
        # the sender to DM: the explicit sender seat-token, else the seat that
        # OWNS the dispatching session (source) — dispatches usually carry only
        # source (a session id, not addressable), so resolve it to a seat.
        # CUSTODY, NOT AUTHORSHIP: this DM wakes whoever must CHASE the row,
        # which after a transfer is the custodian and not the seat that wrote
        # it. The `source` fallback below is unchanged and still covers a row
        # carrying neither.
        from . import dispatches as _d
        sender = _d.custodian_of(r) or None
        if not sender and r.get("source"):
            try:
                # ADDRESSING A THIRD PARTY, not acquiring an identity to act
                # under: this resolves the ROW's own source session to a name.
                # The admission door must never be pointed here, or resolving a
                # stranger's row starts failing.
                sender = seats.derive_seat(r.get("source"))
            except Exception:
                sender = None
        sender = str(sender or "")
        # need an id8 to check the claim key, a recipient to check presence, and
        # a sender seat-token to DM (its beacon is the wake path)
        if len(rid) < 8 or not recip or not sender:
            continue
        # A SELF-ADDRESSED ROW HAS NO PEER, WHICH IS NOT THE SAME AS NO READER.
        # This dropped the row because the sender and the recipient are one
        # seat, so the DM would be a seat talking to itself. That reasoning is
        # true and stops one party short: the row still has a THIRD party who
        # is neither, and owedpush already names them for exactly this case in
        # exactly these words — "a row nobody owns is not a row nobody owes".
        # THE SHAPE, AND IT IS SILENT BY CONSTRUCTION: a self-addressed row
        # appears in no seat's `dispatch list --mine --issued` except its own
        # seat's, so when that seat cannot answer — walled, wedged, gone — the
        # row has no reader at all, and this rung is the surface built to catch
        # exactly that. Route it to the integrator rather than dropping it, and
        # SAY that is what happened, or the digest reads as though the sender
        # had been told.
        # AND THE THIRD PARTY HAS TO ACTUALLY BE A THIRD PARTY. The first cut
        # of this rerouted EVERY self-addressed row to the integrator and an
        # arm two screens up caught it inside a minute: when the seat talking
        # to itself IS the integrator, "route it to the integrator" rebuilds
        # the self-DM this exclusion was written to prevent. The rule is not
        # "prefer the integrator", it is "there is someone who is NEITHER" —
        # and when there is not, the original exclusion is correct and stays.
        self_addressed = bool(recip_key) and recip_key == _recipient_key(sender)
        if self_addressed:
            # RESOLVED HERE, LAZILY AND ONCE PER SWEEP. The roster is not
            # cached, so asking per row reads the file once per row; asking
            # before the loop reads it on every sweep that meets no
            # self-addressed row at all, which is most of them.
            if integrator is _UNASKED:
                integrator, integrator_why = seats_integrator.integrator_seat()
            if not integrator:
                # AN UNRESOLVABLE INTEGRATOR IS NOT A SEAT THAT FAILS THE
                # COMPARISON. Both lines below need the name: one to ask
                # whether the self-talking seat IS the integrator, one to
                # address them. Treating None as "not a match" would send this
                # row's alert to whatever `integrator` was — and there is no
                # such value — while treating it as a match would silently drop
                # a row on a fact nobody measured. The block's own rule decides
                # it: there must be someone who is NEITHER, and when nobody can
                # be named there is not. So the original exclusion stands, and
                # it SAYS SO once per process rather than skipping quietly.
                _warn_once(
                    "idle-dispatch-integrator:%s" % (integrator_why or "?"),
                    "the idle-dispatch sweep cannot reroute self-addressed "
                    "rows — the integrator could not be resolved (%s); those "
                    "rows are being skipped, not cleared\n"
                    % (integrator_why or "no reason given"))
                continue
            if recip_key == _recipient_key(integrator):
                continue          # no party left who is neither: nobody to tell
            sender = integrator
        if dispatches._age_s(r, read_now) < IDLE_DISPATCH_S:
            continue              # too fresh — give the recipient time to pick up
        if ("dispatch:" + rid[:8]) in claimed:
            continue              # claimed the dispatch ITSELF => being worked
        presence = seats.presence_of(seats.last_seen(recip))
        # FRESHNESS MEASURES WHETHER A PROCESS IS TICKING; THIS RUNG ASKS
        # WHETHER WORK CAN COMPLETE. For a healthy seat those are the same
        # reading, and for a WALLED one they are opposite: a seat its provider
        # is refusing keeps crossing tool boundaries — it answers, it simply
        # cannot finish — so it reads `fresh` forever and every row addressed
        # to it was dropped here before it could be classified.
        #
        # THE MODULE ALREADY KNEW ABOUT WALLS AND COULD NOT REACH THIS ONE.
        # `_provider_wall` below says in its own docstring that a wall "is the
        # answer that makes the second question's default answer wrong", but it
        # ran only for rows that had ALREADY PASSED this line, so the state
        # where the wall matters most was the one state it could never see.
        # A WHOLE FAMILY CAN READ RATE-LIMITED WITH ITS PANES LIVE AND ITS
        # TURNS MINUTES OLD, and this rung then reports "no idle dispatches"
        # while overdue rows sit on every one of those seats.
        #
        # THE READ IS PAID HERE AND ONCE. This line sits AFTER the age and
        # claimed filters, so only an aged, unclaimed row on a fresh recipient
        # pays for a liveness lookup, and the result is carried down to the
        # wall and pane facts below rather than measured a second time.
        live_early = _liveness(recip) if presence == "fresh" else None
        fresh_walled = bool(presence == "fresh" and _provider_wall(recip, live_early))
        if presence == "fresh" and not fresh_walled:
            continue              # recipient crossed a tool boundary recently => busy
        held = by_holder.get(recip_key) or []
        state = (CLAIM_UNKNOWN if unknown
                 else CLAIM_HOLDING if held
                 else CLAIM_UNKNOWN if anon else CLAIM_NONE)
        f = {
            "id": rid, "id8": rid[:8], "recipient": recip_display,
            "recipient_key": recip_key, "sender": sender,
            "lane": str(r.get("lane") or "review"),
            "presence": presence,
            "claim": state,
            "stranded": state == CLAIM_NONE,
            "held": [seats._pub_res(x) for x in sorted(held)],
            "age_min": int(dispatches._age_s(r, read_now) // 60),
            "overdue": dispatches._is_overdue(r, read_now),
        }
        # only a HOLDING owner gets the context read: that is the state where
        # "why is a working seat quiet?" is the live question, and the answer
        # (wedged at 100% of window) changes what the coordinator does next.
        f["context"] = _context_pressure(recip) if state == CLAIM_HOLDING else ""
        # THE MIRROR: a HOLDING owner is asked "why is a working seat quiet?";
        # a STRANDED one is asked "did this seat abandon the row?" — and a
        # provider wall is the answer that makes the second question's default
        # answer wrong. Only CLAIM_NONE pays for the read, for the same reason
        # only CLAIM_HOLDING pays for the context one.
        #
        # A ROW ADMITTED *BECAUSE* IT IS WALLED CARRIES ITS WALL WHATEVER IT
        # HOLDS. The CLAIM_NONE gate below is a cost choice about rows that
        # reached here on their own; for a fresh_walled row the wall is not an
        # extra fact, it is the ADMISSION REASON, and omitting it would print a
        # finding whose only explanation was the thing it declined to read.
        live = (live_early if live_early is not None
                else _liveness(recip) if state == CLAIM_NONE or fresh_walled
                else None)
        want_facts = state == CLAIM_NONE or fresh_walled
        f["wall"] = _provider_wall(recip, live) if want_facts else ""
        f["live_pane"] = _live_pane(recip, live) if want_facts else ""
        f["self_addressed"] = self_addressed
        f["fresh_walled"] = fresh_walled
        # THE SECOND LEG'S ADMISSION, and it is deliberately NOT the claim
        # state. Only a STRANDED row is even a candidate: a HOLDING owner is
        # rescued and never poked (their room may hold uncommitted work) and a
        # CLAIM-UNKNOWN one is an unread fact, so neither pays for the probe.
        # The wall exclusion is not a second opinion about liveness — it is the
        # same sentence the STRANDED text already says out loud, that a walled
        # seat is not parked and its PROVIDER is refusing it. Waking a seat its
        # provider is refusing delivers a row it cannot answer and bills it for
        # someone else's 429.
        # MEASURED UNCONDITIONALLY, AND THAT IS A CORRECTNESS CHOICE, NOT A
        # COST ONE. Gating the probe on CLAIM_NONE made `wake_route` report
        # UNKNOWN for a holding seat — but that was "we did not look", not "we
        # could not tell", so the field lied about its own reason and the
        # surface would have printed "beacon unknown" for a seat that was
        # simply never a candidate. It also made the admission untestable: with
        # the target empty for every non-stranded row, the `stranded` clause in
        # `wakeable` could be deleted with no arm reddening, which is precisely
        # the decorative guard clause `_live_pane` warns about one screen up —
        # extra safety to read, none to have. Measuring always keeps this field
        # honest and leaves ADMISSION as the single gate.
        route, wake_pids = _wake_route(recip_key)
        f["wake_route"] = route
        f["wake_pids"] = wake_pids
        # ROUTED BY KEY, NEVER BY DISPLAY. Both spellings sit on this dict and
        # read identically at a call site, which is what makes the trap
        # structural rather than careless; the wake carries recip_key and the
        # TEXT carries the display form, exactly as the sender alert does.
        f["wake_target"] = recip_key if route == WAKE_ARMED else ""
        f["wakeable"] = bool(f["stranded"] and f["wake_target"]
                             and not f["wall"])
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# the latch (one alert per stranded dispatch per episode) + the DM-to-sender
# ---------------------------------------------------------------------------

def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


_STRANDED_TEXT = (
    "@%(sender)s IDLE-DISPATCH: your dispatch %(id8)s (%(lane)s) to "
    "@%(recipient)s is STRANDED — the recipient is %(presence)s (%(why)s) "
    "AND holds no live claim on ANY resource (checked by holder "
    "across the whole claims ledger, not just dispatch:%(id8)s), ~%(age_min)dmin "
    "old%(od)s.%(admit)s%(wall)s%(live_pane)s%(tail)s"
    "[idle-dispatch watchdog]")

_HOLDING_TEXT = (
    "@%(sender)s IDLE-DISPATCH: your dispatch %(id8)s (%(lane)s) to "
    "@%(recipient)s is QUIET BUT HOLDING — NOT stranded. The recipient is "
    "%(presence)s (%(why)s) but holds %(held)s, ~%(age_min)dmin "
    "old%(od)s.%(admit)s%(context)s Quiet + holding is a busy or WEDGED owner (out of "
    "context window with the pane still alive looks exactly like this), not an "
    "abandoned row — their room may hold uncommitted work that reassignment "
    "would destroy. RESCUE, do not reassign: `helm seat autocompact --dry-run` "
    "and `helm seat where %(recipient)s` to tell wedged from busy, preserve the "
    "room first (`git -C <room> stash create`), then recover the seat. "
    "[idle-dispatch watchdog]")

# THE SAME TEXT AS STRANDED WITH ONE CLAUSE CHANGED, and the smallness is the
# point. `_stranded_tail` already replaces the closing RECOMMENDATION when a
# pane fact is present, so the only thing left contradicting the evidence was
# the HEADLINE — and the headline is the word a reader acts on. Re-stating the
# wake advice here would duplicate the tail and the pane fact, so every other
# field, including %(why)s and %(tail)s, stays exactly as the stranded case
# has it.
_WORKING_TEXT = (
    "@%(sender)s IDLE-DISPATCH: your dispatch %(id8)s (%(lane)s) to "
    "@%(recipient)s is IDLE BUT ITS RECIPIENT IS LIVE — NOT stranded. The "
    "recipient is %(presence)s (%(why)s) and holds no live claim, and BOTH "
    "of those are the ordinary condition of a working reviewer rather than "
    "evidence of abandonment: presence goes quiet 120s after a tool boundary "
    "while a whole-suite gate runs ~835s, and reviewing, rebasing, retipping "
    "and gating take no worktree lease. ~%(age_min)dmin "
    "old%(od)s.%(admit)s%(wall)s%(live_pane)s%(tail)s"
    "[idle-dispatch watchdog]")

_UNKNOWN_TEXT = (
    "@%(sender)s IDLE-DISPATCH: your dispatch %(id8)s (%(lane)s) to "
    "@%(recipient)s needs a LOOK — claim state UNKNOWN. The recipient is "
    "%(presence)s (%(why)s) and the claims ledger could not be read, "
    "~%(age_min)dmin old%(od)s.%(admit)s UNKNOWN IS NOT 'holds no claim': they may be "
    "holding a lease over uncommitted work. Do NOT reassign on an unread fact — "
    "read it (`helm chat claims`), then `helm seat where %(recipient)s`. "
    "[idle-dispatch watchdog]")

_TEXTS = {CLAIM_NONE: _STRANDED_TEXT, CLAIM_HOLDING: _HOLDING_TEXT,
          CLAIM_UNKNOWN: _UNKNOWN_TEXT}

# THE SECOND ADDRESSEE. The sender alert above tells the coordinator their
# obligation is idle; this tells the ONE SEAT THAT CAN DISCHARGE IT. Every
# recovery on the estate used to begin with a human or the integrator
# addressing the owing seat by hand, because the rung knew exactly who owed
# what and told everyone except them.
#
# IT DOES NOT DIAGNOSE THE SEAT, because it cannot: "you went idle" and "you
# never saw the row" both happen and this rung cannot tell them apart. It names
# the row, the age, and the two honest exits — take it, or say in the room that
# it is not yours so it can be rerouted. A watchdog that guesses the cause
# invites an argument about the guess instead of an action on the row.
#
# THE AGE IS A SCAN-TIME FACT AND SAYS SO. The claim state is re-read
# immediately before this send, but the age was measured when the scan ran, and
# a message that reports a measured number as if it were current is the exact
# class this rung already fixed once inside `scan` (three independent
# time.time() reads that let the filter and the report disagree about one row).
_WAKE_RECIPIENT_TEXT = (
    "@%(recipient)s IDLE-DISPATCH WAKE: you own dispatch %(id8)s (%(lane)s) "
    "from @%(sender)s and it is OPEN with nothing claimed against it — as of "
    "this scan you held no live claim on any resource and had shown no recent "
    "activity for ~%(age_min)dmin%(od)s. This is not a diagnosis: it cannot "
    "tell an idle-done seat from one that never saw the row, and both happen. "
    "TWO HONEST EXITS — take it (`helm work claim <lane>`, or reply in the "
    "room that you have it), or say in the room that it is NOT yours so it can "
    "be rerouted. If `helm dispatch list --open` shows you NOTHING that is a "
    "known identity-binding defect and not proof the row is gone: read it by "
    "id with `helm dispatch list --open --mine --json`, which is the spelling "
    "no project scope narrows (add `--all-projects` to widen the project axis "
    "on any other selector). [idle-dispatch watchdog]")
# BOTH RECOVERY SPELLINGS MUST OUTLIVE PROJECT SCOPE (task/2437 round two,
# finding 4). This text is read by a seat that ALREADY cannot see its row, and
# the two commands it named — `list --open` and `list --json` — are both scoped
# to the caller's checkout now, so the recovery advice pointed a seat standing
# in another project's tree at two listings that would hide the row again. The
# identity flags are the exempt axis by law (a row that names you is yours
# wherever its code lives), so the recovery names THEM, and names the escape for
# every other question.


def _held_phrase(held):
    if not held:
        return "a live claim"
    if len(held) == 1:
        return "a live claim on %s" % held[0]
    return "%d live claims (%s)" % (len(held), ", ".join(held[:3]))


def _why_phrase(f):
    """The parenthetical after `presence`, WHICH IS A CLAIM AND NOT A CONSTANT.

    "(no recent activity)" is true of every row admitted for being QUIET, and
    false of one admitted for being WALLED: a walled seat has plenty of recent
    activity, what it lacks is progress. An alert that contradicts, two words
    later, the presence bucket it has just printed has spent its credibility to
    say nothing, and leaves the reader to pick which half to believe."""
    if f.get("fresh_walled"):
        return ("still crossing tool boundaries, so this is NOT quiet — its "
                "PROVIDER is refusing it, and activity here is not progress")
    return "no recent activity"


def _admit_phrase(f):
    """Why THIS reader is being told, when the answer is not obvious.

    A finding that arrives with no account of its own admission reads as an
    ordinary alert, and both new admissions look WRONG without one: an alert
    about a seat that is demonstrably busy, and an alert to a coordinator who
    never sent the row. Empty for every row that reached the scan the ordinary
    way, so the untouched cases stay untouched."""
    out = ""
    if f.get("self_addressed"):
        out += (" THIS ROW IS SELF-ADDRESSED: @%s is both its sender and its "
                "recipient, so no peer was ever going to be told and no seat's "
                "own `dispatch list --mine --issued` contains it except "
                "theirs. You are reading it because you are NEITHER."
                % f.get("recipient"))
    if f.get("fresh_walled"):
        out += (" IT REACHED YOU THROUGH THE FRESHNESS GATE, which normally "
                "drops a row on an active recipient: freshness measures "
                "whether a process is TICKING and this rung asks whether work "
                "can COMPLETE, and for a walled seat those are opposite "
                "readings.")
    return out


def _alert_text(f):
    """The alert for ONE finding, chosen by its claim state. Three states, three
    recommendations, because reassigning a quiet HOLDER destroys their work and
    reassigning on an UNREAD ledger is the same act with less evidence. An
    unrecognised state falls back to the UNKNOWN text — the safe one — so no
    future state can inherit the reassign recommendation by accident."""
    od = " (past its deadline)" if f.get("overdue") else ""
    # LIVENESS AND CLAIMS ARE DIFFERENT AXES, AND COLLAPSING THEM IS WHAT
    # PRODUCED A FALSE STRANDED AGAINST A SEAT MID-GATE. "Quiet" and "holds no
    # claim" are each true of a working reviewer almost continuously —
    # FRESH_S is 120s while a whole-suite gate on this fleet runs ~835s, and
    # the review role takes no worktree lease — so their conjunction is the
    # STEADY STATE of that role, not a signal about it. A MEASURED LIVE PANE
    # is a POSITIVE reading and outranks an inference drawn from two absences.
    #
    # THE ALERT STILL FIRES, and that is deliberate: the sender's obligation
    # really is idle and they still need to know. Only the WORD the reader
    # acts on changes, and with it the prescribed disposition — STRANDED says
    # re-check or reassign, and reassigning a seat that is gating the row is
    # the one action that destroys work. Nothing is suppressed and the claim
    # state is untouched.
    text = _TEXTS.get(f.get("claim"), _UNKNOWN_TEXT)
    if f.get("claim") == CLAIM_NONE and f.get("live_pane"):
        text = _WORKING_TEXT
    return text % {
        "sender": f["sender"], "id8": f["id8"], "lane": f["lane"],
        "recipient": f["recipient"], "presence": f["presence"],
        "age_min": f["age_min"], "od": od,
        "held": _held_phrase(f.get("held")),
        "context": f.get("context") or "",
        # .get with a default, like "context" beside it: this dict is built
        # EXPLICITLY, so a field added to the finding and not here raises
        # KeyError inside the alert path — which is exactly what happened and
        # showed up as ZERO alerts rather than an error.
        "wall": f.get("wall") or "",
        # every template that names %(live_pane)s needs it here, and the
        # KeyError-means-ZERO-alerts warning above is why this line ships in
        # the same commit as the template that uses it
        "live_pane": f.get("live_pane") or "",
        # BOTH DEFAULT THROUGH THEIR COMPOSER, never a bare .get: a template
        # naming a key this dict lacks raises KeyError inside the alert path
        # and surfaces as ZERO alerts — the failure the comment above records
        # happening once already.
        "why": _why_phrase(f),
        "admit": _admit_phrase(f),
        "tail": _stranded_tail(f)}


def _stranded_tail(f):
    """The closing recommendation, which must not CONTRADICT the facts above it.

    The STRANDED text ended with a fixed "It may be idle-done or parked
    WITHOUT reporting … Re-check or reassign". When a contradicting fact is
    present that reads as a self-negating alert: the wall fact says the seat
    is not parked and its provider is refusing it, or the pane fact says it
    is LIVE and between turns — and then the very next sentence says it may
    be parked and should be reassigned. An alert that asserts both readings
    has told the reader nothing and leaves them to pick, which is how the
    destructive one gets picked.

    This existed before the pane fact and the pane fact made it louder, so it
    is cured here rather than inherited. The default is UNCHANGED for a row
    with no contradicting fact — the ordinary stranded case is untouched."""
    if f.get("wall"):
        return (" This alert cannot tell whether reassignment would help; inspect "
                "the measured remediation and work ownership before acting. ")
    if f.get("live_pane"):
        # A LIVE PANE AND AN ARMED BEACON ARE DIFFERENT FACTS, AND THE REMEDY
        # TRAVELS THE SECOND ONE. `live_pane` says a process is there; an
        # @mention reaches the seat through its beacon, and `wake_route`
        # already measures whether one is listening — this module's own
        # WAKE_NONE names the consequence, "a DM is queued bytes". Naming the
        # wake route without consulting it sends the sender at a door this
        # same finding knows is shut, while the sentence beside it forbids the
        # only other move. The console line has consulted `wake_route` all
        # along, so the two surfaces built from ONE finding disagreed: the
        # operator read "no wake: beacon none" and the sender was told to use
        # the wake route.
        #
        # THREE-WAY, BECAUSE `WAKE_UNKNOWN` IS NOT `WAKE_NONE` — the constant
        # says so in its own comment, "the probe could not tell — never
        # none". Collapsing them with `!= WAKE_ARMED` would tell the sender a
        # beacon is dead on evidence that only says it was not measured, which
        # is the same unmeasured-as-negative error one refusal further on.
        if f.get("wake_route") == WAKE_NONE:
            return (" DO NOT reassign on this alert: the measured live pane "
                    "explains the quiet. But NO BEACON IS LISTENING, so an "
                    "@mention queues rather than wakes — this seat needs "
                    "pane-level tending to get a turn, and reassignment is "
                    "still not automatic. ")
        if f.get("wake_route") != WAKE_ARMED:
            return (" DO NOT reassign on this alert: the measured live pane "
                    "explains the quiet. The wake path could NOT be measured, "
                    "so an @mention may queue rather than wake; confirm a "
                    "beacon before relying on it. ")
        return (" DO NOT reassign on this alert: the measured live pane explains "
                "the quiet; use its named WAKE route. ")
    return (" It may be idle-done or parked WITHOUT reporting (the "
            "assume-busy gap). Re-check or reassign — never auto-poached. ")


LEG_ALERT = "alert"           # leg A: tell the SENDER their obligation is idle
LEG_WAKE = "wake"             # leg B: wake the RECIPIENT who owes the row
LEG_REDELIVER = "redeliver"   # leg C: an obligation whose delivery never landed
# THE DOMAIN THIS LEG ACTUALLY MEASURES, named once. Machine consumers get
# it as a field, humans get it in the line, and both read this constant.
# THE DOMAIN. The one genuinely hardcoded fact: which delivery marker this leg
# reads. Everything else about the scope is DERIVED from it and from
# REDELIVERABLE_COMPLETE, so no two surfaces can disagree by edit.
REDELIVERABLE_SCOPE_KIND = "dispatch-row"
# THE HUMAN HALF IS DERIVED FROM THE MACHINE HALF, never retyped beside it.
# The defect this cures: the constant reached the JSON field only,
# every human line spelled "dispatch-row" by hand, and the arm asserting one
# shared spelling executed the JSON path alone — so it stayed green with every
# human scope word deleted. Two surfaces promising the same thing in two
# independently-typed spellings is two promises, and one of them rots silently.

# THE ONE FACT BOTH SURFACES REPORT. The human sentence and the machine field
# are derived from THIS, never written twice — a review caught that a hardcoded
# "a FLOOR, not a total" beside an independent redeliverable_complete lets one
# edit contradict the other, which is the same two-promises defect one layer in.
REDELIVERABLE_COMPLETE = False
# THE SLUG'S SUFFIX IS THE COMPLETENESS FACT, not a second copy of it. Flipping
# REDELIVERABLE_COMPLETE moves the machine slug, the machine field and the human
# sentence together or it moves none of them.
REDELIVERABLE_SCOPE = "%s-%s" % (
    REDELIVERABLE_SCOPE_KIND, "total" if REDELIVERABLE_COMPLETE else "floor")
REDELIVERABLE_SCOPE_NOTE = (
    "%s scope — %s: this leg reads %s delivery, not obligation-level delivery"
    % (REDELIVERABLE_SCOPE_KIND,
       "a COMPLETE total" if REDELIVERABLE_COMPLETE else "a FLOOR, not a total",
       REDELIVERABLE_SCOPE_KIND))
WAKE_LATCH = "wake:"          # retained: leg B's record key prefix

# How long a reservation may stand before it is treated as a CRASHED attempt.
# Bound by how long a DM may legitimately take plus margin — NOT by the alert
# cadence, because a process that died mid-send should be retryable in seconds
# rather than blocking its own row for a quarter of an hour.
INFLIGHT_TTL_S = 120


def _record_key(leg, id8):
    """ONE RECORD PER (leg, id8), holding told/retry/inflight as FIELDS.

    Three separate key namespaces would be three things the re-arm sweep has to
    know about, and the sweep drops every key it did not SEE this pass — a
    namespace it does not enumerate is reaped every single pass, silently
    turning a backoff into no latch at all. That already happened once here.
    One record per leg makes a partial view unrepresentable: the sweep deletes
    the whole record or none of it."""
    return "%s:%s" % (leg, id8)


def _new_token():
    """An unpredictable reservation token. CAS equality IS ownership, so it
    carries no process identity and is never resumed by a new process — only
    observed until it expires and then replaced."""
    import secrets
    return secrets.token_hex(8)


def _blocked(rec, now):
    """"" or the EXACT reason this leg may not send right now.

    THREE TRUTHFUL STATES, not one latch. TOLD means delivered and drives the
    informational backoff. RETRY means a send was attempted and failed, and
    drives its own throttle on its own counter — sharing one counter is how a
    backoff can erase itself, because restoring the prior entry to preserve the
    informational count restores the very count that provides the backoff.
    INFLIGHT means another pass owns an attempt right now; an overlapping pass
    then observes a TRUE in-flight rather than a fake delivery latch."""
    told = rec.get("told") or {}
    if told.get("at"):
        # THE STORED COUNT IS THE EXPONENT, not the count minus one. After a
        # first delivery n == 1 and the next alert owes 2x LATCH_TTL_S — the
        # doubling IS the fix this backoff exists to be, and an off-by-one
        # here silently halves every wait. A pre-existing arm pins it.
        n = int(told.get("n") or 0)
        wait = min(LATCH_TTL_S * (2 ** n), LATCH_BACKOFF_CAP_S)
        if now - told["at"] < wait:
            return "told %dmin ago; next after %dmin" % (
                (now - told["at"]) // 60, wait // 60)
    retry = rec.get("retry") or {}
    if retry.get("next_at") and now < retry["next_at"]:
        return "retry throttled for %ds (%s)" % (
            int(retry["next_at"] - now), retry.get("reason") or "send failed")
    infl = rec.get("inflight") or {}
    if infl.get("expires_at") and now < infl["expires_at"]:
        return "another pass holds an in-flight attempt"
    return ""


def _undelivered_obligations():
    """The OWED rows whose obligation was never delivered — task/928 slice B.

    CONSUMES `dispatches.owed()` RATHER THAN RE-DERIVING THE DEBT. That verb is
    the fleet's one answer to who-owes-this-row: it builds the successor index
    ONCE and threads it, because asking carrier() per row over a 1,300-row
    ledger is, in its author's words, the shape that makes a predicate too
    expensive to adopt. A second enumerator here would be a second opinion
    nobody asked for, and the two would drift the way `_open` and `_not_closed`
    already did.

    UNDELIVERED IS `delivery != observed`, WHICH IS THE LEDGER'S OWN WORD.
    `observed` is earned by a mention the recipient's beacon actually matched;
    anything else means the seat was never demonstrably told. The precedent to
    copy is retip, which DOWNGRADES an observation earned against the OLD tip —
    a delivery is evidence about a ROW-STATE, not about a row.

    RETURNS None WHEN THE LEDGER CANNOT BE READ, never []. An unreadable ledger
    is not an empty one: [] would report "nothing is undelivered" and, worse,
    would let the re-arm sweep reap every latch it did not see this pass.
    """
    from . import dispatches
    snap, unavailable = dispatches.snapshot()
    if unavailable or not isinstance(snap, dict):
        return None
    out = []
    for row in dispatches.owed(snap):
        if str(row.get("delivery") or "") == "observed":
            continue
        if not str(row.get("recipient") or "").strip():
            continue
        out.append(row)
    return out


def _wake_text(f):
    """The recipient's wake. Rendered inside its own leg's failure boundary
    (see check): this module already paid once for a template KeyError that
    surfaced as ZERO alerts rather than an error, and a second addressee
    doubles the number of templates that can do it."""
    od = " (past its deadline)" if f.get("overdue") else ""
    return _WAKE_RECIPIENT_TEXT % {
        "recipient": f["recipient"], "id8": f["id8"], "lane": f["lane"],
        "sender": f["sender"], "age_min": f["age_min"], "od": od}


def _claims_by_holder():
    """(holder-index, live-resource-keys, readable?) — ONE fresh claims read,
    taken after scan's probes and before any send is owed.

    BOTH HALVES ARE NEEDED BECAUSE `scan` USES BOTH. It excludes a row whose
    `dispatch:<id8>` key is claimed BY ANYONE — that key means the row is
    being worked, whoever holds it — and separately asks whether THIS
    recipient holds anything. A recheck that carried only holder membership
    re-admitted a row another seat had claimed in the interim and sent the
    wake to the seat that no longer owed it.

    WHY A SECOND READ AT ALL: scan's claim answer is minutes stale by the time
    it matters, because scan then spends real time on presence, seat_liveness
    and beacon probes for every finding. The seat that picks up its row during
    that window is the single most likely thing to happen next, and waking it
    for a row it just claimed trains it to skip the channel.

    AND AN UNREADABLE LEDGER REFUSES THE WAKE rather than permitting it. That
    is this module's standing law one door over — an unreadable claims ledger
    cannot authorize a poke either — and it is why the caller must not burn the
    wake latch on this path: a refusal for lack of evidence has to be RETRIED
    when the evidence returns, not recorded as a wake that happened."""
    try:
        claims = seats._live_claims()
    except Exception:
        return {}, set(), False
    if claims is None:
        return {}, set(), False
    by_holder, keys, anon = _claim_index(claims)
    if anon:
        # the same asymmetry _claim_index already keeps: a claim we cannot
        # ATTRIBUTE cannot be ruled out as this recipient's
        return by_holder, keys, False
    return by_holder, keys, True


def _deliver(to, render, f, leg):
    """Render and send ONE message -> did it actually go?

    RENDERING IS INSIDE THE BOUNDARY because a template KeyError has already
    cost this module every alert in a pass once, and a second addressee
    doubles the templates that can do it.

    RETURNS `(ok, reason)`, NOT A BARE BOOL. A refusal that arrives as False
    with its reason dropped on the floor reaches the retry record as the
    generic "send failed" and reaches the operator as nothing at all — the
    transport knew exactly why and every layer above it had to guess.

    AND THE TRANSPORT'S ANSWER IS READ. `seats.dm` reports a refusal by
    RETURNING `(None, reason)`, not by raising, so a caller that only catches
    exceptions records a delivery for a message the resolver declined,
    reporting a send on a row nobody was sent. A
    non-tuple return is treated as success, which is what a transport that
    signals purely by raising means.
    """
    try:
        text = render(f)
    except Exception as e:                    # noqa: BLE001 — never break the pass
        print("helm seat idle-dispatch: %s render failed (%s -> %s): %s"
              % (leg, f.get("id8"), to, e), file=sys.stderr)
        return False, "render failed: %s" % e
    try:
        result = seats.dm(to, text, who="idle-dispatch")
    except Exception as e:   # a down chat node never blocks detection
        print("helm seat idle-dispatch: %s dm failed (%s -> %s): %s"
              % (leg, f.get("id8"), to, e), file=sys.stderr)
        return False, "transport raised: %s" % e
    # FAIL CLOSED ON ANYTHING THAT IS NOT THE ONE SUCCESS SHAPE. seats.dm has
    # exactly one contract — `(row, None)` — so None, a 1-tuple, a 3-tuple and
    # a bare object are all CONTRACT VIOLATIONS, not dialects to normalise.
    # Treating them as success was measured admitting every one of them, and
    # the reason it survived review is that the test fixture returned None
    # itself: the arms validated a shape production never emits.
    if not (isinstance(result, tuple) and len(result) == 2):
        print("helm seat idle-dispatch: %s dm returned %r, not (row, err) "
              "(%s -> %s) — treating as NOT delivered"
              % (leg, type(result).__name__, f.get("id8"), to), file=sys.stderr)
        return False, "contract violation: returned %s" % type(result).__name__
    row, err = result
    # `err is not None`, NOT `if err`. The sealed contract permits exactly
    # (row, None); a reviewer measured (row, ""), (row, False) and (row, 0) all
    # reading as accepted under a truthiness test. A transport that reports an
    # empty-string reason is reporting a REASON.
    if err is not None or row is None:
        print("helm seat idle-dispatch: %s dm REFUSED (%s -> %s): %s"
              % (leg, f.get("id8"), to, err or "no row"), file=sys.stderr)
        if err is None:
            return False, "accepted but returned no row"
        # A REFUSAL THAT NAMES NOTHING IS STILL A REFUSAL, and str("") is "".
        # The arm for the falsey-error table caught exactly this: the empty
        # string reached the retry record and the operator's line as blank,
        # which reads as "no reason given" — indistinguishable from a bug in
        # this function rather than a terse transport.
        return False, str(err) or (
            "transport refused with an EMPTY reason (%r)" % (err,))
    return True, ""


def _release(key, token):
    """Drop a reservation WITHOUT advancing either counter.

    THE THIRD OUTCOME, and its absence is what made a vanished precondition
    unrepresentable. `_finish` has exactly two moves: success advances TOLD,
    failure advances RETRY. A wake we DECLINED TO ATTEMPT is neither. Calling
    it a failure would back off a send nobody made — the next pass would wait
    out a penalty earned by the recipient's beacon exiting, which is the one
    party that did nothing wrong. Calling it a success would claim a wake.

    So the reservation is simply released and the record is left exactly as it
    was found, which lets the very next pass try again the moment a beacon is
    armed. Token equality is ownership here for the same reason it is in
    `_finish`: a successor may already own this record."""
    from . import pk
    path = _state_path()
    try:
        with open(path + ".lock", "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            st = pk.read_json(path, {}) or {}
            rec = st.get(key)
            if not isinstance(rec, dict):
                return False
            if (rec.get("inflight") or {}).get("token") != token:
                return False          # a successor owns it; never touch it
            rec = dict(rec)
            rec.pop("inflight", None)
            st[key] = rec
            pk.write_json(path, st)
            return True
    except Exception as e:                    # noqa: BLE001
        print("helm seat idle-dispatch: release failed (%s): %s" % (key, e),
              file=sys.stderr)
        return False


def _finish(key, token, ok, reason=""):
    """CAS the reservation FORWARD — never back.

    ONLY THE TOKEN OWNER MAY FINISH. Equality of the unpredictable token IS
    ownership; a mismatch means a successor already owns this record and this
    result is stale, so it is dropped rather than written.

    AND THE MOVE IS ALWAYS FORWARD. Restoring the entry a reservation replaced
    was the previous design and it quietly destroyed its own backoff: the
    restore put back the counter that PROVIDES the backoff, so repeated
    failures never advanced and a permanently refusing transport retried every
    pass. Success advances TOLD and clears RETRY; failure advances RETRY on its
    own counter. Neither borrows the other's, and no path rewinds either.

    AT-LEAST-ONCE, AND THE WINDOW IS NAMED: if the send SUCCEEDS and this CAS
    never lands (a crash between the two), the reservation expires and a later
    pass sends again. Exactly-once would need a transport idempotency key plus
    a recovery lookup; without one, a duplicate wake after a crash is strictly
    safer than a wake lost forever.

    CENSUSED RATHER THAN INFERRED: seats.dm accepts an `origin` parameter, but
    nothing in its contract documents it as a dedup key — its docstring
    describes routing identity, lane placement and threading, and its return
    is exactly `(row, None)` or `(None, reason)`. So there is no transport
    idempotency to build on today, and this module claims AT-LEAST-ONCE. If
    `origin` ever gains that meaning, upgrading this claim is a separate row
    with its own evidence, never a quiet re-reading of the same code."""
    from . import pk
    path = _state_path()
    now = time.time()
    try:
        with open(path + ".lock", "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            st = pk.read_json(path, {}) or {}
            rec = st.get(key)
            if not isinstance(rec, dict):
                return False
            if (rec.get("inflight") or {}).get("token") != token:
                return False          # a successor owns it; never overwrite
            rec = dict(rec)
            rec.pop("inflight", None)
            if ok:
                told = dict(rec.get("told") or {})
                told["at"] = now
                told["n"] = int(told.get("n") or 0) + 1
                rec["told"] = told
                rec.pop("retry", None)
            else:
                retry = dict(rec.get("retry") or {})
                n = int(retry.get("n") or 0) + 1
                retry.update({"n": n, "at": now,
                              "reason": reason or "send failed",
                              "next_at": now + min(
                                  RETRY_TTL_S * (2 ** (n - 1)),
                                  LATCH_BACKOFF_CAP_S)})
                rec["retry"] = retry
            st[key] = rec
            pk.write_json(path, st)
            return True
    except Exception as e:                    # noqa: BLE001
        print("helm seat idle-dispatch: finish failed (%s): %s" % (key, e),
              file=sys.stderr)
        return False


def check(post=True, quiet=False):
    """One read-only pass: scan -> per-leg dedup-latch -> DM. Returns
    {"findings": [...], "alerted": [...], "woke": [...]}. The fcntl lock covers
    the scan/latch/write transaction so overlapping passes never double-alert.

    TWO FIRST-CLASS LEGS, INDEPENDENTLY ADMITTED AND INDEPENDENTLY LATCHED.
    Leg A tells the SENDER their obligation is idle; leg B wakes the RECIPIENT,
    the one seat that can discharge it. They are not two views of one alert:
    they have different addressees, different admission (leg B fires only on a
    STRANDED row with a measured armed beacon and no provider wall) and
    different truth conditions, so they get different latch keys. Sharing one
    key would let whichever leg fired first silently consume the other's alert
    for the whole backoff window — and since leg A always admits and leg B
    admits narrowly, the loser would always have been the wake.

    THE LEGACY KEY IS READ AS SENDER-ONLY. State files written before this
    carry bare id8 keys, and those are leg A's; the wake takes its own
    namespace so an existing latch can never suppress a leg that has never run.

    THE RE-ARM SWEEP MUST SEE BOTH SPELLINGS. It drops latches for dispatches
    no longer stranded, keyed on what this pass saw — so a wake key absent from
    `seen` would be reaped every single pass, silently converting the backoff
    into no latch at all. That is the "every filter was written for ONE
    addressee" trap in its purest form: the sweep is correct code that becomes
    wrong the moment a second key namespace exists."""
    from . import pk
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        findings = scan()
        # the fresh claim read that leg B's send is owed against — taken here,
        # after scan's probes, so the latch decision and the evidence it rests
        # on are one transaction rather than two
        held_now, keys_now, claims_readable = _claims_by_holder()
        st = pk.read_json(p, {}) or {}
        now = time.time()
        alerted = []
        woke = []
        # latch key -> the entry it REPLACED. The latch is written under this
        # lock so an overlapping pass cannot double-send, and retracted after
        # a delivery that did not happen — restoring the prior entry rather
        # than dropping it, so a persistently failing send re-backs-off
        # instead of nagging at the floor interval forever.
        # `--quiet` NEITHER RESERVES NOR MUTATES. A flag documented as
        # skipping the DM must not consume the state that means "told": it
        # reserved both legs and sent nothing, and the next ordinary pass read
        # that as already-told and sent zero. A suppressed send is not an
        # attempt, so it leaves no trace at all rather than one that is undone
        # afterwards.
        sending = bool(post and not quiet)
        seen_keys = set()
        for f in findings:
            key = _record_key(LEG_ALERT, f["id8"])
            wake_key = _record_key(LEG_WAKE, f["id8"])
            seen_keys.add(key)
            # THE WAKE KEY IS MARKED SEEN ONLY IF THE ROW IS STILL WAKE-
            # ELIGIBLE, and that is decided below rather than here. Marking it
            # unconditionally kept a completed wake's TOLD record alive through
            # states that are NOT stranded — CLAIM_HOLDING above all. A
            # recipient who took a normal worktree claim carried the previous
            # episode's backoff, so if that claim was later released while the
            # dispatch stayed open, the row re-stranded and the wake was
            # SUPPRESSED by a latch earned in an episode that had already
            # ended. The re-arm contract says a picked-up row that re-strands
            # alerts again; this made that false for leg B only.
            entry = st.get(key)
            # BACKOFF, because a flat re-alert on a condition that may never
            # resolve is an unbounded alert source. LATCH_TTL_S alone meant one
            # DM every 15 MINUTES for as long as a dispatch stayed stranded —
            # and a dispatch can stay stranded for days (a retired recipient, a
            # lane nobody picks up). Measured 2026-07-30: one row stranded ~37h
            # had generated ~148 identical DMs to its sender, and EVERY ONE is a
            # permanent obligation in that seat's stop-guard, which counts
            # undelivered rows and is not discharged by reading. The sender's
            # inbox reached 497 undelivered — past the point where any of them
            # can be acted on individually, so the guard that exists to surface
            # obligations had buried them.
            #
            # A watchdog that has said the same thing 148 times is not informing
            # anyone; it is training them to skip the channel. Each repeat
            # doubles the wait, capped, so a genuinely stuck row still speaks —
            # just at a rate a human or a seat can absorb. ~148 alerts over 37h
            # becomes ~14.
            rec = st.get(key) or {}
            why = _blocked(rec, now)
            f["latched"] = bool(why)
            f["skip_reason"] = why
            if not why:
                # DECIDE ALWAYS, RESERVE ONLY WHEN SENDING. `--dry-run` exists
                # to report what WOULD alert without consuming anything, so
                # gating the DECISION on sending — rather than only the write
                # — makes it report nothing at all, which is the opposite of
                # its contract.
                f["repeat"] = int((rec.get("told") or {}).get("n") or 0)
                if sending:
                    token = _new_token()
                    f["token"] = token
                    rec = dict(rec)
                    rec["recipient"] = f["recipient"]
                    rec["inflight"] = {"token": token,
                                       "expires_at": now + INFLIGHT_TTL_S}
                    st[key] = rec
                alerted.append(f)

            # ---- LEG B: wake the seat that OWES the row --------------------
            # NO `continue` ABOVE, and that is the whole point. Leg A used to
            # end the iteration when it was latched, so a second leg written
            # underneath it would silently never run for any row whose sender
            # alert had already fired — which, given leg A admits every finding
            # and fires first, is every row that matters.
            f["wake_sent"] = False
            f["wake_latched"] = False
            f["wake_skipped"] = ""
            if f.get("wakeable"):
                # Still stranded and still routable: this episode continues, so
                # its record must survive the sweep.
                seen_keys.add(wake_key)
            if not f.get("wakeable"):
                # not a candidate at all: holding, unknown, walled, no beacon.
                # Left silent rather than recorded as a skip — a row that was
                # never admitted has nothing to explain.
                continue
            if not claims_readable:
                # AN UNREAD FACT CANNOT AUTHORIZE A POKE. The latch is
                # deliberately NOT burned: this is a refusal for missing
                # evidence and must retry when the evidence returns, not be
                # recorded as a wake that happened.
                f["wake_skipped"] = ("claims ledger unreadable at send time — "
                                     "refused, will retry")
                continue
            if ("dispatch:" + f["id8"]) in keys_now:
                # SOMEBODY took the row itself between the scan and now. Which
                # seat holds it does not matter: the obligation is being
                # worked, and waking the original recipient addresses a seat
                # that no longer owes it. Not latched — if that claim lapses
                # and the row re-strands, it deserves a wake.
                f["wake_skipped"] = ("the dispatch was claimed after the scan "
                                     "— no longer stranded")
                continue
            if held_now.get(f["wake_target"]):
                # they picked it up between the scan and now, which is the
                # single most likely thing to have happened. Also not latched:
                # if they release it and re-strand, that deserves a wake.
                f["wake_skipped"] = ("recipient claimed work after the scan — "
                                     "no longer stranded")
                continue
            wrec = st.get(wake_key) or {}
            wwhy = _blocked(wrec, now)
            f["wake_latched"] = bool(wwhy)
            if wwhy:
                f["wake_skipped"] = wwhy
            else:
                f["wake_repeat"] = int((wrec.get("told") or {}).get("n") or 0)
                if sending:
                    wtoken = _new_token()
                    f["wake_token"] = wtoken
                    wrec = dict(wrec)
                    wrec["recipient"] = f["recipient"]
                    wrec["inflight"] = {"token": wtoken,
                                        "expires_at": now + INFLIGHT_TTL_S}
                    st[wake_key] = wrec
                woke.append(f)
        # LEG C — THE UNDELIVERED OBLIGATIONS (task/928 slice B). This half
        # ENUMERATES and does not send: the owner asked to be ABLE TO BURN DOWN
        # ALL ROWS, and a seat that cannot ask "what do I owe" cannot start.
        # The SEND is deliberately not wired here, because escalation counts
        # UNACKNOWLEDGED attempts and `acknowledged` has no field yet — a
        # counter over an undefined predicate escalates on noise.
        #
        # THE SEEN-KEY OBLIGATION IS RECORDED HERE FOR WHOEVER WIRES THE SEND:
        # this population comes from owed(), NOT from scan()'s findings, so the
        # moment a latch record exists under LEG_REDELIVER its key MUST be
        # added to seen_keys. The reaper below drops every key it did not see
        # this pass, which would silently convert the backoff into no latch at
        # all — the exact trap this module's own docstring says it has already
        # sprung once.
        owed_rows = _undelivered_obligations()
        if owed_rows is None:
            # UNREADABLE IS NOT EMPTY. Reporting [] here would say "nothing is
            # owed" for a ledger nobody could read.
            redeliverable = None
        else:
            redeliverable = [{"id8": str(r.get("id") or "")[:8],
                              "id": r.get("id"),
                              "recipient": r.get("recipient"),
                              "lane": r.get("lane"),
                              "delivery": r.get("delivery") or "",
                              "leg": LEG_REDELIVER}
                             for r in owed_rows]
        # re-arm: drop latches for dispatches no longer stranded (resolved /
        # picked up), so a later re-strand of the SAME id alerts again
        for k in [k for k in st if k not in seen_keys]:
            st.pop(k, None)
        # NON-MUTATING UNLESS WE ACTUALLY SEND. Both --dry-run and --quiet
        # report what WOULD happen, so neither may consume a latch, mint a
        # reservation, or REAP. The reap is the sharp edge: this block drops
        # every record whose dispatch is no longer stranded, so a --quiet pass
        # over an empty scan was measured erasing a live TOLD record — the
        # next real pass then re-alerted a seat it had already told.
        if sending:
            pk.write_json(p, st)

    if sending:
        # TWO SENDS, TWO BOUNDARIES. Each leg's render AND delivery sit inside
        # its own try, so neither a down chat node nor a template KeyError in
        # one addressee's text can take the other addressee's message with it.
        # This module has already been bitten by exactly that: a field added to
        # the finding and not to the format dict raised inside the alert path
        # and surfaced as ZERO alerts rather than as an error.
        # RE-READ WHAT IS STILL OPEN. The scan read OPEN rows ONCE, and the
        # only fresh read before sending was the CLAIMS ledger — so a verdict,
        # cancel or close landing after the scan left claims empty and both
        # messages went out telling two seats a TERMINAL dispatch was open and
        # owed. Claims answer "who is holding it", never "does it still exist".
        #
        # AN UNREADABLE LEDGER DOES NOT SUPPRESS. If this read fails we cannot
        # prove anything closed, and refusing every send on missing evidence
        # would silence the whole rung on a transient error — the opposite of
        # the failure this cures. Absence of proof of closure is not proof.
        def _live_rows():
            """The OPEN rows as of RIGHT NOW, by id. None when unreadable."""
            try:
                return {str(r.get("id") or ""): r for r in dispatches.open_rows()}
            except Exception as e:            # noqa: BLE001
                print("helm seat idle-dispatch: open-row recheck failed: %s" % e,
                      file=sys.stderr)
                return None

        def _went_terminal(f):
            """Is this finding still owed, MEASURED AT THIS SEND BOUNDARY.

            A SNAPSHOT TAKEN ONCE BEFORE THE LOOP IS A WINDOW, NOT A CHECK.
            The previous cure read a SET OF IDS before every sender alert and
            every wake, so a row closing during the loop still sent for every
            element after the first — the same measured-early-used-late defect
            one layer finer, which is exactly how the scan-time version failed.

            AND IDENTITY IS NOT JUST THE ID. An OPEN REBIND keeps the dispatch
            id and CHANGES THE RECIPIENT. Under an id-only test the row is
            present, nothing looks stale, and the wake goes to the seat that
            USED TO owe it — a live seat, correctly addressed, told to pick up
            work that is no longer theirs. No freshness fix reaches that case;
            only binding the recipient does.

            AN UNREADABLE LEDGER DOES NOT SUPPRESS: absence of proof of
            closure is not proof of closure, and refusing every send on a
            transient read error would silence the rung this exists to keep
            honest."""
            rows = _live_rows()
            if rows is None:
                return False
            row = rows.get(str(f.get("id") or ""))
            if row is None:
                return True                   # closed or cancelled since scan
            # CANONICAL AGAINST CANONICAL. scan() puts the HUMAN-FACING
            # spelling in f["recipient"] (recipient_display), and the row
            # carries the canonical one — so comparing those two fields marked
            # a perfectly stable row as REBOUND on every send. A row with
            # recipient "ds4pro" and display "@DS4Pro" released BOTH legs
            # without delivering either, and my own rebind arm could not see
            # it because its fixture used identical spellings on both sides.
            # f["recipient_key"] is the canonical form scan already computed.
            was = str(f.get("recipient_key") or "")
            now_recip = _recipient_key(row.get("recipient"))
            if was and now_recip and was != now_recip:
                f["rebound_to"] = now_recip
                return True                   # rebound: no longer this seat's
            return False

        undelivered = []
        for f in alerted:
            if _went_terminal(f):
                f["sent"] = False
                f["closed_after_scan"] = True
                _release(_record_key(LEG_ALERT, f["id8"]), f["token"])
                continue
            ok, why = _deliver(f["sender"], _alert_text, f, "alert")
            f["sent"] = ok
            f["send_error"] = why
            _finish(_record_key(LEG_ALERT, f["id8"]), f["token"], ok, why)
            if not ok:
                undelivered.append(_record_key(LEG_ALERT, f["id8"]))
        for f in woke:
            # RE-MEASURE THE BEACON AT SEND TIME, NOT AT SCAN TIME. The route
            # was proved during the scan; since then this pass has finished
            # scanning and sent every sender alert, and a beacon that exits in
            # that window leaves the DM as queued bytes nobody will read —
            # while wake_sent went True and the terminal printed WOKE. This
            # rung exists to make "no live beacon" unclaimable as a wake, so
            # measuring once and asserting later is the defect wearing the
            # feature's own clothes.
            if _went_terminal(f):
                f["wake_sent"] = False
                f["closed_after_scan"] = True
                _release(_record_key(LEG_WAKE, f["id8"]), f["wake_token"])
                continue
            # RE-MEASURE THE CLAIM BESIDE THE BEACON. held_now/keys_now were
            # taken under the scan lock, before the reservations were written
            # and before EVERY sender alert was delivered. A recipient who
            # picks the row up in that interval — or any seat claiming the
            # worktree — is invisible to the scan-time read, so the wake goes
            # out for work already in hand. The beacon is re-measured here for
            # the same reason; the claim is the same class of fact and was the
            # only one still trusted from before the window.
            held_at_send, keys_at_send, claims_ok = _claims_by_holder()
            # AN UNREAD FACT CANNOT AUTHORIZE A POKE, and this is the OPPOSITE
            # polarity from the open-row check above — deliberately, because
            # the two questions fail in opposite directions and I got them the
            # same way round once already.
            #   OPEN ROWS: absence of proof of CLOSURE is not proof of closure,
            #   so an unreadable ledger must NOT suppress; refusing there would
            #   silence the whole rung on a transient error.
            #   CLAIMS: _claims_by_holder's own contract says an unreadable or
            #   unattributable read must REFUSE the wake — we would be poking a
            #   seat about work we cannot see the ownership of.
            # Declined, not failed: neither TOLD nor RETRY advances, so the
            # next pass wakes them the moment the ledger reads again.
            if not claims_ok:
                f["wake_sent"] = False
                f["wake_skipped"] = "claims unreadable at the send boundary"
                _release(_record_key(LEG_WAKE, f["id8"]), f["wake_token"])
                continue
            if (("dispatch:" + f["id8"]) in keys_at_send
                    or held_at_send.get(f["wake_target"])):
                f["wake_sent"] = False
                f["wake_skipped"] = "picked up after scan, before the wake"
                _release(_record_key(LEG_WAKE, f["id8"]), f["wake_token"])
                continue
            route, _pids = _wake_route(f["wake_target"])
            if route != WAKE_ARMED:
                f["wake_sent"] = False
                f["wake_skipped"] = ("beacon %s at send time (was %s at scan)"
                                     % (route, f.get("wake_route")))
                # RELEASED, NOT FAILED. No send was attempted, so nothing may
                # advance: a RETRY penalty here would delay the wake for the
                # recipient's beacon restarting, and the latch must stay
                # untouched so the next pass wakes them the moment it is back.
                _release(_record_key(LEG_WAKE, f["id8"]), f["wake_token"])
                continue
            ok, why = _deliver(f["wake_target"], _wake_text, f, "wake")
            f["wake_sent"] = ok
            f["wake_error"] = why
            _finish(_record_key(LEG_WAKE, f["id8"]), f["wake_token"], ok, why)
            if not ok:
                undelivered.append(_record_key(LEG_WAKE, f["id8"]))
        # RETRACT THE LATCHES FOR SENDS THAT DID NOT HAPPEN. A latch records
        # "this seat has been told", and a render that raised or a transport
        # that refused told nobody. Each send's outcome is CAS'd forward onto
        # its own reservation above — success advances TOLD, failure advances
        # RETRY on a separate counter — so a failed send never reads as a
        # delivery and never has to be undone.
    return {"findings": findings, "alerted": alerted, "woke": woke,
            "undelivered": undelivered if sending else [],
            # task/928 slice B. A DISTINCT KEY: `undelivered`
            # already means "latch keys whose send failed" in
            # this same dict, and one name for two facts is how
            # a consumer reads the wrong one. None (never [])
            # when the ledger could not be read.
            "redeliverable": redeliverable}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _alert_note(f):
    """The alert leg's line-tail. SENT IS NOT ASSUMED.

    This renderer printed `ALERTED @seat` for every non-latched finding —
    including the ones whose DM the resolver had just REFUSED. `check` knew:
    it set sent=False and stored the reason. The terminal said the seat was
    told. Only stderr disagreed, and nobody reads stderr under a pager.

    That is the exact defect class this whole row exists to close, so the
    renderer is not allowed to be the last place it survives: a leg whose
    refusals are invisible is indistinguishable from a leg that never ran.
    """
    if f.get("rebound_to"):
        return " not sent: rebound to @%s after the scan" % f["rebound_to"]
    if f.get("closed_after_scan"):
        # NOT a failure and NOT a delivery: the row stopped being owed between
        # the scan and the send, so nobody should have been told anything.
        return " not sent: row closed after the scan"
    if f.get("latched"):
        return " (latched)"
    if "sent" not in f:            # --dry-run / --quiet: decided, not sent
        return " would alert @%s" % f["sender"]
    if f["sent"]:
        return " ALERTED @%s" % f["sender"]
    return " ALERT FAILED @%s: %s" % (f["sender"],
                                      f.get("send_error") or "send failed")


def _wake_note(f):
    """The wake leg's own line-tail, so the reader can tell the THREE outcomes
    apart that all used to look like one silent rung: the recipient was woken,
    the recipient was a candidate and something refused it (and what), or the
    row was never a wake candidate at all. A leg whose refusals are invisible
    is indistinguishable from a leg that never runs — which is the whole class
    of defect this row exists to close, so it must not be reintroduced by the
    renderer."""
    # FIVE NAMED OUTCOMES, because this rung's founding defect was a refusal
    # nobody could see: a leg whose skips are silent is indistinguishable from
    # a leg that never runs.
    if f.get("rebound_to"):
        return " | no wake: rebound to @%s after the scan" % f["rebound_to"]
    if f.get("closed_after_scan"):
        return " | no wake: row closed after the scan"
    if f.get("wake_sent"):
        return " | WOKE @%s" % f.get("wake_target")
    if "wake_error" in f:          # a candidate we TRIED and the send refused
        # KEYED ON THE TRIED-MARKER, NOT ON wake_sent. `check` initialises
        # wake_sent=False on EVERY finding before the legs run, so presence of
        # that key means "a finding exists", not "a wake was attempted" —
        # reading it as the latter reports WAKE FAILED on rows that were never
        # candidates. wake_error is written only where a send actually returned.
        return " | WAKE FAILED @%s: %s" % (
            f.get("wake_target"), f.get("wake_error") or "send failed")
    if f.get("wake_repeat") is not None and not f.get("wake_skipped"):
        return " | would wake @%s" % f.get("wake_target")
    if f.get("wake_latched"):
        # told-active, retry-throttled or in-flight — the exact state, with
        # its reason and its next time, never a bare "latched"
        return " | wake held: %s" % (f.get("wake_skipped") or "state active")
    if f.get("wake_skipped"):
        return " | wake refused: %s" % f["wake_skipped"]
    if f.get("stranded") and f.get("wake_route") != WAKE_ARMED:
        return " | no wake: beacon %s" % f.get("wake_route")
    if f.get("stranded") and f.get("wall"):
        return " | no wake: provider wall"
    return ""


def cmd_idle_dispatch(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if "-h" in args or "--help" in args:
        print(_USAGE)
        return 0
    res = check(post="--dry-run" not in args, quiet="--quiet" in args)
    if "--json" in args:
        # THE SCOPE TRAVELS WITH THE DATA, not in the help text. A burn-down
        # consumer reads this dict and nothing else, so an undeclared list is
        # readable only as the total the declared-floor contract forbids.
        # complete is False unconditionally today and will stay so until the
        # obligation seam exists to widen the domain.
        res = dict(res, redeliverable_scope=REDELIVERABLE_SCOPE,
                   redeliverable_complete=REDELIVERABLE_COMPLETE)
        print(json.dumps(res))
    else:
        for f in res["findings"]:
            print("%s -> @%s: %s %s%s ~%dmin%s%s"
                  % (f["id8"], f["recipient"], f["presence"],
                     # THE SAME WORD THE DM USES. Two surfaces answering one
                     # question differently is how a reader learns to trust
                     # neither, so the live-pane demotion is applied here too.
                     ("WORKING" if f.get("live_pane") else "STRANDED")
                     if f["claim"] == CLAIM_NONE
                     else "HOLDING" if f["claim"] == CLAIM_HOLDING
                     else "CLAIM-UNKNOWN",
                     " [%s]" % f["held"][0] if f.get("held") else "",
                     f["age_min"], " OVERDUE" if f["overdue"] else "",
                     _alert_note(f))
                  + _wake_note(f))
        owed_rows = res.get("redeliverable")
        for r in owed_rows or ():
            print("%s -> @%s: OWED on %s, delivery=%s (%s scope)"
                  % (r["id8"], r["recipient"], r["lane"] or "?",
                     r["delivery"] or "none recorded",
                     REDELIVERABLE_SCOPE_KIND))
        if owed_rows is None:
            # THE WHOLE POINT OF THE None. Printing nothing here, or folding
            # this into the empty case below, would answer a question that was
            # never asked of a readable ledger.
            print("owed-undelivered: UNKNOWN — the dispatch ledger could not "
                  "be read. This is NOT an empty answer.")
        elif not res["findings"] and not owed_rows:
            # THE EMPTY ANSWER CARRIES THE SAME QUALIFIER AS THE NON-EMPTY ONE.
            # An unqualified "nothing is owed" is the totalizing claim on the
            # polarity a reader is MOST likely to believe and least likely to
            # question — the narrow branch was honest and this one undid it.
            print("no idle dispatches, and no row is owed undelivered at %s"
                  % REDELIVERABLE_SCOPE_NOTE)
        elif owed_rows:
            print("owed-undelivered: %d row(s) at %s"
                  % (len(owed_rows), REDELIVERABLE_SCOPE_NOTE))
    # rc carries the tri-state a burn-down caller has to branch on: 0 means
    # ANSWERED, non-zero means the ledger could not be read.
    return 0 if res.get("redeliverable") is not None else 1


if __name__ == "__main__":
    raise SystemExit(cmd_idle_dispatch())
