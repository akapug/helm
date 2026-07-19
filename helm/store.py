#!/usr/bin/env python3
"""helm store — the ONE typed personal-knowledge store resolver.

The unification of the four near-clone mc resolvers (priors.py + lexicon.py +
heuristics_store.py + the episodic memory reader) behind one loader, one JIT
resolver, one lifecycle. The store spans several PHYSICAL ROOTS but reads as
one logical store; every entry records where it lives:

  adopted      ~/.claude/projects/<home-slug>/memory — the LIVE store the
               user's agents write today. helm adopts it IN PLACE (same files,
               one more resolver); HELM_ADOPTED_DIR overrides it for tests.
  helm-global  ~/.helm/_global/{premises,heuristics,lexicon,references,priors}
  project      ~/.helm/<name>/{premises,heuristics,lexicon,references}

Scope precedence on a same-type same-slug collision: project > helm-global >
adopted (narrowest wins — the shadowing law).

Entry TYPES (filename prefix is the classifier, matching drain/doctor):
  prior      prior-*.md (+ legacy prem-*.md dual-read; prior-* wins on the
             same id). class DERIVED from confidence: 1.0 = certain (premise),
             < 1.0 = prior (belief). The mc priors.py laws are ported intact:
             confidence coercion, DORMANT_BELOW, BELIEF_CLAMP, pin handling,
             the live/retired/delete_eligible lifecycle, one-line-JSON
             evidence_log/confidence_history, the certain-prior
             contradiction-logged-not-applied rule, the tombstone law.
  lexicon    lex-*.md — term/definition; matches when the TERM appears in the
             turn text (word-boundary).
  heuristic  heuristic-*.md — a MOVE you apply; confidence 1.0 by construction
             and load_class always jit (the only question is whether its
             trigger-pattern fires THIS turn).
  reference  ref-*.md — harvested external reference material (id, summary,
             url, keywords, domain; load_class default jit).
  episodic   any other *.md carrying frontmatter identity (name/description).
             Never injected: load_class dormant. reflex-*.md and MEMORY.md
             belong to other owners and are skipped.

Writers are BYTE-SHAPE-COMPATIBLE with the mc writers (same frontmatter key
order) — the live mc hooks keep reading/writing these same files. New writes
NEVER target the adopted root unless explicitly pointed there; lifecycle
updates (evidence/supersede/retire) write back IN PLACE wherever the entry
lives — including adopted; that is the adoption contract.

Import-safe, side-effect-free, stdlib-only.
"""
import json
import os
import re
import sys

from . import home, pk

_slug = pk.slug

# Confidence machinery (ported from mc priors.py — same constants, same laws).
CERTAIN = 1.0            # confidence == CERTAIN -> class certain (a premise)
DORMANT_BELOW = 0.4      # confidence < this -> load_class dormant (never injects)
ACT_AT = 0.85            # confidence >= this -> auto-act tier
BELIEF_CLAMP = (0.05, 0.99)  # a belief NEVER auto-reaches 1.0 (human-only rail)

STATUS_LIVE = "live"
STATUS_RETIRED = "retired"
STATUS_DELETE_ELIGIBLE = "delete_eligible"

# PINNED priors inject EVERY turn (load_class always). Pin = a `pin: true`
# flag OR membership here (belt-and-suspenders, same tuple as mc so the live
# store's pins survive the adoption).
PINNED_SLUGS = ("human-is-context-free", "build-the-full-depgraph", "drift-is-the-enemy")

LEGACY_PREFIX = "prem-"
PRIOR_PREFIX = "prior-"

# SPECIFICITY guard (anti-wallpaper, ported from mc priors.py): a JIT entry
# must match the turn via at least one SPECIFIC (non-generic) keyword or its
# id — a generic-only match would wallpaper nearly every turn.
GENERIC_KEYWORDS = frozenset({
    "build", "code", "fix", "test", "work", "task", "run", "make", "do", "the", "a", "an",
    "is", "it", "this", "that", "agent", "buildr", "change", "file", "add", "use", "new",
    "now", "get", "set", "go", "and", "or", "to", "of", "in", "on", "for", "with",
})
# Heuristics carry the mc heuristics_store extras on top of the shared set.
_HEURISTIC_GENERIC = GENERIC_KEYWORDS | {"move", "apply", "domain", "heuristic", "strategy"}
_MIN_HEURISTIC_TOKEN = 3  # mc heuristics_store._MIN_TOKEN

# Where each type's NEW writes land inside a helm root (the adopted root stays
# flat and is only ever an explicit target).
TYPE_SUBDIR = {"prior": "premises", "lexicon": "lexicon",
               "heuristic": "heuristics", "reference": "references"}
# Subdirs scanned per helm root ("priors" = the pre-declared _global prior-art
# corpus category; harmless no-op where absent).
_SCAN_SUBDIRS = ("premises", "heuristics", "lexicon", "references", "priors")

_TYPE_ORDER = ("prior", "heuristic", "reference", "lexicon", "episodic")


# ---------------------------------------------------------------------------
# roots — the injectable physical root set
# ---------------------------------------------------------------------------

def adopted_dir():
    """The live claude memory store helm adopts. HELM_ADOPTED_DIR (or legacy
    MELD_ADOPTED_DIR) overrides — that is how tests point it at a tmp dir."""
    return home.env("ADOPTED_DIR") or home.adopted_memory_dir()


def roots(project=None):
    """The ordered physical root set as (root, scope, dir) triples, WIDEST
    first — a later root SHADOWS an earlier one on a same-type same-slug
    collision, which is exactly the project > helm-global > adopted law."""
    out = [("adopted", "global", adopted_dir()),
           ("helm-global", "global", home.global_dir())]
    if project:
        out.append(("project", "project:" + project, home.project_dir(project)))
    return out


def _default_dir(etype, project=None):
    base = home.project_dir(project) if project else home.global_dir()
    return os.path.join(base, TYPE_SUBDIR[etype])


# ---------------------------------------------------------------------------
# derivation (class + load_class are DERIVED, never authored — mc priors law)
# ---------------------------------------------------------------------------

def _coerce_conf(raw):
    """A missing/blank/garbled confidence reads as 1.0 (a legacy premise IS a
    certain-prior)."""
    s = str(raw or "").strip()
    if s == "":
        return CERTAIN
    try:
        v = float(s)
    except ValueError:
        return CERTAIN
    return max(0.0, min(1.0, v))


def derive_class(conf):
    """confidence == 1.0 -> 'certain' (a premise); else 'prior' (a belief)."""
    return "certain" if conf >= CERTAIN else "prior"


def derive_load_class(e, conf, pinned_flag):
    """The injection tier: pinned -> always; authored intent honored except a
    decayed belief is forced dormant; default jit (only a pin earns always)."""
    if pinned_flag:
        return "always"
    explicit = str(e.get("load_class") or "").strip().lower()
    if explicit in ("always", "jit", "dormant"):
        if conf < DORMANT_BELOW and explicit != "dormant":
            return "dormant"
        return explicit
    if conf < DORMANT_BELOW:
        return "dormant"
    return "jit"


def _is_pinned(e):
    flag = str(e.get("pin") or "").strip().lower()
    if flag in ("true", "1", "yes"):
        return True
    return _slug(str(e.get("id") or "")) in PINNED_SLUGS


def _decode_lists(e):
    """evidence_log / confidence_history are one-line JSON; decode to lists.
    Fail-open: malformed -> []."""
    for k in ("evidence_log", "confidence_history"):
        raw = str(e.get(k) or "").strip()
        if not raw or raw in ("[]", "null"):
            e[k] = []
            continue
        try:
            v = json.loads(raw)
            e[k] = v if isinstance(v, list) else []
        except Exception:
            e[k] = []
    return e


def _json1(v):
    return json.dumps(v or [], separators=(",", ":"), ensure_ascii=False)


def _scope_rank(scope):
    """Lexicon authored-scope precedence: space: > project: > global."""
    if scope and scope.startswith("space:"):
        return 0
    if scope and scope.startswith("project:"):
        return 1
    return 2


# ---------------------------------------------------------------------------
# parsers — one per type, all fail-open (a garbled file is skipped, never fatal)
# ---------------------------------------------------------------------------

_PRIOR_DEFAULTS = {
    "id": "", "statement": "", "confidence": "", "load_class": "",
    "status": "live", "domain": "", "keywords": "", "stated_ts": "",
    "last_updated": "", "source": "", "pin": "",
    "evidence_log": "", "confidence_history": "",
    "supersedes": "", "replaced_by": "", "source_prior": "",
    "retired_ts": "", "retired_why": "",
    # attestation receipt (premise.py annotates; parse + rewrite carry through)
    "attest_payload": "", "attest_ts": "", "attest_by": "", "attest_turn": "",
    "attest_receipt": "", "attest_chain_index": "",
}

_LEX_DEFAULTS = {"term": "", "scope": "global", "definition": "", "kind": "",
                 "source": "", "examples": [], "updated_ts": "", "hits": "0"}

_HEUR_DEFAULTS = {
    "id": "", "move": "", "statement": "", "trigger": "", "keywords": "",
    "domain": "", "status": "live", "load_class": "jit",
    "stated_ts": "", "last_updated": "", "source": "",
    "supersedes": "", "replaced_by": "", "retired_ts": "", "retired_why": "",
    "name": "", "description": "",
}

_REF_DEFAULTS = {
    "id": "", "statement": "", "summary": "", "url": "", "keywords": "",
    "domain": "", "status": "live", "load_class": "", "source": "",
    "stated_ts": "", "last_updated": "", "name": "", "description": "",
    "supersedes": "", "replaced_by": "", "retired_ts": "", "retired_why": "",
}

_EPISODIC_DEFAULTS = {"name": "", "description": "", "type": "", "load_class": ""}


def _parse_prior(path):
    e = pk.parse_simple_frontmatter(path, _PRIOR_DEFAULTS)
    if not (e and e["id"] and e["statement"]):
        return None
    _decode_lists(e)
    conf = _coerce_conf(e.get("confidence"))
    pin = _is_pinned(e)
    e.update({"type": "prior", "confidence": conf, "class": derive_class(conf),
              "load_class": derive_load_class(e, conf, pin), "pinned": pin,
              "status": e.get("status") or STATUS_LIVE})
    return e


def _parse_lexicon(path):
    e = pk.parse_simple_frontmatter(path, _LEX_DEFAULTS, list_keys=("examples",))
    if not (e and e["term"] and e["definition"]):
        return None
    e["term_scope"] = e.pop("scope")  # authored scope; entry scope is root-derived
    e.update({"type": "lexicon", "id": e["term"], "statement": e["definition"],
              "confidence": 1.0, "class": "lexicon", "load_class": "jit",
              "status": STATUS_LIVE, "keywords": "", "domain": "", "pinned": False})
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
    never injected — where mc's resolvers silently dropped it."""
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
    dirs = [d] if root == "adopted" else [os.path.join(d, s) for s in _SCAN_SUBDIRS]
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


def load_all(project=None, include_retired=False, include_dormant=True, types=None):
    """Every entry across the root set, enriched (id/type/statement/confidence/
    class/load_class/status/keywords/domain/scope/root/path/pinned), deduped by
    (type, slug) with the shadowing law: project > helm-global > adopted."""
    if isinstance(types, str):
        types = (types,)
    merged = {}
    for root, scope, d in roots(project):
        merged.update(_load_root(root, scope, d))
    out = []
    for e in merged.values():
        # status filter AFTER the merge: a retired/tombstoned shadow-WINNER
        # drops out entirely — it must not un-bury the wider-scope entry it
        # shadowed (the tombstone law survives scope precedence).
        if not include_retired and e.get("status") != STATUS_LIVE:
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


# ---------------------------------------------------------------------------
# resolve — the ONE JIT lane (priors + heuristics + lexicon + references)
# ---------------------------------------------------------------------------

def resolve_prompt(text, project=None, cap=4):
    """JIT: live, non-dormant entries whose id/keywords (lexicon: term)
    word-boundary-match the turn text, ranked confidence-weighted
    (score = specific-guarded hits * confidence; ties -> last_updated desc then
    id). Pinned (load_class always) entries are NOT returned here — they are
    emitted unconditionally via pinned(). Salience law: EMPTY on no match."""
    low = (text or "").lower()
    if not low.strip():
        return []
    scored = []
    for e in load_all(project=project, include_dormant=False,
                      types=("prior", "heuristic", "lexicon", "reference")):
        if e.get("load_class") == "always":
            continue
        heur = e["type"] == "heuristic"
        generic = _HEURISTIC_GENERIC if heur else GENERIC_KEYWORDS
        min_len = _MIN_HEURISTIC_TOKEN if heur else 1
        probes = [(str(e["id"]).lower(), False)] + [
            (k.strip().lower(), k.strip().lower() in generic)
            for k in (e.get("keywords") or "").split(",") if k.strip()]
        hits = 0
        specific = False
        for p, is_generic in {(p, g) for p, g in probes}:
            # substring prefilter before the (expensive) word-boundary regex —
            # ~all probes miss on any given prompt, so only true hits pay the
            # regex. Measured 88ms -> 1.3ms per call on the live store, and this
            # runs on EVERY prompt in EVERY session fleet-wide.
            if p and len(p) >= min_len and p in low and \
                    re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", low):
                hits += 1
                if not is_generic:
                    specific = True
        if hits and specific:
            scored.append((hits * e["confidence"],
                           str(e.get("last_updated") or e.get("updated_ts") or ""),
                           str(e["id"]), e))
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    try:
        n = int(cap)
    except (TypeError, ValueError):
        n = 4
    return [t[3] for t in scored[:max(n, 0)]]


def pinned(project=None):
    """load_class=always entries — returned ALWAYS (no relevance gate), ranked
    confidence-desc then id so the always-on consumer can budget the top-N."""
    out = [e for e in load_all(project=project) if e.get("load_class") == "always"]
    out.sort(key=lambda e: (-e["confidence"], str(e["id"])))
    return out


# ---------------------------------------------------------------------------
# writers — byte-shape-compatible with the mc writers (same key order)
# ---------------------------------------------------------------------------

def write_prior(e, root_dir=None, path=None):
    """Write a prior-*.md (mc priors._write shape). class/load_class re-derived
    on write so a stale authored value can never desync from confidence."""
    if path is None:
        path = os.path.join(root_dir or _default_dir("prior"),
                            PRIOR_PREFIX + _slug(str(e["id"])) + ".md")
    conf = _coerce_conf(e.get("confidence"))
    if conf < CERTAIN:
        # a BELIEF clamps to 0.99 BEFORE the %.2f serialization: 0.995..0.999
        # would round to "confidence: 1.00" beside "class: prior" and the next
        # read would coerce it into a certain premise — an agent-suppliable
        # value crossing the human-only certainty rail.
        conf = min(conf, BELIEF_CLAMP[1])
    klass = derive_class(conf)
    pin = _is_pinned(e)
    lc = derive_load_class(e, conf, pin)
    st = re.sub(r"\s+", " ", (e.get("statement") or "").replace('"', "'"))
    status = e.get("status") or "live"
    flag = "" if status == "live" else " [" + status + "]"
    label = "premise" if klass == "certain" else "prior"
    body = [
        "---",
        "name: " + PRIOR_PREFIX + _slug(str(e["id"])),
        'description: "' + label + flag + ': ' + (str(e["id"]) + " - " + st)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: prior",
        "  id: " + str(e["id"]),
        "  statement: " + st,
        "  confidence: " + ("%.2f" % conf),
        "  class: " + klass,
        "  load_class: " + lc,
        "  status: " + status,
        "  domain: " + (e.get("domain") or ""),
        "  keywords: " + (e.get("keywords") or ""),
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "human"),
        "  evidence_log: " + _json1(e.get("evidence_log")),
        "  confidence_history: " + _json1(e.get("confidence_history")),
    ]
    if pin:
        body.append("  pin: true")
    # attestation annotations survive rewrites — the ledger is the truth, but a
    # store rewrite (evidence/retire) must never orphan the entry's receipt keys
    for opt in ("attest_payload", "attest_ts", "attest_by", "attest_turn",
                "attest_receipt", "attest_chain_index"):
        if e.get(opt) not in (None, ""):
            body.append("  " + opt + ": " + str(e[opt]))
    for opt in ("supersedes", "replaced_by", "source_prior"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    if e.get("retired_ts"):
        body += ["  retired_ts: " + str(e["retired_ts"]),
                 "  retired_why: " + (e.get("retired_why") or "")]
    body += ["---", "",
             label.upper() + flag + ": " + (e.get("statement") or ""),
             "",
             "**Why a " + label + ":** human-stated standing "
             + ("truth" if klass == "certain" else "belief")
             + "; agent flags, never silently overrides; confidence updates on logged evidence.",
             ""]
    pk.atomic_write(path, "\n".join(body))
    return path


def write_lexicon(e, root_dir=None, path=None):
    """Write a lex-*.md (mc lexicon._write_term shape). Filename follows the mc
    term_path law: global -> lex-<term>.md; narrower authored scope prefixes
    lex-<scope-slug>--<term>.md so a project define never clobbers global."""
    term = e.get("term") or str(e.get("id") or "")
    scope = e.get("term_scope") or "global"
    if path is None:
        name = "lex-" + _slug(term) + ".md" if scope == "global" \
            else "lex-" + _slug(scope) + "--" + _slug(term) + ".md"
        path = os.path.join(root_dir or _default_dir("lexicon"), name)
    d = (e.get("definition") or e.get("statement") or "").replace('"', "'")
    body = [
        "---",
        "name: lex-" + _slug(term),
        'description: "' + ("lexicon: " + term + " = " + d)[:200] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: lexicon",
        "  term: " + term,
        "  scope: " + scope,
        "  kind: " + (e.get("kind") or "phrase"),
        "  source: " + (e.get("source") or "define"),
        "  updated_ts: " + str(e.get("updated_ts") or ""),
        "  hits: " + str(e.get("hits") or "0"),
        "  definition: " + re.sub(r"\s+", " ", e.get("definition") or e.get("statement") or "").strip(),
    ]
    ex = e.get("examples") or []
    if ex:
        body.append("  examples: " + " || ".join(ex))
    body += ["---", "", term + (" (" + e["kind"] + ")" if e.get("kind") else "")
             + ": " + (e.get("definition") or e.get("statement") or ""), ""]
    pk.atomic_write(path, "\n".join(body))
    return path


def write_heuristic(e, root_dir=None, path=None):
    """Write a heuristic-*.md (mc heuristics_store._write shape). confidence is
    always 1 (a move, not a belief) so it is NOT a field; load_class always jit."""
    if path is None:
        path = os.path.join(root_dir or _default_dir("heuristic"),
                            "heuristic-" + _slug(str(e["id"])) + ".md")
    move_raw = (e.get("move") or e.get("statement") or "").strip()
    move = re.sub(r"\s+", " ", move_raw.replace('"', "'"))
    trig = (e.get("trigger") or e.get("keywords") or "").strip()
    status = e.get("status") or "live"
    flag = "" if status == "live" else " [" + status + "]"
    body = [
        "---",
        "name: heuristic-" + _slug(str(e["id"])),
        'description: "heuristic' + flag + ' (cross-domain MOVE, confidence=1): '
        + (str(e["id"]) + " - " + move)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: heuristic",
        "  id: " + str(e["id"]),
        "  move: " + move,
        "  trigger: " + trig,
        "  domain: " + (e.get("domain") or ""),
        "  load_class: jit",
        "  status: " + status,
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "human"),
    ]
    for opt in ("supersedes", "replaced_by"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    if e.get("retired_ts"):
        body += ["  retired_ts: " + str(e["retired_ts"]),
                 "  retired_why: " + (e.get("retired_why") or "")]
    body += ["---", "",
             "HEURISTIC" + flag + " (a cross-domain MOVE you APPLY, not a belief you hold): "
             + move_raw,
             "",
             "**Trigger pattern** (when it fires): " + (trig or "(none authored)"),
             "",
             "**Why a heuristic, not a premise:** this is a STRATEGY you reach for across domains "
             "(confidence=1 by construction), distinct from a premise/prior (a belief that gates). "
             "A reflex is this move compiled to fire every turn. It surfaces JIT when its trigger "
             "pattern appears in a turn - never added to the always-on digest.",
             ""]
    pk.atomic_write(path, "\n".join(body))
    return path


def write_reference(e, root_dir=None, path=None):
    """Write a ref-*.md — the NEW class for harvested external reference
    material, same house frontmatter shape as its siblings."""
    if path is None:
        path = os.path.join(root_dir or _default_dir("reference"),
                            "ref-" + _slug(str(e["id"])) + ".md")
    summary_raw = (e.get("statement") or e.get("summary") or "").strip()
    summary = re.sub(r"\s+", " ", summary_raw.replace('"', "'"))
    status = e.get("status") or "live"
    flag = "" if status == "live" else " [" + status + "]"
    lc = str(e.get("load_class") or "").strip().lower()
    if lc not in ("always", "jit", "dormant"):
        lc = "jit"
    body = [
        "---",
        "name: ref-" + _slug(str(e["id"])),
        'description: "reference' + flag + ': ' + (str(e["id"]) + " - " + summary)[:170] + '"',
        "metadata:",
        "  node_type: memory",
        "  type: reference",
        "  id: " + str(e["id"]),
        "  summary: " + summary,
        "  url: " + (e.get("url") or ""),
        "  keywords: " + (e.get("keywords") or ""),
        "  domain: " + (e.get("domain") or ""),
        "  load_class: " + lc,
        "  status: " + status,
        "  stated_ts: " + str(e.get("stated_ts") or ""),
        "  last_updated: " + str(e.get("last_updated") or e.get("stated_ts") or ""),
        "  source: " + (e.get("source") or "harvest"),
    ]
    for opt in ("supersedes", "replaced_by"):
        if e.get(opt):
            body.append("  " + opt + ": " + str(e[opt]))
    if e.get("retired_ts"):
        body += ["  retired_ts: " + str(e["retired_ts"]),
                 "  retired_why: " + (e.get("retired_why") or "")]
    body += ["---", "", "REFERENCE" + flag + ": " + summary_raw, ""]
    if e.get("url"):
        body += ["Source: " + e["url"], ""]
    pk.atomic_write(path, "\n".join(body))
    return path


_WRITERS = {"prior": write_prior, "heuristic": write_heuristic,
            "reference": write_reference, "lexicon": write_lexicon}


# ---------------------------------------------------------------------------
# lifecycle — evidence, tombstone, retire (the mc laws, root-aware)
# ---------------------------------------------------------------------------

def apply_evidence(pid, ts, delta, reason, by="agent", kind=None, project=None):
    """Move a prior's confidence by `delta`, appending the receipt to
    evidence_log + a snapshot to confidence_history. A belief clamps to
    [0.05, 0.99] (never auto-1.0). A certain-prior (human truth) is NOT
    auto-demoted by agent evidence — the contradiction is LOGGED (the drift
    report reads exactly that) and surfaced, confidence stays pinned. An
    un-reasoned move is refused. Writes back IN PLACE on whichever root holds
    the entry — including adopted (the adoption contract)."""
    e = _find(pid, project=project, types=("prior",))
    if not e:
        return None, "not found"
    if not str(reason or "").strip():
        return None, "evidence move requires a reason (un-logged moves are forbidden)"
    try:
        d = float(delta)
    except (TypeError, ValueError):
        return None, "delta not numeric"
    old = e["confidence"]
    new = old + d
    surfaced_msg = None
    if e["class"] == "certain":
        new = min(CERTAIN, max(BELIEF_CLAMP[0], new)) if by == "human" else old
        if by != "human":
            surfaced_msg = ("certain-prior: agent evidence is LOGGED + surfaced as "
                            "drift (contradicted), confidence not auto-applied")
    else:
        new = max(BELIEF_CLAMP[0], min(BELIEF_CLAMP[1], new))
    e["evidence_log"] = list(e.get("evidence_log") or []) + [
        {"ts": ts, "type": (kind or ("support" if d >= 0 else "contradict")),
         "delta": round(d, 4), "reason": reason, "by": by}]
    e["confidence_history"] = list(e.get("confidence_history") or []) + [
        {"ts": ts, "value": round(new, 4), "reason": reason}]
    e["confidence"] = new
    e["last_updated"] = ts
    write_prior(e, path=e["path"])
    return e, surfaced_msg


def mark_superseded(old_id, new_id, ts, reason="", project=None):
    """Tombstone OLD as superseded BY NEW: old.status=delete_eligible +
    old.replaced_by=new, backpointer new.supersedes=old. A tombstoned entry
    STOPS injecting (load skips non-live) but the FILE STAYS — this function
    NEVER deletes (the presence-gated sweep is the physical delete). Idempotent;
    refuses self-supersede and a missing replacement."""
    old_id = (old_id or "").strip()
    new_id = (new_id or "").strip()
    if not old_id or not new_id:
        return None, "supersede requires both <old-id> and <new-id>"
    if _slug(old_id) == _slug(new_id):
        return None, "an entry cannot supersede itself"
    writable = ("prior", "heuristic", "reference")
    old = _find(old_id, project=project, types=writable)
    if not old:
        return None, "old entry '" + old_id + "' not found"
    new = _find(new_id, project=project, types=writable)
    if not new:
        return None, "new entry '" + new_id + "' not found (the replacement must exist first)"
    old.update({"status": STATUS_DELETE_ELIGIBLE, "replaced_by": str(new["id"]),
                "last_updated": ts})
    if reason:
        old["retired_why"] = reason
        old["retired_ts"] = old.get("retired_ts") or ts
    _WRITERS[old["type"]](old, path=old["path"])
    if _slug(str(new.get("supersedes") or "")) != _slug(str(old["id"])):
        new["supersedes"] = str(old["id"])
        new["last_updated"] = ts
        _WRITERS[new["type"]](new, path=new["path"])
    return old, None


def retire(eid, ts, why="", project=None):
    """Human-retire: status=retired, file KEPT as the record (never deleted)."""
    e = _find(eid, project=project, types=("prior", "heuristic", "reference"))
    if not e:
        return None, "'" + str(eid) + "' not found"
    e.update({"status": STATUS_RETIRED, "retired_ts": ts, "retired_why": why,
              "last_updated": ts})
    _WRITERS[e["type"]](e, path=e["path"])
    return e, None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = """usage: helm store <verb> [args] [--project P]
  list [--type T] [--all]                     entries (live; --all incl. retired)
  get <id>                                    one entry, full record
  resolve <text>                              JIT lookup — what would fire for this prompt
  pinned                                      the always-on lane
  add <type> <id> | <statement> [| ...]       type: prior|premise|lexicon|heuristic|reference
      prior:     <id> | <statement> [| conf [| keywords [| domain]]]  (belief, default 0.6)
      premise:   <id> | <statement> [| keywords [| domain]]           (certain, conf 1.0)
      lexicon:   <term> | <definition> [| kind [| ex1 || ex2]]
      heuristic: <id> | <move> [| trigger-csv [| domain]]
      reference: <id> | <summary> [| url [| keywords [| domain]]]
      flags: [--source S] [--rationale <text...>]   (rationale seeds evidence_log)
  resolve                                     prompt on stdin -> JIT hits
  pinned                                      the always-on lane
  evidence <ts> <id> <delta> <reason...>      move a belief (logged + clamped)
  supersede <ts> <old-id> <new-id> [reason]   TOMBSTONE old (file kept)
  retire <ts> <id> [why...]                   retire (file kept as the record)
  counts                                      per-root type inventory"""


def _fmt(e):
    t = e["type"]
    if t == "prior":
        tag = "PREMISE" if e["class"] == "certain" else "PRIOR"
        return tag + " " + str(e["id"]) + " [" + ("%.2f" % e["confidence"]) + "]: " \
            + (e.get("statement") or "")
    if t == "heuristic":
        return "HEURISTIC " + str(e["id"]) + ": " + (e.get("statement") or "")
    if t == "lexicon":
        kind = e.get("kind") or ""
        return "TERM " + str(e["id"]) + ((" (" + kind + ")") if kind else "") + ": " \
            + (e.get("definition") or "")
    if t == "reference":
        url = e.get("url") or ""
        return "REF " + str(e["id"]) + ": " + (e.get("statement") or "") \
            + ((" <" + url + ">") if url else "")
    return str(e["id"]) + ": " + (e.get("statement") or "")


def cmd_store(args):
    """store <list|get|add|resolve|pinned|evidence|supersede|retire|counts> — the ONE typed personal-knowledge store."""
    args = list(args)
    project = None
    if "--project" in args:
        i = args.index("--project")
        if i + 1 >= len(args):
            print("helm store: --project needs a name", file=sys.stderr)
            return 2
        project = args[i + 1]
        del args[i:i + 2]
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    cmd, rest = args[0], args[1:]

    if cmd == "list":
        t = None
        if "--type" in rest:
            i = rest.index("--type")
            t = rest[i + 1] if i + 1 < len(rest) else None
        es = load_all(project=project, include_retired=("--all" in rest),
                      types=(t,) if t else None)
        if not es:
            print("helm store: empty. Add one: helm store add prior <id> | <statement>")
            return 0
        es.sort(key=lambda e: (e["type"], -e["confidence"], str(e["id"])))
        print("helm store (" + str(len(es)) + " entries):")
        for e in es:
            mark = "" if e["status"] == STATUS_LIVE else " [" + e["status"] + "]"
            print("  - " + str(e["id"]) + mark + " [" + e["type"] + " "
                  + ("%.2f" % e["confidence"]) + "/" + e["load_class"] + " "
                  + e["scope"] + "]: " + (e.get("statement") or "")[:100])
        return 0

    if cmd == "get":
        eid = " ".join(a for a in rest if not a.startswith("--")).strip()
        e = _find(eid, project=project)
        if not e:
            print("helm store: '" + eid + "' not found")
            return 1
        print(str(e["id"]) + " [" + e["type"] + " " + e["class"] + " "
              + ("%.2f" % e["confidence"]) + "/" + e["load_class"] + " "
              + e["status"] + " " + e["root"] + "]: " + (e.get("statement") or ""))
        for k in ("keywords", "domain", "url", "supersedes", "replaced_by",
                  "retired_why"):
            if e.get(k):
                print("  " + k + ": " + str(e[k]))
        print("  path: " + e["path"])
        return 0

    if cmd == "add":
        if len(rest) < 2:
            print(_USAGE, file=sys.stderr)
            return 2
        etype = rest[0]
        if etype not in ("prior", "premise", "lexicon", "heuristic", "reference"):
            print("helm store: unknown type '" + etype
                  + "' (prior|premise|lexicon|heuristic|reference)", file=sys.stderr)
            return 2
        tail = rest[1:]
        source = None
        rationale = ""
        kept = []
        i = 0
        while i < len(tail):
            if tail[i] == "--source" and i + 1 < len(tail):
                source = tail[i + 1]
                i += 2
                continue
            if tail[i] == "--rationale":
                rationale = " ".join(tail[i + 1:]).strip()
                break
            kept.append(tail[i])
            i += 1
        parts = [p.strip() for p in " ".join(kept).split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            print(_USAGE, file=sys.stderr)
            return 2
        ts = pk.now_ts()

        if etype in ("prior", "premise"):
            path = os.path.join(_default_dir("prior", project),
                                PRIOR_PREFIX + _slug(parts[0]) + ".md")
            e = _parse_prior(path) or {}
            if etype == "prior":
                try:
                    conf = float(parts[2]) if len(parts) > 2 and parts[2] else 0.6
                except ValueError:
                    conf = 0.6
                kw = parts[3] if len(parts) > 3 else e.get("keywords", "")
                dom = parts[4] if len(parts) > 4 else e.get("domain", "")
                src = source or "agent-inferred"
            else:
                conf = CERTAIN
                kw = parts[2] if len(parts) > 2 else e.get("keywords", "")
                dom = parts[3] if len(parts) > 3 else e.get("domain", "")
                src = source or "human"
            conf = _coerce_conf(conf)
            if etype == "prior":
                # the prior verb mints BELIEFS; certainty (1.0) is the premise
                # verb's human-only lane — an add-prior 0.999/1.0 clamps to 0.99
                conf = min(conf, BELIEF_CLAMP[1])
            e.update({"id": parts[0], "statement": parts[1], "status": STATUS_LIVE,
                      "stated_ts": ts, "last_updated": ts, "confidence": conf,
                      "keywords": kw, "domain": dom, "source": src})
            # SEED the audit trail at creation when a rationale is given, so a
            # belief is never confidence-without-a-receipt — even at birth.
            if rationale and not (e.get("evidence_log") or e.get("confidence_history")):
                seed_kind = "stated" if etype == "premise" else "assigned"
                e["evidence_log"] = [{"ts": ts, "type": seed_kind, "delta": round(conf, 4),
                                      "reason": rationale, "by": src}]
                e["confidence_history"] = [{"ts": ts, "value": round(conf, 4),
                                            "reason": rationale}]
            p = write_prior(e, path=path)
            print("helm store: LIVE '" + parts[0] + "' [" + derive_class(conf) + " "
                  + ("%.2f" % conf) + "] - " + parts[1])
            print("  stored: " + p)
            return 0

        if etype == "lexicon":
            scope = ("project:" + project) if project else "global"
            e = {"term": parts[0], "definition": parts[1],
                 "kind": parts[2] if len(parts) > 2 and parts[2] else "phrase",
                 "term_scope": scope, "source": source or "define",
                 "updated_ts": ts, "hits": "0"}
            if len(parts) > 3 and parts[3]:
                e["examples"] = [x.strip() for x in parts[3].split("||") if x.strip()]
            p = write_lexicon(e, root_dir=_default_dir("lexicon", project))
            print("helm store: LIVE '" + parts[0] + "' [lexicon " + scope + "] - " + parts[1])
            print("  stored: " + p)
            return 0

        if etype == "heuristic":
            path = os.path.join(_default_dir("heuristic", project),
                                "heuristic-" + _slug(parts[0]) + ".md")
            e = _parse_heuristic(path) or {}
            trig = parts[2] if len(parts) > 2 else (e.get("trigger") or "")
            dom = parts[3] if len(parts) > 3 else e.get("domain", "")
            e.update({"id": parts[0], "move": parts[1], "statement": parts[1],
                      "trigger": trig, "domain": dom, "status": STATUS_LIVE,
                      "stated_ts": ts, "last_updated": ts,
                      "source": source or e.get("source") or "human"})
            p = write_heuristic(e, path=path)
            print("helm store: LIVE '" + parts[0] + "' [heuristic conf=1 jit] - " + parts[1])
            if trig:
                print("  trigger: " + trig)
            print("  stored: " + p)
            return 0

        # reference: <id> | <summary> [| url [| keywords [| domain]]]
        path = os.path.join(_default_dir("reference", project),
                            "ref-" + _slug(parts[0]) + ".md")
        e = _parse_reference(path, os.path.basename(path)) or {}
        e.update({"id": parts[0], "statement": parts[1], "summary": parts[1],
                  "url": parts[2] if len(parts) > 2 else e.get("url", ""),
                  "keywords": parts[3] if len(parts) > 3 else e.get("keywords", ""),
                  "domain": parts[4] if len(parts) > 4 else e.get("domain", ""),
                  "status": STATUS_LIVE, "stated_ts": e.get("stated_ts") or ts,
                  "last_updated": ts, "source": source or e.get("source") or "harvest"})
        p = write_reference(e, path=path)
        print("helm store: LIVE '" + parts[0] + "' [reference jit] - " + parts[1])
        print("  stored: " + p)
        return 0

    if cmd == "resolve":
        # explicit args win; stdin is the hook path (may be an empty pipe —
        # ignoring args silently made an advertised verb a no-op). The --project
        # flag PAIR was already stripped above — filtering every word equal to
        # the project name here ate real query tokens (exactly the keyword most
        # likely to match that project's entries).
        text = " ".join(a for a in rest if not a.startswith("--"))
        from_args = bool(text)
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read()
        if not text.strip():
            print("usage: helm store resolve <text>   (or pipe prompt text on stdin)",
                  file=sys.stderr)
            return 2
        hits = resolve_prompt(text, project=project)
        for e in hits:
            print(_fmt(e))
        if not hits and from_args:
            # a human asked directly — explain the silence; the stdin/hook path
            # stays empty-on-no-match (salience law)
            print("helm store resolve: no JIT match (salience law — generic-only "
                  "matches never fire); try `helm store get <id>` for direct lookup")
        return 0

    if cmd == "pinned":
        for e in pinned(project=project):
            print(_fmt(e))
        return 0

    if cmd == "evidence":
        if len(rest) < 4:
            print("usage: helm store evidence <ts> <id> <delta> <reason...>", file=sys.stderr)
            return 2
        e, err = apply_evidence(rest[1], rest[0], rest[2], " ".join(rest[3:]),
                                by="agent", project=project)
        if err and not e:
            print("helm store evidence: " + err, file=sys.stderr)
            return 1
        if err:
            # informational: a certain-prior's contradiction was LOGGED + surfaced
            # as drift (confidence intentionally not moved). Not an error.
            print("helm store: " + err)
        print("helm store: '" + rest[1] + "' confidence -> " + ("%.2f" % e["confidence"]))
        return 0

    if cmd == "supersede":
        if len(rest) < 3:
            print("usage: helm store supersede <ts> <old-id> <new-id> [reason...]",
                  file=sys.stderr)
            return 2
        e, err = mark_superseded(rest[1], rest[2], rest[0], " ".join(rest[3:]),
                                 project=project)
        if err:
            print("helm store supersede: " + err, file=sys.stderr)
            return 1
        print("helm store: TOMBSTONED '" + rest[1] + "' (delete_eligible, replaced_by '"
              + rest[2] + "') - file KEPT until the sweep verifies no dangling ref")
        return 0

    if cmd == "retire":
        if len(rest) < 2:
            print("usage: helm store retire <ts> <id> [why...]", file=sys.stderr)
            return 2
        e, err = retire(rest[1], rest[0], " ".join(rest[2:]), project=project)
        if err:
            print("helm store retire: " + err, file=sys.stderr)
            return 1
        print("helm store: RETIRED '" + rest[1] + "' (" + (e.get("retired_why") or "")
              + ") - file kept as the record")
        return 0

    if cmd == "counts":
        c = counts(project=project)
        print("helm store counts:")
        for root in c:
            per = c[root]
            row = " ".join(k + "=" + str(per[k]) for k in sorted(per)) or "-"
            print("  %-11s %4d  %s" % (root, sum(per.values()), row))
        return 0

    print("helm store: unknown verb '" + cmd + "'", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
