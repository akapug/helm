#!/usr/bin/env python3
"""helm codex pace calibration — backtest the runway model against its own
history (task/3447).

REPLAY, not a fresh model: for each past history line we replay the same
arithmetics `codexpace.read` would use at that line's `ts`, and compare the
model's predicted wall/dry time against the wall the LATER lines show (the
Pro reset it rides against, or the spent window the line itself records).
Bias (signed, mean) and median abs error per trailing window (1/3/6/12/24h);
the best window is the lowest median-abs with n >= MIN_N, ties to the
shorter window (less history, more conservative). The live `helm burn runway`
and the web credit page then render `runway Xh, backtest +/-Yh, n=Z`, or
UNCALIBRATED when n is too small.

READ-ONLY on the ledgers: reads the history file under the same eventledger
contract as codexpace, and never writes. The live fleet rate comes from the
proxy-usage ledger via `codexpace.live_reading`; the backtest's per-account
rate is the account's own least-squares pace (the reading stores no fleet
rate), and its fleet number uses the median measured weight as a rate — an
approximation, flagged where it renders.

The history file format (v1) is codexpace._state/codex-budget-history.jsonl:
  {v: 1, id: "codex-budget:%.3f", ts: <epoch>,
   accounts: [{member, proven, label, plan, state, reached_type,
               reset_credits, windows: [{label, seconds, used_percent,
               reset_at}]}]}
The model's predicted wall for an account at reading ts:
  if Pro reset is ahead: min(used_pct->100 straight-line, reset - ts)
  else: (100 - used_pct) / rate, rate = least-squares pct per hour
The actual wall shown by later history is the same reset (if it passed) or
the spent-window instant. Bias = mean(predicted - actual); positive =
over-predicted runway.
"""
import json
import os
import statistics
import time

from . import codexpace

# Trailing windows to consider; WINDOW_H (6.0) is among them so the best
# window can be the live one.
WINDOW_HOURS = (1.0, 3.0, 6.0, 12.0, 24.0)
# Fewer predictions than this and the window is UNCALIBRATED: the band is
# noise. Chosen so an 84h history has at least one window with n >= 8.
MIN_N = 8
HOUR = codexpace.HOUR
_MIN_POINTS = codexpace.MIN_POINTS
_MIN_SPAN_S = codexpace.MIN_SPAN_S
RESET_TOLERANCE_S = codexpace.RESET_TOLERANCE_S


def read_history(now=None, path=None):
    """(lines inside one weekly window, oldest first, None) — the same read
    codexpace.read uses. Read-only; returns [] when the history file is
    missing or unreadable (graceful, not a traceback)."""
    return codexpace.read_history(now=now, path=path)


def anthropic_history_path():
    """The anthropic usage history path, the same file the native provider
    appends to (~/.cache/helm/native-usage-history.jsonl). Read-only."""
    return os.path.expanduser("~/.cache/helm/native-usage-history.jsonl")


def read_anthropic_history(hours=168, path=None):
    """(rows inside `hours`, oldest first, None) — the same read the
    provider's own `history()` uses, but returning the raw flat rows (not
    downsampled) so the converter can fold them. Read-only; returns [] when
    the history file is missing or unreadable (graceful, not a traceback)."""
    now = time.time() if now is None else now
    cutoff = now - hours * HOUR
    rows = []
    path = path or anthropic_history_path()
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or r.get("provider") != "anthropic":
                    continue
                probed_at = r.get("probed_at", "")
                ts = _probed_at_epoch(probed_at)
                if ts is None or ts < cutoff:
                    continue
                if r.get("gauges"):
                    rows.append(r)
    except OSError:
        pass
    rows.sort(key=lambda r: _probed_at_epoch(r.get("probed_at")) or 0)
    return rows, None


def _probed_at_epoch(s):
    """True epoch seconds for a provider probed_at (RFC3339 UTC '...Z'; a naive
    local string still parses). A parse failure returns None."""
    import calendar
    if not isinstance(s, str):
        return None
    try:
        t = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return calendar.timegm(t) if "Z" in s[19:] or "+00:00" in s[19:] \
        else time.mktime(t)


def _fold_gauges(row, ts):
    """One flat anthropic row -> one nested account entry, the same shape
    `codexpace.history_line` writes for codex: member, proven, label,
    plan, state, reached_type, reset_credits, windows."""
    account = row.get("account") or "(anonymous)"
    state = row.get("status") or "unknown"
    proven = state in ("allowed", "due-refresh", "expired-token",
                       "reauth-needed", "api-error", "network-error")
    plan = row.get("plan") or "unknown"
    reached_type = row.get("reached_type")
    reset_credits = None
    windows = []
    for g in row.get("gauges") or ():
        if not isinstance(g, dict):
            continue
        label = g.get("label")
        if not label:
            continue
        windows.append({
            "label": label,
            "seconds": None,
            "used_percent": _anth_num(g.get("utilization")),
            "reset_at": g.get("reset"),
        })
    # member: the account handle is the member key for anthropic (no account_id
    # or user_id to hash). The hash is the account string itself, so the
    # member is stable across probe cycles.
    return {
        "member": account,
        "proven": proven,
        "label": account,
        "plan": plan,
        "state": state,
        "reached_type": reached_type,
        "reset_credits": reset_credits,
        "windows": windows,
    }


def _anth_num(value):
    """A finite number, or None. A bool is not a percentage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") \
        else None


def anthropic_history_line(rows, ts, path=None):
    """One probe cycle's flat anthropic rows -> one nested history line, the
    same shape `codexpace.history_line` writes for codex. The provider's
    `_append_history` already wrote the flat row; this folds it into the
    nested shape so the same backtest machinery can replay it."""
    accounts = [_fold_gauges(r, ts) for r in rows]
    accounts = [a for a in accounts if a.get("windows")]
    if not accounts:
        return None
    line = {"v": 1, "id": "anthropic-budget:%.3f" % ts, "ts": ts,
            "accounts": accounts}
    return line


def append_anthropic_history(rows, now=None, path=None):
    """Fold the provider's flat probe rows into the nested shape and append
    one line to the anthropic history file. `rows` is the list the provider
    returns from `history()` for a single probe cycle (or the whole list if
    called from the watch pass). Best effort: never raises. The flat file is
    NOT modified; a SEPARATE file is written so the two can coexist.

    Returns True when the line landed, False otherwise.
    """
    now = now if now is not None else time.time()
    # The provider's flat rows already carry a probed_at; we need the ts in
    # epoch. The `now` is the cycle's epoch (the provider's history() is called
    # with now=time.time()). Use now as the line's ts.
    line = anthropic_history_line(rows, now)
    if line is None:
        return False
    # The nested anthropic history file: same dir, different name. This is a
    # NEW file so the two (flat + nested) can coexist during the transition.
    target = path or os.path.join(
        os.path.dirname(anthropic_history_path()),
        "anthropic-budget-history.jsonl")
    from . import eventledger
    with eventledger.locked(target) as held:
        if not held:
            return False
        try:
            # opportunistic prune: keep the file bounded (~30 days is plenty)
            if os.path.exists(target):
                try:
                    if os.path.getsize(target) > 32 * 1024 * 2:
                        cutoff = now - 30 * 86400
                        with open(target) as f:
                            keep = [l for l in f
                                   if _anth_num(json.loads(l).get("ts")) >= cutoff]
                        tmp = target + ".tmp"
                        with open(tmp, "w") as f:
                            f.writelines(
                                [json.dumps(r, separators=(",", ":")) + "\n"
                                 for r in keep])
                        os.replace(tmp, target)
                except OSError:
                    pass
            with open(target, "a") as f:
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
            return True
        except OSError:
            return False


def _member_series(lines, member):
    """(ts, entry) oldest first, only the rows carrying `member`."""
    out = []
    for line in lines or ():
        if not isinstance(line, dict) or not _num(line.get("ts")):
            continue
        for entry in line.get("accounts") or ():
            if isinstance(entry, dict) and entry.get("member") == member:
                out.append((line["ts"], entry))
    return out


def _points_for_member(series_member, label):
    """(ts, used, reset_at) oldest first, on the longest window matching `label`."""
    out = []
    for ts, entry in series_member or ():
        w = codexpace._longest(entry.get("windows"))
        if w is None or w.get("label") != label:
            continue
        used = codexpace._num(w.get("used_percent"))
        if used is None:
            continue
        out.append((ts, used, codexpace._num(w.get("reset_at"))))
    return out


def _predict(entry, pred_ts, series_member):
    """The model's predicted wall (hours) for this entry at `pred_ts`.
    Mirrors codexpace._account's rate + hours_to_wall logic. None if the
    model cannot predict (fewer than MIN_POINTS, flat, walled, no rate)."""
    newest = codexpace._longest(entry.get("windows"))
    if newest is None:
        return None
    used = codexpace._num(newest.get("used_percent"))
    reset_at = codexpace._num(newest.get("reset_at"))
    label = newest.get("label")
    if used is None:
        return None
    if used >= 100.0:
        return 0.0
    pts = _points_for_member(series_member, label)
    # the rate must be computed from points ending at or before pred_ts
    pts = [p for p in pts if p[0] <= pred_ts]
    # segment by Pro-reset jumps so the sawtooth (7d window resetting) is
    # not averaged into a near-zero rate. Mirror codexpace._segment.
    pts = codexpace._segment(pts)
    if len(pts) < _MIN_POINTS:
        return None
    span = pts[-1][0] - pts[0][0]
    if span < _MIN_SPAN_S:
        return None
    mt = sum(p[0] for p in pts) / len(pts)
    mu = sum(p[1] for p in pts) / len(pts)
    den = sum((p[0] - mt) ** 2 for p in pts)
    if den <= 0:
        return None
    rate = sum((p[0] - mt) * (p[1] - mu) for p in pts) / den * HOUR
    if rate is None or rate <= 0:
        return None
    wall = (100.0 - used) / rate
    if reset_at is not None and reset_at > pred_ts:
        return min(wall, (reset_at - pred_ts) / HOUR)
    return wall


def _actual_wall(entry, series_member, pred_ts):
    """What the later lines actually show for this member at the prediction
    instant, scored against the model's predicted wall. Returns:

    * ``{"wall_h": h}`` when a real wall is observed — the first later line
      in the same reset segment (``ts < reset_at``) whose ``used_percent >=
      100``. ``h`` is its time from ``pred_ts``. This is the only number that
      can feed a real measured error.
    * ``{"censored": True, "time_to_reset_h": h}`` when the reset arrives first
      (no 100% line before ``reset_at``) — the account lasted to the reset.
      The caller classifies hit/miss against the predicted wall: hit when the
      prediction is at least the time-to-reset, a miss otherwise. Never zero.
    * ``None`` when there is no later line to observe against.

    This is the measurement the old zero-error bug hid: a real wall is the
    first later line that hit the cap, and a reset-first outcome is censored,
    not a free 0.0."""
    reset_at = codexpace._num(codexpace._longest(entry.get("windows")).get("reset_at"))
    later = [(ts, e) for ts, e in series_member or () if ts > pred_ts]
    if not later:
        return None
    for ts, e in later:
        newest = codexpace._longest(e.get("windows"))
        if newest is None:
            continue
        used = codexpace._num(newest.get("used_percent"))
        if used is None or used >= 100.0:
            # a wall, or a post-reset row with no percent — this segment's
            # real wall (or the segment ended); stop looking
            if used is not None:
                if reset_at is None or ts < reset_at + RESET_TOLERANCE_S:
                    return {"wall_h": (ts - pred_ts) / HOUR}
                return None  # 100% only appeared after this segment reset
            return None
    if reset_at is None:
        return None
    # no 100% line before the reset: the reset is what the account lasted to
    return {"censored": True, "time_to_reset_h": (reset_at - pred_ts) / HOUR}
def _num(value):
    """A finite number, or None. A bool is not a percentage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") \
        else None


def calibrate(now=None):
    """Backtest the runway model against codex history. Returns a dict:
      {
        'lines': int,                # history lines used
        'best': str or None,         # best window label (str) or None
        'best_window_h': float or None,
        'windows': {label: {'bias_h': float or None,
                             'median_abs_h': float or None,
                             'n': int, 'censored_n': int,
                             'censored_hits': int, 'censored_misses': int}},
        'uncalibrated': bool,        # True if no window had n >= MIN_N
      }
    Each window's n is the number of predictions with a REAL observed wall
    (a later line that actually hit 100%%); censored outcomes (the reset
    arrived before a wall, so the account lasted to the reset) are counted
    separately, hit when the model's predicted wall met the time-to-reset,
    miss otherwise. A window with no real walls carries bias_h and
    median_abs_h as None (no real-wall errors to average); only windows
    with n >= MIN_N are candidates for `best`, which picks the lowest
    median_abs_h among them (ties to the shorter window). Read-only; a
    missing/unreadable history returns an empty result (best=None,
    uncalibrated=True) rather than raising.
    """
    now = time.time() if now is None else now
    lines, _err = codexpace.read_history(now=now)
    if not lines:
        return {"lines": 0, "best": None, "best_window_h": None,
                "windows": {}, "uncalibrated": True}

    # Build per-member series: member -> list of (ts, entry)
    series = {}
    for line in lines or ():
        if not isinstance(line, dict) or not _num(line.get("ts")):
            continue
        for entry in line.get("accounts") or ():
            if isinstance(entry, dict) and entry.get("member"):
                series.setdefault(entry["member"], []).append(
                    (line["ts"], entry))

    predictions = []
    censored = []
    for line in lines or ():
        pred_ts = _num(line.get("ts"))
        if pred_ts is None:
            continue
        for entry in line.get("accounts") or ():
            member = entry.get("member")
            if not member:
                continue
            member_series = series.get(member) or []
            pred = _predict(entry, pred_ts, member_series)
            if pred is None:
                continue
            # (c) a prediction made on a window that has already walled
            # has no future wall to observe; skip it rather than score
            # the model against itself.
            newest = codexpace._longest(entry.get("windows"))
            used = codexpace._num(newest.get("used_percent")) if newest else None
            if used is not None and used >= 100.0:
                continue
            actual = _actual_wall(entry, member_series, pred_ts)
            if actual is None:
                continue
            if actual.get("wall_h") is not None:
                predictions.append((pred_ts, pred, actual["wall_h"]))
            else:
                # censored: reset arrived first. classify hit/miss against the
                # model's predicted wall; never a free zero.
                censored.append((pred_ts, pred, actual["time_to_reset_h"]))

    windows = {}
    for w_h in WINDOW_HOURS:
        cutoff = now - w_h * HOUR
        sample = [(pred, a) for p, pred, a in predictions if p >= cutoff]
        cenc = [(p, ttr) for p, pred, ttr in censored if p >= cutoff]
        if len(sample) < MIN_N and not cenc:
            continue
        errors = [pred - actual for pred, actual in sample]
        c_hits = sum(1 for p, pred, ttr in censored if p >= cutoff and pred >= ttr - 1e-9)
        c_miss = len(cenc) - c_hits
        win = {
            "bias_h": round(statistics.mean(errors), 3) if errors else None,
            "median_abs_h": round(statistics.median([abs(e) for e in errors]), 3) if errors else None,
            "n": len(sample),
            "censored_n": len(cenc),
            "censored_hits": c_hits,
            "censored_misses": c_miss,
        }
        windows["%.1f" % w_h] = win

    # A window is only calibrated when it carries enough real walls; censored-only
    # windows (reset-ride history) carry counts but stay uncalibrated.
    calibrated = [wh for wh in WINDOW_HOURS
                  if windows.get("%.1f" % wh, {}).get("n", 0) >= MIN_N]
    if not windows or not calibrated:
        return {"lines": len(lines), "best": None, "best_window_h": None,
                "windows": windows, "uncalibrated": True}

    # best = lowest median_abs among calibrated windows only (n >= MIN_N),
    # ties to the shorter window (less history, more conservative).
    best_wh = min(calibrated,
                  key=lambda wh: (windows["%.1f" % wh]["median_abs_h"], wh))
    return {
        "lines": len(lines),
        "best": "%.1f" % best_wh,
        "best_window_h": best_wh,
        "windows": windows,
        "uncalibrated": False,
    }


def render(result):
    """Render the calibration result as plain-text lines.
    `result` is the dict returned by `calibrate()`.
    """
    lines = []
    if result.get("uncalibrated") or not result.get("best"):
        lines.append("runway backtest: UNCALIBRATED ({} history line(s), "
                     "no window with n >= {})".format(
            result.get("lines", 0), MIN_N))
        return lines
    best = result.get("best")
    wh = float(best)
    bw = result.get("windows") or {}
    bw_d = bw.get(best) or {}
    med = bw_d.get("median_abs_h")
    bias = bw_d.get("bias_h")
    n = bw_d.get("n", 0)
    lines.append(
        "runway backtest: median {:+.2f}h, bias {:+.2f}h, n={}".format(
            med or 0.0, bias or 0.0, n)
        + " (best {0}h window)".format(wh))
    cenc = bw_d.get("censored_n") or 0
    if cenc:
        lines.append(
            "backtest censored: {0} reset-before-wall reading(s), "
            "{1} hit / {2} miss against the runway".format(
                cenc, bw_d.get("censored_hits", 0),
                bw_d.get("censored_misses", 0)))
    return lines


# The calibration is read-only; it never touches the ledger or the state dir
# except to read history lines. It does NOT write any snapshot to disk.
