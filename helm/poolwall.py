#!/usr/bin/env python3
"""The per-seat proxy-pool wall: one refusal, one expiry, one room line.

THE SHAPE THIS CLOSES. A codex proxy seat whose pool has no available
credential gets, on EVERY wake, the proxy's own refusal in its pane:

    API Error: Request rejected (429) · no available credential for
    gpt-5.6-sol via provider codex: 6 cooling down (reset in 2h27m37s)

The inbox beacon, resume-turn and the boot-brief re-arm each re-poked the
seat, each poke burned a request, each printed one more copy of that line, and
the owner read a pane full of them. The family-wide credential wall that
already pauses delivery (proxywatch's dark latch) never fired, because the
FAMILY was healthy: other codex seats on other accounts were taking turns. The
wall is per SEAT — per proxy pool — and it carries its own expiry.

WHERE THE EXPIRY COMES FROM. The pane line is a printed line with no
timestamp, so "reset in 2h27m37s" read off the pane at some later instant
anchors to nothing. The seat's proxy.log carries the SAME refusal, timestamped
by the proxy at the moment it refused:

    [<date> <time>] [<request id>] [warn ] [gin_logger.go:<n>] 429 | ...
        | refusal_origin_v1=local | response_body="{... \\"message\\":\\"no
        available credential for gpt-5.6-sol via provider codex: 6 cooling
        down (reset in 4h33m7s)\\"}"

So the expiry is the log row's own timestamp plus its own reset duration, and
the clock at read time never enters the computation. Successive rows of one
wall agree on that instant to within the seconds between them, which is what
makes the expiry instant the wall's identity.

THE POOL'S LATEST STATEMENT DECIDES. The last agent-path row in the log is
what the pool said most recently. A pool refusal there that has not expired is
a wall; a completed request there is recovery, whatever an older refusal's
reset instant says — an account added to the pool ends the wall early, and
the successful request that follows is the proof.

FAIL TOWARD LOUD. A missing or unreadable log, a row whose reset clause does
not parse, or a seat with no proxy family never walls the seat: the wake goes
through, the proxy refuses again and prints one more line, which is visible
and recoverable, while a false wall is a seat nobody can wake.

ONE ROOM LINE PER WALL PER SEAT. The announcement ledger records the expiry
instant a seat was last announced for; a wall whose instant is within
``ANNOUNCE_TOLERANCE_S`` of the recorded one is the same wall and is not
announced again. A wall that starts after the recorded one expired has a new
instant, and earns one new line.
"""
import datetime
import fcntl
import os
import re
import time

from . import home, pk

STATE = "PROXY-COOLDOWN"          # proxywatch's own name for a local pool cooldown
LEDGER = "poolwall.json"
#: Two rows of one wall disagree on the reset instant by the seconds between
#: them; a new wall after expiry is hours away. Sixty seconds separates them.
ANNOUNCE_TOLERANCE_S = 60
ROOM = "helm"
WHO = "proxywatch"

# The producer prints the provider clause only when a provider was selected
# ("for claude-opus-5: 5 cooling down" is a measured spelling without one) and
# may append further clauses after the reset ("..., 1 auth-failed").
REFUSAL_RE = re.compile(
    r"no available credential for (?P<model>[^\s:]+)"
    r"(?: via provider (?P<provider>[^:\s]+))?: (?P<count>\d+) cooling down "
    r"\(reset in (?P<reset>[^)]*)\)", re.I)
_DURATION_RE = re.compile(
    r"^\s*(?:(?P<h>\d+)h)?\s*(?:(?P<m>\d+)m)?\s*(?:(?P<s>\d+)s)?\s*$")
_LOG_TS = "%Y-%m-%d %H:%M:%S"


def parse_reset(text):
    """Seconds for a producer reset clause (``2h27m37s``, ``27m37s``,
    ``37s``, ``2h``, ``127h35m21s``), or None for anything else. An empty
    clause is None: a duration nobody printed is not zero."""
    m = _DURATION_RE.match(str(text or ""))
    if not m or not any(m.group(k) for k in ("h", "m", "s")):
        return None
    return (int(m.group("h") or 0) * 3600 + int(m.group("m") or 0) * 60
            + int(m.group("s") or 0))


def parse_refusal(text):
    """{model, provider, count, reset_s, message} for a pool refusal, or None.

    None for a line that is not a pool refusal AND for a refusal whose reset
    clause this build cannot read: a wall with no expiry would be a wall with
    no end, which is the loud direction's opposite."""
    m = REFUSAL_RE.search(str(text or ""))
    if not m:
        return None
    reset_s = parse_reset(m.group("reset"))
    if reset_s is None:
        return None
    return {"model": m.group("model"), "provider": m.group("provider") or "",
            "count": int(m.group("count")), "reset_s": reset_s,
            "message": m.group(0)}


def _log_epoch(stamp):
    """The proxy log's naive local timestamp as an epoch, or None."""
    try:
        return time.mktime(time.strptime(stamp, _LOG_TS))
    except (TypeError, ValueError, OverflowError):
        return None


def log_statements(path, tail_bytes=None):
    """([row], error) — every agent-path request in the log tail, file order.

    A row is ``{"observed_at": epoch, "code": int, "refusal": dict | None}``.
    ``refusal`` is the parsed pool refusal with ``expires_at`` (observed_at +
    reset_s) for a 429 carrying one, and None for every other request — a
    completed turn, a different refusal, a 429 whose reset this build cannot
    read. Canary rows count on both sides: the reset clock belongs to the
    pool, not to whoever asked, and a canary that completed is recovery."""
    from . import proxywatch
    rows, err, _scope = proxywatch._proxy_log_rows(
        path, proxywatch._TAIL_BYTES if tail_bytes is None else tail_bytes)
    if err:
        return [], err
    out = []
    for stamp, code, request_path, body, _origin, _delivery in rows:
        if not request_path.startswith(proxywatch._AGENT_PATH):
            continue
        at = _log_epoch(stamp)
        if at is None:
            continue
        refusal = parse_refusal(body) if code == 429 and body else None
        if refusal is not None:
            refusal["observed_at"] = at
            refusal["expires_at"] = at + refusal["reset_s"]
        out.append({"observed_at": at, "code": code, "refusal": refusal})
    return out, None


def log_path(family, seat):
    from . import seat as seatmod
    return os.path.join(seatmod._proxy_home(family, seat), "proxy.log")


def _family(seat, family=None):
    if family:
        return family
    from . import seat as seatmod
    fam, err = seatmod.family_for(str(seat or ""), None, False)
    return None if err else fam


def seat_wall(seat, family=None, now=None):
    """(wall, error) — the pool wall in force for one seat, or (None, why).

    The LAST agent-path row in the log is the pool's latest statement. A pool
    refusal there whose expiry is ahead of ``now`` is the wall; anything else
    — a completed request, an older refusal that expired, no traffic at all —
    answers None with the reason. Never raises: every failure is a reason, and
    a reason never walls the seat."""
    try:
        fam = _family(seat, family)
        if not fam:
            return None, "no proxy family"
        rows, err = log_statements(log_path(fam, seat))
        if err:
            return None, err
        if not rows:
            return None, "no agent request in the log tail"
        last = rows[-1]
        if last["refusal"] is None:
            return None, "the pool's latest statement is HTTP %d, not a pool refusal" % last["code"]
        wall = dict(last["refusal"])
        now = time.time() if now is None else now
        if wall["expires_at"] <= now:
            return None, "the last pool refusal expired at %s" % iso(
                wall["expires_at"])
        wall["seat"], wall["family"] = seat, fam
        return wall, None
    except Exception as exc:              # noqa: BLE001 — a wall never raises
        return None, "pool wall unreadable: %s" % exc.__class__.__name__


def pane_anchor(family, seat):
    """A ``line -> datetime | None`` for the pane classifier.

    The pane's refusal line carries a duration and no timestamp; the log
    carries the same message WITH one. The anchor is the latest log row whose
    message equals the pane line's message, as a naive local datetime (the
    classifier's own clock is naive local). The log is read once, lazily, and
    only when a refusal line is actually being judged."""
    cache = {}

    def anchor(line):
        parsed = parse_refusal(line)
        if parsed is None:
            return None
        if "rows" not in cache:
            try:
                cache["rows"] = [r["refusal"] for r in
                                 log_statements(log_path(family, seat))[0]
                                 if r["refusal"] is not None]
            except Exception:             # noqa: BLE001 — unreadable is no anchor
                cache["rows"] = []
        for row in reversed(cache["rows"]):
            if row["message"] == parsed["message"]:
                return datetime.datetime.fromtimestamp(row["observed_at"])
        return None
    return anchor


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def pause(wall, now=None):
    """The delivery-pause record for a wall in force — the same shape every
    consumer of ``proxywatch.delivery_pause`` already renders."""
    now = time.time() if now is None else now
    return {"family": wall["family"], "state": STATE,
            "since": iso(wall["observed_at"]), "until": iso(wall["expires_at"]),
            "age_s": max(0, now - wall["observed_at"]), "stale": False,
            "model": wall["model"], "count": wall["count"],
            "seat": wall["seat"],
            "reason": hold_reason(wall)}


def _pool_phrase(wall):
    provider = (" via provider %s" % wall["provider"]) if wall.get("provider") \
        else ""
    return "no available credential for %s%s, %d cooling down" % (
        wall["model"], provider, wall["count"])


def hold_reason(wall):
    """The one sentence every held wake path gives."""
    return ("%s is walled by its proxy pool: %s, reset at %s — the keystroke "
            "is withheld until then; rows addressed to it stay owed" % (
                wall["seat"], _pool_phrase(wall), iso(wall["expires_at"])))


def blocked_on(wall):
    """The liveness row's short WHAT-it-waits-on for a pool wall."""
    return "proxy pool: %s; reset at %s" % (
        _pool_phrase(wall), iso(wall["expires_at"]))


def room_line(wall):
    return ("poolwall: seat %s is WALLED by its proxy pool — %s; reset at %s. "
            "Every wake into its pane is held until then and rows addressed "
            "to it stay owed; one line per wall." % (
                wall["seat"], _pool_phrase(wall), iso(wall["expires_at"])))


def _ledger_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", LEDGER)


def _read_ledger():
    data = pk.read_json(_ledger_path(), default={})
    return data if isinstance(data, dict) else {}


def announced(seat, wall, ledger=None):
    """Has THIS wall (by expiry instant) already earned its line?"""
    ledger = _read_ledger() if ledger is None else ledger
    row = ledger.get(seat)
    if not isinstance(row, dict):
        return False
    at = row.get("expires_at")
    return isinstance(at, (int, float)) and \
        abs(at - wall["expires_at"]) <= ANNOUNCE_TOLERANCE_S


def _claim(seat, wall, now):
    """Record the announcement under a file lock -> True when THIS caller
    claimed it, False when another already had. Two observers of one wall
    reach one post because the claim is the lock's critical section."""
    path = _ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ledger = _read_ledger()
            if announced(seat, wall, ledger):
                return False
            ledger[seat] = {"expires_at": wall["expires_at"],
                            "announced_at": now, "model": wall["model"],
                            "count": wall["count"]}
            pk.write_json(path, ledger)
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _unclaim(seat):
    path = _ledger_path()
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ledger = _read_ledger()
            if ledger.pop(seat, None) is not None:
                pk.write_json(path, ledger)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def announce(seat, wall=None, now=None, post=None):
    """Post the wall's one room line if it has not been posted -> body or None.

    ``wall`` defaults to the seat's wall in force; no wall, nothing to say.
    A failed post releases the claim so the next observer retries; a wall is
    never announced twice and never silently unannounced. Never raises."""
    try:
        now = time.time() if now is None else now
        if wall is None:
            wall, _why = seat_wall(seat, now=now)
        if wall is None or not _claim(seat, wall, now):
            return None
        body = room_line(wall)
        try:
            if post is None:
                from . import chat
                chat.post(body, who=WHO, room=ROOM)
            else:
                post(body)
        except Exception:                 # noqa: BLE001 — retry on the next observer
            _unclaim(seat)
            return None
        return body
    except Exception:                     # noqa: BLE001 — an announcement never raises
        return None


def announcements(seats, now=None):
    """[(seat, body)] — one line per seat whose wall is in force and unposted,
    claimed here for a caller that delivers through its own durable outbox
    (proxywatch's pass: the claim is taken once, the outbox retries)."""
    out = []
    now = time.time() if now is None else now
    for seat in seats:
        try:
            wall, _why = seat_wall(seat, now=now)
            if wall is not None and _claim(seat, wall, now):
                out.append((seat, room_line(wall)))
        except Exception:                 # noqa: BLE001 — one seat never blinds the pass
            continue
    return out


def rearm_hold(seat, session=None):
    """The reason a boot-brief re-arm keystroke is HELD for this seat, or "".

    Reads the SAME pause every other delivery obeys (``delivery_pause``), so
    the re-arm cannot become the one wake that walks around the wall."""
    try:
        from .seats_identity import _delivery_pause
        held = _delivery_pause(seat, session)
    except Exception as exc:              # noqa: BLE001 — unreadable holds
        return ("the delivery pause for %s could not be read (%s), and a "
                "keystroke needs positive authority" % (
                    seat, exc.__class__.__name__))
    if not held:
        return ""
    return held.get("reason") or (
        "delivery to %s is paused (state %s)" % (
            seat, held.get("state") or "UNKNOWN"))


__all__ = ["parse_reset", "parse_refusal", "log_statements", "seat_wall",
           "pane_anchor", "pause", "hold_reason", "blocked_on", "room_line",
           "announce", "announced", "announcements", "rearm_hold", "iso",
           "STATE"]
