#!/usr/bin/env python3
"""The DRAIN actuator — the intake dir is transient, never a destination.

Raw memory entries accumulate in the adopted store faster than anyone re-reads
them; undrained, they are lost AND useless. Drain reads each raw entry,
classifies it, routes it into its typed home, and archives the source with a
receipt. Nothing is ever deleted: every touched file is copied into a dated
archive dir FIRST, and the net is verified non-empty before any mutation (a
rollback net that captured nothing is a failed drain, loudly).

Classification (frontmatter `type:` first, filename prefix second):
  feedback entries    -> typed PRIOR (confidence 0.9, source human-feedback,
                         load_class jit — the JIT lane's specificity guard is
                         what keeps 169 new priors from wallpapering turns)
  reference entries   -> typed REFERENCE (ref-*.md)
  project entries     -> the project's journal in ~/.helm/<project>/journal/
                         (target resolved against the registry; unresolved
                         entries STAY and are reported)
  prem-*/prior-* twins-> the prem tombstone archives (--sweep-dups only;
                         operator-gated: it removes canon-store files, safe
                         per read-time dedup but surfaced first by design)
  already-typed files -> skipped (governed by the store)
  everything else     -> kept in place as episodic (not everything must move)

Dry-run by default; --apply executes; --limit N drains incrementally.
The index file (MEMORY.md) is a projection: lines referencing moved files are
re-pointed, never invented.
"""
import os
import re
import shutil
import sys

from . import home, pk, registry

_FM_DEFAULTS = {"name": "", "description": "", "type": "", "load_class": "",
                "originsessionid": ""}

TYPED_PREFIXES = ("prior-", "lex-", "heuristic-", "ref-", "reflex-")
DRAIN_CONFIDENCE = 0.9  # human feedback is strong evidence, not certainty


def _mem_dir():
    return home.adopted_memory_dir()


def _entries(mem):
    for n in sorted(os.listdir(mem)):
        p = os.path.join(mem, n)
        if not (n.endswith(".md") and os.path.isfile(p)) or n == "MEMORY.md":
            continue
        yield n, p


def _retype_or_conflict(src, dst, slug, to_type, e, names, planned_dsts):
    """A retype must NEVER clobber an existing typed entry (it may hold months
    of curated evidence) or a sibling drain action's destination. Either
    collision downgrades to a surfaced 'conflict' the operator resolves, never
    a silent overwrite."""
    if dst in names:
        return {"op": "conflict", "src": src, "dst": dst,
                "why": "target %s already exists in the store — would overwrite "
                       "a curated entry; resolve by hand (supersede/merge)" % dst}
    if dst in planned_dsts:
        return {"op": "conflict", "src": src, "dst": dst,
                "why": "two intake files map to %s (also %s) — slug collision"
                       % (dst, planned_dsts[dst])}
    planned_dsts[dst] = src
    return {"op": "retype", "src": src, "to_type": to_type, "dst": dst,
            "id": slug, "statement": e.get("description") or "",
            "origin": e.get("originsessionid") or ""}


def classify(mem=None):
    """-> list of action dicts. Pure derivation, no writes."""
    mem = mem or _mem_dir()
    names = set(os.listdir(mem)) if os.path.isdir(mem) else set()
    project_names = sorted(registry.load()["projects"], key=len, reverse=True)
    plan = []
    planned_dsts = {}   # dst filename -> first src that claimed it (collision guard)
    for n, p in _entries(mem):
        if n.startswith("prem-"):
            twin = "prior-" + n[len("prem-"):]
            if twin in names:
                plan.append({"op": "sweep-dup", "src": n, "twin": twin})
            continue  # un-twinned prem files stay: the store dual-reads them
        if n.startswith(TYPED_PREFIXES):
            continue
        e = pk.parse_simple_frontmatter(p, _FM_DEFAULTS) or {}
        etype = (e.get("type") or "").lower()
        base = n[:-3]
        if etype == "feedback" or n.startswith(("feedback-", "feedback_")):
            slug = pk.slug(re.sub(r"^feedback[-_]", "", base))
            dst = "prior-" + slug + ".md"
            plan.append(_retype_or_conflict(n, dst, slug, "prior", e, names, planned_dsts))
            continue
        if etype == "reference" or n.startswith(("reference-", "reference_")):
            slug = pk.slug(re.sub(r"^reference[-_]", "", base))
            dst = "ref-" + slug + ".md"
            plan.append(_retype_or_conflict(n, dst, slug, "reference", e, names, planned_dsts))
            continue
        if etype == "project" or n.startswith(("proj-", "proj_", "project_")):
            # filename-prefix match always counts; a description match needs a
            # name specific enough not to wallpaper (short names like "dev"
            # appear as ordinary words in half the corpus)
            blob = (e.get("description") or "").lower()
            target = next(
                (pn for pn in project_names
                 if base.lower().startswith(pn.lower() + "-") or base.lower() == pn.lower()
                 or (len(pn) >= 6 and re.search(
                     r"(?<![a-z0-9])" + re.escape(pn.lower()) + r"(?![a-z0-9])", blob))),
                None)
            if target:
                plan.append({"op": "route-project", "src": n, "project": target,
                             "dst": os.path.join(home.project_dir(target), "journal", n)})
            else:
                plan.append({"op": "keep", "src": n,
                             "why": "project entry, no registry match"})
            continue
        plan.append({"op": "keep", "src": n, "why": "episodic"})
    return plan


def _keywords_from(slug, statement):
    from .store import GENERIC_KEYWORDS
    words = [w for w in re.split(r"[^a-z0-9]+", (slug + " " + statement[:80]).lower())
             if len(w) > 3 and w not in GENERIC_KEYWORDS]
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return ",".join(seen[:6])


def _retype_text(src_path, act, ts):
    """The upgraded entry: typed frontmatter + the ORIGINAL body preserved
    verbatim (a drain that loses the why-and-how of an entry drained nothing)."""
    with open(src_path, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    body = raw.split("---", 2)[2].lstrip("\n") if raw.count("---") >= 2 else raw
    st = re.sub(r"\s+", " ", (act["statement"] or act["id"]).replace('"', "'"))[:300]
    to_prior = act["to_type"] == "prior"
    kind = "prior" if to_prior else "reference"
    fm = [
        "---",
        "name: " + act["dst"][:-3],
        'description: "' + kind + ": " + (act["id"] + " - " + st)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: " + kind,
        "  id: " + act["id"],
        "  statement: " + st,
    ]
    if to_prior:
        fm += ["  confidence: %.2f" % DRAIN_CONFIDENCE,
               "  class: prior",
               "  load_class: jit",
               "  evidence_log: " + '[{"ts":"%s","type":"stated","delta":%.2f,'
               '"reason":"drained from feedback memory","by":"human"}]'
               % (ts, DRAIN_CONFIDENCE),
               "  confidence_history: " + '[{"ts":"%s","value":%.2f,'
               '"reason":"drained from feedback memory"}]' % (ts, DRAIN_CONFIDENCE)]
    else:
        fm += ["  load_class: jit"]
    fm += ["  status: live",
           "  keywords: " + _keywords_from(act["id"], act["statement"] or ""),
           "  source: drain",
           "  stated_ts: " + ts,
           "  last_updated: " + ts,
           "  drained_from: " + act["src"]]
    if act.get("origin"):
        fm.append("  origin_session: " + act["origin"])
    fm += ["---", ""]
    return "\n".join(fm) + body


def _repoint_index(mem, renames):
    """MEMORY.md is a projection — re-point lines whose link target moved."""
    idx = os.path.join(mem, "MEMORY.md")
    try:
        with open(idx, encoding="utf-8") as f:
            raw = f.read()
    except Exception:
        return 0
    hits = 0
    for old, new in renames.items():
        if "(" + old + ")" in raw:
            raw = raw.replace("(" + old + ")", "(" + new + ")")
            hits += 1
    if hits:
        pk.atomic_write(idx, raw)
    return hits


def apply(plan, mem=None, sweep_dups=False, limit=None):
    """Execute a plan. Archive-first with a verified net; returns the receipt."""
    mem = mem or _mem_dir()
    ts = pk.now_ts()
    doable = [a for a in plan if a["op"] in ("retype", "route-project")
              or (sweep_dups and a["op"] == "sweep-dup")]
    if limit:
        doable = doable[:limit]
    if not doable:
        return {"ts": ts, "applied": 0, "note": "nothing to apply"}

    net = os.path.join(mem, "archive", "drain-" + ts.replace(":", "").replace("-", "")[:13])
    os.makedirs(net, exist_ok=True)
    # net every SOURCE and any pre-existing DESTINATION (a retype overwriting an
    # existing typed file must leave that file recoverable — classify() blocks
    # the common case, but a race or a hand-crafted plan must still be safe).
    to_net = {}
    for a in doable:
        to_net[a["src"]] = os.path.join(mem, a["src"])
        dst = a.get("dst")
        if a["op"] == "retype" and dst and os.path.isfile(os.path.join(mem, dst)):
            # a subdir, not a name prefix: a src literally named
            # overwritten-<dst> must never collide with the netted dst
            os.makedirs(os.path.join(net, "overwritten"), exist_ok=True)
            to_net[os.path.join("overwritten", dst)] = os.path.join(mem, dst)
    for label, srcpath in to_net.items():
        shutil.copy2(srcpath, os.path.join(net, label))
    # the rollback net must hold every file, byte-identical, before ANY mutation
    for label, srcpath in to_net.items():
        cop = os.path.join(net, label)
        if not os.path.isfile(cop) or os.path.getsize(cop) != os.path.getsize(srcpath):
            raise RuntimeError("drain: rollback net incomplete for %s — ABORTING, "
                              "nothing mutated" % label)

    renames = {}
    applied = []
    for a in doable:
        src = os.path.join(mem, a["src"])
        if a["op"] == "sweep-dup":
            os.remove(src)
            renames[a["src"]] = a["twin"]
        elif a["op"] == "retype":
            pk.atomic_write(os.path.join(mem, a["dst"]), _retype_text(src, a, ts))
            os.remove(src)
            renames[a["src"]] = a["dst"]
        elif a["op"] == "route-project":
            os.makedirs(os.path.dirname(a["dst"]), exist_ok=True)
            shutil.copy2(src, a["dst"])
            os.remove(src)
            renames[a["src"]] = a["dst"]
        applied.append(a)
    repointed = _repoint_index(mem, renames)
    receipt = {"ts": ts, "applied": len(applied), "actions": applied,
               "index_lines_repointed": repointed, "net": net}
    pk.write_json(os.path.join(net, "RECEIPT.json"), receipt)
    return receipt


def cmd_drain(args):
    """drain [--apply] [--sweep-dups] [--limit N] — classify raw memory
    entries and route them to typed homes. Dry-run by default."""
    mem = _mem_dir()
    if not os.path.isdir(mem):
        print("helm drain: no adopted memory dir at " + mem)
        return 1
    plan = classify(mem)
    by_op = {}
    for a in plan:
        by_op.setdefault(a["op"], []).append(a)
    n_do = len(by_op.get("retype", [])) + len(by_op.get("route-project", []))
    print("helm drain plan (%d raw entries):" % len(plan))
    for op in ("retype", "route-project", "sweep-dup", "conflict", "keep"):
        acts = by_op.get(op, [])
        if not acts:
            continue
        print("  %-14s %d" % (op, len(acts)))
        for a in acts[:3 if op not in ("keep", "conflict") else (5 if op == "conflict" else 2)]:
            tgt = a.get("dst") or a.get("twin") or a.get("why", "")
            print("      %s -> %s" % (a["src"], tgt))
        if len(acts) > (5 if op == "conflict" else 3):
            print("      ... %d more" % (len(acts) - (5 if op == "conflict" else 3)))
    if by_op.get("conflict"):
        print("  note: %d conflict%s NOT auto-drained (would overwrite a curated "
              "entry or collide) — resolve by hand" % (
                  len(by_op["conflict"]), "s"[:len(by_op["conflict"]) != 1]))
    if by_op.get("sweep-dup") and "--sweep-dups" not in args:
        print("  note: %d prem/prior twin tombstones need --sweep-dups "
              "(removes canon-store files; safe per read-time dedup — "
              "operator-gated by design)" % len(by_op["sweep-dup"]))
    if "--apply" not in args:
        print("helm drain: DRY-RUN (nothing moved). Re-run with --apply%s." %
              (" [--sweep-dups]" if by_op.get("sweep-dup") else ""))
        return 0
    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    receipt = apply(plan, mem, sweep_dups="--sweep-dups" in args, limit=limit)
    print("helm drain: APPLIED %d action%s; net + receipt at %s; "
          "%d index lines re-pointed"
          % (receipt["applied"], "s"[:receipt["applied"] != 1],
             receipt.get("net", "-"), receipt.get("index_lines_repointed", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(cmd_drain(sys.argv[1:]))
