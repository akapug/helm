"""helm premise — the native attestation chain (the PRIMARY proof).

Append-only, tamper-evident hash chain under the helm home. Depends only on
_common (imports nothing from _verify/_capture/_cli).
"""
import hashlib
import json
import os

from .. import cell, home, pk
from ._common import CHAIN_V, fcntl


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


def _read_head(f):
    """Re-read the chain from an open file handle (already positioned/locked) ->
    (prev_rec_hash, count). A torn/unparseable line is skipped, never a wedge."""
    f.seek(0)
    n = 0
    prev = ""
    for line in f:
        if not line.strip():
            continue
        try:
            prev = json.loads(line).get("rec_hash", prev)
        except ValueError:
            continue
        n += 1
    return prev, n


def _append_record(op, premise_id, digest, *, root, project, ts, source,
                   attest_by, supersedes, supersedes_record):
    """Append ONE record to the native chain, linked to the current head.
    Returns the stored record dict. This never touches the network.

    CONCURRENCY: the read-head + construct + append is done under an
    exclusive inter-process lock (fcntl.flock) held on the chain file itself, so
    two agents capturing at once serialize and NEVER fork the chain (same prev +
    index). 'a+' creates the file and, being O_APPEND, always writes at EOF
    regardless of the read cursor; flush + fsync make the row durable before the
    lock releases."""
    path = _chain_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            prev, count = _read_head(f)
            core = {"v": CHAIN_V, "op": op, "premise_id": str(premise_id),
                    "root": root or "", "project": project or "", "digest": digest,
                    "ts": ts, "source": source or "", "attest_by": attest_by or "",
                    "supersedes": supersedes or "",
                    "supersedes_record": supersedes_record or ""}
            rec = dict(core, prev=prev, rec_hash=_record_hash(core, prev),
                       chain_index=count)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
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
            rec["rec_hash"], memo="helm-attest:v%d:%s" % (CHAIN_V, premise_id),
            timeout=cell.anchor_timeout())   # bounded best-effort on the capture path
        if turn:
            out["anchor_turn"] = turn
            out["anchor_label"] = cell.anchor_label(turn)
        else:
            out["anchor_reason"] = err
    return out


def _record_by_hash(rec_hash):
    """The native record with this rec_hash, or None. Read-only; no lock."""
    if not rec_hash:
        return None
    for rec in chain_records():
        if rec.get("rec_hash") == rec_hash:
            return rec
    return None
