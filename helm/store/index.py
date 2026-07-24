"""helm store — MEMORY.md index cap + the add-guard/format leaf helpers.

The index_cap budget actuator + its CLI, plus the near-dup guard (_tokens/
_near_dup) and the CLI row formatter (_fmt). Moved verbatim from the pre-split
helm/store.py.
"""
import os
import re
import sys

from .. import pk
from ._common import _slug, _JIT_TYPES, _recency, STATUS_PROVISIONAL
from .load import adopted_dir, load_all


# ---------------------------------------------------------------------------
# index cap — the MEMORY.md budget actuator (memGC's missing half)
# ---------------------------------------------------------------------------

INDEX_BUDGET_LINES = 60  # the ancestor's proven structural fix (144 -> 58 live)


def _memory_index_path():
    """MEMORY.md — the natively-injected index projection, in the adopted dir."""
    return os.path.join(adopted_dir(), "MEMORY.md")


def _index_link_backing(line):
    """The backing .md filename in a markdown index link line, else None."""
    m = re.search(r"\]\(([^)]+\.md)\)", line)
    return os.path.basename(m.group(1)) if m else None


def index_cap(budget_lines=INDEX_BUDGET_LINES, apply=False, project=None):
    """Cap MEMORY.md at budget_lines by DEMOTING (unlinking) index link lines
    whose backing file is a typed jit-resolvable entry — the safety rail the
    ancestor lacked: the content stays reachable through inject, so unlinking is
    provably LOSSLESS. Ranked oldest last_updated first. NEVER demotes an
    always-class line (pinned) or an untyped/dormant line (unreachable via
    inject — unlinking WOULD lose it; run drain-v2 first — sequencing is the
    point). Demoted lines archive to a drain-style net + receipt. Dry-run
    default; on --apply the file is RE-READ fresh and atomically rewritten so a
    concurrent native append is preserved. -> receipt."""
    path = _memory_index_path()
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {"path": path, "exists": False, "lines": 0, "bytes": 0,
                "budget": budget_lines, "over": 0, "demotable": 0, "plan": [],
                "demoted": 0}
    lines = raw.splitlines()
    total = len(lines)
    over = max(total - budget_lines, 0)
    entries = {os.path.basename(e["path"]): e
               for e in load_all(project=project, include_retired=False)}
    demotable = []
    for i, line in enumerate(lines):
        backing = _index_link_backing(line)
        if not backing:
            continue
        e = entries.get(backing)
        # no live typed backing, not jit-typed, or always/dormant -> KEEP
        # (untyped/dormant content is unreachable via inject; always is pinned)
        if not e or e["type"] not in _JIT_TYPES \
                or e.get("load_class") in ("always", "dormant"):
            continue
        demotable.append((_recency(e), i, line, backing))
    demotable.sort(key=lambda t: t[0])  # oldest last_updated first
    to_demote = demotable[:over] if over else []
    receipt = {"path": path, "exists": True, "lines": total, "bytes": len(raw),
               "budget": budget_lines, "over": over, "demotable": len(demotable),
               "plan": [(t[2], t[3]) for t in to_demote], "demoted": 0}
    if not apply or not to_demote:
        return receipt
    try:  # re-read fresh: tolerate a native append landed since the plan
        with open(path, encoding="utf-8") as f:
            cur = f.read().splitlines()
    except OSError:
        return receipt
    remaining = [t[2] for t in to_demote]
    kept, removed = [], []
    for ln in cur:
        if ln in remaining:
            remaining.remove(ln)   # each demoted line dropped exactly once
            removed.append(ln)
        else:
            kept.append(ln)
    if not removed:
        return receipt  # the native path already changed every planned line
    ts = pk.now_ts()
    net = os.path.join(adopted_dir(), "archive",
                       "index-cap-" + ts.replace(":", "").replace("-", "")[:13])
    os.makedirs(net, exist_ok=True)
    pk.atomic_write(os.path.join(net, "MEMORY-demoted.md"),
                    "# demoted from MEMORY.md " + ts + " (content stays live via "
                    "inject — this is the unlink record)\n\n" + "\n".join(removed) + "\n")
    pk.write_json(os.path.join(net, "RECEIPT.json"),
                  {"ts": ts, "budget": budget_lines, "demoted": len(removed),
                   "removed": removed, "kept_lines": len(kept)})
    pk.atomic_write(path, "\n".join(kept) + ("\n" if raw.endswith("\n") else ""))
    receipt.update({"demoted": len(removed), "net": net, "kept_lines": len(kept)})
    pk.event("index.cap", path, "%d line%s demoted (budget %d, lossless — content "
             "stays jit-resolvable)" % (len(removed), "s"[:len(removed) != 1], budget_lines))
    return receipt


def cmd_index(args):
    """index cap [--budget-lines N] [--apply] [--project P] — the MEMORY.md
    budget actuator: demote link lines whose backing entry stays jit-resolvable
    (lossless), oldest first, until under budget. Self-firing Stop-hook line:
    `helm index cap --apply` (documented so the write path cannot exceed
    budget). Dry-run default."""
    args = list(args)
    project = None
    if "--project" in args:
        i = args.index("--project")
        project = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    if not args or args[0] != "cap":
        print("usage: helm index cap [--budget-lines N] [--apply]", file=sys.stderr)
        return 2
    rest = args[1:]
    # cap MUTATES MEMORY.md under --apply; junk refuses before the actuator.
    from ..cli import guard_tail
    rc = guard_tail("helm index cap", rest, flags=("--apply",),
                    valued=("--budget-lines",),
                    usage="index cap [--budget-lines N] [--apply]")
    if rc is not None:
        return rc
    budget = INDEX_BUDGET_LINES
    if "--budget-lines" in rest:
        j = rest.index("--budget-lines")
        try:
            budget = int(rest[j + 1])
        except (ValueError, IndexError):
            print("helm index cap: --budget-lines needs an integer", file=sys.stderr)
            return 2
    apply = "--apply" in rest
    r = index_cap(budget_lines=budget, apply=apply, project=project)
    if not r["exists"]:
        print("helm index cap: no MEMORY.md at " + r["path"])
        return 0
    print("helm index cap: MEMORY.md %d lines / %dB (budget %d lines, %d over); "
          "%d demotable (typed jit-resolvable — lossless)"
          % (r["lines"], r["bytes"], r["budget"], r["over"], r["demotable"]))
    for line, backing in r["plan"][:8]:
        print("  - %s  [%s stays live via inject]" % (line.strip()[:80], backing))
    if len(r["plan"]) > 8:
        print("  ... %d more" % (len(r["plan"]) - 8))
    if not r["over"]:
        print("helm index cap: under budget — nothing to demote")
        return 0
    if not r["plan"]:
        print("helm index cap: over budget but NOTHING safely demotable — the "
              "overflow is always-class or untyped lines; run `helm drain` "
              "(v2 upgrade) first so more lines become jit-resolvable")
        return 0
    if not apply:
        print("helm index cap: DRY-RUN (MEMORY.md untouched). Re-run with --apply.")
    elif r["demoted"]:
        print("helm index cap: DEMOTED %d line%s (net + receipt at %s); MEMORY.md "
              "now %d lines" % (r["demoted"], "s"[:r["demoted"] != 1],
                               r.get("net", "-"), r.get("kept_lines", 0)))
    return 0


# ---------------------------------------------------------------------------
# add guard — supersede-not-duplicate (the drain conflict law at add time)
# ---------------------------------------------------------------------------

DUP_OVERLAP = 0.8  # near-identical statement threshold (token-set Jaccard)


def _tokens(s):
    return set(re.split(r"[^a-z0-9]+", (s or "").lower())) - {""}


def _near_dup(etype, eid, statement, project=None):
    """The nearest LIVE same-type entry (excluding eid) whose statement's
    normalized token-set overlap (Jaccard) >= DUP_OVERLAP -> (entry, overlap),
    else (None, 0). Similarity WARNS, never blocks — the operator may
    genuinely want both."""
    want, tok = _slug(str(eid)), _tokens(statement)
    best, best_ov = None, 0.0
    for e in load_all(project=project, types=(etype,)):
        if _slug(str(e["id"])) == want:
            continue
        other = _tokens(e.get("statement"))
        ov = len(tok & other) / len(tok | other) if tok and other else 0.0
        if ov >= DUP_OVERLAP and ov > best_ov:
            best, best_ov = e, ov
    return best, best_ov


def _fmt(e):
    # provisional (xrev-cleared) fires but stays visibly tagged everywhere it
    # renders — the CLI resolve/pinned view, alongside the inject line + web.
    pv = "[provisional] " if e.get("status") == STATUS_PROVISIONAL else ""
    t = e["type"]
    if t == "prior":
        tag = "PREMISE" if e["class"] == "certain" else "PRIOR"
        return pv + tag + " " + str(e["id"]) + " [" + ("%.2f" % e["confidence"]) + "]: " \
            + (e.get("statement") or "")
    if t == "heuristic":
        return pv + "HEURISTIC " + str(e["id"]) + ": " + (e.get("statement") or "")
    if t == "lexicon":
        kind = e.get("kind") or ""
        return pv + "TERM " + str(e["id"]) + ((" (" + kind + ")") if kind else "") + ": " \
            + (e.get("definition") or "")
    if t == "reference":
        url = e.get("url") or ""
        return pv + "REF " + str(e["id"]) + ": " + (e.get("statement") or "") \
            + ((" <" + url + ">") if url else "")
    return pv + str(e["id"]) + ": " + (e.get("statement") or "")
