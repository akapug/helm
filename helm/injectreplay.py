"""THE ROUTER REPLAY: run inject's own construction over recorded turns, and
reproduce the recorded numbers FIRST (trigger design 7.1).

"Before any change is judged, the replay must reproduce today's numbers. A
replay that cannot fail proves nothing." So this has two modes over one turn
file, and they are printed side by side:

  --recorded   the RECORDED basis: what the ledger rows joined to each turn
               say was delivered (rendered bytes, lane bytes, fired ids), and,
               with --gold, the E2 gold's own provenance (a pair delivered iff
               it fired or rode the reflex lane). This is the reproduction:
               the aggregation code is the same one the replay mode uses, so a
               number that does not match the published baseline is a defect
               in this instrument, not a finding.
  (default)    the REPLAY basis: `_gather_admitted` of the tree under test,
               turn by turn in time order, with the seen-state held in memory
               per session, the clock set to each turn's own timestamp, and no
               ledger row, latch or state written anywhere. The store it reads
               is TODAY's store, so replay and recorded differ by every store
               edit since the window as well as by the code; compare two trees
               on the replay basis, never a tree against the recorded one.

Two trees: run `python3 -P <tip>/helm/injectreplay.py --tree <base> TURNS ...`
to replay the BASE tree's construction with this file's harness (the harness
calls only `_gather_admitted`, whose signature both trees share).

A turn file is JSONL: {session, ts, project, prompt} plus, for --recorded,
the joined ledger fields (rendered_bytes, lane_bytes, fired) — the shape of
E2's turns.jsonl. The gold (E2 gold.jsonl + fable_labels.jsonl) is optional.
Prompt text never leaves this process: the outputs are ids, kinds and bytes.
"""
import collections
import copy
import json
import os
import statistics
import sys
import time


def load_turns(path, limit=None):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                t = json.loads(line)
            except ValueError:
                continue
            if isinstance(t, dict) and t.get("prompt") is not None:
                rows.append(t)
    rows.sort(key=lambda t: (str(t.get("ts")), str(t.get("session"))))
    return rows[:limit] if limit else rows


def _key(t):
    return "%s|%s" % (t.get("session"), t.get("ts"))


def recorded(turns):
    """Per-turn results off the recorded ledger fields."""
    out = []
    for t in turns:
        lanes = t.get("lane_bytes") or {}
        fired = t.get("fired") or {}
        out.append({"key": _key(t), "kind": None,
                    "bytes": int(t.get("rendered_bytes") or 0),
                    "jit_bytes": int(lanes.get("jit") or 0),
                    "pinned_bytes": int(lanes.get("pinned") or 0),
                    "reflex_bytes": int(lanes.get("reflex") or 0),
                    "jit": list(fired.get("jit") or ()),
                    "reflex": list(fired.get("reflex") or ()),
                    "pinned": list(fired.get("pinned") or ())})
    return out


class _Clock(object):
    """The time module with time() pinned to the turn being replayed."""

    def __init__(self, real):
        self._real = real
        self.now = real.time()

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(self._real, name)


def replay(turns):
    """Per-turn results off the tree's own `_gather_admitted`. Writes nothing:
    mutations are returned by construction and only the seen-state is kept,
    in memory. Sessions are renamed `replay-<id>` so no live counter, latch
    or epoch file of the real session is read."""
    os.environ["HELM_AGENT_HARNESS"] = "replay"
    os.environ.pop("HELM_CF_ENDPOINT", None)
    import importlib
    from helm import inject as _inject
    # the MODULE: the package re-exports a function of the same name
    _whisper = importlib.import_module("helm.inject._whisper")
    mem = {}
    real_load = _inject._seen_load

    def seen_load(session):
        return copy.deepcopy(mem[session]) if session in mem else real_load(session)

    clock = _Clock(time)
    _inject._seen_load = seen_load
    _inject._greeted_today = lambda: True
    _whisper.time = clock
    out = []
    try:
        for t in turns:
            sid = "replay-%s" % t.get("session")
            clock.now = _epoch(t.get("ts")) or time.time()
            try:
                sections, row, mutations, _cmp = _whisper._gather_admitted(
                    t["prompt"], project=t.get("project"), session=sid,
                    compare=None, cwd=None)
            except Exception as exc:          # noqa: BLE001 — count, go on
                out.append({"key": _key(t), "error": repr(exc)[:120]})
                continue
            if mutations.get("seen"):
                mem[sid] = copy.deepcopy(mutations["seen"][1])
            lanes = row["sample"]["lane_bytes"]
            fired = row.get("fired") or {}
            out.append({"key": _key(t), "kind": row.get("arrival"),
                        "bytes": row["sample"]["rendered_bytes"],
                        "jit_bytes": lanes["jit"],
                        "pinned_bytes": lanes["pinned"],
                        "reflex_bytes": lanes["reflex"],
                        "jit": list(fired.get("jit") or ()),
                        "reflex": list(fired.get("reflex") or ()),
                        "pinned": list(fired.get("pinned") or ()),
                        "fast_path": bool(row.get("fast_path")),
                        "over_cap": bool(row.get("over_cap"))})
    finally:
        _inject._seen_load = real_load
        _whisper.time = time
    return out


def _epoch(ts):
    try:
        import calendar
        return calendar.timegm(time.strptime(str(ts).rstrip("Z").split(".")[0],
                                             "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def aggregate(results):
    """The live-measurement basis (injmeasure): mean and median bytes/turn,
    JIT bytes/turn, JIT lines/turn, silent share; plus per-arrival means."""
    ok = [r for r in results if "error" not in r]
    if not ok:
        return {"turns": 0, "errors": len(results)}
    b = [r["bytes"] for r in ok]
    kinds = collections.defaultdict(list)
    for r in ok:
        kinds[r.get("kind") or "?"].append(r["bytes"])
    return {
        "turns": len(ok), "errors": len(results) - len(ok),
        "mean_bytes": round(statistics.mean(b), 1),
        "median_bytes": statistics.median(b),
        "jit_bytes": round(statistics.mean(r["jit_bytes"] for r in ok), 1),
        "pinned_bytes": round(statistics.mean(r["pinned_bytes"] for r in ok), 1),
        "reflex_bytes": round(statistics.mean(r["reflex_bytes"] for r in ok), 1),
        "jit_lines": round(statistics.mean(len(r["jit"]) for r in ok), 2),
        "silent_share": round(sum(1 for x in b if x == 0) / float(len(b)), 3),
        "fast_path": sum(1 for r in ok if r.get("fast_path")),
        "over_cap": sum(1 for r in ok if r.get("over_cap")),
        "by_arrival": {k: {"turns": len(v), "mean_bytes": round(
            statistics.mean(v), 1)} for k, v in sorted(kinds.items())},
    }


def gold(results, gold_path, labels_path, recorded_basis=False):
    """Recall and precision of delivered lines on the E2 gold (design 7.2).

    Positives are the first labeller's (strict); every strict positive is
    inside the second labeller's set, so they are the CONSENSUS positives
    too (E2 fable_agreement). A pair is delivered when its id fired on its
    turn (the JIT or reflex lane); on the recorded basis, when its gold
    provenance says it fired (src fired_*) or rode the reflex lane.
    JIT precision is over the delivered JIT pairs (the 181 of the published
    11.6% = 21/181), reflex precision over the delivered reflex pairs."""
    G = [json.loads(l) for l in open(gold_path, encoding="utf-8")]
    lenient = {}
    if labels_path:
        for l in open(labels_path, encoding="utf-8"):
            r = json.loads(l)
            lenient[r["pair_id"]] = r["label"] == "relevant"
    by = {r["key"]: r for r in results if "error" not in r}
    pos = sum(1 for g in G if g["gold"])
    hit = jit_n = jit_tp = rf_n = rf_tp = 0
    lost, missing = [], 0
    for g in G:
        if recorded_basis:
            delivered = g["src"].startswith("fired") or g["src"] == "reflex"
        else:
            r = by.get(g["turn"])
            if r is None:
                missing += 1
                continue
            delivered = g["id"] in r["jit"] or g["id"] in r["reflex"]
        is_reflex = g["src"] == "reflex"
        if delivered:
            if is_reflex:
                rf_n += 1
                rf_tp += g["gold"]
            else:
                jit_n += 1
                jit_tp += g["gold"]
            hit += g["gold"]
        elif g["gold"]:
            lost.append(g["id"])
    return {"positives": pos, "recall": "%d/%d" % (hit, pos),
            "jit_precision": "%d/%d = %.1f%%" % (jit_tp, jit_n, 100.0 * jit_tp / jit_n)
            if jit_n else None,
            "reflex_precision": "%d/%d" % (rf_tp, rf_n),
            "lost_positives": sorted(lost), "turns_missing": missing,
            "lenient_labels": len(lenient)}


def cmd(args):
    """helm inject --replay TURNS [--recorded] [--gold G --labels L]
    [--limit N] [--out FILE]"""
    def opt(name):
        return args[args.index(name) + 1] if name in args \
            and args.index(name) + 1 < len(args) else None
    path = opt("--replay")
    if not path or not os.path.exists(path):
        print("helm inject --replay: a turn file is required", file=sys.stderr)
        return 2
    limit = int(opt("--limit")) if opt("--limit") else None
    turns = load_turns(path, limit)
    basis = "recorded" if "--recorded" in args else "replay"
    results = recorded(turns) if basis == "recorded" else replay(turns)
    out = {"basis": basis, "turn_file": path, "aggregate": aggregate(results)}
    if opt("--gold"):
        out["gold"] = gold(results, opt("--gold"), opt("--labels"),
                           recorded_basis=basis == "recorded")
    if opt("--out"):
        with open(opt("--out"), "w", encoding="utf-8") as f:
            json.dump({"summary": out, "turns": results}, f)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--tree" in argv:
        i = argv.index("--tree")
        sys.path.insert(0, os.path.abspath(argv[i + 1]))
        del argv[i:i + 2]
    else:
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
    if argv and not argv[0].startswith("-"):
        argv = ["--replay"] + argv
    sys.exit(cmd(argv))
