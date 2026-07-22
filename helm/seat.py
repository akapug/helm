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

THE CHILD-STAMP GUARD (same register, opposite direction): every minted
launch strips CHILD_STAMP_VARS (CLAUDE_CODE_CHILD_SESSION + the inherited
SID/bridge id) — a seat that inherits them runs as a subprocess child with
transcript persistence silently OFF (bug-class
child-stamp-kills-seat-persistence).

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
import re
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

# The child-session stamp that must never reach a LAUNCHED seat: a pane minted
# by a daemon that was itself started from inside a Claude session inherits
# these, and CC then treats the seat as a subprocess child — transcript
# persistence silently OFF, /branch broken, the session unrecoverable
# (bug-class child-stamp-kills-seat-persistence; live-verified 2026-07-21:
# every fleet seat carried the stamp + the daemon's inherited SID). Every mint
# (launch_line, launch.sh, smoke env) strips the trio so a seat is born a true
# top-level session.
CHILD_STAMP_VARS = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_CODE_BRIDGE_SESSION_ID")

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
              "max_context": 360000,
              # --multi probe models: two DISTINCT models one codex OAuth serves,
              # the exact pair the proven mixed fan-out routed (run-1 2026-07-21).
              "probe_models": ("gpt-5.6-sol", "gpt-5.6-terra")},
    # kimi keys come in two flavors that 401 on each other's endpoint: a
    # CODING-plan key ("sk-kimi-…") wants api.kimi.com/coding/v1 (dual-wire;
    # OpenAI wire live-verified 2026-07-20), a Moonshot PLATFORM key (plain
    # "sk-…") wants api.moonshot.ai/v1 (serves kimi-k3 too — live-verified
    # 2026-07-21). key_base_urls dispatches by key prefix at add time (first
    # match wins); base_url is the no-match default. A mismatched pairing is
    # not a loud failure: the proxy loads the key as an auth, the first call
    # 401s upstream, and CLIProxyAPI quarantines the auth so every later call
    # 503s `auth_unavailable` — hence dispatch-by-shape, not one hardcoded URL.
    "kimi": {"port": 8318, "model": "kimi-k3", "mode": "proxy-key",
             "base_url": "https://api.moonshot.ai/v1",
             "key_base_urls": (("sk-kimi-", "https://api.kimi.com/coding/v1"),),
             "key_env": "KIMI_API_KEY", "provider": "moonshot",
             # one alias in the proxy config -> one probe; the mixed fan-out
             # leg needs two and SKIPs (loudly) for single-model families.
             "probe_models": ("kimi-k3",)},
}

def _family_port_bases_are_unique():
    """One collision-free owner for the port namespace: no two families share
    a base port. The instance derivation (base+N) is per-family, so distinct
    bases are the floor the whole scheme stands on; the interleave headroom
    between a proxy family's base+N range and the next family's base is a
    FAMILIES-table discipline (see `_instance_port`)."""
    bases = [f["port"] for f in FAMILIES.values()]
    return len(bases) == len(set(bases))


assert _family_port_bases_are_unique(), \
    "FAMILIES base ports must be distinct (the instance-port scheme's floor)"

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
               [--room R]             override the project-derived chat room
  up <family> | down <family>         start/stop the seat's local proxy
  launch <family> [--model M] [--room R] [--multi]  print the exact launch line (never runs it)
                                      --multi: mixed-model fleet — DROP the
                                      CLAUDE_CODE_SUBAGENT_MODEL pin (it blunt-pins
                                      over per-agent frontmatter) + mint probe agents
  spawn <seat> [--room R] [--cwd DIR] [--print]  SELF-ONBOARDING spawn: reap a
                                      stale same-name seat, launch via the
                                      detected metaharness (orca/herdr pane +
                                      onboarding injection) or DETACHED HEADLESS
                                      when none (onboarding = the boot first-
                                      prompt), register spawn.json; --print
                                      shows the exact per-harness calls
  where <seat> [--json]               resolve a spawned seat: harness,
                                      handle/pid, worktree, room, liveness
  resume <seat>                       relaunch the seat's pane via the metaharness
                                      (freshest launch.sh + --resume/--continue)
  smoke <family> [--multi]            the 4-leg acceptance gate (prompt/tool/subagent/whisper);
                                      --multi adds the mixed-model fan-out leg (conductor-log-verified)
  autocompact [--threshold N] [--once]  proxy-seat context watchdog: read each
                                      seat's context%%, inject /compact at the
                                      threshold BEFORE the 100%% hang (latched;
                                      --install-timer for the cadence)
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


def child_stamp_unsets():
    """The `-u VAR ...` run that strips the child-session stamp — composed
    into every minted launch line (and, via launch_line, every launch.sh) so
    a launched seat starts as a top-level session with real persistence."""
    return " ".join("-u " + v for v in CHILD_STAMP_VARS)


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
    except (ProcessLookupError, TypeError, ValueError, OverflowError):
        # no such pid, a non-int, or an int too large for C (a corrupt/hostile
        # pidfile body) — all unparseable, all "not a live proxy we can prove".
        return False
    except PermissionError:
        return True


def _pid_identity(pid):
    """Stable process-birth identity used to distinguish a spawned seat from a
    later process that reused its pid. Linux /proc starttime is preferred; ps
    keeps the guard useful on other Unix hosts. None means unverifiable."""
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            tail = f.read().rpartition(") ")[2].split()
        return "proc:%s" % tail[19] if len(tail) > 19 else None
    except (OSError, TypeError, ValueError):
        pass
    try:
        p = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return None
    started = p.stdout.strip()
    return "ps:%s" % started if p.returncode == 0 and started else None


def _recorded_pid_alive(rec):
    """True only when the recorded headless process is still the same process;
    False when gone/reused, None when a live pid cannot be authenticated."""
    pid = (rec or {}).get("pid")
    if not _pid_alive(pid):
        return False
    expected = (rec or {}).get("pid_identity")
    actual = _pid_identity(pid)
    if expected is None or actual is None:
        return None
    return actual == expected


def _proxy_home(family, seat=None):
    """The dir that owns a seat's proxy fate (config.yaml/token/proxy.pid/
    proxy.log). Instance 1 (seat == family) keeps the family dir — back-compat,
    the live 8317 proxy is undisrupted. Instances N≥2 get instances/<seat>/ so
    one instance's proxy restart/429-stall/log never touches a sibling's."""
    seat = seat or family
    return seat_dir(family) if seat == family else _instance_dir(family, seat)


def _instance_port(family, seat=None):
    """A seat's OWN proxy port. Instance 1 keeps fam["port"] (8317 for codex —
    the port every minted launch.sh already points at). Instances N≥2 derive
    deterministically from the numeric seat suffix (codex-2 -> port+2), so the
    mapping needs no allocation state. COLLISION INVARIANT: only mode=proxy
    families mint instances (the launch gate refuses proxy-key families), so
    instance ports come from ONE family's block at a time; a new proxy family
    MUST be assigned a base far enough from every existing proxy family's
    block that base+N ranges never interleave (codex occupies 8317+N; leave
    headroom). `_family_port_bases_are_unique` asserts the bases themselves
    are distinct; the interleave headroom is a FAMILIES-table discipline."""
    seat = seat or family
    base = FAMILIES[family]["port"]
    if seat == family:
        return base
    m = re.match(r"^%s-(\d+)$" % re.escape(family), seat)
    if m:
        return base + int(m.group(1))
    return base  # a non-numeric seat name shares the family port (instance 1)


import contextlib as _contextlib


@_contextlib.contextmanager
def _proxy_lock(family, seat=None):
    """Serialize _up/_down per proxy-home: an flock on <proxy_home>/.proxy.lock.
    Without it two concurrent _up calls both pass the empty-pidfile check and
    double-start, and a _down can delete a CONCURRENT replacement's fresh
    pidfile (killing the old proxy, then unlinking the NEW record — leaving
    the new proxy alive but unmanageable). The lock makes the check→spawn→
    record and the verify→signal→unlink sequences each atomic."""
    import fcntl
    home = _proxy_home(family, seat)
    os.makedirs(home, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(home, ".proxy.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _proxy_pid_record(family, seat=None):
    """The pidfile as an authenticated record: {pid, identity} or None. The
    pidfile carries the process-BIRTH identity beside the pid (`<pid>
    <identity>`) so a later signal goes to the SAME process, never a reused
    pid — the proxy-lifecycle twin of the headless-seat `_pid_identity` guard.
    Legacy bare-`<pid>` files parse with identity=None (treated unverifiable:
    never signalled, reported stale)."""
    try:
        with open(os.path.join(_proxy_home(family, seat), "proxy.pid")) as f:
            parts = f.read().split()
        pid = int(parts[0])
    except (OSError, ValueError, IndexError):
        return None
    return {"pid": pid, "identity": parts[1] if len(parts) > 1 else None}


def _running_pid(family, seat=None):
    """The live proxy pid, ONLY when it is verifiably the SAME process the
    pidfile recorded — a captured birth identity that still matches. FAIL
    CLOSED: a record with no usable identity (legacy bare pid, or '?' from a
    failed capture) is UNVERIFIABLE and returns None, so `_down` treats it as
    stale and never signals the number — the reused-pid SIGTERM finding. A
    live pid whose captured identity no longer matches is a REUSED pid and is
    likewise refused. There is no alive-check-only fallback: trusting an
    unauthenticated number is exactly the hazard this guard exists to close."""
    rec = _proxy_pid_record(family, seat)
    if not rec or not _pid_alive(rec["pid"]):
        return None
    ident = rec["identity"]
    if not ident or ident == "?":
        return None            # unauthenticated: refuse, never signal
    return rec["pid"] if _pid_identity(rec["pid"]) == ident else None


def _port_open(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False


def _read_token(family, seat=None):
    """The seat's proxy token. Instances mint their own; an instance minted
    before per-instance proxies (or mid-migration) falls back to the family
    token so its launch line stays valid."""
    for d in (_proxy_home(family, seat), seat_dir(family)):
        try:
            with open(os.path.join(d, "token")) as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return None


def _token_file(family, seat=None):
    """The 0600 token file a launch line/script should READ AT EXEC TIME.
    Resolves the bearer in the child shell, so the live token never transits
    the script text, the printed stdout line, or any process argv (the
    no-keys-in-argv gate). For an INSTANCE seat this is ALWAYS the instance's
    own path (instances/<seat>/token) — never an existence-based fallback —
    because launch.sh is written BEFORE `_mint_instance_proxy` runs; pointing
    at the instance path means the script picks up the token the mint writes
    a moment later, and stays correct across every later re-mint (the
    first-mint stale-token finding). Instance 1 (seat == family) uses the
    family file. An UNMINTED-instance launch line (printed for an operator
    before `up`) resolves empty until the mint lands — a clean empty var, not
    the wrong account."""
    return os.path.join(_proxy_home(family, seat), "token")


def _token_export(family, seat=None):
    """The shell statement that puts the seat's bearer into the environ WITHOUT
    it ever touching a process argv: read the 0600 token file into a var and
    `export` it (both shell builtins — no external process, no argv). `env`'s
    NAME=value form is deliberately NOT used: the external env binary would
    carry the resolved secret in its own argv (/proc/pid/cmdline). The launch
    line/script prepend this, then exec claude (which inherits the export).
    2>/dev/null keeps an unminted seat's read a clean empty var."""
    return ("ANTHROPIC_AUTH_TOKEN=$(cat %s 2>/dev/null); export ANTHROPIC_AUTH_TOKEN; "
            % shlex.quote(_token_file(family, seat)))


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
    """The proxy config that passed the live eval, verbatim shape. The
    nonstream-keepalive-interval is NOT optional: a long non-streaming pass
    (compaction's ~360k summarize — the longest single request a session
    makes) sits silent while the upstream thinks, the proxy reaps the idle
    socket, and Claude Code gets an empty HTTP 200 ('proxy or gateway
    intercepting') — owner-witnessed on a codex-2 /compact 2026-07-22. The
    live family configs carry 15s by hand; the generator must emit it too or
    every re-mint silently strips the fix (as-prevented)."""
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
            "  disable-control-panel: true\n"
            # heartbeat during long non-streaming thinking passes — see docstring.
            "nonstream-keepalive-interval: 15\n"
            # the STREAMING leg too (owner-witnessed 2026-07-22: with the
            # nonstream keepalive already loaded, EVERY request at ~90% context
            # still died empty-200 — the stream stalls before/during bytes at
            # extreme payload sizes). keepalive-seconds emits SSE heartbeats so
            # a long stream stays alive; bootstrap-retries retries a stream
            # that stalls before its first byte. StreamingConfig has ONLY these
            # two knobs — no upstream/read timeout field exists in the schema.
            "streaming:\n"
            "  keepalive-seconds: 15\n"
            "  bootstrap-retries: 2\n") % (port, auth_dir, token)


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
            # same long-nonstream keepalive as _config_yaml (compaction survival)
            "nonstream-keepalive-interval: 15\n"
            "streaming:\n"
            "  keepalive-seconds: 15\n"
            "  bootstrap-retries: 2\n"
            % (port, token, provider, base_url, api_key, model, model))


def _key_base_url(fam, api_key):
    """The outbound base-url for THIS key: some providers mint key flavors
    bound to different endpoints (kimi coding-plan "sk-kimi-…" vs Moonshot
    platform "sk-…"), and the wrong pairing 401s upstream — which CLIProxyAPI
    answers by quarantining the auth (every later call 503s auth_unavailable).
    Shared by every proxy-key family: an optional key_base_urls tuple of
    (prefix, url) pairs dispatches by key shape, first match wins; families
    without it (or with an unmatched key) keep fam["base_url"]."""
    for prefix, url in fam.get("key_base_urls", ()):
        if api_key.startswith(prefix):
            return url
    return fam["base_url"]


def _instance_dir(family, seat):
    """An instance's isolated config root: seat != family (codex-2, codex-3…)
    lives under instances/<seat>; instance 1 keeps the family dir (back-compat)."""
    return os.path.join(seat_dir(family), "instances", seat) \
        if seat and seat != family else seat_dir(family)


def launch_line(family, model=None, room=None, seat=None, room_source=None,
                multi=False):
    """The exact seat launch command. env -u ANTHROPIC_API_KEY is part of the
    line: an inherited key must never ride into a proxied seat either. The
    child-stamp trio (CHILD_STAMP_VARS) is unset right beside it: a spawning
    daemon born inside a Claude session stamps its panes CLAUDE_CODE_CHILD_
    SESSION=1 (+ its own SID/bridge id), and CC then silently disables the
    seat's transcript persistence — the seat must start top-level.
    HELM_CHAT_NAME=<seat> is the STABLE seat identity: the SessionStart join
    hook (seats.py derive_seat) keys the roster on it, so the seat joins as
    'codex'/'codex-2'/'kimi'/… instead of an ephemeral agent-<sid8> — and
    @codex / @codex-2 / @kimi fleet posts then deliver to it. HELM_CELL_PROFILE
    + DREGG_PROFILE bind both helm's signing call and the dregg SDK fallback to
    that SAME seat identity; HELM_CELL_BIN selects the dregg-native client
    signer. A seat therefore never inherits the owner's ambient profile. `seat`
    (slice 6 — N-per-credhome) defaults to the family name; when set it swaps
    the three identity vars + the config dir (instances/<seat>). Instances
    share ONLY the family OAuth cred pool (same account — no quota
    multiplication); everything else is per-instance: config/session state AND,
    since per-instance proxies, the proxy fate itself — each instance gets its
    own port/config/token/log (`_mint_instance_proxy`), so one instance's
    restart or 429-stall never takes a sibling down. `room` adds
    HELM_CHAT_ROOM=<room>; a project-derived default also
    carries HELM_CHAT_ROOM_SOURCE=derived so later SessionStart joins cannot
    undo an operator rehome/clear. The command clears inherited room/source
    first, making explicit --room and project-less un-homed launches stable.
    --dangerously-skip-permissions is CANONICAL for a fleet seat
    (owner-asked 2026-07-21): an agent pane exists to do work unattended, and
    a per-tool permission prompt strands it silently (the owner had to flip
    kimi/codex into auto-mode by hand). The beacon permit narrows an
    interactive session; a launched seat skips wholesale — it never has a
    human at its keyboard to answer a prompt. `multi` (the proven mixed-model
    law, premise multimodel-one-cc-proven-per-agent-frontmatter-no-fork):
    DROP CLAUDE_CODE_SUBAGENT_MODEL entirely — that env var blunt-pins EVERY
    subagent to one model, overriding the per-agent `model:` frontmatter that
    IS the mixed-fleet mechanism; the probe agents minted beside this line
    carry the per-model pins instead."""
    fam = FAMILIES[family]
    model = model or fam["model"]
    seat = seat or family
    port = _instance_port(family, seat)
    cfgdir = shlex.quote(os.path.join(_instance_dir(family, seat), "claude"))
    homing = (" HELM_CHAT_ROOM=%s" % shlex.quote(room)) if room else ""
    if room and room_source:
        homing += " HELM_CHAT_ROOM_SOURCE=%s" % shlex.quote(room_source)
    # Teach CC the seat's real context window + a safe autocompact margin so a
    # non-claude model never sails past its window into the unrecoverable 400
    # (navigate-multimodel-cc-context / ctx-window-recovery-is-clear). Appended
    # AFTER the signing env so HELM_CELL_BIN/PROFILE + DREGG_PROFILE stay
    # byte-identical.
    ctxenv = " CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=%s" % AUTOCOMPACT_PCT_OVERRIDE
    if fam.get("max_context"):
        ctxenv += " CLAUDE_CODE_MAX_CONTEXT_TOKENS=%d" % fam["max_context"]
    # --multi: no pin (frontmatter routes per-subagent); default: today's line.
    pin = "" if multi else " CLAUDE_CODE_SUBAGENT_MODEL=%s" % model
    # NO-keys-in-argv (codex-2 re-review): the bearer is NEVER a NAME=value arg
    # to the EXTERNAL `env` binary — `env TOKEN=$(cat f)` would put the
    # resolved secret in env's OWN argv (/proc/pid/cmdline). Instead the token
    # is exported into the seat's environ by `_token_export` (a shell builtin,
    # no argv), and the `env` call below only UNSETS inherited vars and sets
    # the non-secret ones. claude inherits the token from the export, so it
    # never transits any process argv, the script text, or the printed line —
    # and it resolves AT EXEC, so the line stays mint-order-immune (the
    # first-mint finding).
    return ("env -u ANTHROPIC_API_KEY %s -u HELM_CHAT_ROOM"
            " -u MELD_CHAT_ROOM -u HELM_CHAT_ROOM_SOURCE"
            " -u MELD_CHAT_ROOM_SOURCE"
            " ANTHROPIC_BASE_URL=http://127.0.0.1:%d"
            "%s"
            " CLAUDE_CONFIG_DIR=%s"
            " HELM_CHAT_NAME=%s%s"
            " HELM_CELL_BIN=%s"
            " HELM_CELL_PROFILE=%s"
            " DREGG_PROFILE=%s%s"
            " claude --dangerously-skip-permissions --model %s"
            % (child_stamp_unsets(),
               port,
               pin, cfgdir, shlex.quote(seat), homing,
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


def _link_skills(cdir):
    """A seat's config dir is a fresh CLAUDE_CONFIG_DIR, so CC discovers NO
    skills there (it never reads the host's ~/.claude or the owner's home) —
    without this a seat agent can't /learn, /premise, /afk, etc. Canonical-
    first (universal skill distribution): point <cdir>/skills at skillsync's
    CANONICAL source, the same target every credhome carries — a seat is born
    with exactly the fleet set, and a skill added to canonical is instantly
    visible here. Only when no canonical dir exists on this host (foreign
    machine, no MC checkout, no HELM_SKILLS_CANONICAL) fall back to mirroring the
    minting host's own CLAUDE_CONFIG_DIR skills, as before. Symlink (not
    copy) so skill edits propagate live; a stale or indirect symlink is
    normalized to the canonical target, but a REAL skills dir is never
    clobbered at mint — that estate repair (backup + move + link, superset-
    checked) is `helm skills sync`'s deliberate job, not a mint side effect.
    Best-effort: a link failure is loud (stderr) but never fatal — the seat
    still mints, exactly like the delivery-hook install."""
    from . import skillsync
    src = skillsync.canonical()
    if not os.path.isdir(src):
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude")
        src = os.path.join(base, "skills")
        if not os.path.isdir(src):
            return
    link = os.path.join(cdir, "skills")
    try:
        if os.path.islink(link):
            if os.readlink(link).rstrip(os.sep) == src.rstrip(os.sep):
                return
            os.unlink(link)
        elif os.path.exists(link):
            print("helm seat: %s/skills is a REAL dir — left untouched; "
                  "`helm skills sync --apply` folds it into the canonical "
                  "source" % cdir, file=sys.stderr)
            return
        os.symlink(src, link)
    except OSError as e:
        print("helm seat: skills not linked into %s (%s); a seat agent won't "
              "see /learn until fixed" % (cdir, e), file=sys.stderr)


# The onboarding state that, if absent, makes CC run its first-run wizard (theme
# picker, bypass-permissions accept, tips) — which STALLS a launched seat at an
# interactive prompt before it ever reaches the composer or runs SessionStart,
# so it never joins chat. Copied (not invented) from an already-onboarded config
# so lastOnboardingVersion matches the CC the host actually runs.
_ONBOARD_KEYS = ("hasCompletedOnboarding", "lastOnboardingVersion", "theme",
                 "numStartups", "tipsHistory", "bypassPermissionsModeAccepted",
                 "hasAcknowledgedCostThreshold")


def _onboarded_refs():
    """Config files to borrow onboarding flags from, best first: the minting
    host's own config (its CC version matches what a seat will run), then the
    plain ~/.claude.json."""
    refs = []
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    if base:
        refs.append(os.path.join(base, ".claude.json"))
    refs.append(os.path.join(os.path.expanduser("~"), ".claude.json"))
    return refs


def _git_toplevel(path):
    """The git root of path (read-only, best-effort) — the trust dialog keys on
    the git-root realpath, so a seat's workdir trust must name it exactly."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


def _seed_onboarding(cdir, workdir=None):
    """A fresh seat config dir triggers CC's first-run wizard, which blocks the
    seat at an interactive prompt (proven 2026-07-21: codex-2 stalled at the
    theme picker, never joined chat). Seed <cdir>/.claude.json with the
    onboarding-complete flags so a launched seat boots straight to work. Never
    clobber a seat's own state; copy the flags from an onboarded sibling (version
    match), else write a minimal complete marker. Also UNCONDITIONALLY trusts the
    seat's intended workdir (+ its git root) — the folder-trust dialog is an
    exact-match on the git-root realpath and no ref config carries helm-trust
    (root cause of the codex-3 trust stall), so it must be synthesized, not
    copied. Best-effort, non-fatal."""
    from . import pk
    dst = os.path.join(cdir, ".claude.json")
    if os.path.exists(dst):
        return                       # the seat owns its state once it exists
    seed = {"hasCompletedOnboarding": True, "theme": "dark"}
    for ref in _onboarded_refs():
        r = pk.read_json(ref, None)
        if isinstance(r, dict) and r.get("hasCompletedOnboarding"):
            for k in _ONBOARD_KEYS:
                if k in r:
                    seed[k] = r[k]
            # folder-trust is a SEPARATE per-project gate (a second wizard that
            # also stalls a fresh seat): projects[<path>].hasTrustDialogAccepted.
            # Copy just the trust flag for every worktree the ref already trusts,
            # so a seat launched in one of them skips the trust dialog too. Only
            # the trust flags — never the ref's session state/metrics.
            trusted = {}
            for path, pj in (r.get("projects") or {}).items():
                if isinstance(pj, dict) and pj.get("hasTrustDialogAccepted"):
                    trusted[path] = {"hasTrustDialogAccepted": True,
                                     "projectOnboardingSeenCount": 1}
            if trusted:
                seed["projects"] = trusted
            break
    # synthesize trust for the intended workdir (+ its git root) — exact-match
    # keys the dialog needs; helm already made the stronger bypass call.
    projects = seed.setdefault("projects", {})
    wd = os.path.realpath(workdir or os.getcwd())
    for p in {wd, _git_toplevel(wd)}:
        if p:
            projects.setdefault(os.path.realpath(p),
                                {"hasTrustDialogAccepted": True,
                                 "projectOnboardingSeenCount": 1})
    try:
        pk.write_json(dst, seed)
    except OSError as e:
        print("helm seat: onboarding not seeded for %s (%s); a launched seat "
              "may stall at the first-run wizard" % (cdir, e), file=sys.stderr)


def _seed_seat_settings(cdir):
    """CC 2.1.216 records bypass-permissions acceptance in settings.json
    (skipDangerousModePermissionPrompt), NOT .claude.json — so a launched
    --dangerously-skip-permissions seat stalls at the bypass warning without it
    (proven 2026-07-21: codex-3). Merge it (+ a theme) into the settings.json
    that hooks.install_home just wrote, preserving the delivery-lane hooks.
    Best-effort, non-fatal."""
    from . import pk
    p = os.path.join(cdir, "settings.json")
    s = pk.read_json(p, {}) or {}
    changed = False
    for k, v in (("skipDangerousModePermissionPrompt", True), ("theme", "auto")):
        if s.get(k) != v:
            s[k] = v
            changed = True
    if not changed:
        return
    try:
        pk.write_json(p, s)
    except OSError as e:
        print("helm seat: bypass/theme not seeded in %s (%s); a launched seat "
              "may stall at the bypass dialog" % (p, e), file=sys.stderr)


# The probe agent body: per-agent `model:` frontmatter is the WHOLE mixed-model
# mechanism (premise multimodel-one-cc-proven-per-agent-frontmatter-no-fork) —
# the string in `model:` goes to the wire per-request and the proxy conducts.
_PROBE_AGENT_MD = """---
name: %(name)s
description: helm multi-model probe pinned to %(model)s via frontmatter (the proven per-agent mechanism). Spawn with subagent_type %(name)s when asked to run this probe.
model: %(model)s
---
You are a helm multi-model probe subagent running as model %(model)s.
Reply with exactly the marker text given in your task prompt, then name the
model family you actually are — one line, nothing else.
"""


def probe_agents(family):
    """[(agent_name, model)] for the family's probe models — deterministic
    names (helm-probe-<model-slug>) so re-mints overwrite, never accrete."""
    fam = FAMILIES[family]
    models = fam.get("probe_models") or (fam["model"],)
    return [("helm-probe-" + re.sub(r"[^a-z0-9]+", "-", m.lower()).strip("-"), m)
            for m in models]


def _mint_probe_agents(cdir, family):
    """Mint the family's probe agents into <cdir>/agents/*.md (a
    CLAUDE_CONFIG_DIR-scoped agent set). Returns probe_agents(family)."""
    ad = os.path.join(cdir, "agents")
    os.makedirs(ad, exist_ok=True)
    probes = probe_agents(family)
    for name, model in probes:
        with open(os.path.join(ad, name + ".md"), "w") as f:
            f.write(_PROBE_AGENT_MD % {"name": name, "model": model})
    return probes


def _mint_instance_proxy(family, seat):
    """Give an INSTANCE its own proxy fate: config.yaml + token under
    instances/<seat>/, so `helm seat up <seat>` starts a proxy only this
    instance uses. Idempotent (an existing instance token is kept so a live
    launch line stays valid). The OAuth cred pool stays FAMILY-level — the
    instance config's auth-dir points at the family's auth/, so per-instance
    proxies add NO upstream quota (same account, N local listeners). Only
    proxy (OAuth) families have a pool to point at; proxy-key families bake
    their key into ONE family config and are out of scope here. -> the
    instance proxy-home dir."""
    home_dir = _proxy_home(family, seat)
    os.makedirs(home_dir, mode=0o700, exist_ok=True)
    os.chmod(home_dir, 0o700)
    fam = FAMILIES[family]
    token = _seat_token_per(home_dir)         # instance-scoped, stable
    if fam["mode"] == "proxy":
        auth_dir = os.path.join(seat_dir(family), "auth")   # the SHARED pool
        _write_private(os.path.join(home_dir, "config.yaml"),
                       _config_yaml(_instance_port(family, seat), auth_dir, token))
    return home_dir


def _seat_token_per(d):
    """Read-or-mint a proxy token in an explicit dir (instance-scoped twin of
    the family-level _seat_token)."""
    try:
        with open(os.path.join(d, "token")) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    import secrets
    tok = secrets.token_hex(32)
    _write_private(os.path.join(d, "token"), tok + "\n")
    return tok


def _write_launch_assets(family, d, room=None, seat=None, workdir=None,
                         room_source=None, multi=False):
    """The seat's isolated CLAUDE_CONFIG_DIR + the executable launch preset —
    identical for every mode, and refreshed by BOTH `add` and `launch` (a
    stale launch.sh minted before HELM_CHAT_NAME existed is why the live
    kimi seat was absent from the roster). The claude dir is born WIRED
    (G-seatlaunch-installs): the delivery lane (deliver + join + stop-guard)
    plus the beacon permit land here at creation through hooks.py's gated
    merge-preserving write — a seat must never be born deaf. Install trouble
    is loud (stderr) but never fatal: the seat still mints and the message
    names the estate-wide repair. `seat` (slice 6) mints an INSTANCE's assets
    (instances/<seat>/{claude,launch.sh}). The instance's PROXY assets
    (config.yaml/token) are minted separately by `_mint_instance_proxy` at
    launch — this function stays proxy-agnostic."""
    seat = seat or family
    cdir = os.path.join(d, "claude")
    os.makedirs(cdir, exist_ok=True)
    _link_skills(cdir)       # seat agents get the host's /learn, /premise, /afk, …
    _seed_onboarding(cdir, workdir)   # skip the onboarding/trust wizards
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
    _seed_seat_settings(cdir)   # skip the bypass-permissions dialog (settings.json)
    if multi:
        _mint_probe_agents(cdir, family)   # per-model frontmatter pins ride here
    _write_launch_sh(os.path.join(d, "launch.sh"),
                     "#!/bin/sh\n# helm seat %s — minted by `helm seat add`; "
                     "regenerate with `helm seat launch %s`\n"
                     "# child-stamp guard: inherited from a daemon born inside "
                     "a Claude session,\n# these mark the seat a subprocess "
                     "child (persistence silently OFF) — strip.\n"
                     "unset %s\n"
                     "# bearer: exported from the 0600 token file (builtin, no argv) —\n"
                     "# never an env NAME=value arg (the external env binary's argv\n"
                     "# would carry the resolved secret).\n"
                     "%s"
                     "exec %s \"$@\"\n"
                     % (seat, seat, " ".join(CHILD_STAMP_VARS),
                        _token_export(family, seat),
                        launch_line(family, room=room, seat=seat,
                                    room_source=room_source, multi=multi)))


def _write_launch_sh(path, text):
    """launch.sh lands ATOMICALLY (0700 tmp sibling + os.replace): a running
    pane's `sh` reads this script, and an O_TRUNC-in-place rewrite (the
    `_write_private` shape) lets that reader catch a truncated/half file
    mid-re-mint — the slice-6 pool-write lesson, same class. The token never
    leaves the file either way; only the write shape changes."""
    import tempfile
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".launch-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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


def _resolve_homing(explicit_room=None):
    """(room, source) for seat add/launch. CLI wins, then the inherited
    launch seam, then the current git project. Only a derived choice carries
    the marker; explicit choices clear any inherited derived provenance."""
    if explicit_room is not None:
        return explicit_room, None
    env_room, env_source = home.env_pair("CHAT_ROOM", "CHAT_ROOM_SOURCE")
    if env_room:
        source = "derived" if env_source == "derived" else None
        return env_room, source
    from . import seats
    room = seats.derive_home_room(os.getcwd())
    return room, "derived" if room else None


def _add_proxy_key(family, fam, args, room=None, room_source=None):
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
    base_url = _key_base_url(fam, api_key)
    _write_private(os.path.join(d, "config.yaml"),
                   _config_yaml_key(fam["port"], token, fam["provider"],
                                    base_url, fam["model"], api_key))
    _write_launch_assets(family, d, room, room_source=room_source)
    print("helm seat: %s seat minted at %s" % (family, d))
    print("  outbound %s key baked into config.yaml (0600 — value never "
          "printed); provider %s -> %s" % (key_env, fam["provider"], base_url))
    print("  proxy port %d; next: `helm seat up %s`, then `helm seat launch %s`"
          % (fam["port"], family, family))
    return 0


def _add(family, args, room=None, room_source=None):
    fam = FAMILIES.get(family)
    if fam is None:
        print("helm seat: family '%s' not yet wired (have: %s). First-party "
              "Anthropic-compatible families need only a FAMILIES entry with "
              "base_url + key_env — see helm/seat.py." % (family, ", ".join(sorted(FAMILIES))),
              file=sys.stderr)
        return 2
    if fam["mode"] == "proxy-key":
        return _add_proxy_key(
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
    _write_launch_assets(family, d, room, room_source=room_source)

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


def _up(family, quiet=False, seat=None):
    fam = _require_seat(family)
    if fam is None:
        return 1
    seat = seat or family
    # an instance proxy must have its own minted config before it can come up
    cfgd = _proxy_home(family, seat)
    if seat != family and not os.path.exists(os.path.join(cfgd, "config.yaml")):
        print("helm seat: no per-instance proxy for %s yet — `helm seat launch "
              "%s -i %s` mints it" % (seat, family, seat.rsplit("-", 1)[-1]),
              file=sys.stderr)
        return 1
    port = _instance_port(family, seat)
    # Serialize check→spawn→record under the per-home lock: without it two
    # concurrent _up calls both read an empty pidfile and double-start (the
    # atomic-ownership finding). The whole critical section runs under the
    # flock so the empty-check and the record-write are one atomic step.
    with _proxy_lock(family, seat):
        pid = _running_pid(family, seat)
        if pid:
            print("helm seat: %s proxy already running (pid %d, port %d) — "
                  "`helm seat down %s` first" % (seat, pid, port, seat),
                  file=sys.stderr)
            return 1
        b = _proxy_bin()
        if not b:
            print("helm seat: cli-proxy-api binary not found (HELM_PROXY_BIN, "
                  "%s, PATH all empty) — run `helm seat doctor`"
                  % PROXY_BIN_DEFAULT, file=sys.stderr)
            return 1
        # refuse a pre-bound target: an unrelated listener already on the port
        # means a collision — spawning against it would either fight for the
        # bind or, worse, _port_open would read the STRANGER as readiness (the
        # codex MED: _up returned rc=0 + wrote proxy.pid against a foreign
        # listener while its own child lost the bind). Family bases/comments
        # prove no RUNTIME ownership.
        if _port_open(port):
            print("helm seat: port %d already has a listener that is not %s's "
                  "proxy — refusing to spawn against someone else's socket "
                  "(collision). Identify it (`ss -ltnp | grep :%d`) or `helm "
                  "seat down %s` if it is a stale record."
                  % (port, seat, port, seat), file=sys.stderr)
            return 1
        log = open(os.path.join(cfgd, "proxy.log"), "ab")
        try:
            p = subprocess.Popen([b, "-config",
                                  os.path.join(cfgd, "config.yaml")],
                                 stdout=log, stderr=log, start_new_session=True)
        except OSError as exc:
            print("helm seat: proxy failed to launch: %s" % exc, file=sys.stderr)
            return 1
        finally:
            log.close()
        # record pid + BIRTH identity so `_down`/`_running_pid` signal only THIS
        # incarnation — a reused pid is never proxied-on or killed (the bare
        # reusable-PID finding). Capture right after spawn; /proc can lag a tick,
        # so retry briefly. If identity is STILL unverifiable the proxy would be
        # unmanageable (fail-closed `_down` would refuse to ever signal it) — kill
        # the orphan and fail loudly rather than leave a proxy we cannot stop.
        ident = None
        for _ in range(10):
            ident = _pid_identity(p.pid)
            if ident or p.poll() is not None:
                break
            time.sleep(0.1)
        if not ident:
            p.kill()
            print("helm seat: proxy pid %d birth identity unverifiable — killed "
                  "the orphan rather than leave an unstoppable proxy (no /proc?)"
                  % p.pid, file=sys.stderr)
            return 1
        _write_private(os.path.join(cfgd, "proxy.pid"), "%d %s\n" % (p.pid, ident))
        # readiness = OUR child alive AND the port open. Poll the child first:
        # a dead child with the port held by a late foreign listener must NOT
        # read as success (the other half of the codex MED). We already refused
        # a pre-bound port above, so an open port with a live child is ours.
        for _ in range(30):  # up to ~6s for the port to open
            if p.poll() is not None or _port_open(port):
                break
            time.sleep(0.2)
        if p.poll() is not None:
            os.remove(os.path.join(cfgd, "proxy.pid"))
            print("helm seat: proxy exited rc %s — tail %s"
                  % (p.returncode, os.path.join(cfgd, "proxy.log")), file=sys.stderr)
            return 1
        if not _port_open(port):
            # child alive but never bound (lost the race to a late listener, or
            # wedged): not a proxy we can reach — kill it, don't leave an
            # unmanageable record.
            p.kill()
            os.remove(os.path.join(cfgd, "proxy.pid"))
            print("helm seat: proxy pid %d alive but port %d never opened — "
                  "killed the orphan; tail %s"
                  % (p.pid, port, os.path.join(cfgd, "proxy.log")), file=sys.stderr)
            return 1
    if not quiet:
        print("helm seat: %s proxy up — 127.0.0.1:%d (pid %d)"
              % (seat, port, p.pid))
    return 0


def _down(family, seat=None):
    fam = _require_seat(family)
    if fam is None:
        return 1
    seat = seat or family
    pidfile = os.path.join(_proxy_home(family, seat), "proxy.pid")
    # Serialize verify→signal→unlink under the per-home lock (the atomic-
    # ownership finding): without it a concurrent _up/replacement can write a
    # FRESH pidfile after the old proxy exits, and an unconditional os.remove
    # then deletes the NEW record — leaving the new proxy alive but
    # unmanageable and eligible for a duplicate start.
    with _proxy_lock(family, seat):
        pid = _running_pid(family, seat)
        if not pid:
            # Distinguish a merely-dead proxy from a REUSED/unauthenticated pid:
            # a live process holding our recorded pid that we cannot prove is
            # ours is NOT our proxy — never signal it. Reap the stale file.
            rec = _proxy_pid_record(family, seat)
            if rec and _pid_alive(rec["pid"]):
                os.remove(pidfile)
                print("helm seat: %s proxy pidfile stale — pid %d now belongs to "
                      "an unrelated process (reused); NOT signalled, record "
                      "reaped. NOTE: this includes LEGACY bare-pid records from "
                      "pre-per-instance proxies (every proxy running at land). "
                      "The old proxy may STILL hold the port — to load the new "
                      "config, kill it by hand: `kill %d` (verify with "
                      "`ss -ltnp | grep :%d`), then `helm seat up %s`."
                      % (seat, rec["pid"], rec["pid"],
                         _instance_port(family, seat), seat))
                return 0
            if os.path.exists(pidfile):
                os.remove(pidfile)  # stale
            print("helm seat: %s proxy not running" % seat)
            return 0
        # TOCTOU guard: re-verify the birth identity IMMEDIATELY before each
        # signal. `_running_pid` verified at entry, but the proxy could die and
        # its pid be recycled in the gap before a kill; a recycled pid has a
        # different starttime, so the recheck refuses to signal it. (pidfd would
        # close the window outright; /proc starttime narrows it to the
        # check→kill instant, which is the portable floor here.)
        # Re-read the record under the lock and FAIL CLOSED if it vanished or
        # changed out from under the verified pid (a transient read failure,
        # manual pidfile removal, or a malformed replacement must never crash
        # the lifecycle on a None subscript — codex-2 advisory on 224e6b5).
        owned = _proxy_pid_record(family, seat)
        if not owned or owned["pid"] != pid or not owned["identity"] \
                or owned["identity"] == "?":
            print("helm seat: %s proxy record changed/vanished mid-down — "
                  "refusing to signal an unowned pid %d" % (seat, pid),
                  file=sys.stderr)
            return 1
        expected = owned["identity"]

        def _still_ours():
            return _pid_alive(pid) and _pid_identity(pid) == expected

        if _still_ours():
            os.kill(pid, signal.SIGTERM)
        for _ in range(15):
            if not _pid_alive(pid):
                break
            time.sleep(0.2)
        if _still_ours():
            os.kill(pid, signal.SIGKILL)
        # Unlink ONLY the exact record this operation killed: re-read under the
        # lock and remove just if the pidfile still names THIS pid+birth. A
        # concurrent replacement's fresh record (different pid or birth) is
        # left intact.
        cur = _proxy_pid_record(family, seat)
        if cur and cur["pid"] == pid and cur["identity"] == expected \
                and os.path.exists(pidfile):
            os.remove(pidfile)
    print("helm seat: %s proxy stopped (pid %d)" % (seat, pid))
    return 0


# ---------------------------------------------------------------------------
# smoke — the 4-leg acceptance gate
# ---------------------------------------------------------------------------

def _seat_env(family, config_dir, multi=False):
    """The proxied-seat subprocess env: scrubbed base (so a stray inherited
    ANTHROPIC_API_KEY can never ride along), then the seat's own proxy, chat,
    and dregg-signing identity. Mirrors launch_line so smoke cannot certify a
    materially different process shape — including the child-stamp strip.
    `multi` mirrors launch_line's --multi: NO CLAUDE_CODE_SUBAGENT_MODEL (it
    would blunt-pin every subagent over the per-agent frontmatter — the proven
    mixed-model mechanism); the scrubbed base also guarantees no inherited pin
    leaks back in."""
    fam = FAMILIES[family]
    env = scrub_env(os.environ)
    for v in CHILD_STAMP_VARS:
        env.pop(v, None)
    env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
    env.update({
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % _instance_port(family),
        "ANTHROPIC_AUTH_TOKEN": _read_token(family) or "",
        "CLAUDE_CONFIG_DIR": config_dir,
        "HELM_CHAT_NAME": family,
        "HELM_CELL_BIN": DREGG_SIGNER_DEFAULT,
        "HELM_CELL_PROFILE": family,
        "DREGG_PROFILE": family,
    })
    if not multi:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = fam["model"]
    return env


def _smoke_multi_leg(family, fam, smoke_dir, mark):
    """The mixed-model fan-out leg (the proven run-1 pattern): TWO subagents
    pinned to DIFFERENT models via per-agent frontmatter, one claude-code
    process, no CLAUDE_CODE_SUBAGENT_MODEL. Verified against a CONDUCTOR LOG —
    per-request model names on the wire, not the subagents' word: the smoke
    claude rides through an ephemeral helm modelrouter fronting this seat's
    proxy, and the leg passes only when the router's log shows BOTH probe
    models leaving the process. (The stock proxy's gin log carries no model
    names at debug:false; the router's conductor log is the helm-owned
    equivalent of the debug log run-1 read.)"""
    probes = _mint_probe_agents(smoke_dir, family)
    if len(probes) < 2:
        print("  %-8s SKIP — %s has one probe model; the mixed fan-out needs "
              "two (FAMILIES probe_models)" % ("multi", family))
        return True
    from . import modelrouter
    log_path = os.path.join(smoke_dir, "router.log")
    if os.path.exists(log_path):
        os.remove(log_path)      # stale wire evidence must never certify a run
    srv = modelrouter.start_inprocess(default_family=family, log_path=log_path)
    (a_name, a_model), (b_name, b_model) = probes[0], probes[1]
    try:
        env = _seat_env(family, smoke_dir, multi=True)
        env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:%d" % srv.server_address[1]
        prompt = ("Use the Task tool to spawn exactly two subagents in "
                  "parallel: one with subagent_type %s whose entire task is to "
                  "reply with exactly helm-seat-multi-a-%s, and one with "
                  "subagent_type %s whose entire task is to reply with exactly "
                  "helm-seat-multi-b-%s. Report both replies verbatim."
                  % (a_name, mark, b_name, mark))
        try:
            p = subprocess.run(["claude", "-p", prompt, "--model", fam["model"],
                                "--allowedTools", "Task"],
                               env=env, capture_output=True, text=True,
                               timeout=600)
            reply, note = (p.stdout or "").strip(), ""
        except subprocess.TimeoutExpired:
            p, reply, note = None, "", " (timeout 600s)"
    finally:
        srv.shutdown()
        srv.server_close()
    wired = modelrouter.logged_models(log_path)
    markers = ("helm-seat-multi-a-%s" % mark in reply
               and "helm-seat-multi-b-%s" % mark in reply)
    routed = {a_model, b_model} <= wired
    passed = p is not None and p.returncode == 0 and markers and routed
    if not passed and not note:
        note = " (rc %s%s%s)" % (
            p.returncode if p else "-",
            "" if markers else "; marker missing",
            "" if routed else "; conductor log saw %s, wanted %s+%s"
            % (sorted(wired) or "nothing", a_model, b_model))
    print("  %-8s %s%s — %s" % ("multi", "PASS" if passed else "FAIL", note,
                                (reply or "(no reply)").replace("\n", " ")[:200]))
    return passed


def _smoke(family, multi=False):
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
    if multi:
        ok = _smoke_multi_leg(family, fam, smoke_dir, mark) and ok
    print("  %-8s SKIP — whisper: not yet wired" % "whisper")
    print("helm seat: %s smoke %s (model %s)" % (family, "PASS" if ok else "FAIL", model))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# resume — the resume-at-drain-point capability (rides the metaharness seam)
# ---------------------------------------------------------------------------

def _seat_family(seat_name):
    """'codex' -> codex, 'codex-3' -> codex (slice-6 instances); unknown ->
    (None, reason). A resume must never mint a seat that was never added."""
    if seat_name in FAMILIES:
        return seat_name, None
    base, _, tail = seat_name.rpartition("-")
    if base in FAMILIES and tail.isdigit():
        return base, None
    return None, ("unknown seat '%s' (families: %s; instances: <family>-N)"
                  % (seat_name, ", ".join(sorted(FAMILIES))))


def _split_seat(seat_name):
    """'codex' -> ('codex', 'codex'), 'codex-3' -> ('codex', 'codex-3'): the
    family (which FAMILIES entry / cred pool) beside the full seat identity
    (whose proxy/config/session). Falls back to (seat_name, seat_name) so a
    bare family name is instance 1."""
    fam, _ = _seat_family(seat_name)
    fam = fam or seat_name
    return fam, seat_name


_SESSION_JSONL = re.compile(r"^[0-9a-fA-F-]{36}\.jsonl$")


def _newest_seat_session(instance_dir):
    """(session_id, cwd) of the seat's newest claude session, from its OWN
    isolated CLAUDE_CONFIG_DIR (<instance>/claude/projects/<slug>/<uuid>.jsonl);
    (None, None) when the seat never ran. The id feeds `--resume <id>`, the
    sniffed cwd re-homes the pane where the session actually worked. Only
    uuid-named files count — a sidecar must fall through to --continue, never
    resume the wrong transcript. (Every helm seat runs the `claude` binary —
    codex seats are claude-over-proxy — so claude's resume flags are universal
    here; a raw `codex resume` pane is not a helm seat.)"""
    cands = []
    for p in glob.glob(os.path.join(instance_dir, "claude", "projects",
                                    "*", "*.jsonl")):
        if not _SESSION_JSONL.match(os.path.basename(p)):
            continue
        try:
            cands.append((os.path.getmtime(p), p))
        except OSError:
            pass
    if not cands:
        return None, None
    p = max(cands)[1]
    from . import harnesses
    return os.path.basename(p)[:-len(".jsonl")], harnesses._sniff_cwd(p)


def _homing_from_launch(path):
    """The seat's room + provenance recovered from launch.sh. Shell-aware
    tokenization preserves quoted values and ignores the generated comments;
    resume must not silently turn a derived default into an explicit home."""
    try:
        with open(path) as f:
            tokens = shlex.split(f.read(), comments=True)
    except (OSError, ValueError):
        return None, None
    values = {}
    for token in tokens:
        for name in ("HELM_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE"):
            prefix = name + "="
            if token.startswith(prefix):
                values[name] = token[len(prefix):]
    source = values.get("HELM_CHAT_ROOM_SOURCE")
    return values.get("HELM_CHAT_ROOM"), \
        source if source == "derived" else None


def _room_from_launch(path):
    """Back-compatible room-only view used by the multi-resume seam."""
    return _homing_from_launch(path)[0]


def _multi_from_launch(path):
    """The seat's --multi (mixed-model fleet) shape, recovered from its current
    launch.sh — the resume re-mint must not silently strip it any more than it
    strips --room. --multi's whole effect is DROPPING the CLAUDE_CODE_SUBAGENT_MODEL
    pin (frontmatter routes per subagent); a non-multi exec line ASSIGNS that var.
    The child-stamp unset line names only CHILD_STAMP_VARS, never the pin, so the
    bare assignment substring is an unambiguous marker: present = pinned (non-multi),
    absent = multi. Without this, a --multi seat re-minted on resume regains the
    blunt pin and silently collapses back to single-model."""
    try:
        with open(path) as f:
            return "CLAUDE_CODE_SUBAGENT_MODEL=" not in f.read()
    except OSError:
        return False


def _multi_from_launch(path):
    """Recover the seat's mixed-model shape from launch.sh. --multi's durable
    marker is the ABSENCE of the blunt CLAUDE_CODE_SUBAGENT_MODEL pin; every
    single-model launch assigns it. Missing/unreadable assets default safely to
    the normal pinned shape."""
    try:
        with open(path) as f:
            return "CLAUDE_CODE_SUBAGENT_MODEL=" not in f.read()
    except OSError:
        return False


def _multi_from_launch(path):
    """Recover the seat's mixed-model shape from launch.sh. --multi's durable
    marker is the ABSENCE of the blunt CLAUDE_CODE_SUBAGENT_MODEL pin; every
    single-model launch assigns it. Missing/unreadable assets default safely to
    the normal pinned shape."""
    try:
        with open(path) as f:
            return "CLAUDE_CODE_SUBAGENT_MODEL=" not in f.read()
    except OSError:
        return False


def _multi_from_launch(path):
    """Recover the seat's mixed-model shape from launch.sh. --multi's durable
    marker is the ABSENCE of the blunt CLAUDE_CODE_SUBAGENT_MODEL pin; every
    single-model launch assigns it. Missing/unreadable assets default safely to
    the normal pinned shape."""
    try:
        with open(path) as f:
            return "CLAUDE_CODE_SUBAGENT_MODEL=" not in f.read()
    except OSError:
        return False


def _resume(seat_name, rest):
    """seat resume <seat> — relaunch the seat's pane at its drain point via
    the detected metaharness: the pane runs the seat's freshly re-minted
    launch.sh (latest env/identity/hooks) with claude's own continuity flag
    appended (--resume <id> when the seat's config dir names a session, else
    --continue), so the SESSION survives while the environment refreshes.
    TOKEN LAW: the pane command is the launch.sh PATH — the expanded launch
    line (which carries the proxy token) never crosses the adapter seam."""
    family, err = _seat_family(seat_name)
    if err:
        print("helm seat: " + err, file=sys.stderr)
        return 2
    d = _instance_dir(family, seat_name)
    launch_sh = os.path.join(d, "launch.sh")
    if not os.path.exists(launch_sh):
        print("helm seat: no %s seat minted (%s missing) — `helm seat add %s` "
              "then `helm seat launch %s` first"
              % (seat_name, launch_sh, family, seat_name), file=sys.stderr)
        return 1
    room, room_source = _homing_from_launch(launch_sh)
    multi = _multi_from_launch(launch_sh)
    sid, sess_cwd = _newest_seat_session(d)
    command = "%s %s" % (shlex.quote(launch_sh),
                         ("--resume " + shlex.quote(sid)) if sid else "--continue")
    from . import harness
    ad = harness.detect()
    if ad is None:
        print("helm seat: " + harness.RECOMMENDATION, file=sys.stderr)
        print("  manual paste (env refreshed, session kept): " + command,
              file=sys.stderr)
        return 1
    try:
        # Stop the stale pane BEFORE re-minting: its `sh` is executing THIS
        # launch.sh, and the metaharness's close is not process-synchronous —
        # a re-mint (even atomic-replace) under a still-running reader can
        # swap the script mid-read. Stop first, then refresh, then spawn.
        for row in ad.list():
            if row.get("title") == seat_name and row.get("handle"):
                ad.stop(row["handle"])
                print("  stopped stale %s pane %s" % (seat_name, row["handle"]))
        # env refresh half of the contract: the relaunch rides the LATEST
        # assets (identity vars, delivery hooks, context env), room preserved.
        _write_launch_assets(
            family, d, room, seat_name, room_source=room_source, multi=multi)
        # resume must not strand the seat on a dead proxy either (the same
        # silent-dead-seat class the fable HIGH named in _spawn): mint the
        # instance proxy (idempotent) and start it if down, so the relaunched
        # seat's 8319 line has a live proxy behind it.
        fam = FAMILIES[family]
        if seat_name != family and fam["mode"] == "proxy":
            _mint_instance_proxy(family, seat_name)
        if os.path.exists(os.path.join(_proxy_home(family, seat_name),
                                       "config.yaml")) \
                and not _running_pid(family, seat_name):
            if _up(family, quiet=True, seat=seat_name) == 0:
                print("  (proxy was down — auto-started)")
        handle = ad.spawn(command, title=seat_name,
                          cwd=sess_cwd or os.getcwd())
    except harness.HarnessError as e:
        print("helm seat: %s resume via %s failed: %s"
              % (seat_name, ad.name, e), file=sys.stderr)
        return 1
    print("helm seat: resumed %s via %s — pane %s, %s; env refreshed from %s"
          % (seat_name, ad.name, handle,
             ("session %s… (--resume)" % sid[:8]) if sid
             else "--continue (newest session)", launch_sh))
    return 0


# ---------------------------------------------------------------------------
# spawn / where — the harness-agnostic SELF-ONBOARDING seat spawn
# ---------------------------------------------------------------------------
# THE GAP this closes: a hand-spawned seat is a BARE idle pane — no beacon,
# no work, not addressable (feature without RSH = dead scaffolding). One verb,
# THREE spawn paths dispatched by harness.detect():
#   headless  no metaharness (the standalone DEFAULT — helm is the substrate,
#             orca/herdr are optional front-ends): the minted launch.sh runs
#             DETACHED (start_new_session=True IS setsid; nohup-equivalent io
#             to spawn.log), and the onboarding rides as launch.sh's
#             positional arg — launch.sh execs `claude … "$@"`, so the prompt
#             is the seat's FIRST TURN, self-run at boot. No pane to inject
#             into ⇒ deliver at launch time.
#   orca      adapter.spawn (terminal create, handle captured) + adapter.send
#             --enter of the onboarding first-prompt into the pane.
#   herdr     the same two seam calls (agent start + pane run) via the adapter.
# Common to all: mint hygiene via _write_launch_assets (child-stamp stripped ⇒
# persistence forced ON, --dangerously canonical, skills linked), DUP-NAME
# REAP first (a prior bare same-name seat is killed/closed — the exact live
# bug), and a ROSTER REGISTER (spawn.json + chat-roster mirror) so any agent
# can `helm seat where <name>` and reap. TOKEN LAW holds: the adapter seam
# carries the launch.sh PATH + the secret-free onboarding text, never the
# expanded launch line.

SPAWN_SEND_DELAY_S = 5   # pane-boot grace before the onboarding keystrokes
                         # (HELM_SPAWN_SEND_DELAY overrides; tests set 0)


def onboarding_prompt(seat_name, room=None):
    """The seat's self-onboarding FIRST PROMPT — identical across all three
    spawn paths (only the delivery differs). One line, no newlines (it rides
    `terminal send`/`pane run` as a single keystroke burst) and no secrets
    (it crosses the adapter seam). Content law: arm the beacon FIRST (the only
    idle wake), read the home room, announce, take @<seat> work."""
    r = room or "main"
    flag = "" if r == "main" else " --room %s" % shlex.quote(r)
    return ("You are helm fleet seat '%(s)s' (home room %(r)s). Self-onboard "
            "now, in order: (1) ARM YOUR INBOX BEACON before anything else — "
            "Monitor(command: \"helm chat wait --seat %(s)s --follow\", "
            "persistent: true). Monitor NOT in your tool surface? It is "
            "DEFERRED, not absent — load it with ToolSearch(query: "
            "\"select:Monitor\"), then arm it. Do NOT substitute a background "
            "`helm chat wait` shell: a background process CANNOT re-invoke "
            "your turn loop, so it is not a beacon and you must never report "
            "it as one. Nothing external can wake an idle seat, so the beacon "
            "is mandatory — say so plainly if you could not arm it. "
            "(2) CATCH UP: "
            "`helm chat read%(f)s` — read the room before acting. (3) "
            "ANNOUNCE: `helm chat post%(f)s \"%(s)s online — beacon armed, "
            "taking @%(s)s work\"`. (4) TAKE WORK: rows addressed @%(s)s and "
            "owner posts are yours — do the work, reply in the room, and when "
            "idle again stay parked on the beacon."
            % {"s": seat_name, "r": r, "f": flag})


def _spawn_path(d):
    return os.path.join(d, "spawn.json")


def _spawn_record(d):
    from . import pk
    rec = pk.read_json(_spawn_path(d), None)
    return rec if isinstance(rec, dict) else None


def _reap_stale(seat_name, d, ad):
    """Dup-name reap, BOTH legs. Returns (notes, errors): a replacement must
    never launch after a known same-name process failed to close. Recorded
    headless pids are birth-identity checked so pid reuse cannot kill an
    unrelated process."""
    notes, errors = [], []
    rec = _spawn_record(d)
    pid = (rec or {}).get("pid")
    if (rec or {}).get("harness") == "headless" and pid:
        live = _recorded_pid_alive(rec)
        if live is None:
            errors.append("stale headless %s pid %s is live but its process "
                          "identity is unverifiable; refusing to kill it"
                          % (seat_name, pid))
        elif live:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(15):
                    if _recorded_pid_alive(rec) is not True:
                        break
                    time.sleep(0.2)
                if _recorded_pid_alive(rec) is True:
                    os.kill(pid, signal.SIGKILL)
                    for _ in range(10):
                        if _recorded_pid_alive(rec) is not True:
                            break
                        time.sleep(0.1)
                if _recorded_pid_alive(rec) is True:
                    errors.append("stale headless %s pid %s survived SIGKILL"
                                  % (seat_name, pid))
                else:
                    notes.append("reaped stale headless %s (pid %d)"
                                 % (seat_name, pid))
            except OSError as e:
                errors.append("stale headless %s pid %s NOT reaped (%s)"
                              % (seat_name, pid, e))
    adapters = [ad] if ad is not None else []
    recorded_harness = (rec or {}).get("harness")
    if recorded_harness not in (None, "headless") and \
            all(a.name != recorded_harness for a in adapters):
        from . import harness
        cls = harness.ADAPTERS.get(recorded_harness)
        path = shutil.which(cls.bin) if cls else None
        if path:
            adapters.append(cls(path))
        else:
            errors.append("recorded %s pane %s cannot be checked or reaped: "
                          "the %s CLI is unavailable"
                          % (recorded_harness, (rec or {}).get("handle"),
                             recorded_harness))
    for adapter in adapters:
        try:
            rows = adapter.list()
        except Exception as e:
            errors.append("%s pane scan failed (%s)" % (adapter.name, e))
            rows = []
        for row in rows:
            handle = row.get("handle")
            recorded = adapter.name == recorded_harness and \
                handle == (rec or {}).get("handle")
            if not handle or (row.get("title") != seat_name and not recorded):
                continue
            try:
                adapter.stop(handle)
                notes.append("reaped stale %s pane %s"
                             % (seat_name, row["handle"]))
            except Exception as e:
                errors.append("stale %s pane %s NOT reaped (%s)"
                              % (seat_name, row["handle"], e))
    return notes, errors


def _headless_spawn(launch_sh, onboarding, cwd, log_path):
    """The standalone path: launch.sh detached (start_new_session=True = its
    own setsid session — survives this CLI and any parent pane), stdin from
    /dev/null, stdout+stderr appended to spawn.log (the nohup shape). The
    onboarding is launch.sh's POSITIONAL ARG: the script execs
    `claude … "$@"`, so the prompt lands as the seat's first turn at boot —
    the launch-time delivery, since headless has no pane to inject into."""
    with open(log_path, "ab") as log, open(os.devnull, "rb") as devnull:
        p = subprocess.Popen([launch_sh, onboarding], cwd=cwd, stdin=devnull,
                             stdout=log, stderr=log, start_new_session=True)
    return p.pid


def _register_spawn(seat_name, d, rec):
    """Write the authoritative spawn.json, then its best-effort roster mirror.
    False means the spawn must be torn back down: an unregistered headless
    process cannot be found safely for the next duplicate-name reap."""
    from . import pk
    try:
        pk.write_json(_spawn_path(d), rec)
    except OSError as e:
        print("helm seat: spawn register write failed (%s): %s"
              % (_spawn_path(d), e), file=sys.stderr)
        return False
    try:
        from . import seats as _seats
        _seats.write_roster(seat_name, cwd=rec.get("worktree"),
                            home_room=rec.get("room"))
    except Exception as e:
        print("helm seat: chat-roster mirror skipped (%s) — spawn.json is "
              "still authoritative for `helm seat where`" % e, file=sys.stderr)
    return True


def _spawn_plan(seat_name, d, launch_sh, room, cwd, onboard, ad):
    """--print/--dry-run: the exact per-harness calls, nothing spawned,
    reaped, or re-minted."""
    print("helm seat spawn %s — plan (--print: nothing spawned, reaped, or "
          "re-minted):" % seat_name)
    rec = _spawn_record(d)
    would = []
    if (rec or {}).get("harness") == "headless" and rec.get("pid"):
        live = _recorded_pid_alive(rec)
        if live:
            would.append("kill stale headless pid %d (SIGTERM, then SIGKILL)"
                         % rec["pid"])
        elif live is None:
            would.append("REFUSE live headless pid %d: process identity "
                         "unverifiable" % rec["pid"])
    if ad is not None:
        try:
            would += ["%s stop pane %s" % (ad.name, r["handle"])
                      for r in ad.list()
                      if r.get("title") == seat_name and r.get("handle")]
        except Exception as e:
            would.append("(pane scan failed: %s)" % e)
    print("  reap:  " + ("; ".join(would) or "none (no stale same-name seat)"))
    print("  mint:  refresh %s (child-stamp stripped => persistence ON, "
          "--dangerously canonical, skills linked, hooks wired)" % launch_sh)
    q = shlex.quote(launch_sh)
    if ad is None:
        print("  harness: headless (no metaharness detected — the standalone "
              "default)")
        print("  spawn: detached setsid: %s '<onboarding>'  "
              "(stdin /dev/null, log %s)" % (q, os.path.join(d, "spawn.log")))
        print("  onboard: delivered AT LAUNCH as the claude first-prompt "
              "positional arg")
    else:
        print("  harness: " + ad.name)
        print("  spawn: %s.spawn(command=%s, title=%s, cwd=%s) -> <handle>"
              % (ad.name, q, seat_name, cwd))
        print("  onboard: %s.send(<handle>, <onboarding>, enter=True)"
              % ad.name)
    print("  register: %s {harness, %s, worktree=%s, room=%s}"
          % (_spawn_path(d), "pid" if ad is None else "handle", cwd,
             room or "main"))
    print("  onboarding first-prompt:\n    " + onboard)
    return 0


def _spawn_args(rest):
    """Parse spawn's small option surface without letting a missing value raise
    IndexError or an unknown flag silently change the launch."""
    room, cwd, dry_run = None, os.getcwd(), False
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--print", "--dry-run"):
            dry_run = True
            i += 1
            continue
        if arg not in ("--room", "--cwd"):
            return None, "unknown option %s" % arg
        if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
            return None, "%s wants a value" % arg
        value = rest[i + 1]
        if arg == "--room":
            room = value
        else:
            cwd = os.path.abspath(os.path.expanduser(value))
        i += 2
    return (room, cwd, dry_run), None


def _spawn(seat_name, rest, _locked=False):
    """seat spawn <seat> [--room R] [--cwd DIR] [--print] — see the section
    comment above for the three paths + the common laws. Live replacements are
    serialized per seat so concurrent callers cannot both reap an empty slot,
    launch duplicates, and race the one spawn.json register."""
    family, err = _seat_family(seat_name)
    if err:
        print("helm seat: " + err, file=sys.stderr)
        return 2
    # Instance-spawn gate (the fable MED — it lived only on `launch`, so
    # `spawn kimi-2` / `spawn codex-1` minted launch lines pointed at a SIBLING
    # family's port range). Per-instance proxies are a proxy-family feature;
    # and the numeric suffix must be N>=2 — `-1` maps onto the family itself
    # and base+1 collides with the adjacent family's base port (codex-1 -> 8318
    # = kimi's base). Refuse both before any asset is minted.
    if seat_name != family:
        fam = FAMILIES[family]
        m = re.match(r"^%s-(\d+)$" % re.escape(family), seat_name)
        if fam["mode"] != "proxy":
            print("helm seat: per-instance proxies need an OAuth-pool family "
                  "(mode=proxy); %s is mode=%s — only the family seat `%s` is "
                  "supported" % (family, fam["mode"], family), file=sys.stderr)
            return 2
        if m and int(m.group(1)) < 2:
            print("helm seat: %s is not a distinct instance — instance 1 IS the "
                  "family seat `%s` (and base+1 would collide with a sibling "
                  "family's port); use `%s` or instance N>=2"
                  % (seat_name, family, family), file=sys.stderr)
            return 2
    d = _instance_dir(family, seat_name)
    launch_sh = os.path.join(d, "launch.sh")
    if not os.path.exists(launch_sh) and \
            not os.path.exists(os.path.join(seat_dir(family), "config.yaml")):
        print("helm seat: no %s seat minted — `helm seat add %s` first, then "
              "`helm seat spawn %s`" % (seat_name, family, seat_name),
              file=sys.stderr)
        return 1
    parsed, arg_err = _spawn_args(rest)
    if arg_err:
        print("helm seat: %s; usage: helm seat spawn <seat> [--room R] "
              "[--cwd DIR] [--print]" % arg_err, file=sys.stderr)
        return 2
    room, cwd, dry_run = parsed
    room = room if room is not None else _room_from_launch(launch_sh)
    multi = _multi_from_launch(launch_sh)
    onboard = onboarding_prompt(seat_name, room)
    from . import harness
    ad = harness.detect()
    if dry_run:
        return _spawn_plan(seat_name, d, launch_sh, room, cwd, onboard, ad)
    if not _locked:
        import fcntl
        os.makedirs(d, mode=0o700, exist_ok=True)
        with open(os.path.join(d, ".spawn.lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return _spawn(seat_name, rest, _locked=True)
    notes, reap_errors = _reap_stale(seat_name, d, ad)
    for note in notes:
        print("  " + note)
    if reap_errors:
        for error in reap_errors:
            print("helm seat: " + error, file=sys.stderr)
        print("helm seat: replacement aborted; resolve the stale same-name seat "
              "before retrying", file=sys.stderr)
        return 1
    # mint hygiene AFTER the reap (a stale pane's sh may still be reading the
    # old launch.sh — the resume-verb ordering law), workdir=cwd so the trust
    # seed covers where the seat will actually run.
    _write_launch_assets(family, d, room, seat_name, workdir=cwd, multi=multi)
    # per-instance proxy fate: an INSTANCE seat owns its OWN proxy
    # (instances/<seat>/), so spawn mints + starts THAT seat's proxy — never
    # the family's. MINT FIRST (the fable HIGH): a never-launched instance has
    # no config yet, and gating on its existence silently skipped BOTH the
    # auto-start AND the WARN — a spawn-first codex-2 launched DEAD (launch.sh
    # pointed at 8319, empty token, no proxy) where pre-lane it WORKED on the
    # shared family proxy. `_mint_instance_proxy` is idempotent, so minting
    # here mirrors `launch` and is a no-op for an already-minted instance.
    fam = FAMILIES[family]
    if seat_name != family and fam["mode"] == "proxy":
        _mint_instance_proxy(family, seat_name)
    seat_cfg = os.path.join(_proxy_home(family, seat_name), "config.yaml")
    if os.path.exists(seat_cfg) and not _running_pid(family, seat_name):
        if _up(family, quiet=True, seat=seat_name) == 0:
            print("  (proxy was down — auto-started)")
        else:
            print("helm seat: WARN — %s proxy not running and auto-start "
                  "failed; the seat errors until `helm seat up %s`"
                  % (seat_name, seat_name), file=sys.stderr)
    from . import pk
    rec = {"v": 1, "seat": seat_name, "worktree": cwd, "room": room or "main",
           "launch_sh": launch_sh, "ts": pk.now_ts()}
    if ad is None:
        try:
            pid = _headless_spawn(launch_sh, onboard, cwd,
                                  os.path.join(d, "spawn.log"))
        except OSError as e:
            print("helm seat: headless spawn failed: %s" % e, file=sys.stderr)
            return 1
        rec.update(harness="headless", pid=pid,
                   pid_identity=_pid_identity(pid))
        if not _register_spawn(seat_name, d, rec):
            try:
                if rec["pid_identity"] is None or \
                        _recorded_pid_alive(rec) is True:
                    os.kill(pid, signal.SIGTERM)
            except OSError as e:
                print("helm seat: WARNING — unregistered headless pid %d could "
                      "not be stopped: %s" % (pid, e), file=sys.stderr)
            return 1
        print("helm seat: spawned %s HEADLESS (pid %d, detached; log %s) — "
              "onboarding rides as its first prompt (beacon-arm + @%s work); "
              "`helm seat where %s` resolves it"
              % (seat_name, pid, os.path.join(d, "spawn.log"), seat_name,
                 seat_name))
        return 0
    handle = None
    try:
        handle = ad.spawn(shlex.quote(launch_sh), title=seat_name, cwd=cwd)
        try:
            delay = float(home.env("SPAWN_SEND_DELAY", SPAWN_SEND_DELAY_S))
        except (TypeError, ValueError):
            delay = SPAWN_SEND_DELAY_S
        if delay > 0:            # let claude reach its composer before the
            time.sleep(delay)    # onboarding keystrokes land
        ad.send(handle, onboard, enter=True)
    except harness.HarnessError as e:
        cleanup = ""
        if handle:
            try:
                ad.stop(handle)
                cleanup = "; incomplete pane %s closed" % handle
            except harness.HarnessError as stop_err:
                cleanup = "; WARNING incomplete pane %s not closed: %s" \
                    % (handle, stop_err)
        print("helm seat: %s spawn via %s failed: %s%s"
              % (seat_name, ad.name, e, cleanup), file=sys.stderr)
        return 1
    rec.update(harness=ad.name, handle=handle)
    if not _register_spawn(seat_name, d, rec):
        try:
            ad.stop(handle)
        except harness.HarnessError as e:
            print("helm seat: WARNING — unregistered pane %s could not be "
                  "closed: %s" % (handle, e), file=sys.stderr)
        return 1
    print("helm seat: spawned %s via %s — pane %s; onboarding sent "
          "(beacon-arm + @%s work); `helm seat where %s` resolves it"
          % (seat_name, ad.name, handle, seat_name, seat_name))
    return 0


def _where(seat_name, rest):
    """seat where <seat> — resolve the spawn register: harness, handle/pid,
    worktree, room, and a liveness probe (headless: the pid; pane: the handle
    still listed by the SAME detected metaharness). The record is what a
    reaper needs; `helm seat spawn <seat>` reaps-then-replaces it."""
    if any(arg != "--json" for arg in rest) or rest.count("--json") > 1:
        print("usage: helm seat where <seat> [--json]", file=sys.stderr)
        return 2
    family, err = _seat_family(seat_name)
    if err:
        print("helm seat: " + err, file=sys.stderr)
        return 2
    d = _instance_dir(family, seat_name)
    rec = _spawn_record(d)
    if rec is None:
        print("helm seat: no spawn record for %s (%s missing) — `helm seat "
              "spawn %s` registers one" % (seat_name, _spawn_path(d),
                                           seat_name), file=sys.stderr)
        return 1
    alive = None
    if rec.get("harness") == "headless":
        alive = _recorded_pid_alive(rec)
    else:
        from . import harness
        ad = harness.detect()
        if ad is not None and ad.name == rec.get("harness"):
            try:
                alive = any(r.get("handle") == rec.get("handle")
                            for r in ad.list())
            except harness.HarnessError:
                alive = None    # adapter trouble: report, never guess
    if "--json" in rest:
        print(json.dumps(dict(rec, alive=alive), indent=2, sort_keys=True))
        return 0
    ref = ("pid %s" % rec.get("pid")) if rec.get("harness") == "headless" \
        else ("handle %s" % rec.get("handle"))
    state = {True: "LIVE", False: "GONE (helm seat spawn %s respawns)"
             % seat_name}.get(alive, "unverified (metaharness %r not "
                              "detected here)" % rec.get("harness"))
    print("%s: %s %s — %s; worktree %s, room %s, spawned %s"
          % (seat_name, rec.get("harness"), ref, state, rec.get("worktree"),
             rec.get("room"), rec.get("ts")))
    return 0


# ---------------------------------------------------------------------------
# list / status / doctor
# ---------------------------------------------------------------------------

def _minted_instances(family):
    """[seat] every instance with its OWN minted proxy (config.yaml under
    instances/<seat>/) — the per-instance-proxy fleet, sorted numerically so
    codex-2 precedes codex-10."""
    root = os.path.join(seat_dir(family), "instances")
    out = []
    for name in (os.listdir(root) if os.path.isdir(root) else []):
        if os.path.exists(os.path.join(root, name, "config.yaml")):
            out.append(name)
    def _key(s):
        m = re.match(r"^%s-(\d+)$" % re.escape(family), s)
        return (0, int(m.group(1))) if m else (1, s)
    return sorted(out, key=_key)


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
    port = _instance_port(family)
    live = "proxy UP pid %d port %d%s" % (pid, port, "" if _port_open(port) else
                                          " (port not answering!)") if pid \
        else "proxy down"
    row = "%-8s %-38s %s" % (family, live, cred)
    # per-instance proxies: each minted instance reports its OWN proxy fate
    for inst in _minted_instances(family):
        ipid = _running_pid(family, inst)
        iport = _instance_port(family, inst)
        ilive = "proxy UP pid %d port %d%s" % (
            ipid, iport, "" if _port_open(iport) else " (port not answering!)") \
            if ipid else "proxy down"
        row += "\n  %-6s %-38s" % (inst, ilive)
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
    try:      # proxy-seat context% + autocompact latch — read-only visibility
        from . import autocompact
        for ln in autocompact.report_lines():
            print(ln)
    except Exception as e:
        print("autocompact: report unavailable (%s)" % e)
    return 0 if b and c and not err else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_seat(args):
    """seat add|up|down|launch|spawn|where|resume|smoke|list|status|doctor —
    multimodel seats."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb in ("list", "status"):
        return _status(rest)
    if verb == "doctor":
        return _doctor(rest)
    if verb == "autocompact":
        from . import autocompact
        return autocompact.cmd_autocompact(rest)
    if verb == "spawn":
        if not rest:
            print("usage: helm seat spawn <seat> [--room R] [--cwd DIR] "
                  "[--print]", file=sys.stderr)
            return 2
        return _spawn(rest[0], rest[1:])
    if verb == "where":
        if not rest:
            print("usage: helm seat where <seat> [--json]", file=sys.stderr)
            return 2
        return _where(rest[0], rest[1:])
    if verb == "resume":
        if not rest:
            print("usage: helm seat resume <seat>", file=sys.stderr)
            return 2
        return _resume(rest[0], rest[1:])
    if verb in ("add", "up", "down", "launch", "smoke"):
        if not rest:
            print("usage: helm seat %s <family>" % verb, file=sys.stderr)
            return 2
        family = rest[0]
        # --multi (launch/smoke): the mixed-model fleet shape — no subagent
        # pin, probe agents minted, smoke grows the fan-out leg.
        multi = "--multi" in rest
        # add/launch are self-contained presets: explicit --room wins, then
        # inherited launch homing, then the current git project's default.
        explicit_room = None
        if "--room" in rest:
            try:
                explicit_room = rest[rest.index("--room") + 1]
            except IndexError:
                print("helm seat: --room wants a value", file=sys.stderr)
                return 2
        room, room_source = (None, None)
        if verb in ("add", "launch"):
            room, room_source = _resolve_homing(explicit_room)
        if verb == "add":
            return _add(
                family, rest[1:], room=room, room_source=room_source)
        if verb in ("up", "down"):
            # `helm seat up codex-3` targets instance codex-3's OWN proxy;
            # `helm seat up codex` targets the family (instance-1) proxy.
            fam_name, seat_name = _split_seat(family)
            fn = _up if verb == "up" else _down
            return fn(fam_name, seat=seat_name)
        if verb == "smoke":
            return _smoke(family, multi=multi)
        fam = _require_seat(family)
        if fam is None:
            return 1
        model = rest[rest.index("--model") + 1] if "--model" in rest else None
        # slice 6 — N instances of one family share the OAuth cred POOL but each
        # gets its own proxy fate: -i/--instance N -> seat codex-N on its own
        # port (default 1 = the family proxy, today's exact line).
        inst = 1
        for flag in ("-i", "--instance"):
            if flag in rest:
                try:
                    inst = int(rest[rest.index(flag) + 1])
                except (ValueError, IndexError):
                    print("helm seat: %s wants an integer" % flag, file=sys.stderr)
                    return 2
        seat = family if inst <= 1 else "%s-%d" % (family, inst)
        # Per-instance proxies are a PROXY-family (OAuth-pool) feature only.
        # proxy-key families (kimi) bake ONE key into the family config — there
        # is no pool to point an instance config at, so `_mint_instance_proxy`
        # skips config generation and `up <instance>` must refuse. Accepting
        # `launch kimi -i 2` would print an instance line whose proxy can never
        # come up (and its derived port can collide with a sibling family's
        # block). Refuse up front: this is an unsupported-family gate, not a
        # warn — the launch cannot produce a working instance.
        if inst > 1 and fam["mode"] != "proxy":
            print("helm seat: per-instance proxies need an OAuth-pool family "
                  "(mode=proxy); %s is mode=%s — only `helm seat launch %s` "
                  "(instance 1) is supported"
                  % (family, fam["mode"], family), file=sys.stderr)
            return 2
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
        _write_launch_assets(
            family, _instance_dir(family, seat), room, seat,
            room_source=room_source, multi=multi)
        if seat != family:
            # per-instance proxies: this instance gets its OWN port/config/
            # token/log, so one instance's restart/429-stall never takes a
            # sibling down (the shared-8317 blast-radius). OAuth pool stays
            # family-level — no quota multiplication.
            _mint_instance_proxy(family, seat)
            print("helm seat: %s gets its own proxy — `helm seat up %s` "
                  "(127.0.0.1:%d) before launching"
                  % (seat, seat, _instance_port(family, seat)), file=sys.stderr)
        # the pasteable line: export the bearer from its 0600 file (builtin, no
        # argv), then the env/claude command — the token never transits argv.
        print(_token_export(family, seat) + launch_line(
            family, model, room, seat, room_source=room_source, multi=multi))
        from . import hooks
        hooks.surface_uncovered(out=sys.stderr)  # a running joined-late pane
        return 0                                 # still needs its relaunch
    print("helm seat: unknown verb '%s'" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
