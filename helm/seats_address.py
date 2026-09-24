#!/usr/bin/env python3
"""helm seats — addressing: the mention grammar, a seat's delivery scope, and
the names a row addressed to a seat may carry.

A SEAT IS ADDRESSED BY ITS NAME AND, FOR A WINDOW, BY THE NAME IT HAD. The
rename event on a roster row (seats_common.row_alias, resolved both ways) is the only source
of that second name, and `seat_scope` is where it is read — once per scan
pass — so the per-row predicates in seats_identity.deliverable and the
catchup's `_addressed` compare against a list instead of the roster.

Extracted from seats_identity when the alias lane pushed it past the split
budget; every previous importer still finds these names there (re-exported)
and the facade is unchanged.
"""

import re

from . import pk
from .seats_common import alias_names, seat_row


def _mention_re(seat):
    return re.compile(r"(?<![A-Za-z0-9._-])@" + re.escape(seat)
                      + r"(?![A-Za-z0-9._-])", re.I)
def mentions(text, name):
    """Does `text` address `name` as an @mention? THE canonical answer.

    Boundary-aware and case-insensitive, sharing _mention_re with the delivery
    path so a row that WAKES a seat and a row that READS as addressed to it can
    never disagree. A raw `"@" + name in text` substring test is the bug this
    exists to prevent: it matches @dariason and @daria-extra for the name
    daria, spending attention on rows that address someone else."""
    if not text or not name:
        return False
    return bool(_mention_re(name).search(text))
def seat_scope(seat, r=None):
    """The seat's beacon tuning + roster admission, one roster read. Computed
    ONCE per scan pass and threaded down — the poll path stays ~one stat per
    quiet room. `tracked` cannot depend only on the primary cursor: the primary
    room may not have existed when an otherwise-joined seat entered."""
    # THE ROSTER'S OWN SPELLING, not the caller's: a case variant read here as
    # an absent row, so a JOINED seat answered tracked=False to delivery,
    # catchup, receipts and the stop fingerprint alike. An ambiguous roster is
    # left to answer empty rather than guess, which is the same refusal
    # `write_roster` makes on the way in.
    row = (seat_row(seat, r)[0] or {}) if seat else {}
    return {"home": row.get("home_room"),
            "mute": {pk.slug(x) for x in row.get("mute") or []},
            "tracked": bool(row.get("joined") or row.get("session")
                            or row.get("sessions")),
            # the OLD name a live rename alias still answers to — resolved
            # both ways (seats_common.row_alias) once per scan, so the
            # per-row address checks read a list instead of the roster
            "aliases": alias_names(seat, r) if seat else []}


def seat_names(seat, scope=None):
    """[seat, *live aliases] — every name a row addressed to `seat` may
    carry. `scope` is the precomputed seat_scope; None reads the roster."""
    sc = scope if scope is not None else seat_scope(seat)
    return [seat] + [a for a in sc.get("aliases") or () if a]
