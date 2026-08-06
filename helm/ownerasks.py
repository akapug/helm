#!/usr/bin/env python3
"""helm ownerasks — the OWNER-ASK LEDGER: the durable fix for dropped owner
asks (root cause: asks lived in memory-only panes + siloed scratch, and
report-back went to AGENTS, not the owner).

One append-only jsonl at `~/.helm/_global/owner-asks.jsonl` — the canonical
fleet owner-ask list (premise: builtin-tasklist-mirrored-to-dregg is the
fleet coordination primitive; agents SELF-ADD, a2a-self-add culture). Every
mutation appends a full SNAPSHOT row (event-sourced: last line per id wins),
so writes are O(1) — one unbuffered O_APPEND write, never a read — and the
history is never lost. Mutations never raise and a refused add says so loudly;
reads distinguish absent/empty from UNAVAILABLE, so owner debt is UNKNOWN on
CLI/stop surfaces rather than silently disappearing.

Row schema (every snapshot carries the full shape):
  {id, ts, ask, source, status: open|done|reported,
   done_ref, report_ref, last_updated}

THE BAR (owner-surface-is-the-bar): `done` does NOT close a row. Work merely
finished is invisible work; only `report` — the chat-post id that told the
OWNER — moves a row to `reported`. Anything not `reported` is still open
fleet debt, and the stop-whisper's top rung (seats._ask_candidate) keeps
naming the oldest such row until the owner has actually been told.
"""
import json
import os
import sys

from . import eventledger, home, pk

STATUSES = ("open", "done", "reported")


def ledger_path(name="owner-asks.jsonl"):
    return os.path.join(home.global_dir(), name)


def _append(row, path=None):
    """Append one durable event.  The shared primitive owns symlink defense,
    serialization, one-write O_APPEND, fsync, short-write rollback, and 0600
    permissions.  False is the fail-open mutation result; callers surface it."""
    return eventledger.append(path or ledger_path(), row)


def snapshot(path=None):
    """(rows, unavailable). Missing is known-empty; unsafe/unreadable storage
    is UNKNOWN and must surface on owner-debt list/stop paths."""
    return eventledger.latest_checked(path or ledger_path())


def rows(path=None):
    return snapshot(path)[0]


# The one refusal a caller must be able to tell apart WITHOUT reading prose:
# a write the storage declined is an operational fault, everything else is the
# caller's input. Compared by value against this constant, never sniffed out of
# the message, so rewording the sentence cannot change an exit code.
UNWRITABLE = "ledger unwritable"


def add(ask, source=None, needs=None):
    """Open a new ask row — the owner's words verbatim. -> (row, error).
    Exactly one of the pair is None.

    THE PAIR IS THE SHAPE THE SIBLING LEDGER ALREADY USES (`tasks.add`, same
    docstring sentence), and it is here because a bare None could not say WHICH
    refusal happened. This verb has three — no words, unreadable, storage
    declined — and they owe the operator different sentences and different exit
    codes. Returning None for all three forced `cmd_asks` to RE-DERIVE the
    reason by asking the clarity checker a second time, which put a second
    evaluation of an env-reading predicate on the path purely to recover
    information this function already had. The pair deletes that machinery
    rather than defending it. (@offbox-claude bound FIX on exactly this:
    the module law says mutations never RAISE — it says nothing about
    return-shape poverty, and I had been using the law as cover for the
    conflation I had bound FIX on twice the same night in other seats' code.)

    THE GUARD SITS ON THE WRITE, NOT ON ONE DOOR (task/373, @cj's forward risk
    on the task/353 review). This ledger has add|done|report|list and NO edit
    verb, so a row the owner cannot read could never be fixed after the append.
    The check lived only in `cmd_asks`, leaving `add` — a PUBLIC function — one
    new caller away from filing a permanent unreadable row; the web queue and
    the hooks are the obvious next callers. Measured when it moved: 18 distinct
    `add` fixture strings across tests/, 0 of them refused.

    `needs` NAMES THE HUMAN-ONLY INPUT this ask is waiting on. It is optional
    HERE so historical rows and library callers keep working; the CLI requires
    it for new writes (see cmd_asks), the same split `kind` and verdict
    `polarity` already took."""
    ask = (ask or "").strip()
    if not ask:
        return None, "an ask needs words"
    refusal = _clarity_refusal(" ".join(x for x in (ask, needs) if x))
    if refusal:
        return None, refusal
    ts = pk.now_ts()
    rid = os.urandom(4).hex()
    row = {"id": rid, "ts": ts, "ask": ask,
           "source": source or home.session_id() or "cli",
           "needs": (needs or "").strip() or None,
           "status": "open", "done_ref": None, "report_ref": None,
           "last_updated": ts}
    if not _append(row):
        return None, UNWRITABLE
    pk.event("asks-add", rid, ask)
    return row, None


def _update(rid, status, **patch):
    """Append the row's next snapshot under one read/validate/write lock.
    Concurrent done/report calls therefore cannot reopen a reported ask."""
    path = ledger_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — update NOT recorded" % path
        current, unavailable = eventledger.latest_checked(path)
        if unavailable:
            return None, "owner-ask ledger unavailable: %s" % unavailable
        r = current.get(str(rid or ""))
        if not r:
            return None, "no such ask: %s (helm asks list)" % rid
        if r.get("status") == "reported":
            return None, "ask %s already reported (closed) — add a new ask" % rid
        row = dict(r)
        row.update(patch)
        row["status"] = status
        row["last_updated"] = pk.now_ts()
        if not eventledger.append_unlocked(path, row):
            return None, "ledger unwritable (%s) — update NOT recorded" % path
    pk.event("asks-" + status, str(rid), str(patch.get("done_ref")
                                             or patch.get("report_ref") or ""))
    return row, None


def mark_done(rid, evidence):
    """Work landed (commit/evidence) — the row stays OPEN DEBT until report."""
    if not (evidence or "").strip():
        return None, "done needs evidence (a commit sha / artifact ref)"
    return _update(rid, "done", done_ref=str(evidence).strip())


def mark_report(rid, post_id):
    """THE ONLY CLOSER: the chat-post id that told the OWNER."""
    if not (post_id or "").strip():
        return None, "report needs the chat-post id that told the owner"
    return _update(rid, "reported", report_ref=str(post_id).strip())


def unreported():
    """Every row the owner has NOT been told about (open OR done), oldest
    first — the stop-whisper rung reads [0]."""
    rs = [r for r in rows().values() if r.get("status") != "reported"]
    rs.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
    return rs


def oldest_unreported():
    rs = unreported()
    return rs[0] if rs else None


def stop_candidate():
    current, unavailable = snapshot()
    if unavailable:
        return None, unavailable
    rs = [r for r in current.values() if r.get("status") != "reported"]
    rs.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
    return (rs[0] if rs else None), None


# ---------------------------------------------------------------------------
# DECISION CARDS — the ask primitive GENERALIZED, not twinned (owner,
# 2026-08-04: "that store queue seems like it could be more generally
# useful… can't /refine proposals go directly here? … so that I can just go
# review a durable queue that reaches you via helm/beacon").
#
# An ASK waits on a HUMAN-ONLY INPUT (a credential, a physical act); a
# DECISION waits on a RULING among named options. Same mechanics, second
# ledger file: event-sourced full-snapshot rows through the SAME _append/
# snapshot helpers (ledger_path took a name argument from birth for exactly
# this), same fail-open writes and UNKNOWN-not-empty reads.
#
# The lifecycle reuses the ask ledger's central law under new names:
#   open -> decided -> delivered.
# `decided` does NOT close a card, exactly as `done` does not close an ask —
# a ruling the FILING SEAT never heard is the lost-to-an-unwatched-pane bug
# this queue exists to kill. Only the DM that reached the asker (delivered,
# via the beacon every seat already arms) closes it.
#
# The owner is GUI-first: the web queue (web.py /api/decisions + the console's
# "owner decisions" section) is the decision surface, and its POST routes call
# THE SAME decide/deliver functions the CLI does (one writer path, the store-
# review precedent). On the board's owner_gated_queue key, sync_board owns
# ONLY the rows it stamps src=ledger (projected from the two ledgers, so they
# cannot drift); hand-written rows — owner gates awaiting migration,
# informational FYIs — pass through verbatim (see sync_board).
# ---------------------------------------------------------------------------

DECISIONS_LEDGER = "owner-decisions.jsonl"
DECISION_STATUSES = ("open", "decided", "delivered")
MAX_OPTIONS = 9
CTX_HEAD = 200          # the board projection's phone-readable context head


def decisions_path():
    return ledger_path(DECISIONS_LEDGER)


def decisions_snapshot():
    """(cards-by-id, unavailable) — same contract as snapshot()."""
    return eventledger.latest_checked(decisions_path())


def decision_rows():
    return decisions_snapshot()[0]


def default_options():
    """The implicit approve/reject pair: a card filed with no option lines is
    a yes/no question, and materializing the pair (rather than special-casing
    optionless cards) keeps every verdict THE SAME SHAPE — a chosen option."""
    return [
        {"key": "approve", "label": "Approve",
         "consequence": "proceed as proposed", "recommended": False},
        {"key": "reject", "label": "Reject",
         "consequence": "do not proceed — the asker re-plans",
         "recommended": False},
    ]


def parse_card_body(body):
    """The heredoc grammar -> (context, options, err).

    Everything before the first option line is the CONTEXT paragraph (phone-
    readable — the owner decides from his couch, not from a terminal). An
    option line is `* <label> :: <one-line consequence>`; `*!` marks THE
    recommended one. The `::` is REQUIRED mechanically, like --needs on an
    ask: an option whose consequence is unstated makes the owner reconstruct
    the trade-off himself, which is the exact work the card exists to do for
    him. No option lines at all -> the approve/reject pair."""
    context, opts = [], []
    for line in (body or "").splitlines():
        s = line.strip()
        if not s.startswith("*"):
            if opts and s:
                return None, None, ("context must come BEFORE the option "
                                    "lines — %r follows an option" % s[:40])
            if not opts:
                context.append(line)
            continue
        rec = s.startswith("*!")
        s = s[(2 if rec else 1):].strip()
        if "::" not in s:
            return None, None, (
                "option %r has no `:: <consequence>` — every option names its "
                "one-line consequence, or the owner is left to reconstruct the "
                "trade-off the card exists to spell out" % s[:40])
        label, _sep, consequence = s.partition("::")
        label, consequence = label.strip(), consequence.strip()
        if not label or not consequence:
            return None, None, ("option needs BOTH halves: "
                                "`* <label> :: <one-line consequence>`")
        opts.append({"key": str(len(opts) + 1), "label": label,
                     "consequence": consequence, "recommended": rec})
    context = "\n".join(context).strip()
    if not context:
        return None, None, (
            "a card needs a CONTEXT paragraph (the body before any option "
            "lines) — a title alone makes the owner go find the story")
    if len(opts) > MAX_OPTIONS:
        return None, None, ("%d options — more than %d is a questionnaire, "
                            "not a decision" % (len(opts), MAX_OPTIONS))
    if sum(1 for o in opts if o["recommended"]) > 1:
        return None, None, "at most ONE option may carry the `*!` recommend mark"
    return context, (opts or default_options()), None


def file_decision(title, context, options, asker, refs=None, source=None):
    """Open a decision card -> (row, problem).

    row=None means NOT RECORDED and problem says why (fatal). row set with
    problem set means the durable card landed but a best-effort side surface
    (the one-line room announcement) failed — advisory, caller prints it.
    `asker` is the verdict's RETURN ADDRESS and is required: a card no answer
    can come back to recreates the lost-verdict bug."""
    title = " ".join(str(title or "").split())
    if not title:
        return None, "a decision card needs a title"
    if not str(context or "").strip():
        return None, "a decision card needs its context paragraph"
    asker = str(asker or "").strip().lstrip("@")
    if not asker:
        return None, ("a decision card needs --asker <seat> — the verdict's "
                      "return address")
    # VALIDATE THE RETURN ADDRESS AT FILE TIME. The card has no address-edit
    # verb, so a malformed asker ("bad name!") would file, get decided, and
    # then rest forever undeliverable — the lost-verdict bug wearing a
    # delivered-looking card (codex review, 2026-08-04). Exact canonical seat
    # token or no card.
    from . import seats
    _canon, aerr = seats._canonical_recipient(asker)
    if aerr:
        return None, ("asker %r is not a deliverable seat address — %s "
                      "(1-64 chars of [A-Za-z0-9._-], the exact seat token)"
                      % (asker, aerr))
    ts = pk.now_ts()
    rid = os.urandom(4).hex()
    row = {"id": rid, "ts": ts, "kind": "decision", "title": title,
           "context": str(context).strip(),
           "options": list(options or default_options()),
           "asker": asker,
           "refs": [str(r).strip() for r in (refs or []) if str(r).strip()],
           "source": source or home.session_id() or "cli",
           "status": "open", "verdict": None, "comments": [],
           # WAS THE OWNER ACTUALLY TOLD? None until a headline leaves this box
           # for his phone. Part of the shape from the start rather than
           # inferred later, because "no field" and "never pushed" are the same
           # bytes and only one of them is a fact about delivery.
           "owner_pushed_ts": None,
           "delivered_ref": None, "last_updated": ts}
    if not _append(row, decisions_path()):
        return None, ("ledger unwritable (%s) — card NOT recorded"
                      % decisions_path())
    pk.event("decide-file", rid, title)
    return row, _announce_filed(row)


def _unreached_open(exclude_id=None):
    """OPEN cards the owner was never actually told about -> [row], oldest
    first. A card whose push failed has no second chance of its own: unlike
    a beacon edge, a card is filed ONCE and never re-fires, so without this
    it waits in a queue nobody knows to open. Unreadable ledger -> ()."""
    current, unavailable = snapshot(decisions_path())
    if unavailable:
        return ()
    out = [r for r in current.values()
           if r.get("status") == "open" and not r.get("owner_pushed_ts")
           and str(r.get("id")) != str(exclude_id or "")]
    return sorted(out, key=lambda r: r.get("ts") or 0)


STALE_NAMED = 6        # how many stranded ids one push NAMES — and stamps


def _push_body(row, stale=(), total=None):
    """The phone-readable HEADLINE, never the card body.

    Attention budget: the owner gets the question and the choices, plus where
    to answer. The context paragraph, the refs and the consequences stay on
    the web queue — this is the same headlines-click-to-detail split the rest
    of the owner surface takes, and a phone notification is the smallest
    surface helm has."""
    lines = [row["title"]]
    # THE OPTION KEYS TRAVEL, NOT JUST THE LABELS, because the reply has to be
    # unambiguous from a phone. "2" resolves through _pick_option exactly as a
    # full label does, and a one-character answer is what a notification can
    # realistically carry back.
    lines += ["  %s. %s" % (o.get("key"), o["label"])
              for o in row.get("options") or []]
    # NOT "answer on helm web". Owner, 2026-08-06 (relayed by @opus-integrator):
    # "The only reliable way that we have set up to reach my phone so far is the
    # ntfy notifications ... other than that on my phone, I just use orca's
    # mobile app." The console binds 127.0.0.1, which on a phone is the phone —
    # so naming helm web here was pointing him at a surface that device cannot
    # open. The card id plus a key is answerable from anything that can post a
    # line, which is the point: the decision travels IN the push.
    lines.append("reply with the card id and your pick — card %s (asked by %s)"
                 % (row["id"], row["asker"]))
    if stale:
        # BATCH, NEVER ONE PUSH PER ROW (notify's own rule). An owner who was
        # unreachable while N cards piled up gets ONE extra sentence here, not
        # N buzzes — and he learns the pile exists, which is the whole point.
        #
        # `stale` IS ALREADY THE NAMED SUBSET and `total` is the true count.
        # The first cut sliced HERE and stamped the WHOLE list, so a seventh
        # stranded card was marked told-about while never appearing in any
        # message — silent loss of exactly the thing the carry exists to stop
        # (@codex-3 review). The caller now slices ONCE and stamps only what
        # this line names; the remainder stays unreached and rides the next
        # card, which is self-healing rather than lossy.
        extra = max(0, int(total or len(stale)) - len(stale))
        lines.append("+ %d earlier card%s you were never pushed: %s%s"
                     % (int(total or len(stale)),
                        "" if int(total or len(stale)) == 1 else "s",
                        ", ".join(r["id"] for r in stale),
                        (" (and %d more — helm decide list)" % extra)
                        if extra else ""))
    return "\n".join(lines)


def _record_reached(ids, ts):
    """Stamp owner_pushed_ts on each id -> True when every stamp landed.

    Written AFTER the push, never before: the flag means "this card's
    headline left the machine", so recording it on an unsent push is the
    same lie the beacons latch was fixed for (helm/beacons.py:1646)."""
    path = decisions_path()
    with eventledger.locked(path) as held:
        if not held:
            return False
        current, unavailable = eventledger.latest_checked(path)
        if unavailable:
            return False
        for rid in ids:
            r = current.get(str(rid))
            if not r or r.get("owner_pushed_ts"):
                continue
            row = dict(r)
            row["owner_pushed_ts"] = ts
            row["last_updated"] = ts
            if not eventledger.append_unlocked(path, row):
                return False
    return True


def _owner_reach(row):
    """Push this card's headline to the OWNER'S OWN PHONE -> (reached, note).

    WHY THIS EXISTS. `helm/notify.py` was written for one rule: an alarm about
    the fleet being unreachable must not depend on a fleet member being
    reachable. A decision card is that rule one layer out — A DECISION ONLY
    THE OWNER CAN MAKE MUST NOT DEPEND ON THE OWNER HAPPENING TO LOOK. Until
    this, filing a card announced it to #helm, a room read exclusively by
    seats, and then waited for him to open a laptop. Overnight that is a queue
    of rulings nobody is blocked on because nobody knows it filled up.

    ARMED IS READ BEFORE THE CALL, and that is not defensive style — it is the
    exact defect beacons.py:1646 documents: `owner_push` returns True for two
    different worlds, "delivered, OR deliberately opted out". Recording reach
    on the opt-out True would mark a card as told-about when nothing left the
    box, and a card has no next edge to correct it.

    THE TAIL HAS A REAL EDGE, and stating the bound was NOT enough (@codex-3's
    second pass, correctly): the carry rides the NEXT card's push, so the LAST
    card ever filed had nothing behind it. `flush_unreached` is that edge and
    it rides `helm beacons --post`, the fleet's owner-reachability watchdog —
    already scheduled, already pushing through notify, already re-arming a
    failed edge. A list marker tells an AGENT; only a push tells him.

    -> reached: did a headline actually leave for the phone.
       note:    what the ROOM and the filing seat must be told when it did
                not, or None. Never raises: a notifier must not break the
                verb it reports on."""
    from . import notify
    armed = notify.configured()
    if not armed:
        return False, ("no push channel configured (HELM_NTFY_TOPIC unset) — "
                       "the owner will not learn about this card until he "
                       "opens the web queue")
    # SLICE ONCE. The body used to name stale[:6] while the stamp took the WHOLE
    # list, so a seventh stranded card was recorded as told-about having appeared
    # in no message at all (@codex-3). `named` is now the single source for both:
    # what the push SAYS is exactly what it CLAIMS, and the remainder stays
    # unreached for the next card to carry — self-healing instead of lossy.
    backlog = _unreached_open(exclude_id=row.get("id"))
    named = backlog[:STALE_NAMED]
    if not notify.owner_push(_push_body(row, named, total=len(backlog)),
                             title="helm decision",
                             receipt=("decide.push_failed", str(row["id"]))):
        return False, ("owner push FAILED — the card is durable but he has "
                       "not been told; it rides the next card's push")
    # RE-READ THE CHANNEL AFTER THE CALL, because the True above cannot tell
    # delivered from opted-out and the two reads are not atomic: a topic unset
    # between them makes owner_push return the opt-out True while nothing left
    # the box, which is the beacons latch defect surviving as a TOCTOU
    # (@codex-3). If the channel is gone now, we cannot PROVE the headline
    # left, so nothing is stamped and the card rides the next push. The cost is
    # a possible duplicate notification; the alternative is a card marked told
    # that he never saw, and only one of those two is recoverable.
    if not notify.configured():
        return False, ("the push channel disappeared mid-send — delivery is "
                       "UNPROVEN, so this card stays unpushed and rides the "
                       "next card's push rather than claiming it was told")
    ts = pk.now_ts()
    # ONLY WHAT THE PUSH NAMED. `named`, never `backlog` — see the slice above.
    _record_reached([row["id"]] + [r["id"] for r in named], ts)
    row["owner_pushed_ts"] = ts          # the caller's copy tells the truth too
    return True, None


def flush_unreached():
    """THE RECOVERY EDGE for cards no push ever reached -> (sent, note).

    WHY THIS EXISTS AND WHY STATING THE BOUND WAS NOT ENOUGH (@codex-3, and
    they were right to push back): the carry rides the NEXT card's push, so if
    the LAST card ever filed fails to send, nothing later carries it. Marking
    it [NOT PUSHED] in `helm decide list` exposes the failure to an AGENT and
    leaves the OWNER exactly where task/232 found him — dependent on somebody
    noticing. A list marker is evidence, not delivery.

    So this rides an EXISTING PERIODIC PASS rather than inventing a timer:
    `helm beacons --post` is the fleet's owner-reachability watchdog, already
    runs on a schedule, already pushes through notify, and already keeps a
    failed edge armed to re-push next pass. An undelivered owner DECISION is
    an owner-reachability failure, so it belongs to that watchdog rather than
    to a rival mechanism (compose, don't parallel).

    IT IS QUIET WHEN THERE IS NOTHING TO SAY: no unreached cards, or no push
    channel, and it sends nothing. When it does send it stamps EXACTLY the ids
    it named, so a later pass carries any remainder — the same
    say-what-you-claim rule the file-time carry takes.

    -> (True, None)       one batched push left the box; those cards are stamped
       (False, note)      nothing sent, and note says why (never an error)
    """
    from . import notify
    if not notify.configured():
        return False, None                      # opted out: nothing to retry
    backlog = _unreached_open()
    if not backlog:
        return False, None                      # nothing owed
    named = backlog[:STALE_NAMED]
    extra = len(backlog) - len(named)
    body = "\n".join(
        ["%d decision%s are waiting and you were never pushed them:"
         % (len(backlog), "" if len(backlog) == 1 else "s")]
        + ["  %s — %s" % (r["id"], (r.get("title") or "")[:70]) for r in named]
        + (["  (and %d more — helm decide list)" % extra] if extra else [])
        + ["reply with a card id and your pick"])
    if not notify.owner_push(body, title="helm decisions waiting",
                             receipt=("decide.flush_failed", "backlog")):
        return False, "owner push FAILED — the backlog stays armed for the next pass"
    # SAME TOCTOU GUARD AS THE FILE-TIME PATH: owner_push returns True for a
    # deliberate opt-out too, so a channel that vanished mid-send must not
    # stamp. Unproven stays unreached and the next pass carries it.
    if not notify.configured():
        return False, ("the push channel disappeared mid-send — delivery "
                       "UNPROVEN, backlog stays armed")
    _record_reached([r["id"] for r in named], pk.now_ts())
    return True, None


def _announce_filed(row):
    """ONE compact line in the filing seat's room (#helm for helm work), so
    the room knows an owner decision is pending — the attention budget is one
    line, never the card body (the card lives on the web queue). Fail-open:
    a room outage must never eat the durable card. -> advisory or None.

    THE ROOM LINE NOW STATES OWNER REACH, because the two facts read
    identically and only one of them means the decision is moving: a card the
    owner was pushed is waiting on HIM, and a card he was never pushed is
    waiting on NOTHING. Saying only "pending owner" for both is how a queue
    grows while every seat believes it is being read."""
    reached, note = _owner_reach(row)
    try:
        from . import chat
        opts = "/".join(o["label"] for o in row["options"])
        chat.post("[decision %s] pending owner: %s (%s) — filed by %s; "
                  "%s; review on helm web or `helm decide show %s`"
                  % (row["id"], row["title"], opts, row["asker"],
                     "pushed to his phone" if reached
                     else "NOT PUSHED — " + str(note), row["id"]),
                  who=row["asker"])
        return note
    except Exception as e:                           # noqa: BLE001 — advisory
        room = "card recorded; room announcement failed (%s)" % e
        return room if reached else room + "; " + str(note)


def _pick_option(row, choice):
    """Resolve the owner's choice against THIS card's options: exact key
    first, then casefolded exact label. Never a substring — 'Ship now' and
    'Ship now, revert later' must stay distinct choices."""
    want = str(choice or "").strip()
    for o in row.get("options") or []:
        if str(o.get("key")) == want:
            return o
    for o in row.get("options") or []:
        if str(o.get("label") or "").casefold() == want.casefold():
            return o
    return None


def decide(rid, choice, comment="", by=None):
    """The owner's RULING -> (row, err): verdict recorded, status 'decided'.

    Deliberately NOT the closer — deliver_verdict is, the same split done/
    reported already took: a ruling the asker never heard is open fleet debt.
    A decided card refuses a second ruling (the ledger is append-only history;
    a changed mind is a comment or a new card, never a rewrite)."""
    path = decisions_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
        current, unavailable = eventledger.latest_checked(path)
        if unavailable:
            return None, "decision ledger unavailable: %s" % unavailable
        r = current.get(str(rid or ""))
        if not r:
            return None, "no such decision card: %s (helm decide list)" % rid
        if r.get("status") != "open":
            v = r.get("verdict") or {}
            return None, ("card %s already decided (%s) — the ruling stands; "
                          "comment on it or file a new card"
                          % (rid, v.get("label") or v.get("choice")))
        opt = _pick_option(r, choice)
        if not opt:
            return None, ("choice %r matches no option on card %s — one of: %s"
                          % (choice, rid,
                             ", ".join("%s (%s)" % (o["key"], o["label"])
                                       for o in r.get("options") or [])))
        row = dict(r)
        row["verdict"] = {"choice": opt["key"], "label": opt["label"],
                          "consequence": opt["consequence"],
                          "comment": str(comment or "").strip(),
                          "by": str(by or "owner"), "ts": pk.now_ts()}
        row["status"] = "decided"
        row["last_updated"] = row["verdict"]["ts"]
        if not eventledger.append_unlocked(path, row):
            return None, "ledger unwritable (%s) — verdict NOT recorded" % path
    pk.event("decide-verdict", str(rid), "%s%s" % (
        opt["label"], (" — " + comment.strip()) if str(comment or "").strip() else ""))
    return row, None


def _verdict_text(row):
    v = row.get("verdict") or {}
    out = ("[decision %s] verdict on %r: %s — %s"
           % (row["id"], row.get("title"), v.get("label"), v.get("consequence")))
    if v.get("comment"):
        out += ". " + v["comment"]
    # the pull-depth pointer: the DM is ATTENTION, the ledger is AVAILABILITY
    # — a lane row can evaporate (tmpfs), the card cannot.
    out += " — full card: helm decide show %s" % row["id"]
    return out


def _dm_row_present(row):
    """Is the recorded delivered_ref still a readable row in the asker's
    lane? Chat is tmpfs: a reboot wipes the dir, rotation drops old rows, a
    seat GC can delete the lane file — all three read as ABSENT here, and
    absence is exactly what re-delivery cures. An unreadable lane counts as
    absent too: claiming presence on a lane that cannot be read would be the
    delivery claim this check exists to forbid."""
    ref = str(row.get("delivered_ref") or "")
    if not ref:
        return False
    try:
        from . import chat
        with open(chat.room_path(chat.dm_room(row["asker"])),
                  encoding="utf-8") as f:
            for line in f:
                try:
                    if json.loads(line).get("id") == ref:
                        return True
                except ValueError:
                    continue
    except OSError:
        return False
    return False


def deliver_verdict(rid):
    """The NOTIFICATION leg, re-entrant: DM the ruling to the ASKER
    -> (row, err).

    THE LEDGER IS THE VERDICT RECORD; THE DM IS ATTENTION. Chat lanes live
    on tmpfs, are never journaled, rotate past their cap and can be GC'd —
    SENT is not SEEN, and a terminal state bound to a volatile row would
    read 'delivered' while the ruling was unreachable after a reboot (codex
    review, 2026-08-04). So `delivered` binds to DURABLE state — the
    verdict and delivered_ref are ledger rows, and `helm decide show`
    always pulls the ruling — while the DM stays a lossy notification,
    re-sendable FROM the ledger.

    THE WHOLE OBJECT (every card-state x DM-evidence x resend-outcome cell
    — round 3 pinned the one this table's absence let slip: loss + failed
    resend read terminally delivered on a dead ref, invisible everywhere):

      missing card . . . . . . . . . . -> err, no side effect
      open . . . . . . . . . . . . . . -> err "no verdict yet"
      decided, send ok . . . . . . . . -> append delivered(fresh ref)
      decided, send FAILS  . . . . . . -> no append; err; card stays decided
                                          = VISIBLE (web queue + --open) and
                                          retry reachable
      decided, send ok, append fails . -> err; at-least-once says a retry
                                          re-DMs (documented, not hidden)
      delivered, evidence present  . . -> idempotent no-op (verified, never
                                          assumed — no duplicate DM)
      delivered, evidence GONE . . . . -> FIRST append decided(reopen,
                                          ref cleared); the durable record
                                          precedes the side effect, so:
                                            reopen append fails -> err, NOT
                                              resent (never side-effect what
                                              cannot be recorded)
                                            then send ok  -> append delivered
                                              (fresh ref): ledger reads
                                              ...delivered,decided,delivered
                                            then send FAILS -> err; card is
                                              durably decided = VISIBLE with
                                              retry reachable, never
                                              terminally delivered on dead
                                              evidence
      ledger locked/unavailable  . . . -> err before any side effect

    Delivery is AT-LEAST-ONCE, stated rather than pretended away: the DM
    send and the ledger append are two operations, so a crash between them
    — or a lane lost after them — re-DMs on retry. Exactly-once would need
    an outbox the lane does not warrant; a duplicate notification is cheap,
    a lost ruling is not. Runs under the ledger lock so CONCURRENT retries
    cannot double-DM (one operation sends at most once)."""
    path = decisions_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — delivery NOT recorded" % path
        current, unavailable = eventledger.latest_checked(path)
        if unavailable:
            return None, "decision ledger unavailable: %s" % unavailable
        r = current.get(str(rid or ""))
        if not r:
            return None, "no such decision card: %s (helm decide list)" % rid
        if r.get("status") == "open":
            return None, ("card %s has no verdict yet — the owner decides "
                          "first (web queue, or helm decide verdict)" % rid)
        if r.get("status") == "delivered":
            if _dm_row_present(r):
                return r, None            # verified, not assumed: no re-DM
            # EVIDENCE GONE -> REOPEN DURABLY BEFORE ANY RESEND. Round 3's
            # hole: detect-then-resend left a FAILED resend terminally
            # 'delivered' on a dead ref — filtered off the web queue and
            # --open, so the undelivered ruling was invisible and no retry
            # was reachable. The reopen snapshot lands FIRST; only then is
            # a send attempted, so every later failure leaves a card that
            # says what is true: decided, delivery pending, visible.
            lost_ref = str(r.get("delivered_ref") or "")
            reopened = dict(r)
            reopened["status"] = "decided"
            reopened["delivered_ref"] = None
            reopened["last_updated"] = pk.now_ts()
            if not eventledger.append_unlocked(path, reopened):
                return None, ("delivery evidence for %s is GONE but the "
                              "ledger refused the reopen snapshot (%s) — "
                              "NOT resending: the durable record precedes "
                              "the side effect" % (rid, path))
            # journaled HERE, not after the lock: the send below may fail
            # and return early, and the reopen is durable either way
            pk.event("decide-delivery-reopened", str(rid), lost_ref)
            r = reopened
        try:
            from . import seats
            dmrow, err = seats.dm(r["asker"], _verdict_text(r),
                                  who=seats.owner_name())
        except Exception as e:                       # noqa: BLE001 — surfaced
            dmrow, err = None, str(e)
        if err or not dmrow:
            return None, ("delivery to %s failed: %s — card stays '%s' "
                          "(visible on the queue); retry: helm decide "
                          "deliver %s"
                          % (r["asker"], err or "no row", r["status"], rid))
        row = dict(r)
        row["status"] = "delivered"
        row["delivered_ref"] = str(dmrow.get("id") or "")
        row["last_updated"] = pk.now_ts()
        if not eventledger.append_unlocked(path, row):
            return None, ("DM sent to %s but the ledger append failed (%s) — "
                          "card still reads '%s'; a retry WILL re-DM "
                          "(at-least-once, by design)"
                          % (r["asker"], path, r["status"]))
    pk.event("decide-delivered", str(rid), row["delivered_ref"])
    return row, None


def comment_decision(rid, text, by=None):
    """A NON-CLOSING owner note -> (row, problem). Appends to the card's
    comments and best-effort DMs the asker (row+problem = recorded, DM
    failed — advisory). The card's status never moves: comment is the
    owner's 'not yet / tell me more' lane, verdict remains the closer."""
    text = str(text or "").strip()
    if not text:
        return None, "an empty comment says nothing"
    path = decisions_path()
    with eventledger.locked(path) as held:
        if not held:
            return None, "ledger unwritable (%s) — comment NOT recorded" % path
        current, unavailable = eventledger.latest_checked(path)
        if unavailable:
            return None, "decision ledger unavailable: %s" % unavailable
        r = current.get(str(rid or ""))
        if not r:
            return None, "no such decision card: %s (helm decide list)" % rid
        row = dict(r)
        row["comments"] = list(r.get("comments") or []) + [
            {"ts": pk.now_ts(), "text": text, "by": str(by or "owner")}]
        row["last_updated"] = row["comments"][-1]["ts"]
        if not eventledger.append_unlocked(path, row):
            return None, "ledger unwritable (%s) — comment NOT recorded" % path
    pk.event("decide-comment", str(rid), text)
    try:
        from . import seats
        _dmrow, err = seats.dm(row["asker"],
                               "[decision %s] owner comment on %r: %s"
                               % (rid, row.get("title"), text),
                               who=seats.owner_name())
        if err:
            return row, ("comment recorded; DM to %s failed: %s"
                         % (row["asker"], err))
        return row, None
    except Exception as e:                           # noqa: BLE001 — advisory
        return row, "comment recorded; DM to %s failed: %s" % (row["asker"], e)


def open_decisions():
    """Open cards oldest-first — the web queue's order and the board's."""
    rs = [r for r in decision_rows().values() if r.get("status") == "open"]
    rs.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
    return rs


# Every row this projection writes onto the board carries this stamp, and the
# stamp is the ONLY thing sync_board ever replaces. It is provenance, not
# decoration: the live key's hand-written rows use the same `state` vocabulary
# ("waiting-owner"), so no field VALUE could partition ours from theirs.
BOARD_SRC = "ledger"


def board_queue():
    """Derive the board's owner_gated_queue rows from the DURABLE ledgers —
    open decision cards first, then unreported asks. This list is the SINGLE
    SOURCE for the rows the projection OWNS (stamped src=ledger); row shape
    matches what the console already renders there ({ask, why, state,
    since})."""
    out = []
    for r in open_decisions():
        opts = " | ".join(o["label"] + ("*" if o.get("recommended") else "")
                          for o in r.get("options") or [])
        ctx = " ".join(str(r.get("context") or "").split())
        if len(ctx) > CTX_HEAD:
            ctx = ctx[:CTX_HEAD - 1] + "…"
        out.append({"ask": "decide: " + str(r.get("title") or ""),
                    "why": ctx + (" — options: " + opts if opts else ""),
                    "state": "waiting-owner-decision",
                    "since": str(r.get("ts") or ""), "src": BOARD_SRC})
    for r in unreported():
        out.append({"ask": str(r.get("ask") or ""),
                    "why": ("needs: " + r["needs"]) if r.get("needs") else "",
                    "state": "waiting-owner", "since": str(r.get("ts") or ""),
                    "src": BOARD_SRC})
    return out


def sync_board():
    """PROJECT the two ledgers onto the board's owner_gated_queue -> (ok, err).

    PRESERVED-PASSTHROUGH, not a wholesale replace. The first cut assigned
    the derived list over the key, and codex's review proved that as live
    data loss: the LIVE board held two hand-written rows (a device-auth
    owner gate, a machine-wide-profile FYI) beside 0 unreported asks and no
    decision ledger, so the first successful sync would have deleted both
    while reporting success. The projection therefore replaces ONLY rows it
    wrote — the ones stamped src=ledger — and every other row passes
    through verbatim, after the derived ones.

    How legacy rows COEXIST with the typed ledgers, decided explicitly
    rather than silently discarded: an INFORMATIONAL FYI's canonical home
    stays its board row — it is console annotation, not owner debt; no
    ledger wants it, and it rides the passthrough for as long as its author
    leaves it there. An owner GATE's canonical home is the asks ledger
    (`helm asks add … --needs …` — durable, stop-whispered); migrating one
    is its owner's explicit act, deleting the hand row afterwards — the
    projection never force-migrates and never deletes what it cannot
    re-derive.

    The board's own laws still govern: no key means no write (helm does not
    own the console renderer), a non-list key refuses (there is no row
    order to merge into), and the narrative-key ownership gate runs as for
    any writer. Refusals come back as err; every caller treats them as
    ADVISORY — a board projection must never break the primitive."""
    from . import board
    derived = board_queue()

    def _mutate(b):
        if "owner_gated_queue" not in b:
            raise KeyError(
                "board has no owner_gated_queue key — helm does not own the "
                "console renderer, so nothing is projected until it does")
        cur = b.get("owner_gated_queue")
        if not isinstance(cur, list):
            raise TypeError(
                "owner_gated_queue holds a %s, not a list — refusing to "
                "project over a shape no merge understands"
                % type(cur).__name__)
        kept = [r for r in cur
                if not (isinstance(r, dict) and r.get("src") == BOARD_SRC)]
        b["owner_gated_queue"] = derived + kept
    try:
        return board.update(_mutate, seat=board._acting_seat())
    except (KeyError, TypeError) as e:
        return False, str(e.args[0] if e.args else e)
    except Exception as e:                           # noqa: BLE001 — advisory
        return False, "board sync failed: %s" % e


USAGE = ("usage: helm asks add <text> --needs \"<what only the owner can "
         "supply>\" [--source S] | done <id> <evidence> | "
         "report <id> <chat-post-id> | list [--open] [--json]\n"
         "  --needs is REQUIRED: an ask with no named human-only input is a "
         "task, not an owner gate.")


def _advise_owner(text):
    """OWNER-BOUND ADVISORY. This verb writes text the owner reads, so it runs
    the clarity die in owner mode and prints what it finds. Advisory by law:
    it cannot refuse the write and cannot change the exit code, and every
    failure path is silent (see helm.clarity.advise). Silence it with
    HELM_CLARITY_ADVISE_OFF=asks."""
    try:
        from .clarity import advise
        advise(text, "asks")
    except Exception:                    # noqa: BLE001 — fail-open by law
        pass


def _clarity_refusal(text):
    """The ERROR-severity clarity finding that must stop a write -> str|None.

    Separate from `_advise_owner` on purpose. That one is the SHARED advisory
    (board.py and fleetnotes.py call the same primitive) and its law is that it
    cannot refuse and cannot change an exit code. This asks THE SAME CHECKER a
    different question — "is anything here an ERROR" — and lets THIS verb act
    on its own answer, which is a decision about this ledger's writes rather
    than a change to a shared law.

    FAIL-OPEN, like everything else on this path: an unreadable checker returns
    None and the write proceeds. A guard that cannot run must not become a
    guard that blocks. And it honours the SAME silence switch the advisory
    does, so the existing escape hatch keeps working rather than half-working.
    """
    try:
        from .clarity import _advise_silenced, check_text
        if not isinstance(text, str) or not text.strip() \
                or _advise_silenced("asks"):
            return None
        for f in check_text(text, mode="owner", lexicon={}) or []:
            if f.get("severity") == "error":
                return str(f.get("message") or "").strip() or None
    except Exception:                    # noqa: BLE001 — fail-open by law
        return None
    return None


def cmd_asks(args):
    """asks add|done|report|list — the owner-ask ledger verbs."""
    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb == "add":
        source = None
        if "--source" in rest:
            i = rest.index("--source")
            source = rest[i + 1] if len(rest) > i + 1 else None
            del rest[i:i + 2 if source else i + 1]
        needs = None
        if "--needs" in rest:
            i = rest.index("--needs")
            needs = rest[i + 1] if len(rest) > i + 1 else None
            del rest[i:i + (2 if needs else 1)]
        text = " ".join(rest).strip()
        if not text:
            print(USAGE, file=sys.stderr)
            return 2
        # NAME THE HUMAN-ONLY INPUT OR IT IS NOT AN OWNER ASK.
        #
        # Measured 2026-07-26: the queue held 19 open asks, the oldest four days
        # old, and ELEVEN WERE NOT OWNER GATES AT ALL — engineering tasks nobody
        # had started, policy the owner had already decided, and work already
        # finished. They buried the four that were real, and the owner read none
        # of them because the volume made the queue worthless.
        #
        # The test is mechanical rather than a judgement about prose: say WHAT
        # ONLY A HUMAN CAN SUPPLY. A credential, a physical act, an account only
        # they hold, a genuine vision call. If you cannot name it in a few words,
        # the ask belongs on the task list and you are the one who does it.
        if not (needs or "").strip():
            print("helm asks: name the HUMAN-ONLY input — "
                  "`--needs \"<what only the owner can supply>\"`.\n"
                  "  A credential, a physical act, an account only they hold, a "
                  "vision call. If you cannot name one, this is not an owner "
                  "ask: put it on the task list and do it.\n"
                  "  (11 of 19 rows in this queue were not owner gates; they "
                  "buried the 4 that were.)", file=sys.stderr)
            return 2
        # A GUARD THAT CAN ONLY SHRUG SHOULD REFUSE (task/353, and I filed it
        # after tripping it myself). The clarity die ran AFTER the append, so a
        # 103-word ask printed "max 20 in owner mode" and landed anyway — and
        # the asks ledger has no edit, amend or supersede verb, so the row sat
        # on the owner's queue permanently in the form the guard had just said
        # he could not read. done/report both CLOSE it while the input is still
        # owed; a shorter duplicate splits his attention across two rows for one
        # question. There was no cure available AFTER the write, which is
        # exactly why the check belongs BEFORE it.
        #
        # ERROR SEVERITY ONLY, and that distinction is measured rather than
        # assumed: the word cap comes back `severity="error"` while ordinary
        # register findings are advisory, so this refuses what the checker
        # itself calls an error and keeps advising everything else.
        #
        # THE SHARED ADVISORY IS NOT TOUCHED. `_advise_owner`/`clarity.advise`
        # is used by board.py and fleetnotes.py too and its docstring states
        # the law — "it cannot refuse the write and cannot change the exit
        # code". Changing that primitive would change three surfaces to fix
        # one. THIS verb decides about ITS OWN writes, the same way it already
        # refuses an ask with no `--needs`.
        #
        # THE CHECK LIVES IN `add` (task/373) AND SO DOES THE REASON. This verb
        # no longer decides WHETHER to write, and it no longer RE-DERIVES why a
        # write was declined: `add` returns (row, error), so the sentence is
        # chosen from what the write itself reported. An earlier draft returned
        # a bare None for every refusal and asked the clarity checker a second
        # time here to recover the reason — which put a second evaluation of an
        # env-reading predicate on the path to rebuild information the callee
        # already had. Only UNWRITABLE is compared by value, because it is the
        # one fault that is operational rather than the caller's input; every
        # other error is the caller's words and reads back verbatim.
        row, err = add(text, source=source, needs=needs)
        if err == UNWRITABLE:
            print("helm asks: ledger unwritable (%s) — ask NOT recorded"
                  % ledger_path(), file=sys.stderr)
            return 1
        if err:
            print("helm asks: REFUSED — the owner cannot read this, and this "
                  "ledger has no edit verb, so a filed row could never be "
                  "fixed:\n  %s\nShorten it and run the command again; nothing "
                  "was written. (silence: %s=asks)"
                  % (err, "HELM_CLARITY_ADVISE_OFF"), file=sys.stderr)
            return 2
        print("ask %s open: %s" % (row["id"], row["ask"]))
        _advise_owner(" ".join(x for x in (text, needs) if x))
        return 0
    if verb in ("done", "report"):
        if len(rest) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        rid, ref = rest[0], " ".join(rest[1:]).strip()
        row, why = (mark_done if verb == "done" else mark_report)(rid, ref)
        if not row:
            print("helm asks: " + why, file=sys.stderr)
            return 1
        if verb == "done":
            print("ask %s done (%s) — still OPEN until the owner hears it: "
                  "helm asks report %s <chat-post-id>" % (rid, ref, rid))
        else:
            print("ask %s reported — closed (post %s)" % (rid, ref))
        return 0
    if verb == "list":
        current, unavailable = snapshot()
        if unavailable:
            print("helm asks: ledger unavailable; owner debt UNKNOWN: %s"
                  % unavailable, file=sys.stderr)
            return 1
        rs = sorted(current.values(),
                    key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
        if "--open" in rest:
            rs = [r for r in rs if r.get("status") != "reported"]
        if "--json" in rest:
            print(json.dumps(rs, indent=2, ensure_ascii=False))
            return 0
        if not rs:
            print("no asks" + (" open" if "--open" in rest else ""))
            return 0
        for r in rs:
            refs = " ".join(x for x in (
                ("done:%s" % r["done_ref"]) if r.get("done_ref") else "",
                ("post:%s" % r["report_ref"]) if r.get("report_ref") else "") if x)
            print("%s  %-8s  %s  %s%s" % (r.get("id"), r.get("status"),
                                          r.get("ts"), r.get("ask"),
                                          ("  [%s]" % refs) if refs else ""))
            # SHOW THE HUMAN-ONLY INPUT. It is the whole reason the row is in
            # the owner's queue rather than on the task list, and a field that
            # is recorded but never rendered is dead weight that teaches nobody.
            # Rows written before `needs` existed simply have none to show.
            if r.get("needs"):
                print("            needs: %s" % r["needs"])
        return 0
    print(USAGE, file=sys.stderr)
    return 2


DECIDE_USAGE = """usage: helm decide file <title...> [--asker SEAT] [--ref R]... [--source S]
       helm decide list [--open] [--json]
       helm decide show <id> [--json]
       helm decide verdict <id> <choice> [--comment TEXT...]
       helm decide deliver <id>
       helm decide comment <id> <text...>
       helm decide board-sync
  `file` reads the CARD BODY on stdin (quoted heredoc): a phone-readable
  context paragraph, then option lines `* <label> :: <one-line consequence>`
  (`*!` marks the recommended one; no option lines = an approve/reject card):
    helm decide file store GC policy --ref lane/store-gc <<'EOF'
    The store holds 400 retired entries; scans cost 2s per resolve.
    * Archive to cold file :: resolves fast; history one file away
    *! Leave in place :: zero risk; scans stay slow until indexed
    EOF
  The owner answers on the WEB queue (helm web -> the work tab, the
  owner<->fleet surface; the home deck shows a count that links there);
  `verdict` is CLI parity through the same functions. The verdict is a DURABLE ledger row first and
  a DM to the asker second (at-least-once: the beacon wakes them, and
  `deliver` re-verifies the tmpfs lane, re-sending FROM the ledger if a
  reboot/rotation/GC ate the DM). The board's owner_gated_queue rows stamped
  src=ledger are a derived projection of open cards + unreported asks;
  hand-written rows pass through untouched."""


def _pop_valued(rest, flag):
    """Extract one `--flag value` pair from rest (in place) -> value or None."""
    if flag not in rest:
        return None
    i = rest.index(flag)
    value = rest[i + 1] if len(rest) > i + 1 else None
    del rest[i:i + (2 if value is not None else 1)]
    return value


def _print_card(r):
    v, extra = r.get("verdict") or {}, ""
    if v:
        extra = " -> %s" % (v.get("label") or v.get("choice"))
    # AN OPEN CARD HE WAS NEVER PUSHED IS NOT WAITING ON HIM. It reads exactly
    # like one that is, so the list says which — otherwise the oldest row on
    # this queue looks like the owner ignoring us when nothing ever asked him.
    if r.get("status") == "open" and not r.get("owner_pushed_ts"):
        extra += "  [NOT PUSHED to the owner]"
    print("%s  %-9s  %s  %s%s  (asker: %s)"
          % (r.get("id"), r.get("status"), r.get("ts"), r.get("title"),
             extra, r.get("asker")))


def cmd_decide(args):
    """decide file|list|show|verdict|deliver|comment|board-sync — the owner
    DECISION queue (the ask ledger generalized to rulings-among-options)."""
    args = list(args or [])
    if not args:
        print(DECIDE_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    if verb == "file":
        asker = _pop_valued(rest, "--asker")
        source = _pop_valued(rest, "--source")
        refs = []
        while "--ref" in rest:
            r = _pop_valued(rest, "--ref")
            if r:
                refs.append(r)
        title = " ".join(rest).strip()
        if not title:
            print(DECIDE_USAGE, file=sys.stderr)
            return 2
        body = "" if sys.stdin.isatty() else sys.stdin.read()
        context, options, err = parse_card_body(body)
        if err:
            print("helm decide: " + err, file=sys.stderr)
            return 2
        if not asker:
            from . import seats
            asker = seats.own_name()
        if not asker:
            print("helm decide: no seat identity in this process — pass "
                  "--asker <your seat> so the verdict has a return address",
                  file=sys.stderr)
            return 2
        row, problem = file_decision(title, context, options, asker,
                                     refs=refs, source=source)
        if not row:
            print("helm decide: " + problem, file=sys.stderr)
            return 1
        print("decision %s filed for the owner: %s (%s) — verdict will DM %s"
              % (row["id"], row["title"],
                 "/".join(o["label"] for o in row["options"]), row["asker"]))
        if problem:
            print("helm decide: " + problem, file=sys.stderr)
        ok, berr = sync_board()
        if not ok:
            print("helm decide: board not synced — " + str(berr), file=sys.stderr)
        return 0
    if verb == "list":
        current, unavailable = decisions_snapshot()
        if unavailable:
            print("helm decide: ledger unavailable; owner decisions UNKNOWN: "
                  "%s" % unavailable, file=sys.stderr)
            return 1
        rs = sorted(current.values(),
                    key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
        if "--open" in rest:
            rs = [r for r in rs if r.get("status") != "delivered"]
        if "--json" in rest:
            print(json.dumps(rs, indent=2, ensure_ascii=False))
            return 0
        if not rs:
            print("no decision cards" + (" open" if "--open" in rest else ""))
            return 0
        for r in rs:
            _print_card(r)
        return 0
    if verb == "show":
        if not rest:
            print(DECIDE_USAGE, file=sys.stderr)
            return 2
        current, unavailable = decisions_snapshot()
        if unavailable:
            print("helm decide: ledger unavailable: %s" % unavailable,
                  file=sys.stderr)
            return 1
        r = current.get(rest[0])
        if not r:
            print("helm decide: no such card: %s" % rest[0], file=sys.stderr)
            return 1
        if "--json" in rest:
            print(json.dumps(r, indent=2, ensure_ascii=False))
            return 0
        _print_card(r)
        print("  " + str(r.get("context") or "").replace("\n", "\n  "))
        for o in r.get("options") or []:
            print("  [%s] %s%s :: %s"
                  % (o.get("key"), o.get("label"),
                     " (recommended)" if o.get("recommended") else "",
                     o.get("consequence")))
        for c in r.get("comments") or []:
            print("  comment %s (%s): %s"
                  % (c.get("ts"), c.get("by"), c.get("text")))
        v = r.get("verdict") or {}
        if v:
            print("  verdict %s (%s): %s%s"
                  % (v.get("ts"), v.get("by"), v.get("label"),
                     (" — " + v["comment"]) if v.get("comment") else ""))
        # the READ-ONLY loss observation: `delivered` binds to the ledger,
        # and the DM evidence is tmpfs — show is the per-card surface where
        # anyone holding the id discovers a lost DM without mutating state
        if r.get("status") == "delivered" and not _dm_row_present(r):
            print("  WARNING: delivered evidence GONE — dm %s is no longer "
                  "in %s's lane; `helm decide deliver %s` re-sends from the "
                  "ledger" % (r.get("delivered_ref"), r.get("asker"), rest[0]))
        if r.get("refs"):
            print("  refs: " + " ".join(r["refs"]))
        return 0
    if verb == "verdict":
        comment = _pop_valued(rest, "--comment") or ""
        if len(rest) < 2:
            print(DECIDE_USAGE, file=sys.stderr)
            return 2
        rid, choice = rest[0], " ".join(rest[1:])
        row, err = decide(rid, choice, comment=comment)
        if not row:
            print("helm decide: " + err, file=sys.stderr)
            return 1
        delivered, derr = deliver_verdict(rid)
        ok, berr = sync_board()
        if not ok:
            print("helm decide: board not synced — " + str(berr), file=sys.stderr)
        if derr:
            print("card %s decided (%s) but NOT delivered — %s"
                  % (rid, row["verdict"]["label"], derr))
            return 1
        print("card %s decided (%s) and delivered to %s (dm %s)"
              % (rid, row["verdict"]["label"], delivered["asker"],
                 delivered["delivered_ref"]))
        return 0
    if verb == "deliver":
        if not rest:
            print(DECIDE_USAGE, file=sys.stderr)
            return 2
        row, err = deliver_verdict(rest[0])
        if not row:
            print("helm decide: " + err, file=sys.stderr)
            return 1
        ok, berr = sync_board()
        if not ok:
            print("helm decide: board not synced — " + str(berr), file=sys.stderr)
        print("card %s delivered to %s (dm %s)"
              % (rest[0], row["asker"], row["delivered_ref"]))
        return 0
    if verb == "comment":
        if len(rest) < 2:
            print(DECIDE_USAGE, file=sys.stderr)
            return 2
        row, problem = comment_decision(rest[0], " ".join(rest[1:]))
        if not row:
            print("helm decide: " + problem, file=sys.stderr)
            return 1
        print("card %s comment recorded (card stays %s)"
              % (rest[0], row["status"]))
        if problem:
            print("helm decide: " + problem, file=sys.stderr)
        return 0
    if verb == "board-sync":
        ok, berr = sync_board()
        if not ok:
            print("helm decide: " + str(berr), file=sys.stderr)
            return 1
        print("board owner_gated_queue re-derived (%d rows)" % len(board_queue()))
        return 0
    print(DECIDE_USAGE, file=sys.stderr)
    return 2
