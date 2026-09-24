"""One pane tail, normalized ONCE, read by every classifier.

THE DEFECT CLASS THIS EXISTS TO END. Scanning a tail with several free-text
scanners, each over its own copy of the text — one ANSI-cleaned, one lowercased
— and passing CHARACTER OFFSETS between them cannot be made correct. Offsets
are not portable across `str.lower()`: a capital I-with-dot lowercases to TWO
codepoints, so every offset after it shifts by one and a wall's end consumes
its own newline. Such scanners disagree on a new shape each time one is
examined, which is the signature of a wrong question rather than wrong
answers.

SO THERE IS ONE ORIGIN AND ONE COORDINATE. `normalize` strips ANSI once and
returns lines; a position is `(line, col)` into THAT list and nothing else.
Case-insensitive matching happens through `re.I` against the same string,
never against a lowercased copy, so no offset ever crosses representations.

PRODUCERS EXPOSE CANDIDATES, NEVER VERDICTS. A producer that returns "the
current X" has already thrown away what a later consumer needs to tell a stale
X from a live one — and worse, a producer that returns THE LAST QUALIFYING
candidate silently resurrects an old one when a newer candidate does not
qualify. Every producer here returns EVERY candidate with its span and its
qualification, and judgment lives in the consumer.

The recognition rules are reused verbatim from the scanners this module
replaces; what changes is the population they judge.
"""

import re
from collections import namedtuple

#: One escape sequence, stripped once. The only normalization that happens.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

#: Recognition rules, reused verbatim rather than re-derived: they are applied
#: to a preserved candidate population instead of to a helper's final answer.
OPTION = re.compile(r"^\s*(\d+)\.\s+(\S.*?)\s*$")
PROMPT = re.compile(r"^\s*❯")
RULE_COMPOSER = re.compile(r"^\s*[─━]{2}([^─━].*)$")
AFFIRMATIVE = re.compile(r"^yes\b", re.I)
NEGATIVE = re.compile(r"^no\b", re.I)

#: A run's qualification, kept as a REASON rather than a bool, because
#: "did not qualify" and "qualified" are not the only two states a consumer
#: cares about: an unrecognised shape must be able to suppress an older
#: actionable one without itself becoming actionable.
QUALIFIED = "qualified"          # contiguous 1..N carrying a yes/no label
UNQUALIFIED = "unqualified"      # a numbered run that is not a dialog

#: WHAT UNQUALIFIED ENTITLES A CONSUMER TO CONCLUDE, AND THE LIST IS SHORT.
#: A newer UNQUALIFIED run SUPPRESSES fallback to an older QUALIFIED one in the
#: current-input decision: no old choice is offered. That is the whole of it.
#: It is NOT proof the older dialog cleared, NOT permission to type anywhere
#: else, and NOT work. Those are three different facts with three different
#: sources, and a consumer that reads suppression as any of them has invented
#: a clearance nothing measured — which is the failure this module exists to
#: make unavailable rather than merely discouraged.
Pos = namedtuple("Pos", "line col")
Run = namedtuple("Run", "start end options qualification ended_by")


def normalize(tail):
    """[str] — the tail's lines, ANSI stripped ONCE, nothing lowercased.

    The returned list IS the coordinate space. Every position this module
    hands out indexes into it, so a caller cannot accidentally measure one
    representation and address another.
    """
    return [ANSI.sub("", line) for line in (tail or "").splitlines()]


def option_runs(lines):
    """[Run] — EVERY numbered run in the tail, oldest first, with its span.

`_prompt_options` yields only the last QUALIFYING run, so a newer run
    that does not qualify falls through to an older one that does: an answered
    `1. Yes / 2. No` beneath a newer `1. src/main.py / 2. ...` comes back as
    the live dialog, which is reproduced against that helper in this suite.
    Every run is kept here, with `ended_by` naming the line that closed it, so
    a consumer can see that something newer sits below.

    A blank line does not break a run — a dialog can be drawn with gaps — but
    any other non-matching prose does, and that prose is recorded rather than
    discarded because it is the evidence a run is over.
    """
    runs, cur, started = [], [], None

    def close(ended_by):
        if cur:
            runs.append(Run(started, Pos(cur[-1][0], 0), [(n, l) for _i, n, l in cur],
                            _qualification(cur), ended_by))

    for i, line in enumerate(lines):
        if not line.strip():
            continue                     # a blank row never breaks a dialog
        m = OPTION.match(line)
        if not m:
            close(Pos(i, 0))
            cur, started = [], None
            continue
        n = int(m.group(1))
        if n == 1:
            close(Pos(i, 0))
            cur, started = [(i, 1, m.group(2))], Pos(i, m.start(1))
        elif cur and n == cur[-1][1] + 1:
            cur.append((i, n, m.group(2)))
        else:
            close(Pos(i, 0))
            cur, started = [], None
    close(None)                          # None: ran to the end of the tail
    return runs


def _qualification(entries):
    """QUALIFIED only for a real dialog, and the label test is the whole point.

    Ordinary numbered prose in a transcript is contiguous and sequential too,
    so length alone would make a plan's own steps actionable. A run earns
    QUALIFIED by offering a yes/no label; everything else is UNQUALIFIED and
    can still SUPPRESS an older run without becoming actionable itself.
    """
    if len(entries) < 2:
        return UNQUALIFIED
    for _i, _n, label in entries:
        if AFFIRMATIVE.match(label or "") or NEGATIVE.match(label or ""):
            return QUALIFIED
    return UNQUALIFIED


#: WHAT A COMPOSER OBSERVATION IS ALLOWED TO SAY ABOUT ITS OCCUPANT.
#: BARE and OCCUPIED are what the TAIL can support: there is a composer and it
#: is empty, or there is a composer and it holds something. WHO put that
#: something there is a question about AUTHORSHIP, and the tail carries no
#: evidence for it — helm's own placed text and a human's typing render
#: identically. So attribution takes the consumer's evidence (`placed`) or it
#: answers UNKNOWN, and UNKNOWN never collapses into either answer.
BARE = "bare"                    # a composer with nothing in it
OCCUPIED = "occupied"            # a composer holding text, author not yet known
AI = "ai"                        # occupied by text the caller PROVED it placed
HUMAN_DRAFT = "human-draft"      # occupied by text the caller did NOT place
UNKNOWN = "unknown"              # readable composer, authorship unevidenced

#: WHERE THE OBSERVATION CAME FROM, because the two are not equally strong.
#: A metaharness that exposes the current input as its own field is stating a
#: fact; a line scraped out of rendered text is an inference from pixels that a
#: scrollback row can imitate. `_CLIAdapter.read` currently folds the former
#: into the latter by appending it as synthetic text, which destroys exactly
#: the distinction an actuator needs before it types.
STRUCTURED = "structured"
SCRAPED = "scraped"

Composer = namedtuple("Composer", "pos kind ownership provenance text")


def composer_observations(lines, placed=None, structured=None):
    """[Composer] — EVERY composer-shaped row, oldest first.

    NOT "the current composer". Which observation owns input is a judgment that
    needs the other events in the tail, and a producer that answers it here
    hands a consumer one row with no way to see the ones it beat.

    `placed` is the text the CALLER can prove it put in the composer. With it,
    an occupied composer attributes to AI or HUMAN_DRAFT; without it the answer
    is OCCUPIED and the caller may not upgrade that to either. `structured` is
    the metaharness's own current-input field, which arrives as a fact rather
    than as a reading and is recorded as such.
    """
    out = []
    if structured is not None:
        out.append(Composer(None, "field", _own(structured, placed),
                            STRUCTURED, structured))
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = PROMPT.match(line)
        if m:
            body = line[m.end():].strip()
            out.append(Composer(Pos(i, m.start()), "prompt",
                                _own(body, placed), SCRAPED, body))
            continue
        rule = RULE_COMPOSER.match(line)
        if rule:
            body = rule.group(1).strip()
            out.append(Composer(Pos(i, rule.start(1)), "rule-draft",
                                _own(body, placed), SCRAPED, body))
    return out


def _own(text, placed):
    """BARE / AI / HUMAN_DRAFT / OCCUPIED — never a guess.

    An empty composer is BARE and needs no author. A composer holding text has
    one, and the tail cannot name it: helm's own placed keystrokes and a
    human's typing render the same. So the answer is OCCUPIED until the caller
    supplies what it placed, and a caller that supplies nothing gets no
    attribution rather than a default.
    """
    body = (text or "").strip()
    if not body:
        return BARE
    if placed is None:
        return OCCUPIED
    return AI if body == (placed or "").strip() else HUMAN_DRAFT


Wall = namedtuple("Wall", "pos spelling line force")


def wall_occurrences(lines, now=None):
    """[Wall] — EVERY quota wall in the tail, oldest first, each judged ONCE.

    ONE OCCURRENCE CARRIES ITS OWN SPELLING, DETAIL AND EXPIRY. Asking each
    question with its own scan lets two rungs answer about DIFFERENT walls on
    the same pane: expiry dating whichever pattern matched first while recovery
    anchored on the last one, so an undated line above a later expired line
    could brand a working seat walled. Here the spelling that matched, the line
    it matched in, and the in-force verdict are fields of ONE record, so no
    consumer can pair a verdict with a wall that did not produce it.

    TIES ARE DETERMINISTIC. Several spellings can match one line; the winner is
    the earliest match position, and the earliest pattern in the table breaks a
    positional tie. A line yields at most one wall, so a vendor rewording that
    makes two patterns overlap cannot silently double a pane's wall count.

    MISSING OR MALFORMED DATES ARE NOT EXPIRY. `_wall_in_force` answers
    IN_FORCE, EXPIRED, UNDATED or UNANCHORED, and each is its own state: a
    dated clause this build cannot parse fails toward the loud answer as
    UNDATED, because a false wall is visible and recoverable while a false
    clean bill is silent.

    THIS PRODUCER HAS NO OBSERVATION INSTANT TO GIVE. A pool refusal states
    its reset as a DURATION ("reset in 2h27m37s"), so its expiry is that
    duration past the moment the producer wrote the line, and a list of pane
    lines does not carry that moment. Only `_wall_in_force`'s `observed`
    argument supplies it, and the caller that has one is the seat classifier
    reading the seat's own proxy.log. So a pool line judged here is always
    UNANCHORED, which is UNKNOWN and not a wall — see `wall_standing`, which
    is where that answer is acted on.
    """
    from . import seat  # noqa: F401 — facade contract: an impl import is accompanied by the facade in its own scope
    from .seat_lifecycle import _wall_in_force, _wall_patterns
    pats = _wall_patterns()
    if not pats:
        # An empty table would answer "no walls" for every pane and restore
        # the defect silently, so refuse rather than report a clean fleet.
        raise RuntimeError(
            "no BLOCKED_ON_QUOTA patterns in _LIVENESS_STATES; wall "
            "occurrences cannot be derived from an empty table")
    out = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        best = None
        for rank, pat in enumerate(pats):
            m = re.search(pat, line, re.I)
            if m and (best is None or (m.start(), rank) < best[0]):
                best = ((m.start(), rank), pat)
        if best is None:
            continue
        out.append(Wall(Pos(i, best[0][0]), best[1], line,
                        _wall_in_force(line, now)))
    return out


#: THE EVENT KINDS, in the one ordered list every classifier reads.
RUN = "run"                      # a numbered option run
WALL_EVENT = "wall"              # a quota wall occurrence
COMPOSER_EVENT = "composer"      # a composer-shaped row

Event = namedtuple("Event", "pos kind detail")

#: A STRUCTURED DRAFT IS NOT A TAIL EVENT AND IS NOT GIVEN A FAKE POSITION.
#: The metaharness's current-input field describes the pane's live state, not
#: something rendered at a place in the scrollback. Sorting it into the
#: positional list would require inventing a coordinate, and an invented
#: coordinate is exactly what this module exists to remove. It rides alongside,
#: where a consumer must decide deliberately what weight to give it.
Parsed = namedtuple("Parsed", "events structured lines")


def parse(tail, placed=None, structured=None, now=None):
    """Parsed(events, structured) — ONE pass, ONE coordinate, every candidate.

    `events` is every positioned candidate from every producer, ordered by
    (line, col) — a total order by construction, so two events rendered on one
    line still order deterministically and the invariant never rests on having
    failed to observe that case.

    NOTHING HERE DECIDES WHAT IS CURRENT. The list is the evidence; which
    events are in force is a separate question with its own rules, because
    "rendered most recently" and "still in force" are different facts and no
    ordering converts one into the other. A wall drawn over by a dialog is
    still a wall.
    """
    lines = normalize(tail)
    obs = composer_observations(lines, placed=placed, structured=structured)
    events = [Event(r.start, RUN, r) for r in option_runs(lines)]
    events += [Event(w.pos, WALL_EVENT, w) for w in wall_occurrences(lines, now)]
    events += [Event(c.pos, COMPOSER_EVENT, c) for c in obs if c.pos is not None]
    field = [c for c in obs if c.pos is None]
    return Parsed(sorted(events, key=lambda e: e.pos),
                  field[0] if field else None, lines)


#: WHAT A LINE BELOW A WALL IS, AND THE THIRD ANSWER IS THE IMPORTANT ONE.
#: WORK means the parser POSITIVELY attributed the line to the seat doing
#: something. NON_WORK means it positively recognised the line as something
#: that is not work — the wall's own remedy continuation, footer chrome, a bare
#: composer glyph. UNATTRIBUTED means neither: prose the parser does not
#: recognise, which is most prose.
#:
#: THE ASYMMETRY IS THE WHOLE POINT AND IT IS NOT SYMMETRIC BY ACCIDENT. A gap
#: in evidence can HIDE activity and can never MANUFACTURE it, so UNATTRIBUTED
#: may sustain a wall and may never lift one. Letting it clear would make the
#: noisiest pane look the healthiest, which inverts the answer exactly where it
#: costs most.
WORK = "work"
NON_WORK = "non-work"
UNATTRIBUTED = "unattributed"


def attribute(lines, i, walls=None, composers=None):
    """WORK / NON_WORK / UNATTRIBUTED for ONE line, by what it IS.

    THIS REPORTS A KIND AND CHOOSES NO POLICY. Whether UNATTRIBUTED lines
    clear a wall is a consumer's decision with consumer consequences — the
    shipped recovery rung treats unrecognised prose as work, and tightening
    that changes which seats read walled — so the distinction is made visible
    here and the choice is left where it can be argued and tested.
    """
    from . import seat  # noqa: F401 — facade contract: an impl import is accompanied by the facade in its own scope
    from .seat_lifecycle import _PANE_CHROME, _is_wall_line
    line = lines[i]
    if not line.strip():
        return NON_WORK
    if _is_wall_line(line):
        return NON_WORK              # a wall restated is not a reply to itself
    if _PANE_CHROME.match(line):
        return NON_WORK              # redrawn under a parked pane too
    for c in (composers or []):
        if c.pos is not None and c.pos.line == i:
            # A composer carrying TEXT is a submission and real evidence the
            # seat moved; an empty glyph IS the parked state, and reading it as
            # progress inverts the answer exactly when it matters.
            return NON_WORK if c.ownership == BARE else WORK
    return UNATTRIBUTED


#: IN-FORCE IS PER TYPE AND THE THIRD ANSWER IS NOT A FAILURE.
#: Each type has its own lifecycle, judged independently, so several things can
#: be in force at once — a wall does not stop being a wall because a dialog was
#: drawn over it. UNDETERMINED means the tail carries no evidence either way,
#: and it is a real answer: collapsing it into IN_FORCE invents a dialog and
#: collapsing it into ENDED invents a clearance.
IN_FORCE = "in-force"
ENDED = "ended"
UNDETERMINED = "undetermined"

Standing = namedtuple("Standing", "state occurrence why")


def modal_standing(parsed):
    """Standing — whether a dialog still owns input, judged on its OWN events.

    END OF THE OPTION LIST IS NOT END OF OWNERSHIP. A dialog can print prose or
    a footer under its choices and still be capturing keys, so a run's
    terminator says the LIST finished and says nothing about the dialog. What
    the tail CAN establish is the negative: a newer run that does not qualify
    SUPPRESSES fallback to an older qualified one, and a composer holding text
    below the run means something else took the keystrokes.

    So the honest answers are UNDETERMINED and ENDED far more often than
    IN_FORCE, and IN_FORCE here is still NOT permission to type — that needs
    the explicit ownership facts an actuator gathers at the moment it acts.
    """
    runs = [e for e in parsed.events if e.kind == RUN]
    if not runs:
        return Standing(ENDED, None, "no numbered run in the tail")
    newest = runs[-1]
    if newest.detail.qualification == UNQUALIFIED:
        # SUPPRESSION, NOT CLEARANCE: no old choice is offered, and that is the
        # whole entitlement. It is not proof the older dialog went away.
        return Standing(UNDETERMINED, newest,
                        "the newest numbered run does not qualify, so no older "
                        "choice is offered and nothing here says the older "
                        "dialog cleared")
    after = [e for e in parsed.events
             if e.pos > newest.pos and e.kind == COMPOSER_EVENT
             and e.detail.ownership != BARE]
    if after:
        return Standing(ENDED, newest,
                        "a composer holding text is rendered below the run")
    return Standing(UNDETERMINED, newest,
                    "a qualifying run with nothing below it — the tail cannot "
                    "show whether the dialog still owns input")


def wall_standing(parsed):
    """Standing — whether a quota wall still binds, judged on its OWN events.

    THE NEWEST WALL IS THE ONLY ONE THAT CAN BIND; everything above it is
    scrollback by construction. Its own verdict decides first — an EXPIRED line
    is history — and then position: work rendered below it proves the wall is
    not binding now, whatever any timestamp says and whatever credential it
    came from.

    UNKNOWN IS NOT A WALL, AND IT IS NOT A CLEARANCE EITHER. An UNANCHORED
    verdict means the line dates its reset from an observation instant this
    reader was never given, so nothing here knows whether the wall is over.
    Reading that as IN_FORCE binds the seat forever — the duration never
    elapses because no clock was ever started — and reading it as ENDED claims
    a clearance no evidence supports. It answers UNDETERMINED, which is the
    state this module already keeps for "the tail carries no evidence either
    way". Attributed work below the wall still wins: that is real evidence and
    it clears the wall whatever the verdict was, so the position scan runs
    first and only a tail with no such work falls through to UNDETERMINED.

    ONLY ATTRIBUTED WORK LIFTS IT. Unrecognised prose below a wall answers
    UNATTRIBUTED and leaves the wall standing: a gap in evidence can hide
    activity and can never manufacture it, so letting it clear would make the
    noisiest pane look the healthiest. There is deliberately NO PARAMETER to
    restore the looser reading — an option that can turn the contract off is
    the contract being optional, and if this proves too strict that is a
    measured finding to bring back, not a flag to flip.
    """
    from . import seat  # noqa: F401 — facade contract: an impl import is accompanied by the facade in its own scope
    from .seat_lifecycle import WALL_EXPIRED, WALL_UNANCHORED
    walls = [e for e in parsed.events if e.kind == WALL_EVENT]
    if not walls:
        return Standing(ENDED, None, "no quota wall in the tail")
    newest = walls[-1]
    if newest.detail.force == WALL_EXPIRED:
        return Standing(ENDED, newest, "the wall line states a reset that has "
                                       "passed, so the line is history")
    composers = [e.detail for e in parsed.events if e.kind == COMPOSER_EVENT]
    for i in range(newest.pos.line + 1, len(parsed.lines)):
        if attribute(parsed.lines, i, composers=composers) == WORK:
            return Standing(ENDED, newest,
                            "attributed work is rendered below the newest wall")
    if newest.detail.force == WALL_UNANCHORED:
        return Standing(UNDETERMINED, newest,
                        "the wall line dates its reset from an observation "
                        "instant this reader was not given, so whether the "
                        "wall is still in force is UNKNOWN — and UNKNOWN is "
                        "neither a wall nor a clearance")
    return Standing(IN_FORCE, newest,
                    "the newest wall is not expired and no attributed work is "
                    "rendered below it")
