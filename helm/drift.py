#!/usr/bin/env python3
"""The drift report — confidence-weighted beliefs, surfaced to the operator
ONLY on drift. No drift, no output; no news is silence.

Drift =
  1. CONTRADICTED certainty: a confidence-1.0 premise whose evidence log
     carries agent contradictions (the log records what confidence refuses to
     move for — that logged tension IS the alignment-break signal).
  2. TIER CROSSING since the last report: a belief crossed the auto-act line
     (0.85) in either direction, or fell dormant (< 0.4).
  3. DECAY: beliefs sitting dormant — held so weakly they no longer inject.
  4. EVOLVED: a superseded premise. When the native supersession chain
     verifies (premise.verify_link — OFFLINE, drift never calls the node) it
     is ATTESTED belief-evolution ("held X until T, then Y", provable);
     without a native link it is an unbacked store-only supersession. Either
     way the history is READ, never silently lost (DECISION clause 6).

State: one snapshot of {id: confidence} PER SCOPE under _global/.state/ —
the diff between same-scope runs is what makes crossings reportable exactly
once. Scope-keying matters: a --project run resolves shadowed confidences,
and writing those into the global snapshot minted spurious tier-crossings
on the next global run (and vice versa). Evolution hops latch the same way
in a sibling scope-keyed file ({old-slug: new-slug}) — each hop reports
exactly once, and a re-pointed hop re-reports.
"""
import os

from . import home, pk

ACT_AT = 0.85
DORMANT_BELOW = 0.4


def _state_path(project=None):
    name = ("drift-snapshot-%s.json" % pk.slug(project)) if project \
        else "drift-snapshot.json"
    return os.path.join(home.global_dir(), ".state", name)


def _evolved_path(project=None):
    name = ("drift-evolved-%s.json" % pk.slug(project)) if project \
        else "drift-evolved.json"
    return os.path.join(home.global_dir(), ".state", name)


def _contradictions(e):
    """Tolerate legacy string rows in old evidence logs — fail-open, dict-only."""
    return [r for r in (e.get("evidence_log") or []) if isinstance(r, dict)
            and (r.get("type") == "contradict" or (r.get("delta") or 0) < 0)]


def findings(project=None, snapshot=True):
    """-> (rows, n). The STRUCTURED drift feed — each row a dict evolve can mint
    commands from: {"kind": "contradicted", id, n, latest} | {"kind": "tier",
    id, tier, dir, was, now} | {"kind": "decayed", id, conf} | {"kind":
    "evolved", id, to, attested}. report() is the human rendering of exactly
    this list; n counts the LIVE priors."""
    from . import premise, store
    entries = store.load_all(project=project, include_dormant=True,
                             include_retired=True, types=("prior",))
    live = [e for e in entries if e.get("status") == store.STATUS_LIVE]
    by_slug = {pk.slug(str(e["id"])): e for e in entries}
    prev = pk.read_json(_state_path(project), {})
    prev_ev = pk.read_json(_evolved_path(project), {})
    out = []
    ev_now = {}
    for e in sorted(entries, key=lambda x: x["id"]):
        eid, conf = e["id"], e["confidence"]
        if e.get("status") != store.STATUS_LIVE:
            # a superseded premise is the evolution lane; retired beliefs and
            # dangling replacements stay silent (nothing verifiable to say).
            # A SAME-slug replaced_by is the drain's twin-migration tombstone
            # (prem-* -> prior-* file move), not a belief changing — skip it.
            rslug = pk.slug(str(e.get("replaced_by") or ""))
            new = by_slug.get(rslug)
            if e.get("class") == "certain" and new is not None \
                    and rslug != pk.slug(str(eid)):
                key, to = pk.slug(str(eid)), pk.slug(str(new["id"]))
                ev_now[key] = to
                if prev_ev.get(key) != to:
                    out.append({"kind": "evolved", "id": eid,
                                "to": str(new["id"]),
                                "attested":
                                    premise.verify_link(e, new)[0] == "attested"})
            continue
        if e.get("class") == "certain":
            rows = [r for r in _contradictions(e) if r.get("by") != "human"]
            if rows:
                out.append({"kind": "contradicted", "id": eid, "n": len(rows),
                            "latest": (rows[-1].get("reason") or "")[:120]})
            continue
        was = prev.get(eid)
        if was is not None:
            for tier, name in ((ACT_AT, "auto-act"), (DORMANT_BELOW, "dormant")):
                if (was >= tier) != (conf >= tier):
                    out.append({"kind": "tier", "id": eid, "tier": name,
                                "dir": "rose" if conf >= tier else "fell",
                                "was": was, "now": conf})
        if e.get("load_class") == "dormant":
            out.append({"kind": "decayed", "id": eid, "conf": conf})
    if snapshot:
        pk.write_json(_state_path(project), {e["id"]: e["confidence"] for e in live
                                             if e.get("class") != "certain"})
        pk.write_json(_evolved_path(project), ev_now)
    return out, len(live)


def _line(f):
    if f["kind"] == "contradicted":
        return "CONTRADICTED  %s — %d agent contradiction%s logged; latest: %s" \
            % (f["id"], f["n"], "s"[:f["n"] != 1], f["latest"])
    if f["kind"] == "tier":
        arrow = "rose past" if f["dir"] == "rose" else "fell below"
        return "TIER          %s — %s %s (%.2f -> %.2f)" \
            % (f["id"], arrow, f["tier"], f["was"], f["now"])
    if f["kind"] == "evolved":
        if f["attested"]:
            return "EVOLVED       %s -> %s — attested chain (biography: helm " \
                "premise-check --chain %s)" % (f["id"], f["to"], f["id"])
        return "EVOLVED       %s -> %s — NO native chain (store-only " \
            "supersession, unbacked)" % (f["id"], f["to"])
    return "DECAYED       %s — confidence %.2f, no longer injecting " \
        "(re-confirm or retire)" % (f["id"], f["conf"])


def report(project=None, snapshot=True):
    """-> (lines, counts). Empty lines list == no drift."""
    rows, n = findings(project=project, snapshot=snapshot)
    return [_line(f) for f in rows], n


def cmd_drift(args):
    """drift [--project P] [--peek] — surface belief drift; silent when none."""
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    lines, n = report(project=project, snapshot="--peek" not in args)
    if not lines:
        print("helm drift: no drift (%d priors steady)." % n)
        return 0
    print("helm drift (%d of %d priors):" % (len(lines), n))
    for line in lines:
        print("  " + line)
    return 0
