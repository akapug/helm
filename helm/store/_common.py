"""helm store — shared constants, module-level state, and leaf helpers.

The base of the store package's one-way dependency graph:
  _common <- load <- resolve <- write <- index <- cli
Holds the confidence/status constants, the type maps, and the leaf helpers
used by 2+ clusters (derivation, pin/scope/recency, JSON coercion). Recency
parsing lives here so readers and the write boundary cannot accept two meanings.
"""
import datetime
import json
import re

from .. import localnames, pk

_slug = pk.slug

# Confidence machinery (ported from the predecessor store's priors.py — same
# constants, same laws).
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


# ---------------------------------------------------------------------------
# THE ONE STALE-ON-REMINT CONTRACT
#
# Fields that DO NOT survive a re-mint over a retired id — new, often
# contradictory text under the same slug. It lives here because it had TWO
# spellings: store/cli.py's list and premise/_capture.py's own hard-coded
# tuple. `gloss` was added to the first and not the second, so a re-minted
# premise kept firing a line the entry no longer said (r3). A
# second spelling of a contract always drifts from the first — the only
# question is when someone notices.
#
# A GLOSS IS DERIVED FROM THE STATEMENT, which is why it belongs here: keeping
# it across a re-mint is worse than the truncation it exists to prevent, because
# a severed sentence is visibly incomplete and a stale gloss is confidently
# wrong.
STALE_ON_REMINT = ("replaced_by", "supersedes", "retired_ts", "retired_why",
                   "xrev_by", "xrev_ts", "policy_kind", "policy_members",
                   "policy_reason", "gloss")
# PROVISIONAL (xrev-cleared candidate): a candidate a cross-family /x review has
# cleared (owner canon 2026-07-22: the technical ones may go PROVISIONALLY LIVE
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
# flag OR membership here (belt-and-suspenders, the same tuple as the
# predecessor store's, so the live store's pins survive the adoption).
PINNED_SLUGS = ("human-is-context-free", "build-the-full-depgraph", "drift-is-the-enemy")

LEGACY_PREFIX = "prem-"
PRIOR_PREFIX = "prior-"

# THE one retest line (the resolve-test nudge) — a capture is done at FIRES,
# never at stored:. ONE constant for EVERY capture surface (store add,
# keywords --add, and helm premise's capture + supersede legs): a second
# spelling of the loop's last step would drift from the first — the
# STALE_ON_REMINT lesson above, applied to prose. It lives HERE (not in
# cli.py, where it was born) because premise/_capture.py emits it too, and a
# constant reachable only through one emitter's module invites the copy.
RETEST = ("  now RETEST: helm store resolve \"<a sentence someone would "
          "actually type>\" — 3+ phrasings, one from the incident, plus a "
          "must-MISS control so the entry has not become a spammer")

# SPECIFICITY guard (anti-wallpaper, ported from the predecessor's priors.py): a JIT entry
# must match the turn via at least one SPECIFIC (non-generic) keyword or its
# id — a generic-only match would wallpaper nearly every turn.
#
# THE BOUNDARY IS GRAMMATICAL, NOT A CASE LIST: every CLOSED-CLASS (function)
# word belongs here, because English does not mint new modals or pronouns, so
# the set is finite and enumerable — unlike a list of "words that felt generic",
# which is a per-case handler and always one token short. Open-class content
# words stay OUT even when they feel common, because they can be topical.
#
# WHY THIS MATTERS MORE THAN IT LOOKS, measured live on 2026-07-25 with the
# prompt "the dispatch to codex-3 is overdue, should I cancel it": all FOUR jit
# slots were won by the modal `should` at weight 0.250, while the actually
# relevant `dispatch-to-codex-seat-use-incept-not-custom-intent` (0.098) and
# helm's OWN `dispatch` capability index were pushed over cap. DF weighting was
# working exactly as designed — `should` occurs in 4 entries so it is RARE and
# therefore heavily weighted, while `dispatch` is common at 0.062. A meaningless
# modal beat the topical term BECAUSE it was rarer. Function words are the exact
# population that is rare-yet-meaningless, so leaving them out of this set turns
# df-weighting from a relevance signal into an anti-relevance one.
#
# One set fixes both ends: drain (_common consumer, drain.py) stops EXTRACTING
# them as keywords, and the specificity guard stops a function-word-only match
# from firing for entries that already carry them. `should` is 6 characters, so
# drain's `len(w) >= 5` filter never caught it.
def host_generic_keywords():
    """A HOST'S OWN GENERIC WORDS: a name so common in one operator's store
    (the tool helm replaced, say) that it matches nearly every entry there and
    so says nothing about a turn. It is that host's word, not English, so its
    local names carry it (`generic-keywords`, helm/localnames.py). Read once,
    into the set below, at import."""
    return frozenset(w.lower() for w in localnames.words("generic-keywords"))


GENERIC_KEYWORDS = frozenset({
    # original content-ish set, kept verbatim
    "build", "code", "fix", "test", "work", "task", "run", "make", "do", "the", "a", "an",
    "is", "it", "this", "that", "agent", "change", "file", "add", "use", "new",
    "now", "get", "set", "go", "and", "or", "to", "of", "in", "on", "for", "with",
    # modals — the live failure above
    "should", "could", "would", "might", "must", "can", "cannot", "will",
    "shall", "may", "ought",
    # auxiliaries / copula
    "be", "been", "being", "am", "are", "was", "were", "has", "have", "had",
    "does", "did", "done", "doing", "let",
    # negation
    "not", "no", "nor", "never", "none",
    # pronouns + possessives
    "i", "me", "my", "mine", "myself", "you", "your", "yours", "we", "us",
    "our", "ours", "they", "them", "their", "theirs", "he", "him", "his",
    "she", "her", "hers", "its", "these", "those", "there", "here", "who",
    "whom", "whose", "someone", "something", "anything", "everything",
    # question words
    "what", "why", "how", "when", "where", "which",
    # conjunctions / subordinators
    "if", "then", "else", "but", "so", "because", "although", "though",
    "while", "unless", "until", "since", "than", "whether", "as", "that",
    # prepositions / particles
    "at", "by", "from", "into", "onto", "out", "off", "over", "under",
    "about", "after", "before", "between", "through", "during", "without",
    "within", "against", "across", "up", "down", "via", "per", "upon",
    # determiners / quantifiers / degree
    "all", "any", "some", "each", "every", "both", "few", "many", "much",
    "more", "most", "less", "least", "other", "another", "same", "such",
    "very", "just", "only", "also", "too", "still", "again", "even", "yet",
    "own", "way", "thing", "things",
}) | host_generic_keywords()
# Heuristics carry the predecessor heuristics store's extras on top of the shared set.
_HEURISTIC_GENERIC = GENERIC_KEYWORDS | {"move", "apply", "domain", "heuristic", "strategy"}
_MIN_HEURISTIC_TOKEN = 3  # the predecessor heuristics store's _MIN_TOKEN

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
    Fail-open: malformed -> [].

    IDEMPOTENT, and that is load-bearing rather than defensive: this runs at
    TWO doors on purpose (see _parse_entry), so an entry reaching the second
    one is already decoded. Re-decoding a LIST would stringify it to a Python
    repr — single quotes — which json.loads rejects, and the fail-open would
    then silently WIPE the history it was meant to preserve. An already-typed
    value is therefore left exactly as it is."""
    for k in ("evidence_log", "confidence_history"):
        if isinstance(e.get(k), list):       # already decoded — never re-decode
            continue
        raw = str(e.get(k) or "").strip()
        if not raw or raw in ("[]", "null"):
            e[k] = []
            continue
        try:
            v = json.loads(raw)
            e[k] = v if isinstance(v, list) else []
        except Exception:
            e[k] = []
    # pending_revision is a DICT, not a list. THIS FUNCTION IS THE ONE DECODER;
    # what was wrong was the claim about where it RUNS. An earlier version of
    # this comment said it "decodes HERE because this is the one door every
    # reader comes through" — measured false by a second read: it was reached
    # from _parse_prior ONLY, one of six parsers, so `helm store confirm`
    # raised AttributeError on heuristic/reference/lexicon entries, where
    # pending_revision was still a str. Every revise arm used a PRIOR id, so
    # 9821 tests passed over it: the control was real and aimed at the one
    # type that worked. A string passed through untouched here is exactly the
    # writer-emits-what-the-reader-drops twin that made `revise` report
    # success and stage nothing in the first place.
    raw = e.get("pending_revision")
    if isinstance(raw, str):
        raw = raw.strip()
        try:
            v = json.loads(raw) if raw and raw not in ("null", "[]") else None
        except Exception:
            v = None
        # FAIL-OPEN TO ABSENT, never to a half-shape: a malformed revision must
        # read as "none staged" rather than as a revision with no statement,
        # which confirm would then land as an empty edit.
        e["pending_revision"] = v if isinstance(v, dict) and v.get("statement") else None
    return e


def _json1(v):
    return json.dumps(v or [], separators=(",", ":"), ensure_ascii=False)


def _pending_lines(e):
    """The staged-revision front-matter line, and ONLY when there IS one.

    ONE owner for this predicate, because the field is optional and the
    round-trip contract pins entry BYTES. `_json1` renders a missing value as
    `[]`, so emitting the line unconditionally writes `pending_revision: []`
    into every entry that has never been revised — which is every entry that
    exists — and test_round_trip_and_byte_shape is exactly the tripwire for
    that. Three of the four writers built the line inline in a list literal
    and the fourth appended it under a guard, so the same field was
    simultaneously mandatory in three types and optional in one. Splice this
    into a literal with `*` rather than copying the guard a fourth time."""
    return (["  pending_revision: " + _json1(e["pending_revision"])]
            if e.get("pending_revision") else [])


def _scope_rank(scope):
    """Lexicon authored-scope precedence: space: > project: > global."""
    if scope and scope.startswith("space:"):
        return 0
    if scope and scope.startswith("project:"):
        return 1
    return 2


_TS_ISO = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?$")
_EPOCH_DIVISORS = {9: 1, 10: 1, 13: 1000, 16: 1000000,
                   19: 1000000000}


def _timestamp_scalar(ts):
    """-> (epoch seconds, None) or (None, reason), for reads AND writes.

    The store inherited four real raw-epoch units: seconds, milliseconds,
    microseconds and nanoseconds. Their digit lengths are unambiguous at the
    dates this corpus can represent; every other numeric length refuses rather
    than silently becoming a far-future sort key. ISO dates/times preserve
    minutes, fractions and offsets instead of validating one meaning and
    sorting another (#145). Naive ISO values keep the historical UTC meaning."""
    raw = str(ts or "").strip()
    if not raw:
        return None, "an empty timestamp is not a timestamp"
    if raw.isdigit():
        divisor = _EPOCH_DIVISORS.get(len(raw))
        if divisor is None:
            return None, ("timestamp %r has an ambiguous epoch width — use "
                          "9/10-digit seconds, 13-digit milliseconds, 16-digit "
                          "microseconds, or 19-digit nanoseconds" % raw)
        return int(raw) / float(divisor), None
    if not _TS_ISO.fullmatch(raw):
        return None, "timestamp %r is not an ISO date/time or epoch" % raw
    try:
        iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        # 3.11 widened fromisoformat; older interpreters refuse a colonless
        # offset (-0700) and any fraction that is not exactly 3 or 6 digits,
        # both of which _TS_ISO deliberately admits — normalize to one
        # microsecond-floor meaning (pad right, truncate past 6): same
        # instant on every supported interpreter
        iso = re.sub(r"\.(\d{1,6})\d*(?=[+-]|$)",
                     lambda m: "." + m.group(1).ljust(6, "0"), iso)
        iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
        stamp = datetime.datetime.fromisoformat(iso)
        stamp = stamp.replace(tzinfo=datetime.timezone.utc) \
            if stamp.tzinfo is None else stamp.astimezone(datetime.timezone.utc)
        return stamp.timestamp(), None
    except (ValueError, OverflowError):
        # OverflowError: a boundary stamp (9999-12-31 / 0001-01-01 with an
        # offset) parses, then overflows the aware-UTC conversion — the same
        # refusal, not a crash
        return None, "timestamp %r is ISO-shaped but not a real date/time" % raw


def _recency(e):
    """One epoch-second scalar from the same grammar the write boundary accepts.

    Unknown/blank legacy values read as 0 (oldest): an existing malformed entry
    stays readable but never wins a slot, while its next rewrite is refused until
    the timestamp is repaired."""
    raw = e.get("last_updated") or e.get("updated_ts") or e.get("stated_ts")
    value, _err = _timestamp_scalar(raw)
    return value if value is not None else 0.0
