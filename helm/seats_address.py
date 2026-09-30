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
def seat_scope(seat, r=None, own_room=None):
    """The seat's beacon tuning + roster admission, one roster read. Computed
    ONCE per scan pass and threaded down — the poll path stays ~one stat per
    quiet room. `tracked` cannot depend only on the primary cursor: the primary
    room may not have existed when an otherwise-joined seat entered.

    `own_room` NARROWS the scope to the seat's own mail. The Orca door's
    delivery leg passes it for a pane working in a project that is not helm's
    (hooks.orca_foreign), naming the room the pane homed to from its own cwd.
    That room replaces the roster's home, and `project_only` tells
    `deliverable` that #main is no longer an @all room for this pass. A DM,
    an @mention, a reply and a reaction still reach the seat in any room; a
    plain row reaches it only in its own project's room; an @all in #main,
    in #helm or in another project's room does not (premise
    feedback-project-seat-context-stays-in-project). "main" names no project,
    so it leaves the seat no home room at all.

    ONE ROW STILL CROSSES: a row the seat's OWN beacon already rang the
    doorbell for is owed to this hook (`deliver` reads a held token before
    this scope), because the seat was told a row is waiting and a held token
    left unreleased pins room rotation. The beacon's own scope decides that,
    and this narrowing does not reach it."""
    # THE ROSTER'S OWN SPELLING, not the caller's: a case variant read here as
    # an absent row, so a JOINED seat answered tracked=False to delivery,
    # catchup, receipts and the stop fingerprint alike. An ambiguous roster is
    # left to answer empty rather than guess, which is the same refusal
    # `write_roster` makes on the way in.
    row = (seat_row(seat, r)[0] or {}) if seat else {}
    sc = {"home": row.get("home_room"),
          "mute": {pk.slug(x) for x in row.get("mute") or []},
          "tracked": bool(row.get("joined") or row.get("session")
                          or row.get("sessions")),
          # the OLD name a live rename alias still answers to — resolved
          # both ways (seats_common.row_alias) once per scan, so the
          # per-row address checks read a list instead of the roster
          "aliases": alias_names(seat, r) if seat else []}
    if own_room is not None:
        sc.update(home=None if own_room == "main" else own_room,
                  project_only=True)
    return sc


def boundary_scope(seat, room, project_only=False):
    """THE scope `seat`'s tool-boundary hook delivers under: the roster's
    (`seat_scope`), narrowed to its own mail and `room` when the Orca door
    admitted a pane working in a project that is not helm's (`project_only`,
    the hook's `hooks.orca_foreign`; `room`, the room the hook homed the
    pane to from the session's cwd through `resolve_homing`). ONE SOURCE
    (task/3382): `seats_delivery.deliver_any` scopes the hook's pass here,
    and the short read asks here which rows the hook owes its reader
    (`chatshort.owed_to`), so the two never scope one seat two ways. The
    seat's beacon waiter reaches here with no `project_only`, so it scopes
    by the roster even for such a pane, and what it holds the hook shows:
    the short read asks both scopes (`boundary_scope(seat, None)` is the
    waiter's) and keeps a row either owes."""
    return seat_scope(seat, own_room=room if project_only else None)


def seat_names(seat, scope=None):
    """[seat, *live aliases] — every name a row addressed to `seat` may
    carry. `scope` is the precomputed seat_scope; None reads the roster."""
    sc = scope if scope is not None else seat_scope(seat)
    return [seat] + [a for a in sc.get("aliases") or () if a]
