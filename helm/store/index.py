"""helm store — MEMORY.md index cap + the add-guard/format leaf helpers.

The index_cap budget actuator + its CLI, plus the near-dup guard (_tokens/
_near_dup and the shared near_dup_warning wording) and the CLI row formatter
(_fmt). Moved verbatim from the pre-split helm/store.py.
"""
import os
import re
import sys
import time

from .. import eventledger, pk, projscope
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
    default; on --apply the file is locked, RE-READ fresh, and atomically
    rewritten so concurrent cap transactions cannot lose one another. -> receipt."""
    path = _memory_index_path()
    projscope.spend_or_raise("reading memory index")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return {"path": path, "exists": False, "lines": 0, "bytes": 0,
                "budget": budget_lines, "over": 0, "demotable": 0, "plan": [],
                "demoted": 0}
    projscope.spend_or_raise("reading memory index")
    lines = raw.splitlines()
    projscope.spend_or_raise("splitting memory index")
    total = len(lines)
    over = max(total - budget_lines, 0)
    if not over:
        return {"path": path, "exists": True, "lines": total,
                "bytes": len(raw), "budget": budget_lines, "over": 0,
                "demotable": 0, "plan": [], "demoted": 0}
    projscope.spend_or_raise("loading memory index entries")
    entries = {os.path.basename(e["path"]): e
               for e in load_all(project=project, include_retired=False)}
    projscope.spend_or_raise("classifying memory index lines")
    demotable = []
    for i, line in enumerate(lines):
        projscope.spend_or_raise("classifying memory index line")
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
    projscope.spend_or_raise("sorting memory index demotions")
    demotable.sort(key=lambda t: t[0])  # oldest last_updated first
    projscope.spend_or_raise("sorting memory index demotions")
    to_demote = demotable[:over] if over else []
    receipt = {"path": path, "exists": True, "lines": total, "bytes": len(raw),
               "budget": budget_lines, "over": over, "demotable": len(demotable),
               "plan": [(t[2], t[3]) for t in to_demote], "demoted": 0}
    if not apply or not to_demote:
        return receipt
    left = projscope.spend_or_raise("locking memory index")
    lock = eventledger.locked(path) if left is None \
        else eventledger.locked(path, timeout=left)
    with lock as held:
        projscope.spend_or_raise("locking memory index result")
        if not held:
            return receipt
        try:  # re-read under the shared cap lock before replacing the projection
            with open(path, encoding="utf-8") as f:
                cur_raw = f.read()
            projscope.spend_or_raise("refreshing memory index")
        except OSError:
            return receipt
        remaining = [t[2] for t in to_demote]
        kept, removed = [], []
        for ln in cur_raw.splitlines():
            projscope.spend_or_raise("rewriting memory index line")
            if ln in remaining:
                remaining.remove(ln)   # each planned line dropped exactly once
                removed.append(ln)
            else:
                kept.append(ln)
        projscope.spend_or_raise("rewriting memory index")
        if not removed:
            return receipt
        ts = pk.now_ts()
        net = os.path.join(
            adopted_dir(), "archive",
            "index-cap-%d-%d" % (os.getpid(), time.time_ns()))
        # PREPARED is durable before mutation but claims no completed demotion.
        # Once MEMORY changes, the admitted accounting tail marks it committed.
        projscope.spend_or_raise("applying memory index cap")
        os.makedirs(net, exist_ok=False)
        pk.atomic_write(
            os.path.join(net, "MEMORY-demoted.md"),
            "# demoted from MEMORY.md " + ts + " (content stays live via "
            "inject — this is the unlink record)\n\n" + "\n".join(removed) + "\n")
        receipt_path = os.path.join(net, "RECEIPT.json")
        transaction = {"ts": ts, "status": "prepared", "budget": budget_lines,
                       "demoted": len(removed), "removed": removed,
                       "kept_lines": len(kept)}
        pk.write_json(receipt_path, transaction)
        pk.atomic_write(
            path, "\n".join(kept) + ("\n" if cur_raw.endswith("\n") else ""))
        transaction["status"] = "committed"
        pk.write_json(receipt_path, transaction)
        receipt.update({"demoted": len(removed), "net": net,
                        "kept_lines": len(kept)})
        pk.event("index.cap", path,
                 "%d line%s demoted (budget %d, lossless — content stays "
                 "jit-resolvable)" %
                 (len(removed), "s"[:len(removed) != 1], budget_lines))
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


def dup_tokens(value, stopwords=()):
    """THE CANONICAL DUPLICATE TOKENIZER for the repo -> a set of words.

    ONE SPLITTER, IMPORTED, NOT COPIED. Every duplicate question in the tree
    is a token-set Jaccard against `DUP_OVERLAP`, and a second hand-rolled
    splitter beside this one is how two doors come to disagree about whether
    a pair is the same row: `helm task add`'s guard reads this function and
    this threshold rather than holding its own, so retuning the number here
    moves every door at once instead of leaving a silent twin behind.

    UNICODE WORDS, NOT `a-z0-9`. An ASCII-only split drops EVERY non-Latin
    character, which makes two byte-identical CJK titles tokenize to the empty
    set, score 0.0 against each other and read as distinct — a duplicate
    detector that cannot see an exact copy.
    `[^\\W_]+` keeps letters and digits in any script and splits on
    underscore, which the `\\w` class would otherwise weld into a word.

    A SCRIPT WITH NO SPACES YIELDS ONE TOKEN PER RUN, and that is the honest
    answer rather than a wrong one: an exact copy matches exactly (1.0), and
    two different runs share nothing (0.0), so this is conservative in the
    safe direction — it can miss a NEAR duplicate in such a script, and it
    can never invent one.

    IT NEVER RAISES. A caller resolving a value it did not write — a title
    typed at a door, a statement read off a ledger row somebody else's code
    wrote — must be able to ask this question about a malformed value and
    get an ANSWER. Anything that is not a string is not tokenizable, and the
    empty set is what says so; the caller reads that as UNKNOWN and decides.
    Raising here would put an AttributeError on a user's add command.

    `stopwords` is the caller's field-specific grammar list (titles are
    sentences; statements are not), applied after the split so the splitter
    itself stays one shape.
    """
    if not isinstance(value, str):
        return set()
    return set(re.findall(r"[^\W_]+", value.lower(), re.UNICODE)) - set(stopwords)


def _tokens(s):
    """The store's own statement tokens — `dup_tokens` with no stoplist."""
    return dup_tokens(s)


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


def near_dup_warning(dup, ov, ts, eid, project=None):
    """The statement-overlap WARNING lines every capture surface prints —
    the caller prefixes the first line with its own verb name ('helm store
    add: ' / 'helm premise: ') and prints the rest verbatim.

    ONE spelling, because store add and premise capture each carrying their
    own copy is the proven drift class (STALE_ON_REMINT; the RETEST
    constant): the wording that teaches the cure must be the same wherever
    the same situation arises. The cure command is `helm store supersede`
    on BOTH surfaces — the warn proceeds, so by the time the operator acts
    the new entry already exists and the store-level tombstone is the right
    verb (premise --supersede would capture a THIRD statement)."""
    pflag = (" --project " + project) if project else ""
    return [
        "WARNING possible duplicate of '%s' (%d%% statement overlap) — if "
        "it IS the same knowledge, supersede instead of accumulating:"
        % (dup["id"], round(ov * 100)),
        "  helm store supersede %s %s %s <reason...>%s"
        % (ts, dup["id"], eid, pflag),
    ]


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
