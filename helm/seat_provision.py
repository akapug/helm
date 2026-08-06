"""Seat credential selection and provisioning for :mod:`helm.seat`."""
import glob
import json
import math
import os
import re
import shlex
import stat
import sys
import time

from . import seat_launch_assets as _assets_impl
from .seat_launch_assets import (
    proxy_debug,
    _config_yaml,
    _config_yaml_key,
    _key_base_url,
    _instance_dir,
    _SEAT_SURFACE_REFUSED,
    _seat_surface_error,
    _nested_surface_error,
    _dq_escape,
    launch_line,
    _seat_token,
    _link_skills,
    _ONBOARD_KEYS,
    _FEATURE_CACHE_KEYS,
    _FEATURE_CACHE_GATE,
    _FEATURE_CACHE_MIN_FEATURES,
    _FEATURE_CACHE_MAX_AGE_MS,
    _FEATURE_CACHE_FUTURE_SKEW_MS,
    _feature_cache_complete,
    _feature_cache_seed,
    _warn_feature_cache,
    _onboarded_refs,
    _git_toplevel,
    _seed_onboarding,
    _seed_seat_settings,
    _PROBE_AGENT_MD,
    probe_agents,
    _mint_probe_agents,
    _mint_instance_proxy,
    _seat_token_per,
    _write_launch_assets,
    _write_launch_sh,
    _env_file_value,
    _hermes_pool_key,
    _opencode_authstore_key,
    _resolve_homing,
)

_IMPL_MODULES = (_assets_impl,)

def _add_proxy_key(family, fam, args, room=None, room_source=None):
    """mode "proxy-key": an API-key provider behind the same local proxy via
    its openai-compatibility block. No OAuth, no auth-dir. Key source order:
    $<key_env>, then --key-from <.env-style file>, then — for POOL families
    (pool_providers set, e.g. ds4pro) — the PREFERRED opencode tool auth store
    (OPENCODE_AUTHSTORE) by the provider's `authstore` name, then the hermes
    CLI's credential_pool[<provider>] (HERMES_AUTH) as fallback. Both read-only.
    A pool family serves ONE selected provider per mint: `--provider <name>`
    else pool_default; the provider's base_url + upstream model id come from
    its pool table (the authstore carries no base_url, so the table's wins; on
    the hermes fallback the credential_pool entry's own base_url wins when
    present, so the seat rides exactly the endpoint the cred was minted for).
    The key is baked into the seat's 0600 config.yaml once, at add time — never
    printed, never logged. A family may name key_env_fallbacks — sibling env
    vars tried IN ORDER when <key_env> itself is unset (owner rule 2026-07-29:
    our own KIMI_API_KEY_PRIMARY before the loaned KIMI_API_KEY_EMBER). The
    chain is shared by the env-var and --key-from paths alike, and which var
    supplied the key is reported BY NAME (never the value) so a fallback mint
    is visible, not silent."""
    key_env = fam["key_env"]
    key_vars = (key_env,) + tuple(fam.get("key_env_fallbacks") or ())
    key_var = next((v for v in key_vars if os.environ.get(v)), None)
    api_key = os.environ.get(key_var) if key_var else None
    # provider selection + outbound routing. Non-pool families (kimi) carry the
    # provider/base_url/upstream on the family; pool families (ds4pro) resolve
    # them from the selected provider's table.
    provider = fam.get("provider")
    base_url = fam.get("base_url")
    upstream = fam.get("upstream_model")
    pool_provider = None
    if fam.get("pool_providers"):
        pool_provider = fam.get("pool_default")
        if "--provider" in args:
            try:
                pool_provider = args[args.index("--provider") + 1]
            except IndexError:
                print("helm seat: --provider wants a value", file=sys.stderr)
                return 2
        prov_cfg = fam["pool_providers"].get(pool_provider)
        if prov_cfg is None:
            print("helm seat: %s has no provider '%s' — choose one of: %s"
                  % (family, pool_provider,
                     ", ".join(sorted(fam["pool_providers"]))),
                  file=sys.stderr)
            return 2
        provider = pool_provider
        base_url = prov_cfg["base_url"]
        upstream = prov_cfg["upstream_model"]
    if not api_key and "--key-from" in args:
        path = os.path.expanduser(args[args.index("--key-from") + 1])
        for v in key_vars:
            api_key = _env_file_value(path, v)
            if api_key:
                key_var = v
                break
        if not api_key:
            print("helm seat: no %s line found in %s"
                  % (" or ".join(v + "=" for v in key_vars), path),
                  file=sys.stderr)
            return 1
    pool_err = None
    as_err = None
    if not api_key and pool_provider:
        # PREFER the opencode auth store (fresh, owner-maintained); the
        # authstore carries no base_url so the pool-table base_url (set above)
        # stands. Fall back to the hermes credential_pool, whose entry base_url
        # wins when present.
        authstore_prov = prov_cfg.get("authstore")
        if authstore_prov:
            api_key, as_err = _opencode_authstore_key(authstore_prov)
            if not api_key:
                pool_err = as_err
        if not api_key:
            api_key, ent_base, hermes_err = _hermes_pool_key(pool_provider)
            if api_key and ent_base:
                base_url = ent_base   # the pool entry's own base_url wins
            elif not api_key:
                # both sources tried and failed: report BOTH reasons — the
                # authstore is the PREFERRED path, so masking its error behind
                # the hermes one hides the reason the operator most needs.
                pool_err = "; ".join(e for e in (as_err, hermes_err) if e)
    if not api_key:
        pool_hint = ""
        if pool_provider:
            pool_hint = (", or ensure %s or %s carries a live %s bearer (%s)"
                         % (OPENCODE_AUTHSTORE, HERMES_AUTH, pool_provider,
                            pool_err))
        print("helm seat: no outbound key — export %s or pass "
              "--key-from <env-file> carrying one as a <var>= line%s, then "
              "re-run `helm seat add %s`"
              % (" / ".join(v + "=<key>" for v in key_vars), pool_hint,
                 family),
              file=sys.stderr)
        return 1
    if pool_provider is None:
        # non-pool proxy-key (kimi): dispatch the outbound endpoint by key shape
        base_url = _key_base_url(fam, api_key)
    d = seat_dir(family)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    token = _seat_token(family, d)
    _write_private(os.path.join(d, "config.yaml"),
                   _config_yaml_key(fam["port"], token, provider,
                                    base_url, fam["model"], api_key, upstream))
    if _write_launch_assets(family, d, room, room_source=room_source) \
            is _SEAT_SURFACE_REFUSED:
        return 1
    print("helm seat: %s seat minted at %s" % (family, d))
    print("  outbound %s key baked into config.yaml (0600 — value never "
          "printed); provider %s -> %s" % (key_var or key_env, provider,
                                           base_url))
    print("  proxy port %d; next: `helm seat up %s`, then `helm seat launch %s`"
          % (fam["port"], family, family))
    return 0


def _adoptable_cred(src, glob_pat):
    """(path, None) for the newest file under `src` matching glob_pat, else
    (None, reason). `src` may be the credential itself or a directory holding
    it — the owner should not have to know which shape the proxy chose."""
    src = os.path.expanduser(src)
    if os.path.isfile(src):
        return src, None
    if not os.path.isdir(src):
        return None, "no such file or directory: %s" % src
    import glob as _glob
    hits = _glob.glob(os.path.join(src, glob_pat))
    hits += _glob.glob(os.path.join(src, "auth", glob_pat))
    if not hits:
        return None, "no %s under %s (or its auth/ subdir)" % (glob_pat, src)
    return max(hits, key=lambda p: os.path.getmtime(p)), None


def _oauth_cred_reason(path):
    """None when the blob is a plausible, usable OAuth credential; else the
    reason it is not.

    ADDED FOR kimi's r1 FIX, and the asymmetry they named is the whole argument:
    mode "proxy" validates hard — _cred_exp refuses an expired token and
    translate_codex_auth parses the structure — while proxy-oauth checked only
    that a FILENAME matched a glob and then copied the bytes. They reproduced it:
    a file named `antigravity-garbage.json` containing "this is not json and not
    a credential" was adopted at 0600 and the command printed "gemini seat
    minted" and "next: helm seat launch gemini", rc 0, for a seat that cannot
    authenticate. So the NO-credential path correctly returned 1 while the
    BAD-credential path returned 0 — the green line over an absent capability
    that the r1 commit message itself said it was avoiding.

    WHAT IS AND IS NOT CHECKED. An expired access_token is NOT fatal on its own:
    these proxies refresh, so a stale access token with a live refresh_token is
    an ordinary, adoptable credential, and refusing it would reject the common
    case. What must be refused is a blob with NO path to authenticate at all —
    unparseable, disabled/quarantined, or expired with nothing to refresh from.
    The check answers "can this possibly authenticate", not "is this fresh",
    because the second question is the proxy's to answer at call time and ours
    would go stale the moment we finished asking it.
    """
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except OSError as exc:
        return "unreadable: %s" % exc
    except ValueError:
        return "not JSON — a credential file that does not parse cannot auth"
    if not isinstance(blob, dict):
        return "JSON but not an object (got %s)" % type(blob).__name__
    if blob.get("disabled"):
        return ("marked disabled — the proxy quarantines a credential that "
                "failed upstream; adopting it re-imports a known-dead auth")
    access = blob.get("access_token")
    refresh = blob.get("refresh_token")
    if not (isinstance(access, str) and access.strip()) and \
       not (isinstance(refresh, str) and refresh.strip()):
        return ("carries neither access_token nor refresh_token — keys present: "
                "%s" % ", ".join(sorted(blob)[:8]) or "none")
    if not (isinstance(refresh, str) and refresh.strip()):
        exp = blob.get("expired")
        if isinstance(exp, str) and exp.strip():
            try:
                import datetime
                when = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if when.timestamp() <= time.time():
                    return ("access_token expired at %s and there is no "
                            "refresh_token to renew it" % exp)
            except ValueError:
                pass          # unparseable stamp is not evidence of expiry
    return None


def _add_proxy_oauth(family, fam, args, room=None, room_source=None):
    """mode "proxy-oauth": the proxy authenticates ITSELF via its own login
    flag, so there is no sibling cred store to translate and no bearer to bake.

    Two ways in, and the difference matters to the owner:

    ADOPT — `--auth-from <file-or-dir>` takes a credential the owner has ALREADY
    minted (e.g. a login run against a scratch config while the family was being
    proven out) and copies it into the seat's own auth-dir at 0600. Making them
    repeat an interactive browser login for a credential that already exists on
    the same disk is a CLI-gate on a GUI-first owner, and the whole point of a
    seat home is that it is the ONE place the fleet looks. The copy is
    re-permissioned on the way in: the live grok credential was found at 0664,
    group- and world-readable, because the proxy wrote it under its own umask
    with nothing watching. Nothing was watching precisely because it lived
    outside the seat system.

    LOGIN — with no --auth-from we print the exact one-time command and STOP at
    rc 1. We do not mint a home and call it a seat: a seat that cannot
    authenticate is not a seat, and reporting success here would be the same
    laundering this codebase keeps finding — a green line over an absent
    capability. The home is still written, so the login has a config to target.
    """
    d = seat_dir(family)
    auth = os.path.join(d, "auth")
    os.makedirs(auth, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    os.chmod(auth, 0o700)
    token = _seat_token(family, d)
    cfg = os.path.join(d, "config.yaml")
    _write_private(cfg, _config_yaml(fam["port"], auth, token))

    adopted = None
    if "--auth-from" in args:
        try:
            src = args[args.index("--auth-from") + 1]
        except IndexError:
            print("helm seat: --auth-from wants a path", file=sys.stderr)
            return 2
        found, err = _adoptable_cred(src, fam["auth_glob"])
        if err:
            print("helm seat: nothing to adopt — %s" % err, file=sys.stderr)
            return 1
        # CONTENT, not just filename. The glob proves what a file is CALLED.
        bad = _oauth_cred_reason(found)
        if bad:
            print("helm seat: refusing to adopt %s — %s"
                  % (os.path.basename(found), bad), file=sys.stderr)
            print("  a credential that cannot authenticate is not a seat. Run "
                  "the login instead:", file=sys.stderr)
            print("    %s -config %s %s"
                  % (_proxy_bin() or "cli-proxy-api", cfg, fam["login_flag"]),
                  file=sys.stderr)
            return 1
        dst = os.path.join(auth, os.path.basename(found))
        with open(found, "rb") as f:
            blob = f.read()
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.chmod(dst, 0o600)
        adopted = (found, dst, os.stat(found).st_mode & 0o777)

    have = _glob_auth(auth, fam["auth_glob"])
    print("helm seat: %s seat minted at %s" % (family, d))
    if adopted:
        src_path, dst_path, src_mode = adopted
        print("  adopted credential %s -> %s (0600)"
              % (os.path.basename(src_path), os.path.relpath(dst_path, d)))
        if src_mode & 0o077:
            print("  NOTE: the source was mode %04o — group/world readable. The "
                  "copy is 0600; consider removing the original." % src_mode)
    if not have:
        print("  NO CREDENTIAL YET — this seat cannot authenticate. Run the "
              "one-time login, which writes into the seat's own auth-dir:",
              file=sys.stderr)
        print("    %s -config %s %s"
              % (_proxy_bin() or "cli-proxy-api", cfg, fam["login_flag"]),
              file=sys.stderr)
        print("  then re-run `helm seat add %s` to verify, or pass "
              "--auth-from <path> to adopt an existing one." % family,
              file=sys.stderr)
        return 1
    if _write_launch_assets(family, d, room, room_source=room_source) \
            is _SEAT_SURFACE_REFUSED:
        return 1
    print("  model %s (measured, not chosen by version — see FAMILIES), "
          "proxy port %d" % (fam["model"], fam["port"]))
    print("  next: `helm seat up %s`, then `helm seat launch %s`"
          % (family, family))
    return 0


def _glob_auth(auth_dir, pat):
    import glob as _glob
    return sorted(_glob.glob(os.path.join(auth_dir, pat)))


def _add(family, args, room=None, room_source=None):
    fam = FAMILIES.get(family)
    if fam is None:
        print("helm seat: family '%s' not yet wired (have: %s). First-party "
              "Anthropic-compatible families need only a FAMILIES entry with "
              "base_url + key_env — see helm/seat.py." % (family, ", ".join(sorted(FAMILIES))),
              file=sys.stderr)
        return 2
    ownership = _seat_surface_error(family, family)
    if ownership:
        print("helm seat: " + ownership, file=sys.stderr)
        return 1
    if fam["mode"] == "proxy-key":
        return _add_proxy_key(
            family, fam, args, room, room_source=room_source)
    if fam["mode"] == "proxy-oauth":
        return _add_proxy_oauth(
            family, fam, args, room, room_source=room_source)
    if fam["mode"] != "proxy":
        print("helm seat: family '%s' mode '%s' not yet wired — proxyless add "
              "not implemented" % (family, fam["mode"]), file=sys.stderr)
        return 2
    src = None
    if "--auth-from" in args:
        src = os.path.expanduser(args[args.index("--auth-from") + 1])
        if not os.path.exists(src):
            print(_UNBLOCK % ("--auth-from path does not exist: %s" % src), file=sys.stderr)
            return 1
    else:
        src, err = newest_valid_codex_auth()
        if err:
            print(_UNBLOCK % err, file=sys.stderr)
            return 1
    exp = _cred_exp(src)
    if not exp or exp <= time.time():
        print(_UNBLOCK % ("source cred is expired (%s%s)" % (
            src, ", access token exp " + _rfc3339(exp) if exp else "")), file=sys.stderr)
        return 1
    rec, fname, err = translate_codex_auth(src)
    if err:
        print(_UNBLOCK % err, file=sys.stderr)
        return 1

    d = seat_dir(family)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    auth_dir = os.path.join(d, "auth")
    # The pool premise (codexhomes.py): one-cred-per-seat is a DEFAULT, not an
    # invariant — pooled creds from OTHER accounts are the proxy's usage-cap
    # fall-through and must survive a seat re-add. Replace only the SAME
    # account's file(s); never delete what can't be attributed (fail-open —
    # `helm codex pooled` reports junk, the proxy skips it).
    removed, kept = [], 0
    for pooled in glob.glob(os.path.join(auth_dir, "codex-*.json")):
        base = os.path.basename(pooled)
        if base == fname:
            continue  # the mint rewrites this spelling in place below
        try:
            with open(pooled) as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = None
        acct = old.get("account_id") if isinstance(old, dict) else None
        if acct and acct == rec.get("account_id"):
            os.remove(pooled)  # same account, stale spelling — this re-add IS its refresh
            removed.append(base)
            continue
        kept += 1
    _write_private(os.path.join(auth_dir, fname),
                   json.dumps(rec, indent=2, sort_keys=False) + "\n")
    token = _seat_token(family, d)
    _write_private(os.path.join(d, "config.yaml"),
                   _config_yaml(fam["port"], auth_dir, token))
    if _write_launch_assets(family, d, room, room_source=room_source) \
            is _SEAT_SURFACE_REFUSED:
        return 1

    print("helm seat: %s seat minted at %s" % (family, d))
    print("  cred %s (%s) from %s (read-only), access token valid until %s"
          % (rec["email"], fname.rsplit("-", 1)[1][:-5], src, rec["expired"]))
    if removed:
        print("  replaced same-account pooled cred%s: %s"
              % ("s"[:len(removed) != 1], ", ".join(sorted(removed))))
    if kept:
        print("  %d other pooled cred%s preserved (the proxy's usage-cap "
              "fall-through) — `helm codex pooled` lists them"
              % (kept, "s"[:kept != 1]))
    print("  proxy port %d; next: `helm seat up %s`, then `helm seat launch %s`"
          % (fam["port"], family, family))
    return 0
