#!/usr/bin/env python3
"""helm chat node — supervision of the chat room node (a dregg tmpfs node).

INTERIM SCAFFOLDING by design (one-node-per-team law, PRD 2026-07-19): the
target topology is one team node; interim, chat gets its OWN tmpfs node so a signed
chat turn can never land on a disk-persisted chain. Everything here is
node-agnostic — the node migration repoints the url in the state file and
this module keeps working unchanged.

The daemon is the SUBSTRATE's (`dregg-cave-node`), helm only supervises it
via a user systemd unit `helm-chat-node.service` (precedent: the team node's
dregg-cave.service) — the no-daemons law stays intact. Data-dir lives on
tmpfs (/dev/shm/helm-chat-node): messages exist only in RAM.

Provisioning contract (probed live against the binary, 2026-07-19):
  - a fresh node boots LOCKED and UNHEALTHY (no blocks yet);
  - POST /api/cipherclerk/unlock {"passphrase"} — first unlock SETS the
    passphrase, unlocks /turns/submit, returns the bearer token;
  - one faucet turn (POST /api/faucet) commits the first block -> healthy,
    which the client binary's health gate demands;
  - the bearer token goes stale on reboot (tmpfs dies); chat's send path
    re-unlocks with the STORED passphrase (see chat._revive).

State: <helm-home>/_global/.state/chat-node.json (0600) {url, passphrase,
token, binary} — provisioning config written at `node up` time (binary is
the node binary `up` installed, see BIN_RECORD), never in the send/read hot
path (the RAM canon governs message data; the unit file and
this credential live on disk exactly like every other service config).

Import-safe, stdlib-only.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from . import cell, home, pk

UNIT = "helm-chat-node.service"
DATA_DIR = "/dev/shm/helm-chat-node"
PORT = 8898
GOSSIP_PORT = 18898
# HOW LONG `up` WAITS FOR THE API, and why it is ten minutes. A rebased node
# runs the full verified-runtime (Lean) init before it binds its HTTP API:
# measured 150.2-154.2 s on a fab build node, and a laptop may be slower. The fee-loop
# build answers in 0.17-0.26 s (this host's journal, three boots). The default
# is sized for the slow build, and it costs a failing fast one nothing: the
# wait ends the moment the unit's process exits (wait_boot), so a refusal is
# still reported within seconds, never after the full wait.
HEALTH_WAIT_S = 600
BOOT_WAIT_ENV = "CHAT_NODE_BOOT_WAIT_S"   # HELM_ (or MELD_) prefixed
BOOT_TICK_S = 30                          # one progress line per tick
START_GRACE_S = 10                        # a --no-block start still queued
# The node's identity, as opposed to its data: its CHAIN DESCRIPTOR, which is
# every top-level file in the data dir except the store. A fee-loop build's
# descriptor is node.key plus, once it has run, starbridge-seed.json and
# agent-alice.key. A rebased build's is node.key, genesis.json, .devnet,
# node-0.env, faucet.key, fee-well.key, issuer-well.key, the agent keys and
# starbridge-seed.json once written — and it will not start from node.key
# alone ("blocklace requires consensus_genesis_unix_seconds ..."). The ledger
# (dregg.redb) is deliberately never part of it — see identity_dir().
STORE_PREFIX = "dregg.redb"
DESCRIPTOR_MAX_BYTES = 1 << 20   # keys and JSON; anything larger is not one
GENESIS_FILE = "genesis.json"
# The mint (init or the successor ceremony) runs the full verified-runtime
# init: 145-150 s measured on a fast host. The unit's TimeoutStartSec covers
# the mint, the `genesis --help` probe and the copy, with room to spare, so a
# slow mint is ended by python's own timeout — whose cleanup runs — and never
# by systemd's SIGTERM, which leaves a half-made scratch dir behind.
MINT_TIMEOUT_S = 900
PROBE_TIMEOUT_S = 30
START_TIMEOUT_S = MINT_TIMEOUT_S + PROBE_TIMEOUT_S + 60
MINT_DIR_PREFIX = "chat-node-mint-"   # scratch beside the snapshot, on disk
STAGE_INFIX = ".mint-"                # a data dir being assembled, beside it
PARTIAL_INFIX = ".partial-"           # a keyless data dir moved aside
PRE_GENESIS_INFIX = ".pre-genesis-"   # the fee-loop identity a ceremony replaced
REPLACED_INFIX = ".replaced-"         # a snapshot a differing key replaced
FAUCET_KEY = "faucet.key"
# THE GENESIS PATCH, applied before a new chain first runs. It replaces the
# fee-loop build's genesis-LESS backfills (fee well pointed at the faucet
# cell, EmitEvent-only turns fee-exempt), which never run on a node that can
# start. Both keys have readers on the rebased node: `fee_well` at
# node/src/lib.rs:2417 and `coordination_fee_exempt` at lib.rs:2431 (into
# executor_setup.rs:408); a patched node logs "genesis coordination_fee_exempt:
# EmitEvent-only turns are fee-exempt" at boot (measured on a scratch node).
#
# THE FAUCET CELL IS READ FROM THE MINTED DESCRIPTOR, never assumed. The
# faucet's KEY is fixed (every build derives it from one devnet constant), but
# its CELL is not: the same public key names one cell in an older build's
# genesis (token id zero) and a different one in the rebased build's (token
# id blake3("default")) — both real descriptors are the fixtures in
# tests/fixtures/chatnode_rebased_genesis. So the patch derives the public
# key faucet.key controls and takes the one initial cell carrying it.
GENESIS_PATCH_EXEMPT = ("coordination_fee_exempt", True)


def descriptor_files(data_dir):
    """The data dir's chain descriptor: its top-level regular files, minus
    the store and anything too large to be a key or a descriptor. []
    for a dir that is absent or unreadable."""
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(data_dir, n)
        if n.startswith(STORE_PREFIX) or n.endswith(".tmp") \
                or os.path.islink(p) or not os.path.isfile(p):
            continue
        try:
            if os.path.getsize(p) > DESCRIPTOR_MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(n)
    return out

# THE BINARY `up` INSTALLED IS RECORDED, because nothing else knew it. The
# unit names a binary, but `up` re-renders the unit from the default
# resolution every time it runs, and that resolution was HELM_CHAT_NODE_BIN,
# else `dregg-cave-node` on PATH, else ~/.local/bin/dregg-cave-node. So a node
# brought up on a new chain's binary with the variable set went back to the
# old binary on the next bare `up`, against the new chain's data. A bare `up`
# is not rare: status and doctor print it as the fix. `up` now records the
# binary (absolute path and sha256) beside the node credential, and the
# default resolution prefers that record. A record it cannot honour (the
# file is gone, its bytes changed, the record will not read) is a refusal,
# never a quiet fall back to whichever binary the PATH offers.
BIN_ENV = "CHAT_NODE_BIN"             # HELM_ (or MELD_) prefixed
BIN_RECORD = "binary"                 # chat-node.json: {"path", "sha256"}
BIN_FIX = "Fix: HELM_CHAT_NODE_BIN=<binary> helm chat node up"
BIN_SOURCES = {
    "env": "from HELM_CHAT_NODE_BIN",
    "recorded": "recorded by `helm chat node up`",
    "fallback": "fallback (nothing recorded): dregg-cave-node on PATH, else "
                "~/.local/bin",
}
_SHA_MEMO = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_SHA_MEMO": (
        "sha256 keyed by path, inode, size, mtime and ctime; a changed file "
        "misses"),
}


def file_sha256(path):
    """The sha256 of a regular file, as hex. Raises OSError, including for a
    path that is not a regular file (a FIFO would hang the read).

    Memoised for this process on the file's whole stat identity (device,
    inode, size, mtime and ctime in ns): status reads the binary twice, and
    a rebased node is about 330 MB, 0.43 s to hash. A write moves ctime,
    which no caller can set back, so a changed file misses the memo."""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise OSError("not a regular file: %s" % path)
    key = (os.path.abspath(path), st.st_dev, st.st_ino, st.st_size,
           st.st_mtime_ns, st.st_ctime_ns)
    if key not in _SHA_MEMO:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _SHA_MEMO[key] = h.hexdigest()
    return _SHA_MEMO[key]


def install_path(b):
    """The absolute path `up` renders into the unit and records: a bare name
    resolves on PATH (as the unit would not), anything else is made
    absolute. Symlinks are kept, so the record names what the operator
    named; the sha256 follows the link, so repointing it is a change."""
    if os.sep not in b:
        b = shutil.which(b) or b
    return os.path.abspath(os.path.expanduser(b))


def recorded_binary():
    """(record, None) for the binary the last `up` installed, as {"path",
    "sha256"}, when that file still holds those bytes; (None, None) when
    nothing is recorded; (None, why) when a record exists and cannot be
    honoured. UNREADABLE IS NOT ABSENT: a state file that will not parse
    may hold a record, so it refuses rather than read as none."""
    path = state_path()
    try:
        st = pk.read_json(path, {}, strict=True)
    except Exception as e:            # noqa: BLE001 — any unreadable state is one refusal, named by its class
        st = e
    if not isinstance(st, dict):
        return None, ("the chat node state %s does not read as a JSON object "
                      "(%s), so the recorded binary is unknown — refusing to "
                      "guess one. %s" % (path, st.__class__.__name__, BIN_FIX))
    rec = st.get(BIN_RECORD)
    if rec is None:
        return None, None
    rec = rec if isinstance(rec, dict) else {}
    where, want = rec.get("path"), rec.get("sha256")
    if not (isinstance(where, str) and os.path.isabs(where)
            and isinstance(want, str)
            and re.fullmatch(r"[0-9a-f]{64}", want)):
        return None, ("the binary record in %s is malformed (it needs an "
                      "absolute path and a sha256) — refusing to fall back "
                      "to another binary. %s" % (path, BIN_FIX))
    try:
        have = file_sha256(where)
    except OSError as e:
        gone = isinstance(e, FileNotFoundError)
        return None, ("the recorded chat node binary %s is %s — refusing to "
                      "fall back to another binary, which may run another "
                      "chain. %s" % (where, "MISSING" if gone else
                                     "unreadable (%s)" % e.__class__.__name__,
                                     BIN_FIX))
    if have != want:
        return None, ("the recorded chat node binary %s has CHANGED (sha256 "
                      "%s recorded, %s now) — refusing to run bytes `up` never "
                      "installed. %s" % (where, want[:12], have[:12], BIN_FIX))
    return {"path": where, "sha256": want}, None


def bin_resolution():
    """Which node binary `up` installs, and where that came from:
    {"path", "source", "reason"}. The order is HELM_CHAT_NODE_BIN ("env"),
    else the binary the last `up` recorded ("recorded"), else — ONLY when
    nothing is recorded — `dregg-cave-node` on PATH, else
    ~/.local/bin/dregg-cave-node ("fallback"). A path of None always carries
    a reason; a record that cannot be honoured is source "recorded" with no
    path, and nothing below it is consulted."""
    explicit = home.env(BIN_ENV)
    if explicit:
        return {"path": explicit, "source": "env", "reason": None}
    rec, why = recorded_binary()
    if why:
        return {"path": None, "source": "recorded", "reason": why}
    if rec:
        return {"path": rec["path"], "source": "recorded", "reason": None}
    known = os.path.join(os.path.expanduser("~"), ".local", "bin", "dregg-cave-node")
    found = shutil.which("dregg-cave-node") or (
        known if os.path.exists(known) else None)
    if found:
        return {"path": found, "source": "fallback", "reason": None}
    return {"path": None, "source": None,
            "reason": "dregg-cave-node binary not found — set "
                      "HELM_CHAT_NODE_BIN or install to ~/.local/bin"}


def bin_path():
    """The node binary bin_resolution names; None when it names none."""
    return bin_resolution()["path"]


def record_binary(b):
    """Record `b` as the binary `up` installed, into the 0600 state beside
    the node credential. Returns None, or why it could not be recorded."""
    try:
        rec = {"path": b, "sha256": file_sha256(b)}
        st = state()
        if st.get(BIN_RECORD) != rec:
            st[BIN_RECORD] = rec
            write_state(st)
    except OSError as e:
        return "%s: %s" % (e.__class__.__name__, e)
    return None


def unit_binary():
    """The binary the installed unit's ExecStart names; None without a unit."""
    try:
        with open(unit_path(), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"^ExecStart=(\S+)", text, re.M)
    return m.group(1) if m else None


def binary_report():
    """(level, line) naming the binary the unit runs and where it came from,
    the one line `status` and `doctor` both print; level is "ok", "warn" or
    "fail". None when nothing resolves and nothing is recorded: a host that
    reaches a node elsewhere has no local binary to name."""
    res = bin_resolution()
    if res["path"] is None:
        if res["source"] == "recorded":
            return "fail", "node binary REFUSED — " + res["reason"]
        return None
    want = install_path(res["path"])
    ran = unit_binary()
    if ran and ran != want:
        return "warn", ("the unit runs %s, but the next `helm chat node up` "
                        "installs %s (%s)"
                        % (ran, want, BIN_SOURCES[res["source"]]))
    return "ok", "node binary %s (%s)" % (want, BIN_SOURCES[res["source"]])


def helm_bin():
    """The stable helm entrypoint a generated unit may name — never a
    worktree's PATH entry, since a persistent unit must outlive a disposable
    checkout (the autocompact timer resolves itself the same way)."""
    return os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")


def identity_dir():
    """<helm-home>/_global/.state/chat-node-identity — the node's DURABLE half.

    THE CAVE STAYS RAM-HOT: one-cave-per-team keeps the ledger on tmpfs and
    a2a-ram-only-disk-log-after keeps the send/read path off disk. But a node
    has two halves and only one of them is data. `dregg.redb` IS the RAM-hot
    room and it is SUPPOSED to evaporate on reboot. `node.key` is the node's
    IDENTITY, and an identity that changes every boot is not a room that
    forgets — it is a different node wearing the room's name.

    So what is snapshotted here is 32 bytes of key material and a seed, NEVER
    the ledger. Messages still exist only in RAM. What survives is WHO the
    node is, which is the same category as the node passphrase already stored
    beside it at 0600, and as the unit file itself.

    Bug class `durable-identity-in-ephemeral-store`, three instances measured
    on 2026-07-28 alone: the seat register keyed a durable seat on an
    ephemeral orca handle (fixed by `helm seat rebind`, a40a9f1); this data
    dir had no re-init across a boot (c1bd1b9); and the identity inside it had
    no reconstruction path at all (here). Each is a durable thing keyed on an
    ephemeral one, and each presented as something else entirely."""
    return os.path.join(home.global_dir(), ".state", "chat-node-identity")


def _same_bytes(path, blob):
    try:
        with open(path, "rb") as f:
            return f.read() == blob
    except OSError:
        return False


def snapshot_identity(data_dir=DATA_DIR):
    """Copy the cave's chain descriptor to disk (0600). Returns (saved, err).

    Content-addressed and idempotent: an unchanged file is not rewritten, so
    calling this on every `node up` costs nothing and never churns mtimes.
    Files absent from the cave are skipped, not invented — and the store is
    never copied (descriptor_files).

    A DIFFERENT node.key NEVER SILENTLY REPLACES THE SNAPSHOT'S. The snapshot
    may be the only copy of a validator key; before a differing key is
    written over it, the whole snapshot is archived beside itself
    (identity_dir() + ".replaced-<ts>"), exactly as the successor ceremony
    archives the identity it replaces."""
    d = identity_dir()
    saved = []
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        names = descriptor_files(data_dir)
        old_key = os.path.join(d, "node.key")
        if "node.key" in names and os.path.isfile(old_key):
            with open(os.path.join(data_dir, "node.key"), "rb") as f:
                live_key = f.read()
            if not _same_bytes(old_key, live_key):
                shutil.copytree(d, "%s%s%d" % (d, REPLACED_INFIX,
                                               int(time.time())))
        for name in _install_order(names, "node.key"):
            src = os.path.join(data_dir, name)
            with open(src, "rb") as f:
                blob = f.read()
            dst = os.path.join(d, name)
            if _same_bytes(dst, blob):
                continue
            tmp = dst + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.replace(tmp, dst)
            saved.append(name)
    except OSError as e:
        return saved, "identity snapshot failed: %s" % e
    return saved, None


def _install_order(names, key):
    """`names` with the node key FIRST: a copy that stops part way has
    written the one file a node cannot run without before any other."""
    return [n for n in names if n == key] + [n for n in names if n != key]


def _install_file(src, dst):
    """Copy one descriptor file to a path that must not exist yet (0600)."""
    with open(src, "rb") as f:
        blob = f.read()
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)


def _swap_in(stage, data_dir):
    """Put a complete staged dir in place of an absent or EMPTY data dir.
    rename(2) replaces an empty directory atomically and refuses a non-empty
    one, so a data dir is only ever absent/empty or whole."""
    os.rename(stage, data_dir)


def _install(data_dir, src_dir, key):
    """Install `src_dir`'s descriptor into an absent or empty `data_dir`,
    ATOMICALLY: assembled in a sibling dir on the data dir's own filesystem
    and renamed onto it only once complete, with `key` installed as node.key.
    Returns the installed names. On any failure the data dir is untouched
    (still absent or empty) and the sibling is removed.

    WHY. A per-file move into the data dir that failed part way (ENOSPC on
    the ninth of ten files, or systemd's SIGTERM) left a dir holding
    genesis.json and no node.key; the next start called it live, and a
    rebased node with a genesis and no key GENERATES one — a stranger — which
    the next `up` then snapshotted over the only copy of the real key.

    THE DATA DIR MUST NOT BE A MOUNT POINT. The stage dir lives in its
    parent, and rename(2) cannot replace a mount point (EBUSY) or cross into
    another filesystem (EXDEV). That fails loudly and keeps the snapshot
    intact — never a key loss — and the shipped unit's data dir is a plain
    subdirectory of /dev/shm, so it is unaffected."""
    names = descriptor_files(src_dir)
    parent = os.path.dirname(os.path.abspath(data_dir))
    os.makedirs(parent, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=os.path.basename(data_dir) + STAGE_INFIX,
                             dir=parent)
    try:
        for name in _install_order(names, key):
            _install_file(os.path.join(src_dir, name), os.path.join(
                stage, "node.key" if name == key else name))
        _swap_in(stage, data_dir)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return ["node.key" if n == key else n for n in names]


def restore_identity(data_dir=DATA_DIR):
    """Copy a snapshotted identity INTO a cave. Returns (restored, err).

    Into an absent or empty cave the whole snapshot is installed atomically
    (_install): the cave is either untouched or complete, never a genesis
    without its key. Into a cave that already holds files, only the missing
    ones are added, node.key first — and a file already present is NEVER
    overwritten: a live node's own identity outranks the snapshot, so a
    restore can never re-key something that is running.

    A SNAPSHOT WITHOUT node.key IS NOT AN IDENTITY, and is refused rather
    than installed. Measured by review: a snapshot holding the descriptor
    but no key was installed whole and reported as "the same node across the
    reboot" — the keyless data dir prepare's own fence exists to catch, one
    step after that fence ran. A rebased node started on it GENERATES a key
    (node/src/state.rs, first run), and the next `up` snapshotted that
    stranger: with no saved node.key to compare, neither the DISAGREES gate
    nor the archive-before-overwrite could fire."""
    d = identity_dir()
    names = descriptor_files(d)
    if not names:
        return [], None
    if "node.key" not in names:
        return [], ("the identity snapshot at %s holds %s but no node.key — "
                    "refusing to restore a keyless descriptor (a node started "
                    "on it mints a stranger key, and the next `up` would "
                    "snapshot that stranger as this node). Put node.key back "
                    "beside them, or move the snapshot aside deliberately so "
                    "prepare mints a new node" % (d, ", ".join(names)))
    try:
        if not (os.path.isdir(data_dir) and os.listdir(data_dir)):
            return _install(data_dir, d, "node.key"), None
    except OSError as e:
        return [], "identity restore failed: %s" % e
    restored = []
    try:
        for name in _install_order(names, "node.key"):
            dst = os.path.join(data_dir, name)
            if os.path.exists(dst):
                continue
            _install_file(os.path.join(d, name), dst)
            restored.append(name)
    except OSError as e:
        return restored, "identity restore failed: %s" % e
    return restored, None


def identity_state(data_dir=DATA_DIR):
    """{saved, live, matched} — the owner-facing answer to 'will this node come
    back as itself?'. `matched` is False when a snapshot exists but the running
    cave holds a DIFFERENT key, which is the one state that looks fine and is
    not: the next reboot would restore a stranger."""
    d = identity_dir()
    saved = descriptor_files(d)
    live = descriptor_files(data_dir)
    matched = True
    for name in saved:
        if name not in live:
            continue
        try:
            with open(os.path.join(d, name), "rb") as f:
                blob = f.read()
        except OSError:
            matched = False
            break
        if not _same_bytes(os.path.join(data_dir, name), blob):
            matched = False
            break
    return {"saved": saved, "live": live, "matched": matched}


# THE ED25519 PUBLIC KEY OF A 32-BYTE SEED (RFC 8032 section 5.1.5), and
# nothing else — no signing, no verification. It exists so the genesis patch
# can say which cell faucet.key CONTROLS instead of trusting a constant; the
# arms pin it to RFC 8032 test vector 1 and to a real minted faucet key.
_ED_P = 2 ** 255 - 19
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P


def _ed_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _ED_P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _ED_P
    c = 2 * p[3] * q[3] * _ED_D % _ED_P
    d = 2 * p[2] * q[2] % _ED_P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_base():
    y = 4 * pow(5, _ED_P - 2, _ED_P) % _ED_P
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_P - 2, _ED_P) % _ED_P
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P:
        x = x * pow(2, (_ED_P - 1) // 4, _ED_P) % _ED_P
    x = _ED_P - x if x & 1 else x
    return (x, y, 1, x * y % _ED_P)


def ed25519_public_key(seed):
    """The 32-byte Ed25519 public key for a 32-byte seed."""
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    q, p = (0, 1, 1, 0), _ed_base()
    while a:
        if a & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        a >>= 1
    zi = pow(q[2], _ED_P - 2, _ED_P)
    x, y = q[0] * zi % _ED_P, q[1] * zi % _ED_P
    return (y | (x & 1) << 255).to_bytes(32, "little")


def faucet_cell_of(genesis, faucet_seed):
    """The id of the ONE initial cell whose public key is faucet_seed's, or
    None — none, or more than one, is not an answer to guess between."""
    pk = ed25519_public_key(faucet_seed).hex()
    ids = [c.get("id") for c in genesis.get("initial_cells") or []
           if isinstance(c, dict) and str(c.get("public_key", "")).lower() == pk]
    return ids[0] if len(ids) == 1 and isinstance(ids[0], str) else None


def patch_genesis(path):
    """Point the fee well of the genesis.json at `path` at the faucet cell and
    mark EmitEvent-only turns fee-exempt, in place and atomically, keeping its
    mode. Returns an error string or None.

    The faucet cell is the initial cell carrying the public key of the
    faucet.key BESIDE the genesis (faucet_cell_of). No faucet.key, or no
    single cell carrying its key, REFUSES: a fee well naming a cell the chain
    does not have would route every fee nowhere, silently."""
    try:
        with open(path, encoding="utf-8") as f:
            g = json.load(f)
        mode = os.stat(path).st_mode & 0o777
    except (OSError, ValueError) as e:
        return "genesis patch: cannot read %s (%s)" % (path, e)
    if not isinstance(g, dict):
        return "genesis patch: %s is not a JSON object" % path
    key = os.path.join(os.path.dirname(path), FAUCET_KEY)
    try:
        with open(key, "rb") as f:
            seed = f.read()
    except OSError as e:
        return ("genesis patch: refusing — no readable %s beside %s (%s), so "
                "which cell is the faucet cannot be told"
                % (FAUCET_KEY, path, e.strerror or e))
    cell_id = faucet_cell_of(g, seed) if len(seed) == 32 else None
    if not cell_id:
        return ("genesis patch: refusing — no single initial cell in %s "
                "carries the public key of %s, so pointing the fee well at a "
                "faucet cell would be a guess" % (path, FAUCET_KEY))
    g.update((GENESIS_PATCH_EXEMPT, ("fee_well", cell_id)))
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(g, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        return "genesis patch: cannot write %s (%s)" % (path, e)
    return None


def mints_genesis(binary):
    """Does this node binary run the successor ceremony — `genesis
    --reuse-validator-keys-from`? That is the rebased build, the one that
    needs a genesis.json to start. Asked through `genesis --help`, which the
    CLI parser answers before any node code runs. False for a binary that
    cannot answer: a fee-loop build then keeps its genesis-less identity."""
    try:
        p = subprocess.run([binary, "genesis", "--help"], capture_output=True,
                           text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "--reuse-validator-keys-from" in p.stdout


def _run_mint(argv):
    """(ok, reason) for one mint subprocess (init or the ceremony)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=MINT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "%s failed: %s" % (argv[1], e)
    if p.returncode != 0:
        return False, "%s failed (rc %s): %s" % (
            argv[1], p.returncode, _ANSI.sub("", p.stderr or p.stdout or "")
            .strip()[-300:])
    return True, None


def _identity_archives():
    """Every archived snapshot beside identity_dir() that holds a node.key,
    oldest first: what a ceremony or a differing key set aside."""
    snap = identity_dir()
    parent, base = os.path.split(snap)
    try:
        names = os.listdir(parent)
    except OSError:
        return []

    def when(n):
        # by the archive's timestamp, not its name: `.replaced-` sorts after
        # every `.pre-genesis-` lexically, whatever their ages
        tail = n.rsplit("-", 1)[-1]
        return (int(tail) if tail.isdigit() else 0, n)
    return [os.path.join(parent, n) for n in sorted(names, key=when)
            if n.startswith(base + PRE_GENESIS_INFIX)
            or n.startswith(base + REPLACED_INFIX)
            if os.path.isfile(os.path.join(parent, n, "node.key"))]


def _replace_snapshot(src_dir, key, archive_infix):
    """Make `src_dir`'s descriptor (with `key` as node.key) THE snapshot, so
    that a snapshot exists at every instant. Returns the archive path of the
    snapshot it replaced, or None.

    Written whole into `<snap>.new` (via a staging dir renamed into place, so
    `.new` existing means complete), then swapped: the old snapshot to its
    archive, `.new` to the snapshot, and the archive back if that second
    rename fails. A process killed between the two renames leaves `.new`
    complete and no snapshot, which _recover_snapshot finishes."""
    snap = identity_dir()
    parent = os.path.dirname(snap)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    new = snap + ".new"
    shutil.rmtree(new, ignore_errors=True)
    stage = tempfile.mkdtemp(prefix=os.path.basename(snap) + STAGE_INFIX,
                             dir=parent)
    try:
        for name in _install_order(descriptor_files(src_dir), key):
            _install_file(os.path.join(src_dir, name), os.path.join(
                stage, "node.key" if name == key else name))
        os.rename(stage, new)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    archived = None
    if os.path.isdir(snap) and os.listdir(snap):
        archived = "%s%s%d" % (snap, archive_infix, int(time.time()))
        os.replace(snap, archived)
    elif os.path.isdir(snap):
        os.rmdir(snap)
    try:
        os.replace(new, snap)
    except BaseException:
        if archived:
            os.replace(archived, snap)
        raise
    return archived


def _recover_snapshot():
    """Finish or discard an interrupted _replace_snapshot. A complete `.new`
    with no snapshot is the second rename that did not happen: do it. A
    `.new` beside a snapshot is a write the old snapshot outranks: drop it."""
    snap = identity_dir()
    new = snap + ".new"
    if not os.path.isdir(new):
        return None
    if os.path.isdir(snap):
        shutil.rmtree(new, ignore_errors=True)
        return None
    os.replace(new, snap)
    return "finished an interrupted snapshot swap into %s" % snap


def _sweep_stale_mints(data_dir):
    """Remove scratch that a killed prepare left behind: mint dirs beside the
    snapshot and half-assembled data dirs beside the data dir, once older
    than the unit's start timeout (so never a running prepare's own). Each
    can hold a copy of a key; by construction none is ever its only copy —
    the snapshot is written before a data dir is, and a ceremony's key is
    the snapshot's. Returns the paths removed."""
    swept = []
    now = time.time()
    snap = identity_dir()
    places = ((os.path.dirname(snap), (MINT_DIR_PREFIX,
                                       os.path.basename(snap) + STAGE_INFIX)),
              (os.path.dirname(os.path.abspath(data_dir)),
               (os.path.basename(data_dir) + STAGE_INFIX,)))
    for parent, prefixes in places:
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for n in names:
            p = os.path.join(parent, n)
            if not n.startswith(prefixes) or not os.path.isdir(p):
                continue
            try:
                if now - os.path.getmtime(p) < START_TIMEOUT_S:
                    continue
            except OSError:
                continue
            shutil.rmtree(p, ignore_errors=True)
            swept.append(p)
    return swept


def _check_minted(out, keep=None):
    """The mint output is a startable descriptor: a node key (node.key from
    init, node-0.key from the ceremony), byte-identical to `keep` when given,
    and a patched genesis. Returns (key name, error)."""
    names = descriptor_files(out)
    key = "node.key" if "node.key" in names else "node-0.key"
    if key not in names:
        return None, ("the mint reported success but wrote no node.key — "
                      "refusing to start a keyless node")
    if keep is not None and not _same_bytes(os.path.join(out, key), keep):
        return None, ("the successor ceremony did not keep the node key (its "
                      "%s differs from the snapshot) — refusing a re-keyed "
                      "node" % key)
    if GENESIS_FILE in names:
        err = patch_genesis(os.path.join(out, GENESIS_FILE))
        if err:
            return None, err
    elif keep is not None:
        return None, "the successor ceremony wrote no genesis.json"
    return key, None


def _mint(data_dir, argv_for, keep, archive_infix):
    """Run one mint into scratch on disk, make its output THE snapshot, then
    install it into the data dir. Returns (archived snapshot or None, err).

    THE ORDER IS THE SAFETY. The snapshot holds the new descriptor BEFORE the
    data dir receives it, so a key never lives only in the scratch dir the
    `finally` deletes, and an install that fails leaves the data dir empty
    with a snapshot the next start restores whole."""
    state = os.path.dirname(identity_dir())
    os.makedirs(state, mode=0o700, exist_ok=True)
    work = tempfile.mkdtemp(prefix=MINT_DIR_PREFIX, dir=state)
    out = os.path.join(work, "out")
    try:
        ok, why = _run_mint(argv_for(work, out))
        if not ok:
            return None, why
        key, err = _check_minted(out, keep)
        if err:
            return None, err
        archived = _replace_snapshot(out, key, archive_infix)
        try:
            _install(data_dir, out, key)
        except OSError as e:
            return archived, ("install into %s failed (%s) — the data dir is "
                              "untouched and the snapshot at %s holds the new "
                              "descriptor, so the next start restores it"
                              % (data_dir, e, identity_dir()))
        return archived, None
    except OSError as e:
        return None, "mint failed: %s" % e
    finally:
        shutil.rmtree(work, ignore_errors=True)


ROLLBACK = ("ROLLBACK to the fee-loop identity: `helm chat node down`; move "
            "the data dir aside; `mv {snap} {snap}.genesis-$(date +%s)`; "
            "`mv {archived} {snap}`; then `HELM_CHAT_NODE_BIN=<the fee-loop "
            "binary> helm chat node up` — prepare restores the fee-loop "
            "identity, and the old binary never sees a genesis it was not "
            "built for")


def _successor_ceremony(data_dir, binary):
    """Mint a genesis around the SNAPSHOTTED node key. Returns (msg, err).

    `genesis --validators 1 --reuse-validator-keys-from K --output O` keeps
    the validator key and the federation id (measured: 150 s). The old key
    goes in as K/node-0.key and comes back as O/node-0.key, installed as
    node.key. The fee-loop node's own agent key and starbridge seed are NOT
    carried over — the ceremony mints agent keys and the node writes the
    seed — and the old snapshot is archived beside the new one, never
    deleted; ROLLBACK names the way back."""
    path = os.path.join(identity_dir(), "node.key")
    try:
        with open(path, "rb") as f:
            key = f.read()
    except OSError as e:
        return None, ("cannot read the snapshotted node key %s (%s) — "
                      "refusing to mint without it" % (path, e.strerror or e))

    def argv_for(work, out):
        keys = os.path.join(work, "keys")
        os.mkdir(keys, 0o700)
        fd = os.open(os.path.join(keys, "node-0.key"),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return [binary, "genesis", "--validators", "1",
                "--reuse-validator-keys-from", keys, "--output", out]
    archived, err = _mint(data_dir, argv_for, key, PRE_GENESIS_INFIX)
    if err:
        return None, err
    return ("minted a genesis around the same node key (successor ceremony), "
            "snapshotted it at %s, and archived the fee-loop identity at %s. "
            % (identity_dir(), archived)
            + ROLLBACK.format(snap=identity_dir(), archived=archived)), None


PREPARE_LOCK = "chat-node-prepare.lock"   # beside the snapshot, on disk


def prepare_lock_path():
    return os.path.join(os.path.dirname(identity_dir()), PREPARE_LOCK)


def prepare(data_dir=DATA_DIR, binary=None):
    """ExecStartPre: guarantee a STARTABLE data dir. Returns (msg, err).

    ONE PREPARE AT A TIME, AND A SECOND ONE REFUSES. Two at once (the unit's
    ExecStartPre and a hand-run `helm chat node prepare`, or two `up`s) cannot
    lose a key, but they can each mint and leave the data dir on one genesis
    and the snapshot on the other. So the whole run holds an flock on
    prepare_lock_path(); a prepare that finds it held refuses at once, naming
    the lock, and never waits: a waiting ExecStartPre would stall the unit's
    start behind a mint it cannot see. The kernel drops the lock when its
    holder exits, however it exits, so no stale lock can outlive a prepare."""
    import fcntl
    lock = prepare_lock_path()
    try:
        os.makedirs(os.path.dirname(lock), mode=0o700, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        return None, "cannot open the prepare lock %s: %s" % (lock, e)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None, ("another prepare holds %s — refusing to run a second "
                          "at once (two can leave the data dir and the snapshot "
                          "on different genesis files). Let it finish; the lock "
                          "frees itself when that prepare exits" % lock)
        return _prepare_locked(data_dir, binary)
    finally:
        os.close(fd)


def _prepare_locked(data_dir, binary):
    """prepare's body, run only under the prepare lock: guarantee a STARTABLE
    data dir without ever re-keying one.

    Replaces the generator's `test -d || init` one-liner, which carried two
    defects a shell conditional cannot express:

    1. IT MINTED A NEW IDENTITY EVERY BOOT. Correct as a crash-loop fix (47
       restarts on this host, ~1827 across the fleet, measured the morning of
       2026-07-28) and wrong as a continuity policy: /dev/shm dies on every
       reboot, so every reboot produced a new node key and the team's node came
       back a stranger. The owner saw the symptom as "old data restored in
       chat, but no new dregg turns".
    2. `init` NO-OPS ON AN EXISTING-BUT-EMPTY DIR AND STILL EXITS 0 (probed
       against the binary, 2026-07-28). Anything that pre-creates the data dir
       makes `test -d` true, skips the init and leaves a KEYLESS cave that
       `run` refuses — while every exit code says success. `_up` did exactly
       that, one line before starting the unit.

    The ordering IS the fix: restore into a dir we create ourselves, and call
    `init` only when there is nothing to restore — into a fresh path it
    creates itself, so the empty-dir trap is unreachable by construction.

    AND IT KNOWS ABOUT GENESIS. A rebased node exits 1 without a genesis.json,
    so restoring node.key alone yields a node that never starts. Three cases:
    a snapshot that carries a genesis restores whole; a snapshot that is a
    fee-loop identity (node.key, no genesis) meets a rebased binary with the
    successor ceremony, which keeps the key; and nothing to restore means
    `init`, patched when it wrote a genesis. A fee-loop binary still restores
    its genesis-less identity exactly as before.

    AND IT NEVER LOSES THE KEY. Every mint makes its output the snapshot
    before the data dir receives it, and every install is atomic (_install);
    a data dir holding files but no node.key is moved aside rather than
    called live (a node started on it would mint a stranger key); a snapshot
    holding files but no node.key is refused, never restored (the same
    stranger, made by prepare itself — restore_identity); and with no
    node.key in the snapshot but one in an archive beside it, it refuses to
    mint rather than bury that key under a new one."""
    notes = ["swept stale mint scratch %s" % p
             for p in _sweep_stale_mints(data_dir)]
    try:
        rec = _recover_snapshot()
    except OSError as e:
        return None, "could not recover an interrupted snapshot swap: %s" % e
    if rec:
        notes.append(rec)
    if os.path.isdir(data_dir) and os.listdir(data_dir):
        if os.path.isfile(os.path.join(data_dir, "node.key")):
            return "data-dir live at %s — untouched" % data_dir, None
        aside = "%s%s%d" % (data_dir, PARTIAL_INFIX, int(time.time()))
        try:
            os.rename(data_dir, aside)
        except OSError as e:
            return None, ("the data dir %s holds files but no node.key, and "
                          "could not be moved aside (%s) — refusing to start "
                          "a node that would mint a stranger key" % (data_dir, e))
        notes.append("moved a data dir holding no node.key aside to %s — a "
                     "node started on it mints a stranger key" % aside)

    def said(msg):
        return "; ".join(notes + [msg])
    snap = descriptor_files(identity_dir())
    if "node.key" not in snap:
        archives = _identity_archives()
        if archives:
            return None, ("the snapshot at %s holds no node.key, but %s does — "
                          "refusing to mint a new identity over an archived "
                          "one. Put it back (`mv %s %s`) or move the archive "
                          "aside deliberately, then start again"
                          % (identity_dir(), archives[-1], archives[-1],
                             identity_dir()))
    res = {"path": binary} if binary else bin_resolution()
    b = res["path"]
    if "node.key" in snap and GENESIS_FILE not in snap \
            and b and mints_genesis(b):
        msg, err = _successor_ceremony(data_dir, b)
        return (said(msg) if msg else None), err
    restored, err = restore_identity(data_dir)
    if err:
        return None, err
    if restored:
        return said("restored node identity (%s) into %s — same node across "
                    "the reboot" % (", ".join(restored), data_dir)), None
    if not b:
        return None, ("no node binary and no identity snapshot to restore: "
                      + res["reason"])
    # Nothing to restore: `init` mints into a path that does not exist yet
    # (its silent no-op on an existing dir is unreachable), and the data dir
    # receives the result only once the snapshot holds it.
    _archived, err = _mint(data_dir, lambda _work, out: [
        b, "init", "--data-dir", out], None, REPLACED_INFIX)
    if err:
        return None, err
    return said("minted a new node identity and snapshotted it (%s) — this "
                "boot starts fresh, the next one will not"
                % ", ".join(descriptor_files(identity_dir()))), None


def unit_path():
    return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", UNIT)


# WHERE THE MACHINE DECLARES ITS CONSENSUS POSTURE, and not knowing about it is
# why this node had never once started here.
#
# dregg refuses to boot when `lean_available()` is false — it will not "serve as
# if verified while running the un-verified executor", which is exactly the
# discipline that makes it trustworthy. The escape hatch is the operator's to
# open, via DREGG_ALLOW_UNVERIFIED_CONSENSUS / DREGG_ALLOW_UNAUDITED_PQ. On this
# machine the operator HAS opened it, for the team cave node, in a systemd
# drop-in — the standard way to state machine-local posture without editing a
# shipped unit. Our unit is the SAME BINARY on the SAME machine for the SAME
# operator, and it carried no Environment= lines at all, so it hit the gate and
# restart-looped while helm reported a timeout.
#
# So: MIRROR the operator's existing declaration, never mint one. If they have
# declared nothing, we write nothing and dregg's refusal stands — helm must not
# be the layer that quietly opts a node out of verification. This module's
# docstring already claimed "precedent: the team node's dregg-cave.service"; it
# mirrored the unit and missed the drop-in, which is where the precedent lived.
PEER_UNIT = "dregg-cave.service"


def _dropin_dir(unit):
    return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user",
                        unit + ".d")


def declared_posture(unit=PEER_UNIT):
    """[(source_path, "DREGG_X=1")] — DREGG_* Environment= lines the operator
    declared for `unit`, read from its systemd drop-ins.

    Only DREGG_-prefixed names are mirrored. A drop-in can hold anything, and
    copying arbitrary Environment= lines into a second unit would propagate
    unrelated (possibly secret-bearing) values into another file on disk. The
    consensus posture flags are what gate startup, so they are what we carry.

    PARSED PER ASSIGNMENT, NOT PER LINE, and the first version got this wrong in
    a way a live run reproduced. systemd's Environment= takes N whitespace-
    separated, optionally-quoted assignments on ONE line, so

        Environment=DREGG_ALLOW_UNVERIFIED_CONSENSUS=1 AWS_SECRET_ACCESS_KEY=x

    is two assignments. Checking `whole_string.startswith("DREGG_")` accepted
    that line and mirrored it WHOLE — writing the secret into a second 0600 file
    on disk, which is precisely the leak the filter exists to prevent. A filter
    that validates the first token of an N-token grammar is not a filter. shlex
    gives the same quote-aware splitting systemd does.
    """
    import shlex
    found = []
    d = _dropin_dir(unit)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".conf"))
    except OSError:
        return found
    for n in names:
        p = os.path.join(d, n)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln.startswith("Environment="):
                continue
            try:
                parts = shlex.split(ln.split("=", 1)[1].strip())
            except ValueError:      # unbalanced quotes: parse nothing, mirror
                continue            # nothing — never guess at a malformed line
            for assign in parts:
                name, sep, _val = assign.partition("=")
                if sep and name.startswith("DREGG_"):
                    found.append((p, assign))
    return found


def write_posture_dropin(posture, unit=UNIT):
    """Mirror `posture` into <unit>.d/10-helm-posture.conf.

    Returns the path written, the path REMOVED (as a withdrawal), or None when
    there was nothing to do.

    EMPTY POSTURE HAS TWO MEANINGS and conflating them was a real hole, found
    and reproduced live. "Never declared" must write nothing — dregg's
    refusal is the correct outcome. "WITHDRAWN", where the operator deleted the
    declaration we previously mirrored, must CLEAR the mirror: otherwise our
    copy keeps granting the opt-out after the grant was rescinded, and the node
    goes on running unverified on an authority that no longer exists. The
    difference between the two is simply whether our mirror is already on disk.

    The first version returned None for both, so a withdrawal silently left the
    stale grant in place — and the comment written INTO the file promised
    "remove the source declaration and this file becomes empty on the next node
    up", which the code did not do. A false guarantee in a generated file is
    worse than no comment: it is the thing a future reader checks INSTEAD of the
    code. Both the behaviour and the sentence are fixed here.
    """
    d = _dropin_dir(unit)
    p = os.path.join(d, "10-helm-posture.conf")
    if not posture:
        if os.path.exists(p):
            os.remove(p)            # WITHDRAWN — the grant must not outlive it
            return p
        return None                 # never declared — correct to write nothing
    os.makedirs(d, exist_ok=True)
    srcs = sorted({src for src, _ in posture})
    body = ["# Written by `helm chat node up`. MIRRORED, not decided here:",
            "# these are the operator's own consensus-posture declarations for",
            "# %s, copied so the same binary can start for chat." % PEER_UNIT,
            "# Source: " + ", ".join(srcs),
            "# Withdraw the source declaration and the next `node up` DELETES",
            "# this file — helm never mints a verification opt-out, and never",
            "# outlives one.",
            "[Service]"]
    body += ["Environment=" + a for _src, a in posture]
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(body) + "\n")
    return p


def last_failure(unit=UNIT, lines=40, invocation=None):
    """The service's OWN last words, or None.

    `_up` used to report "the unit started but the API never answered", which is
    a TIMEOUT — a symptom shared by every possible cause. The service had already
    said precisely what was wrong ("REFUSING TO START: lean_available() is false
    ... set DREGG_ALLOW_UNVERIFIED_CONSENSUS=1"), in one actionable sentence, and
    helm replaced it with the least informative true statement available. A
    provisioner that owns a service must relay that service's diagnosis, not
    paraphrase its silence.

    THE NODE COLOURS ITS LOG, AND THE JOURNAL KEEPS THE COLOUR. dregg's tracing
    writes ANSI escapes whether or not stdout is a terminal, and `journalctl -o
    cat` passes them through (measured on this host's helm-chat-node journal),
    so a real error line reads `...Z<ESC>[0m <ESC>[31mERROR<ESC>[0m ...` and
    never contains " ERROR ". Only a REFUSING line was ever found; every other
    refusal went unrelayed. The escapes are stripped before matching, which
    also keeps them off the operator's terminal.

    SCOPED TO ONE RUN WHEN THE RUN IS KNOWN. The last 40 lines of a unit can
    hold an error from a run before the one being asked about (a refusal the
    operator already cured, then a clean stop), and relaying it names a
    failure that is not this run's. With `invocation` (unit_state's
    InvocationID) only that run's lines are read: every line the unit writes
    carries it as _SYSTEMD_INVOCATION_ID. Without it, the unit's last lines.
    """
    scope = (["_SYSTEMD_INVOCATION_ID=" + invocation]
             if invocation and _INVOCATION.fullmatch(invocation)
             else ["-u", unit])
    try:
        p = subprocess.run(["journalctl", "--user"] + scope + [
                            "-n", str(lines), "--no-pager", "-o", "cat"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    hits = [ln for ln in (_ANSI.sub("", raw).strip()
                          for raw in p.stdout.splitlines())
            if "REFUSING" in ln or " ERROR " in ln
            or ln.startswith(PREPARE_FAILED)]
    return hits[-1] if hits else None


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


# THE NODE'S REFUSALS THAT HAVE ONE KNOWN CURE, matched on the node's and the
# signer's own words (dregg, rebased tip). A refusal relayed without its cure
# sends the operator to the source to find it; each row here says what to do.
#
#   store-epoch   persist refuses a store written under an older canonical
#                 schema epoch ("... re-genesis is required"), or a populated
#                 store that records no epoch at all ("... (re-genesis
#                 required)"). It re-genesises rather than reinterpreting.
#   clock-policy  a rebased node will not boot without a genesis.json that
#                 publishes its consensus clock ("blocklace requires
#                 consensus_genesis_unix_seconds + consensus_time_mode ...").
#   pq-identity   the node binds a post-quantum identity from a cell's first
#                 turn; a cell created without a claimable binding is refused.
#   swap-veto     the verified Lean executor rejected a turn the legacy
#                 executor would have accepted. Measured root cause: a signer
#                 that derives the executor federation id from the solo flag
#                 rather than the node's configured committee.
#
# The cave node's disk copy: the node's migration installs a
# dregg-cave-restore helper that copies this directory back into
# /dev/shm/dregg-cave whenever the tmpfs lacks node.key, so archiving only the
# tmpfs store brings the old store straight back on the next boot.
CAVE_RESTORE_DIR = os.path.join("~", ".local", "share", "dregg-cave", "data")
CAVE_TMPFS_DIR = "/dev/shm/dregg-cave"   # the migration's TMPFS_DIR


def _regenesis_remedy(data_dir=DATA_DIR):
    d = data_dir
    ident = identity_state(data_dir)
    out = ("re-genesis: archive the data dir and keep the identity. `helm "
           "chat node down`, move %s aside (`mv %s %s.pre-regenesis-$(date "
           "+%%s)`), then `helm chat node up`" % (d, d, d))
    if ident["saved"]:
        out += (" — prepare restores %s from %s into the fresh dir, so the "
                "node keeps its identity" % (", ".join(ident["saved"]),
                                             identity_dir()))
    else:
        out += (". NO identity snapshot exists at %s yet: copy its chain "
                "descriptor (node.key and every other file but %s) out of %s "
                "before moving it, or the fresh dir mints a new node"
                % (identity_dir(), STORE_PREFIX, d))
    cave = os.path.expanduser(CAVE_RESTORE_DIR)
    if os.path.normpath(data_dir) == CAVE_TMPFS_DIR:
        out += (". This is the cave node (%s): archive %s too and keep only "
                "its identity files there — the dregg-cave-restore helper "
                "the node's migration installed copies that directory "
                "back into %s on every boot, so the old store returns"
                % (PEER_UNIT, cave, CAVE_TMPFS_DIR))
    else:
        out += (". The cave node's store at %s is a different node's and is "
                "untouched by this" % cave)
    return out


def _genesis_remedy(data_dir=DATA_DIR):
    out = ("the data dir %s has no genesis.json, and a rebased node refuses "
           "to invent a consensus clock; a node key alone cannot start it. "
           "prepare mints the chain descriptor around the SAME key (the "
           "successor ceremony, about 150 s) when it finds the dir empty: "
           "`helm chat node down`, move %s aside, then `helm chat node up` "
           "— the snapshot at %s must hold the node.key to keep"
           % (data_dir, data_dir, identity_dir()))
    archives = [a for a in _identity_archives() if PRE_GENESIS_INFIX in a]
    if archives:
        out += ". " + ROLLBACK.format(snap=identity_dir(), archived=archives[-1])
    return out


REFUSALS = (
    ("store-epoch",
     re.compile(r"no canonical state schema epoch|re-genesis (?:is )?required"),
     _regenesis_remedy),
    ("clock-policy",
     re.compile(r"blocklace requires consensus_genesis_unix_seconds"),
     _genesis_remedy),
    ("pq-identity",
     re.compile(r"post-quantum signer identity is neither Cell-committed nor "
                r"independently enrolled"),
     lambda _d=None: (
         "install a signer whose join leaves a claimable stub on a node that "
         "binds the PQ identity from the first turn (dregg client-sign: "
         "\"leave a claimable stub ...\"), then send once per profile: the "
         "first send claims the stub")),
    ("swap-veto",
     re.compile(r"vetoed the commit \(THE SWAP strict mode\)|"
                r"THE SWAP authority inversion"),
     lambda _d=None: (
         "the verified executor refused what the signer built; the measured "
         "cause is a signer on the wrong executor federation id. Install a "
         "signer that reads the federation id from the node's configured "
         "committee (dregg sdk-net: \"read the executor federation id from "
         "the configured committee, not the solo flag\")")),
)


def classify_refusal(text, data_dir=DATA_DIR):
    """{kind, remedy} for a refusal line with a known cure, else None."""
    for kind, pattern, remedy in REFUSALS:
        if text and pattern.search(text):
            return {"kind": kind, "remedy": remedy(data_dir)}
    return None


def refusal_remedy(text, data_dir=DATA_DIR):
    """The cure for a known refusal line, or None."""
    hit = classify_refusal(text, data_dir)
    return hit["remedy"] if hit else None


def unreachable_line(url, d=None):
    """One operator line for a chat node URL that did not answer, carrying
    what systemd and the journal can add: a node still initializing is not
    down, and a refusal with a known cure names it. Shared by `helm chat node
    status` and `helm doctor`, so the two can never disagree. The journal
    line is the SERVICE's text, so it is laundered before it reaches a
    terminal."""
    from . import chat
    d = d or boot_diagnosis(url)
    line = chat._safe_reason(d["line"]) if d["line"] else None
    if d["state"] == "initializing":
        return ("INITIALIZING VERIFIED RUNTIME at %s — the node is running and "
                "has not bound its API yet (a rebased node's Lean init takes "
                "about 150 s); chat posts fall back to [unsigned] until it "
                "answers" % url)
    if d["state"] == "preparing":
        return ("PREPARING at %s — the unit's prepare step is still running; "
                "chat posts fall back to [unsigned] until the node answers"
                % url)
    if d["state"] == "hung":
        return ("NOT INITIALIZING at %s — the node is %s: it is hung, not "
                "booting; `journalctl --user -u %s`, then `helm chat node down` "
                "and `up`" % (url, line, UNIT))
    if d["state"] == "refused":
        return ("REFUSED TO START at %s — the service said: %s — cure: %s"
                % (url, line, d["remedy"]))
    out = "API UNREACHABLE at %s — chat posts fall back to [unsigned]" % url
    if line:
        out += " — the service said: " + line
    return out


def serves_unit(url, port=PORT):
    """Is `url` the address this module's own unit serves? Only then does the
    unit's journal speak for it."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url or "")
        return (u.hostname in ("127.0.0.1", "localhost", "::1")
                and u.port == port)
    except ValueError:
        return False


def hung_diagnosis(st):
    """The `hung` diagnosis for a unit_state, or None when it is not hung: a
    process running (not gone, not in ExecStartPre) for longer than
    hung_after_s without its API answering. The ONE place the hung verdict and
    its words are made, so `up` (wait_boot) and `status`/`doctor`
    (boot_diagnosis) can never disagree about the same unit. The words hold
    for a node that never bound its API and for one that bound it and then
    wedged: either way it runs and does not answer."""
    if not st or _gone(st) or st["sub"] == "start-pre" \
            or st["running_s"] is None or st["running_s"] <= hung_after_s():
        return None
    return {"state": "hung", "remedy": None,
            "line": "running %ds and its API does not answer, past the %ds "
                    "boot wait" % (st["running_s"], hung_after_s())}


def boot_diagnosis(url):
    """Why a chat node URL is not answering, as far as systemd and the
    journal can say. {state, line, remedy} with state one of:

      initializing  the unit's process is running and has not bound its API —
                    a rebased node's verified-runtime init, not a failure;
      hung          the process has run longer than hung_after_s (the boot
                    wait, floored at its default) without binding its API —
                    not booting;
      preparing     ExecStartPre (prepare) is still running;
      refused       this run exited and its last words name a known refusal:
                    `remedy` is the cure;
      down          the process exited (line: this run's last error, if any),
                    was stopped cleanly (Result=success, no line: a clean
                    stop is not a refusal, whatever an earlier run logged),
                    or is being stopped (`deactivating`: not hung, going);
      unknown       helm cannot tell (not this unit's URL, or no systemd)."""
    if not serves_unit(url):
        return {"state": "unknown", "line": None, "remedy": None}
    st = unit_state(UNIT)
    if st is None:
        return {"state": "unknown", "line": None, "remedy": None}
    if not _gone(st):
        if st["sub"] == "start-pre":
            return {"state": "preparing", "line": None, "remedy": None}
        return (hung_diagnosis(st)
                or {"state": "initializing", "line": None, "remedy": None})
    if st["result"] == "success":
        return {"state": "down", "line": None, "remedy": None}
    line = last_failure(invocation=st["invocation"])
    cure = refusal_remedy(line)
    return {"state": "refused" if cure else "down", "line": line,
            "remedy": cure}


def unit_text(binary, data_dir=DATA_DIR, port=PORT, gossip=GOSSIP_PORT):
    """The unit body — mirrors the team node's dregg-cave.service, tmpfs
    data-dir + its own ports. Faucet ON: chat turns must never die on
    computrons (the provisioning exhaustion lesson).

    ExecStartPre PREPARES THE DATA DIR, because tmpfs REQUIRES it. /dev/shm is
    wiped on every reboot, so the data dir this unit was pointed at does not
    survive one — and `run` refuses to create it: the binary exits 1 with
    "data directory does not exist ... Run `dregg-node init` first". The
    generator never emitted any prepare step, so after any reboot the unit
    could only ever fail. Measured 2026-07-28, the morning after a reboot: 47
    NRestarts on this host and ~1827 across the fleet, and every
    node_unreachable DEGRADED line in the chat log traced here.

    TMPFS STAYS — this is the fix, not a workaround. one-cave-per-team is
    explicit that the cave is RAM-hot with disk only as the after-log, and
    a2a-ram-only-disk-log-after says the send/read path never touches disk.
    Moving to a durable data dir would trade a two-line unit bug for a
    violation of the topology law. What tmpfs demands is that recreation be
    automatic, which is precisely what was missing.

    THE PREPARE STEP IS A HELM VERB, NOT A SHELL CONDITIONAL. The first fix
    here was `test -d || init`, which restarted the node but re-keyed it on
    every boot and could not survive an empty dir (see prepare()). Neither
    defect is expressible — or testable — in a one-line Exec conditional; both
    are covered by tests now that the logic lives in Python. helm_bin() rather
    than PATH so a disposable worktree can never own a persistent unit.

    ExecStartPre failure is FATAL by design (no `-` prefix): a node that
    cannot prove which node it is must not start. The alternative is a stranger
    node silently serving the team's room.

    StartLimitIntervalSec=60 FIXES A RATE LIMITER THAT COULD NEVER FIRE. The
    default interval is 10s and RestartSec is 3, so at most ~3 starts fit in a
    window and StartLimitBurst=5 was unreachable — the unit looped forever
    instead of entering `failed`, which is why 1827 restarts were SILENT. At
    60s, five restarts three seconds apart land well inside the window and the
    limit trips, so a genuinely broken node stops and says so. A crash loop
    nobody can see is worse than a dead service everyone can.

    PREPARE IS TOLD WHICH BINARY, and the start may take minutes. On a
    rebased node prepare may run the genesis mint (about 150 s), and it runs
    under systemd, which carries no HELM_CHAT_NODE_BIN: without `--bin` an
    override chosen at `up` never reached the mint, which then used whatever
    the default resolution found. systemd's default start timeout (90 s)
    would kill that mint half way, so TimeoutStartSec covers it."""
    return """[Unit]
Description=helm chat node (dregg tmpfs node :%d)
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
ExecStartPre=%s chat node prepare --data-dir %s --bin %s
ExecStart=%s run --data-dir %s --port %d --gossip-port %d --enable-faucet
Environment=RUST_LOG=info,dregg_node::blocklace_sync=warn
TimeoutStartSec=%d
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
""" % (port, helm_bin(), data_dir, binary,
       binary, data_dir, port, gossip, START_TIMEOUT_S)


def state_path():
    return os.path.join(home.global_dir(), ".state", "chat-node.json")


def state():
    return pk.read_json(state_path(), {}) or {}


def write_state(d):
    """0600 FROM BIRTH: the tmp is created (and fchmod'd, covering a stale
    leftover) before the credential bytes land — pk.write_json's umask-mode
    tmp would expose the passphrase/token for a window on a shared host."""
    import json
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def default_url():
    return "http://127.0.0.1:%d" % PORT


def _systemctl(*args):
    """(rc, out+err) — systemd absent degrades to a loud reason, no traceback."""
    try:
        p = subprocess.run(["systemctl", "--user"] + list(args),
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "systemctl unavailable: %s" % exc
    return p.returncode, (p.stdout + p.stderr).strip()


def unlock(url, passphrase):
    """POST the unlock (first unlock SETS the passphrase). Returns (token,
    None) or (None, reason)."""
    r = cell.post_json(url + "/api/cipherclerk/unlock", {"passphrase": passphrase})
    if not isinstance(r, dict):
        return None, "unlock unreachable at %s" % url
    if r.get("success") is not True:
        return None, "unlock refused: %s" % (r.get("error") or "success was not true")
    token = r.get("bearer_token")
    if not isinstance(token, str):
        return None, "unlock accepted without a string bearer_token"
    return token, None


def bootstrap_cell_hex():
    """A deterministic throwaway recipient for the first-block faucet turn —
    NEVER a real profile's cell (a stub-materialized cell cannot sign)."""
    return hashlib.blake2b(b"helm-chat-node-bootstrap", digest_size=32).hexdigest()


# ── THE FAUCET IS A CELL, NOT A MINT ──────────────────────────────────────
#
# `POST /api/faucet` cannot create value: node/src/api.rs says so in as many
# words ("This endpoint NEVER creates it: value enters only by genesis
# issuer-moves"). It signs a Transfer OUT OF one cell, and on a genesis-less
# cave that cell is also the fee well, so the whole devnet supply circulates
# between the agents and it. When it reaches zero every funded turn fails and
# every surface that promises an "auto-faucet refund on the next post" is
# promising a cure the node cannot perform.
#
# A funded turn's cost is what makes a balance dry, not zero: the faucet still
# HELD 260 computrons while refusing a grant that needed 697.
FAUCET_DRY_BELOW = 1500     # one funded turn (measured: a 73B digest send ~1442)
REFUEL_VERB = "helm chat node refuel"

# ── LOW COMES BEFORE DRY ──────────────────────────────────────────────────
#
# DRY was the only state any surface could name, and it is named by the
# refusal that has already degraded every seat's signed turn. Measured on
# the live node: a refuel left the faucet at 8260, it paid out 7667 — eleven
# times the 697 one grant turn costs — and was refusing again within ten
# minutes. So the faucet reads LOW while it can still pay K more grants, the
# warning names how many are left and the refuel that restores it, and one
# wake per low spell reaches the seats that can act before the refusal does.
FAUCET_LOW_GRANTS = 5
#: One grant turn as the live node charged it: the refusal's `need`, and the
#: metered cost of the one Transfer receipt the refuel produced. Used until a
#: refusal at THIS node records a larger one.
GRANT_DEFAULT = 697
#: A refuel signs a Transfer, and the signer tops the SENDING cell up from the
#: faucet for that turn's fee. Moving a source's whole balance therefore asks
#: the dry faucet to pay for its own refill, which is how the first live refuel
#: failed. This many fees stay behind in the source by default.
FEE_MARGIN_GRANTS = 2
LOW_GRANTS_ENV = "HELM_CHAT_FAUCET_LOW_GRANTS"
OWNER_ENV = "HELM_CHAT_NODE_OWNER"
WAKE_BOT = "faucet-watch"   # the author a low-faucet wake is posted as
#: The marker chat's diagnostic looks for to tell a DRY SOURCE from a rate
#: limit, a wrong recipient or an unreachable node.
FAUCET_DRY_MARK = "faucet SOURCE dry"

_INSUFFICIENT = re.compile(
    r"insufficient balance on cell ([0-9a-fA-F]{8,64})\s*:\s*"
    r"need\s+(\d+)\s*,\s*have\s+(\d+)")


def parse_insufficient(text):
    """(cell, need, have) from dregg's TurnError::InsufficientBalance, else None.

    THE CELL IS ABBREVIATED. dregg renders a CellId as 16 hex characters in
    this message and in the fee-well log line alike, so every faucet id helm
    can observe is a PREFIX and has to be resolved against the node before it
    can be read back."""
    m = _INSUFFICIENT.search(str(text or ""))
    if not m:
        return None
    return m.group(1).lower(), int(m.group(2)), int(m.group(3))


def faucet_state_path():
    return os.path.join(home.global_dir(), ".state", "chat-faucet.json")


def record_faucet_shortfall(cell_hex, need, have):
    """Keep what the node said when the faucet could not pay.

    THE REFUSAL IS THE ONLY PLACE THE NODE STATES WHAT A FUNDED TURN COSTS, so
    discarding it is why no surface could print a requirement. The LARGEST need
    is kept, not the latest: a dry threshold that drifts down after one cheap
    grant would call a faucet healthy for the turn it just refused.

    Carries no secret: a cell id, two integers and a stamp."""
    path = faucet_state_path()
    prior = pk.read_json(path, {}) or {}
    prior = prior if isinstance(prior, dict) else {}
    need = int(need)
    # THE HIGH WATER MARK BELONGS TO ONE NODE AND ONE SEASON. Keeping the
    # largest need forever, across node identities and cost models, made a
    # rebuilt room read DRY at a balance its own turns never needed: the record
    # is bound to the node it was observed at and expires, so a stale season
    # cannot outlive the node that produced it.
    same = (prior.get("cell") == cell_hex
            and prior.get("node") == node_identity())
    if same and isinstance(prior.get("need"), int) and not _stale_shortfall(prior):
        need = max(need, prior["need"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pk.write_json(path, {"cell": cell_hex, "need": need, "have": int(have),
                         "node": node_identity(), "at": pk.now_ts()})


SHORTFALL_TTL_S = 7 * 24 * 3600   # one week: long enough to span a quiet room


def _stale_shortfall(rec):
    """A record with no readable stamp is stale: an observation that cannot say
    WHEN it was made cannot be shown to still describe this season."""
    at = pk.parse_ts_epoch(rec.get("at"))
    if at is None:
        return True
    return (time.time() - at) > SHORTFALL_TTL_S


def node_identity(url=None):
    """This node's own identity, so a recorded observation can say WHICH node
    it was made at. The url is the identity helm can always read; a rebuilt
    cave at the same url is separated by the faucet cell it resolves to."""
    from . import chat
    return (url or chat.node_url() or default_url()).rstrip("/")


def faucet_shortfall():
    """The recorded shortfall AS WRITTEN, whatever node or season it came
    from. Every consumer that reasons about THIS node wants
    `current_shortfall()` instead; this raw reader stays for the writer and
    for anything that needs to see a record it is about to replace."""
    rec = pk.read_json(faucet_state_path(), {})
    return rec if isinstance(rec, dict) else {}


def current_shortfall(url=None):
    """The recorded shortfall IF it describes this node and this season, else
    {}. One door, so every consumer answers the identity question the same
    way: a record observed at another node, or older than SHORTFALL_TTL_S,
    describes neither this faucet's requirement nor its cell."""
    rec = faucet_shortfall()
    # ABSENCE IS NOT PROVENANCE, and admitting a missing stamp as a wildcard
    # was the same mistake one field in: a record written before the stamp
    # existed carries no claim about WHICH node refused, so after a url change
    # it qualified for a node it had never described. The stamp must MATCH the
    # target; absent, null and mismatched are all unqualified. That retires
    # every pre-stamp record rather than trusting it, which is fail-closed and
    # deliberate — an unqualified record leaves the floor and the cell to a
    # source that can say which node it is about.
    if rec.get("node") != node_identity(url) or _stale_shortfall(rec):
        return {}
    return rec


def _hexish(value):
    return isinstance(value, str) and 8 <= len(value) <= 64 \
        and all(c in "0123456789abcdefABCDEF" for c in value)


def read_fee_well(text):
    """The fee-well cell id out of the node's own boot line, ANSI and all.

    THE FIELD IS NOT SEARCHABLE AS IT PRINTS. dregg logs through `tracing`,
    which writes `fee_well` and `=` as separate spans with SGR escapes between
    them, so the raw MESSAGE never contains the substring `fee_well=` — a
    journal filter on it matches nothing and a regex on `-o cat` output finds
    nothing, both silently. Measured against the live unit: the reader returned
    no fee well while the line was in the journal three times over. So the
    SELECTOR is the line's own prose and the escapes are stripped here before
    anything is matched."""
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")
    m = re.findall(r"fee_well\s*=\s*([0-9a-fA-F]{8,64})", clean)
    return m[-1].lower() if m else None


def _fee_well_from_journal():
    """The node states its fee well ONCE, at boot, in its own log line.

    There is no read endpoint for it, so this is the only source that does not
    need an incident to have happened first."""
    try:
        p = subprocess.run(["journalctl", "--user", "-u", UNIT, "--no-pager",
                            "-g", "fee loop", "-n", "20"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return read_fee_well(p.stdout)


def configured_url():
    """The node THIS HOST is configured to talk to, resolved once."""
    from . import chat
    return (chat.node_url() or default_url()).rstrip("/")


def faucet_cell_prefix(url=None):
    """WHICH cell the node pays every grant out of, most explicit source first.

    EVERY SOURCE MUST BE ABLE TO SAY WHICH NODE IT DESCRIBES, and a source
    that cannot is usable only for the target it is definitionally about.
    Discovering a cell from a source that describes a DIFFERENT node than the
    one being asked about is not a near-miss: helm then resolves that cell at
    the wrong node, and the two outcomes are UNKNOWN naming a cell that node
    never had, or — worse — an ordinary unrelated cell there matching the
    prefix and being reported as that node's FAUCET.

      * TOLD (HELM_CHAT_FAUCET_CELL) is an operator assertion about the
        context this host is configured for. It is not proof for every URL a
        caller might pass, so it is scoped to the configured target and
        contributes nothing to an explicitly-named foreign one.
      * THE RECORD carries the node it was observed at, so it qualifies for
        exactly that node.
      * THE JOURNAL is the local systemd unit's own boot line, so it is
        definitionally about whatever node THIS HOST runs.

    THE JOURNAL IS ADMITTED ONLY AGAINST THE ONE BINDING THIS SOURCE CAN
    JUSTIFY, and "configured" is NOT that binding. `chat.node_url()` takes
    HELM_CHAT_NODE_URL first, which may name a REMOTE node, and otherwise the
    node-state url — which `provision()` writes from whatever url its CALLER
    passed, so it carries no locality contract either. Admitting the journal
    for "the configured target" would therefore re-open exactly the
    wrong-target lookup this door exists to close, one layer in.
    What CAN be justified is `default_url()`: it is helm's own constant for
    the port of the unit whose journal this reads (UNIT, PORT), so when the
    target IS that address, the source and the question are about the same
    node by construction. Any other target — remote, migrated, or merely
    unprovable — does not consume the journal at all. That is fail-closed and
    deliberately narrower than the previous behaviour; a node-migrated
    deployment reading a non-default url now needs a target-stamped record or
    the operator override, and gets UNKNOWN rather than another node's cell.

    (prefix, disqualified) — `disqualified` is True only when a source was
    REFUSED for target reasons AND nothing eligible answered, so a refused
    override or journal can never veto a qualifying record."""
    target = (url or configured_url()).rstrip("/")
    disqualified = False

    # The override is an operator assertion about the context THIS HOST is
    # configured for, so it is scoped to that target and says nothing about
    # a URL a caller names explicitly.
    told = home.env("CHAT_FAUCET_CELL")
    if _hexish(told):
        if target == configured_url():
            return told.lower(), False
        disqualified = True

    # The record names its own node, so it is asked for the target directly
    # and outranks nothing it does not describe.
    seen = current_shortfall(target).get("cell")
    if _hexish(seen):
        return seen.lower(), False

    if target != default_url():
        return "", True
    well = _fee_well_from_journal()
    return (well or ""), (disqualified and not well)


def resolve_cell_id(url, prefix):
    """A full 64-hex id for an abbreviated one; None when it is not UNIQUE.

    Two matches is not an answer, and neither is zero — a prefix that resolves
    to anything other than exactly one cell leaves the faucet UNKNOWN."""
    if len(prefix) == 64:
        return prefix
    rows = cell.get_json(url + "/api/cells?limit=500", timeout=4)
    if not isinstance(rows, list):
        return None
    hit = [r["id"].lower() for r in rows
           if isinstance(r, dict) and isinstance(r.get("id"), str)
           and r["id"].lower().startswith(prefix)]
    return hit[0] if len(hit) == 1 else None


def faucet_state(url=None):
    """DRY, FUNDED or UNKNOWN — the faucet cell's own condition.

    UNKNOWN IS A THIRD STATE, not a polite FUNDED. A balance helm could not
    read says nothing about whether the next grant will be paid, and the two
    ways to get that wrong are opposite: calling it healthy hides an outage,
    calling it dry sends an operator to refuel a faucet that is fine."""
    from . import chat
    url = (url or chat.node_url() or default_url()).rstrip("/")
    rec = current_shortfall(url)
    observed = rec.get("need") if isinstance(rec.get("need"), int) else None
    out = {"state": "unknown", "cell": None, "balance": None,
           "threshold": max(FAUCET_DRY_BELOW, observed or 0),
           "observed_need": observed, "reason": ""}
    out.update(_level(None, observed, out["threshold"]))
    prefix, disqualified = faucet_cell_prefix(url)
    if not prefix:
        # TWO DIFFERENT UNKNOWNS, and collapsing them told an operator to go
        # looking for a faucet that helm was never entitled to name. "I have
        # no source that describes THIS node" is a different repair from
        # "nothing has ever observed a faucet cell at all".
        if disqualified:
            # NAME ONLY THE SOURCES THAT ACTUALLY SPOKE. Asserting an
            # override that is not set sends the reader to unset a variable
            # that does not exist.
            told = "HELM_CHAT_FAUCET_CELL asserts the cell for the configured "\
                   "node (%s), " % configured_url() \
                if _hexish(home.env("CHAT_FAUCET_CELL")) else ""
            out["reason"] = (
                "no source describes node %s: no recorded refusal names it, "
                "%sand the unit journal describes the local node (%s) — helm "
                "will not resolve another node's cell here"
                % (url, told, default_url()))
        else:
            out["reason"] = (
                "helm has never observed this node's faucet cell — no "
                "HELM_CHAT_FAUCET_CELL, no recorded refusal, and no "
                "fee_well line in the unit journal")
        return out
    cid = resolve_cell_id(url, prefix)
    if not cid:
        out["reason"] = ("the faucet cell prefix %s did not resolve to exactly "
                         "one cell at %s" % (prefix[:16], url))
        return out
    out["cell"] = cid
    info = cell.get_json(url + "/api/cell/" + cid, timeout=4)
    bal = info.get("balance") if isinstance(info, dict) else None
    if not isinstance(bal, int):
        out["reason"] = ("the node did not answer for cell %s at %s"
                         % (cid[:12], url))
        return out
    out["balance"] = bal
    out["state"] = "dry" if bal < out["threshold"] else "funded"
    out.update(_level(bal, observed, out["threshold"]))
    return out


def low_grants():
    """K, from HELM_CHAT_FAUCET_LOW_GRANTS. Unparseable or below one is the
    default, never zero: a line at zero grants is the DRY line again, which is
    the warning this rung exists to get ahead of."""
    try:
        k = int(str(os.environ.get(LOW_GRANTS_ENV)).strip())
    except ValueError:
        return FAUCET_LOW_GRANTS
    return k if k >= 1 else FAUCET_LOW_GRANTS


def _level(bal, observed, threshold):
    """The LOW reading for one balance: what a grant costs, the line K of them
    sit above, how many are left and whether it is crossed.

    THE GRANT IS THE LARGEST RECENT ONE, not the latest: the refusal record
    keeps this node's high-water need for a week, so a cheap grant cannot
    shrink the line under the one that next fails. DRY IS LOW TOO — with K at
    one, the DRY floor sits above the LOW line, and a faucet that cannot pay
    must never read as not low. An unread balance leaves `low` None: UNKNOWN,
    never low and never fine."""
    grant = observed if isinstance(observed, int) and observed > 0 \
        else GRANT_DEFAULT
    k = low_grants()
    out = {"grant": grant, "low_grants": k, "low_below": k * grant,
           "grants_left": None, "low": None}
    if isinstance(bal, int):
        out["grants_left"] = max(bal, 0) // grant
        out["low"] = bal < max(k * grant, threshold)
    return out


def refill_amount(fa):
    """What `refuel --amount N` should move to leave the faucet at twice its
    LOW line — a named number, so the warning is a command and not advice."""
    return max(2 * fa["low_below"] - fa["balance"], fa["grant"])


def low_line(fa):
    """The one LOW sentence doctor, status and the wake all print, so the
    three can never disagree about the count or the remedy."""
    left = fa["grants_left"]
    return ("faucet LOW — cell %s holds %d, about %d grant%s of %d left "
            "(it warns below %d grants = %d; %s). The faucet never mints: "
            "`%s --amount %d` before it runs dry"
            % (fa["cell"][:12], fa["balance"], left, "" if left == 1 else "s",
               fa["grant"], fa["low_grants"], fa["low_below"], LOW_GRANTS_ENV,
               REFUEL_VERB, refill_amount(fa)))


def faucet_low_path():
    return os.path.join(home.global_dir(), ".state", "chat-faucet-low.json")


def faucet_watch(url=None, reading=None, may_open=True, post=None,
                 roster=None):
    """(room, mentions) when THIS call woke someone, else None — ONE wake per
    LOW SPELL.

    A spell OPENS on the first reading that is LOW and CLOSES on the first
    reading that is not, so a recovered faucet re-arms. The latch is a small
    file beside the refusal record, rewritten under an flock so two seats'
    grants cannot both open one spell — the shape scratch.spells gives the
    seat-throttle wake. An UNKNOWN reading neither opens nor closes, and an
    UNDELIVERED wake does not latch: a latch that marches past a failed post
    has dropped the one message the spell was for.

    `may_open=False` is for readers that must never post (`status`, and
    `refuel` on its funded and apply paths): they can close a spell, never
    open one. `doctor` only reads the faucet and never touches the latch.
    `post` and `roster` are seams; the defaults are chat.post and the roster
    file."""
    import fcntl
    from . import chat
    url = (url or chat.node_url() or default_url()).rstrip("/")
    fa = faucet_state(url) if reading is None else reading
    if not isinstance(fa.get("balance"), int) or fa.get("low") is None \
            or (fa["low"] and not may_open):
        return None
    path = faucet_low_path()
    if not fa["low"] and not os.path.exists(path):
        return None
    key = "%s %s" % (node_identity(url), (fa.get("cell") or "")[:16])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = pk.read_json(path, {})
        state = state if isinstance(state, dict) else {}
        woke = None
        if not fa["low"]:
            if state.pop(key, None) is None:
                return None
        elif key in state:
            return None
        else:
            woke = _low_wake(url, fa, post, roster)
            if woke is None:
                return None           # undelivered: the next reading retries
            state[key] = {"since": pk.now_ts(), "balance": fa["balance"],
                          "grants_left": fa["grants_left"]}
        pk.write_json(path, state)
    return woke


def low_spell(url=None, cell_id=None):
    """The open spell for this node and cell, or {} — what a reader prints to
    say the wake already went out."""
    rec = pk.read_json(faucet_low_path(), {})
    rec = rec if isinstance(rec, dict) else {}
    got = rec.get("%s %s" % (node_identity(url), (cell_id or "")[:16]))
    return got if isinstance(got, dict) else {}


def _mentionable(name):
    """A seat name that can ride a mention unchanged: the label launder's
    fixed point and the mention alphabet. Anything else is not named at all,
    never repaired into a different name (scratch._inert_seat's rule)."""
    from . import seats_common
    return isinstance(name, str) and bool(name) \
        and seats_common._seat_label(name) == name \
        and bool(seats_common._SEAT_TOKEN.fullmatch(name))


def node_owner(rows):
    """(seat, None) for the node's owner, else (None, why). NEVER a guess:
    the owner is the seat HELM_CHAT_NODE_OWNER names, and only when the
    roster carries it — a name nothing answers to is a typo, said so."""
    from . import seats_common
    name = (os.environ.get(OWNER_ENV) or "").strip()
    if not name:
        return None, "no node owner seat is set (%s)" % OWNER_ENV
    if not _mentionable(name) or not isinstance(rows.get(name), dict):
        return None, ("%s names %s, which does not resolve in the roster — "
                      "not woken" % (OWNER_ENV, seats_common._seat_label(name)))
    return name, None


def _wake_post(text, room):
    """UNSIGNED, deliberately: a signed turn is paid for out of the faucet
    this wake is about."""
    from . import chat
    return chat.post(text, room=room, who=WAKE_BOT, sign=False)


def _low_wake(url, fa, post=None, roster=None):
    """Post ONE wake for a spell that just opened -> (room, mentions), or
    None when it could not be delivered. The integrator is resolved from the
    roster, never spelled; the owner is the seat the operator named."""
    from . import seats_common, seats_integrator
    try:
        rows = seats_common.roster() if roster is None else roster
    except Exception:                 # noqa: BLE001 — the integrator then
        rows = {}                     # resolves as unknown and is said so
    rows = rows if isinstance(rows, dict) else {}
    integrator, why = seats_integrator.integrator_seat(snapshot=rows)
    if integrator and not _mentionable(integrator):
        integrator, why = None, ("the integrator's roster key %s is not a "
                                 "mentionable seat name — not woken"
                                 % seats_common._seat_label(integrator))
    owner, owner_why = node_owner(rows)
    mentions = [integrator] if integrator else []
    if owner and owner not in mentions:
        mentions.append(owner)
    lines = ["[%s] chat node %s: %s%s" % (
        WAKE_BOT, url, low_line(fa),
        " — it is DRY already, refusing grants" if fa.get("state") == "dry"
        else "")]
    lines.append("Once it is dry, a send the node refuses for its fee cannot "
                 "be topped up and reads faucet_source_dry. The refuel is a "
                 "dry run until --apply, and it keeps a %d-fee margin in the "
                 "source." % FEE_MARGIN_GRANTS)
    lines.extend("(%s)" % w for w in (why, owner_why) if w)
    if mentions:
        lines.append(" ".join("@" + m for m in mentions))
    try:
        written = (post or _wake_post)("\n".join(lines), "main")
    except Exception:                 # noqa: BLE001 — retried next reading
        return None
    if not isinstance(written, dict) or not written.get("id"):
        return None
    return "main", mentions


def classify_faucet_refusal(err, recipient):
    """Split "the faucet's SOURCE is empty" out of every other refusal.

    A rate limit, an unreachable node and a dry source all arrive as one
    `faucet refused: …` string, and folding them together put the wrong repair
    in front of the operator: waiting cures a rate limit and cures nothing
    here. The tell is WHICH CELL the node names — a shortfall on the RECIPIENT
    is the recipient's problem; a shortfall on any other cell is the pool the
    faucet pays out of."""
    parsed = parse_insufficient(err)
    if not parsed:
        return str(err)
    cid, need, have = parsed
    if recipient and str(recipient).lower().startswith(cid):
        return str(err)
    record_faucet_shortfall(cid, need, have)
    return ("%s: cell %s holds %d and this grant needed %d; the faucet only "
            "pays out of that cell and never mints, so no top-up can refund "
            "it — %s" % (FAUCET_DRY_MARK, cid[:16], have, need, REFUEL_VERB))


def _funded_source(url, faucet_cell):
    """The richest cell on this node whose PUBLIC key matches a local profile.

    That match is the whole question behind the refusal below: it separates
    "helm holds no credential for any funded cell" from "helm holds the
    credential and the signer has no verb that spends it". No seed is opened
    to answer it — the node publishes each cell's public key and the profile
    store publishes each identity's, and two public halves are enough."""
    keys = cell.profile_public_keys()
    if not keys:
        return None, ("no dregg profile is readable at %s, so helm holds no "
                      "key that could sign a transfer" % cell.profiles_dir())
    rows = cell.get_json(url + "/api/cells?limit=500", timeout=4)
    if not isinstance(rows, list):
        return None, "the node did not list its cells at %s" % url
    best = None
    for r in rows:
        cid = r.get("id") if isinstance(r, dict) else None
        if not isinstance(cid, str) or cid.lower() == (faucet_cell or ""):
            continue
        info = cell.get_json(url + "/api/cell/" + cid, timeout=4)
        if not isinstance(info, dict):
            continue
        bal = info.get("balance")
        owner = keys.get(str(info.get("public_key") or "").lower())
        if not isinstance(bal, int) or owner is None:
            continue
        if best is None or bal > best["source_balance"]:
            best = {"source": cid.lower(), "source_profile": owner,
                    "source_balance": bal}
    if best is None:
        return None, ("no cell on this node is owned by a local profile, so "
                      "helm holds no key for any funded cell")
    return best, None


def refuel_amount(src, fee, amount=None):
    """(amount, refusal) — what a refuel may move out of `src` and still pay
    for its own turn.

    THE SOURCE PAYS A FEE TOO, and the signer tops a short sending cell up
    FROM THE FAUCET — the thing that is dry. Moving a source's whole balance
    failed live for exactly that reason. So the default leaves
    FEE_MARGIN_GRANTS fees behind, and an explicit amount is refused, with
    the arithmetic, when it would leave less than one."""
    bal = src["source_balance"]
    margin = FEE_MARGIN_GRANTS * fee
    where = "cell %s (profile %s) holds %d" % (
        src["source"][:12], src["source_profile"], bal)
    if amount is None:
        if bal - margin <= 0:
            return None, ("%s, not more than the %d-fee margin (%d) its own "
                          "Transfer turn must keep: the signer tops a short "
                          "sending cell up from the faucet, which is the dry "
                          "thing, so there is nothing it can safely move"
                          % (where, FEE_MARGIN_GRANTS, margin))
        return bal - margin, None
    if amount <= 0:
        return None, "--amount must be a positive number of computrons"
    if bal - amount < fee:
        return None, ("--amount %d would leave %d in the source, less than one "
                      "fee (%d): %s, and the signer tops a short sending cell "
                      "up from the faucet, which is the dry thing. Move at "
                      "most %d" % (amount, bal - amount, fee, where,
                                   max(bal - fee, 0)))
    return amount, None


def refuel_plan(url=None, amount=None):
    """(plan, refusal) — everything a refuel would move, before it moves any.

    Read-only by construction: it resolves the faucet, the source and the
    signer's verb set and returns them. A faucet that is not LOW yields a
    plan that says so, which is what makes a second run a no-op rather than a
    second move. LOW, not DRY, is the line: the LOW warning names this verb
    as its remedy, and a remedy that answered "already funded" to the
    warning that prescribed it would be a door that contradicts its sign."""
    from . import chat
    url = (url or chat.node_url() or default_url()).rstrip("/")
    fa = faucet_state(url)
    if fa["state"] == "unknown":
        return None, ("the faucet balance is UNKNOWN — %s. UNKNOWN is never DRY "
                      "and never healthy, so nothing is moved" % fa["reason"])
    if not fa["low"]:
        return {"url": url, "funded": True, "faucet": fa["cell"],
                "balance": fa["balance"], "threshold": fa["threshold"],
                "low_below": fa["low_below"]}, None
    src, err = _funded_source(url, fa["cell"])
    if err:
        return None, err
    moved, err = refuel_amount(src, fa["grant"], amount)
    if err:
        return None, err
    # TRI-STATE, NOT A BOOLEAN. A signer helm could not ask is UNKNOWN, and
    # collapsing that into "no transfer verb" would print a confident claim
    # about a binary nothing ran — the same absence-is-not-evidence error the
    # faucet balance carries one layer up.
    verb = cell.signer_supports("transfer")
    return dict(src, url=url, funded=False, faucet=fa["cell"], amount=moved,
                fee=fa["grant"],
                balance=fa["balance"], threshold=fa["threshold"],
                low_below=fa["low_below"], observed_need=fa["observed_need"],
                verb_known=verb is not None, can_sign=verb is True), None


def refuel_move(plan):
    """One sentence naming what moves, from which cell to which. Shared by the
    plan, the refusal and the receipt so the three can never disagree."""
    return ("%d computrons from cell %s (profile %s, balance %d, keeping %d "
            "for its own fee) into faucet cell %s (balance %d)"
            % (plan["amount"], plan["source"][:12], plan["source_profile"],
               plan["source_balance"],
               plan["source_balance"] - plan["amount"], plan["faucet"][:12],
               plan["balance"]))


def _cell_balance(url, cid):
    """One cell's balance as the node reports it, or None — unreadable is
    never zero."""
    info = cell.get_json(url + "/api/cell/" + cid, timeout=4)
    bal = info.get("balance") if isinstance(info, dict) else None
    return bal if isinstance(bal, int) else None


#: Seconds between the re-reads that settle an UNKNOWN transfer. Only the
#: "nothing changed" answer waits: a committed move is not un-committed by
#: time, but a move still in flight can look like nothing at first.
REREAD_DELAYS_S = (1.5, 3.0)
_sleep = time.sleep


def classify_by_balances(plan, pre_source, pre_faucet, timed_out=False):
    """(verdict, sentence) for a transfer the signer could not prove, from
    the only witnesses left: the two balances it would have changed.

    A TIMED-OUT SIGNER NEVER SETTLES AS NOT COMMITTED. A signer killed at its
    budget may have left a submit queued at the node, and that turn can land
    after the last re-read; "safe to re-run" there is how the value moves
    twice. So `timed_out` caps the no-change answer at UNKNOWN, worded as what
    was seen: no change within the re-read window.

    THE OLD NODE CANNOT ANSWER BY HASH. Its /api/receipts ignores the
    turn_hash filter and returns the chain head, so the signer can never find
    its own receipt there and exits nonzero over transfers that committed —
    measured live. The balances can answer: COMMITTED when the faucet rose by
    exactly the amount and the source fell by the amount plus a fee no larger
    than the margin; NOT COMMITTED when neither changed across every re-read;
    anything else UNKNOWN, with both deltas, because a grant or a second
    refuel inside the window moves the same numbers."""
    url, amount = plan["url"], plan["amount"]
    margin = FEE_MARGIN_GRANTS * plan["fee"]
    reads = 0
    for delay in (0,) + tuple(REREAD_DELAYS_S):
        if delay:
            _sleep(delay)
        src = _cell_balance(url, plan["source"])
        fau = _cell_balance(url, plan["faucet"])
        reads += 1
        if src is None or fau is None:
            return "unknown", ("the balances could not be re-read (source %s, "
                               "faucet %s), so whether the value went across "
                               "is UNKNOWN" % ("unreadable" if src is None else src,
                                            "unreadable" if fau is None else fau))
        fell, rose = pre_source - src, fau - pre_faucet
        if fell == 0 and rose == 0:
            continue
        if rose == amount and amount < fell <= amount + margin:
            return "committed", (
                "COMMITTED, confirmed from balances and not from a receipt: "
                "source %s fell by %d (%d + a %d fee) and faucet %s rose by %d"
                % (plan["source"][:12], fell, amount, fell - amount,
                   plan["faucet"][:12], rose))
        return "unknown", ("the balances do not settle it: source %s fell by "
                           "%d and faucet %s rose by %d, where a committed "
                           "move of %d is a rise of exactly %d and a fall of "
                           "%d plus one fee — a grant or a second refuel in "
                           "the same window moves these numbers too"
                           % (plan["source"][:12], fell, plan["faucet"][:12],
                              rose, amount, amount, amount))
    if timed_out:
        return "unknown", ("no change seen within %.1f s of re-reads (source "
                           "%s holds %d, faucet %s holds %d), but the signer "
                           "TIMED OUT, so a submit it left queued can still "
                           "land" % (sum(REREAD_DELAYS_S), plan["source"][:12],
                                     pre_source, plan["faucet"][:12],
                                     pre_faucet))
    return "not-committed", ("NOT COMMITTED: neither balance changed across %d "
                             "re-reads (source %s holds %d, faucet %s holds %d)"
                             % (reads, plan["source"][:12], pre_source,
                                plan["faucet"][:12], pre_faucet))


def _refuel_apply(plan):
    from . import chat
    # AN ECONOMIC TURN IS NOT A COORDINATION TURN. chat's _env_extra forwards
    # DREGG_COORDINATION_EXEMPT for its EmitEvent-only sends; a Transfer carries
    # a balance_change, dregg's admission gate disqualifies it from the exempt
    # class, and declaring otherwise would have the client meter a fee the node
    # is about to charge anyway. The env is built here, deliberately without it.
    env = {"DREGG_NODE_URL": plan["url"], "MELD_NODE_URL": plan["url"]}
    token = chat._node_token()
    if token:
        env["DREGG_API_TOKEN"] = token
        env["MELD_NODE_TOKEN"] = token
    # THE PLAN IS RE-READ AT THE MOMENT OF THE MOVE. The balances behind it
    # were read before the signer was even asked what verbs it has, so a second
    # refuel (or any grant) between plan and submit could have funded the
    # faucet already, and submitting the stale plan would overfund it and drain
    # the source twice. Re-reading NARROWS that window; it cannot close it,
    # because nothing in the argv carries an idempotency key or an expected
    # destination balance — closing it needs the signer/node protocol to take
    # one (see the refusal text and docs/VERBS.md).
    fresh = faucet_state(plan["url"])
    if fresh.get("state") == "unknown":
        print("helm chat node refuel: the faucet's balance could not be "
              "re-read at the moment of the move (%s), so nothing was "
              "submitted" % (fresh.get("reason") or "no reason given"),
              file=sys.stderr)
        return 1
    if not fresh.get("low"):
        print("helm chat node refuel: nothing to do — faucet cell %s now "
              "holds %d, at or above the %d its LOW line asks for"
              % (fresh["cell"][:12], fresh["balance"], fresh["low_below"]))
        _watch(plan["url"], fresh, may_open=False)
        return 0
    # THE SOURCE IS RE-READ TOO, for two reasons: the margin is only true of
    # the balance it is checked against, and an UNKNOWN outcome below is
    # classified from how far this number falls.
    pre_source = _cell_balance(plan["url"], plan["source"])
    if pre_source is None:
        print("helm chat node refuel: the source cell %s could not be re-read "
              "at the moment of the move, so nothing was submitted"
              % plan["source"][:12], file=sys.stderr)
        return 1
    _amount, short = refuel_amount(dict(plan, source_balance=pre_source),
                                   plan["fee"], plan["amount"])
    if short:
        print("helm chat node refuel: REFUSED at the moment of the move — "
              + short, file=sys.stderr)
        return 1
    rc, out, err = cell.run_bin(
        ["transfer", "--profile", plan["source_profile"],
         "--to", plan["faucet"], "--amount", str(plan["amount"])],
        timeout=60, env_extra=env)
    # A RESULT IS ONLY WHAT A RECEIPT PROVES. A signer that submits, commits
    # and THEN fails to resolve its receipt exits nonzero over a transfer that
    # HAPPENED, so reading a nonzero exit as "refused" invites a retry that
    # moves the value twice; and exit 0 with any JSON object at all is not a
    # receipt, so reading it as "moved" reports a transfer nobody witnessed.
    # Both unproven directions are UNKNOWN until the balances below settle
    # them. This mirrors the send seam in chat.py, which calls a nonzero
    # result a non-delivery ONLY when the signer's last word is the node's
    # explicit refusal (chat._node_refusal). Bound dregg has no `transfer`
    # verb, so no refusal shape exists here to read that way.
    receipt = _transfer_receipt(cell._last_json(out))
    if rc == 0 and receipt:
        print("helm chat node refuel: moved %s (%s)"
              % (refuel_move(plan), receipt))
        _watch(plan["url"], may_open=False)
        return 0
    print("helm chat node refuel: the signer's outcome is UNKNOWN (rc %s%s) — "
          "%s. Re-reading both balances to settle it; nothing is retried."
          % (rc, "" if receipt else ", no receipt in its output",
             chat._safe_reason((err or out or "").strip())), file=sys.stderr)
    verdict, said = classify_by_balances(plan, pre_source, fresh["balance"],
                                         timed_out=rc is None)
    if verdict == "committed":
        print("helm chat node refuel: " + said + ". Do NOT retry.")
        _watch(plan["url"], may_open=False)
        return 0
    if verdict == "not-committed":
        print("helm chat node refuel: " + said + ". Re-running the refuel is "
              "safe.", file=sys.stderr)
        return 1
    print("helm chat node refuel: outcome still UNKNOWN — %s. Read `helm chat "
          "node status` before retrying: a retry MAY move the value twice."
          % said, file=sys.stderr)
    return 1


COMMITTED_WORDS = ("committed", "applied", "finalized", "included", "landed")
# A node ACCEPTING a turn is not a node APPLYING one. These say the submission
# was taken; none of them says the value moved.
_RESULT_FLAGS = ("success", "accepted", "committed", "ok")


def _transfer_receipt(info):
    """The signer's proof that a transfer COMMITTED, or "" when it proved
    nothing.

    TWO HALVES, AND A HASH IS ONLY ONE OF THEM. A hash names a turn; it does
    not say the turn was applied, and a QUEUED answer can already carry one.
    So a receipt needs an identity (a hex-ish value under a turn/receipt key)
    AND a positive statement of commitment, with no contradicting one. helm
    holds no transfer protocol to read here — bound dregg has no `transfer`
    verb at all — so an answer that states nothing about commitment leaves the
    outcome unwitnessed and the door says UNKNOWN rather than inferring it."""
    if not isinstance(info, dict):
        return ""
    if any(info.get(k) is False for k in _RESULT_FLAGS):
        return ""
    # BOTH WORDS, NEVER THE FIRST TRUTHY ONE. `status: committed` beside
    # `state: queued` is a CONTRADICTION, and picking whichever came first
    # hid it — the same first-truthy read that let a queued answer pass.
    # AND PRESENT-BUT-MALFORMED IS NOT ABSENT: `state: false`, `0` or `[]`
    # collapse to "" under a truthiness read, so a committed status beside a
    # malformed state read as a clean commitment. A field the producer SENT
    # has to satisfy the contract or the answer is unreadable, which is
    # UNKNOWN — absent means the key is missing or null, nothing else.
    said = []
    for key in ("status", "state"):
        word = info.get(key)
        if key not in info or word is None:
            continue
        if not isinstance(word, str):
            return ""
        if word.strip():
            said.append(word.strip().lower())
    if any(w not in COMMITTED_WORDS for w in said):
        return ""
    # ADMISSION IS NOT COMMITMENT, and the two are one word apart. In bound
    # dregg's send source `accepted: true` beside a turn hash says the node
    # TOOK the turn, not that it applied one; `ok` and `success` are the same
    # shape of claim, and reading any of them as finality is the any-dict
    # defect one level in: narrower, still wrong. So `accepted`, `ok` and
    # `success` reach this reader ONLY through the stated-failure refusal
    # above, where a False value refuses; never here. The only things
    # that establish a transfer HAPPENED are an explicit `committed` flag and
    # a committed status word. ONE EXPRESSION DECIDES THAT, deliberately. A
    # second guard beside it re-checking the admission flags cannot change an
    # answer this line already gives, and a guard that cannot change an
    # outcome reads as rigour while providing none — worse, it invites the
    # next reader to trust it instead of this line. The mutation matrix tells
    # them apart: mutate THIS line and the admission arms go red.
    committed = info.get("committed") is True or bool(said)
    if not committed:
        return ""
    for key in ("receipt_hash", "turn_hash", "tx_hash", "receipt", "turn"):
        v = info.get(key)
        if isinstance(v, str) and _hexish(v):
            return "%s %s" % (key, v[:16])
    return ""


def _refuel(args):
    """refuel — put computrons back into the cell the faucet pays out of."""
    amount = None
    if "--amount" in args:
        i = args.index("--amount")
        try:
            amount = int(args[i + 1])
        except (IndexError, ValueError):
            print("helm chat node refuel: --amount needs a number",
                  file=sys.stderr)
            return 2
    plan, refusal = refuel_plan(amount=amount)
    if refusal:
        print("helm chat node refuel: REFUSED — " + refusal, file=sys.stderr)
        return 1
    if plan["funded"]:
        print("helm chat node refuel: faucet cell %s is already funded — "
              "balance %d, at or above the %d its LOW line asks for; nothing "
              "moved" % (plan["faucet"][:12], plan["balance"],
                         plan["low_below"]))
        # THE FUNDED ANSWER IS A READING THAT IS NOT LOW: it closes an open
        # spell the same way the apply path's re-read does, so the wake
        # re-arms whether the refill went through this door or not.
        _watch(plan["url"], may_open=False)
        return 0
    if not plan["verb_known"]:
        from . import chat
        print("helm chat node refuel: REFUSED — helm could not ask the "
              "configured signer whether it can transfer (%s), so whether this "
              "move is possible is UNKNOWN, which is not the same as "
              "impossible. It would have moved %s."
              % (chat._safe_reason(cell.bin_status()["reason"]),
                 refuel_move(plan)), file=sys.stderr)
        return 1
    if not plan["can_sign"]:
        # THE CREDENTIAL IS NOT THE MISSING HALF. helm holds the key for the
        # funded cell named here; what it lacks is a signer verb that spends
        # one. Saying that precisely is what routes the repair to the signer
        # instead of to a credential hunt that would find nothing wrong.
        print("helm chat node refuel: REFUSED — helm cannot sign a Transfer: "
              "the configured signer (%s) exposes no `transfer` verb, so no "
              "local key can move value out of a funded cell. It would have "
              "moved %s. Fix the signer, then re-run."
              % (cell.bin_path() or "none", refuel_move(plan)),
              file=sys.stderr)
        return 1
    if "--apply" not in args:
        print("helm chat node refuel: would move %s — re-run with --apply to "
              "sign it" % refuel_move(plan))
        return 0
    return _refuel_apply(plan)


def faucet(url, recipient, amount):
    """One faucet grant, validated instead of fire-and-forgotten. Returns the
    accepted response or a precise reason. The node's contract is
    {success:true}; reachable JSON with success:false is a refusal, not health."""
    r = cell.post_json(url + "/api/faucet",
                       {"recipient": recipient, "amount": amount})
    if not isinstance(r, dict):
        return None, "faucet unreachable or returned a non-object response at %s" % url
    if r.get("success") is not True:
        err = "faucet refused: %s" % (r.get("error") or "success was not true")
        reading = refusal_reading(url, err, recipient)
        if reading:
            _watch(url, reading)
        return None, err
    _watch(url)
    return r, None


def refusal_reading(url, err, recipient):
    """A faucet reading out of the node's own refusal, or None.

    A refusal on the faucet's SOURCE states its balance (`have`), so the DRY
    spell can open from it with no further read — which is the case that
    matters: after the grant that crossed the line every later grant is
    refused, and a watch that only ran on success would never run again."""
    parsed = parse_insufficient(err)
    if not parsed:
        return None
    cid, need, have = parsed
    if recipient and str(recipient).lower().startswith(cid):
        return None
    prior = current_shortfall(url).get("need")
    observed = max(need, prior if isinstance(prior, int) else 0)
    threshold = max(FAUCET_DRY_BELOW, observed)
    out = {"state": "dry" if have < threshold else "funded", "cell": cid,
           "balance": have, "threshold": threshold, "observed_need": observed,
           "reason": ""}
    out.update(_level(have, observed, threshold))
    return out


def _watch(url, reading=None, may_open=True):
    """THE GRANT PATH DRIVES THE WAKE. Every funded grant is a drain on the
    faucet, and every seat's signed post reaches one, so reading the faucet
    here is what gets the warning out before the refusal does. FAIL-OPEN: a
    watch that raised would fail the grant it rides on."""
    try:
        return faucet_watch(url, reading=reading, may_open=may_open)
    except Exception:                 # noqa: BLE001 — never fail the grant
        return None


def ensure_healthy_result(url):
    """Precise bootstrap result for callers that need attribution."""
    st = cell.get_json(url + "/status", timeout=3)
    if isinstance(st, dict) and st.get("healthy") is True:
        return True, None
    _r, err = faucet(url, bootstrap_cell_hex(), 1)
    if err:
        return False, err
    for _ in range(20):
        st = cell.get_json(url + "/status", timeout=3)
        if isinstance(st, dict) and st.get("healthy") is True:
            return True, None
        time.sleep(0.25)
    return False, "faucet accepted but the node never became healthy at %s" % url


def ensure_healthy(url):
    """Backward-compatible bool API; precise callers use ensure_healthy_result."""
    return ensure_healthy_result(url)[0]


def provision(url, passphrase=None):
    """Unlock + bootstrap a reachable node; persist {url, passphrase, token}
    0600. Returns (state-dict, None) or (None, reason). Idempotent — a stored
    passphrase re-unlocks the same node across restarts AND re-sets itself on
    a fresh tmpfs after reboot."""
    st = state()
    passphrase = passphrase or st.get("passphrase") or secrets.token_hex(16)
    token, err = unlock(url, passphrase)
    if err:
        return None, err
    healthy, err = ensure_healthy_result(url)
    if not healthy:
        return None, "node reachable but never produced a block: %s" % err
    st.update({"url": url, "passphrase": passphrase, "token": token})
    write_state(st)
    return st, None


def boot_wait_s():
    """HELM_CHAT_NODE_BOOT_WAIT_S seconds, else HEALTH_WAIT_S. A value that
    does not parse, or is not positive, falls back to the default rather than
    raising: a typo here must never be able to stop a node coming up."""
    raw = home.env(BOOT_WAIT_ENV)
    try:
        v = float(raw) if raw else 0.0
    except ValueError:
        v = 0.0
    return v if v > 0 else HEALTH_WAIT_S


def hung_after_s():
    """Seconds a running process may go without binding its API before it is
    `hung` (boot_diagnosis): the boot wait, floored at its default.

    The boot wait is `up`'s patience, documented as such, and the fee-loop
    build answers in under a second — so an operator may set it to 30 s
    without a thought for the rebased node's 150 s Lean init. If that knob
    were the hung threshold too, doctor would FAIL a healthy first boot from
    30 s to 150 s and status would tell the operator to `down` and `up` it:
    the one thing boot_wait_s promises a typo can never do. Measured before
    the floor: HELM_CHAT_NODE_BOOT_WAIT_S=30, a node 45 s into its init,
    read `hung`. Raising the wait (a slow host) raises this; lowering it
    never lowers this below HEALTH_WAIT_S."""
    return max(boot_wait_s(), HEALTH_WAIT_S)


def unit_state(unit=UNIT):
    """systemd's own view of the unit, or None when systemctl cannot answer
    (no systemd here, or it failed):

      active, sub   ActiveState / SubState
      restarts      NRestarts
      result        Result — `success` after a clean stop, else the reason
                    the last run ended (`exit-code`, `signal`, `timeout`...)
      invocation    InvocationID of the current (or last) run, "" if none —
                    the key that scopes the journal to THIS run
      running_s     seconds since the main process started, None when it has
                    not (read against the same CLOCK_MONOTONIC systemd uses)
    """
    rc, out = _systemctl(
        "show", unit, "--property=ActiveState,SubState,NRestarts,Result,"
        "InvocationID,ExecMainStartTimestampMonotonic")
    if rc != 0:
        return None
    kv = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)

    def num(key):
        try:
            return int(kv.get(key) or 0)
        except ValueError:
            return 0
    started = num("ExecMainStartTimestampMonotonic")
    inv = kv.get("InvocationID", "")
    return {"active": kv.get("ActiveState", ""), "sub": kv.get("SubState", ""),
            "restarts": num("NRestarts"), "result": kv.get("Result", ""),
            "invocation": inv if _INVOCATION.fullmatch(inv) else "",
            "running_s": (time.monotonic() - started / 1e6) if started
            else None}


_INVOCATION = re.compile(r"[0-9a-f]{32}")


def _gone(st):
    """Is the unit's process gone right now? Stopped, failed, waiting out
    RestartSec before a retry (`auto-restart`), or on its way out
    (`deactivating`: a stop in flight, up to TimeoutStopSec — its running
    time is not a boot, so it must not read as hung). A running process and
    a running ExecStartPre (`start-pre`) are not."""
    return bool(st) and (st["active"] in ("failed", "inactive", "deactivating")
                         or st["sub"] in ("auto-restart", "dead", "failed",
                                          "exited"))


def _exited(st, restarts0=0):
    """Has the unit's process gone since a wait began? Gone now, or
    restarted since then (NRestarts above `restarts0`)."""
    return _gone(st) or bool(st) and st["restarts"] > restarts0


def wait_boot(url, unit=None, seconds=None, say=None):
    """Wait for the HTTP API to answer /api/receipts. Returns (outcome, what):

      ("up", seconds)           the API answered;
      ("exited", unit_state)    the unit's process exited first — a failure,
                                reported at once rather than after the wait;
      ("initializing", seconds) the wait ran out with the process still
                                running — NOT a failure: a rebased node's
                                verified-runtime init can outlast any wait;
      ("hung", unit_state)      the process has run past hung_after_s without
                                its API answering — reported at the first
                                look that shows it, never after the wait.
                                `up` against a node that is already running
                                reaches this at once: `enable --now` does not
                                restart it, so its clock is the old run's.

    With no `unit`, only the API is watched. `say(text)` gets one progress
    line per BOOT_TICK_S.

    A START STILL QUEUED IS NOT AN EXIT. `up` starts the unit with
    --no-block, so the first reads can still show the state the LAST run left
    — `inactive`/`dead`, or `failed` after an earlier crash loop, since
    systemd clears that only when the queued job is dispatched — before this
    start has begun. A stopped state reads as an exit only once this start
    has been seen (the unit active, or a new InvocationID), or after
    START_GRACE_S; a start still queued past the grace does read as exited,
    which is the bound on this rule. (`up` also runs `reset-failed` first.)"""
    seconds = boot_wait_s() if seconds is None else seconds
    start = time.time()
    first = unit_state(unit) if unit else None
    restarts0 = first["restarts"] if first else 0
    inv0 = first["invocation"] if first else ""
    idle = ("inactive", "failed")
    started = bool(first) and first["active"] not in idle
    next_look, next_say = start, start + BOOT_TICK_S
    while True:
        if isinstance(cell.get_json(url + "/api/receipts", timeout=2), list):
            return "up", time.time() - start
        now = time.time()
        if unit and now >= next_look:
            st = unit_state(unit)
            started = started or bool(st) and (
                st["active"] not in idle or st["invocation"] != inv0)
            queued = (st and st["active"] in idle and not started
                      and now - start < START_GRACE_S)
            if not queued and _exited(st, restarts0):
                return "exited", st
            if hung_diagnosis(st):
                return "hung", st
            next_look = now + 2
        if now - start >= seconds:
            return "initializing", now - start
        if say and now >= next_say:
            say("still initializing verified runtime (%ds)" % (now - start))
            next_say += BOOT_TICK_S
        time.sleep(0.5)


def wait_api(url, seconds=None):
    """Wait for the HTTP API to answer /api/receipts. True iff it did."""
    return wait_boot(url, seconds=seconds)[0] == "up"


def _up(args):
    from . import chat
    res = bin_resolution()
    if not res["path"]:
        print("helm chat node: " + chat._safe_reason(res["reason"]),
              file=sys.stderr)
        return 1
    b = install_path(res["path"])
    # A BINARY THAT CANNOT BE RECORDED IS NOT INSTALLED. Without its record a
    # later bare `up` resolves some other binary against this chain, so a
    # path that will not hash stops here, before the unit names it.
    try:
        file_sha256(b)
    except OSError as e:
        print("helm chat node: " + chat._safe_reason(
            "the node binary %s cannot be read (%s) — nothing installed"
            % (b, e.__class__.__name__)), file=sys.stderr)
        return 1
    up_path = unit_path()
    want = unit_text(b)
    # REWRITE A DRIFTED UNIT, don't just create a missing one. `up` used to
    # write the unit only when absent, so every generator fix — the restart
    # limiter, the prepare step, the identity restore — landed in the source
    # and never reached the hosts that already had a unit file. A fix that
    # cannot deploy itself is not a fix.
    have = None
    if os.path.exists(up_path):
        try:
            with open(up_path, encoding="utf-8") as f:
                have = f.read()
        except OSError:
            have = None
    if have != want:
        os.makedirs(os.path.dirname(up_path), exist_ok=True)
        tmp = up_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(want)
        os.replace(tmp, up_path)
        print("helm chat node: unit %s (0600): %s" % (
            "written" if have is None else "REFRESHED (it had drifted)", up_path))
    err = record_binary(b)
    if err:
        print("helm chat node: " + chat._safe_reason(
            "the unit runs %s, but recording it failed (%s) — a later bare "
            "`helm chat node up` would not know it; fix the state file %s and "
            "re-run" % (b, err, state_path())), file=sys.stderr)
        return 1
    print("helm chat node: " + chat._safe_reason(
        "runs %s (%s); recorded, so a later bare `up` keeps this binary"
        % (b, BIN_SOURCES[res["source"]])))
    posture = declared_posture()
    dropin = write_posture_dropin(posture)
    if dropin:
        print("helm chat node: mirrored %d posture flag%s from %s into %s" % (
            len(posture), "s"[:len(posture) != 1], PEER_UNIT,
            os.path.basename(dropin)))
    # NO makedirs HERE. Pre-creating the data dir is exactly what defeats the
    # prepare step: `dregg-cave-node init` no-ops on an existing-but-empty dir
    # and still exits 0, so the unit would start a KEYLESS cave with every exit
    # code reporting success. prepare() owns the dir's existence, alone.
    # --no-block: the start job now includes prepare, which may run the
    # genesis mint for minutes; a blocking start would outlive _systemctl's
    # own timeout and read as a failed start. wait_boot watches the unit
    # (a running prepare is `start-pre`, not an exit) and the API instead.
    # A unit an earlier crash loop left `failed` would read as THIS start's
    # exit until the queued job runs; clear that first. Its answer is not a
    # gate (a unit that is not failed has nothing to reset).
    _systemctl("reset-failed", UNIT)
    for verb in (("daemon-reload",), ("enable", "--now", "--no-block", UNIT)):
        rc, out = _systemctl(*verb)
        if rc not in (0,):
            print("helm chat node: systemctl %s failed: %s" % (" ".join(verb), out),
                  file=sys.stderr)
            return 1
    url = default_url()
    wait = boot_wait_s()
    print("helm chat node: waiting up to %ds for the API at %s — initializing "
          "verified runtime (a rebased node's Lean init takes about 150 s; the "
          "wait ends at once if the node exits)" % (wait, url))
    outcome, what = wait_boot(url, unit=UNIT, seconds=wait,
                              say=lambda t: print("helm chat node: " + t))
    if outcome == "hung":
        # THE SAME WORDS STATUS PRINTS. Waiting the whole boot wait on a node
        # that is already past it, then calling it "still initializing",
        # told the operator to wait while status told them to restart.
        print("helm chat node: " + unreachable_line(url, hung_diagnosis(what)),
              file=sys.stderr)
        return 1
    if outcome == "initializing":
        # NOT A FAILURE. The process is alive and has not refused; it has not
        # finished its verified-runtime init inside the wait. Calling that
        # "did not come up" sends the operator to debug a healthy boot.
        print("helm chat node: still initializing verified runtime at %s after "
              "%ds — the unit is running and has not failed. Re-run `helm chat "
              "node up` once the API answers (it provisions then), or raise "
              "HELM_%s." % (url, what, BOOT_WAIT_ENV), file=sys.stderr)
        return 1
    if outcome == "exited":
        # RELAY THE SERVICE'S OWN DIAGNOSIS. "The API never answered" is a
        # timeout: true of a refused start, a wrong port, a missing data-dir and
        # a slow boot alike. dregg says exactly which, so say that instead —
        # and, where the refusal has one known cure, say the cure. The
        # journal is read for THE RUN THAT EXITED, never an older one.
        why = last_failure(invocation=(what or {}).get("invocation"))
        msg = ("helm chat node: the node exited before its API answered at %s"
               % url)
        if why:
            msg += "\n  the service said: " + why
            cure = refusal_remedy(why)
            if cure:
                msg += "\n  cure: " + cure
            if not posture and "DREGG_ALLOW" in why:
                msg += ("\n  no DREGG_* posture is declared for %s, so nothing was "
                        "mirrored. helm will not opt a node out of verification on "
                        "your behalf — declare it for the team node (or rebuild the "
                        "binary against the Lean archive) and re-run." % PEER_UNIT)
        else:
            msg += " (journalctl --user -u %s)" % UNIT
        print(msg, file=sys.stderr)
        return 1
    # SNAPSHOT BEFORE THE PROVISIONING GATE. A node that is up but not yet
    # provisioned still has an identity worth keeping, and provisioning is the
    # step most likely to fail on a degraded host — gating the snapshot behind
    # it means the hosts that need continuity most are exactly the ones that
    # never get it. Idempotent, so later passes pick up the seed and agent key
    # once running has created them.
    # NEVER SNAPSHOT A STRANGER. A running cave whose descriptor differs from
    # the snapshot's is not the node the snapshot saved; writing it over the
    # snapshot would make the next reboot restore the stranger and could
    # erase the only copy of the real key. Stop here and say so.
    ident = identity_state()
    if ident["saved"] and not ident["matched"]:
        print("helm chat node: identity snapshot DISAGREES with the running "
              "cave at %s — refusing to snapshot it over %s (a reboot would "
              "restore a different node, and the snapshot may hold the only "
              "copy of the real key). Compare the two node.key files; if the "
              "running one is right, archive the snapshot by hand and re-run"
              % (DATA_DIR, identity_dir()), file=sys.stderr)
        return 1
    saved, serr = snapshot_identity()
    if serr:
        print("helm chat node: identity NOT snapshotted — %s (this node will "
              "come back as a stranger after a reboot)" % serr, file=sys.stderr)
    elif saved:
        print("helm chat node: identity snapshotted (%s) to %s — the next "
              "reboot restores THIS node, not a new one" % (
                  ", ".join(saved), identity_dir()))
    st, err = provision(url)
    if err:
        print("helm chat node: node up but provisioning failed — %s" % err,
              file=sys.stderr)
        return 1
    head = cell.get_json(url + "/api/receipts", timeout=3) or []
    print("helm chat node: LIVE at %s — data-dir %s (tmpfs), chain head %s, "
          "faucet on, credential stored (0600)" % (
              url, DATA_DIR,
              head[0].get("chain_index") if head else "(genesis)"))
    return 0


def _down(args):
    rc, out = _systemctl("stop", UNIT)
    if rc not in (0,):
        print("helm chat node: systemctl stop failed: %s" % out, file=sys.stderr)
        return 1
    print("helm chat node: stopped — the RAM room is gone; the journal keeps "
          "whatever `helm chat log-flush` recorded")
    return 0


def _status(args):
    from . import chat
    rc, out = _systemctl("is-active", UNIT)
    print("helm chat node: unit %s (%s)" % (UNIT, out or "unknown"))
    # WHICH BINARY, AND WHY THAT ONE. The same line doctor prints; a refused
    # record fails status, since the `up` this surface recommends would refuse.
    report = binary_report()
    if report:
        print("helm chat node: " + chat._safe_reason(report[1]),
              file=sys.stderr if report[0] != "ok" else sys.stdout)
    refused = bool(report) and report[0] == "fail"
    url = chat.node_url() or default_url()
    transport = chat.transport_status()
    if transport.get("mode") == "degraded":
        print("helm chat node: " + chat.transport_failure_summary(transport),
              file=sys.stderr)
    elif transport.get("mode") == "ready":
        print("helm chat node: signing " + chat.transport_label(transport))
    head = cell.get_json(url + "/api/receipts", timeout=3)
    if head is None:
        print("helm chat node: " + unreachable_line(url))
        return 1
    print("helm chat node: API LIVE at %s — chain head %s" % (
        url, head[0].get("chain_index") if head else "(no receipts yet)"))
    # THE SOURCE BEFORE THE SINKS. Every balance below is funded out of one
    # cell, so a list of twenty zeroes means nothing until this line says
    # whether the pool that refills them holds anything.
    fa = faucet_state(url)
    if fa["state"] == "dry":
        print("helm chat node: FAUCET DRY — cell %s holds %d, below the %d a "
              "funded turn needs%s; the faucet never mints, so `%s`"
              % (fa["cell"][:12], fa["balance"], fa["threshold"],
                 (" (largest refused grant asked %d)" % fa["observed_need"])
                 if fa["observed_need"] else "",
                 "%s --amount %d" % (REFUEL_VERB, refill_amount(fa))),
              file=sys.stderr)
    elif fa["state"] == "unknown":
        print("helm chat node: faucet balance UNKNOWN — %s (unknown is neither "
              "dry nor healthy)" % fa["reason"], file=sys.stderr)
    elif fa["low"]:
        print("helm chat node: " + low_line(fa).replace("faucet LOW",
                                                        "FAUCET LOW", 1),
              file=sys.stderr)
    else:
        print("helm chat node: faucet cell %s funded — balance %d, about %d "
              "grants of %d left" % (fa["cell"][:12], fa["balance"],
                                     fa["grants_left"], fa["grant"]))
    # A READER CLOSES A SPELL, NEVER OPENS ONE. On a fee-free node no grant
    # ever re-reads the faucet, so a faucet refilled by hand or from another
    # host left the latch standing and the NEXT low spell woke nobody; this
    # reading re-arms it. `may_open=False` cannot post.
    _watch(url, fa, may_open=False)
    spell = low_spell(url, fa["cell"]) if fa["low"] else {}
    if spell.get("since"):
        print("helm chat node: the low-faucet wake for this spell went out at "
              "%s" % spell["since"])
    cells = pk.read_json(chat.cells_path(), {}) or {}
    for profile in sorted(cells):
        info = cell.get_json(url + "/api/cell/" + cells[profile], timeout=3) or {}
        print("  cell %s (%s): balance %s" % (
            cells[profile][:12], profile, info.get("balance", "?")))
    # IS THE RUNNING SIGNER THE ONE WE THINK WE FIXED? Owner, 2026-07-29:
    # "every time i look back on it after a few hours, it's broken again". It
    # was not breaking repeatedly — the faucet fix was committed 52 minutes
    # AFTER the deployed binary was built, and stayed unbuilt for 20 hours
    # while every surface here said the signer was ready. Presence was the only
    # thing anything checked. This is the rung that reads the source.
    # Both reasons are LAUNDERED at the sink: the staleness reason embeds a git
    # COMMIT SUBJECT and a repo path, which are externally-authored strings, and
    # the core reason embeds names the signer binary printed. Neither is helm's
    # text, so neither reaches a terminal unsanitised.
    for what, stale in (("signer", cell.signer_staleness()),
                        ("node", cell.node_staleness())):
        if stale.get("state") == "stale":
            print("helm chat node: %s STALE — %s"
                  % (what.upper(), chat._safe_reason(stale["reason"])))
        elif stale.get("state") == "unknown":
            print("helm chat node: %s freshness UNKNOWN — %s (unproven is not "
                  "the same as current)"
                  % (what, chat._safe_reason(stale.get("reason"))))
    cores = cell.signer_cores()
    if cores.get("state") == "marshal-only":
        print("helm chat node: signer is MARSHAL-ONLY (%s) — real signatures "
              "via the unaudited fips204 fallback, matching a devnet node "
              "built without the Lean archive. Expected on devnet; NEVER ship "
              "it as a verified node." % chat._safe_reason(cores["reason"]))
    ident = identity_state()
    if not ident["saved"]:
        print("helm chat node: identity NOT snapshotted — the next reboot "
              "mints a NEW node key and this node returns as a stranger. "
              "Fix: helm chat node up")
    elif not ident["matched"]:
        print("helm chat node: identity snapshot DISAGREES with the running "
              "cave — a reboot would restore a different node than the one "
              "serving now. Fix: helm chat node up")
    else:
        print("helm chat node: identity snapshotted (%s) — survives a reboot"
              % ", ".join(ident["saved"]))
    return 1 if transport.get("mode") == "degraded" or refused else 0


def _prepare(args):
    """ExecStartPre body. Loud on both paths: systemd swallows nothing here,
    and a silent prepare is what let a keyless cave look like a healthy one."""
    opts = {"--data-dir": DATA_DIR, "--bin": None}
    for flag in opts:
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print("helm chat node prepare: %s needs a path" % flag,
                      file=sys.stderr)
                return 2
            opts[flag] = args[i + 1]
    try:
        msg, err = prepare(opts["--data-dir"], binary=opts["--bin"])
    except OSError as e:
        # Every prepare failure takes this path: the journal line below is
        # what `up` relays, and a traceback is not.
        msg, err = None, "%s: %s" % (e.__class__.__name__, e)
    if err:
        print(PREPARE_FAILED + err, file=sys.stderr)
        return 1
    print("helm chat node prepare: " + msg)
    return 0


# The mark a failed prepare leaves in the journal, which last_failure relays:
# a prepare that fails is the unit's own last words, as much as a node's
# REFUSING line is.
PREPARE_FAILED = "helm chat node prepare: FAILED — "


# ONE TABLE, AND IT IS THE ONLY PLACE A VERB IS DECLARED.
#
# The help drift this file's history is about was cured one layer up, in
# `chat.py`, by deriving the help entry from this module's usage string. That
# moved the drift rather than ending it: the usage was still hand prose
# sitting beside a dispatch dict built inline, so a verb could be advertised
# and undispatchable, or dispatchable and unadvertised, exactly as before —
# only one file further down. A SECOND COPY OF A VERB LIST IS A PROMISE TO
# UPDATE TWO THINGS FOREVER, and it is broken the first time somebody is in a
# hurry; `cmd_node`'s own docstring had already lost `refuel`.
#
# So the table carries the handler AND its one line, the dispatcher indexes
# it, and the usage is rendered from it. A verb with no description cannot be
# written down here, and a description with no handler will not run.
_VERBS = (
    ("up", lambda a: _up(a),
     "write the unit if missing (0600), record its binary, start, wait for\n"
     "           health, unlock + bootstrap the chain, store the node "
     "credential (0600), snapshot identity"),
    ("down", lambda a: _down(a),
     "stop the unit (the RAM room evaporates — flush first: "
     "helm chat log-flush)"),
    ("status", lambda a: _status(a),
     "unit + node health + chain head + joined-cell balances + identity"),
    ("prepare", lambda a: _prepare(a),
     "ExecStartPre [--data-dir D] [--bin B]: restore the chain descriptor\n"
     "           into the tmpfs cave, or mint one (keeping a snapshotted key)\n"
     "           and snapshot it — never re-key a live cave (run by the unit;\n"
     "           one at a time: a second refuses). D must NOT be a mount point:\n"
     "           the data dir is assembled beside it and renamed into place"),
    ("refuel", lambda a: _refuel(a),
     "state (and with --apply, sign) the move that puts computrons back\n"
     "           into the faucet cell the node pays every grant out of"),
)


def _usage():
    """The usage text, RENDERED from the dispatch table rather than restated.

    Returned rather than stored in a module constant so there is no name a
    later edit can update in isolation — the only way to change what this
    says is to change what the dispatcher does."""
    names = "|".join(name for name, _fn, _doc in _VERBS)
    lines = ["  %-8s %s" % (name, doc) for name, _fn, doc in _VERBS]
    return "usage: helm chat node <%s>\n%s" % (names, "\n".join(lines))


def cmd_node(args):
    """Supervise the chat tmpfs node.

    THE VERBS ARE NOT LISTED HERE ON PURPOSE. This docstring is a third place
    the list could rot, and it already had — it named four verbs while the
    dispatcher served five. `_VERBS` is the list; `_usage()` renders it."""
    verb = args[0] if args else "status"
    fn = dict((name, f) for name, f, _doc in _VERBS).get(verb)
    if fn is None:
        print(_usage(), file=sys.stderr)
        return 2
    return fn(args[1:])
