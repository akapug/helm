#!/usr/bin/env python3
"""helm premise — attested premise capture, NATIVE-primary.

A premise (a confidence-1.0 truth) is written into the typed store
(store.write_prior — the same path `helm store add premise` takes) AND attested
by helm ITSELF, with no external binary and no network dependency. The PRIMARY
attestation is a stdlib, append-only, tamper-evident HASH CHAIN kept under the
helm home; a dregg node, if one is reachable, is an OPTIONAL external anchor.

WHY NATIVE-PRIMARY (the corrected de-Meld design). helm rides dregg + cv only;
it never depends on the meld binary. An agent-run capture cannot be signed by
the owner's cell, and the stdlib-feasible dregg ingress is signed by the NODE
operator, not the user — so a "the user's cell signed this premise" claim would
be dishonest. Instead the proof is the QUOTE plus a chain: provenance (owner
said X, at T, source) bound into a record whose hash links to its predecessor.
This verifies OFFLINE and is the primary evidence. The dregg anchor, when
present, is honestly labelled "dregg node <url> anchored digest at turn <hash>"
— never a signer claim.

THE NATIVE CHAIN (<helm-home>/_global/.state/attest-chain.jsonl). One JSON
record per line. Each record's tamper-evident CORE carries:
  v                 schema/evidence version (CHAIN_V)
  op                create | supersede | retire
  premise_id        the entry id
  root, project     provenance root (global/project/adopted) + project name
  digest            the canonical statement digest (prem:b2b:<blake2b-256 hex>)
  ts, source        capture timestamp + source (human)
  attest_by         the RECORDING label (profile/agent) — provenance, NOT a
                    cryptographic signer (honest: native attestation is not a
                    cell signature)
  supersedes,       the superseded id + its record hash (a supersede op links
  supersedes_record the chain across premise evolution)
and then:
  prev              the predecessor record's rec_hash ("" at genesis)
  rec_hash          blake2b256( canonical(core) + prev )
  chain_index       the record's ordinal (convenience; not hashed)
So rec_hash = hash(canonical_serialization + predecessor_hash): reorder, insert
or edit any record and every subsequent rec_hash fails to recompute. A local
chain is tamper-evident relative to a retained head — git, another copy, or the
optional dregg anchor supplies the external commitment.

CANONICALIZATION (the digest contract — stable by construction):
  1. unicodedata.normalize("NFC", statement)
  2. replace every '"' with "'"  (mirrors store.write_prior's serialization, so
     the digest recomputed from the stored statement equals the capture digest)
  3. collapse every whitespace run to one space
  4. strip
DIGEST — algorithm-tagged (blake2b-256; blake3 tooling is absent on this host):
  payload = "prem:b2b:" + blake2b(canonical_utf8, digest_size=32).hexdigest()

FRONTMATTER pointer (the entry's attest_* keys point back at the chain):
  attest_payload    the digest (prem:b2b:<hex>)
  attest_ts         capture ts
  attest_by         the recording label (provenance, NOT a signer)
  attest_record     the native rec_hash — the PRIMARY proof pointer
  attest_chain_index the record's index in the native chain
  attest_supersedes_record  the prior premise's rec_hash (supersede linkage)
  attest_anchor, attest_anchor_turn  OPTIONAL external dregg anchor (honest)

GRACEFUL DEGRADE. The store write and the native record ALWAYS land — offline,
with no node, with nothing configured. Only the OPTIONAL dregg anchor can be
pending; a pending anchor is queued to attest-queue.jsonl and `--retry-queue`
re-anchors it once a node appears. Capture never raises and is never PREVENTED
by the network: the OPTIONAL anchor is a bounded best-effort on the capture path
(cell.DEFAULT_ANCHOR_TIMEOUT, ~2s; env HELM_NODE_ANCHOR_TIMEOUT) — on any delay
or failure it queues and the native record still stands. If an in-place
annotation ever refuses (a file-shape surprise), the native record is NOT
orphaned: a durable reconciliation row is queued and `--retry-queue` re-annotates
it (the record already stands in the chain).

SUPERSESSION (DECISION clauses 5-6): `--supersede <old-id> <new-id> |
<statement>` captures NEW, tombstones OLD through the store's own lifecycle
(local — lands with no node), and appends ONE native supersede record linking
`supersedes_record` to OLD's rec_hash. `premise-check --chain <id>` walks the
chain and prints the attested biography; drift.py reads the same linkage
offline and reports a verified supersession as EVOLVED.

BACKFILL (--attest-existing <id> / --attest-sweep): entries captured before
attestation existed (the adopted corpus included) get a native record appended
IN PLACE — the digest is computed from the entry's CURRENT stored statement and
the file is annotated with the attest_* keys ONLY (never a rewrite, never a
twin). The sweep is offline + free: no computrons, no faucet, no bearer token.

env2 legacy note: the OPTIONAL dregg config vars (HELM_NODE_URL /
HELM_NODE_TOKEN) accept the legacy MELD_* name READ-ONLY as a migration
fallback (via home.env). No MELD_* value is ever canonical, and no meld binary
is ever needed.

Import-safe, stdlib-only.

This module is a PACKAGE (decomposed from the original premise.py). The public
API is preserved byte-identically at the package boundary: every name below is
re-exported so `from helm import premise; premise.X` keeps working unchanged.
Internal clusters: _common <- _chain <- _verify <- _capture <- _cli (one-way).
"""
# Top-level imports the old module exposed as public attributes (preserved).
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

# Public constants.
from ._common import (
    CHAIN_V,
    DEFAULT_PROFILE,
    DIGEST_TAG,
    EXIT_NO_NATIVE_PROOF,
    RETRY_PAUSE_S,
    canonicalize,
    digest_payload,
    payload_digest,
)
from ._chain import chain_records, record_attestation
from ._verify import verify_chain, verify_link, verify_record
from ._capture import attest_existing, attest_profile, certain_set, cmd_premise
from ._cli import cmd_premise_check

# Private helpers the test suite reaches for directly (underscore-prefixed, so
# they stay OFF the public-surface set the superset proof measures).
from ._common import _queue_path
from ._chain import _chain_path, _append_record
from ._verify import _record_expect
from ._capture import _annotate, _enqueue
