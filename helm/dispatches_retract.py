"""`helm.dispatches_retract` -- A WRONG VERDICT, CORRECTED WITHOUT A REWRITE.

ONE QUESTION. A verdict is immutable; a retraction is a later fact: one
`verdict-retract` event appended after it, admitted for the verdict's author,
the integrator or the owner, carrying the corrected READING as a labelled
claim, and followed by the reissue. The fold reads it back through
`_retract_record`, and every writer refuses a retracted row through
`retracted_refusal`.

MEASURED AT THE CUT, THE REMAINDER REACHES IN THROUGH FOUR NAMES, all read
inside function bodies, so each resolves the published object at call time:
the fold (`RETRACT_EVENT`, `RETRACTED`, `_retract_record`) and the cancel and
verdict writers (`retracted_refusal`).

THE EVENT-KIND CENSUS READS THE KIND THROUGH THIS SPELLING. It resolves an
event kind spelled as a module constant; `dispatches.RETRACT_EVENT` resolves
through the same constant table the bare name did.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `dispatches.NAME` AT
ITS CALL SITE -- 59 reads of 37 distinct names; no other byte of a moved line
changed. A `from` import, or a bare global left behind, binds the object ONCE
at import, so an arm that patches an attribute on `dispatches` and then drives
this code would reach the original and measure nothing, while every structural
guard stayed green. The module spelling keeps the lookup at CALL TIME, exactly
as a bare global did inside the ledger. Names this file OWNS are spelled that
way too, because they are published back onto `dispatches` and the suites
patch them there.

THE CYCLE IS BROKEN THE WAY THE LEDGER'S OTHER SATELLITES BREAK IT: this
module imports `dispatches` EAGERLY, and `dispatches` imports this one at the
END of its own body, after every name it needs exists. A module object is in
`sys.modules` from the first line of its execution, so either import order
resolves.
"""
from . import dispatches
from . import home, pk


# ---------------------------------------------------------------------------
# THE VERDICT RETRACTION (task/3060)
# ---------------------------------------------------------------------------
# A VERDICT IS IMMUTABLE, AND UNTIL THIS EXISTED A WRONG ONE HAD NO CORRECTIVE.
# The door refuses a second verdict on a verdicted row ("terminal is
# immutable"), `cancel` refuses a row whose verdict declared a polarity, and
# the only moves left were to contest it with a successor review (whose FIX
# became chain debt while the parent APPROVE still read READY for its tip), to
# retire it, or to not land it. The measured case: a delegated reader running
# inside a seat wrote an APPROVE with no findings that its brief never
# authorized, and that immutable row kept authorizing a land nobody meant.
#
# A RETRACTION IS A LATER FACT, NEVER A REWRITE. It is one `verdict-retract`
# event appended after the verdict. The verdict event, its evidence, its gate
# binding and its attestation stay exactly as recorded; the projection reads
# polarity RETRACTED and keeps the original in `retracted_polarity`. The
# retraction carries the corrected READING as a labelled claim for the
# integrator — it is not a polarity and re-enters no verdict semantics.
#
# WHO MAY RETRACT, and the list is short on purpose: the verdict's AUTHOR (the
# row's recipient seat, from any session, because the session that erred is
# usually gone), the INTEGRATOR role resolved from the roster, or the OWNER
# through his own capability. The row's SENDER is refused: a sender who
# disagrees with a review contests it with `dispatch send --supersedes`, the
# path whose reviewer-shopping residual is already disclosed.
RETRACT_EVENT = "verdict-retract"
#: What the retracting hand now says the review found. A CLAIM for the
#: integrator to read, never a polarity: source-clean (the delta reads clean
#: and waits on the land gate), fix, supersede, or unknown (the verdict was
#: wrong and nobody has re-read it yet).
RETRACT_READS = ("source-clean", "fix", "supersede", "unknown")
#: How the retracting hand knows. `unverified` is absent on purpose: a
#: retraction removes authority, and one that cannot say how it knows is a
#: guess about somebody else's review.
RETRACT_BASES = ("measured", "inferred")
#: The door that admitted the retraction, recorded on the event.
RETRACT_ROLES = ("author", "integrator", "owner")
#: The projected polarity of a retracted verdict. It is outside POLARITIES,
#: so `clean_polarity` refuses it at every writer and `_replay_polarity`
#: reads it as UNDECLARED: authority fails closed.
RETRACTED = "retracted"
_RETRACT_PROOF_V = 1
_RETRACT_REASON_CAP = 256


def _retract_admission_error(state):
    """Why this row's verdict cannot be retracted, or None when it can.

    ONE PREDICATE FOR THE WRITER AND THE REPLAY. `retract` asks it before it
    mints anything, `_record_retract` asks it under the ledger lock, and
    `_apply` asks it through `_retract_record` for every event it folds."""
    if not isinstance(state, dict):
        return "the row is unreadable"
    if state.get("verdict_retracted"):
        return "its verdict is already retracted"
    if state.get("status") != "verdict":
        return ("only a verdicted row has a verdict to retract (this one is "
                "%s)" % (state.get("status") or "in an unknown state"))
    if dispatches._replay_polarity(state.get("polarity")) is None:
        return ("its verdict declared no polarity, so it authorized nothing "
                "to retract: `helm dispatch cancel %s <reason>` closes it as "
                "advisory" % str(state.get("id") or "")[:12])
    terminal = dispatches._close_retired_by(state)
    if terminal:
        return "it is already retired by %s" % terminal
    return None


def _retract_record(event, state):
    """(state fields, None) for a well-formed retraction of `state`, else
    (None, why). The replay's validator and the writer's projection."""
    err = dispatches._retract_admission_error(state)
    if err:
        return None, err
    reason, err = dispatches._clean(event.get("retract_reason"), "retract reason",
                         dispatches._RETRACT_REASON_CAP)
    if err:
        return None, err
    if event.get("retract_reads") not in dispatches.RETRACT_READS:
        return None, "retract reads must be one of %s" % "|".join(dispatches.RETRACT_READS)
    if event.get("retract_basis") not in dispatches.RETRACT_BASES:
        return None, "retract basis must be one of %s" % "|".join(dispatches.RETRACT_BASES)
    if event.get("retract_role") not in dispatches.RETRACT_ROLES:
        return None, "retract role must be one of %s" % "|".join(dispatches.RETRACT_ROLES)
    seat = event.get("retract_seat")
    if not isinstance(seat, str) or not dispatches._TOKEN.fullmatch(seat):
        return None, "retract seat must be an exact seat token"
    # THE EVENT NAMES WHAT IT RETRACTS, the way a retip names the tip it
    # moves: a retraction spliced onto a different verdict is inert.
    if event.get("retracted_polarity") != state.get("polarity") \
            or str(event.get("retracted_tip") or "") \
            != str(state.get("reviewed_tip") or ""):
        return None, "the retraction names a verdict this row does not carry"
    successor = event.get("retract_successor")
    if successor is not None and (not isinstance(successor, str)
                                  or not dispatches._ID.fullmatch(successor)
                                  or successor == state.get("id")):
        return None, "retract successor must be another row's id"
    same = event.get("retract_same_session")
    if same is not None and not isinstance(same, bool):
        return None, "retract same-session must be true, false or absent"
    if type(event.get("retract_proof_version")) is not int \
            or event.get("retract_proof_version") != dispatches._RETRACT_PROOF_V:
        return None, "unknown retract proof version"
    if not dispatches._valid_ts(event.get("ts")):
        return None, "retract timestamp is unreadable"
    fields = {"retracted_polarity": state.get("polarity"),
              "retract_ts": event.get("ts"), "retract_reason": reason,
              "retract_reads": event["retract_reads"],
              "retract_basis": event["retract_basis"],
              "retract_role": event["retract_role"], "retract_seat": seat,
              "retract_successor": successor}
    # ABSENT STAYS ABSENT: a verdict that recorded no author session cannot
    # be compared, and a default would claim a comparison nobody made.
    if same is not None:
        fields["retract_same_session"] = same
    return fields, None


def retracted_refusal(row, act):
    """The sentence a door says over a retracted row: what was retracted,
    when, by whom, and where the review now lives."""
    rid = str(row.get("id") or "")
    successor = str(row.get("retract_successor") or "")
    where = ("the successor %s carries the review" % successor[:12]
             if successor else
             "re-request it: `helm dispatch send %s %s --ref %s --kind %s "
             "--supersedes %s` (body on stdin)"
             % (row.get("recipient") or "<reviewer>",
                row.get("lane") or "<lane>",
                row.get("tip") or "<tip>", row.get("kind") or "review",
                rid[:12]))
    return ("dispatch %s: its %s verdict was RETRACTED at %s by @%s (%s), so "
            "%s; %s" % (rid[:12],
                        str(row.get("retracted_polarity") or "?").upper(),
                        row.get("retract_ts") or "an unrecorded time",
                        row.get("retract_seat") or "?",
                        row.get("retract_role") or "?", act, where))


def _retract_role(row, seat, owner=None):
    """(role, None) for the door that admits `seat` to retract `row`'s
    verdict, else (None, why).

    THE AUTHOR IS THE ROW'S RECIPIENT, because a land-authorizing verdict
    binds its author to exactly that seat (`mark_verdict`). The identity is
    the seat, never the session: the session that wrote a wrong verdict is
    usually gone, and requiring it would leave the error with no author able
    to take it back."""
    if owner is not None:
        return "owner", None
    from . import seats
    if seats.recipient_matches(row.get("recipient") or "", seat):
        return "author", None
    from . import seats_integrator
    integrator, why = seats_integrator.integrator_seat()
    if integrator and seats.recipient_matches(integrator, seat):
        return "integrator", None
    return None, (
        "refusing to retract dispatch %s's verdict as @%s: a verdict is "
        "retracted by its AUTHOR (@%s, from any session), the INTEGRATOR (%s) "
        "or the OWNER. A seat that disagrees with a review contests it with "
        "a successor: `helm dispatch send %s %s --ref %s --kind %s "
        "--supersedes %s`"
        % (str(row.get("id") or "")[:12], seat, row.get("recipient") or "?",
           "@" + integrator if integrator else "unresolved: %s" % why,
           row.get("recipient") or "<reviewer>", row.get("lane") or "<lane>",
           row.get("tip") or "<tip>", row.get("kind") or "review",
           str(row.get("id") or "")[:12]))


def _retract_matches(row, seat, reason, reads, basis, successor):
    """Is `row` already retracted exactly as asked? The idempotent retry
    reconciles the standing retraction and never writes a second one."""
    return bool(row.get("verdict_retracted")) \
        and str(row.get("retract_seat") or "").casefold() == seat.casefold() \
        and row.get("retract_reason") == reason \
        and row.get("retract_reads") == reads \
        and row.get("retract_basis") == basis \
        and (successor is None or row.get("retract_successor") == successor)


def _record_retract(rid, seat, role, reason, reads, basis, successor=None,
                    session=None):
    """(row, err) — append ONE `verdict-retract` event, under the lock.

    Every admission is decided again here from the locked read: the caller's
    pre-read may be seconds old, and a verdict retracted or retired in that
    window must not be retracted twice."""
    path = dispatches.ledger_path()

    def attempt(txn):
        if not txn.held:
            return None, "ledger unwritable (%s) -- retraction NOT recorded" % path
        current, unavailable = dispatches.snapshot()
        if unavailable:
            return None, "dispatch ledger unavailable: %s" % unavailable
        row, err = dispatches._resolve_row(current, rid)
        if err:
            return None, err
        if dispatches._retract_matches(row, seat, reason, reads, basis, successor):
            return row, None
        err = dispatches._retract_admission_error(row)
        if err:
            if row.get("verdict_retracted"):
                return None, dispatches.retracted_refusal(row, "it is not retracted again")
            return None, "dispatch %s cannot be retracted: %s" % (row["id"], err)
        event = {"v": 3, "event": dispatches.RETRACT_EVENT, "seq": row["seq"] + 1,
                 "id": row["id"], "ts": pk.now_ts(),
                 "retract_reason": reason, "retract_reads": reads,
                 "retract_basis": basis, "retract_role": role,
                 "retract_seat": seat,
                 "retracted_polarity": row.get("polarity"),
                 "retracted_tip": row.get("reviewed_tip"),
                 "retract_proof_version": dispatches._RETRACT_PROOF_V}
        # OMITTED WHEN THERE IS NONE, never written as null.
        if successor:
            event["retract_successor"] = successor
        author_session = row.get("verdict_author_session")
        if session and isinstance(author_session, str) and author_session:
            event["retract_same_session"] = session == author_session
        # THE REDUCER IS THE WRITER'S PROJECTION, and its refusal is an error:
        # an event it would not fold must never reach the ledger.
        out = dispatches._apply(row, event)
        if out is row:
            fields, why = dispatches._retract_record(event, row)
            return None, ("the retraction was refused by the reducer before "
                          "append (%s) — nothing was recorded" % why)
        if not txn.append(event):
            return None, "ledger unwritable (%s) -- retraction NOT recorded" % path

        def finish():
            pk.event("dispatch-retract", row["id"],
                     "%s retracted by %s (%s): reads %s" % (
                         str(row.get("polarity") or "").upper(), seat, role,
                         reads))
            return out, None
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)


def _finish_reissue(new, seat, notify):
    """Idempotently finish the post-retraction round and public hand-off."""
    opened = dispatches._open_pair(new, {}, "retract", acting=seat)
    ephemeral = {key: new[key] for key in (
        dispatches._WRITE_WARNINGS, dispatches._ADMISSION_NOTES) if key in new}
    if notify:
        context = new.get("note") or new.get("lane") or new.get("ref")
        if opened.get("room"):
            context = "%s | pair meld %s (helm chat meld join %s)" % (
                context, opened["room"], opened["room"])
        current, unavailable = dispatches.snapshot()
        live = current.get(new["id"]) if not unavailable else None
        if live is not None:
            new = live
        handoff_key = "dispatch-handoff:" + new["id"]
        failed = dispatches._notify_failed_for(new["id"])
        keyed_failure = failed and failed.get("chat_event_id") == handoff_key
        if unavailable and not failed:
            dispatches._record_notify_failed(
                new["id"], "notification reconciliation unreadable: "
                + str(unavailable), chat_event_id=handoff_key)
            failed = True
        if new.get("delivery") != "observed":
            # A failed call may still have appended before raising. Look for
            # that durable mention even when a notify-failed receipt exists;
            # the receipt records uncertainty, not non-delivery.
            mention_id, read_fault = dispatches._existing_public_notification(new)
            if not mention_id and (not failed or keyed_failure):
                if read_fault:
                    if not failed:
                        dispatches._record_notify_failed(
                            new["id"], "notification reconciliation unreadable: "
                            + read_fault, chat_event_id=handoff_key)
                else:
                    # THE ROW'S HAND-OFF IS ONE CHAT EVENT. Two retries can
                    # both read main before either posts; under one chat key
                    # the second post returns the first row, never a second
                    # mention. A failure receipt names the key only when that
                    # guarantee was already active; an older unkeyed receipt
                    # stays fail-closed because its mention may have rotated
                    # out of the room. No lock of ours is held: each ledger
                    # write here goes through its own door.
                    mention_id = dispatches._notify_public(
                        new, context, event_id=handoff_key)
            if mention_id:
                observed, _delivery_err = dispatches._mark_delivered(
                    new["id"], mention_id)
                if observed:
                    new = observed
    new = dict(new)
    new.update(ephemeral)
    if opened:
        new[dispatches._PAIR_MELD] = dict(opened)
    return new


def retract(rid, reason, reads, basis, reissue=False, successor=None,
            owner=None, notify=True):
    """(row, err) — RETRACT a standing verdict: `helm dispatch retract`.

    The row keeps its verdict and gains a `verdict-retract` event after it;
    the projection reads RETRACTED everywhere authority is read. `reads` is
    the corrected reading (RETRACT_READS), `basis` how the retracting hand
    knows (RETRACT_BASES).

    `reissue` MINTS THE SUCCESSOR REVIEW in the same motion: same recipient,
    lane, tip and kind, `--supersedes` this row, the lane author's name
    inherited as sender (the move mint `rebind` uses, so the successor does
    not read as a self-review by whoever retracted). The successor is written
    FIRST, and a retraction that then fails disowns it, so a failed call never
    leaves a successor claiming an obligation that did not move. An identical
    retry after the durable retraction repairs its one pair round and public
    hand-off without minting either twice. `successor` instead links an existing
    row that already supersedes this one.

    `owner` is the owner's capability (`ownerasks.OwnerDoor`) and nothing
    else: a caller-stated name is never the owner. Without it the acting seat
    is resolved through `_acting_author` and must hold the author's or the
    integrator's door (`_retract_role`)."""
    reason, err = dispatches._clean(reason, "retract reason", dispatches._RETRACT_REASON_CAP)
    if err:
        return None, err
    if reads not in dispatches.RETRACT_READS:
        return None, ("--reads must be one of %s (what the review now reads)"
                      % "|".join(dispatches.RETRACT_READS))
    if basis not in dispatches.RETRACT_BASES:
        return None, ("a retraction declares its basis: --%s"
                      % "|--".join(dispatches.RETRACT_BASES))
    if reissue and successor:
        return None, ("--reissue mints the successor and --successor names an "
                      "existing one: pass one")
    # THE ROW FIRST: a row this helm cannot read is refused by the vocabulary
    # rung before anything else is asked about it (`unknown_kinds_refusal`).
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    row, err = dispatches._resolve_row(current, rid)
    if err:
        return None, err
    if owner is not None:
        from . import ownerasks
        if not isinstance(owner, ownerasks.OwnerDoor):
            return None, ("the owner's door is a capability, never a name: "
                          "retract as your own seat")
        seat = ownerasks.OWNER
    else:
        seat, err = dispatches._acting_author("retract this verdict")
        if err:
            return None, err
    successor_id = None
    if successor:
        # NO allow_retired: a successor retired by the terminality rung
        # carries nothing, so it cannot be named as the row that does.
        kid, err = dispatches._resolve_row(current, successor)
        if err:
            return None, "--successor: " + err
        if kid.get("supersedes") != row["id"]:
            return None, ("--successor %s does not supersede %s, so it does "
                          "not carry this review; name the row minted with "
                          "--supersedes %s, or pass --reissue"
                          % (kid["id"][:12], row["id"][:12], row["id"][:12]))
        successor_id = kid["id"]
    if dispatches._retract_matches(row, seat, reason, reads, basis, successor_id):
        out = dict(row)
        if reissue:
            recorded = row.get("retract_successor")
            if not recorded:
                return None, dispatches.retracted_refusal(
                    row, "the recorded retraction did not reissue a successor")
            # NO allow_retired: the repair opens a round and posts a hand-off,
            # and a retired successor is terminal, so there is none to finish.
            new, err = dispatches._resolve_row(current, recorded)
            if err:
                return None, ("the recorded reissue successor cannot be "
                              "finished: " + err)
            if new.get("supersedes") != row["id"]:
                return None, ("the recorded reissue successor %s does not "
                              "supersede %s" % (new["id"][:12], row["id"][:12]))
            out["reissued"] = dispatches._finish_reissue(new, seat, notify)
        return out, None
    err = dispatches._retract_admission_error(row)
    if err:
        if row.get("verdict_retracted"):
            return None, dispatches.retracted_refusal(row, "it is not retracted again")
        return None, "dispatch %s cannot be retracted: %s" % (row["id"], err)
    role, err = dispatches._retract_role(row, seat, owner)
    if err:
        return None, err
    new = None
    if reissue:
        # THE ROW'S OWN REPOSITORY, exactly as rebind resolves it: the
        # successor re-requests the same obligation, never a new one.
        repo_path = str(row.get("repo_id") or "")[:-5] or None
        new, add_err = dispatches.add(
            row.get("recipient"), row.get("lane"),
            ref=row.get("tip") or row.get("ref"), note=row.get("note"),
            deadline_s=int(row["deadline_s"]) if row.get("deadline_s")
            else None,
            repo=repo_path, kind=row.get("kind"), notify=False,
            supersedes=row["id"], _reason=True, pair_meld=None,
            _ref_branch=row.get("ref_branch"), _preserve_origin=dispatches._MOVE_MINT,
            _refusal_door="reissue")
        if new is None:
            return None, ("retraction NOT recorded: the successor review was "
                          "refused: %s" % (add_err or "dispatch NOT recorded"))
        successor_id = new["id"]
    out, err = dispatches._record_retract(row["id"], seat, role, reason, reads, basis,
                               successor=successor_id,
                               session=home.session_id())
    if err:
        if new is not None:
            fate = dispatches._rebind_disown_child(
                new["id"], "retract aborted: the verdict on %s was not "
                "retracted" % row["id"][:12])
            err = "%s; %s" % (err, fate)
        return None, err
    out = dict(out)
    if new is not None:
        # Only a SUCCESSFUL retraction owns a successor round. The finisher is
        # idempotent because a process may die after this durable event and an
        # identical retry must repair, not duplicate, both post-write legs.
        out["reissued"] = dispatches._finish_reissue(new, seat, notify)
    return out, None


# ---------------------------------------------------------------------------
# PUBLISH BACK ONTO THE LEDGER
# ---------------------------------------------------------------------------
def owned():
    """The names this module owns, read from the LEDGER'S declaration.

    ONE SOURCE OF TRUTH. `dispatches._OWNER_NAMES` is both the tuple the
    retired-name rung reads and the tuple this publish loop walks, so a name
    added here without a declaration is not silently bound, and a declared
    name this module does not define fails loudly at publish rather than
    quietly at a caller.
    """
    token = __name__.rsplit(".", 1)[-1]
    for module, names in dispatches._OWNER_NAMES:
        if module == token:
            return names
    return ()


def _publish():
    """Bind the declared names onto the ledger module. Once, at import."""
    for _name in owned():
        setattr(dispatches, _name, globals()[_name])


_publish()
