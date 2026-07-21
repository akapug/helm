#!/usr/bin/env python3
"""helm seat — multimodel seats for the claude-code harness.

A seat gives a NON-Claude model family the full claude-code harness (hooks,
skills, subagents) by pointing one isolated claude invocation at a local
Anthropic-wire proxy (CLIProxyAPI) that authenticates to the family's own
subscription OAuth. Proven live 2026-07-18 (see the claudex seat eval): codex
gpt-5.6-sol passed plain-prompt, tool round-trip, and subagent-spawn legs.

Family table is data: "proxy" families (codex/OpenAI OAuth) need CLIProxyAPI;
first-party-Anthropic-compatible families (kimi/glm/deepseek) need only a
base-url + key and slot in without a proxy — same seat dir, same launch shape.

FLEET DELIVERY + IDENTITY: a seat is a first-class chat member. launch_line
exports HELM_CHAT_NAME=<family>, so the SessionStart join hook registers the
seat in the roster under its family name ('codex'/'kimi'/…) — @codex / @kimi
and owner posts then deliver to it between tool calls. The delivery hooks
(deliver + join) live in the seat's claude/ config dir; `helm hooks install`
wires them there (hooks.py's DELIVERY_SPECS) and `helm hooks status` reports
seat coverage. The same launch line wires dregg-native client signing:
HELM_CELL_BIN=dregg-client-sign + per-seat HELM_CELL_PROFILE/DREGG_PROFILE,
so each family writes cave turns as its own stable cell instead of inheriting the
owner's profile. A seat already running an old session must be relaunched (a
fresh `helm seat launch`) to pick up the identity, signer, and hooks.

Seat dir (~/.helm/_global/seats/<family>/, 0700):
  config.yaml   proxy config (0600 — carries the per-seat proxy token)
  auth/         proxy auth-dir with the TRANSLATED cred (0600). The proxy
                refreshes tokens into THIS copy only; the source cred home
                under ~/.codex-homes is read-only to helm, forever.
  token         the random per-seat proxy token (0600)
  launch.sh     the pasteable/executable launch preset (0700)
  claude/       the seat's isolated CLAUDE_CONFIG_DIR
  smoke-claude/ throwaway CLAUDE_CONFIG_DIR, recreated per smoke run
  proxy.pid / proxy.log

THE SCRUB GUARD (contract — mechanical, not advisory):
A CLAUDE-model seat must never inherit proxy env. Any helm code path that
launches, prints, or mints a `claude` invocation destined for a Claude
Max-OAuth seat MUST compose one of:
  scrub_env(env)  -> copy of env with SCRUB_VARS removed (subprocess launches)
  scrub_prefix()  -> "env -u VAR ..." string prefix (printed shell commands)
so ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY can never
bleed from a proxied seat's shell into a Max-OAuth seat. Known integration
points for the integrator: sessions.resume_command(), transcripts.make_cmd().

HARD LAWS:
  - ANTHROPIC_API_KEY is never written anywhere by this module; launch lines
    actively unset it (env -u).
  - Source auth.json files are read + translated only — never modified.
  - Token-bearing files exist only inside the seat dir, 0600 from creation.
  - No browser logins: an expired/absent source cred prints the human's
    unblock line and exits 1.

Import-safe, stdlib-only.
"""
import base64
import glob
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

from . import home

# The env triple that must never reach a Claude Max-OAuth seat.
SCRUB_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")

CODEX_HOMES = os.path.join(os.path.expanduser("~"), ".codex-homes")
PROXY_BIN_DEFAULT = os.path.join(os.path.expanduser("~"), ".local", "bin", "cli-proxy-api")
DREGG_SIGNER_DEFAULT = os.path.join(os.path.expanduser("~"), ".local", "bin",
                                    "dregg-client-sign")

# Presets as data (the addendum's table). Three modes: "proxy" (OAuth cred
# translated into CLIProxyAPI, e.g. codex), "proxy-key" (an API-key provider
# behind the same proxy via its openai-compatibility block, e.g. kimi), and
# "first-party" (Anthropic-compatible endpoint, no proxy — future glm/deepseek:
# only mode+base_url+key_env needed).
FAMILIES = {
    "codex": {"port": 8317, "model": "gpt-5.6-sol", "mode": "proxy",
              # CC hardcodes a 200k window for any non-`claude-` model and never
              # asks the proxy; gpt-5.6-sol's real window is 372k, so autocompact
              # under-fires and the seat 400s past the real limit (unrecoverable
              # in-band). CLAUDE_CODE_MAX_CONTEXT_TOKENS (launch_line) teaches CC
              # the real window — shaved to 360k (seats request max_tokens=32k, CC
              # reserves 20k). Small-window codex families (spark 128k) want 128000.
              "max_context": 360000},
    # kimi rides the kimi.com CODING-plan endpoint (dual-wire; OpenAI wire at
    # /coding/v1 — live-verified 2026-07-20). A Moonshot PLATFORM key would
    # need base_url https://api.moonshot.ai/v1 instead; platform endpoints
    # reject coding-plan keys ("Invalid Authentication") and vice versa.
    "kimi": {"port": 8318, "model": "kimi-k3", "mode": "proxy-key",
             "base_url": "https://api.kimi.com/coding/v1",
             "key_env": "KIMI_API_KEY", "provider": "moonshot"},
}

# CC's autocompact trigger = pct × (window − 20k). Against the CORRECT window it
# otherwise fires with only a thin margin under a 32k-max_tokens turn; 78% lands
# the trigger with real headroom (sol ≈ 265k, well under the ~340k reject point;
# spark ≈ 84k, under 128k). Honored only for non-`claude-` model names — exactly
# the proxy seats. Both env knobs verified in CC 2.1.216 (undocumented — re-verify
# on CC upgrades: `strings` the binary for the names).
AUTOCOMPACT_PCT_OVERRIDE = "78"

_USAGE = """usage: helm seat <verb> [args]
  add <family> [--auth-from <path>]   mint the seat (translate cred read-only)
               [--key-from <path>]    proxy-key families: .env-style key file
               [--room R]             home the seat's chat in team room R
  up <family> | down <family>         start/stop the seat's local proxy
  launch <family> [--model M] [--room R]  print the exact launch line (never runs it)
  smoke <family>                      the 4-leg acceptance gate (prompt/tool/subagent/whisper)
  list | status                       seats, proxy liveness, cred expiry
  doctor                              binary + cred + seat health, read-only
families: %s""" % ", ".join(sorted(FAMILIES))


# ---------------------------------------------------------------------------
# the scrub guard
# ---------------------------------------------------------------------------

def scrub_env(env):
    """A copy of `env` with the proxy triple removed. Compose this into every
    subprocess env that launches claude for a CLAUDE-model seat."""
    return {k: v for k, v in dict(env).items() if k not in SCRUB_VARS}


def scrub_prefix():
    """The printed-command form of the guard: an `env -u ...` prefix for
    pasteable claude commands minted for Claude seats."""
    return "env " + " ".join("-u " + v for v in SCRUB_VARS) + " "


# ---------------------------------------------------------------------------
# paths + small primitives
# ---------------------------------------------------------------------------

def seats_root():
    return os.path.join(home.global_dir(), "seats")


def seat_dir(family):
    return os.path.join(seats_root(), family)


def _write_private(path, text, mode=0o600):
    """Token-bearing writes: mode enforced from creation (O_CREAT with mode),
    re-enforced on rewrite of an existing file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, mode)


def _jwt_claims(tok):
    """Unverified payload decode — identity/expiry label, not authentication."""
    try:
        payload = (tok or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


def _rfc3339(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _proxy_bin():
    """HELM_PROXY_BIN else ~/.local/bin/cli-proxy-api else PATH; None missing."""
    explicit = home.env("PROXY_BIN")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    if os.path.exists(PROXY_BIN_DEFAULT):
        return PROXY_BIN_DEFAULT
    return shutil.which("cli-proxy-api")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, TypeError):
        return False
    except PermissionError:
        return True


def _running_pid(family):
    """The live proxy pid from the seat's pidfile, else None."""
    try:
        with open(os.path.join(seat_dir(family), "proxy.pid")) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def _port_open(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False


def _read_token(family):
    try:
        with open(os.path.join(seat_dir(family), "token")) as f:
            return f.read().strip()
    except OSError:
        return None


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
    the account_id whenever identity knows it (dedup + linkage key off it)."""
    try:
        with open(src_path) as f:
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
    """The access_token exp epoch of an auth.json, None when unparseable."""
    try:
        with open(src_path) as f:
            a = json.load(f)
    except (OSError, ValueError):
        return None
    exp = _jwt_claims((a.get("tokens") or {}).get("access_token")).get("exp")
    return exp if isinstance(exp, (int, float)) else None


def newest_valid_codex_auth():
    """Newest-mtime non-expired ~/.codex-homes/*/auth.json (realpath-deduped,
    symlink alias homes collapse). (path, None) or (None, reason)."""
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
        return None, ("no valid codex cred under %s (absent or every access "
                      "token expired)" % CODEX_HOMES)
    return max(cands)[1], None


_UNBLOCK = """helm seat: %s
helm seat will never open a browser login itself. Unblock (human, one-time):
  CODEX_HOME=~/.codex-homes/<home> codex login --device-auth
then re-run `helm seat add codex`."""


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

def _config_yaml(port, auth_dir, token):
    """The proxy config that passed the live eval, verbatim shape."""
    return ('host: "127.0.0.1"\n'
            "port: %d\n"
            'auth-dir: "%s"\n'
            "api-keys:\n"
            '  - "%s"\n'
            "debug: false\n"
            "usage-statistics-enabled: false\n"
            "remote-management:\n"
            "  allow-remote: false\n"
            '  secret-key: ""\n'
            "  disable-control-panel: true\n") % (port, auth_dir, token)


def _config_yaml_key(port, token, provider, base_url, model, api_key):
    """The proxy-key config: same inbound head (the per-seat token claude
    presents), no auth-dir (no OAuth cred), plus the openai-compatibility
    provider block carrying the outbound API key (0600 via _write_private —
    the same trust level as the seat token beside it)."""
    return ('host: "127.0.0.1"\n'
            "port: %d\n"
            "api-keys:\n"
            '  - "%s"\n'
            "debug: false\n"
            "usage-statistics-enabled: false\n"
            "remote-management:\n"
            "  allow-remote: false\n"
            '  secret-key: ""\n'
            "  disable-control-panel: true\n"
            "openai-compatibility:\n"
            '  - name: "%s"\n'
            '    base-url: "%s"\n'
            "    api-key-entries:\n"
            '      - api-key: "%s"\n'
            "    models:\n"
            '      - name: "%s"\n'
            '        alias: "%s"\n'
            % (port, token, provider, base_url, api_key, model, model))


def _instance_dir(family, seat):
    """An instance's isolated config root: seat != family (codex-2, codex-3…)
    lives under instances/<seat>; instance 1 keeps the family dir (back-compat)."""
    return os.path.join(seat_dir(family), "instances", seat) \
        if seat and seat != family else seat_dir(family)


def launch_line(family, model=None, room=None, seat=None):
    """The exact seat launch command. env -u ANTHROPIC_API_KEY is part of the
    line: an inherited key must never ride into a proxied seat either.
    HELM_CHAT_NAME=<seat> is the STABLE seat identity: the SessionStart join
    hook (seats.py derive_seat) keys the roster on it, so the seat joins as
    'codex'/'codex-2'/'kimi'/… instead of an ephemeral agent-<sid8> — and
    @codex / @codex-2 / @kimi fleet posts then deliver to it. HELM_CELL_PROFILE
    + DREGG_PROFILE bind both helm's signing call and the dregg SDK fallback to
    that SAME seat identity; HELM_CELL_BIN selects the dregg-native client
    signer. A seat therefore never inherits the owner's ambient profile. `seat`
    (slice 6 — N-per-credhome) defaults to the family name; when set it swaps
    the three identity vars + the config dir (instances/<seat>) so N instances
    of one family share the proxy/port/token/pool but never config/session
    state. `room` (seat add/launch --room) adds HELM_CHAT_ROOM=<room> —
    team-room homing (slice 3): the seat's chat defaults (post/read/join/
    deliver) live in its team channel while the multi-room deliver still hears
    @mentions from any room; no room ⇒ no export, exactly today's main-homed
    fleet. --dangerously-skip-permissions is CANONICAL for a fleet seat
    (owner-asked 2026-07-21): an agent pane exists to do work unattended, and
    a per-tool permission prompt strands it silently (the owner had to flip
    kimi/codex into auto-mode by hand). The beacon permit narrows an
    interactive session; a launched seat skips wholesale — it never has a
    human at its keyboard to answer a prompt."""
    fam = FAMILIES[family]
    model = model or fam["model"]
    seat = seat or family
    cfgdir = shlex.quote(os.path.join(_instance_dir(family, seat), "claude"))
    homing = (" HELM_CHAT_ROOM=%s" % shlex.quote(room)) if room else ""
    # Teach CC the seat's real context window + a safe autocompact margin so a
    # non-claude model never sails past its window into the unrecoverable 400
    # (navigate-multimodel-cc-context / ctx-window-recovery-is-clear). Appended
    # AFTER the signing env so HELM_CELL_BIN/PROFILE + DREGG_PROFILE stay
    # byte-identical.
    ctxenv = " CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=%s" % AUTOCOMPACT_PCT_OVERRIDE
    if fam.get("max_context"):
        ctxenv += " CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % fam["max_context"]
    return ("env -u ANTHROPIC_API_KEY"
            " ANTHROPIC_BASE_URL=http://127.0.0.1:%d"
            " ANTHROPIC_AUTH_TOKEN=%s"
            " CLAUDE_CODE_SUBAGENT_MODEL=%s"
            " CLAUDE_CONFIG_DIR=%s"
            " HELM_CHAT_NAME=%s%s"
            " HELM_CELL_BIN=%s"
            " HELM_CELL_PROFILE=%s"
            " DREGG_PROFILE=%s%s"
            " claude --dangerously-skip-permissions --model %s"
            % (fam["port"], _read_token(family) or "<seat-token-missing>",
               model, cfgdir, shlex.quote(seat), homing,
               shlex.quote(DREGG_SIGNER_DEFAULT), shlex.quote(seat),
               shlex.quote(seat), ctxenv, model))


def _seat_token(family, d):
    """Read-or-mint the per-seat proxy token — stable across re-adds so a
    minted launch line stays valid."""
    token = _read_token(family)
    if not token:
        import secrets
        token = secrets.token_hex(32)
        _write_private(os.path.join(d, "token"), token + "\n")
    return token


def _write_launch_assets(family, d, room=None, seat=None):
    """The seat's isolated CLAUDE_CONFIG_DIR + the executable launch preset —
    identical for every mode, and refreshed by BOTH `add` and `launch` (a
    stale launch.sh minted before HELM_CHAT_NAME existed is why the live
    kimi seat was absent from the roster). The claude dir is born WIRED
    (G-seatlaunch-installs): the delivery lane (deliver + join + stop-guard)
    plus the beacon permit land here at creation through hooks.py's gated
    merge-preserving write — a seat must never be born deaf. Install trouble
    is loud (stderr) but never fatal: the seat still mints and the message
    names the estate-wide repair. `seat` (slice 6) mints an INSTANCE's assets
    (instances/<seat>/{claude,launch.sh}); the shared token/config.yaml stay
    family-level and are NOT re-minted here."""
    seat = seat or family
    cdir = os.path.join(d, "claude")
    os.makedirs(cdir, exist_ok=True)
    from . import hooks
    action, detail = hooks.install_home(cdir, specs=hooks.DELIVERY_SPECS)
    if action == "fail":
        print("helm seat: WARNING — %s delivery hooks not installed (%s); "
              "`helm hooks install` closes it" % (seat, detail),
              file=sys.stderr)
    elif action != "ok":
        print("helm seat: %s claude dir wired for fleet delivery (%s: "
              "deliver + join + stop-guard + beacon permit)" % (seat, action),
              file=sys.stderr)
    _write_private(os.path.join(d, "launch.sh"),
                   "#!/bin/sh\n# helm seat %s — minted by `helm seat add`; "
                   "regenerate with `helm seat launch %s`\nexec %s \"$@\"\n"
                   % (seat, seat, launch_line(family, room=room, seat=seat)),
                   mode=0o700)


def _env_file_value(path, key):
    """The value of the `key=...` line in a .env-style file (`export ` prefix
    and surrounding quotes tolerated); None absent/unreadable. The value is
    secret — callers must never print or log it."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if line.startswith(key + "="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                return val
    return None


def _add_proxy_key(family, fam, args, room=None):
    """mode "proxy-key": an API-key provider behind the same local proxy via
    its openai-compatibility block. No OAuth, no auth-dir. Key source order:
    $<key_env>, then --key-from <.env-style file>. The key is baked into the
    seat's 0600 config.yaml once, at add time — never printed, never logged."""
    key_env = fam["key_env"]
    api_key = os.environ.get(key_env)
    if not api_key and "--key-from" in args:
        path = os.path.expanduser(args[args.index("--key-from") + 1])
        api_key = _env_file_value(path, key_env)
        if not api_key:
            print("helm seat: no %s= line found in %s" % (key_env, path),
                  file=sys.stderr)
            return 1
    if not api_key:
        print("helm seat: no outbound key — export %s=<key> or pass "
              "--key-from <env-file> carrying a %s= line, then re-run "
              "`helm seat add %s`" % (key_env, key_env, family), file=sys.stderr)
        return 1
    d = seat_dir(family)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    token = _seat_token(family, d)
    _write_private(os.path.join(d, "config.yaml"),
                   _config_yaml_key(fam["port"], token, fam["provider"],
                                    fam["base_url"], fam["model"], api_key))
    _write_launch_assets(family, d, room)
    print("helm seat: %s seat minted at %s" % (family, d))
    print("  outbound %s key baked into config.yaml (0600 — value never "
          "printed); provider %s -> %s" % (key_env, fam["provider"], fam["base_url"]))
    print("  proxy port %d; next: `helm seat up %s`, then `helm seat launch %s`"
          % (fam["port"], family, family))
    return 0


def _add(family, args, room=None):
    fam = FAMILIES.get(family)
    if fam is None:
        print("helm seat: family '%s' not yet wired (have: %s). First-party "
              "Anthropic-compatible families need only a FAMILIES entry with "
              "base_url + key_env — see helm/seat.py." % (family, ", ".join(sorted(FAMILIES))),
              file=sys.stderr)
        return 2
    if fam["mode"] == "proxy-key":
        return _add_proxy_key(family, fam, args, room)
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
    _write_launch_assets(family, d, room)

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


# ---------------------------------------------------------------------------
# up / down
# ---------------------------------------------------------------------------

def _require_seat(family):
    if FAMILIES.get(family) is None:
        print("helm seat: unknown family '%s' (have: %s)"
              % (family, ", ".join(sorted(FAMILIES))), file=sys.stderr)
        return None
    if not os.path.exists(os.path.join(seat_dir(family), "config.yaml")):
        print("helm seat: no %s seat yet — `helm seat add %s`" % (family, family),
              file=sys.stderr)
        return None
    return FAMILIES[family]


def _up(family, quiet=False):
    fam = _require_seat(family)
    if fam is None:
        return 1
    pid = _running_pid(family)
    if pid:
        print("helm seat: %s proxy already running (pid %d, port %d) — "
              "`helm seat down %s` first" % (family, pid, fam["port"], family),
              file=sys.stderr)
        return 1
    b = _proxy_bin()
    if not b:
        print("helm seat: cli-proxy-api binary not found (HELM_PROXY_BIN, %s, "
              "PATH all empty) — run `helm seat doctor`" % PROXY_BIN_DEFAULT,
              file=sys.stderr)
        return 1
    d = seat_dir(family)
    log = open(os.path.join(d, "proxy.log"), "ab")
    try:
        p = subprocess.Popen([b, "-config", os.path.join(d, "config.yaml")],
                             stdout=log, stderr=log, start_new_session=True)
    except OSError as exc:
        print("helm seat: proxy failed to launch: %s" % exc, file=sys.stderr)
        return 1
    finally:
        log.close()
    _write_private(os.path.join(d, "proxy.pid"), "%d\n" % p.pid)
    for _ in range(30):  # up to ~6s for the port to open
        if p.poll() is not None or _port_open(fam["port"]):
            break
        time.sleep(0.2)
    if p.poll() is not None:
        os.remove(os.path.join(d, "proxy.pid"))
        print("helm seat: proxy exited rc %s — tail %s"
              % (p.returncode, os.path.join(d, "proxy.log")), file=sys.stderr)
        return 1
    if not quiet:
        print("helm seat: %s proxy up — 127.0.0.1:%d (pid %d)"
              % (family, fam["port"], p.pid))
    return 0


def _down(family):
    fam = _require_seat(family)
    if fam is None:
        return 1
    pid = _running_pid(family)
    pidfile = os.path.join(seat_dir(family), "proxy.pid")
    if not pid:
        if os.path.exists(pidfile):
            os.remove(pidfile)  # stale
        print("helm seat: %s proxy not running" % family)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(15):
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    if _pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
    os.remove(pidfile)
    print("helm seat: %s proxy stopped (pid %d)" % (family, pid))
    return 0


# ---------------------------------------------------------------------------
# smoke — the 4-leg acceptance gate
# ---------------------------------------------------------------------------

def _seat_env(family, config_dir):
    """The proxied-seat subprocess env: scrubbed base (so a stray inherited
    ANTHROPIC_API_KEY can never ride along), then the seat's own proxy, chat,
    and dregg-signing identity. Mirrors launch_line so smoke cannot certify a
    materially different process shape."""
    fam = FAMILIES[family]
    env = scrub_env(os.environ)
    env.update({
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % fam["port"],
        "ANTHROPIC_AUTH_TOKEN": _read_token(family) or "",
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDE_CODE_SUBAGENT_MODEL": fam["model"],
        "HELM_CHAT_NAME": family,
        "HELM_CELL_BIN": DREGG_SIGNER_DEFAULT,
        "HELM_CELL_PROFILE": family,
        "DREGG_PROFILE": family,
    })
    return env


def _smoke(family):
    fam = _require_seat(family)
    if fam is None:
        return 1
    if not shutil.which("claude"):
        print("helm seat: `claude` not on PATH — cannot smoke", file=sys.stderr)
        return 1
    if not _running_pid(family):
        if _up(family, quiet=True) != 0:
            return 1
        print("  (proxy was down — auto-started)")
    model = fam["model"]
    smoke_dir = os.path.join(seat_dir(family), "smoke-claude")
    shutil.rmtree(smoke_dir, ignore_errors=True)
    os.makedirs(smoke_dir)
    mark = "%d" % (time.time() % 100000)
    legs = (
        ("prompt", "Say which model family you are in one sentence.", None, None),
        ("tool", "Run exactly this command with the Bash tool: "
                 "echo helm-seat-tool-proof-%s — then report the command "
                 "output verbatim." % mark,
         "Bash(echo:*)", "helm-seat-tool-proof-%s" % mark),
        ("subagent", "Use the Task tool to spawn one subagent whose entire "
                     "task is to reply with exactly: helm-seat-subagent-ok-%s "
                     "plus its model family. Report the subagent's reply "
                     "verbatim." % mark,
         "Task", "helm-seat-subagent-ok-%s" % mark),
    )
    ok = True
    for name, prompt, allowed, marker in legs:
        # prompt rides directly after -p: --allowedTools is variadic and
        # swallows a trailing positional (live-found 2026-07-18)
        cmd = ["claude", "-p", prompt, "--model", model]
        if allowed:
            cmd += ["--allowedTools", allowed]
        try:
            p = subprocess.run(cmd, env=_seat_env(family, smoke_dir),
                               capture_output=True, text=True, timeout=300)
            reply = (p.stdout or "").strip()
            passed = p.returncode == 0 and reply and (marker is None or marker in reply)
            note = "" if passed else " (rc %s%s)" % (
                p.returncode, "; marker missing" if marker and reply else "")
        except subprocess.TimeoutExpired:
            reply, passed, note = "", False, " (timeout 300s)"
        ok = ok and passed
        print("  %-8s %s%s — %s" % (name, "PASS" if passed else "FAIL", note,
                                    (reply or "(no reply)").replace("\n", " ")[:200]))
    print("  %-8s SKIP — whisper: not yet wired" % "whisper")
    print("helm seat: %s smoke %s (model %s)" % (family, "PASS" if ok else "FAIL", model))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# list / status / doctor
# ---------------------------------------------------------------------------

def _seat_row(family):
    d = seat_dir(family)
    fam = FAMILIES.get(family) or {}
    creds = sorted(glob.glob(os.path.join(d, "auth", "*.json")))
    cred = "api-key cred (baked into config.yaml)" \
        if fam.get("mode") == "proxy-key" else "no cred"
    if creds:
        try:
            with open(creds[0]) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            rec = {}
        exp = _jwt_claims(rec.get("access_token")).get("exp")
        state = "cred unparseable"
        if isinstance(exp, (int, float)):
            left = exp - time.time()
            state = "EXPIRED %s" % _rfc3339(exp) if left <= 0 else \
                "valid until %s (%dh left)" % (_rfc3339(exp), left // 3600)
        cred = "%s — %s" % (rec.get("email", "?"), state)
        if len(creds) > 1:
            cred += " (+%d more pooled)" % (len(creds) - 1)
    pid = _running_pid(family)
    port = fam.get("port")
    live = "proxy UP pid %d port %d%s" % (pid, port, "" if _port_open(port) else
                                          " (port not answering!)") if pid \
        else "proxy down"
    row = "%-8s %-38s %s" % (family, live, cred)
    if family == "codex":   # slice 6: live-instance / pooled-capacity suffix
        try:
            from . import codexhomes, seats as _seats
            now = time.time()
            live_n = 0
            for s, r in _seats.roster().items():
                if s == "codex" or s.startswith("codex-"):
                    ls = _seats.last_seen(s, r)
                    if ls and now - ls < _seats.QUIET_S:
                        live_n += 1
            row += "  [instances: %d live / cap %d]" % (
                live_n, codexhomes.capacity()["total"])
        except Exception:
            pass
    return row


def _status(args):
    root = seats_root()
    fams = sorted(f for f in (os.listdir(root) if os.path.isdir(root) else [])
                  if os.path.isdir(os.path.join(root, f)))
    if not fams:
        print("helm seat: no seats yet — `helm seat add codex`")
        return 0
    for f in fams:
        print(_seat_row(f))
    return 0


def _doctor(args):
    b = _proxy_bin()
    if b:
        try:
            v = subprocess.run([b, "--version"], capture_output=True, text=True,
                               timeout=5).stdout.strip().splitlines()
            print("proxy binary: %s (%s)" % (b, v[0] if v else "version unknown"))
        except (OSError, subprocess.TimeoutExpired):
            print("proxy binary: %s (present, --version failed)" % b)
    else:
        print("proxy binary: MISSING — install CLIProxyAPI to %s, e.g.\n"
              "  gh release download v7.2.88 --repo router-for-me/CLIProxyAPI "
              "--pattern 'CLIProxyAPI_*_linux_amd64.tar.gz'" % PROXY_BIN_DEFAULT)
    c = shutil.which("claude")
    print("claude binary: %s" % (c or "MISSING from PATH"))
    src, err = newest_valid_codex_auth()
    if err:
        print("codex cred: " + err)
    else:
        exp = _cred_exp(src)
        print("codex cred: %s (newest valid, access token until %s)"
              % (src, _rfc3339(exp)))
    _status([])
    return 0 if b and c and not err else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_seat(args):
    """seat add|up|down|launch|smoke|list|status|doctor — multimodel seats."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb in ("list", "status"):
        return _status(rest)
    if verb == "doctor":
        return _doctor(rest)
    if verb in ("add", "up", "down", "launch", "smoke"):
        if not rest:
            print("usage: helm seat %s <family>" % verb, file=sys.stderr)
            return 2
        family = rest[0]
        # --room homes the seat in a team channel (default main — un-homed):
        # add/launch bake HELM_CHAT_ROOM=<room> into the line + launch.sh
        room = rest[rest.index("--room") + 1] if "--room" in rest else None
        if verb == "add":
            return _add(family, rest[1:], room=room)
        if verb == "up":
            return _up(family)
        if verb == "down":
            return _down(family)
        if verb == "smoke":
            return _smoke(family)
        fam = _require_seat(family)
        if fam is None:
            return 1
        model = rest[rest.index("--model") + 1] if "--model" in rest else None
        # slice 6 — N instances of one family share the proxy/port/token/pool:
        # -i/--instance N -> seat codex-N (default 1 = today's exact line).
        inst = 1
        for flag in ("-i", "--instance"):
            if flag in rest:
                try:
                    inst = int(rest[rest.index(flag) + 1])
                except (ValueError, IndexError):
                    print("helm seat: %s wants an integer" % flag, file=sys.stderr)
                    return 2
        seat = family if inst <= 1 else "%s-%d" % (family, inst)
        # guards ride stderr (stdout stays the bare pasteable line), warn
        # never refuse: over-capacity burns one pool faster (fall-through
        # masks it) and a live same-named seat is a relaunch-vs-collision the
        # operator calls (a crashed seat must not brick its slot).
        if inst > 1:
            from . import codexhomes, seats as _seats
            cap = codexhomes.capacity()["total"]
            if inst > cap:
                print("helm seat: WARN — instance %d exceeds pooled fleet "
                      "capacity %d (`helm codex capacity`); the pool falls "
                      "through usage caps but %d concurrent seats burn it "
                      "faster" % (inst, cap, inst), file=sys.stderr)
            row = _seats.roster().get(seat)
            ls = _seats.last_seen(seat, row) if row else None
            if row and ls and time.time() - ls < _seats.QUIET_S:
                print("helm seat: WARN — seat %r already live on the roster "
                      "(last seen %.0fs ago) — relaunch or collision is your "
                      "call" % (seat, time.time() - ls), file=sys.stderr)
        # launch REFRESHES the assets first (G-seatlaunch-installs): delivery
        # hooks + beacon permit + a launch.sh carrying the CURRENT identity
        # shape — retrofitting a seat minted before either existed. stdout
        # stays exactly the pasteable line; notes ride stderr.
        _write_launch_assets(family, _instance_dir(family, seat), room, seat)
        print(launch_line(family, model, room, seat))
        from . import hooks
        hooks.surface_uncovered(out=sys.stderr)  # a running joined-late pane
        return 0                                 # still needs its relaunch
    print("helm seat: unknown verb '%s'" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
