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
re-anchors it once a node appears. Capture never raises and never blocks on the
network.

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
"""
import hashlib
import json
import os
import re
import sys
import unicodedata

from . import cell, home, pk, store

DIGEST_TAG = "prem:b2b:"          # blake2b-256 (see module docstring)
CHAIN_V = 2                       # native attestation-chain schema/evidence version
DEFAULT_PROFILE = "helm-test"     # recording label default — never the user's cell
RETRY_PAUSE_S = 2                 # pause between queued anchor-retry failures

# The tamper-evident core of a native record (hashed; prev/rec_hash/chain_index
# are structural, never part of the hashed body).
_CORE_KEYS = ("v", "op", "premise_id", "root", "project", "digest", "ts",
              "source", "attest_by", "supersedes", "supersedes_record")

_USAGE_PREMISE = ("usage: helm premise <id> | <statement> [| keywords [| domain]] "
                  "[--project P] [--no-attest]\n"
                  "       helm premise --supersede <old-id> <new-id> | <statement> "
                  "[| keywords [| domain]]\n"
                  "       helm premise --retry-queue\n"
                  "       helm premise --attest-existing <id> [--project P]\n"
                  "       helm premise --attest-sweep [--dry] [--limit N]")
_USAGE_CHECK = "usage: helm premise-check <id> [--chain] [--project P]"


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
# the native attestation chain (the PRIMARY proof)
# ---------------------------------------------------------------------------

def _chain_path():
    return os.path.join(home.global_dir(), ".state", "attest-chain.jsonl")


def chain_records():
    """Every native record, in append order. A torn/unparseable line (a crash
    mid-append) is skipped, never a wedge."""
    rows = []
    try:
        with open(_chain_path(), encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def _canon_core(core):
    """The one canonical byte representation of a record's core (sorted keys,
    compact, NFC-safe) — the exact bytes the record hash covers."""
    return json.dumps(core, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _record_hash(core, prev):
    return hashlib.blake2b((_canon_core(core) + (prev or "")).encode("utf-8"),
                           digest_size=32).hexdigest()


def _append_record(op, premise_id, digest, *, root, project, ts, source,
                   attest_by, supersedes, supersedes_record):
    """Append ONE record to the native chain, linked to the current head.
    Returns the stored record dict. This never touches the network."""
    recs = chain_records()
    prev = recs[-1].get("rec_hash", "") if recs else ""
    core = {"v": CHAIN_V, "op": op, "premise_id": str(premise_id),
            "root": root or "", "project": project or "", "digest": digest,
            "ts": ts, "source": source or "", "attest_by": attest_by or "",
            "supersedes": supersedes or "", "supersedes_record": supersedes_record or ""}
    rec = dict(core, prev=prev, rec_hash=_record_hash(core, prev),
               chain_index=len(recs))
    path = _chain_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def record_attestation(op, premise_id, digest, *, root, project="", ts=None,
                       source="human", attest_by="", supersedes="",
                       supersedes_record="", anchor=True):
    """Attest: append ONE native record (the PRIMARY, offline, tamper-evident
    proof) and, best-effort, submit an OPTIONAL dregg external anchor. Returns
    {rec_hash, chain_index, ts, anchor_turn, anchor_label, anchor_reason}. The
    native record always lands; the anchor fails open. Never raises."""
    ts = ts or pk.now_ts()
    rec = _append_record(op, premise_id, digest, root=root, project=project,
                         ts=ts, source=source, attest_by=attest_by,
                         supersedes=supersedes, supersedes_record=supersedes_record)
    out = {"rec_hash": rec["rec_hash"], "chain_index": rec["chain_index"],
           "ts": ts, "anchor_turn": "", "anchor_label": "", "anchor_reason": ""}
    if anchor:
        turn, err = cell.anchor_submit(
            rec["rec_hash"], memo="helm-attest:v%d:%s" % (CHAIN_V, premise_id))
        if turn:
            out["anchor_turn"] = turn
            out["anchor_label"] = cell.anchor_label(turn)
        else:
            out["anchor_reason"] = err
    return out


def verify_chain():
    """Recompute every record hash + confirm predecessor linkage across the
    WHOLE native chain. (ok, detail); one break => tamper-evident fail."""
    recs = chain_records()
    prev = ""
    for i, rec in enumerate(recs):
        if rec.get("prev", "") != prev:
            return False, "record %d (%s): prev breaks the chain" \
                % (i, rec.get("premise_id"))
        core = {k: rec.get(k, "") for k in _CORE_KEYS}
        if _record_hash(core, prev) != rec.get("rec_hash"):
            return False, "record %d (%s): rec_hash does not recompute" \
                % (i, rec.get("premise_id"))
        prev = rec.get("rec_hash", "")
    return True, "chain verified (%d record%s)" % (len(recs), "s"[:len(recs) != 1])


def verify_record(rec_hash):
    """Verify ONE record in place: recompute its hash + confirm its prev links
    to the record before it. (ok, detail)."""
    recs = chain_records()
    for i, rec in enumerate(recs):
        if rec.get("rec_hash") != rec_hash:
            continue
        prev = recs[i - 1].get("rec_hash", "") if i > 0 else ""
        if rec.get("prev", "") != prev:
            return False, "predecessor linkage broken at index %d" % i
        core = {k: rec.get(k, "") for k in _CORE_KEYS}
        if _record_hash(core, prev) != rec_hash:
            return False, "record does not recompute (tampered) at index %d" % i
        return True, ("record %s at index %d links to %s"
                      % (rec_hash[:12], i, ((prev[:12] + "…") if prev else "genesis")))
    return False, "no native record %s in the chain" % (rec_hash or "")[:12]


# ---------------------------------------------------------------------------
# frontmatter helpers + supersession-link verification (OFFLINE)
# ---------------------------------------------------------------------------

def _front(e, key):
    """Read `key` from the entry dict, falling back to its frontmatter file."""
    v = e.get(key)
    if v not in (None, ""):
        return v
    if e.get("path"):
        return (pk.parse_simple_frontmatter(e["path"], {key: ""}) or {}).get(key) or ""
    return ""


def verify_link(old_e, new_e):
    """OFFLINE hop verification old -> new via the native chain (no node):
      attested  new carries attest_supersedes_record == old's attest_record and
                new's digest matches its STORED statement
      broken    the link points elsewhere, old has no record, or the digest no
                longer matches the stored statement
      unbacked  no native supersession record (store-only supersession, or a
                chain start over a never-attested prior)."""
    link = _front(new_e, "attest_supersedes_record")
    if not link:
        return "unbacked", "no native supersession record on '%s'" % new_e["id"]
    payload = _front(new_e, "attest_payload")
    if payload_digest(payload) != payload_digest(digest_payload(new_e.get("statement") or "")):
        return "broken", "attested digest != stored statement of '%s'" % new_e["id"]
    prior = _front(old_e, "attest_record")
    if not prior:
        return "broken", "'%s' links but '%s' has no attest_record" \
            % (new_e["id"], old_e["id"])
    if link != prior:
        return "broken", "supersedes_record %s != prior record %s" \
            % (link[:12], prior[:12])
    return "attested", "native record linkage verified (%s -> %s)" \
        % (prior[:12], (_front(new_e, "attest_record") or "?")[:12])


# ---------------------------------------------------------------------------
# the anchor-retry queue (only the OPTIONAL external anchor ever queues)
# ---------------------------------------------------------------------------

def _queue_path():
    return os.path.join(home.global_dir(), ".state", "attest-queue.jsonl")


def _enqueue(rec):
    path = _queue_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _read_queue(qp):
    rows = []
    try:
        with open(qp, encoding="utf-8") as f:
            for l in f:
                if l.strip():
                    try:
                        rows.append(json.loads(l))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def _rewrite_queue(qp, examined, kept):
    """Atomic queue rewrite that PRESERVES rows appended while the caller
    worked (the queue is append-only, so everything past `examined` rides
    along)."""
    rows = kept + _read_queue(qp)[examined:]
    pk.atomic_write(qp, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                for r in rows))


# ---------------------------------------------------------------------------
# annotation
# ---------------------------------------------------------------------------

def _annotate(path, fields):
    """Insert attest_* keys into the entry's metadata block (just before the
    closing frontmatter fence). fields = ordered (key, value) pairs; blank
    values skipped. Fail-open False on a shape surprise (non-UTF-8 file, or a
    metadata block without two fences). newline='' keeps CRLF byte-identical
    outside the inserted lines."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            lines = f.read().split("\n")
    except (OSError, ValueError):
        return False
    fences = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(fences) < 2:
        return False
    ins = ["  %s: %s" % (k, v) for k, v in fields if str(v) != ""]
    lines[fences[1]:fences[1]] = ins
    pk.atomic_write(path, "\n".join(lines))
    return True


def _attest_fields(digest, ts, profile, info, supersedes_record=""):
    """The ordered attest_* frontmatter for one attestation."""
    fields = [("attest_payload", digest), ("attest_ts", ts),
              ("attest_by", profile), ("attest_record", info["rec_hash"]),
              ("attest_chain_index", info["chain_index"])]
    if supersedes_record:
        fields.append(("attest_supersedes_record", supersedes_record))
    if info.get("anchor_turn"):
        fields.append(("attest_anchor", info["anchor_label"]))
        fields.append(("attest_anchor_turn", info["anchor_turn"]))
    return fields


def _report_anchor(info, pid, project, ts, rec_hash=None):
    """Print the OPTIONAL anchor's honest status; queue it for retry if it did
    not land (the native record is already the proof)."""
    if info.get("anchor_turn"):
        print("  external anchor: " + info["anchor_label"])
        return
    _enqueue({"ts": ts, "id": str(pid), "project": project,
              "rec_hash": rec_hash or info["rec_hash"], "kind": "anchor",
              "reason": info.get("anchor_reason") or "no node"})
    print("  external anchor: none yet — native record stands; queued for retry")
    print("    reason: " + (info.get("anchor_reason") or "no dregg node reachable"))


# ---------------------------------------------------------------------------
# argument helpers
# ---------------------------------------------------------------------------

def _pop_flag(args, flag, takes_value):
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
    """The recording label for THIS capture (env else the test default). A
    LABEL only — helm's native attestation is not a cell signature."""
    return cell.profile_name(default=DEFAULT_PROFILE)


def _root_label(e, project):
    return e.get("root") or ("project" if project else "global")


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------

def _capture_store(parts, project, ts):
    """The store leg every capture path shares (write_prior, CERTAIN, human).
    Re-minting over a NON-live prior life starts FRESH (stale tombstone/attest
    metadata never rides into the new lifecycle). A LIVE attested entry whose
    canonical statement would CHANGE is refused toward --supersede (an in-place
    edit orphans the attestation). -> (path, entry, None) or (None, None,
    refusal)."""
    pid, statement = parts[0], parts[1]
    path = os.path.join(store._default_dir("prior", project),
                        store.PRIOR_PREFIX + pk.slug(pid) + ".md")
    e = store._parse_prior(path) or {}
    if e.get("status") == store.STATUS_LIVE and _front(e, "attest_record") \
            and canonicalize(e.get("statement")) != canonicalize(statement):
        return None, None, (
            "'%s' is LIVE and attested — an in-place edit orphans the "
            "attestation (the chain law). Evolve it instead:\n"
            "  helm premise --supersede %s <new-id> | %s%s"
            % (pid, pid, statement, (" --project " + project) if project else ""))
    if e and e.get("status") != store.STATUS_LIVE:
        for stale in ("replaced_by", "supersedes", "retired_ts", "retired_why",
                      "evidence_log", "confidence_history", "attest_payload",
                      "attest_ts", "attest_by", "attest_record",
                      "attest_chain_index", "attest_supersedes_record",
                      "attest_anchor", "attest_anchor_turn",
                      "attest_turn", "attest_receipt", "attest_supersedes_turn"):
            e.pop(stale, None)
    e.update({"id": pid, "statement": statement, "confidence": store.CERTAIN,
              "keywords": parts[2] if len(parts) > 2 else e.get("keywords", ""),
              "domain": parts[3] if len(parts) > 3 else e.get("domain", ""),
              "status": store.STATUS_LIVE, "stated_ts": ts, "last_updated": ts,
              "source": "human"})
    store.write_prior(e, path=path)
    return path, e, None


def cmd_premise(args):
    """premise <id> | <statement> [| keywords [| domain]] — store + attest.
    premise --supersede <old-id> <new-id> | <statement> — evolve the chain.
    premise --retry-queue — re-attempt OPTIONAL dregg anchors queued while no
    node was reachable (the native record already stands).
    premise --attest-existing <id> — backfill a native record for one entry.
    premise --attest-sweep [--dry] [--limit N] — backfill every live certain."""
    args = list(args)
    if "--retry-queue" in args:
        return _retry_queue()
    project = _pop_flag(args, "--project", True)
    sup_of = _pop_flag(args, "--supersede", True)
    if "--supersede" in args:  # flag present but valueless
        print(_USAGE_PREMISE, file=sys.stderr)
        return 2
    if "--attest-sweep" in args:
        _pop_flag(args, "--attest-sweep", False)
        dry = bool(_pop_flag(args, "--dry", False))
        limit = _pop_flag(args, "--limit", True)
        if "--limit" in args or (limit is not None and not str(limit).isdigit()):
            print(_USAGE_PREMISE, file=sys.stderr)
            return 2
        return _attest_sweep(dry=dry, limit=int(limit) if limit is not None else None)
    if "--attest-existing" in args:
        pid = _pop_flag(args, "--attest-existing", True)
        if not pid:
            print(_USAGE_PREMISE, file=sys.stderr)
            return 2
        return _attest_one(pid, project)
    no_attest = _pop_flag(args, "--no-attest", False)
    parts = [p.strip() for p in " ".join(args).split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        print(_USAGE_PREMISE, file=sys.stderr)
        return 2
    if sup_of:
        if no_attest:
            print("helm premise: --supersede IS the attested lifecycle — for a "
                  "store-only tombstone use `helm store supersede`", file=sys.stderr)
            return 2
        return _supersede(sup_of, parts, project)
    pid, statement = parts[0], parts[1]
    ts = pk.now_ts()

    # 1. STORE — the native record + store both land offline; nothing lost.
    path, e, refuse = _capture_store(parts, project, ts)
    if refuse:
        print("helm premise: " + refuse, file=sys.stderr)
        return 1
    print("helm premise: LIVE '%s' [certain 1.00] - %s" % (pid, statement))
    print("  stored: " + path)

    if no_attest:
        print("  attestation skipped (--no-attest)")
        return 0

    # 2. ATTEST — the native chain record is the primary proof.
    digest = digest_payload(statement)
    if _front(e, "attest_record") and \
            payload_digest(_front(e, "attest_payload")) == payload_digest(digest):
        print("  already attested — record %s (verify: helm premise-check %s)"
              % (_front(e, "attest_record")[:16], pid))
        return 0
    profile = attest_profile()
    info = record_attestation("create", pid, digest, root=_root_label(e, project),
                              project=project, ts=ts, attest_by=profile)
    _annotate(path, _attest_fields(digest, ts, profile, info))
    print("  attested (native): record %s at chain_index %s — recorded by '%s'"
          % (info["rec_hash"][:16], info["chain_index"], profile))
    print("  payload: " + digest)
    _report_anchor(info, pid, project, ts)
    return 0


def _supersede(old_id, parts, project):
    """--supersede <old-id> <new-id> | <statement>: capture NEW, tombstone OLD
    (store lifecycle — lands with no node), and append ONE native supersede
    record linking supersedes_record to OLD's rec_hash. A never-attested OLD is
    stated honestly: the chain starts at the new premise."""
    old = store._find(old_id, project=project, types=("prior",))
    if not old:
        print("helm premise: '%s' not found (helm store list)" % old_id,
              file=sys.stderr)
        return 1
    if old.get("replaced_by"):
        print("helm premise: '%s' already superseded by '%s' — the chain "
              "extends from its tip, never forks"
              % (old["id"], old["replaced_by"]), file=sys.stderr)
        return 1
    if pk.slug(parts[0]) == pk.slug(str(old["id"])):
        print("helm premise: a premise cannot supersede itself — the revision "
              "needs a new id", file=sys.stderr)
        return 1
    ts = pk.now_ts()
    old_record = _front(old, "attest_record")   # capture before the tombstone
    path, _e, refuse = _capture_store(parts, project, ts)
    if refuse:
        print("helm premise: " + refuse, file=sys.stderr)
        return 1
    _old, err = store.mark_superseded(str(old["id"]), parts[0], ts,
                                      reason="premise --supersede", project=project)
    if err:
        print("helm premise: " + err, file=sys.stderr)
        return 1
    print("helm premise: LIVE '%s' [certain 1.00] - %s" % (parts[0], parts[1]))
    print("  supersedes '%s' — tombstoned (delete_eligible, file kept)"
          % old["id"])
    if not old_record:
        print("  note: '%s' was never attested — the chain starts here" % old["id"])
    digest = digest_payload(parts[1])
    profile = attest_profile()
    info = record_attestation("supersede", parts[0], digest,
                              root=_root_label(_e, project), project=project,
                              ts=ts, attest_by=profile, supersedes=str(old["id"]),
                              supersedes_record=old_record)
    _annotate(path, _attest_fields(digest, ts, profile, info,
                                   supersedes_record=old_record))
    print("  attested (native): record %s at chain_index %s — recorded by '%s'"
          % (info["rec_hash"][:16], info["chain_index"], profile))
    print("  payload: " + digest)
    _report_anchor(info, parts[0], project, ts)
    if old_record:
        print("  chain: -> prior record %s (attest_supersedes_record)"
              % old_record[:16])
    return 0


def _retry_queue():
    """Re-attempt the OPTIONAL dregg anchors queued while no node was reachable.
    The native records already stand; this only tries to add the external
    checkpoint. Success annotates attest_anchor* and drops the row; failures
    (and rows whose entry vanished) are kept."""
    qp = _queue_path()
    rows = _read_queue(qp)
    if not rows:
        print("helm premise: attest queue empty.")
        return 0
    kept = []
    done = dropped = 0
    for rec in rows:
        e = store._find(rec.get("id", ""), types=("prior",),
                        project=rec.get("project"))
        if not e:
            rec["reason"] = "entry no longer in the store"
            kept.append(rec)
            continue
        if _front(e, "attest_anchor_turn"):
            dropped += 1
            continue
        rh = rec.get("rec_hash") or _front(e, "attest_record")
        turn, err = cell.anchor_submit(rh, memo="helm-attest:v%d:%s"
                                       % (CHAIN_V, rec.get("id")))
        if err:
            rec["reason"] = err
            kept.append(rec)
            continue
        _annotate(e["path"], [("attest_anchor", cell.anchor_label(turn)),
                              ("attest_anchor_turn", turn)])
        print("helm premise: anchored '%s' — turn %s" % (rec["id"], turn[:16]))
        done += 1
    _rewrite_queue(qp, len(rows), kept)
    tail = (", %d already-anchored row%s dropped" % (dropped, "s"[:dropped != 1])) \
        if dropped else ""
    print("helm premise: anchor replay — %d anchored, %d still pending%s."
          % (done, len(kept), tail))
    return 0 if not kept else 1


# ---------------------------------------------------------------------------
# backfill — append a native record for entries ALREADY in the store
# ---------------------------------------------------------------------------

def attest_existing(e, profile=None, project=None, anchor=True):
    """Append a native record for an entry already in the store (adopted
    included) + annotate in place. The digest is computed from the CURRENT
    stored statement; the byte diff is exactly the attest_* lines. Returns
    (info, None) — the native record always lands; info['annotated'] False = a
    file whose shape refused the annotation (the record still stands in the
    chain), info['anchor_reason'] set = the OPTIONAL anchor did not land."""
    profile = profile or attest_profile()
    digest = digest_payload(e.get("statement") or "")
    info = record_attestation("create", str(e["id"]), digest,
                              root=e.get("root") or "global", project=project,
                              ts=pk.now_ts(), attest_by=profile, anchor=anchor)
    info["annotated"] = _annotate(e["path"], _attest_fields(
        digest, info["ts"], profile, info))
    return info, None


def _attest_one(pid, project):
    """--attest-existing <id>: backfill a native record for one existing entry."""
    e = store._find(pid, project=project, types=("prior",))
    if not e:
        print("helm premise: '%s' not found (helm store list)" % pid,
              file=sys.stderr)
        return 1
    if e.get("status") != store.STATUS_LIVE:
        print("helm premise: '%s' is %s — only LIVE certain truths attest"
              % (pid, e.get("status")), file=sys.stderr)
        return 1
    if e.get("class") != "certain":
        print("helm premise: '%s' holds confidence %.2f — the attestable set "
              "is exactly the confidence-1.0 truths (beliefs never attest)"
              % (pid, e["confidence"]), file=sys.stderr)
        return 1
    if _front(e, "attest_record"):
        print("helm premise: '%s' already attested — record %s (verify: helm "
              "premise-check %s)" % (pid, _front(e, "attest_record")[:16], pid))
        return 0
    profile = attest_profile()
    info, _err = attest_existing(e, profile=profile, project=project)
    print("helm premise: attested existing '%s' [%s] — record %s (chain_index "
          "%s) recorded by '%s'" % (e["id"], e["root"], info["rec_hash"][:16],
                                    info["chain_index"], profile))
    print("  payload: " + digest_payload(e.get("statement") or ""))
    if not info.get("annotated"):
        print("  WARNING: file shape refused the annotation — the record still "
              "stands in the native chain (record %s)" % info["rec_hash"][:16])
    if info.get("anchor_turn"):
        print("  external anchor: " + info["anchor_label"])
    return 0


def _project_names():
    root = home.helm_home()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [n for n in names if n != home.GLOBAL and not n.startswith(".")
            and os.path.isdir(os.path.join(root, n))]


def certain_set():
    """Every LIVE certain (confidence-1.0) prior across ALL physical roots — the
    attestable set. (entry, project) pairs, deduped by real path."""
    seen = set()
    out = []
    for proj in [None] + _project_names():
        for e in store.load_all(project=proj, types=("prior",)):
            if proj and e.get("root") != "project":
                continue
            if e.get("class") != "certain":
                continue
            rp = os.path.realpath(e["path"])
            if rp not in seen:
                seen.add(rp)
                out.append((e, proj))
    return out


def _prune_queue():
    """Drop anchor-queue rows whose entry ALREADY carries an anchor turn.
    Missing-entry rows stay. Returns rows dropped."""
    qp = _queue_path()
    rows = _read_queue(qp)
    kept = [r for r in rows
            if not _front(store._find(r.get("id", ""), types=("prior",),
                                      project=r.get("project")) or {},
                          "attest_anchor_turn")]
    if len(kept) != len(rows):
        _rewrite_queue(qp, len(rows), kept)
    return len(rows) - len(kept)


def _attest_sweep(dry=False, limit=None):
    """--attest-sweep: append a native record for every live certain entry that
    lacks one, across all roots. Offline + free — no computrons, no faucet, no
    bearer. A best-effort dregg anchor is attempted per entry (fail-open)."""
    allc = certain_set()
    todo = [(e, p) for e, p in allc if not _front(e, "attest_record")]
    profile = attest_profile()
    print("helm premise sweep: %d live certain entries — %d attested, %d to attest"
          % (len(allc), len(allc) - len(todo), len(todo)))
    if limit is not None:
        todo = todo[:limit]
        print("  --limit: at most %d this pass" % limit)
    if dry:
        print("  dry run — %d native records to append (offline, no cost); "
              "recording label '%s'; nothing written" % (len(todo), profile))
        return 0
    if not todo:
        pruned = _prune_queue()
        if pruned:
            print("  pruned %d stale queue row%s (entries already anchored)"
                  % (pruned, "s"[:pruned != 1]))
        print("helm premise sweep: nothing to attest.")
        return 0
    done = pending = 0
    for e, proj in todo:
        info, _err = attest_existing(e, profile=profile, project=proj)
        note = "" if info.get("annotated") else \
            " [ANNOTATION FAILED — record in the native chain]"
        anchor = "" if info.get("anchor_turn") else " (anchor pending)"
        if not info.get("anchor_turn"):
            _enqueue({"ts": pk.now_ts(), "id": str(e["id"]), "project": proj,
                      "rec_hash": info["rec_hash"], "kind": "anchor",
                      "reason": info.get("anchor_reason") or "no node"})
            pending += 1
        print("  attested '%s' [%s] — record %s (chain %s)%s%s"
              % (e["id"], e["root"], info["rec_hash"][:16], info["chain_index"],
                 note, anchor))
        done += 1
    pruned = _prune_queue()
    tail = (", %d stale queue row%s pruned" % (pruned, "s"[:pruned != 1])) \
        if pruned else ""
    print("helm premise sweep: %d attested (native), %d anchor%s pending%s."
          % (done, pending, "s"[:pending != 1], tail))
    return 0


# ---------------------------------------------------------------------------
# premise-check — honest tiering (native primary; anchor secondary)
# ---------------------------------------------------------------------------

_CHECK_DEFAULTS = {"attest_payload": "", "attest_ts": "", "attest_by": "",
                   "attest_record": "", "attest_chain_index": "",
                   "attest_supersedes_record": "", "attest_anchor": "",
                   "attest_anchor_turn": ""}


def _anchor_line(meta):
    """The honest external-anchor status line for one entry."""
    aturn = meta.get("attest_anchor_turn")
    if not aturn:
        return "  external anchor: none (native-only)"
    observed, detail = cell.verify_anchor(aturn)
    return "  external anchor: %s — %s" % (
        "CONFIRMED" if observed else "unverified", detail)


def _chain(pid, project):
    """--chain: the supersession chain THROUGH pid, origin first. Re-verify each
    link's digest (vs its stored statement), each record's native-chain
    integrity, and each hop's offline linkage (verify_link); print the attested
    biography. Exit 0 = every digest matches, every record verifies, no hop is
    BROKEN. An unbacked (store-only) hop prints loudly but is a stated design
    state, not corruption."""
    e = store._find(pid, project=project, types=("prior",))
    if not e:
        print("helm premise-check: '%s' not found (helm store list)" % pid,
              file=sys.stderr)
        return 1
    chain = [e]
    seen = {pk.slug(str(e["id"]))}
    for key, front in (("supersedes", True), ("replaced_by", False)):
        cur = e
        while cur.get(key):
            nxt = store._find(cur[key], project=project, types=("prior",))
            if not nxt or pk.slug(str(nxt["id"])) in seen:
                break
            chain.insert(0, nxt) if front else chain.append(nxt)
            seen.add(pk.slug(str(nxt["id"])))
            cur = nxt
    print("helm premise-check --chain: %d link%s through '%s', origin first"
          % (len(chain), "s"[:len(chain) != 1], pid))
    ok = True
    for i, c in enumerate(chain):
        print("  %d. %s [%s] - %s"
              % (i + 1, c["id"], c["status"], c.get("statement") or ""))
        payload = _front(c, "attest_payload")
        if not payload:
            print("       unattested (no payload recorded)")
        else:
            match = payload_digest(payload) == \
                payload_digest(digest_payload(c.get("statement") or ""))
            ok = ok and match
            print("       digest %s %s" % ("MATCH" if match else "MISMATCH", payload))
            rec = _front(c, "attest_record")
            if rec:
                rok, detail = verify_record(rec)
                ok = ok and rok
                print("       native chain %s — %s"
                      % ("VERIFIED" if rok else "BROKEN", detail))
        if i:
            state, why = verify_link(chain[i - 1], c)
            ok = ok and state != "broken"
            print("       link %d->%d %s — %s" % (i, i + 1, state.upper(), why))
    if len(chain) > 1:
        print("  biography:")
        for prev_e, next_e in zip(chain, chain[1:]):
            until = next_e.get("attest_ts") or prev_e.get("retired_ts") \
                or prev_e.get("last_updated") or "?"
            tail = " — LIVE now" if next_e is chain[-1] \
                and next_e.get("status") == store.STATUS_LIVE else ""
            print("    held '%s' until %s, then '%s'%s"
                  % (prev_e.get("statement") or "", until,
                     next_e.get("statement") or "", tail))
    else:
        print("  no supersession links — single-entry chain")
    print("  note: the native hash chain is the primary proof; a dregg anchor, "
          "when present, is an external checkpoint (node-anchored, not "
          "cell-signed)")
    return 0 if ok else 1


def cmd_premise_check(args):
    """premise-check <id> — recompute the digest from the stored statement,
    verify the native chain record (primary), and report the OPTIONAL dregg
    anchor honestly. --chain walks the supersession chain instead."""
    args = list(args)
    project = _pop_flag(args, "--project", True)
    chain = bool(_pop_flag(args, "--chain", False))
    pid = " ".join(a for a in args if not a.startswith("--")).strip()
    if not pid:
        print(_USAGE_CHECK, file=sys.stderr)
        return 2
    if chain:
        return _chain(pid, project)
    e = store._find(pid, project=project, types=("prior",))
    if not e:
        print("helm premise-check: '%s' not found (helm store list)" % pid,
              file=sys.stderr)
        return 1
    print("helm premise-check: " + str(e["id"]))
    print("  statement: " + (e.get("statement") or ""))
    meta = pk.parse_simple_frontmatter(e["path"], _CHECK_DEFAULTS) or {}
    if not meta.get("attest_payload") and not meta.get("attest_record"):
        print("  no attestation recorded — captured with --no-attest (a pending "
              "anchor row may sit in " + _queue_path() + ")")
        return 0
    recomputed = digest_payload(e.get("statement") or "")
    match = payload_digest(meta["attest_payload"]) == payload_digest(recomputed)
    if match:
        print("  digest: MATCH " + meta["attest_payload"])
    else:
        print("  digest: MISMATCH — the stored statement no longer hashes to "
              "the attested payload")
        print("    attested:   " + meta["attest_payload"])
        print("    recomputed: " + recomputed)
    print("  recorded by '%s' at %s (provenance label, not a cell signer)"
          % (meta.get("attest_by") or "?", meta.get("attest_ts") or "?"))
    rec = meta.get("attest_record")
    if rec:
        rok, detail = verify_record(rec)
        match = match and rok
        print("  native chain: %s — %s"
              % ("VERIFIED" if rok else "BROKEN", detail))
    else:
        print("  native chain: no record hash recorded (legacy or --no-attest)")
    print(_anchor_line(meta))
    if meta.get("attest_supersedes_record"):
        print("  chain: supersedes prior record %s — walk it: helm "
              "premise-check --chain %s"
              % (meta["attest_supersedes_record"][:16], pid))
    return 0 if match else 1
