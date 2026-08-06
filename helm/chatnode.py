#!/usr/bin/env python3
"""helm chat node — supervision of the chat room node (a dregg tmpfs node).

INTERIM SCAFFOLDING by design (one-node-per-team law, PRD 2026-07-19): the
target topology is one team node; interim, chat gets its OWN tmpfs node so a signed
chat turn can never land on a disk-persisted chain. Everything here is
node-agnostic — the node migration (scripts/node-migration.sh)
repoints the url in the state file and this module keeps working unchanged.

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
token} — provisioning config written at `node up` time, never in the
send/read hot path (the RAM canon governs message data; the unit file and
this credential live on disk exactly like every other service config).

Import-safe, stdlib-only.
"""
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import time

from . import cell, home, pk

UNIT = "helm-chat-node.service"
DATA_DIR = "/dev/shm/helm-chat-node"
PORT = 8898
GOSSIP_PORT = 18898
HEALTH_WAIT_S = 30
# The node's identity, as opposed to its data. `init` writes only node.key;
# the seed and agent key appear once the node has run. The ledger
# (dregg.redb) is deliberately ABSENT from this list — see identity_dir().
IDENTITY_FILES = ("node.key", "starbridge-seed.json", "agent-alice.key")

_USAGE = """usage: helm chat node <up|down|status|prepare>
  up       write the unit if missing (0600), start, wait for health, unlock +
           bootstrap the chain, store the node credential (0600), snapshot identity
  down     stop the unit (the RAM room evaporates — flush first: helm chat log-flush)
  status   unit + node health + chain head + joined-cell balances + identity
  prepare  ExecStartPre: restore the node identity into the tmpfs cave, or mint
           and snapshot one — never re-key a live cave (run by the unit)"""


def bin_path():
    """HELM_CHAT_NODE_BIN, else `dregg-cave-node` on PATH, else the known
    install; None when nothing resolves."""
    explicit = home.env("CHAT_NODE_BIN")
    if explicit:
        return explicit
    on_path = shutil.which("dregg-cave-node")
    if on_path:
        return on_path
    known = os.path.join(os.path.expanduser("~"), ".local", "bin", "dregg-cave-node")
    return known if os.path.exists(known) else None


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
    ephemeral orca handle (fixed by `helm seat rebind`); this data
    dir had no re-init across a boot (since fixed); and the identity inside it had
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
    """Copy the cave's identity files to disk (0600). Returns (saved, err).

    Content-addressed and idempotent: an unchanged file is not rewritten, so
    calling this on every `node up` costs nothing and never churns mtimes.
    Files absent from the cave are skipped, not invented — a fresh `init`
    produces only node.key, and the rest appear once the node has run."""
    d = identity_dir()
    saved = []
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        for name in IDENTITY_FILES:
            src = os.path.join(data_dir, name)
            if not os.path.isfile(src):
                continue
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


def restore_identity(data_dir=DATA_DIR):
    """Copy a snapshotted identity INTO a cave. Returns (restored, err).

    A file already present in the cave is NEVER overwritten: a live node's own
    identity outranks the snapshot, so a restore can never re-key something
    that is running."""
    d = identity_dir()
    restored = []
    try:
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        for name in IDENTITY_FILES:
            src = os.path.join(d, name)
            dst = os.path.join(data_dir, name)
            if not os.path.isfile(src) or os.path.exists(dst):
                continue
            with open(src, "rb") as f:
                blob = f.read()
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
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
    saved = [n for n in IDENTITY_FILES if os.path.isfile(os.path.join(d, n))]
    live = [n for n in IDENTITY_FILES if os.path.isfile(os.path.join(data_dir, n))]
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


def prepare(data_dir=DATA_DIR, binary=None):
    """ExecStartPre: guarantee a KEYED data dir without ever re-keying one.

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
    `init` only when there is nothing to restore — where it creates the dir
    itself, so the empty-dir trap is unreachable by construction."""
    if os.path.isdir(data_dir) and os.listdir(data_dir):
        return "data-dir live at %s — untouched" % data_dir, None
    restored, err = restore_identity(data_dir)
    if err:
        return None, err
    if restored:
        return ("restored node identity (%s) into %s — same node across the "
                "reboot" % (", ".join(restored), data_dir)), None
    b = binary or bin_path()
    if not b:
        return None, ("dregg-cave-node binary not found and no identity "
                      "snapshot to restore — set HELM_CHAT_NODE_BIN")
    # Nothing to restore: `init` must create the dir ITSELF, so remove the
    # empty one restore_identity made or init silently does nothing.
    try:
        if os.path.isdir(data_dir) and not os.listdir(data_dir):
            os.rmdir(data_dir)
    except OSError as e:
        return None, "could not clear the empty data-dir: %s" % e
    try:
        p = subprocess.run([b, "init", "--data-dir", data_dir],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "init failed: %s" % e
    if p.returncode != 0:
        return None, "init failed (rc %s): %s" % (
            p.returncode, (p.stderr or p.stdout or "").strip()[:200])
    if not os.path.isfile(os.path.join(data_dir, "node.key")):
        return None, ("init reported success but wrote no node.key to %s — "
                      "refusing to start a keyless node" % data_dir)
    saved, serr = snapshot_identity(data_dir)
    if serr:
        return None, serr
    return ("minted a new node identity and snapshotted it (%s) — this boot "
            "starts fresh, the next one will not" % ", ".join(saved)), None


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
    a way kimi reproduced live. systemd's Environment= takes N whitespace-
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

    EMPTY POSTURE HAS TWO MEANINGS and conflating them was a real hole, found by
    kimi and reproduced live. "Never declared" must write nothing — dregg's
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


def last_failure(unit=UNIT, lines=40):
    """The service's OWN last words, or None.

    `_up` used to report "the unit started but the API never answered", which is
    a TIMEOUT — a symptom shared by every possible cause. The service had already
    said precisely what was wrong ("REFUSING TO START: lean_available() is false
    ... set DREGG_ALLOW_UNVERIFIED_CONSENSUS=1"), in one actionable sentence, and
    helm replaced it with the least informative true statement available. A
    provisioner that owns a service must relay that service's diagnosis, not
    paraphrase its silence.
    """
    try:
        p = subprocess.run(["journalctl", "--user", "-u", unit, "-n", str(lines),
                            "--no-pager", "-o", "cat"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    hits = [ln.strip() for ln in p.stdout.splitlines()
            if "REFUSING" in ln or " ERROR " in ln]
    return hits[-1] if hits else None


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
    nobody can see is worse than a dead service everyone can."""
    return """[Unit]
Description=helm chat node (dregg tmpfs node :%d)
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
ExecStartPre=%s chat node prepare --data-dir %s
ExecStart=%s run --data-dir %s --port %d --gossip-port %d --enable-faucet
Environment=RUST_LOG=info,dregg_node::blocklace_sync=warn
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
""" % (port, helm_bin(), data_dir,
       binary, data_dir, port, gossip)


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


def faucet(url, recipient, amount):
    """One faucet grant, validated instead of fire-and-forgotten. Returns the
    accepted response or a precise reason. The node's contract is
    {success:true}; reachable JSON with success:false is a refusal, not health."""
    r = cell.post_json(url + "/api/faucet",
                       {"recipient": recipient, "amount": amount})
    if not isinstance(r, dict):
        return None, "faucet unreachable or returned a non-object response at %s" % url
    if r.get("success") is not True:
        return None, "faucet refused: %s" % (r.get("error") or "success was not true")
    return r, None


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


def wait_api(url, seconds=HEALTH_WAIT_S):
    """Wait for the HTTP API to answer /api/receipts. True iff it did."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if isinstance(cell.get_json(url + "/api/receipts", timeout=2), list):
            return True
        time.sleep(0.5)
    return False


def _up(args):
    b = bin_path()
    if not b:
        print("helm chat node: dregg-cave-node binary not found — set "
              "HELM_CHAT_NODE_BIN or install to ~/.local/bin", file=sys.stderr)
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
    for verb in (("daemon-reload",), ("enable", "--now", UNIT)):
        rc, out = _systemctl(*verb)
        if rc not in (0,):
            print("helm chat node: systemctl %s failed: %s" % (" ".join(verb), out),
                  file=sys.stderr)
            return 1
    url = default_url()
    if not wait_api(url):
        # RELAY THE SERVICE'S OWN DIAGNOSIS. "The API never answered" is a
        # timeout: true of a refused start, a wrong port, a missing data-dir and
        # a slow boot alike. dregg says exactly which, so say that instead.
        why = last_failure()
        msg = ("helm chat node: the unit did not come up at %s" % url)
        if why:
            msg += "\n  the service said: " + why
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
    url = chat.node_url() or default_url()
    transport = chat.transport_status()
    if transport.get("mode") == "degraded":
        print("helm chat node: " + chat.transport_failure_summary(transport),
              file=sys.stderr)
    elif transport.get("mode") == "ready":
        print("helm chat node: signing " + chat.transport_label(transport))
    head = cell.get_json(url + "/api/receipts", timeout=3)
    if head is None:
        print("helm chat node: API UNREACHABLE at %s — chat posts fall back "
              "to [unsigned]" % url)
        return 1
    print("helm chat node: API LIVE at %s — chain head %s" % (
        url, head[0].get("chain_index") if head else "(no receipts yet)"))
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
    stale = cell.signer_staleness()
    if stale.get("state") == "stale":
        print("helm chat node: SIGNER STALE — %s"
              % chat._safe_reason(stale["reason"]))
    elif stale.get("state") == "unknown":
        print("helm chat node: signer freshness UNKNOWN — %s (unproven is not "
              "the same as current)" % chat._safe_reason(stale.get("reason")))
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
    return 1 if transport.get("mode") == "degraded" else 0


def _prepare(args):
    """ExecStartPre body. Loud on both paths: systemd swallows nothing here,
    and a silent prepare is what let a keyless cave look like a healthy one."""
    data_dir = DATA_DIR
    if "--data-dir" in args:
        i = args.index("--data-dir")
        if i + 1 >= len(args):
            print("helm chat node prepare: --data-dir needs a path", file=sys.stderr)
            return 2
        data_dir = args[i + 1]
    msg, err = prepare(data_dir)
    if err:
        print("helm chat node prepare: " + err, file=sys.stderr)
        return 1
    print("helm chat node prepare: " + msg)
    return 0


def cmd_node(args):
    """node up|down|status|prepare — supervise the chat tmpfs node."""
    verb = args[0] if args else "status"
    fn = {"up": _up, "down": _down, "status": _status,
          "prepare": _prepare}.get(verb)
    if fn is None:
        print(_USAGE, file=sys.stderr)
        return 2
    return fn(args[1:])
