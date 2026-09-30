#!/usr/bin/env python3
"""THE SHORT READ: `helm chat read` cut to one line for each row nobody owes
its reader, for a reader whose context is scarce (task/3382).

WHY. Measured over 24 h of the local seats' transcripts: one of them ran
`helm chat read` 558 times for 1.46M characters, 17 % of its context-window
growth. A read prints every row whole, and most of its characters are row
BODIES: a train's merge line, a review verdict, a brief of twenty lines. A
seat triaging what is new needs each row's author, its id and its first
words; the rest it fetches for the one row it acts on.

THE RULING: THE SHORT READ CUTS ONLY ROWS NOBODY OWES THE READER. A row owed
to the reader prints WHOLE, through `chat.run_line`, exactly as a full read
prints it, its newlines and indentation intact; so does every row one of the
owner's doors stamped. Only a row the reader is not owed may be cut. Owed is
what the delivery layer owes (`owed_to`): a row its tool-boundary hook would
show it (`seats_identity.deliverable` on the hook's own tier, ambient=True,
under the hook's own scope, `seats_address.boundary_scope`: an @mention, a
reply to it, a reaction to its row, an @all, a DM, a plain row a person
posted in its home room, which for a pane Orca opened in another project is
the room it homes to from its cwd), a row the reader's beacon waiter would
ring or hold for that hook under the roster's scope, which the waiter keeps
for such a pane too, and a row its wake cursor holds for that hook (`held`:
a row the doorbell rang, or one a mute or an owner rail held), whatever the
reader's scope says now. So a cut row is never a row the hook later shows
clipped to MAX_BYTES and moves the delivery cursor past: before this ruling
a lean seat's long brief reached it as 160 characters here plus 200 bytes
there, and nothing else. Every input `owed_to` reads fails toward whole: a
question it cannot answer keeps the row.

THE SHAPE. A row the reader is not owed prints on ONE line when its body,
its whitespace folded to single spaces, is longer than SHORT_CHARS: the same
`[n] <id>` prefix, stamp and author as a full read, the folded body cut
there, and `… [+K chars]`. Every other row, an ack and a reaction among
them, prints as a full read prints it. When a row was cut, the read ends
with ONE line naming the reader that prints a whole row, `helm chat read
--id <id>`, which finds the id (or an unambiguous prefix) in every live room
and DM lane the way `helm chat ack` does (`seats_ack._locate_row`). A window
with no row in it says so in one line, and a room with no row at all keeps
the full read's answer, which tells an empty room from an absent or
unreadable one.

WHO GETS IT. `--short` asks for it and `--full` refuses it. With neither, a
reader whose model family the catalog saves context for
(`seat_catalog.context_lean`: the families denied LOCAL_UNUSED_TOOLS for the
context their schemas cost) reads short, and every other reader, a native
seat or the owner's shell, reads what it read before, byte for byte. The
family is the one the seat's launch stamped into its environment
(HELM_MODEL_FAMILY, the self-report `seats_runtime` records): it chooses a
rendering and authorizes nothing. The family and its backend are ONE stamp,
so both come from one namespace (`home.env_pair`): a HELM_ family never takes
a stale MELD_ backend, and the other way round. The reader whose rows are
owed is the actor the delivery door admits for the read (`actors`, the door
`pull_delivery` and `helm chat ack` take). A read no door admits (the
owner's shell, a seat whose identity is disputed) cannot say which rows
nobody owes, so it cuts none and says so in one stderr line. A DM lane holds
only its reader's own conversation, so it cuts none without asking.

THE OWNER'S ROWS ARE NEVER CUT, by their ORIGIN alone (`owner_origin`,
`seats.OWNER_RAILS`), the test the owner-unread marker keeps: the web
surface posts under its name box and the TUI under HELM_CHAT_NAME, neither
of which need be one of `owner_names()`, and both drop the marker on every
post.

THE DEFENCE STAYS. A cut line is a `pull_delivery.Partial`, which the
discharge does not count, so a row a read cut stays owed to the hook, as a
row outside the window does; `--id` delivers it. Its [n] acks nothing until
`--id` printed it whole (helm.seats_lastread). The owner-unread marker moves
past every row the read printed (`seen`); a cut row of the owner's origin,
which the read no longer makes, holds it at itself. A short read moves no
`--since` index.
"""
import sys

from . import chat, home
from .pull_delivery import Partial, discharge, filtered, whole

#: The characters of a row's folded body a short read prints before it cuts:
#: the doorbell's lead clip (beacon_doorbell.FIRST_LINE_CHARS), one width
#: across the two surfaces a local seat reads most.
SHORT_CHARS = 160


def lean(family, backend=None):
    """Does a reader of model `family` on `backend` read short by default? A
    seat with no family, or a native one, is no entry of the proxy family
    catalog, so it answers no without loading the catalog (about 100 ms,
    measured, on every read a native seat runs)."""
    family = str(family or "").strip().lower()
    if not family or backend == "native":
        return False
    from . import seat, seat_catalog  # noqa: F401 — the facade first (seat_compat)
    return seat_catalog.context_lean(family)


def mode(args):
    """(short, refusal) for one read's argv: whether it prints short, or the
    one line that refuses a contradictory combination before any row
    prints."""
    asked, full = "--short" in args, "--full" in args
    if asked and full:
        return False, ("helm chat read: --short and --full contradict each "
                       "other; pass one")
    if "--id" in args and (asked or any(
            x in args for x in ("--since", "--limit", "--follow"))):
        return False, ("helm chat read: --id prints whole rows, so it "
                       "takes no --since, --limit, --follow or --short")
    if asked and "--follow" in args:
        return False, ("helm chat read: --follow streams rows whole; --short "
                       "is for a read that returns")
    return asked or (not full and lean(
        *home.env_pair("MODEL_FAMILY", "MODEL_BACKEND"))), None


def owner_origin(m):
    """Did one of the owner's doors stamp row `m`? By its origin alone
    (`seats.OWNER_RAILS`), whatever name it carries."""
    from .seats import OWNER_RAILS
    return m.get("origin") in OWNER_RAILS


def _held(room, rows, seat, session):
    """The indexes into `rows` (the room as the read numbered it) whose
    occurrence `seat`'s wake cursor holds for its hook (`held`), read from
    the room's bytes as the discharge reads them (`pull_delivery._snapshot`).
    Nothing held costs one cursor read."""
    from .seats_delivery import _cursor
    tokens = set((_cursor(room, seat, session, beacon=True) or {})
                 .get("held") or ())
    if not tokens:
        return frozenset()
    from .pull_delivery import _snapshot
    from .seats_cursor import _occurrence
    dev, ino, entries = _snapshot(room)
    return frozenset(i for i, (row, start, _end) in enumerate(entries)
                     if i < len(rows) and row == rows[i]
                     and _occurrence(dev, ino, start) in tokens)


def _why(exc):
    """The first line of `exc`, sanitized, for a one-line stderr reason."""
    return (chat._dsan(str(exc)).splitlines() or [type(exc).__name__])[0]


def owed_to(room, rows, claimed=None, session=None):
    """-> keep(index, row): must the short read print this row of `rows`
    (the room it numbered) whole? True for every row the delivery layer
    owes the read's reader and every row of the owner's origin, so the read
    cuts only rows nobody owes it (the module docstring's ruling). A read no
    door admits keeps every row, and says so on stderr. A DM lane holds only
    its reader's own conversation, so it keeps every row without asking who
    reads it.

    THE HOOK'S OWN SCOPE, from the hook's own inputs: a pane the Orca door
    admits in a project that is not helm's (`hooks.orca_foreign`) is scoped
    by the room it homes to from the session's cwd (`resolve_homing`, the
    one resolver the hook re-homes its payload through; un-homed is main),
    not by the roster's home, and `seats_address.boundary_scope` is where
    both ask.

    AND THE WAITER'S SCOPE, THE UNION FAILING TOWARD WHOLE (R4 of the
    approval-tier read): the seat's beacon waiter calls `deliver_any` with
    no `project_only` (both of its legs, `seats_join.wait` and
    `beacon_doorbell.waiter_bell`), so it scopes by the roster, and a row it
    rings or holds there the hook shows whatever the hook's own scope says.
    So for such a pane a row is owed when EITHER scope owes it: an @all in
    #main after the waiter's last drain is owed though the pane's own scope
    drops #main. Both are asked on the hook's tier (ambient=True, not the
    beacon's): the waiter's ring (any --ambient, the beacon's mute) and its
    hold (the mute and owner-rail rows it holds unrung) are each a subset of
    it. Inside helm the two scopes are one call, asked once.

    EVERY INPUT FAILS TOWARD WHOLE: one the whole read asks (the actor, the
    roster, the pane test, the home room, either scope, the wake cursor)
    keeps every row, and `deliverable` raising on one row keeps that row,
    with one stderr line for it, and the read goes on."""
    if room.startswith(chat.DM_PREFIX):
        return lambda _i, _m: True
    session = session or home.session_id()
    try:
        from . import actors, hooks
        actor, err = actors.resolve_actor(
            session, asserted=claimed, act="cut the rows a read owes nobody")
        if err or actor is None:
            raise LookupError(err or "no seat was admitted")
        seat = actor.canonical_name
        from .seats_identity import (boundary_scope, deliverable,
                                     resolve_homing, safe_cwd)
        foreign = hooks.orca_foreign()
        scope = boundary_scope(seat, (resolve_homing(None, safe_cwd())[0]
                                      or "main") if foreign else None, foreign)
        # the waiter's own call (deliver_any with no project_only); inside
        # helm it is the hook's, so the roster is read once
        scopes = (scope, boundary_scope(seat, None)) if foreign else (scope,)
        held = _held(room, rows, seat, session)
    except Exception as exc:                            # noqa: BLE001
        print("helm chat read: no row is cut, because this read cannot say "
              "which rows nobody owes its reader (%s)" % _why(exc),
              file=sys.stderr)
        return lambda _i, _m: True

    def keep(i, m):
        try:
            return (bool(m.get("dm")) or owner_origin(m) or i in held
                    or any(deliverable(m, seat, room, sc, True, False)
                           for sc in scopes))
        except Exception as exc:                        # noqa: BLE001
            print("helm chat read: row %s prints whole, because this read "
                  "cannot say whether it is owed (%s)"
                  % (str(m.get("id") or "")[:chat.ID_SHOWN], _why(exc)),
                  file=sys.stderr)
            return True
    return keep


def line(run, tag, idx=None, keep=lambda _i, _m: True):
    """The short line for one run of a read (`chat.ack_runs`): the full
    read's line (`chat.run_line`) unless `keep` lets the run's one row be
    cut and its folded body is longer than SHORT_CHARS, then a `Partial`
    that prints the body cut on one line."""
    i, m = run[0]
    if len(run) > 1 or m.get("react") or m.get("ack") or keep(i, m):
        return chat.run_line(run, tag, idx)
    body = " ".join(str(m.get("text") or "").split())
    if len(body) <= SHORT_CHARS:
        return chat.run_line(run, tag, idx)
    return Partial(tag(i) + chat._fmt(m, idx=idx, body="%s… [+%d chars]" % (
        body[:SHORT_CHARS], len(body) - SHORT_CHARS)))


def renderer(room, rows, claimed=None):
    """The line function a short read of `room` prints with, in the place
    of `chat.run_line`: (run, tag, idx) -> line, over `owed_to`'s answer
    for the read's reader, computed once for the read."""
    keep = owed_to(room, rows, claimed)
    return lambda run, tag, idx=None: line(run, tag, idx, keep)


def seen(printed, total):
    """How far into its room a read with `printed` [(index, row, line)]
    showed the OWNER's rows whole: `total`, or the index of the first row of
    the owner's origin it cut. `chat.consume` clears the owner-unread marker
    only when this reaches the count at the owner's post.

    The marker says the owner's rows were read, and the short read never
    cuts a row of the owner's origin (`owed_to`), so a read moves it past
    every row it printed, whole or cut: a cut row of anyone else's, before
    the owner's post or after it, does not hold it. Were it to, one old long
    row before the owner's post would keep the owner-chat-unread steer firing
    on every prompt of a seat that reads short. A cut row of the owner's
    origin, by the same test (`owner_origin`), is the defence: it holds the
    marker at itself."""
    return next((i for i, m, x in printed
                 if isinstance(x, Partial) and owner_origin(m)), total)


def legend_line(n):
    """The one line a short read that cut `n` rows ends with."""
    return ("helm chat: %d row%s cut at %d characters (… [+K chars]); "
            "helm chat read --id <id> prints one whole"
            % (n, "s"[:n != 1], SHORT_CHARS))


def legend(printed):
    """Print `legend_line` when the read's `printed` [(index, row, line)]
    holds a cut row; print nothing when none was cut."""
    n = sum(1 for _i, _m, x in printed if isinstance(x, Partial))
    if n:
        print(legend_line(n), flush=True)


def empty(room, label, since, total):
    """What a short read says when its window holds no row: one line when
    the room has rows before `since`, else the full read's answer
    (`chat.empty_room_line`), which tells an empty room from an absent or an
    unreadable one."""
    if total:
        return ("helm chat [%s]: no rows past --since %d (the room holds %d)"
                % (label, since, total))
    return chat.empty_room_line(room, label)


def read_row(ref, claimed=None):
    """`helm chat read --id REF[,REF...]`: each row a REF names, whole, as
    the read of its room prints it, under `helm chat [<room>]`, in the order
    given; the ids are found in one pass over the lanes (the doorbell's ring
    names its one pull this way). It is a pull like any read, so the rows it
    printed are delivered to the seat it runs as (pull_delivery.discharge),
    and an [n] the seat's last read printed cut for one now acks it
    (seats_lastread.whole): both only for a row that reached the seat whole,
    never through a filter. A REF that names no row is refused on stderr and
    the others still print. -> exit code, 1 when any REF was refused."""
    from .seats_ack import _locate_rows
    refs = str(ref).split(",") if "," in str(ref) else [ref]
    rc, found = 0, []
    for row, room, err in _locate_rows(refs, verb="read --id"):
        if err:
            print("helm chat: " + err, file=sys.stderr)
            rc = 1
        else:
            found.append((row, room))
    cut, printed, reads = filtered(sys.stdout), [], {}
    try:
        for row, room in found:
            if room not in reads:
                reads[room] = chat.read(room)[0]
            rows = reads[room]
            i = next((k for k, m in enumerate(rows)
                      if m.get("id") == row.get("id")), None)
            if i is None:
                print("helm chat: row %s left %s between two reads; run it "
                      "again" % (row.get("id"), room), file=sys.stderr)
                rc = 1
                continue
            out = "helm chat [%s] %s" % (
                "dm" if room.startswith(chat.DM_PREFIX) else room,
                chat.run_line([(i, rows[i])], chat.read_prefix(rows),
                              chat.index_rows(rows)))
            print(out, flush=True)
            printed.append((room, i, rows[i], out))
    finally:
        # only what a harness showed whole, from the first line printed
        shown = printed[:whole([out for _r, _i, _m, out in printed])]
        for room in {r for r, _i, _m, _o in printed}:
            discharge(room, [(i, m, out) for r, i, m, out in shown
                             if r == room], claimed, cut=cut)
    if not cut:
        from . import seats_lastread
        for _room, _i, m, _out in shown:
            seats_lastread.whole(m.get("id"), claimed)
    return rc
