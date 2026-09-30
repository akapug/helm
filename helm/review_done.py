"""helm review done — one verb that reads the row, and a corrected command
after every refusal of the doors it fronts (task/3382).

THE DEFECT, MEASURED over 40.5 hours and about 12,000 tool calls of the three
local seats: hand-backs and verdicts FAILED 62% of the time (156 of 250;
qwenlocal 73%), the worst storm 18 tries in 2.3 minutes. The refusals were
almost all about typing what the row already knew — usage 19, evidence over
the cap 11, missing polarity 10, family proof 8, full sha 4, stale tip 3 — and
the median recovery was 24 calls. Beside them, 14 verdicts landed on the
seat's OWN outgoing row and 7 hand-backs were addressed to their sender.

THREE CURES, ONE MODULE.

  (A) `helm review done <row> clean|concur|fix "<evidence>"` reads the row
      (its full tip, recipient, lane and chain) and records the read through
      the EXISTING door: `clean` is `helm dispatch hold <row> --source-clean
      <tip>`, `concur` is `helm dispatch verdict <row> <tip> --concur
      --measured`, and `fix` is `helm dispatch verdict <row> <tip> --fix
      --measured` with the counts the seat declares, never a default
      (task/3382 F10). It is a FRONT and never a second writer: it calls the
      dispatch verb table itself, so every refusal those doors make still
      happens, in their words.
  (B) every refusal of `helm dispatch send`, `verdict` and `hold`, and of
      `helm review done`, ends with ONE `corrected:` line — the command to
      paste, with what the ROW settles filled in: its full id, recipient,
      lane and chain. A choice only the seat can make stays a quoted
      `<placeholder>`: the polarity, the basis, the counts, the exit answer,
      the cure answer, the holder, the work arm, a path, a reason, a row the
      seat's id did not name (`<ROW_ID>`, its candidate named on the line
      above), and a tip the seat did not type (a typed prefix of the row's
      tip is written out in full; any other tip is `<TIP_YOU_READ>`, and a
      line before it names where the row is now). A choice between flags is
      ONE placeholder naming them, and two answers to one choice become it
      too. A placeholder is recognized by its slot and by its spelling
      anywhere, so a re-paste never grows the text; those four doors REFUSE
      one pasted unfilled, naming it, and `send` refuses a brief carrying one
      (task/3382). Every value is one quoted shell word, the send's prose is
      read through the send door's own partition, and the free text follows
      `--` wherever the verb's parser takes one, so evidence that starts `-h`
      is text, never a flag. The line never truncates anything to fit, never
      resolves a ref NAME in the row's checkout, and never ends with a
      release of an owner-gated hold. NOR DOES IT CARRY BACK ITS REFUSAL'S
      CAUSE (task/3403), which a paste would meet again, forever: a value
      the door refuses for what it is (a count, a relation, a path, a
      reason, a patch tip that is not hex or not off the row's tip, an id
      that names no row, a tip, a send's recipient, kind, deadline, lane,
      ref, note or key, a text that is not one printable line) is its slot's
      placeholder; a meld or design finding the direction cannot carry is
      dropped; an answer the door owes the brief is added as its
      placeholder; and an `--imperfect` read with no cure its direction can
      carry reopens the direction, naming its two ways out.
  (C) `send` refuses a recipient that is the sending seat and names the
      sender of the row it answers; a verdict on a row the seat SENT names the
      row on that chain addressed to it.

A HINT NEVER BREAKS A REFUSAL. The corrected line is computed after the door
has already refused and printed why; a failure while computing it prints
nothing rather than turning a refusal into a crash.
"""
import os
import re
import shlex
import sys

from . import dispatches
from . import freetext

#: What a read can conclude, in the order a reviewer usually reaches them.
OUTCOMES = ("clean", "concur", "fix")
#: The existing door each outcome is recorded through.
DOORS = {"clean": "hold --source-clean", "concur": "verdict --concur --measured",
         "fix": "verdict --fix --measured"}
USAGE = ('usage: helm review done <row-id-prefix> clean|concur|fix "<evidence>" '
         "[--patch-tip SHA] [--no-patch-because R] [--worse-than-main PATH] "
         "[--imperfect] [--finding-count N] [--prior-relation "
         "new|uncured|regression-of-cure] [--diff-handoff ROOM/MSGID] "
         "[--tip SHA]")
#: The flags `done` takes, each mapped to whether it may repeat.
VALUED = {"--patch-tip": False, "--no-patch-because": False,
          "--diff-handoff": False, "--worse-than-main": True,
          "--finding-count": False, "--prior-relation": False,
          "--tip": False}
BARE = ("--imperfect",)
#: What a clean read cannot carry: it records a hold, which names no cure, no
#: exit answer and no findings. `--tip` is the one flag every outcome takes.
NOT_CLEAN = ("--patch-tip", "--no-patch-because", "--diff-handoff",
             "--worse-than-main", "--imperfect", "--finding-count",
             "--prior-relation")
#: A row id prefix shorter than this names too many rows to be worth a lookup.
MIN_PREFIX = 4
#: The shortest `--tip` accepted, git's own default abbreviation.
MIN_TIP = 7
#: How many candidates an ambiguous prefix lists before it counts the rest.
CANDIDATES_SHOWN = 10
#: The statuses a row is in while it still awaits its reader's answer.
AWAITING = ("open", "held")
#: The prefix of the one line every refusal ends with.
CORRECTED = "corrected: "
#: The dispatch verbs whose refusals end with a corrected line.
CORRECTED_VERBS = ("send", "verdict", "hold")
_HEX = re.compile(r"[0-9a-f]+\Z")
#: The placeholders for the choices only the seat can make. A choice between
#: two flags is ONE placeholder naming both (task/3382 F1, F2, F7): the line
#: never picks the flag and leaves only its value open.
TIP_YOU_READ = "<TIP_YOU_READ>"
ROW_PLACEHOLDER = "<ROW_ID>"
POLARITY_PLACEHOLDER = "<--concur|--fix>"
BASIS_PLACEHOLDER = "<--measured|--inferred>"
COUNT_PLACEHOLDER = "<N>"
RELATION_PLACEHOLDER = "<new|uncured|regression-of-cure>"
EXIT_PLACEHOLDER = "<--worse-than-main PATH|--imperfect>"
CURE_PLACEHOLDER = "<--patch-tip SHA|--no-patch-because REASON>"
DIFF_HANDOFF_PLACEHOLDER = "<PAIR-ROOM/MSGID>"
HOLDER_PLACEHOLDER = "<--owner-gated|--source-clean TIP>"
WORK_PLACEHOLDER = "<--new-work|--supersedes ROW>"
#: AN IMPERFECT READ WITH NO CURE ITS DIRECTION CAN CARRY (task/3403): a FIX
#: whose cure answer is "no cure", or a SUPERSEDE, which carries none.
#: `--imperfect` says the tip is no worse than main, and the door admits it
#: only as the carrier of a committed cure; without one the read blocks
#: nothing, which is an approve. So the refused choice is the DIRECTION, and
#: the line names its two real ways out and carries neither the hand-back
#: nor the no-cure answer the door refused together. A model run's read
#: never approves, so its way out that blocks nothing is a concur; at
#: `helm review done` it is `clean`, the reviewer's approve.
NOT_A_BLOCK_PLACEHOLDER = "<--approve|--fix --imperfect --patch-tip SHA>"
RUN_NOT_A_BLOCK_PLACEHOLDER = "<--concur|--fix --imperfect --patch-tip SHA>"
CLEAN_OR_CURE_PLACEHOLDER = "<clean|fix --imperfect --patch-tip SHA>"
#: A model run's read, and a meld room, named twice or refused by shape.
MODEL_PLACEHOLDER = "<MODEL>"
RUN_PLACEHOLDER = "<RUN>"
ROOM_PLACEHOLDER = "<ROOM>"
#: A send's deadline, note and operation key refused by shape, and the
#: answer an irreversible first build row owes (review_door T0).
SECONDS_PLACEHOLDER = "<SECONDS>"
NOTE_PLACEHOLDER = "<note>"
KEY_PLACEHOLDER = "<KEY>"
DOOR_PLACEHOLDER = "<--meld ROOM|--async-because REASON>"
#: What a corrected SEND line emits: a brief that CONTAINS one is not a
#: brief, and the send door refuses it (task/3382 F6). The value
#: placeholders task/3403 added (`<SECONDS>`, `<note>`, `<KEY>`) stay out:
#: they are ordinary words in a brief's prose, and pasted unfilled as a
#: value they are refused whole like every other.
SEND_PLACEHOLDERS = ("<recipient>", "<lane>", "<brief>", "<TIP>",
                     "<build|review>", WORK_PLACEHOLDER, ROW_PLACEHOLDER,
                     DOOR_PLACEHOLDER)
#: WHOLE ARGUMENTS this module itself emits for the seat to fill. Angle
#: brackets alone do not make a value a placeholder: evidence such as
#: ``<expected>`` and a real path such as ``<T>`` remain ordinary values.
_PLACEHOLDERS = frozenset((
    TIP_YOU_READ, ROW_PLACEHOLDER, POLARITY_PLACEHOLDER, BASIS_PLACEHOLDER,
    COUNT_PLACEHOLDER, RELATION_PLACEHOLDER, EXIT_PLACEHOLDER,
    CURE_PLACEHOLDER, DIFF_HANDOFF_PLACEHOLDER, HOLDER_PLACEHOLDER,
    "<PATH>", "<REASON>", "<FULL_SHA>",
    "<row-id-prefix>", "<clean|concur|fix>", "<evidence>", "<reason>",
    "<FINDING>", NOT_A_BLOCK_PLACEHOLDER, RUN_NOT_A_BLOCK_PLACEHOLDER,
    CLEAN_OR_CURE_PLACEHOLDER, MODEL_PLACEHOLDER, RUN_PLACEHOLDER,
    ROOM_PLACEHOLDER, SECONDS_PLACEHOLDER, NOTE_PLACEHOLDER, KEY_PLACEHOLDER,
) + SEND_PLACEHOLDERS)
#: EACH FREE TEXT'S CAP, ONCE: (its placeholder, the `dispatches` constant
#: that caps it, how the door counts it). The line's over-cap placeholder and
#: the set that recognizes it both read this table at call time, so the two
#: name one cap (a clean read's evidence is a HOLD reason, capped as one).
_TEXTS = {"evidence": ("<evidence>", "VERDICT_EVIDENCE_BUDGET",
                       "verdict_evidence_chars"),
          "clean": ("<evidence>", "HOLD_REASON_CAP", None),
          "reason": ("<reason>", "HOLD_REASON_CAP", None),
          "cure": ("<REASON>", "NO_PATCH_REASON_CAP", None),
          "design": ("<FINDING>", "DESIGN_FINDING_CAP", None)}


def _q(text):
    """One shell word, quoted so a paste runs it as typed."""
    return shlex.quote(str(text))


def _capped(kind):
    """(the placeholder for `kind`'s text over its cap, cap, counter)."""
    name, cap, counter = _TEXTS[kind]
    cap = getattr(dispatches, cap)
    return ("%s of at most %d chars>" % (name[:-1], cap), cap,
            getattr(dispatches, counter) if counter else len)


def placeholder(value):
    """Is `value` one of this module's whole-argument placeholders?"""
    value = str(value or "").strip()
    return value in _PLACEHOLDERS \
        or any(value == _capped(kind)[0] for kind in _TEXTS)


def _carried(words):
    """The send placeholders `words` carry INSIDE a word, each named once."""
    return [p for p in SEND_PLACEHOLDERS
            if any(p in str(w) and not placeholder(w) for w in words)]


def unfilled_refusal(argv):
    """Why `argv` cannot be recorded: the placeholders it still carries,
    each named once, in order. None when it carries none.

    A corrected line leaves every value only the seat knows as a quoted
    `<...>`, and a paste that keeps one would record a choice nobody made:
    `<PATH>` measured as a worse-than-main path, `<REASON>` as the reason no
    cure was committed."""
    names = []
    for word in argv or ():
        word = str(word).strip()
        if placeholder(word) and word not in names:
            names.append(word)
    if not names:
        return None
    return ("%s %s still a placeholder, not a value: a corrected line leaves "
            "each value only you know as <...>, and nothing is recorded until "
            "you type yours in its place"
            % (", ".join(_q(n) for n in names),
               "is" if len(names) == 1 else "are"))


def _now_at(row):
    """The line printed above a corrected line whose tip is `<TIP_YOU_READ>`."""
    return ("dispatch %s is now at %s: read that tree, then type its id in "
            "place of %s — a corrected line never names a tip you did not"
            % (row["id"][:12], row.get("tip") or "an unknown tip",
               TIP_YOU_READ))


def _typed_tip(row, typed):
    """(word, note) for a reviewed tip: the seat's OWN tip, written out in
    full when it is a prefix of the row's tip, else `<TIP_YOU_READ>` and the
    note naming where the row is now. The row's tip is never put in its
    place: after a stale refusal that would record a read of a tree the seat
    never named."""
    tip = str(row.get("tip") or "")
    given = str(typed or "").strip().lower()
    if tip and len(given) >= MIN_TIP and _HEX.fullmatch(given) \
            and tip.lower().startswith(given):
        return _q(tip), None
    return _q(TIP_YOU_READ), _now_at(row)


def _matches(left, right):
    """Do two seat names name ONE seat? Absent never matches."""
    if not left or not right:
        return False
    from . import seats                 # deferred, as dispatches does
    return seats.recipient_matches(left, right)


def _newest(rows):
    """The rows newest first, OPEN before HELD: an open row is the one a
    reader can still answer."""
    return sorted(rows, key=lambda r: (r.get("status") == "open",
                                       str(r.get("ts") or "")), reverse=True)


def _describe(row, full=False):
    """One row, the way a reader recognises it; its whole id when `full`."""
    return "%s (%s, @%s, lane %s, tip %s, from @%s)" % (
        row["id"] if full else row["id"][:12], row.get("status") or "?",
        row.get("recipient") or "?", row.get("lane") or "?",
        str(row.get("tip") or "?")[:12], row.get("sender") or "?")


def _guessed(row, typed):
    """The line above a command whose row is `<ROW_ID>` because the id the
    seat typed names no row and a shorter prefix of it names `row` (task/3382
    F8): a token that matches nothing is not evidence of which row it meant."""
    return ("no row id starts with %s — did you mean %s? A corrected line "
            "never names a row you did not: type its id in place of %s"
            % (str(typed or "").strip(), _describe(row, True),
               ROW_PLACEHOLDER))


def _not_open(row):
    """(line, note) when `row` takes no read now, else None. A held row is
    released first, unless its hold is OWNER-GATED: only the owner lifts that
    (task/3382 F9), so the line lists the seat's own open rows and the note
    says whose move it is. A closed row is read by triage."""
    status = row.get("status")
    if status == "held" and row.get("owner_gated") is True:
        return "helm dispatch list --mine --open", (
            "dispatch %s is held (%s) -- %s"
            % (row["id"][:12], row.get("hold_reason") or "no reason given",
               dispatches.held_remedy(row, "read")))
    if status == "held":
        return "helm dispatch release %s" % _q(row["id"]), None
    if status != "open":
        return "helm dispatch triage %s" % _q(row["id"]), None
    return None


# ---------------------------------------------------------------------------
# reading the row
# ---------------------------------------------------------------------------

def resolve(current, token):
    """(row, why, candidates) for a row id prefix of any length >= 4.

    The verdict door resolves 8 or more characters; a seat that remembers 6
    padded them into invented ids (85 of 4,866 typed hex tokens matched
    nothing). So this resolves the prefix itself: one match is the row; more
    than one is refused WITH the candidates, newest first; none is refused,
    and when a shorter prefix of the token names exactly one row that row is
    offered as the candidate — never taken, because a token that matches
    nothing is not evidence of which row was meant."""
    rid = str(token or "").strip().lower()
    if len(rid) < MIN_PREFIX or not _HEX.fullmatch(rid):
        return None, ("%r is not a row id: give %d or more of its hex "
                      "characters (`helm dispatch list --mine` prints them)"
                      % (str(token or ""), MIN_PREFIX)), []
    hits = _newest(row for key, row in current.items() if key.startswith(rid))
    if len(hits) == 1:
        return hits[0], None, hits
    if hits:
        return None, ("ambiguous row id prefix %s: %d rows start with it"
                      % (rid, len(hits))), hits
    for n in range(len(rid) - 1, 7, -1):
        near = [row for key, row in current.items() if key.startswith(rid[:n])]
        if len(near) == 1:
            return None, ("no row id starts with %s; its first %d characters "
                          "name exactly one row, %s — was the id padded?"
                          % (rid, n, near[0]["id"])), near
        if near:
            break
    return None, "no row id starts with %s" % rid, []


def addressed_row(current, row, seat):
    """The row on `row`'s chain addressed to `seat` and still awaiting its
    read, or None. A row with no chain (a legacy row) has none."""
    root = row.get("chain_root")
    if not root or not seat:
        return None
    mine = [r for r in current.values()
            if r.get("chain_root") == root and r.get("id") != row.get("id")
            and r.get("status") in AWAITING and _matches(seat, r.get("recipient"))]
    return (_newest(mine) or [None])[0]


def answered_row(seat, lane, supersedes=None, new_work=False):
    """(row, settled): the row a send from `seat` answers, or None, and
    whether the send itself SETTLES it. Only a `--supersedes` naming exactly
    one row, with no `--new-work` beside it, does. The row a padded
    `--supersedes` id's shorter prefix names, and the newest row on the lane
    addressed to `seat`, are CANDIDATES: named in prose and on the line above
    a corrected command, never filled into one (task/3382 F5, F8)."""
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, False
    if str(supersedes or "").strip():
        row, _why, candidates = resolve(current, supersedes)
        if row is not None:
            return row, not new_work
        return (candidates[0] if len(candidates) == 1 else None), False
    label = str(lane or "").strip()
    label = dispatches._strip_lane_prefix(label) or label
    if not label or not seat:
        return None, False
    mine = [r for r in current.values() if r.get("lane") == label
            and r.get("status") in AWAITING and _matches(seat, r.get("recipient"))]
    return (_newest(mine) or [None])[0], False


def _counterpart(seat, row):
    """The seat on the other end of `row` from `seat`, or None."""
    if not row:
        return None
    sender, recipient = row.get("sender"), row.get("recipient")
    if _matches(seat, recipient) and not _matches(seat, sender):
        return sender
    if _matches(seat, sender) and not _matches(seat, recipient):
        return recipient
    return None


# ---------------------------------------------------------------------------
# (C) the two misroutes
# ---------------------------------------------------------------------------

def misroute_refusal(row, current):
    """Why this seat may not verdict `row`, when it SENT the row and a row on
    the same chain is addressed to it; else None.

    Called by the verdict door, under its lock, for authoring writes. With no
    such row it answers None and the author proof — bound to the recipient's
    exact session — decides, whose refusal now says the caller sent the row.
    A row the seat both sent and received is its own to answer."""
    seat, err = dispatches._acting_author("record this verdict")
    if err or not _matches(seat, row.get("sender")) \
            or _matches(seat, row.get("recipient")):
        return None
    mine = addressed_row(current, row, seat)
    if mine is None:
        return None
    return ("you SENT dispatch %s to @%s, so its verdict is @%s's to record, "
            "not yours. The row on this chain addressed to YOU is %s: record "
            "your read on that one" % (row["id"][:12], row.get("recipient"),
                                        row.get("recipient"), _describe(mine)))


def self_send_refusal(sender, recipient, lane, supersedes=None):
    """Why a send from `sender` to `recipient` is refused as addressed to
    itself, or None when the two are different seats.

    A DM to yourself is never delivered, so the row it mints waits on a
    reader who will never see it. The refusal names the seat on the other end
    of the row this send answers — its sender, for a hand-back — so the
    corrected command can be pasted."""
    if not _matches(sender, recipient):
        return None
    row, _settled = answered_row(sender, lane, supersedes)
    other = _counterpart(sender, row)
    why = ("recipient @%s is the sending seat: a dispatch to yourself reaches "
           "nobody" % sender)
    if other:
        return why + ("; the row this answers, %s, was sent by @%s, so the "
                      "intended recipient is @%s" % (_describe(row),
                                                     row.get("sender"), other))
    return why + ("; no open row on lane %s is addressed to you, so name the "
                  "seat this is for" % (lane or "?"))


# ---------------------------------------------------------------------------
# (B) the corrected command after a refused dispatch verb
# ---------------------------------------------------------------------------

def _ledger_size():
    try:
        return os.path.getsize(dispatches.ledger_path())
    except OSError:
        return -1


def with_corrected(fn, args):
    """Run one dispatch verb; after a refused send, verdict or hold, print ONE
    corrected line.

    A send that exits non-zero AFTER its row was written was answered, not
    refused — its delivery is unconfirmed, and pasting a resend is the one
    thing its own line says not to do — so a send whose call grew the ledger
    prints no corrected line.

    A PLACEHOLDER IS REFUSED BEFORE THE DOOR RUNS: a pasted `<PATH>` is not a
    path, and the door would otherwise record it as one. So is a send brief
    that carries one inside it (task/3382 F6)."""
    verb = args[0] if args else None
    if verb not in CORRECTED_VERBS:
        return fn(args)
    why = unfilled_refusal(args[1:]) \
        or (brief_refusal(args[1:]) if verb == "send" else None)
    if why:
        print("helm dispatch %s: %s" % (verb, why), file=sys.stderr)
        rc = 2
    else:
        before = _ledger_size() if verb == "send" else None
        rc = fn(args)
        if not rc or (verb == "send" and _ledger_size() != before):
            return rc
    line, note = _corrected(args) or (None, None)
    if line:
        for text in (note or "").splitlines():
            print("  " + text, file=sys.stderr)
        print(CORRECTED + line, file=sys.stderr)
    return rc


def corrected(args):
    """The corrected command for one refused dispatch verb, or None."""
    return (_corrected(args) or (None,))[0]


def _corrected(args):
    """(line, note) for one refused dispatch verb, or None. The note, when
    there is one, is printed on the line above."""
    build = {"verdict": verdict_line, "hold": hold_line,
             "send": send_line}.get(args[0] if args else None)
    if build is None:
        return None
    try:
        out = build(list(args[1:]))
    except Exception:                  # noqa: BLE001 — a hint never breaks a refusal
        return None
    return out if isinstance(out, tuple) else (out, None)


def _row_for(token):
    """(current, row, guess) — the row a refused verb named, or the one open
    row addressed to this seat among an ambiguous prefix's candidates. When
    the id names NO row and a shorter prefix of it names one, that row comes
    back as a `guess`: named above the line, never in it (task/3382 F8)."""
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return {}, None, False
    row, _why, candidates = resolve(current, token)
    if row is not None:
        return current, row, False
    if len(candidates) == 1:
        return current, candidates[0], True
    seat, _err = dispatches._acting_author("record this verdict")
    mine = [r for r in candidates if r.get("status") == "open"
            and _matches(seat, r.get("recipient"))]
    return current, (mine[0] if len(mine) == 1 else None), False


def _mine_on_chain(current, row):
    """The row on `row`'s chain addressed to this seat, when `row` is not."""
    seat, err = dispatches._acting_author("record this verdict")
    if err or _matches(seat, row.get("recipient")):
        return row
    return addressed_row(current, row, seat) or row


def _sha_prefix(ref):
    """Is `ref` hex of git's default abbreviation or longer?"""
    ref = str(ref or "").strip().lower()
    return len(ref) >= MIN_TIP and bool(_HEX.fullmatch(ref))


def _full_commit(row, sha):
    """`sha` written out in full when it is a hex prefix naming exactly one
    commit in the row's checkout, else as given: the door refuses it in its
    own words. A REF NAME NEVER RESOLVES (task/3382 F4): `HEAD`, the trunk or
    a branch names whatever the row's checkout holds there now — not the cure
    the seat committed — and main's door refuses a patch tip that is not a
    commit id."""
    if not _sha_prefix(sha):
        return str(sha)
    tip = (dispatches._resolve_tip(row.get("repo_root"), str(sha).strip(),
                                   infer_sha_branch=False) or (None,))[0]
    return tip or str(sha)


def _text(kind, text):
    """The quoted text, or `kind`'s placeholder — naming its cap when the
    text is over it (`_TEXTS`). A text its door refuses WHOLE for any other
    reason is the placeholder too, never carried back to be refused again
    (task/3403): one that is not one printable line, and evidence whose
    subsumption statement the verdict door cannot parse."""
    text = str(text or "").strip()
    over, cap, count = _capped(kind)
    if count(text) > cap:
        return _q(over)
    if not text or not dispatches.one_line(text) or kind == "evidence" \
            and dispatches._parse_subsumption(text)[1]:
        return _q(_TEXTS[kind][0])
    return _q(text)


#: Flags that belong to `send`, which seats mixing the two verbs typed into a
#: verdict: dropped from the corrected verdict WITH their value.
_FOREIGN_VALUED = ("--ref", "--kind", "--note", "--supersedes", "--repo",
                   "--deadline", "--key", "--lane", "--reason", "--to")


def _verdict_words(tokens):
    """(values, evidence) out of a refused verdict's flags, leniently: the
    known flags keep their values, a send flag is dropped with its value, an
    unknown bare flag is dropped, and the evidence starts at the first word
    that is not a flag, or after a bare `--`. An unfilled placeholder among
    the flags (`<--concur|--fix>`) is skipped: the line derives that slot
    again, and a placeholder taken as the first word of the evidence would
    carry every flag after it into the text."""
    bare = set(dispatches.BASIS_FLAGS) | {"--imperfect"} \
        | {"--" + p for p in dispatches.POLARITIES}
    values, i = {}, 0
    while i < len(tokens):
        token = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if token == "--":
            return values, " ".join(tokens[i + 1:])
        if placeholder(token):
            i += 1
            continue
        if token in dispatches._VALUED_VERDICT_FLAGS \
                and nxt is not None and not nxt.startswith("--"):
            values.setdefault(token, []).append(nxt)
            i += 2
        elif token in bare:
            values.setdefault(token, [])
            i += 1
        elif token in _FOREIGN_VALUED and nxt is not None \
                and not nxt.startswith("--"):
            i += 2
        elif token.startswith("--"):
            i += 1
        else:
            return values, " ".join(tokens[i:])
    return values, ""


def _first(values, flag):
    return (values.get(flag) or [None])[0]


def _given(values, flag, hole):
    """The value the seat gave `flag`; `hole` when it gave two different
    ones, which the door refuses (task/3382 F2: the line kept the first);
    None when it gave none."""
    got = list(dict.fromkeys(values.get(flag) or ()))
    return got[0] if len(got) == 1 else (hole if got else None)


def _diff_handoff_words(values, polarity, row=None):
    """Keep a typed diff citation only on a FIX without a patch; leave an
    invalid, duplicate, or unverified reference open rather than repeating its
    refusal. On a resolved row, ask the verdict door's own citation validator."""
    if polarity != "fix" or values.get("--patch-tip"):
        return []
    refs = values.get("--diff-handoff") or ()
    if not refs:
        return []
    ref = refs[0] if len(refs) == 1 else DIFF_HANDOFF_PLACEHOLDER
    if not dispatches._DIFF_HANDOFF_REF.fullmatch(str(ref)):
        ref = DIFF_HANDOFF_PLACEHOLDER
    elif row and row.get("tip"):
        current, unavailable = dispatches.snapshot()
        if not unavailable and dispatches._cite_diff_handoff(
                ref, row, current, row["tip"])[1]:
            ref = DIFF_HANDOFF_PLACEHOLDER
    return ["--diff-handoff", _q(ref)]


def _one(values, flags):
    """The one of `flags` the seat gave, or None when it gave none or two:
    two answers to one choice are refused by the door, and the line keeps
    neither for the seat (task/3382 F1, F2)."""
    given = [flag for flag in flags if flag in values]
    return given[0] if len(given) == 1 else None


def _unknown(value):
    """Is `value` the literal UNKNOWN an observation may be declared as?"""
    return str(value or "").upper() == dispatches.UNKNOWN_OBSERVATION


def _observations(values, owed):
    """The finding flags: the seat's own, else — when the polarity owes them
    — placeholders. A count declared UNKNOWN takes its relation with it,
    because the door refuses a relation beside an UNKNOWN count; a counted
    relation the seat gave beside one is two answers, and both become
    placeholders. A count or relation the door refuses by its shape is its
    placeholder, and a counted relation with no count owes one, because the
    door refuses a relation that describes nothing counted (task/3403)."""
    count = _given(values, "--finding-count", COUNT_PLACEHOLDER)
    relation = _given(values, "--prior-relation", RELATION_PLACEHOLDER)
    unknown = _unknown(count)
    if count and not unknown \
            and dispatches.typed_finding_count(count) is None:
        count = COUNT_PLACEHOLDER
    if relation and not _unknown(relation) \
            and relation not in dispatches.PRIOR_RELATIONS:
        relation = RELATION_PLACEHOLDER
    if unknown and relation and not _unknown(relation):
        count, relation = COUNT_PLACEHOLDER, RELATION_PLACEHOLDER
    if relation and not count and not _unknown(relation):
        count = COUNT_PLACEHOLDER
    if owed:
        count = count or COUNT_PLACEHOLDER
        relation = relation or (dispatches.UNKNOWN_OBSERVATION if unknown
                                else RELATION_PLACEHOLDER)
    out = []
    if count:
        out += ["--finding-count", _q(count)]
    if relation:
        out += ["--prior-relation", _q(relation)]
    return out


def _exit_and_cure(row, values, polarity):
    """The exit answer a FIX/SUPERSEDE owes and the cure answer a FIX owes:
    the seat's own where it gave ONE, and where it gave none or both, the
    placeholder naming both answers (task/3382 F2, F7) — the line never
    picks `--worse-than-main` or `--no-patch-because` for it. With the
    polarity unknown (None) only what the seat gave is carried: a
    placeholder here would be refused beside a concur. A path, a patch tip
    or a reason the door refuses by its shape is its placeholder (task/3403).
    """
    out = []
    worse = values.get("--worse-than-main") or []
    imperfect = "--imperfect" in values
    if polarity in (None, "fix", "supersede"):
        if worse and imperfect:
            out.append(_q(EXIT_PLACEHOLDER))
        elif worse or imperfect:
            for path in worse:
                out += ["--worse-than-main", _q(
                    "<PATH>" if dispatches._clean_worse_than_main_paths(
                        [path])[1] else path)]
            out += ["--imperfect"] if imperfect else []
        elif polarity:
            out.append(_q(EXIT_PLACEHOLDER))
    if polarity not in (None, "fix"):
        return out
    patch = _given(values, "--patch-tip", "<FULL_SHA>")
    reason = _given(values, "--no-patch-because", "<REASON>")
    if patch and reason:
        out.append(_q(CURE_PLACEHOLDER))
    elif patch:
        out += ["--patch-tip", _q(_cure_tip(row, patch))]
    elif reason:
        out += ["--no-patch-because", _text("cure", reason)]
    elif imperfect and not worse and polarity:
        # IMPERFECT CARRIES A CURE: the seat's own exit answer settles it.
        out += ["--patch-tip", _q("<FULL_SHA>")]
    elif polarity:
        out.append(_q(CURE_PLACEHOLDER))
    return out


def _cure_tip(row, sha):
    """The patch tip a line carries: `<FULL_SHA>` for one the door refuses by
    what it is — not hex (a ref name such as HEAD), or a commit measured NOT
    to descend from the row's tip — else the seat's own, in full when it
    names one commit here (task/3403). A hex prefix no commit here answers
    comes back AS TYPED, quoted: fetching the cure from the seat's own clone
    admits the same line."""
    if not _sha_prefix(sha):
        return "<FULL_SHA>"
    full = _full_commit(row, sha)
    tip, root = str(row.get("tip") or ""), row.get("repo_root")
    if root and dispatches._FULL_TIP.fullmatch(tip) \
            and dispatches._FULL_TIP.fullmatch(full):
        from . import vcs              # function-scope by module convention
        if vcs.backend(root).ancestry(root, tip, full) == vcs.NOT_ANCESTOR:
            return "<FULL_SHA>"
    return full


def _not_a_block(values, polarity):
    """Does this read answer the exit question IMPERFECT with no cure its
    direction can carry — a SUPERSEDE, or a FIX whose one cure answer is no
    cure? The door refuses it as not a block (task/3403)."""
    if "--imperfect" not in values or values.get("--worse-than-main"):
        return False
    return polarity == "supersede" or polarity == "fix" \
        and bool(values.get("--no-patch-because")) \
        and not values.get("--patch-tip")


def verdict_line(rest):
    """(line, note): the corrected `helm dispatch verdict` — the row's full
    id, the tip the seat typed (in full when it is a prefix of the row's
    tip, else `<TIP_YOU_READ>`), the polarity and basis the seat gave (else
    placeholders: the line never picks a direction or a basis for it), and
    every answer the polarity owes. An id that names no row puts
    `<ROW_ID>` in the command and its candidate above it."""
    typed = rest[1] if len(rest) > 1 and not rest[1].startswith("--") \
        else None
    values, evidence = _verdict_words(rest[2:] if typed is not None
                                      else rest[1:])
    current, row, guess = _row_for(rest[0] if rest else "")
    if row is None:
        return "helm dispatch list --mine --open"
    if guess:
        rid, note = ROW_PLACEHOLDER, _guessed(row, rest[0])
        tip = _typed_tip(row, typed)[0]
    else:
        row = _mine_on_chain(current, row)
        stop = _not_open(row)
        if stop:
            return stop
        rid = row["id"]
        tip, note = _typed_tip(row, typed)
    polarity = _one(values, ["--" + p for p in dispatches.POLARITIES])
    polarity = polarity[2:] if polarity else None
    basis = _one(values, sorted(dispatches.BASIS_FLAGS)) or BASIS_PLACEHOLDER
    advisory = any(flag in values for flag, _hole in _RUN_FLAGS)
    if advisory and polarity == "approve":
        # A MODEL RUN NEVER APPROVES (`_on_behalf_shape`), so the direction
        # is the refused choice and the seat's to make again (task/3403).
        polarity = None
    if polarity == "approve" \
            and not dispatches._GATE_TOKEN_RE.search(evidence or ""):
        # THE DOOR'S OWN REPAIR: a clean source read holds, and the approve
        # binds the token the integrator's land gate mints.
        return "helm dispatch hold %s --source-clean %s -- %s" % (
            _q(rid), tip, _text("reason", evidence)), note
    # AN IMPERFECT READ NO CURE ANSWERS REOPENS ITS DIRECTION (task/3403):
    # the line names the two ways out, and carries neither the hand-back
    # nor the exit and cure answers the door refused together.
    reopened = _not_a_block(values, polarity) or polarity is None \
        and any(word in _REOPENED for word in rest)
    direction = (RUN_NOT_A_BLOCK_PLACEHOLDER if advisory
                 else NOT_A_BLOCK_PLACEHOLDER) if reopened \
        else "--" + polarity if polarity else POLARITY_PLACEHOLDER
    if reopened:
        polarity = None
    parts = ["helm dispatch verdict", _q(rid), tip, _q(direction), _q(basis)]
    parts += _observations(values, polarity == "fix" and not advisory)
    parts += [] if reopened else _exit_and_cure(row, values, polarity)
    parts += _riders(values, polarity, advisory, row)
    return " ".join(parts + ["--", _text("evidence", evidence)]), note


#: The direction placeholders a reopened read carries: pasted back unfilled,
#: the line prints them again.
_REOPENED = (NOT_A_BLOCK_PLACEHOLDER, RUN_NOT_A_BLOCK_PLACEHOLDER)
#: The flags that record a model run's read on the seat's row, and the
#: placeholder each becomes.
_RUN_FLAGS = (("--reviewer-model", MODEL_PLACEHOLDER),
              ("--reviewer-run", RUN_PLACEHOLDER),
              ("--author-model", MODEL_PLACEHOLDER))


def _run_word(values, flag, hole):
    """One model-run value: the seat's own, else `hole` when it gave two or
    one that is not the one token the door takes."""
    word = _given(values, flag, hole)
    if word is None or placeholder(word) \
            or dispatches._ON_BEHALF_TOKEN.fullmatch(str(word).strip()):
        return word
    return hole


def _riders(values, polarity, advisory, row=None):
    """The meld, the design findings and a model run's read, each only where
    the door admits it beside `polarity` (None: still the seat's choice), and
    each value given twice or refused by its shape as its placeholder
    (task/3403): a meld rides an approve or a FIX that may carry a cure,
    never a model run's read; a design finding rides a FIX; a model run
    names its model and its run as a pair, and never a model that does not
    review."""
    out = []
    meld = _given(values, dispatches._MELD_FLAG, ROOM_PLACEHOLDER)
    no_cure = values.get("--no-patch-because") \
        and not values.get("--patch-tip")
    if meld and not advisory and (polarity in (None, "approve")
                                  or polarity == "fix" and not no_cure):
        out += [dispatches._MELD_FLAG, _q(meld)]
    if polarity in (None, "fix"):
        for finding in values.get("--design-finding") or ():
            out += ["--design-finding", _text("design", finding)]
    if not advisory:
        return out + _diff_handoff_words(values, polarity, row)
    model, run, author = (_run_word(values, flag, hole)
                          for flag, hole in _RUN_FLAGS)
    if model and not placeholder(model) \
            and dispatches.reviewer_model_error(model):
        model = MODEL_PLACEHOLDER
    out += ["--reviewer-model", _q(model or MODEL_PLACEHOLDER),
            "--reviewer-run", _q(run or RUN_PLACEHOLDER)]
    return out + (["--author-model", _q(author)] if author else [])


def _take(words, flag):
    """Remove `flag` and its value from `words`; the value, else None."""
    if flag not in words:
        return None
    at = words.index(flag)
    value = words[at + 1] if at + 1 < len(words) else ""
    del words[at:at + 2]
    return value


def _clean_tip(row, typed):
    """(word, note) for a source-clean tip: the seat's own, resolved in the
    row's checkout, when the hold door would take it (it may DESCEND from the
    row's tip: a cure round moves it on). Any other is `<TIP_YOU_READ>` with
    the note naming where the row is now, never the row's tip put in its
    place."""
    typed = str(typed or "").strip()
    tip = None
    if typed and not placeholder(typed):
        tip = (dispatches._resolve_tip(row.get("repo_root"), typed,
                                       infer_sha_branch=False) or (None,))[0]
    if tip and not dispatches._source_clean_lineage_error(
            row.get("repo_root"), row, tip):
        return _q(tip), None
    return _q(TIP_YOU_READ), _now_at(row)


def _free_text(words):
    """The text after a verb's flags, as its door joins it: a placeholder
    word BEFORE the first `--` is a slot the line fills again, never text,
    and that first `--` ends the flags; the rest is text, verbatim."""
    cut = words.index("--") if "--" in words else len(words)
    words = [w for w in words[:cut] if not placeholder(w)] + words[cut:]
    return " ".join(words[1:] if words[:1] == ["--"] else words)


def hold_line(rest):
    """(line, note): the corrected `helm dispatch hold` — the row's full id,
    the source-clean tip the seat typed (resolved in the row's checkout, else
    `<TIP_YOU_READ>`), and the reason whole or a placeholder naming the cap.
    The holder is the seat's: a hold on BOTH holders, a `--meld` with no
    source-clean tip, or the holder placeholder pasted back leaves it
    `<--owner-gated|--source-clean TIP>` (task/3382 F2)."""
    words = [w for w in rest if w not in ("--owner-gated", HOLDER_PLACEHOLDER)]
    owner = "--owner-gated" in rest
    clean = _take(words, "--source-clean")
    meld = _take(words, dispatches._MELD_FLAG)
    open_holder = owner and clean is not None or meld and clean is None \
        or HOLDER_PLACEHOLDER in rest and not owner and clean is None
    current, row, guess = _row_for(words[0] if words else "")
    if row is None:
        return "helm dispatch list --mine --open"
    if guess:
        rid, note = ROW_PLACEHOLDER, _guessed(row, words[0])
    else:
        if clean is not None and not open_holder:
            row = _mine_on_chain(current, row)
        stop = _not_open(row)
        if stop:
            return stop
        rid, note = row["id"], None
    parts = ["helm dispatch hold", _q(rid)]
    if open_holder:
        parts.append(_q(HOLDER_PLACEHOLDER))
    elif clean is None:
        parts += ["--owner-gated"] if owner else []
    else:
        tip, tip_note = _clean_tip(row, clean)
        parts += ["--source-clean", tip]
        note = note or tip_note
    parts += [dispatches._MELD_FLAG, _q(meld)] if meld else []
    return " ".join(parts + ["--", _text("reason", _free_text(words[1:]))]), \
        note


def _send_parts(rest):
    """(recipient and lane words, brief words, values, bare flags) out of a
    refused send, read through the SEND DOOR'S OWN partition and parser
    (task/3382 F3), lenient only where the door refuses: an option it does
    not take stays a word of the brief (and is dropped before the recipient
    and lane, where it can be neither), and a valued option with no value is
    left out. A PLACEHOLDER IS RECOGNIZED BY ITS SPELLING ANYWHERE (F6): past
    the recipient and lane it is never a word of the brief."""
    pos, values, flags, _err = dispatches._parse_send(list(rest),
                                                      lenient=True)
    head, brief = [], []
    for word in pos or ():
        if len(head) < 2:
            head += [] if word.startswith("--") else [word]
        elif not placeholder(word):
            brief.append(word)
    return head, brief, values or {}, flags


def _send_words(rest):
    """(recipient, lane, brief, values, flags): a placeholder in the
    recipient or lane SLOT leaves it empty, and a brief that carries one
    inside a word is not a brief: it comes back None."""
    head, brief, values, flags = _send_parts(rest)
    recipient, lane = [("" if placeholder(w) else w)
                       for w in (head + ["", ""])[:2]]
    return (recipient, lane, None if _carried(brief) else " ".join(brief),
            values, flags)


def brief_refusal(argv):
    """Why a send's brief cannot be recorded: a word of it CARRIES a
    placeholder a corrected send line emits (task/3382 F6: re-pasted lines
    folded them into the brief, which then passed every door). None when it
    carries none. A brief that quotes one on purpose goes on stdin."""
    names = _carried(_send_parts(argv)[1])
    if not names:
        return None
    return ("the brief carries %s, %s a corrected line left, not your brief: "
            "type the brief in its place (a brief that quotes one on purpose "
            "goes on stdin)" % (", ".join(_q(n) for n in names),
                                "a placeholder" if len(names) == 1
                                else "placeholders"))


def _already_sent(seat, recipient, lane, tip):
    """Does an awaiting row from `seat` to `recipient` on `lane` at `tip`
    exist? Then the send that exited non-zero was a retry the ledger already
    answered (delivery unconfirmed, or recorded before), not a refusal."""
    current, unavailable = dispatches.snapshot()
    label = dispatches._strip_lane_prefix(str(lane or "").strip()) or lane
    return not unavailable and any(
        r.get("lane") == label and r.get("tip") == tip
        and r.get("status") in AWAITING and _matches(seat, r.get("sender"))
        and _matches(recipient, r.get("recipient")) for r in current.values())


def _answers(row, lane, recipient):
    """The line above a send line whose work arm the seat left open while an
    open row on its lane is addressed to it (task/3382 F5)."""
    return ("%s is the newest open row on lane %s addressed to you: if this "
            "send answers it, type --supersedes %s in place of %s%s"
            % (_describe(row, True), lane, row["id"], WORK_PLACEHOLDER,
               ", and @%s as its recipient" % recipient if recipient else ""))


def send_line(rest):
    """(line, note): the corrected `helm dispatch send`. What the send SETTLES
    is filled: a `--supersedes` naming one row gives its full id and, for a
    recipient that is this seat or absent, the seat on the other end of that
    row. The ref and the kind are the seat's own or placeholders: the
    answered row's tip is what the seat was SENT, not what it sends back. A
    hex ref is written out in full; a ref NAME is kept as typed, for the door
    to resolve where the seat's own command would. The work arm is the
    seat's own, or `<--new-work|--supersedes ROW>` — never a row it did not
    name, which is offered on the line above (task/3382 F5, F8). None when
    the send's row already exists."""
    recipient, lane, brief, values, flags = _send_words(rest)
    seat, _err = dispatches._acting_author()
    typed = recipient
    new_work, supersedes = "--new-work" in flags, values.get("--supersedes")
    row, settled = answered_row(seat, lane, supersedes, new_work) \
        if seat else (None, False)
    info = dispatches._repo_info(values.get("--repo"))
    parent_repo_id = (row.get("repo_id") or (dispatches.home_repo_id()[0] if hasattr(dispatches, "home_repo_id") else None)) if row else None
    if not info and row:
        info = dispatches._repo_info(row.get("repo_root"))
    send_repo_id = info.get("repo_id") if info else None
    cross_repo = bool(
        supersedes and row and parent_repo_id and send_repo_id
        and dispatches._real(parent_repo_id) != dispatches._real(send_repo_id)
    )
    if not recipient or _matches(seat, recipient):
        recipient = _counterpart(seat, row) if (settled or cross_repo) else ""
    elif _address_error(recipient):
        # THE DOOR'S OWN ANSWER ABOUT THE ADDRESS (task/3403): one it refuses
        # is the refusal's cause, and carried back it is refused again.
        recipient = ""
    lane = lane or (row.get("lane") if (settled or cross_repo) else "")
    if cross_repo and row is not None:
        settled = False
        pid12 = row["id"][:12]
        if brief and not brief.startswith("--"):
            if pid12 not in brief and row["id"] not in brief:
                brief = "%s (hand-back for %s)" % (brief, pid12)
        elif not brief:
            brief = "<brief (hand-back for %s)>" % pid12
    ref, tip = values.get("--ref"), None
    if ref and info:
        tip = (dispatches._resolve_tip(info["repo"], ref,
                                       infer_sha_branch=False) or (None,))[0]
    if tip and _already_sent(seat, typed, lane, tip):
        return None
    # THE SEAT'S REF, OR NONE: one that did not resolve here may only need
    # its --repo, and the row's tip is a different claim.
    # A VALUE THE DOOR REFUSES BY ITS SHAPE IS ITS SLOT'S PLACEHOLDER, never
    # carried back to be refused again (task/3403).
    kind = values.get("--kind")
    kind = kind if kind and not dispatches.clean_kind(kind)[1] else None
    carried = brief and not brief.startswith("--") \
        and len(brief) <= dispatches.MESSAGE_ARG_CAP \
        and len(brief.encode("utf-8")) <= dispatches.BRIEF_CEILING
    parts = ["helm dispatch send", _q(recipient or "<recipient>"),
             _q(_shaped(lane, "LANE_CAP") or "<lane>"),
             _q(brief if carried else "<brief>")]
    parts += ["--ref", _q(tip if tip and _sha_prefix(ref)
                          else _shaped(ref, "REF_CAP") or "<TIP>"),
              "--kind", _q(kind or "<build|review>")]
    notes = []
    if cross_repo and row is not None:
        parts.append("--new-work")
        notes.append(
            "parent dispatch %s is in %s, while this ref is in %s: a chain "
            "stays inside one repository, so a cross-repo hand-back sends as "
            "--new-work citing the parent in the brief"
            % (row["id"][:12], parent_repo_id, send_repo_id))
    elif new_work and not supersedes:
        parts.append("--new-work")
    elif settled:
        parts += ["--supersedes", _q(row["id"])]
    elif supersedes and not new_work and row is not None:
        parts += ["--supersedes", _q(ROW_PLACEHOLDER)]
        notes.append(_guessed(row, supersedes))
    elif supersedes and not new_work:
        parts += ["--supersedes", _q(ROW_PLACEHOLDER if _names_no_row(
            supersedes) else supersedes)]
    else:
        # Work identity is the sender's choice: with no arm, or both, the
        # line names neither for it.
        parts.append(_q(WORK_PLACEHOLDER))
        if row is not None and not new_work:
            notes.append(_answers(row, lane or row.get("lane"),
                                  "" if recipient
                                  else _counterpart(seat, row)))
    for flag in _SEND_CARRIED:
        if values.get(flag):
            check, hole = _SEND_SHAPES.get(flag, (None, None))
            parts += [flag, _q(hole if check and check(values[flag])
                               else values[flag])]
    parts += _owed(brief if carried else None, lane, values,
                   dispatches.clean_kind(kind)[0],
                   None if cross_repo else supersedes, seat, recipient)
    parts += ["--force"] if "--force" in flags else []
    if brief is not None and brief.startswith("--"):
        # NO ARGUMENT CAN CARRY IT: the door reads a word that starts `--`
        # as an option, so the brief is the seat's to retype.
        parts[3] = _q("<brief>")
        notes.append("your brief begins with %s, which the send door reads "
                     "as an option: type one that begins with a word, or "
                     "pipe it on stdin" % _q(brief.split()[0]))
    return (" ".join(parts), "\n".join(notes)) if notes else " ".join(parts)


#: The send flags a corrected line carries as the seat typed them.
_SEND_CARRIED = ("--note", "--deadline", "--repo", "--key", "--posture-na",
                 "--read-only-because", "--async-because", "--disputes",
                 "--meld")
#: THE SEND VALUES ITS DOOR REFUSES BY SHAPE ALONE (task/3403): each flag's
#: check, the door's own, and the placeholder a refused value becomes.
_SEND_SHAPES = {
    "--deadline": (lambda v: dispatches._deadline(v)[1], SECONDS_PLACEHOLDER),
    "--note": (lambda v: dispatches._clean(v, "note", dispatches.NOTE_CAP)[1],
               NOTE_PLACEHOLDER),
    "--key": (lambda v: dispatches._clean(
        v, "operation key", dispatches.OPERATION_KEY_CAP)[1], KEY_PLACEHOLDER),
}


def _shaped(value, cap):
    """`value` when the send door's `_clean` takes it under the `dispatches`
    cap named `cap`, else None: its slot's placeholder then stands."""
    if not value:
        return None
    return None if dispatches._clean(value, "value", getattr(
        dispatches, cap))[1] else value


def _address_error(recipient):
    """Why the send door refuses `recipient` as an address, or None: the
    door's own reading (`dispatches._recipient_gate`), never a copy."""
    from . import seats                 # deferred, as dispatches does
    return seats.recipient_capability(recipient).get("error")


def _names_no_row(token):
    """Does `token` name no row of a readable ledger? Then a `--supersedes`
    carrying it back is refused again, forever (task/3403)."""
    current, unavailable = dispatches.snapshot()
    return not unavailable and resolve(current, token)[0] is None


def _owed(brief, lane, values, kind, supersedes, seat, recipient):
    """The answers the send door owes this brief and the seat left out, each
    as its placeholder (task/3403): a review brief that forbids editing owes
    `--read-only-because`, one that names a strategy on a dependency seam
    owes a posture or `--posture-na`, and the first build row naming an
    irreversible target owes a meld or `--async-because`. Carried back
    without them, the brief is refused again, forever. Each is asked through
    the door's own check; a brief the line does not carry owes nothing yet."""
    if not brief:
        return []
    from . import posture, review_door  # deferred: both import dispatches
    text = "%s\n%s" % (brief, values.get("--note") or "")
    out = []
    if dispatches.check_read_only("helm dispatch send", text, kind=kind,
                                  because=values.get("--read-only-because")):
        out += ["--read-only-because", _q("<REASON>")]
    if posture.check("helm dispatch send", text,
                     posture_na=values.get("--posture-na")):
        out += ["--posture-na", _q("<REASON>")]
    if kind == "build" and not values.get(dispatches._MELD_FLAG) \
            and not values.get("--async-because") and review_door.plan(
                kind, seat, recipient, lane, None, supersedes=supersedes,
                body=brief)["refuse"]:
        out.append(_q(DOOR_PLACEHOLDER))
    return out


# ---------------------------------------------------------------------------
# (A) helm review done
# ---------------------------------------------------------------------------

def cmd_review(args):
    """`helm review done ...` — the one subverb, and an unknown one refused."""
    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    if args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if args[0] != "done":
        print("helm review: unknown subverb %r — %s" % (args[0], USAGE),
              file=sys.stderr)
        return 2
    return done(args[1:])


def _parse(argv):
    """(opts, positionals, evidence words, rc). rc is set when parsing
    already answered: 0 for --help, 2 for a refusal it printed; `words` is
    None when that refusal stopped the scan part-way.

    A FLAG-SHAPED WORD THE VERB DOES NOT TAKE is refused, and the scan goes
    on so the corrected line keeps the rest (task/3382 F4): one that starts
    with a single dash or holds a space is the start of the evidence
    (`-h foo`), which the line then puts after `--`; an unknown `--flag` is
    dropped from it. A placeholder word past the row and the outcome is a
    slot the line fills again, never evidence (task/3382 F6)."""
    opts, pos, words, bad, i = {}, [], [], None, 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            words += argv[i:]
            break
        if arg in ("-h", "--help"):
            print(USAGE)
            return opts, pos, words, 0
        if arg in VALUED:
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                print("helm review done: %s needs a value" % arg,
                      file=sys.stderr)
                return opts, pos, None, 2
            if arg in opts and not VALUED[arg]:
                # BOTH ANSWERS KEPT, so the line leaves the choice open.
                opts[arg].append(argv[i + 1])
                print("helm review done: %s may be given once" % arg,
                      file=sys.stderr)
                return opts, pos, None, 2
            opts.setdefault(arg, []).append(argv[i + 1])
            i += 2
            continue
        if arg in BARE:
            opts[arg] = []
        elif freetext.FLAG.match(arg):
            bad = bad or arg
            if not arg.startswith("--") or len(arg.split()) > 1:
                words.append(arg)
        elif len(pos) < 2:
            pos.append(arg)
        elif not placeholder(arg):
            words.append(arg)
        i += 1
    if bad:
        return opts, pos, words, freetext.refuse(
            "helm review", "done", bad, "the evidence",
            known=sorted(VALUED) + list(BARE), usage=USAGE.split(
                "helm review done ", 1)[1])
    return opts, pos, words, None


class _Read:
    """What one `done` call knows, so every refusal can print the corrected
    command from it."""

    def __init__(self, token, outcome, evidence, opts):
        self.token, self.outcome = token, outcome
        self.evidence, self.opts = evidence, opts
        # `moved`: the tip this read would bind with no --tip is not the one
        # the seat's own command named — the row was retipped, or the line
        # names another row on its chain. `guess`: `row` is only the
        # candidate a padded id's shorter prefix names (task/3382 F8).
        self.row, self.moved, self.guess = None, False, False

    def refuse(self, why, rc=2):
        if why:
            print("helm review done: " + why, file=sys.stderr)
        try:
            line, note = self.line()
        except Exception:              # noqa: BLE001 — a hint never breaks a refusal
            return rc
        for text in (note or "").splitlines():
            print("  " + text, file=sys.stderr)
        print(CORRECTED + line, file=sys.stderr)
        return rc

    def line(self):
        """(line, note): the corrected `helm review done`, from what this call
        knows, the evidence last after `--`. A row no longer open is answered
        by the verb that reads or reopens it (`_not_open`), and a row the
        seat did not name is `<ROW_ID>`, its candidate named above."""
        stop = _not_open(self.row) if self.row and not self.guess else None
        if stop:
            return stop
        known = OUTCOMES + (CLEAN_OR_CURE_PLACEHOLDER,)
        outcome = self.outcome if self.outcome in known else (
            "clean" if self.outcome == "approve" else "<clean|concur|fix>")
        if outcome == "fix" and _not_a_block(self.opts, "fix"):
            # AN IMPERFECT READ NO CURE ANSWERS REOPENS ITS OUTCOME
            # (task/3403): `clean` is the reviewer's approve.
            outcome = CLEAN_OR_CURE_PLACEHOLDER
        rid = ROW_PLACEHOLDER if self.guess else self.row["id"] if self.row \
            else (self.token or "<row-id-prefix>")
        text = _text("clean" if outcome == "clean" else "evidence",
                     self.evidence)
        parts = ["helm review done", _q(rid), _q(outcome)]
        if outcome != "clean":
            # THE SEAT'S OWN COUNTS, and a FIX owes them (task/3382 F10).
            parts += _observations(self.opts, outcome == "fix")
        if outcome == "fix":
            parts += _exit_and_cure(self.row or {}, self.opts, "fix")
            parts += _diff_handoff_words(self.opts, "fix", self.row)
        elif outcome not in known:
            # THE OUTCOME IS A PLACEHOLDER: what the seat gave, and no more.
            parts += _exit_and_cure(self.row or {}, self.opts, None)
        tip, note = self._tip()
        if self.guess:
            note = _guessed(self.row, self.token)
        return " ".join(parts + tip + ["--", text]), note

    def _tip(self):
        """(words, note) for --tip: the seat's own, in full when it is a
        prefix of the row's tip; `<TIP_YOU_READ>` when it is not, or when the
        seat typed none and the tip this read would bind has moved from the
        one its command named. Never the row's tip in the seat's place."""
        typed = _given(self.opts, "--tip", TIP_YOU_READ)
        if not self.row:
            # ONE THIS VERB REFUSES BY ITS SHAPE is the placeholder
            # (task/3403), never carried back to be refused again.
            typed = typed if typed is None or _sha_prefix(typed) \
                else TIP_YOU_READ
            return (["--tip", _q(typed)] if typed is not None else []), None
        if typed is not None:
            word, note = _typed_tip(self.row, typed)
            return ["--tip", word], note
        if self.moved:
            return ["--tip", _q(TIP_YOU_READ)], _now_at(self.row)
        return [], None


def _reviewed_tip(row, given):
    """(tip, stale, why): the tip this read binds, or the earlier tip of this
    row the read was of, or why `--tip` names neither.

    A row RETIPPED since it was sent now names a tree its reader may never
    have read. So a read with no `--tip` on such a row is refused as stale
    against the tip it was sent at, and `--tip` naming the current tip is how
    a reader says it read that one."""
    now = str(row.get("tip") or "").lower()
    earlier = [str(hop.get("old_tip") or "").lower()
               for hop in row.get("retips") or () if isinstance(hop, dict)]
    if given is None:
        return (None, earlier[0], None) if earlier and earlier[0] \
            and earlier[0] != now else (now, None, None)
    tip = str(given).strip().lower()
    if len(tip) < MIN_TIP or not _HEX.fullmatch(tip):
        return None, None, ("--tip needs %d or more hex characters of the tip "
                            "you read" % MIN_TIP)
    if now.startswith(tip):
        return now, None, None
    old = next((t for t in reversed(earlier) if t.startswith(tip)), None)
    if old:
        return None, old, None
    return None, None, ("--tip %s is no tip dispatch %s was ever at (now %s)"
                        % (tip, row["id"][:12], now[:12]))


def _door_argv(read, reviewed):
    """The dispatch verb table's argv for this read."""
    row, opts = read.row, read.opts
    if read.outcome == "clean":
        return ["hold", row["id"], "--source-clean", reviewed, "--",
                read.evidence]
    argv = ["verdict", row["id"], reviewed, "--" + read.outcome, "--measured"]
    # THE SEAT'S OWN COUNTS, never a default (task/3382 F10): a FIX without
    # them is refused by the door in its words. A count declared UNKNOWN
    # settles its relation, the one the door admits beside it.
    count = _first(opts, "--finding-count")
    relation = _first(opts, "--prior-relation")
    if read.outcome == "fix" \
            and str(count or "").upper() == dispatches.UNKNOWN_OBSERVATION:
        relation = relation or dispatches.UNKNOWN_OBSERVATION
    argv += ["--finding-count", count] if count else []
    argv += ["--prior-relation", relation] if relation else []
    for path in opts.get("--worse-than-main") or ():
        argv += ["--worse-than-main", path]
    argv += ["--imperfect"] if "--imperfect" in opts else []
    patch = _first(opts, "--patch-tip")
    argv += ["--patch-tip", _full_commit(row, patch)] if patch else []
    reason = _first(opts, "--no-patch-because")
    argv += ["--no-patch-because", reason] if reason else []
    handoff = _first(opts, "--diff-handoff")
    argv += ["--diff-handoff", handoff] if handoff is not None else []
    return argv + ["--", read.evidence]


def done(argv):
    """`helm review done <row> clean|concur|fix "<evidence>" [flags]`."""
    opts, pos, words, rc = _parse(list(argv))
    read = _Read(pos[0] if pos else "", pos[1] if len(pos) > 1 else "",
                 "", opts)
    if rc is not None:
        if rc == 0:
            return 0
        if words:
            # THE WHOLE EVIDENCE, AS THE DOOR JOINS IT: a leading `--` ends
            # the flags and is not text; one after a word is the seat's.
            read.evidence = _free_text(words)
        return read.refuse(None, rc)
    evidence, rc = freetext.tail("helm review", "done", words, "the evidence")
    read.evidence = evidence or ""
    if rc is not None:
        return read.refuse(None, rc)
    why = unfilled_refusal(argv)
    if why:
        # THE LINE NAMES THE ROW when the prefix does: its full id, and where
        # its tip is now for a `<TIP_YOU_READ>` still unfilled.
        current, unavailable = dispatches.snapshot()
        read.row = None if unavailable else resolve(current, read.token)[0]
        return read.refuse(why)
    if len(pos) < 2:
        return read.refuse("name the row and what your read concluded — %s"
                           % USAGE)
    if read.outcome not in OUTCOMES:
        return read.refuse("%r is not an outcome: clean (the read found "
                           "nothing; a source-clean hold), concur (endorse, "
                           "blocking nothing) or fix (hand it back)"
                           % read.outcome)
    if not read.evidence.strip():
        return read.refuse("give the evidence: what the read measured, in "
                           "one quoted argument")
    wrong = [flag for flag in NOT_CLEAN if flag in opts]
    if read.outcome == "clean" and wrong:
        return read.refuse("clean records a source-clean hold, which carries "
                           "no %s: a read that found nothing names no cure, "
                           "no exit answer and no findings"
                           % " / ".join(wrong))
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return read.refuse("dispatch ledger unavailable: %s" % unavailable, 1)
    row, why, candidates = resolve(current, read.token)
    if row is None:
        seat, _err = dispatches._acting_author("record this review")
        mine = [r for r in candidates if r.get("status") == "open"
                and _matches(seat, r.get("recipient"))]
        # ONE CANDIDATE is a padded id's shorter prefix: a guess, named above
        # the line and never in it (task/3382 F8).
        read.guess = len(candidates) == 1
        read.row = candidates[0] if read.guess else (
            mine[0] if len(mine) == 1 else None)
        if read.row is None:
            # THE TYPED ID IS THIS REFUSAL'S CAUSE (task/3403): carried back,
            # it is refused again, forever. The slot is its placeholder and
            # the candidates, if any, are named above the line.
            read.token = None
        for cand in candidates[:CANDIDATES_SHOWN] if len(candidates) > 1 else ():
            why += "\n  " + _describe(cand)
        if len(candidates) > CANDIDATES_SHOWN:
            why += "\n  ... and %d more" % (len(candidates) - CANDIDATES_SHOWN)
        return read.refuse(why)
    read.row = row
    seat, err = dispatches._acting_author("record this review")
    if err:
        return read.refuse(err, 1)
    if not _matches(seat, row.get("recipient")):
        mine = addressed_row(current, row, seat)
        said = ("you SENT dispatch %s to @%s; its read is @%s's to record, not "
                "yours" if _matches(seat, row.get("sender")) else
                "dispatch %s is addressed to @%s, not to you; its read is "
                "@%s's to record") % (row["id"][:12], row.get("recipient"),
                                       row.get("recipient"))
        if mine is None:
            read.row = None
            read.token = None
            print("helm review done: %s, and no open row on this chain is "
                  "addressed to you" % said, file=sys.stderr)
            print(CORRECTED + "helm dispatch list --mine --open",
                  file=sys.stderr)
            return 1
        # THE CHAIN SETTLES THE ROW, NOT THE TREE: the seat's command named
        # the tip of the row it typed, so another tip is its to name.
        read.row, read.moved = mine, mine.get("tip") != row.get("tip")
        return read.refuse("%s. The row on this chain addressed to YOU is %s"
                           % (said, _describe(mine)), 1)
    reviewed, stale, why = _reviewed_tip(row, _first(opts, "--tip"))
    if why:
        return read.refuse(why)
    if stale:
        read.moved = True
        print("helm review done: " + dispatches.stale_tip_refusal(
            stale, row["tip"]), file=sys.stderr)
        return read.refuse("dispatch %s was retipped since it was sent (%s -> "
                           "%s): read %s, then name it with --tip"
                           % (row["id"][:12], stale[:12], row["tip"][:12],
                              row["tip"][:12]), 1)
    argv = _door_argv(read, reviewed)
    print("helm review: %s @%s %s at %s — through the dispatch %s door" % (
        row["id"][:12], row.get("recipient"), row.get("lane") or "?",
        reviewed[:12], DOORS[read.outcome]))
    rc = dispatches._cmd_dispatch(argv)
    return read.refuse(None, rc) if rc else 0
