"""Canonical paths, records, and process primitives for :mod:`helm.seat`."""
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time

from . import home

# ---------------------------------------------------------------------------
# paths + small primitives
# ---------------------------------------------------------------------------

def seats_root():
    return os.path.join(home.global_dir(), "seats")


def seat_dir(family):
    return os.path.join(seats_root(), family)


# A PROXY THAT IS UP CAN STILL BE WRONG, and "UP" was the only thing we reported.
#
# WHY THIS EXISTS: a long-running proxy holds the code AND config it started
# with, so a landed fix is inert for it until it restarts —
# `beacon-holds-the-code-it-armed-with`, one layer down. Measured 2026-07-25:
# ds4pro had been up 2d09h on a pre-Jul-23 binary while the
# transient-error-cooldown fix ("seat: emit
# transient-error-cooldown-seconds 5 in every proxy config") sat in its config
# unread. That is a real and checkable drift, and nothing reported it.
#
# NO CAUSAL CLAIM AT ALL. This comment previously held a two-subclass hypothesis
# — "ds4pro's drops cleared on restart and MAY be stale-proxy" — and it is dead.
# Found still standing here by seafan-claude in post-land review, after I had
# retracted the same claim in three other places and missed this one.
#
# WHY IT IS DEAD: ds4pro was restarted at 17:22, confirmed fresh, and STILL
# empty-200s every detect case (01-07, all completion_tokens at the cap); only
# its CLAIM case answered. So the earlier restart never fixed anything — it
# cleared exactly the cases that would have succeeded anyway, because
# claim-shaped prompts answer even on a bad path. That is the identical confound
# that produced the original "age is the variable" claim from kimi's 06/08/09,
# made a second time an hour later against a different seat.
#
# THE CURRENT PICTURE, stated at MODERATE confidence because this class has now
# been reframed five times and each framing was a competent bisection superseded
# by one more counterexample: ONE class, split by MODEL FAMILY. kimi-k3 and
# deepseek-v4-pro drop the open-ended defect-hunt at a high, non-total,
# NONDETERMINISTIC rate; codex, gemini and grok complete it. Unaffected by proxy
# freshness. Unaffected by max_tokens (cap 1200 gives ct 1200 empty, cap 4000
# gives ct 4000 empty — the reasoning expands to fill any budget and never
# converges).
#
# So this warning promises NOTHING about completion drops, in either direction.
# It states only the fact identity can prove: the running process has not loaded
# a changed launch input.
#
# `helm seat list` said "proxy UP pid N port P" and had no way to say "up, on
# code it no longer matches". Same missing-surface class as the dispatch `kind`
# field: the instrument reported liveness because liveness was what it could
# cheaply see, and the question that mattered was never asked.
_PROXY_LAUNCH_RECORD_V = 2
_PROXY_RECORD_UNSET = object()
PROXY_CURRENT = 0
PROXY_STALE = 1
PROXY_UNKNOWN = 2

# The CREDENTIAL trio's sibling to PROXY_* above, same law and same shape:
# A CHECK THAT CANNOT SEE A CASE MUST RETURN UNKNOWN, NEVER A VERDICT.
# `proxy_drift` already encodes it for launch inputs and vcs.ancestry /
# vcs.marker_state for the git substrate; credential reads were the one health
# axis still folding "I could not read it" into a finding ABOUT the credential.
# Ordered like PROXY_*: the good state is falsy, every problem state truthy.
#
# WHY FOUR AND NOT THREE. CRED_EXPIRED and CRED_ABSENT are both REAL negatives
# and both actionable, but by different remedies (refresh/re-login vs pool a
# cred), and the surface has always distinguished them — collapsing them to buy
# a tidier trio would regress a true reading to fix a false one. UNKNOWN is the
# one that was missing.
CRED_VALID = 0
CRED_EXPIRED = 1
CRED_ABSENT = 2
CRED_UNKNOWN = 3
_BINARY_IDENTITY_TYPES = {
    "source": str, "path": str, "dev": int, "ino": int,
    "size": int, "mtime_ns": int,
}


def _config_digest(path):
    """Content identity: an identical remint is still the SAME loaded config."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_identity(path):
    """Invocation + deployed-target identity without hashing a large executable."""
    source = os.path.abspath(path)
    resolved = os.path.realpath(source)
    st = os.stat(resolved)
    return {"source": source, "path": resolved, "dev": st.st_dev,
            "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _valid_binary_identity(binary):
    return isinstance(binary, dict) and all(
        type(binary.get(key)) is value_type
        for key, value_type in _BINARY_IDENTITY_TYPES.items())


def _proxy_launch_inputs(config_path, binary_path):
    return {"v": _PROXY_LAUNCH_RECORD_V,
            "config_sha256": _config_digest(config_path),
            "binary": _binary_identity(binary_path)}


def _encode_launch_inputs(inputs):
    raw = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_launch_inputs(token):
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        rec = json.loads(raw)
        if rec.get("v") != _PROXY_LAUNCH_RECORD_V \
                or not isinstance(rec.get("config_sha256"), str) \
                or not _valid_binary_identity(rec.get("binary")):
            return None
        return rec
    except (ValueError, TypeError, AttributeError):
        return None


def proxy_drift(family, seat=None, record=_PROXY_RECORD_UNSET):
    """Return (PROXY_CURRENT|PROXY_STALE|PROXY_UNKNOWN, detail).

    UNKNOWABLE IS NOT FRESH. Required config or recorded binary read failures,
    and legacy pid records that predate launch-input capture, return the truthy
    PROXY_UNKNOWN state rather than silently certifying a process as current.
    A down proxy is current because no running process can hold stale inputs."""
    rec = _running_pid_rec(family, seat) if record is _PROXY_RECORD_UNSET else record
    if not rec:
        return PROXY_CURRENT, None
    launch = rec.get("launch")
    if not launch:
        return PROXY_UNKNOWN, \
            "launch inputs were not recorded; restart once to capture them"
    if not isinstance(launch, dict) \
            or not isinstance(launch.get("config_sha256"), str) \
            or not _valid_binary_identity(launch.get("binary")):
        return PROXY_UNKNOWN, "recorded proxy launch inputs are malformed"
    seat_name = seat or family
    cfg = os.path.join(_proxy_home(family, seat), "config.yaml")
    try:
        config_digest = _config_digest(cfg)
    except OSError as exc:
        return PROXY_UNKNOWN, "cannot read required config %s (%s)" % (cfg, exc)
    binary = launch["binary"]
    binary_source = binary["source"]
    try:
        binary_now = _binary_identity(binary_source)
    except (OSError, TypeError, ValueError) as exc:
        return PROXY_UNKNOWN, \
            "cannot read recorded proxy binary %s (%s)" % (binary_source, exc)
    changed = []
    if config_digest != launch.get("config_sha256"):
        changed.append("its config")
    if binary_now != binary:
        changed.append("the proxy binary")
    if not changed:
        return PROXY_CURRENT, None
    remedy = "helm seat down %s && helm seat up %s" % (seat_name, seat_name)
    return PROXY_STALE, ("STALE — launch inputs changed (%s); this process has "
                         "not loaded those changes. Restart: `%s`" %
                         (" and ".join(changed), remedy))


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
        return os.path.abspath(explicit) if os.path.exists(explicit) else None
    if os.path.exists(PROXY_BIN_DEFAULT):
        return os.path.abspath(PROXY_BIN_DEFAULT)
    found = shutil.which("cli-proxy-api")
    return os.path.abspath(found) if found else None


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


def _instance_gate(family, seat_name):
    """The ONE per-instance admissibility predicate, shared by every verb that
    can mint instance assets (spawn/resume — the fable MED was this gate
    living on spawn alone, so resume minted the very seats spawn refuses).
    Returns an error string to print, or None when the seat may proceed.
    Per-instance proxies are a proxy-family (OAuth-pool) feature; and the
    numeric suffix must be N>=2 — `-1` maps onto the family itself and base+1
    collides with the adjacent family's base port (codex-1 -> 8318 = kimi's
    base). The family seat itself is always admissible."""
    if seat_name == family:
        return _seat_surface_error(family, seat_name)
    fam = FAMILIES[family]
    if fam["mode"] != "proxy":
        return ("per-instance proxies need an OAuth-pool family "
                "(mode=proxy); %s is mode=%s — only the family seat `%s` is "
                "supported" % (family, fam["mode"], family))
    m = re.match(r"^%s-(\d+)$" % re.escape(family), seat_name)
    if m and m.group(1) != str(int(m.group(1))):
        # a zero-padded suffix passes int()==N but names a DISTINCT proxy home
        # whose derived port aliases the canonical instance's (codex-02 ->
        # instances/codex-02 yet port base+2 = codex-2's) — two homes/tokens,
        # one port, a confusing pre-bound-port failure. Refuse non-canonical
        # up front (fable adversarial LOW).
        return ("%s is not a canonical instance name — a zero-padded suffix "
                "aliases `%s-%d`'s port with a separate proxy home; use "
                "`%s-%d`" % (seat_name, family, int(m.group(1)),
                             family, int(m.group(1))))
    if m and int(m.group(1)) < 2:
        return ("%s is not a distinct instance — instance 1 IS the family "
                "seat `%s` (and base+1 would collide with a sibling family's "
                "port); use `%s` or instance N>=2" % (seat_name, family, family))
    return _seat_surface_error(family, seat_name)


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
    """The pidfile as {pid, identity, launch} or None. The first two fields stay
    backward-compatible (`<pid> <birth-identity>`); field three is URL-safe
    base64 JSON carrying the exact config/binary identities loaded at launch.
    Legacy records still authenticate process ownership but age is UNKNOWN until
    one restart captures launch inputs."""
    try:
        with open(os.path.join(_proxy_home(family, seat), "proxy.pid")) as f:
            parts = f.read().split()
        pid = int(parts[0])
    except (OSError, ValueError, IndexError):
        return None
    # pid < 2 can never be a spawned proxy: 0/1/negative are kernel-reserved
    # or the process-group / whole-signal-set selectors for kill(2). Treat a
    # corrupt file carrying one as stale (never signalled, reported down) so
    # _down's remediation never echoes `kill -1`/`kill 0` into advice an agent
    # would paste verbatim (fable adversarial MED — the signal path was already
    # fail-closed, but the printed suggestion was not).
    if pid < 2:
        return None
    launch = _decode_launch_inputs(parts[2]) if len(parts) > 2 else None
    return {"pid": pid, "identity": parts[1] if len(parts) > 1 else None,
            "launch": launch}


# The FOUR fail-closed conditions, named. Collapsing them to one None is right
# for a caller about to SIGNAL — it must not distinguish them, and refusing is
# the whole guard. It is wrong for a caller about to REPORT: "I cannot verify
# this pid" and "there is no process" are different claims, and rendering the
# first as the second is a confident falsehood about a running service.
PROXY_NO_RECORD = "no-record"        # no pidfile at all
PROXY_PID_DEAD = "pid-dead"          # pidfile names a pid that is gone
PROXY_UNVERIFIABLE = "unverifiable"  # bare or '?' identity — legacy pidfile
PROXY_PID_REUSED = "pid-reused"      # alive, but not the process we launched


def _proxy_pid_verdict(family, seat=None):
    """(rec, reason, raw) — the ONE owner of the fail-closed ladder.

    rec is the authenticated record or None; reason names WHY when None; raw is
    the unauthenticated pidfile record, which still carries a pid worth
    reporting when the refusal was about IDENTITY rather than existence.
    """
    raw = _proxy_pid_record(family, seat)
    if not raw:
        return None, PROXY_NO_RECORD, None
    if not _pid_alive(raw["pid"]):
        return None, PROXY_PID_DEAD, raw
    ident = raw["identity"]
    if not ident or ident == "?":
        return None, PROXY_UNVERIFIABLE, raw
    if _pid_identity(raw["pid"]) != ident:
        return None, PROXY_PID_REUSED, raw
    return raw, None, raw


def _running_pid_rec(family, seat=None):
    """The live proxy as authenticated {pid, identity, launch}, or None —
    `_running_pid`'s record-returning twin. ONE read of the pidfile produces
    the owned snapshot the caller threads through its signal/unlink (the
    atomic-ownership finding: re-reading the file after authenticating it lets
    a transient failure/malformed replacement split the verify from the kill).
    FAIL CLOSED identically: bare/'?' identity, dead pid, and birth-mismatch
    all return None — the ladder now lives in `_proxy_pid_verdict`, which this
    reads for its first element only, so signalling behaviour is unchanged."""
    return _proxy_pid_verdict(family, seat)[0]


def _running_pid(family, seat=None):
    """The live proxy pid, ONLY when it is verifiably the SAME process the
    pidfile recorded — a captured birth identity that still matches. FAIL
    CLOSED: a record with no usable identity (legacy bare pid, or '?' from a
    failed capture) is UNVERIFIABLE and returns None, so `_down` treats it as
    stale and never signals the number — the reused-pid SIGTERM finding. A
    live pid whose captured identity no longer matches is a REUSED pid and is
    likewise refused. There is no alive-check-only fallback: trusting an
    unauthenticated number is exactly the hazard this guard exists to close."""
    rec = _running_pid_rec(family, seat)
    return rec["pid"] if rec else None


def _port_open(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout):
            return True
    except OSError:
        return False
