"""helm store — shared constants, module-level state, and leaf helpers.

The base of the store package's one-way dependency graph:
  _common <- load <- resolve <- write <- index <- cli
Holds the confidence/status constants, the type maps, and the leaf helpers
used by 2+ clusters (derivation, pin/scope/recency, JSON coercion). Moved
verbatim from the pre-split helm/store.py; no logic changed.
"""
import calendar
import json
import re

from .. import pk

_slug = pk.slug

# Confidence machinery (the priors confidence law — constants + laws).
CERTAIN = 1.0            # confidence == CERTAIN -> class certain (a premise)
DORMANT_BELOW = 0.4      # confidence < this -> load_class dormant (never injects)
ACT_AT = 0.85            # confidence >= this -> auto-act tier
BELIEF_CLAMP = (0.05, 0.99)  # a belief NEVER auto-reaches 1.0 (human-only rail)

STATUS_LIVE = "live"
STATUS_RETIRED = "retired"
STATUS_DELETE_ELIGIBLE = "delete_eligible"
# CANDIDATE (safe inferred capture): an agent-inferred entry lands here, NEVER
# live — it is a non-live status, so the load_all live-filter already excludes
# it from resolve/pinned/inject (the hard law: never silently authoritative).
# v2 (autolearn): EVERY capturable type may be born a candidate — capture
# everything, canonize NOTHING automatically; premise stays refused (an
# inference may not claim certainty even in escrow — the human-only rail).
# `helm store confirm` promotes, `reject` retires-in-place, drain
# --expire-candidates is the age leg. source: inferred|asked-once|explicit.
STATUS_CANDIDATE = "candidate"
# PROVISIONAL (xrev-cleared candidate): a candidate a cross-family /x review has
# cleared (by design: the technical ones may go PROVISIONALLY LIVE
# once xrev clears them — xrev is the gate, not the owner). A provisional entry
# FIRES through the resolver like live (it is usable knowledge) but renders with
# a visible [provisional] tag everywhere (CLI list + inject line + web) until the
# owner ratifies it (confirm -> live) or rejects it (reject -> retired). The
# graduation verb is `helm store xrev-clear <id> --by <reviewer>` (candidate ->
# provisional; the reviewer ATTESTS the review happened, the verb never runs it).
STATUS_PROVISIONAL = "provisional"

# The statuses that FIRE through the injecting lanes (resolve/pinned/inject):
# live = human-canon, provisional = xrev-cleared-but-not-yet-owner-ratified.
# candidate/retired/delete_eligible stay OUT (the hard law: never silently
# authoritative — an un-reviewed candidate fires NOTHING).
INJECTABLE_STATUSES = (STATUS_LIVE, STATUS_PROVISIONAL)

# PINNED priors inject EVERY turn (load_class always). Pin = a `pin: true`
# flag OR membership here (belt-and-suspenders, the canonical tuple so the live
# store's pins survive the adoption).
PINNED_SLUGS = ("human-is-context-free", "build-the-full-depgraph", "drift-is-the-enemy")

LEGACY_PREFIX = "prem-"
PRIOR_PREFIX = "prior-"

# SPECIFICITY guard (anti-wallpaper, the priors specificity law): a JIT entry
# must match the turn via at least one SPECIFIC (non-generic) keyword or its
# id — a generic-only match would wallpaper nearly every turn.
GENERIC_KEYWORDS = frozenset({
    "build", "code", "fix", "test", "work", "task", "run", "make", "do", "the", "a", "an",
    "is", "it", "this", "that", "agent", "change", "file", "add", "use", "new",
    "now", "get", "set", "go", "and", "or", "to", "of", "in", "on", "for", "with",
})
# Heuristics carry the heuristics-store extras on top of the shared set.
_HEURISTIC_GENERIC = GENERIC_KEYWORDS | {"move", "apply", "domain", "heuristic", "strategy"}
_MIN_HEURISTIC_TOKEN = 3  # the heuristics-store minimum token length

# Where each type's NEW writes land inside a helm root (the adopted root stays
# flat and is only ever an explicit target).
TYPE_SUBDIR = {"prior": "premises", "lexicon": "lexicon",
               "heuristic": "heuristics", "reference": "references"}
# Subdirs scanned per helm root ("priors" = the pre-declared _global prior-art
# corpus category; harmless no-op where absent).
_SCAN_SUBDIRS = ("premises", "heuristics", "lexicon", "references", "priors")

_TYPE_ORDER = ("prior", "heuristic", "reference", "lexicon", "capability", "episodic")

# The JIT-resolvable types (episodic never fires — load_class dormant).
# `capability` is never PARSED from disk (capability.py owns its module-constant
# catalog); it is registered here so the WIRED-substrate self-index — fed in
# through resolve_prompt's entries= seam by inject — rides the ONE JIT lane
# (DF-weighted, specificity-gated, cap-4, cooldowned), not a parallel injector.
_JIT_TYPES = ("prior", "heuristic", "lexicon", "reference", "capability")


# ---------------------------------------------------------------------------
# derivation (class + load_class are DERIVED, never authored — the priors law)
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
    """pin: true/1/yes pins; an EXPLICIT false/0/no un-pins — it beats the
    PINNED_SLUGS tuple (what makes `helm store demote` stick on a founding
    pin); an absent flag falls through to the tuple."""
    flag = str(e.get("pin") or "").strip().lower()
    if flag in ("true", "1", "yes"):
        return True
    if flag in ("false", "0", "no"):
        return False
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


def _recency(e):
    """One comparable recency scalar (epoch seconds) from the store's mixed
    timestamp formats — ISO ('2026-06-17' / full Z stamps), raw epoch strings,
    blank. Unknown/blank reads 0 (oldest): an entry with no timestamp ranks
    LAST in the pinned walk — date it (evidence) or demote it."""
    raw = str(e.get("last_updated") or e.get("updated_ts")
              or e.get("stated_ts") or "").strip()
    try:
        return float(raw)
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}):(\d{2}))?", raw)
        return float(calendar.timegm(
            tuple(int(x or 0) for x in m.groups()) + (0, 0, 0))) if m else 0.0
