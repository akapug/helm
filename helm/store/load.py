"""helm store — roots, parsers, and the load lane.

The physical-root set, the per-type fail-open parsers, and load_all() with its
shadowing law + the status/dedup projections (entries/candidates/reviewable/
counts/_find). Moved verbatim from the pre-split helm/store.py.
"""
import copy
import os
import threading

from .. import home, pk
from ._common import (
    _slug, _coerce_conf, _is_pinned, _decode_lists, derive_class,
    derive_load_class, _scope_rank, _recency,
    STATUS_LIVE, STATUS_CANDIDATE, STATUS_PROVISIONAL, INJECTABLE_STATUSES,
    LEGACY_PREFIX, PRIOR_PREFIX, TYPE_SUBDIR, _SCAN_SUBDIRS, _TYPE_ORDER,
)


# ---------------------------------------------------------------------------
# roots — the injectable physical root set
# ---------------------------------------------------------------------------

def adopted_dir():
    """The live claude memory store helm adopts. HELM_ADOPTED_DIR (or legacy
    MELD_ADOPTED_DIR) overrides — that is how tests point it at a tmp dir."""
    return home.env("ADOPTED_DIR") or home.adopted_memory_dir()


# project -> (registry.json mtime, [claude memory dirs]); the mtime key keeps
# the registry load + dir stats off the per-turn hot path (20+ dirs otherwise).
_ADOPTED_PROJECT_CACHE = {}


def _project_adopted_dirs(project):
    """The claude per-project memory dirs helm ADOPTS as project-scoped store
    roots: the project's canonical cwd + observed worktree cwds, collapsed to
    the one project (like sessions). Resolved from the registry, mtime-cached.
    Fail-open: no registry / unknown project / no memory dir -> [] (existing
    single-store behavior — every hermetic test that never seeds a registry is
    unaffected)."""
    if not project:
        return []
    try:
        mtime = os.path.getmtime(home.registry_path())
    except OSError:
        return []
    cached = _ADOPTED_PROJECT_CACHE.get(project)
    if cached and cached[0] == mtime:
        return cached[1]
    dirs, seen = [], set()
    try:
        from .. import registry
        rec = registry.get(project)
    except Exception:
        rec = None
    if rec:
        for cwd in [rec.get("path")] + list(rec.get("cwds") or []):
            if not cwd:
                continue
            d = home.claude_memory_dir_for(cwd)
            if d not in seen and os.path.isdir(d):
                seen.add(d)
                dirs.append(d)
    _ADOPTED_PROJECT_CACHE[project] = (mtime, dirs)
    return dirs


def roots(project=None):
    """The ordered physical root set as (root, scope, dir) triples, WIDEST
    first — a later root SHADOWS an earlier one on a same-type same-slug
    collision, which is exactly the project > adopted-project > helm-global >
    adopted law. adopted-project is the project's OWN claude memory dir(s)
    (raw live store); it beats helm-global (project-specific raw over global)
    and is beaten by the authored ~/.helm/<name> layer. The same-slug shadow
    also dedups a byte-duplicated clone across two of a project's dirs."""
    out = [("adopted", "global", adopted_dir()),
           ("helm-global", "global", home.global_dir())]
    if project:
        for d in _project_adopted_dirs(project):
            out.append(("adopted-project", "project:" + project, d))
        out.append(("project", "project:" + project, home.project_dir(project)))
    return out


def _default_dir(etype, project=None):
    base = home.project_dir(project) if project else home.global_dir()
    return os.path.join(base, TYPE_SUBDIR[etype])


# ---------------------------------------------------------------------------
# parsers — one per type, all fail-open (a garbled file is skipped, never fatal)
# ---------------------------------------------------------------------------

_PRIOR_DEFAULTS = {
    "id": "", "statement": "", "confidence": "", "load_class": "",
    "gloss": "",
    "status": "live", "domain": "", "keywords": "", "stated_ts": "",
    "last_updated": "", "source": "", "pin": "",
    "evidence_log": "", "confidence_history": "",
    "policy_kind": "", "policy_members": [], "policy_reason": "",
    "supersedes": "", "replaced_by": "", "source_prior": "",
    "retired_ts": "", "retired_why": "",
    # xrev-clear receipt (candidate -> provisional): who attested the
    # cross-family review + when. Carries through parse/rewrite for display.
    "xrev_by": "", "xrev_ts": "",
    # attestation pointer (premise.py annotates; parse + rewrite carry through).
    # attest_record is the NATIVE hash-chain record (the primary proof);
    # attest_anchor* is the OPTIONAL dregg anchor; attest_turn/attest_receipt/
    # attest_supersedes_turn are legacy (read-only, pre-native attestations).
    "attest_payload": "", "attest_ts": "", "attest_by": "",
    "attest_record": "", "attest_chain_index": "",
    "attest_supersedes_record": "", "attest_anchor": "", "attest_anchor_turn": "",
    "attest_turn": "", "attest_receipt": "", "attest_supersedes_turn": "",
}

_LEX_DEFAULTS = {"term": "", "scope": "global", "definition": "", "kind": "",
                 "gloss": "",
                 "keywords": "", "domain": "", "source": "", "examples": [],
                 "updated_ts": "", "hits": "0", "status": "live",
                 "retired_ts": "", "retired_why": "",
                 "xrev_by": "", "xrev_ts": "",
                 # canon-as-controlled-language, Lane 1 (the synonym map). One
                 # canonical entry per concept; synonyms MAP to it rather than
                 # competing (docs/CANON_CONTROLLED_LANGUAGE.md §2). aliases =
                 # CSV of synonym TERMS a person/agent might say (linter +
                 # translator read this). alias_triggers = CSV of the
                 # word-boundary PROBE forms each alias contributes; the
                 # resolver folds these into THIS entry's probe set so a concept
                 # with synonyms keeps its full 1/df weight (resolve._probes).
                 # canonical = back-pointer on a STUB alias entry only (a
                 # synonym someone still `get`s by name); a stub does NOT compete
                 # as a resolve candidate (resolve._jit_candidates). Each field
                 # ships with a default ON PURPOSE: the parser only keeps keys
                 # that exist in the defaults, so a field lacking one is silently
                 # dropped on rewrite (the proven meme:true loss). New field ->
                 # new default, always.
                 "aliases": "", "alias_triggers": "", "canonical": ""}

_HEUR_DEFAULTS = {
    "id": "", "move": "", "statement": "", "trigger": "", "keywords": "",
    "gloss": "",
    "domain": "", "status": "live", "load_class": "jit",
    "stated_ts": "", "last_updated": "", "source": "",
    "supersedes": "", "replaced_by": "", "retired_ts": "", "retired_why": "",
    "xrev_by": "", "xrev_ts": "", "name": "", "description": "",
}

_REF_DEFAULTS = {
    "id": "", "statement": "", "summary": "", "url": "", "keywords": "",
    "gloss": "",
    "domain": "", "status": "live", "load_class": "", "source": "",
    "stated_ts": "", "last_updated": "", "name": "", "description": "",
    "supersedes": "", "replaced_by": "", "retired_ts": "", "retired_why": "",
    "xrev_by": "", "xrev_ts": "",
}

_EPISODIC_DEFAULTS = {"name": "", "description": "", "type": "", "load_class": ""}


def _parse_prior(path):
    e = pk.parse_simple_frontmatter(
        path, _PRIOR_DEFAULTS, list_keys=("policy_members",))
    if not (e and e["id"] and e["statement"]):
        return None
    _decode_lists(e)
    raw_conf = str(e.get("confidence") or "").strip()
    try:
        policy_confidence_valid = float(raw_conf) == 1.0
    except (TypeError, ValueError):
        policy_confidence_valid = False
    source = str(e.get("source") or "").strip()
    source_lower = source.casefold()
    policy_source_valid = bool(source) and "inferred" not in source_lower \
        and not source_lower.startswith("agent")
    conf = _coerce_conf(e.get("confidence"))
    pin = _is_pinned(e)
    e.update({"type": "prior", "confidence": conf, "class": derive_class(conf),
              "_policy_confidence_valid": policy_confidence_valid,
              "_policy_source_valid": policy_source_valid,
              "load_class": derive_load_class(e, conf, pin), "pinned": pin,
              "status": e.get("status") or STATUS_LIVE})
    return e


def _parse_lexicon(path):
    e = pk.parse_simple_frontmatter(path, _LEX_DEFAULTS, list_keys=("examples",))
    if not (e and e["term"] and e["definition"]):
        return None
    e["term_scope"] = e.pop("scope")  # authored scope; entry scope is root-derived
    status = e.get("status") or STATUS_LIVE  # a candidate lexicon is non-live
    kw = (e.get("keywords") or "").strip()
    # legacy mis-file: the add verb once filed the keywords CSV into kind: (a
    # real kind is a single taxonomy slug, never CSV) — those files must keep
    # resolving, unrewritten. The comma gate keeps taxonomy words ("phrase",
    # "bug-class") out of the probe vocabulary.
    kind = (e.get("kind") or "").strip()
    if not kw and "," in kind:
        kw = kind
        kind = "phrase"  # the CSV was never a taxonomy slug — any rewrite
        # (redefine/confirm/supersede) now persists the migrated shape instead
        # of carrying the mis-file forever
    e.update({"type": "lexicon", "id": e["term"], "statement": e["definition"],
              "confidence": 1.0, "class": "lexicon", "load_class": "jit",
              "status": status, "keywords": kw, "kind": kind,
              "domain": (e.get("domain") or "").strip(), "pinned": False,
              # synonym-map fields (Lane 1): normalized to stripped strings so
              # the resolver's probe fold + the stub-exclusion read a clean CSV
              "aliases": (e.get("aliases") or "").strip(),
              "alias_triggers": (e.get("alias_triggers") or "").strip(),
              "canonical": (e.get("canonical") or "").strip()})
    return e


def _parse_heuristic(path):
    e = pk.parse_simple_frontmatter(path, _HEUR_DEFAULTS)
    if not e or not e.get("id"):
        return None
    # move preferred, statement the priors-style alt key; description covers the
    # live store's hand-authored files that carry the move only in description.
    stmt = (e.get("move") or e.get("statement") or e.get("description") or "").strip()
    if not stmt:
        return None
    trig = (e.get("trigger") or e.get("keywords") or "").strip()
    e.update({"type": "heuristic", "move": stmt, "statement": stmt,
              "trigger": trig, "keywords": trig,
              "confidence": 1.0, "class": "heuristic", "load_class": "jit",
              "status": e.get("status") or STATUS_LIVE, "pinned": False})
    return e


def _parse_reference(path, name):
    e = pk.parse_simple_frontmatter(path, _REF_DEFAULTS)
    if not e:
        return None
    eid = (e.get("id") or "").strip()
    if not eid:
        nm = (e.get("name") or "").strip()
        eid = nm[4:] if nm.startswith("ref-") else (nm or name[4:-3])
    stmt = (e.get("statement") or e.get("summary") or e.get("description") or "").strip()
    if not (eid and stmt):
        return None
    lc = str(e.get("load_class") or "").strip().lower()
    e.update({"type": "reference", "id": eid, "statement": stmt,
              "confidence": 1.0, "class": "reference",
              "load_class": lc if lc in ("always", "jit", "dormant") else "jit",
              "status": e.get("status") or STATUS_LIVE, "pinned": False})
    return e


def _parse_episodic(path, name):
    e = pk.parse_simple_frontmatter(path, _EPISODIC_DEFAULTS)
    if not e:
        return None
    nm = (e.get("name") or "").strip()
    desc = (e.get("description") or "").strip()
    if not (nm or desc):
        return None  # no frontmatter identity (MEMORY.md-class) -> not an entry
    return {"type": "episodic", "id": nm or name[:-3], "statement": desc,
            "memory_type": (e.get("type") or "").strip(), "confidence": 1.0,
            "class": "episodic", "load_class": "dormant", "status": STATUS_LIVE,
            "keywords": "", "domain": "", "pinned": False}


def _parse_entry(path, name):
    """Filename-prefix dispatch (the drain/doctor classifier). reflex-*.md is
    reflex.py's store and MEMORY.md is the index — neither is an entry here.
    A typed-prefix file that fails its typed parse (the live store carries
    ~200 prem-/lex- named files that are really bulk memory: name+description
    only, no id/statement) FALLS BACK to episodic — visible in the inventory,
    never injected — where the legacy resolvers silently dropped it."""
    if name == "MEMORY.md" or name.startswith("reflex-"):
        return None
    if name.startswith((PRIOR_PREFIX, LEGACY_PREFIX)):
        e = _parse_prior(path)
        if e:
            e["_legacy"] = name.startswith(LEGACY_PREFIX)
            return e
    elif name.startswith("lex-"):
        e = _parse_lexicon(path)
        if e:
            return e
    elif name.startswith("heuristic-"):
        e = _parse_heuristic(path)
        if e:
            return e
    elif name.startswith("ref-"):
        e = _parse_reference(path, name)
        if e:
            return e
    return _parse_episodic(path, name)


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

def _entry_files(root, d):
    # adopted + adopted-project are flat claude memory dirs; helm roots nest
    dirs = [d] if root in ("adopted", "adopted-project") \
        else [os.path.join(d, s) for s in _SCAN_SUBDIRS]
    for sub in dirs:
        try:
            names = sorted(os.listdir(sub))
        except Exception:
            continue
        for n in names:
            p = os.path.join(sub, n)
            if n.endswith(".md") and os.path.isfile(p):
                yield n, p


def _load_root(root, scope, d):
    """One root -> {(type, slug): entry}, ALL statuses. Inside a root: prior-*
    always wins over a same-id legacy prem-* (dual-read migration law), and the
    narrowest authored lexicon scope wins per term (space: > project: > global).
    The live/retired filter is applied AFTER the cross-root merge (load_all) —
    filtering per-root let a retired narrow-scope entry vanish from the shadow
    map, resurrecting the stale wide-scope entry it shadowed."""
    out = {}
    for name, path in _entry_files(root, d):
        e = _parse_entry(path, name)
        if not e:
            continue
        e.update({"root": root, "scope": scope, "path": path})
        key = (e["type"], _slug(str(e["id"])))
        cur = out.get(key)
        if cur is not None:
            if e["type"] == "prior" and e.get("_legacy"):
                continue
            if e["type"] == "lexicon" and \
                    _scope_rank(e.get("term_scope")) >= _scope_rank(cur.get("term_scope")):
                continue
        out[key] = e
    return out


_TLS = threading.local()    # per-THREAD .depth + .cache; see _scope_state()


def _scope_state():
    """This thread's scope depth and cache.

    PER-THREAD, NOT PER-PROCESS, and that distinction is the whole correctness
    of this cache. helm web is a ThreadingHTTPServer: two overlapping requests
    run in two threads inside one interpreter. A module-level dict and a class
    attribute are shared by both, so thread A's snapshot answered thread B's
    read (@codex-2 measured it deterministically: a probe expecting
    {first, second} returned {first, first}), and A leaving its scope cleared
    the cache out from under B while B was still inside one.

    Nothing here is shared, so there is no lock and no contention: a thread can
    only ever see the reads it made itself, which is exactly what "scoped to
    ONE operation" was always supposed to mean."""
    tls = _TLS
    if not hasattr(tls, "depth"):
        tls.depth = 0
        tls.cache = {}
    return tls


def _detached(rows):
    """A caller-owned copy of a cached result. FULLY recursive, on purpose.

    `list(rows)` was NOT enough. It builds a new list around the SAME entry
    dicts, so a caller that mutates one entry mutates what the next read in the
    same scope sees (@codex-2's first probe: the following read saw 99). The
    docstring said "a copy: callers mutate their result" and the code delivered
    a copy of the wrong thing.

    ONE LEVEL WAS NOT ENOUGH EITHER, and I argued otherwise before @codex-2
    measured it. I claimed list values were the only mutable ones and that a
    deepcopy "would spend the win". Both halves were false against the real
    corpus: 892 of 1,308 entries carry confidence_history / evidence_log lists
    whose ELEMENTS are dicts, and mutating one of those nested dicts poisoned
    the next cached read. The cost I was protecting turned out to be noise —
    deepcopy measured 12.98ms per copy against a 5.75ms one-level copy, inside
    an 11.5s projection.

    So the rule is now the simple one with no bound to get wrong: a caller owns
    everything it is handed, all the way down. A stated-but-false isolation
    bound is worse than an honest cost."""
    return copy.deepcopy(list(rows))


class read_scope:
    """Memoize whole-store reads for the duration of ONE operation.

    MEASURED 2026-07-31. `helm lr list` / `/api/lr` took 25-41s to project 312
    land loops, and the owner's console card renders UNKNOWN because its budget
    is 12s — so the land pipeline was unreadable on the surface built to show
    it. The profile put 30.7 of 41 seconds in ONE place: `approval_tier` calls
    `load_certain_policy` per row, which calls `load_all`, which re-walks and
    re-parses the ENTIRE store. 179 rows produced 716 root loads and 244,335
    frontmatter parses of the same 1,308 files.

    THE CACHE IS SCOPED TO AN OPERATION, NEVER TO TIME, and that is the whole
    design. A time-keyed or mtime-keyed cache would serve stale policy — and a
    long-lived process serving boot-time state is the exact failure class this
    fix exists inside (helm web served 40h-old code today; the owner console
    served an 8-day-old render). An in-place edit does not bump a directory
    mtime, so an mtime key would be silently wrong. Entering this scope starts
    an empty cache and LEAVING IT DROPS THE CACHE, so no read can ever outlive
    the operation that asked for it. Outside a scope nothing is cached and
    behaviour is byte-identical to before.

    Re-entrant on purpose: nested scopes share the outermost cache and only the
    outermost clears, so a caller need not know whether its callee also scopes.
    Re-entrancy is per-thread too — a nested scope shares the cache of the
    outer scope ON ITS OWN THREAD and is invisible to every other thread.
    """

    def __enter__(self):
        _scope_state().depth += 1
        return self

    def __exit__(self, *exc):
        t = _scope_state()
        t.depth -= 1
        if t.depth <= 0:
            t.depth = 0
            t.cache.clear()
        return False


def load_all(project=None, include_retired=False, include_dormant=True, types=None):
    """Every entry across the root set, enriched (id/type/statement/confidence/
    class/load_class/status/keywords/domain/scope/root/path/pinned), deduped by
    (type, slug) with the shadowing law: project > helm-global > adopted.

    Inside a `read_scope()` the result is memoized for that operation; outside
    one it is recomputed every call, exactly as before."""
    t = _scope_state()
    if t.depth > 0:
        key = (project, bool(include_retired), bool(include_dormant),
               tuple(types) if isinstance(types, (list, tuple)) else types)
        hit = t.cache.get(key)
        if hit is not None:
            return _detached(hit)
        out = _load_all_uncached(project, include_retired, include_dormant,
                                 types)
        t.cache[key] = out
        # DETACH ON THE MISS PATH TOO. Returning `out` handed the caller the
        # very list of dicts now sitting in the cache, so the FIRST caller
        # could poison every later read in the scope — the miss path was the
        # more dangerous of the two and the one the old code left open.
        return _detached(out)
    return _load_all_uncached(project, include_retired, include_dormant, types)


def _load_all_uncached(project=None, include_retired=False,
                       include_dormant=True, types=None):
    if isinstance(types, str):
        types = (types,)
    merged = {}
    for root, scope, d in roots(project):
        merged.update(_load_root(root, scope, d))
    out = []
    for e in merged.values():
        # status filter AFTER the merge: a retired shadow-WINNER
        # drops out entirely — it must not un-bury the wider-scope entry it
        # shadowed (the record law survives scope precedence). live AND
        # provisional (xrev-cleared) both inject; candidate/retired stay out.
        if not include_retired and e.get("status") not in INJECTABLE_STATUSES:
            continue
        if not include_dormant and e.get("load_class") == "dormant":
            continue
        if types and e["type"] not in types:
            continue
        out.append(e)
    return out


def entries(project=None):
    """The web/status projection alias — same list as load_all()."""
    return load_all(project=project)


def candidates(project=None, types=None):
    """The safe-inferred-capture tier: every status:candidate entry (excluded
    from every injecting lane by the load_all live-filter — the hard law). The
    surface `list --candidates` and coach's dup-search read here."""
    return [e for e in load_all(project=project, include_retired=True, types=types)
            if e.get("status") == STATUS_CANDIDATE]


def reviewable(project=None, types=None):
    """The owner review queue: candidate (fires NOTHING) + provisional (fires
    WITH a [provisional] tag) — the two non-ratified states the owner browses,
    approves (confirm -> live), or rejects (reject -> retired) in the web review
    panel. xrev-clear graduates candidate -> provisional between them. Newest
    capture first so the freshest inference is reviewed first."""
    rows = [e for e in load_all(project=project, include_retired=True, types=types)
            if e.get("status") in (STATUS_CANDIDATE, STATUS_PROVISIONAL)]
    rows.sort(key=_recency, reverse=True)
    return rows


def counts(project=None):
    """{root: {type: n}} per physical root (post intra-root dedup, retired
    included) — the doctor/status inventory view."""
    out = {}
    for root, scope, d in roots(project):
        per = {}
        for e in _load_root(root, scope, d).values():
            per[e["type"]] = per.get(e["type"], 0) + 1
        out[root] = per
    return out


def _find(eid, project=None, types=None):
    """The one entry an id resolves to: the shadowing-law winner, typed-first
    (prior > heuristic > reference > lexicon > episodic on a slug tie)."""
    want = _slug(str(eid or ""))
    hits = {}
    for e in load_all(project=project, include_retired=True):
        if _slug(str(e["id"])) == want and (not types or e["type"] in types):
            hits[e["type"]] = e
    for t in _TYPE_ORDER:
        if t in hits:
            return hits[t]
    return None


def _policy_hits(kind, project=None):
    """Every live prior declaring this policy kind. THE ONE SCAN.

    `policy_declared` and `load_certain_policy` must agree about what "exists"
    means or a caller can be told both "there is no policy" and "the policy is
    unusable" about the same store — which is two owners of one fact, the
    defect this project keeps finding in new places. They share this."""
    kind = str(kind or "").strip().casefold()
    if not kind:
        return []
    # CASEFOLD BOTH SIDES. Writing canonically is not enough on its own — every
    # row already on disk keeps whatever case it was written with, and a
    # read-only fix would let new mixed-case rows keep arriving. @kimi's point:
    # one place is not enough, in either direction.
    return [e for e in load_all(project=project, include_retired=True,
                                types=("prior",))
            if e.get("status") == STATUS_LIVE
            and str(e.get("policy_kind") or "").strip().casefold() == kind]


def policy_declared(kind, project=None):
    """Does ANY live prior declare this policy kind at all?

    ABSENT AND UNREADABLE ARE DIFFERENT ANSWERS, and `load_certain_policy`
    returns a sentence for both — "no live policy declares kind X" reads the
    same to a caller as "policy X lacks a human source". A land gate built on
    that string must either parse prose or conflate two states that demand
    OPPOSITE behaviour: no policy means nothing to enforce, an unreadable one
    means we cannot say. This answers only the first question."""
    try:
        return bool(_policy_hits(kind, project=project))
    except Exception:                       # noqa: BLE001
        return True     # unreadable store: assume a policy EXISTS, so the
                        # caller lands in UNKNOWN rather than in "no tier"


def load_certain_policy(kind, project=None):
    """Return the one live certain prior declaring ``policy_kind == kind``.

    Policy is ordinary typed-store data under the existing root/shadow law, not
    a second registry. Any competing live declaration or incomplete declaration
    makes the policy unavailable rather than letting a caller guess authority.
    """
    kind = str(kind or "").strip()
    if not kind:
        return None, "policy kind is required"
    hits = _policy_hits(kind, project=project)
    if not hits:
        return None, "no live policy declares kind %s" % kind
    if len(hits) > 1:
        return None, "ambiguous policy kind %s: %s" % (
            kind, ", ".join(sorted(str(e["id"]) for e in hits)))
    policy = hits[0]
    if policy.get("class") != "certain" \
            or not policy.get("_policy_confidence_valid"):
        return None, "policy %s is not explicitly certain" % policy["id"]
    if not policy.get("_policy_source_valid"):
        return None, "policy %s lacks a human source" % policy["id"]
    members = policy.get("policy_members")
    if not isinstance(members, list) or not members:
        return None, "policy %s has no policy_members" % policy["id"]
    values = [kind, policy.get("policy_reason") or ""] + members
    if any(any(ord(c) < 32 or ord(c) == 127 for c in str(value))
           for value in values):
        return None, "policy %s contains control characters" % policy["id"]
    return policy, None
