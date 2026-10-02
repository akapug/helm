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

THE SWITCH POOL (task/3871). The default-home watcher (store heuristic
fleet-autoswitch-at-the-wall) switches the baseload home between the Max
accounts only, so the POOL is the accounts whose newest history row names a
Max plan (`tier`, written by the creds probe; a row that names none takes
the plan from the providers' account census, a read of home and Orca
metadata files with no vendor call). A Team account homed to a
project is not in it, however much room it has. SWITCHABLE mirrors the
watcher's candidate rule: a pool account whose weekly window is under
SWITCH_WEEKLY_PCT, and whose 5h window is either IDLE or OK under
SWITCH_SESSION_PCT.

  IDLE IS MEASURED, NOT ASSUMED. An account nobody has used since its last
  window reads 0% with NO reset instant (the vendor opens a window at the
  first use; measured on every account in the history). That is a read row
  saying the window is not open, so it counts as room, for at most
  IDLE_MAX_AGE_S (the watcher's own trust bound for a remembered reading),
  and past IDLE_FRESH_S only under IDLE_STALE_WEEKLY_PCT of its week (the
  watcher's bound for a stale reading). ANY NEWER ROW for the account that
  did not read it idle (a 429, a lapsed copy's refresh-due row) ends it:
  the account may be in use. A window that is OPEN but has too few readings
  for a pace, a probe that did not read, or a gauge with no percent is an
  account in use that nothing can read: that is UNKNOWN, and never room.
  THE BLIND SPOT, stated: a probe cycle that writes NO row at all leaves
  the last idle reading standing, so an account put to use unseen reads
  idle until its next row, within the two bounds above.

THE POOL READING, from the same rows: each account's newest weekly reading
of the week still open, and what it spent over the last BURN_S. The pool's
BURN is those spends summed, per hour (percent of one account's week per
hour; a Max 20x and a Max 5x week are added as equals, a stated bound). Its
LEFT is the weekly headroom summed. The HORIZON is what the pool must last
until: the owner's (`helm burn horizon anthropic <iso>`, read at most
HORIZON_MAX_S out, so no account resets twice before it), else the soonest
weekly reset that returns at least CARRY_PCT of a week. An account whose
week resets before the horizon adds a whole week, beside the headroom it
has until then. The ratio of the
burn to the burn that lasts exactly to the horizon is the PACE: EASE (runs
out before it: "ease off about N%", ORANGE, RED when the run-out is inside
one 5h window), EVEN ("on pace", YELLOW) or FASTER ("can go faster",
GREEN), each held with hysteresis. It is advice, and it moves no burn flag.

WHAT IT DOES, and only this: one line per account on `helm creds` and `helm
burn` and a field on the quota page's account rows; the pool line and one
line per project with an authored light on `helm burn` (the light's own
sentence, `registry.LIGHT_SAYS`, and its authored reason: one meaning per
light); a steer said at most
once per state per window to a seat running on a WATCH or TIGHT account
(`steer`, through `helm inject`). A seat on the BASELOAD home whose pool has
a SWITCHABLE account hears that the watcher will switch it, with the pool's
pace and its project's advice, and is never told to route away. Only when no
account is known to be switchable does the steer ask it to route new work to
other families and keep its own going. A seat HOMED to one credential (a
named credhome) is not moved, so it always hears it and is asked to rank its
work and pace it to the reset. Neither is told to stop or wait for a reset.
When the default home's account changes (the watcher's switch, or a hand
switch in Orca), every native Claude seat that takes a turn inside
HANDOFF_SAY_S hears ONE line for that switch (`handoff`): the pool's burn,
what is left, the run-out against the horizon, one adjustment, and its
project's advice. A seat homed to an account outside the pool does not.
And a tie-break where helm ranks Claude accounts for new work
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
#: A baseload seat whose pool has a switchable account: never route away.
STEER_SWITCH = "the watcher will switch this home to one with room"

#: THE WATCHER'S CANDIDATE RULE (fleet-autoswitch-at-the-wall v2): a Max
#: account under 90% weekly and under 70% of its 5h session.
POOL_TIER = "Max"
SWITCH_WEEKLY_PCT = 90.0
SWITCH_SESSION_PCT = 70.0
#: How old a reading of an unused account may be and still count (the
#: watcher's READING_MAX_AGE_S): an idle copy's token lapses in hours. Past
#: IDLE_FRESH_S (a lapsed idle copy's age) the watcher's stale bound holds.
IDLE_MAX_AGE_S = 24 * 3600
IDLE_FRESH_S = 8 * 3600
IDLE_STALE_WEEKLY_PCT = 80.0
WEEK_LABEL = "7d"
#: The pool's burn is read over the rows the pass already holds.
BURN_S = WINDOW_S + HOUR
MIN_BURN_SPAN_S = HOUR
#: THE PACE, as the pool's burn over the burn that lasts to the horizon.
#: Each band is entered past its mark and left only inside the other one.
EASE, EVEN, FASTER = "EASE", "EVEN", "FASTER"
EASE_AT, EASE_CLEAR = 1.10, 1.00
FASTER_AT, FASTER_CLEAR = 0.70, 0.80
#: The owner's horizon: AUTHORED, one record per family.
HORIZON_NAME = "pace-horizon.json"
HORIZON_MAX_S = 7 * 86400
#: The handoff line: its seen-state key, ledger id, and how long after a
#: switch a seat taking a turn still hears it.
HANDOFF_KEY = "pacecoach"
HANDOFF_ID = "steer:pacecoach"
HANDOFF_SAY_S = 2 * 3600
#: The words a baseload seat hears while its pool has a switchable account.


def snapshot_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", SNAPSHOT_NAME)


def _num(value):
    from . import codexpace
    return codexpace._num(value)


def _hm(at):
    return time.strftime("%H:%MZ", time.gmtime(at)) if at else "?"


def _day(at):
    return time.strftime("%a %H:%MZ", time.gmtime(at)) if at else "?"


# ------------------------------------------------------------------ reading

def _readings(rows, label=LABEL):
    """History rows -> {account: [(ts, used_pct, reset_at)]}, oldest first,
    for one account-wide gauge (the 5h one unless `label` names another).
    A row counts only when the probe read it (`burnflags.READ_STATUSES`) and
    that gauge carries a percent; a row whose gauges are not a list is no
    reading."""
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
                      if isinstance(g, dict) and g.get("label") == label), None)
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
           "reset_at": None, "measured_at": None, "points": 0, "span_s": 0,
           "idle": False}
    if not points:
        rec["why"] = "no reading of this account's 5h window"
        return _latched(rec, prior, now)
    at, used, reset = points[-1]
    rec.update(used_pct=used, reset_at=reset, measured_at=at)
    if reset is None and used == 0.0:
        # IDLE: the vendor opens a window at the first use, so 0% with no
        # reset is a read row saying no window is open.
        rec["idle"] = now - at <= IDLE_MAX_AGE_S
        rec["why"] = "no 5h window is open (unused since %s)" % _hm(at)
        return _latched(rec, prior, now)
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


def _tiers(rows):
    """History rows -> {account: the plan its newest row names}."""
    out = {}
    for row in rows or ():
        if isinstance(row, dict) and row.get("account") \
                and isinstance(row.get("tier"), str):
            out[row["account"]] = row["tier"]
    return out


def _last_rows(rows):
    """{account: the instant of its newest row, whatever its status}."""
    out = {}
    for row in rows or ():
        if isinstance(row, dict) and row.get("account"):
            at = pk.parse_ts_epoch(row.get("probed_at")) or 0
            out[row["account"]] = max(at, out.get(row["account"], 0))
    return out


def _same_reset(a, b):
    return a is None and b is None or a is not None and b is not None \
        and abs(a - b) <= RESET_TOLERANCE_S


def week_reading(points, now):
    """One account's weekly readings (oldest first) -> its weekly fields: the
    newest reading, when it reads the week still open (a reset ahead, or 0%
    with no reset: a week nobody has started), and what the account spent
    of it over the last BURN_S. PURE."""
    rec = {"weekly_pct": None, "weekly_reset_at": None, "weekly_at": None,
           "weekly_spent_pct": None, "weekly_spent_from": None}
    if not points:
        return rec
    at, used, reset = points[-1]
    if not (reset is None and used == 0.0 or reset is not None
            and reset > now):
        return rec
    rec.update(weekly_pct=used, weekly_reset_at=reset, weekly_at=at)
    spent = [p for p in points if p[0] >= now - BURN_S
             and _same_reset(p[2], reset)]
    if spent:
        rec.update(weekly_spent_pct=round(max(0.0, used - spent[0][1]), 1),
                   weekly_spent_from=spent[0][0])
    return rec


def read(rows, now, prior=None, default_key=None, horizon=None, census=None):
    """History rows + the prior snapshot -> the reading: each account's 5h
    record with its weekly fields and pool membership, the pool, and the
    default home's account (`default_key`, None when unread) with the last
    switch of it. `census` is {account: plan} for rows that name none. PURE."""
    from . import accounts
    prior = prior if isinstance(prior, dict) else {}
    before = prior.get("accounts")
    before = before if isinstance(before, dict) else {}
    weekly, tiers, last = (_readings(rows, WEEK_LABEL), _tiers(rows),
                           _last_rows(rows))
    out = {}
    for name, points in sorted(_readings(rows).items()):
        key = accounts.measured_key(name)
        rec = account_reading(points, now, before.get(key))
        if rec["idle"] and last.get(name, 0) > rec["measured_at"]:
            rec.update(idle=False, why="read idle at %s; a newer probe did "
                       "not read it" % _hm(rec["measured_at"]))
        rec["idle_stale"] = rec["idle"] \
            and now - rec["measured_at"] > IDLE_FRESH_S
        rec["label"] = accounts.mask_identity(name)
        rec.update(week_reading(weekly.get(name) or [], now))
        plan = tiers.get(name) or (census or {}).get(name)
        rec["pool"] = str(plan or "").startswith(POOL_TIER)
        out[key] = rec
    reading = {"v": V, "ts": now, "accounts": out,
               "pool": pool_reading(out, now, horizon, prior.get("pool"))}
    reading.update(_switched(prior, default_key, now))
    return reading


def _switched(prior, key, now):
    """{"default", "switch"}: the default home's account this pass, and the
    last time it changed. An unread home carries both forward."""
    held = prior.get("default") if isinstance(prior.get("default"), dict) \
        else {}
    last = prior.get("switch") if isinstance(prior.get("switch"), dict) \
        else None
    if not key:
        return {"default": held or None, "switch": last}
    if held.get("key") and held["key"] != key:
        last = {"at": now, "from": held["key"], "to": key}
    since = held.get("since") if held.get("key") == key else now
    return {"default": {"key": key, "since": since}, "switch": last}


# ------------------------------------------------------------------ the pool

def switchable(rec):
    """True when the watcher can switch the baseload home onto this
    account: a pool account under SWITCH_WEEKLY_PCT of its week whose 5h
    window is IDLE, or OK under SWITCH_SESSION_PCT. An account in use that
    nothing can read (UNKNOWN with an open window, or no reading) is not."""
    if not isinstance(rec, dict) or not rec.get("pool"):
        return False
    weekly = _num(rec.get("weekly_pct"))
    if weekly is None or weekly >= SWITCH_WEEKLY_PCT:
        return False
    if rec.get("idle") is True:
        return not rec.get("idle_stale") or weekly < IDLE_STALE_WEEKLY_PCT
    used = _num(rec.get("used_pct"))
    return rec.get("state") == OK and used is not None \
        and used < SWITCH_SESSION_PCT


def _horizon(known, now, horizon):
    """(instant, source) the pool must last until: the owner's while it is
    ahead, else the soonest weekly reset returning at least CARRY_PCT of a
    week, else the soonest weekly reset; (None, None) with none."""
    from . import burnflags
    at = _num((horizon or {}).get("at"))
    if at is not None and at > now:
        return min(at, now + HORIZON_MAX_S), "owner"
    resets = sorted((r["weekly_reset_at"], r["weekly_pct"]) for r in known
                    if _num(r.get("weekly_reset_at")) is not None
                    and r["weekly_reset_at"] > now)
    real = [t for t, used in resets if used >= burnflags.CARRY_PCT]
    if real:
        return real[0], "reset"
    return (resets[0][0], "reset") if resets else (None, None)


def _step(pct):
    """A percentage to the nearest 5, never under 5."""
    return max(5, int(round(pct / 5.0)) * 5)


def pool_reading(recs, now, horizon=None, prior=None):
    """The switch pool's weekly pace against its horizon -> its record.
    PURE. `horizon` is the owner's record ({"at": ...}) or None; `prior` is
    the last pass's pool record, which carries the hysteresis."""
    from . import burnflags
    members = [r for r in (recs or {}).values()
               if isinstance(r, dict) and r.get("pool")]
    known = [r for r in members if _num(r.get("weekly_pct")) is not None]
    out = {"state": UNKNOWN, "colour": burnflags.GREY, "advice": None,
           "why": None, "accounts": len(members), "read": len(known),
           "left_pct": None, "pct_per_hour": None, "span_s": 0,
           "runout_at": None, "horizon_at": None, "horizon_from": None,
           "ratio": None}
    if not known:
        out["why"] = "no account in the switch pool has a weekly reading"
        return out
    left = sum(max(0.0, 100.0 - r["weekly_pct"]) for r in known)
    at, source = _horizon(known, now, horizon)
    out.update(left_pct=round(left, 1), horizon_at=at, horizon_from=source)
    spent = [(r["weekly_spent_pct"], r["weekly_spent_from"]) for r in known
             if _num(r.get("weekly_spent_pct")) is not None
             and _num(r.get("weekly_spent_from")) is not None]
    span = max(0.0, now - min(f for _s, f in spent)) if spent else 0.0
    out["span_s"] = int(span)
    if span < MIN_BURN_SPAN_S:
        out["why"] = ("the pool's weekly readings span %dm; a burn needs %dm"
                      % (span // 60, MIN_BURN_SPAN_S // 60))
        return out
    rate = sum(s for s, _f in spent) / (span / HOUR)
    out["pct_per_hour"] = round(rate, 2)
    if at is None:
        out["why"] = "no horizon: no pool account names a weekly reset"
        return out
    # A reset before the horizon adds a whole week; the headroom until it
    # is already in `left`. The horizon is at most a week out: one reset.
    supply = left + 100.0 * sum(1 for r in known
                                if _num(r.get("weekly_reset_at")) is not None
                                and r["weekly_reset_at"] < at)
    if rate > 0:
        out["runout_at"] = now + supply / rate * HOUR
    if supply <= 0:
        out.update(state=EASE, colour=burnflags.RED, runout_at=now,
                   advice="the pool is spent until %s" % _day(at))
        return out
    ratio = rate / (supply / max((at - now) / HOUR, 1e-6))
    out["ratio"] = round(ratio, 2)
    held = (prior or {}).get("state") if isinstance(prior, dict) else None
    if held == EASE and ratio >= EASE_CLEAR or ratio >= EASE_AT:
        imminent = out["runout_at"] is not None \
            and out["runout_at"] - now <= WINDOW_S
        out.update(state=EASE, colour=burnflags.RED if imminent
                   else burnflags.ORANGE,
                   advice="ease off about %d%%" % _step(100 * (1 - 1 / ratio)))
    elif held == FASTER and ratio <= FASTER_CLEAR or ratio <= FASTER_AT:
        more = "" if ratio <= 0 else " (about %d%% more)" % min(
            200, _step(100 * (1 / ratio - 1)))
        out.update(state=FASTER, colour=burnflags.GREEN,
                   advice="can go faster" + more)
    else:
        out.update(state=EVEN, colour=burnflags.YELLOW, advice="on pace")
    return out


def pool_clause(pool, short=False):
    """The pool's pace in one clause: the colour, the adjustment, and (in
    full) the burn, what is left, the run-out and the horizon."""
    pool = pool if isinstance(pool, dict) else {}
    if pool.get("state") in (None, UNKNOWN):
        return "Claude pool pace UNKNOWN: %s" % (pool.get("why")
                                                   or "not measured")
    out, horizon = pool.get("runout_at"), pool.get("horizon_at")
    vs = ("out ~%s, before horizon %s" % (_day(out), _day(horizon))
          if out and horizon and out < horizon
          else "lasts past horizon %s" % _day(horizon))
    if short:
        return "Claude pool %s: %s, %s" % (pool["colour"], pool["advice"], vs)
    return ("Claude pool %s: %s; %.1f%%/h of a week over %.0fh, %.0f%% left, "
            "%s" % (pool["colour"], pool["advice"],
                    pool.get("pct_per_hour") or 0.0,
                    (pool.get("span_s") or 0) / HOUR,
                    pool.get("left_pct") or 0.0, vs))


def _project_light(cwd):
    """(project, authored colour, its reason) for a working directory, or
    (None, None, ""): only an authored light binds (`registry.admits`' law).
    Never raises."""
    try:
        from . import registry
        from .inject import _ledger
        reg = registry.load(strict=True)
        key = _ledger.project_for_cwd(cwd, projects=reg.get("projects") or {})
        if not key:
            return None, None, ""
        lit = registry.light(key, reg["projects"].get(key))
    except Exception:                           # noqa: BLE001
        return None, None, ""
    if not lit["authored"] or lit["colour"] not in registry.LIGHT_SAYS:
        return key, None, ""
    return key, lit["colour"], str(lit.get("reason") or "")


def advice(pool, cwd, short=False):
    """What a seat hears after its headline: its project's light in the
    registry's own sentence, the pool's pace, and LAST the light's authored
    reason, so a cap at the steer cuts the reason and never the advice."""
    from . import registry
    key, colour, why = _project_light(cwd) if cwd else (None, None, "")
    light = " %s is %s: %s." % (key, colour.upper(),
                                registry.LIGHT_SAYS[colour]) if colour else ""
    why = " %s's light: %s." % (key, why.strip().rstrip(".")) \
        if colour and why.strip() else ""
    return "%s %s.%s" % (light, pool_clause(pool, short=short), why)


# ------------------------------------------------------------------ the pass

def _recent_rows(now):
    """The native history's rows from the last window and an hour (every
    reading an open window can hold, and the pool's burn span), and each
    account's newest READ row from the IDLE_MAX_AGE_S before that: an
    unused account is read rarely, and that row is its idle reading and its
    week."""
    from . import burnflags
    floor, older, latest = now - WINDOW_S - HOUR, {}, {}
    recent = []
    for row in burnflags._history_lines():
        at = pk.parse_ts_epoch(row.get("probed_at")) or 0
        if at >= floor:
            recent.append(row)
        elif at >= now - IDLE_MAX_AGE_S:
            # and its newest row of any status, which ends an idle reading
            latest[row["account"]] = row
            if str(row.get("status") or "").startswith(
                    burnflags.READ_STATUSES):
                older[row["account"]] = row
    later = [r for a, r in latest.items() if r is not older.get(a)]
    return list(older.values()) + later + recent


def write_snapshot(reading, path=None):
    """Persist the reading -> True/False. Best effort, never raises."""
    import json
    try:
        pk.atomic_write(path or snapshot_path(),
                        json.dumps(reading, sort_keys=True))
        return True
    except Exception:                           # noqa: BLE001
        return False


def _default_key():
    """The default home's account handle, or None when it is unreadable."""
    from . import homes
    return _account_key(homes.DEFAULTS["claude"])


def _census_tiers():
    """{account name: plan} from the providers' account census: each home's
    and Orca's account metadata files, with no vendor call."""
    from . import providers
    return {a["name"]: a["tier"]
            for a in providers.NativeQuotaProvider().accounts()
            if a.get("provider") == FAMILY and isinstance(a.get("tier"), str)}


def watch_pass(now=None, rows=None, path=None):
    """The watchdog pass's rung: read the history, fold it against the prior
    snapshot's states, persist -> the reading. The account census is asked
    only when some row names no plan (rows written before rows carried
    one)."""
    now = time.time() if now is None else now
    rows = _recent_rows(now) if rows is None else rows
    prior = pk.read_json(path or snapshot_path(), default=None)
    try:
        default_key = _default_key()
    except Exception:                           # noqa: BLE001
        default_key = None
    tiers, census = _tiers(rows), {}
    if any(isinstance(r, dict) and r.get("account")
           and r["account"] not in tiers for r in rows):
        try:
            census = _census_tiers()
        except Exception:                       # noqa: BLE001
            census = {}
    reading = read(rows, now, prior, default_key=default_key,
                   horizon=load_horizon(now), census=census)
    write_snapshot(reading, path=path)
    return reading


# ------------------------------------------------------------------ horizon

def horizon_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", HORIZON_NAME)


def load_horizon(now=None, path=None):
    """The owner's horizon for the Claude pool ({"at", "by", "why", "ts"})
    while it is ahead, else None."""
    doc = pk.read_json(path or horizon_path(), default=None)
    rec = doc.get(FAMILY) if isinstance(doc, dict) else None
    at = _num((rec or {}).get("at")) if isinstance(rec, dict) else None
    now = time.time() if now is None else now
    return dict(rec, at=at) if at is not None and at > now else None


def set_horizon(at, by=None, why=None, now=None, path=None):
    """Record (or, with `at` None, clear) the owner's horizon -> (ok, err)."""
    import json
    now = time.time() if now is None else now
    if at is not None and not now < at <= now + HORIZON_MAX_S:
        return False, ("the horizon must be ahead of now and at most %d days "
                       "out" % (HORIZON_MAX_S // 86400))
    target = path or horizon_path()
    doc = pk.read_json(target, default=None)
    doc = doc if isinstance(doc, dict) else {}
    if at is None:
        doc.pop(FAMILY, None)
    else:
        doc[FAMILY] = {"at": at, "by": by or "unknown", "why": why or "",
                       "ts": now}
    try:
        pk.atomic_write(target, json.dumps(doc, sort_keys=True))
    except OSError as exc:
        return False, "cannot write %s (%s)" % (target, exc)
    return True, None


_HORIZON_USAGE = ("helm burn horizon [anthropic <utc-iso> [reason...] | "
                  "anthropic --clear]")


def cmd_horizon(args):
    """horizon [anthropic <utc-iso> [reason...] | anthropic --clear] — what
    the Claude pool must last until. Bare prints it. Exit 2 on usage."""
    import sys
    args = list(args or ())
    if not args:
        rec = load_horizon()
        if rec:
            print("helm burn horizon: anthropic lasts until %s (set by %s%s)"
                  % (_day(rec["at"]), rec.get("by") or "?",
                     ": " + rec["why"] if rec.get("why") else ""))
        else:
            print("helm burn horizon: anthropic has no owner horizon; the "
                  "pool paces to the soonest weekly reset that returns at "
                  "least half a week")
        return 0
    if args[0] != FAMILY or len(args) < 2:
        print("helm burn horizon: only %s has a pool horizon (codex paces to "
              "its next Pro reset: `helm burn runway`)\nusage: %s"
              % (FAMILY, _HORIZON_USAGE), file=sys.stderr)
        return 2
    if args[1] == "--clear":
        ok, err = set_horizon(None)
        print(("helm burn horizon: anthropic horizon cleared" if ok
               else "helm burn horizon: %s" % err),
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2
    at = pk.parse_ts_epoch(args[1])
    if at is None:
        print("helm burn horizon: %r is not a UTC instant (e.g. "
              "2026-10-03T07:00Z)\nusage: %s" % (args[1], _HORIZON_USAGE),
              file=sys.stderr)
        return 2
    from . import freetext
    why, rc = freetext.tail("helm burn horizon", "reason", args[2:],
                            "the reason", usage=_HORIZON_USAGE)
    if rc is not None:
        return rc
    ok, err = set_horizon(at, by=home.chat_name(), why=why)
    if not ok:
        print("helm burn horizon: %s" % err, file=sys.stderr)
        return 2
    print("helm burn horizon: anthropic lasts until %s; the next watchdog "
          "pass paces the pool to it" % _day(at))
    return 0


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
    """`helm burn`'s lines about the Claude pace: one per account, the
    pool's, and one per project with an authored light. Never raises."""
    try:
        snap = cached(now=now)
        if snap is None:
            return ["  claude 5h pace: not measured — the posting watchdog "
                    "pass writes it (`helm proxywatch --post`)"]
        rows = sorted(snap["accounts"].values(),
                      key=lambda r: str(r.get("label")))
        if not rows:
            return ["  claude 5h pace: no Claude account in the usage history"]
        out = ["  claude 5h pace %s: %s%s" % (r.get("label"), describe(r),
                                              _week_suffix(r))
               for r in rows]
        # THE OWNER'S HORIZON AS IT STANDS NOW, over the pass's accounts, so
        # a horizon just set shows here before the next pass carries it.
        pool = pool_reading(snap["accounts"], snap["ts"],
                            load_horizon(snap["ts"]), snap.get("pool"))
        out.append("  claude pool pace: %s%s" % (
            pool_clause(pool), _pool_suffix(pool)))
        return out + _project_lines()
    except Exception as exc:                    # noqa: BLE001
        return ["  claude 5h pace: unreadable (%s)" % type(exc).__name__]


def _week_suffix(rec):
    if _num(rec.get("weekly_pct")) is None:
        return ""
    return "; week %.0f%%%s" % (rec["weekly_pct"],
                                " (switch pool)" if rec.get("pool") else "")


def _pool_suffix(pool):
    if pool.get("state") in (None, UNKNOWN):
        return ""
    return "; %d of %d pool account(s) read, horizon %s" % (
        pool["read"], pool["accounts"],
        "owner-set" if pool.get("horizon_from") == "owner"
        else "the soonest reset returning half a week")


def _project_lines():
    """One line per project whose light is authored, green first. An
    unreadable registry costs these lines only, named."""
    from . import registry
    try:
        lit = registry.lights(registry.load(strict=True))
    except Exception as exc:                    # noqa: BLE001
        return ["  claude pace per project: unreadable (%s)"
                % type(exc).__name__]
    order = ("green", "yellow", "orange", "red")
    rows = sorted((order.index(v["colour"]), k) for k, v in lit.items()
                  if v.get("authored") and v.get("colour") in order)
    return ["  claude pace for %s (%s): %s%s" % (
        k, order[i].upper(), registry.LIGHT_SAYS[order[i]],
        " (reason: %s)" % lit[k]["reason"] if lit[k].get("reason") else "")
        for i, k in rows]


def steer_line(rec, homed=False):
    pace = rec.get("pct_per_hour")
    return ("REFLEX: this account's 5h window is %s at %.0f%%%s%s, resets %s: "
            "%s." % (rec.get("state"), rec.get("used_pct") or 0.0,
                     "" if pace is None else ", %.0f%%/h" % pace,
                     ", hits 100%% ~%s" % _hm(rec["hit_at"])
                     if rec.get("hit_at") else "",
                     _hm(rec.get("reset_at")),
                     STEER_HOMED if homed else STEER_ASK))


def switch_line(rec, snap, cwd=None):
    """The baseload seat's line while its pool has a switchable account: the
    watcher will switch it, the pool's pace, and its project's advice."""
    return ("REFLEX: 5h %s at %.0f%%, resets %s; %s.%s"
            % (rec.get("state"), rec.get("used_pct") or 0.0,
               _hm(rec.get("reset_at")), STEER_SWITCH,
               advice((snap or {}).get("pool"), cwd, short=True)))


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
    if _proxy_seat():
        return None
    if not homed and _room_elsewhere(payload, key):
        return switch_line(rec, payload, context.get("cwd")), _heard(key, rec)
    return steer_line(rec, homed), _heard(key, rec)


def _room_elsewhere(snap, key):
    """True when the watcher can switch a BASELOAD seat's home onto another
    account (`switchable`). Such a seat is then told the switch is coming,
    never to route away. A seat homed to one credential (a named credhome)
    is not moved, so this is never asked for it. UNKNOWN IS NOT ROOM (task
    3718): an account in use that nothing can read may be as full as this
    one. An IDLE one is read: its window is not open (task/3871)."""
    return any(switchable(r)
               for k, r in ((snap or {}).get("accounts") or {}).items()
               if k != key)


def handoff(context, said=None, now=None, snap=None):
    """(line, key) for a native Claude seat that has not heard the default
    home's latest switch (`said` is the key it last heard), taking a turn
    within HANDOFF_SAY_S of it, else None. One line per switch: the pool's
    pace and the seat's project advice. A seat homed to an account outside
    the pool does not hear it. Words only."""
    context = context or {}
    if context.get("harness") != "claude":
        return None
    payload = _load() if snap is None else snap
    last = (payload or {}).get("switch")
    at = _num(last.get("at")) if isinstance(last, dict) else None
    if at is None:
        return None
    key = "switch|%d" % at
    now = time.time() if now is None else now
    if said == key or not 0 <= now - at <= HANDOFF_SAY_S:
        return None
    if snap is None and not _fresh(payload, now):
        return None
    if _off_pool(context, payload) or _proxy_seat():
        return None
    return ("REFLEX: " + switched_text(payload, context.get("cwd")), key)


def switched_text(snap, cwd=None):
    """The default home's latest switch with the advice after it (the
    project's light for `cwd`, the pool's pace), or None when no switch is
    recorded. The words each seat hears (`handoff`) and the #seats row
    (task/3876, no cwd) share it. Words only."""
    last = (snap or {}).get("switch")
    at = _num(last.get("at")) if isinstance(last, dict) else None
    if at is None:
        return None
    return ("the Claude home switched accounts at %s.%s"
            % (_hm(at), advice(snap.get("pool"), cwd)))


def _off_pool(context, snap):
    """True for a seat homed (a named credhome) to an account the snapshot
    does not read as in the switch pool: its spend is not known to be the
    pool's, so it is not told."""
    home_dir = context.get("config_home")
    from . import cred
    if not home_dir or not cred.is_credhome(home_dir):
        return False
    rec = ((snap or {}).get("accounts") or {}).get(_account_key(home_dir))
    return not (isinstance(rec, dict) and rec.get("pool"))


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
