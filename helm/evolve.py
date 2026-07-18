#!/usr/bin/env python3
"""The self-evolution engine — one cycle of notice -> name -> propose.

The loop that keeps helm improving itself: observe the stores, notice what
drifted or accumulated, and PROPOSE the next actions. The anti-rulesurf gate
is constitutional: evolve NEVER mutates — every proposal is an explicit verb
the operator (or a supervising agent) runs deliberately. A system that edits
its own beliefs unprompted isn't learning, it's drifting.

`helm evolve` composes the observers:
  sync     is the registry current?
  drain    what raw intake is routable to typed homes?
  drift    what beliefs moved or broke?
  doctor   what's structurally unhealthy?
  whoami   is the warmth leg still empty?

and emits a short, ranked proposal list. Empty stores + steady beliefs +
healthy home -> one quiet line.
"""
from . import drain, drift, registry


def proposals():
    out = []

    reg, report = registry.sync()
    if report["new"]:
        out.append(("registry", "%d new project%s discovered: %s"
                    % (len(report["new"]), "s"[:len(report["new"]) != 1],
                       ", ".join(report["new"])), None))

    plan = drain.classify()
    routable = [a for a in plan if a["op"] in ("retype", "route-project")]
    dups = [a for a in plan if a["op"] == "sweep-dup"]
    if routable:
        out.append(("drain", "%d raw entr%s routable to typed homes"
                    % (len(routable), "ies" if len(routable) != 1 else "y"),
                    "helm drain --apply"))
    if dups:
        out.append(("drain", "%d prem/prior twin tombstones sweepable (operator-gated)"
                    % len(dups), "helm drain --apply --sweep-dups"))

    lines, _ = drift.report(snapshot=False)
    if lines:
        out.append(("drift", "%d belief%s drifting" % (len(lines), "s"[:len(lines) != 1]),
                    "helm drift"))

    try:
        from . import whoami
        if not whoami.profile_has_content():
            out.append(("who", "the warmth leg is empty — the five-minute interview "
                        "makes every agent warmer", "helm interview"))
    except Exception:
        pass
    return out


def cmd_evolve(args):
    """evolve — one observe/propose cycle. Proposes, never mutates."""
    props = proposals()
    if not props:
        print("helm evolve: steady — nothing to propose.")
        return 0
    print("helm evolve — %d proposal%s (nothing mutated):" % (len(props), "s"[:len(props) != 1]))
    for area, what, verb in props:
        print("  [%s] %s%s" % (area, what, ("  ->  " + verb) if verb else ""))
    return 0
