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

# Presets as data (the addendum's table). v1 wires "codex"; first-party
# Anthropic-compatible families later need only mode+base_url+key_env — no
# proxy, e.g.:
#   "kimi":     {"port": 8318, "model": "kimi-k3", "mode": "first-party",
#                "base_url": "https://api.moonshot.ai/anthropic",
#                "key_env": "MOONSHOT_API_KEY"},
#   "glm" / "deepseek": same shape.
FAMILIES = {
    "codex": {"port": 8317, "model": "gpt-5.6-sol", "mode": "proxy"},
}

_USAGE = """usage: helm seat <verb> [args]
  add <family> [--auth-from <path>]   mint the seat (translate cred read-only)
  up <family> | down <family>         start/stop the seat's local proxy
  launch <family> [--model M]         print the exact launch line (never runs it)
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
    access_token exp claim (RFC3339)."""
    try:
        with open(src_path) as f:
            a = json.load(f)
    except (OSError, ValueError) as exc:
        return None, None, "unreadable auth.json %s (%s)" % (src_path, exc)
    t = a.get("tokens") or {}
    idc = _jwt_claims(t.get("id_token"))
    exp = _jwt_claims(t.get("access_token")).get("exp")
    if not isinstance(exp, (int, float)):
        return None, None, "no exp claim in access_token (%s)" % src_path
    email = idc.get("email") or "unknown"
    plan = (idc.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type") or "unknown"
    rec = {
        "id_token": t.get("id_token"),
        "access_token": t.get("access_token"),
        "refresh_token": t.get("refresh_token"),
        "account_id": t.get("account_id"),
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


def launch_line(family, model=None):
    """The exact seat launch command. env -u ANTHROPIC_API_KEY is part of the
    line: an inherited key must never ride into a proxied seat either."""
    fam = FAMILIES[family]
    model = model or fam["model"]
    cfgdir = shlex.quote(os.path.join(seat_dir(family), "claude"))
    return ("env -u ANTHROPIC_API_KEY"
            " ANTHROPIC_BASE_URL=http://127.0.0.1:%d"
            " ANTHROPIC_AUTH_TOKEN=%s"
            " CLAUDE_CODE_SUBAGENT_MODEL=%s"
            " CLAUDE_CONFIG_DIR=%s"
            " claude --model %s"
            % (fam["port"], _read_token(family) or "<seat-token-missing>",
               model, cfgdir, model))


def _add(family, args):
    fam = FAMILIES.get(family)
    if fam is None:
        print("helm seat: family '%s' not yet wired (have: %s). First-party "
              "Anthropic-compatible families need only a FAMILIES entry with "
              "base_url + key_env — see helm/seat.py." % (family, ", ".join(sorted(FAMILIES))),
              file=sys.stderr)
        return 2
    if fam["mode"] != "proxy":
        print("helm seat: family '%s' is first-party — proxyless add not yet "
              "implemented" % family, file=sys.stderr)
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
    for stale in glob.glob(os.path.join(auth_dir, "codex-*.json")):
        os.remove(stale)  # one cred per seat; re-add refreshes it
    _write_private(os.path.join(auth_dir, fname),
                   json.dumps(rec, indent=2, sort_keys=False) + "\n")
    token = _read_token(family)
    if not token:  # stable across re-adds so a minted launch line stays valid
        import secrets
        token = secrets.token_hex(32)
        _write_private(os.path.join(d, "token"), token + "\n")
    _write_private(os.path.join(d, "config.yaml"),
                   _config_yaml(fam["port"], auth_dir, token))
    os.makedirs(os.path.join(d, "claude"), exist_ok=True)
    _write_private(os.path.join(d, "launch.sh"),
                   "#!/bin/sh\n# helm seat %s — minted by `helm seat add`; "
                   "regenerate with `helm seat launch %s`\nexec %s \"$@\"\n"
                   % (family, family, launch_line(family)), mode=0o700)

    print("helm seat: %s seat minted at %s" % (family, d))
    print("  cred %s (%s) from %s (read-only), access token valid until %s"
          % (rec["email"], fname.rsplit("-", 1)[1][:-5], src, rec["expired"]))
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
    ANTHROPIC_API_KEY can never ride along), then the seat's own triple."""
    fam = FAMILIES[family]
    env = scrub_env(os.environ)
    env.update({
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % fam["port"],
        "ANTHROPIC_AUTH_TOKEN": _read_token(family) or "",
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDE_CODE_SUBAGENT_MODEL": fam["model"],
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
        cmd = ["claude", "-p", "--model", model]
        if allowed:
            cmd += ["--allowedTools", allowed]
        cmd.append(prompt)
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
    creds = glob.glob(os.path.join(d, "auth", "*.json"))
    cred = "no cred"
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
    pid = _running_pid(family)
    port = fam.get("port")
    live = "proxy UP pid %d port %d%s" % (pid, port, "" if _port_open(port) else
                                          " (port not answering!)") if pid \
        else "proxy down"
    return "%-8s %-38s %s" % (family, live, cred)


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
        if verb == "add":
            return _add(family, rest[1:])
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
        print(launch_line(family, model))
        return 0
    print("helm seat: unknown verb '%s'" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
