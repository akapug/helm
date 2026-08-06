"""The integration board's ONE write path.

The board is the owner's console truth, and until now every writer was an
ad-hoc `json.dump(open(path, "w"))` in a throwaway script. That is unsafe two
ways at once, and both fired on 2026-07-27:

  LOST UPDATE — two agents read the same board seconds apart, each appended its
  own rows, each wrote the whole object back. Both writes happened to survive;
  the integrator's own verification called that luck rather than safety, and it
  is. Nothing ordered them.

  TORN FILE — `open(path, "w")` truncates BEFORE the new bytes land. A crash,
  a full disk or a killed pane between those two moments leaves a zero-length
  or half-written board, and the owner's console loses every row.

The primitives to fix it already existed (`eventledger.locked`, `pk.atomic_write`);
what was missing was a place that composes them. Callers pass a mutate function
instead of a finished object so the read and the write happen INSIDE one lock —
an API that hands back the parsed board and trusts the caller to write it later
re-opens the same race with more steps.

Single-writer-per-artifact was the previous answer and it is honour-system: it
was documented only in board-render.py, the one component that never writes.
An invariant stated only where it cannot be violated is not stated.
"""

import json
import os
import sys

from . import eventledger, fleetnotes, pk

DEFAULT = os.path.expanduser("~/.helm/_global/integration-board.json")


def path():
    return os.environ.get("HELM_BOARD") or DEFAULT


# ---------------------------------------------------------------------------
# key ownership — writer-per-KEY, enforced at the ONE write path
# ---------------------------------------------------------------------------
#
# The board used to be single-writer BY CONTRACT: a sentence in the file naming
# the integrator, with nothing to enforce it. That is the honour-system shape
# this module's own docstring already indicts — "an invariant stated only where
# it cannot be violated is not stated" — and it went stale the moment the lock
# landed and made the mechanical argument obsolete. Nobody noticed for days.
#
# The meld (2026-08-01, owner + opus-integrator + helm-claude-2) replaced it
# with writer-per-KEY, and helm-claude-2's sharpening is the load-bearing part:
# the split is NOT a list of key names, it is APPEND vs REPLACE.
#
#   ANY seat may APPEND to ANY key.  REPLACING a NARRATIVE key is the owner's.
#
# Two seats appending two findings is two findings — no contradiction is
# CONSTRUCTIBLE. Two seats replacing one narrative slot is a contradiction by
# construction. A key added later therefore defaults to open-for-annotation and
# closed-for-replacement, which is the safe direction; the contract this
# replaces defaulted to a rule nobody could enforce.
#
# ENFORCED HERE, not in the typed writers, for the reason the last contract
# failed: set_value and add_note are conveniences, and `update(mutate)` takes an
# arbitrary function. A rule living in the wrappers is bypassed by the one call
# that does not use them. So the check runs on the RESULT — snapshot, mutate,
# diff, refuse before the write — and cannot be routed around by any caller.
NARRATIVE = {
    "tasks": "the console's account of what the fleet is doing",
    "landed": "the land record the owner's console reads as history",
    "owner_gated_queue": "what is waiting on the owner specifically",
    "burn_down_0_2": "the 0.2 burn-down the console renders as progress",
    "building": "who is building what, as one coherent statement",
    "freeze": "the land-window freeze state a lander consults",
    "_contract": "the ownership map itself",
    "_writer_seat": "who owns the narrative keys",
}

_OWNER_KEY = "_writer_seat"


def _appended_only(before, after):
    """True when `after` is `before` with rows added at either end.

    add_note PREPENDS and add_landed PREPENDS, so growth shows up as the old
    list surviving intact as a contiguous run at one END of the new one.
    Anything else — a reorder, an edit in place, a truncation — is a REPLACE
    and is gated, because it can contradict what another writer put there.

    NO LENGTH GUARD, deliberately. A first cut carried
    `if len(after) < len(before): return False` and it was DEAD: a shorter
    `after` cannot contain `before` as a contiguous run, so the comparison
    below already rejects every truncation. The mutation that removed it left
    all 35 tests green, which is the honest signal that it was decoration —
    and a line that reads as a safety check while deciding nothing is the
    exact shape this module exists to argue against."""
    if not isinstance(before, list) or not isinstance(after, list):
        return False
    if not before:
        return True                      # empty -> anything is pure addition
    return before in (after[:len(before)], after[-len(before):])


def _ownership_error(before, after, seat):
    """The refusal, or None. Fail-open on an UNDECLARED caller, by the same law
    as seats.foreign_seat: a process that declares no seat is not provably
    foreign, and the owner's own CLI and operator scripts must keep working.
    This catches the honest mistake — a seat replacing a narrative slot — and
    does not pretend to stop a determined bypass. Stated, not advertised as
    total."""
    if not seat:
        return None
    owner = str(before.get(_OWNER_KEY) or "").strip()
    if not owner or owner == seat:
        return None                      # no declared owner -> nothing to own
    for key in sorted(NARRATIVE):
        old, new = before.get(key, _MISSING), after.get(key, _MISSING)
        if old == new or _appended_only(old, new):
            continue
        return ("%r is a NARRATIVE key and %s owns replacing it — you may "
                "APPEND to any key (that is two findings, not a contradiction) "
                "but replacing this one is a statement about what is true, and "
                "two writers cannot both make it. Ask %s, or append instead. "
                "(why %r is narrative: %s)"
                % (key, owner, owner, key, NARRATIVE[key]))
    return None


def update(mutate, board_path=None, seat=None, after_write=None):
    """Apply mutate(board_dict) under an exclusive lock. Returns (ok, err).

    mutate edits the dict in place; its return value is ignored. A mutate that
    raises leaves the board UNTOUCHED — the write only happens after it returns,
    so a half-applied edit never reaches disk.

    `after_write(board)` is a POST-COMMIT projection that runs while the same
    board lock is still held and may return a warning. It cannot make the board
    write atomic with another file, but it orders every board-derived projection
    behind the authoritative mutation: an older landing can never project after
    a newer one. Because the board has already committed, a warning returns as
    `(True, warning)` rather than lying that the source write failed.

    `seat` declares WHO is writing. It is optional and its absence fails open
    (see _ownership_error); when supplied, replacing a NARRATIVE key that
    another seat owns is refused BEFORE the write, so the refusal costs nothing
    and leaves no partial state.
    """
    p = board_path or path()
    with eventledger.locked(p) as held:
        if not held:
            return False, "board is locked by another writer (or the lock could not be taken) — nothing written"
        board = pk.read_json(p, default=None)
        if not isinstance(board, dict):
            return False, "board at %s is missing or not a JSON object — refusing to create one from a partial edit" % p
        before = json.loads(json.dumps(board))   # the pre-image to diff against
        mutate(board)
        err = _ownership_error(before, board, seat)
        if err:
            return False, err            # refused BEFORE the write: no partial
        pk.write_json(p, board)
        if not after_write:
            return True, None
        try:
            return True, after_write(board)
        except Exception as e:          # post-COMMIT: a raise cannot undo source
            return True, ("board committed, but its post-write projection raised "
                          "%s: %s" % (type(e).__name__, e))


def add_landed(name, sha, note, board_path=None, seat=None):
    """Prepend one landed row and derive the homepage headline from that write.

    The board stays the ONE source: each call reconciles the stable
    `board-landed` note to the board's CURRENT newest row while the board lock is
    still held. This is `headlines-click-to-detail` (owner law, 2026-07-22 +
    2026-08-04) made compositional rather than a second list an agent must
    remember to maintain. A matching projection is skipped, so an ordinary
    retry cannot refresh its timestamp; a missing projection IS repaired.

    The two files cannot share one atomic rename. If the authoritative board
    commits and the projection fails, return ok=True WITH a warning: calling the
    board write failed would be a lie, while swallowing the dead homepage feed
    would recreate the owner's reported defect."""
    def _mutate(board):
        rows = board.setdefault("landed", [])
        dup = [r for r in rows if isinstance(r, dict)
               and r.get("name") == name and r.get("sha") == sha]
        if not dup:
            rows.insert(0, {"name": name, "sha": sha, "note": note})

    def _project(board):
        rows = board.get("landed")
        latest = rows[0] if isinstance(rows, list) and rows and isinstance(
            rows[0], dict) else None
        if not latest:
            return "landed row recorded, but no newest board row could project"
        headline = "Landed %s @ %s" % (
            latest.get("name") or "?", str(latest.get("sha") or "?")[:12])
        depth = len([r for r in rows if isinstance(r, dict)])
        if depth > 1:
            # THE COUNT IS BOARD DEPTH, NOT "SINCE YOU LAST LOOKED", and the
            # difference is a claim I could not support.
            #
            # The brief asked for lands-since-you-last-looked. THERE IS NO
            # OWNER-LOOK EVENT IN HELM: /api/notes is GET-only by design, the
            # card is polled rather than opened, and set_note REPLACES the whole
            # row so no marker survives the next land. My first cut used the
            # position of the sha the previous note named, which reads like that
            # count and is not: the note is rewritten EVERY land, so two lands in
            # a row both report "+1" and an idempotent retry erases it entirely
            # (codex, r2 review — reproduced).
            #
            # Board depth is what the data actually supports: true, monotonic,
            # retry-safe, and CLICKABLE — an owner who reads "170 on the board"
            # can go find 170. A real since-you-looked count needs a look event,
            # which is an owner-surface change and belongs in its own row rather
            # than being faked from what happens to be lying around.
            headline += " — %d on the board" % depth
        detail = latest.get("note") or None
        row, note_err = fleetnotes.set_note(
            "board-landed", headline, detail=detail, goto="board",
            idempotent=True)
        if note_err:
            warning = ("landed row recorded, but fleet-note headline failed: "
                       + note_err)
            pk.event("board-landed-note-failed", str(name), warning)
            return warning
        return row.get("warning")

    return update(_mutate, board_path, seat=seat, after_write=_project)


def set_value(key, value, allow_new=False, board_path=None, seat=None):
    """Set a TOP-LEVEL SCALAR on the board -> (ok, err).

    TWO REFUSALS, and both come from what this module cannot know.

    NO NEW KEYS WITHOUT INTENT. helm does not own the board's RENDERER — the
    board's own `_contract` names it as helm-claude's console generator, and
    nothing in this repository reads the file. So the set of keys the owner
    actually SEES is unknowable from here, and inventing one writes into a
    surface no console displays: a row that reads as recorded and is invisible
    to the person it was recorded for. What IS knowable is the negative: a key
    the board has never carried is certainly not rendered. `--new` is therefore
    a statement that the reader has been coordinated with, not a convenience.

    NEVER CLOBBER STRUCTURE WITH A SCALAR. `landed` is a list of 37 rows and
    `owner_open_items` is a list of dicts; `set landed "done"` would replace an
    audit trail with a word, atomically and under a lock, which is the most
    convincing possible way to destroy it. A structured value needs a verb that
    understands its shape — `add_landed` is one — so a scalar set refuses.
    """
    def _mutate(b):
        cur = b.get(key, _MISSING)
        if cur is _MISSING and not allow_new:
            raise KeyError(
                "board has no key %r — helm does not own the console that "
                "renders this file, so a new key may be written and never "
                "displayed. Pass --new only once the reader knows about it. "
                "Existing keys: %s" % (key, ", ".join(sorted(b))))
        if isinstance(cur, (list, dict)):
            raise TypeError(
                "%r currently holds a %s of %d entr%s — a scalar set would "
                "replace it wholesale. Use a verb that understands the shape "
                "(landed has one); this refuses rather than flatten it."
                % (key, type(cur).__name__, len(cur),
                   "y" if len(cur) == 1 else "ies"))
        b[key] = value
    try:
        return update(_mutate, board_path, seat=seat)
    except (KeyError, TypeError) as e:
        return False, str(e.args[0] if e.args else e)


def add_note(key, text, board_path=None, seat=None):
    """Prepend one STRING row to an EXISTING list-of-strings key -> (ok, err).

    The board carries several newest-first running logs — `burn_down_0_2`,
    `notes`, `rescue_queue` — and nothing could write any of them. `set_value`
    refuses a list on purpose, and `add_landed` understands exactly one key's
    dict shape. So the console's 0.2 burn-down sat FIVE DAYS stale while every
    item inside it moved, which is the same class the burn-down exists to
    report: a surface asserting a state that is no longer true.

    THREE REFUSALS, each for something this module cannot know.

    NO NEW KEYS, for `set_value`'s reason exactly: helm does not own the
    renderer, so a key the board has never carried is certainly not displayed.

    NOT A LIST. A scalar has no row to prepend and a dict has no order; writing
    either would destroy the value or invent a shape.

    NOT A LIST OF STRINGS. `landed` holds dicts, and a bare string prepended to
    it is a row the renderer reads with `.get()` — it silently drops, or it
    raises. A structured list already has a verb that understands it, so this
    one refuses and names the shape it found.

    The empty list is the ONE case where the row shape cannot be checked, and
    it is allowed: there is no existing row to contradict, and the key had to
    exist already, which means someone coordinated it with the renderer.

    Idempotent against the row it JUST wrote — a retry after an ambiguous
    failure does not double-post. Deliberately not idempotent against older
    rows: this is a running log, and a status that legitimately recurs later
    must be able to say so.
    """
    def _mutate(b):
        cur = b.get(key, _MISSING)
        if cur is _MISSING:
            raise KeyError(
                "board has no key %r — helm does not own the console that "
                "renders this file, so a new key may be written and never "
                "displayed. Existing keys: %s" % (key, ", ".join(sorted(b))))
        if not isinstance(cur, list):
            raise TypeError(
                "%r holds a %s, not a list — there is no row to prepend. Use "
                "`helm board set <key> <value...>` for a scalar." % (key, type(cur).__name__))
        bad = [r for r in cur if not isinstance(r, str)]
        if bad:
            raise TypeError(
                "%r is a list of %s, not of strings — a bare string prepended "
                "to it is a row the console reads with .get() and drops. Use a "
                "verb that understands that shape (`landed` has one)."
                % (key, type(bad[0]).__name__))
        if not cur or cur[0] != text:
            cur.insert(0, text)
    try:
        return update(_mutate, board_path, seat=seat)
    except (KeyError, TypeError) as e:
        return False, str(e.args[0] if e.args else e)


_MISSING = object()


# ---------------------------------------------------------------------------
# the verb — the half that was missing
# ---------------------------------------------------------------------------
# THE SAFE WRITE PATH SHIPPED WITH NO WAY TO USE IT. `update` and `add_landed`
# landed on 2026-07-27 to replace exactly the ad-hoc
# `json.dump(board, open(path, "w"))` that had already produced a lost update
# and could produce a torn file — and no verb, no import, and no caller ever
# arrived. Every writer stayed ad-hoc, so the module protected nothing while
# reading as done. Found by `helm wiring` on its first real run, which is the
# entire argument for that census: a rule cannot notice this, and a graph can.

_USAGE = """usage: helm board show [--json]
       helm board landed <name> <sha> <note...>
       helm board set <key> <value...> [--new]
       helm board note <key> <text...>
  The integration board — the owner's console truth. `landed` prepends one row
  through the LOCKED, idempotent write path; hand-editing the JSON is what the
  lock exists to prevent. `set` updates an EXISTING top-level scalar and
  refuses two things it cannot know to be safe: a NEW key (helm does not own
  the console that renders this file, so an invented key may never be
  displayed — --new says the reader was coordinated with) and clobbering a
  list or dict with a scalar. `note` prepends one line to an EXISTING
  list-of-strings log (burn_down_0_2, notes, rescue_queue) and refuses a new
  key, a non-list, and a list of dicts. HELM_BOARD overrides the path.
"""


def _acting_seat():
    """This process's DECLARED seat, or None.

    THE CLI IS THE PRIMARY ENTRY POINT, so a guard it never invokes is a guard
    that does not exist — the same shape as a tri-state nobody prints. It uses
    the DECLARED name only (seats.own_name): the owner's own shell declares
    none and therefore fails open, which is the intended asymmetry."""
    try:
        from . import seats
        return seats.own_name() or None
    except Exception:                    # noqa: BLE001 — identity is unknown,
        return None                      # never a traceback out of a board write


def _advise_owner(text):
    """OWNER-BOUND ADVISORY. This verb writes text the owner reads, so it runs
    the clarity die in owner mode and prints what it finds. Advisory by law:
    it cannot refuse the write and cannot change the exit code, and every
    failure path is silent (see helm.clarity.advise). Silence it with
    HELM_CLARITY_ADVISE_OFF=board."""
    try:
        from .clarity import advise
        advise(text, "board")
    except Exception:                    # noqa: BLE001 — fail-open by law
        pass


def cmd_board(args):
    """board show|landed — the owner console's board, written safely."""
    args = list(args or [])
    verb = args[0] if args else ""
    if not verb or verb in ("-h", "--help", "help"):
        print(_USAGE)
        return 0 if verb else 2
    if verb == "show":
        board = pk.read_json(path(), default=None)
        if not isinstance(board, dict):
            print("helm board: no board at %s" % path(), file=sys.stderr)
            return 1
        if "--json" in args:
            print(json.dumps(board, indent=2, sort_keys=True))
            return 0
        for key in sorted(board):
            v = board[key]
            n = len(v) if isinstance(v, (list, dict)) else 1
            print("  %-22s %s" % (key, "%d row(s)" % n
                                  if isinstance(v, (list, dict)) else v))
        return 0
    if verb == "set":
        rest = [a for a in args[1:] if a != "--new"]
        if len(rest) < 2:
            print(_USAGE, file=sys.stderr)
            return 2
        key, value = rest[0], " ".join(rest[1:])
        ok, err = set_value(key, value, allow_new="--new" in args,
                            seat=_acting_seat())
        if not ok:
            print("helm board: " + err, file=sys.stderr)
            return 1
        print("helm board: %s = %s" % (key, value[:70]))
        return 0
    if verb == "note":
        rest = args[1:]
        if len(rest) < 2:
            print(_USAGE, file=sys.stderr)
            return 2
        key, text = rest[0], " ".join(rest[1:])
        ok, err = add_note(key, text, seat=_acting_seat())
        if not ok:
            print("helm board: " + err, file=sys.stderr)
            return 1
        print("helm board: %s <- %s" % (key, text[:70]))
        return 0
    if verb == "landed":
        rest = args[1:]
        if len(rest) < 3:
            print(_USAGE, file=sys.stderr)
            return 2
        name, sha, note = rest[0], rest[1], " ".join(rest[2:])
        ok, err = add_landed(name, sha, note, seat=_acting_seat())
        if not ok:
            print("helm board: " + err, file=sys.stderr)
            return 1
        if err:
            print("helm board: WARNING: " + err, file=sys.stderr)
        print("helm board: landed row recorded — %s @ %s" % (name, sha))
        _advise_owner(note)
        return 0
    print("helm board: unknown verb '%s'" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2
