"""Close-door grammar acceptance invariants over a labelled corpus.

Parser-agnostic: pass ``ref(value, polarity) -> bool``. Mutual exclusion alone
is insufficient: a one-sided precedence guard can classify ambiguous evidence
as one polarity, while refusing everything passes both negative clauses.
"""
import itertools

APPROVE_M = "Subsumption verified "
FIX_M = "Subsumption verified FIX findings were answered on trunk: "
GATE = "gate:0123456789abcdef"
SEPS = [".", "\n", "|", "—"]


def corpus():
    """Yield ``(text, approve|fix|malformed)`` across the grammar space."""
    approve_bodies = ["X landed on trunk 5113755",
                      "the walk descends on trunk abc"]
    fix_bodies = ["rewired at 5113755", "answered by r2"]
    for approve, fix in itertools.product(approve_bodies, fix_bodies):
        one_approve = APPROVE_M + approve
        one_fix = FIX_M + fix
        for separator in SEPS:
            for prefix in ("", GATE + separator + " "):
                yield prefix + one_approve + separator + " " + one_fix, \
                    "malformed"
                yield prefix + one_fix + separator + " " + one_approve, \
                    "malformed"
                yield prefix + one_approve.upper() + separator + " " + one_fix, \
                    "malformed"
                yield prefix + one_approve.lower() + separator + " " + one_fix, \
                    "malformed"
                yield prefix + one_approve, "approve"
                yield prefix + one_fix, "fix"


def mutual_exclusion(ref):
    """No input may be claimed by both polarities."""
    return [text for text, _expected in corpus()
            if ref(text, "approve") and ref(text, "fix")]


def multi_marker_claims(ref):
    """Ambiguous multi-marker input must be claimed by neither polarity."""
    return [text for text, expected in corpus()
            if expected == "malformed"
            and (ref(text, "approve") or ref(text, "fix"))]


def misclassified(ref):
    """Every well-formed input must be claimed by exactly its polarity."""
    bad = []
    for text, expected in corpus():
        if expected == "malformed":
            continue
        other = "fix" if expected == "approve" else "approve"
        if not ref(text, expected) or ref(text, other):
            bad.append((text, expected))
    return bad


def report(ref):
    return {"mutual_exclusion": len(mutual_exclusion(ref)),
            "multi_marker_claims": len(multi_marker_claims(ref)),
            "misclassified": len(misclassified(ref))}
