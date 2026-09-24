"""helm store — the ONE JIT resolve lane.

Probe vocabulary, DF weighting, the word-boundary keyword-match law, and the
two emitters resolve_prompt (JIT, relevance-gated) + pinned (always-on lane).
Moved verbatim from the pre-split helm/store.py.
"""
import re

from .. import promptcensus
from ._common import (
    GENERIC_KEYWORDS, _HEURISTIC_GENERIC, _MIN_HEURISTIC_TOKEN, _JIT_TYPES,
    _recency,
)
from .load import load_all

# THE LONE-WORD BAR (task/2978). An entry whose ONLY match is one single-word
# probe fires only while that word reaches fewer than this share of recent
# turns (helm.promptcensus). MEASURED on the E2 gold: 61% of JIT fires came
# from one single-word probe and 7.8% of those were relevant, against 20.7% for
# phrase matches. The relevant lone words sat at a prompt fraction of at most
# 3.5% (dev split: unresponsive 0.0%, substring 0.0%, yesterday 3.5%), and
# most irrelevant ones well above it (please, fine, know, ready, report).
# Chosen on the dev split (0.035 already holds `yesterday`) and read on test,
# through the real resolver and census with this lane's other legs in place:
# on the consensus test fired set every relevant pair the base admits stays
# admitted, cap-4 precision moves from 0.14 to 0.28 and AUC from 0.60 to 0.77.
# On the 1,284-turn exact-join replay this law alone accounts for 107 of the
# lane's 131 B/turn saving. A phrase match and a match of two or more probes
# are NOT held: co-occurring common words were the only match of several
# relevant pairs (land + merge + receipt), so only the LONE word is the noise.
_LONE_COMMON = 0.04


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
    present and future cap by construction (cross-family gate, 2026-07-23).

    LEXICON FOLD (canon controlled-language, Lane 1) — a lexicon entry's probe
    set is extended with its `alias_triggers`: {term} u keywords u
    alias_triggers. This is the DF-split fix (docs/CANON_CONTROLLED_LANGUAGE.md
    §2.2). A concept's synonyms carry their probe forms on the ONE canonical
    entry rather than in competing lex-*.md files, so every alias word fires
    THIS document and the df denominator counts the concept once — not once per
    synonym, which halved each entry's 1/df weight (CD's ~5 keyword re-tunes in
    one session). The stub alias entries whose triggers these represent are kept
    out of the candidate set by _jit_candidates, so the fold consolidates weight
    instead of re-splitting it."""
    own = _own_probes(e)
    have = {p for p, _g in own}
    return own | {(p, g) for p, g in _gate_probes(e) if p not in have}


# A keyword cell spelled `route:<id>` is a ROUTE, never a word probe (trigger
# design lane 1): it declares the MOMENT the entry answers, a route id of
# helm.moments.ROUTES, and inject delivers the entry when that moment's
# detector fires, through routed(). It never matches text and never counts
# in any df. This generalizes task/2978's `notice:<kind>`, which named a
# fixed notice kind; that spelling reads as `route:arrival.<kind>` (a Monitor
# expiry is the arrival route arrival.monitor-expired), and `helm store
# doctor` lists every cell still spelled the old way with its re-key command.
ROUTE_PREFIX = "route:"
NOTICE_PREFIX = "notice:"
_ROUTE_CELL = (ROUTE_PREFIX, NOTICE_PREFIX)


def route_cell(cell):
    """The route id a keyword cell declares, or None for a word probe."""
    c = str(cell or "").strip().lower()
    if c.startswith(ROUTE_PREFIX):
        return c[len(ROUTE_PREFIX):] or None
    if c.startswith(NOTICE_PREFIX):
        return ("arrival." + c[len(NOTICE_PREFIX):]) if c[len(NOTICE_PREFIX):] \
            else None
    return None


def _cells(raw):
    """The lowercased word-probe cells of a keyword CSV: route cells out."""
    return [c for c in (k.strip().lower() for k in str(raw or "").split(","))
            if c and not c.startswith(_ROUTE_CELL)]


def routes(e):
    """The route ids the entry declares (`route:<id>` cells, and the legacy
    `notice:<kind>` as `arrival.<kind>`)."""
    return {r for r in (route_cell(k) for k in str(e.get("keywords") or "")
                        .split(",")) if r}


def legacy_route_cells(e):
    """The entry's cells still spelled `notice:<kind>` (doctor's re-key list)."""
    return [k.strip() for k in str(e.get("keywords") or "").split(",")
            if k.strip().lower().startswith(NOTICE_PREFIX)]


def routed(entries, route_ids):
    """The JIT candidates that declare any of `route_ids`, in the order the
    lane ranks ties (confidence, then recency): the deterministic route from a
    detected moment to the rule about it (task/2978, generalized). A caller
    hands in the SEAT's entry list, so the route is scope-fenced exactly as
    the keyword lane is. No route, or no declaring entry, is an empty route."""
    want = set(route_ids or ())
    if not want:
        return []
    hit = [e for e in _jit_candidates(entries) if routes(e) & want]
    hit.sort(key=lambda e: (e["confidence"], _recency(e)), reverse=True)
    return hit


def _own_probes(e):
    """The probes the entry itself carries — id + keywords (+ lexicon alias
    triggers). The DF denominator counts THESE and only these, so linking a
    gate never dilutes anyone's weight (see _df_map). A route cell
    (route_cell) is not a probe."""
    generic = _HEURISTIC_GENERIC if e["type"] == "heuristic" else GENERIC_KEYWORDS
    kws = {(k, k in generic) for k in _cells(e.get("keywords"))}
    if e.get("type") == "capability":
        return kws
    if e.get("type") == "lexicon":
        kws |= {(t.strip().lower(), t.strip().lower() in generic)
                for t in (e.get("alias_triggers") or "").split(",") if t.strip()}
    return {(str(e["id"]).lower(), False)} | kws


def _gate_probes(e):
    """THE GATE FOLD (task/1346): the probes the entry BORROWS from the entries
    it gates / is gated by — `gate_probes`, derived at load by
    load.link_gates. A rule's vocabulary pulls its gate as strongly as the
    rule, and vice versa: the specimen gate ("two gates must pass before a PR
    is considered") was keyed on upstream/pr/oss and the Orca ruling on
    orca/fork/rebuild, and "upstream PR to orca" reached one of them. Borrowed
    probes score at the OWNER's 1/df (they are the same words), never the
    generic flag of the borrower's type — a word is generic by its own type's
    list, which is the owner's."""
    generic = _HEURISTIC_GENERIC if e["type"] == "heuristic" else GENERIC_KEYWORDS
    return {(k, k in generic) for k in _cells(e.get("gate_probes"))}


def _jit_candidates(entries):
    """The JIT-resolvable slice of a load_all() list — the uniform post-filter
    (always/dormant/episodic out) so a raw caller-supplied list needs no
    pre-shaping. Shared by resolve_prompt and inject --explain.

    LEXICON ALIAS STUB EXCLUSION (canon controlled-language, Lane 1) — a lexicon
    entry carrying a `canonical:` back-pointer is a synonym STUB kept only so a
    `get <synonym>` redirects rather than 404s; its triggers are already folded
    into the canonical entry's probe set (_probes). It must NOT compete as its
    own candidate: if it did, it would re-add its shared probes to the df
    denominator and re-split the exact weight the fold just consolidated — CD's
    DF-split, back again. The stub stays in load_all() (so `get` finds it); it
    is only barred from the resolve/df candidate set here."""
    return [e for e in entries if e["type"] in _JIT_TYPES
            and e.get("load_class") not in ("always", "dormant")
            and not (e["type"] == "lexicon"
                     and str(e.get("canonical") or "").strip())]


def _df_map(entries):
    """probe -> document frequency over the candidate set: one pass over the
    in-memory list, computed fresh per resolve call, never persisted. A probe
    carried by MANY entries' keywords is a weak signal; a rare one is strong —
    a matched probe scores 1/df."""
    df = {}
    for e in entries:
        for p in {p for p, _g in _own_probes(e)}:
            df[p] = df.get(p, 0) + 1
    # A BORROWED PROBE IS NOT A SECOND DOCUMENT. Counting the gate fold here
    # would halve every rare probe a linked pair shares — the DF-split the
    # lexicon fold already cured once — so the fold only needs a denominator
    # where the owner sits OUTSIDE the candidate set (a pinned rule lending
    # words to a JIT gate): one document, the owner.
    for e in entries:
        for p in {p for p, _g in _gate_probes(e)}:
            df.setdefault(p, 1)
    return df


_INFLECT_MIN = 4
_INFLECT = r"(?:e?s|ed|ing)?"


def _probe_re(p):
    """The word-boundary pattern for one probe, INFLECTION-TOLERANT on the right
    edge for probes of >= _INFLECT_MIN alphabetic characters.

    WHY: word-boundary matching is the right law — it is what stops the probe
    `cap` from firing on `capability` — but applied to the BARE stem it also
    stops `await` from firing on `awaiting` and `pane` from `panes`, which are
    the SAME concept, not a different one. The failure is silent: the entry
    simply never resolves, and nothing reports a near-miss.

    The evidence that this was already hurting is IN the store's own keyword
    vocabulary. Authors had been hand-padding it with morphological variants to
    work around the exact matcher — `account` AND `accounts`, `adapt` AND
    `adapted`, `address` AND `addressed` AND `addressing` all sit in the live
    lexicon as separate probes. That padding is a workaround, and an incomplete
    one, because it only covers the inflections whoever wrote that entry
    happened to think of.

    Measured on the live store (1098 candidate entries, 4919 distinct probes)
    against a 6558-word corpus of real fleet chat: 26 probes newly fire, and
    every one is semantically correct (await/awaiting, invoke/invokes,
    respond/responds, subtree/subtrees). Small, but monotone — this only ADDS
    matches, so no entry that resolved before stops resolving.

    THE >= 4 FLOOR IS THE SAFETY, and it is why the suffix set is a whitelist
    rather than a stemmer. Short stems form unrelated words under suffixing:
    `ban` + `d` is `band`, `car` + `d` is `card`, `hat` + `ed` is `hated`. So
    `d` is excluded entirely and three-letter probes stay exact. What remains
    (`s`/`es`/`ed`/`ing` on a >= 4-char stem) cannot reach a different concept
    by accident in this vocabulary.

    Non-alphabetic probes — every kebab-case id, every slug — are matched
    exactly. An id is not an English word and has no inflections."""
    core = r"(?<![a-z0-9])" + re.escape(p)
    if len(p) >= _INFLECT_MIN and p.isalpha():
        return core + _INFLECT + r"(?![a-z0-9])"
    return core + r"(?![a-z0-9])"


def _probe_hits(e, low):
    """The ONE keyword-match law: (hits, specific, matched) for entry `e`
    against lowercased turn text — id + csv keywords, word-boundary (see
    _probe_re for the inflection allowance), the specificity guard. Shared by
    resolve_prompt (scoring) and inject --explain (the why); `matched` is the
    sorted list of PROBES that hit, never the surface forms they matched,
    because the caller looks each one up in the DF map."""
    min_len = _MIN_HEURISTIC_TOKEN if e["type"] == "heuristic" else 1
    hits = 0
    specific = False
    matched = []
    for p, is_generic in _probes(e):
        # substring prefilter before the (expensive) word-boundary regex —
        # ~all probes miss on any given prompt, so only true hits pay the
        # regex. Measured 88ms -> 1.3ms per call on the live store, and this
        # runs on EVERY prompt in EVERY session fleet-wide. Still sound with
        # the inflection allowance: every accepted form has the probe as a
        # literal prefix, so a probe absent as a substring cannot match.
        if p and len(p) >= min_len and p in low and re.search(_probe_re(p), low):
            hits += 1
            matched.append(p)
            if not is_generic:
                specific = True
    matched.sort()
    return hits, specific, matched


def resolve_prompt(text, project=None, cap=4, entries=None, df=None,
                   census=None, held=None):
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
    instead of a fresh parse; None = load_all() as ever.

    THE LONE-WORD LAW (task/2978, _LONE_COMMON): an entry matched by ONE
    single-word probe is HELD when the prompt census (census=, else read
    here) measures that word in more than _LONE_COMMON of recent turns. An
    unmeasured census holds nothing. `held`, when a list, receives
    (entry, probe, fraction) for each held entry, so --explain can name what
    the law withheld instead of leaving it indistinguishable from no match."""
    low = (text or "").lower()
    if not low.strip():
        return []
    if entries is None:
        # THE RESOLVE DOOR ANSWERS FOR ITS PROJECT (task/2978). `helm store
        # resolve` from the helm checkout returned a lexicon recorded to
        # another project: inject's seat door was fenced (load_entries) and
        # this default load was not, so the CLI, the MCP resolver and coach
        # each described a lane no seat in that project receives. With a
        # project the fence applies; with none this stays the global lens
        # (the MCP server's "absent scope means GLOBAL").
        entries = load_all(project=project, include_dormant=False,
                           types=_JIT_TYPES, scope_fence=bool(project))
    cand = _jit_candidates(entries)
    # df= is the SAME map this would compute, supplied by a caller that already
    # needs it this turn (inject computes it once and threads it to both the
    # lane and the cooldown scorer). It is never a different weighting: a
    # caller that passes a df built over another corpus would re-rank the lane
    # silently, so the only supported producer is _df_map over THESE candidates.
    if df is None:
        df = _df_map(cand)
    if census is None:
        census = promptcensus.load()
    scored = []
    for e in cand:
        hits, specific, matched = _probe_hits(e, low)
        if not (hits and specific):
            continue
        if hits == 1 and " " not in matched[0] and census.measured:
            frac = census.frac(matched[0])
            if frac > _LONE_COMMON:
                if held is not None:
                    held.append((e, matched[0], frac))
                continue
        scored.append((e["confidence"] * sum(1.0 / df[p] for p in matched),
                       _recency(e),
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
