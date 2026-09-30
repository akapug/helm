#!/usr/bin/env python3
"""helm claude pace — each Claude account's FIVE-HOUR window: its pace, where
that pace lands it at the reset, and one gentle state (pace5h).

THE FAILURE THIS PREVENTS. One account's 5h window read 4% at 15:52Z, 33% at
17:22Z, 72% at 18:52Z and 93% at 19:37Z, and reset at 19:50Z at 96%. The level
was on every surface and the rate on none, so builder subagents kept landing on
that account while cloud credit and local seats had room. Nothing in helm
watched the 5h pace; the codex pace (`codexpace`) reads codex's LONGEST window
and nothing else.

THE READING, per account, from the native usage history the creds probe cycle
already writes (`brief.usage_history_path`). This module never probes, so it
reads the vendor no more often than the existing readers do:

  WINDOW      the newest reading's 5h reset, minus five hours. Only readings
              inside it that carry that reset count.
  PACE        the least-squares slope of those readings in percent per hour,
              by the codex pace's own arithmetic (`codexpace._slope`).
  PROJECTION  the newest reading plus the pace up to the reset; the hit time
              is where that line crosses 100%.

  TOO FEW READINGS IS UNKNOWN, NEVER A GUESS. A pace needs MIN_POINTS readings
  over at least MIN_SPAN_S inside the current window, and WATCH needs
  WATCH_SPAN_S of them: early in a window the pace is carried up to ~4.6
  hours to the reset, so a twenty-minute slope's noise is magnified past the
  hysteresis band. Until then a projection over the wall reads UNKNOWN and
  says why.

  A THROTTLED PROBE IS NO NEW READING. The usage endpoint answers 429 when it
  is polled too often, and the probe then writes no history row, or a row with
  no gauges. Neither is a reading here, and neither is zero: the state stays
  what the readings already said.

THE STATE, with hysteresis so it does not flap:

  OK       the pace lands the window under WATCH_PCT at the reset.
  WATCH    the projection reaches WATCH_PCT before the reset, on at least
           WATCH_SPAN_S of readings.
  TIGHT    the newest reading is at or over TIGHT_PCT with more than
           TIGHT_LEFT_S to the reset (no pace needed).
  UNKNOWN  no pace, or no reading of the open window.

  WATCH and TIGHT clear only when the projection falls under CLEAR_PCT or the
  window resets. `since` is the instant the account entered its state, so a
  state change is a new `since` and nothing else is.

  THE STEER CANNOT FLAP even where a state does. A seat hears a state keyed
  on its account, the state and the window's reset, never on `since`: each
  state is said at most once per window, and a step back down (TIGHT to
  WATCH) is not said at all.

WHAT IT DOES, and only this: one line per account on `helm creds` and `helm
burn` and a field on the quota page's account rows; a steer said at most once
per state per window to a seat running on a WATCH or TIGHT account (`steer`,
through `helm inject`). A seat on the BASELOAD home hears it only while no
other Claude account reads OK: the watcher moves its credential when its
window caps, so while another account has room it has nothing to act on, and
when none is known to have room, the steer asks it to route new work to other
families and keep its own going. An account with no reading (UNKNOWN) is not
known to have room, so it is never counted as room. A seat HOMED to one
credential (a named credhome) is not moved, so it always hears it and is asked
to rank its work and pace it to the reset. Neither is told to stop or wait for
a reset. And a tie-break where helm ranks Claude accounts for new work
(`providers.allocate`, through `pressed_accounts`): of two equal choices the
account that is not WATCH or TIGHT ranks first, which rarely decides anything,
since a Claude account's headroom is its 5h headroom and exact ties are
uncommon. `helm route` is not wired: its policy edge E9 never offers the
native family build work, so no seat pick there chooses between Claude
accounts for it. It never stops, pauses or reroutes a turn in flight, and
never refuses a dispatch.

THE SNAPSHOT is written by the posting watchdog pass (`watch_pass`), the one
reader of the history, and it carries the hysteresis. Every surface reads the
snapshot under the burn flags' freshness bound. Accounts are keyed by
`accounts.measured_key` and labelled by `accounts.mask_identity`, so no
address reaches the file.

ONLY CLAUDE. kimi and opencode-go have no usage reader at all: helm learns of
their limits from a refusal body after the wall, with no percent and no reset
before it, so there is nothing to pace. Codex's 5h window lifts inside the
session and its per-seat pool wall (`poolwall`) already speaks for it.
"""
import os
import time

from . import home, pk

FAMILY = "anthropic"
LABEL = "5h"
WINDOW_S = 5 * 3600
HOUR = 3600.0
MIN_POINTS = 3
MIN_SPAN_S = 20 * 60
#: The span of readings a WATCH is entered on: an hour, not MIN_SPAN_S.
WATCH_SPAN_S = 60 * 60
WATCH_PCT = 100.0
CLEAR_PCT = 90.0
TIGHT_PCT = 85.0
TIGHT_LEFT_S = 30 * 60
#: Two readings of one window agree on its reset instant (codexpace's bound).
RESET_TOLERANCE_S = 300
OK, WATCH, TIGHT, UNKNOWN = "OK", "WATCH", "TIGHT", "UNKNOWN"
PRESSED = (WATCH, TIGHT)
SNAPSHOT_NAME = "claude-pace5h.json"
V = 1
#: The inject seen-state key and the ledger id of the steer.
SEEN_KEY = "pace5h"
STEER_ID = "steer:pace5h"
#: The seat register's word for a native Claude seat.
NATIVE_SEAT = "claude"
STEER_ASK = ("no other Claude account is known to have room, so route NEW "
             "builds and reviews to codex, local and other seats; keep your "
             "own work going")
#: A seat homed to one credential is not moved by the watcher.
STEER_HOMED = ("this seat is homed to this account, so the watcher will not "
               "move it: rank your work and pace it to the reset, and route "
               "new builds to other seats")


def snapshot_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", SNAPSHOT_NAME)


def _num(value):
    from . import codexpace
    return codexpace._num(value)


def _hm(at):
    return time.strftime("%H:%MZ", time.gmtime(at)) if at else "?"


# ------------------------------------------------------------------ reading

def _readings(rows):
    """History rows -> {account: [(ts, used_pct, reset_at)]}, oldest first.
    A row counts only when the probe read it (`burnflags.READ_STATUSES`) and
    its account-wide 5h gauge carries a percent; a row whose gauges are not
    a list is no reading."""
    from . import burnflags
    out = {}
    for row in rows or ():
        if not isinstance(row, dict) or not row.get("account") \
                or (row.get("provider") or FAMILY) != FAMILY:
            continue
        if not str(row.get("status") or "").startswith(burnflags.READ_STATUSES):
            continue
        gauges = row.get("gauges") or ()
        if not isinstance(gauges, (list, tuple)):
            continue
        at = pk.parse_ts_epoch(row.get("probed_at"))
        gauge = next((g for g in gauges
                      if isinstance(g, dict) and g.get("label") == LABEL), None)
        used = burnflags._utilization_pct(gauge) if gauge else None
        if not at or used is None:
            continue
        out.setdefault(row["account"], []).append(
            (float(at), used, _num(gauge.get("reset"))))
    for points in out.values():
        points.sort(key=lambda p: p[0])
    return out


def _same_window(prior, reset):
    at = _num((prior or {}).get("reset_at"))
    return at is not None and reset is not None \
        and abs(at - reset) <= RESET_TOLERANCE_S


def account_reading(points, now, prior=None):
    """One account's readings (oldest first) + its prior record -> its record.
    PURE."""
    from . import codexpace
    rec = {"state": UNKNOWN, "why": None, "used_pct": None,
           "pct_per_hour": None, "projected_pct": None, "hit_at": None,
           "reset_at": None, "measured_at": None, "points": 0, "span_s": 0}
    if not points:
        rec["why"] = "no reading of this account's 5h window"
        return _latched(rec, prior, now)
    at, used, reset = points[-1]
    rec.update(used_pct=used, reset_at=reset, measured_at=at)
    if reset is None:
        rec["why"] = "the newest 5h reading carries no reset instant"
        return _latched(rec, prior, now)
    if reset <= now:
        rec["why"] = ("the window reset at %s and no reading of the next one "
                      "is in yet" % _hm(reset))
        return _latched(rec, prior, now)
    window = [p for p in points if p[0] >= reset - WINDOW_S
              and p[2] is not None and abs(p[2] - reset) <= RESET_TOLERANCE_S]
    span = window[-1][0] - window[0][0] if window else 0.0
    rec.update(points=len(window), span_s=int(span))
    pace = codexpace._slope(window) \
        if len(window) >= MIN_POINTS and span >= MIN_SPAN_S else None
    projected = None
    if pace is not None:
        projected = used + pace * max(0.0, reset - at) / HOUR
        rec.update(pct_per_hour=round(pace, 1),
                   projected_pct=round(projected, 1))
        hit = at if used >= 100.0 else (
            at + (100.0 - used) / pace * HOUR if pace > 0 else None)
        rec["hit_at"] = hit if hit is not None and hit <= reset else None
    tight = used >= TIGHT_PCT and reset - now > TIGHT_LEFT_S
    held = (prior or {}).get("state") if _same_window(prior, reset) \
        and (prior or {}).get("state") in PRESSED else None
    if tight:
        rec["state"] = TIGHT
    elif held and (projected is None or projected >= CLEAR_PCT):
        rec["state"] = held
    elif projected is not None and projected >= WATCH_PCT \
            and span >= WATCH_SPAN_S:
        rec["state"] = WATCH
    elif projected is not None and projected >= WATCH_PCT:
        rec["why"] = ("projects %.0f%% at the reset from %dm of readings; a "
                      "WATCH waits for an hour of them" % (projected,
                                                            span // 60))
    elif projected is None:
        rec["why"] = ("%d reading(s) over %dm in this window; a pace needs %d "
                      "over at least %dm" % (len(window), span // 60,
                                             MIN_POINTS, MIN_SPAN_S // 60))
    else:
        rec["state"] = OK
    return _latched(rec, prior, now)


def _latched(rec, prior, now):
    """Stamp `since`: carried while the state and its window hold."""
    prior = prior or {}
    same = prior.get("state") == rec["state"] and (
        rec["state"] not in PRESSED or _same_window(prior, rec["reset_at"]))
    rec["since"] = prior.get("since") if same and prior.get("since") else now
    return rec


def read(rows, now, prior=None):
    """History rows + the prior snapshot -> the reading. PURE."""
    from . import accounts
    before = (prior or {}).get("accounts") if isinstance(prior, dict) else None
    before = before if isinstance(before, dict) else {}
    out = {}
    for name, points in sorted(_readings(rows).items()):
        key = accounts.measured_key(name)
        rec = account_reading(points, now, before.get(key))
        rec["label"] = accounts.mask_identity(name)
        out[key] = rec
    return {"v": V, "ts": now, "accounts": out}


# ------------------------------------------------------------------ the pass

def _recent_rows(now):
    """The native history's rows from the last window and an hour: every
    reading an open window can hold, and the newest one of a closed one."""
    from . import burnflags
    floor = now - WINDOW_S - HOUR
    return [row for row in burnflags._history_lines()
            if (pk.parse_ts_epoch(row.get("probed_at")) or 0) >= floor]


def write_snapshot(reading, path=None):
    """Persist the reading -> True/False. Best effort, never raises."""
    import json
    try:
        pk.atomic_write(path or snapshot_path(),
                        json.dumps(reading, sort_keys=True))
        return True
    except Exception:                           # noqa: BLE001
        return False


def watch_pass(now=None, rows=None, path=None):
    """The watchdog pass's rung: read the history, fold it against the prior
    snapshot's states, persist -> the reading."""
    now = time.time() if now is None else now
    rows = _recent_rows(now) if rows is None else rows
    prior = pk.read_json(path or snapshot_path(), default=None)
    reading = read(rows, now, prior)
    write_snapshot(reading, path=path)
    return reading


def _load(path=None):
    payload = pk.read_json(path or snapshot_path(), default=None)
    if not isinstance(payload, dict) or payload.get("v") != V \
            or _num(payload.get("ts")) is None \
            or not isinstance(payload.get("accounts"), dict):
        return None
    return payload


def _fresh(payload, now=None, max_age=None):
    from . import burnflags
    now = time.time() if now is None else now
    bound = burnflags.max_age_s() if max_age is None else max_age
    return payload is not None and 0 <= now - payload["ts"] <= bound


def cached(now=None, max_age=None, path=None):
    """The snapshot, or None when it is absent, unreadable or older than the
    burn flags' own freshness bound. NEVER reads the history."""
    payload = _load(path)
    return payload if _fresh(payload, now, max_age) else None


# ------------------------------------------------------------------ surfaces

def describe(rec):
    """One account's record in one line: state, percent, pace, projected hit
    time and reset time."""
    state = rec.get("state") or UNKNOWN
    if state == UNKNOWN:
        return "5h UNKNOWN: %s" % (rec.get("why") or "no reading")
    pace = rec.get("pct_per_hour")
    text = "5h %s: %.0f%% at %s" % (state, rec.get("used_pct") or 0.0,
                                     "?%/h" if pace is None
                                     else "%.1f%%/h" % pace)
    if rec.get("hit_at"):
        text += ", hits 100%% ~%s" % _hm(rec["hit_at"])
    elif rec.get("projected_pct") is not None:
        text += ", projects %.0f%% at the reset" % rec["projected_pct"]
    return text + ", resets %s" % _hm(rec.get("reset_at"))


def _key(name):
    """An account name's snapshot key. A second home on one account is named
    "email#dir" (`providers.NativeQuotaProvider.accounts`), and the history
    keeps one name per identity, the part before the "#"."""
    from . import accounts
    return accounts.measured_key(str(name).partition("#")[0])


def account_record(name, snap):
    """One account name's record with its line, or None: the quota page's
    `pace_5h` field."""
    rec = ((snap or {}).get("accounts") or {}).get(_key(name))
    return dict(rec, line=describe(rec)) if isinstance(rec, dict) else None


def account_line(name, snap):
    """`helm creds`'s line for one account name, or None."""
    rec = account_record(name, snap)
    return rec["line"] if rec else None


def burn_lines(now=None):
    """`helm burn`'s lines about the Claude 5h pace. Never raises."""
    try:
        snap = cached(now=now)
        if snap is None:
            return ["  claude 5h pace: not measured — the posting watchdog "
                    "pass writes it (`helm proxywatch --post`)"]
        rows = sorted(snap["accounts"].values(),
                      key=lambda r: str(r.get("label")))
        if not rows:
            return ["  claude 5h pace: no Claude account in the usage history"]
        return ["  claude 5h pace %s: %s" % (r.get("label"), describe(r))
                for r in rows]
    except Exception as exc:                    # noqa: BLE001
        return ["  claude 5h pace: unreadable (%s)" % type(exc).__name__]


def steer_line(rec, homed=False):
    pace = rec.get("pct_per_hour")
    return ("REFLEX: this account's 5h window is %s at %.0f%%%s%s, resets %s: "
            "%s." % (rec.get("state"), rec.get("used_pct") or 0.0,
                     "" if pace is None else ", %.0f%%/h" % pace,
                     ", hits 100%% ~%s" % _hm(rec["hit_at"])
                     if rec.get("hit_at") else "",
                     _hm(rec.get("reset_at")),
                     STEER_HOMED if homed else STEER_ASK))


def _pressed(snap):
    return {k: r for k, r in ((snap or {}).get("accounts") or {}).items()
            if isinstance(r, dict) and r.get("state") in PRESSED}


def _account_key(config_home):
    from . import accounts, cred
    email = cred.account_of(config_home).get("email") if config_home else None
    return accounts.measured_key(email) if email else None


def _proxy_seat():
    """True when this process is a proxy seat: its Claude harness bills
    another family, whatever home it runs in."""
    name = home.chat_name()
    if not name:
        return False
    from . import seat
    family, _err = seat._seat_family(str(name))
    return bool(family) and family != NATIVE_SEAT


def steer(context, said=None, now=None, snap=None):
    """(line, key) for a Claude seat whose own account is WATCH or TIGHT and
    has not heard this state of this window in this context (`said` is the
    key it last heard), else None. THE HOT PATH: one small read per turn;
    the freshness bound and the account are asked only once some account is
    pressed, and the seat family only once that account is this seat's.
    Words only: nothing here touches the turn it rides."""
    context = context or {}
    if context.get("harness") != "claude" or not context.get("config_home"):
        return None
    payload = _load() if snap is None else snap
    pressed = _pressed(payload)
    if not pressed:
        return None
    # ALREADY HEARD: a context's home never changes, so the key it heard
    # names its account, and a state it heard is not re-read from the home.
    key = str(said or "").partition("|")[0]
    if key in pressed and not _unheard(said, key, pressed[key]):
        return None
    if snap is None and not _fresh(payload, now):
        return None
    key = _account_key(context["config_home"])
    rec = pressed.get(key)
    if rec is None or not _unheard(said, key, rec):
        return None
    from . import cred
    homed = cred.is_credhome(context["config_home"])
    if not homed and _room_elsewhere(payload, key) or _proxy_seat():
        return None
    return steer_line(rec, homed), _heard(key, rec)


def _room_elsewhere(snap, key):
    """True when some other Claude account reads OK. The watcher moves a
    BASELOAD seat's credential when its window caps, so such a seat on a
    pressed account has nothing to do about it while another account has
    room, and hears nothing. A seat homed to one credential (a named
    credhome) is not moved, so this is never asked for it. UNKNOWN IS NOT
    ROOM: an account with no reading of its window, or a record that names
    no state, may be as full as this one, so only OK counts."""
    return any(isinstance(r, dict) and r.get("state") == OK
               for k, r in ((snap or {}).get("accounts") or {}).items()
               if k != key)


def _heard(key, rec):
    """The key a seat keeps once it heard `rec`: account|state|window reset.
    Never `since`: a state that clears and comes back in one window is the
    same state, said once."""
    reset = _num(rec.get("reset_at"))
    return "%s|%s|%s" % (key, rec["state"],
                         "?" if reset is None else "%d" % reset)


def _unheard(said, key, rec):
    """True when `rec` is news to a seat whose last heard key is `said`: a
    new account or window, or a step up from WATCH to TIGHT. A state already
    heard in this window, or a step back down, is not."""
    parts = str(said or "").split("|")
    if len(parts) != 3 or parts[0] != key:
        return True
    try:
        reset = float(parts[2])
    except ValueError:
        return True
    if not _same_window({"reset_at": reset}, _num(rec.get("reset_at"))):
        return True
    return parts[1] not in PRESSED \
        or PRESSED.index(rec["state"]) > PRESSED.index(parts[1])


def pressed_accounts(names, now=None, snap=None):
    """{account name: WATCH or TIGHT} for the named accounts that are, from a
    fresh snapshot; {} otherwise. `providers.allocate` reads it as a
    tie-break between equal choices, never as an exclusion."""
    pressed = _pressed(cached(now=now) if snap is None else snap)
    if not pressed:
        return {}
    out = {}
    for name in names or ():
        rec = pressed.get(_key(name))
        if rec:
            out[name] = rec["state"]
    return out
