#!/usr/bin/env python3
"""helmese — the L2 SEED REGISTER that ships in the repo.

WHAT THIS IS AND IS NOT. helm ships a curated, universal, VERSIONED seed
register so a fresh deployment starts with a designed vocabulary instead of an
empty one. The live per-deployment vocabulary is the GROWN INSTANCE and it lives
in the store (`~/.helm/_global/lexicon`, `~/.helm/<project>/lexicon`), authored
over time by whoever is working. Those are two artifacts with one lineage: the
seed is what the grown instance looked like on day zero. This module NEVER writes
to the store and the store never edits this module.

HELMESE IS A PROJECTION, NEVER A SOURCE OF TRUTH (council amendment 1, MEASURED:
under a dense register comprehension rose 77 vs 62 while ACTION ACCURACY FELL 60
vs 73 — readers understood it better and acted worse when it was authoritative).
Ids, recipients, polarity and lifecycle state live only in the typed dispatch,
chat and ledger fields. A helmese clause is RENDERED from those fields and
decodes back to them. Nothing here is ever the record.

EVERY ENTRY IS EARNED (amendment 5). An operator or role is seeded only when a
real coordination fixture needed it. The register is deliberately small, and the
things left OUT are recorded below with their reasons — an exclusion nobody wrote
down gets re-litigated by the next reader who thinks a symbol looks useful.

VERSION IS IMMUTABLE PER SLICE (amendment 3). Every rendered slice carries the
register version and digest it was written against. A decoder that meets an
UNKNOWN version REFUSES to interpret and falls back to the canonical typed
fields, rather than guessing with a newer table. That refusal is CALLABLE, not
prose: `accepts(stamp)` is the decision, and it was added after a reviewer
observed that the paragraph promised a contract the module did not ship.
`digest()` is computed from the content itself, so a register edited without a
version bump is still detectable, and it covers PROVENANCE too — amendment 4
makes `earned` mandatory, so a rewritten justification is a changed register.
"""
import hashlib

# The register version. BUMP THIS when any seeded entry changes meaning, is
# added, or is retired — the digest catches an edit that forgets, but a version
# is what a decoder can compare and refuse on.
#
# 1.2.0: no entry changed — the DIGEST INPUT did. Fields were colon-joined, so
# text could move across a field boundary without moving the digest. Fields are
# length-framed now, which changes every digest and therefore needs a version a
# decoder can refuse on.
# 1.1.0: two glosses reworded. They carried a semicolon and a parenthetical, so
# the canonical dual form a reader was told to write FAILED the owner rule table
# that told them to write it. A register whose own expansions cannot pass the
# die is not a controlled vocabulary.
VERSION = "1.2.0"


# ---------------------------------------------------------------------------
# OPERATORS — exactly the eight, plus the one admitted transformation
# ---------------------------------------------------------------------------
#
# Each entry carries WHY IT EXISTS, because "mandatory provenance in every
# coordination slice" (amendment 4) applies to the register itself first: a
# vocabulary that cannot say who asked for a symbol cannot ask its users to
# attribute anything.
OPERATORS = (
    {"symbol": "→", "name": "then",
     "gloss": "sequence: the left completes before the right begins",
     "earned": "land ordering — 'gate → review → fold' is the shape every "
               "lane in the fleet already speaks in prose"},
    {"symbol": "⊥", "name": "refuses",
     "gloss": "a hard stop with a named cause, never a soft preference",
     "earned": "guard refusals are the fleet's most common coordination "
               "event and prose spells them a dozen ways"},
    {"symbol": "⊳", "name": "prefer",
     "gloss": "choose the left over the right, both being available",
     "earned": "routing and reviewer selection state a preference between "
               "live options many times per night"},
    {"symbol": "¬", "name": "not",
     "gloss": "negation of the adjacent term only, never of the clause",
     "earned": "'not landed' and 'landed' are different rows; the scope of "
               "the negation is exactly what prose loses"},
    {"symbol": "|", "name": "or",
     "gloss": "alternatives, at least one of which holds",
     "earned": "verdict polarity and routing both enumerate alternatives"},
    {"symbol": "·", "name": "and",
     "gloss": "conjunction of independent facts, order-insensitive",
     "earned": "status lines compose independent measurements constantly"},
    {"symbol": ">", "name": "outranks",
     "gloss": "precedence between two rules or claims that both apply",
     "earned": "'facts-verbatim > register' and 'owner > integrator' are "
               "live rulings that prose renders ambiguously"},
    {"symbol": "μ", "name": "measured",
     "gloss": "this value came from a run, not a recollection",
     "earned": "the provenance rule already demands MEASURED/TRACED/INFERRED; "
               "the register needs the shortest of the three because it is "
               "the one that carries a number"},
    {"symbol": "∴", "name": "therefore",
     "gloss": "the right follows from the left as stated, not as inferred",
     "earned": "a conclusion drawn from a cited measurement is the fleet's "
               "load-bearing sentence shape"},
    {"symbol": "↦", "name": "becomes",
     "gloss": "transformation of one value into another, never mere "
              "sequence of two",
     "earned": "admitted by the council only after the sequence/transform "
               "conflation was named; kept separate BECAUSE the confusion is "
               "the reason it was requested"},
)


# ---------------------------------------------------------------------------
# ROLES — six suffixes, marking a participant's relation to an act
# ---------------------------------------------------------------------------
ROLES = (
    {"suffix": "-lead", "gloss": "owns the act and its outcome",
     "earned": "every lane has exactly one accountable seat and the board "
               "already asks who holds the ball"},
    {"suffix": "-toward", "gloss": "the act is directed at this",
     "earned": "dispatch recipients and land targets"},
    {"suffix": "-from", "gloss": "origin or authority for the act",
     "earned": "verdicts, rulings and citations all name a source"},
    {"suffix": "-with", "gloss": "acts jointly, sharing the outcome",
     "earned": "melds are two seats sharing one result, which no other "
               "role suffix expresses"},
    {"suffix": "-against", "gloss": "opposes or refutes the act",
     "earned": "adversarial review is a first-class relation here; a "
               "refutation is not a weaker form of agreement"},
    {"suffix": "-as", "gloss": "acts in a named capacity, not as itself",
     "earned": "a seat reviewing AS the approval leg differs from the same "
               "seat commenting, and the ledger already distinguishes them"},
)


# ---------------------------------------------------------------------------
# DUAL-FORM SAFETY ENTRIES — registered, never freehanded
# ---------------------------------------------------------------------------
#
# A safety line carries its own plain-English expansion IN the entry, so a
# reader who does not know the register still reads the prohibition. These are
# permitted precisely because they are REGISTERED: an unregistered dense safety
# string is the failure mode this shape exists to prevent.
SAFETY = (
    {"dense": "禁推main", "plain": "never push to main",
     "earned": "the highest-cost irreversible act the fleet can take, and "
               "the one most likely to be attempted under load"},
    {"dense": "禁改史", "plain": "never rewrite landed history",
     "earned": "a rewritten trunk invalidates every in-flight review and "
               "every receipt bound to it"},
)


# ---------------------------------------------------------------------------
# DELIBERATELY NOT SEEDED — with the reason, so it is not re-litigated
# ---------------------------------------------------------------------------
#
# An exclusion nobody wrote down gets re-proposed by the next reader who thinks
# a symbol looks useful. Anything added here later requires a REAL coordination
# fixture that needed it, not an aesthetic argument.
NOT_SEEDED = (
    {"item": "Ω ↻ Θ Σ λ γ κ",
     "why": "no coordination fixture required them; they were proposed for "
            "expressive range, which is the argument that grows a register "
            "past what its readers actually speak"},
    {"item": "decorative brackets",
     "why": "they carry no state and cost a decode step; visual structure is "
            "the renderer's job, not the register's"},
    {"item": "the among/between/across/around family",
     "why": "prose disguised as grammar — each one expands to a relation the "
            "typed fields already hold, so seeding them invites freehanding "
            "state that belongs in a row"},
)


def _framed(*fields):
    """Length-prefixed field framing for the digest input.

    A SEPARATOR IS NOT FRAMING. Joining fields with ':' let text move ACROSS a
    boundary while the joined bytes stayed identical: gloss "sequence: the left
    completes..." with earned E hashes exactly like gloss "sequence" with earned
    " the left completes...:E". The register's MEANING changed, the digest did
    not, and accepts() went on approving the old stamp. Every field now carries
    its own byte length, so no rearrangement can produce the same input."""
    out = bytearray()
    for f in fields:
        b = str(f).encode("utf-8")
        out += b"%d:" % len(b) + b
    return bytes(out)


def digest():
    """A stable content digest of the whole seeded register.

    A version can be forgotten; a digest cannot. A decoder comparing digests
    detects a register edited WITHOUT a version bump, which is the failure a
    version number alone cannot see. Stable across runs and platforms: the
    input is built in declaration order from the entries themselves, never
    from dict iteration order or object identity.

    PROVENANCE IS PART OF THE CONTENT. Amendment 4 makes `earned` mandatory, so
    an entry whose justification changed is a changed entry — omitting it let a
    rewritten provenance ship under an unchanged digest.
    """
    h = hashlib.sha256()
    h.update(_framed("v", VERSION))
    for row in OPERATORS:
        h.update(_framed("op", row["symbol"], row["name"], row["gloss"],
                         row["earned"]))
    for row in ROLES:
        h.update(_framed("role", row["suffix"], row["gloss"], row["earned"]))
    for row in SAFETY:
        h.update(_framed("safe", row["dense"], row["plain"], row["earned"]))
    return h.hexdigest()[:16]


def stamp():
    """What every rendered slice carries: version and digest, together.

    Neither alone is enough — the version is what a decoder REFUSES on, the
    digest is what catches an edit that forgot to bump it.
    """
    return {"register": VERSION, "digest": digest()}


def accepts(stamp):
    """Can THIS table interpret a slice carrying `stamp`? (ok, reason).

    The refusal half of the version law. A decoder meeting an unknown version
    must fall back to the canonical typed fields rather than guess with a newer
    table, and the docstring above promised exactly that — but promising a
    refusal is not shipping one, so this is the callable form.

    Both legs matter and neither substitutes for the other: the VERSION is what
    a decoder compares and refuses on, and the DIGEST catches the edit that
    changed meaning without bumping it. A matching version with a divergent
    digest is the more dangerous case, because it looks safe.
    """
    if not isinstance(stamp, dict):
        return False, "no stamp: a slice without a register stamp is undecodable"
    got_v, got_d = stamp.get("register"), stamp.get("digest")
    if got_v != VERSION:
        return False, ("register version %r is not %r — decode from the typed "
                       "fields instead of guessing with this table"
                       % (got_v, VERSION))
    if got_d != digest():
        return False, ("register %s digest %r is not %r — same version, "
                       "different content, which is the edit a version number "
                       "cannot see" % (VERSION, got_d, digest()))
    return True, ""


def gloss(token):
    """The plain-English expansion of one seeded token, or None.

    DETERMINISTIC BY CONSTRUCTION: a lookup over the shipped table, never a
    model call. The owner adapter glosses ONCE per artifact (amendment 6), and
    a gloss that varied between two renderings of the same card would be worse
    than no gloss — the reader would learn that the expansion is a guess.
    """
    if not isinstance(token, str) or not token:
        return None
    for row in OPERATORS:
        if row["symbol"] == token:
            return row["gloss"]
    for row in ROLES:
        if row["suffix"] == token or token.endswith(row["suffix"]):
            return row["gloss"]
    for row in SAFETY:
        if row["dense"] == token:
            return row["plain"]
    return None
