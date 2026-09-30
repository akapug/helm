#!/usr/bin/env python3
"""helm offpeak — a paid provider that may spend ONLY outside its vendor's peak.

THE OWNER'S ASK: "i would totally top up more if we could easily
find a way to only use the api key during off-peak rates" and "make it last
via offpeak usage". DeepSeek bills half price off-peak. Its peak is 01:00-04:00
and 06:00-10:00 UTC, Monday to Friday, except Chinese public holidays; every
other hour is off-peak, weekends included.

THE WINDOW IS DATA, NOT CODE. A pool provider row in the catalog carries a
``billing_window`` (see `window_error` for the exact shape), and ONE pure
function answers "is this instant peak" (`in_peak`). Everything else here --
the guard, the next change, the timer's calendar, the hold -- is derived from
that one declaration, so a vendor that moves its hours is a one-line edit.

CONSERVATIVE ON HOLIDAYS. A Chinese public holiday is still PEAK here unless
the window's ``offpeak_dates`` names that date: a missing table must never
spend at the peak price. ``peak_dates`` adds a date the weekday rule misses
(a weekend make-up working day, if the vendor bills one as peak). Both are
UTC dates; the peak windows fall inside one China-time day, so the UTC date
and the China date agree for every peak minute.

THREE LAYERS, ONE PREDICATE:
  * HARD, at the proxy. `apply_gate` writes ``disabled: true`` (with a marker
    comment) into the provider block while the window is closed and removes
    only its own marked line when it opens. `proxy_config_plan` runs it, so
    `seat up`, the */3 `seat doctor --ensure` reconciler and a reboot's
    respawn all start the proxy with the flag the proved clock calls for. A
    close publishes disabled bytes first; if safe replacement is impossible,
    a verified listener stays stopped rather than serving stale open bytes.
  * SOFT, at delivery. `seat_pause` is read first by
    `proxywatch.delivery_pause`, so a seat whose every route is closed is held
    the way a walled seat is held: the keystroke is withheld and rows addressed
    to it stay owed. The canary skips it, so a closed provider is not measured
    as dark.
  * SCHEDULE. `helm offpeak --install-timer` installs a systemd user timer
    whose OnCalendar lines are derived from the declared windows, in explicit
    UTC, starting the guard ``guard_lead_s`` early and ending it
    ``guard_lag_s`` late. An otherwise-open route also needs a fresh bounded
    Date proof from the vendor itself, so wall-clock skew fails closed.
"""
import datetime
import email.utils
import json
import os
import re
import subprocess
import sys
import time

STATE = "PEAK-WINDOW"
MARK = "# helm offpeak gate"
_PRESERVED = "    # helm offpeak preserved: "
WINDOW_KEYS = frozenset(("vendor", "tz", "peak_days", "peak", "guard_lead_s",
                         "guard_lag_s", "clock_max_skew_s", "offpeak_dates",
                         "peak_dates", "source"))
_REQUIRED = frozenset(("vendor", "tz", "peak_days", "peak", "guard_lead_s",
                       "guard_lag_s", "clock_max_skew_s"))
_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)\Z")
_DATE = re.compile(r"\d{4}-\d\d-\d\d\Z")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_HORIZON_DAYS = 40
_SERVICE_NAME = "helm-offpeak.service"
_TIMER_NAME = "helm-offpeak.timer"
SYSTEMCTL_TIMEOUT_S = 30
CLOCK_TIMEOUT_S = 5
CLOCK_URLS = {"deepseek": "https://api.deepseek.com/user/balance"}

_SERVICE = """[Unit]
Description=helm off-peak gate: close paid providers during their vendor's peak window

[Service]
Type=oneshot
WorkingDirectory=%(cwd)s
Environment=HELM_CHAT_NAME=offpeak-gate
UnsetEnvironment=CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION HELM_SESSION_ID
ExecStart=%(helm)s offpeak --apply
"""

_TIMER = """[Unit]
Description=helm off-peak gate boundaries (explicit UTC, derived from the catalog)

[Timer]
%(calendar)s
OnCalendar=*:0/15
OnBootSec=1min
OnUnitActiveSec=1min
Persistent=true
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


def now():
    """The clock every caller reads; tests pin it here."""
    return time.time()


def window_error(window):
    """Why a declared billing window cannot be evaluated, else None."""
    if not isinstance(window, dict):
        return "a billing window must be a table"
    extra = sorted(set(window) - WINDOW_KEYS)
    missing = sorted(_REQUIRED - set(window))
    if extra or missing:
        return "billing window keys: unknown %s, missing %s" % (extra, missing)
    if window["tz"] != "UTC":
        return "billing window tz must be UTC (got %r)" % (window["tz"],)
    days = window["peak_days"]
    if not isinstance(days, tuple) or not days or any(
            type(d) is not int or not 0 <= d <= 6 for d in days):
        return "peak_days must be a non-empty tuple of weekday ints 0-6"
    peak = window["peak"]
    if not isinstance(peak, tuple) or not peak:
        return "peak must be a non-empty tuple of (HH:MM, HH:MM) spans"
    for span in peak:
        if not (isinstance(span, tuple) and len(span) == 2
                and all(isinstance(t, str) and _HHMM.match(t) for t in span)):
            return "peak span %r is not (HH:MM, HH:MM)" % (span,)
        if _minutes(span[0]) >= _minutes(span[1]):
            return "peak span %r must start before it ends" % (span,)
    lead = window.get("guard_lead_s")
    lag = window.get("guard_lag_s")
    skew = window.get("clock_max_skew_s")
    if type(lead) is not int or not 0 <= lead <= 3600:
        return "guard_lead_s must be an int of 0-3600 seconds"
    if type(lag) is not int or not 0 <= lag <= 3600:
        return "guard_lag_s must be an int of 0-3600 seconds"
    if type(skew) is not int or not 1 <= skew <= min(lead, lag):
        return ("clock_max_skew_s must be an int of 1-min(guard_lead_s, "
                "guard_lag_s) seconds")
    for key in ("offpeak_dates", "peak_dates"):
        dates = window.get(key, ())
        if not isinstance(dates, tuple) or any(
                not isinstance(d, str) or not _DATE.match(d) for d in dates):
            return "%s must be a tuple of YYYY-MM-DD strings" % key
    return None


def _minutes(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _peak_day(window, day):
    stamp = day.isoformat()
    if stamp in window.get("peak_dates", ()):
        return True
    if stamp in window.get("offpeak_dates", ()):
        return False
    return day.weekday() in window["peak_days"]


def _utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)


def in_peak(window, epoch):
    """THE ONE PREDICATE: does the vendor bill ``epoch`` at its peak price?"""
    at = _utc(epoch)
    if not _peak_day(window, at.date()):
        return False
    minute = at.hour * 60 + at.minute
    return any(_minutes(a) <= minute < _minutes(b) for a, b in window["peak"])


def guarded(window, epoch):
    """Is the provider CLOSED: peak, within its lead, or within its lag."""
    return in_peak(window, epoch) or \
        in_peak(window, epoch + window.get("guard_lead_s", 0)) or \
        in_peak(window, epoch - window.get("guard_lag_s", 0))


def _edges(window, epoch):
    """Every instant the guard may change, sorted, around ``epoch``."""
    lead = window.get("guard_lead_s", 0)
    lag = window.get("guard_lag_s", 0)
    first = _utc(epoch).date() - datetime.timedelta(days=_HORIZON_DAYS)
    out = []
    for n in range(2 * _HORIZON_DAYS + 1):
        day = first + datetime.timedelta(days=n)
        if not _peak_day(window, day):
            continue
        base = datetime.datetime(day.year, day.month, day.day,
                                 tzinfo=datetime.timezone.utc).timestamp()
        for a, b in window["peak"]:
            out += [base + _minutes(a) * 60 - lead,
                    base + _minutes(b) * 60 + lag]
    return sorted(out)


def next_change(window, epoch):
    """The first instant after ``epoch`` the guard flips, or None."""
    state = guarded(window, epoch)
    return next((e for e in _edges(window, epoch)
                 if e > epoch and guarded(window, e) != state), None)


def last_change(window, epoch):
    """The latest instant at or before ``epoch`` the guard took its state."""
    state = guarded(window, epoch)
    earlier = [e for e in _edges(window, epoch) if e <= epoch]
    for edge in reversed(earlier):
        if guarded(window, edge - 1) != state:
            return edge
    return None


def iso(epoch):
    return None if epoch is None else \
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def describe(window):
    """One human line naming the declared window and the holiday policy."""
    days = ",".join(_DAYS[d] for d in window["peak_days"])
    spans = " and ".join("%s-%s" % span for span in window["peak"])
    holidays = ("%d off-peak holiday date(s) declared"
                % len(window.get("offpeak_dates", ()))
                if window.get("offpeak_dates") else
                "public holidays count as peak (no holiday table)")
    return ("%s peak %s UTC on %s, guard from %ds early to %ds late, "
            "vendor-clock skew <=%ds; %s" % (
                window["vendor"], spans, days, window.get("guard_lead_s", 0),
                window.get("guard_lag_s", 0), window["clock_max_skew_s"],
                holidays))


def gated_providers(fam):
    """[(proxy provider name, window)] for every pool row declaring one."""
    rows = (fam or {}).get("pool_providers")
    out = []
    for name, row in (rows.items() if isinstance(rows, dict) else ()):
        if isinstance(row, dict) and row.get("billing_window"):
            out.append((row.get("proxy_provider") or name,
                        row["billing_window"]))
    return out


def _family(family, fam=None):
    if fam is not None:
        return fam
    from . import seat
    return seat.FAMILIES.get(family) or {}


_CLOCK_CACHE = {}
_CLOCK_PROOF_FRESH_S = 60
_CLOCK_FAILURE_FRESH_S = 15


def _vendor_date(window):
    """The vendor's HTTPS Date epoch, or (None, a fail-closed reason)."""
    import http.client
    import urllib.error
    import urllib.request
    from .moneyread import _RefuseRedirect
    vendor = window["vendor"]
    url = CLOCK_URLS.get(vendor)
    if not url:
        return None, "%s vendor clock URL is undeclared" % vendor
    request = urllib.request.Request(url, method="HEAD", headers={
        "Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        reply = urllib.request.build_opener(_RefuseRedirect).open(
            request, timeout=CLOCK_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return None, "%s vendor clock redirected" % vendor
        header = exc.headers.get("Date") if exc.headers else None
    except (urllib.error.URLError, http.client.HTTPException, OSError,
            ValueError):
        return None, "%s vendor clock is unreachable" % vendor
    else:
        with reply:
            header = reply.headers.get("Date")
    if not header:
        return None, "%s vendor clock response has no Date header" % vendor
    try:
        stamp = email.utils.parsedate_to_datetime(header)
        if stamp.tzinfo is None:
            raise ValueError("Date header has no timezone")
        return stamp.timestamp(), None
    except (TypeError, ValueError, OverflowError):
        return None, "%s vendor clock Date header is malformed" % vendor


def clock_error(window):
    """Why local UTC lacks a fresh bounded proof from this vendor, else None.

    THE SKEW IS THE LIVE HOST CLOCK AGAINST THE VENDOR'S, never against a
    caller's evaluation instant. proxywatch evaluates the `now` its pass began
    with after the pass's canaries have run (a pass measured 137 s on the
    fleet host), and comparing that instant with the vendor's Date read its
    own age as clock skew: every off-peak pass withheld the paid seat's
    canary and runtime proof although the host clock was exact."""
    vendor = window["vendor"]
    proof = _CLOCK_CACHE.get(vendor)
    age = time.monotonic() - proof["mono"] if proof else None
    if not (proof and 0 <= age <= proof["fresh_s"]):
        server, err = _vendor_date(window)
        proof = {"mono": time.monotonic(), "server": server, "error": err,
                 "fresh_s": (_CLOCK_FAILURE_FRESH_S if err else
                             _CLOCK_PROOF_FRESH_S)}
        _CLOCK_CACHE[vendor] = proof
    if proof["error"]:
        return proof["error"]
    expected = proof["server"] + (time.monotonic() - proof["mono"])
    skew = abs(now() - expected)
    limit = window["clock_max_skew_s"]
    if skew > limit:
        return ("%s vendor clock skew %.1fs exceeds %ds" %
                (vendor, skew, limit))
    return None


def gate(family, fam=None, at=None, prove_clock=None):
    """[{provider, window, closed, since, until}] — this family's gates now."""
    fam = _family(family, fam)
    prove_clock = at is None if prove_clock is None else prove_clock
    at = now() if at is None else at
    out = []
    for provider, window in gated_providers(fam):
        scheduled = guarded(window, at)
        clock = None if scheduled or not prove_clock else clock_error(window)
        out.append({"provider": provider, "window": window,
                    "closed": scheduled or bool(clock),
                    "clock_error": clock,
                    "since": None if clock else last_change(window, at),
                    "until": None if clock else next_change(window, at)})
    return out


def _config_providers(family, seat_name):
    """The provider block names a minted seat's config carries, or None when
    there is no readable config (the catalog's pool then stands in)."""
    from . import seat, seat_launch_assets as sla
    try:
        with open(os.path.join(seat._proxy_home(family, seat_name),
                               "config.yaml"), encoding="utf-8") as f:
            _prefix, blocks = sla._top_blocks(f.read())
        _head, items = sla._provider_sections(
            dict(blocks).get("openai-compatibility", ""))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return {name for name, _item in items if name}


def _all_closed(family, fam, at, seat_name=None, prove_clock=False):
    """The gates when EVERY route the seat can take is closed, else [].

    THE CONFIG DECIDES, NOT THE CATALOG, because the proxy routes what its
    config carries: a seat whose config holds only the gated block has no
    open route at peak even if its family's pool declares another, and a
    seat carrying an open block beside the gated one is never held. With no
    readable config the family's pool rows stand in."""
    fam = _family(family, fam)
    gates = gate(family, fam, at, prove_clock=prove_clock)
    if not gates:
        return []
    routes = _config_providers(family, seat_name) if seat_name else None
    if routes is None:
        rows = fam.get("pool_providers")
        routes = {row.get("proxy_provider") or name
                  for name, row in (rows.items() if isinstance(rows, dict)
                                    else ()) if isinstance(row, dict)}
    closed = {g["provider"] for g in gates if g["closed"]}
    if not routes or not routes <= closed:
        return []
    return [g for g in gates if g["provider"] in routes]


def seat_pause(seat_name, family, at=None, fam=None, prove_clock=False):
    """The delivery-pause record for a seat whose every route is closed, or
    None — the same shape `poolwall.pause` gives `delivery_pause`."""
    at = now() if at is None else at
    gates = _all_closed(family, fam, at, seat_name,
                        prove_clock=prove_clock)
    if not gates:
        return None
    until = min((g["until"] for g in gates if g["until"]), default=None)
    since = max((g["since"] for g in gates if g["since"]), default=None)
    return {"family": family, "state": STATE, "since": iso(since),
            "until": iso(until),
            "age_s": max(0, at - since) if since else None, "stale": False,
            "seat": seat_name, "reason": hold_reason(seat_name, gates, until)}


def hold_reason(seat_name, gates, until):
    clock = next((g.get("clock_error") for g in gates
                  if g.get("clock_error")), None)
    why = ("vendor clock is unproved (%s)" % clock if clock else
           "the vendor's peak window (%s)" % describe(gates[0]["window"]))
    return ("%s is OFF-PEAK-ONLY: provider %s is closed for %s — the keystroke "
            "is withheld until %s; rows addressed to it stay owed" % (
                seat_name, "+".join(g["provider"] for g in gates), why,
                iso(until) or "UNKNOWN"))


def canary_hold(family, at=None, fam=None, seat_name=None,
                prove_clock=False):
    """Why this seat's canary must not run now, or "" — a closed provider
    answers the proxy's own 502, which is the gate and not a dark upstream."""
    at = now() if at is None else at
    gates = _all_closed(family, fam, at, seat_name,
                        prove_clock=prove_clock)
    if not gates:
        return ""
    clock = next((g.get("clock_error") for g in gates
                  if g.get("clock_error")), None)
    return "not canaried: the off-peak gate holds %s closed until %s%s" % (
        "+".join(g["provider"] for g in gates),
        iso(min((g["until"] for g in gates if g["until"]), default=None)),
        " (clock: %s)" % clock if clock else "")


def status_line(family, fam=None, at=None, present=None):
    """``OFF-PEAK-ONLY: ...`` for a family whose every route is gated, an
    ``off-peak route ...`` line for one gated route beside an open one, and ""
    for every other. ``present`` limits it to the provider blocks a seat's
    config actually carries."""
    fam = _family(family, fam)
    gates = [g for g in gate(family, fam, at)
             if present is None or g["provider"] in present]
    if not gates:
        return ""
    parts = ["%s %s until %s" % (
        g["provider"],
        "CLOSED (clock unproved: %s)" % g["clock_error"]
        if g.get("clock_error") else
        "CLOSED (peak guard)" if g["closed"] else "OPEN",
        iso(g["until"]) or "UNKNOWN") for g in gates]
    only = present <= {g["provider"] for g in gates} if present is not None \
        else len(gated_providers(fam)) == len(fam.get("pool_providers") or ())
    return ("OFF-PEAK-ONLY: " if only else "off-peak route: ") + \
        "; ".join(parts)


def seat_line(family, name, at=None):
    """The status line for one minted seat, limited to its config's blocks."""
    if not gated_providers(_family(family)):
        return ""
    return status_line(family, at=at, present=_config_providers(family, name))


def _set_disabled(item, closed):
    """One provider item with the gate's reversible disabled override."""
    lines = item.splitlines(True)
    kept = []
    skip_gate = False
    for line in lines:
        if line.startswith(_PRESERVED):
            try:
                kept.append(json.loads(line[len(_PRESERVED):]))
                skip_gate = True
                continue
            except (TypeError, ValueError):
                pass
        if re.match(r"^    disabled\s*:", line) and MARK in line:
            if skip_gate:
                skip_gate = False
            continue
        skip_gate = False
        kept.append(line)
    if not closed:
        return "".join(kept)

    # the facade first: it seeds the names the implementation modules share
    from . import seat, seat_launch_assets as sla  # noqa: F401
    restored = "".join(kept)
    if sla._provider_disabled(restored):
        return restored
    flags = [i for i, line in enumerate(kept)
             if re.match(r"^    disabled\s*:", line)]
    flag = "    disabled: true  %s\n" % MARK
    if len(flags) == 1:
        i = flags[0]
        kept[i:i + 1] = [_PRESERVED + json.dumps(kept[i]) + "\n", flag]
    elif not flags:
        if re.match(r"^  - [A-Za-z0-9_-]+:\s*\S", kept[0]):
            kept.insert(1, flag)
        else:
            if not kept[-1].endswith("\n"):
                kept[-1] += "\n"
            kept.append(flag)
    return "".join(kept)


def apply_gate(text, family, fam=None, at=None, closed=None, prove_clock=None):
    """The config text with every gated provider block flagged as the clock
    calls for. ``closed`` overrides the clock with an explicit set of provider
    names (the plan's "every gate open" comparison passes ``()``). An
    operator's own unmarked ``disabled`` line is never removed or doubled."""
    # the facade first: it seeds the names the implementation modules share
    from . import seat, seat_launch_assets as sla  # noqa: F401
    gates = gate(family, fam, at, prove_clock=prove_clock)
    if not gates:
        return text
    names = {g["provider"] for g in gates}
    shut = {g["provider"] for g in gates if g["closed"]} if closed is None \
        else set(closed) & names
    prefix, blocks = sla._top_blocks(text)
    out = [prefix]
    for key, body in blocks:
        if key != "openai-compatibility":
            out.append(body)
            continue
        head, items = sla._provider_sections(body)
        out.append(head)
        for name, item in items:
            out.append(_set_disabled(item, name in shut)
                       if name in names else item)
    return "".join(out)


def closing_transition(old, desired, family, fam=None):
    """Did this plan turn any currently billed route from enabled to disabled?"""
    # the facade first: it seeds the names the implementation modules share
    from . import seat, seat_launch_assets as sla  # noqa: F401
    names = {provider for provider, _window in
             gated_providers(_family(family, fam))}
    if not names:
        return False

    def items(text):
        _prefix, blocks = sla._top_blocks(text)
        _head, rows = sla._provider_sections(
            dict(blocks).get("openai-compatibility", ""))
        return dict(rows)

    before, after = items(old), items(desired)
    return any(name in before and name in after
               and not sla._provider_disabled(before[name])
               and sla._provider_disabled(after[name]) for name in names)


def _minted_gated():
    from . import seat
    for family, name in seat._minted_seats():
        if gated_providers(seat.FAMILIES.get(family) or {}):
            yield family, name


def _config_agrees(family, name, at):
    from . import seat
    path = os.path.join(seat._proxy_home(family, name), "config.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        return apply_gate(text, family, at=at, prove_clock=True) == text, None
    except (OSError, ValueError) as exc:
        return None, exc.__class__.__name__


def rows(at=None):
    """One row per minted seat of a gated family: gate state + config truth."""
    at = now() if at is None else at
    out = []
    for family, name in _minted_gated():
        agrees, err = _config_agrees(family, name, at)
        out.append({"seat": name, "family": family,
                    "gates": [{"provider": g["provider"],
                               "closed": g["closed"],
                               "clock_error": g["clock_error"],
                               "until": iso(g["until"]),
                               "window": describe(g["window"])}
                              for g in gate(family, at=at,
                                            prove_clock=True)],
                    "config": "UNREADABLE (%s)" % err if err else
                    "agrees" if agrees else "DRIFTED"})
    return out


def apply(at=None):
    """Reconcile every gated seat through the supervisor's own door, then
    ledger the balance behind each gated key (the timer's cadence is the
    burn meter's)."""
    from . import seat
    out = []
    for family, name in _minted_gated():
        label, state, detail = seat._ensure_row(family, name)
        out.append({"seat": label, "family": family, "state": state,
                    "detail": detail, "status": seat_line(family, name, at)})
    try:
        record_balances(at)
    except Exception as exc:              # noqa: BLE001 — money is a report
        out.append({"seat": "-", "family": "-", "state": "healthy",
                    "detail": "balance read failed: %s"
                    % exc.__class__.__name__, "status": ""})
    return out


# ---------------------------------------------------------------- the money
#
# MAKE IT LAST, MEASURED. The prepaid balance behind a gated key is read from
# the vendor (a free GET that spends nothing), one ledger row per read, and
# the days left are the balance over the MEASURED burn: the drops between
# consecutive reads (a rise is a top-up and burns nothing) over the time they
# span. Every token the gate lets through is off-peak, so this is the
# off-peak burn by construction. The key goes in one header to the one URL
# below, never follows a redirect, and never reaches the ledger: an account is
# the key's eight-hex digest, the join the usage ledger already writes.

BALANCE_LEDGER = "offpeak-balance.jsonl"
BALANCE_URLS = {"deepseek": "https://api.deepseek.com/user/balance"}
BALANCE_TIMEOUT_S = 10
_BURN_MIN_SPAN_S = 3600
_BURN_HORIZON_S = 7 * 86400


def balance_ledger_path():
    from . import home
    return os.path.join(home.global_dir(), BALANCE_LEDGER)


def _gated_keys(family, name):
    """[(vendor, key)] off the gated blocks of one seat's config, disabled or
    not: reading a balance spends nothing, so a closed block still lends it."""
    from . import seat, seat_launch_assets as sla
    fam = _family(family)
    vendors = {p: w["vendor"] for p, w in gated_providers(fam)}
    try:
        with open(os.path.join(seat._proxy_home(family, name),
                               "config.yaml"), encoding="utf-8") as f:
            _prefix, blocks = sla._top_blocks(f.read())
        _head, items = sla._provider_sections(
            dict(blocks).get("openai-compatibility", ""))
    except (OSError, ValueError):
        return []
    out = []
    for block_name, item in items:
        if block_name not in vendors or vendors[block_name] not in BALANCE_URLS:
            continue
        try:
            keys = sla._provider_info(item, None)["api_keys"]
        except ValueError:
            continue
        out += [(vendors[block_name], k) for k in keys if k]
    return out


def fetch_balance(vendor, key, timeout=BALANCE_TIMEOUT_S):
    """({"usd", "available"}, None) or (None, failure word) — one GET."""
    import urllib.error
    import urllib.request
    from .moneyread import _RefuseRedirect
    request = urllib.request.Request(BALANCE_URLS[vendor], headers={
        "Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(_RefuseRedirect).open(
                request, timeout=timeout) as reply:
            data = json.loads(reply.read(1 << 16).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, "http-%s" % exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None, "network"
    infos = data.get("balance_infos") if isinstance(data, dict) else None
    usd = [i for i in (infos or ()) if isinstance(i, dict)
           and i.get("currency") == "USD"]
    try:
        return {"usd": float(usd[0]["total_balance"]),
                "available": data.get("is_available") is True}, None
    except (IndexError, KeyError, TypeError, ValueError):
        return None, "schema"


def record_balances(at=None, fetch=None):
    """Read and ledger the balance behind every gated seat's key -> rows."""
    import hashlib
    from . import eventledger
    at = now() if at is None else at
    fetch = fetch_balance if fetch is None else fetch
    seen, rows = set(), []
    for family, name in _minted_gated():
        for vendor, key in _gated_keys(family, name):
            account = hashlib.sha256(key.encode()).hexdigest()[:8]
            if (vendor, account) in seen:
                continue
            seen.add((vendor, account))
            reading, why = fetch(vendor, key)
            row = {"v": 1, "id": "%s-%s-%d" % (vendor, account, at * 1000),
                   "ts": at, "vendor": vendor, "account": account,
                   "usd": reading["usd"] if reading else None,
                   "available": reading["available"] if reading else None,
                   "error": why}
            eventledger.append(balance_ledger_path(), row)
            rows.append(row)
    return rows


def burn_per_day(rows, at, horizon_s=_BURN_HORIZON_S):
    """USD per day from the drops between consecutive reads, or None when
    the reads span less than an hour."""
    reads = sorted((r["ts"], r["usd"]) for r in rows
                   if isinstance(r.get("usd"), (int, float))
                   and isinstance(r.get("ts"), (int, float))
                   and at - horizon_s <= r["ts"] <= at)
    if len(reads) < 2 or reads[-1][0] - reads[0][0] < _BURN_MIN_SPAN_S:
        return None
    spent = sum(max(0.0, a[1] - b[1]) for a, b in zip(reads, reads[1:]))
    return spent * 86400 / (reads[-1][0] - reads[0][0])


def balance_lines(at=None, rows=None):
    """One line per vendor account the ledger holds, newest read first."""
    at = now() if at is None else at
    if rows is None:
        from . import eventledger
        rows, _err = eventledger.checked_events(balance_ledger_path())
    by = {}
    for r in rows or ():
        if isinstance(r, dict) and r.get("v") == 1:
            by.setdefault((r.get("vendor"), r.get("account")), []).append(r)
    out = []
    for (vendor, account), mine in sorted(by.items(), key=str):
        good = [r for r in mine if isinstance(r.get("usd"), (int, float))]
        if not good:
            out.append("%s %s balance UNREAD (%s)" % (
                vendor, account, mine[-1].get("error") or "no read"))
            continue
        last = max(good, key=lambda r: r["ts"])
        burn = burn_per_day(good, at)
        tail = ("burn unmeasured (two reads an hour apart are needed)"
                if burn is None else
                "burn $%.2f/day, all of it off-peak -> %s" % (
                    burn, "no measured spend" if burn <= 0 else
                    "~%.1f days left" % (last["usd"] / burn)))
        out.append("%s %s balance $%.2f (read %s); %s" % (
            vendor, account, last["usd"], iso(last["ts"]), tail))
    return out


def calendar_lines(windows):
    """OnCalendar lines (explicit UTC) for every close and open edge."""
    by_time = {}
    dates = set()
    for window in windows:
        lead = window.get("guard_lead_s", 0)
        lag = window.get("guard_lag_s", 0)
        for day in window["peak_days"]:
            for a, b in window["peak"]:
                for offset in (_minutes(a) * 60 - lead,
                               _minutes(b) * 60 + lag):
                    total = (day * 86400 + offset) % (7 * 86400)
                    clock = time.strftime("%H:%M:%S", time.gmtime(total % 86400))
                    by_time.setdefault(clock, set()).add(total // 86400)
        for stamp in window.get("peak_dates", ()):
            base = datetime.datetime.strptime(stamp, "%Y-%m-%d").replace(
                tzinfo=datetime.timezone.utc).timestamp()
            for a, b in window["peak"]:
                for offset in (_minutes(a) * 60 - lead,
                               _minutes(b) * 60 + lag):
                    dates.add(time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.gmtime(base + offset)))
    lines = ["OnCalendar=%s *-*-* %s UTC" % (
        ",".join(_DAYS[d] for d in sorted(days)), clock)
        for clock, days in sorted(by_time.items())]
    return lines + ["OnCalendar=%s UTC" % stamp for stamp in sorted(dates)]


def timer_units(table=None, inputs=None):
    """`inputs` replaces per-install values (timerhealth.unit_values)."""
    from . import seat, timerhealth, work
    table = seat.FAMILIES if table is None else table
    windows = [w for fam in table.values()
               for _p, w in gated_providers(fam)]
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    udir = timerhealth.user_unit_dir()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    return (os.path.join(udir, _SERVICE_NAME),
            _SERVICE % timerhealth.unit_values({"helm": helm_bin, "cwd": cwd},
                                               inputs),
            os.path.join(udir, _TIMER_NAME),
            _TIMER % timerhealth.unit_values(
                {"calendar": "\n".join(calendar_lines(windows))}, inputs))


def install_timer():
    """(ok, detail) — write the units, reload, enable --now. Idempotent."""
    from . import timerhealth
    spath, service, tpath, timer = timer_units()
    if "OnCalendar=" not in timer.split("OnCalendar=*:0/15")[0]:
        return False, "no family declares a billing window; nothing to time"
    # The bare `systemctl` on PATH, and one sentence for either step's
    # failure: this installer's own report, which names no verb.
    error, _unchanged = timerhealth.install_user_timer(
        ((spath, service), (tpath, timer)), (_TIMER_NAME,),
        subprocess=subprocess, timeout=SYSTEMCTL_TIMEOUT_S,
        clean=lambda r: " ".join((r.stderr or r.stdout
                                  or "exit %d" % r.returncode).split())[:300],
        failed="systemctl --user failed: %(detail)s")
    if error:
        return False, error
    return True, "enabled %s (%s)" % (_TIMER_NAME, tpath)


_USAGE = """usage: helm offpeak [--json] | --apply [--json] | --install-timer

  A paid provider whose catalog pool row declares a billing_window spends
  only OUTSIDE its vendor's peak. Bare: each gated seat's window, whether its
  provider is OPEN or CLOSED now, the next change (UTC) and whether its proxy
  config agrees. --apply reconciles every gated seat through the supervisor's
  own door (`seat doctor --ensure`'s row): the config gets or loses the
  marked `disabled: true` line and the proxy restarts on it. If a verified
  listener cannot be replaced during a close, fail closed: keep the closed
  config and leave it stopped rather than restore a peak-billed route.
  A route opens only after the vendor's own HTTPS Date proves host skew within
  its declared bound; the schedule closes early and opens late by more than
  that bound. --install-timer installs helm-offpeak.timer, whose OnCalendar
  lines are derived in explicit UTC with a one-minute monotonic backstop.
  Public holidays count as PEAK unless the window's offpeak_dates names the
  date: without a holiday table the gate never spends at the peak price.
"""


def cmd_offpeak(args):
    from .cli import guard_tail
    args = list(args or ())
    rc = guard_tail("helm offpeak", args,
                    flags=("--json", "--apply", "--install-timer"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    if "--install-timer" in args and len(args) > 1:
        print(_USAGE, file=sys.stderr)
        return 2
    if "--install-timer" in args:
        ok, detail = install_timer()
        print("helm offpeak: %s" % detail, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    as_json = "--json" in args
    if "--apply" in args:
        result = apply()
        unknown = sum(1 for r in result if r["state"] == "unknown")
        if as_json:
            print(json.dumps({"rows": result, "unknown": unknown},
                             indent=2, sort_keys=True))
        else:
            for r in result:
                print("%-10s %-9s %s — %s" % (r["seat"], r["state"].upper(),
                                              r["detail"], r["status"]))
            if not result:
                print("helm offpeak: no minted seat has a gated provider")
        return 2 if unknown else 0
    table = rows()
    if as_json:
        print(json.dumps({"rows": table, "now": iso(now())}, indent=2,
                         sort_keys=True))
        return 0
    if not table:
        print("helm offpeak: no minted seat has a gated provider")
    for line in balance_lines():
        print(line)
    for r in table:
        for g in r["gates"]:
            reason = ("  clock %s" % g["clock_error"]
                      if g.get("clock_error") else "")
            print("%-10s %-15s %-6s until %s  config %s%s\n           %s" % (
                r["seat"], g["provider"],
                "CLOSED" if g["closed"] else "OPEN", g["until"] or "UNKNOWN",
                r["config"], reason, g["window"]))
    return 0


__all__ = ["STATE", "MARK", "window_error", "in_peak", "guarded",
           "next_change", "last_change", "describe", "gated_providers",
           "gate", "clock_error", "seat_pause", "canary_hold", "status_line",
           "seat_line",
           "apply_gate",
           "rows", "apply", "calendar_lines", "timer_units", "install_timer",
           "cmd_offpeak", "now", "iso", "fetch_balance", "record_balances",
           "burn_per_day", "balance_lines", "balance_ledger_path"]
