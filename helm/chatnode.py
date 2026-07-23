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

_USAGE = """usage: helm chat node <up|down|status>
  up      write the unit if missing (0600), start, wait for health, unlock +
          bootstrap the chain, store the node credential (0600)
  down    stop the unit (the RAM room evaporates — flush first: helm chat log-flush)
  status  unit + node health + chain head + joined-cell balances"""


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


def unit_path():
    return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", UNIT)


def unit_text(binary, data_dir=DATA_DIR, port=PORT, gossip=GOSSIP_PORT):
    """The unit body — mirrors the team node's dregg-cave.service, tmpfs
    data-dir + its own ports. Faucet ON: chat turns must never die on
    computrons (the provisioning exhaustion lesson)."""
    return """[Unit]
Description=helm chat node (dregg tmpfs node :%d)
After=network.target

[Service]
Type=simple
ExecStart=%s run --data-dir %s --port %d --gossip-port %d --enable-faucet
Environment=RUST_LOG=info,dregg_node::blocklace_sync=warn
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
""" % (port, binary, data_dir, port, gossip)


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
    if not r.get("success"):
        return None, "unlock refused: %s" % (r.get("error") or r)
    return r.get("bearer_token") or "", None


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
    if not r.get("success"):
        return None, "faucet refused: %s" % (r.get("error") or "success was false")
    return r, None


def ensure_healthy(url):
    """The node's client-facing health gate demands one committed block; a
    faucet turn to the throwaway bootstrap cell provides it. Returns
    (True, None) or (False, precise reason) — the faucet response is never
    discarded."""
    st = cell.get_json(url + "/status", timeout=3)
    if isinstance(st, dict) and st.get("healthy"):
        return True, None
    _r, err = faucet(url, bootstrap_cell_hex(), 1)
    if err:
        return False, err
    for _ in range(20):
        st = cell.get_json(url + "/status", timeout=3)
        if isinstance(st, dict) and st.get("healthy"):
            return True, None
        time.sleep(0.25)
    return False, "faucet accepted but the node never became healthy at %s" % url


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
    healthy, err = ensure_healthy(url)
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
    if not os.path.exists(up_path):
        os.makedirs(os.path.dirname(up_path), exist_ok=True)
        fd = os.open(up_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(unit_text(b))
        print("helm chat node: unit written (0600): " + up_path)
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    for verb in (("daemon-reload",), ("enable", "--now", UNIT)):
        rc, out = _systemctl(*verb)
        if rc not in (0,):
            print("helm chat node: systemctl %s failed: %s" % (" ".join(verb), out),
                  file=sys.stderr)
            return 1
    url = default_url()
    if not wait_api(url):
        print("helm chat node: unit started but the API never answered at %s "
              "(journalctl --user -u %s)" % (url, UNIT), file=sys.stderr)
        return 1
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
    return 1 if transport.get("mode") == "degraded" else 0


def cmd_node(args):
    """node up|down|status — supervise the chat tmpfs node."""
    verb = args[0] if args else "status"
    fn = {"up": _up, "down": _down, "status": _status}.get(verb)
    if fn is None:
        print(_USAGE, file=sys.stderr)
        return 2
    return fn(args[1:])
