"""The LIVE copy of a Claude account: what every helm reader that MEASURES an
account reads through (task/2283).

ORCA IS THE REFERENCE. Orca keeps one managed copy of each Claude account at
<orca user data>/claude-accounts/<id>/auth/.credentials.json and is that
chain's one refresher: it rotates and persists the single-use refresh token
before it presents an access token, refreshes an account it is not using when
it measures it, and materializes the selected account into ~/.claude, reading
the CLI's own refreshes back. Every account works in Orca because each one has
exactly one maintained chain.

A helm home is another copy, maintained only while a seat runs in it or
keepalive grants on its own chain. Measuring an account through a copy nobody
maintains printed "due-refresh" and "no live access token" about accounts Orca
was using fine: the fault was the copy, never the account.

THE RULE. A reader that measures an account reads through a LIVE copy of it:
any helm home the caller scanned that holds one, else Orca's managed copy.
When no copy is live it reads Orca's OWN last measurement of the account (the
accounts.list snapshot Orca's accounts pane and phone app render), asked for
WITHOUT a refresh: no provider call and no token rotation happens on helm's
behalf. helm never refreshes Orca's copy, never writes it and never launches a
seat on it: a read presents an access token Orca already made live to one GET
of the usage endpoint, which rotates nothing.

SECRETS NEVER SURFACE: paths, expiries, emails and percentages leave this
module; token bytes never do.

THE ROSTER'S DOOR: `providers.default_provider().cred_state()` reads every
account through this module; each row names its `source` (credhome, default,
orca, orca-reading) and, for a reading Orca took, `source_at`.
"""
import os
import time

from . import cred, providers

#: The row sources, as `cred_state` stamps them.
CREDHOME = "credhome"
DEFAULT = "default"
ORCA = "orca"
ORCA_READING = "orca-reading"
#: accounts.list without a refresh answers from Orca's memory; this bounds a
#: wedged runtime.
SNAPSHOT_TIMEOUT_S = 5
#: An Orca reading older than this is not a measurement of today's account.
READING_MAX_AGE_S = 24 * 3600
ORCA_ACCOUNT_JSON = "oauth-account.json"


def orca_accounts():
    """[{email, path, oauth_account}] — each Claude account Orca manages whose
    dir proves itself Orca's by cred.orca_copy's own rules (content identity
    from oauth-account.json, the ownership marker naming the dir, one dir per
    account). Read-only; [] when there is no store."""
    root = cred.orca_accounts_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out, seen = [], set()
    for name in names:
        doc = cred._read_json(os.path.join(root, name, "auth", ORCA_ACCOUNT_JSON))
        email = cred._email_or_none(doc.get("emailAddress")) \
            if isinstance(doc, dict) else None
        if not email or email.casefold() in seen:
            continue
        seen.add(email.casefold())
        copy = cred.orca_copy(email)
        if copy.get("state") == "found":
            out.append({"email": email, "path": copy["path"], "oauth_account": doc})
    return out


def orca_copy_for(email, paths=()):
    """Orca's managed copy of the account the helm dirs in `paths` hold ->
    {email, path, oauth_account} or None. Matched the way cred.orca_copy
    matches (content email, the ownership marker, one dir per account) AND on
    organizationUuid / accountUuid wherever the homes and Orca's copy both
    carry them: one email can sit in several organizations, and another
    organization's token is another account's. Homes that disagree among
    themselves on a key prove no identity, so they get no Orca copy."""
    from .cred.orca import ID_KEYS
    ids = {}
    for p in paths:
        if not p:
            continue
        block = cred.oauth_block(p)
        for k in ID_KEYS:
            v = block.get(k)
            if isinstance(v, str) and v and ids.setdefault(k, v) != v:
                return None
    copy = cred.orca_copy(email, ids)
    if copy.get("state") != "found":
        return None
    doc = cred._read_json(os.path.join(copy["path"], ORCA_ACCOUNT_JSON))
    return {"email": email, "path": copy["path"],
            "oauth_account": doc if isinstance(doc, dict) else {}}


#: `copies(..., held=LOOK_UP)` finds Orca's copy itself; a caller that already
#: has it (or knows there is none) hands it over.
LOOK_UP = object()


def _expires_ms(oauth):
    """A claudeAiOauth block's access-token expiry in epoch ms, or None:
    providers' one reading of expiresAt (a bool is not a number there)."""
    return providers._expires_ms(oauth)


def copies(email, paths=(), held=LOOK_UP):
    """Every copy of `email` a reader may present, freshest live copy first ->
    [{path, source, has_token, expires_at, live}]. `paths` are the caller's
    own helm homes for this account (the provider's scan); Orca's managed copy
    is `held` (an orca_copy_for() answer the caller already has, or None) or,
    by default, looked up here by the identity those homes carry."""
    out = []
    default = os.path.realpath(os.path.expanduser("~/.claude"))
    for p in paths:
        if not p:
            continue
        real = os.path.realpath(os.path.expanduser(p))
        out.append({"path": real,
                    "source": DEFAULT if real == default else CREDHOME})
    if held is LOOK_UP:
        held = orca_copy_for(email, paths)
    if held:
        out.append({"path": held["path"], "source": ORCA})
    for c in out:
        oauth = providers._claude_oauth(c["path"])
        c["has_token"] = bool(oauth.get("accessToken"))
        c["expires_at"] = _expires_ms(oauth)
        c["live"] = providers._token_live(oauth)
    return sorted(out, key=lambda c: (not c["live"], -(c["expires_at"] or 0)))


def live_copy(email, paths=(), held=LOOK_UP):
    """The copy to read `email` through: the freshest LIVE one, or None."""
    return next((c for c in copies(email, paths, held) if c["live"]), None)


def _ago(ms):
    return providers._ago(time.time() - ms / 1000) if ms else None


def no_live_copy(email, paths=(), held=LOOK_UP):
    """Why no copy of `email` can be read, one clause per copy, naming no cure
    helm does not own: Orca refreshes its copy when it next measures the
    account, and helm never refreshes Orca's chain."""
    parts = []
    for c in copies(email, paths, held):
        who = ("Orca's copy" if c["source"] == ORCA
               else "~/.claude" if c["source"] == DEFAULT
               else "helm home %s" % os.path.basename(c["path"]))
        if not c["has_token"]:
            parts.append("%s holds no access token" % who)
        elif not c["expires_at"]:
            parts.append("%s holds a token with no expiry" % who)
        else:
            parts.append("%s expired %s ago" % (who, _ago(c["expires_at"])))
    if not parts:
        return "no copy of %s is on this machine" % email
    return "no copy of %s is live (%s)" % (email, "; ".join(parts))


def _window(pr, key, label, kind, now):
    w = pr.get(key)
    if not isinstance(w, dict):
        return None
    reset_ms = w.get("resetsAt")
    reset = reset_ms / 1000 if isinstance(reset_ms, (int, float)) \
        and not isinstance(reset_ms, bool) else None
    pct = w.get("usedPercent")
    # A WINDOW THAT HAS ENDED SINCE THE READING is not measured any more: the
    # percent was about a window that no longer exists.
    if reset is not None and reset <= now:
        pct = None
    return providers.NativeQuotaProvider._gauge(label, kind, pct, reset)


def _reading(pr, now):
    """One Orca ProviderRateLimits -> {gauges, at, status, error}."""
    gauges = [g for g in (
        _window(pr, "session", "5h", "session", now),
        _window(pr, "weekly", "7d", "period", now)) if g]
    fable = pr.get("fableWeekly")
    if isinstance(fable, dict) and (fable.get("usedPercent") or 0) > 0:
        g = _window(pr, "fableWeekly", "7d-fable", "period", now)
        if g:
            gauges.append(g)
    at = pr.get("updatedAt")
    return {"gauges": gauges,
            "at": at / 1000 if isinstance(at, (int, float))
            and not isinstance(at, bool) else None,
            "status": pr.get("status"), "error": pr.get("error")}


def _provenance_id(pr):
    """The managed account id Orca says a reading was taken with, or None
    ("managed:<id>" or "managed:<id>:wsl:<distro>"; "system" is no account)."""
    meta = pr.get("usageMetadata") if isinstance(pr, dict) else None
    prov = (meta or {}).get("authProvenance") if isinstance(meta, dict) else None
    if not isinstance(prov, str) or not prov.startswith("managed:"):
        return None
    return prov[len("managed:"):].split(":", 1)[0] or None


def orca_readings(adapter=None, now=None):
    """({email_casefold: reading}, why) — Orca's own last measurement of each
    Claude account it manages, from accounts.list WITHOUT refreshUsage: Orca
    answers from memory, calls no provider and refreshes no token. A reading is
    bound to its account by the id Orca records it under (an inactive
    account's accountId; the active reading's authProvenance), never by which
    account happens to be selected now. ({}, why) when Orca cannot answer."""
    from .harness import OrcaAdapter
    now = time.time() if now is None else now
    res, err = (adapter or OrcaAdapter()).rpc(
        "accounts.list", {"refreshUsage": False}, timeout=SNAPSHOT_TIMEOUT_S)
    if err:
        return {}, err
    claude = (res or {}).get("claude") if isinstance(res, dict) else None
    emails = {a["id"]: a["email"] for a in (claude or {}).get("accounts") or ()
              if isinstance(a, dict) and isinstance(a.get("id"), str)
              and isinstance(a.get("email"), str) and a["email"]}
    rl = (res or {}).get("rateLimits") or {}
    out = {}
    pairs = []
    active = rl.get("claude")
    if isinstance(active, dict):
        pairs.append((_provenance_id(active), active))
    for ia in rl.get("inactiveClaudeAccounts") or ():
        if isinstance(ia, dict) and isinstance(ia.get("rateLimits"), dict):
            got = _provenance_id(ia["rateLimits"])
            if got and got != ia.get("accountId"):
                continue          # a reading filed under another account's id
            pairs.append((ia.get("accountId"), ia["rateLimits"]))
    for aid, pr in pairs:
        email = emails.get(aid)
        if not email:
            continue
        reading = _reading(pr, now)
        if reading["at"] is None or now - reading["at"] > READING_MAX_AGE_S:
            continue
        key = email.casefold()
        if key not in out or reading["at"] > out[key]["at"]:
            out[key] = reading
    return out, None
