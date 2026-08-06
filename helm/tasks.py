#!/usr/bin/env python3
"""helm tasks — the FLEET TASK LEDGER: shared, resolvable work items.

WHY THIS FILE EXISTS. Until 2026-08-05 the fleet's task numbering lived inside
ONE seat's harness task list. Every "#263" written in the room resolved for
opus-integrator and for nobody else — measured: 441 citations across 260 chat
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
   source, origin, closed_reason, last_updated}

TOMBSTONES ARE FIRST-CLASS. 55% of the fleet's citation load points at items
that were CLOSED before the migration. A tombstone carries the id, the title
and `status: closed` and nothing else — it exists so that a seat reading a
week-old row gets a sentence instead of a dangling pointer. It is never work,
never assigned, and never shown in the open list.
"""
import json
import os
import re
import sys
import time

from . import eventledger, home

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
# owner name. A source:owner row is indistinguishable from a misconfigured seat. `origin`
# carries no such collision.
#
# LEGACY IS UNKNOWN, NEVER BACKFILLED. The 251 migration rows carry free-text
# origins nobody witnessed as provenance; they pass through and read UNKNOWN,
# which is TRUE. Inventing a provenance for a row nobody witnessed would be the
# erasure this field exists to end, performed in the other direction.
ORIGINS = ("owner", "agent")

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


def snapshot(path=None):
    """(rows-by-id, unavailable). Missing is known-empty; unreadable storage is
    UNKNOWN and must surface as such rather than as an empty backlog."""
    return eventledger.latest_checked(path or ledger_path())


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


def _row(tid, title, status, owner, note, refs, source, origin, closed_reason):
    now = time.time()
    return {
        "id": tid,
        "ts": now,
        "last_updated": now,
        "title": title,
        "status": status,
        "owner": owner or None,
        "note": note or None,
        "refs": list(refs or ()),
        "source": source or None,
        "origin": origin or None,
        "closed_reason": closed_reason or None,
        # Present and empty from birth. The published schema promises this key,
        # and a surface that reads row["comments"] must not have to know
        # whether anyone has commented yet.
        "comments": [],
    }


def add(title, owner, note=None, refs=None, source=None, tid=None,
        status="open", origin=None, closed_reason=None, path=None):
    """File one task -> (row, error). Exactly one of the pair is None.

    `owner` is REQUIRED for live work and refused when blank: a backlog nobody
    is accountable to is a list, not a ledger. A tombstone (status=closed) is
    the deliberate exception — it records history, it is not work.

    `tid` is optional and is how a MIGRATED item keeps its original number; the
    id is normalized, so the caller may pass 263, #263 or task/263.
    """
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

    p = path or ledger_path()
    with eventledger.locked(p) as held:
        if not held:
            return None, "task ledger is not writable — obligations UNKNOWN"
        existing, unavailable = eventledger.latest_checked(p)
        if unavailable:
            return None, "task ledger unreadable (%s) — refusing to mint" % unavailable
        if tid is None:
            new_id = "task/%d" % _next_number(existing)
        else:
            new_id = normalize_id(tid)
            if not new_id:
                return None, ("unparseable task id %r — want a number (263) or a "
                              "slug (task/work-tab-backlog)" % tid)
            if new_id in existing:
                return None, "%s already exists — use `helm task update`" % new_id
        # A ROW CAN BE BORN A REASONLESS TOMBSTONE, AND UPDATE() REFUSES THE
        # IDENTICAL END STATE (@offbox-claude, found in a meld after four
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
                   closed_reason)
        if not eventledger.append_unlocked(p, row):
            return None, "task ledger refused the write"
    return row, None


def update(token, path=None, force=False, **fields):
    """Append a new full snapshot with `fields` applied -> (row, error).

    Event-sourced: the previous row is never rewritten, so a correction stays
    auditable rather than replacing the record of what was believed before.

    `force` overrides the incumbent-owner refusal, and it lives HERE rather
    than in the CLI for the reason the title and reason rules moved here: the
    guard belongs to the invariant, not to one door. Measured — the first cut
    put the incumbent check in `cmd_task claim`, and `helm task update --owner
    thief` walked straight around it in the very next commit.
    """
    tid = normalize_id(token)
    if not tid:
        return None, "unparseable task id %r" % (token,)
    allowed = ("title", "status", "owner", "note", "refs", "source",
               "origin", "closed_reason")
    unknown = [k for k in fields if k not in allowed]
    if unknown:
        return None, "unknown field(s): %s" % ", ".join(sorted(unknown))
    if "status" in fields and fields["status"] not in STATUSES:
        return None, "unknown status %r" % fields["status"]
    # BOTH DOORS, and this function's own docstring is why: the guard belongs
    # to the invariant, not to one entrance — measured when `update --owner
    # thief` walked straight around a check that lived only in the CLI. A
    # closed set enforced at add() and open at update() is not a closed set.
    if "origin" in fields:
        # PASSING None ERASED A WITNESSED PROVENANCE (@codex). The tri-state
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
        existing, unavailable = eventledger.latest_checked(p)
        if unavailable:
            return None, "task ledger unreadable (%s)" % unavailable
        prev = existing.get(tid)
        if not prev:
            return None, "%s does not exist" % tid
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
        # codex-3's live repro: `helm task claim 172` on a CLOSED row returned
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
        # WHY --force DOES NOT OVERRIDE IT. force's documented job is the
        # INCUMBENT-OWNER refusal: taking a live row from the seat holding it.
        # A closed row has no incumbent to take it from, so force is answering
        # a question nobody asked — and letting it through would mean the only
        # way to say "I really mean it" also silently means "and resurrect
        # tombstones". Different invariant, different flag, if anyone ever
        # wants one.
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
        # TAKING A ROW FROM A LIVE HOLDER IS HOW TWO SEATS BUILD ONE THING.
        incumbent = (prev.get("owner") or "").strip()
        wanted = (row.get("owner") or "").strip()
        if incumbent and wanted and wanted != incumbent and not force:
            return None, ("%s is held by %s — coordinate, or force if you know "
                          "they are gone" % (tid, incumbent))
        row["last_updated"] = time.time()
        if row.get("status") == "in_progress" and not (row.get("owner") or ""):
            return None, ("%s would be in_progress with no owner seat — name "
                          "one with --owner" % tid)
        if not eventledger.append_unlocked(p, row):
            return None, "task ledger refused the write"
    return row, None


def close(token, reason, path=None):
    reason = (reason or "").strip()
    if not reason:
        return None, "closing a task needs a reason — a silent close is a drop"
    return update(token, path=path, status="closed", closed_reason=reason)


def counts(path=None):
    """(by-status, unavailable) for the console badge and the list header."""
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


def comment(token, text, by=None, path=None):
    """Append one non-closing note -> (row, error).

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

    p = path or ledger_path()
    with eventledger.locked(p) as held:
        if not held:
            return None, "task ledger is not writable"
        existing, unavailable = eventledger.latest_checked(p)
        if unavailable:
            return None, "task ledger unreadable (%s)" % unavailable
        prev = existing.get(tid)
        if not prev:
            return None, "%s does not exist" % tid
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
    # that seat was building — measured at this lane's tip by codex-3, the
    # exact residual hole I asked them to attack. The exclusion is a fact
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
        owner = (row.get("owner") or "").strip()
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
    paths still emitting raw rows (@codex). Every round I fixed the readers I
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
    return out


def as_json(row):
    return json.dumps(public_row(row), sort_keys=True)


USAGE = (
    "usage: helm task add <title...> [--owner SEAT | --mine] [--note N] "
    "[--ref R]... [--id NNN] [--owner-asked]\n"
    "       helm task list [--all] [--owner SEAT] [--json]\n"
    "       helm task show <id> [--json]        (id: 263, #263 or task/263)\n"
    "       helm task resolve <token> [--json]  (what does '#263' mean?)\n"
    "       helm task claim <id> [--owner SEAT] [--force]\n"
    "       helm task update <id> [--title T] [--note N] [--owner S] "
    "[--status S] [--origin owner|agent] [--ref R]...\n"
    "       helm task close <id> <reason...>\n"
    "       helm task comment <id> <text...>\n"
    "\n"
    "The FLEET TASK LEDGER — shared work items every seat can resolve. Ids keep\n"
    "the number they were born with, so a week-old chat row citing '#263' still\n"
    "means something. `resolve` is that lookup and it takes any spelling.")


def _take(rest, flag):
    """Pull `--flag value` out of rest -> value or None."""
    if flag not in rest:
        return None
    i = rest.index(flag)
    val = rest[i + 1] if len(rest) > i + 1 else None
    del rest[i:i + (2 if val else 1)]
    return val


def _take_all(rest, flag):
    out = []
    while flag in rest:
        val = _take(rest, flag)
        if val:
            out.append(val)
    return out


def _cli_error(err):
    """Library errors state the FACT; the CLI adds the remedy in ITS vocabulary.

    update() cannot say "--force" — it does not know it has a CLI, and naming a
    caller's flags from inside the invariant is how a library ends up wrong
    about a surface it never sees. So the refusal says a row is HELD and this
    layer, which owns the flags, says how to mean it anyway.
    """
    if "is held by" in err:
        return err + " — pass --force to take it anyway"
    # THE FLAG SPELLING BELONGS HERE, NOT IN THE LIBRARY (@codex-3). update()
    # is called by the API too, and an error naming `--force` tells an API
    # caller to pass a flag it has no way to pass. The library states the
    # invariant in its own terms ("forcing does not override it") and this
    # layer, which knows it is a CLI, adds the command a person would type.
    if "cannot be reopened" in err:
        return err + " (`helm task add ... --ref <id>`)"
    return err


def origin_of(row):
    """THE origin any CONSUMER sees: "owner" | "agent" | None (UNKNOWN).

    ONE OWNER FOR THE DERIVATION, and it exists because the closed set was a
    closed set only at the WRITE doors. Measured on the real ledger (@codex,
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


def _fmt(row, indent=""):
    st = row.get("status") or "?"
    mark = {"open": " ", "in_progress": ">", "closed": "x"}.get(st, "?")
    owner = row.get("owner") or ("-" if st == "closed" else "UNOWNED")
    # THE GLYPH IS THE POINT OF THE FIELD. A provenance nothing renders is a
    # provenance nobody sorts by, which is how 16 rows ended up smuggling it
    # into titles. "@" marks owner-asked work; a declared agent row and an
    # UNKNOWN legacy row both stay blank, because the distinction that earns
    # a mark here is "did the owner ask for this", not "is it classified".
    origin = "@" if origin_of(row) == "owner" else " "
    return "%s%s%s %-12s %-11s %-16s %s" % (
        indent, mark, origin, row.get("id"), st, owner[:16],
        (row.get("title") or "")[:96])


def cmd_task(args):
    """task add|list|show|resolve|claim|close|comment — the fleet task ledger."""
    from . import seats

    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]

    if verb == "add":
        owner = _take(rest, "--owner")
        note = _take(rest, "--note")
        tid = _take(rest, "--id")
        refs = _take_all(rest, "--ref")
        mine = "--mine" in rest
        if mine:
            rest.remove("--mine")
        title = " ".join(rest).strip()
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
        if mine and not owner:
            owner = seats.own_name()
        # --owner-asked IS THE WHOLE WIRING. The field existed and was
        # populated on 251 rows by a migration, but no door could write it, so
        # the fact the owner actually wanted visible had nowhere to go and
        # went into titles instead. A flag rather than a value because the
        # answer is one bit at filing time: an agent knows whether it is
        # filing something the owner asked for. Omitted means `agent` when a
        # seat files it — a live row filed by a seat has a witnessed
        # provenance, so recording it is not a guess — while the 251 legacy
        # rows keep None and read UNKNOWN.
        owner_asked = "--owner-asked" in rest
        if owner_asked:
            rest.remove("--owner-asked")
            title = " ".join(rest).strip()
        row, err = add(title, owner, note=note, refs=refs,
                       tid=tid, source=seats.own_name(),
                       origin="owner" if owner_asked else "agent")
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: filed %s — %s" % (row["id"], row["title"]))
        return 0

    if verb == "list":
        want_all = "--all" in rest
        as_js = "--json" in rest
        only = _take(rest, "--owner")
        snap, unavailable = snapshot()
        if unavailable:
            # UNKNOWN is not EMPTY. A backlog we cannot read must never print
            # as a backlog with nothing in it.
            print("helm task: ledger UNAVAILABLE (%s) — the backlog is UNKNOWN, "
                  "not empty" % unavailable, file=sys.stderr)
            return 1
        rows_ = sorted(snap.values(), key=sort_key) if want_all else open_rows()
        if only:
            rows_ = [r for r in rows_ if (r.get("owner") or "") == only]
        if as_js:
            # THROUGH THE ONE OWNER. This path built its own json.dumps and so
            # emitted raw rows while `show` next door was correct — the exact
            # divergence a single serialization door removes.
            print(json.dumps([public_row(r) for r in rows_], sort_keys=True))
            return 0
        if not rows_:
            print("helm task: no %s tasks" % ("" if want_all else "open"))
            return 0
        for r in rows_:
            print(_fmt(r))
        by, _ = counts()
        print("\n%d shown — %s" % (
            len(rows_), ", ".join("%s %d" % (k, by[k]) for k in sorted(by))))
        return 0

    if verb in ("show", "resolve"):
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
        for field in ("note", "closed_reason", "source"):
            if row.get(field):
                print("    %-14s %s" % (field, row[field]))
        # ORIGIN THROUGH THE NORMALIZER, WITH THE RECORD BESIDE IT (@codex).
        # `show` printed the raw field, so 251 rows displayed
        # `corpus-2026-08-05` — a fourth state on a surface whose whole claim
        # is a closed set. Printing only the normalized value would have
        # hidden a true audit fact instead, so both are said: what the field
        # MEANS, and what is actually recorded when those differ.
        raw = row.get("origin")
        if raw:
            print("    %-14s %s" % ("origin", raw if origin_of(row)
                                    else "UNKNOWN (recorded: %s)" % raw))
        if row.get("refs"):
            print("    %-14s %s" % ("refs", " ".join(row["refs"])))
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
            rest.remove("--force")
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

    if verb == "close":
        if len(rest) < 2:
            print("helm task: close needs an id and a reason", file=sys.stderr)
            return 2
        row, err = close(rest[0], " ".join(rest[1:]))
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: %s closed — %s" % (row["id"], row["closed_reason"]))
        return 0

    if verb == "update":
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
            rest.remove("--force")
        fields = {}
        # SNAPSHOT BEFORE CONSUMPTION. `_take` deletes a bare flag and returns
        # None when nothing follows it, so a VALUELESS `--origin` at the end
        # of the line vanished without trace and the command reported success
        # (@codex). Remembering what was actually typed is the only way to
        # tell "not passed" from "passed with nothing".
        typed = set(rest)
        pairs = (("--title", "title"), ("--note", "note"),
                 ("--owner", "owner"), ("--status", "status"),
                 ("--origin", "origin"))
        for flag, key in pairs:
            val = _take(rest, flag)
            if val is not None:
                fields[key] = val
        valueless = [f for f, k in pairs if f in typed and k not in fields]
        if valueless:
            print("helm task: %s needs a value — nothing was changed.\n%s"
                  % (", ".join(valueless), USAGE), file=sys.stderr)
            return 2
        refs = _take_all(rest, "--ref")
        if refs:
            fields["refs"] = refs
        # AN UNCONSUMED FLAG IS A TYPO, AND SILENCE MADE IT A LIE (@codex).
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
            print("helm task: nothing to change — pass --title, --note, "
                  "--owner, --status, --origin or --ref", file=sys.stderr)
            return 2
        row, err = update(token, force=uforce, **fields)
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: updated %s — %s" % (row["id"], ", ".join(sorted(fields))))
        return 0

    if verb == "comment":
        if len(rest) < 2:
            print("helm task: comment needs an id and text", file=sys.stderr)
            return 2
        row, err = comment(rest[0], " ".join(rest[1:]), by=seats.own_name())
        if err:
            print("helm task: %s" % _cli_error(err), file=sys.stderr)
            return 2
        print("helm task: noted on %s" % row["id"])
        return 0

    print(USAGE, file=sys.stderr)
    return 2
