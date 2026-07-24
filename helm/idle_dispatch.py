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
    an OPEN dispatch row (status != verdict) whose RECIPIENT is quiet/absent
    (no recent tool boundary) AND holds NO live claim on dispatch:<id8> AND the
    dispatch is older than IDLE_DISPATCH_S (a soft "should have been picked up"
    window, earlier than the hard deadline so an idle-on-gate surfaces BEFORE it
    is overdue — ds4pro was idle on a gate, possibly not yet past its deadline).

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

LATCH_TTL_S = 15 * 60          # one alert per stranded dispatch per episode
IDLE_DISPATCH_S = 15 * 60      # soft "should-be-picked-up" window (== QUIET_S);
                               # earlier than the hard deadline so idle-on-gate
                               # surfaces BEFORE overdue (ds4pro was idle first)
_STATE = "idle_dispatch.json"

_USAGE = """usage: helm seat idle-dispatch [--once] [--dry-run] [--quiet] [--json]
  One read-only pass: cross every OPEN dispatch against its recipient's
  presence and flag the ones STRANDED on an idle seat (recipient quiet/absent,
  no live claim, older than the soft window). DMs the dispatch's SENDER one
  latched alert — a DM, not a broadcast, so it wakes a parked coordinator's
  beacon. Latched: one alert per stranded dispatch per episode. --dry-run
  reports without DMing; --quiet skips the DM; --once accepted for stability.
"""


# ---------------------------------------------------------------------------
# the detector — the open_rows x recipient-presence join
# ---------------------------------------------------------------------------

def scan():
    """Read-only: every OPEN dispatch stranded on an idle recipient. A dispatch
    is stranded when its recipient shows no recent tool boundary (presence
    quiet/absent), holds NO live claim on dispatch:<id8>, and it is older than
    the soft window (a fresh dispatch the seat has not reached yet is NOT a
    finding). Never writes, never reassigns. Fail-CLOSED on an unreadable claims
    file (a claim may exist -> never flag on uncertainty)."""
    try:
        rows = dispatches.open_rows()
    except Exception:
        return []
    claims = seats._live_claims()
    if claims is None:            # unreadable claims => 'unsure' => fail-closed
        return []
    claimed = {r for r in claims if r != "_fence"}
    out = []
    for r in rows:
        rid = str(r.get("id") or "")
        recip = str(r.get("recipient") or "")
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
        if recip.casefold() == sender.casefold():
            continue              # self-dispatch: no coordinator to wake but self
        if dispatches._age_s(r) < IDLE_DISPATCH_S:
            continue              # too fresh — give the recipient time to pick up
        if ("dispatch:" + rid[:8]) in claimed:
            continue              # actively claimed => being worked, not stranded
        presence = seats.presence_of(seats.last_seen(recip))
        if presence == "fresh":
            continue              # recipient crossed a tool boundary recently => busy
        out.append({
            "id": rid, "id8": rid[:8], "recipient": recip, "sender": sender,
            "lane": str(r.get("lane") or "review"),
            "presence": presence,
            "age_min": int(dispatches._age_s(r) // 60),
            "overdue": dispatches._is_overdue(r),
        })
    return out


# ---------------------------------------------------------------------------
# the latch (one alert per stranded dispatch per episode) + the DM-to-sender
# ---------------------------------------------------------------------------

def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def _alert_text(f):
    od = " (past its deadline)" if f.get("overdue") else ""
    return ("@%(sender)s IDLE-DISPATCH: your dispatch %(id8)s (%(lane)s) to "
            "@%(recipient)s is STRANDED — the recipient is %(presence)s (no "
            "recent activity), holds no claim, ~%(age_min)dmin old%(od)s. It "
            "may be idle-done or parked WITHOUT reporting (the assume-busy gap). "
            "Re-check or reassign — never auto-poached. [idle-dispatch watchdog]"
            % {"sender": f["sender"], "id8": f["id8"], "lane": f["lane"],
               "recipient": f["recipient"], "presence": f["presence"],
               "age_min": f["age_min"], "od": od})


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
            if entry and now - (entry.get("alerted_at") or 0) < LATCH_TTL_S:
                f["latched"] = True
                continue
            f["latched"] = False
            st[key] = {"alerted_at": now, "recipient": f["recipient"]}
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
                print("helm idle-dispatch: dm failed (%s -> %s): %s"
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
            print("%s -> @%s: %s ~%dmin%s%s"
                  % (f["id8"], f["recipient"], f["presence"], f["age_min"],
                     " OVERDUE" if f["overdue"] else "",
                     " (latched)" if f.get("latched") else " ALERTED @%s" % f["sender"]))
        if not res["findings"]:
            print("no stranded dispatches")
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_idle_dispatch())
