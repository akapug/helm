#!/usr/bin/env python3
"""helm codex budget — what the POOLED codex creds have left, on EVERY window.

THE FAILURE THIS MODULE PREVENTS (task/2480, owner: "dunno how to make those
ultra creds last longer"). A pooled codex account can be weekly-capped while
its 5h window still looks healthy, and a reader that paces on the short window
calls that account fine and keeps routing into a wall:

    primary_window   (5h) used_percent 52   -> looks like headroom
    secondary_window (7d) used_percent 100  -> limit_reached true

TWO PROPERTIES THIS MODULE OWNS:
  1. THE RIGHT WINDOW. The binding window for codex is the FULLEST
     account-wide window, never the shortest one. `binding_gauge` below.
     providers._primary prefers the account-wide SESSION window, which is
     correct for anthropic and wrong here.
  2. THE RIGHT FILE. The live codex credential is the one in the SEAT PROXY's
     auth-dir, not the one in a codex CLI home: the codex CLI refreshes a home
     only while it is running in it, and the proxy refreshes its own pooled
     copy. A codex home can therefore hold an access token weeks old that the
     vendor answers HTTP 401 for — "Could not parse your authentication token"
     — while the pooled token for the SAME account answers 200. Reading the
     home yields task/2283's `needs_reauth` during a wall: a true statement
     about bytes nobody serves, printed in place of the budget the fleet is
     actually spending.

LAWS:
  - A TOKEN NEVER REACHES STDOUT, A LOG, OR THE CACHE. Rows carry email /
    account_id / plan / percentages only.
  - AN UNREADABLE ACCOUNT IS `unknown`, NEVER 0%. A rejected token or an
    unreachable endpoint says nothing about headroom, and a zero there would
    read as "wide open" — the precise inversion that routes work into a wall.
  - ONE PROBE PER IDENTITY. Two pool files can carry one account_id (alias
    logins share quota, e.g. two members of one Team workspace);
    probing each would spend two calls on one budget and court the vendor's
    own 429.
  - THE PARSER IS providers._codex_gauges, NOT A SECOND ONE. It already turns
    primary_window/secondary_window into labelled gauges and already suffixes
    the scoped `additional_rate_limits` entries, which is exactly the
    account-wide-versus-metered split the ceiling needs.
"""
import json
import os
import time
import urllib.error

from . import home
from . import pk


# THE OWNER'S KNOB. A fleet-wide soft ceiling on the LONGEST window: at or past
# it, codex is spending the week's budget, whatever the 5h window says.
CEILING_ENV = "HELM_CODEX_WEEKLY_CEILING_PCT"
DEFAULT_CEILING_PCT = 90.0

# The budget snapshot the dispatch gate reads. proxywatch's timed pass is the
# WRITER; the gate never probes (see `cached_budget`).
#
# IT LIVES BESIDE proxywatch's OWN STATE, under the helm home, NOT under the
# cache root. The cache root is anchored on the real $HOME and survives an
# HELM_HOME override, so a snapshot there would have made every dispatch test
# in the tree read THIS machine's live pool — and on a weekly-capped host that
# is a guard refusing work in somebody else's suite. Anchoring on helm_home
# means one env var isolates it, exactly as it does for the watch state.
SNAPSHOT_NAME = "codex-pool-budget.json"
GATE_MAX_AGE_S = 3600

# WHAT THE POOL DIRECTORY ITSELF ANSWERED, and the three answers are three
# different facts (task/2480 R2/R3). Collapsing any two of them is how an
# unread pool becomes a complete census, or a retired pool keeps refusing:
#   measured    — the directory listed and it holds pooled records.
#   known-empty — the directory listed and holds NO pooled record (or does not
#                 exist at all). This is a MEASUREMENT: the pool is empty. It
#                 RETIRES an older refusal, because the accounts that justified
#                 it are provably gone.
#   unread      — the directory could not be enumerated. Nothing is known, so
#                 nothing is published and the standing snapshot is left alone.
CENSUS_MEASURED = "measured"
CENSUS_EMPTY = "known-empty"
CENSUS_UNREAD = "unread"


def ceiling_pct():
    """The soft ceiling, HELM_CODEX_WEEKLY_CEILING_PCT-settable.

    A junk or out-of-range value falls back to the default rather than raising:
    this is consulted on the dispatch path, and a typo in an env var must not
    be able to refuse the fleet's work."""
    raw = home.env("CODEX_WEEKLY_CEILING_PCT")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_CEILING_PCT
    try:
        val = float(str(raw).strip())
    except ValueError:
        return DEFAULT_CEILING_PCT
    return val if 0.0 < val <= 100.0 else DEFAULT_CEILING_PCT


def snapshot_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", SNAPSHOT_NAME)


# ------------------------------------------------------------------ the pool

def pool_accounts(census=None):
    """One row per pooled CREDENTIAL: {account_id, user_id, email, plan,
    tier, file, files, access_token}. Deduped by
    `codexhomes._same_census_member` — account id AND user id, BOTH KNOWN
    (else email, else filename) — because two spellings of one login share
    one budget while three Team members sharing one workspace id do NOT, and
    a record whose user id is UNKNOWN is never folded into a known sibling:
    the unknown half could be any member, so two such records stay two rows
    (task/2742 — measured: one row carrying both files and the first
    record's token, so one member's probe read as the other's): measured: two
    members carry the same account id, hit their 7d walls 2.5 minutes
    apart, and a census keyed on the account alone probed one of them and
    printed the reading as a third member's.

    AN UNREADABLE POOL FILE IS A CENSUS ROW, NOT AN ABSENCE (task/2480 R2).
    `codexhomes.read_pool` hands back (filename, None) for every file it
    could not open or parse, and dropping those made a pool of one capped
    account plus one unreadable file read as a COMPLETE, fully measured,
    fully capped census — the one state `verdict` refuses on. The unreadable
    file's account, type and availability are all UNKNOWN, and that
    uncertainty is precisely the reason a complete census cannot be claimed.
    So the filename stays, carrying `unread`, and `probe_record` turns it into
    an explicit UNKNOWN row that no verdict can read as a wall.

    A file that PARSES and is not a codex record is a different answer — it is
    KNOWN not to be a pooled codex credential — and it is still dropped.

    THIS FUNCTION SEES ROWS, NEVER THE READING'S KIND (task/2480 R5). An
    UNREAD census carries no records, so this answers `[]` to it exactly as it
    answers `[]` to an empty pool — which is why every caller here takes the
    PoolCensus itself and asks `census.unknown` BEFORE it asks for rows.
    Passing the census in also keeps one call's answer to one enumeration."""
    from . import codexhomes
    census = codexhomes.read_pool() if census is None else census
    out, by_key, by_key_plain = [], [], {}
    for fname, rec in census.records:
        if rec is None:
            out.append({"account_id": None, "email": None, "plan": None,
                        "tier": None, "file": fname, "files": [fname],
                        "access_token": None, "unread": True})
            continue
        if not isinstance(rec, dict) or rec.get("type") != "codex":
            continue
        if rec.get("disabled"):
            continue
        email, plan, acct_id, _exp = codexhomes._identity({"tokens": rec})
        email = rec.get("email") or email
        acct_id = rec.get("account_id") or acct_id
        user_id = codexhomes._user_id({"tokens": rec})
        twin = next((r for k, r in by_key
                     if acct_id and codexhomes._same_census_member(
                         k, (acct_id, user_id))),
                    None) if acct_id else by_key_plain.get(email or fname)
        if twin is not None:
            twin["files"].append(fname)
            continue
        row = {"account_id": acct_id, "user_id": user_id, "email": email,
               "plan": plan,
               "tier": codexhomes.tier(plan) if plan else None,
               "file": fname, "files": [fname],
               "access_token": rec.get("access_token")}
        if acct_id:
            by_key.append(((acct_id, user_id), row))
        else:
            by_key_plain[email or fname] = row
        out.append(row)
    return out


def pool_census():
    """(accounts, census) — the pool AND whether this reading is complete.

    THE EMPTY POOL AND THE UNREADABLE ONE ARE NOT THE SAME ANSWER (task/2480
    R3). `helm codex unpool` of the last account leaves a directory that lists
    and is empty: a MEASUREMENT that the pool is gone, and the one fact that
    may retire a standing all-capped refusal. A directory that will not
    enumerate is silence, and silence must change nothing.

    ONE ENUMERATION DECIDES BOTH THE ROWS AND THE EMPTINESS (task/2480 F1).
    This asked the directory TWICE: `pool_accounts` through a glob, then
    `os.listdir` to classify the empty answer. The two readings could — and
    for a pool path carrying glob metacharacters, always did — DISAGREE: the
    glob returned `[]`, the listdir then SUCCEEDED over a directory full of
    `codex-*.json`, and this function threw those names away and returned
    `known-empty` anyway. proxywatch published that empty census, `verdict`
    over `[]` is a quiet admit, and a standing all-capped refusal was retired
    by a reading that had measured nothing.

    So `codexhomes.read_pool` is the single trustworthy enumeration (one
    `os.listdir` + `fnmatch` on the NAME) and it REPORTS `unread` rather than
    answering `[]` when the directory will not enumerate. `known-empty` is
    reported only when that one enumeration SUCCEEDED and matched no
    candidate; a failure is `unread`, which publishes nothing and leaves the
    standing snapshot exactly as it was. A candidate the record reader could
    not PARSE is neither — it is an `unread` ROW inside a measured census
    (R2), and it still is.

    A POOL DIRECTORY THAT DOES NOT EXIST IS `known-empty` HERE, deliberately
    (task/2480 R5): for the BUDGET, "no pool dir" and "an empty pool dir" are
    the same fact — nothing is pooled, nothing can be spent, and that reading
    may retire a refusal. The one rung for which they differ is doctor's
    intent-vs-actual, which stat()s the dir first and so learns something this
    function cannot: that the directory DISAPPEARED between the stat and the
    read. It reads `census.kind` itself."""
    from . import codexhomes
    census = codexhomes.read_pool()
    if census.unknown:
        return [], CENSUS_UNREAD    # unenumerable: NOT a measured empty
    accounts = pool_accounts(census)
    if accounts:
        return accounts, CENSUS_MEASURED
    return [], CENSUS_EMPTY


class PoolMatch(tuple):
    """`pool_record_for`'s answer. It unpacks as (record, note), as it
    always has. `unknown` says what a missing record means: False is "this
    identity is not pooled" (the caller may read the home, as before), and
    True is "whether this identity is pooled could not be decided" (the
    caller reads UNKNOWN, and never the home or another member's file)."""

    def __new__(cls, record=None, note=None, unknown=False):
        match = tuple.__new__(cls, (record, note))
        match.unknown = unknown
        return match

    def __reduce__(self):
        # A tuple subclass copies and pickles through its ONE tuple argument,
        # which this `__new__` reads as the record: a copy came back as
        # ((record, note), None), so a copied UNKNOWN read as a hit whose
        # record is a tuple. The three fields travel as themselves.
        return PoolMatch, (self[0], self[1], self.unknown)


def pool_record_for(account_id=None, email=None, census=None, user_id=None):
    """(record, note) — the pooled record serving this identity, or (None,
    note) when the pool does not serve it. A `PoolMatch`, so `unknown` rides
    beside the pair.

    THE ACCOUNT ID NAMES A WORKSPACE, NOT A MEMBER (task/2981). On a Team
    plan every member carries the workspace id. This matched on that id
    first, so every member's `helm creds` row read the first member's file
    (measured: three members at one identical headroom and one reset, while
    one of them had walled). So a record that carries the id serves only when
    `codexhomes._serves_member` proves it is this member: user ids equal, or
    addresses equal. A plan KNOWN to be personal (`codexhomes.personal_plan`)
    has an id unique to its holder, and it still serves alone, as before. Any
    other plan (team, one helm has no word for, an unreadable one) may be a
    workspace. When the id is pooled and no record is proven to be this
    member, the answer is UNKNOWN, with a note that says why. It is never
    another member's file.

    ONE ADDRESS RULE FOR BOTH JOINS. The member join above and the email
    fallback below compare addresses through `codexhomes._same_address`
    (case-folded). The fallback matched exact case while the member join
    folded, so "Someone@Example.com" found its record by one join and not by
    the other.

    A POOL THAT WOULD NOT ENUMERATE IS A NOTE, NEVER A SILENT MISS (task/2480
    R5). `(None, None)` is the sentence "this identity is not pooled", and the
    caller renders it as an ordinary home fallback — so answering it out of an
    enumeration error would hand the operator a SECOND-best reading with
    nothing said. The reason rides the note instead. A caller that must do
    more than annotate (providers refuses the whole row) reads the census
    itself and passes it here, so the pool is enumerated ONCE per probe.

    EXACT ACCOUNT ID FIRST, AND AN EMAIL NEVER OVERRIDES A CONTRADICTION
    (task/2480 R4). The email fallback exists because a codex home can carry an
    id the pool files do not (a JWT-only identity), and it is right exactly
    while the id is ABSENT from the pool. It was applied on any id MISS, so a
    home holding account A fell through to pooled account B merely because B
    shares the login address — and the caller then probed B's token under B's
    account header and rendered the answer as A's budget. Two different
    accounts, one number, no note: the quota of one attributed to the other.

    So the fallback now asks whether the pooled row CONTRADICTS the id it was
    reached through. A pooled row with no id of its own cannot contradict one
    and is still served. A pooled row carrying a DIFFERENT known id is refused,
    and the note names BOTH ids — the operator is the only one who can say
    which identity is stale, and hiding the pair would leave them reading a
    home fallback with no idea a near-miss exists."""
    from . import codexhomes
    census = codexhomes.read_pool() if census is None else census
    if census.unknown:
        return PoolMatch(None, "%s — whether this identity is pooled is "
                               "UNKNOWN, so this reading is not the pooled "
                               "one" % census.error, unknown=True)
    rows = pool_accounts(census)
    if account_id:
        same = [r for r in rows if r.get("account_id") == account_id]
        members = [r for r in same if codexhomes._serves_member(
            r, user_id, email, len(same) > 1)]
        # the member key outranks the address when both prove a record
        hit = next((r for r in members
                    if user_id and r.get("user_id") == user_id), None) \
            or next(iter(members), None)
        if hit:
            return PoolMatch(hit)
        if same:
            who = email or ("a user id and no address" if user_id
                            else "no address and no user id")
            return PoolMatch(None, (
                "the pool holds %d credential%s on account %s and none is "
                "proven to be this member (%s). On a Team plan that id is the "
                "WORKSPACE, shared by every member, so another member's file "
                "is never read as this one. This member's budget is UNKNOWN "
                "until its own credential is pooled (`helm codex pool "
                "<name>`)" % (len(same), "" if len(same) == 1 else "s",
                              codexhomes._short(account_id), who)),
                unknown=True)
    if email:
        hit = next((r for r in rows
                    if codexhomes._same_address(r.get("email"), email)), None)
        if hit is None:
            return PoolMatch()
        pooled_id = hit.get("account_id")
        if not account_id or not pooled_id or pooled_id == account_id:
            return PoolMatch(hit)
        return PoolMatch(None, (
            "the pooled record for %s carries account %s, and this home "
            "carries account %s — one email, two account ids, so NEITHER may "
            "stand in for the other (re-pool with `helm codex pool <name>`, "
            "or fix whichever identity is stale)"
            % (email, pooled_id, account_id)))
    return PoolMatch()


# ---------------------------------------------------------------- the vendor

def _get_json(url, headers):
    from . import providers
    return providers.NativeQuotaProvider._get_json(url, headers)


def _reached_type(data):
    """The vendor sends this as an OBJECT on both team and pro plans
    ({"type": "workspace_owner_credits_depleted", "details": null}), while
    other readings in this tree treat it as a bare string. Normalize to a
    string or None so no renderer has to guess."""
    raw = (data or {}).get("rate_limit_reached_type")
    if isinstance(raw, dict):
        raw = raw.get("type")
    return str(raw) if isinstance(raw, str) and raw else None


def _label_seconds(label):
    """Window length in seconds from a gauge label ('5h', '7d'). The label is
    minted by _codex_gauges FROM limit_window_seconds, so this is a round trip,
    not a guess. 0 for anything unparseable — an unrankable window can never
    win `longest`."""
    try:
        if label.endswith("d"):
            return int(label[:-1]) * 86400
        if label.endswith("h"):
            return int(label[:-1]) * 3600
    except ValueError:
        pass
    return 0


def windows_of(data, now=None):
    """The ACCOUNT-WIDE windows, longest first: [{label, seconds,
    used_percent, reset_at, reset_after_seconds}].

    Scoped gauges (the `additional_rate_limits` metered features, which
    _codex_gauges suffixes with '-<name>') are EXCLUDED: a 0% Spark budget is
    not headroom on the account's weekly cap, and folding it in would let a
    never-used metered feature vote the pool clear.

    reset_after_seconds is derived from the gauge's reset_at rather than read
    twice from the body: `_codex_gauges` is the one parser, and the vendor's
    own reset_after_seconds is reset_at minus the instant of the read
    (measured: 5431 against reset_at 1789383626, read at 1789378195)."""
    from . import providers
    now = time.time() if now is None else now
    out = []
    for g in providers.NativeQuotaProvider._codex_gauges(data or {}):
        if "-" in g["label"]:
            continue
        reset = g.get("reset")
        out.append({"label": g["label"], "seconds": _label_seconds(g["label"]),
                    "used_percent": round(g["utilization"] * 100.0, 1),
                    "reset_at": reset,
                    "reset_after_seconds": max(0, int(reset - now))
                    if isinstance(reset, (int, float)) else None})
    out.sort(key=lambda w: -w["seconds"])
    return out


def binding_of(gauges):
    """The FULLEST account-wide gauge, in `providers._gauge` shape — the one
    `cred_state` must report headroom and reset from. See `binding_gauge`."""
    wide = [g for g in (gauges or ()) if "-" not in g["label"]]
    return max(wide, key=lambda g: (g["utilization"], _label_seconds(g["label"]))) \
        if wide else None


def binding_gauge(data, now=None):
    """The window helm must pace on: the FULLEST account-wide window.

    NOT the longest and NOT the session one. The owner's wall was a 7d at 100%
    under a 5h at 52%, so pacing on the shortest window lies; but a fresh
    weekly under a nearly-spent 5h would lie the other way. The fullest window
    is the one that stops work next, whichever it is. Ties go to the longer
    window — it takes longer to recover from."""
    ws = windows_of(data, now=now)
    return max(ws, key=lambda w: (w["used_percent"], w["seconds"])) if ws else None


class _Malformed(Exception):
    """The vendor's answer could not be understood for ONE account.

    It is an exception rather than a returned flag because the parse it guards
    is several calls deep (`_codex_gauges` -> `_gauge` -> arithmetic on the
    body's own values), and the whole point of task/2480 R5 is that exactly ONE
    boundary catches every way that parse can fail."""


def _number(value):
    """A finite int/float, or None. `True` is not a percentage: bool is an int
    subclass in Python, and a body carrying used_percent=true would otherwise
    be read as 1%."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value == value and value not in (float("inf"),
                                                     float("-inf")) else None


def _validate_windows(data):
    """Raise _Malformed unless every ACCOUNT-WIDE window in this body is a
    shape helm can read as a percentage.

    THE FORGED ZERO IS WHY THIS EXISTS. `providers._gauge` normalizes a missing
    or null percent with `(percent or 0)`, which is right for a caller
    rendering a gauge and catastrophic for one deciding whether a pool has
    headroom: a present 7d window with a null used_percent became a MEASURED
    0%, flowed to longest_pct 0.0 / state ok / status allowed, and voted the
    pool clear. An unreadable account must be UNKNOWN — the module law — and a
    body that cannot say how full a window is has not said it is empty."""
    rl = data.get("rate_limit")
    if rl is None:
        return
    if not isinstance(rl, dict):
        raise _Malformed("rate_limit is a %s, not an object"
                         % type(rl).__name__)
    for key in ("primary_window", "secondary_window"):
        w = rl.get(key)
        if w is None:
            continue                    # absent window: nothing claimed
        if not isinstance(w, dict):
            raise _Malformed("%s is a %s, not an object"
                             % (key, type(w).__name__))
        if _number(w.get("used_percent")) is None:
            raise _Malformed("%s carries no numeric used_percent (%r)"
                             % (key, w.get("used_percent")))
        for field in ("limit_window_seconds", "reset_at"):
            if w.get(field) is not None and _number(w.get(field)) is None:
                raise _Malformed("%s carries a non-numeric %s (%r)"
                                 % (key, field, w.get(field)))


def _unknown(acct, status, note):
    """An account whose budget could NOT be read. longest_pct is None, never
    0.0 — see the module law."""
    return {"account_id": acct.get("account_id"),
            "user_id": acct.get("user_id"), "email": acct.get("email"),
            "plan": acct.get("plan"), "tier": acct.get("tier"),
            "file": acct.get("file"), "files": list(acct.get("files") or ()),
            "state": "unknown", "status": status, "allowed": None,
            "reached_type": None, "note": note, "windows": [],
            "binding": None, "longest_pct": None, "gauges": [],
            "binding_gauge": None}


def probe_record(acct, get_json=None, now=None, ceiling=None):
    """One pooled account's budget row. NEVER raises; every failure degrades
    to an `unknown` row carrying the reason."""
    from . import providers
    get_json = get_json or _get_json
    now = time.time() if now is None else now
    ceiling = ceiling_pct() if ceiling is None else ceiling
    if acct.get("unread"):
        # THE CENSUS ENTRY FOR A FILE THAT WOULD NOT PARSE. Distinct from a
        # parseable record with no token, because the two send an operator to
        # different places: this one says the BYTES are unreadable.
        return _unknown(acct, "unreadable-file",
                        "pooled file %s could not be read or parsed, so the "
                        "account it holds is UNKNOWN — this census is NOT a "
                        "complete one" % (acct.get("file") or "?"))
    token = acct.get("access_token")
    if not token:
        return _unknown(acct, "no-credentials",
                        "pooled record carries no access token")
    headers = {"Authorization": "Bearer " + token}
    if acct.get("account_id"):
        headers["chatgpt-account-id"] = acct["account_id"]
    try:
        data = get_json(providers.CODEX_USAGE_URL, headers)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()          # an HTTPError IS a response object — close its fp
        return _unknown(acct,
                        "needs_reauth" if code in (401, 403) else "http_%d" % code,
                        "pooled access token rejected — the proxy refreshes "
                        "its own copy, codex refreshes the home's; re-pool "
                        "with `helm codex pool <name>`" if code in (401, 403)
                        else "usage endpoint answered http_%d" % code)
    except Exception:                       # noqa: BLE001 — never a failure
        return _unknown(acct, "network-error", "usage endpoint unreachable")
    if not isinstance(data, dict):
        return _unknown(acct, "malformed", "usage endpoint returned no object")
    # ONE PARSE BOUNDARY PER ACCOUNT (task/2480 R5). The GET had its own try
    # and everything after it ran bare, so a body the vendor answered 200 for
    # and helm could not parse RAISED out of this function — through
    # `pool_budget`'s comprehension, which then lost every HEALTHY SIBLING in
    # the same pass, and through proxywatch, which then wrote no snapshot at
    # all. This function's contract is that it never raises; that contract now
    # covers the parse as well as the call.
    try:
        _validate_windows(data)
        rl = data.get("rate_limit") or {}
        gauges = providers.NativeQuotaProvider._codex_gauges(data)
        windows = windows_of(data, now=now)
        binding = binding_gauge(data, now=now)
        allowed = rl.get("allowed")
        reached = _reached_type(data)
    except _Malformed as e:
        return _unknown(acct, "malformed", "usage body is unreadable: %s" % e)
    except Exception as e:                  # noqa: BLE001 — never a failure
        return _unknown(acct, "malformed",
                        "usage body could not be parsed (%s: %s)"
                        % (e.__class__.__name__, e))
    longest = windows[0] if windows else None
    exhausted = bool(rl.get("limit_reached")) or allowed is False \
        or any(w["used_percent"] >= 100.0 for w in windows)
    if exhausted:
        state = "exhausted"
    elif longest and longest["used_percent"] >= ceiling:
        state = "near"
    else:
        state = "ok"
    return {"account_id": acct.get("account_id"),
            # THE MEMBER HALF OF THE IDENTITY RIDES THE ROW (`codexhomes.
            # _member_key`). `pool_accounts` already resolved it, and dropping
            # it here made every consumer re-key on the account id alone — the
            # WORKSPACE id on a Team plan, shared by every member. A consumer
            # that must name ONE credential (the reset-credit policy spends an
            # irreversible asset on the one it names) cannot do that from a
            # workspace id, and cannot recover the user id from a row that
            # threw it away.
            "user_id": acct.get("user_id"),
            "email": data.get("email") or acct.get("email"),
            "plan": data.get("plan_type") or acct.get("plan"),
            "tier": acct.get("tier"), "file": acct.get("file"),
            "files": list(acct.get("files") or ()),
            "state": state, "status": "blocked" if exhausted else "allowed",
            "allowed": allowed, "reached_type": reached, "note": None,
            "windows": windows, "binding": binding,
            "longest_pct": longest["used_percent"] if longest else None,
            "gauges": gauges, "binding_gauge": binding_of(gauges)}


def pool_budget(get_json=None, now=None, ceiling=None, write_cache=True,
                accounts=None):
    """Every pooled codex identity's budget, probed once each.

    AN UNREAD POOL PROBES NOTHING AND PUBLISHES NOTHING (task/2480 R5). The
    snapshot this writes is what the dispatch gate reads, so writing an EMPTY
    one after a failed enumeration is the original defect at its loudest: a
    standing all-capped refusal retired by an error. `accounts` lets a caller
    that has ALREADY read the census (proxywatch, which must tell empty from
    unread before it gets here) hand the rows over rather than asking the
    directory a second time and risking two different answers."""
    ceiling = ceiling_pct() if ceiling is None else ceiling
    if accounts is None:
        accounts, census = pool_census()
        if census == CENSUS_UNREAD:
            return []           # nothing read, nothing probed, nothing written
    rows = [probe_record(a, get_json=get_json, now=now, ceiling=ceiling)
            for a in accounts]
    if write_cache:
        write_snapshot(rows, ceiling=ceiling, now=now)
    return rows


# ----------------------------------------------------------------- the cache

def _redacted(rows):
    """The snapshot shape — explicitly rebuilt field by field rather than
    copied, so no future field on a row can carry a token into a file the
    dispatch gate reads."""
    keep = ("account_id", "email", "plan", "tier", "file", "state", "status",
            "allowed", "reached_type", "note", "windows", "binding",
            "longest_pct")
    return [{k: r.get(k) for k in keep} for r in rows]


def write_snapshot(rows, ceiling=None, now=None, census=CENSUS_MEASURED):
    """Persist the budget for the dispatch gate. Best effort: a fleet must not
    lose a proxywatch pass because a cache dir is read-only.

    `census` records WHICH READING this is (see the constants). An empty
    `known-empty` snapshot is a real publication and deliberately overwrites an
    older populated one: it is the measurement that retires a refusal whose
    accounts have been unpooled."""
    from . import pk
    payload = {"ts": time.time() if now is None else now,
               "ceiling": ceiling_pct() if ceiling is None else ceiling,
               "census": census,
               "rows": _redacted(rows)}
    try:
        path = snapshot_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.atomic_write(path, json.dumps(payload, sort_keys=True))
        return True
    except Exception:                       # noqa: BLE001
        return False


def cached_budget(max_age_s=GATE_MAX_AGE_S, now=None):
    """(rows, age_s) from the snapshot, or (None, None).

    THE DISPATCH GATE NEVER PROBES. A send must not pay five vendor
    round-trips, and a vendor outage must never be able to stall the fleet's
    routing. proxywatch's timed pass is the writer; a stale or absent snapshot
    ADMITS (absence is not a measured contradiction)."""
    now = time.time() if now is None else now
    try:
        with pk.open_regular(snapshot_path(), encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None, None
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        return None, None
    age = now - float(payload.get("ts") or 0)
    if age < 0 or age > max_age_s:
        return None, None
    return payload["rows"], age


# --------------------------------------------------------------- the verdict

def verdict(rows, ceiling=None):
    """The fleet's codex budget decision.

    decision:
      clear   — no known account is at the ceiling
      mixed   — some known account is at it and something else is not
      capped  — EVERY pooled account is known AND at or past its longest
                window's ceiling. The only state that refuses.
      unknown — nothing could be read

    `capped` deliberately requires every row to be KNOWN. An unreadable
    account is not a measured wall, and refusing the fleet's work on absence
    is the guard-fires-on-absence bug class."""
    ceiling = ceiling_pct() if ceiling is None else ceiling
    rows = list(rows or ())
    known = [r for r in rows if r.get("longest_pct") is not None]
    unknown = [r for r in rows if r.get("longest_pct") is None]
    over = [r for r in known if r["longest_pct"] >= ceiling]
    under = [r for r in known if r["longest_pct"] < ceiling]
    if not known:
        decision = "unknown"
    elif not under and not unknown:
        decision = "capped"
    elif over:
        decision = "mixed"
    else:
        decision = "clear"
    return {"ceiling": ceiling, "decision": decision, "total": len(rows),
            "over": over, "under": under, "unknown": unknown, "rows": rows}


def _name(row):
    return row.get("email") or row.get("account_id") or row.get("file") or "?"


def _window_text(row):
    parts = []
    for w in row.get("windows") or ():
        secs = w.get("reset_after_seconds")
        when = "?" if secs is None else (
            "%dm" % (secs // 60) if secs < 3600 else "%.1fh" % (secs / 3600.0))
        parts.append("%s %.0f%% resets %s" % (w["label"], w["used_percent"], when))
    return " · ".join(parts)


def budget_lines(rows, ceiling=None):
    """The per-account surface: one line each, every window on it."""
    v = verdict(rows, ceiling=ceiling)
    out = ["  codex pool budget (ceiling %.0f%% on the longest window, %s):"
           % (v["ceiling"], CEILING_ENV)]
    for r in sorted(v["rows"], key=lambda r: _name(r)):
        detail = _window_text(r) or (r.get("note") or "no windows reported")
        reached = " reached=%s" % r["reached_type"] if r.get("reached_type") else ""
        out.append("    %-8s %-30s %-6s %s%s"
                   % (r.get("state") or "?", _name(r)[:30],
                      r.get("plan") or "?", detail, reached))
    return out


def unknown_text(v):
    """The unreadable accounts, NAMED. A count alone ("1 account(s)
    unreadable") tells an operator a census is incomplete and not which file
    to go and look at — and for an unparseable pool file the FILENAME is the
    only handle that exists (`_name` falls back to it)."""
    return ", ".join(sorted(_name(r) for r in v["unknown"]))


def unread_text(recipient, v):
    """The advisory for a pool that admits but was not fully read.

    PROMISED AND NOT DELIVERED (task/2480 R6). Both `helm dispatch`'s own help
    and docs/VERBS say any unread account proceeds AND warns, and the warning
    only ever fired when some OTHER account was over the ceiling — so a fresh
    pass where every reachable account answered 401 admitted in silence, which
    is indistinguishable from a healthy pool. The policy stays ADMIT; what
    changes is that the uncertainty is said out loud."""
    left = headroom_text(v)
    return ("recipient %r: the pooled codex census is INCOMPLETE — %d of %d "
            "accounts could NOT be read (%s). Admitted, because an unread "
            "account is not a measured wall. Headroom known: %s."
            % (recipient, len(v["unknown"]), v["total"], unknown_text(v),
               left or "none named"))


def headroom_text(v):
    """Which accounts still have room, named, with how much."""
    return ", ".join("%s %.0f%% used" % (_name(r), r["longest_pct"])
                     for r in sorted(v["under"], key=lambda r: r["longest_pct"]))


def refusal_text(recipient, v):
    return ("recipient %r is UNUSABLE right now: EVERY pooled codex account is "
            "at or past the %.0f%% ceiling on its longest window (%s). The "
            "longest window is the one that runs out — filing work here spends "
            "a budget that is gone. Wait for the reset, raise %s, or pass "
            "force=True to file it anyway."
            % (recipient, v["ceiling"],
               ", ".join("%s %.0f%%" % (_name(r), r["longest_pct"])
                         for r in v["over"]) or "no account readable",
               CEILING_ENV))


def warning_text(recipient, v):
    left = headroom_text(v)
    unknown = (" %d account(s) unreadable: %s"
               % (len(v["unknown"]), unknown_text(v))) if v["unknown"] else ""
    return ("recipient %r: %d of %d pooled codex accounts are at or past the "
            "%.0f%% weekly ceiling (%s). Headroom left: %s.%s"
            % (recipient, len(v["over"]), v["total"], v["ceiling"],
               ", ".join("%s %.0f%%" % (_name(r), r["longest_pct"])
                         for r in v["over"]),
               left or "none named", unknown))
