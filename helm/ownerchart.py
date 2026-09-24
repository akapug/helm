"""The owner's progress chart: an ASCII bar dashboard, rendered only when away.

IT IS A SMALL STOP HOOK, and the only-when-away condition is the hard part —
everything else here is rendering.

WHY A CHART AND NOT PROSE: it exists to keep fleet dialect away from a reader
who does not speak it. The acceptance test is that the operator can gauge
progress without interpreting terminology — so no lane names, no gate tokens,
no shas, no fleet dialect. If a line here needs a glossary it has failed.

PRIOR ART, AND WHY THIS IS NOT A SECOND COPY OF IT. `helm brief` already ships
a section of this exact name, so the resemblance is the first thing to settle:
  - brief's WAITING ON YOU (helm/brief.py `_waiting`) composes interview
    status, the queued-attestation count and stale registry project paths, and
    never reads board.owner_gated_queue. The two sets are DISJOINT under one
    heading, so this renders what brief structurally cannot see rather than
    restating it.
  - brief's stamp parser (helm/brief.py `_ts_epoch`) fail-opens an unparseable
    stamp to 0. That is correct for a WINDOW test, where 0 falls in no window,
    and wrong for an AGE, where it renders as some twenty thousand days — and
    it does not carry the seconds-omitted shape the board actually writes.
    Opposite failure semantics, so `_age` below refuses rather than reusing it.
  - brief is PULL and he types it; this is PUSH and only while he is away.
DECISION: build, not extend.

THIS MODULE ONLY RENDERS. It gathers nothing: every figure comes from board
keys the owner's console already reads. A second collector would be a second
source of truth about the same work, and the two would disagree on the day it
mattered.
"""

import calendar
import os
import re
import time

# ONE SENTINEL, ONE READER. The /afk skill is explicit that posture is declared
# by the owner's WORDS and that away state, when wired mechanically, rides the
# reflex layer's marker-file signal — "written/removed only on the owner's
# word". So this module READS a flag and never writes or infers one.
#
# INFERRING AWAY FROM ACTIVITY WOULD BE THE BUG. "He has not posted in 20
# minutes" is not absence, it is a quiet stretch, and a chart that fires on it
# would interrupt him while he is typing — which is precisely the failure the
# only-when-AFK condition exists to prevent. An inferred posture is also the
# second away-state mechanism the skill bans by name.
# THE SENTINEL IS NOT DEFINED HERE. helm/away.py owns it, because away.py is
# what WRITES it and a sentinel belongs to its writer — otherwise two modules
# agree on a filename by coincidence and drift apart the day one is edited.
#
# THE RULE IS IN away.py AND THIS FILE ONCE VIOLATED IT. The chart read the
# flag before the verb existed, so it defined the path first and kept its own
# copy after the verb arrived. A duplicated constant is not a style problem: it
# is two sources of truth about whether a person is at their desk, and they
# drift the day one is edited. This module now has NO opinion about where the
# flag lives.
from .away import (is_away as _is_away, marker_path as away_marker,
                   declared_by as _declared_by)  # noqa: F401


_FULL, _HALF, _EMPTY = "█", "▒", "░"
_WIDTH = 12

# CAPS, BECAUSE AN UNCAPPED PUSH SURFACE SPENDS THE ATTENTION IT EXISTS TO
# SERVE. The live board renders 16 task rows today and nothing bounded it: at
# 100 rows this prints 100 lines on EVERY clean stop, forever, while he is
# away. Numbers follow brief.py's existing call on the same question rather
# than being re-derived (MAX_WAITING/MAX_SEATS 6, MAX_BUILT 12, "~40-line
# render cap") — one house answer, not two.
#
# A DROPPED ROW IS ALWAYS COUNTED OUT LOUD. Silent truncation reads as "that
# is everything", which is the same lie the missing age told, and this chart
# exists because of that lie. WAITING ON YOU is capped highest and its
# remainder is stated most plainly: a partial view of what blocks HIM is the
# one thing this surface must never hand him.
_MAX_TASKS = 12
_MAX_SEATS = 6
_MAX_WAITING = 6


def _fold(rows, cap, noun):
    """(rows to render, the '+N more' line or None) — never a silent cut."""
    rows = list(rows)
    if len(rows) <= cap:
        return rows, None
    return rows[:cap], "  ... and %d more %s not shown" % (len(rows) - cap, noun)



def away(marker=None):
    """True while the owner's away flag exists. Never infers, never writes.

    A THIN PASS-THROUGH TO away.is_away, kept as a name because the stop seam
    already calls ownerchart.away() and the arms patch it. What it must never
    do again is re-implement the question.
    """
    return _is_away(marker)


def _bar(stage):
    """LIVE -> full, building -> half, anything else -> empty.

    WHAT A FULL BAR ACTUALLY CLAIMS, stated precisely because the loose version
    is the more flattering one: it claims THE BOARD RECORDS THIS AS LIVE. It
    does not claim this module verified anything. The chart is exactly as true
    as the board and deliberately has no second opinion — a second collector
    would be a second source of truth about the same work, and the two would
    disagree on the day it mattered. That is why the freshness line exists: it
    hands him the age of the evidence so he can discount it himself.

    UNKNOWN STAGES READ EMPTY, NOT FULL. A stage word this function does not
    recognise is a piece whose progress is UNKNOWN, and the honest rendering of
    unknown progress is an empty bar — an unrecognised word must never inherit
    the confident end of the scale.
    """
    s = str(stage or "").lower()
    if "live" in s:
        return _FULL * _WIDTH, "done"
    if "build" in s:
        return _HALF * (_WIDTH // 2) + _EMPTY * (_WIDTH - _WIDTH // 2), "in progress"
    return _EMPTY * _WIDTH, "not started"


# THE BOARD'S TITLES CARRY FLEET DIALECT AND THIS IS WHERE IT GETS STOPPED.
# MEASURED on the live board, described rather than quoted (a module whose job
# is stripping shas has no business carrying five of them in its own source,
# and the docref rung is right to refuse them): a majority of task titles end
# in an arrow followed by one to three abbreviated commit hashes, e.g. a
# runbook row and a watchdog row each trailing one, and a class-of-bug row
# trailing three. The standing rule is no shas unless he asks, and a
# renderer that passes them through defeats the one thing the chart is for.
# Stripped HERE rather than at the writer, because the board is the fleet's
# working record and seats legitimately put shas in it — this is the surface
# that owes him plain words, not the store.
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_ARROW_TAIL = re.compile(r"\s*->.*$")


def _plain(text):
    """A title he can read: no shas, no arrow tails, no trailing punctuation."""
    t = _ARROW_TAIL.sub("", str(text or ""))
    t = _SHA.sub("", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -/(,;:")
    return t


# NO CLOCK LIVES IN THIS MODULE. `now` is passed in by the caller, for the
# same reason the away flag is: a surface that reaches for its own time can
# make claims nobody handed it. Given no clock this renders NO ages rather
# than guessing one — a missing age is honest, an invented one is not.
def _age(since, now):
    """'waiting 22 days' from a board `since` stamp, or '' when unknowable.

    THE CONTRACT `since` CARRIES, ADDRESSED TO WHOEVER WRITES A QUEUE ROW
    rather than to whoever reads this module, because they are the ones who
    have to honour it: `since` means HOW LONG THIS QUESTION HAS BEEN OPEN. A material rewrite of an ask RESETS it and
    keeps the history in the why-line. Queue rows are verb-immutable by
    design and the owner discharges one by CLOSING it, so in practice this
    almost never fires — but if a stale `since` ever rides a fresh question,
    this renderer will print a true number attached to the wrong claim, which
    is worse than the missing age it was built to fix: an absent age hides an
    old block, an inherited one invents one.

    THE BOARD USES TWO STAMP SHAPES — measured, not assumed: one row reads
    2026-08-26T09:14:38Z and another 2026-08-04T19:15Z, seconds omitted. A
    parser that knows only one shape silently drops the age off the OLDER
    rows, which are exactly the ones he needs to see.
    """
    if not since or now is None:
        return ""
    t = str(since).strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            stamp = calendar.timegm(time.strptime(t, fmt))
            break
        except ValueError:
            continue
    else:
        return ""
    mins = (now - stamp) / 60.0
    if mins < 0:
        return ""            # a future stamp is a broken row, not an age
    if mins < 90:
        return "waiting %d min" % int(mins)
    if mins < 60 * 36:
        return "waiting %d hours" % int(mins // 60)
    return "waiting %d days" % int(mins // 1440)


def _ask_detail(why, width):
    """The actionable half of a why-line.

    A `decide` card's row carries its context paragraph AND an `— options: …`
    tail. Clipping from the front keeps the prose and throws away the choices,
    which is backwards: the options are the thing he can ACT on. So when the
    tail is there it wins the space.
    """
    text = str(why or "")
    marker = "— options: "
    if marker in text:
        return _clip("choices: " + text.split(marker, 1)[1], width)
    return _clip(text, width)


def _clip(text, n):
    """Clip on a WORD boundary — a title cut mid-word reads as a typo to him."""
    t = _plain(text)
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return (cut or t[:n]).rstrip(" -/(,;:") + "..."


def burn_line(value):
    """The `burn_flags` board scalar -> one plain line, or None.

    BOARD-KEY-ONLY AND NO DIALECT. This reads the one string the watchdog put
    on the board and gathers nothing else, per this module's contract. The
    colour WORD is translated into what he should do about it, because a
    colour name is fleet dialect and `GREY` most of all — it renders as *not
    measured*, never as a colour and never as green. The verb pointer at the
    end is for agents and is dropped here: he is reading a chart, not a
    command line.

    An unrecognised string renders VERBATIM rather than not at all: a line
    nobody can parse is still a line he can read, and dropping it would hide
    the one figure this section exists to show."""
    from .burnflags import BEHAVIOUR, COLOURS, GREY
    text = str(value or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split(";") if p.strip()]
    parts = [p for p in parts if not p.startswith("read helm")]
    head = parts[0] if parts else ""
    if head.startswith("burn "):
        head = head[len("burn "):]
    colour = next((c for c in COLOURS if head == c), None)
    if not colour:
        return "  " + _plain(text)
    plain = ("not measured yet — treat it as normal work and say so"
             if colour == GREY else BEHAVIOUR[colour]["say"])
    room = BEHAVIOUR[colour]["capacity"]
    line = "  about %d project%s at once — %s" % (
        room, "" if room == 1 else "s", plain)
    for part in parts[1:]:
        line += "; " + _plain(part)
    return line


def render(board, width=_WIDTH, now=None, as_of=None):
    """The chart, or None when there is nothing worth waking him for.

    `now` (epoch seconds) and `as_of` (the board's own mtime, epoch seconds)
    are supplied by the CALLER and never read here. Both are optional and both
    degrade honestly: no clock renders no ages, no mtime renders no freshness
    line. Nothing is ever guessed to fill the space.
    """
    tasks = [t for t in (board.get("tasks") or []) if isinstance(t, dict)]
    building = [b for b in (board.get("building") or []) if isinstance(b, dict)]
    waiting = [r for r in (board.get("owner_gated_queue") or [])
               if isinstance(r, dict) and str(r.get("state", "")).startswith("waiting")]
    # THE GUARD IS DERIVED FROM WHAT RENDERS, NOT FROM WHAT WAS SUPPLIED. It
    # tested raw `building` rows while the section below only renders rows
    # carrying BOTH a lane and a seat — so a board of malformed building rows
    # passed the guard and produced a body with no sections in it. My own
    # comment here argued for deriving the answer from the sections rather
    # than restating them, and then restated them.
    seats = {}
    for b in building:
        lane, seat = str(b.get("lane") or ""), str(b.get("seat") or "")
        if lane and seat:
            seats.setdefault(lane, seat)
    if not tasks and not waiting and not seats:
        return None

    # EVERY SECTION IS INDEPENDENT, AND THE EARLY RETURN USED TO FORGET ONE.
    # It checked tasks and waiting but not `building`, so a board carrying only
    # in-flight work rendered NOTHING — the one state where an operator most
    # wants to see that something is moving. A guard that enumerates sections
    # has to enumerate all of them, which is the argument for deriving the
    # answer from the sections rather than restating them.
    out = []
    # HOW MUCH MAY RUN COMES FIRST, because it is the figure that changes what
    # he does next. It never WAKES him on its own — the guard above is
    # unchanged, and a colour with no work behind it is not worth an
    # interruption.
    burn = burn_line(board.get("burn_flags"))
    if burn:
        out += ["", "HOW MUCH CAN RUN NOW", "", burn]
    if tasks:
        out += ["", "WHERE THE WORK IS", ""]
    # UNFINISHED WORK OUTRANKS FINISHED WORK FOR THE SPACE. When the cap bites,
    # a screen of full bars is the least useful thing it could keep.
    tasks, folded = _fold(sorted(tasks, key=lambda t: _bar(t.get("stage"))[1] == "done"),
                          _MAX_TASKS, "pieces")
    for t in tasks:
        bar, word = _bar(t.get("stage"))
        title = _clip(t.get("t") or t.get("owner") or "untitled", 56)
        if title:
            out.append("  %s  %-12s %s" % (bar, word, title))
    if folded:
        out.append(folded)
    if seats:
        out += ["", "WHO IS ON WHAT", ""]
        # SORTED BY WHAT IS PRINTED FIRST. seats.items() orders by LANE while
        # the line leads with the SEAT, so sorting the dict left his eye a
        # jumbled left column.
        pairs, more = _fold(sorted(seats.items(), key=lambda kv: (kv[1], kv[0])),
                            _MAX_SEATS, "seats")
        for lane, seat in pairs:
            out.append("  %-22s %s" % (seat, _clip(lane.replace("-", " "), 46)))
        if more:
            out.append(more)
    if waiting:
        out += ["", "WAITING ON YOU", ""]
        # OLDEST FIRST. The dogfood run is what made this non-negotiable: a row
        # had been waiting on him since 2026-08-04 — 22 days — and the chart
        # rendered it indistinguishable from one filed that minute. A
        # waiting-on-you list that hides a three-week block is worse than no
        # list, because it looks like it has already told you everything.
        rows, more = _fold(sorted(waiting, key=lambda r: str(r.get("since") or "~")),
                           _MAX_WAITING, "things waiting on you")
        for r in rows:
            age = _age(r.get("since"), now)
            # THE AGE DOES NOT GET TO EAT THE ASK. Clipping an age-bearing row
            # to 52 characters to make room renders live asks as
            # "<some> relogin (device-auth) for..." — dropping WHICH account,
            # the one word that identifies it. On the section whose whole job
            # is telling him what is blocked on him, trading the identifying
            # tail for the age is backwards: he can act on the subject and
            # cannot act on a number.
            # The line runs to about 91 characters, which a terminal is fine
            # with and a truncated subject is not.
            head = _clip(r.get("ask"), 68)
            if not head:
                # A ROW WITH NO ASK MUST NOT RENDER AS A BARE AGE. Without this
                # the line came out as "   [waiting 21 days]" with no subject —
                # an unlabelled block, which is worse than an absent one
                # because it spends his attention and cannot repay it. Fall
                # back to the why-line, which is the only other thing that
                # might name the subject, and label it plainly if that is
                # empty too so the row is still countable.
                head = _clip(r.get("why"), 68) or "(unlabelled request — see the board)"
                detail = ""
            else:
                detail = _ask_detail(r.get("why"), 64)
            out.append("  %s%s" % (head, ("   [%s]" % age) if age else ""))
            if detail:
                out.append("      %s" % detail)
        if more:
            out.append(more)
    # THE FRESHNESS LINE. Twelve full bars with no date is a chart that says
    # "finished" about a board nobody has touched in three days. He cannot
    # discount evidence whose age he cannot see, and this module refuses to
    # verify anything itself, so handing him the age IS the honesty mechanism.
    if as_of is not None and now is not None:
        mins = max(0.0, (now - as_of) / 60.0)
        if mins < 90:
            stale = "%d min ago" % int(mins)
        elif mins < 60 * 36:
            stale = "%d hours ago" % int(mins // 60)
        else:
            stale = "%d days ago" % int(mins // 1440)
        out += ["", "  (progress above is whatever the board last recorded, "
                "updated %s)" % stale]
    # THE OFF-SWITCH, IN HIS WORDS, ON THE ARTIFACT ITSELF. A dashboard a man
    # cannot dismiss is a trap, and "ask an agent to stop it" is not a
    # dismissal he can perform alone. So the line names the VERB that lifts the
    # posture, not the file under it: `helm back` is the surface, and a raw
    # path would hand him a route around the only one that is supported.
    # THE ATTRIBUTION IS CONSUMED HERE, which is the only reason recording it
    # is worth anything. Nothing stops a machine declaring a person away; what
    # this can promise is that if one did, the reader sees which one — and
    # visibility that lives only in a file nobody opens is not visibility.
    out += ["", "  (%s — say `helm back` when you return)"
            % _who_marked(_declared_by()), ""]
    return "\n".join(out)


def _who_marked(by):
    """The attribution clause, from the marker's `<name> (<provenance>)`.

    A flag set through one of the OWNER'S OWN DOORS (away.declare with an
    OwnerDoor writes `<owner> (web)`) is his act: the card he pressed says
    "You set this here", and this line must not tell him a third party who
    happens to carry his name did it. Every other declarer is named exactly
    as before, provenance included, so a machine declaring a person away
    stays visible."""
    if not by or by == "an unnamed process":
        return "you are marked away"
    _name, _, via = by.rpartition(" (")
    if via.endswith(")"):
        from .seats import OWNER_RAILS
        if via[:-1] in OWNER_RAILS:
            return ("you are marked away (you set it from your %s console)"
                    % via[:-1])
    return "%s marked you away" % by
