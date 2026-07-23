"""helm store — the ONE JIT resolve lane.

Probe vocabulary, DF weighting, the word-boundary keyword-match law, and the
two emitters resolve_prompt (JIT, relevance-gated) + pinned (always-on lane).
Moved verbatim from the pre-split helm/store.py.
"""
import re

from ._common import (
    GENERIC_KEYWORDS, _HEURISTIC_GENERIC, _MIN_HEURISTIC_TOKEN, _JIT_TYPES,
    _recency,
)
from .load import load_all


def _probes(e):
    """The entry's probe set — id + csv keywords, lowercased, deduped — as
    {(probe, is_generic)}. The ONE probe vocabulary, shared by matching
    (_probe_hits) and DF weighting (_df_map).

    EXCEPTION — capabilities probe on CURATED KEYWORDS ONLY, never the bare id.
    A store id is a long kebab-case slug that word-boundary-matches ~never, but
    a capability id is a short verb-slug ('pending','meld','store','dispatch')
    that IS a common dev word — as a specific probe it broad-fires the whole
    lever on any turn containing it ('the PR is pending', 'consensus root'),
    the exact relevance regression the index exists to avoid. The curated
    keywords carry every intended trigger (the verb word, when wanted, is
    listed there), so dropping the id probe loses nothing and protects every
    present and future cap by construction (cross-family gate, 2026-07-23)."""
    generic = _HEURISTIC_GENERIC if e["type"] == "heuristic" else GENERIC_KEYWORDS
    kws = {(k.strip().lower(), k.strip().lower() in generic)
           for k in (e.get("keywords") or "").split(",") if k.strip()}
    if e.get("type") == "capability":
        return kws
    return {(str(e["id"]).lower(), False)} | kws


def _jit_candidates(entries):
    """The JIT-resolvable slice of a load_all() list — the uniform post-filter
    (always/dormant/episodic out) so a raw caller-supplied list needs no
    pre-shaping. Shared by resolve_prompt and inject --explain."""
    return [e for e in entries if e["type"] in _JIT_TYPES
            and e.get("load_class") not in ("always", "dormant")]


def _df_map(entries):
    """probe -> document frequency over the candidate set: one pass over the
    in-memory list, computed fresh per resolve call, never persisted. A probe
    carried by MANY entries' keywords is a weak signal; a rare one is strong —
    a matched probe scores 1/df."""
    df = {}
    for e in entries:
        for p in {p for p, _g in _probes(e)}:
            df[p] = df.get(p, 0) + 1
    return df


def _probe_hits(e, low):
    """The ONE keyword-match law: (hits, specific, matched) for entry `e`
    against lowercased turn text — id + csv keywords, word-boundary, the
    specificity guard. Shared by resolve_prompt (scoring) and inject --explain
    (the why); `matched` is the sorted probe list that actually hit."""
    min_len = _MIN_HEURISTIC_TOKEN if e["type"] == "heuristic" else 1
    hits = 0
    specific = False
    matched = []
    for p, is_generic in _probes(e):
        # substring prefilter before the (expensive) word-boundary regex —
        # ~all probes miss on any given prompt, so only true hits pay the
        # regex. Measured 88ms -> 1.3ms per call on the live store, and this
        # runs on EVERY prompt in EVERY session fleet-wide.
        if p and len(p) >= min_len and p in low and \
                re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", low):
            hits += 1
            matched.append(p)
            if not is_generic:
                specific = True
    matched.sort()
    return hits, specific, matched


def resolve_prompt(text, project=None, cap=4, entries=None):
    """JIT: live, non-dormant entries whose id/keywords (lexicon: term +
    keywords) word-boundary-match the turn text, DF-WEIGHTED: each matched probe
    contributes 1/df (df = how many candidate entries carry that probe, one
    in-memory pass per call), summed then confidence-weighted — one rare
    keyword outranks a pile of shared ones, so the cap-4 winners are earned,
    not (hits, id-desc) noise. The specificity guard is unchanged: at least
    one non-generic probe must hit. Ties break most-recently-updated first,
    then stable load order — NEVER the id (with ~400 same-day priors the old
    id tiebreak made the winners reverse-alphabetical). Pinned (load_class
    always) entries are NOT returned here — they are emitted unconditionally
    via pinned(). Salience law: EMPTY on no match. entries= feeds the lane
    from a caller-supplied load_all() list (the inject parsed-entry cache)
    instead of a fresh parse; None = load_all() as ever."""
    low = (text or "").lower()
    if not low.strip():
        return []
    if entries is None:
        entries = load_all(project=project, include_dormant=False, types=_JIT_TYPES)
    cand = _jit_candidates(entries)
    df = _df_map(cand)
    scored = []
    for e in cand:
        hits, specific, matched = _probe_hits(e, low)
        if hits and specific:
            scored.append((e["confidence"] * sum(1.0 / df[p] for p in matched),
                           str(e.get("last_updated") or e.get("updated_ts") or ""),
                           e))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)  # stable: never id order
    try:
        n = int(cap)
    except (TypeError, ValueError):
        n = 4
    return [t[2] for t in scored[:max(n, 0)]]


def pinned(project=None, entries=None):
    """load_class=always entries — returned ALWAYS (no relevance gate), in the
    ONE deterministic budget-walk order the injecting consumer truncates:
    confidence desc, then recency desc (_recency over last_updated/updated_ts/
    stated_ts), then id asc. The old (-confidence, id) key made an
    all-conf-1.0 lane effectively ALPHABETICAL — 8 of 11 live always-entries
    sat past the byte-budget fold forever (the pinned-starvation class); now
    the winners are earned by freshness and the tie-break is still total.
    entries= as in resolve_prompt: a supplied load_all() list skips the parse."""
    if entries is None:
        entries = load_all(project=project)
    out = [e for e in entries if e.get("load_class") == "always"]
    out.sort(key=lambda e: (-e["confidence"], -_recency(e), str(e["id"])))
    return out
