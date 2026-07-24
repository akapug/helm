"""helm.cred identity cluster: content-derived account reads (see __init__)."""
import os

from .. import cred as _cred
from .. import homes
from ._common import (ACCOUNT_JSON, _CACHE, _email_or_none, _read_json,
                      _stat_key, _str_or_none)


def _read_account(real):
    """The fail-closed read: every failure names its reason and claims NOTHING."""
    out = {"path": real, "email": None, "uuid": None, "org": None,
           "ok": False, "error": None}
    src = os.path.join(real, ACCOUNT_JSON)
    if not os.path.exists(src):
        out["error"] = "no %s" % ACCOUNT_JSON
        return out
    doc = _read_json(src)
    if not isinstance(doc, dict):
        out["error"] = "%s unreadable or not an object" % ACCOUNT_JSON
        return out
    oa = doc.get("oauthAccount")
    if not isinstance(oa, dict):
        out["error"] = "no oauthAccount block (never logged in here?)"
        return out
    email = _email_or_none(oa.get("emailAddress"))
    if not email:
        out["error"] = "oauthAccount carries no valid emailAddress"
        return out
    out.update(email=email, ok=True, uuid=_str_or_none(oa.get("accountUuid")),
               org=_str_or_none(oa.get("organizationName")))
    return out


def account_of(config_dir):
    """THE identity function: which account a config dir actually holds, read
    from its CONTENT. -> {path, email, uuid, org, ok, error}. Cached by the
    .claude.json stat key, so every caller can ask freely."""
    real = os.path.realpath(os.path.expanduser(config_dir or ""))
    src = os.path.join(real, ACCOUNT_JSON)
    key = _stat_key(src)
    hit = _CACHE.get(real)
    if hit is not None and hit[0] == key:
        return dict(hit[1])
    for _ in range(2):
        before = _stat_key(src)
        res = _cred._read_account(real)
        after = _stat_key(src)
        if before == after:
            _CACHE[real] = (after, res)
            return dict(res)
    res = {"path": real, "email": None, "uuid": None, "org": None,
           "ok": False, "error": "%s changed during identity read" % ACCOUNT_JSON}
    _CACHE[real] = (_stat_key(src), res)
    return dict(res)


def oauth_block(config_dir):
    """The raw oauthAccount dict (identity metadata only — the tokens live in
    .credentials.json). {} when unreadable."""
    doc = _read_json(os.path.join(os.path.realpath(os.path.expanduser(config_dir)),
                                  ACCOUNT_JSON))
    oa = doc.get("oauthAccount") if isinstance(doc, dict) else None
    return oa if isinstance(oa, dict) else {}


def verdict_for(path, default=False):
    """(verdict, account) for one config dir — AGREE | DRIFT | UNKNOWN | N/A.
    N/A = the provider default home or any dir outside the claude homes root:
    the canonical-name rule (dir name == folded account email) does not apply
    there, so there is nothing to agree or disagree with."""
    real = os.path.realpath(os.path.expanduser(path))
    acct = account_of(real)
    if not acct["ok"]:
        return "UNKNOWN", acct
    named = (not default
             and os.path.dirname(real) == os.path.realpath(homes.ROOTS["claude"]))
    if not named:
        return "N/A", acct
    return ("AGREE" if homes.canonical_name(acct["email"]) == os.path.basename(real)
            else "DRIFT"), acct
