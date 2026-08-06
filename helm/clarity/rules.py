#!/usr/bin/env python3
"""helm.clarity.rules — THE one clarity rule table, and the die that stamps it.

WHY ONE TABLE. The published experiment measured the SKILL (the system given
to the model at write time); the die is the LINTER (deterministic, exit 1).
Ship only the linter and you get nagging after the fact; ship only the skill
and you get an unverifiable claim of clarity. They must share one rule table,
or they drift and each becomes evidence for the other's correctness while
neither is checked. Every consumer — `helm clarity check`, `helm clarity
skill`, any future Stop rung — reads RULES below and nothing else.

MEASURED (the published experiment: six writing tasks, four conditions, two
model families, scored as violations per 100 words): baseline Claude 4.36;
a banned-words list 4.21 — a 3% move; the STE skill AS A SYSTEM 1.12. A
prohibition list is not a system, which is why the weakest rule here
(hedge-term, the prohibition list) ships labelled weak instead of quietly
carrying the same authority as the structural rules.

FORKED, NOT INVENTED (the owner's standing reuse law): the sentence
segmentation, code stripping, contraction shape and the marketing/hedge lists
descend from ste-lint.py / ste-writing-skill.md in
github.com/woosal1337/blog, videos/ep01-the-cure-for-ai-slop ("The cure for
AI slop is a 1986 aircraft manual"), which in turn implements the mechanical
subset of ASD-STE100. Two rules are OURS and are the reason this is not a
port: domain-term drift against helm's own curated store lexicon (ASD-STE100
constrains ~900 GENERAL words; our ambiguity lives in our DOMAIN terms, and
we already own and curate that dictionary), and provenance tiers on
load-bearing claims (helm's mark-a-claim-measured-traced-or-inferred move,
written before this frame existed, which is evidence the frame is right).

HARD LIMIT, stated rather than faked: helm is stdlib-only, so there is no POS
tagger here. ASD-STE100's core — the ~900-word approved dictionary (one part
of speech, one meaning per word), noun-cluster caps, verb-form and
active-voice rules — CANNOT be checked deterministically without one. Those
rules do not exist in this table. The upstream BANNED general-vocabulary list
("commence", "utilize", …) is deliberately not forked either: a word list
pretending to be the dictionary rule is a guard that measures nothing.

ONE MORE HONESTY SEAM: one-instruction-per-sentence is APPROXIMATE (imperative
verb + conjunction + second imperative, over a closed verb list) and is
reported as ADVISORY, never as a violation — it must not pretend to be exact.
"""
import re

# ---------------------------------------------------------------------------
# limits — STE 4.1 / 5.1
# ---------------------------------------------------------------------------

# owner mode carries the strict cap, not the descriptive one: an owner-bound
# card is read once, under load, by someone who did not write it — the same
# conditions that earned instructions the tighter limit.
SENTENCE_MAX = {"strict": 20, "descriptive": 25, "owner": 20}  # STE rule 4.1
PARAGRAPH_MAX_SENTENCES = 6                       # STE rule 5.1

# ---------------------------------------------------------------------------
# segmentation — forked from upstream ste-lint.py, then made WRAP-AWARE
# ---------------------------------------------------------------------------

# An unterminated fence is still a fence (the punt-detector learned this
# against a real transcript). Fences are replaced by their own newlines so
# line numbers survive; inline code by a space so words do not fuse.
_FENCE = re.compile(r"```.*?```|```.*", re.S)
_INLINE_CODE = re.compile(r"`[^`]*`")
_HEADING = re.compile(r"^\s*#{1,6}\s*")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENT_SPLIT = re.compile(r"(?<=[.!?:])\s+(?=[A-Z0-9\"'\-(])")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-/]*")


def strip_code(text):
    text = _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text or "")
    return _INLINE_CODE.sub(" ", text)


def parse(text):
    """The document model every check reads: a list of paragraphs, each a list
    of (line, sentence) pairs.

    WRAP-AWARE, and this is not pedantry: upstream splits per LINE, which is
    correct for raw model output but blind on anything hard-wrapped at 72
    columns — i.e. every commit message and most docs, the exact corpus this
    die is aimed at. A wrapped 40-word sentence read per-line becomes two
    innocent 20s and the length rule never fires (the positive control below
    is such a commit message). So continuation lines JOIN the open segment;
    a blank line ends the paragraph; a heading or list item starts its own
    segment (a list item is one unit even without a terminator)."""
    lines = strip_code(text).split("\n")
    paras, cur, seg = [], [], None   # seg: [(line, piece), ...]

    def close_seg():
        nonlocal seg
        if seg:
            cur.append(seg)
            seg = None

    def close_para():
        nonlocal cur
        close_seg()
        para = []
        for pieces in cur:
            marks, buf, off = [], [], 0
            for line, piece in pieces:
                marks.append((off, line))
                buf.append(piece)
                off += len(piece) + 1        # the joining space
            joined = " ".join(buf)
            pos = 0
            for s in _SENT_SPLIT.split(joined):
                s = s.strip()
                if not s:
                    continue
                at = joined.find(s, pos)
                pos = at + len(s)
                # marks ascend in both offset and line: the last mark at or
                # before the sentence start is the line the sentence starts on
                para.append((max(l for o, l in marks if o <= at), s))
        if para:
            paras.append(para)
        cur = []

    for i, rawline in enumerate(lines, 1):
        stripped = rawline.strip()
        if not stripped:
            close_para()
            continue
        heading = bool(_HEADING.match(stripped))
        item = bool(_LIST.match(stripped))
        body = _LIST.sub("", _HEADING.sub("", stripped))
        if not body:
            continue
        if heading:                  # one line by definition; never joined
            close_seg()
            cur.append([(i, body)])
        elif item or seg is None:    # a new list item or a fresh segment
            close_seg()
            seg = [(i, body)]
        else:                        # hard-wrap continuation joins
            seg.append((i, body))
    close_para()
    return paras


def words(sentence):
    return _WORD.findall(sentence)


def word_count(text):
    return sum(len(words(s)) for para in parse(text) for _, s in para)


def _clip(s, n=90):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "..."


# ---------------------------------------------------------------------------
# rule mechanisms
# ---------------------------------------------------------------------------

def _check_sentence_length(doc, ctx):
    limit = SENTENCE_MAX[ctx["mode"]]
    out = []
    for para in doc:
        for line, s in para:
            n = len(words(s))
            if n > limit:
                out.append((line, "%d words (max %d in %s mode): %s"
                            % (n, limit, ctx["mode"], _clip(s))))
    return out


def _check_paragraph_length(doc, ctx):
    out = []
    for para in doc:
        if len(para) > PARAGRAPH_MAX_SENTENCES:
            out.append((para[0][0], "paragraph has %d sentences (max %d)"
                        % (len(para), PARAGRAPH_MAX_SENTENCES)))
    return out


def _check_semicolon(doc, ctx):
    return [(line, "semicolon — write two sentences: " + _clip(s))
            for para in doc for line, s in para if ";" in s]


# 't/'re/'ve/'ll/'d/'m are contractions unconditionally. 's is AMBIGUOUS —
# "the seat's home" is a possessive, not a contraction, and upstream's blanket
# \w+'s flagged every possessive in sight — so 's counts only on the closed
# pronoun/demonstrative stems where it can only mean "is"/"us".
_CONTRACTION = re.compile(
    r"\b\w+['’](?:t|re|ve|ll|d|m)\b"
    r"|\b(?:it|that|there|here|what|who|let|he|she|one|this"
    r"|everything|nothing|something)['’]s\b", re.I)


def _check_contraction(doc, ctx):
    out = []
    for para in doc:
        for line, s in para:
            for m in _CONTRACTION.finditer(s):
                out.append((line, "contraction '%s' — write the words out"
                            % m.group(0)))
    return out


# APPROXIMATE by construction (no POS tagger): an imperative-led sentence that
# chains a second imperative with "and"/"then". The verb list is closed and
# small on purpose — coordination-traffic command verbs, not English.
IMPERATIVES = frozenset("""
    add apply check claim clear close commit file fix install kill land
    launch measure merge open post push read rebase relaunch release remove
    report rerun restart resume review run send set ship split start stop
    update use verify wire write
""".split())

_CHAIN = re.compile(r"[,;]?\s+\b(?:and|then)\s+([A-Za-z]+)", re.I)


def _check_one_instruction(doc, ctx):
    out = []
    for para in doc:
        for line, s in para:
            toks = words(s)
            if not toks or toks[0].lower() not in IMPERATIVES:
                continue
            for m in _CHAIN.finditer(s):
                if m.group(1).lower() in IMPERATIVES:
                    out.append((line, "may chain more than one instruction "
                                "(approximate): " + _clip(s)))
                    break
    return out


# THE WEAKEST RULE, shipped labelled: a term list is exactly the intervention
# the experiment measured at a 3% move. It stays because a hedge word in a
# verdict is still a hedge word — but its findings carry the weak label so a
# reader never mistakes list-hygiene for the system.
MARKETING = (
    "seamless", "seamlessly", "robust", "powerful", "cutting-edge",
    "effortless", "effortlessly", "world-class", "next-generation",
    "revolutionary", "blazing", "lightning-fast", "delightful", "turnkey",
    "best-in-class", "state-of-the-art", "game-changing", "first-class",
    "battle-tested", "enterprise-grade", "supercharge", "unlock", "unleash",
    "empower", "empowers",
)
HEDGES = (
    "it is important to note", "it should be noted", "it is worth noting",
    "please note that", "as mentioned previously", "as noted above",
    "potentially", "basically", "essentially", "arguably",
    "generally speaking",
)
HEDGE_TERMS = MARKETING + HEDGES

_HEDGE_PATTERNS = tuple(
    (t, re.compile(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])"))
    for t in HEDGE_TERMS)


def _check_hedge(doc, ctx):
    out = []
    for para in doc:
        for line, s in para:
            low = s.lower()
            for term, pat in _HEDGE_PATTERNS:
                if pat.search(low):
                    out.append((line, "hedge/marketing term '%s': %s"
                                % (term, _clip(s))))
    return out


# ---------------------------------------------------------------------------
# STE-AI-06 — vague quantifiers, the rule this table was missing
# ---------------------------------------------------------------------------
#
# ASD-STE100 bans approximate quantity words where an exact number is knowable.
# It belongs here for a reason this fleet measured rather than inherited: a
# coordination surface that says "several rows are stale" costs the reader the
# one thing they need, which is WHICH and HOW MANY, and it reads as a finding
# either way. Tonight a raw branch count would have reported 97 pieces of debt
# where the real tail was 11 — a vague quantifier is the same failure with the
# number removed entirely.
#
# ERROR, not advisory: unlike one-instruction (which needs a POS tagger to judge
# and honestly reports approximate), this is a closed word list matched on word
# boundaries. It has no false-positive class that a writer cannot fix by
# counting.
VAGUE_QUANTIFIERS = (
    "a number of", "a few", "a couple of", "several", "various", "numerous",
    "many", "much of", "most of", "some of", "a lot of", "lots of",
    "a handful of", "multiple", "countless", "myriad",
)

_VAGUE_PATTERNS = tuple(
    (t, re.compile(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])"))
    for t in VAGUE_QUANTIFIERS)


def _check_vague_quantifier(doc, ctx):
    out = []
    for para in doc:
        for line, s in para:
            low = s.lower()
            for term, pat in _VAGUE_PATTERNS:
                if pat.search(low):
                    out.append((line, "vague quantifier '%s' — say the number: %s"
                                % (term, _clip(s))))
    return out


# ---------------------------------------------------------------------------
# OURS #1 — domain-term drift against helm's curated lexicon
# ---------------------------------------------------------------------------

# Synonyms the fleet has actually drifted to, keyed by the lexicon term they
# drift FROM. A row is ACTIVE only when its term is live in the loaded
# lexicon — the store stays the authority on which terms exist; this table is
# the authority on what counts as a synonym (the same split as ASD-STE100's
# dictionary, which lists the banned synonym next to the approved word).
# Grown by observation, never by guessing.
DRIFT_SYNONYMS = {
    "de-meld": ("unmeld", "un-meld"),
    "cli-proxy": ("proxy shim",),
}

# Space-variants that are ordinary English predicates, not the coinage: "this
# is already done" does not misuse the verdict tag "already-done", and a rule
# that flagged it would be switched off within a day. Curated, commented,
# visible — the ASD-STE100 move of giving a word an approved OTHER meaning.
DRIFT_EXEMPT = frozenset((
    "already done",     # ordinary predicate; the lexicon coinage is the tag
    "as public",        # "treat it as public" is plain English
))


def term_variants(term):
    """The deterministic misspellings of a compound term: the same words
    joined by space, hyphen, or nothing. 'cli-proxy' -> 'cli proxy',
    'cliproxy'. A single-word term has no variants (nothing checkable)."""
    parts = [p for p in re.split(r"[-\s]+", term) if p]
    if len(parts) < 2:
        return ()
    forms = {" ".join(parts), "-".join(parts), "".join(parts)}
    forms.discard(term)
    return tuple(sorted(f for f in forms if f not in DRIFT_EXEMPT))


def _drift_patterns(lexicon):
    pats = []
    for term in sorted(lexicon):
        alts = list(term_variants(term))
        alts += [s for s in DRIFT_SYNONYMS.get(term, ()) if s.lower() != term]
        for alt in alts:
            pats.append((term, alt, re.compile(
                r"(?<![a-z0-9])" + re.escape(alt.lower()) + r"(?![a-z0-9])")))
    return pats


def _check_lexicon_drift(doc, ctx):
    lexicon = ctx.get("lexicon") or {}
    if not lexicon:
        return []
    out = []
    pats = _drift_patterns(lexicon)
    for para in doc:
        for line, s in para:
            low = s.lower()
            for term, alt, pat in pats:
                if pat.search(low):
                    out.append((line, "'%s' drifts from lexicon term '%s' — "
                                "one name for one thing: %s"
                                % (alt, term, _clip(s))))
    return out


# ---------------------------------------------------------------------------
# OURS #2 — provenance tiers on load-bearing claims
# ---------------------------------------------------------------------------

# The tier vocabulary of helm's mark-a-claim-measured-traced-or-inferred move.
_TIER = re.compile(r"\b(measured|traced|inferred)\b", re.I)

# A load-bearing claim, deterministically: a quantitative assertion (number +
# unit, ratio, or from-N-to-M) or a verification verdict. Deliberately tight —
# a provenance rule that fires on every digit becomes wallpaper.
_QUANT = re.compile(
    r"\b\d[\d,.]*\s*(%|percent|ms|s|sec|seconds?|min|minutes?|hours?|days?|"
    r"kb|mb|gb|bytes?|tokens?|words?|lines?|files?|tests?|seats?|sentences?|"
    r"violations?|messages?|rounds?|passed|failed|skipped)(?![a-z])", re.I)
_RATIO = re.compile(r"\b\d+\s*(?:/|of)\s*\d+\b")
_RANGE = re.compile(r"\bfrom\s+\d[\d,.]*\s+to\s+\d[\d,.]*\b", re.I)
_VERDICT = re.compile(
    r"\b(tests? pass(?:es|ed)?|suite (?:is )?green|verified|confirmed|"
    r"reproduced|no regressions?|works end.to.end)\b", re.I)

# A spec is not an empirical claim: "max 20 words" states a limit, it does not
# report a measurement, and a provenance nag on every stated limit would teach
# readers to skip the rule (this module's own docstring would trip it).
_NORMATIVE = re.compile(
    r"\b(max|maximum|min|minimum|limit|cap|capped|budget|at most|up to|"
    r"must|should|shall)\b", re.I)


def _check_provenance(doc, ctx):
    out = []
    for para in doc:
        if _TIER.search(" ".join(s for _, s in para)):
            continue        # the tier covers its paragraph, per the move's
            # real usage: "MEASURED: suite green. Runtime 41s." — the second
            # sentence inherits the opener's tier
        for line, s in para:
            if s.endswith("?") or _NORMATIVE.search(s):
                continue
            if _QUANT.search(s) or _RATIO.search(s) or _RANGE.search(s) \
                    or _VERDICT.search(s):
                out.append((line, "load-bearing claim carries no provenance "
                            "tier (MEASURED / TRACED / INFERRED): " + _clip(s)))
    return out


# ---------------------------------------------------------------------------
# OWNER MODE — the L3 adapter rules (helmese draft-2, amendment 6)
# ---------------------------------------------------------------------------
#
# These run ONLY in owner mode. Coordination text between agents may carry the
# register bare — that is what the register is for. The owner never agreed to
# learn it, so the same symbol reaching HIM must arrive glossed.

# FACTS-VERBATIM OUTRANKS THE REGISTER (amendment 6). Verdict evidence, quoted
# output, and cited strings are EXEMPT: their author is forbidden to rewrite
# them, so a die that flagged them would demand an edit the rules prohibit.
# WHAT COUNTS AS QUOTED, and — just as load-bearing — what does not.
#
# Straight double quotes, typographic quotes of both weights, backticks, and
# Markdown blockquote lines. STRAIGHT SINGLE QUOTES ARE DELIBERATELY OUT: the
# apostrophe is the same character, so "the gate's ⊥ fired" would open a span
# that swallows the rest of the line and silently exempt text nobody quoted. A
# false EXEMPTION is the dangerous direction — it HIDES the rule rather than
# firing it — and helm ships no POS tagger. Recorded in NOT_CHECKABLE beside
# the ASCII operators, for the same reason.
_VERBATIM_RE = re.compile(
    r'"[^"]*"'
    r'|\u2018[^\u2019]*\u2019'
    r'|\u201c[^\u201d]*\u201d'
    r'|(?P<bt>`+)(?!`)(?:(?!(?P=bt))[\s\S])*?(?P=bt)(?!`)'
    r'|^[ \t]*>[^\n]*', re.M)


def _speakable(s):
    """`s` with every quoted span blanked, preserving length AND newlines so
    line numbers survive. This is the text the author actually controls.

    Applied to the WHOLE text before segmentation, never per sentence: a quote
    can span sentence boundaries, and masking afterwards left the tail of
    'Evidence: "Gate. Next ⊥ review".' looking like unquoted prose."""
    def blank(m):
        return "".join("\n" if c == "\n" else " " for c in m.group(0))
    return _VERBATIM_RE.sub(blank, s)


# Which rules are owner-mode only. ONE place owns this fact — check_text skips
# them elsewhere and `helm clarity rules` labels them from the same set.
OWNER_ONLY = frozenset(("gloss-once", "mechanism-first"))

# A gloss is the registered dual form: the symbol, then its plain expansion in
# parentheses — exactly the shape helmese.SAFETY already ships
# ("\u7981\u63a8main (never push to main)").
_GLOSSED_RE_CACHE = {}


def _glossed_pattern(token):
    """token + its parenthetical, CAPTURED. Matching the shape alone accepted
    '⊥ (banana)' and, worse, '禁推main (push to main now)' — an INVERTED safety
    gloss. A false expansion is worse than none, so the content is compared."""
    pat = _GLOSSED_RE_CACHE.get(token)
    if pat is None:
        pat = re.compile(re.escape(token) + r"\s*\(([^)]*)\)")
        _GLOSSED_RE_CACHE[token] = pat
    return pat


_TOKEN_RE_CACHE = {}


def _token_pattern(token, wordlike):
    """Where a register token really occurs.

    THE BOUNDARY FOLLOWS THE ENTRY KIND, not the token's own edge characters.
    A dense SAFETY entry is a WORD and is guarded on BOTH sides; a symbolic
    operator is unbounded, because '⊥' must still match pressed tight against
    surrounding text, which is how the register is actually written.

    Deriving the guard from the characters instead was asymmetric and wrong in
    a way one example hid: 禁推main ends in ASCII so it got a trailing guard and
    `禁推mainland` was fixed, but it BEGINS with a non-ASCII character so it got
    no leading guard and `x禁推main` still matched. 禁改史 is non-ASCII at both
    ends and so was guarded on neither. The kind is knowable; the edge
    character is a proxy for it, and a bad one."""
    key = (token, wordlike)
    pat = _TOKEN_RE_CACHE.get(key)
    if pat is None:
        body = re.escape(token)
        if wordlike:
            body = r"(?<![A-Za-z0-9_])" + body + r"(?![A-Za-z0-9_])"
        pat = re.compile(body)
        _TOKEN_RE_CACHE[key] = pat
    return pat


def _norm_gloss(text):
    """Comparison form: whitespace collapsed, case folded, edge punctuation
    dropped. Deliberately NOT a fuzzy match — the register is the authority on
    what a symbol means, and 'close enough' is how a wrong expansion ships."""
    return " ".join((text or "").split()).casefold().strip(" .,;:!?")


def _dense_tokens():
    """The register tokens an owner cannot decode AND a linter can identify.

    TWO SCOPE LIMITS, both stated rather than silently held:

    ASCII OPERATORS ARE OUT ('|' and '>'). They are indistinguishable from
    ordinary punctuation without a parser — '>' appears in every diff, quoted
    reply, and comparison, and '|' in every table and pipeline. Measured while
    building this rule: the first version flagged the string
    "<object object at 0x...>" as an unglossed register symbol. helm is
    stdlib-only and ships no POS tagger, so this is not a gap to be closed
    later by trying harder; it is the same honest absence NOT_CHECKABLE
    already records for the STE dictionary rules.

    ROLES ARE OUT. A role rides on a name ('codex-2-against') and reads as
    English, so glossing it would add noise without removing opacity."""
    from .. import helmese
    out = [(r["symbol"], False) for r in helmese.OPERATORS]
    out += [(r["dense"], True) for r in helmese.SAFETY]
    return tuple((t, wordlike) for t, wordlike in out if not t.isascii())


def _check_gloss_once(doc, ctx):
    """FOUR ways a register symbol reaches the owner wrong, one cure: write the
    registered dual form exactly once, BEFORE using it bare.

      WRONG    the parenthetical is not what the register says. '⊥ (banana)'
               passed a shape-only check, and so did an inverted safety gloss.
      ABSENT   no expansion anywhere.
      LATE     glossed, but only AFTER a bare use. The rule is "gloss once,
               THEN bare"; a reader who met the bare symbol first was not
               helped by an expansion further down.
      REPEATED glossed more than once. Counted per OCCURRENCE, not per
               sentence.

    Reads the QUOTE-MASKED document, selected BY KEY: a wholly-quoted artifact
    masks to an EMPTY document, and choosing it by truthiness fell back to the
    raw text exactly where everything was quoted."""
    if ctx["mode"] != "owner":
        return []
    from .. import helmese
    doc = ctx["owner_doc"] if "owner_doc" in ctx else doc
    seen, first_bare, first_good, good_n, bad = {}, {}, {}, {}, {}
    order = 0
    for para in doc:
        for line, sent in para:
            order += 1
            for tok, wordlike in _dense_tokens():
                occurrences = list(_token_pattern(tok, wordlike).finditer(sent))
                if not occurrences:
                    continue
                seen.setdefault(tok, line)
                want = _norm_gloss(helmese.gloss(tok))
                heads = {}
                for m in _glossed_pattern(tok).finditer(sent):
                    heads[m.start()] = m.group(1)
                for occ in occurrences:
                    at = (order, occ.start())
                    if occ.start() in heads:
                        if _norm_gloss(heads[occ.start()]) == want:
                            good_n[tok] = good_n.get(tok, 0) + 1
                            if tok not in first_good or at < first_good[tok]:
                                first_good[tok] = at
                                good_second = None
                        else:
                            bad.setdefault(tok, (line, heads[occ.start()]))
                    elif tok not in first_bare or at < first_bare[tok]:
                        first_bare[tok] = at
    out = []
    for tok, line in seen.items():
        want = helmese.gloss(tok) or "its plain form"
        if tok in bad:
            bline, got = bad[tok]
            out.append((bline, "register symbol '%s' is glossed WRONG: the "
                        "register says %r, not %r" % (tok, want, got.strip())))
        elif tok not in first_good:
            out.append((line, "register symbol '%s' reaches the owner "
                        "unglossed \u2014 write '%s (%s)' once, then use it bare"
                        % (tok, tok, want)))
        elif tok in first_bare and first_bare[tok] < first_good[tok]:
            out.append((line, "register symbol '%s' is used bare BEFORE it is "
                        "glossed \u2014 the first reader meets it undefined; "
                        "gloss it at its first appearance" % tok))
        elif good_n.get(tok, 0) > 1:
            out.append((line, "register symbol '%s' is glossed %d times "
                        "\u2014 gloss once per artifact, not per sentence"
                        % (tok, good_n[tok])))
    return sorted(out)


# Verbs that open on the MECHANISM. Not banned words: they are correct in the
# body. The rule is about the FIRST sentence, which is the only one a scanning
# owner is guaranteed to read.
MECHANISM_OPENERS = (
    "refactored", "rewrote", "wired", "implemented", "added", "removed",
    "patched", "migrated", "plumbed", "hooked", "extracted", "renamed",
    "bumped",
)

# DELIBERATELY NOT OPENERS: "landed", "merged", "committed". In this fleet a
# land IS the outcome — the board exists to announce them and carries 182 such
# rows — so "Landed X @ sha" is the news, not the mechanism behind it. Found by
# dogfooding the advisory through `helm note set` on real fleet text rather than
# by review: flagging the single most common owner-bound sentence in the repo
# would have made this rule noise on its first day, and a noisy advisory gets
# switched off before anyone reads its true findings.
NOT_MECHANISM = ("landed", "merged", "committed")


def _check_mechanism_first(doc, ctx):
    """ADVISORY by construction. 'Outcome-first' is a judgment the die can
    only approximate, and an approximate rule that blocks is a rule that gets
    switched off (the same call one-instruction already made)."""
    if ctx["mode"] != "owner":
        return []
    for para in (ctx["owner_doc"] if "owner_doc" in ctx else doc):
        for line, s in para:
            first = s.strip().lstrip("*-# ").lower()
            for verb in MECHANISM_OPENERS:
                if first.startswith(verb):
                    return [(line, "opens on mechanism ('%s') \u2014 lead with "
                             "what changed FOR THE READER, then how: %s"
                             % (verb, _clip(s)))]
            return []          # only the artifact's first sentence is judged
    return []


# ---------------------------------------------------------------------------
# THE TABLE — the die. Both consumers (check + skill) iterate this and only
# this; a rule not in this table does not exist.
# ---------------------------------------------------------------------------

RULES = (
    {"id": "sentence-length", "severity": "error", "source": "STE 4.1",
     "weak": False,
     "guidance": "Write short sentences: max %d words for an instruction "
                 "(strict mode) or an owner-bound card (owner mode), max %d "
                 "for a descriptive sentence."
                 % (SENTENCE_MAX["strict"], SENTENCE_MAX["descriptive"]),
     "check": _check_sentence_length},
    {"id": "paragraph-length", "severity": "error", "source": "STE 5.1",
     "weak": False,
     "guidance": "Keep a paragraph to one topic and at most %d sentences."
                 % PARAGRAPH_MAX_SENTENCES,
     "check": _check_paragraph_length},
    {"id": "no-semicolon", "severity": "error", "source": "skill",
     "weak": False,
     "guidance": "Do not use semicolons. Write two sentences.",
     "check": _check_semicolon},
    {"id": "no-contraction", "severity": "error", "source": "skill",
     "weak": False,
     "guidance": "Do not use contractions. Write the words out.",
     "check": _check_contraction},
    {"id": "one-instruction", "severity": "advisory", "source": "STE 4.2",
     "weak": False,
     "guidance": "Write one instruction per sentence. Do not chain steps "
                 "with 'and' or 'then'. (The check for this is approximate "
                 "and reports advisories, never violations.)",
     "check": _check_one_instruction},
    {"id": "hedge-term", "severity": "error", "source": "skill",
     "weak": True,
     "guidance": "Do not use hedge or marketing terms (%s, ...). This is the "
                 "weakest rule — a banned-word list moved the measured "
                 "experiment 3%% — and its findings carry a [weak] label."
                 % ", ".join(HEDGE_TERMS[:3]),
     "check": _check_hedge},
    {"id": "vague-quantifier", "severity": "error", "source": "STE AI-06",
     "weak": False,
     "guidance": "Do not use an approximate quantity word (%s, ...) where the "
                 "number is knowable. Count it and say the number — 'several "
                 "rows are stale' costs the reader exactly what they needed."
                 % ", ".join(VAGUE_QUANTIFIERS[:3]),
     "check": _check_vague_quantifier},
    {"id": "lexicon-drift", "severity": "error", "source": "helm",
     "weak": False,
     "guidance": "Use the lexicon term, exactly, for a thing the lexicon "
                 "names — one name for one thing. The helm store's curated "
                 "lexicon is the controlled vocabulary.",
     "check": _check_lexicon_drift},
    {"id": "provenance", "severity": "error", "source": "helm",
     "weak": False,
     "guidance": "Mark every load-bearing claim MEASURED (I ran it, here is "
                 "the output), TRACED (I read the code path), or INFERRED "
                 "(I reasoned from a premise).",
     "check": _check_provenance},
    {"id": "gloss-once", "severity": "error", "source": "helmese am.6",
     "weak": False,
     "guidance": "Owner mode: gloss a register symbol ONCE per artifact, in "
                 "the registered dual form, then use it bare. The expansion "
                 "must MATCH the register, not merely be present. Quoted "
                 "evidence is exempt: double, typographic, backtick and "
                 "blockquote forms, but not straight single quotes.",
     "check": _check_gloss_once},
    {"id": "mechanism-first", "severity": "advisory", "source": "helmese am.6",
     "weak": False,
     "guidance": "Owner mode: open on the OUTCOME, not the mechanism. The "
                 "first sentence is the only one a scanning reader is "
                 "guaranteed to read. (Approximate; advisory only.)",
     "check": _check_mechanism_first},
)

# What this table CANNOT contain, so no reader infers it was covered: the
# approved-word dictionary, noun-cluster caps, verb-form and active-voice
# rules all need a POS tagger, and helm is stdlib-only by promise.
NOT_CHECKABLE = ("approved-word dictionary (needs POS per token)",
                 "noun-cluster limit (needs POS sequences)",
                 "verb-form / active-voice rules (needs a parse)",
                 "gloss-once on the ASCII register operators '|' and '>' "
                 "(indistinguishable from ordinary punctuation without a "
                 "parser; the non-ASCII symbols ARE checked)",
                 "the verbatim exemption on STRAIGHT SINGLE quotes (the "
                 "apostrophe is the same character; double, typographic, "
                 "backtick and blockquote forms ARE exempt)")


def check_text(text, mode="descriptive", lexicon=None):
    """Run the whole table over `text`. Returns findings, each
    {rule, severity, weak, line, message}, line-ordered. The caller supplies
    the lexicon ({term: definition}) — this module never touches the store,
    so the die itself stays pure and hermetically testable."""
    if mode not in SENTENCE_MAX:
        raise ValueError("unknown mode %r (%s)"
                         % (mode, "|".join(sorted(SENTENCE_MAX))))
    doc = parse(text)
    ctx = {"mode": mode, "lexicon": lexicon or {}}
    if mode == "owner":
        # ONE extra parse, owner mode only: the owner rules must not read
        # quoted evidence, and the mask has to precede segmentation.
        ctx["owner_doc"] = parse(_speakable(text))
    out = []
    for rule in RULES:
        # Owner-mode rules do not judge agent-to-agent text: the register is
        # exactly what agents are meant to speak bare between themselves.
        if rule["id"] in OWNER_ONLY and mode != "owner":
            continue
        for line, msg in rule["check"](doc, ctx):
            out.append({"rule": rule["id"], "severity": rule["severity"],
                        "weak": rule["weak"], "line": line, "message": msg})
    out.sort(key=lambda f: (f["line"], f["rule"]))
    return out
