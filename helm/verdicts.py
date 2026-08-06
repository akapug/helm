"""Canonical dispatch-verdict polarity vocabulary.

FOUR POLARITIES, AND THE FOURTH AUTHORIZES NOTHING BY CONSTRUCTION.

`approve` / `fix` / `supersede` all speak about WORK: they answer "may this
land", "what must change", "what replaces it". `concur` answers a different
question — "I have read this and I endorse it" — and that question has no
landing attached.

THE ASYMMETRY IT CURES, found by using the ledger for an ARC council on
2026-08-05. `approve` requires a verified gate token, correctly and
permanently: an ungated approve is immutable evidence that could authorize a
land it never earned. `fix` and `supersede` bind ungated, correctly, because
they authorize no land. So before `concur`, THE ONLY UNGATED VERDICTS AVAILABLE
WERE THE DISAPPROVING ONES — on a row whose artifact is not a landable tree, a
seat that AGREED could not record it without minting a whole-suite receipt to
endorse a 133-line markdown file, while a seat that OBJECTED bound for free.

That is a live incentive: agreement gated, objection free. It is the same
attention-budget asymmetry the council named as its missing failure mode,
turning up in the verdict verbs themselves. The cure is NOT to weaken the gate
rule — that rule exists because an ungated approve once authorized a land — but
to give endorsement its own word that promises nothing.

WHY IT IS POWERLESS BY DEFAULT RATHER THAN BY A GUARD. Every close door reads
`_CLOSE_POLARITY[reason]` as a strict allowlist (`polarity not in ...` refuses),
so a polarity named in NO door can open none of them. `concur` is absent from
every tuple, and the suite pins that absence across the WHOLE door set rather
than trusting this sentence. Adding it to a door later is a deliberate act with
a test to delete first — which is the shape a never-authorizes rule should have.
"""

POLARITIES = ("approve", "fix", "supersede", "concur")
POLARITY_FLAGS = frozenset("--" + polarity for polarity in POLARITIES)

# The polarities that speak about landable WORK. `concur` is deliberately
# absent: it records a reader's endorsement and carries no landing claim, so
# nothing downstream may consult it as authorization.
WORK_POLARITIES = ("approve", "fix", "supersede")

# ── BASIS: how the reviewer KNOWS (task/338, owner ask) ─────────────────────
#
# The owner asked for doubt to be legible: "always making legible how much
# doubt should go into a conversation". It was captured as a premise at
# certainty 1.00 and never delivered — 2.34% adoption DECAYING TO ZERO, 221
# verdicts unmarked BY CONSTRUCTION, because `mark_verdict` had no parameter
# for it, the verdict row had no field, and nothing rendered one. The marker
# could not fail to be heeded; it could not be EMITTED. That is the bottom
# rung, not the top one.
#
# A MARKER, NOT A NUMBER, and that is a deliberate narrowing of the original
# request (a 0-100 average over four sub-scores). A percentage invites a
# fabricated precision nobody can audit — "78%" is unfalsifiable — while these
# three are CHECKABLE against what the reviewer actually did: MEASURED means a
# tool ran and produced the finding, and helm/claimev.py already classifies
# claim shapes against tools actually invoked, so MEASURED-with-no-tool-call
# is a detectable lie. Applying the marker to a whole fable ruling cost about
# one word per conclusion and zero deliberation, because an enum is a reflex
# and a percentage is a judgement call on every line.
#
# A REQUIRED ARGUMENT RATHER THAN A RULE, for the reason the measurement
# gives: adoption was 2.34% among the seats that CAN read the premise and
# exactly 0% among every non-Claude family. Prose is not model-family-proof.
# A refusing verb is.
BASES = ("measured", "inferred", "unverified")
BASIS_FLAGS = frozenset("--" + basis for basis in BASES)


def clean_basis(value):
    """(basis, err). None/"" means historical UNMARKED during replay.

    Mirrors clean_polarity exactly, including the rule that matters most: an
    unrecognised string is an ERROR, never quietly coerced. A basis silently
    downgraded to `unverified` would let a typo read as honest doubt, which
    is the one failure this vocabulary cannot afford."""
    if value is None or value == "":
        return None, None
    b = str(value).strip().lower()
    if b not in BASES:
        return None, "verdict basis must be one of %s (got %r)" % (
            "/".join(BASES), value)
    return b, None


def replay_basis(value):
    """Replay is NOT a trust boundary — write-time validation is the gate.
    This is the fail-closed backstop: a hand-edited, forged, or
    future-versioned row carrying an unknown basis reads UNVERIFIED rather
    than borrowing a confidence nobody recorded. The 221 rows written before
    the field existed read UNMARKED (None), which is honest — they were not
    unverified, they were never asked."""
    b, err = clean_basis(value)
    if err:
        return "unverified"
    return b
