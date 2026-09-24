"""A READING: an answer, the confidence it was reached with, and what was
looked at to reach it. The value type the beacon consumption verdict is
threaded through.

THE DEFECT THIS EXISTS TO END, stated as one mechanism rather than as a list.
Deciding whether a seat is being woken runs through nine stages in three
modules -- the sample of pending rows (`seats_stop_fp._pending_all`), the wait
it implies (`beacons.undrained`), the verdict (`beacons._verdict`), the
reconciliation against a standing alarm (`beacons.attend`, `drain_proven`), the
repair decision (`beacons.escalate`), the authority to act, the keystroke, the
settlement and the disclosure. EVERY STAGE RECEIVED THE PREVIOUS STAGE'S OUTPUT
AS A BARE VALUE AND RE-INFERRED WHAT IT MEANT: a duration, a hit list, an
action string, each handed on without the evidence that produced it. So each
consumer re-derived meaning from PRESENCE or ABSENCE, and every one of them
reached for the reassuring reading.

THE FAILURES COME IN PAIRS, AND THE PAIR IS THE SIGNATURE. Consuming the last
row cannot clear an alarm, and a rotation that never reached the room clears it
anyway: the SAME missing fact producing OPPOSITE errors, one stage refusing a
real drain while the stage beside it invents one. That is what an
unrepresentable "I could not tell" looks like from the outside, and a cure
aimed at either half leaves the other.

THE RULE THAT MAKES THIS MORE THAN A WRAPPER. A reading derived from an
UNKNOWN reading may not claim PROVEN or REFUTED on the strength of the prior --
it must supply evidence OF ITS OWN, and `derive` refuses at the call site if it
does not. Certainty is manufactured downstream of an absence, or it is not
manufactured at all. Everything else here is bookkeeping in service of that one
refusal.

WHY THE CHECKS ARE IN `__new__` AND NOT IN A TEST. A validator that runs only
in the suite admits the bad object for the eight minutes a gate takes and for
the whole of production. The same law the chat store learned: validate at the
WRITE door, so the malformed value never exists rather than being noticed
later.

PRIOR ART, AND A DUPLICATION NAMED RATHER THAN HIDDEN. `helm.rowstate` reached
this shape first for a different object -- `Derivation(state, evidence,
unknown)`, with "evidence is never empty except for UNKNOWN" written in its
docstring. This module is that pattern applied to the consumption verdict, with
two differences: the invariant is ENFORCED here (rowstate's is documented and a
caller may still build an evidence-free claim), and confidence is explicit
rather than implied by the state. `Evidence` is therefore spelled twice in this
tree on purpose: making beacons depend on the land-request subsystem to borrow
a value type would be a worse edge than the duplication. Unifying them is its
own lane and its own review.
"""

# The three answers a stage may give. UNKNOWN is not a soft REFUTED: REFUTED is
# a measured negative, UNKNOWN means the instrument could not answer, and
# collapsing them is exactly how a gap in sampling became a fact about the
# world. Spelled as the tree spells it everywhere else -- rowstate, foldcheck,
# seat_ledger and seat_reachability all use this uppercase form.
PROVEN = "PROVEN"
REFUTED = "REFUTED"
UNKNOWN = "UNKNOWN"

CONFIDENCES = (PROVEN, REFUTED, UNKNOWN)


class Evidence(tuple):
    """One thing that was looked at, `(kind, detail)`.

    A tuple so it compares and hashes by value: an arm asserts on the evidence
    a stage produced, never on prose that happens to mention it."""

    __slots__ = ()

    def __new__(cls, kind, detail=None):
        kind = str(kind).strip()
        if not kind:
            raise ValueError("evidence with no kind says nothing was looked at")
        return tuple.__new__(cls, (kind, detail))

    @property
    def kind(self):
        return self[0]

    @property
    def detail(self):
        return self[1]

    def __repr__(self):
        return "Evidence(%r, %r)" % (self[0], self[1])


def _evidence(items):
    """Normalise whatever a caller passed into a tuple of `Evidence`.

    A bare `(kind, detail)` pair is admitted because the alternative is call
    sites that read as ceremony; anything else is refused BY NAME rather than
    coerced into a shape that would silently lose it."""
    out = []
    for item in items or ():
        if isinstance(item, Evidence):
            out.append(item)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            out.append(Evidence(item[0], item[1]))
        else:
            raise TypeError(
                "evidence must be Evidence or a (kind, detail) pair, got %r"
                % (item,))
    return tuple(out)


class Reading(tuple):
    """`(question, answer, confidence, evidence, lacked)` -- a stage's output
    with the grounds for it attached, so the next stage never has to guess.

    `question` says what was asked, in the stage's own words. Without it a
    reader holding an answer of `None` cannot tell whether it means "nothing is
    waiting" or "nobody asked yet", and that ambiguity is half this module's
    reason to exist.

    `lacked` names the fact that was missing, and is present EXACTLY when the
    confidence is UNKNOWN. A refusal that does not say what it lacked is
    indistinguishable from a crash, and gets treated as one."""

    __slots__ = ()

    def __new__(cls, question, answer=None, confidence=UNKNOWN, evidence=(),
                lacked=None, prior=None, despite=None):
        question = str(question).strip()
        if not question:
            raise ValueError("a reading with no question cannot be read back")
        if confidence not in CONFIDENCES:
            raise ValueError("confidence must be one of %s, got %r"
                             % (", ".join(CONFIDENCES), confidence))
        evidence = _evidence(evidence)
        lacked = None if lacked is None else str(lacked).strip() or None
        if confidence == UNKNOWN and not lacked:
            raise ValueError(
                "%r is UNKNOWN without naming what it lacked: an unexplained "
                "refusal reads downstream as a failure, not as an absence"
                % question)
        if confidence != UNKNOWN and lacked:
            raise ValueError(
                "%r claims %s and also names a missing fact (%s): a reading is "
                "either grounded or it is not" % (question, confidence, lacked))
        if confidence != UNKNOWN and not evidence:
            raise ValueError(
                "%r claims %s with nothing looked at: a claim with no evidence "
                "is the reassuring reading this type exists to refuse"
                % (question, confidence))
        despite = None if despite is None else str(despite).strip() or None
        if despite and confidence == UNKNOWN:
            raise ValueError(
                "%r is UNKNOWN and also says why a prior's missing fact did "
                "not matter (%s): that argument is for a reading that reached "
                "certainty over an unanswered prior, and an UNKNOWN reached "
                "none" % (question, despite))
        return tuple.__new__(cls, (question, answer, confidence, evidence,
                                   lacked, prior, despite))

    @property
    def question(self):
        return self[0]

    @property
    def answer(self):
        return self[1]

    @property
    def confidence(self):
        return self[2]

    @property
    def evidence(self):
        return self[3]

    @property
    def lacked(self):
        return self[4]

    @property
    def prior(self):
        return self[5]

    @property
    def despite(self):
        """Why this reading's certainty did not need what its prior lacked, or
        None because it never reached over an unanswered prior.

        Present EXACTLY when a stage answered a question its prior could not,
        without new evidence -- see `derive`. It is kept on the object rather
        than checked and dropped so that the move stays visible in `why` and
        `chain` for as long as the reading exists."""
        return self[6]

    @property
    def proven(self):
        return self[2] == PROVEN

    @property
    def refuted(self):
        return self[2] == REFUTED

    @property
    def unknown(self):
        return self[2] == UNKNOWN

    def because(self, kind):
        """The detail of the first evidence entry of `kind`, or None."""
        return next((e.detail for e in self[3] if e.kind == kind), None)

    def derive(self, question, answer=None, confidence=UNKNOWN, evidence=(),
               lacked=None, despite=None):
        """THE ONE DOOR. Build the next stage's reading from this one.

        A stage deriving from an UNKNOWN prior may not claim PROVEN or REFUTED
        by simply reusing the prior's evidence to reach a firmer answer than
        the prior reached: that is the move that turned "the room was never
        scanned" into "the room was empty". There are exactly TWO honest ways
        past an unanswered prior, and this door asks for one of them.

        SUPPLY WHAT WAS MISSING: pass evidence this reading does not already
        carry. Nothing else is needed -- the new fact is the argument.

        OR SAY WHY IT WAS NOT NEEDED, in `despite`. The question changed, and
        the fact the prior lacked does not bear on the NEW one. Testing
        EVIDENCE NOVELTY here instead is a proxy that breaks the moment the
        proposition changes. A reading that is
        UNKNOWN about whether the owed room's row drained, carrying
        `scanned=('other',)`, PROVES "did this sample pass room other?" on
        exactly the evidence it already holds -- and the old rule refused it
        while admitting any irrelevant new Evidence at all. Sufficiency for the
        TARGET proposition is the question; the evidence list's freshness never
        was.

        `despite` IS AN AUDITABLE JUSTIFICATION, NOT AN ENTAILMENT CHECK, and
        calling it anything stronger would be a bypass with a docstring. This
        door does not and cannot decide whether the prose is TRUE or whether
        the cited evidence actually entails the new answer: any non-blank
        sentence is accepted. What it buys is that the move is IMPOSSIBLE TO
        MAKE SILENTLY -- it is required, it names the caller's reasoning, and
        it rides in `why` and `chain` for as long as the reading exists, so a
        reader can challenge the reasoning. The novelty test it replaces
        checked something equally unrelated to sufficiency (was any Evidence
        object new) and disclosed nothing. Where an ACTUAL authority depends on
        the answer, demand target-specific evidence at that door rather than
        trusting this one.

        Carrying an UNKNOWN forward is always allowed and needs no argument:
        call `carry` for that, which keeps the original missing fact rather
        than restating it."""
        evidence = _evidence(evidence)
        despite = None if despite is None else str(despite).strip() or None
        # A DROPPED ARGUMENT READS AS AN HONOURED ONE. `despite` answers "why
        # did this reading not need what the prior lacked", and a reading that
        # reaches no certainty has not reached over anything -- so passing one
        # here is a caller who believes they are justifying a move they are
        # not making. Refused rather than silently discarded, because the
        # silent discard is indistinguishable from acceptance at the call site.
        if confidence == UNKNOWN and despite:
            raise ValueError(
                "%r derives an UNKNOWN and also says why a prior's missing "
                "fact did not matter (%s): that argument is for a reading "
                "that reached certainty over an unanswered prior, and this "
                "one reached none" % (question, despite))
        if self.unknown and confidence != UNKNOWN:
            fresh = tuple(e for e in evidence if e not in self[3])
            if not fresh and not despite:
                raise ValueError(
                    "%r claims %s from a prior that could not answer (%s). "
                    "Either measure the missing fact and pass it as evidence, "
                    "or say in `despite` why this question does not need it -- "
                    "reaching certainty over an unanswered prior in silence is "
                    "what this door exists to stop"
                    % (question, confidence, self.lacked))
            if fresh:
                despite = None
        else:
            despite = None
        return Reading(question, answer, confidence, evidence, lacked,
                       prior=self, despite=despite)

    def carry(self, question):
        """Propagate this reading's UNKNOWN to the next stage's question,
        keeping the fact that was actually missing.

        Restating it in the new stage's words is how the original cause got
        lost three stages downstream and a reader was sent to look at the wrong
        instrument."""
        if not self.unknown:
            raise ValueError(
                "carry propagates a refusal; %r is %s and has an answer to "
                "derive from" % (self[0], self[2]))
        return Reading(question, None, UNKNOWN, self[3], self.lacked,
                       prior=self, despite=None)

    def chain(self):
        """This reading and every reading it was derived from, oldest first.

        DISCLOSE renders this, not a stage's local opinion: a verdict that
        reads PROVEN at the last stage and UNKNOWN two stages back is a story
        the owner is entitled to see whole."""
        out, node = [], self
        while node is not None:
            out.append(node)
            node = node.prior
        out.reverse()
        return tuple(out)

    def why(self):
        """One sentence, GENERATED from this reading rather than written beside
        it.

        Prose restated next to an object drifts from it -- twice in this
        subsystem a docstring kept asserting a guarantee the code had stopped
        providing. A sentence computed from the fields cannot."""
        if self.unknown:
            return "%s: UNKNOWN, because %s" % (self[0], self.lacked)
        sentence = "%s: %s (%s), on %s" % (
            self[0], self[2], self[1],
            ", ".join(e.kind for e in self[3]) or "nothing")
        if self.despite:
            # DISCLOSED, NEVER MERELY PERMITTED. A certainty reached over an
            # unanswered prior is exactly the reading a later reader should be
            # able to argue with, so the grounds travel in the sentence rather
            # than sitting on the object for someone who thinks to look.
            sentence += " -- answered over an unanswered prior (%s) because %s" % (
                self.prior.lacked, self.despite)
        return sentence

    def __repr__(self):
        return "Reading(%r, %r, %s, %r, %r, despite=%r)" % (
            self[0], self[1], self[2], list(self[3]), self[4], self[6])


def proven(question, answer, *evidence):
    """A grounded positive. Evidence is required by the type, not by habit."""
    return Reading(question, answer, PROVEN, evidence)


def refuted(question, *evidence, **kw):
    """A grounded negative -- something was looked at and it was NOT there.

    `answer` defaults to False so the common call reads as the fact it is; a
    stage with a richer negative (which rooms were read, say) passes its own.
    An unrecognised keyword is REFUSED rather than dropped: a misspelled
    `answer=` that vanished would turn a rich negative into a bare False and
    the call site would read as though it had been honoured."""
    answer = kw.pop("answer", False)
    if kw:
        raise TypeError("refuted() got unexpected keyword(s): %s"
                        % ", ".join(sorted(kw)))
    return Reading(question, answer, REFUTED, evidence)


def unknown(question, lacked, *evidence):
    """A refusal that says what it lacked. The only honest third answer."""
    return Reading(question, None, UNKNOWN, evidence, lacked)
