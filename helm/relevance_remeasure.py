"""helm relevance remeasure — the gate on the claim that the local head is as
good as the outside evaluator.

The claim that the local head ties the evaluator was measured on a small gold
split, so its interval is wide. Before any project that MAY leave the LAN runs
local-only BY CHOICE, the frozen head is re-scored on a larger HELD-OUT split
of the evaluator's own labels (the silver harvest) through the SAME service
that scores live turns, and a receipt records whether it passed. The receipt
is bound to the head version it measured: a new head needs a new receipt.
Residency-forced local scoring does not wait for this gate.

WHAT PASSES (every criterion is a flag and is written into the receipt):
  * at least --min-turns held-out turns were scored;
  * the lower end of the turn-grouped bootstrap 90% interval of the head's AUC
    against the evaluator's keep decisions (evaluator p >= its threshold) is
    at least --min-auc;
  * at the head's own frozen threshold, the head keeps at least --min-recall
    of the lines the evaluator keeps.

THE HELD-OUT SPLIT is a stable function of the turn key (`heldout`), so a later
refit can exclude exactly these turns. Turns named in any --exclude file (the
head's training items, the gold; comma-separated) never enter it.
"""
import hashlib
import json
import random
import sys
import time

from . import pk, relevance

DEFAULTS = {"share": 0.3, "min_turns": 1000, "min_auc": 0.85,
            "min_recall": 0.90, "evaluator_threshold": relevance.JEV_THRESHOLD,
            "boot": 200, "workers": 2}


def heldout(turn, share):
    """True for the stable `share` of turn keys that is held out."""
    h = int(hashlib.sha256(str(turn).encode("utf-8")).hexdigest()[:8], 16)
    return h / float(1 << 32) < share


def _jsonl(path):
    with open(path, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if isinstance(r, dict):
                yield r


def load_labels(path):
    """{pair: p} from either label shape: harvest rows {turn, p: {pair: p}}
    or flat rows {pair, p}."""
    out = {}
    for r in _jsonl(path):
        if isinstance(r.get("p"), dict):
            out.update({k: float(v) for k, v in r["p"].items()
                        if isinstance(v, (int, float))})
        elif isinstance(r.get("pair"), str) and isinstance(r.get("p"), (int, float)):
            out[r["pair"]] = float(r["p"])
    return out


def split(items_path, labels, exclude_paths, share):
    """{turn: {"text": str, "rows": [{id, text}]}} for the held-out turns
    that carry at least one labelled pair."""
    excluded = set()
    for p in exclude_paths:
        excluded.update(str(r.get("turn")) for r in _jsonl(p))
    turns = {}
    for r in _jsonl(items_path):
        turn, pair = str(r.get("turn")), r.get("pair")
        if pair not in labels or turn in excluded or not heldout(turn, share):
            continue
        t = turns.setdefault(turn, {"text": r.get("content") or "", "rows": []})
        t["rows"].append({"id": pair, "text": relevance.note(r.get("line") or "")})
    return turns


def _auc_pairs(pairs):
    pos = [h for h, e in pairs if e]
    neg = [h for h, e in pairs if not e]
    return relevance.auc(pos, neg)


def metrics(scored, labels, evaluator_threshold, head_threshold, boot, seed=0):
    """The gate's numbers from {turn: {pair: head p}}."""
    by_turn = {t: [(p, labels[k] >= evaluator_threshold) for k, p in ps.items()]
               for t, ps in scored.items() if ps}
    flat = [x for v in by_turn.values() for x in v]
    point = _auc_pairs(flat)
    rng = random.Random(seed)
    keys = sorted(by_turn)
    boots = []
    for _ in range(boot if keys else 0):
        sample = [x for k in (rng.choice(keys) for _ in keys) for x in by_turn[k]]
        a = _auc_pairs(sample)
        if a is not None:
            boots.append(a)
    boots.sort()
    lo = boots[int(0.05 * len(boots))] if boots else None
    hi = boots[min(len(boots) - 1, int(0.95 * len(boots)))] if boots else None
    kept_e = [(h, e) for h, e in flat if e]
    recall = sum(1 for h, _ in kept_e if head_threshold is not None and h >= head_threshold) \
        / len(kept_e) if kept_e else None
    keep = sum(1 for h, _ in flat if head_threshold is not None and h >= head_threshold) \
        / len(flat) if flat else None
    hs = [h for t, ps in scored.items() for h in ps.values()]
    es = [labels[k] for t, ps in scored.items() for k in ps]
    r = None
    if len(hs) > 2:
        mh, me = sum(hs) / len(hs), sum(es) / len(es)
        num = sum((a - mh) * (b - me) for a, b in zip(hs, es))
        den = (sum((a - mh) ** 2 for a in hs) * sum((b - me) ** 2 for b in es)) ** .5
        r = num / den if den else None
    rnd = lambda v: None if v is None else round(v, 4)
    return {"turns": len(by_turn), "pairs": len(flat),
            "evaluator_keeps": len(kept_e), "auc": rnd(point),
            "auc_ci90": [rnd(lo), rnd(hi)], "recall_of_evaluator_keeps": rnd(recall),
            "head_keep_share": rnd(keep), "pearson_r": rnd(r)}


def verdict(m, crit):
    """(pass, [reasons it did not pass])."""
    fails = []
    if m["turns"] < crit["min_turns"]:
        fails.append("%d held-out turns scored, fewer than %d" % (m["turns"], crit["min_turns"]))
    lo = (m.get("auc_ci90") or [None])[0]
    if lo is None or lo < crit["min_auc"]:
        fails.append("AUC 90%% lower bound %s is under %s" % (lo, crit["min_auc"]))
    rec = m.get("recall_of_evaluator_keeps")
    if rec is None or rec < crit["min_recall"]:
        fails.append("recall of evaluator keeps %s is under %s" % (rec, crit["min_recall"]))
    return not fails, fails


def run(items, labels_path, exclude=(), crit=None, limit=None, score=None,
        health=None, progress=None):
    """Score the held-out split through the live scorer service and judge it.
    `score(text, rows) -> ({id: p}, model, threshold)` and `health()` are the
    service seams (defaults: the configured endpoint)."""
    crit = dict(DEFAULTS, **(crit or {}))
    cfg = relevance.settings()
    score = score or (lambda text, rows: relevance.local_scores(
        text, [{"id": r["id"], "note": r["text"]} for r in rows], cfg))
    health = health or (lambda: relevance.local_health(cfg))
    head = (health() or {}).get("model")
    labels = load_labels(labels_path)
    turns = split(items, labels, exclude, crit["share"])
    keys = sorted(turns)[:limit] if limit else sorted(turns)
    scored, models, thr, errors = {}, set(), None, 0
    from concurrent.futures import ThreadPoolExecutor

    def one(k):
        return k, score(turns[k]["text"], turns[k]["rows"])
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, int(crit["workers"]))) as pool:
        futs = [pool.submit(one, k) for k in keys]
        for n, f in enumerate(futs, 1):
            try:
                k, (p, model, t) = f.result()
            except Exception:               # noqa: BLE001 — counted, never fatal
                errors += 1
                continue
            scored[k] = p
            models.add(model)
            thr = t if t is not None else thr
            if progress and n % 100 == 0:
                progress("%d/%d turns, %.0f s" % (n, len(keys), time.time() - t0))
    if len(models) > 1 or (models and head not in models):
        raise RuntimeError("the service changed heads mid-run: %s then %s"
                           % (head, sorted(models)))
    m = metrics(scored, labels, crit["evaluator_threshold"], thr, crit["boot"])
    ok, fails = verdict(m, crit)
    return {"head": head, "pass": ok, "fails": fails, "criteria": crit,
            "metrics": dict(m, errors=errors, head_threshold=thr),
            "inputs": {"items": items, "labels": labels_path,
                       "exclude": list(exclude), "held_out_turns": len(turns)},
            "ts": pk.now_ts(), "seconds": round(time.time() - t0, 1)}


USAGE = ("usage: helm relevance remeasure --items FILE --labels FILE "
         "[--exclude FILE[,FILE...]] [--share F] [--min-turns N] [--min-auc F] "
         "[--min-recall F] [--limit-turns N] [--workers N] [--boot N] [--apply] [--json]")


def cmd(args):
    from .cli import guard_tail
    opts, excl, flags, i = {}, [], set(), 0
    valued = ("--items", "--labels", "--share", "--min-turns", "--min-auc",
              "--min-recall", "--limit-turns", "--workers", "--boot")
    rc = guard_tail("helm relevance remeasure", args, flags=("--apply", "--json"),
                    valued=valued + ("--exclude",), usage=USAGE)
    if rc is not None:
        return rc
    while i < len(args):
        a = args[i]
        if a in ("--apply", "--json"):
            flags.add(a)
            i += 1
            continue
        if a not in valued + ("--exclude",) or i + 1 >= len(args):
            print(USAGE, file=sys.stderr)
            return 2
        if a == "--exclude":
            excl.extend(x for x in args[i + 1].split(",") if x)
        else:
            opts[a] = args[i + 1]
        i += 2
    if "--items" not in opts or "--labels" not in opts:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        crit = {k: t(opts["--" + k.replace("_", "-")]) for k, t in (
            ("share", float), ("min_turns", int), ("min_auc", float),
            ("min_recall", float), ("workers", int), ("boot", int))
            if "--" + k.replace("_", "-") in opts}
        limit = int(opts["--limit-turns"]) if "--limit-turns" in opts else None
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        rec = run(opts["--items"], opts["--labels"], excl, crit, limit,
                  progress=lambda s: print("helm relevance remeasure: " + s,
                                           file=sys.stderr, flush=True))
    except Exception as exc:                # noqa: BLE001 — the gate stays shut, loudly
        print("helm relevance remeasure: %s: %s — no receipt written"
              % (exc.__class__.__name__, exc), file=sys.stderr)
        return 1
    if limit:
        rec["pass"] = False
        rec["fails"].append("--limit-turns ran a sample, which never passes")
    if "--apply" in flags:
        pk.write_json(relevance.receipt_path(), rec)
    if "--json" in flags:
        print(json.dumps(rec, indent=1))
    else:
        m = rec["metrics"]
        print("helm relevance remeasure: head %s — %s" % (
            rec["head"], "PASS" if rec["pass"] else "NOT PASSED"))
        print("  %d turns, %d pairs, %d evaluator keeps; AUC %s (90%% %s..%s); "
              "recall of evaluator keeps %s at head threshold %s; head keeps %s; r %s"
              % (m["turns"], m["pairs"], m["evaluator_keeps"], m["auc"],
                 m["auc_ci90"][0], m["auc_ci90"][1], m["recall_of_evaluator_keeps"],
                 m["head_threshold"], m["head_keep_share"], m["pearson_r"]))
        for f in rec["fails"]:
            print("  not passed: " + f)
        print("  receipt %s" % ("written to " + relevance.receipt_path()
                                if "--apply" in flags else "not written (dry run; --apply)"))
    return 0 if rec["pass"] else 3
