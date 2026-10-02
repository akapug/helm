"""helm.burnspend — one seat's own spend, joined to its account's weekly %.

task/4015: `helm burn spend` reports, for the seat's OWN sessions, the calls
they made and the context they re-read versus output, deduped by message.id
and priced per model, beside the weekly percent of the account the seat's own
home holds (helm's EXISTING claudepace snapshot), over that account's current
weekly window.

The verb EXTENDS the existing `helm burn` surface (the owner's scope line:
Orca feeds helm's existing quota surfaces, not a replacement meter), so it is a
`spend` subcommand in burnflags, not a new verb family. The spend is read from
the Claude Code session logs the harness already writes under the seat's own
config home (`<home>/projects/<project>/<session>.jsonl`, plus that session's
`<session>/subagents/` transcripts).

The design contract (from the task note, agreed in meta):

- THE SEAT'S OWN SESSIONS, never the project dir. Every helm home links its
  `projects/` into one shared store, so a cwd's project dir holds other seats',
  the owner's and other accounts' sessions. The verb reads the session ids it
  is given (the running session's own id by default) and nothing else.
- THE ACCOUNT IS THE SEAT'S HOME'S. CLAUDE_CONFIG_DIR, else the default home,
  keyed exactly as claudepace keys its snapshot (`claudepace._account_key`).
- THE WINDOW IS THE ACCOUNT'S WEEK: from its measured weekly reset minus seven
  days; with no measured reset, the last seven days, and the output says so.
  A message is in the window by its own `timestamp`; one with no readable
  timestamp is counted apart, never folded in.
- DEDUPE by message.id, never by api block: the first record for an id wins.
- PRICE per model, from the model ids helm catalogs
  (seat_catalog.CC_AGENT_FRONTMATTER_MODELS). Cache read: Opus 5.5 0.05x,
  Fable 5.1 0.025x, every other catalogued model 0.1x. Cache write: 1.25x for
  the 5-minute window, 2x for the 1-hour one. A model outside the catalog is
  UNKNOWN: listed, and left out of every equivalent, never guessed.
- UNKNOWN IS UNKNOWN, NOT ZERO. A cache write whose window the log does not
  split is its own bucket with an UNKNOWN equivalent; a seat with no session
  id or no log says why rather than claiming it spent nothing.
- NEVER print a Max savings %. The verb STATES the confounds (shared accounts,
  rounding, resets).
"""
import glob
import json
import os
import time

OPUS_55 = "claude-opus-5-5"
FABLE_51 = "claude-fable-5-1"
# The cache-read multiple of a model's own input price, for the models the
# task names; every other catalogued model is OTHER_MODEL_CACHE_READ_MULTIPLE.
CACHE_READ_MULTIPLE = {OPUS_55: 0.05, FABLE_51: 0.025}
OTHER_MODEL_CACHE_READ_MULTIPLE = 0.10
CACHE_WRITE_5M_MULTIPLE = 1.25
CACHE_WRITE_1H_MULTIPLE = 2.0
WEEK_S = 7 * 86400


def known_models():
    """The Claude model ids helm catalogs: the only ones priced."""
    from . import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
    return frozenset(seat_catalog.CC_AGENT_FRONTMATTER_MODELS)


def model_id(name):
    """A transcript's model name as a catalog id: `claude-opus-5-5[1m]` is
    `claude-opus-5-5`."""
    return str(name or "").strip().partition("[")[0]


def cache_read_multiple(model):
    """The model's cache-read multiple, or None when the model is UNKNOWN."""
    base = model_id(model)
    if base not in known_models():
        return None
    return CACHE_READ_MULTIPLE.get(base, OTHER_MODEL_CACHE_READ_MULTIPLE)


def config_home():
    """The seat's own Claude config home: CLAUDE_CONFIG_DIR, else the default."""
    from . import homes
    raw = (os.environ.get(homes.ENV_VAR["claude"]) or "").strip()
    if raw:
        return os.path.realpath(os.path.expanduser(raw))
    return homes.DEFAULTS["claude"]


def account(home):
    """The snapshot key of the account `home` holds, or None when unreadable.
    The same key claudepace files its weekly readings under."""
    try:
        from . import claudepace
        return claudepace._account_key(home)
    except Exception:                           # noqa: BLE001
        return None


def window(reset_at, now):
    """(since, label) — the account's current weekly window, or the last seven
    days when its reset is not measured."""
    if isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool) \
            and reset_at > now:
        since = reset_at - WEEK_S
        return since, "this weekly window (since %s)" % _iso(since)
    return now - WEEK_S, ("the last 7 days (the account's weekly reset is "
                          "not measured)")


def session_logs(home, session):
    """Every transcript of one session under `home`: its main log and its
    subagents' (`<sid>/subagents/*.jsonl`, `<sid>/subagents/workflows/*/`)."""
    root = os.path.join(home, "projects", "*")
    sid = glob.escape(session)
    return (glob.glob(os.path.join(root, sid + ".jsonl"))
            + glob.glob(os.path.join(root, sid, "subagents", "*.jsonl"))
            + glob.glob(os.path.join(root, sid, "subagents", "workflows", "*",
                                     "*.jsonl")))


def usage_records(lines, cwd=None):
    """Session-log lines -> the per-message usage records, deduped by
    message.id (the first wins), in first-seen order.

    `lines` is an iterable of file paths (streamed line by line) or entries
    (dicts or raw JSON lines). A file is never loaded whole."""
    seen, order = set(), []
    for entry in lines:
        if isinstance(entry, str) and os.path.exists(entry):
            try:
                with open(entry, "r", errors="replace") as fh:
                    for raw in fh:
                        if raw.strip():
                            _collect(_parse_entry(raw, cwd), seen, order)
            except OSError:
                continue
            continue
        _collect(_parse_entry(entry, cwd), seen, order)
    return order


def _parse_entry(entry, cwd):
    """The usage record an entry holds, or None. A record is an entry whose
    `message` carries an id and a usage dict; an explicitly non-assistant
    type, a non-dict and an unparseable line are none."""
    if isinstance(entry, str):
        try:
            entry = json.loads(entry)
        except (ValueError, TypeError):
            return None
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("type"), str) and entry["type"] != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not message.get("id") or not isinstance(usage, dict):
        return None
    from . import turnresponse
    return {
        "id": message["id"],
        "model": message.get("model"),
        "usage": usage,
        "cwd": entry.get("cwd") if isinstance(entry.get("cwd"), str) else cwd,
        "ts": turnresponse._epoch(entry.get("timestamp")),
    }


def _collect(rec, seen, order):
    if rec is not None and rec["id"] not in seen:
        seen.add(rec["id"])
        order.append(rec)


def _int(value):
    return int(value) if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else 0


def price_usage(records):
    """Usage records -> the task's token buckets and their equivalents.

    Raw counts: fresh_input, cache_read, cache_creation_5m, cache_creation_1h,
    cache_creation_unsplit (a write the log does not split by window), output,
    thinking. Equivalents, in fresh-input tokens of each record's own model:
    cache_read_equivalent, cache_creation_5m_equivalent,
    cache_creation_1h_equivalent, and cache_creation_unsplit_equivalent, which
    is None (UNKNOWN) whenever any unsplit write exists. A record whose model
    helm does not catalog is counted in the raw buckets, named in
    `unknown_models`, counted in `unpriced_records` and left out of every
    equivalent."""
    buckets = {
        "fresh_input": 0, "cache_read": 0, "cache_creation_5m": 0,
        "cache_creation_1h": 0, "cache_creation_unsplit": 0, "output": 0,
        "thinking": 0, "cache_read_equivalent": 0.0,
        "cache_creation_5m_equivalent": 0.0,
        "cache_creation_1h_equivalent": 0.0,
        "cache_creation_unsplit_equivalent": 0.0, "unpriced_records": 0,
    }
    unknown = set()
    for rec in records:
        usage = rec["usage"]
        split = usage.get("cache_creation")
        split = split if isinstance(split, dict) else {}
        details = usage.get("output_tokens_details")
        details = details if isinstance(details, dict) else {}
        counts = {
            "fresh_input": _int(usage.get("input_tokens")),
            "cache_read": _int(usage.get("cache_read_input_tokens")),
            "cache_creation_5m": _int(split.get("ephemeral_5m_input_tokens")),
            "cache_creation_1h": _int(split.get("ephemeral_1h_input_tokens")),
            "output": _int(usage.get("output_tokens")),
            "thinking": _int(details.get("thinking_tokens")),
        }
        counts["cache_creation_unsplit"] = max(
            0, _int(usage.get("cache_creation_input_tokens"))
            - counts["cache_creation_5m"] - counts["cache_creation_1h"])
        for name, n in counts.items():
            buckets[name] += n
        multiple = cache_read_multiple(rec["model"])
        if multiple is None:
            if any(counts.values()):
                unknown.add(model_id(rec["model"]) or "unknown")
                buckets["unpriced_records"] += 1
            continue
        buckets["cache_read_equivalent"] += counts["cache_read"] * multiple
        buckets["cache_creation_5m_equivalent"] += (
            counts["cache_creation_5m"] * CACHE_WRITE_5M_MULTIPLE)
        buckets["cache_creation_1h_equivalent"] += (
            counts["cache_creation_1h"] * CACHE_WRITE_1H_MULTIPLE)
    if buckets["cache_creation_unsplit"]:
        buckets["cache_creation_unsplit_equivalent"] = None
    buckets["unknown_models"] = sorted(unknown)
    return buckets


def seat_spend(sessions, home, since=None, now=None):
    """The spend of `sessions` under `home` since `since`.

    Only those sessions' own transcripts are read, and a file untouched since
    `since` is skipped unread. A record is in the window by its own
    timestamp; a record with none is counted in `untimed` and left out.
    `source_ok` is False, with a `why`, when there is no session or no log."""
    now = time.time() if now is None else now
    out = {"source_ok": False, "sessions": list(sessions or ()),
           "since": _iso(since), "now": _iso(now), "records": 0,
           "untimed": 0, "missing_sessions": [], "buckets": {}}
    if not out["sessions"]:
        out["why"] = ("no session id: run it inside a Claude seat, or name one "
                      "with --session <id>")
        return out
    files = []
    for sid in out["sessions"]:
        found = session_logs(home, sid)
        if not found:
            out["missing_sessions"].append(sid)
        for path in found:
            try:
                if since is None or os.path.getmtime(path) >= since:
                    files.append(path)
            except OSError:
                continue
    if len(out["missing_sessions"]) == len(out["sessions"]):
        out["why"] = ("no Claude Code session log for %s under %s"
                      % (", ".join(out["sessions"]),
                         os.path.join(home, "projects")))
        return out
    records = []
    for rec in usage_records(files):
        if rec["ts"] is None:
            out["untimed"] += 1
        elif since is None or rec["ts"] >= since:
            records.append(rec)
    out.update(source_ok=True, records=len(records),
               buckets=price_usage(records))
    return out


def spend_reading(sessions=None, now=None, snap=None):
    """The verb's whole reading: the seat's home and account, the account's
    weekly percent and window, and the seat's spend over that window.
    `sessions` defaults to the running session's own id; `snap` to
    claudepace's cached snapshot."""
    from . import claudepace, home as helm_home
    now = time.time() if now is None else now
    if sessions is None:
        own = helm_home.session_id()
        sessions = [own] if own else []
    seat_home = config_home()
    key = account(seat_home)
    if snap is None:
        try:
            snap = claudepace.cached(now=now)
        except Exception:                       # noqa: BLE001
            snap = None
    rec = ((snap or {}).get("accounts") or {}).get(key) if key else None
    rec = rec if isinstance(rec, dict) else {}
    weekly, reset_at = rec.get("weekly_pct"), rec.get("weekly_reset_at")
    since, label = window(reset_at, now)
    out = seat_spend(sessions, seat_home, since=since, now=now)
    out.update(account=key, window=label, weekly_pct=weekly,
               weekly_reset_at=reset_at)
    return out


def _iso(at):
    if at is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def render(reading, weekly_pct=None, weekly_reset_at=None):
    """The `helm burn spend` human lines. NEVER a Max savings %."""
    account_key = reading.get("account")
    sessions = reading.get("sessions") or []
    lines = ["helm burn spend: %d assistant message(s) in %d session(s) of "
             "this seat (%s) on account %s, over %s"
             % (reading.get("records", 0), len(sessions),
                ", ".join(sessions) or "none", account_key or "unknown",
                reading.get("window") or "an unstated window")]
    buckets = reading.get("buckets") or {}
    lines.append(
        "  fresh input %s tokens; cache read %s; cache write %s (5m) + %s "
        "(1h) + %s (window unknown); output %s; thinking %s"
        % tuple(_num(buckets.get(name)) for name in (
            "fresh_input", "cache_read", "cache_creation_5m",
            "cache_creation_1h", "cache_creation_unsplit", "output",
            "thinking")))
    if buckets.get("unknown_models"):
        lines.append(
            "  %d model(s) helm has no price for — their equivalents are "
            "UNKNOWN, never zeroed: %s"
            % (len(buckets["unknown_models"]),
               ", ".join(buckets["unknown_models"])))
    if reading.get("untimed"):
        lines.append("  %d message(s) carry no readable timestamp and are left "
                     "out of the window, not counted as zero"
                     % reading["untimed"])
    if reading.get("missing_sessions"):
        lines.append("  no log found for session(s): %s"
                     % ", ".join(reading["missing_sessions"]))
    if isinstance(weekly_pct, (int, float)) and not isinstance(weekly_pct,
                                                               bool):
        lines.append("  weekly pace: %.0f%% of the week used (resets %s)"
                     % (weekly_pct, _iso(weekly_reset_at)
                        if isinstance(weekly_reset_at, (int, float))
                        else "an unknown instant"))
    else:
        lines.append("  weekly pace: not measured")
    lines.append(
        "  note: %s — the weekly figure is the whole account's, which other "
        "seats on it share; a reset or rounding moves it."
        % ("the account may be shared across seats" if account_key
           else "the account could not be identified"))
    return lines


def _num(value):
    if value is None:
        return "?"
    if isinstance(value, float):
        return "%g" % value
    return str(int(value))
