"""What makes a review INDEPENDENT of the work it reads.

THE ORDER IS CONTEXT, THEN MODEL, THEN FAMILY, and only the first two can
refuse. A review is independent when the reader came to the lane with nothing
of the author's turn in its head and answered with a different model. A
different vendor FAMILY is a preference on top of that — it is printed, it
ranks a candidate, and it never decides the refusal.

WHY THE FIRST TWO AND NOT THE THIRD. The failure a review exists to catch is
the one the author's own context made invisible: a premise the author adopted
early, carried through the whole turn, and stopped testing. A fresh context
window does not hold that premise, so it can see the code the author stopped
seeing. A different model brings a second set of priors on top. A different
family brings a third, and it is the weakest of the three, because two models
of one family that share no context and no weights already disagree about
plenty. Ranking by it is right; refusing on it walls a smarter available
reader out in favour of a duller admitted one.

WHAT THIS MODULE IS NOT. It is not the approval tier. The tier asks whether a
seat's APPROVE is capable of CLOSING a row at all, from the owner's stored
policy; this asks whether THIS reader is independent of THIS author. A seat
can be inside the tier and not independent of one particular lane, and a
perfectly independent reader can sit outside the tier. Both answers are
needed and neither substitutes for the other.

RESOLVED MODELS, NEVER ALIASES. "opus", "fable" and "sonnet" are routing
aliases: the same alias resolves to different weights on different days and
through different proxies, so two turns that both say "opus" prove nothing
about each other. The caller must pass what the runtime evidence RESOLVED —
the provider's own model id. An alias reaching this door is not a weaker
answer than a resolved id, it is a different question, so the checks refuse
on an absent resolved model rather than comparing the labels it came with.

UNKNOWN REFUSES AND NAMES ITS INPUT. Every refusal here says which of the
four inputs it could not obtain or found shared. That matters more than the
verdict: an operator reading "the author's resolved model is unknown" knows
what to go fix, while "not independent" sends them to re-read the code.
"""

# The inputs, in the order they are checked. Each name is the word a refusal
# uses, so the sentence an operator reads is the key they go looking for.
INPUTS = ("seat", "session", "model")

# The three answers. `ok` admits; `shared` is a measured contradiction (two
# sides gave the same value); `unknown` is a missing input. The last two both
# refuse, and they are DISTINCT because they clear differently — `shared`
# clears only by routing to a different reader, `unknown` clears by making
# the runtime record the thing it is not recording.
OK = "ok"
SHARED = "shared"
UNKNOWN = "unknown"

SAME_FAMILY = "same"
OTHER_FAMILY = "other"
UNKNOWN_FAMILY = "unknown"


def _value(side, key):
    """The stripped string at `key`, or None where there is nothing to read.

    A non-string, an empty string and a whitespace-only string are all the
    same fact — the producer did not record this input — and they must reach
    the caller as the same answer, because a refusal that says "unknown" for
    one and compares the other two is a check with a hole in it.
    """
    if not isinstance(side, dict):
        return None
    raw = side.get(key)
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _label(side, default):
    return _value(side, "label") or _value(side, "seat") or default


def independence(author, reviewer):
    """(state, reason, facts) — is this reviewer independent of this author?

    `author` and `reviewer` are mappings carrying `seat`, `session` and
    `model`, plus an optional `family` and `label`. `model` must be the
    RESOLVED model the runtime evidence recorded, never a routing alias.

    -> (OK, None, facts) when the reader is a different seat, on a different
       session, answering with a different resolved model.
    -> (SHARED, reason, facts) when one of those three inputs is the same on
       both sides. The reason names WHICH.
    -> (UNKNOWN, reason, facts) when an input is missing on either side. The
       reason names WHICH input and WHOSE.

    FAMILY IS IN `facts`, NEVER IN THE STATE. `facts["family"]` is "same",
    "other" or "unknown", for printing and for ranking. Nothing here reads it.
    """
    facts = {"family": family_relation(author, reviewer),
             "author": _label(author, "author"),
             "reviewer": _label(reviewer, "reviewer")}
    for key in INPUTS:
        mine = _value(author, key)
        theirs = _value(reviewer, key)
        facts[key] = (mine, theirs)
        if mine is None or theirs is None:
            whose = []
            if mine is None:
                whose.append("the author's")
            if theirs is None:
                whose.append("the reviewer's")
            return UNKNOWN, ("%s resolved %s is unknown, so independence "
                             "cannot be measured — record it rather than "
                             "assume it" % (" and ".join(whose), key)), facts
        if mine == theirs:
            return SHARED, ("author and reviewer share one %s (%s), so this "
                            "read is not independent of the work"
                            % (key, mine)), facts
    return OK, None, facts


def family_relation(author, reviewer):
    """"same" / "other" / "unknown" — A PREFERENCE, never a refusal.

    Printed on the verdict row, and one term of the ranking key. Returned
    from its own function so that no caller can reach it by unpacking an
    independence refusal, which is exactly how a preference becomes a gate.
    """
    mine = _value(author, "family")
    theirs = _value(reviewer, "family")
    if mine is None or theirs is None:
        return UNKNOWN_FAMILY
    return SAME_FAMILY if mine == theirs else OTHER_FAMILY


def rank(author, candidate, cost=0):
    """(key, reason) — where this candidate sorts, and why, LOWER IS BETTER.

    The order the key encodes, most significant first:

      1. FRESH CONTEXT. A candidate sharing the author's seat or session is
         not a second opinion at all, so it sorts behind everything.
      2. MODEL DISTANCE. A different resolved model outranks an unknown one,
         which outranks the author's own model.
      3. FAMILY. Other family preferred, unknown next, same family last, so
         a cross-family reader is taken first between two candidates the
         first two terms could not separate. A caller with its own outer
         terms — a rationed vendor, a walled credential — sorts on those
         first and is right to: this key orders peers, not priorities.
      4. COST TIER, as the caller measures it. Last, because a cheaper
         reader that cannot see the defect saves nothing.

    The reason is the ROW'S OWN SENTENCE and is meant to be printed beside
    it. A ranking nobody can read is re-derived by hand by the next person
    who doubts it, and they will derive it differently.
    """
    state, _why, facts = independence(author, candidate)
    # READ THE INPUTS AGAIN RATHER THAN THE FACTS. `independence` returns as
    # soon as one input refuses, so `facts` carries only the inputs it
    # reached; ranking a candidate whose SEAT already refused would find no
    # session key at all. The ranking must sort every candidate, including
    # the ones the check turned away.
    pairs = [(_value(author, key), _value(candidate, key))
             for key in ("seat", "session")]
    if any(mine is not None and mine == theirs for mine, theirs in pairs):
        fresh, context = 2, "shares the author's seat or session"
    elif any(mine is None or theirs is None for mine, theirs in pairs):
        # UNPROVEN SITS BETWEEN THEM, not with either. Sorting it with the
        # shared case hides a reader nothing is known against; sorting it
        # with the fresh case promotes one on an absence of evidence.
        fresh, context = 1, "context unproven"
    else:
        fresh, context = 0, "fresh context"
    if state == OK:
        model, model_why = 0, "different model"
    elif state == UNKNOWN:
        model, model_why = 1, "model unknown"
    else:
        model, model_why = 2, "same model"
    family = {OTHER_FAMILY: 0, UNKNOWN_FAMILY: 1, SAME_FAMILY: 2}[
        facts["family"]]
    family_why = {OTHER_FAMILY: "other family", UNKNOWN_FAMILY: "family "
                  "unknown", SAME_FAMILY: "same family"}[facts["family"]]
    try:
        tier = int(cost)
    except (TypeError, ValueError):
        tier = 0
    return ((fresh, model, family, tier),
            "%s; %s; %s" % (context, model_why, family_why))


def line(author, reviewer):
    """The one line a surface prints about this pairing.

    ADMITTED or REFUSED first, because that is the answer; the reason and the
    family preference after it, because those are what the reader does next.
    """
    state, why, facts = independence(author, reviewer)
    head = "independence ADMITTED" if state == OK else \
        "independence REFUSED (%s)" % state
    tail = why or "different seat, different session, different resolved model"
    return "%s — %s [family: %s]" % (head, tail, facts["family"])
