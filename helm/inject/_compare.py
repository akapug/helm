"""helm inject — the compare cluster: the pluggable comparison-resolver seam
(LocalBackend authority, CFCompareBackend stub, the compare-ledger, and the
owner's --compare-report read side).

Moved verbatim from the pre-split helm/inject.py.
"""
import json
import os
import time

from .. import home, pk
from ._common import CF_TIMEOUT, JIT_CAP, LEDGER_MAX
from ._entries import load_entries
from ._ledger import _append_jsonl, _read_jsonl


# ---------------------------------------------------------------------------
# pluggable comparison-resolver seam — the local JIT
# resolver is the AUTHORITY; a comparison backend runs in PARALLEL and its
# results are LOGGED/COMPARED, NEVER trusted as truth. A backend is anything with
# `name`, a mandatory `source` declaration (law 3: name your source), a
# configured() gate, and resolve(text, project) -> ranked entry-id list.
# ---------------------------------------------------------------------------

class LocalBackend:
    """The AUTHORITY: today's keyword JIT behind the one interface. resolve
    returns store.resolve_prompt's UNCAPPED ranked ids — the resolver's full
    opinion, which is what a comparison backend is measured against (the cap-4 is an
    injection-budget policy, not a finding). gather does NOT call this on the
    hot path (it reuses the ids it already computed, keeping the local lane
    byte-identical); it exists so the interface is real and the report can name
    the authority."""
    name = "local"
    source = "the typed store's keyword JIT resolver — authoritative; truth lives in files"

    def configured(self):
        return True

    def resolve(self, text, project=None, entries=None):
        from .. import store
        if entries is None:
            entries = load_entries(project)
        return [str(e["id"]) for e in store.resolve_prompt(
            text, project=project, cap=len(entries), entries=entries)]


class CFCompareBackend:
    """A THIN, documented STUB adapter for Cloudflare's agentic-memory.
    Zero-dep: stdlib urllib only. A COMPARISON BACKEND — a projection
    of the typed store queried in parallel and COMPARED, NEVER canonical (the
    future replica-write leg is the OTHER lane's, not this one's; no push verb ships).

    OFF unless HELM_CF_ENDPOINT is set (=> the default, zero cost — one env
    read). The wire shape (documented; a real endpoint is NEVER called in tests):
        POST  <HELM_CF_ENDPOINT>              # the agentic-memory query URL
        Authorization: Bearer <HELM_CF_TOKEN> # when HELM_CF_TOKEN is set
        Content-Type: application/json
        {"query": <prompt>, "project": <scope or null>, "top_k": <cap>}
      -> {"results": [{"id": "..."}, ...]}    # ranked; helm reads the ids only
    The call is synchronous + hard-timeboxed (CF_TIMEOUT) because a short-lived
    hook process cannot host a thread that outlives it. resolve() raises on any
    trouble by design; _compare_run centralises the fail-open (one attribution
    point) so beta churn degrades to local silently."""
    name = "cf"
    source = "projection of the typed store; truth lives in files (Cloudflare agentic-memory replica, NEVER canonical)"

    def configured(self):
        return bool(home.env("CF_ENDPOINT"))

    def resolve(self, text, project=None, entries=None, top_k=JIT_CAP):
        endpoint = home.env("CF_ENDPOINT")
        if not endpoint or not (text or "").strip():
            return []
        import urllib.request
        body = json.dumps({"query": text, "project": project,
                           "top_k": top_k}).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        token = home.env("CF_TOKEN")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=CF_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [str(r["id"]) for r in (data.get("results") or ())
                if isinstance(r, dict) and r.get("id") is not None]


LOCAL_BACKEND = LocalBackend()        # the authority, behind the interface
_COMPARE_BACKENDS = (CFCompareBackend(),)  # registered comparison backends, order = priority


def _active_compare():
    """The first CONFIGURED comparison backend, or None (=> comparison OFF).
    THE hot-path gate: an unconfigured fleet pays only configured()'s env
    read(s) — no import, no object build, no I/O, no store parse. Fail-open: a
    backend whose configured() raises is skipped, never a blocked turn."""
    for b in _COMPARE_BACKENDS:
        try:
            if b.configured():
                return b
        except Exception:
            continue
    return None


def _compare_ledger_path():
    return os.path.join(home.global_dir(), ".state", "compare-ledger.jsonl")


def _compare_diverge(local_ids, compare_ids):
    """(local_only, compare_only, agreed) sorted id lists — one turn's
    divergence. local_only = keyword JIT found, comparison backend missed;
    compare_only = comparison backend found, keyword JIT missed (the beta's
    candidate signal — owner judges signal vs noise); agreed = both."""
    ls, cs = set(local_ids), set(compare_ids)
    return sorted(ls - cs), sorted(cs - ls), sorted(ls & cs)


def _compare_run(backend, text, local_ids, project=None, session=None):
    """Run the comparison backend in PARALLEL to the AUTHORITATIVE local
    resolve, COMPARE, and append ONE divergence row to the compare-ledger.
    HARD: local_ids is already computed and is NOT touched here; a
    raising/slow/failing backend logs an {error: true} row and returns — never
    the local lane, never a blocked turn. Ids only, never prompt text (the
    ledger law)."""
    t0 = time.time()
    row = {"v": 1, "ts": pk.now_ts(), "project": project,
           "backend": getattr(backend, "name", "?")}
    if session:
        row["session"] = session
    try:
        compare_ids = [str(i) for i in backend.resolve(text, project=project)]
    except Exception:
        row["error"] = True
        row["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        _append_jsonl(_compare_ledger_path(), row, LEDGER_MAX)
        return
    local_only, compare_only, agreed = _compare_diverge(local_ids, compare_ids)
    row.update({"local_n": len(local_ids), "compare_n": len(compare_ids),
                "agreed": agreed, "local_only": local_only,
                "compare_only": compare_only,
                "elapsed_ms": round((time.time() - t0) * 1000, 1)})
    _append_jsonl(_compare_ledger_path(), row, LEDGER_MAX)


def _compare_rows():
    return _read_jsonl(_compare_ledger_path())


def compare_report(project=None):
    """Aggregate the compare-ledger into the owner's local-vs-comparison
    verdict, READ-ONLY. `off` is True when NO comparison backend is configured
    AND no rows exist yet (the honest zero-state). Divergence ids ride a count
    map so the verdict is concrete ('CF found N entries keywords missed')
    rather than a vibe."""
    rows = _compare_rows()
    active = _active_compare()
    agg = {"turns": 0, "errors": 0, "agreed": 0,
           "local_only": 0, "compare_only": 0}
    lo_ids, co_ids = {}, {}
    for r in rows:
        if r.get("error"):
            agg["errors"] += 1
            continue
        agg["turns"] += 1
        agg["agreed"] += len(r.get("agreed") or ())
        for i in (r.get("local_only") or ()):
            agg["local_only"] += 1
            lo_ids[i] = lo_ids.get(i, 0) + 1
        for i in (r.get("compare_only") or ()):
            agg["compare_only"] += 1
            co_ids[i] = co_ids.get(i, 0) + 1
    return {"configured": active is not None,
            "backend": getattr(active, "name", None),
            "rows": len(rows), "local_only_ids": lo_ids,
            "compare_only_ids": co_ids, **agg}


def _hot_ids(counts, n=5):
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def _compare_report(project=None):
    """--compare-report: compare_report() rendered for the owner. No ledger
    row, no state mutation, no live query — pure read over the accumulated
    ledger."""
    r = compare_report(project)
    if not r["configured"] and r["rows"] == 0:
        print("comparison backend off (set HELM_CF_ENDPOINT). No comparison "
              "backend is configured and the compare-ledger is empty —")
        print("the local keyword JIT resolver is the sole authority.")
        print("activate: export HELM_CF_ENDPOINT=<cloudflare agentic-memory query URL>"
              " [HELM_CF_TOKEN=<bearer>]")
        print("(the CF endpoint is the owner one-step; wire shape + env in "
              "docs/ENVIRONMENT.md).")
        return 0
    state = ("backend %s active" % r["backend"]) if r["configured"] \
        else "comparison backend currently OFF (past rows shown)"
    print("compare-report — %s, %d comparison turn%s (%d error%s degraded to local)" % (
        state, r["turns"], "s"[:r["turns"] != 1],
        r["errors"], "s"[:r["errors"] != 1]))
    print("  agreed       %6d  (both surfaced)" % r["agreed"])
    print("  local-only   %6d  (keyword JIT found, comparison backend missed)" % r["local_only"])
    print("  compare-only %6d  (comparison backend found, keyword JIT missed — "
          "owner judges signal vs noise)" % r["compare_only"])
    hot = _hot_ids(r["compare_only_ids"])
    if hot:
        print("  compare-only hot ids: " + ", ".join("%s x%d" % (i, n) for i, n in hot))
    if not r["configured"]:
        print("set HELM_CF_ENDPOINT to resume the comparison (docs/ENVIRONMENT.md).")
    return 0
