#!/usr/bin/env python3
"""helm codex pace — percent per hour per codex ACCOUNT, and the fleet's
RUNWAY against its HORIZON (task/2984).

THE FAILURE THIS PREVENTS. The money axis of the burn flag reads LEVELS: the
account with the most headroom, the share of accounts past half their week. A
level cannot see a wall coming. A fleet can hold most of its codex week and
still run dry days before the first Pro account resets, because the RATE, not
the level, decides when the last account walls. Until this module the owner
read that by hand and declared the colour himself.

THREE READINGS, EACH WITH ITS INPUTS STATED

  PACE     per account: the least-squares slope of the account's LONGEST
           window, in percent per hour, over a stated window (`WINDOW_H`),
           from the budget history the watchdog pass records, counting only
           the points after the account's last reset.
  RUNWAY   the fleet's supply in TOKENS over its token rate. Supply is each
           account's remaining percent times its tokens per percent: the
           ledger's tokens on that account over the percent the account moved
           while it spent them. The rate is the ledger's codex tokens per hour
           over the same stated window. A Team account holds a small fraction
           of a Pro account's week, so percentages cannot be added across
           plans; tokens can.
  HORIZON  the next natural reset of a Pro account. The Pro accounts carry the
           baseload, and a Team reset restores too little to end a shortage.
           A spent reset credit that restores at least `RESTORE_FRACTION` of
           the supply is the other horizon; the reset policy decides when a
           credit is spent, and hands such an event in (`planned`).

  CREDITS  each account's HELD reset credits, read from the same usage body
           the watchdog pass already probes (`codexresets.usage_balance`),
           and each valued against the horizon by THE RESET CLOCK law below.
           A non-Pro credit counts its account's week in tokens.
           The runway the verdict reads is the supply plus every credit that
           lands supply before the horizon.

THE VERDICT is the key of the fold's step table (`burnflags._RUNWAY_STEP_TABLE`):
`short` (runway under the horizon: the money axis steps UP one colour),
`use-it` (runway at least `WELL_ABOVE` times the horizon while an account
projects to strand at least `STRAND_PCT` of its week: the axis steps DOWN),
`even`, or `unknown`. The step itself is the fold's, never this module's.

LAWS

  TOO FEW READINGS IS UNKNOWN, NEVER 0. A rate needs `MIN_POINTS` readings
  over at least `MIN_SPAN_S`: the vendor's percent is an integer, and a slope
  over a shorter span is the rounding, not the burn. An account without a
  rate carries None on every field derived from one.

  ONE MEMBER, ONE SERIES. On a Team plan the account id is the WORKSPACE, and
  every member carries it (task/2981). A series keyed on it pours every
  member's readings into one line. The key is `codexresets.member_id` over
  the account id and the user id; a personal plan's account id names one
  person on its own. A Team record with NO user id is keyed on its own pool
  file, so its numbers never join a sibling's, and it is LEFT OUT of the
  fleet supply: it may be a second copy of a member already counted.

  THE PROVIDERS USAGE HISTORY IS NOT READ FOR CODEX. Its codex rows are keyed
  by codex home name and carry no member proof, and a Team member's rows
  written before the per-member pool match carry a sibling's numbers. The
  history read here is written by the watchdog pass from per-credential
  probes, and each line names its member.

  AN INCOMPLETE READING STEPS NOTHING. Every account unread, unproven or
  without a weight makes the supply a FLOOR, and a floor proves neither a
  shortage nor a surplus: the verdict is `unknown`. The same holds for an
  unreadable ledger and for a newest pass older than the budget gate's own
  freshness bound.

  THE RESET CLOCK SETS A CREDIT'S WORTH (the owner: "codex resets also reset
  the date to a week from the point the reset was used").
  A credit spent on an account whose natural reset is h hours away gives one
  fresh week W now and moves that account's next reset from h to 168h out,
  so over the long run it nets W*h/168. For a PRO credit that is two cases,
  each stated per credit in `fleet.credits.per`:
    - its account resets AFTER the horizon: spent once that account is empty
      it adds W before the horizon, and the runway counts it. A shortage
      empties every account before the horizon, so such a credit is always
      spendable in time;
    - its account resets AT or BEFORE the horizon (the horizon account
      itself, or an earlier one): spending it first forfeits that refill, so
      it adds nothing before the horizon and is worth W after it.
  A NON-PRO credit counts W before the horizon wherever its reset lies. The
  owner's ruling: a smaller account's week is exhausted again in well under
  a day, so the week its reset clock moves costs little. A workspace
  "credits depleted" wall is a weekly wall a reset is measured to lift
  (`codexresets.CREDITS_DEPLETED_TYPES`), so a credit held there counts.
  A credit whose worth before the horizon cannot be stated (no weight, an
  unread reset, a wall with no reason) and an account whose balance was not
  read are UNKNOWN, never 0: while one could land supply before the horizon,
  a short runway is a floor and the verdict is `unknown`.

  NO IDENTITY REACHES THE FOLD. `fold_input` carries the verdict, the cause
  and numbers; the cause names plans and seats, never an account. The
  snapshot's per-account rows carry the masked address
  (`accounts.mask_identity`), as the owner's other cards do.

  ONE CHAT LINE AND ONE PHONE PUSH PER WALL. A wall is (member, the reset
  instant of the walled window). The claim is written before delivery, each
  channel is acknowledged on its own, and only a failed channel is retried.

BOUNDS, stated and not corrected. Ledger gaps (a sidecar the meter could not
read) lower both a weight and the rate. A request a seat proxy forwards to
another local proxy would appear in both ledgers once such chains exist.
Native codex use outside the proxies moves an account's percent with no
ledger row, which raises that account's tokens per percent.
"""
import bisect
import fcntl
import json
import os
import time

from . import home, pk

FAMILY = "codex"
HISTORY_NAME = "codex-budget-history.jsonl"
SNAPSHOT_NAME = "codex-runway.json"
WALLS_NAME = "codex-walls.json"
V = 1
HOUR = 3600.0

#: The stated window for a rate and for the fleet's token rate.
WINDOW_H = 6.0
MIN_POINTS = 3
MIN_SPAN_S = 3600
#: A weight needs the account to have moved this many points: below it the
#: vendor's integer rounding is the weight.
WEIGHT_MIN_DELTA_PCT = 3.0
#: How far back a weight and the history read reach: one weekly window.
WEIGHT_SPAN_S = 7 * 86400
HISTORY_KEEP_S = 8 * 86400
HISTORY_MAX_BYTES = 4 * 1024 * 1024
#: Two readings of one window agree on its reset instant; a restarted window
#: moves it by days. A fall of more than the rounding is a restart too.
RESET_TOLERANCE_S = 300
RESET_DROP_PCT = 2.0
RESTORE_FRACTION = 0.5
#: "Well above" the horizon: a margin over a straight-line projection whose
#: inputs carry the vendor's integer rounding, not a measurement.
WELL_ABOVE = 1.5
STRAND_PCT = 20.0
TOP_SEATS_S = 3 * 3600
TOP_SEATS = 3
TOP_SEAT_MIN_SHARE = 0.05
WALL_KEEP_S = 86400
ROOM = "helm"
WHO = "proxywatch"


def _state_dir():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state")


def history_path():
    return os.path.join(_state_dir(), HISTORY_NAME)


def snapshot_path():
    return os.path.join(_state_dir(), SNAPSHOT_NAME)


def walls_path():
    return os.path.join(_state_dir(), WALLS_NAME)


def _num(value):
    """A finite number, or None. A bool is not a percentage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") \
        else None


def _verdicts():
    from . import burnflags
    return (burnflags.RUNWAY_SHORT, burnflags.RUNWAY_USE_IT,
            burnflags.RUNWAY_EVEN, burnflags.RUNWAY_UNKNOWN)


# ------------------------------------------------------------------ identity

def member_key(row):
    """(key, proven) for one budget row, or (None, False) when the row names
    no credential at all. See the ONE MEMBER, ONE SERIES law."""
    from . import codexhomes, codexresets
    account_id, user_id = row.get("account_id"), row.get("user_id")
    if account_id and codexhomes.personal_plan(row.get("plan")):
        return codexresets.member_id({"account_id": account_id}), True
    if account_id and user_id:
        return codexresets.member_id({"account_id": account_id,
                                      "user_id": user_id}), True
    handle = row.get("file") or "|".join(sorted(row.get("files") or ()))
    if not handle:
        return None, False
    return codexresets.member_id({"file": handle}), False


def _balance(value):
    """The held reset-credit counts of one budget row, or None when the
    balance was not read. Counts only: the row's own shape is
    `codexresets.usage_balance`'s."""
    if not isinstance(value, dict):
        return None
    held = value.get("available")
    if isinstance(held, bool) or not isinstance(held, int) or held < 0:
        return None
    applicable = value.get("applicable")
    return {"available": held,
            "applicable": applicable if isinstance(applicable, int)
            and not isinstance(applicable, bool) else None}


def _longest(windows):
    """The account's LONGEST window, or None. Chosen by length among every
    window, so a longest window with no percent is an unread account rather
    than a silent fall back to the five-hour one."""
    sized = [w for w in windows or () if isinstance(w, dict)]
    if not sized:
        return None
    best = max(sized, key=lambda w: _num(w.get("seconds")) or 0)
    return best if _num(best.get("used_percent")) is not None else None


# ------------------------------------------------------------------ history

def history_line(rows, now):
    """One watchdog pass's budget rows -> one history line. No token, no
    account id and no user id: the member digest, the display label the reset
    ledger already uses, the plan and the windows."""
    from . import codexresets
    accounts, index = [], {}
    for row in rows or ():
        key, proven = member_key(row)
        if key is None:
            continue
        entry = {"member": key, "proven": proven,
                 "label": codexresets.label(row), "plan": row.get("plan"),
                 "state": row.get("state"),
                 "reached_type": row.get("reached_type"),
                 "reset_credits": _balance(row.get("reset_credits")),
                 "windows": [{"label": w.get("label"),
                              "seconds": w.get("seconds"),
                              "used_percent": w.get("used_percent"),
                              "reset_at": w.get("reset_at")}
                             for w in (row.get("windows") or ())
                             if isinstance(w, dict)]}
        if key in index:
            # ONE CREDENTIAL, TWO FILES (two logins of one person): the
            # readable copy wins, and the account is written once.
            if _longest(accounts[index[key]]["windows"]) is None:
                accounts[index[key]] = entry
            continue
        index[key] = len(accounts)
        accounts.append(entry)
    # THE LEDGER'S ROW GRAMMAR asks for a non-empty id: one pass, one line.
    return {"v": V, "id": "codex-budget:%.3f" % now, "ts": now,
            "accounts": accounts}


def _append_line(line, path=None):
    """Append one history line under the ledger lock, pruning first when the
    file has grown past its bound -> True when it landed."""
    from . import eventledger
    target = path or history_path()
    with eventledger.locked(target) as held:
        if not held:
            return False
        try:
            if os.path.getsize(target) > HISTORY_MAX_BYTES:
                _prune_unlocked(target, line["ts"])
        except OSError:
            pass
        return eventledger.append_unlocked(target, line)


def _prune_unlocked(target, now):
    from . import eventledger
    rows, err = eventledger.checked_events(target)
    if err:
        return
    keep = [r for r in rows if isinstance(r, dict)
            and (_num(r.get("ts")) or 0) >= now - HISTORY_KEEP_S]
    pk.atomic_write(target, "".join(json.dumps(r, separators=(",", ":")) + "\n"
                                    for r in keep), mode=0o600)


def append_history(rows, now, path=None):
    return _append_line(history_line(rows, now), path=path)


def read_history(now=None, path=None):
    """(lines inside one weekly window, oldest first, None) or ([], why)."""
    from . import eventledger
    now = time.time() if now is None else now
    rows, err = eventledger.checked_events(path or history_path())
    if err:
        return [], "codex budget history unreadable: %s" % err
    lines = [r for r in rows if isinstance(r, dict) and r.get("v") == V
             and _num(r.get("ts")) is not None
             and now - WEIGHT_SPAN_S <= r["ts"] <= now + 1]
    lines.sort(key=lambda r: r["ts"])
    return lines, None


def ledger_requests(now):
    """The proxy-usage ledger's requests inside one weekly window, or None
    when the ledger could not be read. An absent ledger is an empty one."""
    from . import proxy_usage
    requests, _last, unknown = proxy_usage.events(since=now - WEIGHT_SPAN_S)
    if unknown and not requests:
        return None
    return requests


class _Ledger:
    """Tokens by account and by seat, indexed for window sums.

    ONE FAMILY BY DEFAULT, EVERY FAMILY ON REQUEST (task/3156). The pace and
    the wall events read codex and nothing else, so `family` defaults to it;
    the per-seat burn a project's team is measured against reads every proxy
    family from the same ledger (`family=None`), so there is one reader of the
    proxy-usage ledger's tokens and not a second one per surface."""

    def __init__(self, requests, family=FAMILY):
        from . import proxy_usage
        rows = []
        for ev in requests or ():
            if not isinstance(ev, dict) or ev.get("failed"):
                continue
            fam = str(ev.get("family") or "")
            if family is not None and fam != family:
                continue
            at = _num(ev.get("at"))
            at = _num(ev.get("ts")) if at is None else at
            if at is None:
                continue
            inp, out = proxy_usage.tokens_of(ev)
            rows.append((at, float(inp + out), str(ev.get("seat") or "?"),
                         str(ev.get("source") or "").strip().casefold(), fam))
        rows.sort(key=lambda r: r[0])
        self.rows = rows
        self.ats = [r[0] for r in rows]

    def _slice(self, t0, t1):
        return self.rows[bisect.bisect_right(self.ats, t0):
                         bisect.bisect_right(self.ats, t1)]

    def on(self, label, t0, t1):
        """Tokens spent on the account whose ledger `source` is `label`."""
        want = str(label or "").strip().casefold()
        if "@" not in want:
            return 0.0
        return sum(r[1] for r in self._slice(t0, t1) if r[3] == want)

    def per_hour(self, t0, t1):
        span = (t1 - t0) / HOUR
        return sum(r[1] for r in self._slice(t0, t1)) / span if span > 0 \
            else None

    def seats(self, t0, t1, label=None):
        """[(seat, share)] by tokens, largest first."""
        want = str(label or "").strip().casefold() if label else None
        by = {}
        for r in self._slice(t0, t1):
            if want is None or r[3] == want:
                by[r[2]] = by.get(r[2], 0.0) + r[1]
        total = sum(by.values())
        if total <= 0:
            return []
        ranked = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(seat, round(n / total, 3)) for seat, n in ranked
                if n / total >= TOP_SEAT_MIN_SHARE][:TOP_SEATS]

    def burn(self, t0, t1):
        """{family: {seat: tokens}} over (t0, t1] — EVERY seat, no top-N cut
        and no share floor: `seats` ranks for a sentence, this sums for a bill,
        and a seat cut from a bill is spend nobody is charged for."""
        out = {}
        for r in self._slice(t0, t1):
            fam = out.setdefault(r[4], {})
            fam[r[2]] = fam.get(r[2], 0.0) + r[1]
        return out


# THE PER-SEAT BURN, every proxy family (task/3156). A projection of the
# proxy-usage ledger that the posting pass writes beside the runway, so a
# routing answer reads one small file instead of the whole ledger.
SEAT_BURN_NAME = "seat-burn.json"
#: The window a project's burn is measured over: the same three hours the
#: runway cause names its top seats over, so the two sentences agree.
SEAT_BURN_WINDOW_S = TOP_SEATS_S


def seat_burn_path():
    return os.path.join(_state_dir(), SEAT_BURN_NAME)


def seat_burn(requests, now, window_s=SEAT_BURN_WINDOW_S):
    """The ledger's requests -> {v, ts, window_s, families: {family:
    {per_hour, seats: {seat: per_hour}}}}. PURE. `requests` None is a ledger
    that could not be read, and the reading says so rather than reading zero."""
    if requests is None:
        return {"v": V, "ts": now, "window_s": window_s, "families": {},
                "why": "the proxy-usage ledger could not be read"}
    hours = window_s / HOUR
    burned = _Ledger(requests, family=None).burn(now - window_s, now)
    return {"v": V, "ts": now, "window_s": window_s, "why": None,
            "families": {fam: {"per_hour": sum(seats.values()) / hours,
                               "seats": {seat: n / hours
                                         for seat, n in sorted(seats.items())}}
                         for fam, seats in sorted(burned.items())}}


def write_seat_burn(reading, path=None):
    """Persist the per-seat burn -> True/False. Best effort, never raises."""
    try:
        pk.atomic_write(path or seat_burn_path(),
                        json.dumps(reading, sort_keys=True))
        return True
    except Exception:                           # noqa: BLE001
        return False


def cached_seat_burn(now=None, max_age=None, path=None):
    """The per-seat burn from its snapshot, or None when absent, unreadable or
    past the burn flags' own freshness bound. NEVER reads the ledger."""
    from . import burnflags
    now = time.time() if now is None else now
    bound = burnflags.max_age_s() if max_age is None else max_age
    payload = pk.read_json(path or seat_burn_path(), default=None)
    if not isinstance(payload, dict) or payload.get("v") != V \
            or _num(payload.get("ts")) is None \
            or not isinstance(payload.get("families"), dict):
        return None
    age = now - payload["ts"]
    return payload if 0 <= age <= bound else None


def live_seat_burn(now=None):
    """The per-seat burn read from the ledger itself (seconds on a busy host):
    for a verb a person ran, when the pass has not written the snapshot."""
    now = time.time() if now is None else now
    return seat_burn(ledger_requests(now), now)


# ------------------------------------------------------------------ the pace

def _points(series, label):
    """(ts, used, reset_at) on the window named `label`, oldest first."""
    out = []
    for ts, entry in series:
        w = _longest(entry.get("windows"))
        if w is None or w.get("label") != label:
            continue
        out.append((ts, _num(w["used_percent"]), _num(w.get("reset_at"))))
    return out


def _segment(points):
    """The points after the window's last restart."""
    seg = []
    for p in points:
        if seg:
            last = seg[-1]
            moved = (p[2] is not None and last[2] is not None
                     and abs(p[2] - last[2]) > RESET_TOLERANCE_S)
            if moved or p[1] < last[1] - RESET_DROP_PCT:
                seg = []
        seg.append(p)
    return seg


def _slope(points):
    """Least-squares percent per hour, or None."""
    n = len(points)
    mt = sum(p[0] for p in points) / n
    mu = sum(p[1] for p in points) / n
    den = sum((p[0] - mt) ** 2 for p in points)
    if den <= 0:
        return None
    return sum((p[0] - mt) * (p[1] - mu) for p in points) / den * HOUR


def _account(entry, series, ledger, now, window_s):
    from . import accounts as accountsmod, codexresets
    label = entry.get("label")
    rec = {"account": accountsmod.mask_identity(label) or "?",
           "member": entry["member"], "proven": bool(entry.get("proven")),
           "plan": entry.get("plan"), "state": "unread", "why": None,
           "window": None, "used_pct": None, "pct_per_hour": None,
           "points": 0, "span_h": 0.0, "reset_at": None,
           "hours_to_reset": None, "hours_to_wall": None,
           "walls_before_reset": None, "strand_pct": None,
           "tokens_per_pct": None, "weight_source": None,
           "credits_held": (_balance(entry.get("reset_credits"))
                            or {}).get("available"),
           "reached_type": entry.get("reached_type"),
           "wall_kind": codexresets.wall_kind(entry.get("reached_type"))}
    newest = _longest(entry.get("windows"))
    if newest is None:
        rec["why"] = ("the newest pass could not read this account (%s)"
                      % (entry.get("state") or "no window"))
        return rec
    used, reset = _num(newest["used_percent"]), _num(newest.get("reset_at"))
    rec.update(window=newest.get("label"), used_pct=used, reset_at=reset,
               hours_to_reset=(max(0.0, (reset - now) / HOUR)
                               if reset is not None else None))
    seg = _segment(_points(series, newest.get("label")))
    recent = [p for p in seg if p[0] >= now - window_s]
    span = (recent[-1][0] - recent[0][0]) if recent else 0.0
    rec.update(points=len(recent), span_h=round(span / HOUR, 3))
    rate = None
    if len(recent) >= MIN_POINTS and span >= MIN_SPAN_S:
        rate = _slope(recent)
    if rate is None:
        rec["why"] = ("%d reading(s) over %.1fh in the last %gh; a rate needs "
                      "%d over at least %gh" % (len(recent), span / HOUR,
                                                window_s / HOUR, MIN_POINTS,
                                                MIN_SPAN_S / HOUR))
    else:
        rec["pct_per_hour"] = round(rate, 3)
    walled = used >= 100.0
    rec["state"] = ("walled" if walled else
                    "measured" if rate is not None else "unknown")
    hours_reset = rec["hours_to_reset"]
    if walled:
        rec.update(hours_to_wall=0.0, strand_pct=0.0,
                   walls_before_reset=hours_reset is not None)
    elif rate is not None:
        rec["hours_to_wall"] = (round((100.0 - used) / rate, 2)
                                if rate > 0 else None)
        if hours_reset is not None:
            rec["walls_before_reset"] = (rec["hours_to_wall"] is not None
                                         and rec["hours_to_wall"] < hours_reset)
            rec["strand_pct"] = round(max(
                0.0, 100.0 - used - max(rate, 0.0) * hours_reset), 1)
    first, last = (seg[0], seg[-1]) if seg else (None, None)
    if ledger is not None and first is not None:
        delta = last[1] - first[1]
        spent = ledger.on(label, first[0], last[0])
        if delta >= WEIGHT_MIN_DELTA_PCT and spent > 0:
            rec.update(tokens_per_pct=spent / delta, weight_source="measured")
    return rec


def _borrow_weights(accounts):
    """An account with no weight of its own takes the median measured weight
    of its own plan, and says so."""
    by_plan = {}
    for a in accounts:
        if a["weight_source"] == "measured":
            by_plan.setdefault(a["plan"], []).append(a["tokens_per_pct"])
    for a in accounts:
        mates = sorted(by_plan.get(a["plan"]) or ())
        if a["tokens_per_pct"] is None and mates and a["state"] != "unread":
            a.update(tokens_per_pct=mates[len(mates) // 2],
                     weight_source="same-plan")


def _is_pro(plan):
    from . import codexhomes
    return codexhomes.tier(plan) != codexhomes.tier("team")


def _fleet(accounts, newest, ledger, now, window_s, planned):
    from . import codexbudget
    fleet = {"state": "unknown", "why": None, "tokens_per_hour": None,
             "supply_tokens": None, "runway_h": None, "idle": False,
             "horizon_at": None, "horizon_h": None, "horizon_kind": None,
             "top_seats": [], "left_out": [], "accounts": len(accounts),
             "newest_pass_age_s": (round(now - newest["ts"])
                                   if newest else None)}
    if newest is None or not accounts:
        fleet["why"] = ("no codex budget history yet; the posting watchdog "
                        "pass records one line per pass")
        return fleet
    if now - newest["ts"] > codexbudget.GATE_MAX_AGE_S:
        fleet["why"] = ("the newest budget pass is %.1fh old, past the %dh "
                        "freshness bound" % ((now - newest["ts"]) / HOUR,
                                             codexbudget.GATE_MAX_AGE_S // 3600))
        return fleet
    if ledger is None:
        fleet["why"] = ("the proxy-usage ledger could not be read, so the "
                        "fleet's token rate is unknown")
        return fleet
    rate = ledger.per_hour(now - window_s, now)
    fleet["tokens_per_hour"] = rate
    fleet["top_seats"] = ledger.seats(now - TOP_SEATS_S, now)
    supply, left = 0.0, []
    for a in accounts:
        if not a["proven"]:
            left.append((a, "member not proven"))
        elif a["state"] == "unread":
            left.append((a, "unread"))
        elif a["used_pct"] < 100.0 and a["tokens_per_pct"] is None:
            left.append((a, "no tokens-per-percent weight"))
        elif a["used_pct"] < 100.0:
            supply += (100.0 - a["used_pct"]) * a["tokens_per_pct"]
    fleet["supply_tokens"] = supply
    fleet["left_out"] = [a["account"] for a, _why in left]
    if rate:
        fleet["runway_h"] = round(supply / rate, 2)
    else:
        fleet["idle"] = True
    resets = [(a["reset_at"], "pro-reset") for a in accounts
              if _is_pro(a["plan"]) and a["reset_at"] is not None
              and a["reset_at"] > now]
    resets += [(_num(p.get("at")), "credit") for p in planned or ()
               if isinstance(p, dict) and _num(p.get("at")) is not None
               and p["at"] > now and (_num(p.get("restores_tokens")) or 0)
               >= RESTORE_FRACTION * supply]
    if resets:
        at, kind = min(resets)
        fleet.update(horizon_at=at, horizon_h=round((at - now) / HOUR, 2),
                     horizon_kind=kind)
    fleet["state"] = "floor" if left else "measured"
    # THE WHY REACHES THE FOLD, so it names a plan and a reason, never an
    # account; the masked names ride `left_out` for the per-account surface.
    fleet["why"] = ("left out of the supply: %s" % "; ".join(
        "a %s account (%s)" % (a["plan"] or "?", why)
        for a, why in left)) if left else None
    return fleet


def _hours(h):
    if h is None:
        return "?"
    return "%.1fh" % h if h < 10 else "%.0fh" % h


# ------------------------------------------------------------------ credits

def _day(at):
    return time.strftime("%A", time.localtime(at)) if at is not None else "?"


def _worth(tokens, rate):
    """One account's week in the fleet's own hours, or in tokens when the
    fleet's rate is unknown or idle. An unknown week is never 0."""
    if tokens is None:
        return "an unknown amount"
    if rate:
        return "~" + _hours(tokens / rate)
    return "~%s tokens" % _tokens(tokens)


def _usable(a):
    """(True | None, why) — whether a credit held on this account is known to
    lift the wall it would be spent against. None is not known either way.
    A rate-limit wall and a workspace credits-depleted wall are both measured
    to lift (`codexresets.wall_kind`)."""
    from . import codexresets
    kind = a.get("wall_kind")
    if not a["proven"]:
        return None, ("not counted: its member is not proven, so it may be "
                      "a sibling's credit read twice")
    if kind == codexresets.WALL_UNRECOGNISED:
        return None, ("value unknown: the vendor calls its wall %r, which "
                      "helm does not recognise" % a.get("reached_type"))
    if kind is None and (a["used_pct"] or 0.0) >= 100.0:
        return None, ("value unknown: its week is spent and the vendor names "
                      "no reason, so a reset may not lift it")
    return True, None


def _credits(accounts, fleet):
    """The held reset credits, each valued against the horizon -> the fleet
    block's `credits`. See THE RESET CLOCK law."""
    rate = fleet.get("tokens_per_hour") or None
    horizon = fleet.get("horizon_at")
    pro_horizon = fleet.get("horizon_kind") == "pro-reset"
    name = ("%s's reset" % _day(horizon)) if pro_horizon and horizon \
        else "the horizon"
    per, unread, unknown = [], [], 0
    for a in accounts:
        if a["state"] == "unread":
            continue                    # the fleet's own why already says so
        pro = _is_pro(a["plan"])
        # A NON-PRO CREDIT IS NEVER AFTER THE HORIZON ONLY: its clock is
        # cheap (THE RESET CLOCK), so it counts wherever its reset lies.
        later = (not pro or horizon is None or a["reset_at"] is None
                 or a["reset_at"] > horizon + RESET_TOLERANCE_S)
        held = a["credits_held"]
        if held is None:
            unread.append(a)
            if later:
                unknown += 1            # a credit there could carry supply
            continue
        if held <= 0:
            continue
        week = 100.0 * a["tokens_per_pct"] if a["tokens_per_pct"] else None
        usable, why = _usable(a)
        if not later:
            own = pro_horizon and \
                abs(a["reset_at"] - horizon) <= RESET_TOLERANCE_S
            kind, value = ("horizon" if own else "early"), 0.0
            why = "it cannot help before %s without forfeiting %s; %s" % (
                name, "it" if own else "its own account's earlier %s reset"
                % _day(a["reset_at"]),
                "worth %s after" % _worth(week, rate) if usable else why)
        elif usable is None:
            kind, value = "unknown", None
        elif week is None:
            kind, value = "unknown", None
            why = ("value unknown: its account has no tokens-per-percent "
                   "weight, so its week is not known in tokens")
        elif not pro:
            kind, value = "counted", week
            why = ("counted: spent once its account is empty it adds a %s "
                   "week, %s, before the horizon; a smaller account's reset "
                   "clock costs little" % (_plan(a["plan"]),
                                           _worth(week, rate)))
        elif horizon is None:
            kind, value = "unknown", None
            why = "value before a horizon unknown: no Pro reset is known"
        elif a["reset_at"] is None:
            kind, value = "unknown", None
            why = "value unknown: its account's reset is unread"
        else:
            kind, value = "counted", week
            why = ("counted: its account resets after %s (%s), so spent once "
                   "that account is empty it adds %s before the horizon"
                   % (name, _day(a["reset_at"]), _worth(week, rate)))
        worth_h = (round(week / rate, 2)
                   if week is not None and rate else None)
        horizon_h = fleet.get("horizon_h")
        for n in range(held):
            item_kind, item_value, item_why = kind, value, why
            if item_value and n and horizon_h is not None:
                if worth_h is None:
                    item_kind, item_value = "unknown", None
                    item_why = ("value unknown: credit %d cannot be placed before "
                                "the horizon without a measured fleet burn rate"
                                % (n + 1))
                elif n * worth_h >= horizon_h:
                    item_kind, item_value = "after-horizon", 0.0
                    item_why = ("not counted: credit %d cannot be redeemed until "
                                "~%s, after the %s horizon" %
                                (n + 1, _hours(n * worth_h),
                                 _hours(horizon_h)))
            if item_value is None:
                unknown += 1
            per.append({"account": a["account"], "plan": a["plan"],
                        "kind": item_kind, "usable": usable,
                        "week_tokens": week, "value_tokens": item_value,
                        "worth_h": worth_h, "why": item_why})
    value = sum(p["value_tokens"] or 0.0 for p in per)
    supply = fleet.get("supply_tokens")
    return {"held": len(per),
            "usable": sum(1 for p in per if p["usable"]),
            "value_tokens": value, "unknown": unknown,
            "unread": [a["account"] for a in unread],
            "unread_plans": [a["plan"] for a in unread],
            "runway_h": (round((supply + value) / rate, 2)
                         if rate and supply is not None else None),
            "per": per}


def _plan(plan):
    return (plan or "?").capitalize()


def _credits_text(credits):
    """The credits in plain words, by plan and never by account: this reaches
    the fold. '' when nothing is held and no balance went unread."""
    groups, order = {}, []
    for p in credits["per"]:
        key = (p["plan"], p["why"])
        if key not in groups:
            groups[key] = 0
            order.append(key)
        groups[key] += 1
    out = []
    for key in order:
        plan, why = key
        n = groups[key]
        out.append("+%d %s reset credit%s held%s: %s"
                   % (n, _plan(plan), "s"[:n != 1], "" if n == 1 else ", each",
                      why))
    plans = {}
    for plan in credits.get("unread_plans") or ():
        plans[plan] = plans.get(plan, 0) + 1
    for plan, n in sorted(plans.items(), key=lambda kv: str(kv[0])):
        out.append("reset-credit balance unread on %d %s account%s"
                   % (n, _plan(plan), "s"[:n != 1]))
    return ". ".join(out)


def _tokens(n):
    if n is None:
        return "?"
    if n >= 1e9:
        return "%.1fB" % (n / 1e9)
    if n >= 1e6:
        return "%.0fM" % (n / 1e6)
    return "%.0fk" % (n / 1e3)


def _local(at):
    return time.strftime("%a %m-%d %H:%M %Z", time.localtime(at)) \
        if at is not None else "unknown"


def _seats_text(seats):
    return ", ".join("%s %.0f%%" % (s, share * 100) for s, share in seats) \
        or "none in the ledger"


def _verdict(fleet, accounts, window_h):
    """(verdict, cause). The cause ends with the held reset credits in plain
    words, whatever the verdict, so a reader never has to guess whether the
    runway counted them."""
    verdict, cause = _runway_verdict(fleet, accounts, window_h)
    text = _credits_text(fleet["credits"])
    return verdict, cause + (". " + text if text else "")


def _runway_verdict(fleet, accounts, window_h):
    short, use_it, even, unknown = _verdicts()
    if fleet["state"] != "measured":
        return unknown, "codex runway not stated: %s" % fleet["why"]
    credits = fleet["credits"]
    runway = fleet["runway_h"]
    said = "idle, no codex tokens spent" if runway is None else _hours(runway)
    counted = [p for p in credits["per"] if p["kind"] == "counted"]
    if runway is not None and counted:
        # THE RUNWAY THE VERDICT READS COUNTS EVERY CREDIT THAT LANDS SUPPLY
        # BEFORE THE HORIZON, and says how much of it is credit.
        said = "%s (%s of supply + %s from %d reset credit%s)" % (
            _hours(credits["runway_h"]), _hours(runway),
            _hours(credits["runway_h"] - runway), len(counted),
            "s"[:len(counted) != 1])
        runway = credits["runway_h"]
    if fleet["horizon_h"] is None:
        return unknown, ("codex runway %s, and no Pro account's reset is "
                         "known, so there is no horizon to set it against"
                         % said)
    horizon = fleet["horizon_h"]
    name = ("the next Pro reset" if fleet["horizon_kind"] == "pro-reset"
            else "a planned reset credit")
    rate = "%s tokens/h over the last %gh" % (
        _tokens(fleet["tokens_per_hour"]), window_h)
    if runway is not None and runway < horizon and credits["unknown"]:
        # A CREDIT OF UNKNOWN WORTH MAKES THE SUPPLY A FLOOR, and a floor
        # proves no shortage: it could carry the fleet to the horizon.
        return unknown, ("codex runway %s is under the %s horizon (%s, %s) at "
                         "%s, but what %d reset credit%s could add before it "
                         "is not known, so the shortage is not proven"
                         % (said, _hours(horizon), name,
                            _local(fleet["horizon_at"]), rate,
                            credits["unknown"],
                            "s"[:credits["unknown"] != 1]))
    if runway is not None and runway < horizon:
        return short, ("codex runway %s is under the %s horizon (%s, %s) at "
                       "%s; top seats over %dh: %s"
                       % (said, _hours(horizon), name,
                          _local(fleet["horizon_at"]), rate,
                          TOP_SEATS_S // 3600,
                          _seats_text(fleet["top_seats"])))
    stranders = [a for a in accounts if a["strand_pct"] is not None
                 and a["strand_pct"] >= STRAND_PCT]
    if stranders and (runway is None or runway >= WELL_ABOVE * horizon):
        return use_it, ("codex runway %s is at least %gx the %s horizon (%s), "
                        "and %s project to strand at least %.0f%% of the week "
                        "at their own reset; use it"
                        % (said, WELL_ABOVE, _hours(horizon), name,
                           ", ".join("a %s account %.0f%%"
                                     % (a["plan"] or "?", a["strand_pct"])
                                     for a in stranders), STRAND_PCT))
    return even, ("codex runway %s against the %s horizon (%s, %s) at %s"
                  % (said, _hours(horizon), name, _local(fleet["horizon_at"]),
                     rate))


def read(lines, requests, now, window_h=WINDOW_H, planned=()):
    """History lines + ledger requests -> the reading. PURE: no file and no
    network. `requests` None is a ledger that could not be read, which is not
    the empty ledger `[]`."""
    window_s = window_h * HOUR
    lines = sorted((line for line in lines or ()
                    if isinstance(line, dict)
                    and _num(line.get("ts")) is not None),
                   key=lambda line: line["ts"])
    series = {}
    for line in lines:
        for entry in line.get("accounts") or ():
            if isinstance(entry, dict) and entry.get("member"):
                series.setdefault(entry["member"], []).append(
                    (line["ts"], entry))
    newest = lines[-1] if lines else None
    ledger = _Ledger(requests) if requests is not None else None
    accounts = [_account(entry, series[entry["member"]], ledger, now,
                         window_s)
                for entry in (newest or {}).get("accounts") or ()
                if isinstance(entry, dict) and entry.get("member")]
    _borrow_weights(accounts)
    fleet = _fleet(accounts, newest, ledger, now, window_s, planned)
    fleet["credits"] = _credits(accounts, fleet)
    verdict, cause = _verdict(fleet, accounts, window_h)
    return {"v": V, "ts": now, "window_h": window_h, "passes": len(lines),
            "accounts": accounts, "fleet": fleet, "verdict": verdict,
            "cause": cause}


def fold_input(reading):
    """The burn-flag fold's runway input: the verdict, the cause and numbers.
    No account identity, by construction."""
    fleet = (reading or {}).get("fleet") or {}
    return {"verdict": (reading or {}).get("verdict"),
            "cause": (reading or {}).get("cause"),
            "state": fleet.get("state"),
            "measured_at": (reading or {}).get("ts"),
            "runway_h": fleet.get("runway_h"),
            "horizon_h": fleet.get("horizon_h"),
            "horizon_at": fleet.get("horizon_at"),
            "tokens_per_hour": fleet.get("tokens_per_hour"),
            "top_seats": [list(s) for s in fleet.get("top_seats") or ()],
            # NUMBERS ONLY: the per-credit rows name accounts and stay in the
            # snapshot.
            "credits": {k: (fleet.get("credits") or {}).get(k)
                        for k in ("held", "usable", "value_tokens", "unknown",
                                  "runway_h")}}


# ------------------------------------------------------------------ the walls

def wall_events(rows, reading, requests, now):
    """One event per account whose LONGEST window is spent this pass.

    A five-hour wall alone is not a wall event: it lifts inside the session
    window, and the per-seat pool wall (`poolwall`) already speaks for it."""
    from . import codexresets
    rows = list(rows or ())
    ledger = _Ledger(requests or ())
    windows = [(row, _longest(row.get("windows"))) for row in rows]
    still_open = sum(1 for _row, w in windows
                     if w is not None and _num(w["used_percent"]) < 100.0)
    runway = (reading or {}).get("cause")
    events = []
    for row, w in windows:
        if w is None or _num(w["used_percent"]) < 100.0:
            continue
        key, _proven = member_key(row)
        if key is None:
            continue
        label = codexresets.label(row)
        reset = _num(w.get("reset_at"))
        body = ("codex WALL: %s spent its %s window (%.0f%%; resets %s, in "
                "%s). Seats on it over the last %dh: %s. %d of %d accounts "
                "still open.%s"
                % (label, w.get("label") or "longest",
                   _num(w["used_percent"]), _local(reset),
                   _hours(max(0.0, (reset - now) / HOUR))
                   if reset is not None else "?",
                   TOP_SEATS_S // 3600,
                   _seats_text(ledger.seats(now - TOP_SEATS_S, now, label)),
                   still_open, len(rows),
                   (" " + runway[:1].upper() + runway[1:] + ".")
                   if runway else ""))
        events.append({"member": key, "reset_at": reset, "body": body})
    return events


def _same_wall(rec, event):
    a, b = _num(rec.get("reset_at")), _num(event.get("reset_at"))
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= RESET_TOLERANCE_S


def _post(body, post):
    if post is None:
        from . import chat
        chat.post(body, who=WHO, room=ROOM)
    else:
        post(body)


def _push(body, push, member):
    if push is None:
        from . import notify
        push = notify.owner_push
    return bool(push(body, title="helm codex wall",
                     receipt=("codexpace.notify_failed", member[:12])))


def announce(events, now=None, post=None, push=None, path=None):
    """Claim each NEW wall, then deliver every channel still owed -> the
    bodies that reached the room on this call.

    ONE CRITICAL SECTION: the claim, the delivery and the acknowledgement all
    run under the ledger's lock, so two observers of one wall cannot both
    deliver it. Never raises for a channel: a failed post or push stays owed
    and is the only thing the next call retries."""
    now = time.time() if now is None else now
    target = path or walls_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    delivered = []
    with open(target + ".lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            ledger = pk.read_json(target, default={})
            ledger = ledger if isinstance(ledger, dict) else {}
            for member in list(ledger):
                rec = ledger[member]
                if not isinstance(rec, dict) or \
                        (_num(rec.get("reset_at")) or 0) < now - WALL_KEEP_S:
                    ledger.pop(member)
            for event in events or ():
                rec = ledger.get(event["member"])
                if rec and _same_wall(rec, event):
                    continue
                ledger[event["member"]] = {
                    "reset_at": event["reset_at"], "claimed_at": now,
                    "body": event["body"], "chat": False, "push": False}
            pk.write_json(target, ledger)
            for member in sorted(ledger):
                rec = ledger[member]
                if not rec.get("chat"):
                    try:
                        _post(rec["body"], post)
                        rec["chat"] = True
                        delivered.append(rec["body"])
                    except Exception:           # noqa: BLE001 — owed, retried
                        pass
                if not rec.get("push"):
                    try:
                        rec["push"] = _push(rec["body"], push, member)
                    except Exception:           # noqa: BLE001 — owed, retried
                        pass
            pk.write_json(target, ledger)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return delivered


# ------------------------------------------------------------------ the pass

def write_snapshot(reading, path=None):
    """Persist the reading for `helm burn` and the fold -> True/False. Best
    effort and never raises."""
    try:
        pk.atomic_write(path or snapshot_path(),
                        json.dumps(reading, sort_keys=True))
        return True
    except Exception:                           # noqa: BLE001
        return False


def cached_reading(now=None, max_age=None, path=None):
    """(reading, age_s) from the snapshot, or (None, None) when it is absent,
    unreadable or older than the burn flags' own freshness bound."""
    from . import burnflags
    now = time.time() if now is None else now
    bound = burnflags.max_age_s() if max_age is None else max_age
    payload = pk.read_json(path or snapshot_path(), default=None)
    if not isinstance(payload, dict) or payload.get("v") != V \
            or _num(payload.get("ts")) is None:
        return None, None
    age = now - payload["ts"]
    if age < 0 or age > bound:
        return None, None
    return payload, age


def cached_fold_input(now=None, max_age=None):
    reading, _age = cached_reading(now=now, max_age=max_age)
    return fold_input(reading) if reading else None


def watch_pass(rows, now=None):
    """The watchdog pass's rung -> the fold input, or None when the pass did
    not read the pool. It records the history line, reads the ledger the
    meter just made durable, persists the reading and speaks for a new wall."""
    if rows is None:
        return None
    now = time.time() if now is None else now
    append_history(rows, now)
    lines, _err = read_history(now=now)
    requests = ledger_requests(now)
    reading = read(lines, requests, now)
    write_snapshot(reading)
    # THE SAME LEDGER READ, EVERY FAMILY: the per-seat burn a project's share
    # is measured against (task/3156). Best effort, like the snapshot above.
    write_seat_burn(seat_burn(requests, now))
    try:
        announce(wall_events(rows, reading, requests, now), now=now)
    except Exception:                           # noqa: BLE001 — never the pass
        pass
    return fold_input(reading)


def live_reading(now=None, window_h=WINDOW_H):
    now = time.time() if now is None else now
    lines, err = read_history(now=now)
    # THE LEDGER IS READ WHOLE (seconds on a busy host), so a history with
    # nothing to weigh does not pay for it.
    reading = read(lines, ledger_requests(now) if lines else [], now,
                   window_h=window_h)
    if err:
        reading["fleet"]["why"] = err
    return reading


# ------------------------------------------------------------------ surfaces

def _ago(seconds):
    return "%ds" % seconds if seconds < 120 else "%dm" % (seconds // 60)


def burn_line(now=None):
    """The ONE line `helm burn` prints about codex pace. Never raises."""
    try:
        now = time.time() if now is None else now
        reading, age = cached_reading(now=now)
        if reading is None:
            return ("  codex runway: not measured — the posting watchdog pass "
                    "writes it (`helm proxywatch --post`); "
                    "`helm burn runway` reads it live")
        return "  codex runway [%s, read %s ago]: %s" % (
            reading.get("verdict"), _ago(age), reading.get("cause"))
    except Exception as exc:                    # noqa: BLE001
        return "  codex runway: unreadable (%s)" % type(exc).__name__


def render(reading, now=None):
    """The per-account table and the fleet lines for `helm burn runway`."""
    fleet = reading["fleet"]
    out = ["codex pace over the last %gh (%d budget pass(es) read)"
           % (reading["window_h"], reading.get("passes") or 0),
           "  %-26s %-5s %-4s %5s %7s %8s %8s %7s %-14s %s"
           % ("account", "plan", "win", "used", "%/h", "to wall", "to reset",
              "strands", "tokens/%", "state")]
    for a in reading["accounts"]:
        def fmt(value, spec):
            return "?" if value is None else spec % value
        out.append("  %-26s %-5s %-4s %5s %7s %8s %8s %7s %-14s %s%s"
                   % (a["account"][:26], (a["plan"] or "?")[:5],
                      a["window"] or "?", fmt(a["used_pct"], "%.0f%%"),
                      fmt(a["pct_per_hour"], "%.2f"),
                      _hours(a["hours_to_wall"]), _hours(a["hours_to_reset"]),
                      fmt(a["strand_pct"], "%.0f%%"),
                      ("%s (%s)" % (_tokens(a["tokens_per_pct"]),
                                    "own" if a["weight_source"] == "measured"
                                    else "plan")
                       if a["tokens_per_pct"] else "?"),
                      a["state"], "" if a["proven"] else " (member not proven)"))
        if a["why"] and a["state"] != "measured":
            out.append("      %s" % a["why"])
    out.append("fleet: %s; %s tokens/h over %gh; supply %s tokens; runway %s; "
               "horizon %s (%s)"
               % (fleet["state"], _tokens(fleet["tokens_per_hour"]),
                  reading["window_h"], _tokens(fleet["supply_tokens"]),
                  "idle" if fleet["idle"] else _hours(fleet["runway_h"]),
                  _hours(fleet["horizon_h"]), _local(fleet["horizon_at"])))
    if fleet.get("why"):
        out.append("  %s" % fleet["why"])
    credits = fleet.get("credits") or {}
    if credits.get("per") or credits.get("unread"):
        out.append("reset credits: %d held, %d usable, %s tokens counted "
                   "before the horizon (runway with them %s)%s"
                   % (credits["held"], credits["usable"],
                      _tokens(credits["value_tokens"]),
                      _hours(credits["runway_h"]),
                      "; %d of unknown worth" % credits["unknown"]
                      if credits["unknown"] else ""))
        for p in credits.get("per") or ():
            out.append("  %-26s %-5s %s" % (p["account"][:26],
                                             (p["plan"] or "?")[:5], p["why"]))
        for name in credits.get("unread") or ():
            out.append("  %-26s %-5s the reset-credit balance was not read"
                       % (name[:26], ""))
    out.append("verdict %s: %s" % (reading["verdict"], reading["cause"]))
    return out
