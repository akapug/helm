#!/usr/bin/env python3
"""helm needs_act — the one classifier every wake surface asks: does THIS row
need an ACT from THIS seat, or is it FYI it can read at the next wake?

task/4019 slice A: measured over one night, the
integrator was woken 235 times, 50 of them by stop-guard blocks, each wake
re-reading a median 393k context. The wake classes that needed no act from
the woken seat: rows @-addressed to another seat (two of one seat's first
three one-shot wakes were "@<another seat> ...: meld-diff-applied-3937", a row
stamped with only that other seat in its addressees) and stale-bot sweeps
whose proposals were all keep (one sweep woke ~10 seats in 3 minutes).

THE LAW, in order:

  * UNCLASSIFIABLE IS ACT. A suppressed wake for a row that needed an act is
    the expensive direction; a spurious wake is the cheap one. Every probe
    below that cannot answer says ACT.
  * THE OWNER IS ACT (machine_senders.owner_rail: an owner-rail origin AND
    one of his names). His rows are never FYI for anybody.
  * A STALE-BOT ALL-KEEP SWEEP IS FYI (cure 1c): from stale-bot, tagged
    [stale-bot], every numbered line's PROPOSED disposition is a keep, and the
    numbered lines account for every row the header counts (a capped digest
    whose remainder is unshown cannot be proven all-keep). Only a proposed
    close, move, supersede, redispatch or escalate is ACT. The sweep's
    structured disposition outranks the URGENT words below (an aged
    dispatch's line always reads "past its deadline").
  * A SEAT RECOVERY NOTICE IS FYI: from seat-events, leading after its
    mentions with seatevents.FYI_LEAD (a family came back), and naming no
    deadline or failure. The opening event (a wall, an outage) stays ACT.
  * A DEADLINE OR FAILURE ROW IS ACT (URGENT below), whoever it names.
  * A DIRECT ADDRESS IS ACT: a reply to this seat's row, a reaction to it, an
    @mention of any of its names. A DM carries no addressee stamp, so it can
    never match the FYI shape below.
  * A ROW ADDRESSED TO OTHER SEATS IS FYI (cure 1b): its `addressees` stamp
    (chat.post's capability snapshot) is non-empty, every entry resolved to a
    JOINED seat, none is this seat, and the text carries no @all broadcast.
    A malformed, ABSENT or UNKNOWN addressee makes the row unclassifiable.
    A row with no stamp keeps its old answer.

Nothing here consumes, parks or drops a row: FYI means "delivered at the
next wake", and every cursor, ledger row and ACK path is untouched.
"""

import re

from . import machine_senders, seatevents
from .seats_address import _mention_re
from .seats_common import MAX_BYTES, _BROADCAST, _clip, _scrub, names_match

STALEBOT = "stale-bot"
_STALEBOT_TAG = "[stale-bot]"
# stalebot.digest_text: "@<owner> [stale-bot] <N> aged or cure-awaiting
# row(s) ..." then one "<i>. <id> (<kind>, <why>) PROPOSED <terminal>: ..."
# line per SHOWN row. Only a numbered line's word counts: the header's
# "PROPOSED dispositions below" is prose.
_HEADER = re.compile(r"\[stale-bot\]\s+(\d+)\s+aged", re.I)
_PROPOSED = re.compile(
    r"^\d+\.\s+\S+.*?\bPROPOSED\s+([A-Za-z][A-Za-z0-9-]*)\s*:", re.M)
# stalebot.KEEP is the only disposition that asks nothing of the owner.
_KEEP = frozenset({"still-live-keep"})
# A deadline or a failure is ACT for every reader it reaches. Broad on
# purpose: a false hit costs one wake, a miss can cost a missed deadline.
URGENT = re.compile(
    r"\b(deadline|overdue|due\s+(?:by|before|at|in)|veto\s+window"
    r"|expir(?:e|es|ed|ing|y)|fail(?:s|ed|ing|ure|ures)?|refused|broken"
    r"|outage|error|regress(?:ed|ion|ions)?|blocker|urgent|p0)\b", re.I)


def _text(row):
    text = row.get("text") if isinstance(row, dict) else None
    return text if isinstance(text, str) else None


def urgent(row):
    """True when the row's text names a deadline or a failure."""
    text = _text(row)
    return bool(text and URGENT.search(text))


def stalebot_all_keep(row):
    """True only for a stale-bot sweep PROVEN to propose nothing but keep."""
    if not isinstance(row, dict) \
            or str(row.get("from") or "").strip().casefold() != STALEBOT:
        return False
    text = _text(row)
    if not text or _STALEBOT_TAG not in text:
        return False
    head = _HEADER.search(text)
    words = _PROPOSED.findall(text)
    if not head or not words or int(head.group(1)) != len(words):
        return False            # unparseable, or rows the digest did not show
    return all(w.casefold() in _KEEP for w in words)


def seat_event_recovery(row):
    """True for a #seats recovery notice (seatevents.RECOVERIES): posted by
    seat-events and leading, after its mentions, with seatevents.FYI_LEAD."""
    return isinstance(row, dict) \
        and str(row.get("from") or "").strip().casefold() == seatevents.WHO \
        and seatevents.is_recovery(_text(row))


def addressed_elsewhere(row, names):
    """True when the row is PROVABLY addressed to other seats and not this one
    (`names`: this seat's name plus its live aliases, seats_address.seat_names).
    Every shape it cannot prove answers False, which keeps the caller's
    existing answer."""
    if not isinstance(row, dict):
        return False
    addrs = row.get("addressees")
    text = _text(row)
    if not addrs or not isinstance(addrs, list) or text is None:
        return False
    if row.get("dm") or row.get("react") or _BROADCAST.search(text):
        return False
    if machine_senders.owner_rail(row) or urgent(row):
        return False
    if names_match(row.get("rfrom"), names):
        return False            # a reply to this seat's row addresses it
    for a in addrs:
        if not isinstance(a, dict) or a.get("error") \
                or a.get("membership") != "JOINED" or not a.get("canonical"):
            return False        # an addressee it cannot resolve: unclassifiable
        if names_match(a.get("canonical"), names) \
                or names_match(a.get("raw"), names):
            return False
    return not any(_mention_re(n).search(text) for n in names if n)


def needs_act(row, names):
    """Does this row need an ACT from the seat whose names are given?
    True unless the row is provably a stale-bot all-keep sweep, a seat
    recovery notice naming no failure, or addressed to other seats only."""
    if not isinstance(row, dict):
        return True
    if machine_senders.owner_rail(row):
        return True
    if stalebot_all_keep(row):
        # The sweep's own structured disposition outranks URGENT: an aged
        # dispatch's line always says "past its deadline", and the bot has
        # already weighed that and proposed keep.
        return False
    if seat_event_recovery(row) and not urgent(row):
        return False
    return urgent(row) or not addressed_elsewhere(row, names)


def lead_mention(text, names):
    """`text` (raw, as posted), led by this seat's @mention when the delivery
    line (scrubbed, then clipped to seats_common.MAX_BYTES) would cut off
    every mention of it. An alarm that names its seats after a long body
    (scratch-gc's backup line) otherwise shows its reader a line addressed to
    nobody, and a real ACT reads as FYI. The lead says WHO the row wakes and
    WHY: this seat, @-named past the cut. Nothing is removed from the text,
    and a row whose mention shows is returned unchanged."""
    if not isinstance(text, str) or len(text.encode("utf-8")) <= MAX_BYTES:
        return text             # scrubbing only shortens: nothing is cut
    hits = [(n, _mention_re(n).search(text)) for n in names or () if n]
    hits = [(n, hit) for n, hit in hits if hit]
    shown = _clip(_scrub(text))
    if not hits or any(re.search(r"@%s(?![A-Za-z0-9._-])" % re.escape(n),
                                 shown, re.I) for n, _hit in hits):
        return text
    return "%s, named past the cut: %s" % (hits[0][1].group(0), text)
