#!/usr/bin/env python3
"""helm cell — the OPTIONAL dregg-node leg.

helm rides dregg (the substrate) + cv (recall) ONLY. It never depends on the
meld binary/project — meld is a sibling that also rides dregg. Premise
attestation is helm-NATIVE (a stdlib append-only hash chain; see premise.py);
this module is only the OPTIONAL external checkpoint + a few node-liveness
reads, all over stdlib urllib. No 'meld' binary is needed for attestation.

Env law (env2 new-with-old-fallback): HELM_* is canonical; the legacy MELD_*
name is accepted READ-ONLY as a migration fallback (via home.env):

  HELM_NODE_URL        (MELD_NODE_URL)        node, default http://127.0.0.1:8899
  HELM_NODE_TOKEN      (MELD_NODE_TOKEN)      optional bearer for a gated node
  HELM_NODE_PASSPHRASE (MELD_NODE_PASSPHRASE) reserved (node unlock, operator side)

OPTIONAL a2a transport: a small explicit-opt-in escape hatch (`helm cell
<verb>` + chat's signed rows) can still drive a co-located cell binary when
HELM_CELL_BIN (legacy MELD_CELL_BIN) points at one. It is NEVER auto-resolved
(no PATH probe, no sibling-build guess) and NEVER used for attestation; with no
HELM_CELL_BIN set it degrades cleanly. helm is fully functional without it.

Import-safe, stdlib-only.
"""
import json
import os
import subprocess
import sys

from . import home

DEFAULT_NODE_URL = "http://127.0.0.1:8899"
# dregg REFUSES a turn whose fee budget is below the anchor's real computron cost
# (its EmitEvent costs ~100; dregg's DEFAULT_ANCHOR_FEE is 1000) — a `fee: 0`
# submit NEVER commits (see dregg node-target/src/lib.rs). Match it, env-overridable.
DEFAULT_ANCHOR_FEE = 1000
# The OPTIONAL anchor rides the capture path best-effort: keep the bound SMALL so
# a slow/hung node never noticeably delays capture (the native record is the proof).
DEFAULT_ANCHOR_TIMEOUT = 2


def node_url():
    return (home.env("NODE_URL") or DEFAULT_NODE_URL).rstrip("/")


def anchor_fee():
    """The per-anchor computron fee budget the submit stamps. dregg charges the
    EmitEvent a real cost and rejects an underfunded turn, so this is >= dregg's
    DEFAULT_ANCHOR_FEE. Override with HELM_NODE_ANCHOR_FEE (legacy MELD_*)."""
    v = home.env("NODE_ANCHOR_FEE")
    try:
        return int(v) if v not in (None, "") else DEFAULT_ANCHOR_FEE
    except (TypeError, ValueError):
        return DEFAULT_ANCHOR_FEE


def anchor_timeout():
    """The bounded best-effort anchor timeout on the capture path (small by
    design). Override with HELM_NODE_ANCHOR_TIMEOUT (legacy MELD_*)."""
    v = home.env("NODE_ANCHOR_TIMEOUT")
    try:
        return int(v) if v not in (None, "") else DEFAULT_ANCHOR_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_ANCHOR_TIMEOUT


def profile_name(default="helm-agent"):
    """The effective identity/recording label (HELM_CELL_PROFILE >
    MELD_AGENT_PROFILE > `default`). This is a provenance LABEL only — helm's
    native attestation is not a cell signature, so this never proves a signer.
    Attestation callers pass a safer default (see premise.attest_profile)."""
    return os.environ.get("HELM_CELL_PROFILE") \
        or os.environ.get("MELD_AGENT_PROFILE") or default


# ---------------------------------------------------------------------------
# node HTTP — stdlib urllib, fail-open (down/refused/garbled -> None)
# ---------------------------------------------------------------------------

def get_json(url, timeout=4):
    """One node GET -> parsed JSON, fail-open None. An HTTP error status is
    CLOSED before dropping — an abandoned HTTPError holds its socket and
    detonates under -W error::ResourceWarning."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        e.close()
        return None
    except Exception:
        return None


def post_json(url, payload, timeout=8, headers=None):
    """One node POST (JSON in, JSON out), same fail-open None law as get_json.
    `headers` layers over the default Content-Type (e.g. an Authorization
    bearer for a gated node)."""
    import urllib.error
    import urllib.request
    hdr = {"Content-Type": "application/json"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        e.close()
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# OPTIONAL external anchor — checkpoint a native record onto a dregg node
# ---------------------------------------------------------------------------
#
# This is the SECOND tier of attestation, never the first. The native hash
# chain (premise.py) is the primary, offline proof. IF a dregg node is
# configured + reachable, helm MAY post the record's hash to the node's thin
# /turn/submit ingress as an EXTERNAL ANCHOR. The node signs the turn with its
# OWN operator cell (confused-deputy hardening, dregg F-P1-3: the request
# `agent` is advisory only) — so the honest claim is "dregg node <url> anchored
# this digest at turn <hash>", NEVER "the user's cell signed". Best-effort +
# FAIL-OPEN: node absent/locked/refusing => no anchor, the native record still
# stands.

ANCHOR_TOPIC = "helm.attest"
ANCHOR_ENDPOINT = "/turn/submit"


def anchor_submit(rec_hash, memo=None, timeout=8):
    """POST `rec_hash` (a 64-hex native record/chain-head hash) to the dregg
    node's thin ingress as an external anchor. (turn_hash, None) on a node-
    accepted anchor; (None, reason) fail-open otherwise. Never raises, never a
    signer claim."""
    word = (rec_hash or "").strip().lower()
    data_words = [word] if len(word) == 64 and all(
        c in "0123456789abcdef" for c in word) else []
    body = {
        "agent": "00" * 32,          # advisory only — the node signs as itself
        "nonce": 0, "fee": anchor_fee(),   # fee:0 NEVER commits on dregg
        "memo": memo or ("helm-attest:" + (rec_hash or "")),
        "actions": [{
            "method": "attest",
            "effects": [{"kind": "emit_event", "topic": ANCHOR_TOPIC,
                         "data": data_words}],
        }],
    }
    headers = {}
    tok = home.env("NODE_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok
    resp = post_json(node_url() + ANCHOR_ENDPOINT, body, timeout=timeout,
                     headers=headers)
    if resp is None:
        return None, "node unreachable/locked at " + node_url()
    if not isinstance(resp, dict):   # a valid JSON list/string is NOT acceptance
        return None, "node returned a non-object anchor response — fail open"
    if resp.get("accepted") and resp.get("turn_hash"):
        return resp["turn_hash"], None
    return None, "node did not accept the anchor: %s" \
        % (resp.get("error") or "no turn_hash")


def anchor_label(turn_hash):
    """The HONEST one-line description of an external anchor — node-anchored,
    never cell-signed."""
    return "dregg node %s anchored digest at turn %s" % (node_url(), turn_hash)


def verify_anchor(turn_hash, timeout=4):
    """Best-effort read-back: does the dregg node still show a turn with this
    hash? (observed, detail). Tries /api/turn/<hash>/proof then the starbridge
    receipt filter.

    HONEST SCOPE (helm A1): this only OBSERVES that a turn with the stored hash
    EXISTS on the node — it does NOT prove that turn's EmitEvent carries the
    entry's native record hash. attest_anchor_turn is mutable frontmatter and is
    not part of the native record, so an unrelated real turn could be substituted
    and still 'observe' here. Until dregg exposes payload disclosure helm can
    consume, this is 'turn observed', NEVER independent re-verification of the
    external commitment. Reports only what the node shows — never a signer
    identity helm cannot prove."""
    url = node_url()
    proof = get_json("%s/api/turn/%s/proof" % (url, turn_hash), timeout=timeout)
    if isinstance(proof, dict) and proof.get("turn_hash"):
        return True, "turn present on %s (payload binding unavailable)" % url
    rec = get_json("%s/api/starbridge/receipts?turn_hash=%s" % (url, turn_hash),
                   timeout=timeout)
    if isinstance(rec, list) and rec:
        return True, "receipt present on %s (payload binding unavailable)" % url
    if proof is None and rec is None:
        return False, "node unreachable at " + url
    return False, "turn not found on " + url


# ---------------------------------------------------------------------------
# OPTIONAL a2a transport (explicit opt-in, NEVER attestation, always degrades)
# ---------------------------------------------------------------------------

# HELM_* -> every name a configured signer bin reads: the dregg-native
# dregg-client-sign (DREGG_*) and the legacy meld-style cell bin (MELD_*).
# One pair per target so build_env stays a plain loop; a HELM var with two
# targets simply appears twice.
ENV_MAP = (
    ("HELM_NODE_URL", "MELD_NODE_URL"),
    ("HELM_NODE_URL", "DREGG_NODE_URL"),
    ("HELM_CELL_PROFILE", "MELD_AGENT_PROFILE"),
    ("HELM_CELL_PROFILE", "DREGG_PROFILE"),
    ("HELM_NODE_TOKEN", "MELD_NODE_TOKEN"),
    ("HELM_NODE_TOKEN", "DREGG_API_TOKEN"),
    ("HELM_NODE_PASSPHRASE", "MELD_NODE_PASSPHRASE"),
    ("HELM_NODE_PASSPHRASE", "DREGG_NODE_PASSPHRASE"),
    ("HELM_ROSTER", "MELD_ROSTER"),
)

PASS_VERBS = ("join", "accept", "send", "recv", "heartbeat", "roster")

_USAGE = """usage: helm cell <verb> [args]
  status                                         node liveness (dregg HTTP)
  join|accept|send|recv|heartbeat|roster [...]   OPTIONAL a2a transport — only
                                                 when HELM_CELL_BIN points at a
                                                 cell binary; degrades otherwise
                                                 (attestation never needs it)"""


def build_env():
    """Subprocess env for the OPTIONAL a2a transport: a copy of os.environ with
    each set HELM_* mapped onto its signer-facing names (legacy MELD_* and
    dregg-native DREGG_*; HELM wins; absent HELM leaves a directly-set
    MELD_*/DREGG_* untouched — the env2 pattern)."""
    env = dict(os.environ)
    for h, m in ENV_MAP:
        v = os.environ.get(h)
        if v is not None:
            env[m] = v
    return env


def bin_path():
    """The OPTIONAL a2a cell binary, EXPLICIT only: HELM_CELL_BIN (legacy
    MELD_CELL_BIN). Never auto-resolved — no PATH probe, no sibling-build guess
    — so helm never silently execs a binary. None => the transport degrades."""
    return home.env("CELL_BIN") or None


def _usable(b):
    """A configured signer is real ONLY when it is a regular file AND
    executable — a directory or a non-executable file (mode 0600, etc.) is NOT
    a usable binary. os.path.exists() would say yes and let status claim
    'signed' + let the signing leg burn an unlock lap; isfile + X_OK is the
    minimum honest meaning of 'signer ready' (codex day-review #1)."""
    return bool(b and os.path.isfile(b) and os.access(b, os.X_OK))


def bin_status():
    """One signer-configuration truth for send + every status projection.
    The configured path is deliberately not returned: diagnostics identify the
    failure class without publishing a possibly hostile path string."""
    b = bin_path()
    if not b:
        return {"configured": False, "usable": False, "state": "unset",
                "reason": "HELM_CELL_BIN is unset"}
    if not os.path.exists(b):
        return {"configured": True, "usable": False, "state": "missing",
                "reason": "HELM_CELL_BIN is set but the signer path does not exist"}
    if not os.path.isfile(b):
        return {"configured": True, "usable": False, "state": "not_file",
                "reason": "HELM_CELL_BIN is set but the signer path is not a regular file"}
    if not _usable(b):
        return {"configured": True, "usable": False,
                "state": "not_executable",
                "reason": "HELM_CELL_BIN is set but the signer file is not executable"}
    return {"configured": True, "usable": True, "state": "ready",
            "reason": "signer ready"}


def bin_ready():
    """True only when the configured signer is a real executable file."""
    return bin_status()["usable"]


def roster_path():
    return home.env("ROSTER") \
        or os.path.join(os.path.expanduser("~"), ".dregg", "roster.toml")


def run_bin(args, timeout=90, env_extra=None):
    """Run the OPTIONAL cell binary captured. (rc, stdout, stderr); rc None +
    reason when no binary is configured (HELM_CELL_BIN unset) or launch fails.
    env_extra lays over the mapped env."""
    b = bin_path()
    status = bin_status()
    if not status["usable"]:
        return None, "", "a2a transport unavailable — " + status["reason"]
    env = build_env()
    env.update(env_extra or {})
    try:
        p = subprocess.run([b] + args, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except OSError as exc:
        detail = exc.strerror or exc.__class__.__name__
        return None, "", "configured signer could not execute (%s)" % detail
    except subprocess.TimeoutExpired:
        return None, "", "cell %s timed out after %ss" % (args[0], timeout)
    return p.returncode, p.stdout, p.stderr


def _last_json(out):
    """The a2a binary's stdout contract: one JSON object line (progress goes to
    stderr). Parse the last JSON-looking stdout line, fail-open."""
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


def _roster_summary():
    """Roster view: the a2a binary's own `roster --json` when a binary is
    configured, else a regex read of the roster file."""
    path = roster_path()
    rc, out, _err = run_bin(["roster", "--json"], timeout=15)
    if rc == 0 and out.strip().startswith(("[", "{")):
        try:
            data = json.loads(out)
            cells = data if isinstance(data, list) else data.get("cells") or []
            labels = [str(c.get("label") or "") for c in cells if isinstance(c, dict)]
            n = len(cells)
            return "roster %s: %d cell%s%s" % (
                path, n, "s"[:n != 1],
                (" (" + ", ".join(l for l in labels if l) + ")") if any(labels) else "")
        except (ValueError, AttributeError):
            pass
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
        print("helm cell: node LIVE at %s — chain head %s (finality %s)"
              % (url, h.get("chain_index"), h.get("finality")))
    elif live:
        print("helm cell: node LIVE at %s — no receipts yet" % url)
    else:
        print("helm cell: node UNREACHABLE at %s (attestation is native + "
              "offline; the node is only an optional anchor)" % url)
    print("helm cell: " + _roster_summary())
    b = bin_path()
    signer = bin_status()
    if signer["usable"]:
        print("helm cell: optional a2a binary " + b)
    elif signer["configured"]:
        print("helm cell: optional a2a signer UNAVAILABLE — %s" % signer["reason"])
    else:
        print("helm cell: optional a2a transport OFF (no HELM_CELL_BIN) — "
              "attestation does not need it")
    return 0 if live and (not signer["configured"] or signer["usable"]) else 1


def cmd_cell(args):
    """cell <status|join|accept|send|recv|heartbeat|roster> — node liveness +
    the OPTIONAL a2a transport (degrades with no HELM_CELL_BIN)."""
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
    signer = bin_status()
    if not signer["usable"]:
        print("helm cell: a2a transport unavailable — %s" % signer["reason"],
              file=sys.stderr)
        return 1
    try:
        return subprocess.call([b, verb] + rest, env=build_env())
    except OSError as exc:
        print("helm cell: a2a transport unavailable — %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
