#!/usr/bin/env python3
"""helm cell — the substrate leg: a helm-named wrapper over the meld binary
(the predecessor's cell/whisper/roster machinery, surfaced as helm verbs).

Env law (the env2 new-with-old-fallback pattern): every verb maps HELM_* to the
binary's MELD_* internally —

  HELM_NODE_URL        -> MELD_NODE_URL        (node, default http://127.0.0.1:8899)
  HELM_CELL_PROFILE    -> MELD_AGENT_PROFILE   (identity profile in ~/.dregg/profiles)
  HELM_NODE_TOKEN      -> MELD_NODE_TOKEN      (bearer for /turns/submit)
  HELM_NODE_PASSPHRASE -> MELD_NODE_PASSPHRASE (unlock alternative to the token)
  HELM_ROSTER          -> MELD_ROSTER          (roster file, default ~/.dregg/roster.toml)

A HELM_* value wins; with no HELM_* set the binary sees its own legacy MELD_*
untouched. Binary resolution: HELM_CELL_BIN (legacy MELD_CELL_BIN) env, else
the known build, else `meld` on PATH; a missing binary is a graceful
"substrate unavailable" line, never a traceback.

Profile/cell mechanics (read off the meld + dregg-sdk source): a profile is
~/.dregg/profiles/<name>.json {name, seed_hex, public_key_hex, created_at};
its cell id is CellId::derive_raw(public_key, blake3(domain)) — blake3, which
this host cannot compute in Python — so own_cell() gets the hex the honest
way: `meld join` (idempotent on the node; creates + faucet-funds the profile
on first use) prints one stdout JSON object carrying "cell", cached per
profile in <helm-home>/_global/.state/cells.json.

Import-safe, stdlib-only.
"""
import json
import os
import shutil
import subprocess
import sys

from . import home, pk

KNOWN_BIN = "/home/owner/dev/akapug/meld/target/release/meld"
DEFAULT_NODE_URL = "http://127.0.0.1:8899"

ENV_MAP = (
    ("HELM_NODE_URL", "MELD_NODE_URL"),
    ("HELM_CELL_PROFILE", "MELD_AGENT_PROFILE"),
    ("HELM_NODE_TOKEN", "MELD_NODE_TOKEN"),
    ("HELM_NODE_PASSPHRASE", "MELD_NODE_PASSPHRASE"),
    ("HELM_ROSTER", "MELD_ROSTER"),
)

# The binary's own subcommands, passed through verbatim (env mapped).
PASS_VERBS = ("join", "accept", "send", "recv", "heartbeat", "roster")

_USAGE = """usage: helm cell <verb> [args]
  join|accept|send|recv|heartbeat|roster [...]   pass through to the substrate
                                                 binary (HELM_* env mapped in)
  status                                         node liveness + profile + roster"""


def build_env():
    """The subprocess env: a copy of os.environ with each set HELM_* mapped
    onto its MELD_* name (HELM wins; absent HELM leaves legacy MELD_* as the
    binary's own fallback — the env2 pattern)."""
    env = dict(os.environ)
    for h, m in ENV_MAP:
        v = os.environ.get(h)
        if v is not None:
            env[m] = v
    return env


def bin_path():
    """HELM_CELL_BIN (legacy MELD_CELL_BIN) env, else the known build path,
    else `meld` on PATH; None when nothing resolves."""
    explicit = home.env("CELL_BIN")
    if explicit:
        return explicit
    if os.path.exists(KNOWN_BIN):
        return KNOWN_BIN
    return shutil.which("meld")


def node_url():
    return (home.env("NODE_URL") or DEFAULT_NODE_URL).rstrip("/")


def profile_name(default="meld-agent"):
    """The effective identity profile (HELM_CELL_PROFILE > MELD_AGENT_PROFILE >
    `default`). Passthrough/status callers keep the binary's own default
    (meld-agent); attestation callers pass a safer default (see premise.py)."""
    return os.environ.get("HELM_CELL_PROFILE") \
        or os.environ.get("MELD_AGENT_PROFILE") or default


def profiles_dir():
    return os.path.join(os.path.expanduser("~"), ".dregg", "profiles")


def roster_path():
    return home.env("ROSTER") \
        or os.path.join(os.path.expanduser("~"), ".dregg", "roster.toml")


def run_bin(args, timeout=90):
    """Run the binary captured. (rc, stdout, stderr); rc None + reason in the
    third slot when the substrate is unavailable (missing binary/launch fail)."""
    b = bin_path()
    if not b or not os.path.exists(b):
        return None, "", ("meld binary not found (set HELM_CELL_BIN, or build "
                          + KNOWN_BIN + ", or put `meld` on PATH)")
    try:
        p = subprocess.run([b] + args, env=build_env(), capture_output=True,
                           text=True, timeout=timeout)
    except OSError as exc:
        return None, "", "meld binary failed to launch: %s" % exc
    except subprocess.TimeoutExpired:
        return None, "", "meld %s timed out after %ss" % (args[0], timeout)
    return p.returncode, p.stdout, p.stderr


def _last_json(out):
    """The binary's stdout contract: exactly one JSON object line (progress
    goes to stderr). Parse the last JSON-looking stdout line, fail-open."""
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# own-cell resolution + the self-write send path (premise.py's substrate seam)
# ---------------------------------------------------------------------------

def _cells_cache_path():
    return os.path.join(home.global_dir(), ".state", "cells.json")


def own_cell(profile):
    """The profile's own cell hex: cache-first; on a miss run `meld join
    --profile P` (idempotent; first use creates + funds the profile) and parse
    "cell" from its stdout JSON. Returns (cell_hex, None) or (None, reason)."""
    cache = pk.read_json(_cells_cache_path(), {}) or {}
    hexid = cache.get(profile)
    if hexid:
        return hexid, None
    rc, out, err = run_bin(["join", "--profile", profile])
    if rc is None:
        return None, err
    if rc != 0:
        return None, "meld join failed (rc %d): %s" % (rc, (err or out).strip()[-300:])
    info = _last_json(out)
    if not (info and info.get("cell")):
        return None, "meld join printed no cell id: %s" % (out or "").strip()[-200:]
    cache[profile] = info["cell"]
    pk.write_json(_cells_cache_path(), cache)
    return info["cell"], None


def send_self(payload, profile):
    """SELF-WRITE: deposit `payload` into the profile's OWN whisper PAYLOAD
    slots via the proven `meld send` path — never the 8-byte heartbeat tag
    (decision-record law). Returns (send-info dict with turn_hash/receipt_hash/
    chain_index, None) or (None, reason)."""
    hexid, err = own_cell(profile)
    if err:
        return None, err
    rc, out, err2 = run_bin(["send", "--profile", profile, "--to", hexid, payload])
    if rc is None:
        return None, err2
    if rc != 0:
        return None, "meld send failed (rc %d): %s" % (rc, (err2 or out).strip()[-300:])
    info = _last_json(out)
    if not (info and info.get("sent")):
        return None, "meld send printed no receipt JSON: %s" % (out or "").strip()[-200:]
    return info, None


def get_json(url, timeout=4):
    """One node GET -> parsed JSON, fail-open None (down/refused/garbled)."""
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _roster_summary():
    path = roster_path()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return "roster %s (absent)" % path
    import re
    labels = re.findall(r'label\s*=\s*"([^"]*)"', raw)
    n = raw.count("[[cell]]")
    return "roster %s: %d cell%s%s" % (
        path, n, "s"[:n != 1], (" (" + ", ".join(labels) + ")") if labels else "")


def _status(args):
    url = node_url()
    receipts = get_json(url + "/api/receipts")
    live = receipts is not None
    if isinstance(receipts, list) and receipts:
        h = receipts[0]
        print("helm cell: node LIVE at %s — chain head %s (finality %s, "
              "consensus_final %s, attested_height %s)" % (
                  url, h.get("chain_index"), h.get("finality"),
                  h.get("consensus_final"), h.get("attested_height")))
    elif live:
        print("helm cell: node LIVE at %s — no receipts yet" % url)
    else:
        print("helm cell: node UNREACHABLE at %s" % url)
    p = profile_name()
    pj = os.path.join(profiles_dir(), p + ".json")
    state = "exists" if os.path.exists(pj) \
        else "not yet created — `helm cell join` mints it"
    print("helm cell: profile '%s' (%s)" % (p, state))
    print("helm cell: " + _roster_summary())
    b = bin_path()
    if not b or not os.path.exists(b):
        print("helm cell: substrate binary MISSING (set HELM_CELL_BIN)")
    else:
        print("helm cell: binary " + b)
    return 0 if live else 1


def cmd_cell(args):
    """cell <join|accept|send|recv|heartbeat|roster|status> — the substrate leg."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb == "status":
        return _status(rest)
    if verb not in PASS_VERBS:
        print("helm cell: unknown verb '%s'" % verb, file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2
    b = bin_path()
    if not b or not os.path.exists(b):
        print("helm cell: substrate unavailable — meld binary not found "
              "(set HELM_CELL_BIN, or build " + KNOWN_BIN
              + ", or put `meld` on PATH)", file=sys.stderr)
        return 1
    try:
        return subprocess.call([b, verb] + rest, env=build_env())
    except OSError as exc:
        print("helm cell: substrate unavailable — %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
