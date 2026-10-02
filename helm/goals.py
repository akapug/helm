#!/usr/bin/env python3
"""helm goals — THE OWNER'S GOALS, carried to DONE on criteria he approved.

WHY THIS EXISTS. The owner: "my goals are captured and then always
at best partially executed ... we are more like 30-40% efficiency". Measured
on the task ledger when this was built: of 57 closed owner-asked rows, 34 closed
citing only a land; of 102 still open, the median age was 12 days. A row
closed on its LANE's acceptance, and nothing held the last steps: switch it
on, measure it live, tell him.

His words define the loop: "i state a goal yall figure out how to get there
and i click yes on your well thought out acceptance criteria and/or add
comment just like already there". So:

  A GOAL IS A TASK ROW, NOT A NEW STORE. It carries `origin: owner`, the ONE
  accountable seat as its `owner`, and a `goal` sub-object: his words
  verbatim with where he said them, his why, and up to seven criteria, each
  with the command or observation that proves it and exactly one marked as
  the criterion that tests his WHY. Its id resolves everywhere a task id
  does, and work hangs off it through the task field `continues`, which
  already existed and is re-parentable.

  HE APPROVES CRITERIA, NEVER MECHANISMS, through the decision card he
  already uses: one option ("Yes"), and the comment box he already has for
  "no", "not yet" and "add X". A comment is answered on THE SAME CARD at its
  next rev (`ownerasks.revise_decision`), so his queue never holds two cards
  for one goal and his page refuses a Yes on a rev it no longer shows; only
  after his verdict does a changed scope get a new card. HIS YES BINDS THE
  REV: the row names `card_rev`, the card rev whose body carries its
  criteria, in the same write as the criteria, and a Yes counts only on that
  rev. CUSTODY FOLLOWS THE GOAL: the open card's asker is always the goal's
  accountable seat, so when that seat changes the card moves with it in the
  same write (`follow_custody`), and only that seat revises it. No new owner
  door is minted here; the card rides `ownerasks.file_decision`.

  DONE IS A DOOR, NOT A STATUS ANYBODY MAY WRITE. `tasks.update()` refuses to
  close a goal row unless the call carries a GoalDoor, which only this module
  mints, and only `report` and `supersede` hold one that closes. `report`
  opens only when every approved criterion's latest evidence passes AND the
  why-criterion's proof was re-run by a seat other than the accountable one
  (integrator decision 4: the accountable seat never self-certifies the why).
  Landing a lane closes its child task and never the goal.

  STATE IS DERIVED, never stored twice. `state()` reads the row, its current
  card and its children; only the terminal facts (`report_ref`,
  `superseded`) are written.

  THE PROJECTION (`goals-projection.json` in the global helm home) is what a
  Stop-path reader folds instead of the 39 MB task ledger. Every task write
  that touches a goal's story refreshes it under the ledger lock, and
  `read_projection()` says whether it is still current by reading only the
  ledgers' appended tails.
"""
import json
import os
import sys
import time

from . import freetext, home, ownerasks, pk, tasks

STATES = ("proposed", "commented", "revised", "criteria-approved",
          "in-progress", "criteria-measured", "done", "superseded")
WAITING_ON_YES = ("proposed", "commented", "revised")
MOVING = ("criteria-approved", "in-progress", "criteria-measured")
TERMINAL = ("done", "superseded")

# THE DOOR'S BOUNDS. Each one is REFUSED over, never cut: a goal row is his
# testimony plus the bar he approved, and a silently shortened copy of either
# is a bar nobody set.
MAX_CRITERIA = 7        # more is a checklist, not a goal
WORDS_MAX = 6000        # characters of his words kept on the row
WHY_MAX = 1200
TEXT_MAX = 200          # one criterion's words (the clarity die caps at 20 words)
PROOF_MAX = 300
FIELD_MAX = 600         # one measurement's value / how
REF_MAX = 300
EVIDENCE_KEEP = 6       # per criterion on the row; older rows keep the rest
# THE WEB QUEUE SERVES A CARD'S CONTEXT UP TO THIS MANY CHARACTERS
# (web_core.DECISION_CTX_CAP, pinned equal by a test), so the card is built to
# fit it: the criteria are never the part that falls off.
CARD_CONTEXT_MAX = 4000
# A FIRST WAVE IS JUDGED THIS LONG AFTER ITS LAST CHILD CLOSED. Measuring at
# the instant of the last land would score every goal zero, because the
# measurement always follows the land; two hours is the LAST-STEP stall
# threshold, so the yield and the stall agree on when "done" was owed.
FIRST_WAVE_GRACE_S = 2 * 3600
PROJECTION = "goals-projection.json"
TAIL_READ_MAX = 4 * 1024 * 1024

YES_KEY = "1"
YES_LINE = ("*! Yes, done means these :: the goal closes only when each is "
            "measured and you are told")
CLOSER_ACTS = ("report", "supersede")

REFUSE_CLOSE = (
    "%s is a GOAL: it closes only when every criterion the owner approved is "
    "measured and he is told, never on a land. Measure each criterion with "
    "`helm goal measure %s <key> --pass <value> --how <command>`, then close "
    "it with `helm goal report %s <chat-post-id>`. If he replaced it, run "
    "`helm goal supersede %s --owner-ref <his post>`. Landing a lane closes "
    "its child task, never the goal")


# ---------------------------------------------------------------------------
# THE DOOR CAPABILITY — the OwnerDoor pattern (ownerasks.py). A GoalDoor is
# minted only by `_door()` below, and a census in tests/test_goals.py pins
# that call site, so no other module can write a goal record or close one.
# ---------------------------------------------------------------------------

_MINT = object()


class GoalDoor(object):
    """Which goal verb a task-row write comes from. Minted only by `_door()`;
    `tasks.update()` and `tasks.add()` refuse a goal write without one."""
    __slots__ = ("act",)

    def __init__(self, act, mint=None):
        if mint is not _MINT:
            raise TypeError("a GoalDoor is minted by helm.goals for its own "
                            "verbs; it is never constructed")
        object.__setattr__(self, "act", str(act))

    def __setattr__(self, *_a):
        raise AttributeError("a GoalDoor is immutable")

    def __repr__(self):
        return "<GoalDoor %s>" % self.act


def _door(act):
    return GoalDoor(act, mint=_MINT)


# ---------------------------------------------------------------------------
# THE BODY GRAMMAR AND THE CHECKS AT THE DOOR
# ---------------------------------------------------------------------------

def parse_body(body, words=True):
    """(words, criteria, err) from the stdin body.

    `add` takes a WORDS: section (his words, verbatim, any number of lines)
    and then a CRITERIA: section. `revise` takes criteria only, with or
    without the CRITERIA: header: his words are testimony and are fixed at
    capture, so a new statement is a new goal, not a revision.

    A criterion line is `- <outcome> :: <proof>`; `-! ` marks the one that
    tests his WHY. The `::` is required mechanically, the same bar `::` sets on
    a card option and `--needs` sets on an ask."""
    section = None if words else "criteria"
    said, lines, saw = [], [], set()
    for line in str(body or "").splitlines():
        head = line.strip().upper()
        if head in ("WORDS:", "CRITERIA:"):
            if head == "WORDS:" and not words:
                return None, None, (
                    "revise changes the criteria only. His words are "
                    "testimony and are fixed at capture; a new statement is "
                    "a new goal")
            if head in saw or (head == "WORDS:" and section is not None):
                return None, None, ("the body takes WORDS: once, first, then "
                                    "CRITERIA: once")
            saw.add(head)
            section = head[:-1].lower()
            continue
        if section == "words":
            said.append(line)
        elif section == "criteria":
            lines.append(line)
        elif line.strip():
            return None, None, ("the body starts with a WORDS: line (his "
                                "words, verbatim), then a CRITERIA: line")
    text = "\n".join(said).strip("\n")
    if words and not text.strip():
        return None, None, ("no WORDS — a goal carries the owner's own words, "
                            "verbatim, in a WORDS: section")
    criteria = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        why = s.startswith("-!")
        if not s.startswith("-"):
            return None, None, ("a criterion line starts with `- ` or `-! `: "
                                "%r" % s[:60])
        s = s[2 if why else 1:].strip()
        if "::" not in s:
            return None, None, (
                "criterion %r has no `:: <proof>` — every criterion names the "
                "command or observation that proves it, or nobody can say "
                "when it is met" % s[:60])
        what, _sep, proof = s.partition("::")
        what, proof = what.strip(), proof.strip()
        if not what or not proof:
            return None, None, ("a criterion needs BOTH halves: "
                                "`- <outcome> :: <proof>`")
        criteria.append({"text": what, "proof": proof, "why_test": why})
    return (text if words else None), criteria, None


def check_criteria(criteria):
    """Why this criteria set may not go in front of the owner, or None."""
    if not criteria:
        return ("no criteria — list what done means, one "
                "`- <outcome> :: <proof>` line each, in a CRITERIA: section")
    if len(criteria) > MAX_CRITERIA:
        return ("%d criteria — more than %d is a checklist, not a goal. Keep "
                "the outcomes he would check; the rest are child tasks"
                % (len(criteria), MAX_CRITERIA))
    marked = sum(1 for c in criteria if c["why_test"])
    if not marked:
        return ("no criterion is marked `-!` — mark the one that tests his "
                "WHY: the reason he gave is the acceptance test")
    if marked > 1:
        return ("%d criteria are marked `-!` — mark exactly one, the one that "
                "tests his WHY" % marked)
    seen = set()
    for n, c in enumerate(criteria, 1):
        if len(c["text"]) > TEXT_MAX:
            return ("criterion %d is %d characters — at most %d. It is a line "
                    "he reads on a card, not a spec" % (n, len(c["text"]),
                                                        TEXT_MAX))
        if len(c["proof"]) > PROOF_MAX:
            return ("criterion %d's proof is %d characters — at most %d. Name "
                    "the command or observation; the reasoning goes in a "
                    "child task" % (n, len(c["proof"]), PROOF_MAX))
        if c["text"].casefold() in seen:
            return "criterion %d repeats an earlier one: %r" % (n, c["text"])
        seen.add(c["text"].casefold())
        # A CRITERION IS A TARGET HE APPROVES, NOT A CLAIM, so the provenance
        # rule (mark every number MEASURED/TRACED/INFERRED) is skipped: "the
        # brief prints in 6 s" is the bar, and nothing has been measured yet.
        unclear = ownerasks._clarity_refusal(c["text"], skip=("provenance",),
                                             surface="goals")
        if unclear:
            return ("criterion %d is not readable for the owner: %s"
                    % (n, unclear))
    return None


def _criteria_rows(parsed, version, prior=(), next_key=1):
    """(criteria, next_key). An unchanged criterion keeps its key, version and
    evidence; a new or changed one gets a fresh key at `version`. Keys are
    never reused, so a key in old evidence names one criterion forever."""
    old = {(c.get("text"), c.get("proof"), bool(c.get("why_test"))): c
           for c in prior if isinstance(c, dict)}
    out = []
    for p in parsed:
        keep = old.get((p["text"], p["proof"], p["why_test"]))
        if keep:
            out.append(dict(keep))
            continue
        out.append({"key": "c%d" % next_key, "text": p["text"],
                    "proof": p["proof"], "why_test": p["why_test"],
                    "version": version, "evidence": []})
        next_key += 1
    return out, next_key


def _accountable_error(owner):
    """Why `owner` cannot hold a goal, or None."""
    owner = str(owner or "").strip().lstrip("@")
    if not owner:
        return ("a goal names ONE accountable seat — pass --owner SEAT. It "
                "is the seat his criteria card answers to")
    bad = tasks._owner_placeholder_error(owner)
    if bad:
        return bad
    from . import seats
    _canon, err = seats._canonical_recipient(owner)
    if err:
        return ("--owner %r is not a seat address (%s) — the criteria card's "
                "verdict returns to it" % (owner, err))
    if ownerasks.is_owner(owner):
        return ("--owner %r is one of the owner's own names. The accountable "
                "seat is the one that carries the goal to done, and the "
                "verdict on its card returns to it" % owner)
    return None


def _latest(criterion):
    """The newest evidence at the criterion's current version, or None."""
    ev = [e for e in criterion.get("evidence") or ()
          if isinstance(e, dict) and e.get("version") == criterion.get("version")]
    return ev[-1] if ev else None


def _criteria(goal):
    return [c for c in (goal or {}).get("criteria") or () if isinstance(c, dict)]


def _same_seat(a, b):
    from . import seats
    return seats.recipient_matches(a, b)


def _excerpt(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _when(epoch):
    return pk.epoch_ts(epoch) if isinstance(epoch, (int, float)) else "?"


# ---------------------------------------------------------------------------
# THE CARD — built from the goal, filed through the one decision door.
# ---------------------------------------------------------------------------

def card_options():
    """The one option, parsed by the card grammar so its shape is the one
    every other card has."""
    _ctx, options, _err = ownerasks.parse_card_body("goal\n" + YES_LINE)
    return options


def card_title(row):
    return "Goal %s: %s — done means…" % (
        row["id"], _excerpt(row.get("title"), 80))


def card_context(row):
    """His words, the why, and the numbered criteria with proofs — cut to the
    web queue's cap from the WORDS end only, with the cut said in the text."""
    goal = row["goal"]
    lines = ["", "Why: %s" % (goal.get("why") or "not recorded"), "",
             "Done means (criteria v%d):" % goal["criteria_version"]]
    for n, c in enumerate(_criteria(goal), 1):
        lines.append("%d. %s%s" % (n, c["text"],
                                   "  [tests your why]" if c["why_test"]
                                   else ""))
        lines.append("   proof: %s" % c["proof"])
    lines += ["", "Work may start before your yes. Your yes fixes what done "
              "means. To change a criterion, comment."]
    tail = "\n".join(lines)
    head = "Your words (%s):\n" % goal["words_ref"]
    words = goal["words"]
    room = CARD_CONTEXT_MAX - len(head) - len(tail) - 2
    if len(words) > room:
        note = "\n[%%d of %d characters shown; the whole text is on %s]" % (
            len(words), row["id"])
        keep = max(0, room - len(note % len(words)))
        words = words[:keep] + note % keep
    return "%s\"%s\"%s" % (head, words, tail)


def _card_refs(row):
    """What a goal card points at: its goal, and the criteria version its
    body carries. Filing and every in-place revision write the same pair, so
    `propose` finds a card for the version the row is at."""
    return [row["id"], "goal-criteria:v%d" % row["goal"]["criteria_version"]]


def _file_card(row, by, path=None):
    """(row, notes) — file the criteria card and record its id on the row.
    A card that cannot be filed leaves the row standing with `card: null`;
    `helm goal propose` files it later."""
    tid = row["id"]
    card, problem = ownerasks.file_decision(
        card_title(row), card_context(row), card_options(),
        tasks.owner_of(row), refs=_card_refs(row), source=by)
    if card is None:
        return row, ["criteria card NOT FILED — %s. The goal stands without "
                     "a card; `helm goal propose %s` files it" % (problem, tid)]
    notes = ["card %s: %s" % (card["id"], problem)] if problem else []
    return _record_card(row, card, path, notes)


def _record_card(row, card, path, notes):
    """Record `card` as the goal's current card, and the rev whose body
    carries the row's criteria (`card_rev`), on the row as it stands NOW: a
    measurement landing between the filing and this write must survive it,
    so a moved row is re-read and the card set on the fresh record.

    ONLY ON A ROW STILL AT THE CRITERIA THE CARD CARRIES. A row that moved
    on to another criteria version, or already names a card, is left as it
    is: this card's body is not its criteria, so binding it (`card_rev`)
    would let his Yes on it approve criteria he never read. The card is told
    it needs no answer, and the row that landed is what comes back."""
    tid, new, err = row["id"], None, "the row kept moving"
    for _attempt in range(3):
        got, why = tasks.update(tid, path=path, expect=row,
                                goal=dict(row["goal"], card=card["id"],
                                          card_rev=ownerasks.card_rev(card)),
                                goal_door=_door("card"))
        if got is not tasks.SKIPPED:
            new, err = got, why
            break
        fresh, why = _goal_row(tid, path)
        if why:
            err = why
            break
        moved = fresh["goal"]
        if moved.get("card") or moved.get("criteria_version") != \
                row["goal"].get("criteria_version"):
            notes.append(
                "card %s is in his queue but %s moved on before it was "
                "recorded (criteria v%s, card %s), so it was NOT recorded; "
                "card %s needs no answer" % (
                    card["id"], tid, moved.get("criteria_version"),
                    moved.get("card") or "none", card["id"]))
            notes += _comment_old_card(card["id"], (
                "%s moved on to criteria v%s (card %s) before this card was "
                "recorded on it. This card needs no answer." % (
                    tid, moved.get("criteria_version"),
                    moved.get("card") or "none")))
            return fresh, notes
        row = fresh
    if new is None:
        notes.append("card %s is in his queue but was NOT recorded on %s (%s); "
                     "`helm goal propose %s` adopts it" % (card["id"], tid, err,
                                                           tid))
        return row, notes
    ok, berr = ownerasks.sync_board()
    if not ok:
        notes.append("board not synced — %s" % berr)
    return new, notes


def _comment_old_card(card_id, text):
    """A card that no longer carries the criteria gets one line saying so.
    It cannot be closed from here: only his verdict closes a card."""
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable or (cards.get(str(card_id)) or {}).get("status") != "open":
        return []
    _row, problem = ownerasks.comment_decision(card_id, text)
    return ["old card %s: %s" % (card_id, problem)] if problem else []


def _rev_number(raw):
    """A recorded rev as an int >= 1, or None when it is unreadable."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return None
    return raw


def _bound_rev(goal):
    """The card rev the row says carries its current criteria (`card_rev`),
    or None when no card is recorded or the recorded rev is unreadable. A
    row written before the binding existed names none and reads as rev 1,
    the card as filed, the way a verdict that names no rev ruled on rev 1."""
    goal = goal or {}
    if not goal.get("card"):
        return None
    return _rev_number(goal.get("card_rev", 1))


def _said_yes(card):
    """(yes, when) — his Yes on this card, on whatever rev it was ruled."""
    if not isinstance(card, dict):
        return False, None
    verdict = card.get("verdict") or {}
    if card.get("status") in ("decided", "delivered") \
            and str(verdict.get("choice")) == YES_KEY:
        return True, pk.parse_ts_epoch(verdict.get("ts"))
    return False, None


def follow_custody(prev, row):
    """(card, problem) — CARD CUSTODY FOLLOWS THE GOAL.

    `tasks.update` calls this under the task ledger's lock after it
    committed a write that changed a goal row's owner. Every door that
    changes a task's owner comes through that one function (a seat
    reassignment of a dead seat's holdings, a BUILD takeover, a claim of a
    row nobody held), so this one call covers them all. The goal's OPEN card
    moves to the new accountable seat (`ownerasks.move_asker`), recorded on
    the card; a decided card keeps the asker its verdict belongs to, and a
    goal with no card has nothing to move. (None, None) when nothing moved.

    THE ROW IS THE CUSTODY RECORD AND THE CARD FOLLOWS IT. The row write has
    already landed, and a failed card move cannot undo it: the failure is
    journaled here, `helm goal show` says the card "still answers to its
    previous seat", the previous seat is still refused (`_hold_card`), and
    the accountable seat's next `helm goal revise` moves the card first."""
    goal = (row or {}).get("goal")
    if not isinstance(goal, dict) or not isinstance((prev or {}).get("goal"),
                                                    dict):
        return None, None
    tid, cid, to = row.get("id"), goal.get("card"), tasks.owner_of(row)
    if not cid or not to or _same_seat(tasks.owner_of(prev), to):
        return None, None
    cards, unavailable = ownerasks.decisions_snapshot()
    card = None if unavailable else cards.get(str(cid))
    if card is not None and card.get("status") != "open":
        return None, None
    moved, problem = ownerasks.move_asker(cid, to, _door("custody"), tid)
    if moved is None:
        problem = unavailable or problem
        pk.event("goal-custody-lag", str(tid),
                 "card %s still answers to its previous seat, not %s: %s"
                 % (cid, to, problem))
        return None, problem
    return moved, None


def _hold_card(row, card, by, notes):
    """(card, err) — THE GOAL'S OPEN CARD IS REVISED BY ITS ACCOUNTABLE SEAT,
    which is the card's asker, because custody follows the goal. A card
    still naming a previous seat (its move was lost: `follow_custody`) is
    moved first by the accountable seat's own revise; every other seat is
    refused, the previous one included while the card still names it."""
    tid, cid = row["id"], str(card["id"])
    accountable = tasks.owner_of(row)
    if not _same_seat(by, accountable):
        return None, ("card %s answers to %s, the seat accountable for %s, "
                      "and only its asker revises it; you are %s. %s"
                      % (cid, accountable, tid, by, ownerasks.REVISE_DOOR))
    if _same_seat(card.get("asker"), accountable):
        return card, None
    moved, problem = ownerasks.move_asker(cid, accountable, _door("custody"),
                                          tid)
    if moved is None:
        return None, ("card %s still answers to %s, and moving it to %s, the "
                      "seat accountable for %s, failed: %s. Nothing was "
                      "revised" % (cid, card.get("asker"), accountable, tid,
                                   problem))
    notes.append("card %s now answers to %s (it answered to %s)"
                 % (cid, accountable, card.get("asker")))
    return moved, None


def approval(card, goal):
    """(approved, when) — his Yes on THIS goal's criteria, as an epoch.

    HIS YES BINDS THE REV HE READ. It counts only on the goal's current card
    and only when it was ruled on the rev the row says carries its criteria
    (`card_rev`). The card is revised before the row is written, so a row
    write lost in between leaves a card whose newer body he can say yes to
    while the row still carries the older criteria; that Yes approved a
    different revision, and it counts once the same revise, run again,
    binds the row to the rev he approved."""
    yes, when = _said_yes(card)
    if not yes or str(card.get("id")) != str((goal or {}).get("card")):
        return False, None
    ruled = _rev_number((card.get("verdict") or {}).get("rev", 1))
    if ruled is None or ruled != _bound_rev(goal):
        return False, None
    return True, when


def _yes_elsewhere(card, goal):
    """His Yes is on this goal's current card, ruled on a rev the row does
    not carry."""
    return (_said_yes(card)[0] and isinstance(card, dict)
            and str(card.get("id")) == str((goal or {}).get("card"))
            and not approval(card, goal)[0])


def _owner_said(card):
    """When the owner last commented on this card, as an epoch, or None."""
    said = [pk.parse_ts_epoch(c.get("ts")) for c in card.get("comments") or ()
            if isinstance(c, dict) and c.get("by") == ownerasks.OWNER]
    said = [t for t in said if t is not None]
    return max(said) if said else None


def _open_comment(card):
    """When the owner last commented on this card's CURRENT revision, as an
    epoch, or None when he has not.

    A comment names the revision it was made on (`rev`, the decision
    ledger's revise), and one written before revisions existed was made on
    rev 1. A comment on an earlier revision was answered by the revision
    after it. ORDER, NOT THE CLOCK: card stamps are whole seconds, so a
    comparison with the filing stamp reads a comment made in the filing's
    second as already answered. An unparseable stamp is still a comment."""
    now = _rev(card)
    said = [pk.parse_ts_epoch(c.get("ts")) or 0.0
            for c in card.get("comments") or ()
            if isinstance(c, dict) and c.get("by") == ownerasks.OWNER
            and (c.get("rev") or 1) == now]
    return max(said) if said else None


def _rev(card):
    """The card's revision number: 1 for a card written before revisions."""
    try:
        return max(1, int(card.get("rev") or 1))
    except (TypeError, ValueError):
        return 1


def _revised_at(card):
    """When this card's current body was written. A card revised in place
    carries its prior bodies in `revisions`; the newest stamp wins."""
    stamps = [pk.parse_ts_epoch(card.get("ts")),
              pk.parse_ts_epoch(card.get("rev_ts"))]
    stamps += [pk.parse_ts_epoch(r.get("ts"))
               for r in card.get("revisions") or () if isinstance(r, dict)]
    stamps = [t for t in stamps if t is not None]
    return max(stamps) if stamps else None


# ---------------------------------------------------------------------------
# STATE — derived, one function, three sources.
# ---------------------------------------------------------------------------

def children_of(known):
    """{goal id: [child rows]} — every row whose story root is a goal."""
    out = {tid: [] for tid, r in known.items()
           if isinstance(r, dict) and isinstance(r.get("goal"), dict)}
    for tid, r in known.items():
        if not isinstance(r, dict) or type(r.get("continues")) is not str \
                or not r.get("continues"):
            continue
        root = tasks._story_root(known, tid)
        if root != tid and root in out:
            out[root].append(r)
    return out


def state(row, card, children=()):
    """{state, flags, met, total, approved_ts} for one goal row.

    `card` is the goal's CURRENT card row (or None); `children` are the rows
    whose story root is this goal. Work may start before his Yes, so a
    proposed goal with children is still proposed: the Yes gates DONE, never
    START (integrator decision 2)."""
    goal = row.get("goal") or {}
    crit = _criteria(goal)
    latest = [_latest(c) for c in crit]
    met = sum(1 for e in latest if e and e.get("pass") is True)
    current = isinstance(card, dict) and str(card.get("id")) == str(goal.get("card"))
    approved, approved_ts = approval(card, goal) if current else (False, None)
    flags = []
    if goal.get("superseded"):
        st = "superseded"
    elif goal.get("report_ref"):
        st = "done"
    elif approved:
        if crit and met == len(crit):
            st = "criteria-measured"
        elif children or any(latest):
            st = "in-progress"
        else:
            st = "criteria-approved"
    else:
        if not goal.get("card"):
            flags.append("card-not-filed")
        elif not current:
            flags.append("card-missing")
        elif card.get("status") == "open" and not card.get("owner_pushed_ts"):
            flags.append("card-not-pushed")
        if current and _yes_elsewhere(card, goal):
            flags.append("yes-other-rev")
        if current and card.get("status") == "open" \
                and not _same_seat(card.get("asker"), tasks.owner_of(row)):
            flags.append("card-asker-behind")
        if current and _open_comment(card) is not None:
            st = "commented"
        elif current and (_rev(card) > 1 or goal.get("cards")):
            st = "revised"
        else:
            st = "proposed"
    return {"state": st, "flags": flags, "met": met, "total": len(crit),
            "approved_ts": approved_ts}


def entry(row, card, children=()):
    """One goal's projection entry: everything a cheap reader needs, the
    stall inputs included, with no ledger fold."""
    goal = row["goal"]
    st = state(row, card, children)
    closed = [c.get("last_updated") or c.get("ts") for c in children
              if c.get("status") == "closed"]
    crit = []
    for c in _criteria(goal):
        e = _latest(c)
        crit.append({"key": c.get("key"), "text": c.get("text"),
                     "proof": c.get("proof"), "why_test": bool(c.get("why_test")),
                     "version": c.get("version"),
                     "latest": None if not e else {
                         k: e.get(k) for k in ("pass", "ts", "by", "value",
                                               "pre_approval")}})
    current = isinstance(card, dict) and str(card.get("id")) == str(goal.get("card"))
    stamps = [row.get("last_updated")] + [c["latest"]["ts"] for c in crit
                                          if c["latest"]]
    stamps += [c.get("last_updated") for c in children]
    if current:
        stamps += [pk.parse_ts_epoch(card.get("last_updated"))]
    stamps = [t for t in stamps if isinstance(t, (int, float))]
    return {
        "title": row.get("title"),
        "accountable": tasks.owner_of(row) or None,
        "status": row.get("status"),
        "state": st["state"], "flags": st["flags"],
        "pct": [st["met"], st["total"]],
        "criteria_version": goal.get("criteria_version"),
        "criteria": crit,
        "card": None if not goal.get("card") else {
            "id": goal["card"],
            "status": card.get("status") if current else None,
            "rev": _rev(card) if current else None,
            "bound_rev": _bound_rev(goal),
            "asker": card.get("asker") if current else None,
            "pushed": bool(card.get("owner_pushed_ts")) if current else None,
            "approved_ts": st["approved_ts"],
            "last_owner_comment_ts": _owner_said(card) if current else None,
            "open_comment_ts": _open_comment(card) if current else None,
            "last_rev_ts": _revised_at(card) if current else None},
        "cards": list(goal.get("cards") or ()),
        "children": {
            "open": sum(1 for c in children if c.get("status") != "closed"),
            "closed": len(closed),
            "last_closed_ts": max(closed) if closed else None,
            "rows": [{"id": c.get("id"), "status": c.get("status"),
                      "ts": c.get("ts"), "last_updated": c.get("last_updated")}
                     for c in sorted(children, key=tasks.sort_key)]},
        "filed_ts": row.get("ts"),
        "last_event_ts": max(stamps) if stamps else None,
        "words_ref": goal.get("words_ref"),
        "report_ref": goal.get("report_ref"),
        "report": goal.get("report"),
        "superseded": goal.get("superseded"),
    }


def compute(known, cards, sources=None):
    """The projection dict from a task fold and a card fold."""
    kids = children_of(known)
    out = {}
    for tid in sorted(kids, key=lambda t: tasks.sort_key(known[t])):
        row = known[tid]
        card = cards.get(str(row["goal"].get("card") or ""))
        out[tid] = entry(row, card, kids[tid])
    return {"v": 1, "computed_at": time.time(), "sources": sources or {},
            "goals": out}


# ---------------------------------------------------------------------------
# THE PROJECTION FILE
# ---------------------------------------------------------------------------

def projection_path():
    return os.path.join(home.global_dir(), PROJECTION)


def _source(path):
    """The ledger's size and inode, taken BEFORE its read: a write racing the
    read then shows as a newer tail and reads stale, never as covered."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return {"path": path, "size": 0, "ino": None}
    except OSError as e:
        return {"path": path, "size": None, "error": str(e)}
    return {"path": path, "size": st.st_size, "ino": st.st_ino}


def refresh_projection(known=None, override=None, tasks_path=None,
                       tasks_source=None):
    """(projection, err) — recompute and write the projection file.

    `known` is a task fold the caller already holds (the task writer passes
    its own, under its lock, with `override` the row it just appended), so a
    goal-touching write costs no second fold of the task ledger.

    THE PROJECTION IS DATED TO WHAT WAS FOLDED. `tasks_source` is the task
    ledger's `_source`, measured BEFORE `known` was folded; a caller that
    folded OUTSIDE the ledger lock (the read verbs) passes it, because a
    source measured here, after its fold, covers a row that raced the fold:
    inside the size the projection claims, outside the fold it holds, and
    `read_projection` would then serve the old answer as current. The writer
    holds the lock, so a source measured after its commit covers exactly
    its fold plus `override`; a fresh fold here is measured before it."""
    tpath = tasks_path or tasks.ledger_path()
    sources = {"tasks": tasks_source or _source(tpath),
               "decisions": _source(ownerasks.decisions_path())}
    if known is None:
        known, unavailable = tasks.snapshot(tpath)
        if unavailable:
            return None, "task ledger unreadable (%s)" % unavailable
    elif override is not None:
        known = dict(known)
        known[str(override.get("id"))] = override
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return None, "decision ledger unreadable (%s)" % unavailable
    proj = compute(known, cards, sources)
    try:
        pk.atomic_write(projection_path(),
                        json.dumps(proj, ensure_ascii=False, sort_keys=True)
                        + "\n", mode=0o600)
    except OSError as e:
        return proj, "projection not written (%s)" % e
    return proj, None


def _tail_moved(source, ids, goal_rows):
    """Why the ledger behind `source` has moved past the projection for these
    ids, or None. Reads only the bytes appended since."""
    if not isinstance(source, dict) or not isinstance(source.get("size"), int):
        return "was not measured when the projection was written"
    path = source.get("path") or ""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None if source["size"] == 0 else "is gone"
    except OSError as e:
        return "is unreadable (%s)" % e
    if source.get("ino") is not None and st.st_ino != source["ino"]:
        return "was replaced"
    if st.st_size < source["size"]:
        return "shrank"
    if st.st_size == source["size"]:
        return None
    if st.st_size - source["size"] > TAIL_READ_MAX:
        return "grew by more than %d bytes" % TAIL_READ_MAX
    try:
        with open(path, "rb") as f:
            f.seek(source["size"])
            tail = f.read(st.st_size - source["size"])
    except OSError as e:
        return "is unreadable (%s)" % e
    for line in tail.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue            # a torn line: its whole row arrives later
        if not isinstance(r, dict):
            continue
        if str(r.get("id")) in ids or (goal_rows and (
                isinstance(r.get("goal"), dict) or r.get("continues") in ids)):
            return "has a newer row for %s" % r.get("id")
    return None


def read_projection():
    """(projection, problem) — the cheap read for the Stop path and the web.

    problem is None when the projection is current. Otherwise it is a
    sentence: absent, unreadable, or STALE with the reason. A stale
    projection still comes back, because an old answer that says it is old
    is more use to a whisper than no answer; the caller decides."""
    try:
        with open(projection_path(), encoding="utf-8") as f:
            proj = json.load(f)
    except FileNotFoundError:
        return None, "absent — `helm goal sync` writes it"
    except (OSError, ValueError) as e:
        return None, "unreadable (%s)" % e
    if not isinstance(proj, dict) or not isinstance(proj.get("goals"), dict):
        return None, "malformed — `helm goal sync` rewrites it"
    goals_ = proj["goals"]
    task_ids = set(goals_)
    for e in goals_.values():
        task_ids |= {str(r.get("id")) for r in
                     ((e.get("children") or {}).get("rows") or ())}
    card_ids = {str((e.get("card") or {}).get("id")) for e in goals_.values()
                if e.get("card")}
    src = proj.get("sources") or {}
    for key, ids, goal_rows in (("tasks", task_ids, True),
                                ("decisions", card_ids, False)):
        why = _tail_moved(src.get(key), ids, goal_rows)
        if why:
            return proj, "stale: the %s ledger %s" % (key, why)
    return proj, None


# ---------------------------------------------------------------------------
# THE VERBS — each resolves its actor through the identity layer, and an
# actor that does not resolve writes nothing.
# ---------------------------------------------------------------------------

def _actor(act):
    by, who, err = ownerasks.acting_author(act)
    return by, who, err


def _goal_row(token, path=None):
    """(row, err) — a goal row by any spelling of its id."""
    tid = tasks.normalize_id(token)
    if not tid:
        return None, "%r is not a task id" % (token,)
    known, unavailable = tasks.snapshot(path, strict=True)
    if unavailable:
        return None, "task ledger unreadable (%s)" % unavailable
    row = known.get(tid)
    if not row:
        return None, "%s does not exist" % tid
    if not isinstance(row.get("goal"), dict):
        return None, ("%s is a task, not a goal — `helm goal add ... --from "
                      "%s` promotes it" % (tid, tid))
    return row, None


def _live_goal(token, path=None):
    row, err = _goal_row(token, path)
    if err:
        return None, err
    goal = row["goal"]
    if goal.get("superseded"):
        return None, "%s was superseded (%s) — nothing more happens to it" % (
            row["id"], (goal["superseded"] or {}).get("owner_ref"))
    if goal.get("report_ref"):
        return None, "%s is DONE (reported in post %s)" % (
            row["id"], goal["report_ref"])
    return row, None


# ---------------------------------------------------------------------------
# HIS WORD, RESOLVED (integrator decision D1). Two acts rest on the owner's
# word rather than on criteria: `supersede` closes a goal he replaced, and a
# failing `measure --ref` reopens a done goal he disputed. Both took any
# string, so any seat could close his goal, or reopen a done one, citing a
# post that need not exist. The ref must now RESOLVE to a record helm can read
# and he authored, and the record it resolved to is written on the row:
#
#   a chat row    its id, or 4+ characters of it, found in every room and DM
#                 lane by the reader `helm chat ack` uses
#                 (`seats_ack._locate_row`), so a prefix resolves only when no
#                 unread lane could hide a second match. The row is his when
#                 its author is one of his names (`ownerasks.is_owner`) AND
#                 one of his own doors stamped it (`seats.OWNER_RAILS`), the
#                 two-part law of `machine_senders.owner_rail`: a name alone
#                 is what any process can type.
#   his card word `card:<id>` or `card:<id>@<ts>`: his comment on one of THIS
#                 goal's criteria cards, recorded through his door (an OwnerDoor
#                 write carries `door`), with the ts `helm decide show <id>`
#                 prints. The card's sole verdict is Yes: it approves criteria
#                 and cannot also mean replace/dispute, so the bare form
#                 resolves only when he left exactly one comment on that card.
#
# HIS LATER WORD ONLY. The record is dated STRICTLY AFTER the goal's current
# criteria card took its current revision (its rev_ts, or its filing ts when
# it was never revised; a card comment by its own ts, and never one made on
# an earlier rev), and a chat row is never the post the goal's
# words_ref names. The words that asked for a goal, anything he said before
# he saw it as a goal, and his "not yet, add X" that a later revision
# answered are not his word replacing or disputing the goal as it stands, and
# citing them is the proxy owner-voice this door stops.
# A REOPEN is also dated strictly after the report that marked the goal done
# (`_after_report`): a post he made while the work was in flight, or one that
# reopened it before, disputes no done he had yet been told of.
#
# Anything else refuses, naming what was looked up. A store that cannot be
# read refuses as well, because "not found" over an unread store is a guess.
# ---------------------------------------------------------------------------

CARD_REF = "card:"
OWNER_WORD_FORMS = (
    "a chat row he posted (its id, or 4+ characters of it, as `helm chat "
    "read` prints it) or card:<id>@<ts> for his comment on this goal's "
    "criteria card (as `helm decide show <id>` prints it), dated strictly "
    "after that card's current revision (filed, or last revised) and never "
    "the post its words_ref names; the card's Yes verdict approves criteria "
    "and cannot replace or dispute the goal")


def owner_word(ref, goal, flag, tid=None):
    """(record, err): the owner-authored record `ref` names for the goal
    record `goal`, or why it names none. `flag` is how the caller spelled the
    ref (`--owner-ref`, `--ref`), and `tid` the goal's id, for the refusal.

    The record is {"kind": "chat", "id", "ts", "room"} for his chat row, or
    {"kind": "card-comment", "id": <card id>, "ts", "rev"} for his comment
    on one of the goal's criteria cards. Its Yes verdict is approval, not a
    replacement or dispute, and refuses. So does a record dated at or before
    the goal's current card took its current revision, and the post its
    words_ref names."""
    ref, tid = str(ref or "").strip(), tid or "this goal"
    said, err = (_card_word(ref, goal, flag, tid) if ref.startswith(CARD_REF)
                 else _chat_word(ref, flag))
    if err:
        return None, err
    return _later_word(said, ref, goal, flag, tid)


def _names_words_ref(rid, goal):
    """Does the goal's words_ref name chat row `rid`: its whole id, or 4+ of
    its leading characters, bare or after the `chat:` the door's example
    writes?"""
    w = str(goal.get("words_ref") or "").strip()
    w = w[len("chat:"):].strip() if w.startswith("chat:") else w
    rid = str(rid or "")
    return bool(rid) and (w == rid or (len(w) >= 4 and rid.startswith(w)))


def _later_word(said, ref, goal, flag, tid):
    """(said, err): `said` when it is dated strictly after the goal's current
    criteria card took its current revision (rev_ts, or its filing ts when
    it was never revised), is not a comment made on an earlier rev of that
    card, and is not the post that asked for the goal."""
    where = _label(said_text(said))
    if said["kind"] == "chat" and _names_words_ref(said.get("id"), goal):
        return None, ("%s %r resolves to %s, the post that asked for this goal "
                      "(its words_ref %s). The words that created a goal "
                      "cannot replace or reopen it: cite his later word"
                      % (flag, ref, where, _label(goal.get("words_ref"))))
    cid = str(goal.get("card") or "")
    if not cid:
        return None, ("%s %r cannot be shown to postdate %s's criteria card: "
                      "none is recorded on it (`helm goal propose %s` files "
                      "one). Nothing was written" % (flag, ref, tid, tid))
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return None, ("%s %r cannot be shown to postdate %s's criteria card "
                      "%s: decision ledger unreadable (%s). Nothing was "
                      "written" % (flag, ref, tid, cid, unavailable))
    # THE CURRENT REVISION, not the filing: his "not yet, add X" on rev 1
    # is answered by rev 2, and is no word on the goal as it now reads. The
    # filing time bounds only a card never revised: a revised card with no
    # rev_ts took its body at a time nobody recorded, an unread time
    card = cards.get(cid) if isinstance(cards.get(cid), dict) else {}
    rev = ownerasks.card_rev(card)
    current = (card.get("rev_ts") or card.get("ts") if rev == 1
               else card.get("rev_ts"))
    at, since = (pk.parse_ts_epoch(said.get("ts")), pk.parse_ts_epoch(current))
    if at is None or since is None or at <= since:
        why = ("the words that created it, a comment a revision answered, or "
               "anything older, cannot" if at is not None and since is not
               None else "a time that does not read cannot be shown to be "
               "later")
        return None, ("%s %r resolves to %s, dated %s, and %s's criteria card "
                      "%s took its current revision at %s. His word replacing "
                      "or reopening a goal is dated strictly after that: %s"
                      % (flag, ref, where, _label(said.get("ts") or "none"),
                         tid, cid, _label(current or "unknown"), why))
    # ORDER, NOT ONLY THE CLOCK: his comment on this card names the rev it was
    # made on, and one made on an earlier rev was answered, whatever a clock
    # that stepped back says (`_open_comment` reads it the same way)
    made = said.get("rev") if said.get("rev") is not None else 1
    if said["kind"] == "card-comment" and str(said.get("id")) == cid \
            and made != rev:
        return None, ("%s %r resolves to %s, made on rev %s of card %s, which "
                      "now reads rev %s: the revision after it answered it, so "
                      "it cannot replace or reopen %s"
                      % (flag, ref, where, _label(str(made)), cid,
                         rev if rev is not None else "UNREADABLE", tid))
    return said, None


def _after_report(said, ref, goal, flag, tid):
    """err, or None when his word `said` is dated strictly after the report
    that marked the goal done: a dispute of done postdates the done. A report
    with no readable time, or a word whose time does not read, refuses."""
    rep = goal.get("report") if isinstance(goal.get("report"), dict) else {}
    done = rep.get("ts")
    done = (done if isinstance(done, (int, float))
            and not isinstance(done, bool) else None)
    at = pk.parse_ts_epoch(said.get("ts"))
    if at is not None and done is not None and at > done:
        return None
    why = ("a word he gave before he was told it was done cannot dispute it"
           if at is not None and done is not None else
           "a time that does not read cannot be shown to be later")
    return ("%s %r resolves to %s, dated %s, and %s was reported DONE at %s. "
            "His word reopening a done goal is dated strictly after the "
            "report that marked it done: %s"
            % (flag, ref, _label(said_text(said)),
               _label(said.get("ts") or "none"), tid,
               pk.epoch_ts(done) if done is not None else "unknown", why))


def _label(name):
    """An author as recorded, laundered for the terminal the refusal reaches
    (the seats_ack law: a planted name must not reshape the sentence)."""
    from . import seats_ack
    return seats_ack._seat_label(name or "?")


def _chat_word(ref, flag):
    from . import seats, seats_ack
    m, room, err = seats_ack._locate_row(ref, verb=flag)
    if err:
        return None, ("%s %r was looked up as a chat row id in every room and "
                      "DM lane: %s. It must name %s"
                      % (flag, ref, str(err).rstrip(". "), OWNER_WORD_FORMS))
    where = "chat row %s in %s" % (_label(m.get("id")), room)
    author = _label(m.get("from"))
    if m.get("react"):
        return None, ("%s %r resolves to %s, a reaction by %s. A reaction is "
                      "not his word: cite the post" % (flag, ref, where, author))
    if not ownerasks.is_owner(m.get("from")):
        return None, ("%s %r resolves to %s, posted by %s — not the owner. A "
                      "goal closes or reopens on HIS word: %s"
                      % (flag, ref, where, author, OWNER_WORD_FORMS))
    if m.get("origin") not in seats.OWNER_RAILS:
        return None, (
            "%s %r resolves to %s, posted under the owner's name %s but not "
            "through one of his doors (origin %s; his doors stamp %s). A name "
            "alone is what any process can type"
            % (flag, ref, where, author,
               _label(m.get("origin") or "none"),
               ", ".join(seats.OWNER_RAILS)))
    return {"kind": "chat", "id": m.get("id"), "ts": m.get("ts"),
            "room": room}, None


def _card_word(ref, goal, flag, tid):
    cid, _at, when = ref[len(CARD_REF):].partition("@")
    cid, when = cid.strip(), when.strip()
    mine = [str(c) for c in [goal.get("card")] + list(goal.get("cards") or ())
            if c]
    if cid not in mine:
        return None, ("%s %r names card %s, which is not one of %s's criteria "
                      "cards (%s). His word on a goal is read from its own card"
                      % (flag, ref, cid or "(no id)", tid,
                         ", ".join(mine) or "none filed"))
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return None, ("%s %r cannot be shown to be his word: decision ledger "
                      "unreadable (%s). Nothing was written"
                      % (flag, ref, unavailable))
    card = cards.get(cid)
    if not isinstance(card, dict):
        return None, ("%s %r: card %s is %s's, and the decision ledger has no "
                      "such card" % (flag, ref, cid, tid))
    words = [("card-comment", c) for c in card.get("comments") or ()
             if isinstance(c, dict)]
    if isinstance(card.get("verdict"), dict):
        words.append(("card-verdict", card["verdict"]))
    if when:
        words = [w for w in words if str(w[1].get("ts") or "") == when]
        if not words:
            return None, ("%s %r: card %s carries no comment or verdict at %s "
                          "(`helm decide show %s` prints each one's time)"
                          % (flag, ref, cid, when, cid))
    his = [w for w in words
           if w[1].get("door") and ownerasks.is_owner(w[1].get("by"))]
    if not his and when:
        return None, ("%s %r: the word on card %s at %s is by %s — not the "
                      "owner through one of his doors"
                      % (flag, ref, cid, when, ", ".join(sorted({
                          _label(w[1].get("by")) for w in words}))))
    if not his:
        return None, ("%s %r: card %s carries no comment or verdict of the "
                      "owner. It must name %s" % (flag, ref, cid,
                                                  OWNER_WORD_FORMS))
    if any(k == "card-comment" for k, _e in his):
        # HIS YES IS NO CANDIDATE beside a comment of his, bare or at one
        # time: it refuses below, so offering it as a choice offers a refusal
        his = [w for w in his if w[0] == "card-comment"]
    if len(his) > 1:
        return None, ("%s %r is ambiguous: the owner said %d things on card %s "
                      "(at %s). Name one as card:%s@<ts>"
                      % (flag, ref, len(his), cid,
                         ", ".join(str(w[1].get("ts")) for w in his), cid))
    kind, e = his[0]
    if kind == "card-verdict":
        return None, ("%s %r names the owner's Yes verdict on goal card %s. "
                      "That Yes approves the goal's criteria and is the Yes "
                      "that authorized DONE; it does not replace or dispute "
                      "the goal. Cite his post or comment that does"
                      % (flag, ref, cid))
    return {"kind": kind, "id": cid, "ts": e.get("ts"), "rev": e.get("rev")}, None


def said_text(said):
    """One clause naming the owner-authored record a goal act rested on."""
    if not isinstance(said, dict):
        return ""
    if said.get("kind") == "chat":
        return "his post %s in %s" % (said.get("id"), said.get("room"))
    return "his %s on card %s at %s" % (
        "verdict" if said.get("kind") == "card-verdict" else "comment",
        said.get("id"), said.get("ts"))


def _update(row, act, path=None, **fields):
    """tasks.update with the door and the row this verb judged."""
    got, err = tasks.update(row["id"], path=path, expect=row,
                            goal_door=_door(act), **fields)
    if got is tasks.SKIPPED:
        return None, err
    return got, err


def add(title, owner, words, words_ref, why, criteria, from_task=None,
        project=None, force_new=False, path=None):
    """(row, notes, err) — capture a goal and put its criteria card in front
    of the owner. `from_task` promotes an existing owner-asked row in place:
    same id, note and comments, with the goal record stamped on."""
    by, who, err = _actor("capture an owner goal")
    if err:
        return None, [], err
    err = _accountable_error(owner)
    if err:
        return None, [], err
    owner = str(owner).strip().lstrip("@")
    words = str(words or "").strip("\n")
    if not words.strip():
        return None, [], ("no WORDS — a goal carries the owner's own words, "
                          "verbatim")
    if len(words) > WORDS_MAX:
        return None, [], (
            "his words are %d characters — at most %d on the row. Quote the "
            "part that states the goal; --words-ref points at the whole post"
            % (len(words), WORDS_MAX))
    words_ref = str(words_ref or "").strip()
    if not words_ref:
        return None, [], (
            "no --words-ref — his words are testimony, so say where he said "
            "them: a chat post id, a transcript path:line, or a web chat row")
    if len(words_ref) > REF_MAX:
        return None, [], "--words-ref is over %d characters" % REF_MAX
    why = str(why or "").strip() or None
    if why and len(why) > WHY_MAX:
        return None, [], "--why is over %d characters" % WHY_MAX
    err = check_criteria(criteria)
    if err:
        return None, [], err
    rows_, next_key = _criteria_rows(criteria, 1)
    goal = {"v": 1, "words": words, "words_ref": words_ref, "why": why,
            "criteria_version": 1, "next_key": next_key, "criteria": rows_,
            "card": None, "card_rev": None, "cards": [], "report_ref": None,
            "report": None,
            "superseded": None, "captured_by": by, "captured_ts": time.time()}
    probe = dict(goal)
    probe_row = {"id": "task/0", "title": title or "", "goal": probe}
    if len(card_context(probe_row)) > CARD_CONTEXT_MAX:
        return None, [], ("the criteria do not fit one card (%d characters "
                          "of context, the web queue serves %d) — shorten the "
                          "proofs" % (len(card_context(probe_row)),
                                      CARD_CONTEXT_MAX))
    if from_task:
        row, err = _promote(from_task, title, owner, goal, who, path)
    else:
        row, err = tasks.add(title, owner, source=by, origin="owner",
                             priority="P1", project=project,
                             force_new=force_new, path=path, goal=goal,
                             goal_door=_door("add"))
    if err:
        return None, [], err
    row, notes = _file_card(row, by, path)
    return row, notes, None


def _promote(token, title, owner, goal, who, path):
    tid = tasks.normalize_id(token)
    if not tid:
        return None, "--from %r is not a task id" % (token,)
    known, unavailable = tasks.snapshot(path, strict=True)
    if unavailable:
        return None, "task ledger unreadable (%s)" % unavailable
    prev = known.get(tid)
    if not prev:
        return None, "--from %s does not exist" % tid
    if isinstance(prev.get("goal"), dict):
        return None, "%s is already a goal — `helm goal show %s`" % (tid, tid)
    if prev.get("status") == "closed":
        return None, ("%s is CLOSED — a goal he restates is a new goal: file it "
                      "without --from and let it cite %s" % (tid, tid))
    if tasks.origin_of(prev) == "agent":
        return None, ("%s was filed as agent work. A goal is one the owner "
                      "stated: file it without --from, and let %s continue it"
                      % (tid, tid))
    if prev.get("continues"):
        return None, (
            "%s continues %s, and a goal is a story root. Promote it out "
            "first: `helm task update %s --continues=`"
            % (tid, prev["continues"], tid))
    fields = {"goal": goal}
    if tasks.origin_of(prev) != "owner":
        fields["origin"] = "owner"
    if not _same_seat(tasks.owner_of(prev), owner):
        fields["owner"] = owner
    if title and title.strip() != prev.get("title"):
        fields["title"] = title.strip()
    rank = None
    if prev.get("priority") not in tasks.PRIORITIES:
        fields["priority"], rank = "P1", who
    return _update(prev, "promote", path=path, rank_actor=rank, **fields)


def propose(token, path=None):
    """(row, notes, err) — put the current criteria in front of the owner when
    no card carries them: the filing failed, or the card was lost."""
    by, _who, err = _actor("propose a goal's criteria")
    if err:
        return None, [], err
    row, err = _live_goal(token, path)
    if err:
        return None, [], err
    goal, tid = row["goal"], row["id"]
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return None, [], "decision ledger unreadable (%s)" % unavailable
    card = cards.get(str(goal.get("card") or ""))
    if card:
        if approval(card, goal)[0]:
            return None, [], ("the owner already said yes to %s's criteria "
                              "(card %s) — `helm goal revise %s` changes them"
                              % (tid, card["id"], tid))
        if _yes_elsewhere(card, goal):
            return None, [], (
                "card %s carries his yes on rev %s, and %s's criteria are "
                "bound to rev %s — run the same `helm goal revise %s` again "
                "to bind the row to the criteria he approved"
                % (card["id"], (card.get("verdict") or {}).get("rev"), tid,
                   _bound_rev(goal), tid))
        return None, [], ("card %s already carries %s's criteria and is %s — "
                          "it is waiting on him, not on a new card"
                          % (card["id"], tid, card.get("status")))
    want = _card_refs(row)[1]
    orphan = [c for c in cards.values() if c.get("status") == "open"
              and tid in (c.get("refs") or ()) and want in (c.get("refs") or ())]
    if orphan:
        orphan.sort(key=lambda c: str(c.get("ts") or ""))
        new, notes = _record_card(row, orphan[-1], path, [])
        return new, ["adopted card %s, already in his queue" % orphan[-1]["id"]]\
            + notes, None
    new, notes = _file_card(row, by, path)
    return new, notes, None


def _next_goal(row, criteria):
    """(goal, err) — the row's goal record at its next criteria version.
    An unchanged criterion keeps its key and evidence (`_criteria_rows`)."""
    goal = dict(row["goal"])
    now = [(c.get("text"), c.get("proof"), bool(c.get("why_test")))
           for c in _criteria(goal)]
    if now == [(c["text"], c["proof"], c["why_test"]) for c in criteria]:
        return None, "nothing changed — these are the current criteria"
    version = int(goal.get("criteria_version") or 1) + 1
    goal["criteria"], goal["next_key"] = _criteria_rows(
        criteria, version, _criteria(goal), int(goal.get("next_key") or 1))
    goal["criteria_version"] = version
    if len(card_context(dict(row, goal=goal))) > CARD_CONTEXT_MAX:
        return None, "the criteria do not fit one card — shorten the proofs"
    return goal, None


def revise(token, criteria, path=None):
    """(row, notes, err) — a new criteria version, on the card he reads.

    AN OPEN CARD IS REVISED IN PLACE (`_revise_on_card`): his comment is a
    "not yet", and the answer is the same card at its next rev, never a
    second card beside it in his queue. A DECIDED card is never re-ruled
    (ownerasks `decide`), so after his verdict a changed scope is a new card
    and the decided one moves to `cards`; so is a card that was never filed
    or is missing from the decision ledger. The one decided card that is
    not replaced is the one whose body ALREADY carries these criteria: a
    revise whose row write was lost moved the card, he ruled on it, and
    this run binds the row to what he ruled on. An unreadable decision
    ledger cannot say which case applies, and nothing is written."""
    by, _who, err = _actor("revise a goal's criteria")
    if err:
        return None, [], err
    row, err = _live_goal(token, path)
    if err:
        return None, [], err
    err = check_criteria(criteria)
    if err:
        return None, [], err
    goal, err = _next_goal(row, criteria)
    if err:
        return None, [], err
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return None, [], (
            "decision ledger unreadable (%s): whether %s's card is open "
            "(revised in place) or decided (a new card) cannot be told. "
            "Nothing was written" % (unavailable, row["id"]))
    old = goal.get("card")
    card = cards.get(str(old or ""))
    if card and (card.get("status") == "open"
                 or _carries(card, dict(row, goal=goal))):
        return _revise_on_card(row, criteria, card, by, path)
    if old:
        goal["cards"] = list(goal.get("cards") or ()) + [old]
    goal["card"], goal["card_rev"] = None, None
    new, err = _update(row, "revise", path=path, goal=goal)
    if err:
        return None, [], err
    new, notes = _file_card(new, by, path)
    return new, notes, None


def _carries(card, row):
    """Does the card's current body carry `row`'s criteria: the title and
    context the goal verbs write for it, word for word?"""
    return (card.get("title"), str(card.get("context") or "").strip()) == (
        " ".join(card_title(row).split()), card_context(row).strip())


def _revise_on_card(row, criteria, card, by, path=None):
    """(row, notes, err) — the new criteria on the goal's CURRENT card: an
    open card is revised in place (`ownerasks.revise_decision`, goal-ledger
    L1), the same id at its next rev, the replaced body kept on its
    `revisions`, his thread kept, its refs naming the new criteria version,
    the owner pushed again, and his page refusing a Yes on the rev it no
    longer shows. Only the card's asker revises it, and that is always the
    goal's accountable seat (`_hold_card`); any other seat is refused.

    THE CARD FIRST, THE ROW SECOND. The card's refusals (another seat, a
    card decided or revised since it was read) then write nothing at all.
    A row write that fails after the card moved leaves the card carrying the
    new criteria and the row the old ones, and the refusal says so; the same
    revise run again finds the card already carrying this body (still open,
    or decided by his verdict on it), does not revise it a second time, and
    writes the row only.

    THE ROW NAMES THE REV THAT CARRIES ITS CRITERIA (`card_rev`), in the
    same write as the criteria, so his Yes counts only on that rev
    (`approval`): a Yes he gave on the new rev before the row caught up
    counts once this write binds the row to it."""
    tid, cid = row["id"], str(card["id"])
    goal, err = _next_goal(row, criteria)
    if err:
        return None, [], err
    probe = dict(row, goal=goal)
    notes = []
    if card.get("status") == "open":
        card, err = _hold_card(row, card, by, notes)
        if err:
            return None, notes, err
    if not _carries(card, probe):
        got, problem = ownerasks.revise_decision(
            cid, card_context(probe), card_options(),
            title=" ".join(card_title(probe).split()),
            rev=ownerasks.card_rev(card), refs=_card_refs(probe))
        if got is None:
            return None, [], (
                "card %s is open, so %s's criteria are revised on it in "
                "place, and it refused: %s" % (cid, tid, problem))
        if problem:
            notes.append("card %s rev %s: %s" % (cid, got.get("rev"), problem))
        card = got
    bound, start = ownerasks.card_rev(card), goal["criteria_version"]
    for _attempt in range(3):
        new, why = tasks.update(tid, path=path, expect=row,
                                goal=dict(goal, card_rev=bound),
                                goal_door=_door("revise"))
        if new is not tasks.SKIPPED:
            break
        fresh, why = _live_goal(tid, path)
        if why:
            new = None
            break
        row = fresh
        goal, why = _next_goal(row, criteria)
        if why or goal["criteria_version"] != start:
            new, why = None, why or "its criteria were revised meanwhile"
            break
    else:
        new, why = None, "the row kept moving under three attempts"
    if new is None:
        return None, notes, (
            "card %s now reads criteria v%d at rev %s, and %s was NOT updated "
            "(%s); a yes he gives on rev %s counts only once the row is. Run "
            "the same `helm goal revise %s` again: it finds the card already "
            "carrying these criteria and writes the row only"
            % (cid, start, bound, tid, why, bound, tid))
    ok, berr = ownerasks.sync_board()
    if not ok:
        notes.append("board not synced — %s" % berr)
    return new, notes, None


def _proof_commands(proof):
    """The commands a proof names: its backtick spans, or the proof itself
    when it starts with a helm command."""
    spans = [s.strip() for s in str(proof or "").split("`")[1::2] if s.strip()]
    if spans:
        return spans
    p = str(proof or "").strip()
    return [p] if p.startswith("helm ") else []


def measure(token, key, passed, value, how, ref=None, path=None):
    """(row, notes, err) — record one criterion's evidence.

    Any admitted seat may measure (integrator decision 4); the row says who.
    A fail recorded against a DONE goal is the owner disputing it: the goal
    reopens through this door, and only with --ref naming his word dated
    after the report that marked it done."""
    by, _who, err = _actor("measure a goal criterion")
    if err:
        return None, [], err
    value, how = str(value or "").strip(), str(how or "").strip()
    ref = str(ref or "").strip() or None
    if not value:
        return None, [], ("no value — say what the proof showed (--pass "
                          "<value> or --fail <value>)")
    if not how:
        return None, [], ("no --how — name the command or observation you "
                          "actually ran; evidence nobody can re-run is not "
                          "evidence")
    for name, text, cap in (("the value", value, FIELD_MAX),
                            ("--how", how, FIELD_MAX), ("--ref", ref, REF_MAX)):
        if text and len(text) > cap:
            return None, [], "%s is over %d characters" % (name, cap)
    row, err = _goal_row(token, path)
    if err:
        return None, [], err
    goal = dict(row["goal"])
    if goal.get("superseded"):
        return None, [], "%s was superseded — nothing more is measured" % row["id"]
    crit = [dict(c) for c in _criteria(goal)]
    hit = [c for c in crit if c.get("key") == key]
    if not hit:
        return None, [], ("%s has no criterion %r — its criteria are %s"
                          % (row["id"], key, ", ".join(c.get("key") or "?"
                                                       for c in crit)))
    c = hit[0]
    cards, unavailable = ownerasks.decisions_snapshot()
    approved = None if unavailable else approval(
        cards.get(str(goal.get("card") or "")), goal)[0]
    notes = []
    wanted = _proof_commands(c.get("proof"))
    if wanted and not any(w in how for w in wanted):
        notes.append("--how does not contain the proof's command (%s) — the "
                     "evidence stands, but say why it proves the criterion"
                     % "; ".join(wanted))
    # WHO WAS ACCOUNTABLE WHEN THIS LANDED rides the entry, so a pass the
    # accountable seat recorded is still its own after custody moves
    # (`_independent_why`).
    e = {"ts": time.time(), "by": by, "pass": bool(passed), "value": value,
         "how": how, "ref": ref, "version": c.get("version"),
         "accountable": tasks.owner_of(row) or None,
         "pre_approval": None if approved is None else not approved}
    # THE ROW KEEPS THE NEWEST FEW; the ledger keeps every one, because each
    # earlier snapshot of this row is still in it.
    c["evidence"] = (list(c.get("evidence") or ()) + [e])[-EVIDENCE_KEEP:]  # noqa: SILENT_CAP — older evidence stays in the row's earlier ledger events
    goal["criteria"] = [c if x.get("key") == key else x for x in crit]
    fields, act = {"goal": goal}, "measure"
    if goal.get("report_ref") and not passed:
        if not ref:
            return None, [], (
                "%s is DONE. A fail reopens it, and a done goal reopens on "
                "the owner's word — pass --ref <his post or comment>: %s"
                % (row["id"], OWNER_WORD_FORMS))
        # HIS WORD, RESOLVED (D1): a reopen cites a record he authored, and
        # the record it resolved to rides the reopen.
        said, err = owner_word(ref, goal, "--ref", row["id"])
        err = err or _after_report(said, ref, goal, "--ref", row["id"])
        if err:
            return None, [], ("%s is DONE and reopens only on the owner's "
                              "word, so nothing was written. %s"
                              % (row["id"], err))
        goal["reopened"] = list(goal.get("reopened") or ()) + [{
            "ts": e["ts"], "by": by, "ref": ref, "owner_said": said,
            "key": key, "report_ref": goal["report_ref"]}]
        goal["report_ref"], goal["report"] = None, None
        fields.update(status="open", closed_reason=None)
        act = "reopen"
        notes.append("%s REOPENED: %s failed against the owner's word (%s)"
                     % (row["id"], key, said_text(said)))
    new, err = _update(row, act, path=path, **fields)
    if err:
        return None, [], err
    return new, notes, None


def _independent_why(criterion, accountable):
    """The passing evidence for the why-criterion that a seat OTHER than the
    accountable one recorded, no older than its latest fail — or None.

    OTHER THAN THE SEAT ACCOUNTABLE NOW, AND OTHER THAN THE SEAT THAT WAS
    ACCOUNTABLE WHEN THE PASS WAS RECORDED (`accountable` on the entry,
    written by `measure`). A pass the accountable seat recorded is
    self-certified when it lands and stays so after custody moves to another
    seat; the other seat's report may not spend it. An entry written before
    the field is judged against the seat accountable now."""
    ev = [e for e in criterion.get("evidence") or ()
          if isinstance(e, dict) and e.get("version") == criterion.get("version")]
    if not ev or ev[-1].get("pass") is not True:
        return None
    fails = [e.get("ts") or 0 for e in ev if e.get("pass") is not True]
    floor = max(fails) if fails else 0
    for e in reversed(ev):
        if e.get("pass") is True and (e.get("ts") or 0) >= floor \
                and not _same_seat(e.get("by"), accountable) \
                and not _same_seat(e.get("by"), e.get("accountable")):
            return e
    return None


def report(token, post_id, path=None):
    """(row, notes, err) — THE ONLY CLOSER of a met goal: the chat post that
    told the owner, after every criterion he approved passes and another seat
    re-ran the why-criterion's proof."""
    by, _who, err = _actor("report a goal done")
    if err:
        return None, [], err
    post = str(post_id or "").strip()
    if not post:
        return None, [], "report needs the chat-post id that told the owner"
    if len(post) > REF_MAX:
        return None, [], "the post id is over %d characters" % REF_MAX
    row, err = _live_goal(token, path)
    if err:
        return None, [], err
    tid, goal = row["id"], dict(row["goal"])
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return None, [], ("decision ledger unreadable (%s), so his yes cannot "
                          "be shown — nothing was closed" % unavailable)
    card = cards.get(str(goal.get("card") or ""))
    if _yes_elsewhere(card, goal):
        return None, [], (
            "the owner's yes on card %s was ruled on rev %s, and %s's "
            "criteria are bound to rev %s: he approved a different revision "
            "than the row carries. Run the same `helm goal revise %s` again "
            "to bind the row to the criteria he approved. Nothing was closed"
            % (card["id"], (card.get("verdict") or {}).get("rev"), tid,
               _bound_rev(goal), tid))
    if not approval(card, goal)[0]:
        return None, [], (
            "the owner has not said yes to %s's criteria (card %s is %s). "
            "DONE is measured against criteria he approved"
            % (tid, goal.get("card") or "not filed",
               (card or {}).get("status") or "missing"))
    short = []
    for c in _criteria(goal):
        e = _latest(c)
        if not e:
            short.append("%s has no evidence" % c.get("key"))
        elif e.get("pass") is not True:
            short.append("%s FAILED last (%s)" % (c.get("key"),
                                                   _excerpt(e.get("value"), 60)))
    if short:
        return None, [], (
            "%s is not met: %s. Record passing evidence with `helm goal "
            "measure %s <key> --pass <value> --how <command>`"
            % (tid, "; ".join(short), tid))
    accountable = tasks.owner_of(row)
    why = [c for c in _criteria(goal) if c.get("why_test")]
    if why and not _independent_why(why[0], accountable):
        return None, [], (
            "the why-criterion %s must be re-run by a seat other than %s, "
            "which is accountable for %s and never certifies its own why. "
            "Ask the integrator or another seat to run its proof and record "
            "`helm goal measure %s %s --pass <value> --how <command>`"
            % (why[0].get("key"), accountable, tid, tid, why[0].get("key")))
    goal["report_ref"] = post
    goal["report"] = {"ref": post, "ts": time.time(), "by": by}
    parts = []
    for c in _criteria(goal):
        e = _latest(c)
        parts.append("%s pass (%s, by %s)" % (
            c.get("key"), _excerpt(e.get("value"), 60), e.get("by")))
    reason = ("DONE: %d of %d approved criteria measured passing — %s. The "
              "owner was told in post %s. Values abridged; `helm goal show "
              "%s` has them whole" % (len(parts), len(parts), "; ".join(parts),
                                      post, tid))
    new, err = _update(row, "report", path=path, goal=goal, status="closed",
                       closed_reason=reason)
    if err:
        return None, [], err
    return new, [], None


def supersede(token, owner_ref, by_task=None, path=None):
    """(row, notes, err) — the owner replaced this goal; it closes citing him.

    `owner_ref` must resolve to a record he authored (`owner_word`), and the
    record it resolved to is written on the row beside the ref as typed."""
    by, _who, err = _actor("record a superseded goal")
    if err:
        return None, [], err
    owner_ref = str(owner_ref or "").strip()
    if not owner_ref:
        return None, [], ("no --owner-ref — a goal is superseded on the "
                          "owner's word; cite %s" % OWNER_WORD_FORMS)
    if len(owner_ref) > REF_MAX:
        return None, [], "--owner-ref is over %d characters" % REF_MAX
    row, err = _live_goal(token, path)
    if err:
        return None, [], err
    tid, goal = row["id"], dict(row["goal"])
    nid = None
    if by_task:
        nid = tasks.normalize_id(by_task)
        if not nid or nid == tid or not tasks.get(nid, path=path):
            return None, [], "--by %r is not another row in the ledger" % (
                by_task,)
    said, err = owner_word(owner_ref, goal, "--owner-ref", tid)
    if err:
        return None, [], ("%s closes as superseded only on the owner's word, "
                          "so nothing was written. %s" % (tid, err))
    goal["superseded"] = {"by": nid, "owner_ref": owner_ref,
                          "owner_said": said, "ts": time.time(),
                          "recorded_by": by}
    reason = "SUPERSEDED: the owner replaced this goal (%s)%s" % (
        said_text(said), ("; it continues as %s" % nid) if nid else "")
    new, err = _update(row, "supersede", path=path, goal=goal,
                       status="closed", closed_reason=reason)
    if err:
        return None, [], err
    notes = _comment_old_card(goal.get("card"), (
        "%s was superseded on the owner's word (%s). This card needs no "
        "answer." % (tid, said_text(said)))) if goal.get("card") else []
    return new, notes, None


# ---------------------------------------------------------------------------
# THE CYCLE — "X of Y goals done", and FIRST-WAVE YIELD (integrator decision
# 3): the share of a goal's approved criteria passing when its first wave of
# child tasks had all closed, which is exactly where work stops today.
# ---------------------------------------------------------------------------

def first_wave_closed(children, closed_at):
    """The epoch at which every child filed so far had closed, the first time
    that happened — or None while the first wave is still open."""
    filed = sorted(((c.get("ts") or 0), str(c.get("id"))) for c in children)
    for t in sorted(v for v in (closed_at.get(cid) for _ts, cid in filed) if v):
        wave = [cid for ts, cid in filed if ts <= t]
        if wave and all((closed_at.get(cid) or float("inf")) <= t
                        for cid in wave):
            return t
    return None


def wave_yield(row, history, closed, now):
    """{closed_ts, passing, total} or {pending: why}."""
    if closed is None:
        return {"pending": "first wave still open"}
    cutoff = closed + FIRST_WAVE_GRACE_S
    if cutoff > now:
        return {"pending": "first wave closed %s; read at +%dh"
                % (_when(closed), FIRST_WAVE_GRACE_S // 3600)}
    crit = _criteria(row["goal"])
    passing = 0
    for c in crit:
        ev = sorted((e for (k, v, _t, _b), e in history.items()
                     if k == c.get("key") and v == c.get("version")
                     and (e.get("ts") or 0) <= cutoff),
                    key=lambda e: e.get("ts") or 0)
        passing += 1 if ev and ev[-1].get("pass") is True else 0
    return {"closed_ts": closed, "passing": passing, "total": len(crit)}


def local_midnight(now=None):
    t = time.localtime(now or time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def cycle(since=None, now=None, path=None):
    """(report, err) — every number recomputed from the event-sourced rows.

    Y counts the goals the fleet is carrying: approved and not yet done, or
    done inside the window. Goals still waiting on his yes are counted apart,
    superseded ones are listed apart, and X is the goals reported done in the
    window."""
    now = now or time.time()
    since = local_midnight(now) if since is None else since
    closed_at, history = {}, {}

    def accept(row, prior):
        tid = str(row.get("id"))
        if row.get("status") == "closed":
            if not (isinstance(prior, dict) and prior.get("status") == "closed"):
                closed_at[tid] = row.get("last_updated") or row.get("ts")
        else:
            closed_at.pop(tid, None)
        goal = row.get("goal")
        if isinstance(goal, dict):
            h = history.setdefault(tid, {})
            for c in _criteria(goal):
                for e in c.get("evidence") or ():
                    if isinstance(e, dict):
                        h.setdefault((c.get("key"), e.get("version"),
                                      e.get("ts"), e.get("by")), e)
        return True

    known, unavailable = tasks.snapshot(path, accept=accept)
    if unavailable:
        return None, "task ledger unreadable (%s)" % unavailable
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return None, "decision ledger unreadable (%s)" % unavailable
    kids = children_of(known)
    out = {"since": since, "now": now, "goals": [], "superseded": [],
           "waiting_on_yes": 0, "done": 0, "counted": 0,
           "criteria": [0, 0], "first_wave": [0, 0, 0]}
    for tid in sorted(kids, key=lambda t: tasks.sort_key(known[t])):
        row = known[tid]
        goal = row["goal"]
        st = state(row, cards.get(str(goal.get("card") or "")), kids[tid])
        wave = wave_yield(row, history.get(tid, {}),
                          first_wave_closed(kids[tid], closed_at), now)
        line = {"id": tid, "title": row.get("title"),
                "accountable": tasks.owner_of(row) or None,
                "state": st["state"], "met": st["met"], "total": st["total"],
                "first_wave": wave}
        if st["state"] == "superseded":
            if ((goal.get("superseded") or {}).get("ts") or 0) >= since:
                out["superseded"].append(line)
            continue
        if st["state"] in WAITING_ON_YES:
            out["waiting_on_yes"] += 1
            out["goals"].append(line)
            continue
        done_ts = (goal.get("report") or {}).get("ts") or 0
        if st["state"] == "done" and done_ts < since:
            continue
        out["counted"] += 1
        out["done"] += st["state"] == "done"
        out["criteria"][0] += st["met"]
        out["criteria"][1] += st["total"]
        if "closed_ts" in wave and wave["closed_ts"] >= since:
            out["first_wave"][0] += wave["passing"]
            out["first_wave"][1] += wave["total"]
            out["first_wave"][2] += 1
        out["goals"].append(line)
    return out, None


def _pct(a, b):
    return "%d%%" % round(100.0 * a / b) if b else "n/a"


def bar(met, total):
    return "▰" * met + "▱" * max(0, total - met)


def render_cycle(rep):
    since = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(rep["since"]))
    fw = rep["first_wave"]
    lines = [
        "goals this cycle (since %s): %d of %d done (%s)"
        % (since, rep["done"], rep["counted"], _pct(rep["done"], rep["counted"])),
        "criteria: %d of %d approved criteria measured-pass (%s)"
        % (rep["criteria"][0], rep["criteria"][1],
           _pct(*rep["criteria"])),
        ("first-wave yield: %s (%d of %d criteria passing %dh after each "
         "goal's first wave of child tasks had all closed; %d goal%s; target "
         "80%%)" % (_pct(fw[0], fw[1]), fw[0], fw[1],
                    FIRST_WAVE_GRACE_S // 3600, fw[2],
                    "" if fw[2] == 1 else "s")) if fw[2] else
        "first-wave yield: no goal's first wave closed in this cycle",
        "waiting on the owner's yes: %d" % rep["waiting_on_yes"]]
    for g in rep["goals"]:
        w = g["first_wave"]
        lines.append("  %-7s %d/%d %-17s %-10s %s (@%s)%s" % (
            bar(g["met"], g["total"]), g["met"], g["total"], g["state"],
            g["id"], _excerpt(g["title"], 60), g["accountable"] or "?",
            (" · first wave %d/%d" % (w["passing"], w["total"]))
            if "passing" in w else ""))
    for g in rep["superseded"]:
        lines.append("  superseded this cycle: %s %s" % (
            g["id"], _excerpt(g["title"], 60)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# RENDERING
# ---------------------------------------------------------------------------

def badge(row):
    """`[goal 2/5]` for a task listing: read from the row alone, so a listing
    of every task pays no card or child read for it."""
    goal = row.get("goal") or {}
    crit = _criteria(goal)
    met = sum(1 for c in crit if (_latest(c) or {}).get("pass") is True)
    word = ("superseded" if goal.get("superseded") else
            "done" if goal.get("report_ref") else "goal")
    return "[%s %d/%d]" % (word, met, len(crit))


def task_show_lines(row):
    """The goal block `helm task show` prints: the record, not the derived
    state, which needs the card and the children (`helm goal show`)."""
    goal = row["goal"]
    out = ["%-14s %s criteria v%s, card %s — state and children: `helm goal "
           "show %s`" % ("goal", badge(row), goal.get("criteria_version"),
                         goal.get("card") or "NOT FILED", row["id"]),
           "%-14s %s" % ("said at", goal.get("words_ref")),
           "%-14s %s" % ("why", goal.get("why") or "not recorded")]
    for c in _criteria(goal):
        e = _latest(c)
        out.append("%-14s %s%s %s :: %s%s" % (
            "criterion", c.get("key"), " [why]" if c.get("why_test") else "",
            c.get("text"), c.get("proof"),
            "" if not e else " — latest %s %r by %s" % (
                "PASS" if e.get("pass") else "FAIL", e.get("value"),
                e.get("by"))))
    return out


_FLAG_WORDS = {"card-not-filed": "criteria card NOT FILED",
               "card-missing": "criteria card missing from the decision ledger",
               "card-not-pushed": "card NOT PUSHED to his phone",
               "yes-other-rev": ("he approved a different revision of the "
                                 "card than the row carries"),
               "card-asker-behind": ("the card still answers to its previous "
                                     "seat; the accountable seat's `helm goal "
                                     "revise` moves it")}


def next_step(tid, e):
    """What the state asks for next, in one clause, or ""."""
    st = e["state"]
    if "yes-other-rev" in (e.get("flags") or ()):
        return ("his yes is on rev %s; run the same `helm goal revise %s` "
                "again to bind the row to it"
                % ((e.get("card") or {}).get("rev"), tid))
    if st == "commented":
        return "he commented; answer with `helm goal revise %s`" % tid
    if st in ("proposed", "revised"):
        return "waiting on his yes"
    if st == "criteria-measured":
        return "measured, report owed: `helm goal report %s <chat-post-id>`" % tid
    if st == "done":
        return "reported in post %s" % e.get("report_ref")
    if st == "superseded":
        s = e.get("superseded") or {}
        return "superseded (%s)" % (said_text(s.get("owner_said"))
                                    or "post %s" % s.get("owner_ref"))
    return ""


def list_line(tid, e):
    met, total = e["pct"]
    tail = [next_step(tid, e)] + [_FLAG_WORDS.get(f, f)
                                  for f in e.get("flags") or ()]
    return "  %-7s %d/%d  %-17s %-10s %s  @%s%s" % (
        bar(met, total), met, total, e["state"], tid,
        _excerpt(e.get("title"), 70), e.get("accountable") or "?",
        "".join(" · " + t for t in tail if t))


def show_lines(tid, row, e, card=None):
    """`helm goal show`: the record, the derived state and the next step.
    `card` is the current card row, for his open comment's words."""
    goal = row["goal"]
    out = ["%s  %s" % (tid, row.get("title")),
           "  state        %s (%d of %d criteria passing)%s" % (
               e["state"], e["pct"][0], e["pct"][1],
               "".join(" · " + _FLAG_WORDS.get(f, f) for f in e["flags"])),
           "  accountable  %s" % (e.get("accountable") or "?"),
           "  said at      %s" % goal.get("words_ref"),
           "  his words    \"%s\"" % str(goal.get("words") or "").replace(
               "\n", "\n               "),
           "  why          %s" % (goal.get("why") or "not recorded")]
    step = next_step(tid, e)
    if step:
        out.append("  next         %s" % step)
    ec = e.get("card")
    if ec:
        out.append("  card         %s %s rev %s%s%s (criteria v%s)%s" % (
            ec["id"], ec.get("status") or "MISSING", ec.get("rev") or "?",
            "" if ec.get("pushed") in (None, True) else " NOT PUSHED",
            (" · he said yes at %s" % _when(ec["approved_ts"]))
            if ec.get("approved_ts") else "",
            e.get("criteria_version"),
            ("; earlier cards: " + ", ".join(e["cards"])) if e["cards"] else ""))
        for c in (card or {}).get("comments") or ():
            if isinstance(c, dict) and c.get("by") == ownerasks.OWNER:
                out.append("  he said      %s (%s)" % (c.get("text"),
                                                      c.get("ts")))
    else:
        out.append("  card         NOT FILED — `helm goal propose %s`" % tid)
    out.append("  criteria:")
    for c in _criteria(goal):
        latest = _latest(c)
        mark = "○" if not latest else ("✓" if latest.get("pass") else "✗")
        out.append("    %s %s%s %s" % (mark, c.get("key"),
                                       " [why]" if c.get("why_test") else "",
                                       c.get("text")))
        out.append("        proof: %s" % c.get("proof"))
        if latest:
            out.append("        latest: %s %r by %s at %s%s (how: %s)" % (
                "PASS" if latest.get("pass") else "FAIL", latest.get("value"),
                latest.get("by"), _when(latest.get("ts")),
                " [before his yes]" if latest.get("pre_approval") else "",
                latest.get("how")))
    kids = e["children"]
    out.append("  children     %d open, %d closed" % (kids["open"],
                                                      kids["closed"]))
    for k in kids["rows"]:
        out.append("    %s  %s" % (k["id"], k["status"]))
    if goal.get("report"):
        out.append("  reported     post %s by %s at %s" % (
            goal["report"].get("ref"), goal["report"].get("by"),
            _when(goal["report"].get("ts"))))
    if goal.get("superseded"):
        s = goal["superseded"]
        out.append("  superseded   %s%s" % (
            said_text(s.get("owner_said")) or "post %s" % s.get("owner_ref"),
            (" by %s" % s["by"]) if s.get("by") else ""))
    for r in goal.get("reopened") or ():
        if isinstance(r, dict):
            out.append("  reopened     %s on %s (%s), after report %s" % (
                _when(r.get("ts")), r.get("key"),
                said_text(r.get("owner_said")) or "ref %s" % r.get("ref"),
                r.get("report_ref")))
    return out


# ---------------------------------------------------------------------------
# THE CLI
# ---------------------------------------------------------------------------

USAGE = """usage: helm goal add <title...> --owner SEAT --words-ref REF [--why TEXT] [--from task/NNN] [--project NAME] [--force-new]
       helm goal propose <goal>
       helm goal revise <goal>
       helm goal measure <goal> <key> --pass VALUE|--fail VALUE --how CMD [--ref R]
       helm goal report <goal> <chat-post-id>
       helm goal supersede <goal> --owner-ref REF [--by task/NNN]
       helm goal show <goal> [--json]
       helm goal list [--all|--done] [--json]
       helm goal cycle [--since WHEN|--days N] [--json]
       helm goal sync
  `add` reads its body on stdin (a quoted heredoc): a WORDS: line, the
  owner's words verbatim, then a CRITERIA: line and at most 7 criteria
  `- <outcome> :: <proof>`, with `-! ` marking the one that tests his why.
  `revise` reads new CRITERIA: lines the same way. The criteria go to the
  owner as one decision card; he says yes or comments on the web queue. A
  revise answers his comment on that same card at its next rev (only its
  asker, the goal's accountable seat, revises it; the open card moves with
  the goal when that seat changes); after his verdict, a new card.
  `supersede --owner-ref` and a `measure --fail --ref` that reopens a done
  goal cite the OWNER'S word, and the ref must resolve to a record he
  authored: a chat row he posted through one of his doors (its id, or 4+
  characters of it) or card:<id>[@<ts>] for his comment on the goal's own
  criteria card. It must be dated strictly after that card's current
  revision (filed, or last revised), and never be the post the goal's
  --words-ref names: the words that created a
  goal cannot replace or reopen it. A reopen's word is also dated strictly
  after the report that marked the goal done. The card's Yes verdict
  approves criteria; it cannot replace or dispute the goal. Anything else,
  or a store that cannot be read, refuses and writes nothing."""

# EVERY SUBVERB'S FLAGS, ONE TABLE: (valued, boolean). The handlers take
# their flags from here and the `--help` guard refuses a flag not in it, so the
# vocabulary a subverb accepts and the one it advertises cannot drift apart.
FLAGS = {
    "add": (("--owner", "--words-ref", "--why", "--from", "--project"),
            ("--force-new",)),
    "propose": ((), ()),
    "revise": ((), ()),
    "measure": (("--pass", "--fail", "--how", "--ref"), ()),
    "report": ((), ()),
    "supersede": (("--owner-ref", "--by"), ()),
    "show": ((), ("--json",)),
    "list": ((), ("--all", "--done", "--json")),
    "cycle": (("--since", "--days"), ("--json",)),
    "sync": ((), ()),
}
SUBVERBS = tuple(FLAGS)


def _take(rest, flag):
    """(value, err) — presence read BEFORE the take, so a flag typed with no
    value refuses instead of reading as never passed."""
    typed = any(t == flag or t.startswith(flag + "=") for t in rest)
    value = tasks._take(rest, flag)
    if typed and value is None:
        return None, ("%s wants a value (spell one that starts with a dash as "
                      "%s=VALUE)" % (flag, flag))
    return value, None


def _takes(rest, flags):
    """({flag: value}, err) for every valued flag, each at most once."""
    got = {}
    for flag in flags:
        if sum(1 for t in rest if t == flag or t.startswith(flag + "=")) > 1:
            return None, "%s given twice" % flag
        got[flag], err = _take(rest, flag)
        if err:
            return None, err
    return got, None


def _bools(rest, flags):
    got = {f: f in rest for f in flags}
    rest[:] = [t for t in rest if t not in flags]
    return got


def _positionals(rest, n, what):
    """(values, err) — exactly `n` positionals and no stray flag."""
    stray = [t for t in rest if freetext.FLAG.match(t)]
    if stray:
        return None, "unknown flag %s" % stray[0]
    if len(rest) != n:
        return None, "wants %s" % what
    return list(rest), None


def _stdin():
    return "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()


def _say(notes, line):
    print(line)
    for n in notes:
        print("helm goal: " + n, file=sys.stderr)
    return 0


def _refuse(err, rc=2):
    # THE DOOR IS ALREADY ON THE SENTENCE when the project check wrote it.
    # require_project starts with "helm goal add:"; a second "helm goal:"
    # in front of that is the double prefix (task/3994). A sentence that
    # names no door still gets this one.
    text = str(err)
    if not text.startswith("helm goal"):
        text = "helm goal: " + text
    print(text, file=sys.stderr)
    return rc


def _fold_for_read():
    """(known, cards, tasks_source, err) for the read verbs. The task
    ledger's source is measured BEFORE the fold, so a projection refreshed
    from this fold is dated to what it folded (`refresh_projection`)."""
    src = _source(tasks.ledger_path())
    known, unavailable = tasks.snapshot()
    if unavailable:
        return None, None, None, ("task ledger unreadable (%s) — goals "
                                  "UNKNOWN" % unavailable)
    cards, unavailable = ownerasks.decisions_snapshot()
    if unavailable:
        return None, None, None, ("decision ledger unreadable (%s) — goal "
                                  "states UNKNOWN" % unavailable)
    return known, cards, src, None


def _since(raw, days):
    if raw is not None and days is not None:
        return None, "--since and --days are alternatives; give one"
    if days is not None:
        try:
            n = float(days)
        except ValueError:
            return None, "--days wants a number, got %r" % days
        if n <= 0:
            return None, "--days wants a positive number"
        return time.time() - n * 86400, None
    if raw is None:
        return None, None
    try:
        return float(raw), None
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(raw, fmt)), None
        except ValueError:
            continue
    epoch = pk.parse_ts_epoch(raw)
    if epoch is None:
        return None, ("--since %r is not a time — give an epoch, YYYY-MM-DD, "
                      "YYYY-MM-DDTHH:MM (local) or a UTC stamp" % raw)
    return epoch, None


def _cmd_add(rest):
    opts, err = _takes(rest, FLAGS["add"][0])
    if err:
        return _refuse(err)
    force_new = _bools(rest, FLAGS["add"][1])["--force-new"]
    title, rc = freetext.tail("helm goal", "add", rest, "the goal title")
    if rc is not None:
        return rc
    if not (title or "").strip() and not opts["--from"]:
        return _refuse("add needs a title (or --from task/NNN to promote a "
                       "row)\n" + USAGE)
    # A NEW GOAL NAMES ITS PROJECT, through the check `helm task add` makes
    # (task/3745). `--from` promotes a row that already exists and files
    # nothing new, so it keeps the plain scope read.
    if opts["--from"]:
        project, _how, err = tasks.resolve_scope(opts["--project"],
                                                 "helm goal add")
    else:
        project, _how, err = tasks.require_project(opts["--project"],
                                                   "helm goal add")
    if err:
        return _refuse(err)
    words, criteria, err = parse_body(_stdin())
    if err:
        return _refuse(err)
    row, notes, err = add((title or "").strip(), opts["--owner"], words,
                          opts["--words-ref"], opts["--why"], criteria,
                          from_task=opts["--from"], project=project,
                          force_new=force_new)
    if err:
        return _refuse(err, 1)
    card = row["goal"].get("card")
    return _say(notes, "goal %s %s: %s — %d criteria, accountable %s; %s"
                % (row["id"], "promoted" if opts["--from"] else "captured",
                   row.get("title"), len(row["goal"]["criteria"]),
                   tasks.owner_of(row),
                   ("card %s is in the owner's queue" % card) if card
                   else "NO CARD in his queue yet"))


def _cmd_propose(rest):
    got, err = _positionals(rest, 1, "a goal id")
    if err:
        return _refuse("propose %s" % err)
    row, notes, err = propose(got[0])
    if err:
        return _refuse(err, 1)
    return _say(notes, "goal %s: card %s carries criteria v%d" % (
        row["id"], row["goal"].get("card"), row["goal"]["criteria_version"]))


def _cmd_revise(rest):
    got, err = _positionals(rest, 1, "a goal id")
    if err:
        return _refuse("revise %s" % err)
    _w, criteria, err = parse_body(_stdin(), words=False)
    if err:
        return _refuse(err)
    row, notes, err = revise(got[0], criteria)
    if err:
        return _refuse(err, 1)
    return _say(notes, "goal %s: criteria v%d, card %s" % (
        row["id"], row["goal"]["criteria_version"], row["goal"].get("card")))


def _cmd_measure(rest):
    opts, err = _takes(rest, FLAGS["measure"][0])
    if err:
        return _refuse(err)
    if (opts["--pass"] is None) == (opts["--fail"] is None):
        return _refuse("measure takes exactly one of --pass VALUE or --fail "
                       "VALUE")
    got, err = _positionals(rest, 2, "a goal id and a criterion key")
    if err:
        return _refuse("measure %s" % err)
    passed = opts["--pass"] is not None
    row, notes, err = measure(got[0], got[1], passed,
                              opts["--pass"] if passed else opts["--fail"],
                              opts["--how"], opts["--ref"])
    if err:
        return _refuse(err, 1)
    e = [x for x in row["goal"]["criteria"] if x["key"] == got[1]][0]
    return _say(notes, "goal %s %s: %s recorded" % (
        row["id"], got[1], "PASS" if passed else "FAIL") + (
        " (before his yes)" if _latest(e).get("pre_approval") else ""))


def _cmd_report(rest):
    if len(rest) < 2:
        return _refuse("report needs a goal id and the chat-post id that "
                       "told the owner")
    post, rc = freetext.tail("helm goal", "report", rest[1:], "the post id")
    if rc is not None:
        return rc
    row, notes, err = report(rest[0], post)
    if err:
        return _refuse(err, 1)
    return _say(notes, "goal %s DONE — reported in post %s" % (
        row["id"], row["goal"]["report_ref"]))


def _cmd_supersede(rest):
    opts, err = _takes(rest, FLAGS["supersede"][0])
    if err:
        return _refuse(err)
    got, err = _positionals(rest, 1, "a goal id")
    if err:
        return _refuse("supersede %s" % err)
    row, notes, err = supersede(got[0], opts["--owner-ref"], opts["--by"])
    if err:
        return _refuse(err, 1)
    return _say(notes, "goal %s superseded (%s)" % (
        row["id"], said_text(row["goal"]["superseded"]["owner_said"])))


def _cmd_show(rest):
    as_json = _bools(rest, FLAGS["show"][1])["--json"]
    got, err = _positionals(rest, 1, "a goal id")
    if err:
        return _refuse("show %s" % err)
    known, cards, src, err = _fold_for_read()
    if err:
        return _refuse(err, 1)
    tid = tasks.normalize_id(got[0])
    row = known.get(tid or "")
    if not row or not isinstance(row.get("goal"), dict):
        return _refuse("%s is not a goal" % (tid or got[0]), 1)
    kids = children_of(known)
    e = entry(row, cards.get(str(row["goal"].get("card") or "")), kids[tid])
    refresh_projection(known, tasks_source=src)
    if as_json:
        print(json.dumps({"id": tid, "goal": row["goal"], "derived": e},
                         indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    print("\n".join(show_lines(tid, row, e, cards.get(
        str(row["goal"].get("card") or "")))))
    return 0


def _cmd_list(rest):
    flags = _bools(rest, FLAGS["list"][1])
    got, err = _positionals(rest, 0, "no arguments")
    if err:
        return _refuse("list %s" % err)
    if flags["--all"] and flags["--done"]:
        return _refuse("--all already includes --done; give one")
    known, cards, src, err = _fold_for_read()
    if err:
        return _refuse(err, 1)
    proj, perr = refresh_projection(known, tasks_source=src)
    if proj is None:
        return _refuse(perr, 1)
    goals_ = proj["goals"]
    if flags["--done"]:
        pick = {t: e for t, e in goals_.items() if e["state"] == "done"}
    elif flags["--all"]:
        pick = goals_
    else:
        pick = {t: e for t, e in goals_.items() if e["state"] not in TERMINAL}
    if flags["--json"]:
        print(json.dumps(pick, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    label = "done" if flags["--done"] else "all" if flags["--all"] else "open"
    print("goals (%s): %d" % (label, len(pick)))
    for tid, e in pick.items():
        print(list_line(tid, e))
    if perr:
        print("helm goal: " + perr, file=sys.stderr)
    return 0


def _cmd_cycle(rest):
    as_json = _bools(rest, FLAGS["cycle"][1])["--json"]
    opts, err = _takes(rest, FLAGS["cycle"][0])
    if err:
        return _refuse(err)
    got, err = _positionals(rest, 0, "no arguments")
    if err:
        return _refuse("cycle %s" % err)
    since, err = _since(opts["--since"], opts["--days"])
    if err:
        return _refuse(err)
    rep, err = cycle(since)
    if err:
        return _refuse(err, 1)
    if as_json:
        print(json.dumps(rep, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    print(render_cycle(rep))
    return 0


def _cmd_sync(rest):
    got, err = _positionals(rest, 0, "no arguments")
    if err:
        return _refuse("sync %s" % err)
    proj, err = refresh_projection()
    if proj is None:
        return _refuse(err, 1)
    print("goals projection rewritten: %d goal%s (%s)" % (
        len(proj["goals"]), "" if len(proj["goals"]) == 1 else "s",
        projection_path()))
    if err:
        print("helm goal: " + err, file=sys.stderr)
        return 1
    return 0


_HANDLERS = {"add": _cmd_add, "propose": _cmd_propose, "revise": _cmd_revise,
             "measure": _cmd_measure, "report": _cmd_report,
             "supersede": _cmd_supersede, "show": _cmd_show,
             "list": _cmd_list, "cycle": _cmd_cycle, "sync": _cmd_sync}


def cmd_goal(args):
    """goal add|propose|revise|measure|report|supersede|show|list|cycle|sync
    — the owner's goals, done on criteria he approved."""
    args = list(args or [])
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE, file=sys.stderr if not args else sys.stdout)
        return 2 if not args else 0
    verb, rest = args[0], args[1:]
    if verb not in SUBVERBS:
        from .cli import suggest
        print("helm goal: unknown subverb %r%s\n%s"
              % (verb, suggest(verb, SUBVERBS), USAGE), file=sys.stderr)
        return 2
    if "--help" in rest or "-h" in rest:
        # HELP AFTER JUNK STILL REFUSES, so `--help` stays an honest probe
        # of whether a flag exists (cli.guard_tail's contract).
        known = FLAGS[verb][0] + FLAGS[verb][1] + ("--help", "-h")
        junk = [t for t in rest if freetext.FLAG.match(t)
                and t.split("=", 1)[0] not in known]
        if junk:
            return _refuse("%s: unknown flag %s\n%s" % (verb, junk[0], USAGE))
        print(USAGE)
        return 0
    return _HANDLERS[verb](rest)
