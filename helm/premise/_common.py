"""helm premise — shared state: top-level imports, constants, leaf helpers.

This is the base of the one-way cluster dep graph:
    _common <- _chain <- _verify <- _capture <- _cli
Everything here is imported by the clusters; it imports none of them.
"""
import hashlib
import json
import os
import re
import sys
import unicodedata

try:
    import fcntl                  # POSIX inter-process advisory lock (Linux fleet)
except ImportError:               # pragma: no cover - non-POSIX; the fleet is Linux
    fcntl = None

from .. import cell, home, pk, store

DIGEST_TAG = "prem:b2b:"          # blake2b-256 (see module docstring)
CHAIN_V = 2                       # native attestation-chain schema/evidence version
DEFAULT_PROFILE = "helm-test"     # recording label default — never the user's cell
RETRY_PAUSE_S = 2                 # pause between queued anchor-retry failures
# premise-check exit contract: 0 = primary proof VERIFIED (digest matches AND the
# native record recomputes AND binds to this premise); 1 = present but BROKEN
# (mismatch/tamper/foreign record); EXIT_NO_NATIVE_PROOF = the primary proof is
# ABSENT (never attested / --no-attest / legacy). Absence must NEVER look like
# success to automation, but is distinct from a present-but-broken proof.
EXIT_NO_NATIVE_PROOF = 3

# The tamper-evident core of a native record (hashed; prev/rec_hash/chain_index
# are structural, never part of the hashed body).
_CORE_KEYS = ("v", "op", "premise_id", "root", "project", "digest", "ts",
              "source", "attest_by", "supersedes", "supersedes_record")

_USAGE_PREMISE = ("usage: helm premise <id> | <statement> [| keywords [| domain]] "
                  "[--project P] [--no-attest] [--force-new]\n"
                  "       helm premise --supersede <old-id> <new-id> | <statement> "
                  "[| keywords [| domain]] [--project P] [--force-new]\n"
                  "       helm premise --retry-queue\n"
                  "       helm premise --attest-existing <id> [--project P]\n"
                  "       helm premise --attest-sweep [--dry] [--limit N]")
_USAGE_CHECK = "usage: helm premise-check <id> [--chain] [--project P]"

_CHECK_DEFAULTS = {"attest_payload": "", "attest_ts": "", "attest_by": "",
                   "attest_record": "", "attest_chain_index": "",
                   "attest_supersedes_record": "", "attest_anchor": "",
                   "attest_anchor_turn": ""}


# ---------------------------------------------------------------------------
# canonicalization + digest
# ---------------------------------------------------------------------------

def canonicalize(statement):
    """The exact canonical text the digest covers (see module docstring):
    NFC -> '\"'->\"'\" -> collapse-whitespace -> strip."""
    s = unicodedata.normalize("NFC", statement or "")
    s = s.replace('"', "'")
    return re.sub(r"\s+", " ", s).strip()


def digest_payload(statement):
    """The algorithm-tagged statement digest (73 bytes)."""
    h = hashlib.blake2b(canonicalize(statement).encode("utf-8"), digest_size=32)
    return DIGEST_TAG + h.hexdigest()


def payload_digest(payload):
    """The 64-hex statement digest inside a prem: payload ('' if not one)."""
    return payload[len(DIGEST_TAG):].split(":")[0] if payload.startswith(DIGEST_TAG) else ""


# ---------------------------------------------------------------------------
# frontmatter helpers + provenance-root label
# ---------------------------------------------------------------------------

def _front(e, key):
    """Read `key` from the entry dict, falling back to its frontmatter file."""
    v = e.get(key)
    if v not in (None, ""):
        return v
    if e.get("path"):
        return (pk.parse_simple_frontmatter(e["path"], {key: ""}) or {}).get(key) or ""
    return ""


def _root_label(e, project):
    return e.get("root") or ("project" if project else "global")


# ---------------------------------------------------------------------------
# the anchor-retry queue path (only the OPTIONAL external anchor ever queues)
# ---------------------------------------------------------------------------

def _queue_path():
    return os.path.join(home.global_dir(), ".state", "attest-queue.jsonl")
