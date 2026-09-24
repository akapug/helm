#!/usr/bin/env python3
"""helm tasks — the FLEET TASK LEDGER: shared, resolvable work items.

WHY THIS FILE EXISTS. Until 2026-08-05 the fleet's task numbering lived inside
ONE seat's harness task list. Every "#263" written in the room resolved for
that seat and for nobody else — measured: 441 citations across 260 chat
rows, of which the rest of the fleet could look up exactly ZERO. Seats could
not tell a live item from a closed one, could not check whether a finding was
already filed, and at least one filed a duplicate because the class was
unlookupable. The owner ruled: those tasks "need to get converted to a
group-wide set of tasks in the ledger rows ... so that they can stop polluting
your personal space and be accessible to the whole team."

WHY A SIBLING FILE RATHER THAN A `kind` ON owner-decisions. That experiment has
already been run once, on the sibling ledger, and lost: ownerasks.py:687-691
records an ask queue holding 19 rows of which ELEVEN were engineering tasks,
and "they buried the four that were real." Keeping 120 engineering tasks out of
the 3 rulings the owner actually has to make is a STRUCTURAL property here and
a conditional one there — a `kind` discriminator would need four independent
exclusions (board_queue, the odq badge count, the stop-whisper rung, its own
section) to all hold at once, and N guards each with a clean conscience are
exactly how this repo keeps building blind spots.

IDS ARE THE CROSSWALK, AND THAT IS THE WHOLE POINT. A row's id is
`task/<n>` where <n> is the ORIGINAL number from the private list, so every
historical "#263" resolves BY CONSTRUCTION — no lookup table, no alias verb,
no migration of the citations themselves. `resolve()` accepts every spelling a
seat might type or paste: 263, #263, task/263, task-263.

THE NUMBERING CONTINUES, IT DOES NOT FORK. A new task takes the next integer
after the highest the ledger holds, so the sequence the owner and the fleet
have been reading all week keeps running rather than restarting beside itself.
Minting reads the maximum UNDER THE LEDGER LOCK and appends without releasing
it, because two seats filing at the same instant would otherwise both read the
same maximum and mint the same id — the one race that would put two different
works behind one citation.

Row schema (every mutation appends a full SNAPSHOT; last line per id wins):
  {id, ts, title, status: open|in_progress|closed, owner, note, refs,
   source, origin, closed_reason, last_updated, takeover?}

``takeover`` is present only on an evidence-bound BUILD continuation. It binds
one transfer_id, source/successor lineage, exact measured evidence, and both chat
receipts in the same task snapshot that changes owner.

TOMBSTONES ARE FIRST-CLASS. 55% of the fleet's citation load points at items
that were CLOSED before the migration. A tombstone carries the id, the title
and `status: closed` and nothing else — it exists so that a seat reading a
week-old row gets a sentence instead of a dangling pointer. It is never work,
never assigned, and never shown in the open list.
"""
import calendar
import json
import os
import re
import shlex
import sys
import time

from . import eventledger, freetext, home
# THE DUPLICATE INDEX IS IMPORTED, NEVER RE-SPELLED. The tokenizer and the
# overlap threshold this module's add door refuses on are the STORE's — one
# similarity rule for the repo — so retuning it there moves this door too.
from .store import index as dupindex

STATUSES = ("open", "in_progress", "closed")
OPEN_STATUSES = ("open", "in_progress")

# WHO ASKED FOR THIS WORK — a one-bit fact known for FREE at filing time, not a
# guess an agent makes about work it has not done. The owner asked for "major
# requests FROM ME" to be visible; that is PROVENANCE, and the field to carry
# it ALREADY EXISTED and was unreachable: `origin` is populated on 251 of 291
# rows by the migration alone, while add() never passed it and update()'s
# allowed tuple omitted it. So 16 of 114 live rows smuggled provenance in FOUR
# incompatible spellings — title prefix, note prefix, a verbatim quote, and 98
# rows with nothing — which is the same disease as priority-in-free-text, one
# field over: a convention agents invented because the field was missing, that
# nothing can sort, filter or enforce.
#
# NOT `source`, and the reason is live: source is the FILING SEAT, and task/201
# is open on a signer fallback that mis-attributes an unset profile to the
# owner's name. A source row naming the owner is indistinguishable from a
# misconfigured seat. `origin` carries no such collision.
#
# LEGACY IS UNKNOWN, NEVER BACKFILLED. The 251 migration rows carry free-text
# origins nobody witnessed as provenance; they pass through and read UNKNOWN,
# which is TRUE. Inventing a provenance for a row nobody witnessed would be the
# erasure this field exists to end, performed in the other direction.
ORIGINS = ("owner", "agent")

# PRIORITY, AND THE COMMENT ABOVE PREDICTED IT. That note calls
# "priority-in-free-text" the same disease as the origin gap it was written to
# cure — "a convention agents invented because the field was missing, that
# nothing can sort, filter or enforce" — and then the field was never added.
#
# MEASURED 2026-08-25, and the prediction was exactly right: 40 of 204 open
# rows (19%) carry a priority in their title or note, in SEVEN incompatible
# spellings — P0 x21, P1 x14, BLOCKER x6, P2 x5, and one each of LOW PRIORITY,
# URGENT and CRITICAL. Every one of those is invisible to sort and filter, so
# the board cannot answer "what is most important" and a coordinator reads 204
# undifferentiated rows.
#
# NONE IS A REAL VALUE AND IT IS THE DEFAULT. An unset priority means NOBODY
# HAS RANKED THIS, which is different from "ranked low" and must never be
# rendered as P3 — the same absence-versus-value law this module already keeps
# for origin. Legacy rows are NOT backfilled from their free text: a P0 typed
# in a title was one agent's opinion at one moment, and promoting it to a
# sortable field would launder that into a fleet-wide ranking nobody witnessed.
PRIORITIES = ("P0", "P1", "P2", "P3")

# THE WORDS THIS LEDGER PRINTS FOR "NOBODY", NAMED ONCE so the renderer and the
# guard cannot drift apart — because they already had.
#
# MEASURED 2026-08-07 across 383 rows: 18 store the literal string "UNOWNED" in
# their owner field, and task/387's ledger events show it arrived at add() time,
# not by a later edit. The mechanism is not a typo. This file uses UNOWNED as
# its WORD FOR ABSENCE in three places — the owner column renders it (_fmt), the
# comment block above add() is titled with it, and cmd_task's own reasoning says
# "So: UNOWNED unless someone says otherwise" — while accepting that same word
# as a NAME. A caller who reads the column, or the design prose, and passes
# `--owner UNOWNED` to mean "nobody" is answering the question the vocabulary
# asked them.
#
# A SHAPE CHECK WOULD NOT HAVE CAUGHT THIS, which is the interesting part:
# "UNOWNED" matches home._SEAT_NAME_RE perfectly ([A-Za-z0-9._-], bounded), so
# it is a well-formed seat name. The collision is between two VOCABULARIES —
# what we render for absence and what we accept as an identity — so the cure is
# to RESERVE the render words, not to tighten the shape.
UNOWNED_DISPLAY = "UNOWNED"
CLOSED_OWNER_DISPLAY = "-"
_RESERVED_OWNER_WORDS = frozenset(
    w.casefold() for w in (UNOWNED_DISPLAY, CLOSED_OWNER_DISPLAY))


def _owner_placeholder_error(owner):
    """An error string when `owner` is a word we PRINT for absence, else None.

    Refuses rather than silently normalising, because a caller who typed the
    display word has a belief worth correcting: they think it names a state,
    and the message tells them the state is spelled `--owner ''`. Silently
    accepting it would leave them expecting a claimable row."""
    if str(owner or "").strip().casefold() not in _RESERVED_OWNER_WORDS:
        return None
    return ("%r is the word this ledger PRINTS for an absent owner, not a "
            "seat. Stored, it makes the row UNCLAIMABLE: the incumbent guard "
            "then sees a holder by that name and refuses every ordinary `claim`. "
            "To leave a row unowned pass `--owner ''` or "
            "omit the flag — an open unowned row is what the offer rung "
            "routes to an idle seat." % (str(owner).strip(),))


def owner_of(row):
    """The row's REAL owner, or "" — THE ONE PLACE THAT DECIDES.

    Every reader asking "who holds this" comes through here, so the claim
    guard, the display and the offer text cannot disagree. The door above
    stops new placeholder rows; this makes the 18 that already exist behave
    as what they meant, without a migration that rewrites history nobody can
    audit afterwards. Same shape as mcpd._owner_seat, and for the same reason:
    a value that arrives through a FILE is not covered by any check on the
    code paths that write it."""
    owner = str(row.get("owner") or "").strip()
    return "" if owner.casefold() in _RESERVED_OWNER_WORDS else owner


# A STAND-DOWN IS A STATE, NOT A SENTENCE, AND IT IS DELIBERATELY NOT A STATUS.
# task/406: a stand-down existed only as chat prose, so no guard could consult
# it and the work-offer rung kept handing out deliberately-paused rows. The
# obvious cure — a fourth STATUS — is wrong, and wrong in the direction that
# hides work: `open_rows` is the door BOTH `list` and `offer_rows` read
# through, so a "paused" status would drop the row out of the backlog listing
# as well as the offer, and a row nobody is offered AND nobody can see is
# indistinguishable from a closed one. A stood-down row is WORK OWED. Only the
# OFFER changes; the listing keeps it, marks it, and says why.
STANDDOWN_LIVE = "LIVE"
STANDDOWN_EXPIRED = "EXPIRED"
STANDDOWN_UNREADABLE = "UNREADABLE"
_STANDDOWN_UNIT_S = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
# WHAT A STAND-DOWN MAY PUT ON A SURFACE. show renders the whole payload, so
# these are the bounds the RENDERER needs, expressed where the WRITER can
# enforce them. 200 digits is far under every interpreter's integer-to-string
# conversion limit, so this contract behaves identically on a machine whose
# sys.get_int_max_str_digits differs from the gate's. The key bound is what
# lets a REFUSAL name the field it is refusing.
_STANDDOWN_MAX_DIGITS = 200
_STANDDOWN_MAX_KEY = 64
_STANDDOWN_MAX_RENDER = 4000


def _describe_rejected(value):
    """A description of a REJECTED value, computed WITHOUT RENDERING IT.

    A REFUSAL THAT RENDERS THE REJECTED VALUE INHERITS THAT VALUE'S
    PATHOLOGIES. An int past the interpreter's decimal conversion limit raises
    on str() and repr(), so a guard that formats what it refuses THROWS on
    exactly the hostile input it exists to reject -- and a guard whose refusal
    can throw has not refused.

    TRUNCATION DOES NOT FIX IT, which is the trap: ("%r" % v)[:40] renders the
    whole object FIRST and slices the result, so a capped excerpt raises
    identically. Measured. Every branch below is therefore derived from cheap
    structural properties -- a bit length, a container length, a slice of
    something that is already a string -- and never from a rendering of the
    value as a whole.

    SCOPE, agreed with the reviewer: JSON-shaped values and ordinary Python
    builtins. An arbitrary object whose own __repr__ raises is not in scope
    and this is not a sandbox; unknown types yield their type name alone,
    which touches nothing."""
    name = type(value).__name__
    if isinstance(value, bool):
        return "%s %s" % (name, value)
    if isinstance(value, int):
        # NEVER str() an int here. bit_length is cheap and cannot raise, and
        # log10(2) turns it into the decimal size a human is actually asking
        # about without ever building the digits.
        import math
        digits = 1 if value == 0 else int(abs(value).bit_length()
                                          * math.log10(2)) + 1
        return "%s of about %d digits" % (name, digits)
    if isinstance(value, float):
        return "%s %r" % (name, value)
    if isinstance(value, str):
        return "%s of %d chars starting %r" % (name, len(value), value[:32])
    if isinstance(value, (list, tuple, set, frozenset)):
        return "%s of %d items" % (name, len(value))
    if isinstance(value, dict):
        return "%s with %d keys" % (name, len(value))
    return name


def _standdown_encodable(text):
    """None, or why this text cannot cross the boundaries that carry it.

    ENCODABILITY AND SIZE ARE DIFFERENT PREDICATES AND THIS CONTRACT KEEPS THEM
    APART. A lone surrogate is a perfectly good Python str of length one: it
    satisfies every character bound, and then the ledger's strict UTF-8 encoder
    refuses it and `show` writing to a real UTF-8 stream raises. So the width
    bound cannot answer this question, and answering it by switching the bound
    to BYTES would be a different contract -- one that rejects valid multibyte
    text the signed character bar admits, since 1400 CJK characters are 4200
    bytes. Two predicates, one door, neither borrowed for the other's job.

    The diagnostic is safe by construction rather than by care: repr escapes a
    surrogate to ASCII, so _describe_rejected can name the offending text
    without ever producing the bytes that cannot be written."""
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "%s is not encodable text" % _describe_rejected(text)
    return None


def _standdown_rendered_len(value):
    """(the EXACT number of characters show would print for this value, None)
    or (None, why it is not a stand-down value).

    ONE GRAMMAR, BECAUSE TWO GRAMMARS DISAGREE AND ONLY ONE OF THEM RUNS. An
    earlier form of this predicate ESTIMATED the size of containers as the sum
    of their parts. That is not an upper bound on what str() produces: a list
    renders its elements with repr, so a 3900-character string of NULs costs
    3902 by the sum and renders as about 15600 -- the estimator and the actual
    renderer are two different languages, and the one that decides is not the
    one that prints. The cure is not a better estimate. It is to accept only
    values whose rendering this function can state EXACTLY.

    SO A STAND-DOWN FIELD IS A SCALAR. reason, lift and by are human text and
    until is an instant; a container was never a legitimate value here, and
    admitting one bought nothing except the obligation to predict repr. For a
    string show interpolates it directly, so len IS the rendered width with no
    escaping in between. For an int the decimal size comes from bit_length.
    Everything else -- list, tuple, set, dict, and any object -- is refused by
    KIND rather than by size, which is the only refusal that cannot be
    defeated by a value the estimator was wrong about.

    The caller owes the composition: this answers for ONE value, and the
    contract sums the answers against the payload's shared budget."""
    if value is None:
        return 0, None
    if isinstance(value, bool):
        return len(str(value)), None
    if isinstance(value, int):
        import math
        digits = 1 if value == 0 else int(abs(value).bit_length()
                                          * math.log10(2)) + 1
        if digits > _STANDDOWN_MAX_DIGITS:
            return None, "%s is too large to render" % _describe_rejected(value)
        # THE DIGIT ESTIMATE MAY BE OFF BY ONE AND THAT IS SAFE HERE: it is
        # compared against a ceiling three orders of magnitude below the
        # interpreter's, and str() is only reached for values far under it.
        return len(str(value)), None
    if isinstance(value, float):
        return len(str(value)), None
    if isinstance(value, str):
        why = _standdown_encodable(value)
        if why:
            return None, why
        # THE UNIT IS CHARACTERS, AS SIGNED. len is what show occupies and what
        # the budget counts; the encoder's byte count is a separate ceiling
        # owned by the ledger, and conflating them would silently narrow the
        # bar to reject text that is valid under it.
        return len(value), None
    return None, ("%s is not a stand-down value: a stand-down field is text, "
                  "a number or an instant, never a container"
                  % _describe_rejected(value))


def _standdown_epoch(raw):
    """(epoch, None) or (None, why) for a stand-down deadline.

    THREE REFUSALS THAT ALL LOOKED LIKE NUMBERS. A bool is an int in Python, so
    `until=False` sailed through a numeric check and read as the epoch 0 —
    permanently EXPIRED, which quietly re-offers the row the field exists to
    withhold. `float("-inf")` did the same by comparing less than every clock
    reading. And `float("nan")` compares FALSE against everything, so it read
    as not-yet-expired forever while `time.gmtime` raised on it, crashing the
    one surface a human uses to find out why a row is unoffered.

    So the predicate is not "is it a number" but "is it an INSTANT": not a
    bool, finite, and REPRESENTABLE — proven by asking gmtime, the same call
    the renderer will make, rather than by guessing a range."""
    if isinstance(raw, bool):
        return None, "a boolean is not an instant"
    if not isinstance(raw, (int, float)):
        return None, "%s is not an epoch instant" % _describe_rejected(raw)
    # THE CONVERSION IS PART OF THE VALIDATION, NOT A STEP BEFORE IT. A Python
    # int is unbounded and JSON carries integers verbatim, so a stored
    # until of 10**400 is a value this predicate must be able to REFUSE --
    # and float() raises OverflowError on it. With the conversion outside the
    # boundary the writer crashed instead of refusing and the replay crashed
    # instead of reading UNREADABLE, which turns a hostile value into an
    # exception on whatever surface touched the row first.
    try:
        v = float(raw)
        if v != v:
            return None, "NaN is not an instant"
        if v in (float("inf"), float("-inf")):
            return None, "an infinite value is not an instant"
        time.gmtime(v)
    except (ValueError, OSError, OverflowError):
        return None, ("%s is not a representable instant"
                      % _describe_rejected(raw))
    return v, None


def standdown_contract(value):
    """(normalized_payload, error) — THE ONE VALIDITY PREDICATE, and BOTH the
    writer and the replay read it.

    THE ASYMMETRY THIS EXISTS TO KILL. The first cut validated at the write
    door and classified at the read door with a DIFFERENT, looser rule, so a
    shape the writer refused ({} and "") was read back as ABSENT — meaning a
    value that could never be written legitimately, if it ever reached the
    ledger by any other path, would silently mean "no stand-down" and re-open
    the offer. One predicate, two callers: whatever the writer will not accept,
    the reader treats as UNREADABLE and therefore BLOCKING.

    ONLY A MISSING KEY OR AN EXPLICIT None IS ABSENCE. `--clear` writes None,
    so that is the one spelling of "there is no stand-down here"; everything
    else that fails this contract is somebody's stand-down that did not
    survive."""
    if not isinstance(value, dict):
        return None, ("a stand-down is a mapping with a non-empty 'reason' "
                      "(got %s)" % _describe_rejected(value))
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None, ("a stand-down needs a reason: pass a mapping with a "
                      "non-empty 'reason', or None to CLEAR it")
    out = dict(value)
    out["reason"] = reason
    if value.get("until") is not None:
        epoch, why = _standdown_epoch(value["until"])
        if why:
            return None, "a stand-down 'until' is invalid: %s" % why
        out["until"] = epoch
    # AND NOW EVERY REMAINING FIELD, AGAINST ONE SHARED BUDGET, because the
    # renderer reads the whole mapping and not the two fields named above.
    # 'until' is exempt because it left this door as a float epoch that the
    # renderer formats through gmtime rather than str().
    #
    # THE KEY IS CHECKED FIRST AND IN FULL, AND THAT ORDERING IS THE POINT.
    # A refusal that interpolates the field it is refusing is unbounded unless
    # the field name was bounded FIRST -- so a 40000-character key must be
    # refused by a message that describes it rather than one that prints it,
    # or the diagnostic reproduces the defect it is reporting.
    spent = 0
    for key in out:
        if key == "until":
            continue
        if not isinstance(key, str) or not 0 < len(key) <= _STANDDOWN_MAX_KEY:
            return None, ("a stand-down field name must be text of at most %d "
                          "characters (got %s)"
                          % (_STANDDOWN_MAX_KEY, _describe_rejected(key)))
        # A FIELD NAME IS UNTRUSTED TEXT EXACTLY AS A VALUE IS, and it is the
        # one the diagnostics interpolate, so an unencodable key would make the
        # refusal itself unwritable on the surface reporting it.
        why = _standdown_encodable(key)
        if why:
            return None, "a stand-down field name is invalid: %s" % why
        width, why = _standdown_rendered_len(out[key])
        if why:
            # THE KEY IS SAFE TO NAME AND THE VALUE IS NOT: the key passed the
            # length bound above, and the value is described, never rendered.
            return None, "a stand-down '%s' is invalid: %s" % (key, why)
        spent += len(key) + width
        if spent > _STANDDOWN_MAX_RENDER:
            return None, ("a stand-down renders past its %d-character bound "
                          "at '%s'" % (_STANDDOWN_MAX_RENDER, key))
    return out, None


def parse_standdown_until(raw):
    """(epoch, None) or (None, error) for a lift DEADLINE. Accepts a duration
    ("90m", "4h", "2d", "1w") measured from now, or a "YYYY-MM-DD" date, which
    means the START of that UTC day — not its end. Reading a bare date as
    end-of-day silently buys a stand-down an extra 24 hours nobody typed.

    A DURATION IS RELATIVE TO THE MOMENT OF WRITING AND IS RESOLVED HERE, so
    what lands on the row is an absolute instant. Storing "4h" would make the
    row's meaning depend on when it is READ, and a stand-down whose expiry
    moves every time someone looks at it never expires."""
    txt = str(raw or "").strip()
    if not txt:
        return None, "an empty --until is not a deadline"
    m = re.fullmatch(r"(\d+)([mhdw])", txt)
    if m:
        # SAME BOUNDARY FOR THE ARITHMETIC. The digits are unbounded, so the
        # multiply and the add can produce a value float cannot hold or gmtime
        # cannot render; a deadline nobody can express is a refusal, never a
        # traceback out of a CLI verb.
        try:
            when = time.time() + int(m.group(1)) * _STANDDOWN_UNIT_S[m.group(2)]
            time.gmtime(when)
        except (ValueError, OSError, OverflowError):
            return None, ("%s is too far away to express as an instant"
                          % _describe_rejected(txt))
        return when, None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", txt)
    if m:
        # VALIDATE THE DATE BEFORE CONVERTING IT. calendar.timegm NORMALISES
        # rather than refuses: a 31st in a 30-day month rolls FORWARD into the
        # next month and a zero day rolls BACKWARD into the previous one, so a
        # typo does not fail — it moves the deadline to a day the operator did
        # not choose, in either direction. datetime.date is the door that
        # refuses.
        import calendar
        import datetime
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)),
                              int(m.group(3)))
        except ValueError:
            return None, ("%s is not a real calendar date"
                      % _describe_rejected(txt))
        try:
            return float(calendar.timegm(
                (d.year, d.month, d.day, 0, 0, 0, 0, 1, 0))), None
        except (ValueError, OverflowError):
            return None, "%s is out of range" % _describe_rejected(txt)
    return None, ("%s is neither a duration (90m, 4h, 2d, 1w) nor a "
                  "YYYY-MM-DD date" % _describe_rejected(txt))


def standdown_of(row, now=None):
    """(state, detail) for one row's stand-down.

    state is None when the row carries none, else LIVE, EXPIRED or UNREADABLE.
    `detail` is the stored mapping for LIVE/EXPIRED and the raw value for
    UNREADABLE, so a caller can always SHOW what it refused to interpret.

    UNREADABLE IS NOT ABSENT, AND THAT ASYMMETRY IS THE WHOLE SAFETY ARGUMENT.
    A value that will not parse means somebody wrote a stand-down and it did
    not survive; reading that as "no stand-down" resumes the exact leak this
    field exists to stop, silently, and looks identical to a clean row. So a
    malformed stand-down BLOCKS the offer and says so on the surface — the
    cost of failing closed is one row a human sees in `list` and clears by
    hand, and the cost of failing open is the six-seat re-offer loop measured
    on task/213."""
    raw = row.get("standdown")
    # ABSENCE IS EXACTLY ONE SHAPE: the key is missing, or it holds None (what
    # `--clear` writes). Everything else goes through the WRITER'S OWN
    # contract, so the reader can never be more permissive than the door.
    if raw is None:
        return None, None
    payload, err = standdown_contract(raw)
    if err:
        return STANDDOWN_UNREADABLE, raw
    until = payload.get("until")
    if until is not None and until <= (time.time() if now is None else now):
        return STANDDOWN_EXPIRED, payload
    return STANDDOWN_LIVE, payload


def standdown_blocks_offer(row, now=None):
    """May the work-offer rung hand this row to an idle seat?

    LIVE and UNREADABLE block; EXPIRED and absent do not. An EXPIRED
    stand-down deliberately lifts ITSELF rather than waiting to be cleared:
    without that, every time-bounded stand-down ("resume after the reboot",
    "overnight only") becomes permanent through nobody remembering, which is
    a staleness bug traded for a salience one."""
    state, _ = standdown_of(row, now=now)
    return state in (STANDDOWN_LIVE, STANDDOWN_UNREADABLE)

# 263 | #263 | task/263 | task-263 -> the canonical task/263. Anchored, because
# a substring match would read the "281" inside a sha and answer confidently.
_NUM_ID = re.compile(r"^\s*(?:task[/-])?#?(\d{1,6})\s*$", re.IGNORECASE)
# A free slug id for work that never had a number: task/work-tab-backlog.
# It REFUSES an all-digit slug: a SEVEN-DIGIT one would be filable through this
# pattern (seven digits exceed _NUM_ID's {1,6}) and then permanently
# unresolvable by its own number, which is the one thing this id scheme exists
# to prevent. Spelled in words rather than as a literal on purpose — a bare
# seven-digit run is a citation shape, and the docref rung is right to ask what
# it points at.
_SLUG_ID = re.compile(r"^\s*task[/-](?!\d+\s*$)([a-z0-9][a-z0-9-]{1,63})\s*$",
                      re.IGNORECASE)
# Prose scanning is DELIBERATELY STRICTER THAN `resolve`. A human typing
# `helm task resolve '#263'` has declared what they mean; a chat row saying
# "closes #130" has not — and this repo's own commit subjects cite GitHub
# issues that way (#130, #117). Matching bare #NNN in prose would silently
# turn every issue reference into a task citation, so cited_in requires the
# explicit task/ or task- form and leaves the ambiguous case alone.
_CITED = re.compile(r"\btask[/-](\d{1,6})\b", re.IGNORECASE)


def ledger_path(name="tasks.jsonl"):
    return os.path.join(home.global_dir(), name)


def current_project():
    """The project the caller's cwd derives to, or None — the STORE'S OWN lens.

    ONE derivation for project identity, and it is not this module's:
    helm/inject/_ledger.py:project_for_cwd — registry longest-prefix over
    registered paths plus the lane-worktree convention — is the exact function
    `helm store` scopes with ("scoping to project '%s' (from cwd)"). A second
    cwd→project mechanism here would let the task axis and the store axis
    answer DIFFERENTLY about one directory, which is the bug a project axis
    exists to end (task/974: another project's row rendering inside helm's own
    pipeline count). Fail-open like the store: any trouble reads as "no
    project claims this cwd", never a crash."""
    try:
        from .inject._ledger import project_for_cwd
        return project_for_cwd(os.getcwd())
    except Exception:
        return None


PROJECT_FLAG = "--project"
"""The scope override, named ONCE. Every door below reads this constant
rather than the literal, so the flag cannot be spelled two ways."""


def registered_projects():
    """(sorted names, err) — every project name the registry knows.

    The NAME is what a row's `project` field carries and what
    project_for_cwd returns (rec["name"] or the key), so the validation set
    and the cwd lens read the same namespace. NOT fail-open: current_project
    may answer None on a bad registry because "no project claims this cwd" is
    a legitimate answer, but "is `helm` a real project" has no safe default —
    answering yes on an unreadable registry would file a row into a scope
    nobody registered, which is the whole thing the flag exists to make
    deliberate."""
    from . import registry
    try:
        # STRICT, BECAUSE THIS IS AN AUTHORITY QUESTION AND NOT A LISTING.
        # The ordinary loader turns a malformed registry.json into the empty
        # DEFAULT, so `known` came back as [] with no error and every name was
        # reported NOT REGISTERED — an unreadable authority silently rendered
        # as an empty set that admits nothing, with a remedy ("drop the flag
        # to scope by cwd") pointing at a lens the same broken file cannot
        # answer either. The strict snapshot RAISES instead, which is what
        # turns this into the refusal below. It also does not take the write
        # lock and never migrates authored fields: reading to check membership
        # must not rewrite the thing being checked.
        projects = (registry.load(strict=True) or {}).get("projects") or {}
        return sorted({str(rec.get("name") or key)
                       for key, rec in projects.items()}), None
    except Exception as exc:                      # unreadable registry
        return None, "%s: %s" % (exc.__class__.__name__, exc)


def resolve_scope(explicit, door):
    """(project, decided_by, err) — THE FLAG WINS OVER cwd, at one door.

    task/2446, measured: `helm task add` from a project
    checkout scoped the row to that project because cwd was the only input, so a
    task ABOUT HELM filed from the team that hit the bug landed where the
    making team's `helm task list` does not look. A team USING helm must
    never need to stand in helm's checkout to report something about helm.

    `decided_by` is "flag" or "cwd" and it is RETURNED rather than inferred,
    because the caller has to SAY which one decided: a scope line that reads
    the same for both leaves an operator unable to tell an override from a
    coincidence. None project + "cwd" = no project claims this directory,
    the store's own global-only read.

    An unregistered name is REFUSED and the refusal names the registry: the
    alternative is a row stamped with a scope no listing will ever select
    for, which is invisible in exactly the direction 2446 is about."""
    if explicit is None:
        return current_project(), "cwd", None
    name = str(explicit).strip()
    if not name:
        return None, None, ("%s: %s needs a registered project name — an "
                            "empty scope is not 'the cwd one' and not "
                            "'every project'." % (door, PROJECT_FLAG))
    known, kerr = registered_projects()
    if known is None:
        return None, None, ("%s: the project registry (%s) is UNREADABLE "
                            "(%s), so %s cannot be checked — nothing was "
                            "done. Fix the registry rather than trusting an "
                            "unchecked scope."
                            % (door, home.registry_path(), kerr, PROJECT_FLAG))
    if name not in known:
        return None, None, ("%s: %s %r is not a registered project — the "
                            "registry (%s) knows: %s. `helm projects` lists "
                            "them; drop the flag to scope by cwd."
                            % (door, PROJECT_FLAG, name,
                               home.registry_path(),
                               ", ".join(known) or "(none)"))
    return name, "flag", None


def scope_line(door, project, decided_by, tail=""):
    """The ONE sentence a scoped door prints: WHICH scope and HOW it decided.

    Said the way `helm store` says it so a surprising scope is visible at the
    moment of the write and not a fact discovered later in a listing."""
    how = ("%s, which wins over cwd" % PROJECT_FLAG) if decided_by == "flag" \
        else "cwd; pass %s=NAME to override" % PROJECT_FLAG
    return "%s: scoping to project '%s' (from %s%s)" % (
        door, project, how, tail)


def take_project_flag(rest, door):
    """(value, err) — consume the scope flag, PRESENCE READ BEFORE THE TAKE.

    `_take` DELETES a valueless flag and answers None, which is byte-identical
    to never passing it — so a presence test placed AFTER it can never fire:
    `task list --project` and `task triage --apply --project` both consumed the
    flag, fell through to CWD and reported success, scoping to exactly the
    project the operator had just tried to override. A trailing bare flag and
    `--project --json` leave nothing behind for an `in rest` to find, which is
    the same shape that made a bare `--ref` vanish one verb over.

    So the question is asked first, the order `list`'s own `--owner` leg and
    `triage`'s `--limit` leg already keep. `add` needs none of this: it
    snapshots what was typed for every valued flag and refuses the whole set
    in one sentence."""
    typed = any(t == PROJECT_FLAG or t.startswith(PROJECT_FLAG + "=")
                for t in rest)
    value = _take(rest, PROJECT_FLAG)
    if typed and value is None:
        return None, missing_value_error(door, PROJECT_FLAG, None)
    return value, None


def missing_value_error(door, flag, nxt):
    """The ONE sentence a valued task option prints when its value is not
    beside it — flag-specific about WHAT it wanted, identical about WHY.

    It lives here because two doors ask the question (`take_project_flag`,
    which reads presence before the take, and `option_adjacency_error`, which
    reads the original argv before any take) and a second copy of the sentence
    is how one of them ends up saying "another flag or nothing at all" about
    an argv it can no longer see."""
    beside = "nothing at all" if nxt is None else "another flag (%s)" % nxt
    want = {PROJECT_FLAG: "a registered project name",
            "--owner": "a seat name",
            "--limit": "a WHOLE NUMBER of rows"}.get(flag, "a value")
    return ("%s: %s needs %s and got %s — a valued flag takes the ONE token "
            "beside it in what you typed and never a later one reached across "
            "another flag, so nothing was read, selected or written. Write "
            "%s=VALUE when the value itself starts with a dash."
            % (door, flag, want, beside, flag))


def option_adjacency_error(argv, flags, door):
    """err or None — ONE left-to-right scan of the ORIGINAL argv, BEFORE any
    removal: a valued SPACE-form option takes ONLY its immediately adjacent
    token.

    MEASURED (task/2446): each leg below consumed its
    options in whatever order the code happened to read them, and EVERY
    removal closed a gap in `rest` that had separated a valued flag from a
    later positional. `triage --project --apply fixproj` removed `--apply`
    first, so `--project` then found `fixproj` standing beside it and a
    command whose scope flag was followed by another flag RANKED — under
    `--apply`, wrote — the backlog of a project the operator never adjoined to
    it. `triage --project --limit 1 fixproj --apply` crossed a whole valued
    option the same way, and `list --owner --project fixproj seat-a` invented
    `seat-a` as the owner filter because the scope take had closed the gap
    between `--owner` and it.

    A PRESENCE TEST CANNOT SEE THIS. `take_project_flag` asks what was typed
    before it takes, which is what stops a VALUELESS flag defaulting to cwd —
    but by then the argv already reads as if the operator had typed the
    adjacency, so the flag has a value and there is nothing to refuse. The
    only argv where "immediately beside" still means what was typed is the one
    nothing has been removed from, so that is the argv this reads, once, for
    every valued option the verb takes.

    The `=` form is self-contained and always accepted — it is the spelling
    for a value that itself starts with a dash. Tokens after a literal `--`
    belong to the text and are never inspected, the boundary
    `project_flag_not_applicable` already keeps."""
    head = argv[:argv.index("--")] if "--" in argv else list(argv)
    i = 0
    while i < len(head):
        tok = head[i]
        if tok in flags:
            nxt = head[i + 1] if len(head) > i + 1 else None
            if nxt is None or nxt.startswith("-"):
                return missing_value_error(door, tok, nxt)
            i += 2                      # the value is this option's, not a
            continue                    # token the next option may claim
        i += 1
    return None


def project_flag_not_applicable(door, rest):
    """rc 2 if this id-addressed verb was handed the scope flag, else None.

    MEASURED (task/2446): `update`, `close` and `show` never read cwd — they
    resolve a FLEET-WIDE id, which is the property that makes a cited '#263'
    mean one row everywhere — so there is no scope for a flag to win over.
    Accepting it here would be a silent no-op on a flag whose whole promise
    is choosing a scope; refusing as "unrecognised" would read as "helm has
    no such flag" to someone who just used it on `add`. So it refuses and
    says which of the two it is. Tokens after a literal `--` belong to the
    text and are never inspected."""
    head = rest[:rest.index("--")] if "--" in rest else list(rest)
    if not any(t == PROJECT_FLAG or t.startswith(PROJECT_FLAG + "=")
               for t in head):
        return None
    print("%s: %s is not read here and nothing was done — this verb takes a "
          "FLEET-WIDE id, so it needs no scope: any row resolves from any "
          "directory. %s scopes `add`, `list` and `triage`, the verbs where "
          "cwd would otherwise decide. (Moving a filed row between projects "
          "is not this door either.)" % (door, PROJECT_FLAG, PROJECT_FLAG),
          file=sys.stderr)
    return 2

def project_of_row(row):
    """The row's project scope: a name, or None = the UNSCOPED bucket.

    UNSCOPED IS DISCLOSED, NEVER GUESSED. Rows written before the field
    existed carry no project, and this ledger is append-only history —
    retro-stamping would assert a scope nobody witnessed at filing time, the
    same erasure origin_of refuses one field over. (A mirrored row's
    `project:<cwd-slug>` ref is deliberately NOT read here: that value is a
    cwd-slug, a DIFFERENT NAMESPACE than registry project names, and treating
    one as the other quietly builds the second identity mechanism. The bridge,
    if ever wanted, is design work, not a read-time coercion.)"""
    value = str(row.get("project") or "").strip()
    return value or None


SKIPPED = object()
"""update()'s THIRD ANSWER: the row was read and deliberately not written.

A skip is not a failure and it is not a write, and collapsing it into either
is how a sweep reports a lie. `(None, err)` means something went wrong;
`(row, None)` means a row was appended; `(SKIPPED, reason)` means the writer
held the lock, read the current row, and the caller's RULE declined it. Only a
`rank_rule` caller can receive one, so no existing `row, err = update(...)`
reader can mistake a skip for success: they never pass a rule.

`expect` is the other rule (task/1738): a caller that judged a row from a
snapshot passes that row, and the writer skips when the row it holds under
the lock is no longer the one judged. The same reader contract holds — only
a caller that passes `expect` can receive a skip."""


class RankRule(object):
    """THE SWEEP'S INTENT, RESOLVED UNDER THE WRITER'S LOCK.

    A CALLER THAT DECIDES OUTSIDE THE LOCK DECIDES ABOUT A ROW THAT MAY NO
    LONGER EXIST, AND THAT IS MEASURED RATHER THAN FEARED: a sweep selected
    an unranked row from a snapshot, a seat wrote a deliberate P0 between the
    snapshot and the write, and the sweep's P2
    landed on top of it — the ledger keeps all three events and the row's
    final rank is the stale one. Re-reading just before the call does not fix
    it (the window shrinks, it does not close) and a second lock around
    `update` either races the same way or deadlocks against the one inside.

    So the caller hands the writer an INTENT instead of a VALUE. This object
    carries the rule; `decide` runs against the row the writer already holds
    the lock on, and its refusals are the eligibility the preview could only
    guess at. A rule that declines writes NOTHING and says why.

    `scope` IS REQUIRED AND MAY NOT BE None. An unresolved project is not a
    scope that matches unscoped rows — it is the absence of the answer this
    rule needs, and treating the two as one made the legacy bucket both
    excluded and included at once."""

    __slots__ = ("scope", "include_legacy")

    def __init__(self, scope, include_legacy=False):
        if not (scope or "").strip():
            raise ValueError(
                "RankRule needs a resolved project scope — an unknown scope "
                "cannot decide which rows are in it")
        self.scope = scope.strip()
        self.include_legacy = bool(include_legacy)

    def covers(self, row):
        """Is this row in the population the rule ranks? -> reason or None."""
        project = project_of_row(row)
        if project == self.scope:
            return None
        if project is None and self.include_legacy:
            return None
        return ("is scoped to %s, not %s"
                % (project or "the UNSCOPED legacy bucket", self.scope))

    def decide(self, row):
        """(priority, skip-reason) — exactly one of the two is None.

        THE THREE REFUSALS ARE THE THREE WAYS THE PREVIEW CAN GO STALE
        between its read and this write: somebody ranked the row, somebody
        closed it, or somebody moved it out of scope. Each is reported by
        name, because "42 skipped" tells an operator nothing about whether
        the sweep did its job."""
        if row.get("priority") in PRIORITIES:
            return None, "already ranked %s" % row.get("priority")
        if row.get("status") not in OPEN_STATUSES:
            return None, "is %s, not open work" % (row.get("status") or "?")
        out_of_scope = self.covers(row)
        if out_of_scope:
            return None, out_of_scope
        # THE RULE'S FIRST CLAUSE IS NOT DERIVABLE AND IS NOT WRITTEN. A
        # fleet blocker is P0 because of what it BLOCKS, and no field here
        # records that; ranking by a rule nobody could check is the free-text
        # laundering the rank field exists to refuse.
        return ("P1" if origin_of(row) == "owner" else "P2"), None


def _admit(act):
    """(AdmittedActor, None) or (None, message) — THIS MODULE'S ONE DOOR to
    the admission pass, so no leg has to remember the import or the tuple.

    `actors.resolve_actor` is the single admission pass and its refusals are
    the four this module wants verbatim: a hostile declared name, a session
    rostered to a different seat, a name minted from nothing, and an assertion
    that named somebody else. Wording them again here would be a second
    identity mechanism answering differently about one process."""
    try:
        from . import actors as _actors
    except Exception as e:              # noqa: BLE001 — no seam, no admission
        return None, ("the actor admission seam is unavailable (%s) — who is "
                      "acting is UNKNOWN, and UNKNOWN is not permission"
                      % type(e).__name__)
    # THE SESSION AND THE CWD ARE PASSED, NOT DEFAULTED, and this door was
    # measured broken without them. `_resolve` compares the DECLARED name
    # against the roster's map for the session it is HANDED and populates no
    # default, so omitting the argument leaves that half silent and every
    # legitimate seat is refused as UNCORROBORATED — "this identity is
    # DECLARED and nothing corroborates it, no session was presented". A
    # forgotten argument that reads as a policy.
    actor, err = _actors.resolve_actor(home.session_id(), os.getcwd(), act=act)
    return (None, err) if err else (actor, None)


def rank_actor_error(actor):
    """None when `actor` is a MINTED admission, else why it is not one.

    THE TYPE IS THE CAPABILITY. `actors.AdmittedActor` cannot be constructed
    outside `actors.resolve_actor`, so an isinstance check here IS the proof
    that the admission pass ran — a seat name string carries no such proof and
    is exactly what this refuses. Imported lazily because `actors` reaches the
    roster and the identity seam, which reach back; every other cross-module
    door in this file defers for the same reason.

    AN UNIMPORTABLE `actors` REFUSES RATHER THAN WAVES THROUGH, and says so
    by name: a missing admission seam makes the actor UNKNOWN, and UNKNOWN is
    not permission."""
    try:
        from . import actors as _actors
    except Exception as e:              # noqa: BLE001 — no seam, no admission
        return ("the actor admission seam is unavailable (%s), so who is "
                "acting is UNKNOWN" % type(e).__name__)
    if isinstance(actor, _actors.AdmittedActor):
        return None
    if actor is None:
        return "no actor was resolved"
    return ("%s is not an admitted actor — a name is not an admission"
            % type(actor).__name__)


def normalize_id(token):
    """Every spelling of one id -> the canonical `task/<n>`, or None.

    None means UNPARSEABLE, never "absent" — callers must not turn a bad
    spelling into a missing row, because those want opposite answers.
    """
    # An int is the obvious thing a migration script passes, and refusing it
    # produced an error that told the caller to pass exactly what they had just
    # passed ("want a number (263)" when given 263). Accept it; anything else
    # non-string is still unparseable.
    if isinstance(token, int) and not isinstance(token, bool):
        token = str(token)
    if not isinstance(token, str):
        return None
    m = _NUM_ID.match(token)
    if m:
        return "task/%d" % int(m.group(1))
    m = _SLUG_ID.match(token)
    if m:
        return "task/%s" % m.group(1).lower()
    return None


def snapshot(path=None, strict=False, accept=None):
    """(rows-by-id, unavailable). Missing is known-empty; unreadable storage is
    UNKNOWN and must surface as such rather than as an empty backlog.

    ``accept(row, prior)`` rides through to the fold unchanged: it sees every
    event in ledger order beside the row it replaces, which is how a reader
    that needs the HISTORY — when a row closed, not only that it is closed —
    gets it from this one read instead of walking the ledger a second time.

    ``strict=True`` extends UNKNOWN to a corrupt COMPLETE row: the
    default read SKIPS malformed lines to keep listings alive over one bad
    byte, which is right for a projection and a lie for a decision — a
    corrupt ledger came back ({}, None) and the todos sweep filed every
    stamped row as ORPHANED ("the ledger moved on") over a ledger nobody
    could read. Legs that file, refuse, or classify on the answer pass
    strict; read-only surfaces keep the tolerant default."""
    return eventledger.latest_checked(path or ledger_path(), accept=accept,
                                      strict=strict)


def rows(path=None):
    return snapshot(path)[0]


def get(token, path=None):
    """One row by any spelling of its id -> row or None."""
    tid = normalize_id(token)
    if not tid:
        return None
    return rows(path).get(tid)


def open_rows(path=None):
    """Live work only — tombstones and closed rows are not backlog."""
    out = [r for r in rows(path).values() if r.get("status") in OPEN_STATUSES]
    return sorted(out, key=sort_key)


def sort_key(row):
    """THE ORDERING CONTRACT, and it is PUBLIC because a consumer needed it.

    Numbered rows in NUMERIC order (task/45 before task/263 — never lexical,
    where "263" < "45"), slugged rows after them alphabetically. A list then
    reads like the numbering the fleet already has in its head.

    IT IS PUBLIC BECAUSE helm/web.py REACHED FOR THE UNDERSCORE. I published a
    row schema in chat and left ordering implicit, so the surface author took
    the only thing available and bound a cross-module dependency to a private
    name. That is the producer's failure, not the consumer's: a module that
    does not say what it exports gets read for what it has."""
    tid = str(row.get("id", ""))
    m = _NUM_ID.match(tid)
    return (0, int(m.group(1)), "") if m else (1, 0, tid)


def _next_number(existing):
    """The next integer after the highest the ledger holds. Continues the
    fleet's one sequence rather than forking a second one beside it."""
    top = 0
    for tid in existing:
        m = _NUM_ID.match(str(tid))
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def _typed_field_error(name, value):
    """`continues` and `priority` are a row id and a label: str, or absent.

    THE API IS A DOOR AND ARGV CANNOT PROVE IT SHUT. The CLI can only ever
    hand these fields strings, so every arm driving them through argv is
    silent about a caller passing a list, a dict or a bool — and helm calls
    `add`/`update` directly from more places than it calls the CLI. Two
    opposite failures live behind that gap and neither is hypothetical:

      TRUTHY non-str CRASHES. `_story_error` asks `parent not in known`
      against a dict, so an unhashable argument raises TypeError out of a
      validator whose entire job is returning refusals.

      FALSY non-str FILES SILENTLY. `continues or None` maps ``0``, ``False``
      and ``[]`` onto None, storing "no parent" — an answer the caller never
      gave, indistinguishable afterwards from a row that genuinely continues
      nothing. That is absence and cannot-look sharing one representation,
      inside the field built to end exactly that.

    `type(...) is not str` rather than isinstance, matching this file's bool
    discipline: a str subclass carrying its own __eq__ is not a ledger id.
    """
    if value is None or type(value) is str:
        return None
    return ("%s must be a string or omitted, got %s — a non-string either "
            "crashes the ledger walk or coerces to None and files silently "
            "as no value" % (name, type(value).__name__))


def _story_chain(known, start):
    """(path, root, ring_at) — THE ONE WALK over `continues`.

    TWO WALKS OVER ONE GRAPH IS HOW A WRITER AND A READER COME TO DISAGREE
    ABOUT A RING. This file previously carried two: the write-time validator
    accumulated a seen-set and refused, while the read-time root-finder
    accumulated a different seen-set and returned whatever node it happened to
    stop on. They shared no code, so a hazard fixed in one was still live in
    the other, and neither could be made defensive without re-deriving the
    other's edge cases. Every caller now walks HERE.

    THE THREE THINGS A CALLER CAN NEED, returned together because computing
    them separately is what let them drift:
      path     — ids visited, in order, so a report can print the chain and
                 print the SAME chain twice. An earlier cut rendered a SET,
                 and one defect printed a different arrow-chain on every run.
      root     — the last id reached whose own parent is absent or
                 unresolvable. For a row that continues nothing this is the
                 row itself.
      ring_at  — the id revisited, when the chain closes on itself. None
                 otherwise. A ring has no root, so `root` is None exactly when
                 this is set: the two are alternatives and never both.
      dangling_at — the id a readable row NAMES as its parent and which the
                 ledger does not hold. THREE ANSWERS, NOT TWO: a chain ending
                 because a row continues nothing is ROOTED; one ending because
                 the next id cannot be read is DANGLING; folding them together
                 rebuilds the exact collapse this field exists to end, one
                 level down, in the walk every caller shares. `path` and
                 `root` therefore carry only ids the ledger actually holds —
                 naming an unreadable row as a story's root would group live
                 rows under something no reader can display.

    DEFENSIVE BY CONSTRUCTION, because this reads rows written by older helms
    that never had the field and by callers that are not the CLI. A non-string
    link ENDS the walk rather than reaching a dict lookup — `cur in seen` and
    `known.get(cur)` both raise TypeError on an unhashable argument, and a
    walker that raises is a walker that cannot report. Absence, an unreadable
    row, and a ring are three different answers and none of them is an
    exception."""
    path, seen, cur = [], set(), (start if type(start) is str else None)
    while cur:
        if cur in seen:
            return path, None, cur, None
        row = known.get(cur)
        if not isinstance(row, dict):
            # UNREADABLE, and we only arrive here from a link that NAMED it.
            # Not appended: path and root hold readable ids only.
            return path, (path[-1] if path else None), None, cur
        seen.add(cur)
        path.append(cur)
        nxt = row.get("continues")
        if type(nxt) is not str or not nxt:
            return path, cur, None, None
        cur = nxt
    return path, None, None, None


def _story_error(known, tid, parent):
    """Why `tid` may NOT declare it continues `parent`, or None if it may.

    THIS FIELD EXISTS BECAUSE OF A MEASUREMENT: 83 of 204 open rows — forty
    percent — already named another task inside their own title, note or refs.
    They are continuations, residuals and second halves, and the ledger stored
    that relationship as PROSE, so nothing could act on it. A board that
    cannot express "this is part of that" reports every sequenced half as an
    independent failure, and a count overstating by forty percent reads as
    accumulating debt when much of it is one story mid-flight.

    FOUR REFUSALS, each a different fact and each named separately, because a
    caller that cannot tell them apart cannot fix the right one:
      SELF      — a row cannot continue itself.
      UNKNOWN   — the parent is not in the ledger. Refused rather than stored
                  hopefully: a story rooted at a row nobody can read is worse
                  than no story, and a dangling pointer is re-discovered by
                  every later reader.
      CYCLE     — THIS write closes a ring. The write is the cause.
      PRIOR RING — the chain ABOVE the parent was already ringed before this
                  field refused rings at all. This write is innocent, and
                  answering it with CYCLE sends the reader to repair a row
                  that is fine while the ringed pair stays broken.
    The last two used to share one sentence, which made the second one false."""
    bad = _typed_field_error("continues", parent)
    if bad:
        return bad
    if not parent:
        return None
    if parent == tid:
        return "a row cannot continue ITSELF (%s)" % tid
    if parent not in known:
        return ("continues %s, which is not in the ledger — a story rooted at "
                "a row nobody can read is worse than no story" % parent)
    path, _root, ring_at, dangling_at = _story_chain(known, parent)
    if tid in path:
        return ("continues %s would form a CYCLE (%s) — following continues "
                "must terminate or a story has no root"
                % (parent, " -> ".join([tid] + path[:path.index(tid) + 1])))
    if ring_at:
        return ("continues %s cannot be verified: the chain above %s ALREADY "
                "contains a ring at %s, written before this field refused "
                "them. That ring is not caused by this write — repair %s, not "
                "this row" % (parent, parent, ring_at, ring_at))
    if dangling_at:
        # THE SAME LAW AS PRIOR RING, for the third answer. UNKNOWN refuses a
        # parent the ledger does not hold; accepting a parent whose ANCESTOR
        # is the missing row files this write under a story no reader can
        # root — the identical dangling pointer, one hop removed, admitted by
        # the validator whose UNKNOWN clause explains why that is worse than
        # no story.
        return ("continues %s cannot be verified: the chain above %s DANGLES "
                "at %s — a row names it as parent and the ledger does not "
                "hold it. This write is innocent, but it would root a story "
                "at a row nobody can read — repair %s, not this row"
                % (parent, parent, dangling_at, dangling_at))
    return None


def _story_root(known, tid):
    """The id at the head of `tid`'s story — itself when it continues nothing.

    A ring has no root and this returns the row itself rather than inventing
    one: the ledger holds rows written before rings were refused, and a reader
    that answers arbitrarily is worse than one that answers locally. Callers
    that must DISTINGUISH a rooted chain from a ringed one read `ring_at` off
    `_story_chain` directly."""
    _path, root, _ring, _dangling = _story_chain(known, str(tid))
    return root or str(tid)


def _reported_session():
    """The session id THIS PROCESS DECLARES, or None when it declares none.

    AN UNAUTHENTICATED SELF-DECLARATION, AND THE NAME SAYS SO. `session_id()`
    reads raw environment, so whatever the caller exported is what gets
    recorded. A reviewer measured it end to end: they exported a forged value,
    this function returned that exact string, `_row` stored it and
    `public_row` published it unchanged. So it is the same class of fact as a
    User-Agent header — reported by the subject, useful for CORRELATING rows
    that claim one session, and never evidence of who filed anything.

    THE DOCSTRING THIS REPLACED CLAIMED THE OPPOSITE AND THAT WAS THE DEFECT.
    It said "NEVER FABRICATES — a stamp that falls back to a guess would let a
    row claim an authorship nobody can check". The first clause is true of the
    code (an unreadable environment yields None, never a guess) and the second
    implies what the first cannot buy: not guessing does not make the value
    CHECKABLE, because the honest path and the forged path are the same read.
    Three reviewers in a row read the surrounding prose as an attribution
    claim, which is the measurement that mattered — if the words mislead every
    reader who checks, the words are wrong, not the readers.

    IT IS NEVER AN AUTHORIZATION INPUT, and that is the one property that is
    actually load-bearing. Three review rounds died establishing it; the third
    killed the idea rather than a version of it. See the note at the call site.

    AND IT IS NO LONGER ON THE WIRE, which is a different property from the
    one above and was not implied by it. The value is useless as EVIDENCE
    about its own filer, and it is still a working BEARER for somebody else's
    check: `actors.resolve_actor_reason` corroborates a declared seat name by
    asking the roster whether the acting session resolves, and the acting
    session is read from the same environment this function reads. Publishing
    it handed that bearer to any reader. `public_row` drops it; the store
    keeps it. See the note there.
    """
    try:
        from .home import session_id
        return (session_id() or "").strip() or None
    except Exception:      # noqa: BLE001 — no session seam, no evidence
        return None


def _row(tid, title, status, owner, note, refs, source, origin, closed_reason,
         project=None, posture_na=None, continues=None, priority=None,
         reported_session=None):
    now = time.time()
    # STORAGE DERIVES FROM THE SCHEMA like the doors do: present and explicit
    # from birth for every STORY_FIELDS key (`continues` None = a story of
    # its own; `priority` None = UNRANKED, not low — see PRIORITIES), so a
    # reader never guesses whether a field predates the feature, and a third
    # schema key cannot be stored by a hand-list this literal forgot. The []
    # lookup is the same drift alarm as add()'s: a key added to STORY_FIELDS
    # without threading through this signature raises KeyError here, loudly.
    _story = {"continues": continues, "priority": priority}
    row = {
        "id": tid,
        "ts": now,
        "last_updated": now,
        **{_k: (_story[_k] or None) for _k in STORY_KEYS},
        "title": title,
        "status": status,
        "owner": owner or None,
        "note": note or None,
        "refs": list(refs or ()),
        "source": source or None,
        "origin": origin or None,
        "closed_reason": closed_reason or None,
        # WHOSE PROJECT'S WORK THIS IS (task/974). Stamped at filing time from
        # the filer's derived or declared project; None = UNSCOPED, forever —
        # update() does not list it in `allowed`, so no later edit can
        # retro-scope history (deliberate: migration is design work).
        "project": project or None,
        # THE SESSION THIS ROW'S FILER CLAIMED, AS SOMETHING THAT SURVIVES A
        # RENAME (task/1898). A CLAIM, NOT AN ATTRIBUTION — it is read from
        # the environment, so it identifies nobody; see `_reported_session`.
        # STORED HERE AND DROPPED BY `public_row`: it is worthless as evidence
        # about its own filer and it is a working bearer for the roster
        # corroboration check, so the ledger keeps it and the wire does
        # not. It is recorded because correlating rows that report one
        # session still helps a renamed seat find its own
        # work, and because nothing else on the row survives a rename at all.
        # `owner` and `source` are SEAT-NAME STRINGS, and a seat name is not a
        # stable identity: the 2026-09-09 reboot re-rostered claude sessions by
        # SLOT, so the session a row REPORTS can answer to a different name
        # today while its old name belongs to a DIFFERENT LIVE SEAT.
        # The rows were stranded — `--mine` showed their author nothing, and
        # no verb could return them, because the row recorded no fact any
        # claimant could be checked against. Absent evidence, not a missing
        # feature.
        #
        # Stamped at filing time like `project`, and like `project` it is NOT
        # in update()'s `allowed` list. That is NOT because it proves
        # authorship — it proves nothing, being read from the environment —
        # but because a claim that could be edited later would be worth even
        # less than one fixed at birth, and because a mutable field here is
        # exactly what a reclaim door would need.
        #
        # None on every pre-existing row, and None means UNKNOWN — never "not
        # yours". A row reporting no session falls back to the ordinary
        # takeover contract rather than being refused harder than before.
        "reported_session": reported_session or None,
        # Present and empty from birth. The published schema promises this key,
        # and a surface that reads row["comments"] must not have to know
        # whether anyone has commented yet.
        "comments": [],
    }
    if str(posture_na or "").strip():
        # THE RECORDED ESCAPE (helm/posture.py): the filer's reason the three
        # seam questions do not apply. Present only when given — a row with
        # no seam strategy has nothing to record, and the published row shape
        # stays what it was for every such row.
        row["posture_na"] = str(posture_na).strip()
    return row


def add(title, owner, note=None, refs=None, source=None, tid=None,
        status="open", origin=None, closed_reason=None, path=None,
        project=None, posture_na=None, continues=None, priority=None,
        force_new=False):
    """File one task -> (row, error). Exactly one of the pair is None.

    `owner` is REQUIRED for live work and refused when blank: a backlog nobody
    is accountable to is a list, not a ledger. A tombstone (status=closed) is
    the deliberate exception — it records history, it is not work.

    `tid` is optional and is how a MIGRATED item keeps its original number; the
    id is normalized, so the caller may pass 263, #263 or task/263.

    `project` is EXPLICIT here, derived only at the CLI door: this function is
    called by the mirror and other API callers whose cwd says nothing about
    the work's project — deriving from cwd HERE would stamp the sweep-runner's
    project onto every foreign row it carries. Absent stays absent (UNSCOPED),
    the same law origin follows one field up.
    """
    # A NON-STRING TITLE REFUSES, IT DOES NOT RAISE. `(title or "").strip()`
    # was the first statement in this function, so an int, a list or a dict —
    # anything a caller built wrong, or read out of a payload — put an
    # AttributeError traceback on the filer instead of a sentence. THE TYPE
    # CHECK COMES FIRST for the reason the priority validator's own note
    # gives one door up: the value checks below cannot survive a non-string,
    # and a validator that crashes instead of refusing has validated nothing.
    #
    # `str(title)` IS DELIBERATELY NOT THE CURE. Coercing would file a row
    # titled "[1, 2]" or "<object object at 0x...>" and call it the operator's
    # words — a title nobody wrote, in a ledger whose whole job is to record
    # what somebody asked for.
    if title is not None and not isinstance(title, str):
        return None, ("a task title must be text, not %s — this door files "
                      "what somebody wrote and will not coerce a %s into a "
                      "sentence" % (type(title).__name__, type(title).__name__))
    title = (title or "").strip()
    if not title:
        return None, "a task needs a title"
    if status not in STATUSES:
        return None, "unknown status %r (want %s)" % (status, "|".join(STATUSES))
    # SAME SHAPE AS THE STATUS CHECK ABOVE, deliberately: one validation idiom
    # for one file. Absent stays absent — a row filed without a declared origin
    # reads UNKNOWN rather than being defaulted into a provenance nobody stated.
    if origin is not None and origin not in ORIGINS:
        return None, "unknown origin %r (want %s)" % (origin, "|".join(ORIGINS))
    # SAME IDIOM AGAIN. An unrecognised rank is REFUSED rather than stored: the
    # whole reason this field exists is that seven incompatible spellings were
    # already loose in free text, and a field that accepts an eighth has bought
    # nothing. Absent stays absent — UNRANKED is a real answer, never P3.
    # TYPE BEFORE VALUE, because the value checks below cannot survive a
    # non-string: `priority not in PRIORITIES` refuses a list with a message
    # about SPELLING, and `_story_error` raises TypeError on an unhashable
    # one — a validator crashing instead of refusing.
    # DRIVEN BY THE SCHEMA, not a hand list — SchemaDrivesEveryDoorTest's own
    # rationale: hand-registered doors do not converge. The [] lookup is the
    # drift alarm: a key added to STORY_FIELDS without threading through this
    # signature raises KeyError HERE, at the door, not silently skips.
    _story_args = {"continues": continues, "priority": priority}
    for _name in STORY_KEYS:
        _bad = _typed_field_error(_name, _story_args[_name])
        if _bad:
            return None, _bad
    if priority is not None and priority not in PRIORITIES:
        return None, ("unknown priority %r (want %s, or omit for UNRANKED — "
                      "unranked means nobody has judged this, which is not the "
                      "same as ranked low)" % (priority, "|".join(PRIORITIES)))
    # UNOWNED IS NOT UNACCOUNTABLE, AND THE DIFFERENCE IS THE WHOLE BACKLOG.
    #
    # The mandate this ledger was built under said a row "must name an OWNER
    # SEAT, or we have built a list nobody is accountable to." Taken literally
    # that kills the feature it was protecting: offer_rows() hands IDLE SEATS
    # the rows nobody holds, so a ledger where every row is pre-assigned has an
    # empty offer queue by construction, and unassignable work is exactly the
    # failure the owner named this morning ("did they not get assigned?").
    #
    # So accountability is not a name in a field — it is that the row REACHES
    # somebody. An open unowned row is the next thing an idle seat is offered.
    # What is genuinely incoherent is work IN PROGRESS by nobody, and that is
    # what gets refused.
    if status == "in_progress" and not (owner or "").strip():
        return None, ("in_progress with no owner seat — name who is doing it, "
                      "or file it `open` and let the offer rung route it")
    # AND THE WORD FOR "NOBODY" IS NOT A SEAT. This sits directly under the
    # comment block that USES that word, which is how it got here.
    placeholder = _owner_placeholder_error(owner)
    if placeholder:
        return None, placeholder
    # THE POSTURE GUARD BELONGS TO THE INVARIANT, NOT TO ONE DOOR — this
    # module's own law, and the first cut broke it: the guard lived only in
    # `cmd_task add`, so tasks.add() from the mirror, the todo bridge, the
    # resume-turn recovery path or any script filed a seam strategy unasked
    # (dispatch fff5cef99aec). Every filer passes it here; the CLI
    # only forwards its escapes. An owner-origin row is the owner's own
    # words; a tombstone records history, it is not work (the same exception
    # the owner rule makes two blocks up).
    if status != "closed":
        from . import posture
        refused = posture.check("helm task add", title + "\n" + (note or ""),
                                posture_na=posture_na, owner=origin == "owner")
        if refused:
            return None, refused

    p = path or ledger_path()
    with eventledger.locked(p) as held:
        if not held:
            return None, "task ledger is not writable — obligations UNKNOWN"
        # STRICT, BECAUSE THIS CALLER DECIDES. latest_checked's own
        # contract splits a tolerant PROJECTION read from a DECISION
        # read — one that will file, refuse or classify on the answer —
        # and folds a corrupt COMPLETE row as known-empty for the
        # former. add, update and comment all append to an append-only
        # ledger based on what this returns, so a malformed row skipped
        # here becomes a write made in ignorance of it: a duplicate id
        # admitted, a parent judged dangling, a comment attached to a
        # row whose real state was never read. Poison the read instead.
        existing, unavailable = eventledger.latest_checked(p, strict=True)
        if unavailable:
            return None, "task ledger unreadable (%s) — refusing to mint" % unavailable
        # THE ASK REFLEX, IN THE PRODUCER (task/2622, owner: "how do we
        # prevent me continually asking for things that have been placed on
        # the list and ignored"). A backlog of hundreds of open rows cannot be
        # scanned by the person filing the next one, so the LEDGER resolves
        # the title against the rows it already holds.
        #
        # HERE, AND NOT AT THE CLI DOOR, FOR THE REASON THE POSTURE GUARD
        # SITS HERE. The first cut put it in `cmd_task add` and that forked
        # the semantics: a title typed at a terminal was refused while the
        # SAME title filed through `tasks.add()` — the todo bridge, the
        # resume-turn recovery, any script — was not resolved at all, and the
        # duplicate the guard exists to stop walked in through the door the
        # guard was not on. Every producer owes this, and the ones that
        # legitimately do not pass `force_new` explicitly with their reason.
        #
        # UNDER THE LOCK, ON THE SAME READ THE WRITE IS BASED ON, WHICH IS
        # WHAT MAKES IT RACE-PROOF. The CLI-door version read the ledger
        # OUTSIDE this lock: two adds of one title could each resolve against
        # a backlog that did not yet contain the other and both be filed.
        # `existing` is the strict snapshot this very append is about to
        # extend, so the second writer sees the first's row.
        #
        # A TOMBSTONE IS EXEMPT because it records history rather than work,
        # the same exception the owner rule and the posture guard both make.
        if status != "closed":
            verdict, near = duplicate_verdict(title, existing, project=project)
            refusal = duplicate_refusal(verdict, near)
            if refusal and not force_new:
                return None, refusal
        if tid is None:
            new_id = "task/%d" % _next_number(existing)
        else:
            new_id = normalize_id(tid)
            if not new_id:
                return None, ("unparseable task id %r — want a number (263) or a "
                              "slug (task/work-tab-backlog)" % tid)
            if new_id in existing:
                return None, "%s already exists — use `helm task update`" % new_id
        # THE PARENT IS CHECKED AGAINST THE LEDGER, not taken on the caller's
        # word — it names SELF, UNKNOWN and CYCLE separately so the operator
        # fixes the right one. Deliberately here, after the id is resolved and
        # while `existing` is in hand: a declaration that cannot be verified is the
        # dangling pointer this field exists to replace.
        story_err = _story_error(existing, new_id, continues)
        if story_err:
            return None, "%s %s" % (new_id, story_err)
        # A ROW CAN BE BORN A REASONLESS TOMBSTONE, AND UPDATE() REFUSES THE
        # IDENTICAL END STATE (found in a meld after four
        # async rounds missed it). This is MY OWN LAW one invariant over —
        # the comment in update() reads "a closed set enforced at add() and
        # open at update() is not a closed set", and I wrote it about origin
        # without checking whether the tombstone rule had the same asymmetry.
        # It did, in the other direction: closing REQUIRES a reason, being
        # born closed did not.
        #
        # SAME IDIOM AND SAME WORDING as the closing guard, deliberately, so
        # the two doors cannot drift into saying different things about one
        # invariant. Nothing live creates a born-closed row (checked: no
        # caller passes status=closed to add), so this breaks no path — the
        # 172 historical tombstones were written by a migration that does not
        # run through here.
        if status == "closed" and not (closed_reason or "").strip():
            return None, ("a row born closed is a tombstone and still needs a "
                          "reason — pass closed_reason; a silent close is a "
                          "drop whether it happens at birth or later")
        row = _row(new_id, title, status, owner, note, refs, source, origin,
                   closed_reason, project=project, posture_na=posture_na,
                   continues=continues, priority=priority,
                   # EVERY ROW RECORDS THE SESSION ITS FILER REPORTED,
                   # DELEGATED ROWS INCLUDED, and the stamp is UNCONDITIONAL
                   # because it authorizes nothing. IT IS A CLAIM AND NOT AN
                   # AUDIT FACT: the value is read from the environment and
                   # republished unchanged, so a forged export is stored
                   # verbatim and is indistinguishable from an honest one.
                   # A field that grants no right loses nothing by recording
                   # what the filer said, but it must not be read as saying
                   # who filed.
                   #
                   # DO NOT REBUILD A RECLAIM ON THIS VALUE. Three FIX
                   # verdicts died here: stamping the filer spent FILING as
                   # CUSTODY (`add()` lets a filer name any owner, so
                   # delegating minted a permanent right to take the row
                   # back); restricting to self-filed work still let an
                   # author reclaim after an authorized A->B transfer;
                   # and refusing every once-transferred row stranded the
                   # legitimate new holder. The third finding ended the idea
                   # rather than a version of it — `session_id()` reads raw
                   # environment and `public_row()` copies every field, so
                   # the value is BOTH PUBLISHED AND SETTABLE and a reclaim
                   # keyed on it is a reusable transfer bearer. See
                   # `_reported_session()` for that contract; a renamed seat
                   # recovers work through the evidence-bound takeover
                   # contract until identity ACQUISITION is bound (task/1918).
                   reported_session=_reported_session())
        if not eventledger.append_unlocked(p, row):
            return None, "task ledger refused the write"
    return row, None


def _door_ask(door):
    """One door's ask line, carrying the probe's words when UNMEASURED -> str.

    A door whose probe answered None keeps its place in the offer, because
    absence of evidence closes nothing. The reason it could not be checked is
    a different fact from the offer itself, and it is frequently the sentence
    that says how to make the door open: a token matching several seats names
    every one of them, and sharpening the operand is the whole remedy. A
    refusal that computes that sentence and keeps no place for it leaves the
    offer correct and the reader without the one fact they can act on.

    IT RIDES ON THE ASK RATHER THAN ON THE REFUSAL. A clause appended to the
    whole sentence would say which doors are unmeasured and leave the reader
    to pair reasons with doors; a door that carries its own reason is legible
    at whatever length the table grows to. The doors measured SHUT are named
    with their reasons in their own clause, since those are dropped from the
    offer and a dropped door has no ask to ride on.

    AN UNMEASURED DOOR WITH NO WORDS IS STILL UNMEASURED, so the marker is
    keyed on the tri-state ALONE and the reason merely fills it. Falling
    through to the bare ask when a probe answers None and says nothing renders
    that door EXACTLY like one measured open, which collapses the two states
    this function exists to keep apart — the same defect it cures, one level
    down. No producer in this tree emits that pair today: every None path in
    `takeover.task_owner_door_facts` supplies words, and every falsy-seat
    return in `seat_reassign.resolve_source` carries a reason. But the
    property is held up by discipline spread across two modules and enforced
    nowhere, `_build_contract_reach` already returns an EMPTY second element
    on its True path, and a door added with `reach=lambda n: (None, "")` would
    be mis-rendered in silence. The placeholder costs one branch and makes the
    render unable to claim a measurement it does not have.

    A door measured open renders its ask ALONE. There is nothing to say about
    a check that succeeded, and a parenthetical on every line teaches a reader
    to skip the parentheses.
    """
    if door.reachable is None:
        return "%s (UNMEASURED here: %s)" % (
            door.ask, door.blocked_by or "the probe gave no reason")
    return door.ask


def update(token, path=None, force=False, takeover_auth=None,
           rank_rule=None, rank_actor=None, expect=None, **fields):
    """Append a new full snapshot with `fields` applied -> (row, error).

    Event-sourced: the previous row is never rewritten, so a correction stays
    auditable rather than replacing the record of what was believed before.

    An incumbent owner may be changed (including cleared) only by the opaque,
    evidence-bound BUILD-continuation authorization minted by takeover.py.
    ``force`` remains accepted for call compatibility but authorizes no owner
    mutation: a raw boolean cannot carry exact occurrence, proxywatch, claim,
    lineage, notification, freshness, or task-CAS evidence. The guard lives
    HERE rather than in the CLI because the first version in ``claim`` was
    immediately bypassed by ``update --owner thief``.

    A RANK CHANGE IS AN ACT WITH AN ACTOR, through either door. ``rank_actor``
    is an ``actors.AdmittedActor`` and it is REQUIRED whenever this call would
    change ``priority``: the row's ``source`` records who FILED the work and
    must not be relabelled as who RANKED it, so a rank with no admitted actor
    is refused rather than attributed to the filer. ``rank_rule`` is a
    ``RankRule`` whose ``decide`` runs against the row THIS CALL holds the lock
    on; it yields the third answer ``(SKIPPED, reason)`` when the rule declines,
    and it may not be combined with any other field.

    ``expect`` is the row the caller JUDGED, from its own earlier read. When
    the row under the lock is not that row, nothing is written and the answer
    is ``(SKIPPED, reason)``: a sweep that decided "nobody touched this" from
    a snapshot must not close a row a seat claimed a moment later (task/1738).
    """
    tid = normalize_id(token)
    if not tid:
        return None, "unparseable task id %r" % (token,)
    expect_err = _expect_error(expect)
    if expect_err:
        return None, expect_err
    if rank_rule is not None:
        if not isinstance(rank_rule, RankRule):
            return None, ("rank_rule must be a tasks.RankRule — the rule is "
                          "resolved under this writer's lock and a caller "
                          "cannot hand in a decision already made")
        if fields:
            return None, ("rank_rule decides the priority under the lock and "
                          "cannot be combined with other field(s): %s"
                          % ", ".join(sorted(fields)))
    # `continues` and `priority` ARE MUTABLE, DELIBERATELY, and that is the
    # whole point of them. A row's place in a story is not known at filing
    # time - the owner named this directly: "rows to become subrows of other
    # things, for the whole shape to change as necessary from an overview
    # coordinator perspective". A parent settable only at birth would be
    # decoration, because triage IS re-parenting later, repeatedly, as the
    # picture changes. Same for rank: priority that cannot be revised is a
    # guess frozen at the moment of least information.
    #
    # `project` stays absent from this tuple for the opposite and still-valid
    # reason documented at _row: retro-scoping history is migration work.
    allowed = ("title", "status", "owner", "note", "refs", "source",
               "origin", "closed_reason", "standdown") + STORY_KEYS
    unknown = [k for k in fields if k not in allowed]
    if unknown:
        return None, "unknown field(s): %s" % ", ".join(sorted(unknown))
    if "status" in fields and fields["status"] not in STATUSES:
        return None, "unknown status %r" % fields["status"]
    # VALIDATED AT THE API DOOR, not only at the CLI verb, for the reason the
    # comment below already gives about --continues and --priority: a closed
    # set enforced on one path and open on another is not a closed set, and
    # other helm code calls update() directly. `in fields` never `.get`,
    # because None here is a DELIBERATE CLEAR and truthiness cannot tell that
    # from absence.
    if "standdown" in fields and fields["standdown"] is not None:
        # NORMALISED, NOT MERELY ACCEPTED. The contract returns the value that
        # will be STORED, so an int epoch lands as a float and the replay never
        # has to re-coerce what the writer already settled. Refusing here also
        # means nothing invalid is ever appended to the ledger: the event log
        # keeps what was written, so a bad write is permanent.
        _sd, _sd_err = standdown_contract(fields["standdown"])
        if _sd_err:
            return None, _sd_err
        fields = dict(fields)
        fields["standdown"] = _sd
    # THE SAME LAW THIS FUNCTION ALREADY STATES, AND I NEARLY SHIPPED ITS
    # VIOLATION. Measured on my own first draft: `update` accepted a DANGLING
    # parent, a CYCLE, and the priority "URGENT" — every one of which add()
    # refuses by name. The comment below says it outright: a closed set
    # enforced at add() and open at update() is not a closed set.
    #
    # AND IT IS WORSE FOR THESE TWO FIELDS THAN FOR ANY OTHER, because they
    # are mutable ON PURPOSE. Triage re-parents and re-ranks, so update() is
    # the PRIMARY door for them, not the side entrance — validation that lives
    # only at add() would sit on the path almost nobody takes while the path
    # the work actually runs through stayed open. A cycle arriving that way
    # makes _story_root loop, which is the exact case its own comment claims
    # to defend against.
    # TYPE BEFORE VALUE HERE TOO, and this door matters more: triage drives
    # update(), and other helm code calls it directly. `key in fields` never
    # `.get` — passing None is a DELIBERATE promote-to-root and truthiness
    # cannot tell that from absence.
    for _name in STORY_KEYS:
        if _name in fields:
            _bad = _typed_field_error(_name, fields[_name])
            if _bad:
                return None, _bad
    if "priority" in fields and fields["priority"] is not None \
            and fields["priority"] not in PRIORITIES:
        return None, ("unknown priority %r (want %s, or None for UNRANKED)"
                      % (fields["priority"], "|".join(PRIORITIES)))
    # BOTH DOORS, and this function's own docstring is why: the guard belongs
    # to the invariant, not to one entrance — measured when `update --owner
    # thief` walked straight around a check that lived only in the CLI. A
    # closed set enforced at add() and open at update() is not a closed set.
    if "origin" in fields:
        # PASSING None ERASED A WITNESSED PROVENANCE. The tri-state
        # is not symmetric: UNKNOWN is what an UNWITNESSED row reads, and
        # un-witnessing a row somebody did witness is the erasure this field
        # exists to end, performed through the correction door. A genuinely
        # wrong stamp is corrected by writing the OTHER value, not by
        # blanking it.
        if fields["origin"] is None:
            return None, ("origin cannot be cleared — it records who asked "
                          "for the work, and a witnessed provenance is not "
                          "erasable. Write the correct value instead (%s)"
                          % "|".join(ORIGINS))
        if fields["origin"] not in ORIGINS:
            return None, "unknown origin %r (want %s)" % (
                fields["origin"], "|".join(ORIGINS))

    p = path or ledger_path()
    with eventledger.locked(p) as held:
        if not held:
            return None, "task ledger is not writable — obligations UNKNOWN"
        # STRICT, BECAUSE THIS CALLER DECIDES. latest_checked's own
        # contract splits a tolerant PROJECTION read from a DECISION
        # read — one that will file, refuse or classify on the answer —
        # and folds a corrupt COMPLETE row as known-empty for the
        # former. add, update and comment all append to an append-only
        # ledger based on what this returns, so a malformed row skipped
        # here becomes a write made in ignorance of it: a duplicate id
        # admitted, a parent judged dangling, a comment attached to a
        # row whose real state was never read. Poison the read instead.
        existing, unavailable = eventledger.latest_checked(p, strict=True)
        if unavailable:
            return None, "task ledger unreadable (%s)" % unavailable
        prev = existing.get(tid)
        if not prev:
            return None, "%s does not exist" % tid
        if expect is not None and prev != expect:
            return SKIPPED, _moved(tid)
        # THE PARENT CHECK BELONGS HERE, not only at add(), and it needs the
        # ledger — which is why it sits after the read rather than beside the
        # priority check above. The cycle case is the one that only update()
        # can reach: at add() a brand-new id cannot yet be anyone's ancestor,
        # so a ring is only constructible by RE-PARENTING an existing row.
        # Validation at add() alone would have covered the case that cannot
        # happen and missed the case that can.
        if "continues" in fields:
            story_err = _story_error(existing, tid, fields["continues"])
            if story_err:
                return None, "%s %s" % (tid, story_err)
        # THE RULE DECIDES HERE, AGAINST THE ROW THE LOCK PROTECTS. Every
        # fact it reads — the current rank, the current status, the current
        # project, the current origin — is the state this append will land
        # on, which is the one property a caller's own precheck can never
        # have. See RankRule for the P0 this ordering exists to stop.
        if rank_rule is not None:
            want, decline = rank_rule.decide(prev)
            if decline:
                return SKIPPED, decline
            fields = {"priority": want}
        ranking = ("priority" in fields
                   and fields["priority"] != prev.get("priority"))
        if ranking:
            actor_err = rank_actor_error(rank_actor)
            if actor_err:
                return None, (
                    "ranking %s needs an ADMITTED ACTOR (%s) — `source` "
                    "records who FILED this row and relabelling it as who "
                    "RANKED it erases a witnessed provenance. Resolve one "
                    "with helm.actors.resolve_actor()" % (tid, actor_err))
        row = dict(prev)
        row.update(fields)
        # UPDATE ENFORCES WHAT add() AND close() ENFORCE, because a second door
        # into the same row that skips the first door's rules is not a
        # convenience — it is the rule deleted. Measured by an adversarial
        # reviewer: update(title="") landed a titleless row that add() refuses,
        # and update(status="closed") landed a closed row with no reason, which
        # close() calls "a drop".
        if not (row.get("title") or "").strip():
            return None, "a task needs a title — an update cannot empty it"
        # THE INVARIANT IS ABOUT THE TRANSITION, NOT THE RESULTING STATE, and
        # my first cut got that wrong in a way that broke the ledger's own
        # migration. Guarding on `row["status"] == "closed"` froze EVERY
        # TOMBSTONE: a tombstone is minted reasonless by design (id, title,
        # closed, nothing else), so any later edit to one — retitling it —
        # came back "closing needs a reason", which is not what the caller
        # asked to do. 172 of these rows exist and task/294's whole job is
        # upgrading their titles in place. Fire only when a row is BEING
        # closed.
        closing = prev.get("status") != "closed" and row.get("status") == "closed"
        if closing and not (row.get("closed_reason") or "").strip():
            return None, ("closing needs a reason — use `close`, or pass "
                          "closed_reason; a silent close is a drop")
        # RESURRECTION IS NOT AN UPDATE, AND --force CANNOT BUY IT (task/345).
        # A live repro: `helm task claim 172` on a CLOSED row returned
        # status=in_progress owner=codex-3 WHILE RETAINING closed_reason — one
        # row simultaneously claiming to be live work and carrying the
        # tombstone that explains why it is not. Nothing downstream can read
        # that: every consumer picks one of the two fields and is wrong
        # whenever it picks the other. `claim` reaches this function with
        # status="in_progress" and no terminal check of its own, which is
        # correct — the invariant belongs here, at the door both the CLI and
        # the API come through, for exactly the reason the incumbent check
        # moved here.
        #
        # WHY RAW force DOES NOT OVERRIDE IT. A boolean carries no takeover
        # evidence and authorizes no incumbent mutation; independently, a closed
        # row has no live BUILD owner to transfer. Letting force through here
        # would also make one unrelated escape mean "resurrect tombstones".
        # Different invariant, and no escape exists unless one is designed.
        #
        # EDITS THAT STAY CLOSED REMAIN LEGAL. Retitling a tombstone is
        # task/294's entire job and the `closing` guard above already records
        # why that distinction matters; this fires only on the transition OUT
        # of closed, never on the resting state.
        if prev.get("status") == "closed" and row.get("status") != "closed":
            return None, (
                "%s is CLOSED (%s) — a closed row cannot be reopened, and "
                "forcing does not override it. If the work is genuinely live "
                "again it is NEW work: file a row that cites this one rather "
                "than editing history out from under whoever read it"
                % (tid, (prev.get("closed_reason") or "no reason recorded")))
        # BOTH DOORS, per this function's own docstring: a rule enforced at
        # add() and open at update() is not a rule. Measured here by the same
        # kind of walk-around the origin guard above records.
        if "owner" in fields:
            placeholder = _owner_placeholder_error(fields["owner"])
            if placeholder:
                return None, placeholder
        # TAKING OR CLEARING A ROW FROM A HOLDER IS AN AUTHORIZATION EVENT, not
        # a boolean override. The old `wanted and ... and not force` had TWO
        # bypasses: force=True stole the row with no evidence, and owner=""
        # cleared it because the truthiness guard never ran. Both delete the
        # incumbent's ownership through this same boundary now.
        #
        # `owner_of` and not the raw field: a row already holding the display
        # word has no holder to take it from, and reading it raw is what makes
        # those legacy rows refuse every ordinary claim.
        incumbent = owner_of(prev)
        wanted = (row.get("owner") or "").strip()
        # A RENAME IS NOT A TRANSFER, AND THIS IS THE ONE CASE THAT IS NOT A
        # TAKEOVER AT ALL (task/1898). Seat names are re-rostered by slot on a
        # reboot, so the seat that FILED a row can find it held by a name that
        # is now a DIFFERENT LIVE SEAT — and the takeover contract cannot help,
        # because it is built for lane-to-lane hand-off between two parties.
        # There are not two parties here. There is one session that SAYS it
        # filed the row and is asking for its own work back under the name it
        # answers to today — and "says" is the whole difficulty, because
        # nothing here can tell that session from one that copied its id.
        #
        # AND THE DOOR IS NOT OPEN, BECAUSE IT CANNOT BE MADE SAFE HERE.
        # Three successive versions of a reclaim keyed on this field were each
        # refuted, and refuting the third killed the IDEA:
        #
        #   1. stamping the FILER let a seat reclaim work it had assigned away
        #      — filing spent as custody;
        #   2. restricting to self-filed work still let the author take a row
        #      back after an AUTHORIZED transfer, because an immutable field
        #      cannot express a fact that changes;
        #   3. and the fatal one — `session_id()` reads raw environment
        #      (home._SESSION_ENV, unvalidated) while `public_row()` copies the
        #      whole row, so the value is BOTH PUBLIC AND SETTABLE. A reader
        #      takes the published session id, exports it, names any seat, and
        #      the reclaim hands over the row.
        #
        # I HAD WRITTEN HERE THAT THIS WAS "NOT A REGRESSION" because seat
        # names are equally settable. THAT WAS WRONG, and it is the most
        # useful thing on this lane: spoofing a session could already
        # IMPERSONATE a seat, but it could not change custody to a DIFFERENT
        # NAME. A reclaim door turns a public row field into a REUSABLE
        # TRANSFER BEARER — a capability that did not exist before it.
        #
        # SO THE FIELD IS A REPORTED CLAIM, RECORDED AND NEVER TRUSTED. It
        # records what the filer's environment SAID, not who filed, and it is
        # never consulted by any authorization path. A reviewer measured the
        # difference end to end: a forged export is stored and republished
        # verbatim, indistinguishable from an honest one. A renamed seat recovers
        # its work through the ordinary evidence-bound takeover contract, the
        # same as everyone else, until identity ACQUISITION is bound (roster-
        # resolved acting identity — see the successor row). Do not re-open
        # this door on a value a reader can copy; the arm below pins it shut.
        if incumbent and wanted != incumbent:
            if takeover_auth is None:
                # EVERY DOOR, AND THE COUNT DERIVED FROM THE SAME LIST. This
                # named one capability while takeover accepted two, and the
                # unnamed one covers exactly the row most likely to be here:
                # one held by a seat no roster knows. A hand-kept sentence
                # beside a dispatch drifts the first time the dispatch grows.
                from . import takeover
                # EVERY CLAIM THIS SENTENCE MAKES ABOUT THE DOORS IS READ
                # FROM THE DOORS. The count was already derived so the
                # sentence could not promise a capability the code lacks;
                # the clause beside it invented three more facts in turn —
                # how many there are, how they open, which one to ask for —
                # and each was true only of the rows that existed when it
                # was written. A table that carries its own facts cannot be
                # outlived by the prose that reads them.
                facts = takeover.task_owner_door_facts(incumbent)
                doors = [d.text for d in facts]
                mine = [d for d in facts if d.holder_may_open]
                # HOW THEY OPEN, JOINED FROM THE ROWS. One door renders
                # "TAKEN", two render "TAKEN or FORCED", and a third with new
                # mechanics joins itself in without anyone remembering to.
                how = sorted({d.opened_by for d in facts if not
                              d.holder_may_open})
                # WHICH OF THEM CAN OPEN FOR THIS SEAT, MEASURED HERE. A
                # capability the code can dispatch is not the same fact as a
                # capability the ENVIRONMENT can resolve, and this sentence
                # was honest about the first while silent about the second:
                # the BUILD contract resolves an incumbent through the
                # registered seat families, so for a seat whose family has no
                # register the door it advertises cannot open at all. An
                # UNMEASURABLE door stays in the offer — absence of evidence
                # closes nothing.
                shut = [d for d in facts if d.reachable is False]
                # AN UNMEASURED DOOR KEEPS ITS ASK AND ITS REASON
                # TRAVELS WITH IT. The tri-state decides which doors
                # are OFFERED; WHY a door could not be checked is a
                # separate fact, and it belongs on that door rather
                # than on the refusal. See `_door_ask` — on an UNKNOWN
                # door that reason is frequently the only actionable
                # text the whole sentence carries.
                asks = [_door_ask(d) for d in facts
                        if d.reachable is not False]
                if mine:
                    # A HOLDER-OPENABLE DOOR EXISTS, so the refusal must not
                    # say none does. It still refuses — this branch is the
                    # incumbent guard, not an authorization — but it names
                    # the door instead of denying it.
                    clause = ("%d of them IS yours to open: %s"
                              % (len(mine), "; and ".join(d.text
                                                          for d in mine)))
                elif how:
                    clause = ("NONE of them is yours to open: each is %s by "
                              "somebody else, never offered by the holder"
                              % " or ".join(how))
                else:
                    clause = "NONE of them is yours to open"
                # AN EMPTY TABLE AND A TABLE THAT IS ALL SHUT ARE DIFFERENT
                # FACTS AND MUST NOT SHARE A SENTENCE. "No capability exists"
                # is about the CODE and is true of every row; "none can open
                # for this seat" is about this incumbent's environment and is
                # false the moment another seat reads the same row. Collapsing
                # them tells a reader the wrong one is fixable.
                if asks:
                    advice = "So say it on the row — %s." % ", or ".join(asks)
                elif not facts:
                    advice = ("So say it on the row: there is no capability "
                              "here to ask for.")
                else:
                    advice = ("So say it on the row: no capability here can "
                              "open for %s, so there is nothing to ask for "
                              "on this row." % incumbent)
                # THE MEASUREMENT IS DATED BY ITS OWN WORDS. It is true of the
                # register as it stands in this process, and a seat added
                # afterwards changes it; saying MEASURED NOW keeps a reader
                # from carrying the verdict into tomorrow.
                blocked = ("" if not shut else
                           " MEASURED NOW, %d of those cannot open for %s at "
                           "all: %s — %s."
                           % (len(shut), incumbent,
                              "; and ".join(d.text for d in shut),
                              "; ".join(d.blocked_by for d in shut)))
                return None, (
                    "%s is held by %s — raw force cannot transfer or clear an "
                    "incumbent; quiet, no reply, and claim age are not "
                    "authorization. %d capabilit%s can: %s. If you ARE %s and "
                    "want this row elsewhere, %s.%s %s"
                    % (tid, incumbent, len(doors),
                       "y" if len(doors) == 1 else "ies",
                       "; and ".join(doors), incumbent, clause, blocked,
                       advice))
            from . import takeover
            proof, auth_err = takeover.authorize_task_mutation(
                takeover_auth, tid, prev, fields)
            if auth_err:
                return None, auth_err
            row["takeover"] = proof
        elif takeover_auth is not None:
            return None, ("takeover proof is scoped to an incumbent BUILD-owner "
                          "change and cannot authorize this mutation")
        # A CUSTODY CHANGE IS NOT ACTIVITY ON THE WORK.
        #
        # This stamped last_updated unconditionally, so reassigning a task
        # REFRESHED it — and stalebot reads exactly that field to decide what
        # has gone quiet. Moving a dead seat's backlog to a live one therefore
        # made every stale row look freshly touched and SUPPRESSED the detector
        # on precisely the rows a reassignment exists to rescue. The board goes
        # clean while the work rots, which is the worst shape a staleness
        # surface can take: it is not blind, it is confidently wrong.
        #
        # Reaching here with a takeover proof means the incumbent-owner branch
        # above ran (the `elif` returns for every other proof-bearing call), so
        # a proof plus an owner-only field set IS a pure custody move. It keeps
        # the prior timestamp and records the transfer separately, so the move
        # stays auditable without masquerading as progress. Everything else
        # stamps exactly as before.
        _custody_only = (takeover_auth is not None
                         and set(fields) <= {"owner"}
                         and prev.get("last_updated") is not None)
        if _custody_only:
            row["last_updated"] = prev.get("last_updated")
            row["custody_updated"] = time.time()
        else:
            row["last_updated"] = time.time()
        # WHO RANKED IT, BY WHICH ACT, AND FROM WHAT. The rank field recorded
        # a history of VALUES and could not answer a single question about
        # AGENCY: a sweep's derived P2 and an operator's deliberate P2 landed
        # as the same event. `source` stays the filer's, untouched, on this
        # row and on every historical one.
        if ranking:
            row["ranked_by"] = rank_actor.canonical_name
            row["ranked_at"] = row["last_updated"]
            row["rank_action"] = "rule" if rank_rule is not None else "manual"
            row["ranked_from"] = prev.get("priority") or None
        if row.get("status") == "in_progress" and not (row.get("owner") or ""):
            return None, ("%s would be in_progress with no owner seat — name "
                          "one with --owner" % tid)
        if not eventledger.append_unlocked(p, row):
            return None, "task ledger refused the write"
    return row, None


def _expect_error(expect):
    """None when `expect` is absent or a row dict, else why it is refused."""
    if expect is None or isinstance(expect, dict):
        return None
    return ("expect must be the row dict the caller read, not %s"
            % type(expect).__name__)


def _moved(tid):
    return ("%s changed since the caller read it — nothing was written; "
            "judge it again from a fresh read" % tid)


def close(token, reason, path=None, expect=None):
    reason = (reason or "").strip()
    if not reason:
        return None, "closing a task needs a reason — a silent close is a drop"
    return update(token, path=path, expect=expect, status="closed",
                  closed_reason=reason)


def counts(path=None):
    """(by-status, unavailable) for the console badge — ALWAYS fleet-wide.

    The list footer no longer calls this: a scoped listing's legend must
    count the scoped population (dispatch ff7024b5 — fleet totals
    under a scoped header totalized the withheld rows), so it counts its own
    snapshot in-branch. A badge that means "the whole ledger" keeps meaning
    that; a caller wanting a scoped count filters project_of_row itself."""
    snap, unavailable = snapshot(path)
    if unavailable:
        return {}, unavailable
    out = {}
    for row in snap.values():
        st = row.get("status") or "unknown"
        out[st] = out.get(st, 0) + 1
    return out, None


def cited_in(text, path=None):
    """Every task this prose cites UNAMBIGUOUSLY -> [row, ...].

    EXPLICIT FORMS ONLY: task/263 and task-263. A bare "#263" is NOT read here,
    and the docstring used to promise it was — this repo's own commit subjects
    cite GitHub issues that way ("closes #130"), so scanning prose for #NNN
    turned every issue reference into a task citation.

    THE COST IS REAL AND IS NOT PAPERED OVER: the 441 historical "#263" chat
    citations this module was built for are NOT resolvable through this
    function. They resolve through `resolve`/`normalize_id`, where a human has
    typed the number and declared what they mean. Prose scanning has no such
    declaration, so it takes the narrow reading. If an ambiguous-citation
    surface is ever wanted it needs its own lower-confidence channel that says
    "this MIGHT be task/263", not a widening of this one.
    """
    if not isinstance(text, str):
        return []
    known = rows(path)
    out, seen = [], set()
    for tok in _CITED.findall(text):
        tid = "task/%d" % int(tok)
        if tid in known and tid not in seen:
            seen.add(tid)
            out.append(known[tid])
    return out


def comment(token, text, by=None, path=None, expect=None):
    """Append one non-closing note -> (row, error), or (SKIPPED, reason) when
    `expect` is given and the row moved since the caller read it (see update).

    Comments live IN the row, appended to `comments[]`, which is the shape
    owner-decisions already uses (ownerasks.py:532-533). One ledger, one read,
    no join, and no comment that can outlive the task it annotates. The cost is
    that a row with N comments is O(N) bytes and re-appends whole: bounded, and
    the same cost the decision queue already accepts.
    """
    text = (text or "").strip()
    if not text:
        return None, "an empty comment says nothing — pass the text"
    tid = normalize_id(token)
    if not tid:
        return None, "unparseable task id %r" % (token,)
    expect_err = _expect_error(expect)
    if expect_err:
        return None, expect_err

    p = path or ledger_path()
    with eventledger.locked(p) as held:
        if not held:
            return None, "task ledger is not writable"
        # STRICT, BECAUSE THIS CALLER DECIDES. latest_checked's own
        # contract splits a tolerant PROJECTION read from a DECISION
        # read — one that will file, refuse or classify on the answer —
        # and folds a corrupt COMPLETE row as known-empty for the
        # former. add, update and comment all append to an append-only
        # ledger based on what this returns, so a malformed row skipped
        # here becomes a write made in ignorance of it: a duplicate id
        # admitted, a parent judged dangling, a comment attached to a
        # row whose real state was never read. Poison the read instead.
        existing, unavailable = eventledger.latest_checked(p, strict=True)
        if unavailable:
            return None, "task ledger unreadable (%s)" % unavailable
        prev = existing.get(tid)
        if not prev:
            return None, "%s does not exist" % tid
        if expect is not None and prev != expect:
            return SKIPPED, _moved(tid)
        row = dict(prev)
        row["comments"] = list(prev.get("comments") or ()) + [
            {"ts": time.time(), "text": text, "by": by or None}]
        row["last_updated"] = time.time()
        if not eventledger.append_unlocked(p, row):
            return None, "task ledger refused the write"
    return row, None


def offer_rows(path=None, seat=None, claimed=(), live=None):
    """The idle-seat work-offer producer -> [(id8, line, claim_cmd, mine, kind,
    raw), ...] in seats._offer_rows' tuple shape.

    THIS IS THE HALF THAT MAKES IT A BACKLOG RATHER THAN A LIST. The offer rung
    (seats.py:4133) has been fully built for weeks with exactly one producer —
    the dispatch ledger — and a dispatch row is tip-bound by construction, so an
    un-started plan could never reach the one surface whose job is handing work
    to an idle seat. A task ledger that does not feed it repeats the failure the
    2026-07-21 owner ruling already named: agents keep driving from their own
    private lists because the shared one never reaches them.

    A STRANDED ROW IS OFFERABLE; A LIVE SEAT'S ROW IS NOT — AND THE FIRST
    VERSION OF THIS FUNCTION KEPT ONLY HALF OF THAT SENTENCE. I quoted the
    consumer's rule to justify offering owned rows (seats.py:4185-4195: "a
    named recipient who is NOT LIVE is the STRANDED-WORK case this rung exists
    to rescue, so excluding it would starve the rung") and then implemented the
    conclusion without its antecedent. The consumer's actual code is two
    clauses, not one: `if recip and recip != me and recip in live: continue`
    excludes the in-flight row FIRST, and only what survives that gets the
    [assigned:] marker. Measured on the live ledger before this cure: 110 rows
    offered, 69 of them owned by a seat that was live at that instant — 63% of
    an idle seat's offer list was work other seats had in hand. The rescue was
    poaching, and wiring this into the rung (task/290) would have shipped it
    fleet-wide.

    `live` IS TRI-STATE AND `()` IS NOT `None`. A measured-empty live set says
    every owned row is genuinely stranded and all of them are offerable; `None`
    says the roster could not be read, and an unreadable roster must never be
    the reason a seat takes another's work. So None fails CLOSED — but closed
    to owned rows only, not to the whole list, because liveness is a fact about
    an OWNER and an unowned pool row has no owner to be wrong about. (The
    consumer fails closed to [] on an unreadable CLAIMS file, and that
    asymmetry is deliberate: unknown claims taint every row, unknown liveness
    taints only the owned ones.) `if live is None` is therefore load-bearing —
    `if not live` would collapse measured-empty into unknown and silence the
    rescue exactly when the fleet is emptiest.

    `seat` is the ASKING seat, `claimed` is the set of already-held claim
    resources, and `live` is the live-seat set. All three are passed IN rather
    than imported, so this module never reaches into seats and cannot create
    the cycle that the offer rung's own consumers already have to route around.
    """
    me = str(seat or "")
    held = set(claimed or ())
    # IDENTITY IS COMPARED CASEFOLDED, DISPLAYED VERBATIM. seats._live_seats()
    # returns casefolded names by explicit contract; a task row's owner is
    # whatever a seat typed into `--owner`. Comparing those raw let owner
    # "Alpha" survive a roster of {"alpha"} and be offered as stranded while
    # that seat was building — measured at this lane's tip by a second read,
    # the exact residual hole it was asked to attack. The exclusion is a fact
    # about IDENTITY, so it reduces both sides to one form; the [assigned:]
    # marker keeps the owner's own spelling, because a reader recognises the
    # name they wrote and casefolding the display would make the rescue
    # harder to read to fix a comparison bug.
    me_key = me.casefold()
    live_keys = None if live is None else {str(s).casefold() for s in live}
    out = []
    for row in open_rows(path):
        tid = str(row.get("id", ""))
        short = tid.split("/", 1)[-1]
        # THE RESOURCE NAMESPACE IS OURS, NOT dispatch's. The rung skips a row
        # whose resource another idle seat already holds; sharing dispatch's
        # namespace would let a task and a dispatch with the same short id
        # silence each other.
        res = "task:" + short
        if res in held:
            continue
        # THE ROW'S OWN STAND-DOWN, AND IT IS CHECKED HERE RATHER THAN IN
        # open_rows BECAUSE ONLY THE OFFER IS WRONG. task/406, measured on
        # task/213: that row is paused by an explicit owner work order, five
        # seats read it and wrote a decline rationale, and the rung re-offered
        # it a sixth time — including once to the very seat whose pass comment
        # said it was being recorded "so the next whisper does not re-route it
        # blind". The rung was not ignoring the pause; there was nothing to
        # ignore, because a pause that lives in a note is not a fact any guard
        # can consult. It is one now.
        if standdown_blocks_offer(row):
            continue
        # `owner_of`, because a row holding the display word is NOT held by a
        # seat called "UNOWNED". Read raw, `others` goes True, no live seat
        # matches that name, and the row takes the STRANDED-RESCUE branch — so
        # 18 unowned rows were being offered as somebody's abandoned work
        # instead of as the free pool this rung exists to fill.
        owner = owner_of(row)
        owner_key = owner.casefold()
        others = bool(owner) and owner_key != me_key
        if others:
            # SOMEBODY ELSE'S ROW SURVIVES ONLY BY BEING STRANDED. Unknown
            # roster (None) and a live owner are both exclusions; only an owner
            # measured absent from a readable roster reaches the rescue.
            if live_keys is None or owner_key in live_keys:
                continue
        line = "%s %s" % (tid, (row.get("title") or "")[:120])
        if others:
            line += " [assigned: %s]" % owner[:16]
        # `mine` is an AUTO-CLAIM signal and demands a real owner: an unowned
        # row is the pool, and pool rows are ambiguous by definition — any idle
        # seat could argue fit — so they stay offers and never auto-claims.
        out.append((short, line, "helm task claim " + short,
                    bool(owner) and owner_key == me_key, "task", row))
    return out


def public_row(row):
    """THE serialization shape — ONE owner, because routing N readers through
    origin_of by hand is what produced four rounds of the same defect.

    Rounds 1-3 cured the wire, the glyph, and `show`; round 4 found the JSON
    paths still emitting raw rows. Every round I fixed the readers I
    could find and a new one appeared, which is the signal to change the SHAPE
    rather than patch again: normalizing at the serialization boundary makes a
    NEW consumer correct by DEFAULT instead of correct by remembering.

    NOT IN THE PROJECTION, deliberately, and this is the constraint that
    decides the design: `update()` reads a projected row and writes it back
    (`row = dict(prev); row.update(fields)`), so normalizing upstream of that
    would write `origin: None` into the next event and DESTROY the migration
    tag in the record. The ledger keeps what was written; this is the door
    everything READS through.

    The raw value rides along as `origin_recorded` when it differs, so an
    audit reader loses nothing and no consumer can mistake it for the field."""
    out = dict(row)
    raw = out.get("origin")
    out["origin"] = origin_of(row)
    if raw and out["origin"] is None:
        out["origin_recorded"] = raw
    # THE PROJECT KEY IS ALWAYS PRESENT on the wire, normalized through the
    # one reader: a consumer must not have to know whether a row predates the
    # axis (the same promise `comments` makes about predating rows).
    out["project"] = project_of_row(row)
    # THE REPORTED SESSION IS AUDIT, NOT WIRE, AND THE DIFFERENCE IS A
    # CAPABILITY. `actors.resolve_actor_reason` corroborates a DECLARED seat
    # name by asking `seats_roster.seat_for_session` whether the acting
    # session is on the roster, and the acting session is read from ordinary
    # environment (`home._SESSION_ENV`). So the value that satisfies that
    # check is a BEARER value, and republishing it here put every live seat's
    # bearer on a surface any reader can query. MEASURED: `helm task show
    # <id> --json` returned a live seat's session id, and that id resolved
    # through the roster to that seat's name while an invented one resolved
    # to nothing.
    #
    # IT IS DROPPED AT THE SERIALIZATION BOUNDARY, WHICH IS THE ONLY PLACE
    # THAT CAN HOLD. This function exists because four rounds of fixing
    # readers one at a time kept producing a new raw-row emitter; the same
    # argument decides where a field STOPS being published. A consumer added
    # tomorrow is correct by default.
    #
    # THE RECORD IS NOT DESTROYED. The store keeps the field — `_row` still
    # stamps it, `update()` still refuses to edit it — so correlating rows
    # that report one session is still possible for a reader holding the
    # ledger. What is gone is the ANONYMOUS read: you must already be able to
    # read the store.
    #
    # NOTHING CONSUMED IT. Swept before removing: `reported_session` is named
    # in this module and in tests/test_tasks.py and nowhere else in the tree,
    # so the rename-recovery use the field's own comment describes was never
    # wired to a reader. This removes an exposure, not a feature.
    out.pop("reported_session", None)
    return out


def as_json(row):
    return json.dumps(public_row(row), sort_keys=True)


def _coerce_continues(raw):
    """(parent_or_None, error). EMPTY IS A DELIBERATE CLEAR; GARBAGE IS NOT.

    normalize_id maps an unparseable token to the empty string, and the CLI
    spells "promote this row back to its own story" as `--continues=`. Collapse
    those and `--continues=bogus` SUCCEEDS AND CLEARS AN EXISTING PARENT — a
    typo silently destroys a triage decision and reports success. That is
    absence and cannot-parse sharing one representation, inside the field built
    to end exactly that, and the promote-out spelling is what created it.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    parent = normalize_id(raw)
    if not parent:
        return None, ("%r is not a task id — refusing rather than clearing the "
                      "parent, because an unparseable id and a deliberate "
                      "promote-out must not mean the same thing. To promote a "
                      "row to its own story pass an EMPTY value" % raw)
    return parent, None


def _coerce_priority(raw):
    """(rank_or_None, error). Empty is UNRANKED; the rank set is checked at the
    API door, which both CLI doors already reach."""
    return ((raw or "").strip().upper() or None), None


STORY_FIELDS = (
    ("--continues", "continues", _coerce_continues),
    ("--priority", "priority", _coerce_priority),
)
STORY_FLAGS = tuple(flag for flag, _key, _coerce in STORY_FIELDS)
STORY_KEYS = tuple(key for _flag, key, _coerce in STORY_FIELDS)



USAGE = (
    "usage: helm task add <title...> [--owner SEAT | --mine] [--note N] "
    "[--ref R]... [--id NNN] [--owner-asked] [--posture-na REASON] "
    "[--project NAME] [--force-new] "
    + " ".join("[%s V]" % f for f in STORY_FLAGS) + "\n"
    "       helm task list [--all] [--all-projects] [--project NAME] "
    "[--mine | --owner SEAT] [--json]\n"
    "       helm task show <id> [--json]        (id: 263, #263 or task/263)\n"
    "       helm task resolve <token> [--json]  (what does '#263' mean?)\n"
    "       helm task claim <id> [--owner SEAT]\n"
    "       helm task takeover <id> --from-lane L --transfer-id ID "
    "[--superseding]\n"
    "       helm task update <id> [--title T] [--note N] [--owner S] "
    "[--status S] [--origin owner|agent] [--ref R]... "
    + " ".join("[%s V]" % f for f in STORY_FLAGS)
    + "   (empty value promotes a row out / unranks it)\n"
    "       helm task triage [--apply] [--limit N] [--project NAME]"
    "   (rank the UNRANKED open rows)\n"
    "       helm task close <id> <reason...>\n"
    "       helm task comment <id> <text...>\n"
    "       helm task standdown <id> <reason...> [--lift TEXT] [--until 4h|2d|YYYY-MM-DD]\n"
    "       helm task standdown <id> --clear\n"
    "\n"
    "The FLEET TASK LEDGER — shared work items every seat can resolve. Ids keep\n"
    "the number they were born with, so a week-old chat row citing '#263' still\n"
    "means something. `resolve` is that lookup and it takes any spelling.\n"
    "\n"
    "`add`, `list` and `triage` scope by `--project NAME` when given and by\n"
    "cwd otherwise — THE FLAG WINS, so a task about helm files to helm from\n"
    "any checkout (task/2446) — and each says which scope it used and how it\n"
    "decided. An unregistered name is refused, naming the registry.\n"
    "`update`/`close`/`show` take a FLEET-WIDE id and read no scope at all.\n"
    "\n"
    "`list` shows THIS project's rows (cwd-derived, the store's own lens) and\n"
    "DISCLOSES what it withholds — unscoped legacy + other projects' counts —\n"
    "in one line; --all-projects renders every row, labeled. Ids stay\n"
    "fleet-wide: `show`/`resolve` answer across every scope.\n"
    "\n"
    "`add` RESOLVES the new title against the open backlog before it files.\n"
    "A strong word overlap is REFUSED and the refusal NAMES the three closest\n"
    "rows — comment on the one that already exists, or pass --force-new to say\n"
    "the overlap is a coincidence. A title with no comparable words (only\n"
    "grammar, only punctuation) cannot be resolved at all, so it is refused as\n"
    "UNKNOWN rather than filed as proven-new; --force-new files it unresolved.\n"
    "\n"
    "A value that starts with a dash needs the `=` spelling: `--note=-5` and\n"
    "`--note=--force` are taken literally, while `--note -5` is refused as a\n"
    "probable typo. `--` before the title is the same escape for a TITLE that\n"
    "starts with a dash; it does not reach a flag value.")


# ONE TABLE, EVERY DOOR. These two fields used to be registered BY HAND in
# seven places: _VALUED_FLAGS, add's extraction, add's valueless inventory,
# update's pairs, update's post-coercion, USAGE, and the nothing-to-change
# sentence. That is not an accident waiting to happen, it is a GENERATOR: the
# fields were once registered in four of the seven and shipped a feature
# whose production door refused it; the correction, written specifically to
# close that gap, still missed USAGE and the nothing-to-change sentence.
# Knowing the hazard and looking for it was not enough, twice.
#
# A DOOR LIST MAINTAINED BY MEMORY IS ALWAYS ONE DOOR SHORT, so the list stops
# being maintained by memory. Each entry is (flag, row key, coercion); every
# door below DERIVES from this tuple and none of them names a field again.
# Adding a third mutable field is one line here.
_VALUED_FLAGS = ("--owner", "--note", "--id", "--posture-na",
                 "--ref", PROJECT_FLAG) + STORY_FLAGS
_BOOL_FLAGS = ("--mine", "--owner-asked", "--force-new")


def _is_flag(tok, names):
    return tok in names or any(tok.startswith(n + "=") for n in names)


def _option_span(rest):
    """(leading, middle, trailing) — flags are consumed ONLY from the leading
    run and a CLEAN trailing run; everything between is title.

    WHY POSITION DECIDES AND NOT MEMBERSHIP (task/1451): the old code asked
    `"--mine" in rest` over the WHOLE argv and then removed the first
    occurrence, so an operator typing an unquoted title — `task add fix the
    --mine bypass` — had the word DELETED from the title and the row silently
    became owned. `_take` had the identical shape for valued flags and ate the
    following word too — but ONLY THE FOLLOWED-BY-TITLE-WORDS FORM is the
    defect: `--owner field is eaten and nobody can tell why`
    mid-title filed owner="field" with two words gone, rc 0, while `add
    TITLE --owner bob` is a CLEAN TRAILING SUFFIX and is deliberately read
    as options under the ruling. Both positions legitimate today are
    preserved — `add --owner-asked TITLE` (leading) and `add TITLE --note X`
    (trailing) — and a flag with ordinary TITLE WORDS AFTER IT is left in
    `rest` for the guard below,
    which already refuses every remaining dash-leading token and names the
    escape. NO NEW REFUSAL PATH IS ADDED and no refusal boundary moves; the
    narrowed-out tokens simply reach a door that was already there."""
    n = len(rest)
    lead = 0
    while lead < n:
        tok = rest[lead]
        if _is_flag(tok, _BOOL_FLAGS) or any(
                tok.startswith(f + "=") for f in _VALUED_FLAGS):
            lead += 1
        elif tok in _VALUED_FLAGS:
            lead += 2
        else:
            break
    lead = min(lead, n)
    # RIGHT-TO-LEFT, STOPPING AT THE FIRST TOKEN THAT CANNOT BEGIN A GROUP.
    # THE COMMON READING WINS AND THE ESCAPE SETTLES THE REST (a review ruling,
    # task/1458). `add plain title --owner bob` and `add fix the --owner
    # bypass` are grammatically IDENTICAL — a space-form valued flag followed
    # by a bare word — so no parser can separate them by position, and this
    # code DELIBERATELY reads both as options. A literal title of that shape
    # uses `--` before the whole title. What the right-to-left scan buys is
    # narrower and real: it stops the run at title text, so a flag with
    # ordinary words AFTER it is not swept into the run. A group is one
    # boolean flag, one `=`-form flag, or one valued flag plus the token that
    # follows it; a bare word can only ever be a group's second half.
    tail = n
    i = n
    while i > lead:
        if _is_flag(rest[i - 1], _BOOL_FLAGS) or any(
                rest[i - 1].startswith(f + "=") for f in _VALUED_FLAGS):
            i -= 1                      # a self-contained group
        elif i - 2 >= lead and rest[i - 2] in _VALUED_FLAGS:
            i -= 2                      # `--flag value`, taken as one group
        elif rest[i - 1] in _VALUED_FLAGS:
            i -= 1                      # a bare trailing valued flag: `_take`
                                        # owns that refusal and says it better
        else:
            break                       # title text — the run ends here
        tail = i
    return rest[:lead], rest[lead:tail], rest[tail:]


def _take(rest, flag):
    """Pull `--flag value` or `--flag=value` out of rest -> value or None.

    A VALUE THAT LOOKS LIKE A FLAG IS NOT A VALUE, IN THE SPACE
    FORM. The first cut took whatever token followed, so `--note --owner cj`
    set note to "--owner" and then silently ate the owner too: one typo, two
    wrong fields, rc 0, and a row that claims what nobody asked for. Same
    failure at the end of the line, where a trailing `--note` consumed
    nothing and vanished without trace.

    THE `=` FORM IS AUTHORITATIVE AND ITS VALUE IS ALWAYS LITERAL. `--note
    --force` is ambiguous — two readings, and the wrong one files a lie — but
    `--note=--force` is not ambiguous in any shell or any parser: one token,
    one flag, one value, and the operator wrote the `=` on purpose. So the
    space form keeps guessing-refuses and the `=` form never guesses. That is
    the GNU/argparse split, and adopting it is what makes the space-form
    guard legitimate: a guard whose forbidden thing is UNSAYABLE gets deleted
    by the first person who needs it (a second read REFUTED the claim that
    `--` was that escape — `--` cuts the TITLE and never reaches a flag
    VALUE, so before this there was no way at all to file a note reading
    `--force`, or `-5`, which is an ordinary note).

    A `=` WITH NOTHING AFTER IT IS A VALUE, NOT AN OMISSION: `--note=` means
    "explicitly empty", and add() normalises it, whereas returning None there
    would report the flag missing when it was typed.

    ONE DOOR: every valued flag on this verb comes through here, so both
    rules live here rather than in each branch — which is why `--owner=`,
    `--id=` and `--ref=` gain the literal form in the same edit. A
    flag-shaped SPACE-form value is LEFT IN `rest`, where the caller's
    unconsumed-flag check refuses it by name — that is how the operator
    learns which token was wrong instead of reading a row they did not
    file."""
    eq = flag + "="
    for i, tok in enumerate(rest):
        if tok == flag:
            nxt = rest[i + 1] if len(rest) > i + 1 else None
            val = None if (nxt is None or nxt.startswith("-")) else nxt
            del rest[i:i + (2 if val is not None else 1)]
            return val
        if tok.startswith(eq):
            del rest[i]
            return tok[len(eq):]
    return None


def _take_all(rest, flag):
    """Repeated flag -> list of values, both spellings.

    THE LOOP CONDITION HAD TO LEARN THE `=` FORM TOO. `while flag in rest`
    is an exact-token test, so a lone `--ref=#218` never entered the loop,
    was never consumed, and fell through into the title join — the same
    silent corruption the `=` form was added to end, reintroduced one line
    away from the fix. An empty value stays dropped, which is what `--ref ""`
    already did: a citation with nothing in it cites nothing."""
    out = []
    while flag in rest or any(t.startswith(flag + "=") for t in rest):
        val = _take(rest, flag)
        if val:
            out.append(val)
    return out


def _cli_error(err):
    """Library errors state the fact; this layer adds CLI-only remedies."""
    # THE FLAG SPELLING BELONGS HERE, NOT IN THE LIBRARY. update()
    # is called by the API too, and an error naming a command tells an API caller
    # to use a surface it cannot reach. Incumbent transfer errors already name
    # the evidence-bound contract rather than advertising the retired raw-force
    # bypass.
    # The closed-row invariant remains a separate remedy below.
    if "cannot be reopened" in err:
        return err + " (`helm task add ... --ref <id>`)"
    return err


def origin_of(row):
    """THE origin any CONSUMER sees: "owner" | "agent" | None (UNKNOWN).

    ONE OWNER FOR THE DERIVATION, and it exists because the closed set was a
    closed set only at the WRITE doors. Measured on the real ledger (probe,
    round 1): 251 rows carry `corpus-2026-08-05` — 73 of them LIVE — a value
    the migration wrote long before add()/update() validated anything. So the
    field had FOUR states in production while every surface and every test of
    mine assumed three, and my fixtures faked legacy as None, which is exactly
    the value the real data does not have. A validation that only fires on
    writes says nothing about rows that were already there.

    NORMALIZE ON READ, NEVER REWRITE. The record keeps what was actually
    written — this ledger is event-sourced and a migration tag is a true fact
    about how the row arrived — while every consumer is handed the closed set.
    Anything outside ORIGINS is UNKNOWN, which is the honest reading of a
    provenance nobody witnessed as owner-or-agent, and it is the same answer
    an absent field gets because it is the same situation.

    Read-normalizing is also why no backfill is needed: a later migration that
    learns a row's real provenance can write it, and this stops answering
    UNKNOWN for that row without anything else changing."""
    value = row.get("origin")
    return value if value in ORIGINS else None


def _rank(row):
    """Sort key for `priority`. UNRANKED sorts AFTER every rank, never as P3.

    Nobody-has-judged-this and judged-lowest are different answers, and a
    default that renders the first as the second is the whole reason this
    field refuses an eighth spelling."""
    got = (row or {}).get("priority")
    return PRIORITIES.index(got) if got in PRIORITIES else len(PRIORITIES)


def rank_key(row):
    """PRIORITY FIRST, THEN THE NUMBERING — the ordering a board sorts by.

    PUBLIC FOR THE REASON `sort_key`'S OWN DOCSTRING RECORDS: a surface that
    needs an order and is given none binds to a private name, and the
    producer's silence is the failure. The owner's task card wants "most
    important first" and the only thing published was the numbering, so the
    order lives here rather than as a second PRIORITIES table in JavaScript.

    IT COMPOSES, NEVER REPLACES. `sort_key` still means "the numbering the
    fleet has in its head" and every reader of that keeps it; this is the
    other question. UNRANKED sorts after every rank (`_rank`'s law: nobody
    has judged this is not judged-lowest), so a triage debt collects at the
    bottom of a ranked board instead of being scattered through it."""
    return (_rank(row),) + tuple(sort_key(row))


def stamp_epoch(value):
    """THE ONE TIMESTAMP PARSER FOR THIS LEDGER -> epoch seconds, or None.

    PUBLIC BECAUSE EVERY AGE ON EVERY SURFACE MUST COME OUT OF IT. A second
    parser anywhere — a browser deciding that a stamp is a date above 1e9
    while this one accepts anything above 0 — is two answers to a question
    with one right answer, and the two disagree over a whole window of stamps
    while each renders a confident age from its own opinion. Clients FORMAT a
    number this function produced and nothing else.

    TWO SPELLINGS ARE REAL IN THIS LEDGER, and a reader that knows one of them
    silently ages half the backlog wrong: rows minted here carry a
    ``time.time()`` float, and the rows migrated out of the private list carry
    the ISO form (with or without a trailing Z, with or without an offset,
    with or without fractional seconds — all four are spellings of an
    instant, and refusing three of them would call a dated row undated).

    NONE, NEVER 0, AND NEVER A NUMBER THIS FUNCTION CANNOT STAND BEHIND:

      * a bool is NOT a stamp. ``float(True)`` is 1.0 and Python calls bools
        ints, so the plain numeric branch silently dated every ``True`` to
        one second after the epoch and rendered it as a confident 56-year
        age. The type check has to come first, because by the time the value
        is a float the evidence is gone.
      * NaN and infinity are not instants. ``float("inf") > 0`` is True, so
        a bare positivity test admits an infinite stamp and yields an age of
        minus infinity one subtraction later; NaN passes no comparison at all
        and slips through any ordering silently.
      * zero and negatives are not this ledger's stamps. "We do not know
        when" and "just now" are the two answers an age question must never
        confuse — the defect the burn-down card cured one surface up, where
        an undated row rendered as a fresh one.
    """
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        pass
    else:
        if seconds != seconds or seconds in (float("inf"), float("-inf")):
            return None          # NaN never equals itself; inf is not a date
        return seconds if seconds > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    # THE OFFSET IS PART OF THE INSTANT. `strptime` has no portable %z for
    # "+00:00" across the versions this runs on, so the offset is read off
    # the text and applied to the naive parse — dropping it would misdate a
    # row by up to half a day and call the answer certain.
    shift = 0
    m = re.search(r"([+-])(\d{2}):?(\d{2})$", text)
    if m:
        sign = -1 if m.group(1) == "+" else 1
        shift = sign * (int(m.group(2)) * 3600 + int(m.group(3)) * 60)
        text = text[:m.start()]
    elif text.endswith("Z"):
        text = text[:-1]
    text = text.split(".", 1)[0].replace(" ", "T")
    try:
        return float(calendar.timegm(
            time.strptime(text, "%Y-%m-%dT%H:%M:%S"))) + shift
    except (TypeError, ValueError):
        return None



# NOBODY HAS WRITTEN ON THIS ROW IN SEVEN DAYS is the backlog's own staleness
# line, and it lives HERE because the ledger owns it: the card's mark, the
# filter chip that selects those rows, and the API field they both read have
# to be one number or the chip selects rows that do not call themselves
# stale. A second spelling in JavaScript is exactly the copy this constant
# exists to prevent.
STALE_NOTE_S = 7 * 86400


def filed_epoch(row):
    """When the row was FILED -> epoch seconds, or None when unreadable."""
    return stamp_epoch((row or {}).get("ts"))


def last_note(row):
    """The newest comment on a row -> the comment dict, or None.

    PUBLIC BECAUSE A SURFACE ASKED, which is `sort_key`'s own rule: the owner's
    card renders how long it has been since anybody said anything on a row, and
    the alternative to publishing this is the browser deciding for itself which
    end of ``comments[]`` is newest — a second copy of this ledger's shape in
    JavaScript.

    THE APPEND ORDER IS THE ORDER. ``comment()`` appends, so the last element is
    the newest; re-sorting here would rank a comment whose stamp will not read
    as the oldest rather than leaving it where the record put it.
    """
    notes = [c for c in ((row or {}).get("comments") or ())
             if isinstance(c, dict)]
    return notes[-1] if notes else None


def noted_epoch(row):
    """When anybody last WROTE on the row: its newest comment, else its filing.

    A row nobody has commented on has not been untouched forever — it was
    filed, and that is when its clock started. The fallback is what makes "no
    note in seven days" answerable for a row carrying no notes at all, and that
    is most of the backlog.
    """
    note = last_note(row)
    got = stamp_epoch(note.get("ts")) if note else None
    return got if got is not None else filed_epoch(row)


def board_key(row):
    """RANK FIRST, THEN OLDEST FIRST — the order the owner's board sorts by.

    `rank_key` answers "most important first" and then falls through to THE
    NUMBERING, which is filing order only for rows minted here: a migrated row
    keeps the number it was born with, so inside one rank the numbering sorts
    by which private list a row came out of rather than by how long it has been
    waiting. The owner asked how he is supposed to cross-check the ordering of a
    massive list, and AGE is the part of that order he can check without
    reading a single row of prose.

    AN UNDATED ROW SORTS AFTER THE DATED ONES IN ITS RANK, never as the oldest:
    `stamp_epoch` answers None rather than 0 precisely so a stamp nobody could
    read cannot claim the top of the board. Its tie-break is `sort_key`, which
    is where such a row already was.

    IT COMPOSES, IT DOES NOT REPLACE. `sort_key` still means the numbering and
    `rank_key` still means rank-then-numbering for every reader of them; this is
    the third question, and a surface that wants it asks for it by name.
    """
    filed = filed_epoch(row)
    head = ((_rank(row), 1, 0.0) if filed is None
            else (_rank(row), 0, filed))
    return head + tuple(sort_key(row))


def board_order(rows_):
    """THE ONE SERVER ORDERING -> the rows as a list, board order.

    THE KEY WAS PUBLISHED AND THE ORDERING STILL FORKED. `board_key` existed,
    the browser route sorted by it, and `helm task list` went on sorting by
    `sort_key` — so the same snapshot came out of the API and out of the
    command line as two DIFFERENT lists, and the owner cross-checking a board
    against a terminal was comparing two orders and would have read the
    difference as a ledger change. A published key is an invitation each
    caller answers its own way; a published ORDERING is the answer, and it is
    what both doors call now.

    `rows_` may be the snapshot dict or any iterable of rows — the callers
    hold one or the other and neither should have to convert.
    """
    return sorted(rows_.values() if hasattr(rows_, "values") else rows_,
                  key=board_key)


def row_ages(row, now=None):
    """One row's clock, RESOLVED HERE -> {ts_epoch, age_s, noted_age_s, stale}.

    THE SERVER ANSWERS "HOW OLD", THE CLIENT ANSWERS "HOW TO SAY IT". Every
    field is a number or None, and None means UNKNOWN — never 0, never a
    string a browser would have to parse. A surface that receives this cannot
    invent certainty about a stamp it cannot read, because it never sees the
    stamp: the only thing it can render for None is the word.

    `stale` is a BOOL and never None: it answers "has this row gone seven days
    with nobody writing on it", and an undated row has not been PROVEN
    untouched, so unknown collapses to False rather than to a mark the reader
    would take as measured.
    """
    at = filed_epoch(row)
    noted = noted_epoch(row)
    now = time.time() if now is None else now
    noted_age = None if noted is None else max(0.0, now - noted)
    return {"ts_epoch": at,
            "age_s": None if at is None else max(0.0, now - at),
            "noted_age_s": noted_age,
            "stale": noted_age is not None and noted_age >= STALE_NOTE_S}


def queue_totals(rows_, now=None):
    """THE HEADLINE THE OWNER READS, COUNTED ON THE SERVER over `rows_`.

    IT IS HERE AND NOT IN THE BROWSER because the same line renders in two
    places — the board home's band and the work tab's card — and a count
    computed twice over two arrivals of one payload is two versions of the
    queue waiting to disagree. One read produces one `queue`, both cells
    render it, and neither can hold a number the other does not.

    CLOSED ROWS ARE HISTORY and are not counted: the question is what is
    still in play. `live` is every row still in play, which is deliberately
    NOT the route's `counts.open` (status=="open" alone, with in_progress
    reported beside it) — both numbers are true and they are named
    differently so nothing has to guess which is which.

    AN UNDATED ROW CANNOT BE THE OLDEST. It counts in its rank and it is
    skipped for the oldest-age answer, because `stamp_epoch` answers None
    rather than 0 precisely so a stamp nobody could read cannot claim the top
    of this headline.
    """
    now = time.time() if now is None else now
    t = {"P0": 0, "P1": 0, "P2": 0, "P3": 0, "unranked": 0, "in_progress": 0,
         "live": 0, "oldest": {"P0": None, "P1": None}}
    for row in (rows_.values() if hasattr(rows_, "values") else rows_):
        if not isinstance(row, dict) or row.get("status") == "closed":
            continue
        t["live"] += 1
        if row.get("status") == "in_progress":
            t["in_progress"] += 1
        rank = row.get("priority")
        if rank in PRIORITIES:
            t[rank] += 1
        else:
            t["unranked"] += 1
        if rank not in ("P0", "P1"):
            continue
        at = filed_epoch(row)
        if at is None:
            continue
        age = max(0.0, now - at)
        if t["oldest"][rank] is None or age > t["oldest"][rank]:
            t["oldest"][rank] = age
    return t


# ---------------------------------------------------------------------------
# the ask reflex — a new title resolved against the rows already filed
# ---------------------------------------------------------------------------

# THE OWNER'S SECOND QUESTION, in his words: "how do we prevent me continually
# asking for things that have been placed on the list and ignored". A backlog of
# 477 open rows cannot be scanned by the person filing row 478 — not by him and
# not by a seat — so the LEDGER has to do the resolving at the door where the
# duplicate would be born. This file's own opening paragraph records the same
# failure from the other side: a seat "filed a duplicate because the class was
# unlookupable".
#
# THE SHAPE IS THE TYPED STORE'S, DELIBERATELY (helm/store/index.py: `_tokens`,
# `_near_dup`, `DUP_OVERLAP` — token-set Jaccard, strongest match named). One
# similarity idiom for one repo; a second hand-rolled one beside it is the
# two-surfaces disease this ledger keeps curing in its own fields.
#
# AND IT REFUSES WHERE THE STORE ONLY WARNS. The store warns because two entries
# may legitimately hold one piece of knowledge from two angles. A SECOND TASK
# for one piece of work is never legitimate: it splits the notes, the owner and
# the rank across two rows, which is exactly how a thing lands on the list and
# is then ignored. `--force-new` is the operator saying the overlap is a
# coincidence, and it is a bool flag because the answer is one bit — the
# same flag `helm drain --force-new` already means, so the repo keeps one
# spelling for "file it anyway over a near-duplicate".
#
# THE THRESHOLD IS MEASURED AGAINST THE REAL LEDGER, NOT CHOSEN. Run over
# the live backlog at 1837 open rows, every row scored against its nearest
# neighbour:
#
#     0.5 -> 211 rows refused      0.7 ->  34 rows refused
#     0.6 -> 116 rows refused      0.8 ->  28 rows refused
#
# and the pairs 0.6 refuses that 0.8 lets through are NOT duplicates —
# "Bind task 872 round-two verdict" against "round-six verdict";
# "Re-review binding provenance successor" against "Re-review empty
# binding successor". Titles are short, so a token overlap over three
# content words reaches 0.67 on a pair differing by the only word that
# matters. At 0.8 the refused population is identical titles and word-order
# variants of one ("Run focused verification and review diff" against
# "...and diff review"), which is exactly what this guard exists to stop.
# It lands on the STORE's own number, and IT IS THAT NUMBER rather than a copy
# of it: the name below is bound from `store.index.DUP_OVERLAP`, so the repo
# holds ONE similarity threshold and retuning the store's moves this door with
# it. The first cut wrote the literal here and claimed the same sentence, which
# is two constants that merely agree today.
#
# RE-MEASURED AT 1855 OPEN ROWS, AND THE ANSWER SPLIT IN TWO.
# 0.8 still refuses 28 rows across 17 pairs — and every one of those rows is
# MACHINE-minted: a `harness-mirror:` subject or a `task/resume-turn-*`
# recovery row, both of which reach `add()` with force_new=True and never
# meet this threshold at all. Of the 915 open rows a person or an agent
# typed through this door, ZERO have a neighbour at 0.8. So the population
# this guard refuses in production today is EMPTY, and the population it
# would refuse if the machine legs went through it is 14 exact-title pairs
# plus 3 at exactly 0.80 where one qualifying word carries the distinction
# ("Review owner posture redesign" against "Re-review owner-posture
# redesign"; "Review cured-bucket successor" against "...parity successor").
# Those three name the residual honestly: a short `Re-review X` follow-up is
# legitimate work and lands ON the threshold, which is what `--force-new` is
# for and what the refusal names. The zero has its controls — a verbatim
# re-file of a real open row is refused at 100%, an invented title matches
# nothing — because an instrument with no input also reports zero.
#
# THAT MEASUREMENT EXPIRES. Both of them describe a ledger of a particular
# shape; re-run before moving this number, because a backlog of another
# shape refuses a different population — and re-run the SPLIT too, since a
# producer that stops passing force_new moves rows into the door's own
# population without touching the threshold at all.
DUP_OVERLAP = dupindex.DUP_OVERLAP
DUP_SHOW = 3

# TITLES ARE SENTENCES, so their token sets are dominated by grammar: "the",
# "a", "to", "of" and "is" overlap between any two rows in this ledger. Without
# this set the threshold would be tuned against English rather than against the
# work, and the store's 0.8 on statements would be unreachable here for a real
# duplicate. Counted words only — a stoplist that ate a content word would hide
# the very difference the operator is trying to state.
#
# "NOT" IS A CONTENT WORD AND WAS IN THIS LIST. Dropping it made a title and
# its NEGATION tokenize identically — "the wall verdict anchors" against "the
# wall verdict does not anchor" — so the door refused the row that states the
# OPPOSITE of one already filed as a copy of it, which is the worst thing a
# duplicate guard can do: it deletes the correction. Polarity words stay.
TITLE_STOPWORDS = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "in", "into", "is", "it", "its", "of", "on", "or", "so",
    "than", "that", "the", "then", "this", "to", "we", "when", "with"))


def title_tokens(text):
    """A title's comparable words, or an EMPTY SET when it has none.

    ONE TOKENIZER, IMPORTED. The split is `store.index.dup_tokens` — the
    repo's canonical duplicate tokenizer, unicode-aware — and the only thing
    this field adds is its own grammar list. The first cut re-spelled the
    store's ASCII `[^a-z0-9]+` here, which silently dropped every non-Latin
    character: two byte-identical CJK titles both tokenized empty, scored
    0.0 and were called distinct.

    EMPTY MEANS THE QUESTION CANNOT BE ANSWERED, NOT THAT THE ANSWER IS NO.
    A title that is punctuation, a title that is nothing but grammar, and a
    title that is not a string at all all land here — and every caller reads
    the empty set as UNKNOWN rather than as "no duplicate". It never raises:
    the first cut called `.lower()` on whatever it was handed, so a non-string
    title put an AttributeError traceback on a user's `helm task add`.
    """
    return dupindex.dup_tokens(text, TITLE_STOPWORDS)


def near_duplicates(title, rows, limit=DUP_SHOW):
    """The OPEN rows whose titles share the most words with `title` ->
    [(row, overlap)], strongest first, at most `limit` long.

    OPEN ROWS ONLY, AND THAT IS THE QUESTION'S SCOPE. "Is this already on the
    list" is a question about work somebody could still be doing. A closed row
    with the same title is history, and refusing a new task because the same
    thing was finished months ago would be the opposite of useful.

    IT RANKS, IT DOES NOT DECIDE — `duplicate_verdict` is the decision, and it
    is the ONE decision every door makes. Every caller gets the overlap back,
    so the listing a refusal prints and the threshold it applied read the SAME
    numbers, and a probe can ask for the three neighbours of a title without a
    write door anywhere near it.

    A ROW WHOSE OWN TITLE WILL NOT TOKENIZE IS SKIPPED, NEVER FATAL. This walks
    a ledger other code wrote; one malformed row must not take down the
    resolve for the whole backlog.
    """
    want = title_tokens(title)
    if not want:
        return []
    scored = []
    for row in (rows.values() if hasattr(rows, "values") else rows):
        if not isinstance(row, dict) or row.get("status") not in OPEN_STATUSES:
            continue
        other = title_tokens(row.get("title"))
        if not other:
            continue
        overlap = len(want & other) / float(len(want | other))
        if overlap > 0:
            scored.append((overlap, row))
    # STRONGEST FIRST, AND THE TIE-BREAK IS THE BOARD'S OWN ORDER rather than
    # whatever order the snapshot dict happened to iterate in: two rows at the
    # identical overlap must print in the same order on every run, or the
    # operator cannot tell a re-run from a change in the ledger.
    scored.sort(key=lambda pair: (-pair[0],) + tuple(board_key(pair[1])))
    return [(row, overlap) for overlap, row in scored[:max(0, int(limit))]]


def duplicate_verdict(title, rows, project=None):
    """THE ONE DUPLICATE DECISION -> (verdict, near).

    verdict is "distinct", "duplicate" or "unknown"; `near` is the
    `near_duplicates` listing behind it, so whatever a caller prints and
    whatever it decided came out of one call and cannot disagree.

    THREE ANSWERS, NOT TWO, AND THAT IS THE WHOLE POINT. The first cut had
    two and folded "I could not resolve this" into "go ahead" — a
    stopword-only title, a title of punctuation, a title that was not a
    string, every one of them scored nothing against everything and was
    filed as proven-distinct. UNKNOWN is its own answer and the door refuses
    on it, because a guard that opens whenever it is confused is a guard
    against nothing.

    SCOPED THE WAY THE BOARD IS SCOPED when `project` is given: that
    project's rows plus the UNSCOPED legacy bucket, which is the population
    `/api/tasks` renders and `helm task list` prints. A row in ANOTHER
    project with a similar title is not this row filed twice, and refusing
    against it would make one checkout decide another team's backlog.
    """
    if not title_tokens(title):
        return "unknown", []
    pool = [r for r in (rows.values() if hasattr(rows, "values") else rows)
            if not project or project_of_row(r) in (project, None)]
    near = near_duplicates(title, pool)
    if near and near[0][1] >= DUP_OVERLAP:
        return "duplicate", near
    return "distinct", near


def _dup_listing(near):
    """The closest rows as lines — id, owner and score, strongest first."""
    return "".join(
        "\n  %-3d%% %-12s %-9s %s"
        % (round(overlap * 100), hit.get("id") or "?",
           hit.get("owner") or "UNOWNED", hit.get("title") or "")
        for hit, overlap in near)


# The override, named once. The library parameter and the command-line flag
# are two spellings of one bit, and every refusal below names BOTH — a
# refusal that advertises only the flag is useless to the script that hit it,
# and one that names only the parameter is useless to the person at the
# terminal. `helm drain --force-new` already means "file it anyway over a
# near-duplicate", so the repo keeps one spelling for one act.
_FORCE_SPELLING = "force_new=True (`--force-new` on the command line)"


def duplicate_refusal(verdict, near):
    """The sentence EVERY door prints when `duplicate_verdict` refuses, or None.

    ONE WORDING FOR ONE RULE. Two doors writing their own refusal is how the
    CLI came to say something the direct producer never said at all.
    """
    if verdict == "duplicate":
        hit, overlap = near[0]
        return ("REFUSED — %s already says %d%% of this, and a second row for "
                "one piece of work splits its notes, its owner and its rank. "
                "NOTHING WAS FILED. Comment on it (`helm task comment %s "
                "<text...>`), update it, or re-file with %s if the overlap is "
                "a coincidence. The closest OPEN rows to this title:%s"
                % (hit.get("id") or "?", round(overlap * 100),
                   hit.get("id") or "?", _FORCE_SPELLING, _dup_listing(near)))
    if verdict == "unknown":
        return ("REFUSED — this title has no comparable words, so whether it "
                "is already on the list is UNKNOWN and NOTHING WAS FILED. A "
                "title made only of grammar, punctuation or no string at all "
                "cannot be resolved against a backlog, and a guard that opens "
                "whenever it is confused guards nothing. Give it words, or "
                "re-file with %s to file it unresolved." % _FORCE_SPELLING)
    return None


def _story_order(rows_):
    """[(row, indent)] — parents before their children, children indented.

    THE FIELD IS NOT THE FEATURE. `continues` stores which row a row is a
    subissue of, and a parent link nothing renders is a parent link nobody
    sorts by — the same shape as a provenance field with no glyph, which is
    how rows came to smuggle their provenance into their titles. This is the
    reader that makes the field mean something, and it is why the field and
    this function belong in one change rather than two.

    SCOPE IS THE SHOWN SET, DELIBERATELY. Depth is computed only over rows
    this listing is actually rendering, so a child whose parent is filtered
    out or lives in another project renders at top level. The alternative —
    resolving parents outside the view — would indent a row under something
    the reader cannot see, which reads as a rendering bug rather than as
    scope.

    NOTHING IS DROPPED, AND THAT CLAUSE IS LOAD-BEARING. A ring inside the
    shown set makes every member somebody's child, so a naive roots-first walk
    emits NONE of them and the rows vanish from a listing that counted them —
    a silent drop, in the renderer for the field whose own validator refuses
    rings. Anything the walk did not reach is emitted flat at the end, so the
    worst a corrupt chain costs is indentation, never visibility."""
    known = {r.get("id"): r for r in rows_ if type(r.get("id")) is str}

    # THE RENDERER INHERITS THE WALK'S OBLIGATION. _story_chain ends on a
    # non-string link precisely so no reader crashes on rows older helms or
    # non-CLI callers wrote — and this reader then asked `cid in seen` with
    # whatever the row carried, so a single list-valued id raised TypeError
    # out of the listing for the whole ledger. A row with a non-string id can
    # never be a parent (kids is keyed by string ids only); it needs identity
    # for the visited-sets alone, so it gets the one identity every object
    # has. Mixing ints from id() with str keys in one set is safe: the two
    # types never compare equal.
    def _seen_key(row):
        rid = row.get("id")
        return rid if type(rid) is str else id(row)

    kids, roots = {}, []
    for r in rows_:
        parent = r.get("continues")
        if type(parent) is str and parent in known and parent != r.get("id"):
            kids.setdefault(parent, []).append(r)
        else:
            roots.append(r)
    # A STORY RANKS BY ITS BEST MEMBER, so a P0 subissue lifts its whole story
    # rather than sorting below unranked work its parent happens to carry.
    def story_rank(root):
        best, stack, seen = _rank(root), [root], set()
        while stack:
            cur = stack.pop()
            ck = _seen_key(cur)
            if ck in seen:
                continue
            seen.add(ck)
            best = min(best, _rank(cur))
            cid = cur.get("id")
            if type(cid) is str:
                stack.extend(kids.get(cid, ()))
        return best
    roots.sort(key=story_rank)
    out, emitted = [], set()
    for root in roots:
        stack = [(root, 0)]
        while stack:
            row, depth = stack.pop()
            rk = _seen_key(row)
            if rk in emitted:
                continue
            emitted.add(rk)
            out.append((row, "  " * depth))
            rid = row.get("id")
            if type(rid) is str:
                for kid in sorted(kids.get(rid, ()), key=_rank, reverse=True):
                    stack.append((kid, depth + 1))
    for r in rows_:
        if _seen_key(r) not in emitted:
            out.append((r, ""))
    return out


def _fmt(row, indent="", project_col=False):
    st = row.get("status") or "?"
    mark = {"open": " ", "in_progress": ">", "closed": "x"}.get(st, "?")
    # A STAND-DOWN EARNS THE GLYPH COLUMN because that column answers "what
    # state is this row in for a reader scanning the list", and a row the
    # offer rung will never hand out is in a different state from one it
    # will, whatever its status says. Without this the field is enforced and
    # INVISIBLE — the offer quietly skips the row, the listing shows an
    # ordinary open item, and nobody can see the backlog silently shrinking.
    # An EXPIRED stand-down deliberately renders as ordinary: it no longer
    # blocks, so claiming otherwise would be the surface lying the other way.
    if standdown_blocks_offer(row):
        mark = "~"
    # THE CONSTANTS, not two more string literals — this renderer is where the
    # word came from, so it is the one place that must not spell it separately.
    owner = owner_of(row) or (CLOSED_OWNER_DISPLAY if st == "closed"
                              else UNOWNED_DISPLAY)
    # THE GLYPH IS THE POINT OF THE FIELD. A provenance nothing renders is a
    # provenance nobody sorts by, which is how 16 rows ended up smuggling it
    # into titles. "@" marks owner-asked work; a declared agent row and an
    # UNKNOWN legacy row both stay blank, because the distinction that earns
    # a mark here is "did the owner ask for this", not "is it classified".
    origin = "@" if origin_of(row) == "owner" else " "
    # THE LABEL IS OPT-IN: a scoped listing is one project by construction and
    # a column repeating the scope on every line would be noise; the
    # cross-project view is exactly where a reader cannot tell rows apart, so
    # that is where each row says whose it is. UNSCOPED is printed, not blank
    # — an unlabeled row in a labeled listing reads as belonging to whatever
    # the eye was on.
    proj = ""
    if project_col:
        proj = "%-14s " % ("[%s]" % (project_of_row(row) or "UNSCOPED"))
    # THE RANK COLUMN, AND ITS BLANK IS A STATE. A ranked row prints its
    # rank; an UNRANKED row prints SPACES rather than a word, for the reason
    # `_rank` states — nobody-has-judged-this is not judged-lowest, and any
    # filler that looks like a value would be the eighth spelling this field
    # exists to refuse. Same shape as the origin glyph two lines up, which
    # leaves a declared-agent row and an unknown legacy row equally blank.
    rank = row.get("priority")
    rank = rank if rank in PRIORITIES else "  "
    return "%s%s%s %-2s %-12s %-11s %-16s %s%s" % (
        indent, mark, origin, rank, row.get("id"), st, owner[:16], proj,
        (row.get("title") or "")[:96])


def _free_text(verb, words, what):
    """(text, None) or (None, rc) — a free-text tail, or a refusal.

    THE SHAPE IS THE SAME WHEREVER A VERB JOINS A TAIL, so the scan and the
    teaching live in `helm.freetext` and this only supplies the vocabulary. A
    flag the verb does not implement is indistinguishable from the text itself
    once both are joined, so an unrecognised `--token` is stored AS the record
    while the verb prints its ordinary success line. Measured on `comment`: 24
    bodies on 15 rows lost behind three different flags over two months, every
    author having read "noted on task/NNNN".

    BOTH HALVES SHIP TOGETHER OR NEITHER DOES. The refusal alone turns a
    corrupted record into a command with no way through, so `--` is the escape
    and the message TEACHES it. `close` had neither: `close ID --reason <text>`
    stored "--reason <text>", and `close ID -- <text>` stored "-- <text>", so
    an operator who learned the idiom from the cured verb corrupted the record
    on this one.

    `what` names the tail in the verb's own vocabulary -- "comment text", "a
    close reason" -- because a refusal that cannot say what it wanted is a
    refusal the reader has to guess at. THE ARITY STAYS WITH THE CALLER: an
    empty tail returns `(None, None)` and each verb answers for itself whether
    that is allowed."""
    return freetext.tail("helm task", verb, words, what)


def cmd_task(args):
    """task add|list|triage|show|resolve|claim|takeover|close|comment."""
    from . import seats

    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]

    if verb == "mirror":
        # THE LOOP'S HAND CRANK. A standing cadence runs this unattended; the
        # verb exists so a human can SEE what the loop would do before
        # trusting it, which is how the 628-vs-29 discrepancy was found before
        # a row was written rather than after the board was doubled.
        from . import cli, tasksmirror
        # A JUNK TAIL MUST NEVER REACH AN --apply. `helm task mirror --bogus
        # --apply` used to be a silent write; the honesty contract is that once
        # a subverb matches, every remaining token is a KNOWN flag or the call
        # refuses at exit 2 BEFORE any work runs. This verb writes to the fleet
        # ledger, so it is exactly the shape that rung exists for.
        rc = cli.guard_tail("helm task mirror", rest,
                            flags=("--apply", "--all-statuses", "--json",
                                   "--ensure-timer"))
        if rc is not None:
            return rc
        if "--ensure-timer" in rest:
            # BUILT OWES WIRED. `ensure_timer` existed with no caller, which
            # means the STANDING loop would never have stood — the cadence was
            # a function nobody ran. This is the door that installs it, and the
            # doctor rung below says so when it has not been used.
            ok, detail = tasksmirror.ensure_timer()
            print("helm task mirror: %s" % detail,
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 1
        apply = "--apply" in rest
        rep = tasksmirror.sweep(apply=apply,
                                live_only="--all-statuses" not in rest)
        # THE REFUSAL IS DECIDED BEFORE THE RENDERER IS CHOSEN, because rc is
        # the half every reader shares. An earlier cut put this AFTER the
        # --json branch, so the structured path returned 0 over an unreadable
        # dedup source — while the comment below said, correctly, that rc 0 on
        # a zero-import run reads as "nothing to do". The rule and its
        # exception were three lines apart (task/2378).
        #
        # NOTHING WAS IMPORTED AND THAT IS THE HEADLINE. Without the dedup
        # source every ref misses, so a sweep that ran anyway would re-file
        # every row it has ever filed. It refuses instead, and the refusal is
        # rc 1 because rc 0 on a zero-import run reads as "nothing to do" —
        # the exact sentence that is false here.
        #
        # A MACHINE READER STILL GETS THE WHOLE REPORT. A structured answer and
        # a truthful exit code are not in tension: --json prints `rep`, which
        # carries `dedup_unreadable`, and exits 1 beside it.
        refused = rep.get("dedup_unreadable")
        if "--json" in rest:
            print(json.dumps(rep, indent=1, ensure_ascii=False))
            return 1 if refused else 0
        if refused:
            print("helm task mirror: REFUSED — the dedup source could not be "
                  "read (%s), so every already-imported row would look NEW "
                  "and this sweep would duplicate all of them. Nothing was "
                  "imported. Repair the task ledger, then re-run."
                  % refused, file=sys.stderr)
            return 1
        head = "imported" if apply else "would import"
        print("helm task mirror: %s %d row(s) from %d session(s) in %d "
              "harness home(s)" % (head, len(rep["imported"]), rep["sessions"],
                                   rep["homes"]))
        if rep["imported"]:
            # THE SWEEP REPORT IS NEVER SCOPE-FILTERED — this is the one
            # FLEET-WIDE loop, and a report that hid what it wrote into other
            # projects would be a silent write. It LABELS instead: each
            # import named by the project routing that carried it (cwd-slug
            # namespace, the sweep's own vocabulary; "none" = routed nowhere).
            byproj = {}
            for it in rep["imported"]:
                key = it.get("project") or "none"
                byproj[key] = byproj.get(key, 0) + 1
            print("  projects: " + ", ".join(
                "%s %d" % kv for kv in sorted(byproj.items())))
        if rep["already"]:
            print("  %d already mirrored — the dedup key is in each row's refs,"
                  " so re-running is safe" % rep["already"])
        if rep["closed_unmirrored"]:
            print("  %d finished scratchpad task(s) NOT imported by scope "
                  "ruling — they are counted, not lost: `--all-statuses` "
                  "carries them" % rep["closed_unmirrored"])
        for u in rep["unrouted"]:
            print("  session %s routes to NO project — imported with "
                  "project:none rather than dropped" % u)
        for b in rep["unreadable"]:
            print("  UNREADABLE %s — reported, never silently skipped" % b)
        for r in rep["refused"]:
            print("  refused %s — %s" % (r.get("id", "?"), r["why"]))
        # THE STATUS LEG (task/1738): every mirrored row whose source item
        # finished, counted in the dry run exactly as --apply would act.
        for line in tasksmirror.render_finished(rep["finished"], apply):
            print(line)
        if not apply:
            print("  nothing was written — re-run with --apply")
        return 0

    if verb == "add":
        # `--` ENDS THE FLAGS AND THE REST IS LITERALLY THE TITLE. Without it
        # a title that legitimately begins with a dash is INEXPRESSIBLE — the
        # leading-flag guard below refuses it and there is no way to say "I
        # meant that". A guard with no escape hatch is a guard that eventually
        # gets deleted by someone who needed the thing it forbade.
        #
        # AND IT COVERS TITLES ONLY — NEVER A FLAG VALUE. The earlier cut of
        # this comment claimed `--` was the escape hatch for the whole verb,
        # which is false and was MEASURED false: `--note -- --force`,
        # `--note --force` and (then) `--note=--force` all failed, so a note
        # reading `--force`, or the ordinary note `-5`, could not be filed by
        # any spelling. `--` cuts the tail off the TITLE; it never reaches the
        # value of a flag consumed to its left. The escape hatch for a VALUE
        # is `--note=VALUE`, which _take now honours literally.
        literal = []
        if "--" in rest:
            cut = rest.index("--")
            literal = rest[cut + 1:]
            rest = rest[:cut]
        # WHAT THE OPERATOR ACTUALLY TYPED, snapshotted before any consumption:
        # `_take` now leaves a flag-shaped value in place rather than eating
        # it, so the only way to tell "never passed" from "passed with nothing
        # usable" is to remember the original.
        typed = set(rest)
        # A SET CANNOT COUNT, AND THE DUPLICATE CHECK BELOW NEEDS TO. `--note
        # one --note two` puts `--note` in argv TWICE and in `typed` ONCE, so
        # asking the set how many times a flag was typed answers 1 for every
        # duplicate — the refusal still fires, but it names the wrong reason
        # and sends the operator hunting a typo in a flag they spelled right
        # both times. The ordered copy is the only thing that remembers.
        typed_seq = list(rest)
        # POSITION DECIDES, NOT MEMBERSHIP (task/1451). Only the leading run
        # and a clean trailing run are option territory; the middle is title
        # and its tokens are never consumed. Anything left unconsumed in the
        # option runs is rejoined IN ITS ORIGINAL ORDER so the guard below
        # names the token the operator actually typed first.
        _lead, _middle, _tail = _option_span(rest)
        opts = _lead + _tail
        owner = _take(opts, "--owner")
        note = _take(opts, "--note")
        tid = _take(opts, "--id")
        posture_na = _take(opts, "--posture-na")
        # THE SCOPE OVERRIDE, CONSUMED WITH THE REST (task/2446). It is
        # in _VALUED_FLAGS too, so `--project helm` is option territory
        # and its value can never be joined into the title.
        project_opt = _take(opts, PROJECT_FLAG)
        # RAW FIRST, COERCED SECOND. The coercion maps a VALUELESS flag onto
        # None, which is byte-identical to never passing it — so a bare
        # `--continues` filed a parentless row and reported SUCCESS
        # The guard below needs the UNCOERCED answer to tell "not
        # passed" from "passed with nothing", the same snapshot-before-
        # consumption rule every flag above already follows.
        # RAW FIRST, COERCED SECOND, BOTH FROM THE TABLE. The coercion maps a
        # VALUELESS flag onto None, which is byte-identical to never passing
        # it, so a bare `--continues` filed a parentless row and reported
        # SUCCESS. The guard below needs the UNCOERCED answer to tell "not
        # passed" from "passed with nothing" — the same
        # snapshot-before-consumption rule every flag above already follows.
        raw_story, story = {}, {}
        for _flag, _key, _coerce in STORY_FIELDS:
            raw_story[_flag] = _take(opts, _flag)
            story[_key], _cerr = _coerce(raw_story[_flag] or "")
            if _cerr:
                print("helm task: %s %s" % (_flag, _cerr), file=sys.stderr)
                return 2
        continues, priority = story["continues"], story["priority"]
        refs = _take_all(opts, "--ref")
        mine = "--mine" in opts
        if mine:
            opts.remove("--mine")
        owner_asked = "--owner-asked" in opts
        if owner_asked:
            opts.remove("--owner-asked")
        # CONSUMED HERE WITH ITS TWO SIBLINGS, above the stray-token guard,
        # for the reason that guard's own comment block records: a flag
        # removed BELOW it is refused as "not a title" on a command that is
        # perfectly valid, which is the exact bug `--owner-asked` shipped.
        force_new = "--force-new" in opts
        if force_new:
            opts.remove("--force-new")
        # REBUILD BY POSITION, NEVER BY VALUE (worse-than-main).
        # My first cut filtered typed_seq by membership in the middle and the
        # leftover options, which MATCHES ON TOKEN TEXT — so a duplicate word
        # bound to the wrong occurrence and the title came out REORDERED:
        # `--owner seat-a investigate seat-a` filed the title "seat-a
        # investigate". The middle is never consumed, so its tokens keep their
        # exact positions; only the option runs can lose members, and a member
        # is dropped at its FIRST unconsumed occurrence WITHIN those runs,
        # which is `_take`'s own rule rather than a second one beside it.
        _opt_idx = list(range(len(_lead))) + list(
            range(len(typed_seq) - len(_tail), len(typed_seq)))
        _gone = list(_lead + _tail)
        for t in opts:                    # what survived consumption
            if t in _gone:
                _gone.remove(t)
        _keep = []
        for i in _opt_idx:
            tok = typed_seq[i]
            if tok in _gone:
                _gone.remove(tok)         # consumed — drop this position
            else:
                _keep.append(i)
        _mid_lo = len(_lead)
        _mid_hi = len(typed_seq) - len(_tail)
        _keep += list(range(_mid_lo, _mid_hi))
        rest[:] = [typed_seq[i] for i in sorted(_keep)]
        # EVERY KNOWN FLAG IS CONSUMED ABOVE THIS LINE. That ordering is the
        # guard's whole correctness condition, so it is stated rather than
        # implied: anything still starting with "-" in first position was
        # never recognised, and would otherwise become the row's permanent
        # NAME. task/335 is a real ledger row titled `--help`, filed by an
        # agent that wanted the usage line, which somebody then had to
        # disclose and close. `helm chat post` already polices leading
        # option positions; this door did not.
        #
        # AND THE ORDERING IS NOT THEORETICAL — my first cut put this check
        # ABOVE the --owner-asked removal and it REFUSED
        # `task add --owner-asked "real title"`, a valid command, while the
        # same words in the other order worked (an exact remote probe showed
        # it, rc2 vs rc0). A guard that runs before its
        # inputs are complete does not protect the door, it breaks it.
        #
        # EVERY TOKEN, NOT THE FIRST — and this comment used to say the
        # opposite, using an example the code now refuses. It claimed "a title
        # legitimately contains flag-shaped words further in (fix the --force
        # bypass)"; measured 2026-08-25, that exact string returns rc 2. The
        # widening below at "row e37b25f9" superseded this sentence
        # and nobody updated it, and it cost a builder a set of arms written
        # to the wrong contract. A stale comment beside live code is a defect.
        # A VALUED FLAG THAT PRODUCED NOTHING IS A TYPO, NOT A DEFAULT.
        # `--note --owner cj` set note to "--owner" and
        # eat the owner as well: one typo, two wrong fields, rc 0, and a row
        # claiming what nobody asked for. A trailing `--note` vanished the
        # same way. Refusing is the only answer that does not file a lie.
        #
        # THE REFUSAL NOW CARRIES THE ESCAPE, because that is the whole
        # difference between a guard and an obstruction. It fires on exactly
        # the two readings a space cannot separate — "I typo'd" and "I meant
        # a dash value" — and only the operator knows which, so it must hand
        # back the spelling that says the second one. It did not, and the
        # review that measured that (`--note -5`, an ordinary note, refused
        # with no way to mean it) was right to call the guard deletable.
        # `--owner=` and `--id=` say it too: one door, one rule.
        # `--ref` BELONGS IN THIS LIST AND WAS MISSING (row
        # e2acaae0): `task add REF-PROBE --ref` returned rc 0 with refs=[].
        # `_take` CONSUMES a bare trailing flag while returning None — that is
        # how the space form refuses without leaving a half-parse behind — and
        # the three singleton flags below are caught by exactly this check.
        # The repeated flag had no equivalent, so its valueless form vanished
        # silently instead of refusing. `typed` still holds the token, which
        # is the whole reason this check can see a flag that `rest` no longer
        # contains.
        #
        # MY FIRST FIX FOR THIS WAS WRONG AND THE PROBE CAUGHT IT. I added a
        # break to `_take_all` on the theory that the flag was LEFT in `rest`;
        # it is not, `_take` deletes it, so the branch never fired and the
        # defect survived a green edit. Reusing the mechanism that already
        # works beats inventing a second one beside it.
        # SCOPED TO THE OPTION RUNS, NOT THE WHOLE ARGV (task/1458).
        # `typed` is every token the operator wrote, so a valued flag sitting
        # INSIDE THE TITLE matched here and was reported as a flag that needs a
        # value — advising `--owner=VALUE` to somebody who typed the word as
        # prose. A token the title owns was never a flag, so it must reach the
        # stray guard below, which names the whole-title escape that actually
        # fixes it. `typed` still drives the DUPLICATE reading in that guard,
        # because a duplicate is a real flag given twice wherever it appears.
        typed_opts = [t for t in typed if t in _lead or t in _tail]
        valueless = [f for f, v in (("--owner", owner), ("--note", note),
                                    ("--id", tid),
                                    ("--posture-na", posture_na),
                                    (PROJECT_FLAG, project_opt),
                                    ("--ref", refs or None))
                     + tuple((flag, raw_story[flag])
                             for flag, _k, _c in STORY_FIELDS)
                     if f in typed_opts and v is None]
        if valueless:
            print("helm task: %s needs a value that is not another flag — "
                  "nothing was filed. If the value really starts with a dash, "
                  "write it as %s=VALUE.\n%s"
                  % (", ".join(valueless), valueless[0], USAGE),
                  file=sys.stderr)
            return 2
        # EVERY REMAINING TOKEN, NOT JUST THE FIRST (row e37b25f9).
        # This checked `rest[0]` only, so a flag AFTER a title token survived
        # into the join. Exact probe: `task add PROBE --note=one --note=two`
        # returned rc 0 with note="one" and title="PROBE --note=two". `_take`
        # consumes only the FIRST occurrence of a singleton flag and leaves
        # the rest in place, so a DUPLICATE is just an unconsumed known flag —
        # which is why this is one check rather than a special case for
        # duplicates. It also ends a divergence between the doors: `update`
        # already rejected leftovers anywhere, so the two doors disagreed
        # about the same argv.
        #
        # THE LITERAL-TITLE ESCAPE IS UNTOUCHED: anything after `--` is in
        # `literal`, never in `rest`, so a title that really starts with a
        # dash still files.
        stray = next((t for t in rest if t.startswith("-")), None)
        if stray is not None:
            # WHICH KIND OF STRAY IT IS, and `typed` holds RAW TOKENS rather
            # than flag names — `--note=one` and `--note=two` are two distinct
            # members — so the base name has to be split off BOTH sides before
            # counting. Comparing the stray against `typed` directly reported
            # a duplicate as "not recognised", which is a true refusal wearing
            # the wrong reason: the operator would go looking for a typo in a
            # flag they spelled correctly twice.
            base = stray.split("=", 1)[0]
            dup = sum(1 for t in typed_seq
                      if t.split("=", 1)[0] == base) > 1
            # THE MESSAGE MUST COVER THE MID-TITLE CASE, which is the one an
            # operator actually hits (integrator ruling, task/1451).
            # It used to explain `--` only as the escape for a title that
            # STARTS with a dash, so somebody who typed an unquoted title with
            # a flag word in the MIDDLE was told the token "is not a title"
            # and given no reason to reach for the escape that would fix it.
            known = _is_flag(stray, _BOOL_FLAGS) or any(
                stray.split("=", 1)[0] == f for f in _VALUED_FLAGS)
            why = ("that flag was already given once and this verb takes it "
                   "at most once" if dup else
                   "that flag is only read before the title or after it, and "
                   "this one sits inside it" if known else
                   "that flag was not recognised")
            print("helm task: %r is not a title — %s, so it would have become "
                  "part of the row's name. Put `--` before the whole title to "
                  "take it literally (a title that starts with a dash, or one "
                  "with a flag word inside it), or --flag=VALUE for a flag "
                  "value that starts with a dash.\n%s"
                  % (stray, why, USAGE), file=sys.stderr)
            return 2
        title = " ".join(rest + literal).strip()
        # THE POSTURE GUARD (task/1346, helm/posture.py) lives in add() —
        # the invariant, not this door. The CLI forwards --owner-asked and
        # --posture-na and renders add()'s refusal at exit 2 below.
        # FILING IS NOT OWNING, and defaulting otherwise silently emptied the
        # offer queue. The first cut here did `owner or seats.own_name()`, so
        # every row a real seat filed came out OWNED, so the POOL — the rows
        # an idle seat can take without coordinating — was empty forever, and
        # `mine` never appeared on any offer —
        # the exact failure add()'s comment block says this design exists to
        # avoid, rebuilt in the only door to it. Caught by an adversarial
        # reviewer, not by me and not by the tests.
        #
        # So: UNOWNED unless someone says otherwise. `--mine` is the shortcut
        # for taking it yourself; the filer is recorded in `source` either way,
        # so nothing about provenance is lost by not claiming it.
        # THE SAME REFUSAL `list` MAKES, ONE DOOR OVER, AND I SHIPPED IT ON
        # ONLY ONE OF THEM. `add --mine --owner seat-b` silently took the
        # --owner value: `if mine and not owner` skips own-resolution entirely
        # when --owner is present, so --mine was accepted and discarded with
        # no complaint. Two names for one field cannot both be honoured, and
        # picking either silently answers a question the caller did not ask.
        if mine and owner:
            print("helm task: --mine and --owner %s name two different owners "
                  "and cannot be combined — pass --mine to take it yourself, "
                  "or --owner %s to give it to that seat."
                  % (owner, owner), file=sys.stderr)
            return 2
        # ONE RESOLUTION OF THIS PROCESS'S IDENTITY, SPENT ON BOTH FIELDS
        # (the FIX on this lane). The OWNER came from
        # dispatches.acting_author and the `source` stamp came from
        # seats.own_name() forty lines down — two resolvers answering one
        # question, "which seat is this process", and they disagree on
        # exactly the two identities the switch to acting_author was made
        # for. A ROSTER-BOUND session with no HELM_CHAT_NAME resolves here
        # and returns None from own_name (which reads only the declared
        # env), so the row came out correctly OWNED and filed by NOBODY. A
        # STALE INHERITED name that contradicts the roster is a DISPUTED
        # identity acting_author refuses, while own_name hands it over
        # unexamined — so the stamp named a filer this very call had just
        # declined to call the owner. Resolved once, above the branch,
        # because `source` is asked on EVERY add and not only under --mine.
        #
        # NOT `source = owner`: filing is not owning. `--owner kimi` gives
        # the row away and leaves ME the filer, which is the whole reason
        # provenance is a second field.
        #
        # The action string is still the one --mine refuses in, so the
        # refusal below stays byte-identical; a plain add names what IT is
        # about to do, and its refusal text is never printed — an
        # unresolvable identity there means the filer is UNKNOWN (source
        # None, the honest answer and the pre-existing behaviour), never a
        # refusal. Widening the --mine refusal to every add would refuse
        # more than the defect.
        from . import dispatches as _d
        filer, ident_err = _d.acting_author(
            "take this row yourself" if mine else "record who filed this row")
        if mine and not owner:
            # THE SAME CANONICAL LAW list --mine USES. I moved the read door
            # onto dispatches.acting_author and LEFT THIS ONE on own_name,
            # after writing "one resolver, not two" in the commit message —
            # so the two doors diverged on exactly the identities the switch
            # was for: a roster-only session could LIST its rows and then file
            # one UNOWNED, and a disputed identity was refused by one door and
            # honoured by the other. Third time on this lane I cured the door
            # a finding named and left its sibling.
            owner = filer
            # THE SIBLING OF `list --mine`, AND IT FAILS THE SAME DIRECTION.
            # own_name() returns None when this process declares no identity,
            # and the old code let that fall through: the row was filed
            # UNOWNED — back into the pool any idle seat may take — while the
            # caller who typed --mine believed they had claimed it. Silent,
            # and in the one direction a claim must never fail. An unowned row
            # is a legitimate and useful state, which is exactly why it must
            # be ASKED FOR (omit the flag) rather than arrived at by a
            # resolver returning nothing.
            if not owner:
                print("helm task add --mine: %s" % ident_err, file=sys.stderr)
                print("helm task add --mine: NOTHING WAS FILED — this does "
                      "NOT fall back to filing an UNOWNED row, which is what "
                      "it used to do: the row would have gone into the pool "
                      "for any seat to take while you believed you held it. "
                      "Set the identity, use `--owner SEAT`, or drop --mine "
                      "to file it into the pool deliberately.",
                      file=sys.stderr)
                return 2
        # --owner-asked IS THE WHOLE WIRING. The field existed and was
        # populated on 251 rows by a migration, but no door could write it, so
        # the fact the owner actually wanted visible had nowhere to go and
        # went into titles instead. A flag rather than a value because the
        # answer is one bit at filing time: an agent knows whether it is
        # filing something the owner asked for. Omitted means `agent` when a
        # seat files it — a live row filed by a seat has a witnessed
        # provenance, so recording it is not a guess — while the 251 legacy
        # rows keep None and read UNKNOWN.
        # (--owner-asked is consumed with the other flags above, before the
        # title is built — it used to be stripped here and the title RE-JOINED
        # a second time, which is what let a guard slip in between the two.)
        # THE PROJECT AXIS STAMP (task/974), at the CLI door only: WHOSE work
        # this is — from `--project` when it is given, else the SAME
        # cwd→project derivation the store scopes with, one lens for one
        # question. Said out loud the way the store says
        # it, so a surprising scope is visible at filing time and not a fact
        # discovered later in a listing. No project (unregistered cwd) files
        # UNSCOPED and stays silent, the store's exact global-only contract.
        # THE FLAG WINS OVER cwd, THROUGH THE ONE RESOLVER (task/2446).
        # Measured: this door read cwd and nothing else, so
        # a task filed ABOUT HELM from another project's checkout went to its
        # scope and the making team's `helm task list` never showed it. A
        # team USING helm must never need to stand in helm's own checkout to
        # report something about helm.
        project, decided_by, scope_err = resolve_scope(
            project_opt, "helm task add")
        if scope_err:
            print("%s Nothing was filed." % scope_err, file=sys.stderr)
            return 2
        if project:
            # WHICH SCOPE, AND HOW IT WAS DECIDED, on one line. The flag and
            # the cwd readings are not interchangeable facts: an operator who
            # cannot tell them apart cannot tell an override from a lucky cwd.
            print(scope_line("helm task", project, decided_by),
                  file=sys.stderr)
        # THE ASK REFLEX IS `add()`'S, NOT THIS DOOR'S (task/2622, owner: "how
        # do we prevent me continually asking for things that have been placed
        # on the list and ignored"). The first cut resolved the title HERE,
        # which forked the rule: the same title filed through `tasks.add()` by
        # a script was never resolved at all, and the read this door made was
        # outside the ledger lock, so two concurrent adds could each miss the
        # other. The producer holds the guard, on the snapshot its own write
        # extends; this door only forwards the operator's override and says
        # out loud when it was used.
        #
        # FORCED IS NEVER SILENT. The next reader of this terminal must be able
        # to see that a duplicate refusal was overridden rather than absent.
        if force_new:
            print("helm task add: --force-new — the near-duplicate refusal is "
                  "BYPASSED for this row; nothing was resolved against the "
                  "backlog.", file=sys.stderr)
        row, err = add(title, owner, note=note, refs=refs,
                       tid=tid, source=filer,
                       origin="owner" if owner_asked else "agent",
                       project=project, posture_na=posture_na,
                       continues=continues, priority=priority,
                       force_new=force_new)
        if err:
            # a posture refusal is printed whole — its three questions ARE
            # the message — where every other refusal takes the one-line form
            print(err if err.startswith("helm task add:") else
                  "helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: filed %s — %s" % (row["id"], row["title"]))
        # THE DOOR SAYS WHAT IT FILED, AND UNRANKED IS A STATE IT NAMES.
        # The field has existed since the free-text census and nothing has
        # ever said a word about it at creation, which is how 403 of this
        # project's 440 open rows came to carry no rank: the board cannot
        # answer "what is most important" and no reader ever saw a prompt.
        #
        # IT DOES NOT DEFAULT, AND THAT IS DELIBERATE. Stamping P2 on a row
        # nobody judged would render nobody-has-ranked-this as ranked-in-the-
        # middle, which is the absence-versus-value law this module keeps for
        # `origin` and states in PRIORITIES' own note. A refusal at this door
        # is the stricter option and is NOT taken here either: it would redden
        # 219 in-tree call sites and every fleet caller's muscle memory at
        # once, on a surface whose acceptance is the owner seeing ranks on his
        # board. Named for the integrator rather than chosen silently.
        if not row.get("priority"):
            print("  UNRANKED — nobody has judged this row, which is not the "
                  "same as ranking it low. Rank it with `helm task update %s "
                  "--priority P0|P1|P2|P3`; `helm task triage` sweeps the "
                  "whole backlog." % row["id"])
        return 0

    if verb == "triage":
        # THE RANK DEBT, AND THE ONE PASS THAT PAYS IT DOWN. The field has
        # existed and nothing has ever swept it: measured on the live ledger,
        # 403 of 440 open rows in this project carry no priority, so the board
        # cannot answer "what is most important" and a reader scans an
        # undifferentiated list. A field with no triage verb is the same
        # disease as a field with no glyph — present, correct, and unused.
        #
        # DRY BY DEFAULT AND IT PRINTS WHAT IT WOULD WRITE. `--apply` is the
        # only door that touches the ledger, and it writes through `update()`
        # one row at a time so every rank lands as its own audited event
        # rather than as a silent bulk rewrite.
        #
        # THE RULE IT APPLIES IS THE OWNER'S, AND IT REFUSES THE CLAUSE IT
        # CANNOT DERIVE. Owner-asked rows (origin == owner, the stamp the
        # ledger already keeps) become P1; everything else becomes P2. The
        # rule's FIRST clause — fleet blockers are P0 — is a judgement about
        # what a row BLOCKS, which no field here records, so this never
        # writes a P0 and says so. Ranking by a rule nobody could check would
        # be the free-text laundering this field exists to refuse, one layer
        # up: the two clauses above are derivable from a stamped field, and
        # the third is not.
        # EVERY INPUT IS VALIDATED OR REFUSED BEFORE ANY READ, and that
        # ordering is the shape every selection defect here shared. A decision
        # verb that reads first and validates later has already spent the read
        # on a question it could not state.
        # ADJACENCY IS DECIDED ON THE ORIGINAL ARGV, BEFORE THE FIRST
        # REMOVAL (task/2446 r2). The three lines below and the `--limit` take
        # each delete a token, and every deletion closes a gap that separated
        # a valued flag from a later positional — which is how `--project
        # --apply fixproj` came to write fixproj's ranks.
        adj_err = option_adjacency_error(rest, ("--limit", PROJECT_FLAG),
                                         "helm task")
        if adj_err:
            print(adj_err, file=sys.stderr)
            return 2
        apply = "--apply" in rest
        if apply:
            rest.remove("--apply")
        legacy_too = "--legacy" in rest
        if legacy_too:
            rest.remove("--legacy")
        # PRESENCE IS NOT VALUE, AND `_take` CANNOT TELL THEM APART. A
        # trailing bare `--limit` is DELETED and returns None, which is the
        # same answer as "never passed" — so `helm task triage --limit`
        # silently selected EVERY row and reported success. Remembering what
        # was typed is the only way to separate the two, exactly as this
        # module's `update` leg already does for its own valued flags.
        limit_typed = any(t == "--limit" or t.startswith("--limit=")
                          for t in rest)
        limit = _take(rest, "--limit")
        # THE SCOPE OVERRIDE (task/2446), before the leftover guard below.
        # ASKED BEFORE TAKEN, IN THE HELPER: this leg read presence off `rest`
        # AFTER the take, and `_take` had already deleted the token, so the
        # guard never fired once and `triage --apply --project` ranked the cwd
        # project's backlog.
        project_opt, scope_flag_err = take_project_flag(rest, "helm task")
        if scope_flag_err:
            print("%s Nothing was read or written." % scope_flag_err,
                  file=sys.stderr)
            return 2
        if limit_typed:
            try:
                limit = int(str(limit).strip())
            except (TypeError, ValueError, AttributeError):
                print("helm task: --limit needs a WHOLE NUMBER of rows and "
                      "got %r — nothing was read or written."
                      % (limit,), file=sys.stderr)
                return 2
            # NEGATIVE AND ZERO ARE NOT SMALL SELECTIONS, THEY ARE WRONG
            # ONES. `debt[:-2]` is Python's negative slice and it selected
            # every row BUT the last two while reporting a bound; `--limit 0`
            # selected nothing and then printed "every open row carries a
            # rank" over a backlog that carried none.
            if limit < 1:
                print("helm task: --limit must be 1 or more (got %d) — a "
                      "non-positive bound selects a set nobody asked for, and "
                      "zero would report the debt as PAID. Nothing was read "
                      "or written." % limit, file=sys.stderr)
                return 2
        else:
            limit = None
        if rest:
            print("usage: helm task triage [--apply] [--limit N] "
                  "[--legacy] [%s NAME]\n%s" % (PROJECT_FLAG, USAGE),
                  file=sys.stderr)
            return 2
        # AN UNRESOLVED PROJECT IS NOT A SCOPE THAT MATCHES UNSCOPED ROWS.
        # `project_of_row(r) == scope` and `project_of_row(r) is None` select
        # the SAME rows when scope is None, so one row landed in both buckets:
        # the default pass proposed a rank for a row it simultaneously
        # reported as NOT covered, and `--legacy` listed it twice and would
        # have written it twice. The buckets are disjoint by construction now
        # because the scope is required to exist.
        scope, decided_by, scope_err = resolve_scope(
            project_opt, "helm task triage")
        if scope_err:
            print("%s Nothing was read or written." % scope_err,
                  file=sys.stderr)
            return 2
        if not scope:
            print("helm task: this directory resolves to NO PROJECT, so which "
                  "rows are in scope is UNKNOWN — not 'all of them' and not "
                  "'the unscoped ones'. Run this from a registered project "
                  "checkout, or name the scope with %s NAME. Nothing was read "
                  "or written." % PROJECT_FLAG, file=sys.stderr)
            return 2
        print(scope_line("helm task", scope, decided_by), file=sys.stderr)
        # STRICT, BECAUSE THIS VERB DECIDES. The tolerant projection SKIPS a
        # malformed row to keep listings alive over one bad byte, and this
        # caller turns what it read into a completeness claim: measured on a
        # ledger with one corrupt record, triage returned rc 0, zero UNRANKED
        # and "every open row carries a rank" while the strict reader called
        # the same file corrupt. Skipped evidence must not become an answer.
        rows_, unavailable = snapshot(strict=True)
        if unavailable:
            print("helm task: ledger UNREADABLE (%s) — the rank debt is "
                  "UNKNOWN, not zero" % unavailable, file=sys.stderr)
            return 2
        rule = RankRule(scope, include_legacy=legacy_too)
        # THE SAME LENS `list` USES. The scoped rows are this project's; the
        # UNSCOPED bucket is every row filed before the project axis, which
        # the owner's card renders too — so the debt names them and the header
        # says which is which.
        openrows = [r for r in rows_.values()
                    if r.get("status") in OPEN_STATUSES]
        scoped = [r for r in openrows if project_of_row(r) == scope]
        legacy = [r for r in openrows if project_of_row(r) is None]
        # THE LEGACY BUCKET IS OPT-IN, AND THAT IS NOT TIDINESS. Most open
        # rows predate the project axis; ranking them by a rule derived from a
        # stamp they were never given would retro-stamp history nobody
        # witnessed — the same refusal this module already makes about
        # backfilling a priority out of a row's free text. They are COUNTED in
        # the header either way, because a debt you decline to pay is still a
        # debt and hiding it would make this verb's success line a lie about
        # the board.
        live = scoped + legacy if legacy_too else scoped
        debt = [r for r in live if r.get("priority") not in PRIORITIES]
        debt.sort(key=rank_key)
        asked = [r for r in debt if origin_of(r) == "owner"]
        rest_rows = [r for r in debt if origin_of(r) != "owner"]
        # THE HEADER DESCRIBES THE DEBT, NEVER THE SLICE. `--limit` bounds
        # what this pass WRITES; counting after it would report "6 unranked"
        # about a backlog of sixteen hundred, which is a true number about
        # the wrong population and the most convincing way to be wrong.
        legacy_debt = sum(1 for r in legacy
                          if r.get("priority") not in PRIORITIES)
        print("helm task triage: %d open in scope %s, %d UNRANKED "
              "(%d owner-asked -> P1, %d other -> P2)%s"
              % (len(scoped), scope, len(debt),
                 len(asked), len(rest_rows),
                 "" if legacy_too else
                 "; %d UNSCOPED legacy row(s) unranked and NOT covered — "
                 "they predate the project axis, `--legacy` includes them"
                 % legacy_debt))
        unranked = debt[:limit] if limit is not None else debt
        if limit is not None and len(debt) > len(unranked):
            print("  --limit %d: this pass covers %d of them"
                  % (limit, len(unranked)))
        if not unranked:
            print("  nothing to triage — every open row carries a rank")
            return 0
        if not apply:
            for r in unranked[:20]:
                print("  %s %-12s %s" % ("P1" if origin_of(r) == "owner"
                                         else "P2", r.get("id"),
                                         (r.get("title") or "")[:78]))
            if len(unranked) > 20:
                print("  ... and %d more" % (len(unranked) - 20))
            # THE ADVERTISED COMMAND IS BUILT FROM THE OPTIONS THIS PREVIEW
            # WAS COMPUTED WITH. A bounded preview that says "this pass covers
            # 1 of 3" and then offers the UNRESTRICTED write as writing
            # "these" describes a different selection than the one on screen,
            # and an operator following the text writes the other two.
            # THE PROJECT AXIS BELONGS IN IT TOO (task/2446). The remedy was
            # built from `--limit` and `--legacy` and never learned the new
            # axis, so a preview scoped BY THE FLAG advertised an apply with
            # no scope: copied unchanged it ranked the CWD project's rows —
            # the rows this preview had just excluded — or refused outright
            # from a directory no project claims. SHELL-QUOTED, because a
            # registered name may carry a space and an unquoted one splits
            # into two tokens in the shell the operator pastes it into.
            # THE `=` FORM, SHELL-QUOTED — THE ONLY SPELLING THAT CARRIES
            # EVERY REGISTERED NAME (task/2446). The registry
            # admits a name beginning with a dash, and the space form of a
            # dash-leading value is REFUSED by this module's own parser, so
            # the advertised apply for `-ops` was a command that could not
            # run: the preview said "copy this" and the copy refused as a
            # missing value. Shell quoting is not value admission — it decides
            # how the shell splits the line, never whether the CLI accepts
            # what arrives — so the two have to be done separately and both.
            selection = ((" " + shlex.quote("%s=%s" % (PROJECT_FLAG, scope))
                          if decided_by == "flag" else "")
                         + ("".join(" --limit %d" % limit
                                    if limit is not None else ""))
                         + (" --legacy" if legacy_too else ""))
            print("  DRY. `helm task triage --apply%s` writes THESE %d row(s); "
                  "one row at a time, each its own ledger event. NO P0 IS "
                  "EVER WRITTEN HERE — a fleet blocker is a judgement about "
                  "what a row blocks and no field records it; set those by "
                  "hand with `helm task update <id> --priority P0`."
                  % (selection, len(unranked)))
            return 0
        # THE ACTOR IS RESOLVED ONCE, BEFORE THE FIRST WRITE. Every row this
        # pass ranks records WHO ranked it, and a sweep that could not name
        # its actor must not write half a backlog before finding out.
        actor, actor_err = _admit("rank the backlog")
        if actor_err:
            print("helm task: %s" % actor_err, file=sys.stderr)
            return 2
        wrote, skipped, failed = 0, [], []
        for r in unranked:
            # THE RULE TRAVELS, NOT THE VALUE. `update` re-reads this row
            # under its own lock and decides there, so a rank written by
            # somebody else between the preview above and this call WINS —
            # the sweep skips the row and says so instead of overwriting a
            # deliberate judgement with a derived one.
            _row, err = update(r.get("id"), rank_rule=rule, rank_actor=actor)
            if _row is SKIPPED:
                skipped.append((r.get("id"), err))
            elif err:
                failed.append((r.get("id"), err))
            else:
                wrote += 1
        print("  RANKED %d row(s) as %s%s%s"
              % (wrote, actor.canonical_name,
                 "" if not skipped else "; %d SKIPPED (the row changed under "
                 "the preview)" % len(skipped),
                 "" if not failed else "; %d REFUSED" % len(failed)))
        for rid, why in skipped[:10]:
            print("    skip %-12s %s" % (rid, why))
        for rid, err in failed[:10]:
            print("    %-12s %s" % (rid, err))
        return 0 if not failed else 1

    if verb == "list":
        # SAME SCAN, SAME REASON (task/2446 r2): this leg takes the scope
        # BEFORE `--owner`, so `--owner --project fixproj seat-a` handed
        # `--owner` a token the scope take had moved next to it.
        adj_err = option_adjacency_error(rest, ("--owner", PROJECT_FLAG),
                                         "helm task")
        if adj_err:
            print(adj_err, file=sys.stderr)
            return 2
        want_all = "--all" in rest
        as_js = "--json" in rest
        # EXACT-TOKEN membership on a list, so `--all` cannot swallow
        # `--all-projects` or vice versa — two axes, two flags: --all is the
        # STATUS axis (closed rows too), --all-projects is the PROJECT axis.
        all_projects = "--all-projects" in rest
        if all_projects:
            rest.remove("--all-projects")
        # THE SCOPE OVERRIDE (task/2446): the same flag `add` writes with,
        # read by the listing that has to find the row afterwards. Taken
        # before the leftover guard below, which is what refuses a typo — and
        # through the helper that reads PRESENCE FIRST, so a valueless flag is
        # a refusal instead of a silent fall-through to cwd.
        project_opt, scope_flag_err = take_project_flag(rest, "helm task")
        if scope_flag_err:
            print("%s Nothing was listed." % scope_flag_err, file=sys.stderr)
            return 2
        # ASK BEFORE TAKING, because `_take` CONSUMES the flag even when it
        # yields no value — `--owner --json` deletes `--owner`, returns None,
        # and leaves nothing for a later `in rest` to find. That is the same
        # shape that made the bare `--ref` vanish one verb over, and I got it
        # wrong there first by checking `rest` AFTER the take.
        asked_owner = any(t == "--owner" or t.startswith("--owner=")
                          for t in rest)
        only = _take(rest, "--owner")
        # AN EMPTY FILTER IS NOT "NO FILTER" (row e2acaae0).
        # `task list --owner= --json` returned rc 0 with EVERY row, because
        # `if only:` reads "" as falsy and silently drops the filter — the
        # operator asked to narrow and got the whole ledger, which is the one
        # direction a filter must never fail in. `--owner=` also cannot mean
        # "rows with no owner": that is what the pool already is, and reading
        # it that way would invent a query nobody asked for. So it refuses and
        # names both spellings.
        if asked_owner and not only:
            print("helm task: --owner needs a seat name. `--owner=` filters "
                  "nothing and would have printed the WHOLE ledger; drop the "
                  "flag for every row, or `helm task list` for the open "
                  "pool.\n%s" % USAGE, file=sys.stderr)
            return 2
        # `--mine` WAS ACCEPTED AND IGNORED — the same direction of failure the
        # block above exists to refuse, one flag over. This verb consumed
        # --all, --json, --all-projects and --owner and never looked for
        # --mine at all, so it fell through as an unrecognised token and the
        # caller got the WHOLE ledger under a flag whose entire promise is
        # narrowing. Reproducible on any populated ledger: `list` and
        # `list --mine` return the SAME count, while `--owner <a-seat>` and
        # `--owner <another-seat>` each return their own smaller ones. The
        # control is the finding — the filter machinery works, so this was not an
        # unresolvable identity or a broken predicate; --mine simply never
        # applied one, silently.
        #
        # WHY SILENCE IS THE WHOLE DEFECT AND NOT A DETAIL: this fleet's stall
        # discipline runs on seats auditing their OWN rows. A seat that reads
        # the whole project as its own picks up another seat's work, or reports itself
        # catastrophically overloaded, or "audits its rows clean" against a
        # list that was never its own.
        mine = "--mine" in rest
        if mine:
            rest.remove("--mine")
        # TWO NAMES FOR THE OWNER FILTER CANNOT BOTH BE HONOURED, and picking
        # one silently answers a question the caller did not ask (the same
        # rejection `helm dispatch list` makes for --mine against --to).
        if mine and only:
            print("helm task: --mine and --owner %s name two different owners "
                  "and cannot be combined — pass --mine for your own rows, or "
                  "--owner %s for that seat's." % (only, only), file=sys.stderr)
            return 2
        # EVERY KNOWN FLAG IS CONSUMED ABOVE THIS LINE, AND A LEFTOVER IS A
        # REFUSAL. Without this, the cure above is one typo deep: `--mnie` or
        # `--mine=true` matches no branch, falls through as an unrecognised
        # token exactly as `--mine` itself used to, and rc0-prints the WHOLE
        # ledger under a flag the caller believes narrowed it. That is the
        # identical consequence this verb was just fixed for, reachable by
        # misspelling it — so fixing only the correctly-spelled spelling cures
        # the instance and leaves the class.
        # CONSUME THE FLAGS THIS VERB READS BY MEMBERSHIP. --all and --json are
        # tested with `in rest` and never removed, so a guard that treats any
        # remaining dash-token as unknown REFUSES THE VERB'S OWN FLAGS — and
        # the refusal text listed --json as accepted while rejecting it. That
        # is the over-restriction mirror of the bug being fixed: I cured "an
        # unknown flag is silently ignored" and created "a known flag is
        # loudly refused", which is worse because it stops working callers.
        for known in ("--all", "--json"):
            while known in rest:
                rest.remove(known)
        leftover = sorted(a for a in rest if a.startswith("-"))
        if leftover:
            print("helm task list: unknown option%s %s — accepts --all, "
                  "--all-projects, --mine, --owner SEAT, --json. REFUSED "
                  "rather than ignored: an unrecognised filter would print "
                  "the WHOLE ledger while you believed it was narrowed.\n%s"
                  % ("" if len(leftover) == 1 else "s", " ".join(leftover),
                     USAGE), file=sys.stderr)
            return 2
        if mine:
            # THE CANONICAL ACTOR LAW, not this module's nearest resolver.
            # THE ROOT: both doors were unified on
            # `seats.own_name()` because `add --mine` already used it, and
            # own_name reads only the declared env. dispatches.acting_author
            # is the law that already answers "which seat is this process" —
            # its own docstring names `list --mine` as a case it serves — and
            # it refuses three things own_name cannot see: a stale inherited
            # HELM_CHAT_NAME that disagrees with the roster (reading a
            # stranger's rows as mine), the FAMILY FLOOR (a name three seats
            # share, which would hand one seat the rows of three), and it
            # ACCEPTS a roster-bound session with no env, which own_name
            # falsely refused.
            from . import dispatches as _d
            only, ident_err = _d.acting_author("list your own rows")
            if not only:
                # FAIL LOUD, PRINT NOTHING. Falling back to the unfiltered
                # list IS the defect: the caller would read every seat's
                # backlog as its own, which is exactly what this flag exists
                # to stop. Zero rows, the reason named, the repair named.
                # THE RESOLVER'S OWN WORDS, not a second refusal text. It
                # distinguishes disputed-identity from floor-only and names
                # the exact repair for each; restating that here would drift
                # from the message every other --mine door prints.
                print("helm task list --mine: %s" % ident_err, file=sys.stderr)
                print("helm task list --mine: NO ROWS PRINTED — this does NOT "
                      "fall back to the unfiltered list. A backlog the reader "
                      "believes is 'mine' while it names every seat is the "
                      "exact failure this flag closes.", file=sys.stderr)
                return 1
        snap, unavailable = snapshot()
        if unavailable:
            # UNKNOWN is not EMPTY. A backlog we cannot read must never print
            # as a backlog with nothing in it.
            print("helm task: ledger UNAVAILABLE (%s) — the backlog is UNKNOWN, "
                  "not empty" % unavailable, file=sys.stderr)
            return 1
        # BOTH ARMS READ THE SNAPSHOT. open_rows() is a second ledger read,
        # and the footer below promises "same SNAPSHOT the listing rendered
        # (no second read to drift between list and legend)" — a promise the
        # default arm was silently breaking: a row filed between the two
        # reads appeared in the legend's count and not in the list, or the
        # reverse. One read, one population, both surfaces.
        # THE SAME ORDERING THE BOARD SERVES, THROUGH THE SAME FUNCTION
        # (task/2622). This listing sorted by `sort_key` — the numbering —
        # while `/api/tasks` sorted by `board_key`, so one snapshot came out
        # of the terminal and out of the browser as two DIFFERENT lists and
        # the owner cross-checking one against the other was reading a
        # difference that was not in the ledger. `board_order` is the one
        # ordering and both doors call it.
        rows_ = board_order(snap.values() if want_all else
                            [r for r in snap.values()
                             if r.get("status") in OPEN_STATUSES])
        if only:
            # owner_of(), NOT a raw read — its own doc calls it "THE ONE
            # PLACE THAT DECIDES", and it strips whitespace and maps the
            # reserved placeholder words to unowned. A literal compare here
            # matched those placeholders as real owners and missed padded
            # ones. CASEFOLDED because seat identity treats Kimi and kimi as
            # ONE address everywhere else (_canonical_recipient casefolds
            # unconditionally); a case-sensitive filter hid a seat's own rows
            # from it.
            want = only.casefold()
            rows_ = [r for r in rows_ if owner_of(r).casefold() == want]
        # THE PROJECT AXIS (task/974): the default listing is THIS project's
        # rows — scope derived from cwd by the store's own lens — and what it
        # withholds is SAID, never silent: one disclosure line carries the
        # unscoped-legacy and foreign-project counts, because a filter that
        # hides without saying so is how one backlog grows two truths. A cwd
        # no project claims has no scope to filter by and FAILS OPEN to the
        # full ledger, exactly the store's global-only read. Ids stay
        # fleet-wide — `show`/`resolve` below never scope, so a cited id can
        # never answer ABSENT because of a filter.
        scope, decided_by, scope_err = resolve_scope(
            project_opt, "helm task list")
        if scope_err:
            print("%s Nothing was listed." % scope_err, file=sys.stderr)
            return 2
        # TWO ANSWERS TO ONE QUESTION CANNOT BOTH BE HONOURED, and picking
        # either silently answers a question the caller did not ask — the
        # rejection this verb already makes for --mine against --owner.
        if all_projects and decided_by == "flag":
            print("helm task: %s %s and --all-projects name two different "
                  "scopes and cannot be combined — pass %s %s for that "
                  "project's rows, or --all-projects for every project's."
                  % (PROJECT_FLAG, scope, PROJECT_FLAG, scope),
                  file=sys.stderr)
            return 2
        if all_projects:
            scope = None
        disclosure = None
        if scope:
            print(scope_line("helm task", scope, decided_by,
                             "; --all-projects for every project"),
                  file=sys.stderr)
            pool = rows_
            rows_ = [r for r in pool if project_of_row(r) == scope]
            unscoped = sum(1 for r in pool if project_of_row(r) is None)
            foreign = len(pool) - len(rows_) - unscoped
            if unscoped or foreign:
                disclosure = ("%d unscoped legacy row(s), %d row(s) from "
                              "other projects — --all-projects shows them"
                              % (unscoped, foreign))
        if as_js:
            # THROUGH THE ONE OWNER. This path built its own json.dumps and so
            # emitted raw rows while `show` next door was correct — the exact
            # divergence a single serialization door removes.
            print(json.dumps([public_row(r) for r in rows_], sort_keys=True))
            if disclosure:
                # stderr, because stdout is a parseable JSON array by
                # contract and the disclosure must survive without breaking it
                print("helm task: " + disclosure, file=sys.stderr)
            return 0
        if not rows_:
            print("helm task: no %s tasks%s" % (
                "" if want_all else "open",
                " in project '%s'" % scope if scope else ""))
            if disclosure:
                print(disclosure)
            return 0
        # STORY ORDER, NOT LEDGER ORDER. Parents carry their subissues, and
        # ranked work sorts above unranked — the two fields exist so a reader
        # can see the SHAPE of the backlog rather than its filing sequence.
        for r, indent in _story_order(rows_):
            # labeled per row only in the cross-project view — a scoped
            # listing is one project by construction and says so in its header
            print(_fmt(r, indent=indent, project_col=all_projects))
        if disclosure:
            print("\n" + disclosure)
        # THE FOOTER COUNTS THE POPULATION THE LIST WAS DRAWN FROM — measured
        # (dispatch ff7024b5): this line called fleet-wide counts()
        # under a scoped header, so a scoped view rendered "2 shown — open 5",
        # silently totalizing the exact rows the scope had just withheld and
        # disclosed. Same SNAPSHOT the listing rendered (no second read to
        # drift between list and legend), same scope filter; the status and
        # owner axes keep their context-legend semantics — the footer has
        # always shown the ledger's by-status context AROUND the shown subset,
        # and that stays true one project down.
        by = {}
        for r in snap.values():
            if scope and project_of_row(r) != scope:
                continue
            st = r.get("status") or "unknown"
            by[st] = by.get(st, 0) + 1
        print("\n%d shown — %s" % (
            len(rows_), ", ".join("%s %d" % (k, by[k]) for k in sorted(by))))
        return 0

    if verb in ("show", "resolve"):
        rc = project_flag_not_applicable("helm task %s" % verb, rest)
        if rc is not None:
            return rc
        if not rest:
            print(USAGE, file=sys.stderr)
            return 2
        token = rest[0]
        as_js = "--json" in rest
        tid = normalize_id(token)
        if not tid:
            # UNPARSEABLE and ABSENT want opposite answers: one is a typo, the
            # other is a real question about a number nobody filed.
            print("helm task: %r is not a task id — want a number (263), a "
                  "citation (#263) or the full form (task/263)" % token,
                  file=sys.stderr)
            return 2
        row = get(tid)
        if not row:
            print("helm task: %s does not exist" % tid, file=sys.stderr)
            return 1
        if as_js:
            print(as_json(row))
            return 0
        print(_fmt(row))
        # `project` prints with the provenance fields: a cross-scope reader —
        # the one `show` exists for, since ids are fleet-wide — needs to see
        # WHOSE row answered. Absent = UNSCOPED = no line, like note/source.
        for field in ("note", "closed_reason", "source", "project",
                      "posture_na"):
            if row.get(field):
                print("    %-14s %s" % (field, row[field]))
        # ORIGIN THROUGH THE NORMALIZER, WITH THE RECORD BESIDE IT.
        # `show` printed the raw field, so 251 rows displayed
        # `corpus-2026-08-05` — a fourth state on a surface whose whole claim
        # is a closed set. Printing only the normalized value would have
        # hidden a true audit fact instead, so both are said: what the field
        # MEANS, and what is actually recorded when those differ.
        # THE REASON IS THE RECORD. A reader who finds a row unoffered needs
        # WHY and WHAT LIFTS IT in the same breath, or they re-derive the
        # pause from comments the way five seats did on task/213. EXPIRED and
        # UNREADABLE both print, and print differently from LIVE, because
        # "this no longer blocks" and "this blocks and I cannot read it" are
        # exactly the two states a bare presence check would flatten.
        _sd_state, _sd = standdown_of(row)
        if _sd_state == STANDDOWN_UNREADABLE:
            # THE DIAGNOSTIC CONSUMES A DESCRIPTION, NEVER THE OBJECT.
            # show is the surface a human reaches for to find out WHY a row is
            # unoffered, so it must survive every state it exists to explain.
            print("    %-14s UNREADABLE (blocks the offer, fail-closed): %s"
                  % ("standdown", _describe_rejected(_sd)))
        elif _sd_state:
            _bits = [str(_sd.get("reason") or "").strip()]
            if str(_sd.get("lift") or "").strip():
                _bits.append("lifts when: %s" % str(_sd["lift"]).strip())
            if _sd.get("until") is not None:
                _bits.append("%s %s" % (
                    "expired" if _sd_state == STANDDOWN_EXPIRED else "until",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(float(_sd["until"])))))
            if str(_sd.get("by") or "").strip():
                _bits.append("by %s" % str(_sd["by"]).strip())
            print("    %-14s %s%s" % (
                "standdown",
                "" if _sd_state == STANDDOWN_LIVE else "[EXPIRED, no longer "
                "blocks] ", " | ".join(b for b in _bits if b)))
        raw = row.get("origin")
        if raw:
            print("    %-14s %s" % ("origin", raw if origin_of(row)
                                    else "UNKNOWN (recorded: %s)" % raw))
        if row.get("refs"):
            print("    %-14s %s" % ("refs", " ".join(row["refs"])))
        proof = row.get("takeover")
        if isinstance(proof, dict) and proof.get("transfer_id"):
            print("    %-14s %s BUILD %s -> %s (%s -> %s)" % (
                "takeover", proof["transfer_id"],
                proof.get("incumbent") or "?", proof.get("successor") or "?",
                proof.get("source_lane") or "?",
                proof.get("successor_lane") or "?"))
        for c in row.get("comments") or ():
            print("    comment       %s: %s" % (c.get("by") or "?",
                                                c.get("text")))
        return 0

    if verb == "claim":
        if not rest:
            print(USAGE, file=sys.stderr)
            return 2
        token = rest.pop(0)
        force = "--force" in rest
        if force:
            print("helm task: raw --force carries no incumbent-owner authority. "
                  "Use `helm task takeover` with fresh measured evidence; "
                  "nothing was changed.", file=sys.stderr)
            return 2
        owner = _take(rest, "--owner") or seats.own_name()
        if not owner:
            print("helm task: no seat name — pass --owner SEAT",
                  file=sys.stderr)
            return 2
        # The incumbent refusal lives in update(), not here — a guard on one
        # door is a guard the next door walks around, which is exactly what
        # `helm task update --owner` did to the first version of this check.
        row, err = update(token, status="in_progress", owner=owner, force=force)
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: %s claimed by %s" % (row["id"], owner))
        return 0

    if verb == "takeover":
        if not rest:
            print("helm task: takeover needs an id", file=sys.stderr)
            return 2
        from . import takeover
        token = rest.pop(0)
        source_lane = _take(rest, "--from-lane")
        transfer_id = _take(rest, "--transfer-id")
        superseding = "--superseding" in rest
        if superseding:
            rest.remove("--superseding")
        if not source_lane or not transfer_id or rest:
            print("helm task: takeover needs --from-lane L and --transfer-id ID; "
                  "the only other flag is --superseding. Nothing was changed.\n%s"
                  % USAGE, file=sys.stderr)
            return 2
        row, err = takeover.transfer(token, source_lane, transfer_id,
                                     superseding=superseding)
        if err:
            print("helm task takeover: REFUSE — %s" % err, file=sys.stderr)
            return 2
        proof = row.get("takeover") or {}
        print("helm task takeover: %s BUILD continuation %s -> %s "
              "(transfer %s; source untouched)"
              % (row["id"], proof.get("incumbent"),
                 proof.get("successor"), proof.get("transfer_id")))
        return 0

    if verb == "close":
        rc = project_flag_not_applicable("helm task close", rest)
        if rc is not None:
            return rc
        if len(rest) < 2:
            print("helm task: close needs an id and a reason", file=sys.stderr)
            return 2
        reason, rc = _free_text("close", rest[1:], "a close reason")
        if rc is not None:
            return rc
        row, err = close(rest[0], reason or "")
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: %s closed — %s" % (row["id"], row["closed_reason"]))
        return 0

    if verb == "update":
        rc = project_flag_not_applicable("helm task update", rest)
        if rc is not None:
            return rc
        # WIRED BECAUSE add() ALREADY PROMISED IT. Its duplicate-id refusal
        # says "use `helm task update`", and until the surface-wiring rung
        # failed this commit that subverb did not exist — an error message
        # advertising a verb the parser does not dispatch, which is precisely
        # the class the rung was built to catch. The promise was the right one;
        # the missing half was the implementation.
        if not rest:
            print("helm task: update needs an id", file=sys.stderr)
            return 2
        token = rest.pop(0)
        uforce = "--force" in rest
        if uforce:
            print("helm task: raw --force carries no incumbent-owner authority "
                  "and is not an update escape; nothing was changed.",
                  file=sys.stderr)
            return 2
        fields = {}
        # SNAPSHOT BEFORE CONSUMPTION. `_take` deletes a bare flag and returns
        # None when nothing follows it, so a VALUELESS `--origin` at the end
        # of the line vanished without trace and the command reported success.
        # Remembering what was actually typed is the only way to
        # tell "not passed" from "passed with nothing".
        typed = set(rest)
        # --continues AND --priority BELONG HERE OR THE FEATURE IS UNWIRED.
        # They reached _VALUED_FLAGS and update()'s `allowed` tuple and never
        # THIS one, so the API accepted a re-parent the production CLI door
        # refused, measured. The whole promise of the
        # field is that triage re-parents LATER, and this verb is the only
        # surface an operator has for "later".
        pairs = (("--title", "title"), ("--note", "note"),
                 ("--owner", "owner"), ("--status", "status"),
                 ("--origin", "origin")) + tuple(
                     (flag, key) for flag, key, _c in STORY_FIELDS)
        for flag, key in pairs:
            val = _take(rest, flag)
            if val is not None:
                fields[key] = val
        # EMPTY MEANS PROMOTE OUT / UNRANK, and it is the only spelling that
        # can: the valueless guard below refuses a BARE `--continues`, so
        # `--continues=` is how an operator says "this row is its own story
        # again". Normalizing keeps 263, #263 and task/263 meaning one parent
        # here exactly as `add` already accepts them.
        for _flag, _key, _coerce in STORY_FIELDS:
            if _key in fields:
                fields[_key], _cerr = _coerce(fields[_key])
                if _cerr:
                    print("helm task: %s %s — nothing was changed."
                          % (_flag, _cerr), file=sys.stderr)
                    return 2
        valueless = [f for f, k in pairs if f in typed and k not in fields]
        if valueless:
            # SAME ESCAPE, SAME SENTENCE. `update` shares _take with `add`, so
            # it inherited the identical hole — `--note -5` refused with no way
            # to mean it — and fixing one door while the other still dead-ends
            # is how an operator learns the CLI is inconsistent instead of
            # learning the spelling.
            print("helm task: %s needs a value — nothing was changed. If the "
                  "value really starts with a dash, write it as %s=VALUE.\n%s"
                  % (", ".join(valueless), valueless[0], USAGE),
                  file=sys.stderr)
            return 2
        refs = _take_all(rest, "--ref")
        if refs:
            fields["refs"] = refs
        # AN UNCONSUMED FLAG IS A TYPO, AND SILENCE MADE IT A LIE.
        # Every flag above is taken BY NAME, so a leftover was dropped and the
        # command reported the write it did not do — `--origin owner` returned
        # SUCCESS while retaining the old value, and a valueless `--origin` at
        # the end did the same. Same class as the add leg's `--help` row: this
        # verb's contract is that what you asked for happened.
        leftover = [a for a in rest if a.startswith("-")]
        if leftover:
            print("helm task: unrecognised or valueless flag(s): %s — nothing "
                  "was changed.\n%s" % (" ".join(leftover), USAGE),
                  file=sys.stderr)
            return 2
        if not fields:
            # DERIVED, because this sentence silently omitted the two newest
            # flags and an operator reading it would conclude they do not exist.
            print("helm task: nothing to change — pass --title, --note, "
                  "--owner, --status, --origin, --ref or %s"
                  % ", ".join(STORY_FLAGS), file=sys.stderr)
            return 2
        # A MANUAL RANK IS AN ACT WITH AN ACTOR TOO, and this door is why
        # the requirement could not live in the sweep alone: the integrator's
        # deliberate P0s come through HERE, and they are the ranks whose
        # authorship matters most. Resolved before the write so a refusal
        # costs nothing and names the boundary that refused.
        rank_actor = None
        if "priority" in fields:
            rank_actor, aerr = _admit("rank %s" % token)
            if aerr:
                print("helm task: %s" % aerr, file=sys.stderr)
                return 2
        row, err = update(token, force=uforce, rank_actor=rank_actor, **fields)
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: updated %s — %s%s"
              % (row["id"], ", ".join(sorted(fields)),
                 "" if not rank_actor else
                 " (ranked by %s)" % rank_actor.canonical_name))
        return 0

    if verb == "comment":
        if len(rest) < 2:
            print("helm task: comment needs an id and text", file=sys.stderr)
            return 2
        # THE SAME DOOR AS `close`. This guard was written here first, for
        # the verb that measurably lost 24 bodies; `_free_text` is that cure
        # with its verb and its noun passed in, so a third tail cannot ship
        # with a third spelling of the same refusal.
        text, rc = _free_text("comment", rest[1:], "comment text")
        if rc is not None:
            return rc
        row, err = comment(rest[0], text or "", by=seats.own_name())
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: noted on %s" % row["id"])
        return 0

    if verb == "standdown":
        # THE VERB EXISTS SO THE STATE HAS A DOOR. task/406's finding was not
        # that the offer rung was wrong; it was that a fleet-shaping decision
        # was announced in chat and therefore could not be consulted by any
        # guard. A field with no verb would repeat that at one remove: every
        # seat would have to know the raw mapping shape, and most would go on
        # writing prose into `--note`.
        if not rest:
            print("helm task: standdown needs an id", file=sys.stderr)
            return 2
        token = rest.pop(0)
        # THE OPTION/LITERAL BOUNDARY IS RESOLVED ONCE, BEFORE ANY FLAG IS
        # INTERPRETED -- INCLUDING --clear. Splitting later meant `standdown
        # ID -- --clear` was read as a clear-with-a-reason and REFUSED, so the
        # escape did not actually escape: it protected every word except the
        # reserved ones, which are exactly the words a reason needs it for.
        literal = []
        if "--" in rest:
            cut = rest.index("--")
            literal = rest[cut + 1:]
            rest = rest[:cut]
        if "--clear" in rest:
            rest.remove("--clear")
            if rest or literal:
                print("helm task: --clear takes no reason (got %r); clearing "
                      "and giving a reason are opposite acts"
                      % " ".join(list(rest) + literal), file=sys.stderr)
                return 2
            row, err = update(token, standdown=None)
            if err:
                print("helm task: %s" % _cli_error(err), file=sys.stderr)
                return 2
            print("helm task: %s stand-down CLEARED — the row is offerable "
                  "again" % row["id"])
            return 0
        # THE REASON IS A FREE-TEXT TAIL, WHICH MAKES EVERY PARSE MISTAKE
        # SILENT INSTEAD OF LOUD. _take returns None both for "flag absent"
        # and for "flag present with nothing after it", so a trailing
        # `--until` VANISHED and wrote a PERMANENT stand-down while reporting
        # success — the operator asked for a deadline and got a pause with no
        # end. And any token the parser did not recognise fell into the
        # joined tail, so `--lft "typo"` filed the typo as part of the reason.
        # Both are refused BEFORE the write, and `--` is the literal escape
        # for a reason that genuinely starts with a dash.
        typed = list(rest)

        def _named(flag):
            return [t for t in typed
                    if t == flag or t.startswith(flag + "=")]

        for flag in ("--lift", "--until"):
            if len(_named(flag)) > 1:
                print("helm task: %s given more than once; one deadline and "
                      "one lift condition, or the row records a value nobody "
                      "chose" % flag, file=sys.stderr)
                return 2
        lift = _take(rest, "--lift")
        if _named("--lift") and lift is None:
            print("helm task: --lift needs a value (say what ENDS the "
                  "stand-down); a bare --lift would vanish and the row would "
                  "record no lift condition at all", file=sys.stderr)
            return 2
        until_raw = _take(rest, "--until")
        if _named("--until") and until_raw is None:
            print("helm task: --until needs a value (4h, 2d, 1w or "
                  "YYYY-MM-DD); a bare --until would vanish and write a "
                  "PERMANENT stand-down while reporting success",
                  file=sys.stderr)
            return 2
        until = None
        if until_raw is not None:
            until, uerr = parse_standdown_until(until_raw)
            if uerr:
                print("helm task: %s" % uerr, file=sys.stderr)
                return 2
        stray = [t for t in rest if t.startswith("--")]
        if stray:
            print("helm task: unrecognised option(s) %s — refusing rather "
                  "than filing them as part of the reason. If the reason "
                  "really begins with a dash, put it after a bare --"
                  % ", ".join(repr(t) for t in stray), file=sys.stderr)
            return 2
        reason = " ".join(list(rest) + literal).strip()
        if not reason:
            # REFUSED RATHER THAN STORED EMPTY. A stand-down whose reason is
            # blank is the prose problem inverted: the guard can read it, and
            # the human it stops cannot learn anything from it.
            print("helm task: a stand-down needs a reason — say WHY the row "
                  "may not be offered, and prefer --lift to say what ends it",
                  file=sys.stderr)
            return 2
        payload = {"reason": reason, "ts": time.time(),
                   "by": seats.own_name() or None}
        if lift:
            payload["lift"] = lift
        if until is not None:
            payload["until"] = until
        row, err = update(token, standdown=payload)
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        tail = ""
        if until is not None:
            tail = " until %s" % time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
        print("helm task: %s STOOD DOWN%s — it stays in the backlog and stays "
              "visible, and the work-offer rung will not hand it to an idle "
              "seat: %s" % (row["id"], tail, reason))
        return 0

    print(USAGE, file=sys.stderr)
    return 2
