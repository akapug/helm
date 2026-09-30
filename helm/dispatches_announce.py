"""`helm.dispatches_announce` -- WHAT A VERDICT DOES AFTER IT IS WRITTEN.

ONE QUESTION. Once the ledger holds a verdict, the rest is announcement: the
row's autoclaim is released, the announcement is attested in two phases on the
attestation sidecar (an `intent`, then a `done`), the room is told, and the
lander and the author are nudged. The readers that project those attestations
back onto rows (`attest_projections`, `with_verdict_projections`,
`attest_unverifiable`) are here with the writers they read.

MEASURED AT THE CUT, THE REMAINDER REACHES IN THROUGH SIX NAMES, all read
inside function bodies, so each resolves the published object at call time:
the verdict writer (`_announce_verdict`, `_reconcile_announce`,
`_release_autoclaim`, `gate_state`), the listing label (`_source_label`) and
the progress readers (`autoclaim_resource`).

THE EVENT-KIND CENSUS READS THE SIDECAR BY THIS SPELLING. It tells the two
ledgers apart by whether a writer calls `attest_path()`; it reads
`dispatches.attest_path()` the same way, which keeps `intent` and `done` on
the sidecar side of that census.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `dispatches.NAME` AT
ITS CALL SITE -- 50 reads of 29 distinct names; no other byte of a moved line
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
import os

from . import dispatches
from . import eventledger, pk


#: THE ACTUATOR'S RESOURCE SPELLING, WRITTEN ONCE. Six sites spelled this by
#: hand -- two here, one in the offer layer, three in idle_dispatch -- and the
#: seventh got it wrong: a cure that composed the FULL row id looked up a
#: resource nothing mints and released nothing, while its own arms minted the
#: same wrong spelling and agreed with it. The 8-char prefix is the OFFER
#: LAYER'S choice (seats_work_offer builds the offer tuple from `rid[:8]`), so
#: this function is a reader of that decision and not a second author of it.
def autoclaim_resource(rid):
    """The `dispatch:` claim resource for a row id, or None if it cannot be
    spelled. A row id shorter than the prefix has no resource rather than a
    short one -- the offer layer skips such rows for the same reason."""
    rid = str(rid or "")
    return ("dispatch:" + rid[:8]) if len(rid) >= 8 else None


def _release_autoclaim(rid):
    """Release the `dispatch:<rid>` lease the offer layer claimed for us.

    THE TOKEN IS RECOVERED, NOT DEMANDED. `_binding_ok` requires the lease
    nonce and the actuator threw its copy away, so the holder cannot present
    what it was never given -- and the binding check's own message names the
    cure for exactly that strand: `own_leases` hands a holder its own token
    back. This asks for it the same way `helm chat claims` does.

    IT CAN ONLY RELEASE OUR OWN, AND ONLY IF HELM CAN ADMIT US. The seat
    name is resolved through `helm.actors.resolve_actor` rather than read off
    the identity floor, because a release is a durable mutation and a DERIVED
    name that collided with a real holder would recover that holder's token.
    `own_leases` then returns rows held by that admitted seat and nothing
    else, so the resource is released exactly when the
    holder is the caller -- the same identity `release` would test anyway.
    Another seat's auto-claim is invisible here, which is the point.

    SILENT ON ABSENCE, BECAUSE ABSENCE IS THE ORDINARY CASE. Most verdicts
    are bound by a seat that claimed its own room (`worktree:...`) and holds
    no dispatch resource at all, so "no such lease" is not a problem to
    report. And the verdict is ALREADY WRITTEN by the time this runs: a
    lease that cannot be released costs one stale row on one surface, while
    a raised exception would cost the verdict its answer, which is the
    trade every fail-soft on this path makes.
    """
    try:
        from . import actors, seats
        # THE NAME COMES THROUGH THE ADMISSION DOOR, NOT OFF THE FLOOR. A
        # release DELETES a row from the claims ledger, so this is an
        # ACTUATOR site by the identity layer's own three-way split, and its
        # rule for one is not "classify it" but "resolve it": a DERIVED name
        # that happened to match a real holder would recover that holder's
        # token and release work still running. A caller helm cannot admit
        # releases nothing, which is the right answer and not a degradation.
        actor, err = actors.resolve_actor(
            seats._env_session(), seats.safe_cwd(),
            act="release the lease the actuator claimed for this seat")
        if err:
            return
        seat = actor.canonical_name
        resource = dispatches.autoclaim_resource(rid)
        lease = seats.own_leases(seat).get(resource) if resource else None
        if lease:
            seats.release(resource, seat, lease=lease,
                          session=seats._env_session())
    except Exception:                   # noqa: BLE001 — never fail a verdict
        pass


def gate_state(row):
    """VERIFIED <receipt> / UNVERIFIED — DERIVED from the one recorded field.

    Never stored beside `gate`: a second spelling of one fact is two places to
    update and one of them gets forgotten. UNVERIFIED is the honest reading of
    both a verdict written before `helm gate` existed and one whose evidence
    carries no token — in neither case did anything check the claim."""
    rid = str((row or {}).get("gate") or "")
    return ("VERIFIED " + rid) if dispatches._GATE_ID.fullmatch(rid) else "UNVERIFIED"


def attest_path():
    return os.path.join(os.path.dirname(dispatches.ledger_path()), "attests.jsonl")


INTENT_KEYS = frozenset(("v", "event", "id", "ts", "room", "binding"))
DONE_KEYS = frozenset(("v", "event", "id", "ts", "room", "binding",
                       "payload", "turn", "receipt", "chain", "kind", "text"))
POLARITY_SOURCE = "dispatch-store"
ATTEST_SOURCE = "attest-sidecar"


def _source_label(value):
    return str(value or "source unknown").replace("-", " ")


def _attest_rows():
    """({row id: [attest events]}, unavailable) from ONE sidecar read.

    The generic event-ledger reader owns path safety, event-size bounds, torn
    tails and strict JSON parsing. Attestation adds only its historical blank-
    line grammar and domain event-name validation before reusing `_group`.
    """
    rows, unavailable = eventledger.checked_events(
        dispatches.attest_path(), strict=True, skip_blank=True)
    if unavailable:
        return {}, "attest ledger %s" % unavailable
    if any(row.get("event") not in ("intent", "done")
           or type(row.get("id")) is not str or not row["id"] for row in rows):
        return {}, "attest ledger unscopable row"
    return dispatches._group(rows), None


def _reduce_attest(events, row, reviewed, evidence):
    """Pure per-verdict attest reduction.

    Returns (intent, done, detail, category), where category is a stable
    `unverifiable`/`unknown` protocol value and detail remains human wording.
    The writer API still exposes its historical three-tuple through
    `_attest_state`.
    """
    want = dispatches._binding_key(row, reviewed, evidence)
    rid = str(row["id"])
    intent = done = None
    for event in events:
        if event["event"] == "intent":
            bad = dispatches._intent_schema_error(event, rid, want)
            if bad:
                return None, None, bad[1], bad[0]
            if intent is not None:
                if event != intent:
                    return None, None, "conflicting attest intents", "unknown"
                continue          # byte-semantic identical duplicate: idempotent
            intent = event
        elif event["event"] == "done":
            bad = dispatches._done_schema_error(event, rid, want, intent)
            if bad:
                return None, None, bad[1], bad[0]
            if done is not None:
                if event != done:
                    return None, None, "conflicting attest done rows", "unknown"
                continue
            done = event
    return intent, done, None, None


def _attest_state(row, reviewed, evidence):
    """Single-row read wrapper preserving the writer's historical API."""
    grouped, unavailable = dispatches._attest_rows()
    if unavailable:
        return None, None, unavailable
    intent, done, detail, _category = dispatches._reduce_attest(
        grouped.get(str(row["id"]), ()), row, reviewed, evidence)
    return intent, done, detail


def attest_unverifiable(rows):
    """The decided row ids whose signed delivery record is unverifiable.

    This compatibility surface from the attest-split lane delegates to the
    typed projection rather than interpreting the sidecar again. It is read-only,
    reads the sidecar at most once for the page, and intentionally excludes
    unreadable/unknown state: those are not evidence of a binding split.
    """
    projected = dispatches.attest_projections(rows)
    return frozenset(rid for rid, state in projected.items()
                     if state.get("attest_state") == "unverifiable")


def _intent_schema_error(r, rid, want):
    if set(r.keys()) != dispatches.INTENT_KEYS:
        return "unknown", "attest intent schema violation (keys)"
    if type(r["v"]) is not int or r["v"] != 1:   # bool is not int here
        return "unknown", "attest intent schema violation (v)"
    if not (isinstance(r["ts"], str) and r["ts"]):
        return "unknown", "attest intent schema violation (ts)"
    if not (isinstance(r["room"], str) and r["room"]):
        return "unknown", "attest intent schema violation (room)"
    if r["binding"] != want:
        return "unverifiable", "attest intent binding mismatch"
    return None


def _done_schema_error(r, rid, want, intent):
    if intent is None:
        return "unknown", "attest done without intent"
    if set(r.keys()) != dispatches.DONE_KEYS:
        return "unknown", "attest done schema violation (keys)"
    if type(r["v"]) is not int or r["v"] != 1:   # bool is not int here
        return "unknown", "attest done schema violation (v)"
    if not (isinstance(r["ts"], str) and r["ts"]):
        return "unknown", "attest done schema violation (ts)"
    if r["room"] != intent["room"]:
        return "unverifiable", "attest done room mismatch"
    if r["binding"] != want:
        return "unverifiable", "attest done binding mismatch"
    if not isinstance(r["text"], str):
        return "unknown", "attest done schema violation (text)"
    if r["kind"] == "attested":
        if not (isinstance(r["payload"], str) and r["payload"]
                and isinstance(r["turn"], str) and r["turn"]
                and isinstance(r["receipt"], str) and r["receipt"]
                and type(r["chain"]) is int):
            return "unknown", "attest done evidence incomplete for attested"
    elif r["kind"] == "cite-tier":
        if not (r["payload"] == "" and r["turn"] == "" and r["receipt"] == ""
                and r["chain"] is None):
            return "unknown", "attest done evidence inconsistent for cite-tier"
    else:
        return "unknown", "attest done unknown kind"
    return None


def _binding_key(row, reviewed, evidence):
    import hashlib
    raw = "\x1e".join([str(row.get("lane") or ""), reviewed,
                        str(row["id"]), str(evidence or "")])
    return hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()


def _report_from_done(done, row, reviewed, evidence, chat):
    """The replayed report is RE-VERIFIED, never trusted (round-5 blocker 1):
    an attested done must recompute — committed-signed shape over its stored
    transport evidence AND chat.payload_for over the LEDGER-truth binding +
    the stored text must equal the stored payload. Verification failure is
    UNKNOWN-grade (None), not a downgrade to cite."""
    if done["kind"] == "cite-tier":
        return "delivered UNSIGNED (cite-tier) to %s" % done["room"]
    synthetic = {"vlane": str(row.get("lane") or ""), "vtip": reviewed,
                 "vrid": str(row["id"]), "vref": str(evidence or ""),
                 "text": done["text"], "payload": done["payload"],
                 "turn": done["turn"], "receipt": done["receipt"],
                 "chain": done["chain"]}
    if (chat.committed_signed(synthetic)
            and chat.payload_for(synthetic) == done["payload"]):
        return "attested (signed turn, room %s)" % done["room"]
    return None


def _attest_projection(row, grouped, unavailable):
    """Typed, read-only attestation axis for one canonical dispatch row.

    Dispatch status/polarity and verdict attestation are separate ledgers and
    separate facts. The old code detected a cross-ledger binding mismatch only
    while a writer retried `verdict`, then printed it to an ephemeral console
    line. A reader of either durable row surface saw nothing. This projection
    makes the already-durable mismatch visible without appending or re-signing.
    """
    has_verdict = row.get("status") in ("verdict", "closed") \
        and row.get("verdict_ref") is not None
    base = {"polarity_source": dispatches.POLARITY_SOURCE if has_verdict else None,
            "attest_source": dispatches.ATTEST_SOURCE if has_verdict else None,
            "attest_state": None, "attest_detail": None}
    if not has_verdict:
        return base
    reviewed = str(row.get("reviewed_tip") or "")
    evidence = str(row.get("verdict_ref") or "")
    if unavailable:
        intent = done = None
        detail, category = unavailable, "unknown"
    else:
        intent, done, detail, category = dispatches._reduce_attest(
            grouped.get(str(row["id"]), ()), row, reviewed, evidence)
    if detail:
        base["attest_state"] = category
        base["attest_detail"] = detail
        return base
    if done:
        from . import chat
        report = dispatches._report_from_done(done, row, reviewed, evidence, chat)
        if report is None:
            base["attest_state"] = "unverifiable"
            base["attest_detail"] = (
                "attest done failed signed-turn/payload re-verification")
            return base
        base["attest_state"] = done["kind"]
        base["attest_detail"] = report
        return base
    if intent:
        base["attest_state"] = "doubt"
        base["attest_detail"] = (
            "attest intent exists without a durable done; read-only, never re-sign")
        return base
    base["attest_state"] = "none"
    base["attest_detail"] = "no attest intent recorded for this verdict"
    return base


def _has_verdict(row):
    return row.get("status") in ("verdict", "closed") \
        and row.get("verdict_ref") is not None


def attest_projections(rows):
    """{dispatch id: typed attestation fields} from at most ONE sidecar read."""
    rows = list(rows)
    grouped, unavailable = dispatches._attest_rows() if any(map(dispatches._has_verdict, rows)) \
        else ({}, None)
    return {str(row["id"]): dispatches._attest_projection(row, grouped, unavailable)
            for row in rows}


def with_verdict_projections(rows):
    """Copy rows with source/attestation axes, from at most ONE sidecar read."""
    rows = list(rows)
    grouped, unavailable = dispatches._attest_rows() if any(map(dispatches._has_verdict, rows)) \
        else ({}, None)
    out = []
    for row in rows:
        item = dict(row)
        item.update(dispatches._attest_projection(row, grouped, unavailable))
        out.append(item)
    return out


def _announce_verdict(row, reviewed, evidence):
    """Attest a fresh verdict AT MOST ONCE with replay closedness.

      unknown ledger state -> NEEDS CONFIRMATION, read-only (never no-intent)
      done                 -> report DERIVED from its fields
      intent without done  -> DOUBT: read-only confirmation from the intent's
                              STORED room; never re-signs
      no intent            -> the one provably sign-free state: append intent
                              under lock (single-flight), emit outside it."""
    path = dispatches.attest_path()
    with eventledger.locked(path) as held:
        if not held:
            return ("NEEDS CONFIRMATION — attest ledger unwritable (%s); "
                    "announce not attempted" % path)
        intent, done, unknown = dispatches._attest_state(row, reviewed, evidence)
        if unknown:
            return ("NEEDS CONFIRMATION — %s; read-only until repaired"
                    % unknown)
        if done:
            from . import chat
            state = dispatches._report_from_done(done, row, reviewed, evidence, chat)
            return state or ("NEEDS CONFIRMATION — attest done fails "
                             "re-verification; read-only until repaired")
        if not intent:
            from . import chat as _chat
            room = os.environ.get("HELM_VERDICT_ROOM") or _chat._default_post_room()
            # attestations follow the project room the verdict belongs to
            # (the owner asked why they landed in #main while the fleet works
            # in #helm); HELM_VERDICT_ROOM stays the deliberate-centralization
            # override
            if not eventledger.append_unlocked(path, {
                    "v": 1, "event": "intent", "id": row["id"],
                    "ts": pk.now_ts(), "room": room,
                    "binding": dispatches._binding_key(row, reviewed, evidence)}):
                return ("NEEDS CONFIRMATION — attest intent unwritable; "
                        "announce not attempted")
    if intent:
        return dispatches._confirm_from_room(row, reviewed, evidence, intent)
    return dispatches._emit_and_record(row, reviewed, evidence, room)


def _land_nudge_parts(row):
    """landreq's (word, why, command) for this row, for the nudge below.

    THE WHOLE TRIPLE, because taking [0] threw away the half that says WHY.
    A nudge reading UNVERIFIED with no reason tells the reader something is
    wrong and nothing about what — and the reason was already computed one
    call away. Deferred import: landreq imports this module."""
    from . import landreq
    return landreq.land_nudge_instruction(row["id"])


def _verdict_land_nudge(row):
    """An APPROVE binds — DM the lander the landing command.

    2026-07-29 council G2: 4 stale rows closed by hand because the APPROVE
    settled and then NOTHING woke the lander, and land had no surface at all
    (memory lr-assigns-reviewers-but-never-wakes-them). The review leg has
    idle_dispatch; the LAND leg should have this — one contextual DM per
    approve, never a nag loop. Called AFTER mark_verdict returns (the verdict
    is durable under the ledger lock; the nudge is delivery and must NOT hold
    the lock — a stalled chat transport would stall every verdict in the
    fleet behind it, measured: OI posted the exact trap shape an hour ago).
    A failed DM is invisible (never stops the verdict from binding: the
    verdict is durable; the nudge is delivery).

    IT PRESCRIBES ONLY WHAT THE LADDER PLAINLY ALLOWS — one defect with two
    faces, not two defects. This line was built from verdict POLARITY ALONE
    and never consulted the row's readiness at all, so every approve got the
    identical "ready: helm lr land <id>". Two live specimens, both
    misinforming a real recipient: a row whose author and builder were the
    same seat and whose review field was EMPTY while the projection already
    read READY-SELF-REVIEW; and a row hundreds of commits behind trunk whose
    gate bound a tree that no longer existed. The first cure special-cased
    SELF-REVIEW; self-review is ONE rung of a ladder.

    ONE RULE, AND THE ENUMERATION IS NOT HERE. `landreq.land_instruction`
    answers what this row IS through the projection the board itself reads;
    this asks for the word, prescribes only on plain READY, and carries
    whatever else it gets — SELF-REVIEW, CONTESTED, STALE-BASE, UNVERIFIED,
    a terminal, or a rung the ladder learns tomorrow — without naming one of
    them. A list of states in the notifier is the same defect regrowing one
    branch at a time.

    THE CURE DISCLOSES, NEVER WITHHOLDS. A suppressed DM would rebuild the G2
    defect this nudge exists to cure (an APPROVE that wakes nobody), and the
    threat model is accident — the recipient is misinformed, not attacked, so
    the fix is the missing field said out loud. The command still arrives;
    the "ready:" prescription does not, because ready is exactly what the
    projection says this row is not. Both legs carry the word — the fallback
    room post is the half that is easy to get wrong twice."""
    tip = str(row.get("reviewed_tip") or "?")[:10]
    lane = row.get("lane") or "?"
    # ONE MINTING FOR BOTH LEGS. The command carries --ack-deletions when the
    # door will demand it, and the word and reason come from the same call —
    # so the DM and the room post cannot disagree about what this row is.
    word, why, cmd = dispatches._land_nudge_parts(row)
    if word == "READY":
        # THE ROOM LEG SAYS READY TOO. It used to carry only the command, so
        # the one word a reader scans for was present on one leg and absent
        # on the other, and the quieter leg read like a weaker claim.
        return dispatches._nudge(dispatches._default_lander(),
                      "VERDICT APPROVE at %s — ready: %s" % (tip, cmd),
                      "%s: VERDICT APPROVE — ready: %s" % (lane, cmd))
    # THE REASON RIDES WITH THE WORD. "UNVERIFIED" alone names a problem and
    # withholds the only part that lets the reader act on it.
    detail = "%s (%s)" % (word, why) if why else word
    return dispatches._nudge(dispatches._default_lander(),
                  "VERDICT APPROVE at %s — %s, not plainly READY. Read it, "
                  "then decide: %s" % (tip, detail, cmd),
                  "%s: VERDICT APPROVE %s — %s" % (lane, detail, cmd))


def _default_lander():
    """The seat that folds, ASKED in ONE place.

    Both nudges route through here and a third copy would have put the same
    question in shipping logic three times — the hardcode rung flagged the
    second one the moment it appeared.

    THE ANSWER IS READ, NOT SPELLED. A literal default keeps naming that seat
    after the roster stops carrying it, and nothing fails loudly: the string
    is truthy, the DM is addressed, the room-post fallback is addressed, and
    both report a delivery neither made. seats_lander OBEYS HELM_LANDER when
    it is set — an override exists for the case where the ordinary resolution
    is wrong, and the roster is among the things that can be wrong, so
    confirming it there would take the hatch away at the one moment it is
    needed — and otherwise asks the integrator role, which is the
    relationship the literal was silently asserting anyway.

    SO THE TYPO'S COST LANDS HERE, and `_nudge` below is where it is now
    reported: an override that names nobody is obeyed, the DM does not
    deliver, and the fallback addresses the same name. That is the whole
    reason this call has a warning beside it rather than a silent False.

    IT STILL CANNOT RETURN EMPTY, and `_nudge` below depends on that: its
    fallback is ADDRESSED BY CONSTRUCTION, so a refusal here would emit the
    unaddressed room post that fallback exists to cure. `lander_seat_or_default`
    keeps that invariant and announces a substitution rather than making one
    silently.
    """
    from . import seats_lander
    return seats_lander.lander_seat_or_default()


def _nudge_undelivered(to, why):
    """Say once that a nudge's DM did not land -> None. Never raises.

    ONE LINE PER RECIPIENT PER PROCESS, through the same `_warn_once` the
    integrator and lander doors use for their own unresolved cases, so a
    verdict loop cannot turn a real signal into noise.
    """
    try:
        from .seats_identity import _warn_once
        _warn_once("nudge-undelivered:%s" % to,
                   "a verdict nudge to %r did not deliver (%s) — it was "
                   "posted to the room addressed to the lander instead, which "
                   "reaches nobody if that name has no roster row\n"
                   % (to, why or "no reason given"))
    except Exception:                                       # noqa: BLE001
        pass          # a report about a failed delivery must not fail a verdict


def _nudge(to, body, context):
    """DM `to`; on ANY failure fall back to an ADDRESSED room post. Never raises.

    ONE shape for both poles, because the fallback is the half that is easy to
    get wrong twice — and it already was wrong once. The approve nudge's own
    fallback was DEAD CODE: it branched on `if lander:` immediately after
    binding the lander to a value that cannot be falsy, so the else could
    never execute; and had it executed it would have posted the string
    "@None". The real failure mode is a DM that does not DELIVER, never a name
    that is empty, so that is what this branches on instead.

    The fallback is ADDRESSED BY CONSTRUCTION — _default_lander() cannot return
    empty, so this can never emit the unaddressed room post that is the exact
    defect the author nudge exists to cure.

    An EMPTY `to` skips straight to the fallback rather than reaching the
    transport with nothing to address — an unrecorded author is a known state,
    not an error to discover by exception.

    Returns True when the DM itself landed.
    """
    from . import seats
    try:
        if to:
            # THE DM'S TWO FAILURE EXITS ARE REPORTED BY ONE PATH. A returned
            # (None, reason) and a RAISED transport error both mean the same
            # thing to a reader — the verdict did not reach this name — and
            # the report covered only the first because it sat after the call
            # inside the outer guard, so an exception jumped past it to the
            # silent pass. Narrowing the try to the CALL puts both exits in
            # front of the same reporter.
            #
            # THE RAISE BECOMES ITS OWN REASON rather than a generic word:
            # the exception type and message are what was OBSERVED, and this
            # door still cannot say whether the name is wrong or the
            # transport is down. Saying which would be a diagnosis it has no
            # evidence for.
            try:
                sent, why = seats.dm(to, body, who="dispatches")
            except Exception as exc:                        # noqa: BLE001
                sent, why = None, "the DM raised %s: %s" % (
                    type(exc).__name__, exc)
            # BOTH LEGS MUST AGREE before this reads as delivered. dm's
            # contract is exactly (row, None) or (None, reason); a (row,
            # reason) shape is a CONTRACT VIOLATION, and treating any
            # non-None row as delivery would suppress the fallback while
            # reporting success — a silent wake-path loss inside the cure
            # for silent wake-path loss. Violations
            # fall through to the addressed fallback, which over-delivers;
            # over-waking the lander is recoverable, a swallowed verdict
            # is the founding defect.
            if sent is not None and why is None:
                return True
            # THE DM'S REASON IS THE ONLY EVIDENCE ANYONE EVER GETS, and
            # until now it was spent as a BRANCH and never said out loud.
            # Every later chance to notice is closed on purpose: the
            # fallback's `chat.post` returns a row and reports nothing about
            # whether the addressee resolves (the ABSENT line belongs to the
            # command surface, not to `post`), this function returns False,
            # and both call sites are bare statements. So a verdict addressed
            # to a name nothing answers to went to nobody and said so nowhere.
            #
            # NOT BLOCKING IS NOT THE SAME PROPERTY AS NOT REPORTING. The
            # silence below is deliberate and stays — a nudge must never block
            # a verdict — but a warning is not a block, and the operator who
            # can fix a misaddressed lander is the one running this command.
            # The text says what was OBSERVED, never what it means: a DM can
            # fail for a name that does not resolve and for a transport that
            # did not answer, and this cannot tell them apart.
            dispatches._nudge_undelivered(to, why)
    except Exception:
        pass                          # a nudge must never block a verdict
    try:
        from . import chat
        chat.post("@%s %s" % (dispatches._default_lander(), context), who="dispatches")
    except Exception:
        pass
    return False


def standing_room_line(row):
    """One sentence naming the standing room of the row's author and reader
    (task/3560), or "" when the pair keeps none. Never raises."""
    try:
        from . import meld_standing
        room = meld_standing.open_for(str(row.get("sender") or ""),
                                      str(row.get("recipient") or ""))
    except Exception:                                   # noqa: BLE001
        room = None
    if not room:
        return ""
    return ("Talk it through in your standing room %s: helm chat meld say "
            "%s \"...\" (never blocks), helm chat meld recv %s" % (
                room, room, room))


def _verdict_author_nudge(row):
    """A NON-APPROVE verdict hands the row BACK — wake the seat it went back to.

    IT HANDS BACK A DECISION, NOT NECESSARILY THE TYPING. A FIX may carry the
    reviewer's own committed cure (`patch_tip`), and then what goes back is a
    rebase and a re-dispatch. The wake is owed either way: the lane owner is
    the one seat that can compose the next tip.

    _verdict_land_nudge above covers the APPROVE pole and DMs the LANDER.
    NOTHING covered the other pole, and that omission is the owner's P0 word
    for word: a CHANGES_REQUESTED verdict hands the row back to its author and
    nothing wakes them. The verdict is not lost — _announce_verdict posts it
    and the ledger holds it — but it is announced into the project room with
    NO @mention, and helm's own beacon contract is that a home room does not
    wake a seat row-by-row. Correct, durable, readable on demand, and reaching
    nobody. The half of the loop where the news is good got a wake path a
    fortnight ago; the half where work comes back never did.

    MEASURED 2026-08-11T00:02Z, which is why this ships as a nudge and not as
    a rule telling seats to poll: 22 codex-family verdicts in the preceding two
    hours, 21 of them fix. In the ten minutes before the ledger was read, one seat
    posted that its review was "OPEN ... sent 20 minutes ago" when the verdict
    was already 26 minutes old, and another posted that its three lanes
    were awaiting a reviewer when all three had come back 17-23 minutes
    earlier. Four rows, two seats, every one idle on news that had arrived —
    and the integrator held two more of its own in the same state.

    THE FALLBACK IS ADDRESSED, NEVER AMBIENT. An unusable sender is the one
    case that could rebuild this defect inside its own cure, because posting
    into the room with nobody named is precisely what already fails. So an
    author who cannot be resolved routes to the LANDER BY NAME and says the
    author could not be woken. Waking someone who cannot act is recoverable;
    waking nobody is the thing just measured.

    Delivery, never binding: called AFTER mark_verdict returns, outside the
    ledger lock, and every failure is swallowed — a stalled transport must not
    stall the fleet's verdicts behind it (the finding on the approve pole,
    which applies here unchanged).
    """
    pol = (row.get("polarity") or "UNDECLARED").upper()
    rid = row["id"][:17]
    # resolve_recipient inside dm() is the membership test — an unknown or
    # family-floor sender comes back (None, reason) rather than routing a DM
    # into a lane no seat reads, and _nudge turns that into the room fallback.
    # THE CUSTODIAN IS TOLD, NOT THE AUTHOR. This DM is the delivery leg
    # itself — "your row is back with you" — so after a transfer it must reach
    # the seat that now has to act, never the dead one that wrote it.
    sender = dispatches.custodian_of(row)
    # THE PAIR'S STANDING ROOM IS WHERE THE HAND-BACK IS TALKED THROUGH
    # (task/3560): a pair that keeps one names it here, never a new meld.
    standing = dispatches.standing_room_line(row)
    # RETURNED, NOT SWALLOWED: True means the author's own DM landed, False
    # means it fell back to the lander. A delivery leg that reports nothing
    # can only be tested through its mocks, and an assertion about a mock is
    # an assertion about the test (the vacuous-assertion rung's rule, which
    # fired on this lane's first two drafts and was right both times).
    return dispatches._nudge(sender,
                  "VERDICT %s — %s IS BACK WITH YOU (reviewed tip %s). Nothing "
                  "else will tell you: read it with helm dispatch triage %s%s"
                  % (pol, row.get("lane") or "your lane",
                     str(row.get("reviewed_tip") or "?")[:12], rid,
                     ". " + standing if standing else ""),
                  "VERDICT %s on %s (%s) — ITS AUTHOR COULD NOT BE WOKEN "
                  "(sender %s). Route it by hand."
                  % (pol, row.get("lane") or "?", rid, sender or "UNRECORDED"))


def _record_done(row, reviewed, evidence, room, turn, chat):
    """Verify the turn FIRST — exact wire types AND the full verdict binding
    against ledger truth — then append under a lock that RE-READS state:
    already-done returns the standing report idempotently (two stale
    confirmation writers can never both append — round-5 blocker 4); any
    unknown/conflict bails. Returns the state string on durable success,
    None otherwise."""
    if not dispatches._is_this_verdicts_turn(turn, row, chat):
        return None                          # wrong/absent binding: never
    text = turn.get("text")
    if not isinstance(text, str):
        return None
    # The wire-value partition is EXACT (round-7): a turn is either the
    # complete signed shape, or the exactly-empty unsigned shape — anything
    # else (falsey wrong types included: 0/False/[]/{} launder through
    # truthiness) is NEEDS CONFIRMATION with zero write.
    def _empty_str(v):
        return type(v) is str and v == ""
    signed_shape = (isinstance(turn.get("turn"), str) and turn.get("turn")
                    and isinstance(turn.get("receipt"), str)
                    and turn.get("receipt")
                    and type(turn.get("chain")) is int)
    unsigned_shape = (_empty_str(turn.get("turn") if "turn" in turn else "")
                      and _empty_str(turn.get("receipt") if "receipt" in turn else "")
                      and turn.get("chain") is None
                      and _empty_str(turn.get("payload") if "payload" in turn else ""))
    if not signed_shape and not unsigned_shape:
        return None                          # outside the partition: refuse
    if signed_shape:
        if not (isinstance(turn.get("payload"), str) and turn["payload"]
                and chat.payload_for(turn) == turn["payload"]):
            return None                      # forged/invalid: no done, ever
        done = {"v": 1, "event": "done", "id": row["id"], "ts": pk.now_ts(),
                "room": room, "binding": dispatches._binding_key(row, reviewed, evidence),
                "payload": turn["payload"], "turn": turn["turn"],
                "receipt": turn["receipt"], "chain": turn["chain"],
                "kind": "attested", "text": text}
    else:
        done = {"v": 1, "event": "done", "id": row["id"], "ts": pk.now_ts(),
                "room": room, "binding": dispatches._binding_key(row, reviewed, evidence),
                "payload": "", "turn": "", "receipt": "", "chain": None,
                "kind": "cite-tier", "text": text}
    with eventledger.locked(dispatches.attest_path()) as held:
        if not held:
            return None
        intent, existing, unknown = dispatches._attest_state(row, reviewed, evidence)
        if unknown:
            return None
        if existing is not None:
            return dispatches._report_from_done(existing, row, reviewed, evidence, chat)
        if intent is None:
            return None                      # state moved under us: bail
        if not eventledger.append_unlocked(dispatches.attest_path(), done):
            return None
    return dispatches._report_from_done(done, row, reviewed, evidence, chat)


def _emit_and_record(row, reviewed, evidence, room):
    try:
        from . import chat
        turn = chat.post(
            "VERDICT %s — lane %s tip %s (dispatch %s)" % (
                evidence, row.get("lane") or "?", reviewed[:12], row["id"]),
            room=room, ambient=True,
            # The signed vlane is the row's STORED lane, byte-for-byte —
            # _report_from_done reconstructs the payload from that same field,
            # so signing any normalized spelling self-invalidates the attest
            # (#142 r2, finding 1). Family joins normalize at read.
            verdict={"lane": row.get("lane"), "tip": reviewed,
                     "rid": row["id"], "ref": evidence})
    except Exception as exc:
        why = "%s: %s" % (exc.__class__.__name__, exc)
        pk.event("dispatch-verdict-announce-failed", row["id"], why)
        # intent stands; the next retry enters the DOUBT cell (never re-signs)
        return "NEEDS CONFIRMATION — attestation turn not delivered (%s)" % why
    state = dispatches._record_done(row, reviewed, evidence, room, turn, chat)
    return state or ("NEEDS CONFIRMATION — emitted turn failed verification "
                     "or the done record is unwritable; durable state still "
                     "shows doubt (room %s)" % room)


def _confirm_from_room(row, reviewed, evidence, intent):
    """The DOUBT cell: intent recorded, completion unknown. Upgrade ONLY by
    read-only evidence — the true turn found in the intent's STORED room
    (env changes never redirect the search: finding C). Unreadable
    or absent evidence keeps the doubt; nothing here ever emits or signs."""
    room = intent.get("room") or "main"
    try:
        from . import chat
        msgs, _total = chat.read(room)
    except Exception as exc:
        return ("NEEDS CONFIRMATION — announce in doubt; room %s unreadable "
                "(%s: %s)" % (room, exc.__class__.__name__, exc))
    for r in reversed(msgs):
        if not dispatches._is_this_verdicts_turn(r, row, chat):
            continue
        state = dispatches._record_done(row, reviewed, evidence, room, r, chat)
        if state:
            return state
    return ("NEEDS CONFIRMATION — announce in doubt (intent recorded for "
            "room %s, completion unknown; will not re-sign)" % room)


def _is_this_verdicts_turn(r, row, chat):
    """The FULL binding must match — vrid alone is spoofable by any signed
    row carrying the field (xrev finding #2). Exact verdict shape,
    no shape overlap, and all four fields equal to the ledger's truth."""
    return (chat.is_verdict(r) and not chat.is_reply(r)
            and not r.get("ack") and not r.get("react")
            and r.get("vrid") == str(row["id"])
            and r.get("vlane") == str(row.get("lane") or "")
            and r.get("vtip") == row.get("reviewed_tip")
            and r.get("vref") == str(row.get("verdict_ref") or ""))


def _reconcile_announce(row):
    """Idempotent-retry truth: report the durable attest state; the doubt
    cell may upgrade read-only; heal-by-emit happens ONLY when no intent
    exists (a verdict recorded before the attest ledger existed, or a crash
    strictly before intent — the one provably sign-free state)."""
    return dispatches._announce_verdict(row, row["reviewed_tip"],
                             str(row.get("verdict_ref") or ""))


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
