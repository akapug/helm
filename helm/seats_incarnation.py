"""Identity generations: the roster's one-per-row `incarnation` marker.

THE MARKER IS WHAT LETS A DEFERRED WRITER BE REFUSED. A seat's NAME is not its
identity — a rename moves a row between names, and a lawful key reuse gives one
name to a new row — so a caller that proved something about a seat and then
went away to do work cannot say WHICH row it proved unless that row carries a
token of its own generation. Everything here mints that token, compares it, or
says what to do about a row written before it existed.
"""

import json
import uuid

from . import pk
from .seats_common import _flocked, roster_for_write, roster_path

# THREE STATES A CALLER CAN PROVE, AND THE THIRD ONE IS A STRING. UNCHECKED is
# every ordinary caller and fences nothing. ABSENT says "there was no row when
# I proved this and there must still be none". A GENERATION STRING says "this
# exact incarnation or nothing".
#
# THERE IS NO SENTINEL FOR A MARKERLESS ROW, AND ITS ABSENCE IS THE FENCE. A
# sentinel meaning "some row with no generation" fences a CLASS rather than an
# IDENTITY, and every markerless row satisfies it — so a real rename can carry
# a DIFFERENT markerless row into the key between the proof and the admission
# and pass. A row that cannot be NAMED cannot be fenced, so the proof refuses
# it and names the migration below.
UNCHECKED = object()
ABSENT = object()

#: The one-shot cure a markerless row is refused with. Spelled once so the
#: refusal, the docs and the verb can never drift apart — and it carries
#: `--apply`, because `rebind --all` is DRY BY DEFAULT and a refusal that
#: names a command which changes nothing sends the reader round the loop
#: again. The preview is a separate, deliberate ask.
INCARNATION_MIGRATION = "helm seat rebind --all --apply"


def _markerless(rows):
    """The rows that carry no string generation, sorted."""
    return sorted(name for name, row in rows.items()
                  if isinstance(row, dict)
                  and not isinstance(row.get("incarnation"), str))


def _marked(rows):
    return sum(1 for row in rows.values()
               if isinstance(row, dict)
               and isinstance(row.get("incarnation"), str))


def preview_incarnations():
    """(would-stamp, already-marked, unreadable) — THE DRY READ, AND IT IS A
    DIFFERENT ACCESSOR ON PURPOSE.

    `roster_for_write` is a WRITE-PATH accessor: on a roster it cannot parse
    it performs corruption RECOVERY, `os.replace`-ing the canonical bytes
    aside so the write about to happen cannot delete the only copy. That is
    correct for a write and catastrophic for a preview — a dry run that
    relocates the canonical roster has mutated the thing it promised only to
    look at, and it reports ([], 0) while doing so.

    AN UNREADABLE ROSTER IS UNKNOWN, NEVER EMPTY. A preview that could not
    parse the file must not answer "nothing to stamp", because that is
    indistinguishable from a fully migrated fleet and is the reassuring
    direction."""
    # RESOLVING THE PATH IS NOT READING THE FILE, AND THE TWO FAILURES MEAN
    # OPPOSITE THINGS. `roster_path()` walks the home, and `os.getcwd()`
    # raises FileNotFoundError when the process's cwd has been removed — a
    # real schedule here, because a relative HELM_HOME plus a deleted cwd is
    # exactly what a sweep that removes its own directory leaves behind. Left
    # inside the try below, that resolution failure was caught by the
    # FileNotFoundError arm and answered "proven absent: nothing to stamp",
    # over a roster that exists and is full of unmigrated rows. Only the OPEN
    # may claim absence; a path that could not be resolved is UNKNOWN, which
    # is the same separation `roster_acquired` already draws.
    try:
        path = roster_path()
    except OSError as e:
        return [], 0, ("the roster path could not be resolved (%s: %s), so "
                       "what needs stamping is UNKNOWN — nothing was moved or "
                       "written" % (e.__class__.__name__, e))
    # `or {}` RAN BEFORE THE SHAPE CHECK AND SPENT IT. A roster whose content
    # is a valid JSON `[]` -- or null, or false, or 0, or "" -- became an empty
    # MAPPING here, so the `isinstance` guard below saw a dict, and a file this
    # reader could open and could not use answered "nothing to stamp": the
    # exact reassuring direction the docstring above promises to refuse, and
    # indistinguishable from a fully migrated fleet. A second read found it on
    # task/2444 r8, in a commit two rounds older than the one under review.
    # The raw value is kept and the shape check decides, which is what the
    # acquisition reader beside this one already does.
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            rows = json.load(f)
    except FileNotFoundError:
        return [], 0, ""             # proven absent: nothing to stamp
    except (OSError, ValueError) as e:
        return [], 0, ("the roster could not be read (%s), so what needs "
                       "stamping is UNKNOWN — nothing was moved or written"
                       % type(e).__name__)
    if not isinstance(rows, dict):
        return [], 0, ("the roster parsed as %s rather than a mapping, so what "
                       "needs stamping is UNKNOWN — nothing was moved or "
                       "written" % type(rows).__name__)
    return _markerless(rows), _marked(rows), ""


def migrate_incarnations(apply=False):
    """(names stamped, count already marked, unreadable) — THE ONE-SHOT STAMP.

    Every row the roster's write door touches already gets a string
    `incarnation`; a row that has not been written since the marker existed
    still carries none, and a row with no generation cannot be NAMED by the
    lifecycle fence — so the proof REFUSES it rather than inventing a token
    for "some markerless row", which is a class and not an identity.

    This is the cure that refusal names. IDEMPOTENT — a second run stamps
    nothing — and DRY BY DEFAULT, because a roster rewrite leaves nothing to
    inspect afterwards. It mints the same value the write door mints, under
    the same lock, and touches no other field: a stamped legacy row is exactly
    what an ordinary write would have left behind.

    THE DRY PATH DOES NOT COME THROUGH HERE AT ALL — it delegates to
    `preview_incarnations`, whose whole reason for existing is that this
    function's accessor mutates on a roster it cannot parse.

    IT ADMITS NO IDENTITY. Every name it writes was already a key in the
    roster, so this publication can mint no seat and resolve no name — which
    is why it is a METADATA door to the authority scanner and not a door that
    projects.
    """
    if not apply:
        return preview_incarnations()
    with _flocked(roster_path() + ".lock"):
        r = roster_for_write()
        want = _markerless(r)
        marked = _marked(r)
        if want:
            for name in want:
                r[name]["incarnation"] = uuid.uuid4().hex
            pk.write_json(roster_path(), r)
    return want, marked, ""


def _lifecycle_contested(rows, seat, sid):
    """Why a lifecycle proof is no longer true, or "" — LIFECYCLE, not keys.

    ASKED AT BOTH FENCED BRANCHES, because ownership can move under a row
    that is PRESENT exactly as it can under a name that is empty. A disown
    hands a session to another row while leaving this row's generation
    intact, so the generation still matches, the first writer refreshes,
    projects and publishes, and only the binder afterwards discovers another
    row is the current owner. Matching a generation proves WHICH ROW; it does
    not prove the row is still the one this session belongs to.

    THE SID IS THE HOST-PROVEN LIFECYCLE ONE AND IT IS NOT THE ROW'S SESSION.
    It arrives on its own parameter and is never written, so this fence can
    ask the ownership question without the ordinary self-write path acquiring
    any authority it did not have.

    THE TEST IS "NO OTHER ROW OWNS IT", NEVER "THIS ROW'S SESSION IS
    UNCHANGED" — a legitimate same-generation rebind to a CHANGED sid must
    still bind, and an unchanged-sid requirement would refuse it.

    A proof of absence says the NAME was free. It stops being true the moment
    the name acquires an owner, and a key that is empty AGAIN after a rename
    has exactly that: a LIVE ALIAS still addressing the row that moved, and a
    row elsewhere that is now this session's current one. Both are properties
    of the LIFECYCLE rather than of the key, which is why checking the key
    alone read the second absence as the first — admitting under a name whose
    row had been deliberately moved, reminting, retiring the very alias that
    was covering the window, and publishing, all before the binder's
    current-owner refusal arrived to say none of it should have happened.

    READ FROM THE SNAPSHOT THE CALLER ALREADY HOLDS. `live_alias` takes the
    rows mapping rather than opening the roster, and the owner scan walks that
    same mapping, so this adds no lock, cannot re-enter one, and leaves the
    spawn-outer/roster-inner order untouched.
    """
    from .seats_common import live_alias
    alias, _until = live_alias(seat, rows)
    if alias is not None:
        return "%s is a live rename alias for %s" % (seat, alias)
    sid = str(sid or "")
    if not sid:
        # NOT "NOTHING IS WRONG" — nothing was ASKED. A caller that fences on
        # lifecycle predicates and supplies no sid gets the alias arm only,
        # and that is the caller's gap to close rather than this predicate's
        # to paper over. The one production fence supplies it.
        return ""
    want = str(seat).casefold()
    owner = next((name for name, row in rows.items()
                  if isinstance(row, dict) and row.get("session") == sid
                  and str(name).casefold() != want), None)
    if owner is not None:
        return "session %s is already current for %s" % (sid, owner)
    return ""
