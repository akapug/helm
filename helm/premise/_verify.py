"""helm premise — verification: whole-chain, single-record, and offline hop.

Depends on _common + _chain only.
"""
from ._chain import chain_records, _record_by_hash, _record_hash
from ._common import _CORE_KEYS, _front, _root_label, digest_payload, payload_digest


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


def _norm_root(r):
    """Normalize the provenance-root vocabulary so a bound comparison is honest
    across the capture path (_root_label over an entry with no root key -> its
    'global'/'project' label) and the load/backfill path (the physical root name
    'helm-global'). The two name the SAME global root."""
    r = r or "global"
    return "global" if r in ("global", "helm-global") else r


def _record_expect(e, project):
    """The HASHED record fields a valid attest_record MUST commit for entry `e`.
    A record that recomputes but commits a DIFFERENT claim (a foreign premise's
    record, or the OLD record after the statement changed) does NOT prove THIS
    premise and must read BROKEN. Binds premise_id, the canonical statement
    digest, the operation, provenance root (normalized) + project, and the
    supersession link."""
    sup = _front(e, "attest_supersedes_record")
    return {"premise_id": str(e["id"]),
            "digest": digest_payload(e.get("statement") or ""),
            "op": "supersede" if sup else "create",
            "root": _root_label(e, project),
            "project": project or "",
            "supersedes_record": sup or ""}


def verify_record(rec_hash, expect=None):
    """Verify ONE record in place: recompute its hash, confirm its prev links to
    the record before it, and — when `expect` is given (see _record_expect) —
    confirm the record's HASHED fields BIND to the premise being checked. A
    record that recomputes but commits a different premise_id/digest/op/root/
    project/supersession link is BROKEN, not VERIFIED: an internally-valid
    record belonging to ANOTHER claim, or the OLD record after the statement
    changed, is NOT this premise's proof. (ok, detail)."""
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
        for k, want in (expect or {}).items():
            got = rec.get(k, "")
            if k == "root":
                got, want = _norm_root(got), _norm_root(want)
            if str(got) != str(want):
                return False, ("record commits %s=%r, not the checked premise's "
                               "%r — a foreign or stale record, not this proof"
                               % (k, rec.get(k, ""), want))
        return True, ("record %s at index %d links to %s"
                      % (rec_hash[:12], i, ((prev[:12] + "…") if prev else "genesis")))
    return False, "no native record %s in the chain" % (rec_hash or "")[:12]


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
    # BIND the pointer to the ledger: NEW's own native record must actually
    # commit this supersedes_record (mutable frontmatter alone is forgeable). A
    # record that resolves in the chain but hashes a DIFFERENT link is broken; a
    # record that does not resolve falls back to the frontmatter (legacy).
    new_rec = _front(new_e, "attest_record")
    rec = _record_by_hash(new_rec)
    if rec is not None and str(rec.get("supersedes_record", "")) != str(link):
        return "broken", "'%s' native record commits supersedes_record %s, not %s" \
            % (new_e["id"], (str(rec.get("supersedes_record", "")) or "«none»")[:12],
               link[:12])
    return "attested", "native record linkage verified (%s -> %s)" \
        % (prior[:12], (new_rec or "?")[:12])
