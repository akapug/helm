"""Canonical paths, records, and process primitives for :mod:`helm.seat`."""
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time

from . import home, openflags

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
# transient-error-cooldown fix (909a471, "seat: emit
# transient-error-cooldown-seconds 5 in every proxy config") sat in its config
# unread. That is a real and checkable drift, and nothing reported it.
#
# NO CAUSAL CLAIM AT ALL. This comment previously held a two-subclass hypothesis
# — "ds4pro's drops cleared on restart and MAY be stale-proxy" — and it is dead.
# A post-land review found it still standing here, after I had retracted
# the same claim in three other places and missed this one.
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


def _discard_private(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _stage_private(path, text, mode=0o600):
    """Durably stage secret text beside ``path`` without publishing it."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    prior = None
    try:
        prior = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".private-", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        if prior is not None:
            os.fchown(fd, prior.st_uid, prior.st_gid)
        with os.fdopen(fd, "w") as f:
            fd = -1
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        if fd >= 0:
            os.close(fd)
        _discard_private(tmp)
        raise
    return tmp


def _commit_private(staged, path):
    """Publish one staged sibling and durably record the directory entry."""
    try:
        os.replace(staged, path)
        # openflags REFUSES on a runtime without O_DIRECTORY; a zero fallback
        # would fsync whatever the path resolves to and call the entry durable.
        fd = os.open(os.path.dirname(path),
                     openflags.flags(os.O_RDONLY, "O_DIRECTORY"))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        _discard_private(staged)
        raise


def _write_private(path, text, mode=0o600):
    """Atomically replace one secret-bearing file without a truncation window."""
    _commit_private(_stage_private(path, text, mode), path)


MGMT_SECRET_FILE = "mgmt.token"


def _mgmt_secret(d):
    """Read-or-mint the management password of the sidecar homed in `d`
    (0600 `mgmt.token` beside its config.yaml). A CREDENTIAL: the spawn hands
    it to the proxy as MANAGEMENT_PASSWORD and the usage reader presents it on
    loopback; nothing prints, logs or exports it. It is NOT the inbound seat
    token on purpose: that one is exported into the pane's environment, and
    the management API can download the pooled auth files, which no pane may
    reach."""
    path = os.path.join(d, MGMT_SECRET_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            secret = f.read().strip()
        if secret:
            return secret
    except OSError:
        pass
    import secrets
    secret = secrets.token_hex(32)
    _write_private(path, secret + "\n")
    return secret


def _mgmt_secret_present(d):
    """Whether `d` already holds a management password — the READER's
    question, asked without minting: a sidecar with no file was spawned
    before the meter and reads UNREADABLE until its next respawn."""
    try:
        with open(os.path.join(d, MGMT_SECRET_FILE), encoding="utf-8") as f:
            return bool(f.read().strip())
    except OSError:
        return False


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


# THE PROJECT-INSTANCE BLOCK. A project-canonical instance (`acme-codex`)
# carries no number to derive a port from, and returning the family base for it
# — which is what this module did — gave every such seat its OWN proxy home,
# config and token while pointing all of them at ONE socket: the first listener
# wins and every later seat talks to a stranger's proxy with its own bearer, or
# fails to bind and silently targets the family seat's. So project instances get
# ALLOCATED ports from a block of their own, and the allocation is a LEDGER
# rather than a hash because the property needed is injectivity, and a hash of a
# name only makes collisions rarer, never impossible.
#
# The block sits above every family base with headroom, so a numbered instance's
# base+N can never reach it (the same FAMILIES-table discipline `_instance_port`
# already documents, now asserted for this block by
# `_project_port_block_is_clear`).
PROJECT_PORT_BASE = 8500
PROJECT_PORT_SPAN = 100
_PROJECT_PORT_HEADROOM = 100


def _is_project_instance(family, seat):
    """True for the project-canonical `<project>-<family>` form ALONE.

    THE SET THAT HAS AN ALLOCATION IS NARROWER THAN "NOT A NUMBER", and
    conflating the two is the defect this predicate exists to keep apart. Only
    this form is minted an endpoint out of the ledger, so only this form can be
    unallocated; every OTHER non-numbered seat name shares the family's base
    port and always has — `codex-spark` names a MODEL WINDOW on the family
    endpoint, `seat-b` names an IDENTITY on it. Sending those spellings to the
    ledger asks it for an entry that nothing writes, and its honest None then
    reaches `ANTHROPIC_BASE_URL=http://127.0.0.1:%d` in the launch line, which
    raises TypeError instead of writing a launch.

    THE FAMILY IS THE LAST SEGMENT, which is `spawn_identity`'s own rule and
    the reason a project whose name carries a family word cannot shadow one.
    A non-empty base is required so the bare family name is not read as a
    project of the empty string.
    """
    base, sep, tail = (seat or "").rpartition("-")
    return bool(sep) and bool(base) and tail == family


def _project_port_block_is_clear():
    """'' when the project-instance block cannot collide with a family's own
    port space, else why. The floor the allocation stands on, asserted at import
    the way `_family_port_bases_are_unique` is.

    Reads the table through its OWN module rather than the shared seat namespace:
    this runs at `helm.seat` import time, which is BEFORE that namespace is
    seeded, and an invariant that cannot be evaluated where it is asserted is no
    invariant at all."""
    from .seat_catalog import FAMILIES as table
    bases = [fam["port"] for fam in table.values()]
    inside = sorted(b for b in bases
                    if PROJECT_PORT_BASE <= b < PROJECT_PORT_BASE + PROJECT_PORT_SPAN)
    if inside:
        return ("family base port(s) %s sit inside the project-instance block "
                "%d-%d" % (inside, PROJECT_PORT_BASE,
                           PROJECT_PORT_BASE + PROJECT_PORT_SPAN - 1))
    if PROJECT_PORT_BASE < max(bases) + _PROJECT_PORT_HEADROOM:
        return ("the project-instance block starts at %d, less than %d clear of "
                "the highest family base %d — a numbered instance's base+N "
                "would reach it at a low N" % (PROJECT_PORT_BASE,
                                               _PROJECT_PORT_HEADROOM,
                                               max(bases)))
    return ""


def _numbered_port_collision(family, port, table=None):
    """The OTHER family that declares `port`, or None — the nearest neighbour a
    numbered instance's base+N can land on.

    THE DERIVATION HAD ONE STOP AND IT WAS THE WRONG DISTANCE AWAY. base+N was
    bounded only by the project-instance block at 8500, so codex's numbered
    range ran to 8499 and passed straight through six declared bases on the
    way: ds4flash's 8330 is codex-13, qwen27's 8345 is codex-28, then ds4pro
    8360, grok 8380, gemini 8390, openrouter 8400. The admission door answered
    None for every one, so a numbered seat would have minted its own home,
    config and bearer onto ANOTHER family's socket -- the single outcome the
    per-instance allocation exists to prevent. What stood in for the invariant
    was vacancy on the host, which is a reading of the hour.

    A DECLARED PORT, NOT A RESERVED BAND, and the distinction is what keeps
    this from shrinking the namespace it protects. The FAMILIES comments
    describe bands (ds4flash 8330-8344; qwen27 declares 8345 and reserves
    nothing, because a proxy-key family mints no numbered instances) and
    those govern
    where the NEXT family's base may be declared; they are not allocations,
    because a proxy-key family mints no numbered instances and so binds exactly
    one socket. Stopping codex at the first band boundary would cap it at
    eleven numbered seats, nine of which are running, and cost every fixture in
    the suite that names a high spelling -- for ports nothing can ever bind.
    The ports that are really taken are each family's base, codex's base+N, and
    the project block, so those are what this refuses.

    base+1 is not special-cased here and must not be: kimi's 8318 is one above
    codex's 8317, and `_instance_gate` refuses N<2 for exactly that reason. The
    two rules are the same rule at two distances.
    """
    from .seat_catalog import FAMILIES as catalog
    table = catalog if table is None else table
    return next((other for other, fam in table.items()
                 if other != family and fam["port"] == port), None)


def _numbered_port_reservations_are_disjoint(table=None):
    """'' when no numbered instance can reach a port another family owns, else
    why — asserted at import beside `_project_port_block_is_clear`.

    TWO CLAUSES, NEITHER TRUE BY CONSTRUCTION. That no declared port sits
    inside a numbered range is now a REFUSAL rather than a fact about the
    table, so this states the table's own half: declared ports are distinct,
    and no two NUMBERING families exist to interleave. Only mode=proxy families
    mint numbered instances and there is one, but a second declared anywhere
    below the block would derive the same span as the first -- kimi at 8318
    would give kimi-2 the port 8320 that codex-3 already holds, and neither
    number is any family's DECLARED port, so the refusal above cannot see it.
    This is asserted at import because the reader who would otherwise find it
    is a seat talking to a stranger's proxy with its own bearer.
    """
    from .seat_catalog import FAMILIES as catalog
    table = catalog if table is None else table
    owners = {}
    for family, fam in table.items():
        owners.setdefault(fam["port"], []).append(family)
    shared = sorted(p for p, fams in owners.items() if len(fams) > 1)
    if shared:
        return ("families %s share declared port(s) %s"
                % (sorted(owners[shared[0]]), shared))
    numbering = sorted(f for f, fam in table.items()
                       if fam.get("mode") == "proxy")
    if len(numbering) > 1:
        spans = ", ".join("%s %d-%d" % (f, table[f]["port"] + 2,
                                        PROJECT_PORT_BASE - 1)
                          for f in numbering)
        return ("%d families mint numbered instances (%s) and their base+N "
                "ranges interleave; a derived port is owned by no family's "
                "declaration, so nothing refuses the overlap"
                % (len(numbering), spans))
    return ""


def _instance_ports_path():
    return os.path.join(seats_root(), "instance-ports.json")


_LEDGER_UNWRITTEN = object()   # "no such file", told apart from a file of `null`


def instance_port_ledger():
    """({seat: port}, None) for an ABSENT or wholly VALID ledger, else
    (None, why) — the error naming the FILE and the ENTRY a human has to fix.

    ABSENT IS FRESH; EVERYTHING ELSE THAT IS NOT A VALID TABLE IS UNKNOWN, and
    rendering any of them as an empty dict is the defect. A malformed ledger read
    as {} told the allocator that every endpoint in the block was free, so seat B
    took 8500 — the port seat A's config.yaml and launch.sh already name — and
    then WROTE a ledger saying so. A's token still authorizes A's proxy, B's
    launch line carries B's token at A's socket, and the one file that recorded
    the truth has been replaced by one that contradicts it. The under-lock
    re-read repeated the same reading, so the lock serialized the damage instead
    of preventing it.

    FOUR REFUSALS, ONE DOOR, because this is the only read in the tree and every
    caller — the allocator's two fast paths included — is answered by it:

    * `strict=True` separates a MISSING file from unparseable bytes and from a
      dangling symlink (pk.read_json's own law). A distinct sentinel is what
      separates it from a file whose whole content is `null`: both come back as
      the default, and calling the second one fresh let the next allocation
      REPLACE an existing non-table instead of preserving it and saying so.
    * An entry that is not `name -> int` is malformed; `{"a-codex": "8500"}` is
      not a readable allocation and silently dropping it is the same
      empty-by-default defect one level down.
    * An entry OUTSIDE the block is refused. The block's whole purpose is that a
      project seat's endpoint is distinct from every family base, so an entry at
      8317 records the alias this file exists to end — and returning it from the
      `seat in ledger` fast path hands that alias straight to a launch asset.
    * TWO SEATS AT ONE ENDPOINT is refused. Type-checking each entry says nothing
      about the table, and a ledger mapping two project seats to 8500 was
      accepted: both fast paths returned it, so two seats' bearers aimed at one
      proxy with the ledger itself as the authority for it.

    Refusing is all this does. It never rewrites, so the original bytes survive
    for whoever has to read them.
    """
    from . import pk
    path = _instance_ports_path()
    try:
        led = pk.read_json(path, _LEDGER_UNWRITTEN, strict=True)
    except Exception as e:
        return None, ("the project-instance port ledger %s cannot be read "
                      "(%s: %s)" % (path, type(e).__name__, e))
    if led is _LEDGER_UNWRITTEN:
        return {}, None                  # never written: a fresh fleet
    if not isinstance(led, dict):
        return None, ("the project-instance port ledger %s is not an allocation "
                      "table — its top level is %s, not an object"
                      % (path, "null" if led is None
                         else "a %s" % type(led).__name__))
    out, holder = {}, {}
    top = PROJECT_PORT_BASE + PROJECT_PORT_SPAN - 1
    for seat, port in led.items():
        if not isinstance(seat, str) or not isinstance(port, int) \
                or isinstance(port, bool):
            return None, ("the project-instance port ledger %s holds a "
                          "malformed entry (%r -> %r): an allocation is a seat "
                          "name mapped to a port number" % (path, seat, port))
        if not PROJECT_PORT_BASE <= port <= top:
            return None, ("the project-instance port ledger %s allocates %s the "
                          "endpoint %d, which is OUTSIDE the project-instance "
                          "block %d-%d — an entry there names a port some "
                          "family owns, which is the alias this block exists to "
                          "end" % (path, seat, port, PROJECT_PORT_BASE, top))
        if port in holder:
            return None, ("the project-instance port ledger %s allocates the "
                          "endpoint %d to BOTH %s and %s — one socket cannot "
                          "carry two seats' bearer tokens, and this file is the "
                          "authority that says it does"
                          % (path, port, holder[port], seat))
        holder[port] = seat
        out[seat] = port
    return out, None


def _configured_instance_port_strict(family, seat):
    """(the port an instance's OWN config.yaml names or None, None), else
    (None, why) — the strict half of `_configured_instance_port`.

    ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS, and the lenient reader below
    cannot tell a caller which it got. No config.yaml means this directory holds
    no endpoint at all (`_mint_instance_proxy` is what writes one), so a census
    reserves nothing for it. A config.yaml helm cannot OPEN may name any port in
    the block, so a census that skipped it would report that port free and the
    next allocation would take an endpoint a live proxy is already serving.
    """
    path = os.path.join(_proxy_home(family, seat), "config.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None, None                # no config: no endpoint to reserve
    except OSError as e:
        return None, ("%s cannot be read (%s)" % (path, e))
    m = re.search(r"(?m)^port:[ \t]*(\d+)[ \t]*$", text)
    return (int(m.group(1)) if m else None), None


def _configured_instance_port(family, seat):
    """The port an instance's OWN config.yaml names, or None.

    THE STRONGEST AUTHORITY A READER HAS about a seat that already exists,
    because it is the number the proxy was actually started on — stronger than
    any derivation from the name, which is exactly the mapping that changed.
    Read as text: this is a reader on a status path, and it must not depend on a
    yaml library or on the writer's formatting beyond the one line it wrote.

    LENIENT ON PURPOSE, and only for STATUS: a roster row about a seat whose
    config cannot be read still has to render, and None there degrades one field.
    Every ALLOCATION reads `_configured_instance_port_strict` instead, where the
    same None would be a claim that an endpoint is free.
    """
    return _configured_instance_port_strict(family, seat)[0]


def _minted_block_census():
    """({port: seat} for every endpoint inside the project block an already
    MINTED instance config names, None), else (None, why).

    FUTURE ADMISSION IS NOT RECONCILIATION, and that gap is the finding.
    `_instance_port` refuses `codex-183` from today on; it says nothing about the
    `codex-183` minted while base+N was unbounded, whose config.yaml reads
    `port: 8500` and whose proxy is listening there right now. An allocator that
    consulted only its own ledger would hand 8500 to the next project seat,
    which would write its own bearer into a launch line aimed at that instance's
    socket — the exact alias this block exists to end, arriving from the
    existing fleet instead of from the mapper.

    Reads the CONFIG's own port, not a derivation from the name: the derivation
    is the thing that changed.

    AND AN UNREADABLE SOURCE ENDS THE CENSUS. The first cut skipped an instances
    directory it could not list and mapped an unreadable config to None, so a
    permission wall over one stopped high-number instance dropped that
    instance's reservation and the very next allocation handed its endpoint out
    again. A directory that does not EXIST is a family with no instances, which
    is evidence; any other error is the absence of evidence, and this returns it
    as such rather than as free capacity.
    """
    held = {}
    for family in FAMILIES:
        root = os.path.join(seat_dir(family), "instances")
        try:
            names = os.listdir(root)
        except FileNotFoundError:
            continue                     # no instances for this family
        except OSError as e:
            return None, ("the minted-endpoint census cannot list %s (%s) — an "
                          "instance directory helm cannot read may hold any "
                          "endpoint in the block" % (root, e))
        for name in names:
            port, err = _configured_instance_port_strict(family, name)
            if err:
                return None, "the minted-endpoint census stopped: " + err
            if port is not None and PROJECT_PORT_BASE <= port \
                    < PROJECT_PORT_BASE + PROJECT_PORT_SPAN:
                held.setdefault(port, name)
    return held, None


def _existing_instance_port(family, seat=None):
    """The endpoint an ALREADY-MINTED instance HOLDS — the READER's answer, and
    never None while that instance's own config exists.

    ADMISSION AND THE EXISTING FLEET ARE DIFFERENT QUESTIONS. `_instance_port`
    answers admission, and it must refuse `codex-183`: nothing new may be minted
    onto a derived port that lands inside the allocated block. A status renderer
    is asking something else — this instance exists, its config names a port, a
    pid may be serving it, what IS it? None there formats `port %d` of a None
    and takes the whole roster verb down (the `_unresolvable_family_row`
    history), and calling a live proxy portless is a worse answer than the
    number its own config carries.
    """
    seat = seat or family
    port = _instance_port(family, seat)
    if port is not None or family not in FAMILIES:
        return port
    return _configured_instance_port(family, seat)


def allocate_instance_port(seat):
    """(port, None) — this project instance's OWN endpoint, allocated once and
    stable for its life — or (None, why).

    THE ONE MUTATING DOOR, and the ONLY one: admission asks
    `_instance_endpoint_error`, which is pure, because a durable slot spent by a
    `--print` preview or a refused argument is never given back (the span is 100
    and the entries do not expire). Idempotent and flocked, so two concurrent
    spawns of two project seats cannot both take the lowest free port.

    IT REFUSES INSTEAD OF GUESSING, in every direction the answer is not
    knowable: an unreadable ledger before the lock AND after it (the re-read
    exists because a concurrent writer may have landed, and reading THAT
    writer's bytes as {} would overwrite its allocation), a fully taken block,
    or a file it cannot write. A seat already in the ledger gets its recorded
    port back — an existing allocation is never recomputed and never rewritten.

    Reserved beyond the ledger, because the ledger is not reality's only writer:
    a port an existing instance CONFIG names (`_minted_block_census`) and a port
    something is already LISTENING on (`seat_ports.listen_census`, over the
    kernel's own TCP tables). Probed per candidate rather than for the whole
    block, so the ordinary allocation costs one look.

    AND EVERY ONE OF THOSE THREE SOURCES MAY ANSWER UNKNOWN, which is a REFUSAL
    and never free capacity. That is the whole reason both censuses return a
    reason beside their answer: an unreadable instance directory, an unreadable
    instance config and an unreadable TCP table each arrive as an empty
    SUCCESSFUL census without it, and a candidate is then persisted as free on
    the strength of evidence helm never actually saw.
    """
    import fcntl
    from . import pk
    from . import seat_ports
    ledger, err = instance_port_ledger()
    if err:
        return None, err
    if seat in ledger:
        return ledger[seat], None
    try:
        os.makedirs(seats_root(), mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(seats_root(), ".instance-ports.lock"),
                     os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        return None, ("the project-instance port lock under %s cannot be taken "
                      "(%s)" % (seats_root(), e))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ledger, err = instance_port_ledger()      # re-read INSIDE the lock
        if err:
            return None, err
        if seat in ledger:
            return ledger[seat], None
        minted, census_err = _minted_block_census()
        if census_err:
            return None, ("%s cannot be allocated an endpoint: %s. An "
                          "unreadable census is UNKNOWN, so nothing was written"
                          % (seat, census_err))
        taken = set(ledger.values()) | set(minted)
        for port in range(PROJECT_PORT_BASE,
                          PROJECT_PORT_BASE + PROJECT_PORT_SPAN):
            if port in taken:
                continue
            listening, listen_err = seat_ports.listen_census(port)
            if listen_err:
                return None, ("%s cannot be allocated an endpoint: %s. An "
                              "unreadable census is UNKNOWN, so nothing was "
                              "written" % (seat, listen_err))
            if listening:
                continue
            ledger[seat] = port
            try:
                pk.write_json(_instance_ports_path(), ledger)
            except OSError as e:
                return None, ("the project-instance port ledger %s could not be "
                              "written (%s)" % (_instance_ports_path(), e))
            return port, None
        return None, ("the project-instance port block %d-%d is fully taken — "
                      "%d allocated in %s, %d held by an existing instance "
                      "config (%s), the rest answering on this host"
                      % (PROJECT_PORT_BASE,
                         PROJECT_PORT_BASE + PROJECT_PORT_SPAN - 1,
                         len(ledger), _instance_ports_path(), len(minted),
                         ", ".join(sorted(minted.values())) or "none"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _instance_endpoint_error(family, seat_name):
    """None when this seat may HAVE an endpoint, else why — and PURE, touching
    neither the ledger nor any other durable state.

    THE ADMISSION HALF OF THE ALLOCATION, split from the commit because an
    allocator reached from the gate spends a durable slot on every question:
    `_spawn` calls the gate before the `--print` check, before the argument
    parse, before the workspace proof and before the mint, so a preview of a
    name, a typo'd flag and a refused
    workspace each consumed a durable slot. One hundred previews of one hundred
    project names filled the span without ever creating a seat.
    """
    if family not in FAMILIES:
        return None                      # a non-proxy family has no endpoint
    if seat_name == family:
        return None                      # the family base port, always
    m = re.match(r"^%s-(\d+)$" % re.escape(family), seat_name)
    if m:
        port = FAMILIES[family]["port"] + int(m.group(1))
        if port >= PROJECT_PORT_BASE:
            return ("%s's derived port %d reaches the project-instance block "
                    "%d-%d, where it would alias an allocated project seat's "
                    "endpoint — use a lower instance number"
                    % (seat_name, port, PROJECT_PORT_BASE,
                       PROJECT_PORT_BASE + PROJECT_PORT_SPAN - 1))
        owner = _numbered_port_collision(family, port)
        if owner:
            # THE SAME REFUSAL AT A NEARER DISTANCE, and it was missing. The
            # project block was the only thing base+N was ever stopped by, so
            # every family declared above this one sat inside this family's
            # numbered reach and the door said None. Naming the family that
            # owns the port is what makes the refusal actionable: the answer is
            # a different instance number, never this endpoint.
            return ("%s's derived port %d is %s's DECLARED port — a seat there "
                    "would bind that family's endpoint and aim its own bearer "
                    "at that family's proxy; use another instance number"
                    % (seat_name, port, owner))
        return None
    if not _is_project_instance(family, seat_name):
        # THE SIBLING OF `_instance_port`'s SAME CONFLATION. Only the
        # project-canonical form is allocated, so only it can be refused for
        # want of an allocation; a non-project, non-numbered name shares the
        # family base and needs nothing out of the ledger. Asking here anyway
        # made a FULL block refuse `codex-spark`, a seat whose endpoint is not
        # in the block at all.
        return None
    ledger, err = instance_port_ledger()
    if err:
        # AN UNREADABLE LEDGER IS A REFUSAL, never an empty one. Saying so here
        # is what keeps the allocator from being asked at all.
        return ("%s has no provable per-instance proxy endpoint: %s. Repair or "
                "move that file — sharing the family port would aim this seat's "
                "own bearer at another seat's proxy" % (seat_name, err))
    if seat_name in ledger:
        return None
    minted, census_err = _minted_block_census()
    if census_err:
        # THE SAME LAW ONE RUNG EARLIER. The gate is pure, but "pure" is about
        # writing, not about guessing: a census it cannot complete says nothing
        # about whether a free endpoint exists, and admitting the seat on that
        # would only move the same unknown down to the allocator.
        return ("%s has no provable per-instance proxy endpoint: %s. Repair that "
                "source — sharing the family port would aim this seat's own "
                "bearer at another seat's proxy" % (seat_name, census_err))
    free = [port for port in range(PROJECT_PORT_BASE,
                                   PROJECT_PORT_BASE + PROJECT_PORT_SPAN)
            if port not in set(ledger.values()) | set(minted)]
    if not free:
        return ("%s has no per-instance proxy endpoint — the project-instance "
                "port block %d-%d is fully taken (ledger %s); sharing the "
                "family port would aim this seat's own bearer at another seat's "
                "proxy" % (seat_name, PROJECT_PORT_BASE,
                           PROJECT_PORT_BASE + PROJECT_PORT_SPAN - 1,
                           _instance_ports_path()))
    return None


def _instance_port(family, seat=None):
    """A seat's OWN proxy port, or None when it has no proxy at all.

    Instance 1 keeps fam["port"] (8317 for codex — the port every minted
    launch.sh already points at). Instances N≥2 derive deterministically from
    the numeric seat suffix (codex-2 -> port+2), so that mapping needs no
    allocation state. A PROJECT-CANONICAL instance has no number, so it is
    ALLOCATED an endpoint from the project block above — distinct from the base
    port and from every numbered and project sibling by construction.

    A family outside the proxy table (native `claude`) has NO PORT: it runs no
    proxy, and answering with a number would invite a caller to write one into a
    launch line that must never carry one.

    COLLISION INVARIANT: only mode=proxy families mint numbered instances (the
    launch gate refuses proxy-key families), so numbered ports come from ONE
    family's block at a time; a new proxy family MUST be assigned a base far
    enough from every existing proxy family's block that base+N ranges never
    interleave (codex occupies 8317+N; leave headroom).
    `_family_port_bases_are_unique` asserts the bases themselves are distinct
    and `_project_port_block_is_clear` asserts the project block is outside all
    of them; the interleave headroom is a FAMILIES-table discipline."""
    seat = seat or family
    if family not in FAMILIES:
        return None
    base = FAMILIES[family]["port"]
    if seat == family:
        return base
    m = re.match(r"^%s-(\d+)$" % re.escape(family), seat)
    if m:
        port = base + int(m.group(1))
        # HEADROOM IS NOT A BOUND, and a probe proved it: base+N is unbounded, so
        # codex-183 derives exactly 8500 — the first project-instance endpoint.
        # The block's disjointness from the BASES is an assertable invariant; its
        # disjointness from base+N can only be a REFUSAL, so a suffix that
        # reaches the block has no port here and `_instance_gate` declines the
        # seat. Returning the colliding number instead would hand a reader the
        # one answer this allocation exists to prevent.
        #
        # AND THE BLOCK IS NOT THE NEAREST NEIGHBOUR. The same reasoning binds
        # every family declared above this one: base+N reached their bases too,
        # and answering with the number would hand a launch line the port a
        # DIFFERENT family's proxy is already serving. `_numbered_port_collision`
        # is that neighbour; `_existing_instance_port` is what keeps a status
        # row about an instance minted before the refusal from rendering
        # portless.
        if _numbered_port_collision(family, port):
            return None
        return port if port < PROJECT_PORT_BASE else None
    if not _is_project_instance(family, seat):
        # A NON-PROJECT, NON-NUMBERED NAME IS AN IDENTITY ON THE FAMILY
        # ENDPOINT, not an instance with an endpoint of its own, and the family
        # base is the answer it has always been given. The project block did not
        # widen the set of names with allocations; it only gave THE PROJECT FORM
        # one. Asking the ledger about every other spelling answered None for
        # names no ledger entry can exist for, and a None is what the launch
        # line renders with `%d`.
        return base
    # PURE, AND THAT IS THE POINT. An ALLOCATION here would put a durable write
    # behind every reader there is: the admission gate, a `--print` preview, a
    # status row. A project instance's endpoint is whatever the
    # LEDGER already records for it and None until something commits one
    # (`allocate_instance_port`, at the mint). None is the honest answer for
    # "not allocated yet" — the base port is the alias this block exists to end.
    ledger, _err = instance_port_ledger()
    return (ledger or {}).get(seat)


def _launch_endpoint(family, seat=None):
    """The port a LAUNCH ASSET renders, or ValueError naming why there is none.

    THE ONE DOOR between `_instance_port` — which answers None for three real
    states — and every `%d` that writes an endpoint into a file or a line a seat
    will exec. A None arriving at such a `%d` raises `TypeError: %d format: a
    real number is required`, a message about string formatting that names
    neither the seat nor the missing allocation, and it arrives from inside the
    renderer rather than from the door that knows the answer is absent.

    It RAISES rather than substituting the family base, because the base is the
    alias the project block exists to end: writing 8317 into an unallocated
    project seat's launch.sh points it at the family's socket carrying its own
    bearer, which is the exact state this module refuses one rung earlier.
    """
    seat = seat or family
    port = _instance_port(family, seat)
    if port is not None:
        return port
    if family not in FAMILIES:
        raise ValueError(
            "%s is outside the proxy table, so seat '%s' has no proxy, port or "
            "endpoint to write into a launch asset — a native seat rides "
            "`helm launch --seat`, which carries no ANTHROPIC_BASE_URL"
            % (family, seat))
    if _is_project_instance(family, seat):
        raise ValueError(
            "project instance '%s' holds no allocated endpoint in %s — "
            "`allocate_instance_port` mints one at the spawn that commits to the "
            "seat, and an asset written before that would name the family base "
            "port every project instance used to be aliased onto"
            % (seat, _instance_ports_path()))
    raise ValueError(
        "seat '%s' has no endpoint of its own: %s, which is refused rather "
        "than aliased"
        % (seat, _instance_endpoint_error(family, seat)
           or ("the port its numeric suffix derives reaches the "
               "project-instance block %d-%d"
               % (PROJECT_PORT_BASE, PROJECT_PORT_BASE + PROJECT_PORT_SPAN - 1))))


def _maintenance_endpoint(family, seat=None):
    """The endpoint an instance that ALREADY EXISTS is maintained on, or
    ValueError naming why it cannot be resolved.

    ADMISSION AND MAINTENANCE ARE TWO DOORS, and sending maintenance through
    admission's is the finding. `_launch_endpoint` answers "what port may be
    written into an asset for a seat being admitted?", and it must refuse
    `codex-183`: nothing NEW may be minted onto a derived port inside the
    allocated block. Regenerating the config of a codex-183 that was minted
    while base+N was unbounded is the other question entirely — that instance
    exists, its own config.yaml reads `port: 8500`, and its proxy is serving
    there. Asking admission produced a refusal in the middle of
    `proxy_config_plan`, so `seat up`, the supervise reconciler and the drift
    reader could all RESOLVE that endpoint and then none of them could finish:
    every restart of that seat failed at config regeneration.

    So this reads through `_existing_instance_port` — the reader's accessor, the
    config's own number — and can never reach the new-admission predicate. It
    still RAISES rather than substituting the family base, for `_launch_endpoint`'s
    reason: writing 8317 into an instance's config points it at the family's
    socket. A seat with neither a ledger entry nor a readable config has no
    endpoint to maintain, and saying so names both files.
    """
    seat = seat or family
    port = _existing_instance_port(family, seat)
    if port is not None:
        return port
    if family not in FAMILIES:
        raise ValueError(
            "%s is outside the proxy table, so seat '%s' has no proxy config to "
            "maintain and no endpoint to write into one" % (family, seat))
    raise ValueError(
        "seat '%s' has an existing proxy config to maintain but no resolvable "
        "endpoint: neither %s nor %s names a port, and the family base is the "
        "alias the project-instance block exists to end — re-mint the instance"
        % (seat, _instance_ports_path(),
           os.path.join(_proxy_home(family, seat), "config.yaml")))


import contextlib as _contextlib


def _seat_spawn_gate(family, seat_name):
    """THE FAMILY-AGNOSTIC HALF of the spawn/resume gate: surface OWNERSHIP.

    Every family's spawn passes through exactly this, native `claude` included,
    and it is DELIBERATELY OUTSIDE `_instance_gate` — the proxy-specific
    predicate. Nesting it there makes the ownership proof something a non-proxy
    adapter can reach the mutation without, and a second lifecycle is how one
    arrives there: a native leg doing its own mkdir + register with no proof that
    `seats/claude/instances/<seat>` was still that seat's own directory and not
    a symlink into a sibling's, where the register write lands on the sibling's
    spawn.json. Containment under seats_root is not ownership; that distinction
    is `_seat_surface_error`'s, and this is the door both adapters knock on.
    """
    return _seat_surface_error(family, seat_name)


def _instance_gate(family, seat_name):
    """The PROXY-specific per-instance admissibility predicate, wrapped around
    the shared `_seat_spawn_gate`, shared by every verb that can mint instance
    assets (spawn/resume — the MED was this gate living on spawn alone, so
    resume minted the very seats spawn refuses).
    Returns an error string to print, or None when the seat may proceed.
    Per-instance proxies are a proxy-family (OAuth-pool) feature; and the
    numeric suffix must be N>=2 — `-1` maps onto the family itself and base+1
    collides with the adjacent family's base port (codex-1 -> 8318 = kimi's
    base). The family seat itself is always admissible.

    A family outside the proxy table never belongs here: it has no mode, no port
    and no proxy to gate. Saying so is the refusal — reaching FAMILIES[family]
    for it would raise KeyError from inside a gate whose whole job is to
    answer."""
    if family not in FAMILIES:
        return ("%s is not a proxy-table family — it has no proxy, port or "
                "per-instance endpoint to gate, so `%s` never reaches this "
                "door (native claude rides `helm launch --seat`)"
                % (family, seat_name))
    if seat_name == family:
        return _seat_spawn_gate(family, seat_name)
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
        # up front (an adversarial LOW).
        return ("%s is not a canonical instance name — a zero-padded suffix "
                "aliases `%s-%d`'s port with a separate proxy home; use "
                "`%s-%d`" % (seat_name, family, int(m.group(1)),
                             family, int(m.group(1))))
    if m and int(m.group(1)) < 2:
        return ("%s is not a distinct instance — instance 1 IS the family "
                "seat `%s` (and base+1 would collide with a sibling family's "
                "port); use `%s` or instance N>=2" % (seat_name, family, family))
    # AN INSTANCE THAT CANNOT HAVE ITS OWN ENDPOINT IS NOT ADMISSIBLE, and the
    # only wrong answer is the family's base port — that is the alias the whole
    # allocation exists to stop. Asked of the PURE predicate: a gate that
    # allocated spent a durable slot on every preview and every later refusal.
    endpoint = _instance_endpoint_error(family, seat_name)
    if endpoint:
        return endpoint
    return _seat_spawn_gate(family, seat_name)


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
    # would paste verbatim (an adversarial MED — the signal path was already
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
