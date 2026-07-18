#!/usr/bin/env python3
"""helm premise — attested premise capture per
docs/DECISION-dregg-attested-premises.md (+ the live-proof addendum).

A premise (a confidence-1.0 truth) is written into the typed store
(store.write_prior — the same premise path `helm store add premise` takes)
AND attested on the dregg substrate as ONE self-write turn: the digest of the
canonical statement rides the WHISPER PAYLOAD slots via the proven meld-send
path — NEVER the 8-byte heartbeat tag (addendum law #1). `premise-check`
recomputes the digest from the STORED statement, compares it to the attested
payload, and quotes the FINALITY TIER the node proved (addendum law #2).

CANONICALIZATION (the digest contract — stable by construction):
  1. unicodedata.normalize("NFC", statement)
  2. replace every '"' with "'"  — mirrors store.write_prior's serialization
     (it swaps double quotes for single on write), so the digest recomputed
     from the stored statement always equals the digest taken at capture
  3. collapse every whitespace run to one space  (re.sub(r"\\s+", " ", s))
  4. strip leading/trailing whitespace

DIGEST — ALGORITHM-TAGGED because blake3 tooling is absent on this host
(verified 2026-07-18: no b3sum on PATH, no python `blake3` module):
  payload = "prem:b2b:" + hashlib.blake2b(canonical_utf8, digest_size=32).hexdigest()
73 ASCII bytes — inside the whisper frame's 104-byte budget. The tag makes the
algorithm self-describing and upgradeable: when blake3 tooling lands, a
"prem:b3:<hex>" payload coexists and old attestations stay verifiable.

IDENTITY: an agent-run capture CANNOT be user-signed — the user's cell signs
only when the USER captures. The signing profile is recorded on the entry as
`attest_by`; the default (no HELM_CELL_PROFILE / MELD_AGENT_PROFILE set) is
the TEST profile 'helm-test', so a test-signed attestation is never mistaken
for the user's. The user-cell activation is the owner's first /premise with
their own profile set — by design.

Substrate down / binary missing / no profile: the premise is STORED anyway,
the attestation is queued to <helm-home>/_global/.state/attest-queue.jsonl for
retry, and the capture reports "attestation pending (substrate unavailable)".

NOTE on the attest_* frontmatter: store.write_prior owns the entry's byte
shape and does not carry attest keys, so this module appends them into the
metadata block after the write. A later lifecycle rewrite (evidence/retire)
drops them; the attestation TRUTH lives on the ledger — re-annotate by
re-capturing, or read the turn hash back from the queue/receipts.

Import-safe, stdlib-only.
"""
import hashlib
import json
import os
import re
import sys
import unicodedata

from . import cell, home, pk, store

DIGEST_TAG = "prem:b2b:"          # blake2b-256 (see module docstring)
DEFAULT_PROFILE = "helm-test"     # test-signed by default — never the user's cell

_USAGE_PREMISE = ("usage: helm premise <id> | <statement> [| keywords [| domain]] "
                  "[--project P] [--no-attest]")
_USAGE_CHECK = "usage: helm premise-check <id> [--project P]"


def canonicalize(statement):
    """The exact canonical text the digest covers (see module docstring):
    NFC -> '\"'->\"'\" -> collapse-whitespace -> strip."""
    s = unicodedata.normalize("NFC", statement or "")
    s = s.replace('"', "'")
    return re.sub(r"\s+", " ", s).strip()


def digest_payload(statement):
    """The algorithm-tagged attestation payload for a statement (73 bytes)."""
    h = hashlib.blake2b(canonicalize(statement).encode("utf-8"), digest_size=32)
    return DIGEST_TAG + h.hexdigest()


def _queue_path():
    return os.path.join(home.global_dir(), ".state", "attest-queue.jsonl")


def _enqueue(rec):
    path = _queue_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _annotate(path, fields):
    """Insert attest_* keys into the entry's metadata block (just before the
    closing frontmatter fence). fields = ordered (key, value) pairs; blank
    values are skipped. Fail-open False on a shape surprise."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return False
    fences = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(fences) < 2:
        return False
    ins = ["  %s: %s" % (k, v) for k, v in fields if str(v) != ""]
    lines[fences[1]:fences[1]] = ins
    pk.atomic_write(path, "\n".join(lines))
    return True


def _pop_flag(args, flag, takes_value):
    """Remove `flag` (and its value) from args; return the value (or True)."""
    if flag not in args:
        return None
    i = args.index(flag)
    if not takes_value:
        del args[i]
        return True
    if i + 1 >= len(args):
        return None
    v = args[i + 1]
    del args[i:i + 2]
    return v


def attest_profile():
    """The signing profile for THIS capture (env else the test default)."""
    return cell.profile_name(default=DEFAULT_PROFILE)


def cmd_premise(args):
    """premise <id> | <statement> [| keywords [| domain]] — store + attest.
    premise --retry-queue — replay attestations queued while the substrate
    was down (success annotates the entry + leaves the queue; failures stay)."""
    args = list(args)
    if "--retry-queue" in args:
        return _retry_queue()
    project = _pop_flag(args, "--project", True)
    no_attest = _pop_flag(args, "--no-attest", False)
    parts = [p.strip() for p in " ".join(args).split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        print(_USAGE_PREMISE, file=sys.stderr)
        return 2
    pid, statement = parts[0], parts[1]
    ts = pk.now_ts()

    # 1. STORE — same target + shape as `helm store add premise` (write_prior,
    # confidence CERTAIN, source human); attestation failure never loses it.
    path = os.path.join(store._default_dir("prior", project),
                        store.PRIOR_PREFIX + pk.slug(pid) + ".md")
    e = store._parse_prior(path) or {}
    e.update({"id": pid, "statement": statement, "confidence": store.CERTAIN,
              "keywords": parts[2] if len(parts) > 2 else e.get("keywords", ""),
              "domain": parts[3] if len(parts) > 3 else e.get("domain", ""),
              "status": store.STATUS_LIVE, "stated_ts": ts, "last_updated": ts,
              "source": "human"})
    store.write_prior(e, path=path)
    print("helm premise: LIVE '%s' [certain 1.00] - %s" % (pid, statement))
    print("  stored: " + path)

    if no_attest:
        print("  attestation skipped (--no-attest)")
        return 0

    # 2. ATTEST — the digest rides the whisper PAYLOAD slots (self-write send).
    payload = digest_payload(statement)
    profile = attest_profile()
    info, err = cell.send_self(payload, profile)
    if err:
        qp = _enqueue({"ts": ts, "id": pid, "project": project,
                       "payload": payload, "profile": profile, "reason": err})
        print("  attestation pending (substrate unavailable) — queued: " + qp)
        print("    reason: " + err)
        return 0
    _annotate(path, [("attest_payload", payload), ("attest_ts", ts),
                     ("attest_by", profile),
                     ("attest_turn", info.get("turn_hash", "")),
                     ("attest_receipt", info.get("receipt_hash", "")),
                     ("attest_chain_index", info.get("chain_index", ""))])
    print("  attested: turn %s (chain_index %s) signed by profile '%s'"
          % (info.get("turn_hash"), info.get("chain_index"), profile))
    print("  payload: " + payload)
    return 0


def _retry_queue():
    """Replay pending attestations. Each success annotates the stored entry
    and drops the row; failures (and rows whose entry vanished) are kept."""
    qp = _queue_path()
    try:
        with open(qp, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except OSError:
        rows = []
    if not rows:
        print("helm premise: attest queue empty.")
        return 0
    kept = []
    done = 0
    for rec in rows:
        e = store._find(rec.get("id", ""), types=("prior",),
                        project=rec.get("project"))
        if not e:
            rec["reason"] = "entry no longer in the store"
            kept.append(rec)
            continue
        info, err = cell.send_self(rec["payload"], rec.get("profile") or attest_profile())
        if err:
            rec["reason"] = err
            kept.append(rec)
            continue
        _annotate(e["path"], [("attest_payload", rec["payload"]),
                              ("attest_ts", pk.now_ts()),
                              ("attest_by", rec.get("profile") or attest_profile()),
                              ("attest_turn", info.get("turn_hash", "")),
                              ("attest_receipt", info.get("receipt_hash", "")),
                              ("attest_chain_index", info.get("chain_index", ""))])
        print("helm premise: attested '%s' from queue — turn %s"
              % (rec["id"], (info.get("turn_hash") or "")[:16]))
        done += 1
    pk.atomic_write(qp, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    print("helm premise: queue replay — %d attested, %d still pending." % (done, len(kept)))
    return 0 if not kept else 1


# ---------------------------------------------------------------------------
# premise-check
# ---------------------------------------------------------------------------

_CHECK_DEFAULTS = {"attest_payload": "", "attest_ts": "", "attest_by": "",
                   "attest_turn": "", "attest_receipt": ""}


def _fetch_turn_status(turn_hash):
    """GET /api/turn/{hash}/status. Observed live response shape:
    {"attested_height": N, "consensus_final": bool, "receipt_hash": hex,
     "receipt_present": bool, "turn_hash": hex}. None when unreachable."""
    return cell.get_json(cell.node_url() + "/api/turn/" + turn_hash + "/status")


def finality_tier(st):
    """Map the node's turn-status response to the finality tier it PROVES —
    quoted verbatim in the check output (addendum law #2). Honest mapping of
    the observed fields: consensus_final at an attested height is the
    after-next-height tier; a receipt alone is only ingress-immediate."""
    if not st:
        return "unverified (node unreachable)"
    if st.get("consensus_final") and st.get("attested_height") is not None:
        return ("attested-after-next-height (consensus_final at attested_height %s)"
                % st["attested_height"])
    if st.get("receipt_present"):
        return ("ingress-immediate (receipted on the node; not yet "
                "consensus-final at an attested height)")
    return "unverified (turn not found on the node)"


def cmd_premise_check(args):
    """premise-check <id> — recompute the digest from the stored statement,
    compare to the attested payload, and quote the verified finality tier."""
    args = list(args)
    project = _pop_flag(args, "--project", True)
    pid = " ".join(a for a in args if not a.startswith("--")).strip()
    if not pid:
        print(_USAGE_CHECK, file=sys.stderr)
        return 2
    e = store._find(pid, project=project, types=("prior",))
    if not e:
        print("helm premise-check: '%s' not found (helm store list)" % pid,
              file=sys.stderr)
        return 1
    print("helm premise-check: " + str(e["id"]))
    print("  statement: " + (e.get("statement") or ""))
    meta = pk.parse_simple_frontmatter(e["path"], _CHECK_DEFAULTS) or {}
    if not meta.get("attest_payload"):
        print("  no attestation recorded — captured with --no-attest or while "
              "the substrate was down (a pending record may sit in "
              + _queue_path() + ")")
        return 0
    recomputed = digest_payload(e.get("statement") or "")
    match = recomputed == meta["attest_payload"]
    if match:
        print("  digest: MATCH " + recomputed)
    else:
        print("  digest: MISMATCH — the stored statement no longer hashes to "
              "the attested payload")
        print("    attested:   " + meta["attest_payload"])
        print("    recomputed: " + recomputed)
    print("  attested by profile '%s' at %s"
          % (meta.get("attest_by") or "?", meta.get("attest_ts") or "?"))
    turn = meta.get("attest_turn")
    if turn:
        print("  turn: " + turn)
        print("  finality tier: " + finality_tier(_fetch_turn_status(turn)))
    else:
        print("  finality tier: unverified (no turn hash recorded)")
    return 0 if match else 1
