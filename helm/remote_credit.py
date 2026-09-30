"""The credit a driven remote session spends, and the pace it may spend it at.

A Claude Code cloud session bills the account its config dir is logged into.
The owner's promotional cloud-session credit comes as several accounts, used
ONE AT A TIME in a configured order, each with a dollar balance and an expiry.
The relay (helm/remote_relay.py) asks this module two questions before every
launch: which account pays, and whether today's pace allows the spend.

THE READING. The vendor's usage endpoint (the one helm/providers.py already
probes for every Claude home) reports the promotional credit under the key
`CREDIT_KEY`, with limit, used and remaining dollars and the expiry as its
reset time. The read is one GET with the home's live access token, which is
never printed or stored. An expired token cannot be read until Claude Code
refreshes it, so the last reading of that account stands in when it is at
most READING_MAX_AGE_S old, and says it is remembered.

THE PACE. `daily_budget` is how much may be spent today, and by default it is
NOT CAPPED: the owner never asked for promo credit to be rationed per day, and
an unset knob that spread what was left evenly to each account's expiry
refused launches he wanted. An operator who wants a cap sets it: `auto`
spreads what is left evenly to each account's expiry; a number caps it; 0
stops launches. The per-account credit floor and the concurrency cap bound
every launch either way.
WHEN THE MAX POOL IS SCARCE (the anthropic burn flag ORANGE or RED), the
expiring promo credit is spent FIRST: `auto` stops spreading it to expiry and
caps the day at a generous ceiling instead (HELM_REMOTE_SCARCE_CEILING_USD).
The flag is read from the cached snapshot and never probed; the floor holds.
`spent_today` is MEASURED from the journal's readings (used dollars now minus
used dollars at the start of the UTC day), so it lags the vendor's own meter.
The relay's concurrency cap bounds how far one tick can overshoot it.
"""
import os
import time

from . import pk, providers

#: The usage endpoint's key for the promotional cloud-session credit
#: (MEASURED: limit_dollars, used_dollars, remaining_dollars,
#: resets_at = the credit's expiry). It is the vendor's name, not ours.
CREDIT_KEY = "iguana_necktie"
#: The header the proven prototype sent. Harmless on the endpoint helm's
#: provider reads without it.
_BETA = "oauth-2025-04-20"

FLOOR_ENV = "HELM_REMOTE_CREDIT_FLOOR_USD"
DEFAULT_FLOOR_USD = 5.0
BUDGET_ENV = "HELM_REMOTE_DAILY_BUDGET_USD"
SCARCE_CEILING_ENV = "HELM_REMOTE_SCARCE_CEILING_USD"
#: Generous on purpose: while the Max pool is scarce, promo credit is the
#: cheaper spend, and the floor and the concurrency cap still bound it.
DEFAULT_SCARCE_CEILING_USD = 200.0
#: The burn-flag colours that make the Max pool scarce.
SCARCE_COLOURS = ("ORANGE", "RED")
READING_MAX_AGE_S = 24 * 3600


def _num(value):
    return value if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else None


def home_email(home):
    """The account a Claude home is logged into, from its own .claude.json,
    or None. The file is read, never written."""
    data = pk.read_json(os.path.join(home or "", ".claude.json"), {}) or {}
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    email = (account or {}).get("emailAddress") if isinstance(account, dict) \
        else None
    return email if isinstance(email, str) and email else None


def token_live(home, margin_s=120):
    """Whether the home holds an access token that will outlive `margin_s`.
    The token is looked at, never returned."""
    oauth = providers._claude_oauth(home)
    if not providers._token_live(oauth):
        return False
    exp = providers._expires_ms(oauth)
    return exp is None or exp / 1000 > time.time() + margin_s


def parse_credit(data):
    """{left, used, limit, expires} from one usage-endpoint body, or None when
    the body carries no credit block (an account with no such credit)."""
    block = (data or {}).get(CREDIT_KEY) if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return None
    return {"left": _num(block.get("remaining_dollars")),
            "used": _num(block.get("used_dollars")),
            "limit": _num(block.get("limit_dollars")),
            "expires": block.get("resets_at")
            if isinstance(block.get("resets_at"), str) else None}


def read_credit(home, get_json=None):
    """{at, account, left, used, limit, expires} or {at, account, error}.

    `get_json(url, headers)` is the HTTP seam; the default is the native
    provider's own reader, so helm keeps one way of asking this endpoint."""
    at = pk.now_ts()
    account = home_email(home)
    oauth = providers._claude_oauth(home)
    if not providers._token_live(oauth):
        return {"at": at, "account": account, "error": "no live access token"}
    get_json = get_json or providers.NativeQuotaProvider._get_json
    try:
        data = get_json(providers.ANTHROPIC_USAGE_URL, {
            "Authorization": "Bearer " + oauth["accessToken"],
            "anthropic-beta": _BETA})
    except Exception as exc:              # noqa: BLE001 — a reading, never a raise
        code = getattr(exc, "code", None)
        return {"at": at, "account": account,
                "error": ("HTTP %s" % code) if code else type(exc).__name__}
    credit = parse_credit(data)
    if credit is None:
        return {"at": at, "account": account,
                "error": "the account carries no cloud-session credit"}
    credit.update(at=at, account=account)
    return credit


def floor_usd(environ=None):
    """The balance at or below which an account is not launched on."""
    raw = (environ if environ is not None else os.environ).get(FLOOR_ENV)
    try:
        value = float(raw) if raw not in (None, "") else DEFAULT_FLOOR_USD
    except ValueError:
        return DEFAULT_FLOOR_USD
    return value if value >= 0 else DEFAULT_FLOOR_USD


def remembered(readings, account, now=None):
    """The newest reading of `account` with a balance, at most
    READING_MAX_AGE_S old, or None."""
    now = time.time() if now is None else now
    for r in reversed(readings or ()):
        if str(r.get("account") or "").casefold() != str(account).casefold():
            continue
        if r.get("error") or _num(r.get("left")) is None:
            continue
        at = pk.parse_ts_epoch(r.get("at"))
        if at is not None and now - at <= READING_MAX_AGE_S:
            return r
        return None
    return None


def choose_account(accounts, read, readings, floor, now=None):
    """(account, reading, refusals) — the FIRST account in the configured order
    whose credit is above the floor.

    `accounts` is the seat's ordered [{email, home}]. `read(account)` takes a
    live reading (after any token refresh the caller owes); a failed live
    reading falls back to the remembered one. An account nothing can read is
    SKIPPED, never spent on blind: its credit is unknown, and a launch it
    cannot pay for bills the plan instead. `refusals` names every account
    passed over and why."""
    refusals = []
    for account in accounts or ():
        email = account.get("email")
        live = read(account) or {}
        reading, basis = live, "live"
        if live.get("error"):
            reading = remembered(readings, email, now)
            basis = "remembered (%s)" % live.get("error")
        if not reading:
            refusals.append("%s: credit unreadable (%s) and no reading within "
                            "%dh" % (email, live.get("error") or "no reading",
                                     READING_MAX_AGE_S // 3600))
            continue
        left = _num(reading.get("left"))
        if left is None or _num(reading.get("limit")) is None:
            refusals.append("%s: no cloud-session credit on this account" % email)
            continue
        if left <= floor:
            refusals.append("%s: $%.2f left, at or below the $%.2f floor"
                            % (email, left, floor))
            continue
        chosen = dict(reading, basis=basis)
        return account, chosen, refusals
    return None, None, refusals


def _latest_by_account(readings):
    out = {}
    for r in readings or ():
        if r.get("error") or not r.get("account"):
            continue
        out[str(r["account"]).casefold()] = r
    return out


def max_pool_scarce():
    """The anthropic burn flag's colour when it makes the Max pool scarce
    (ORANGE or RED), else None. Read from the cached snapshot, never probed;
    an absent, stale or unreadable snapshot is None (not scarce)."""
    from . import burnflags
    try:
        flag = burnflags.family_flag(burnflags.NATIVE_FAMILY)
    except Exception:                      # noqa: BLE001 — a reading, never a raise
        return None
    colour = (flag or {}).get("colour") if isinstance(flag, dict) else None
    return colour if colour in SCARCE_COLOURS else None


def scarce_ceiling_usd(environ=None):
    raw = (environ if environ is not None else os.environ).get(
        SCARCE_CEILING_ENV)
    try:
        value = float(raw) if raw not in (None, "") else \
            DEFAULT_SCARCE_CEILING_USD
    except ValueError:
        return DEFAULT_SCARCE_CEILING_USD
    return value if value >= 0 else DEFAULT_SCARCE_CEILING_USD


def daily_budget(readings, now=None, environ=None, scarce=None):
    """(dollars or None, why). None is UNLIMITED only when nothing is known;
    the relay then leans on its concurrency cap alone and says so.

    Unset (the default): no daily cap. `auto`: for each account's latest
    reading, what is left over the days to its expiry, summed; while `scarce`
    names the Max pool's ORANGE or RED flag, the scarce ceiling instead, so
    the promo credit is spent first. `0` stops every launch. A number is a
    fixed cap."""
    now = time.time() if now is None else now
    raw = str((environ if environ is not None else os.environ)
              .get(BUDGET_ENV) or "").strip().casefold()
    if raw == "auto" and scarce:
        return scarce_ceiling_usd(environ), (
            "auto: the Max pool is %s, so promo credit spends first, up to "
            "%s" % (scarce, SCARCE_CEILING_ENV))
    if not raw:
        return None, ("no daily cap (set %s to a number, or to auto to spread "
                      "what is left to each account's expiry)" % BUDGET_ENV)
    if raw != "auto":
        try:
            value = float(raw)
        except ValueError:
            return None, "%s=%r is not a number or auto" % (BUDGET_ENV, raw)
        return max(0.0, value), "%s=%s" % (BUDGET_ENV, raw)
    total, parts = 0.0, 0
    for r in _latest_by_account(readings).values():
        left = _num(r.get("left"))
        expires = pk.parse_ts_epoch(str(r.get("expires") or "")[:19] + "Z") \
            if r.get("expires") else None
        if left is None or expires is None:
            continue
        days = max(1.0, (expires - now) / 86400.0)
        total += max(0.0, left) / days
        parts += 1
    if not parts:
        return None, "auto: no account has a reading with an expiry yet"
    return total, "auto: what is left, spread to each account's expiry"


def flat_for(readings, account, since):
    """Seconds the account's used dollars have not moved, over the readings
    taken at or after `since` (epoch seconds), or None when fewer than two
    such readings exist. The newest readings are walked back while their
    used figure equals the newest one. An ACCOUNT fact: see
    helm/remote_session.py on what it can and cannot say about one session."""
    rows = []
    for r in readings or ():
        at, used = pk.parse_ts_epoch(r.get("at")), _num(r.get("used"))
        if r.get("error") or at is None or used is None or at < since:
            continue
        if str(r.get("account") or "").casefold() == str(account).casefold():
            rows.append((at, used))
    if len(rows) < 2:
        return None
    rows.sort()
    last_at, last_used = rows[-1]
    start = last_at
    for at, used in reversed(rows):
        if used != last_used:
            break
        start = at
    return last_at - start


def spent_today(readings, now=None):
    """Dollars used since 00:00 UTC, over every account, MEASURED from the
    readings: per account, the newest used figure minus the last one before
    the day began (or the first of the day when none is older)."""
    now = time.time() if now is None else now
    day0 = now - (now % 86400)
    spent = 0.0
    by = {}
    for r in readings or ():
        used = _num(r.get("used"))
        at = pk.parse_ts_epoch(r.get("at"))
        if r.get("error") or used is None or at is None or not r.get("account"):
            continue
        by.setdefault(str(r["account"]).casefold(), []).append((at, used))
    for rows in by.values():
        rows.sort()
        before = [u for t, u in rows if t < day0]
        today = [u for t, u in rows if day0 <= t <= now]
        if not today:
            continue
        base = before[-1] if before else today[0]
        spent += max(0.0, today[-1] - base)
    return spent


def reading_line(r):
    """One line for status: never a token, only dollars and dates."""
    if r.get("error"):
        return "%s: unread (%s)" % (r.get("account") or "?", r["error"])
    return "%s: $%.2f left of $%s, expires %s (read %s)" % (
        r.get("account") or "?", r.get("left") or 0.0, r.get("limit"),
        r.get("expires") or "?", r.get("at") or "?")
