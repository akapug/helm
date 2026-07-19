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

State: one snapshot of {id: confidence} PER SCOPE under _global/.state/ —
the diff between same-scope runs is what makes crossings reportable exactly
once. Scope-keying matters: a --project run resolves shadowed confidences,
and writing those into the global snapshot minted spurious tier-crossings
on the next global run (and vice versa).
"""
import os

from . import home, pk

ACT_AT = 0.85
DORMANT_BELOW = 0.4


def _state_path(project=None):
    name = ("drift-snapshot-%s.json" % pk.slug(project)) if project \
        else "drift-snapshot.json"
    return os.path.join(home.global_dir(), ".state", name)


def _contradictions(e):
    """Tolerate legacy string rows in old evidence logs — fail-open, dict-only."""
    return [r for r in (e.get("evidence_log") or []) if isinstance(r, dict)
            and (r.get("type") == "contradict" or (r.get("delta") or 0) < 0)]


def findings(project=None, snapshot=True):
    """-> (rows, n). The STRUCTURED drift feed — each row a dict evolve can mint
    commands from: {"kind": "contradicted", id, n, latest} | {"kind": "tier",
    id, tier, dir, was, now} | {"kind": "decayed", id, conf}. report() is the
    human rendering of exactly this list."""
    from . import store
    entries = store.load_all(project=project, include_dormant=True, types=("prior",))
    prev = pk.read_json(_state_path(project), {})
    out = []
    for e in sorted(entries, key=lambda x: x["id"]):
        eid, conf = e["id"], e["confidence"]
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
        pk.write_json(_state_path(project), {e["id"]: e["confidence"] for e in entries
                                             if e.get("class") != "certain"})
    return out, len(entries)


def _line(f):
    if f["kind"] == "contradicted":
        return "CONTRADICTED  %s — %d agent contradiction%s logged; latest: %s" \
            % (f["id"], f["n"], "s"[:f["n"] != 1], f["latest"])
    if f["kind"] == "tier":
        arrow = "rose past" if f["dir"] == "rose" else "fell below"
        return "TIER          %s — %s %s (%.2f -> %.2f)" \
            % (f["id"], arrow, f["tier"], f["was"], f["now"])
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
