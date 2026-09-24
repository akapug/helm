"""Seat credential selection and provisioning for :mod:`helm.seat`."""
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
    proxy_config_plan,
    regenerate_proxy_config,
    proxy_alias_drift,
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
    _seed_seat_rules,
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
    _safe_endpoint,
)
from .seat_catalog import KEYLESS_API_KEY_PLACEHOLDER, instance_launch_model, \
    pool_base_url
from . import pk

_IMPL_MODULES = (_assets_impl,)

def _mint_providers(family, fam, base_url):
    """The provider blocks this family's FIRST config must carry, or None.

    None for every family whose config is one block — and None is what keeps
    their minted bytes identical, because `_config_yaml_key` then takes its
    single-block path. A per-model family answers with one entry per declared
    model, the default first and carrying the frontmatter ids, derived from
    `proxy_routes` so the mint and the regeneration read ONE declaration
    rather than two copies that can drift.
    """
    from .seat_catalog import family_default_provider, family_model_providers, \
        proxy_routes
    if not family_model_providers(fam):
        return None
    default_block = family_default_provider(fam)
    return tuple({"provider": route["provider"],
                  "base_url": route["base_url"] or base_url,
                  "alias": route["alias"],
                  "upstream": route["upstream_model"],
                  "frontmatter": route["provider"] == default_block}
                 for route in proxy_routes(family))


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
    is visible, not silent.

    A `keyless` family HAS NO KEY AND SAYS SO. Its endpoint takes no
    Authorization header (qwen27: local weights on the owner's own LAN box),
    so the whole key chain above is skipped: there is no env var to read, no
    --key-from file to open and no pool to reach into, and none of those
    absences is a failure to report.

    THE BLOCK STILL CARRIES ONE ENTRY, and that is the proxy's requirement
    rather than ours — `KEYLESS_API_KEY_PLACEHOLDER` records the probe that
    settled it. Writing no entry at all was tried first and produced a proxy
    with zero clients that accepted requests and never answered them."""
    keyless = bool(fam.get("keyless"))
    key_env = fam.get("key_env")
    # A KEYLESS FAMILY NAMES NO ENV VAR AT ALL, so the chain is empty rather
    # than a one-element tuple holding None — which os.environ.get raises on,
    # before any keyless branch below could be reached.
    key_vars = ((key_env,) if key_env else ()) \
        + tuple(fam.get("key_env_fallbacks") or ())
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
        provider = prov_cfg.get("proxy_provider") or pool_provider
        base_url, why = pool_base_url(prov_cfg)
        if why:
            print("helm seat: %s, then re-run `helm seat add %s`"
                  % (why, family), file=sys.stderr)
            return 1
        # A CONFIGURED ENDPOINT MEETS THE RECONCILE'S FLOOR AT THE MINT: the
        # value is the operator's now, and a config written past the floor
        # is one the reconcile refuses to maintain.
        if keyless and not _safe_endpoint(base_url, keyless=True):
            print("helm seat: %s is not an endpoint a keyless seat may use "
                  "(https, or plain http to a literal private or loopback "
                  "address)" % base_url, file=sys.stderr)
            return 1
        upstream = prov_cfg["upstream_model"]
    if keyless:
        # THE CHAIN IS NOT RUN AT ALL, rather than run and then forgiven. A
        # keyless family sent down the ordinary path would report "no outbound
        # key" as a failure, and forgiving that failure here would forgive it
        # for every family that really does need one.
        api_key, key_var = None, None
    elif not api_key and "--key-from" in args:
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
    if not api_key and pool_provider and not keyless:
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
    if not api_key and not keyless:
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
    if pool_provider is None and not keyless:
        # non-pool proxy-key (kimi): dispatch the outbound endpoint by key shape
        base_url = _key_base_url(fam, api_key)
    d = seat_dir(family)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    token = _seat_token(family, d)
    # A PER-MODEL FAMILY IS MULTI-BLOCK FROM THE FIRST BYTE. Minting one block
    # and letting `seat up` grow the rest would put every model on one
    # credential entry for the window between the two commands, which is the
    # exact state CLIProxyAPI's per-CREDENTIAL cooldown turns into "the whole
    # family goes dark on one model's 429" (measured — see the catalog entry).
    _write_private(os.path.join(d, "config.yaml"),
                   _config_yaml_key(fam["port"], token, provider,
                                    base_url, fam["model"], api_key, upstream,
                                    api_keys=(KEYLESS_API_KEY_PLACEHOLDER,)
                                    if keyless else None,
                                    providers=_mint_providers(family, fam,
                                                              base_url)))
    # MINT-ONLY DOOR: nonfatal on a shortened contract (the seat and config
    # are still created, and the minted launch.sh refuses to start a session
    # until the guard resolves). A surface refusal is still fatal.
    if _write_launch_assets(family, d, room, room_source=room_source,
                            model=_persisted_model(d, family),
                            fatal_shortened=False) \
            is _SEAT_SURFACE_REFUSED:
        return 1
    print("helm seat: %s seat minted at %s" % (family, d))
    if keyless:
        print("  no outbound key: this endpoint takes none, and the block "
              "carries the declared placeholder the proxy requires; "
              "provider %s -> %s" % (provider, base_url))
    else:
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

    ADDED FOR the r1 FIX, and the asymmetry it named is the whole argument:
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
        with pk.open_regular(path, encoding="utf-8") as f:
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


def _auth_path_outside_the_seat(d, auth):
    """(path, escaped_to) for the first thing under this seat's auth-dir that
    resolves OUTSIDE the seat's own home, else (None, None).

    A SEAT THAT DOES NOT OWN ITS AUTH-DIR IS A SECOND WRITER ON SOMEBODY
    ELSE'S CREDENTIAL, and the mint could not see it. Reproduced through this
    very door: with `<seat>/auth` a symlink at another family's auth-dir, the
    glob below finds that family's credential THROUGH the link, so the seat
    counts as already provisioned, the share-copy is skipped, the config is
    written naming `<seat>/auth`, and the proxy — which rewrites its auth file
    on every access-token refresh — refreshes into the OTHER family's file.
    The mint printed `seat minted` and returned 0. A symlink to an individual
    credential FILE is the same hazard by a shorter path: a write follows it.

    WHAT IS REFUSED IS DIVERGENCE, NOT RELOCATION. The comparison is against
    the realpath of THIS seat's own directory, so a seats root that lives
    behind a link, or on another volume, moves the whole home together and
    passes. What fails is one seat's auth reaching somewhere its own home does
    not — which is the only shape that makes two seats share a writable file.

    NOTHING IS REPAIRED HERE. The offending path may be the only copy of a
    live credential, and a mint that moved or unlinked it to make room would
    be doing to another seat exactly what this check exists to prevent."""
    if not os.path.lexists(auth):
        return None, None
    own = os.path.realpath(d)
    prefix = own + os.sep
    candidates = [auth]
    if os.path.isdir(auth):
        try:
            candidates += [os.path.join(auth, n) for n in sorted(os.listdir(auth))]
        except OSError:
            pass
    for path in candidates:
        real = os.path.realpath(path)
        if real != own and not real.startswith(prefix):
            return path, real
    return None, None


def _seat_owning(path):
    """The family whose seat home contains `path`, or None. Used only to make
    a refusal name the seat an owner has to go and look at; nothing decides
    anything on it, so an unknown answer costs nothing."""
    for other in FAMILIES:
        root = os.path.realpath(seat_dir(other))
        if path == root or path.startswith(root + os.sep):
            return other
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
    # BEFORE THE FIRST WRITE, because the two lines below are themselves
    # writes through whatever this path resolves to: `makedirs(exist_ok=True)`
    # accepts a symlinked directory and the `chmod` then re-permissions its
    # TARGET.
    escaped, real = _auth_path_outside_the_seat(d, auth)
    if escaped is not None:
        owner = _seat_owning(real)
        print("helm seat: %s's %s leaves this seat's own home%s. A seat that "
              "does not own its auth-dir is a second writer on that "
              "credential: the proxy rewrites its auth file on every token "
              "refresh, so this seat's refresh would land on those bytes."
              % (family, os.path.relpath(escaped, d),
                 " and lands in the %s seat" % owner if owner else ""),
              file=sys.stderr)
        print("  replace it with a real directory this seat owns and re-run "
              "`helm seat add %s`; a family that rides another family's "
              "credential gets its own COPY through "
              "`shares_credential_with`, never a view into the owner's "
              "directory." % family, file=sys.stderr)
        return 1
    os.makedirs(auth, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    os.chmod(auth, 0o700)
    token = _seat_token(family, d)
    cfg = os.path.join(d, "config.yaml")
    # instance 1 IS the family seat (it keeps the family dir and port), so it
    # reads the same instance table every other instance does — `codex` itself
    # may declare a launch model, and an absent row is today's family model.
    _write_private(cfg, _config_yaml(fam["port"], auth, token,
                                     channel=fam.get("auth_type"),
                                     model=instance_launch_model(fam, family),
                                     family=family))

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
    if not have and not adopted:
        found, why = _shared_credential(family, fam)
        if found:
            dst = os.path.join(auth, os.path.basename(found))
            with open(found, "rb") as f:
                blob = f.read()
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.chmod(dst, 0o600)
            adopted = (found, dst, os.stat(found).st_mode & 0o777)
            have = _glob_auth(auth, fam["auth_glob"])
        elif why:
            print("helm seat: %s declares it shares %s's credential and "
                  "cannot: %s" % (family, fam["shares_credential_with"], why),
                  file=sys.stderr)
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
    # MINT-ONLY DOOR: nonfatal on a shortened contract (the seat and config
    # are still created, and the minted launch.sh refuses to start a session
    # until the guard resolves). A surface refusal is still fatal.
    if _write_launch_assets(family, d, room, room_source=room_source,
                            model=_persisted_model(d, family),
                            fatal_shortened=False) \
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


def _shared_credential(family, fam):
    """(path, None) for the sibling credential this family may adopt, else
    (None, why) — and (None, None) when it declares no sharing at all.

    ONE VENDOR CREDENTIAL CAN METER SEVERAL GROUPS, so several families ride
    it: the antigravity account bills its Gemini models against one allowance
    and its Claude and GPT models against another, and seating those as one
    family with fallbacks would change the model under a name a review's
    authority is a claim about.

    THE SHARE IS A COPY, NOT A SHARED DIRECTORY, and the difference is a write
    race. The proxy REWRITES its auth file when the access token refreshes —
    measured on this host: the gemini seat's antigravity-*.json mtime tracks
    its own `expired` stamp, an hour apart — so two proxies pointed at one
    directory are two writers on one file. A copy also keeps `helm seat add`
    out of another seat's credential: this function only ever READS the
    sibling's file.

    WHAT IT DOES NOT PROVE is that the upstream tolerates several holders of
    one refresh token. Ordinary OAuth mints an access token per holder and
    rotates nothing; an upstream that rotated the refresh token on every use
    would invalidate the siblings' copies in turn, and that has not been
    measured here. The symptom would be seats failing to refresh one after
    another, not at once."""
    source = fam.get("shares_credential_with")
    if not source:
        return None, None
    sibling = FAMILIES.get(source) or {}
    src_auth = os.path.join(seat_dir(source), "auth")
    found = _glob_auth(src_auth, fam["auth_glob"])
    if not found:
        return None, ("%s holds no %s credential in %s — mint it there first "
                      "(`helm seat add %s`, then its login)"
                      % (source, sibling.get("auth_type") or "matching",
                         src_auth, source))
    # CONTENT, NOT JUST THE FILENAME — the same bar `--auth-from` is held to,
    # for the same reason: a credential that cannot authenticate is not a
    # seat, and copying an expired one here would mint a seat that reports
    # success and answers nothing.
    newest = max(found, key=os.path.getmtime)
    bad = _oauth_cred_reason(newest)
    if bad:
        return None, ("%s's credential %s cannot authenticate: %s"
                      % (source, os.path.basename(newest), bad))
    return newest, None


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
    # THE POOL IS WRITTEN THROUGH ONE DOOR (task/2478 R4). A mint that
    # scanned, removed and wrote the proxy's auth-dir right here, with its
    # own glob and its own `_write_private`, would be a writer that never
    # takes the pool lock: `cred_follow`'s locked re-proof cannot see such a
    # writer coming, and a seat add landing between that re-proof and the
    # follow's write is replaced by orca's account. The pool premise is
    # unchanged (one cred per seat is a DEFAULT, not an invariant: other
    # accounts survive a re-add, only the SAME account's stale spellings are
    # retired, nothing unattributable is deleted) and lives where the lock is:
    # `codexhomes.pool_provision`, which also refuses — writing nothing — when
    # the file this mint would create already holds a DIFFERENT account, or a
    # KNOWN account while this mint's credential names none (task/2514).
    from . import codexhomes  # lazy: codexhomes imports helm.seat at module top
    if os.path.abspath(auth_dir) != os.path.abspath(codexhomes.pool_dir()):
        print("helm seat: family %s is mode proxy but its auth-dir %s is not "
              "the codex pool %s — the locked pool door serves the codex pool "
              "only, and this mint will not write a credential anywhere else"
              % (family, auth_dir, codexhomes.pool_dir()), file=sys.stderr)
        return 2
    pooled = codexhomes.pool_provision(rec, fname)
    if pooled.get("error"):
        print("helm seat: %s" % pooled["error"], file=sys.stderr)
        return 1
    removed, kept = pooled["removed"], pooled["kept"]
    token = _seat_token(family, d)
    _write_private(os.path.join(d, "config.yaml"),
                   _config_yaml(fam["port"], auth_dir, token,
                                channel=fam.get("auth_type"),
                                model=instance_launch_model(fam, family),
                                family=family))
    # MINT-ONLY DOOR: nonfatal on a shortened contract (the seat and config
    # are still created, and the minted launch.sh refuses to start a session
    # until the guard resolves). A surface refusal is still fatal.
    if _write_launch_assets(family, d, room, room_source=room_source,
                            model=_persisted_model(d, family),
                            fatal_shortened=False) \
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
