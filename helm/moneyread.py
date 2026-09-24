#!/usr/bin/env python3
"""Generic, credential-redacted money readings for limited families.

Readers are data in ``READERS``: ``creds(families)``, ``probe(cred, now)`` and
``rows(reading, family)``.  A reader runs only for the families whose catalog
entry declares it, and only against credentials a minted seat holds, so a home
with no such seat makes no network request.

The one vendor reader is ``openrouter-key`` (task/2936): the OpenRouter account
behind the openrouter, dots3 and ds4flash families, read with the key a seat's
own config holds, plus the LEDGER minute its free-model rate needs.

A snapshot contains only the eight-hex credential join, allowlisted reading
fields and already-redacted budget rows.  The opaque value on ``CredRef`` is
process-local and is never serialized or rendered.
"""
import dataclasses
import datetime
import hashlib
import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

from . import eventledger, home, pk

SNAPSHOT_NAME = "money-readers.json"
HISTORY_NAME = "money-history.jsonl"
SNAPSHOT_VERSION = 1
# A rolling minute is authoritative for one minute, not until the 15-minute
# watchdog returns. Adapters may emit independently expiring Readings, but
# `inputs` joins every Reading for one credential into ONE account before the
# fold: its windows are a conjunction, never rotated alternatives. A minute
# older than LEDGER_FRESH_S leaves the free family's conjunction, exactly as an
# unread minute never joined it, and the vendor's measured windows govern. It
# is never kept as a stale zero or read as permission to spend.
LEDGER_FRESH_S = 60
SOURCES = ("measured", "derived")
OVERFLOW_STATUSES = ("ok", "stale", "unread")
# The openrouter-key reader's window labels; PREPAID is also the one label a
# window with no length may carry (`_window`).
FREE_DAY, PREPAID, FREE_MINUTE = "1d-free", "prepaid", "1m"
_ACCOUNT = re.compile(r"[0-9a-f]{8}\Z")
_SAFE_WORD = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")

# Populated by vendor slices.  A table, not a class hierarchy.
READERS = {}


@dataclasses.dataclass(frozen=True)
class CredRef:
    account: str
    value: object = dataclasses.field(default=None, repr=False, compare=False)


@dataclasses.dataclass(frozen=True)
class Reading:
    status: str
    measured_at: float
    plan: object = None
    overflow: object = None
    windows: tuple = ()
    expires_at: object = None


def cred_ref(identity, value=None):
    """A process-local credential reference whose persisted join is a digest."""
    raw = str(identity).encode("utf-8", "surrogatepass")
    return CredRef(hashlib.sha256(raw).hexdigest()[:8], value)


def snapshot_path():
    return os.path.join(home.global_dir(), ".state", SNAPSHOT_NAME)


def history_path():
    return os.path.join(home.global_dir(), HISTORY_NAME)


def catalog_bindings(table=None):
    """{reader: {families}} from direct and per-pool catalog declarations."""
    if table is None:
        from . import seat
        table = seat.FAMILIES
    out = {}
    for family, spec in (table or {}).items():
        if not isinstance(spec, dict):
            continue
        name = spec.get("money_reader")
        if isinstance(name, str) and name:
            out.setdefault(name, set()).add(family)
        for row in (spec.get("pool_providers") or {}).values():
            name = row.get("money_reader") if isinstance(row, dict) else None
            if isinstance(name, str) and name:
                out.setdefault(name, set()).add(family)
    return {name: tuple(sorted(families)) for name, families in sorted(out.items())}


def catalog_families(table=None):
    return tuple(sorted({family for families in catalog_bindings(table).values()
                         for family in families}))


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


def _source(value, default="measured"):
    if value in SOURCES:
        return value
    return default if value is None else "derived"


def _word(value, default=None):
    text = value if isinstance(value, str) else ""
    return text if _SAFE_WORD.fullmatch(text) else default


def _status(value):
    if value == "ok":
        return "ok"
    text = value if isinstance(value, str) else ""
    why = text.partition(":")[2] if text.startswith("unread:") else ""
    return "unread:%s" % (_word(why, "malformed"))


def _row_state(value):
    return value if value in ("ok", "exhausted") else _status(value)


def _window(raw, default_source="measured"):
    if not isinstance(raw, dict):
        return None
    label = _word(raw.get("label"))
    seconds = _number(raw.get("seconds"))
    used = _number(raw.get("used_percent"))
    reset = _number(raw.get("reset_at"))
    if not isinstance(label, str) or not label:
        return None
    # A PREPAID BALANCE HAS NO PERIOD: no length and no reset. That is the
    # one window a missing length may describe, so only the prepaid shape
    # (dollars, or the prepaid label) is admitted without one; any other
    # window without a positive length is malformed and dropped.
    periodless = raw.get("seconds") is None and raw.get("reset_at") is None \
        and (raw.get("unit") == "dollars" or label == PREPAID)
    if not periodless and (seconds is None or seconds <= 0):
        return None
    out = {"label": label, "seconds": seconds, "used_percent": used,
           "reset_at": reset, "unit": _word(raw.get("unit"), "percent"),
           "source": _source(raw.get("source"), default_source)}
    # A WINDOW MAY EXPIRE BEFORE ITS READING: the ledger minute is believed
    # for one minute inside a reading believed until the day resets.
    expires = _number(raw.get("expires_at"))
    if expires is not None:
        out["expires_at"] = expires
    return out


def reading_dict(reading):
    """Reading -> the complete allowlisted, identity-free persisted shape."""
    if dataclasses.is_dataclass(reading):
        raw = {field.name: getattr(reading, field.name)
               for field in dataclasses.fields(reading)}
    elif isinstance(reading, dict):
        raw = reading
    else:
        raw = {}
    status = _status(raw.get("status"))
    measured = _number(raw.get("measured_at"))
    windows = tuple(w for w in (_window(x) for x in raw.get("windows") or ()) if w)
    plan = _word(raw.get("plan"))
    overflow = raw.get("overflow")
    # The overflow keeps its OWN age and status: a balance carried forward
    # from an earlier read is stale at the age it was measured, never now.
    overflow = ({"kind": _word(overflow.get("kind"), "unknown"),
                 "balance": _number(overflow.get("balance")),
                 "measured_at": _number(overflow.get("measured_at")),
                 "status": (overflow.get("status")
                            if overflow.get("status") in OVERFLOW_STATUSES
                            else "unread")}
                if isinstance(overflow, dict) else None)
    return {"status": status, "measured_at": measured, "plan": plan,
            "overflow": overflow, "windows": list(windows),
            "expires_at": _number(raw.get("expires_at"))}


def reading_rows(reading, family=None):
    """The generic one-account codexbudget row for a Reading."""
    rec = reading_dict(reading)
    windows = rec["windows"]
    measured = [w for w in windows if w.get("used_percent") is not None]
    worst = max(measured, key=lambda w: w["used_percent"]) if measured else None
    ok = rec["status"] == "ok" and worst is not None
    sources = {w["source"] for w in windows}
    return [{"state": "ok" if ok else rec["status"],
             "longest_pct": worst.get("used_percent") if ok else None,
             "source": "derived" if "derived" in sources else "measured",
             "windows": windows}]


def _row(raw):
    if not isinstance(raw, dict):
        return None
    declared = _source(raw.get("source"))
    windows = [w for w in (_window(x, declared)
                            for x in raw.get("windows") or ()) if w]
    source = ("derived" if declared == "derived"
              or any(w["source"] == "derived" for w in windows)
              else "measured")
    return {"state": _row_state(raw.get("state")),
            "longest_pct": _number(raw.get("longest_pct")), "source": source,
            "windows": windows}


def _unread(why, now):
    return Reading("unread:%s" % why, now, windows=())


def probe_snapshot(now=None, readers=None, table=None):
    """Run every catalog-declared reader once per credential; never raises."""
    now = time.time() if now is None else now
    readers = READERS if readers is None else readers
    records = []
    for name, families in catalog_bindings(table).items():
        impl = readers.get(name) if isinstance(readers, dict) else None
        if not isinstance(impl, dict) or not all(callable(impl.get(k))
                                                  for k in ("creds", "probe", "rows")):
            records.append(_record(name, "00000000", families,
                                   _unread("reader-unavailable", now), {}, impl))
            continue
        try:
            creds = tuple(impl["creds"](families) or ())
        except Exception:                       # noqa: BLE001 — reader is total here
            creds = ()
        if not creds:
            records.append(_record(name, "00000000", families,
                                   _unread("credentials-unavailable", now), {}, impl))
            continue
        seen = set()
        for cred in creds:
            account = cred.account if isinstance(cred, CredRef) else "00000000"
            if account in seen:
                continue
            seen.add(account)
            try:
                reading = impl["probe"](cred, now)
            except Exception:                   # noqa: BLE001 — a probe never escapes
                reading = _unread("probe-failed", now)
            rows = {}
            for family in families:
                try:
                    rows[family] = impl["rows"](reading, family)
                except Exception:               # noqa: BLE001 — one mapping stays unread
                    rows[family] = reading_rows(_unread("row-mapping-failed", now))
            records.append(_record(name, account, families, reading, rows, impl))
    return {"v": SNAPSHOT_VERSION, "ts": now, "readings": records}


def _record(name, account, families, reading, rows, impl=None):
    rec = reading_dict(reading)
    if not _ACCOUNT.fullmatch(str(account or "")):
        account = "00000000"
    mapped = {}
    for family in families:
        cleaned = [r for r in (_row(x) for x in (rows.get(family) or ())) if r]
        mapped[family] = cleaned or reading_rows(rec, family)
    return {"reader": _word(name, "unknown-reader"), "account": account,
            "families": list(families), **rec, "rows": mapped}


def write_snapshot(payload, path=None):
    try:
        target = path or snapshot_path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        pk.atomic_write(target, json.dumps(payload, sort_keys=True))
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_snapshot(path=None):
    """(payload, error) where missing/corrupt remains an explicit unknown."""
    try:
        with pk.open_regular(path or snapshot_path(), encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None, "snapshot-absent"
    except (OSError, ValueError):
        return None, "snapshot-unreadable"
    if not isinstance(payload, dict) or payload.get("v") != SNAPSHOT_VERSION \
            or not isinstance(payload.get("readings"), list):
        return None, "snapshot-malformed"
    return payload, None


def _account_row(records):
    """Several independently expiring readings -> one credential conjunction."""
    rows = []
    for batch, _measured, fresh in records:
        for row in batch:
            readable = fresh and row.get("state") in ("ok", "exhausted") \
                and bool(row.get("windows"))
            if readable:
                rows.append(row)
                continue
            state = (row.get("state") if fresh and row.get("state") not in
                     ("ok", "exhausted") else "unread:empty-reading"
                     if fresh else "unread:stale")
            windows = [dict(window, used_percent=None)
                       for window in row.get("windows") or ()]
            if not windows:
                # The absent window set is itself unread input. Without a
                # placeholder, joining this record to a readable sibling would
                # erase it and incorrectly restore permission to spend.
                windows = [{"label": state, "seconds": None,
                            "used_percent": None, "reset_at": None,
                            "unit": "percent", "source": row.get("source")}]
            rows.append({"state": state, "longest_pct": None,
                         "source": row.get("source"), "windows": windows})
    live = [row for row in rows if row.get("state") in ("ok", "exhausted")]
    windows = [window for row in rows for window in row.get("windows") or ()]
    values = [value for row in live for value in (
        [row.get("longest_pct")] +
        [window.get("used_percent") for window in row.get("windows") or ()])
              if _number(value) is not None]
    # A LIVE `exhausted` SURVIVES THE JOIN (task/2935). It is the vendor's own
    # word that this account is spent; rewriting it as `ok` because the row
    # also carried a number made the fold's exhausted-without-a-measured-cap
    # cell unreachable, and an exhausted account read OPEN — a spend YES.
    exhausted = any(row.get("state") == "exhausted" for row in live)
    if values:
        state, pct = ("exhausted" if exhausted else "ok"), max(values)
    else:
        state = "exhausted" if exhausted else next(
            (row.get("state") for row in rows
             if row.get("state") not in ("ok", "exhausted")),
            "unread:row-malformed")
        pct = None
    source = ("derived" if any(
        row.get("source") == "derived" or any(
            window.get("source") == "derived"
            for window in row.get("windows") or ())
        for row in rows) else "measured")
    return {"state": state, "longest_pct": pct,
            "source": source, "windows": windows}


def _expire(windows, now):
    """Every window past its OWN expiry reads unread: a number is believed
    only for as long as the evidence behind it."""
    return [dict(w, used_percent=None)
            if isinstance(w, dict) and _number(w.get("expires_at")) is not None
            and now > w["expires_at"] else w for w in windows or ()]


def _rows_at(rec, family, rows, now):
    """One record's rows for one family AT THE FOLD'S NOW.

    A registered reader maps its own persisted reading again with every
    expired window unread first, so its rules decide on what is still
    believed: an expired ledger minute is not part of a free family's
    conjunction, exactly as an unread one never was. A record no registered
    reader owns keeps the rows it was written with, its expired windows
    unread and its percentage re-taken from what remains."""
    impl = READERS.get(rec.get("reader"))
    if isinstance(impl, dict) and callable(impl.get("rows")):
        try:
            return impl["rows"](dict(rec, windows=_expire(rec.get("windows"), now)),
                                family)
        except Exception:                   # noqa: BLE001 — one mapping stays unread
            return reading_rows(_unread("row-mapping-failed", now), family)
    out = []
    for row in rows:
        windows = _expire(row.get("windows"), now) if isinstance(row, dict) else ()
        if not isinstance(row, dict) or windows == list(row.get("windows") or ()):
            out.append(row)
            continue
        numbers = [w["used_percent"] for w in windows
                   if isinstance(w, dict) and _number(w.get("used_percent")) is not None]
        out.append(dict(row, windows=windows,
                        longest_pct=(max(numbers) if numbers
                                     and row.get("longest_pct") is not None
                                     else None)))
    return out


def inputs(payload, now=None, max_age_s=None, table=None, error=None):
    """Snapshot -> burnflags money maps; stale and failed reads stay explicit."""
    now = time.time() if now is None else now
    bound = float("inf") if max_age_s is None else max_age_s
    out = {"money": {}, "money_measured_at": {}, "money_fresh": {}}
    expected = catalog_families(table)
    if payload is None:
        why = error or "snapshot-unavailable"
        for family in expected:
            out["money"][family] = reading_rows(_unread(why, now), family)
            out["money_fresh"][family] = True
        return out
    grouped = {}
    for rec in payload.get("readings") or ():
        if not isinstance(rec, dict):
            continue
        measured = _number(rec.get("measured_at"))
        expires = _number(rec.get("expires_at"))
        fresh = measured is not None and 0 <= now - measured <= bound \
            and (expires is None or now <= expires)
        account = rec.get("account")
        join = account if _ACCOUNT.fullmatch(str(account or "")) else "00000000"
        for family, rows in (rec.get("rows") or {}).items():
            if family not in expected or not isinstance(rows, list):
                continue
            rows = _rows_at(rec, family, rows, now)
            cleaned = [r for r in (_row(x) for x in rows) if r]
            if not cleaned:
                cleaned = reading_rows(_unread("row-malformed", now), family)
            grouped.setdefault(family, {}).setdefault(join, []).append(
                (cleaned, measured, fresh))
    for family in expected:
        accounts = grouped.get(family) or {}
        records = [record for batches in accounts.values() for record in batches]
        live = [record for record in records if record[2]]
        if live:
            out["money"][family] = [_account_row(batches)
                                     for _join, batches in sorted(accounts.items())]
            out["money_measured_at"][family] = max(
                measured for _batch, measured, _fresh in live)
            out["money_fresh"][family] = True
        elif records:
            out["money"][family] = [_account_row(batches)
                                     for _join, batches in sorted(accounts.items())]
            measured = [stamp for _batch, stamp, _fresh in records
                        if stamp is not None]
            if measured:
                out["money_measured_at"][family] = max(measured)
            out["money_fresh"][family] = False
        else:
            out["money"][family] = reading_rows(
                _unread("reader-missing-from-snapshot", now), family)
            out["money_fresh"][family] = True
    return out


def _iso(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def history_rows(payload):
    """Redacted native-history-shaped rows, preserving each gauge's source."""
    out = []
    for rec in (payload or {}).get("readings") or ():
        if not isinstance(rec, dict):
            continue
        gauges = [{"label": w.get("label"),
                   "utilization": (w.get("used_percent") / 100.0
                                   if _number(w.get("used_percent")) is not None else None),
                   "reset": w.get("reset_at"), "source": _source(w.get("source"))}
                  for w in rec.get("windows") or () if isinstance(w, dict)]
        measured = _number(rec.get("measured_at"))
        probed = _iso(measured) if measured is not None else None
        out.append({"id": "%s:%s:%s" % (rec.get("reader"), rec.get("account"),
                                           probed or "unmeasured"),
                    "provider": rec.get("reader"), "account": rec.get("account"),
                    "probed_at": probed, "status": rec.get("status"),
                    "source": ("derived" if any(g["source"] == "derived"
                                                  for g in gauges) else "measured"),
                    "gauges": gauges, "expires_at": rec.get("expires_at")})
    return out


def append_history(payload, path=None):
    target = path or history_path()
    rows = history_rows(payload)
    with eventledger.locked(target) as held:
        return bool(held) and all(eventledger.append_unlocked(target, row) for row in rows)


def last_good(reader, account, path=None, before=None):
    """(anchor, error) for the newest direct meter reading of one hash.

    A derived history row is not an anchor: feeding a ledger back into itself
    compounds estimates and can only increase confidence it did not measure.
    Corrupt/unreadable history is UNKNOWN, never an empty anchor.
    """
    if _word(reader) is None or not _ACCOUNT.fullmatch(str(account or "")):
        return None, "invalid reader/account"
    rows, error = eventledger.checked_events(path or history_path(), strict=True)
    if error:
        return None, "history unreadable: %s" % error
    best = None
    for row in rows:
        if not isinstance(row, dict) or row.get("provider") != reader \
                or row.get("account") != account or row.get("status") != "ok" \
                or row.get("source") != "measured":
            continue
        measured = pk.parse_ts_epoch(row.get("probed_at"))
        if measured is None or before is not None and measured > before:
            continue
        windows = []
        for gauge in row.get("gauges") or ():
            utilization = _number(gauge.get("utilization")) \
                if isinstance(gauge, dict) else None
            label = _word(gauge.get("label")) if isinstance(gauge, dict) else None
            # Generic history writes gauge provenance. A contradictory derived
            # gauge cannot become a direct anchor merely because the row claims
            # measured; legacy rows with no gauge source remain direct.
            if utilization is None or label is None \
                    or gauge.get("source", "measured") != "measured":
                windows = []
                break
            windows.append({"label": label, "used_percent": utilization * 100.0,
                            "reset_at": _number(gauge.get("reset")),
                            "source": "measured"})
        if windows and (best is None or measured > best[0]):
            best = (measured, windows)
    return (None, None) if best is None else (
        {"measured_at": best[0], "windows": best[1]}, None)


def refresh(now=None, readers=None, table=None, snapshot=None, history=None):
    """Probe, then persist one snapshot and one history row per reading."""
    payload = probe_snapshot(now=now, readers=readers, table=table)
    return payload if write_snapshot(payload, snapshot) \
        and append_history(payload, history) else None


def _tokens(event):
    breakdown = event.get("token_breakdown") if isinstance(event, dict) else None
    if not isinstance(breakdown, dict):
        return 0.0, 0.0, 0.0
    total = _number(breakdown.get("total_tokens"))
    inp = _number((breakdown.get("input") or {}).get("total_tokens")) or 0.0
    out = _number((breakdown.get("output") or {}).get("total_tokens")) or 0.0
    return inp, out, total if total is not None else inp + out


def _price(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _event_account(event, account):
    source = event.get("source") if isinstance(event, dict) else None
    if not isinstance(source, str) or not source:
        return False
    if source.startswith("cred:"):
        return source == "cred:" + account
    return hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()[:8] == account


def _spend(event, unit, pricing):
    if unit == "requests":
        return 1.0
    inp, out, total = _tokens(event)
    if unit == "tokens":
        return total
    if unit != "dollars":
        return None
    model = event.get("model") if isinstance(event, dict) else None
    price = pricing.get(model) if isinstance(pricing, dict) \
        and isinstance(pricing.get(model), dict) else pricing
    if not isinstance(price, dict):
        return None
    prompt = _price(price.get("prompt"))
    completion = _price(price.get("completion"))
    return None if prompt is None or completion is None else inp * prompt + out * completion


def _ledger_window(limit, now, anchors, anchor_at):
    if not isinstance(limit, dict):
        return None
    seconds, cap = _number(limit.get("seconds")), _number(limit.get("cap"))
    label, unit = _word(limit.get("label")), limit.get("unit")
    if label is None or seconds is None or seconds <= 0 or cap is None or cap <= 0 \
            or unit not in ("requests", "tokens", "dollars"):
        return None
    reset = _number(limit.get("reset_at"))
    if limit.get("reset_at") is not None and reset is None:
        return None
    if reset is None:
        # Rolling aggregates decay request by request. Carrying an aggregate
        # anchor would retain requests after they leave the window and count new
        # ones again, so a rolling window always rebuilds from current events.
        start, spend_start, base_pct, anchored = \
            now - seconds, now - seconds, 0.0, False
    else:
        if reset <= now:
            return None
        start = reset - seconds
        prior = anchors.get(label) or {}
        prior_pct = _number(prior.get("used_percent"))
        same_period = _number(prior.get("reset_at")) == reset \
            and prior_pct is not None \
            and anchor_at is not None and start <= anchor_at <= now
        spend_start = anchor_at if same_period else start
        base_pct = prior_pct if same_period else 0.0
        anchored = same_period
    return {"label": label, "seconds": seconds, "cap": cap, "unit": unit,
            "reset_at": reset, "spend_start": spend_start,
            "base_pct": base_pct, "anchored": anchored}


def ledger_reading(now, limits, requests, completeness, anchor=None,
                   pricing=None, safety_factor=1.0, plan=None, account=None):
    """Pure LEDGER reading: an anchor plus complete spend since each window start.

    ``completeness`` must explicitly cover every derived window through ``now``.
    A rolling one-minute row therefore requires ``since <= now-60`` and expires
    after exactly one minute.  Missing coverage is unread, never zero usage.
    """
    limits = tuple(limits or ())
    if not _ACCOUNT.fullmatch(str(account or "")):
        return Reading("unread:ledger-account", now, plan=plan,
                       expires_at=now + LEDGER_FRESH_S)
    factor = _number(safety_factor)
    if factor is None or factor < 1:
        return Reading("unread:ledger-safety-factor", now, plan=plan,
                       expires_at=now + LEDGER_FRESH_S)
    anchor = anchor if isinstance(anchor, dict) else {}
    anchors = {w.get("label"): w for w in (anchor.get("windows") or ())
               if isinstance(w, dict)}
    anchor_at = _number(anchor.get("measured_at"))
    specs = [_ledger_window(limit, now, anchors, anchor_at) for limit in limits]
    if not specs or any(spec is None for spec in specs):
        return Reading("unread:ledger-limit", now, plan=plan,
                       expires_at=now + LEDGER_FRESH_S)
    completeness = completeness if isinstance(completeness, dict) else {}
    since = _number(completeness.get("since"))
    through = _number(completeness.get("through"))
    complete = bool(completeness.get("complete")) \
        and since is not None and since <= min(x["spend_start"] for x in specs) \
        and through is not None and through >= now
    if not complete:
        return Reading("unread:ledger-incomplete", now, plan=plan,
                       expires_at=now + LEDGER_FRESH_S)
    windows = []
    for spec in specs:
        spent = 0.0
        for event in requests or ():
            if not _event_account(event, account):
                continue
            at = _number(event.get("at")) if isinstance(event, dict) else None
            if at is None or at > now or (
                    at <= spec["spend_start"] if spec["anchored"]
                    else at < spec["spend_start"]):
                continue
            amount = _spend(event, spec["unit"], pricing)
            if amount is None:
                return Reading("unread:ledger-pricing", now, plan=plan,
                               expires_at=now + LEDGER_FRESH_S)
            spent += amount
        used = spec["base_pct"] + 100.0 * spent * factor / spec["cap"]
        windows.append({"label": spec["label"], "seconds": spec["seconds"],
                        "used_percent": used, "reset_at": spec["reset_at"],
                        "unit": spec["unit"], "source": "derived"})
    return Reading("ok", now, plan=plan, windows=tuple(windows),
                   expires_at=now + LEDGER_FRESH_S)


# ------------------------------------------------ the openrouter-key reader
#
# ONE ACCOUNT, THREE FAMILIES (task/2936). OpenRouter's free-model caps and
# its credit balance are account-wide ("additional accounts or API keys will
# not affect your rate limits", the vendor's limits page), so one read of the
# key serves every family the catalog binds to this reader, and the key's
# hash is the account join the proxy-usage ledger already writes.
#
# THE READING holds three windows, whatever they say:
#   1d-free  free-model requests today, from GET /api/v1/key, resetting at the
#            next 00:00Z (the vendor's UTC day).
#   prepaid  credits used, from GET /api/v1/credits; a balance has no period.
#   1m       the LEDGER supplement: this key's `:free` requests in the last
#            60 s against the vendor's 20 a minute. The vendor has no meter for
#            the minute, so this window is derived.
#
# THE ROWS are per family, because the same account binds them differently:
#   paid     (a pool leg at this vendor with rung "paid", i.e. ds4flash) spends
#            credits: its budget is the prepaid window.
#   free     spends the free day. The balance binds a free family only when it
#            is negative (the vendor answers 402 then, free models included)
#            or unreadable; a positive balance is not a budget it spends.
#   The minute joins a free family's row only at or past the fold's warning
#   band. The ledger count is a LOWER BOUND (a consumer of the key outside
#   helm's proxies is invisible to it), so below the band it proves nothing,
#   neither headroom nor pressure, and the account-state table reads any
#   derived window as incomplete: emitting it there would turn every
#   successful vendor read into UNKNOWN. At or past the band the lower bound
#   alone proves the pressure, and the fold reads it as an estimate: ORANGE,
#   never RED, never a spend yes. The reading keeps every minute regardless.

OPENROUTER_READER = "openrouter-key"
OPENROUTER_API = "https://openrouter.ai/api/v1"
OPENROUTER_TIMEOUT_S = 10.0
#: The vendor-published free-model rate, account-wide (its limits page).
OPENROUTER_FREE_PER_MINUTE = 20
_DAY_S = 86400


class _Secret:
    """A credential held for one process. It renders as a placeholder, so a
    repr of the value that carries it cannot print the key."""

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def __repr__(self):
        return "<secret>"

    __str__ = __repr__

    def reveal(self):
        return self._value


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect. The stdlib handler copies a request's headers,
    the Authorization header included, onto the redirected request whatever
    its host, so following one could hand the key to any origin a reply
    names. Returning no request makes the 3xx an HTTPError of its own."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_json(url, key, timeout=OPENROUTER_TIMEOUT_S):
    """(the reply's ``data`` object, None) or (None, failure class) for one
    authenticated GET that spends no quota.

    THE KEY TRAVELS IN ONE HEADER, TO ONE URL, AND NOWHERE ELSE: a redirect is
    refused, never followed. A failure is a fixed class word, never the
    exception's text, which can carry the request: auth (401, 403), rate
    (429), http-3xx (any redirect), network, schema (not JSON, or no ``data``
    object) or http-<code>."""
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key, "Accept": "application/json"})
    opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        with opener.open(request, timeout=timeout) as reply:
            body = reply.read(1 << 20)
    except urllib.error.HTTPError as exc:
        code = exc.code if isinstance(exc.code, int) else 0
        try:
            exc.close()
        except Exception:                   # noqa: BLE001 — a close is not a read
            pass
        return None, ("auth" if code in (401, 403) else "rate" if code == 429
                      else "http-3xx" if 300 <= code < 400
                      else "http-%d" % code)
    except (urllib.error.URLError, http.client.HTTPException, OSError,
            ValueError):
        return None, "network"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "schema"
    data = payload.get("data") if isinstance(payload, dict) else None
    return (data, None) if isinstance(data, dict) else (None, "schema")


def _config_keys(path):
    """Every OpenRouter key one seat config's enabled provider blocks hold.

    Read with the mint door's own parser, so this reads the bytes the mint
    wrote. A block at any other endpoint never lends its key: the key is only
    ever sent to the vendor that issued it."""
    from . import seat, seat_launch_assets     # noqa: F401 — seat seeds the impl
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        _prefix, blocks = seat_launch_assets._top_blocks(text)
        block = dict(blocks).get("openai-compatibility", "")
        _prefix, items = seat_launch_assets._provider_sections(block)
    except (OSError, ValueError):
        return ()
    keys = []
    for _name, item in items:
        try:
            if seat_launch_assets._provider_disabled(item):
                continue
            info = seat_launch_assets._provider_info(item, None)
        except ValueError:
            continue
        if info["base_url"].rstrip("/") != OPENROUTER_API:
            continue
        keys += [key for key in info["api_keys"] if isinstance(key, str)
                 and key and not any(c.isspace() for c in key)]
    return tuple(keys)


def _openrouter_creds(families):
    """One CredRef per distinct key a minted seat of these families holds,
    carrying the seats that spend it (the ledger minute's completeness)."""
    from . import seat
    wanted = set(families)
    found = {}
    for family, name in seat._minted_seats():
        if family not in wanted:
            continue
        path = os.path.join(seat._proxy_home(family, name), "config.yaml")
        for key in _config_keys(path):
            found.setdefault(key, []).append(name)
    return [cred_ref(key, value={"key": _Secret(key),
                                 "seats": tuple(dict.fromkeys(seats))})
            for key, seats in found.items()]


def _next_utc_midnight(now):
    return (math.floor(now / _DAY_S) + 1) * _DAY_S


def _free_day(data, now):
    """The 1d-free window off /key's free_model_daily_requests, or unread."""
    free = data.get("free_model_daily_requests") if isinstance(data, dict) else None
    free = free if isinstance(free, dict) else {}
    used, limit = _number(free.get("used")), _number(free.get("limit"))
    remaining = _number(free.get("remaining"))
    pct = None
    if used is not None and limit is not None and used >= 0 and limit > 0:
        pct = 100.0 * used / limit
        if remaining is not None:
            pct = max(pct, 100.0 * (limit - remaining) / limit)
    return {"label": FREE_DAY, "seconds": _DAY_S, "used_percent": pct,
            "reset_at": _next_utc_midnight(now), "unit": "requests",
            "source": "measured"}


def _prior_credit(account):
    """The last balance a snapshot on disk read for this account, with the
    age it was MEASURED at, or None."""
    payload, _error = read_snapshot()
    for rec in (payload or {}).get("readings") or ():
        if not isinstance(rec, dict) or rec.get("reader") != OPENROUTER_READER \
                or rec.get("account") != account:
            continue
        over = rec.get("overflow")
        if isinstance(over, dict) and over.get("status") in ("ok", "stale") \
                and _number(over.get("balance")) is not None \
                and _number(over.get("measured_at")) is not None:
            return {"balance": over["balance"], "measured_at": over["measured_at"]}
    return None


def _prepaid(data, now, account):
    """(the prepaid window, the credit overflow) off /credits.

    AN UNREAD BALANCE KEEPS THE LAST VALUE, MARKED STALE AT ITS OWN AGE, and
    its window reads unread: a carried-forward balance is diagnosis, never
    permission (the integrator's shape ruling on task/2936)."""
    window = {"label": PREPAID, "seconds": None, "used_percent": None,
              "reset_at": None, "unit": "dollars", "source": "measured"}
    credits = _number(data.get("total_credits")) if isinstance(data, dict) else None
    usage = _number(data.get("total_usage")) if isinstance(data, dict) else None
    if credits is None or usage is None or credits < 0 or usage < 0:
        prior = _prior_credit(account)
        return window, dict({"kind": "credits", "balance": None,
                             "measured_at": None}, **(prior or {}),
                            status="stale" if prior else "unread")
    window["used_percent"] = 100.0 * usage / credits if credits > 0 else 100.0
    return window, {"kind": "credits", "balance": credits - usage,
                    "measured_at": now, "status": "ok"}


def _ledger_minute_input(account, seats, now, path=None):
    """(this key's `:free` request events in the minute, the completeness the
    LEDGER core requires) off the proxy-usage ledger.

    COMPLETE means every seat that holds the key has two consecutive READ
    markers from ONE sidecar life around the minute: a pop empties the queue
    and a restart loses it, so only that pair proves the ledger holds every
    request between them."""
    from . import proxy_usage
    rows, error = eventledger.checked_events(path or proxy_usage.ledger_path())
    if error:
        return (), {"complete": False}
    tag = proxy_usage.CRED_PREFIX + account
    wanted = set(seats)
    pairs, events = {}, []
    for row in rows:
        if not isinstance(row, dict) or row.get("v") != proxy_usage.V:
            continue
        if row.get("kind") == "read" and row.get("seat") in wanted:
            pairs[row["seat"]] = ((pairs.get(row["seat"]) or (None, None))[1],
                                  row)
        elif row.get("kind") == "request" and row.get("source") == tag \
                and str(row.get("model") or "").endswith(":free"):
            at = _number(row.get("at"))
            if at is not None and now - 60 <= at <= now:
                events.append({"at": at, "source": tag})
    since, through = [], []
    for seat in wanted:
        prev, last = pairs.get(seat) or (None, None)
        if not prev or not last or prev.get("pid") is None \
                or prev.get("pid") != last.get("pid") \
                or {prev.get("status"), last.get("status")} != {proxy_usage.READ}:
            return (), {"complete": False}
        since.append(_number(prev.get("ts")))
        through.append(_number(last.get("ts")))
    if not wanted or None in since or None in through:
        return (), {"complete": False}
    return events, {"complete": True, "since": max(since),
                    "through": min(through)}


def _free_minute(account, seats, now, path=None):
    """The LEDGER 1m window for one key, or that window unread."""
    limit = [{"label": FREE_MINUTE, "seconds": 60,
              "cap": OPENROUTER_FREE_PER_MINUTE, "unit": "requests",
              "reset_at": None}]
    try:
        events, completeness = _ledger_minute_input(account, seats, now, path)
    except Exception:                       # noqa: BLE001 — unread, never a raise
        events, completeness = (), {"complete": False}
    reading = ledger_reading(now, limit, events, completeness, account=account)
    if reading.status == "ok" and reading.windows:
        # the minute's authority is its own LEDGER_FRESH_S, not the day's
        return dict(reading.windows[0], expires_at=reading.expires_at)
    return {"label": FREE_MINUTE, "seconds": 60, "used_percent": None,
            "reset_at": None, "unit": "requests", "source": "derived"}


def _openrouter_probe(cred, now):
    """GET /key, then /credits, then the ledger minute -> one Reading.

    KNOWN LIMITS, each safe in direction:
    - History rows read derived: the reading always carries the derived 1m
      window, so history_rows labels every row derived and last_good never
      anchors this reader.
    - One stopped or restarted seat blinds the minute: completeness needs two
      READ markers of one sidecar life for EVERY seat holding the key.
    - One key per account is assumed: two keys on one account read as two
      accounts, the day twice and the minute split per key (still a lower
      bound).
    - Prepaid is lifetime arithmetic: usage over every credit ever bought, so
      the ceiling refuses ds4flash at a share of lifetime credits, not of the
      balance."""
    value = cred.value if isinstance(cred, CredRef) \
        and isinstance(cred.value, dict) else {}
    secret = value.get("key")
    if not isinstance(secret, _Secret) or not secret.reveal():
        return _unread("credentials-unavailable", now)
    data, failure = _http_json(OPENROUTER_API + "/key", secret.reveal())
    if failure:
        return _unread(failure, now)
    credits, failure = _http_json(OPENROUTER_API + "/credits", secret.reveal())
    day = _free_day(data, now)
    prepaid, overflow = _prepaid(None if failure else credits, now,
                                 cred.account)
    minute = _free_minute(cred.account, value.get("seats") or (), now)
    plan = "free-tier" if data.get("is_free_tier") is True else "pay-as-you-go"
    return Reading("ok", now, plan=plan, overflow=overflow,
                   windows=(day, prepaid, minute), expires_at=day["reset_at"])


def _openrouter_paid(family):
    """Does this family spend the account's credits? Read off the catalog's
    own rung for a pool leg at this vendor, never a list of family names."""
    from . import seat
    fam = seat.FAMILIES.get(family) or {}
    return any(isinstance(row, dict) and row.get("rung") == "paid"
               and str(row.get("base_url") or "").rstrip("/") == OPENROUTER_API
               for row in (fam.get("pool_providers") or {}).values())


def _warning_band():
    from . import burnflags
    return burnflags.orange_pct()


def _openrouter_rows(reading, family):
    """One family's row off the account reading (the section comment above)."""
    rec = reading_dict(reading)
    if rec["status"] != "ok":
        return reading_rows(rec, family)
    by = {window["label"]: window for window in rec["windows"]}
    unread = {"used_percent": None, "reset_at": None, "source": "measured"}
    day = by.get(FREE_DAY) or dict(unread, label=FREE_DAY, seconds=_DAY_S,
                                    unit="requests")
    prepaid = by.get(PREPAID) or dict(unread, label=PREPAID, seconds=None,
                                      unit="dollars")
    if _openrouter_paid(family):
        chosen = [prepaid]
    else:
        over = rec.get("overflow") or {}
        balance = over.get("balance") if over.get("status") == "ok" else None
        chosen = [day]
        if prepaid["used_percent"] is None or balance is None or balance < 0:
            chosen.append(prepaid)
        minute = by.get(FREE_MINUTE)
        if minute and minute["used_percent"] is not None \
                and minute["used_percent"] >= _warning_band():
            chosen.append(minute)
    numbers = [w["used_percent"] for w in chosen if w["used_percent"] is not None]
    return [{"state": "ok", "longest_pct": max(numbers) if numbers else None,
             "source": ("derived" if any(w["source"] == "derived"
                                         for w in chosen) else "measured"),
             "windows": chosen}]


READERS[OPENROUTER_READER] = {"creds": _openrouter_creds,
                             "probe": _openrouter_probe,
                             "rows": _openrouter_rows}
