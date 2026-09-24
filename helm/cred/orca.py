"""helm.cred orca cluster: a credhome measured against Orca's managed copy of
the same account, and the ONE sync every claude launch onto a credhome runs
before exec (see __init__ for the laws this cluster inherits).

WHY IT EXISTS (task/2505, a rehomed project seat): Orca
keeps each account's LIVE token at
<orca user data>/claude-accounts/<id>/auth/.credentials.json and refreshes it
while its panes use the account. A helm credhome that was copied from that
account weeks earlier still reads AGREE — the identity is right — but its
refresh token had been rotated away under Orca, so the pane launched on it read
"Not logged in". Orca's store is fleet-wide (it rewrites ~/.claude on an
account switch); a credhome pins one seat to one account, and only works when
it carries Orca's live copy.

THE DIRECTION RULE, resolved against the refresh-rotation law keepalive
documents (the token endpoint rotates the refresh token on every grant, and a
rotated-away token presented again trips reuse detection):
  * Orca -> home ONLY. Orca's files are opened read-only and never written.
  * Only when Orca's copy is STRICTLY fresher (access-token expiresAt) AND the
    home's own chain is provably spent: the home has no token or no refresh
    token, carries Orca's family, or its refresh lifetime has passed. Nothing
    local proves a REFRESH: lifetimes within the jitter are one grant Orca
    rotated away OR a login minted minutes apart, and a record that Orca once
    held the home's token fits an independent re-login of Orca's dir as well
    as a refresh. A home on an INDEPENDENT live login chain (lifetimes further
    apart than the jitter) reads OWN-CHAIN and is never written — access
    expiries across two chains say nothing about which works.
  * A home neither provably spent nor provably independent reads
    CHAIN-UNPROVEN, and nothing is automatic either way: a copy would
    overwrite what may be a live login, and a launch would present what may be
    a rotated-away token, the reuse that revokes Orca's live family. The
    launch refuses and names both cures (a fresh login in the home, or
    `sync-orca --replace-own-chain`, the operator's word that the home's chain
    is Orca's to replace); keepalive skips it for the same reason.
  * Never while Orca's family is live in ANY other home, ~/.claude included.
    Orca's copy of the account it has switched ~/.claude to IS that home's
    chain; copying it would put one refresh token in two homes with two live
    refreshers, the state heal refuses and doctor reports as FAIL.
  * After a sync the seat on the home IS a refresher of that family, beside
    Orca. Whichever refreshes first rotates the token away from the other,
    and the other presenting it trips reuse detection, which revokes the whole
    family, both copies. So while the seat runs, Orca must not refresh that
    account: switching Orca to it copies this same family into ~/.claude, and
    the first refresh on either side kills both. The launch line says so.
    keepalive never adds a third refresher: it skips a home that shares
    Orca's family or is STALE against it (see keepalive.refresh_home), and it
    takes the same writer lock this sync takes.

IDENTITY is matched by account EMAIL read from content — the home's
.claude.json oauthAccount against the Orca dir's oauth-account.json, which is
authoritative — never by a directory name. When both sides carry
organizationUuid / accountUuid they must match too (one email can sit in
several organizations). The dir's own .claude.json, when present, only ever
narrows: naming another account beside an agreeing oauth-account.json is a
torn dir (DISAGREE, exec refused); naming this account while oauth-account.json
names another is UNKNOWN (that dir would never be written here). The Orca dir
must also carry Orca's `.orca-managed-claude-auth` ownership marker naming that
dir, the same proof codexhomes demands of `.orca-managed-home`.

SECRETS NEVER SURFACE: token bytes are read into memory to copy and to hash;
only expiries, a family digest prefix and emails leave this module.
"""
import fcntl
import hashlib
import os
import sys
import time

from .. import cred as _cred
from .. import homes
from ._common import (ACCOUNT_JSON, AUTH_JSON, _email_or_none, _json_bytes,
                      _read_json, _read_regular, _stat_key, cache_clear)
from .account import account_of, oauth_block, verdict_for

ORCA_ACCOUNTS_DIR = "claude-accounts"
ORCA_AUTH_DIR = "auth"
ORCA_MARKER = ".orca-managed-claude-auth"
ORCA_ACCOUNT_JSON = "oauth-account.json"
SYNC_ENV = "HELM_CREDHOME_ORCA_SYNC"

FRESH = "FRESH"
STALE = "STALE-vs-ORCA"
NO_COPY = "NO-ORCA-COPY"
UNKNOWN = "UNKNOWN"
DISAGREE = "DISAGREE"
OWN_CHAIN = "OWN-CHAIN"
UNPROVEN = "CHAIN-UNPROVEN"
# The identity keys that must agree when both sides carry them.
ID_KEYS = ("organizationUuid", "accountUuid")
# How far apart two copies' refreshTokenExpiresAt may be and still be one
# grant's lifetime (see _measure).
LIFETIME_JITTER_MS = 5 * 60 * 1000


def orca_accounts_root():
    """Orca's managed claude account store, through the one Orca user-data
    resolver (ORCA_USER_DATA_PATH, XDG_CONFIG_HOME, platform default)."""
    from ..harness import OrcaAdapter
    return os.path.join(OrcaAdapter._user_data_path(), ORCA_ACCOUNTS_DIR)


def _utc(ms):
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return "unknown"
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ms / 1000))


def _token_facts(blob):
    """Secret-free facts about credential bytes: access expiry, refresh-token
    expiry and the refresh family digest prefix (homes._token_family's law —
    hash in memory, only the prefix leaves)."""
    doc = _json_bytes(blob)
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):
        return None

    def ms(key):
        v = oauth.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    tok = oauth.get("refreshToken")
    return {"expires_at": ms("expiresAt"),
            "refresh_expires_at": ms("refreshTokenExpiresAt"),
            "family": (hashlib.sha256(tok.encode()).hexdigest()[:10]
                       if isinstance(tok, str) and tok else None)}


def _read_creds(path):
    """(blob, facts, key, error). ENOENT is (None, None, None, None): absence
    is a state, not an error. The blob never leaves this cluster."""
    key = _stat_key(path)
    try:
        blob, _ = _read_regular(path)
    except FileNotFoundError:
        return None, None, None, None
    except OSError as e:
        return None, None, None, "%s unreadable (%s)" % (AUTH_JSON, e.__class__.__name__)
    if key != _stat_key(path):
        return None, None, None, "%s changed during read" % AUTH_JSON
    facts = _token_facts(blob)
    if facts is None:
        return None, None, None, "%s carries no claudeAiOauth block" % AUTH_JSON
    return blob, facts, key, None


def orca_copy(email, ids=None):
    """Orca's managed dir for this account -> {state, path, reason, id_keys}.
    state: found | none | unknown | disagree. `ids` carries the home's
    organizationUuid/accountUuid; a dir for the same email under another one is
    not this home's copy. A dir whose identity could not be read counts against
    both `none` and `found`: an unread dir may be (another) one that holds it.
    id_keys are the stat keys of the dir's identity files, taken BEFORE they
    were read, so a writer can prove they did not move under it."""
    ids = ids or {}
    root = orca_accounts_root()
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return {"state": "unknown", "path": None,
                "reason": "no Orca claude account store at %s" % _cred._display_path(root)}
    except OSError as e:
        return {"state": "unknown", "path": None,
                "reason": "Orca claude account store unreadable (%s)"
                          % e.__class__.__name__}
    hits, unread = [], 0
    for name in names:
        auth = os.path.join(root, name, ORCA_AUTH_DIR)
        acct_path = os.path.join(auth, ORCA_ACCOUNT_JSON)
        cfg_path = os.path.join(auth, ACCOUNT_JSON)
        id_keys = {acct_path: _stat_key(acct_path), cfg_path: _stat_key(cfg_path)}
        doc = _read_json(acct_path)
        if doc is None and os.path.lexists(acct_path):
            unread += 1
            continue
        named = _email_or_none(doc.get("emailAddress")) if isinstance(doc, dict) else None
        cfg = _read_json(cfg_path)
        # AN EXISTING SECOND IDENTITY THAT CANNOT BE READ IS NOT AN ABSENT ONE.
        # _read_json answers None both for no file and for a file it could not
        # read or parse, and only the first is "primary-only". An unreadable or
        # malformed one may name another account, so it never supports a copy.
        cfg_unread = ((cfg is None and os.path.lexists(cfg_path))
                      or (cfg is not None and not isinstance(cfg, dict)))
        oa = cfg.get("oauthAccount") if isinstance(cfg, dict) else None
        cfg_email = _email_or_none(oa.get("emailAddress")) if isinstance(oa, dict) else None
        if email not in (named, cfg_email):
            if cfg_unread:
                unread += 1
            continue
        shown = _cred._display_path(auth)
        hit = {"path": auth, "id_keys": id_keys, "other_org": False}
        try:
            marker, _ = _read_regular(os.path.join(auth, ORCA_MARKER))
            owner = marker.decode("utf-8", "replace").strip()
        except OSError:
            owner = None
        clash = [(k, ids[k], doc.get(k)) for k in ID_KEYS
                 if isinstance(doc, dict) and ids.get(k) and isinstance(doc.get(k), str)
                 and doc[k] and doc[k] != ids[k]]
        if owner != name:
            hit.update(state="unknown", reason="%s lacks Orca's ownership marker "
                       "naming it (%s)" % (shown, ORCA_MARKER))
        elif cfg_unread:
            hit.update(state="unknown", reason="Orca dir %s has a %s that could not "
                       "be read as an identity — it may name another account, so "
                       "this copy is not proven to be %s's"
                       % (shown, ACCOUNT_JSON, email))
        elif named == email and cfg_email and cfg_email != email:
            hit.update(state="disagree", reason="Orca dir %s names two accounts: "
                       "%s says %s, its %s says %s"
                       % (shown, ORCA_ACCOUNT_JSON, named, ACCOUNT_JSON, cfg_email))
        elif named != email:
            hit.update(state="unknown", reason="Orca dir %s names %s only in its %s; "
                       "its authoritative %s says %s"
                       % (shown, email, ACCOUNT_JSON, ORCA_ACCOUNT_JSON, named or "nothing readable"))
        elif clash:
            hit.update(state="unknown", other_org=True, reason="Orca's copy of %s at %s "
                       "is another identity: %s"
                       % (email, shown, ", ".join("home %s %s, Orca's %s" % (k, h, o)
                                                  for k, h, o in clash)))
        else:
            hit.update(state="found", reason=None)
        hits.append(hit)
    mine = [h for h in hits if not h["other_org"]]
    if len(mine) > 1:
        return {"state": "unknown", "path": None,
                "reason": "%d Orca account dirs claim %s — never guessed"
                          % (len(mine), email)}
    if mine and unread:
        return {"state": "unknown", "path": None,
                "reason": "Orca dir %s claims %s, but %d other account dir%s "
                          "unreadable and may claim it too — never guessed"
                          % (_cred._display_path(mine[0]["path"]), email, unread,
                             " is" if unread == 1 else "s are")}
    if mine:
        return mine[0]
    if hits:
        return hits[0]
    if unread:
        return {"state": "unknown", "path": None,
                "reason": "%d Orca account dir%s unreadable — cannot prove Orca "
                          "holds no copy of %s" % (unread, "s"[:unread != 1], email)}
    return {"state": "none", "path": None,
            "reason": "Orca holds no copy of %s" % email}


def is_credhome(path):
    """A named credhome: a dir directly under the claude homes root. The
    default ~/.claude is Orca's own to rewrite, and proxy seat dirs hold no
    Orca account, so neither is ever synced."""
    real = os.path.realpath(os.path.expanduser(path or ""))
    return os.path.dirname(real) == os.path.realpath(homes.ROOTS["claude"])


def _measure(real):
    """freshness() plus the in-memory blobs sync() needs (never returned by a
    public function)."""
    out = {"home": real, "account": None, "verdict": UNKNOWN, "reason": None,
           "orca_path": None, "home_expires_at": None, "orca_expires_at": None,
           "same_family": False, "chain": None}
    acct = account_of(real)
    if not acct["ok"]:
        out["reason"] = "home identity unreadable (%s)" % acct["error"]
        return out, None, None
    email = out["account"] = acct["email"]
    block = oauth_block(real)
    copy = orca_copy(email, {k: block.get(k) for k in ID_KEYS
                             if isinstance(block.get(k), str)})
    out["orca_path"] = copy["path"]
    if copy["state"] != "found":
        out["verdict"] = {"none": NO_COPY, "disagree": DISAGREE}.get(copy["state"], UNKNOWN)
        out["reason"] = copy["reason"]
        return out, None, None
    oblob, ofacts, okey, oerr = _read_creds(os.path.join(copy["path"], AUTH_JSON))
    if oerr or oblob is None:
        out["reason"] = "Orca's copy %s" % (oerr or "has no %s" % AUTH_JSON)
        return out, None, None
    if ofacts["expires_at"] is None or not ofacts["family"]:
        out["reason"] = "Orca's copy carries no expiresAt or refresh token"
        return out, None, None
    out["orca_expires_at"] = ofacts["expires_at"]
    hblob, hfacts, _hkey, herr = _read_creds(os.path.join(real, AUTH_JSON))
    if herr:
        out["reason"] = "home " + herr
        return out, None, None
    orca = {"blob": oblob, "facts": ofacts, "key": okey, "id_keys": copy["id_keys"],
            "path": os.path.join(copy["path"], AUTH_JSON)}
    if hblob is None:
        out.update(verdict=STALE, chain="no-home-token",
                   reason="home holds no %s; Orca's copy expires %s"
                   % (AUTH_JSON, _utc(ofacts["expires_at"])))
        return out, orca, None
    out["home_expires_at"] = hfacts["expires_at"]
    out["same_family"] = hfacts["family"] is not None and hfacts["family"] == ofacts["family"]
    expiries = "home token expires %s, Orca's copy %s" % (
        _utc(hfacts["expires_at"]), _utc(ofacts["expires_at"]))
    # One grant's lifetime, two copies: a refresh re-issues refreshTokenExpiresAt
    # with sub-second jitter (measured: 126 ms and 757 ms on two live pairs), so
    # "same" is a window. Lifetimes further apart are two grants; lifetimes
    # inside it are one grant OR two logins minted minutes apart, which is why
    # the window never names a chain.
    h_life, o_life = hfacts["refresh_expires_at"], ofacts["refresh_expires_at"]
    one_grant = (h_life is not None and o_life is not None
                 and abs(h_life - o_life) <= LIFETIME_JITTER_MS)
    two_grants = (h_life is not None and o_life is not None
                  and abs(h_life - o_life) > LIFETIME_JITTER_MS)
    if hblob == oblob:
        out.update(verdict=FRESH, reason="byte-identical to Orca's copy")
    elif hfacts["expires_at"] is None or ofacts["expires_at"] > hfacts["expires_at"]:
        # Orca is ahead. Only a home whose own chain is provably spent is
        # Orca's to replace; a home on an independent live login is not stale,
        # it is another chain.
        if out["same_family"]:
            chain = "same-family"
        elif hfacts["family"] is None:
            chain = "no-home-refresh-token"
        elif h_life is not None and h_life <= time.time() * 1000:
            chain = "home-refresh-expired"
        else:
            chain = None
        if chain:
            out.update(verdict=STALE, chain=chain, reason="%s (%s)" % (expiries, chain))
        elif two_grants:
            out.update(verdict=OWN_CHAIN, reason="%s, but nothing proves the home's own "
                       "login chain (refresh lifetime to %s, Orca's to %s) is spent — "
                       "not Orca's to replace, not synced; if this home reads Not "
                       "logged in, %s" % (expiries, _utc(h_life), _utc(o_life), _cure(real)))
        else:
            out.update(verdict=UNPROVEN, reason="%s, and nothing proves whose chain the "
                       "home's token is: %s, which fits both a token Orca rotated "
                       "away (a launch would present it, the reuse that revokes "
                       "Orca's live family) and a login of its own (a sync would "
                       "overwrite it), so %s; %s"
                       % (expiries, "its refresh lifetime (to %s) is within %d min of "
                          "Orca's (to %s)" % (_utc(h_life), LIFETIME_JITTER_MS // 60000,
                                              _utc(o_life))
                          if one_grant else "a refresh lifetime is missing (home to %s, "
                          "Orca's to %s)" % (_utc(h_life), _utc(o_life)),
                          "claude is not started on it" if is_credhome(real) else
                          "nothing grants on it (helm syncs and launch-checks only a "
                          "named credhome, and this is not one)", _cure(real)))
    else:
        out.update(verdict=FRESH, reason=expiries)
    return out, orca, hblob


def _cure(real):
    """The cure a chain verdict names for the home at `real`: a fresh login in
    it always, and the operator's replacement only for a named credhome, the
    one kind `sync-orca` writes and a launch syncs."""
    from .heal import _login_cmd
    cure = "log in fresh in it: %s" % _login_cmd(real)
    if is_credhome(real):
        cure += (", or, if its chain is Orca's to replace, `helm cred sync-orca "
                 "--home %s --apply --replace-own-chain`"
                 % _cred._display_path(os.path.basename(real)))
    return cure


def freshness(home):
    """The credhome's token freshness against Orca's copy of the SAME account.
    verdict: FRESH | STALE-vs-ORCA | OWN-CHAIN | CHAIN-UNPROVEN | NO-ORCA-COPY |
    UNKNOWN | DISAGREE; `chain` names why a STALE home's own chain is spent. Pure read;
    only expiries, emails, paths and booleans are returned."""
    out, _orca, _home = _measure(os.path.realpath(os.path.expanduser(home or "")))
    return out


def _claude_holders(real):
    """Live claude-family processes pinned to this home, or None when the
    probe cannot prove the home free (heal's holders_of law: uncertainty
    refuses)."""
    held = _cred.holders_of(real)
    if held is None:
        return None
    from .heal import _comm_claude_family
    return [(pid, comm) for pid, comm in held if _comm_claude_family(comm)]


def _keepalive_lock():
    """The machine-wide credential-writer lock keepalive's applied sweep
    holds, taken non-blocking: a sweep mid-grant means this pass does not
    write. -> open file (caller closes) or None."""
    from .. import keepalive
    path = keepalive._cache_path("keepalive.lock")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "w")
    except OSError:
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _family_live_elsewhere(family, real):
    """(home, unprovable): the claude home other than `real` whose
    .credentials.json carries this refresh family NOW — the default ~/.claude
    first, then every named home — and why that census could not be completed.
    homes' shared-family audit and heal's _family_elsewhere enforce the same
    law: one family = one home.

    AN UNREAD HOME IS NOT A HOME WITHOUT THE FAMILY. The audit this reuses
    folds an unreadable credentials file into "no token", which is right for a
    report and wrong for a writer: the unread home may be the one holding
    Orca's chain. A root that cannot be listed, or an existing credentials file
    that cannot be read, makes the census unprovable and the caller writes
    nothing. A credentials file with no claude block cannot hold a family."""
    if not family:
        return None, None
    root = homes.ROOTS["claude"]
    try:
        named = sorted(os.path.join(root, n) for n in os.listdir(root))
    except FileNotFoundError:
        named = []
    except OSError as e:
        return None, ("the claude homes root %s could not be listed (%s)"
                      % (_cred._display_path(root), e.__class__.__name__))
    seen = {real}
    for path in [homes.DEFAULTS["claude"]] + named:
        rp = os.path.realpath(path)
        if rp in seen or not os.path.isdir(rp):
            continue
        seen.add(rp)
        try:
            blob, _mode = _read_regular(os.path.join(rp, AUTH_JSON))
        except FileNotFoundError:
            continue
        except OSError as e:
            return None, ("%s in %s could not be read (%s)"
                          % (AUTH_JSON, _cred._display_path(path), e.__class__.__name__))
        facts = _token_facts(blob)
        if facts and facts["family"] == family:
            return path, None
    return None, None


def sync(home, apply=False, replace_own_chain=False):
    """Plan or perform the Orca -> credhome sync for one home. DRY-RUN by
    default. -> freshness() fields plus
      action: none | would-sync | synced | skip | refused
      blocked: why a STALE home is not (to be) written, when it is not
      exec_ok: False when the identity disagrees or the home's chain is
               CHAIN-UNPROVEN (a launch must not run)
      pre_image: the backup snapshot taken before the write, when one was.
    Only a STALE-vs-ORCA home is ever written — or, with replace_own_chain
    (the operator's word, never a launch's), an OWN-CHAIN or CHAIN-UNPROVEN
    one — and only when its identity is not DRIFTED, Orca's family is live in
    no other home, no claude process holds it, a pre-image was captured, and
    the keepalive writer lock is free."""
    real = os.path.realpath(os.path.expanduser(home or ""))
    res, orca, _hblob = _measure(real)
    res.update(action="none", exec_ok=True, pre_image=None, blocked=None)
    writable = (STALE, OWN_CHAIN, UNPROVEN) if replace_own_chain else (STALE,)
    refusing = (DISAGREE,) if replace_own_chain else (DISAGREE, UNPROVEN)
    if res["verdict"] in refusing:
        res.update(action="refused", exec_ok=False)
        return res
    if res["verdict"] not in writable:
        return res

    def skip(why):
        res.update(action="skip", blocked=why, reason="%s; %s" % (res["reason"], why))
        return res

    def shared(fam):
        from .heal import _login_cmd
        other, unprovable = _family_live_elsewhere(fam, real)
        if unprovable:
            return ("cannot prove Orca's refresh chain is live in no other home: %s "
                    "— nothing written" % unprovable)
        return other and ("Orca's copy is the refresh chain live in %s — copying it "
                          "here puts one refresh token in two homes, and reuse "
                          "detection revokes both; log in fresh in this home "
                          "instead: %s" % (_cred._display_path(other),
                                           _login_cmd(real)))

    if verdict_for(real)[0] == "DRIFT":
        return skip("the home is DRIFTED (its name promises another account) — "
                    "`helm cred heal` first")
    why = shared(orca["facts"]["family"])
    if why:
        return skip(why)
    if not apply:
        res.update(action="would-sync")
        return res
    held = _claude_holders(real)
    if held is None:
        return skip("the home could not be proven free of a live claude process")
    if held:
        return skip("live claude pid%s %s hold%s the home — one writer per home"
                    % ("s"[:len(held) != 1], ",".join(str(p) for p, _ in held),
                       "s"[:len(held) == 1]))
    lock = _keepalive_lock()
    if lock is None:
        return skip("the keepalive credential-writer lock is held")
    try:
        auth = os.path.join(real, AUTH_JSON)
        if os.path.lexists(auth):
            snap = _cred.backup(real, apply=True)
            if not snap["ok"]:
                return skip("pre-image capture failed (%s) — nothing written"
                            % snap["reason"])
            res["pre_image"] = snap.get("dest")
        held = _claude_holders(real)
        if held is None or held:
            return skip("a live claude process arrived after the pre-image")
        # Re-measure inside the lock: the decision binds the bytes written.
        again, orca, _hblob = _measure(real)
        if again["verdict"] not in writable or orca is None:
            refused = again["verdict"] in refusing
            res.update(again, action="refused" if refused else "none",
                       exec_ok=not refused, pre_image=res["pre_image"])
            return res
        why = shared(orca["facts"]["family"])
        if why:
            return skip(why)
        keys = dict(orca["id_keys"], **{orca["path"]: orca["key"]})
        if any(_stat_key(p) != k for p, k in keys.items()):
            return skip("Orca's copy or its identity files changed during the sync "
                        "— nothing written")
        _cred._atomic_private(auth, orca["blob"], 0o600)
        cache_clear()
        got, mode = _read_regular(auth)
        if got != orca["blob"] or mode != 0o600:
            res.update(action="refused", exec_ok=False,
                       reason="post-sync verification failed: the home's %s does not "
                              "read back as the bytes written with mode 0600" % AUTH_JSON)
            return res
        facts = _token_facts(got)
        res.update(action="synced", verdict=FRESH, home_expires_at=facts["expires_at"],
                   same_family=True, chain="same-family",
                   reason="synced %s from Orca's copy (expires %s)%s; Orca's files "
                          "untouched" % (AUTH_JSON, _utc(facts["expires_at"]),
                                         "" if again["verdict"] == STALE else
                                         ", replacing a %s home's chain on the "
                                         "operator's word" % again["verdict"]))
        return res
    except OSError as e:
        return skip("sync write failed (%s)" % e.__class__.__name__)
    finally:
        lock.close()


def sync_disabled():
    return (os.environ.get(SYNC_ENV) or "").strip().lower() in ("0", "off", "false", "no")


def launch_sync(home, label, prefix="[helm launch]", out=None):
    """THE ONE DOOR every claude launch onto a credhome passes before exec.
    Prints what it did (one line, stderr) and returns True when the launch may
    proceed. A DISAGREE identity and a CHAIN-UNPROVEN home return False. A
    non-credhome is not synced and says nothing; UNKNOWN launches the home
    as-is and says so."""
    out = out or sys.stderr
    if not home or not is_credhome(home):
        return True
    shown = _cred._display_path(label or home)
    if sync_disabled():
        print("%s home %s: Orca sync disabled (%s) — launching on the home's own "
              "token, freshness not checked" % (prefix, shown, SYNC_ENV), file=out)
        return True
    try:
        res = sync(home, apply=True)
    except Exception as e:          # never a traceback, never a token in text
        print("%s home %s: Orca freshness UNKNOWN (%s) — launching on the home "
              "as-is, NOT synced" % (prefix, shown, e.__class__.__name__), file=out)
        return True
    verdict, action, why = res["verdict"], res["action"], res["reason"]
    if action == "refused":
        print("%s REFUSED home %s (%s for %s): %s — no session started"
              % (prefix, shown, verdict, res["account"] or "-", why), file=out)
        return False
    if action == "synced":
        print("%s home %s was STALE-vs-ORCA for %s: %s (pre-image %s). This seat now "
              "shares Orca's refresh chain for %s: while it runs, do not switch Orca "
              "to that account — the first refresh on either side revokes both"
              % (prefix, shown, res["account"], why,
                 _cred._display_path(res["pre_image"]) if res["pre_image"] else
                 "none — the home held no credentials", res["account"]), file=out)
    elif action == "skip":
        print("%s home %s is STALE-vs-ORCA for %s and was NOT synced: %s — "
              "launching on the stale token"
              % (prefix, shown, res["account"], why), file=out)
    elif verdict == UNKNOWN:
        print("%s home %s: Orca freshness UNKNOWN (%s) — launching on the home "
              "as-is, NOT synced" % (prefix, shown, why), file=out)
    else:
        print("%s home %s: %s for %s (%s)"
              % (prefix, shown, verdict, res["account"], why), file=out)
    return True


def spawn_note(home):
    """The freshness line a `seat spawn` prints for the credhome its pane will
    launch on, or None for a home that is not a named credhome. A pure read: the
    sync itself runs inside the pane's `helm launch`, whose line only that pane
    shows — so the operator reading the spawn learns here whether it will sync,
    and that a home held by a live claude (the spawner's own, when it runs on the
    same home) launches unsynced."""
    if not home or not is_credhome(home):
        return None
    if sync_disabled():
        return "Orca sync disabled (%s) — the pane launches on the home's own token" % SYNC_ENV
    try:
        plan = sync(home)
    except Exception as e:          # never a traceback, never a token in text
        return "Orca freshness UNKNOWN (%s)" % e.__class__.__name__
    line = "Orca freshness %s for %s (%s)" % (plan["verdict"], plan["account"] or "-",
                                             plan["reason"])
    if plan["action"] == "refused":
        return line + " — the pane's launch will REFUSE to start claude"
    if plan["action"] == "skip":
        return line + " — the pane launches UNSYNCED on the stale token"
    if plan["action"] != "would-sync":
        return line
    held = _claude_holders(os.path.realpath(os.path.expanduser(home)))
    if held is None or held:
        return (line + " — a live claude process holds the home (%s), so the pane's "
                "launch will NOT sync it and runs on the stale token"
                % ("unprovable" if held is None else
                   "pid " + ",".join(str(p) for p, _ in held)))
    return line + " — the pane's `helm launch` syncs it from Orca before exec"
