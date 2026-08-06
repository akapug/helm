"""helm.idle_dispatch — the stranded-obligation loud-fail rung.

THE GAP (owner catch 2026-07-24, ds4pro idle-on-an-open-gate): the coordinator
ASSUMES a dispatched agent is BUSY, but a "quiet" presence dot means BOTH
"busy on a long turn" AND "idle-done on an open gate" — indistinguishable, so
only a human glance caught ds4pro sitting idle 68 minutes on an open review.
This rung turns idle-on-an-open-obligation into an OBSERVABLE SIGNAL: it crosses
the dispatch ledger (obligation + deadline, dispatches.py) against the
recipient's presence (seats.presence_of) — the one JOIN neither half computes
today. dispatches is sender-side + clock-only; presence is timestamp-only.

THE SIGNATURE (idle-on-open-dispatch):
    an OPEN dispatch row (not verdict, not cancelled — via dispatches._open,
    which open_rows() applies) whose RECIPIENT is quiet/absent
    (no recent tool boundary) AND the dispatch is older than IDLE_DISPATCH_S (a
    soft "should have been picked up" window, earlier than the hard deadline so
    an idle-on-gate surfaces BEFORE it is overdue — ds4pro was idle on a gate,
    possibly not yet past its deadline).

QUIET IS NOT UNCLAIMED (owner catch 2026-08-03, the codex-3 near-miss). For its
first life this rung tested ONE claim key — `dispatch:<id8>` — and then the
alert said the recipient "holds no claim", a sentence about the SEAT. Those are
different facts. A `dispatch:<id8>` claim is written by exactly one path
(seats.py's idle-seat self-assign offer); a seat that works a dispatch the
NORMAL way — `helm work claim <lane>` — holds `worktree:<project>:<lane>`
instead. Measured on the live estate that day: 21 live claims, every one a
`worktree:…` key, ZERO `dispatch:…` keys. So the clause the alert leaned on was
false for essentially every seat actually working, and it was load-bearing: it
is what turned a quiet holder into a "STRANDED" row whose recommended action was
REASSIGNMENT. codex-3 was DMd as holding-no-claim while holding
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
That un-wired wake is precisely why ds4pro needed a human. Flag-only, never
auto-reassign (the codebase disclaims "reassign on age alone").

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

from . import dispatches, home, seats

LATCH_TTL_S = 15 * 60          # FIRST alert wait; each repeat doubles it
# Ceiling on the doubling. A row stranded for days still speaks — roughly once
# per shift instead of 96 times a day — so the signal survives without the
# channel becoming noise the recipient learns to skip.
LATCH_BACKOFF_CAP_S = 4 * 60 * 60
IDLE_DISPATCH_S = 15 * 60      # soft "should-be-picked-up" window (== QUIET_S);
                               # earlier than the hard deadline so idle-on-gate
                               # surfaces BEFORE overdue (ds4pro was idle first)
_STATE = "idle_dispatch.json"

_USAGE = """usage: helm seat idle-dispatch [--once] [--dry-run] [--quiet] [--json]
  One read-only pass: cross every OPEN dispatch against its recipient's
  presence AND that recipient's directly-looked-up claim state, then DM the
  dispatch's SENDER one latched alert. Three states, three actions:
    STRANDED      quiet + holds no live claim anywhere -> re-check or reassign
    HOLDING       quiet + holds a live claim -> busy or WEDGED owner; RESCUE,
                  never reassign (their room may hold uncommitted work)
    CLAIM-UNKNOWN the claims ledger could not be read -> read it; never
                  reassign on an unread fact
  A DM, not a broadcast, so it wakes a parked coordinator's beacon. Latched:
  one alert per dispatch per episode. --dry-run reports without DMing;
  --quiet skips the DM; --once accepted for stability.
"""


# ---------------------------------------------------------------------------
# the detector — the open_rows x recipient-presence join
# ---------------------------------------------------------------------------

CLAIM_NONE = "none"           # proven: the recipient holds no live claim
CLAIM_HOLDING = "holding"     # proven: the recipient holds >=1 live claim
CLAIM_UNKNOWN = "unknown"     # the ledger could not be read — NOT "none"


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


def _live_pane(seat):
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

    MEASURED 2026-08-05, twice within an hour and on the SAME seat: @codex-2
    was DMd as absent-and-claimless while `seat_liveness` reported state IDLE
    on pane-tail evidence, i.e. a live pane between turns holding an open
    review. Two seats independently reported it STRANDED before either
    checked. For contrast @ds4pro read presence FRESH with the SAME IDLE
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
    try:
        from . import seat as _seat
        row = _seat.seat_liveness(seat) or {}
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
    except Exception:
        return ""


def _provider_wall(seat):
    """"" or a short provider-wall fact — the mirror of _context_pressure,
    for the OPPOSITE claim state.

    THE BLAME THIS CORRECTS. A seat its PROVIDER is refusing has no recent
    activity and holds no claim, so it matches STRANDED exactly — and the
    STRANDED text then tells the sender their recipient "may be idle-done or
    parked WITHOUT reporting" and to "re-check or reassign". Every word of that
    is wrong about a walled seat: it is not parked, it did not fail to report,
    and reassigning punishes it for its provider's 429. Measured 2026-08-04:
    codex, codex-2 and codex-3 sat RATE-LIMITED for five hours while every
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
    try:
        from . import seat as _seat
        row = _seat.seat_liveness(seat) or {}
        state = row.get("state")
        if state not in ("BLOCKED_ON_QUOTA", "WALLED"):
            return ""
        why = str(row.get("blocked_on") or "").strip()
        return " Provider: %s%s — the recipient is not parked, its PROVIDER " \
               "is refusing it; reassignment does not repair the wall." \
               % (state, " (%s)" % why if why else "")
    except Exception:
        return ""


def _context_pressure(seat):
    """"" or a short ' Context: 100.1% of 320k window.' — the cheap read that
    names what the codex-3 case ACTUALLY was: a seat out of window with its pane
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
    out = []
    for r in rows:
        rid = str(r.get("id") or "")
        recip = str(r.get("recipient") or "")
        recip_key = _recipient_key(recip)
        recip_display = str(r.get("recipient_display") or recip)
        # the sender to DM: the explicit sender seat-token, else the seat that
        # OWNS the dispatching session (source) — dispatches usually carry only
        # source (a session id, not addressable), so resolve it to a seat.
        sender = r.get("sender")
        if not sender and r.get("source"):
            try:
                sender = seats.derive_seat(r.get("source"))
            except Exception:
                sender = None
        sender = str(sender or "")
        # need an id8 to check the claim key, a recipient to check presence, and
        # a sender seat-token to DM (its beacon is the wake path)
        if len(rid) < 8 or not recip or not sender:
            continue
        if recip_key and recip_key == _recipient_key(sender):
            continue              # self-dispatch: no coordinator to wake but self
        if dispatches._age_s(r, read_now) < IDLE_DISPATCH_S:
            continue              # too fresh — give the recipient time to pick up
        if ("dispatch:" + rid[:8]) in claimed:
            continue              # claimed the dispatch ITSELF => being worked
        presence = seats.presence_of(seats.last_seen(recip))
        if presence == "fresh":
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
        f["wall"] = _provider_wall(recip) if state == CLAIM_NONE else ""
        f["live_pane"] = _live_pane(recip) if state == CLAIM_NONE else ""
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# the latch (one alert per stranded dispatch per episode) + the DM-to-sender
# ---------------------------------------------------------------------------

def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


_STRANDED_TEXT = (
    "@%(sender)s IDLE-DISPATCH: your dispatch %(id8)s (%(lane)s) to "
    "@%(recipient)s is STRANDED — the recipient is %(presence)s (no recent "
    "activity) AND holds no live claim on ANY resource (checked by holder "
    "across the whole claims ledger, not just dispatch:%(id8)s), ~%(age_min)dmin "
    "old%(od)s.%(wall)s%(live_pane)s%(tail)s"
    "[idle-dispatch watchdog]")

_HOLDING_TEXT = (
    "@%(sender)s IDLE-DISPATCH: your dispatch %(id8)s (%(lane)s) to "
    "@%(recipient)s is QUIET BUT HOLDING — NOT stranded. The recipient is "
    "%(presence)s (no recent activity) but holds %(held)s, ~%(age_min)dmin "
    "old%(od)s.%(context)s Quiet + holding is a busy or WEDGED owner (out of "
    "context window with the pane still alive looks exactly like this), not an "
    "abandoned row — their room may hold uncommitted work that reassignment "
    "would destroy. RESCUE, do not reassign: `helm seat autocompact --dry-run` "
    "and `helm seat where %(recipient)s` to tell wedged from busy, preserve the "
    "room first (`git -C <room> stash create`), then recover the seat. "
    "[idle-dispatch watchdog]")

_UNKNOWN_TEXT = (
    "@%(sender)s IDLE-DISPATCH: your dispatch %(id8)s (%(lane)s) to "
    "@%(recipient)s needs a LOOK — claim state UNKNOWN. The recipient is "
    "%(presence)s (no recent activity) and the claims ledger could not be read, "
    "~%(age_min)dmin old%(od)s. UNKNOWN IS NOT 'holds no claim': they may be "
    "holding a lease over uncommitted work. Do NOT reassign on an unread fact — "
    "read it (`helm chat claims`), then `helm seat where %(recipient)s`. "
    "[idle-dispatch watchdog]")

_TEXTS = {CLAIM_NONE: _STRANDED_TEXT, CLAIM_HOLDING: _HOLDING_TEXT,
          CLAIM_UNKNOWN: _UNKNOWN_TEXT}


def _held_phrase(held):
    if not held:
        return "a live claim"
    if len(held) == 1:
        return "a live claim on %s" % held[0]
    return "%d live claims (%s)" % (len(held), ", ".join(held[:3]))


def _alert_text(f):
    """The alert for ONE finding, chosen by its claim state. Three states, three
    recommendations, because reassigning a quiet HOLDER destroys their work and
    reassigning on an UNREAD ledger is the same act with less evidence. An
    unrecognised state falls back to the UNKNOWN text — the safe one — so no
    future state can inherit the reassign recommendation by accident."""
    od = " (past its deadline)" if f.get("overdue") else ""
    return _TEXTS.get(f.get("claim"), _UNKNOWN_TEXT) % {
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
    if f.get("wall") or f.get("live_pane"):
        return (" DO NOT reassign on this alert: the fact above explains the "
                "quiet, and the recommendation it names is the correct one. ")
    return (" It may be idle-done or parked WITHOUT reporting (the "
            "assume-busy gap). Re-check or reassign — never auto-poached. ")


def check(post=True, quiet=False):
    """One read-only pass: scan -> dedup-latch -> DM the sender. Returns
    {"findings": [...], "alerted": [...]}. The fcntl lock covers the
    scan/latch/write transaction so overlapping passes never double-alert. The
    latch is keyed per DISPATCH id8 (one alert per stranded dispatch per
    LATCH_TTL_S), so a busy fleet with many dispatches is one signal each, and a
    resolved-then-restranded dispatch re-alerts after the window."""
    from . import pk
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        findings = scan()
        st = pk.read_json(p, {}) or {}
        now = time.time()
        alerted = []
        seen_keys = set()
        for f in findings:
            key = f["id8"]
            seen_keys.add(key)
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
            n = int((entry or {}).get("n") or 0)
            wait = min(LATCH_TTL_S * (2 ** n), LATCH_BACKOFF_CAP_S)
            if entry and now - (entry.get("alerted_at") or 0) < wait:
                f["latched"] = True
                continue
            f["latched"] = False
            f["repeat"] = n
            st[key] = {"alerted_at": now, "recipient": f["recipient"],
                       "n": n + 1}
            alerted.append(f)
        # re-arm: drop latches for dispatches no longer stranded (resolved /
        # picked up), so a later re-strand of the SAME id alerts again
        for k in [k for k in st if k not in seen_keys]:
            st.pop(k, None)
        if post:                  # a --dry-run must be NON-MUTATING: it reports
            pk.write_json(p, st)  # what WOULD alert without consuming the latch

    if post and not quiet:
        for f in alerted:
            try:
                seats.dm(f["sender"], _alert_text(f), who="idle-dispatch")
            except Exception as e:   # a down chat node never blocks detection
                print("helm seat idle-dispatch: dm failed (%s -> %s): %s"
                      % (f["id8"], f["sender"], e), file=sys.stderr)
    return {"findings": findings, "alerted": alerted}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_idle_dispatch(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if "-h" in args or "--help" in args:
        print(_USAGE)
        return 0
    res = check(post="--dry-run" not in args, quiet="--quiet" in args)
    if "--json" in args:
        print(json.dumps(res))
    else:
        for f in res["findings"]:
            print("%s -> @%s: %s %s%s ~%dmin%s%s"
                  % (f["id8"], f["recipient"], f["presence"],
                     "STRANDED" if f["claim"] == CLAIM_NONE
                     else "HOLDING" if f["claim"] == CLAIM_HOLDING
                     else "CLAIM-UNKNOWN",
                     " [%s]" % f["held"][0] if f.get("held") else "",
                     f["age_min"], " OVERDUE" if f["overdue"] else "",
                     " (latched)" if f.get("latched") else " ALERTED @%s" % f["sender"]))
        if not res["findings"]:
            print("no idle dispatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_idle_dispatch())
