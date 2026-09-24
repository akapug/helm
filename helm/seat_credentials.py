"""Credential translation and expiry inspection for :mod:`helm.seat`."""
import glob
import json
import os
import time
from . import pk

# ---------------------------------------------------------------------------
# cred translation (the eval's ~20-line recipe, read-only on the source)
# ---------------------------------------------------------------------------

def translate_codex_auth(src_path):
    """codex CLI auth.json -> CLIProxyAPI codex auth record. READ-ONLY on the
    source. Returns (record, filename, err): email from the id_token JWT claim,
    plan from its https://api.openai.com/auth claim, expired from the
    access_token exp claim (RFC3339). account_id falls back to the JWT
    chatgpt_account_id claim (id_token, then access_token) when tokens.account_id
    is absent — a shape the codex CLI has emitted; the pooled record must carry
    the account_id whenever identity knows it (dedup + linkage key off it).

    THE RETURNED FILENAME IS A SUGGESTION, NEVER AN IDENTITY. It is
    `codex-<email>-<plan>.json`, which is injective over (email, plan) and NOT
    over account ids: one owner can hold two codex accounts reachable at one
    address on one plan, and both name this one file. An absent email or plan
    collapses to the literal word `unknown`, so two unrelated half-read
    credentials both name `codex-unknown-unknown.json`. A caller that WRITES
    the pool must therefore resolve its destination by `account_id` and refuse
    a name another account already holds — `helm.codexhomes._follow_pool_name`
    carries that rule and `cred_follow` enforces it (COLLISION / INCOMPLETE);
    `helm codex pool` sidesteps the whole question by naming the file after
    the codexhome instead."""
    try:
        with pk.open_regular(src_path) as f:
            a = json.load(f)
    except (OSError, ValueError) as exc:
        return None, None, "unreadable auth.json %s (%s)" % (src_path, exc)
    t = a.get("tokens") or {}
    idc = _jwt_claims(t.get("id_token"))
    acc = _jwt_claims(t.get("access_token"))
    exp = acc.get("exp")
    if not isinstance(exp, (int, float)):
        return None, None, "no exp claim in access_token (%s)" % src_path
    email = idc.get("email") or "unknown"
    plan = (idc.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type") or "unknown"
    rec = {
        "id_token": t.get("id_token"),
        "access_token": t.get("access_token"),
        "refresh_token": t.get("refresh_token"),
        "account_id": (t.get("account_id")
                       or (idc.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
                       or (acc.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")),
        "last_refresh": a.get("last_refresh"),
        "email": email,
        "type": "codex",
        "expired": _rfc3339(exp),
    }
    return rec, "codex-%s-%s.json" % (email, plan), None


def _cred_exp(src_path):
    """The access_token exp epoch of a codex cred file, None when unparseable.

    TWO SHAPES, both real and both live on this host — reading only the first
    made every POOLED cred look unparseable, and an unparseable exp folds into
    "expired" at every call site:

      codex-home auth.json   {"tokens": {"access_token": "<jwt>"}, ...}
      proxy pool file        {"access_token": "<jwt>", "expired": ..., ...}

    Measured 2026-07-29: both pool files returned None here while carrying JWTs
    valid to 2026-08-08 with disabled=false, which `helm seat status` reported
    correctly from its own path. One format's reader applied to the other
    yields None, and None reads as absence.
    """
    try:
        with pk.open_regular(src_path) as f:
            a = json.load(f)
    except (OSError, ValueError):
        return None
    exp, _src = _cred_expiry(a)
    return exp


def _expired_field_epoch(blob):
    """The `expired` RFC3339 stamp as an epoch, None when absent/unparseable.

    Parsed exactly the way the `--auth-from` adoption validator parses it (Z
    folded to +00:00, ValueError swallowed) — one stamp, one reading. That
    validator is the precedent this whole slice reuses: it is the only cred
    reader in the file that already understood a credential whose access_token
    is NOT a JWT."""
    raw = blob.get("expired") if isinstance(blob, dict) else None
    if not (isinstance(raw, str) and raw.strip()):
        return None
    import datetime
    try:
        return datetime.datetime.fromisoformat(
            raw.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cred_remaining(seconds):
    """Time left, in a unit that does not COLLAPSE the interesting case.

    The old renderer did `(exp - now) // 3600` and printed "%dh left", so
    EVERY remainder under an hour read `0h left`. Measured 2026-08-03 at
    22:25Z: gemini showed "valid until 22:33:41Z (0h left)" — eight minutes,
    on a token that had been refreshing hourly for fourteen hours. A reader
    who acts on that is responding to an integer-truncation, and I nearly
    announced a seat about to die mid-lane on the strength of it.

    Under an hour reports MINUTES; under a minute says so rather than
    rounding to 0m, because "0m" is the same collapse one unit down."""
    seconds = int(seconds)
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def _cred_rolling(blob):
    """A suffix saying the expiry ROLLS, when the record itself proves it can.

    THE LINE COULD NOT TELL A REFRESHING TOKEN FROM A DYING ONE, and rendered
    both identically. That is the same string for "expiring, and the proxy
    will silently mint another in seconds" and "expiring, and the seat stops".
    An operator cannot act correctly on a surface that says one thing about
    two opposite states — and the EXPIRED branch below already names the
    refresh_token, so the code knew; it only mentioned it AFTER expiry, when
    the reassurance is worthless.

    TWO FACTS, BOTH READ FROM THE RECORD, NEITHER A PREDICTION: a short
    `expires_in` lifetime means the stamp is designed to roll, and a
    `refresh_token` means the proxy holds what it needs to roll it. Together
    they say a low remainder is NORMAL here. They do NOT say the next refresh
    will succeed — that is unknowable from a file, and the phrasing stays
    capability ("can refresh") rather than forecast ("will").

    NEVER TOUCHES A TOKEN VALUE: presence only, exactly like `_cred_expiry`.
    The refresh_token is tested for truthiness and never read, compared, or
    rendered."""
    if not isinstance(blob, dict):
        return ""
    tokens = blob.get("tokens") if isinstance(blob.get("tokens"), dict) else {}
    if not (blob.get("refresh_token") or tokens.get("refresh_token")):
        return ""
    life = blob.get("expires_in") or tokens.get("expires_in")
    if not isinstance(life, (int, float)) or life <= 0 or life > 86400:
        return " — ROLLING: a refresh_token is present, so the proxy can mint " \
               "another; a low remainder is not by itself a failure"
    return (" — ROLLING: a %s token with a refresh_token, so a low remainder "
            "is NORMAL and not a warning" % _cred_remaining(life))


def _cred_expiry(blob):
    """(epoch, authority) for a credential record — (None, None) when NEITHER
    authority can be read. `authority` is "jwt" or "expired-field", for a
    message that can name WHY it knows.

    TWO AUTHORITIES, because the fleet runs two credential shapes and only one
    of them carries a JWT:

      codex / grok      access_token IS a JWT -> the `exp` claim
      gemini/antigravity  access_token is an OPAQUE Google token -> no claims
                          at all; the record's own `expired` RFC3339 field is
                          the authority, and the proxy writes it

    Measured 2026-07-29 on the live host: `helm seat doctor` rendered
    `gemini <account> — cred unparseable` while the gemini proxy was
    UP on :8390 and its pool file carried disabled=false and
    expired=2026-07-29T20:13:46-07:00 — a perfectly readable stamp the probe
    never looked at. A JWT-only reader applied to a non-JWT credential returns
    no claims, and "no claims" was being rendered as a finding about the
    CREDENTIAL ("unparseable") rather than about the READER. That is the
    blindness-as-verdict class: the operator is told to go fix auth that works.

    NEVER TOUCHES A TOKEN VALUE: this reads an `exp` claim and a timestamp
    field. No token bytes are returned, logged, or compared."""
    if not isinstance(blob, dict):
        return None, None
    tok = (blob.get("tokens") or {}).get("access_token") or blob.get("access_token")
    exp = _jwt_claims(tok).get("exp")
    if isinstance(exp, (int, float)):
        return exp, "jwt"
    exp = _expired_field_epoch(blob)
    return (exp, "expired-field") if exp is not None else (None, None)


def cred_state(auth_dir):
    """Return (CRED_VALID|CRED_EXPIRED|CRED_ABSENT|CRED_UNKNOWN, detail, email)
    for a seat's pooled credential dir — the tri-state sibling of proxy_drift,
    carrying the same law: UNKNOWABLE IS NOT A FINDING.

    The three outcomes the caller must be able to tell apart:

      CRED_VALID    a readable cred with a future expiry (detail names it)
      CRED_ABSENT   the dir is READABLE and holds no cred file — a REAL negative
      CRED_EXPIRED  a readable cred provably past expiry, or proxy-disabled — a
                    REAL negative
      CRED_UNKNOWN  the dir or file could not be read, or parsed, or carries no
                    recognizable expiry authority. NOT a statement about the
                    credential. Never rendered as ABSENT.

    A POOL IS AN ANY-OF, NOT A FIRST-OF. The previous reader took creds[0] —
    alphabetically first — and reported the whole seat from it, so one stale
    sibling in a pool of live creds spoke for the seat. The proxy hot-reloads
    the whole dir and will use ANY live cred in it, so a pool with one valid
    member is VALID; only when nothing is valid does the worst real negative
    speak, and UNKNOWN outranks nothing (it is reported only when there is no
    real finding to report)."""
    try:
        names = sorted(f for f in os.listdir(auth_dir) if f.endswith(".json"))
    except OSError as exc:
        return (CRED_UNKNOWN,
                "cannot read the seat auth-dir %s (%s) — the proxy may hold a "
                "perfectly good cred in memory; fix the path/permissions or "
                "check `helm seat status`, do NOT re-mint or respawn"
                % (auth_dir, exc.__class__.__name__), None)
    if not names:
        return (CRED_ABSENT,
                "no cred file in %s — pool one: `helm codex pool <home>` (or "
                "`helm seat add <family> --auth-from <file>`)" % auth_dir, None)
    now = time.time()
    best_valid, expired, unknown = None, [], []
    for name in names:
        path = os.path.join(auth_dir, name)
        try:
            with pk.open_regular(path) as f:
                blob = json.load(f)
        except OSError as exc:
            unknown.append("%s unreadable (%s)" % (name, exc.__class__.__name__))
            continue
        except ValueError:
            unknown.append("%s does not parse as JSON" % name)
            continue
        if not isinstance(blob, dict):
            unknown.append("%s is JSON but not an object" % name)
            continue
        email = blob.get("email") or "?"
        if blob.get("disabled"):
            expired.append((None, email, "%s marked DISABLED by the proxy "
                                         "(it failed upstream)" % name))
            continue
        exp, authority = _cred_expiry(blob)
        if exp is None:
            unknown.append("%s carries no readable expiry (no JWT `exp` claim "
                           "and no `expired` field)" % name)
            continue
        if exp <= now:
            expired.append((exp, email, "EXPIRED %s" % _rfc3339(exp)))
        elif best_valid is None or exp > best_valid[0]:
            best_valid = (exp, email, authority, _cred_rolling(blob))
    extra = " (+%d more pooled)" % (len(names) - 1) if len(names) > 1 else ""
    if best_valid:
        exp, email, authority, rolling = best_valid
        return (CRED_VALID, "valid until %s (%s left, per %s)%s%s"
                % (_rfc3339(exp), _cred_remaining(exp - now), authority,
                   rolling, extra), email)
    if expired:
        _exp, email, why = expired[0]
        return (CRED_EXPIRED, "%s%s — the proxy refreshes from a live "
                              "refresh_token; if it cannot, re-login that home"
                % (why, extra), email)
    # Nothing valid, nothing provably dead: every cred in the pool was
    # unreadable in some way. That is a statement about this PROBE.
    return (CRED_UNKNOWN,
            "cred UNKNOWN — %s; the proxy may be authenticating fine from a "
            "cred this probe cannot read. Verify with `helm seat status` or the "
            "proxy log, do NOT re-mint the cred or respawn the proxy"
            % "; ".join(unknown[:3]), None)


def codex_cred_state():
    """(state, path, detail) — the codex source-cred question, tri-state.

    THE AUTHORITY; `newest_valid_codex_auth` below is its projection, the way
    vcs.is_ancestor projects vcs.ancestry: one question, one authority. The
    projection exists because `seat add codex` genuinely needs "give me an
    UN-POOLED home to translate, or tell me why not" — for THAT caller a fully
    pooled fleet really is nothing-to-do. `helm seat doctor` needs the opposite
    reading of the same fact, and a caller cannot recover a state from a
    formatted sentence.

    WHY THIS EXISTS (the exit-code half of the 2026-07-29 fix): the pooled
    "not applicable" case was given honest PROSE but still came back through
    the `reason` channel, and `_doctor` gated its exit status on
    `not err` — so a perfectly healthy fully-pooled fleet exited 1. Measured on
    the live host: `helm seat doctor` printed "not applicable: 2 codex cred(s)
    are POOLED ... which is the normal state" and returned 1. Any cron or
    wrapper gating on that status reads a fleet-wide credential FAILURE, which
    is the same false verdict the prose fix had just removed, surviving in the
    one channel machines actually read."""
    src, reason = _newest_unpooled_codex_auth()
    if src:
        return CRED_VALID, src, None
    from . import codexhomes
    pool_state, detail, _email = cred_state(codexhomes.pool_dir())
    if pool_state == CRED_VALID:
        # Not a finding at all: the creds are where the proxy reads them.
        return CRED_VALID, None, reason
    if pool_state == CRED_UNKNOWN:
        return CRED_UNKNOWN, None, detail
    return pool_state, None, reason


def newest_valid_codex_auth():
    """Newest-mtime non-expired ~/.codex-homes/*/auth.json (realpath-deduped,
    symlink alias homes collapse). (path, None) or (None, reason).

    The PROJECTION of `codex_cred_state` for callers that want a translatable
    source cred or an explanation — `seat add codex`'s exact contract, kept
    byte-identical on purpose. A caller whose negative branch is load-bearing
    (an exit code, an escalation) must switch on `codex_cred_state` instead:
    this signature cannot tell "nothing to pool because everything is already
    pooled" from "no working credential anywhere"."""
    return _newest_unpooled_codex_auth()


def _newest_unpooled_codex_auth():
    seen, cands = set(), []
    for p in sorted(glob.glob(os.path.join(CODEX_HOMES, "*", "auth.json"))):
        real = os.path.realpath(p)
        if real in seen:
            continue
        seen.add(real)
        exp = _cred_exp(p)
        if exp and exp > time.time():
            cands.append((os.path.getmtime(p), p))
    if not cands:
        # CODEX_HOMES IS THE POOLING SOURCE, NOT WHERE A POOLED CRED LIVES.
        # `helm codex pool <name>` translates a codex-home auth.json into the
        # seat proxy's hot-reload auth-dir; a fleet that has pooled everything
        # keeps NOTHING here, so this glob is empty BY ARCHITECTURE and its
        # emptiness is not evidence of anything. Measured 2026-07-29: this line
        # printed "no valid codex cred" while the pool held two creds with 233h
        # left and all three codex seats were building — and it escalated a
        # nonexistent credential problem to the owner. A check that cannot see
        # the case must say so, never return the verdict.
        from . import codexhomes
        pool = sorted(glob.glob(os.path.join(codexhomes.pool_dir(), "*.json")))
        live = [p for p in pool
                if (_cred_exp(p) or 0) > time.time()]
        if live:
            return None, ("not applicable: %d codex cred(s) are POOLED in %s "
                          "(the proxy hot-reloads that dir); %s holds no "
                          "un-pooled home, which is the normal state"
                          % (len(live), codexhomes.pool_dir(), CODEX_HOMES))
        if pool:
            return None, ("no valid codex cred: %d pooled file(s) in %s but "
                          "every access token is expired, and %s is empty"
                          % (len(pool), codexhomes.pool_dir(), CODEX_HOMES))
        return None, ("no valid codex cred in %s or the pool %s (absent or "
                      "every access token expired)"
                      % (CODEX_HOMES, codexhomes.pool_dir()))
    return max(cands)[1], None


_UNBLOCK = """helm seat: %s
helm seat will never open a browser login itself. Unblock (human, one-time):
  CODEX_HOME=~/.codex-homes/<home> codex login --device-auth
then re-run `helm seat add codex`."""
