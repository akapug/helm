"""Move lease holders without granting a new lease or losing rollback bindings.

The claims module remains the public and patchable boundary. Dependencies are
looked up there at call time, rather than copied into this module: a direct
claims patch or a seats-facade patch must still reach the transfer transaction.
"""
from . import seats_claims as claims


def rebind_claim_holder(source, target, snap=None, restore=None):
    """Move every live lease from one seat NAME to another: (ok, msg, manifest).

    `_binding_ok` compares `holder` RAW, so a rename ORPHANS the old name's
    leases, and release_stale cannot rescue them — it needs exactly "stale"
    and a renamed seat's session is usually still LIVE. Under the sibling's
    law: nonce, fence, expiry and ts cross UNTOUCHED, so no TTL grows here.

    THE SESSION IS CLEARED. Kept, `_binding_ok` would refuse the new holder's
    OWN release (their session disagrees with the recorded one) and
    claim_holder_liveness would answer about the WRONG process, marking their
    LIVE lease "stale" for anyone to release_stale away. Cleared reads
    "unknown" — not actionable, the safe direction. It leaves in `session_was`.

    REFUSES ONLY ON A MEASURED CONTRADICTION: a missing name, a byte-identical
    pair (a case-VARIANT is a REAL move — routing casefolds, this does not), a
    target no @mention can address, a target the roster WAS READ and does not
    list, an unparseable ledger (the write is WHOLE-FILE), an unavailable
    lock. An UNREADABLE roster PROCEEDS; a source holding nothing is an EMPTY
    RESULT that writes nothing. The target is stored VERBATIM — hand the
    ROSTER'S spelling, since listedness casefolds and `_binding_ok` does not.
    MANIFEST per lease: `resource` (the STORED key) + `shown` (laundered), the
    RAW `lease` the new holder now needs, `fence`, `remaining` on the
    UNCHANGED expiry, `session_was`. Pinned by tests/test_seats_claims.py.
    """
    src, dst = str(source or "").strip(), str(target or "").strip()
    if not src or not dst:
        return False, "a holder rebind names a SOURCE and a TARGET seat", []
    if src == dst:
        return False, "%s already holds those rows, byte for byte" % dst, []
    listed = claims.claim_holder_listedness({"holder": dst}, snap) \
        if restore is None else "listed"
    if listed in ("unlisted", "unmeasurable"):
        return False, ("%s is %s, so a lease moved there is orphaned again" % (
            dst, "unaddressable as a seat token" if listed == "unmeasurable"
            else "on no roster row (`helm chat seats --all`)")), []
    back = {e.get("resource"): e for e in restore or () if isinstance(e, dict)}
    moved, now = [], claims._now_mono()
    with claims._claim_flocked() as lock:
        if lock.f is None:
            return False, claims._lock_unavailable(), []
        try:
            c = claims._sweep(claims._claims_read(True))
        except OSError as exc:
            return False, "%s — the ledger was NOT rewritten" % exc, []
        for resource, row in list(c.items()):
            if resource == "_fence" or not isinstance(row, dict) \
                    or row.get("holder") != src:
                continue
            undo = back.get(resource)
            if restore is not None and (undo is None or
                                        undo.get("lease") != row.get("lease")):
                continue
            row = dict(row)
            was, row["holder"] = row.get("session"), dst
            row["session"] = undo.get("session_was") if undo else None
            c[resource] = row
            moved.append({"resource": resource, "shown": claims._pub_res(resource),
                          "lease": row.get("lease"), "fence": row.get("fence"),
                          "remaining": int(row.get("exp_mono", now) - now),
                          "session_was": was})
        if not moved:
            return True, "%s holds no matching live lease" % src, []
        claims.pk.write_json(claims.claims_path(), c)
    for entry in moved:
        claims._unlink_delegation_activity(entry["resource"])
    return True, ("%d lease%s moved %s -> %s; %s" % (
        len(moved), "" if len(moved) == 1 else "s", src, dst,
        "sessions RESTORED" if restore is not None else "sessions CLEARED — "
        "the new holder re-records their own on the next `claim`")), moved


def rollback_claim_holder(source, target, manifest):
    """Undo exactly the rows one rebind moved, and STRICTER than its sibling:
    a row returns only if it still carries the target as holder AND THE SAME
    NONCE, so a grant that expired and was re-minted under the new name (ABA)
    is left alone rather than handed back to a seat that holds nothing. Each
    row's recorded session comes back with it. It asks NOTHING of the roster:
    the source is the name a rename may have just removed, and refusing on
    that absence withdraws the undo when it is most needed. Rebind's shape."""
    return claims.rebind_claim_holder(target, source, restore=manifest)
