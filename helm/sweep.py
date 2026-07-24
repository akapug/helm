#!/usr/bin/env python3
"""The SWEEP actuator — lineage-driven supersession of the adopted store.

The adopted store FEEDS inject: a JIT resolver over a corpus whose majority
carries superseded premises fires stale knowledge into live turns, and salience
is the scarce resource. The family tree already knows what died — a project
`descends-from` / `supersedes` an ancestor — so sweep walks those lineage edges
and proposes superseding the ancestor-era entries the store still holds by their
successor-era twins (an ancestor-era premise superseded by its successor).

Never on weak signal. A proposal needs BOTH a directed lineage succession edge
AND a same-slug (same base after stripping the era token) or high keyword-overlap
match between an ancestor-era entry and a successor-era one. Term-mention alone
never qualifies (an entry that MENTIONS a dead harness while stating a live
lesson is not superseded), which is the risk quoted evidence + a per-entry
command are load-bearing against.

PROPOSE-ONLY by default (like drain/evolve) — the dry-run prints each proposed
`helm store supersede` command with its quoted before/after evidence and reason.
`--apply` runs the supersession through store.mark_superseded (the public API):
old is tombstoned delete_eligible with a backpointer, the FILE STAYS (never
deleted, re-promotable), and a receipt lands on the events journal. Fail-open:
a missing registry or unreadable store yields a clean line, never a crash.
"""
import re
import sys

from . import lineage, pk, store

# Directed succession edges: the edge OWNER is the newer/live node, its target
# the ancestor. supersedes is explicit; descends-from/forked-from place the
# owner as the child (successor) of its parent (ancestor). checkout-of is a
# worktree duplicate, not a knowledge succession — deliberately excluded.
SUCCESSION_RELS = ("supersedes", "descends-from", "forked-from")

# Only these types are supersedable through store.mark_superseded.
WRITABLE = ("prior", "heuristic", "reference")

# Segments too generic to be an era token on their own (a same-slug base match
# still gates precision, but these would over-recall the ancestor/successor pool).
GENERIC_SEGMENTS = frozenset({
    "alpha", "beta", "main", "core", "base", "priv", "private", "public",
    "proto", "test", "demo", "prod", "node", "repo", "app", "old", "new", "tmp",
})

OVERLAP_MIN = 0.6       # keyword/statement Jaccard for the high-overlap fallback
MIN_BASE = 4            # a same-slug base shorter than this is a bare type-prefix


def _tokens(name):
    """The era tokens for a project name: its full slug plus each distinctive
    hyphen segment (>= 4 chars, non-generic) — so `project-a-private-beta` tags
    both `lex-project-a--*` (via `project-a`) and its own full slug."""
    s = pk.slug(name)
    toks = {s}
    for seg in s.split("-"):
        if len(seg) >= 4 and seg not in GENERIC_SEGMENTS:
            toks.add(seg)
    return toks


def _has_token(slug, token):
    """token present as a whole word in slug (bounded by non-alphanumeric) —
    `helm` matches `helm-x` and `old_x`, never `helmet` or `rebuilt`."""
    return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", slug) is not None


def _base(slug, tokens):
    """slug with every era token stripped and separators collapsed — the
    era-independent identity two twins share (`old-rollover` / `helm-rollover`
    -> `rollover`). Longest token first so a full slug is removed before a
    segment it contains."""
    b = slug
    for t in sorted(tokens, key=len, reverse=True):
        b = re.sub(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", "-", b)
    return re.sub(r"[^a-z0-9]+", "-", b).strip("-")


def _kwset(e):
    """The comparison term set: authored keywords if any, else the distinctive
    (>= 5 char) words of the statement."""
    kws = {w for w in re.split(r"[,\s]+", (e.get("keywords") or "").lower()) if w}
    if kws:
        return kws
    return {w for w in re.split(r"[^a-z0-9]+", (e.get("statement") or "").lower())
            if len(w) >= 5}


def _overlap(a, b):
    """Jaccard over the two term sets; 0.0 when either is empty."""
    sa, sb = _kwset(a), _kwset(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _succession_pairs():
    """Directed (successor, ancestor, rel) triples off the lineage edges.
    Fail-open: no registry / broken graph -> []."""
    try:
        g = lineage.graph()
    except Exception:
        return []
    out = []
    for name, node in (g or {}).items():
        for e in (node.get("edges") or []):
            rel, to = e.get("rel"), e.get("to")
            if rel in SUCCESSION_RELS and to and to != name:
                out.append((name, to, rel))
    return out


def _clip(s, n=110):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 3] + "..."


def propose(project=None):
    """The lineage-driven supersession proposals, ranked (same-slug before
    high-overlap, stronger first). Pure derivation, no writes. Each: {old_id,
    new_id, type, successor, ancestor, rel, signal, score, reason, old_stmt,
    new_stmt}."""
    pairs = _succession_pairs()
    if not pairs:
        return []
    try:
        entries = [e for e in store.load_all(project=project) if e["type"] in WRITABLE]
    except Exception:
        return []
    if not entries:
        return []

    best = {}   # old_id -> (score_tuple, proposal)
    for successor, ancestor, rel in pairs:
        stoks, atoks = _tokens(successor), _tokens(ancestor)
        if not (stoks and atoks) or (stoks & atoks):
            continue   # a shared token means no clean era split — skip the edge
        olds, news = [], []
        for e in entries:
            sl = pk.slug(str(e["id"]))
            in_a = any(_has_token(sl, t) for t in atoks)
            in_s = any(_has_token(sl, t) for t in stoks)
            if in_a and not in_s:
                olds.append((e, sl))
            elif in_s and not in_a:
                news.append((e, sl))
        if not (olds and news):
            continue
        for old, osl in olds:
            obase = _base(osl, atoks)
            cur = None   # (score_tuple, signal, detail, new)
            for new, nsl in news:
                if new["type"] != old["type"] or nsl == osl:
                    continue
                if obase and len(obase) >= MIN_BASE and obase == _base(nsl, stoks):
                    cand = ((2.0, 1.0), "same-slug", "same base slug '%s'" % obase, new)
                else:
                    ov = _overlap(old, new)
                    if ov < OVERLAP_MIN:
                        continue
                    cand = ((1.0, ov), "high-overlap",
                            "%d%% keyword overlap" % round(100 * ov), new)
                if cur is None or cand[0] > cur[0]:
                    cur = cand
            if cur is None:
                continue
            score, signal, detail, new = cur
            prop = {
                "old_id": str(old["id"]), "new_id": str(new["id"]),
                "type": old["type"], "successor": successor, "ancestor": ancestor,
                "rel": rel, "signal": signal, "score": score[1],
                "reason": "lineage: %s %s %s + %s" % (successor, rel, ancestor, detail),
                "old_stmt": old.get("statement") or "",
                "new_stmt": new.get("statement") or "",
            }
            key = prop["old_id"]
            if key not in best or score > best[key][0]:
                best[key] = (score, prop)
    out = [p for _, p in best.values()]
    out.sort(key=lambda p: (p["signal"] != "same-slug", -p["score"], p["old_id"]))
    return out


def _cmd(p, ts, project):
    pf = (" --project " + project) if project else ""
    return "helm store supersede %s %s %s %s%s" % (
        ts, p["old_id"], p["new_id"], p["reason"], pf)


def apply(props, project=None):
    """Run each proposal through store.mark_superseded (the public supersede
    API — tombstones old delete_eligible, keeps the file). Collects per-entry
    receipts; a stale pair (old already gone/superseded) is reported, never
    fatal. -> receipt dict."""
    ts = pk.now_ts()
    applied, failures = [], []
    for p in props:
        _, err = store.mark_superseded(p["old_id"], p["new_id"], ts, p["reason"],
                                       project=project)
        if err:
            failures.append({"old_id": p["old_id"], "new_id": p["new_id"], "error": err})
        else:
            applied.append({"old_id": p["old_id"], "new_id": p["new_id"],
                            "signal": p["signal"], "reason": p["reason"]})
    if applied:
        pk.event("sweep.apply", "adopted-store",
                 "%d lineage supersession%s applied"
                 % (len(applied), "s"[:len(applied) != 1]))
    return {"ts": ts, "applied": len(applied), "errors": len(failures),
            "actions": applied, "failures": failures}


def cmd_sweep(args):
    """sweep [--apply] [--project P] — lineage-driven supersession sweep of the
    adopted store: propose superseding ancestor-era entries by their
    successor-era twins (lineage edge + same-slug/high-overlap required).
    Dry-run by default."""
    # tail guard before propose() — membership reads ('--apply' in args)
    # let `sweep --frobnicate --help` run the sweep and exit 0.
    from .cli import guard_tail
    rc = guard_tail("helm sweep", args, flags=("--apply",),
                    valued=("--project",),
                    usage="sweep [--apply] [--project P]")
    if rc is not None:
        return rc
    project = None
    if "--project" in args:
        i = args.index("--project")
        if i + 1 >= len(args):
            print("helm sweep: --project needs a name", file=sys.stderr)
            return 2
        project = args[i + 1]
    props = propose(project=project)
    if not props:
        print("helm sweep: no lineage-superseded entries — the adopted store is "
              "clean against the family tree.")
        return 0
    ts = pk.now_ts()
    edges = len({(p["successor"], p["ancestor"]) for p in props})
    if "--apply" not in args:
        print("helm sweep — %d lineage supersession%s proposed across %d edge%s "
              "(PROPOSE-ONLY; nothing superseded):"
              % (len(props), "s"[:len(props) != 1], edges, "s"[:edges != 1]))
        for p in props:
            print("  [%s] %s -> %s   (%s %s %s)" % (
                p["signal"], p["old_id"], p["new_id"],
                p["successor"], p["rel"], p["ancestor"]))
            if p["old_stmt"]:
                print('      old: "%s"' % _clip(p["old_stmt"]))
            if p["new_stmt"]:
                print('      new: "%s"' % _clip(p["new_stmt"]))
            print("      ->  " + _cmd(p, ts, project))
        print("helm sweep: DRY-RUN (nothing superseded). Re-run with --apply to "
              "tombstone via store.supersede (files KEPT — re-promotable).")
        return 0
    r = apply(props, project=project)
    print("helm sweep: APPLIED %d supersession%s%s (files KEPT — delete_eligible, "
          "re-promotable)" % (
              r["applied"], "s"[:r["applied"] != 1],
              (", %d failed" % r["errors"]) if r["errors"] else ""))
    for a in r["actions"]:
        print("  %s -> %s  [%s]" % (a["old_id"], a["new_id"], a["signal"]))
    for f in r["failures"]:
        print("  ! %s -> %s: %s" % (f["old_id"], f["new_id"], f["error"]))
    return 0


if __name__ == "__main__":
    sys.exit(cmd_sweep(sys.argv[1:]))
