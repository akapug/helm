"""helm premise-check — honest tiering (native primary; anchor secondary).

The read/verify CLI. Top of the one-way dep graph: depends on _common, _verify,
and _capture (_pop_flag).
"""
import sys

from .. import cell, pk, store
from ._capture import _pop_flag
from ._common import (EXIT_NO_NATIVE_PROOF, _CHECK_DEFAULTS, _USAGE_CHECK,
                      _front, _queue_path, digest_payload, payload_digest)
from ._verify import _record_expect, verify_link, verify_record


def _anchor_line(meta):
    """The honest external-anchor status line for one entry. A reachable node can
    only show that a turn with the stored hash EXISTS ('turn OBSERVED') — it does
    NOT prove that turn commits this record's hash (attest_anchor_turn is mutable
    frontmatter, swappable for any real turn). So this is never 'CONFIRMED'/
    independent re-verification until dregg exposes payload disclosure."""
    aturn = meta.get("attest_anchor_turn")
    if not aturn:
        return "  external anchor: none (native-only)"
    observed, detail = cell.verify_anchor(aturn)
    return "  external anchor: %s — %s" % (
        "turn OBSERVED" if observed else "unverified", detail)


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
    ok = True        # cleared on any BROKEN digest / record / link
    absent = False   # set when an entry's PRIMARY native proof is missing
    for i, c in enumerate(chain):
        print("  %d. %s [%s] - %s"
              % (i + 1, c["id"], c["status"], c.get("statement") or ""))
        payload = _front(c, "attest_payload")
        rec = _front(c, "attest_record")
        if not payload and not rec:
            print("       unattested (no payload recorded)")
            absent = True
        else:
            if payload:
                match = payload_digest(payload) == \
                    payload_digest(digest_payload(c.get("statement") or ""))
                ok = ok and match
                print("       digest %s %s"
                      % ("MATCH" if match else "MISMATCH", payload))
            else:
                # A record pointer exists but its payload metadata is gone —
                # present-but-DAMAGED proof (mirrors the plain path's exit 1),
                # never the informational not-attested state.
                ok = False
                print("       digest MISSING — attest_record present but "
                      "attest_payload stripped (damaged annotation)")
            if rec:
                rok, detail = verify_record(rec, _record_expect(c, project))
                ok = ok and rok
                print("       native chain %s — %s"
                      % ("VERIFIED" if rok else "BROKEN", detail))
            else:
                print("       native chain: NOT ATTESTED — no native record "
                      "(legacy/--no-attest); a digest match alone is not proof")
                absent = True
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
    # Match the plain-path exit contract: BROKEN (1) beats missing-primary-proof
    # (EXIT_NO_NATIVE_PROOF) beats verified (0). Absence never reads as success.
    return 1 if not ok else (EXIT_NO_NATIVE_PROOF if absent else 0)


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
    rec = meta.get("attest_record")
    if not meta.get("attest_payload") and not rec:
        # The PRIMARY proof is absent (never attested / --no-attest). This is a
        # deliberate informational state — but it MUST NOT read as success to
        # automation, so it exits on its own contract (EXIT_NO_NATIVE_PROOF),
        # distinct from a present-but-broken proof (1).
        print("  native chain: NOT ATTESTED — no native record (captured with "
              "--no-attest; a pending anchor row may sit in " + _queue_path() + ")")
        print("  status: NOT ATTESTED (no primary proof to verify)")
        return EXIT_NO_NATIVE_PROOF
    recomputed = digest_payload(e.get("statement") or "")
    match = payload_digest(meta.get("attest_payload") or "") == payload_digest(recomputed)
    if match:
        print("  digest: MATCH " + meta["attest_payload"])
    else:
        print("  digest: MISMATCH — the stored statement no longer hashes to "
              "the attested payload")
        print("    attested:   " + (meta.get("attest_payload") or "«none»"))
        print("    recomputed: " + recomputed)
    print("  recorded by '%s' at %s (provenance label, not a cell signer)"
          % (meta.get("attest_by") or "?", meta.get("attest_ts") or "?"))
    if not rec:
        # A payload is recorded but the native record hash is not — the primary
        # proof is absent, so a digest match ALONE is not verification. Absence
        # of the primary proof never exits success.
        print("  native chain: NOT ATTESTED — no native record hash recorded "
              "(legacy or --no-attest); a digest match alone is not verification")
        print(_anchor_line(meta))
        return EXIT_NO_NATIVE_PROOF
    # BIND the native record to THIS premise: a record that recomputes but
    # commits a different premise_id/digest/op/root/project/link is BROKEN.
    rok, detail = verify_record(rec, _record_expect(e, project))
    print("  native chain: %s — %s" % ("VERIFIED" if rok else "BROKEN", detail))
    print(_anchor_line(meta))
    if meta.get("attest_supersedes_record"):
        print("  chain: supersedes prior record %s — walk it: helm "
              "premise-check --chain %s"
              % (meta["attest_supersedes_record"][:16], pid))
    return 0 if (match and rok) else 1
