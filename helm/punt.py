#!/usr/bin/env python3
"""helm punt — the Stop rung for the DECLINATION that wears a reason.

OWNER, 2026-07-29, verbatim and unabridged because the wording is the spec:

    "i always want you to do all the things necessary to get to the end
    results... this kind of deferral is EXACTLY how we end up constantly
    discovering 'built not wired' or 'wired not verified' or any of the variety
    of recurring patterns in our harness. 'i won't do X because y (where y is
    based on clock time, my location status, or assumed interruptions it would
    cause, without actually checking, or a variety of other identified punt
    classes)' is 100% of the time a punt and should be called out via
    contextual detstophooks or whatever means you find most effective."

WHY THE EXISTING REFLEX DOES NOT COVER THIS. `punt-tell` already matches
deferral TELLS — "later", "for now", "next session", "todo:", "park it". Those
are the honest punts, the ones that announce themselves. The expensive shape is
the opposite: a declination that arrives fully DRESSED, with a reason that
sounds like diligence.

    "I did not restart the node (would disrupt the fleet mid-work),
     install the staged binary (unproven end-to-end), or chase the
     node build further while you're away."

Every clause reads as judgment. All three were punts. MEASURED minutes later:
the two seats supposedly mid-work had NO LIVE PROCESS AT ALL, so there was
nothing to disrupt; "unproven" was a property of my not having run the proof;
and "while you're away" is a fact about the owner's calendar, not about the
work. The reasons were never checked — they were produced to close the turn.

THE DISCRIMINATOR IS NOT THE EXCUSE, IT IS THE PAIR. Prose is full of honest
negations ("I did not change the window", "this does not fix the node half"),
and a rung that fired on those would be switched off within a day. A finding
needs BOTH halves in ONE sentence: a first-person DECLINED ACTION and a reason
drawn from a named punt CLASS. That pairing is rare in honest reporting and
near-universal in a dressed punt.

THE ESCAPE HATCH IS THE POINT, NOT A LOOPHOLE. Real blockers exist —
owner-held credentials, hardware, decisions only a human can make. The rule is
not "never decline", it is the owner's standing law: anything truly deferred is
LOUDLY FLAGGED, never silently parked. So a declination is discharged by an
OPEN OWNER-ASK ROW (`helm asks add`). File it and the gate opens. That converts
a sentence the owner has to catch by reading into a row he can see on a surface.

FAIL-OPEN, TOTAL. A Stop rung that raises wedges every session on the host.
Every entry point here returns "no finding" rather than propagate — a punt
detector that broke the fleet would be its own worst instance.
"""
import json
import os
import re

from . import home

# ---------------------------------------------------------------------------
# the two halves
# ---------------------------------------------------------------------------

# HALF ONE: a first-person action that did NOT happen. Deliberately narrow —
# "I did not" and friends, not every negation in the language.
_DECLINED = re.compile(
    r"\b("
    r"i (?:did|do) not\b|i didn'?t\b|i (?:will|wo)n'?t\b|i'?m not going to\b|"
    r"i am not going to\b|i have not\b|i haven'?t\b|"
    r"(?:did|do) not (?:install|restart|deploy|run|push|land|build|verify|"
    r"chase|touch|clear|reap|kill|fix)\b|"
    r"three things i did not\b|left it (?:staged|as|at|alone|for)\b|"
    r"leaving it (?:staged|for|to)\b|rather than (?:restart|install|deploy|"
    r"run|land|push|build)\b|stopped short of\b"
    r")", re.I)

# HALF TWO: the named punt classes. Each entry is (class, pattern) so a finding
# can say WHICH excuse it caught — a guard that only says "punt" teaches nobody.
_CLASSES = (
    ("owner-presence", re.compile(
        r"\b(while you'?re away|since you'?re away|you'?re afk|while you sleep|"
        r"you'?re asleep|for the morning|overnight|when you'?re back|"
        r"while the owner is|owner is away|at this hour|until you'?re back)\b",
        re.I)),
    ("assumed-disruption", re.compile(
        r"\b(would disrupt|would interrupt|might disrupt|mid-?work|"
        r"would affect the fleet|don'?t want to disturb|too risky|would be "
        r"risky|risky to|could break|might break the)\b", re.I)),
    ("unproven-as-excuse", re.compile(
        r"\b(unproven(?: end-?to-?end)?|not proven end-?to-?end|"
        r"not yet proven|left .{0,20}staged|staged, not installed|"
        r"NOT installed)\b", re.I)),
    ("size-or-cost", re.compile(
        r"\b(too (?:big|large|expensive|slow)|expensive step|beyond (?:the )?"
        r"scope|out of scope|a marathon|would take too long)\b", re.I)),
    ("someone-else's-lane", re.compile(
        r"\b(that'?s (?:for )?(?:the )?(?:dregg |another |someone )?"
        r"(?:owner|lane|team|agent)'?s?(?: job| call| lane| problem)|"
        r"whoever owns|not (?:mine|my lane) to|belongs to another)\b", re.I)),
)

# HALF THREE: THE PASSIVE TEE — a different shape from the two halves above, and
# the one the owner actually caught. "Worth a display fix eventually; filed
# mentally, not urgent." carries NO first-person declination, so _DECLINED can
# never see it: nothing is declined, work is simply NAMED and set adrift. The
# dressed punt says "I did not do X because Y"; the passive tee says "X should
# happen sometime" and never says by whom.
#
# The discriminator is deferral plus ABSENT ACCOUNTABILITY, never the phrases
# alone — which is why this is the only rung here whose trigger is a MISSING
# token. "Worth a display fix eventually, dispatched to @kimi" is not a punt, it
# is a handoff, and the same deferral words appear in both.
_TEE_DEFER = re.compile(
    r"\b(eventually|some ?day|at some point|later on|down the (?:line|road)|"
    r"in a (?:future|later|separate) (?:pass|round|lane)|another time|"
    r"when (?:we|someone) get(?:s)? (?:to|around to) it|not urgent|no rush|"
    r"low priority|nice to have|for now)\b", re.I)
# An intent word turns a bare schedule remark into TEED WORK. Without it,
# "the daemon eventually reaps it" — a fact about the world — would read as a
# punt, which is the precision failure this half is most exposed to.
_TEE_INTENT = re.compile(
    r"\b(worth|should|could|ought to|needs? (?:a|to|an)|want to|plan to|"
    r"would be (?:good|nice|worth|better)|TODO|we'?ll (?:want|need))\b", re.I)
# Standalone markers: these ARE the tee, no intent word required, because
# "filed" here means noted-and-abandoned. This is the owner's second clause.
_TEE_FILED = re.compile(
    r"\b(filed mentally|mentally filed|noted (?:it )?(?:mentally|for later)|"
    r"mental note|making a note|noting for later|parking (?:it|this) for)\b",
    re.I)
# ACCOUNTABILITY: any of these in the same sentence means the work has somewhere
# to live, so the deferral is a handoff and not a punt. Sentence-window, matching
# this module's existing law — a tracker two sentences away is not this
# sentence's accountability, and widening the window is how the first-person
# half was measured to produce constant false pairs.
_TEE_TRACKER = re.compile(
    r"\b(dispatch(?:ed|es)?|ledger|row [0-9a-f]{6,}|lane/|gate/|ticket|"
    r"issue #?\d+|tracker|board (?:row|item)|task #?\d+|filed as|asks add|"
    r"backlog|@[a-z][a-z0-9._-]{2,})\b", re.I)


def _passive_tee(bare):
    """True when this sentence TEES work and names nowhere for it to live."""
    if _TEE_TRACKER.search(bare):
        return False
    if _TEE_FILED.search(bare):
        return True
    return bool(_TEE_DEFER.search(bare) and _TEE_INTENT.search(bare))


# A sentence carrying an explicit loud-flag marker is already discharged inline.
_LOUD = re.compile(r"\b(helm asks add|owner-ask|OWNER ASK|FLAGGED TO OWNER|"
                   r"asks add)\b", re.I)

_SENT = re.compile(r"(?<=[.!?;\n])\s+")

# Spans inside quotation marks are QUOTED, not committed. Measured over this
# session's own transcript — 6,506 assistant messages, 29,860 sentences — the
# detector produced exactly ONE finding, and it was a sentence DESCRIBING the
# punt shape rather than performing it: `a justified declination — "I did not
# do X because it would disrupt / because you're away"`. Both halves sat inside
# the quotes. Since this module's docs, its commit messages and every chat post
# explaining the rule quote the pattern verbatim, a detector that fires on
# discussion of itself would be unusable exactly where it is discussed most.
_QUOTED = re.compile(r'"[^"]*"|“[^”]*”|`[^`]*`')
# A fenced block is quotation by construction, and a sentence split mid-quote
# leaves an UNBALANCED opening mark that _QUOTED cannot pair — both defeated the
# stripping above until measured against the real transcript.
_FENCE = re.compile(r"```.*?```|```.*", re.S)
_DANGLING = re.compile(r'["“].*$', re.S)
# MARKDOWN IS NOT PUNCTUATION, and this is the miss that mattered. The punt that
# CAUSED this whole rung reads `I did **not** do` in its real rendered form, and
# the emphasis markers split the phrase so the detector sailed straight past its
# own originating example while happily flagging a sentence that merely QUOTED
# it. Recall failure on the true case, precision failure on the false one — the
# exact inversion of what the guard is for. Emphasis is stripped before any
# matching so prose is judged as written, not as marked up.
_EMPHASIS = re.compile(r"[*_~]{1,3}")


def _off(name):
    return str(home.env(name, "1")).lower() in ("0", "off", "no", "false")


def sentences(text):
    return [s.strip() for s in _SENT.split(_FENCE.sub(" ", text or ""))
            if s.strip()]


def findings(text):
    """[(punt_class, sentence)] — declined action AND a class excuse, together.

    One sentence is the window on purpose. Across a paragraph, an unrelated
    negation and an unrelated schedule remark co-occur constantly; inside one
    sentence the pairing is the dressed punt almost every time."""
    out = []
    for s in sentences(text):
        if _LOUD.search(s):
            continue
        # judge the sentence with its QUOTED spans removed, but REPORT the
        # sentence whole — the reader needs the line as written
        bare = _DANGLING.sub(" ", _QUOTED.sub(" ", _EMPHASIS.sub("", s)))
        hit = None
        if _DECLINED.search(bare):
            for cls, pat in _CLASSES:
                if pat.search(bare):
                    hit = cls
                    break
        # the dressed-declination classes keep FIRST refusal on every sentence,
        # so their output is unchanged; the tee only speaks where they are silent
        if hit is None and _passive_tee(bare):
            hit = "passive-tee"
        if hit:
            out.append((hit, " ".join(s.split())[:240]))
    return out


# ---------------------------------------------------------------------------
# the transcript read
# ---------------------------------------------------------------------------

def last_assistant_text(path, tail_bytes=256 * 1024):
    """The final assistant message in a session transcript, or ''.

    Bounded tail read: a transcript is megabytes and the Stop hook runs on
    every turn end. Sidechain (subagent) records never count — a subagent's
    prose is not this turn's promise to the owner."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - tail_bytes))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isSidechain"):
            continue
        msg = d.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            if any(p.strip() for p in parts):
                return "\n".join(parts)
    return ""


def has_open_ask():
    """Is anything on the owner-ask ledger still OPEN?

    The discharge is deliberately coarse. Matching a declination to its own ask
    row would need semantics helm does not have, and a guard that guesses at
    that would refuse correct work. Coarse-but-honest: if you are declining
    things and the owner's ledger is empty, nothing has been surfaced to him at
    all, which is the condition the rule actually forbids.

    THIS FUNCTION SHIPPED VACUOUS AND ITS OWN TEST DID NOT NOTICE (caught
    2026-07-29 by running the gate through the real Stop hook, minutes after
    writing it). `ownerasks.rows()` returns a DICT keyed by id, not a list. The
    first version iterated it directly, so `r` was a string, `r.get` raised
    AttributeError, the blanket `except` swallowed it, and fail-open returned
    True — the gate was permanently discharged and looked fine. The unit test
    that "covered" this mocked rows() with side_effect=OSError, exercising only
    the failure path and never the real shape. Bug class: laundered-vacuity;
    the lesson is that a fail-open branch hides a type error perfectly.

    UNAVAILABLE IS NOT EMPTY. snapshot() reports unreadable/unsafe storage
    separately, and an unknown ledger must fail open (it cannot testify that
    nothing was surfaced) — but that is now a DECLARED state rather than an
    exception nobody sees."""
    try:
        from . import ownerasks
        rows, unavailable = ownerasks.snapshot()
        if unavailable:
            return True                   # cannot read: never block on it
        values = rows.values() if isinstance(rows, dict) else rows
        return any(isinstance(r, dict) and r.get("status") == "open"
                   for r in values)
    except Exception:                     # noqa: BLE001 — advisory, fail-open
        return True                       # unreadable ledger never blocks


def gate_lines(text, open_ask=None):
    """The Stop-hook block, or [] when there is nothing to say."""
    if _off("STOP_GUARD_PUNT"):
        return []
    hits = findings(text)
    if not hits:
        return []
    if has_open_ask() if open_ask is None else open_ask:
        return []
    # The headline must describe the class actually caught. A passive tee
    # DECLINED NOTHING and offered no excuse — telling its author "you declined
    # work and gave a reason" describes a different sentence than the one quoted
    # underneath, and a block whose diagnosis does not match its evidence gets
    # read as a false positive and then routed around.
    kinds = {c for c, _ in hits}
    if kinds == {"passive-tee"}:
        out = ["PUNT GATE — you TEED work and named nowhere for it to live, "
               "and the owner's ask ledger has nothing open:"]
    elif "passive-tee" in kinds:
        out = ["PUNT GATE — declined work with a class excuse, AND work teed "
               "with no owner; the owner's ask ledger has nothing open:"]
    else:
        out = ["PUNT GATE — you declined work and gave a reason from a known "
               "punt class, and the owner's ask ledger has nothing open:"]
    for cls, sent in hits:
        out.append("  [%s] %s" % (cls, sent))
    out.append("Owner canon: doing all the things necessary to get to the END "
               "RESULT is the job, and a reason built on the clock, on where "
               "the owner is, or on a disruption you did not MEASURE is a punt "
               "100%% of the time. Either do it now, or make it loud: "
               "helm asks add \"<what you are not doing and why>\" "
               "--needs \"<what only the owner can supply>\"   (--needs is "
               "REQUIRED — without it `asks add` exits 2 and this gate stays "
               "shut, which is a gate whose stated cure does not run).")
    if "passive-tee" in kinds:
        out.append("A tee is only honest with a HOME: name the dispatch, "
                   "ledger row, lane or owner seat in the same sentence, or "
                   "it is work that exists only in a sentence nobody owns.")
    return out


_USAGE = """usage: helm punt [--text T | --transcript P] [--json]
  What the Stop rung would say about a piece of prose. No argument reads the
  body on stdin. A verb that silently dropped `--tex` would report a clean
  turn for a question nobody asked — the same shape of lie this module exists
  to catch — so an unknown flag REFUSES."""


def cmd_punt(args):
    """helm punt [--text T | --transcript P] [--json] — what would the gate say."""
    if "--help" in args or "-h" in args:
        print(_USAGE)
        return 0
    from .cli import guard_tail
    rc = guard_tail("helm punt", [a for a in args if a.startswith("--")],
                    flags=("--json",), valued=("--text", "--transcript"),
                    usage=_USAGE)
    if rc is not None:
        return rc
    text = ""
    if "--text" in args:
        text = args[args.index("--text") + 1]
    elif "--transcript" in args:
        text = last_assistant_text(args[args.index("--transcript") + 1])
    else:
        import sys
        if not sys.stdin.isatty():
            text = sys.stdin.read()
    hits = findings(text)
    if "--json" in args:
        print(json.dumps({"findings": [{"class": c, "sentence": s}
                                       for c, s in hits],
                          "open_ask": has_open_ask()}))
        return 1 if hits else 0
    if not hits:
        print("helm punt: no dressed declination found")
        return 0
    for cls, sent in hits:
        print("[%s] %s" % (cls, sent))
    return 1
