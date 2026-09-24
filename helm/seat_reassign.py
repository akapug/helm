"""`helm seat reassign` — move EVERY holding of a dead or renamed seat, in one
verb and one ledger event.

WHY A NEW DOOR RATHER THAN A WIDENED OLD ONE. Three verbs already move work
between seats and ALL THREE refuse the dead-seat case, each for a reason that
is correct for its own question (surveyed 2026-08-27, file:line verified):

  `dispatch rebind`   demands MEASURED STARVATION or context exhaustion
                      (dispatches.py, `_recipient_evidence`). Death is
                      neither: a seat with no process is not starved.
  `task takeover`     demands the incumbent's pane be LIVE (takeover.py,
                      "proxywatch did not measure the incumbent pane LIVE —
                      UNKNOWN cannot authorize takeover"). That is the exact
                      INVERSE of this population.
  `release_stale`     demands the holder's session be provably dead
                      (actors.prove_stale). A RENAME leaves the session
                      running, so it answers "live" and the door closes.

A dead or renamed seat falls through all three. There is no gate to widen, so
this verb brings its own evidence rather than borrowing one that was built to
answer something else.

WHY THE HOLDINGS MUST MOVE RATHER THAN BE ALIASED. A rename already writes an
alias list (`seat_keys`) onto the roster row, and it is read by exactly TWO
sites, both in seats_receipts.py — chat receipts. dispatches, tasks and
seats_claims all compare with EXACT canonical-token equality or raw `!=`
(`seats_common.recipient_matches`: "Exact canonical-token equality — never
substring or slug equality"; `seats_claims._binding_ok`: `seat !=
row.get("holder")`, deliberately not even casefolded). Teaching every
comparator about aliases would mean teaching every comparator, forever. The
bytes move instead.

THE TWO SEAMS THAT MAKE ROLE-ADDRESSING POSSIBLE LATER WITHOUT PAYING FOR IT
NOW (decided 2026-08-27 under owner canon "work binds to a ROLE, not to an
ephemeral identity"):

  SOURCE IS A SESSION FIRST.  `resolve_source` resolves a session id before a
  name, because the case this verb exists for is precisely the one where the
  name already changed underneath the holdings. A name-keyed source verb is
  useless exactly when it is needed.

  TARGET RESOLVES THROUGH ONE DOOR.  `resolve_target` is the single function
  that turns an operand into a seat. Today it accepts an exact roster key and
  falls back to a FAMILY token naming the one live seat of that family. When
  roles become first-class, this function changes and the four ledgers do not.
  Whether every mover can ADDRESS that seat is a separate question, asked by
  `move_refusals` once the census knows which movers will run.

WHAT IS DELIBERATELY NOT BUILT: a role COLUMN on dispatch/task/lease rows.
That is a schema change across four ledgers, and every row written before it
would carry a null role that reads as "no role" rather than "unknown" — an
absence minted to look like an answer. Role STORAGE is a separate row for
whenever something needs to ask, after the fact, what role held a thing.
"""
import contextlib
import hashlib
import json
import os

from . import home, pk


# ---------------------------------------------------------------------------
# THE SEAM IS A THIRD ARTIFACT AND IT HAS AN OWNER: THIS FILE.
#
# This subsystem was built by three writers in one worktree — this module and the
# verb here, the send-BODY storage in dispatches.py, and the lease-holder
# rebind in seats_claims.py. EACH HALF BEING GREEN PROVES NOTHING ABOUT THE
# COMPOSITION: every half was verified against a tree the other halves had not
# landed in, so all three can be individually correct and jointly wrong.
#
# THE COMPOSITION CLAIM IS THE OWNER'S DOGFOOD BAR, not a suite result: move a
# dead seat's holdings through this verb and have the recipient read the BRIEF
# OFF THE ROW with no DM. That is one sentence and it spans all three halves —
# `holdings` must find the row, `_move_dispatch` must move it, the body must
# have travelled, and the lease must be releasable by the new holder. A green
# `tests.test_seat_reassign`, a green `tests.test_dispatches` and a green
# `tests.test_seats_claims` do not add up to it.
#
# So the composition gets its own arm and its own gate on the COMPOSED tree,
# and neither is satisfied by three passing halves. If you are reading this
# because you are about to land one half alone: the half without the body
# storage moves a row nobody can read, which just relocates the re-briefing.
# ---------------------------------------------------------------------------

LEDGER = "seat-reassigns.jsonl"

# The four surfaces a seat can hold work on. Order is the order the manifest
# prints and the order the moves run: the cheapest-to-undo first, so a partial
# run leaves the least surprising state.
SURFACES = ("leases", "dispatch_in", "dispatch_out", "tasks")

# Source dispositions. DEAD and GONE proceed; LIVE is a MEASURED
# CONTRADICTION and refuses without --force; UNKNOWN proceeds WITH THE UNKNOWN
# NAMED, because refusing on absence is what makes a recovery verb useless at
# the one moment it is needed — the morning after a reboot, when nothing can
# be measured about a process that no longer exists. Owner canon for this row:
# "refuse only on measured contradiction, never on absence."
SOURCE_DEAD, SOURCE_LIVE, SOURCE_UNKNOWN = "dead", "live", "unknown"


def ledger_path():
    return os.path.join(home.global_dir(), LEDGER)


def resolve_source(token):
    """(seat, session, how) for the seat whose holdings are moving, or
    (None, None, refusal).

    SESSION BEFORE NAME, and the order is the whole point. This verb's
    population is seats whose NAME stopped being a reliable key — a rename
    carries the session and orphans every row that spelled the old name. A
    resolver that asked for the name first would answer correctly only in the
    cases where the verb was not needed.

    A source that resolves to NO roster row at all is not an error here. That
    is the pure rename-orphan case: the holdings exist, spelled with a name
    nothing answers to any more, and moving them is exactly the job. The token
    is returned as the holder key to search for.
    """
    from . import seats_roster
    t = str(token or "").strip()
    if not t:
        return None, None, "name the seat or session whose holdings should move"
    # AN UNREADABLE ROSTER IS NOT AN ABSENT SEAT. Falling through to "treat
    # the token as a bare holder key" would silently downgrade every
    # resolution to its weakest form at the moment the strongest evidence
    # became unavailable.
    #
    # THE TRI-STATE READER, BECAUSE THE FAIL-OPEN ONE CANNOT RAISE AND THE
    # GUARD ABOVE WAS WRITTEN FOR A RAISE. `seats_common.roster()` is
    # `pk.read_json(roster_path(), {}) or {}`, and `pk.read_json` swallows
    # missing, unreadable, malformed and wrong-shaped alike. MEASURED against
    # the real reader: missing, malformed and chmod-000 all return `{}` with
    # NO exception, and a roster file holding a JSON ARRAY comes back as that
    # LIST — which `t in r` then tests as list membership and the family scan
    # iterates as if it were seat names. So the strongest evidence going
    # unavailable is exactly the case that reached the weak path, through a
    # door the comment above already forbade.
    #
    # `roster_checked` is the reader written for this caller — its own
    # docstring says it is "for truth consumers that cannot accept fail-open
    # {}", and that "unreadable, malformed, or wrong-shaped state is a failed
    # probe: callers render UNKNOWN rather than treating a parse failure as
    # evidence that no seat exists". MEASURED on the same four inputs: missing
    # is failed=False and proven empty, malformed and wrong-shape are both
    # failed=True, a good roster is unchanged.
    r, roster_failed = seats_roster.roster_checked()
    if roster_failed:
        return None, None, ("the roster could not be read, so whether %r "
                            "names a live seat is UNKNOWN" % t)
    # THE EXACT PATH REFUSES AMBIGUITY TOO, and it did not before. It called
    # `seat_for_session`, which on a multi-row match WARNS and returns hits[0]
    # by binding age — correct for a caller that merely NAMES an agent, and
    # wrong here for the reason `seats_for_session_prefix` states in its own
    # docstring: "a caller that MOVES CUSTODY cannot [take the first hit],
    # because a prefix matching two seats would hand one seat's holdings to
    # the wrong successor." The prefix branch below already honoured that; this
    # branch, two lines away, did not. A warning is not a refusal, and nobody
    # reads a warning on a command that then reports success.
    #
    # THIS IS A LIVE STATE, not a hypothetical: seats that share ancestry can
    # be independently live descendants carrying the same remembered session,
    # and they must not be collapsed onto one holder.
    # THE EXACT HISTORICAL SID THE OPERATOR NAMED IS WHAT TRAVELS, not the
    # seat's CURRENT session. Returning row["session"] here discarded the
    # incarnation actually being reassigned and substituted whatever that seat
    # is bound to now — so the audit row claimed a move of the live
    # incarnation when the operator had named a historical one, and the fence
    # keyed on the wrong identity. `t` matched by exact session lookup, so it
    # IS an exact stored session; hand it back unchanged.
    exact = seats_roster.seats_for_session(t) if len(t) >= 8 else []
    if len(exact) == 1:
        return exact[0], t, ("session %.8s -> seat %s" % (t, exact[0]))
    if len(exact) > 1:
        return None, None, ("session %.8s is remembered by %d seats (%s) — a "
                            "custody move may not pick by binding age; name "
                            "the seat exactly, or repair the roster with "
                            "`helm chat seat disown <wrong-seat> %.8s`"
                            % (t, len(exact), ", ".join(sorted(exact)), t))
    if t in r:
        return t, (r[t] or {}).get("session"), "exact roster row"
    # A DISPLAYED SESSION IS A PREFIX. `seat_for_session` matches a
    # session id EXACTLY, but every surface that shows an operator a sid prints
    # an 8+-char prefix — so the id they were shown resolved to NOTHING here,
    # fell through to the orphan branch below, and the exact holder-key
    # comparisons then found no holdings and reported a confident TOTAL 0 at
    # rc 0. A silent no-op wearing a success banner, on the verb whose entire
    # job is to find holdings a name can no longer reach.
    #
    # AMBIGUITY REFUSES HERE even though the canonical resolver takes the
    # first hit. That resolver's callers NAME an agent; this one MOVES
    # CUSTODY, and handing one seat's holdings to another seat's successor is
    # not recoverable by re-running the verb.
    hits = seats_roster.seats_for_session_prefix(r, t)
    if len(hits) == 1:
        row = r.get(hits[0]) or {}
        # A PREFIX MUST RESOLVE TO THE FULL SESSION IT MATCHED, never to the
        # row's current binding — the operator was shown a prefix of a
        # HISTORICAL sid and a rename may have moved `session` on since. Search
        # the row's own history for the one this prefix names; if exactly one
        # matches, that is the exact incarnation. More than one is ambiguity
        # the prefix cannot resolve, and none means the prefix matched
        # something this row no longer remembers.
        known = [v for v in ([row.get("session")] + list(row.get("sessions")
                 or []) + list(row.get("cursor_sessions") or []))
                 if isinstance(v, str) and v and v.startswith(t)]
        matched = list(dict.fromkeys(known))
        if len(matched) != 1:
            return None, None, ("session prefix %r matches %d stored sessions "
                                "on @%s — give the full session id; a custody "
                                "move may not guess which incarnation"
                                % (t, len(matched), hits[0]))
        return hits[0], matched[0], ("session prefix %.8s -> seat %s "
                                     "(incarnation %.8s)"
                                     % (t, hits[0], matched[0]))
    if len(hits) > 1:
        return None, None, ("session prefix %r matches %d seats (%s) — name "
                            "the seat, or give more of the session id; a "
                            "custody move may not guess"
                            % (t, len(hits), ", ".join(sorted(hits))))
    # NO ROSTER ROW. Not a refusal: this is the orphan case.
    return t, None, ("no roster row answers to %r — treating it as an ORPHANED "
                     "holder key, which is what a rename leaves behind" % t)


def resolve_target(token):
    """(seat, how) for the seat receiving the holdings, or (None, refusal).

    THE ONE DOOR A ROLE PLUGS INTO. Everything downstream stores whatever this
    returns, so when "the codex-family reviewer" becomes addressable, this
    function learns it and the four ledgers do not change.

    EXACT NAME WINS OVER FAMILY, always. `codex` is both a family and a real
    seat name on this fleet; resolving the family first would silently retarget
    a row addressed to the seat.
    """
    from . import seats_roster
    t = str(token or "").strip()
    if not t:
        return None, "name the seat to receive the holdings (--to)"
    # SAME TRI-STATE READER AS THE SOURCE RESOLVER, and the same reason: the
    # fail-open reader cannot raise, so this guard never fired for a roster
    # that was merely malformed. A target seat is a truth consumer — deciding
    # that a name "can receive work" off a roster nobody could parse is the
    # false-negative twin of the source side's false weak resolution.
    r, roster_failed = seats_roster.roster_checked()
    if roster_failed:
        return None, ("the roster could not be read, so whether %r can "
                      "receive work is UNKNOWN" % t)
    if t in r:
        return t, "exact roster row"
    # FAMILY FALLBACK — the minimal form of role addressing. Two is an
    # ambiguity to REPORT, never a tie to break silently (the same rule
    # `seat_for_session` states for its own multi-match case).
    #
    # MEMBERSHIP IS THE ROSTER ROW, NOT THE SESSION FIELD, and that is a
    # correction from the fixture. A seat that JOINED has a row; whether that
    # row also carries a `session` depends on whether its own process stamped
    # one, and the roster guard legitimately refuses a cross-seat stamp
    # ("presence and delivery are per-PROCESS facts"). Requiring `session`
    # would therefore have made a correctly-joined seat unaddressable because
    # of who was asking. The session narrows a TIE and nothing more.
    fam = [s for s in sorted(r) if s == t or s.startswith(t + "-")]
    if not fam:
        return None, ("no seat %r is on the roster — `helm chat seats` lists "
                      "them; holdings may not be moved to a name nothing "
                      "answers to" % t)
    if len(fam) == 1:
        return fam[0], "family %r -> its one member %s" % (t, fam[0])
    live = [s for s in fam if (r[s] or {}).get("session")]
    if len(live) == 1:
        return live[0], ("family %r has %d members; %s is the only one with a "
                         "live session" % (t, len(fam), live[0]))
    return None, ("%r names a FAMILY with %d member(s) (%s), %d of them with a "
                  "live session — name one exactly; a reassignment may not "
                  "pick for you" % (t, len(fam), ", ".join(fam), len(live)))
    # MEASURED CONTRADICTION, the only kind that refuses a target here.
    return None, ("no seat %r is on the roster — `helm chat seats` lists them; "
                  "holdings may not be moved to a name nothing answers to" % t)


def _open_inbound(man, to):
    # HELD rows never reach `rebind` — `_move_dispatch` retains them first —
    # and neither does a row already addressed to the target, which it counts
    # as moved without asking rebind.
    return [r for r in (man.get("dispatch_in") or [])
            if str(r.get("status") or "") != "held"
            and not _already_addressed(r.get("recipient"), to)]


def _already_addressed(stamp, to):
    from . import seats
    return bool(stamp) and seats.recipient_matches(stamp, to)


def _already_held(lease, to):
    # STORED EXACTLY AS THE TARGET'S NAME, NOT CANONICAL, because that is how
    # leases are held. The lease mover matches the stored holder raw and stores
    # the target verbatim, and `_binding_ok` compares the stored holder raw on
    # release and extend. A lease stored exactly as the target's name is the
    # target's. A lease under another case spelling of the source was not
    # moved, and the target cannot release it, so it is still left behind.
    #
    # `holder` IS NOT THE STORED VALUE. It is claims_list's scrubbed and
    # stripped `holder_id`, so a lease stored as `worker ` or as `worker` plus
    # a format character reads `worker` here, and neither is the target's.
    # Exactness comes from the producer's `holder_exact`; an entry without it
    # is not exact, so it counts.
    return (lease.get("holder_exact") is True
            and bool(lease.get("holder"))
            and lease.get("holder") == str(to or "").strip())


def _lease_rung(to, reason):
    from . import seats_claims
    listed = seats_claims.claim_holder_listedness({"holder": to})
    if listed == "unmeasurable":
        return "unaddressable as a seat token (1-64 of [A-Za-z0-9._-])"
    if listed == "unlisted":
        return "on no roster row (`helm chat seats --all`)"
    return None


def _rebind_rung(to, reason):
    from . import dispatches
    # add()'s order, on add()'s own operand: the canonical recipient, then the
    # roster gate, then the usability gate. rebind passes no force to add.
    canon, err = dispatches._recipient_operand(to)
    if err:
        return err
    ok, why = dispatches._validate_recipient_rostered(canon, False)
    if not ok:
        return why
    ok, why, _warning = dispatches._validate_recipient_usable(canon, False)
    return None if ok else why


def _rebind_reason_rung(to, reason):
    from . import dispatches
    # rebind's own composition, then mark_cancel's own check on it. rebind
    # clips to 256, so only a character mark_cancel refuses can fail here —
    # and it fails AFTER add() has appended the child.
    canon, _err = dispatches._recipient_operand(to)
    cut = ("rebound to %s: %s" % (canon or to, str(reason or "").strip()))[:256]
    return dispatches._clean(cut, "cancel reason",
                             dispatches._CANCEL_REASON_CAP)[1]


def _rebind_actor_rung(to, reason):
    from . import dispatches
    # add() asks who is acting before it writes the child; the custody, task
    # and lease movers ask nobody, so a refusal here split every run.
    return dispatches._acting_author()[1]


def _custody_rung(to, reason):
    from . import dispatches
    if dispatches._TOKEN.fullmatch(str(to or "").strip()):
        return None
    return ("the custody writer stores the target verbatim and admits only "
            "1-64 of [A-Za-z0-9._-]")


def _custody_reason_rung(to, reason):
    from . import dispatches
    return dispatches._clean(reason, "custody reason", 256)[1]


def _owner_rung(to, reason):
    from . import tasks
    return tasks._owner_placeholder_error(to)


# WHAT EACH MOVER REFUSES, ABOUT WHICH OPERAND, AND WHEN THAT MOVER RUNS AT ALL.
# (door, subject, runs-for-this-manifest, rung). The lease mover runs on every
# apply — it asks before it looks for leases — so its rung is asked
# unconditionally; the others run once per row of their surface, so theirs are
# asked only when that surface holds a row the mover will act on. Asking a
# rung whose mover will not run would refuse a move that works today.
_TARGET, _REASON, _ACTOR = "target", "reason", "actor"
_MOVE_RUNGS = (
    ("dispatch rebind", _TARGET, lambda m, t: bool(_open_inbound(m, t)),
     _rebind_rung),
    ("dispatch rebind", _ACTOR, lambda m, t: bool(_open_inbound(m, t)),
     _rebind_actor_rung),
    ("dispatch rebind", _REASON, lambda m, t: bool(_open_inbound(m, t)),
     _rebind_reason_rung),
    ("dispatch custody", _TARGET, lambda m, t: bool(m.get("dispatch_out")),
     _custody_rung),
    ("dispatch custody", _REASON, lambda m, t: bool(m.get("dispatch_out")),
     _custody_reason_rung),
    ("task owner", _TARGET, lambda m, t: bool(m.get("tasks")), _owner_rung),
    ("lease holder", _TARGET, lambda m, t: True, _lease_rung),
)


def move_refusals(to, reason, man):
    """[(subject, text)] — every reason a mover would refuse this move, asked
    BEFORE any mover writes.

    `resolve_target` accepts an exact roster key, and a roster key is any
    string. The movers are stricter, and each refuses only when its own turn
    comes — the lease mover last. A target they cannot address therefore let
    the task move and then kept the lease: a seat split deterministically on a
    stable roster, reported as rc 1 after the fact (task/2455). The reason and
    the acting identity split a seat the same way: the custody writer refuses
    a reason over 256 characters or with a control character, rebind's cancel
    refuses the control character after it has appended the child, and add()
    refuses a process with no seat identity — each after an earlier mover
    wrote. One predicate, built from the movers' own rungs, asked once while
    nothing has moved.

    ASKED AT THE CENSUS, NOT IN `resolve_target`, because which movers run is
    a property of the manifest. `resolve_target`'s only callers resolve before
    any census exists, and a rung asked for a surface the source does not hold
    would refuse moves that succeed today.

    NOT A TRANSACTION. The roster, the usability and the identity readings can
    change between this ask and a mover's own; a mover's refusal then still
    reports as before. Row-shaped refusals are about the row, not the move,
    and are not asked here: a successor already carrying a dispatch, a ref
    outside the recorded repo, a row that is not OPEN, and add()'s posture
    guard on a stored note that names a seam strategy without its questions."""
    return [(subject, "%s refuses %s: %s"
             % (door, "@" + to if subject == _TARGET else
                "the reason" if subject == _REASON else
                "this process's seat identity", why))
            for door, subject, runs, rung in _MOVE_RUNGS
            if runs(man, to) for why in [rung(to, reason)] if why]


def _move_refusal_lines(refusals):
    return ["helm seat reassign: REFUSED — %s: %s"
            % ("the target cannot receive these holdings"
               if subject == _TARGET else "a mover would split this move",
               text) for subject, text in refusals]


def _default_reason(seat, state, why):
    """The reason recorded when the operator gave none, as one printable line
    every mover accepts. It embeds the disposition prose, which is unbounded
    and can carry a newline; the operator's own reason is never rewritten, it
    is asked of the movers and refused."""
    import unicodedata
    from . import dispatches
    text = "".join(c for c in " ".join(("seat reassign: %s is %s (%s)"
                                        % (seat, state, why)).split())
                   if unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp"))
    cap = dispatches._CANCEL_REASON_CAP
    return text if len(text) <= cap else text[:cap - 1] + "\u2026"


def source_disposition(seat):
    """(state, why) — is the source measurably still working?

    THE MUST-MISS THIS VERB OWES: a LIVE seat mid-turn is not reassignable
    without an explicit override, because moving work out from under a running
    agent is how you get two builders on one lane.

    Reuses `seat_resume_all.prove_reboot_dead`, the single-seat form of the
    fleet sweep's classifier, rather than re-deriving death. It already
    requires several independent instruments to AGREE before saying dead, and
    returns UNKNOWN with the census's own words otherwise — which is the
    distinction this verb needs and the one a hand-rolled pgrep would lose.
    """
    from . import harness, seat_resume_all
    try:
        ad = harness.detect()
        state, _handle, why = seat_resume_all.prove_reboot_dead(
            seat, ad, bind_boot=False)
    except Exception as exc:                 # noqa: BLE001
        state, why = None, "liveness could not be measured (%s)" % exc
    if state in (seat_resume_all.DEAD_PANE, seat_resume_all.PANE_GONE):
        return SOURCE_DEAD, "%s — %s" % (state, why)
    if state == seat_resume_all.LIVE:
        return SOURCE_LIVE, why
    # THE SECOND ARM EXISTS BECAUSE THE FIRST IS BLIND TO NATIVE SEATS, and
    # the blindness pointed the DANGEROUS way. `prove_reboot_dead` resolves a
    # seat through its FAMILY REGISTER (codex, gemini, grok, kimi and their -N
    # instances), so for a NATIVE claude seat it answers "register sits under
    # no known family" — UNKNOWN. UNKNOWN proceeds here by design, so the
    # must-miss protecting a LIVE source fired for proxy seats and FAILED OPEN
    # for every native one. Measured against the live fleet: this verb was
    # willing to move the holdings of a seat that was working at that moment.
    #
    # THE CONTRADICTING EVIDENCE WAS ALREADY IN MY OWN OUTPUT — the manifest
    # prints `liveness` on every lease it lists, and it read "live" two lines
    # under a disposition that said unknown. A surface asserting a state that
    # another field in the same system already answers, and disagrees with, is
    # the defect; the missing measurement is not.
    #
    # THE SECOND OPINION IS THE ROSTER SESSION AGAINST THE LIVE SID SET, and
    # the two obvious alternatives were both REJECTED BY MEASUREMENT rather
    # than by taste. `claim_holder_liveness(<name>)` answers "live" for ANY
    # string — it returns live for "zzz" and for "definitely-not-a-seat" —
    # so building a refusal on it would refuse every reassignment. The
    # per-lease `liveness` field reads "live" for all 16 current rows, which
    # is not proof it is broken but leaves it UNFALSIFIABLE on today's
    # population, and a discriminator that cannot be made to say "no" is not
    # one to gate on. `session in live_sids()` says True for any seat holding a
    # live session and False for a seat with no session and for a name that is
    # not a seat at all.
    #
    # IT IS CONSULTED ONLY TO REFUSE, NEVER TO AUTHORIZE. It cannot prove
    # death — a seat can be alive with no recorded session — so a negative
    # leaves the original UNKNOWN standing and the verb proceeds with the
    # unknown named. A positive is a MEASURED CONTRADICTION and refuses.
    try:
        from . import seats_roster, sessions
        from .seats_common import seat_row
        row = seat_row(seat)[0] or {}
        sid = row.get("session")
        alive = bool(sid) and sid in (sessions.live_sids() or ())
    except Exception:                        # noqa: BLE001 — a second opinion
        alive = False                        # that cannot be READ is not one
    if alive:
        return SOURCE_LIVE, ("the reboot classifier could not place %s (%s), "
                             "but its roster session is in the LIVE set — a "
                             "second surface measured it working" % (seat, why))
    return SOURCE_UNKNOWN, "%s — %s" % (state, why)


# Reading the dispatch store fails for BOTH halves at once — there is one read
# and it either produced rows or it did not. Naming only one surface would let
# the other report a confident zero off a read that never happened.
_DISPATCH_SURFACES = ("dispatch_in", "dispatch_out")
_TASK_SURFACES = ("tasks",)


def _unread(surfaces, detail):
    """ONE unread entry: WHICH surfaces are unknown, and WHY.

    The surfaces used to live only as a prefix inside the message text, which
    meant every reader that wanted them had to parse prose — and the one reader
    that tried compared a surface key against the whole string, so it silently
    never matched. A value that must be parsed to be used is a value waiting to
    be parsed wrong; this carries it.
    """
    return {"surfaces": [str(x) for x in surfaces], "detail": str(detail)}


def unread_text(entry):
    """The human line for one unread entry, from either shape.

    Rows written before `_unread` are plain strings and live in the audit
    ledger forever, so a reader that assumed the new shape would crash on real
    history. Old rows keep printing exactly as they always did.
    """
    if isinstance(entry, dict):
        return "%s: %s" % ("/".join(entry.get("surfaces") or ["?"]),
                           entry.get("detail", ""))
    return str(entry)


def unread_labels(entries):
    """Human labels for unread entries — NEVER an empty string.

    `unread_surfaces` deliberately yields nothing for a legacy string entry,
    because inventing a surface from prose is how a dead membership test got
    written in the first place. But the operator message was built from those
    surfaces alone, so a run whose only unread entry was legacy rendered as
    "could NOT read , so ..." — a sentence naming nothing, about the one
    condition the whole channel exists to report.

    An entry that can name its surfaces names them; one that cannot falls
    back to its own text, which is always non-empty.
    """
    out = []
    for e in (entries or []):
        names = sorted(unread_surfaces([e]))
        out.append("/".join(names) if names else unread_text(e))
    return out


def unread_surfaces(entries):
    """The SET of surfaces no census could read.

    A legacy string entry yields NOTHING here rather than a guess: parsing its
    prefix would produce "dispatch", which is not a surface, and inventing a
    surface from prose is how the dead membership test got written the first
    time. An old row still prints via `unread_text`; it just cannot claim to
    know which surface it darkened.
    """
    out = set()
    for e in (entries or []):
        if isinstance(e, dict):
            out.update(str(x) for x in (e.get("surfaces") or []))
    return out


def holdings(seat, root=None):
    """(manifest, unread) — EVERYTHING this seat holds, across all four
    surfaces, plus what could not be read.

    NOTHING LIKE THIS EXISTED. Searched before building it (by `holdings`, by
    `by_seat`/`for_seat`/`by_holder`, by the CLI verb table end to end, and by
    which modules import both `dispatches` and `tasks`): four partial readers
    exist and none crosses all four surfaces. `stalebot.collect` comes closest
    — it unions land-requests, overdue dispatch rows and stale tasks and
    buckets them by owner — but it is AGE-FILTERED (only rows already stalled)
    and has no lease leg at all. `idle_dispatch._claims_by_holder` is the only
    place dispatch rows and leases are joined per-seat, and it is read-only by
    design.

    `unread` RIDES BESIDE THE ROWS rather than being folded into them, the
    same contract `hooks.seat_homes` states: a surface that could not be read
    is not a surface with nothing on it, and no consumer may mistake one for
    the other.

    LAND REQUESTS ARE NOT A FIFTH SURFACE. They are projected from the very
    dispatch rows counted here (`landreq` reads `author` off `row["sender"]`),
    so moving the dispatch rows moves them. Counting them separately would
    double-report one obligation, and moving them separately would fight the
    projection.
    """
    man = {k: [] for k in SURFACES}
    unread = []

    from . import seats_claims, seats as _seats
    try:
        # A LIST OF ROWS, each already carrying `resource` — measured, after a
        # first cut assumed a {resource: row} mapping and the dogfood printed
        # "'list' object has no attribute 'items'". The manifest reported that
        # as UNREAD rather than as zero leases, which is the one property this
        # function was built to have, so the wrong guess cost a line of output
        # instead of a false all-clear.
        # READ THE IDENTITY FIELD, NOT THE LAYOUT ONE. `holder` is
        # `_clip(holder_id, 40)` — a TRUNCATION for display — so comparing a
        # seat against it silently misses every holder whose id runs past 40
        # characters, and the manifest then reports that lease as ABSENT rather
        # than unread: the false all-clear this function exists to prevent.
        #
        # The store already solved this and says so at the write: it publishes
        # `holder_id`, scrubbed but NEVER clipped, because "laundering strips
        # control bytes and is a safety property, truncation is a layout one,
        # and only the first belongs in an identity". An earlier fix clipped the
        # QUERY to match the clipped value, which would have papered over a
        # distinction the substrate had already drawn correctly.
        # gc=False: A CENSUS MAY NOT MUTATE WHAT IT IS MEASURING. claims_list
        # persists an expiry sweep when any row has aged out, so reading the
        # claims surface to decide what to move would itself write — the plan
        # then rests on a state the plan's own census created.
        # THE STRICT QUESTION FIRST. The tolerant table below cannot see a
        # malformed claim — the sweep erased it — so a census that only walked
        # it reported a clean partial-free reading over a file the MOVE would
        # refuse. Ask the mover's own reader here, where a refusal costs
        # nothing, instead of downstream where it costs a split seat
        # (task/2455).
        unavailable = seats_claims.claims_unavailable()
        if unavailable:
            unread.append(_unread(["leases"], str(unavailable)))
        malformed = 0
        for row in ([] if unavailable
                    else seats_claims.claims_list(gc=False) or []):
            if not isinstance(row, dict):
                # A ROW THAT CANNOT BE READ IS NOT A ROW THAT ISN'T THERE.
                # `continue` made a malformed claim indistinguishable from an
                # absent one, and absent is exactly the answer that lets a
                # reassignment leave a live lease behind. Counted, then
                # surfaced below: the census reports what it could not read.
                malformed += 1
                continue
            # `holder` is the fallback only for rows written before holder_id.
            holder = row.get("holder_id") or row.get("holder")
            if not _seats.recipient_matches(holder, seat):
                continue
            # THE HOLDER RIDES WITH THE ROW, as the recipient and custodian do
            # on the dispatch legs: the final census asks whose lease it is,
            # and the canonical match above erases the spelling. `holder` is
            # the scrubbed identity, so the producer's `holder_exact` rides
            # beside it to say whether that identity is what is stored.
            man["leases"].append({"resource": row.get("resource"),
                                  "holder": holder,
                                  "holder_exact":
                                      row.get("holder_exact") is True,
                                  "fence": row.get("fence"),
                                  "remaining": row.get("remaining"),
                                  "liveness": row.get("liveness")})
        if malformed:
            unread.append(_unread(["leases"],
                                  "%d claim row(s) were not readable as rows, "
                                  "so any lease they name is UNKNOWN rather "
                                  "than absent" % malformed))
    except Exception as exc:                 # noqa: BLE001
        unread.append(_unread(["leases"], str(exc)))

    from . import dispatches, seats as _seats
    try:
        # TWO VALUES, not three — measured. `_snapshot` returns a triple
        # internally; the public `snapshot` does not.
        snap, unavailable = dispatches.snapshot()
        if unavailable:
            unread.append(_unread(_DISPATCH_SURFACES, str(unavailable)))
        else:
            bad_rows = 0
            for rid, row in (snap or {}).items():
                if not isinstance(row, dict):
                    bad_rows += 1        # same law as the claims loop above
                    continue
                if row.get("status") not in ("open", "held"):
                    continue
                entry = {"id": rid, "lane": row.get("lane"),
                         "tip": row.get("tip"), "kind": row.get("kind"),
                         "status": row.get("status")}
                # THE CANONICAL COMPARATOR, not `==`. Every other obligation
                # reader in this repo routes a stamp-to-seat compare through
                # `seats.recipient_matches`, and a second, stricter door here
                # drifts from the one that STAMPS the field.
                inbound = _seats.recipient_matches(row.get("recipient"), seat)
                if inbound:
                    man["dispatch_in"].append(dict(
                        entry, recipient=row.get("recipient")))
                # THE ISSUER HALF IS A HOLDING TOO. A rename orphaned a seat's
                # own ISSUED rows on 2026-08-27 — `--mine` printed nothing
                # while they still owed the delivery leg — and that half is
                # invisible to every recipient-shaped instrument.
                # CUSTODY, NOT AUTHORSHIP, and the canonical comparator. A row
                # whose delivery leg was already transferred is held by its
                # CUSTODIAN; reading raw `sender` here would hand this seat
                # rows it no longer owes and miss the ones it now does.
                # A SELF-ADDRESSED ROW IS ONE HOLDING, NOT TWO. When the seat
                # is both custodian and recipient the row matched BOTH legs,
                # and the move processes inbound first: `rebind` mints a child
                # and CANCELS this row, so the outbound leg then called
                # `mark_custody` on a row that is already superseded — a
                # guaranteed refusal reported as a failure, on a holding that
                # had in fact just moved. Rebinding the recipient already
                # carries the whole obligation to the target, so the custody
                # leg has nothing left to move.
                if inbound:
                    continue
                custodian = dispatches.custodian_of(row)
                if _seats.recipient_matches(custodian, seat):
                    man["dispatch_out"].append(dict(entry, role="sender",
                                                    custodian=custodian))
            if bad_rows:
                # COUNTING IS NOT REPORTING. A tally with no reader is the same
                # false all-clear as the `continue` it replaced, just with a
                # variable to point at.
                unread.append(_unread(_DISPATCH_SURFACES,
                                      "%d row(s) were not readable as rows, so "
                                      "any obligation they carry is UNKNOWN "
                                      "rather than absent" % bad_rows))
    except Exception as exc:                 # noqa: BLE001
        unread.append(_unread(_DISPATCH_SURFACES, str(exc)))

    from . import tasks
    try:
        # A MAPPING id -> row, NOT a list. An earlier cut iterated it directly,
        # which yields the KEYS — strings — so `isinstance(row, dict)` was
        # False for all 1710 rows and the leg returned ZERO with no exception.
        # A FALSE ZERO, not an unread: the very failure this manifest's
        # `unread` channel exists to prevent, arriving through the one door
        # that channel cannot see, because nothing raised.
        #
        # SO THE SHAPE IS ASSERTED RATHER THAN ASSUMED. If the ledger holds
        # rows and none of them is a dict, that is a reader that no longer fits
        # its store, and it is reported as UNREAD. A census whose zero cannot
        # be distinguished from a broken reader is not a census.
        # SNAPSHOT, NOT rows(), AND THE RULE IS WRITTEN DOWN ONE MODULE
        # OVER. `ownership_census` states it in its own source: "SNAPSHOT
        # READERS, NOT rows(). `dispatches.rows()` and `tasks.rows()` discard
        # the unavailable channel, so an unreadable ledger renders as a
        # measured-looking zero backlog and a clean exit code." The dispatch
        # leg forty lines above already obeys it. This one called `rows()`,
        # which is `snapshot(path)[0]` — the tuple's second element, the whole
        # unknown/empty distinction, dropped at the subscript. A seat
        # reassigned over an unreadable task ledger reported NO TASKS TO MOVE
        # and moved none, with nothing in `unread` and rc 0.
        #
        # STRICT, because this leg MOVES ROWS. `tasks.snapshot`'s own docstring
        # draws the line: "Legs that file, refuse, or classify on the answer
        # pass strict; read-only surfaces keep the tolerant default." The
        # tolerant read SKIPS a malformed line to keep a listing alive, which
        # is right for a projection and a lie for a custody transfer — a row
        # skipped here is a holding left behind on a dead seat.
        # MEASURED before adopting it, against the live 2432-row ledger:
        # tolerant and strict return the same 2432 rows and the same None, so
        # the stricter read refuses nothing that works today.
        raw, unavailable = tasks.snapshot(strict=True)
        if unavailable:
            unread.append(_unread(_TASK_SURFACES, str(unavailable)))
            raw = {}
        raw = raw or {}
        vals = list(raw.values()) if isinstance(raw, dict) else list(raw)
        shaped = [r for r in vals if isinstance(r, dict)]
        if vals and not shaped:
            unread.append(_unread(
                _TASK_SURFACES,
                "the ledger holds %d row(s) and none is a mapping — this "
                "reader no longer fits its store" % len(vals)))
            vals = []
        for row in shaped:
            if tasks.owner_of(row) != seat:
                continue
            if str(row.get("status") or "") in ("closed", "done"):
                continue
            man["tasks"].append({"id": row.get("id"),
                                 "title": (row.get("title") or "")[:80],
                                 "status": row.get("status")})
    except Exception as exc:                 # noqa: BLE001
        unread.append(_unread(["tasks"], str(exc)))

    return man, unread


def manifest_lines(seat, to, man, unread):
    """The full manifest, printed. The task's own bar: `Print the full moved
    manifest` — an operator who cannot see what moved cannot tell a partial
    run from a complete one."""
    out = ["helm seat reassign: %s -> %s" % (seat, to)]
    total = 0
    for key in SURFACES:
        rows = man.get(key) or []
        total += len(rows)
        out.append("  %-12s %d" % (key, len(rows)))
        for r in rows:
            out.append("      %s" % json.dumps(r, sort_keys=True))
    out.append("  TOTAL %d holding(s)" % total)
    for u in unread:
        # NEVER FOLDED INTO THE COUNT. An unreadable surface is not an empty
        # one, and the total above is a count of what was SEEN.
        out.append("  UNREAD %s — this surface was not counted above"
                   % unread_text(u))
    return out


AUDIT_EVENT_BUDGET = 48 * 1024        # under eventledger's 64 KiB hard cap


def _bounded_event(row):
    """An audit row small enough that the ledger will actually accept it.

    `eventledger` REFUSES any event over MAX_EVENT_BYTES (64 KiB), and this
    event embeds the full per-row moved/refused lists across four surfaces. So
    the bigger the reassignment, the likelier the audit row is REJECTED — and
    the audit matters most exactly when the move is largest. A 127-row rename
    orphan is a real measured population, not a hypothetical.

    Losing the row entirely to keep its detail is the wrong trade, so the
    DETAIL degrades and the ROW survives: counts are always kept, per-row
    entries are dropped surface by surface (largest first) until it fits, and
    what was dropped is stated in the row rather than silently missing.

    Measured with the LEDGER'S OWN ENCODER — ensure_ascii=False, the compact
    separators it writes — because measuring a different serialization than
    the one that gets written is the defect this repo just spent an evening
    removing from the body cap.
    """
    def _size(obj):
        return len(json.dumps(obj, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))

    if _size(row) <= AUDIT_EVENT_BUDGET:
        return row
    row = dict(row)
    dropped = []
    for bucket in ("refused", "moved"):        # refusals carry the long prose
        section = row.get(bucket)
        if not isinstance(section, dict):
            continue
        # A COPY, BECAUSE THE SECTION IS THE CALLER'S. `dict(row)` above and
        # `record`'s own copy are both shallow, so replacing a surface in
        # place rewrote the verb's own result, and the rename's carry line
        # then counted a dropped surface's summary keys as holdings
        # (task/2529).
        section = row[bucket] = dict(section)
        for surface in sorted(section, key=lambda k: -_size(section.get(k))):
            entries = section.get(surface)
            if not isinstance(entries, list) or not entries:
                continue
            section[surface] = {"count": len(entries), "detail": "DROPPED"}
            dropped.append("%s.%s (%d)" % (bucket, surface, len(entries)))
            if _size(row) <= AUDIT_EVENT_BUDGET:
                break
        if _size(row) <= AUDIT_EVENT_BUDGET:
            break
    if dropped:
        row["detail_dropped"] = (
            "per-row detail omitted to fit the ledger's event cap: %s"
            % ", ".join(dropped))
    return row


# THE WRITERS THAT MUST CONSULT THE FENCE BEFORE THIS VERB MAY EVER CLAIM
# COMPLETION. Mapped against the code rather than guessed: the only
# fence-like thing in helm is the seats_rename DM redirect, consumed by chat
# and receipts, and NO work-assignment fence exists at all.
#
#   tasks.add                    a task filed against the source mid-op
#   tasks.update (owner-changing) a task reassigned TO the source mid-op
#   dispatch send/rebind/custody  a row addressed to the source mid-op
#   claims                        a lease taken by the source mid-op
#
# UNTIL EVERY ONE OF THEM ROUTES OR REFUSES THROUGH THE FENCE, A CLEAN CENSUS
# PROVES NOTHING ABOUT CONCURRENT ARRIVALS — it proves only that the four
# surfaces looked empty across a few hundred milliseconds of sequential reads.
# So the verb is NARROWED: it does not claim completion at all. Every op it
# writes is PARTIAL and RESUMABLE, and the source stays fenced.
#
# FLIP THIS TO True IN THE COMMIT THAT WIRES THE LAST CONSUMER, not before,
# and not as a "we are basically there". It is one boolean precisely so that

def _stamp(event):
    """The ledger-row shape every append shares — extracted so the LOCKED
    intent write and `record` cannot drift apart. Two stamping paths would be
    two definitions of what a reassignment row IS, and the fence reads rows
    written by both."""
    row = dict(event)
    row.setdefault("v", 1)
    row.setdefault("event", "seat-reassign")
    row.setdefault("ts", pk.now_ts())
    row.setdefault("id", os.urandom(16).hex())
    return _bounded_event(row)


@contextlib.contextmanager
def source_lock(source_key, target):
    """An EPHEMERAL per-source lock, held across census + moves + final census.

    THIS IS THE WHOLE CONCURRENCY STORY, and it is deliberately not a ledger
    row. The previous design recorded an operation as durable rows — intent,
    completion, phase, op id — which meant a crash left a HALF-DONE OPERATION
    that something had to reason about: is it resumable, does it still fence,
    who may adopt it. Every one of those questions produced a defect.

    An OS lock has none of them because THE KERNEL RELEASES IT WHEN THE
    PROCESS DIES. There is no half-held lock, so there is no state to recover:
    a crashed run leaves the stores exactly as far as it got, and the next run
    re-censuses and continues from what is actually true. That is the entire
    difference between an operation you must finish and a query you can repeat.

    It carries the TARGET so a concurrent run toward a DIFFERENT target can be
    refused — split work is the one genuine conflict, as opposed to late work,
    which converges on its own.

    Yields (True, None) when held, (False, why) when somebody else holds it.
    """
    held, refusal = _take_source_lock(source_key, target)
    try:
        yield (refusal is None), refusal
    finally:
        for fh in held:
            try:
                fh.close()                   # closing drops the flock
            except OSError:
                pass


def _take_source_lock(source_key, target):
    """(the filehandles taken so far, refusal-or-None). NO YIELD LIVES IN HERE.

    THE RETURN IS A TUPLE BECAUSE THREE FILES ARE TAKEN, and every one of
    them must reach the caller's `finally` even when the second or third
    refuses — a refusal that returned only the last handle would leak the
    earlier ones for the life of the process.

    THE BUG THIS SHAPE EXISTS TO MAKE UNREPRESENTABLE.
    The acquisition used to sit in a `try/except OSError` whose try-block also
    contained the `yield`. In a contextmanager the yield IS the with-body, and
    an exception from the body is thrown back in AT that yield — so a disk
    error while MOVING the work was caught by the lock's own handler, which
    then yielded a second time and turned it into "generator didn't stop after
    throw()". The caller lost the real failure and got a lock message about a
    lock that had been taken perfectly well.

    Splitting acquisition out means the handler cannot reach the body: there is
    no yield inside it to re-enter. The body's exceptions now propagate as
    themselves, and the `finally` in the caller still drops the flock, because
    releasing is what a lock owes on EVERY exit and not only on the clean one.
    """
    import fcntl
    here = os.path.dirname(ledger_path())
    # THREE FILES, TWO LOCK MODES, AND THE MODES ARE THE WHOLE DESIGN.
    #
    # The filename IS the mutual-exclusion token, so a helm that spells this
    # source differently takes a different file and never contends: both runs
    # census, both move what they find, both return 0, and one source's
    # holdings end up split across two targets with nothing observable saying
    # so. The older spelling therefore has to be touched by both populations.
    #
    # But that spelling is LOSSY — it folds names differing only where it
    # substitutes — so taking it EXCLUSIVELY would refuse two genuinely
    # distinct seats, which is the availability defect this lock key exists to
    # end. SHARED solves both at once, because shared and exclusive are what
    # the two populations respectively need: a helm holding the old spelling
    # holds it EXCLUSIVELY, so it blocks this run's SHARED take and is blocked
    # by it; two runs of this vintage both take SHARED, pass each other, and
    # contend only where identity is exact — the keyed file below.
    # ONE RENDEZVOUS PER PREDECESSOR, TAKEN SHARED, PLUS ONE EXCLUSIVE
    # NAMESPACE OF OUR OWN — and the dot is what makes the last one provable.
    # NEITHER SLUG CAN EMIT A DOT, and that is the whole proof — it is NOT
    # an ASCII claim. Both keep a character only when `str.isalnum()` is true
    # of it or it is `-` or `_`, and `isalnum()` is true of letters and digits
    # in EVERY script, so a slug may well carry non-ASCII characters. What it
    # can never carry is `.`, which is neither alphanumeric in any script nor
    # one of the two kept punctuation marks, and is therefore always
    # substituted. So a seat name can render `reassign-<slug>` and can never
    # render `reassign.<slug>`. Sharing one prefix let a seat LITERALLY NAMED
    # `abc-<abc's own digest>` render the same filename as seat `abc`'s keyed
    # spelling, and one seat's exclusive take then blocked an unrelated seat's
    # shared take — the cross-seat contention this key exists to end, arriving
    # one namespace over.
    #
    # MOVING THE EXCLUSIVE TAKE IS NOT ENOUGH ON ITS OWN: an older helm that
    # locked the KEYED spelling exclusively would then never meet us. So both
    # older spellings are rendezvous points held SHARED, which excludes each
    # predecessor in both arrival orders while never refusing a sibling of
    # this vintage, whatever two valid names happen to render.
    rendezvous = [os.path.join(here, "reassign-%s.lock" % slug)
                  for slug in (_legacy_lock_slug(source_key),
                               _lock_slug(source_key))]
    path = os.path.join(here, "reassign.%s.lock" % _lock_slug(source_key))
    held = []
    fh = None
    try:
        # THE DIRECTORY MAY NOT EXIST YET, and that is the ordinary case rather
        # than an error: a fresh helm home, a test fixture, a first-ever run.
        # Without this the open raises, the except below reports NOT-HELD, and
        # the verb refuses every reassignment on a clean install while
        # reporting a concurrency conflict that never happened.
        try:
            os.makedirs(here, exist_ok=True)
        except OSError:
            pass                             # the open below reports it properly
        for rendez in rendezvous:
            fh = open(rendez, "a+", encoding="utf-8")
            held.append(fh)
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                # ONLY AN EXCLUSIVE HOLDER CAN BLOCK A SHARED TAKE, and
                # nothing of this vintage takes these files exclusively. The
                # content is read for the target because that is what the
                # exclusive holder wrote there; it is never written here,
                # since two shared holders writing one file would interleave.
                fh.seek(0)
                holder = (fh.read() or "").strip()
                toward = (" toward @%s" % holder) if holder else ""
                return tuple(held), (
                    "an older reassignment of this source holds %s "
                    "exclusively%s — let it finish; a re-run afterwards "
                    "continues from whatever is true then"
                    % (os.path.basename(rendez), toward))
        fh = open(path, "a+", encoding="utf-8")
        held.append(fh)
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = (fh.read() or "").strip()
            if holder and holder != str(target):
                return tuple(held), (
                    "another reassignment of this source is running toward "
                    "@%s; this run targets @%s and two different destinations "
                    "cannot both be right" % (holder, target))
            return tuple(held), (
                "another reassignment of this source is already running "
                "toward the same target — let it finish; a re-run afterwards "
                "continues from whatever is true then")
        fh.seek(0)
        fh.truncate()
        fh.write(str(target))
        fh.flush()
        return tuple(held), None
    except OSError as exc:                   # noqa: BLE001
        return tuple(held), ("the source lock could not be taken (%s), so a "
                             "concurrent reassignment cannot be ruled out"
                             % type(exc).__name__)


def _legacy_lock_slug(value):
    """The spelling an older helm gives this source, kept so both exclude.

    IT IS LOSSY AND IT IS NEVER AN IDENTITY HERE. It is a rendezvous point two
    populations can both name, taken SHARED by this one precisely because it
    folds distinct seats together: a shared take cannot refuse a sibling of
    this vintage, and cannot pass an older exclusive holder. See
    `_take_source_lock` for why those are the two facts that matter.
    """
    keep = "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in str(value or "")).strip("-")
    return keep[:64] or "unnamed"


def _lock_slug(value):
    """A filename-safe slug that SEPARATES the names the readable part folds.

    A LOSSY SLUG IS A FALSE MUTUAL EXCLUSION, and that only became reachable
    when the source lock started keying on the SEAT rather than the session.
    A session id is opaque hex and collides with nothing; a seat name is
    `[A-Za-z0-9._-]`, so the old rule — map every non-`[alnum-_]` byte to `-`,
    then truncate at 64 — sent `worker.a` and `worker-a` to ONE lock file, and
    two long names sharing a prefix to another. Distinct valid seats then
    excluded each other: a reassignment refused for a concurrency conflict
    that does not exist, naming a target it has nothing to do with.

    So the readable part is kept for a human reading `ls`, and a digest of the
    EXACT input is appended for the machine.

    THE CLAIM IS SEPARATION BY CONSTRUCTION, NOT INJECTIVITY. Two names the
    readable part folds together share a lock ONLY IF THEIR DIGESTS ALSO
    COLLIDE, because the digest is taken over the exact input — which is a
    weaker statement than "no longer share a lock" and is the one that holds.
    The admitted name space IS finite — 64 characters of a restricted
    alphabet — and it is still vastly larger than the 2**48
    suffixes this digest can produce, so pigeonhole applies and a collision is
    possible; nothing here rules one out and no incidence has been measured.
    Exact separation would need an identity-preserving encoding for admitted
    keys and a policy for the orphans that leaves behind.

    THE FILENAME IS THE TOKEN, so `_take_source_lock` holds the older spelling
    alongside this one — see there for why.
    """
    raw = str(value or "")
    keep = "".join(c if c.isalnum() or c in "-_" else "-" for c in raw).strip("-")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (keep[:48] or "unnamed", digest)


def record(event):
    """Append ONE event describing the whole reassignment.

    ONE EVENT, WHICH IS THE THING `rebind` CANNOT DO. A rebind is two appends
    under two separately-acquired locks — a fresh row, then a cancel — with a
    documented race between them that needed a recheck and a disown path to
    paper over. Calling it N times to move N rows would inherit N copies of
    that window. This records the whole move as one row so a reader can never
    see half a reassignment.
    """
    from . import eventledger
    row = dict(event)
    row.setdefault("v", 1)
    row.setdefault("event", "seat-reassign")
    row.setdefault("ts", pk.now_ts())
    # AN EVENT WITHOUT AN id IS DROPPED ON READ. `eventledger.checked_rows`
    # admits a row only when `isinstance(row, dict) and row.get("id")`, so
    # every reassignment audit row this function wrote was invisible to the
    # checked reader — the append SUCCEEDED, `record` returned True, and the
    # audit trail was empty. A write that reports success and cannot be read
    # back is the worst shape a ledger has: it satisfies its writer and lies
    # to its auditor. Minted the same way dispatch mints one.
    row.setdefault("id", os.urandom(16).hex())
    # ONE OP ID, SHARED, so a reassignment stays correlatable even if its
    # record ever has to be written in more than one piece.
    # NO DEFAULT OP, DELIBERATELY. Defaulting `op` to the row id made every
    # standalone row look like an operation whose completion never arrived —
    # so a pre-fence legacy intent, and any row written outside an apply,
    # would fence its incarnation FOREVER. The op is minted by `apply` and set
    # explicitly on the intent/completion PAIR; a row without one is not part
    # of an operation and must not be read as an open fence.
    row = _bounded_event(row)
    # A BOOL, NOT A PAIR — measured, after the composition arm crashed on
    # `rid, err = record(result)`. Every unit arm here passed because none of
    # them ran `--apply`; the seam test found it on its first run. That is the
    # argument for the seam test in one line.
    ok = eventledger.append(ledger_path(), row)
    return (True, None) if ok else (
        False, "the reassign ledger refused the append (lock unavailable or "
               "the row was rejected) — %s" % ledger_path())


def events():
    """Every recorded reassignment, plus why the ledger could not be read."""
    from . import eventledger
    return eventledger.checked_events(ledger_path())


def _move_dispatch(rows, to, reason, out):
    """Move OPEN/HELD dispatch rows by composing `dispatch rebind` per row.

    COMPOSED, NOT REIMPLEMENTED, and the reason is the survey: rebind's twelve
    refusals are almost all CORRECT and hard-won — it refuses to mint a sibling
    against work a live successor already carries, it refuses to change a row's
    repo, and it handles the known race where the source leaves CANCELLABLE
    between its two locks. Re-deriving those would mean re-earning them.

    WHAT WE OVERRIDE, AND ONLY THIS: the evidence gate. `_recipient_evidence`
    admits measured starvation or context exhaustion, and a DEAD seat is
    neither — the survey's central finding. So each row goes through with
    force plus the reassignment's own reason, which carries the disposition
    the caller measured. The override is narrow on purpose: every other
    refusal still fires, and any row rebind declines is reported rather than
    retried differently.
    """
    from . import dispatches
    moved, refused = [], []
    for row in rows:
        rid = row.get("id")
        # ALREADY THE TARGET'S. A source label that differs from the target
        # only in case matches the same canonical recipient, and the dispatch
        # writer stores that canonical form, so these rows already route to
        # the target. rebind refuses them as "already addressed"; counting
        # that as a refusal kept rc 1 on every re-run, and the verb never
        # converged. Nothing is written for them.
        if _already_addressed(row.get("recipient"), to):
            moved.append({"id": rid, "lane": row.get("lane"), "new": None,
                          "already": True})
            out.append("  already dispatch %s (%s) is addressed to @%s"
                       % (str(rid)[:12], row.get("lane"), to))
            continue
        # (result, err) — result is None on EVERY refusal, and the second
        # element is then the refusal text. Read out of the source rather than
        # assumed: eleven of its twelve returns are `None, "<sentence>"`.
        # A HELD ROW IS A HOLDING THAT REBIND CANNOT MOVE. The census counts
        # open AND held rows — correctly, the seat holds both — but `rebind`
        # refuses any row whose status is not `open`, so a held row arrived
        # here as a generic failure with rebind's wording, which reads like a
        # defect rather than like the one action the operator can take. Named
        # for what it is, with the remedy, and NOT counted as moved.
        if str(row.get("status") or "") == "held":
            why = ("HELD rows cannot be rebound (rebind moves OPEN rows only) "
                   "— release it first: helm dispatch release %s"
                   % str(rid)[:12])
            refused.append({"id": rid, "lane": row.get("lane"), "why": why,
                            "held": True})
            out.append("  RETAINED dispatch %s (%s) — %s"
                       % (str(rid)[:12], row.get("lane"), why))
            continue
        res, err = dispatches.rebind(rid, to, reason=reason, force=True)
        # BOTH HALVES, NOT THE HAPPY ONE. The comment above says ELEVEN of
        # rebind's twelve returns are `None, "<sentence>"` — which concedes
        # that one is not, yet success was keyed on the result alone anyway.
        # A truthy result carrying an error is a PARTIAL, and counting
        # it as moved is how a reassignment reports custody it never took.
        if res and not err:
            # THE WRITE'S ADVISORIES ARE THE CALLER'S TO SURFACE, and this
            # composer was dropping them. `rebind` attaches notes about THIS
            # WRITE — most importantly "authorship could not be preserved",
            # which fires when a pre-2026-07-28 row carries sender=None and
            # the acting seat therefore authors the child. dispatches.py says
            # it in its own words: that fact "is not durable row state, and
            # the caller is the only one who can act on it". We read only id,
            # lane and new, so a legacy row silently changed author under a
            # line that said `moved`. The operator could not have known.
            notes = [n for n in (res.get(dispatches._ADMISSION_NOTES) or ())
                     if n]
            entry = {"id": rid, "lane": row.get("lane"),
                     "new": (res.get("new") or {}).get("id")}
            if notes:
                entry["advisories"] = list(notes)
            moved.append(entry)
            out.append("  moved   dispatch %s (%s) -> %s"
                       % (str(rid)[:12], row.get("lane"),
                          str((res.get("new") or {}).get("id") or "?")[:12]))
            for note in notes:
                out.append("          NOTE %s" % note)
        else:
            why = err or "rebind returned no result and no reason"
            refused.append({"id": rid, "lane": row.get("lane"), "why": why,
                            "partial": bool(res and err)})
            out.append("  REFUSED dispatch %s (%s) — %s%s"
                       % (str(rid)[:12], row.get("lane"), why,
                          " [PARTIAL: a result came back WITH this error]"
                          if (res and err) else ""))
    return moved, refused


def _move_custody(rows, seat, to, reason, out, force=False, disposition=None):
    """Move the DELIVERY LEG of every row this seat SENT.

    ONE VERB MOVES EVERY HOLDING. The first cut of this only
    REPORTED these rows, on the reasoning that helm had no verb to transfer
    senderhood and that refusing to hijack a reviewer was the conservative
    answer. Half of that was right: `dispatches.rebind` moves the RECIPIENT and
    must never be pointed at a sent row. The other half traded a loud failure
    for a quiet one — a row left stranded under a green banner still strands
    the obligation, which is the whole thing the operator asked to fix.

    `dispatches.mark_custody` is the mirror rebind was missing: it moves who
    must CHASE the row and rewrites neither the recipient nor the sender, so
    authorship survives and the delivery leg follows a living seat.
    """
    from . import dispatches, takeover
    moved, refused = [], []
    for row in rows:
        rid = row.get("id")
        # Each writer gets a fresh capability, not a fresh measurement. A
        # single capability expires during a large batch; the original
        # disposition still bounds how long this command may mint from it.
        auth, err = takeover.mint_reassign_custody(
            seat, to, force=force, reason=reason, disposition=disposition)
        # THE COMPARE-AND-SWAP is `auth.source`, read inside `mark_custody`.
        # It used to be passed here as `expected=seat` — the same value the
        # auth was minted with, duplicated at the call site, and defaulting to
        # None so that omitting it silently disabled the swap. The manifest
        # measured this seat as the holder; a transfer that landed since must
        # refuse rather than overwrite a fresher one, and that is now a
        # property of the capability rather than of remembering an argument.
        # ALREADY THE TARGET'S is the WRITER'S answer, never this snapshot's.
        # A custodian that differs from the target only in case matches it
        # canonically, `mark_custody` writes nothing, and counting that as a
        # move told the rename it carried a row nobody wrote (task/2529). But
        # deciding it here from the census skipped the writer's locked
        # current-row, status and compare-and-swap checks, so a row a third
        # seat took in the meantime read as the target's (review, task/2529
        # r1). The writer reports a checked no-op in `state`.
        res, state = None, {}
        if auth is not None:
            res, err = dispatches.mark_custody(rid, auth, outcome=state)
        if res and not err and state.get("already"):
            moved.append({"id": rid, "lane": row.get("lane"),
                          "recipient": row.get("recipient"),
                          "custodian": to, "already": True})
            out.append("  already custody %s (%s) delivery leg is @%s"
                       % (str(rid)[:12], row.get("lane"), to))
        elif res and not err:
            moved.append({"id": rid, "lane": row.get("lane"),
                          "recipient": row.get("recipient"),
                          "custodian": to})
            out.append("  custody dispatch %s (%s -> %s) delivery leg now %s"
                       % (str(rid)[:12], row.get("lane"),
                          row.get("recipient"), to))
        else:
            why = err or "mark_custody returned no result and no reason"
            refused.append({"id": rid, "lane": row.get("lane"), "why": why})
            out.append("  REFUSED custody %s (%s) — %s"
                       % (str(rid)[:12], row.get("lane"), why))
    return moved, refused


def _move_leases(seat, to, out):
    """Move every live worktree lease, and PRINT THE NEW HOLDER'S TOKEN.

    `_binding_ok` compares `holder` RAW, so a rename orphans the old name's
    leases and `release_stale` cannot rescue them — it needs exactly "stale",
    and a renamed seat's session is usually still LIVE. That is the whole
    reason a rename leaves rooms fenced for their full remaining TTL with
    nobody able to release them.

    THE RAW LEASE TOKEN IS PART OF THE HANDOVER, NOT A DETAIL. `claims_list`
    deliberately never publishes it — holders read their own through
    `own_leases` — so a seat that RECEIVES a lease this way has no other way
    to learn the nonce it now needs to release or extend. Moving the lease and
    withholding the token would hand somebody a room they cannot give back,
    which is the same shape as moving a row whose brief nobody can read.
    """
    from . import seats_claims
    ok, msg, manifest = seats_claims.rebind_claim_holder(seat, to)
    out.append("  %s leases: %s" % ("moved  " if ok else "REFUSED", msg))
    for row in (manifest or []):
        out.append("      %s  lease=%s fence=%s remaining=%ss (was session %s)"
                   % (row.get("shown") or row.get("resource"),
                      row.get("lease"), row.get("fence"),
                      row.get("remaining"), row.get("session_was")))
    if manifest:
        out.append("      ^ @%s: those lease tokens are yours now — release "
                   "or extend needs them and no other surface publishes them"
                   % to)
    return (manifest or []), ([] if ok else [{"why": msg}])


def _move_tasks(rows, seat, to, reason, out, force=False, disposition=None):
    """Move task OWNERSHIP through a capability minted for THIS population.

    `tasks.update` refuses to change an incumbent owner — correctly, because an
    ownership move is an authorization event — and its ONLY authorization was
    takeover's BUILD-continuation proof, which is lane-bound AND requires the
    incumbent's pane be measured LIVE. That is the exact inverse of a dead
    seat, so a dead seat's task rows were unmovable by any verb.

    `takeover.mint_seat_reassign` is the sibling capability: same
    compare-and-swap discipline, same expiry clock, different rules, and it
    moves OWNER ONLY. It deliberately cannot set `status`, because a
    reassignment says who holds the row, never that work resumed.
    """
    from . import tasks, takeover
    moved, refused = [], []
    for entry in rows:
        tid = entry.get("id")
        prev = None
        try:
            raw, unavailable = tasks.snapshot(strict=True)
            if unavailable:
                # AN UNREADABLE LEDGER IS NOT A VANISHED ROW, and the branch
                # below cannot tell them apart from a None. Both refuse, so
                # the SAFETY was never in doubt — the REASON was, and a
                # custody transfer that reports "row vanished since the
                # manifest" over a ledger nobody could read sends its operator
                # to look for a race that did not happen.
                refused.append({"id": tid, "why": "task ledger unreadable: "
                                                  "%s" % unavailable})
                out.append("  REFUSED %s — task ledger unreadable (%s)"
                           % (tid, unavailable))
                continue
            prev = (raw or {}).get(tid) if isinstance(raw, dict) else None
        except Exception as exc:             # noqa: BLE001
            refused.append({"id": tid, "why": "could not re-read: %s" % exc})
            out.append("  REFUSED %s — could not re-read (%s)" % (tid, exc))
            continue
        if not isinstance(prev, dict):
            # RE-READ, NOT REUSED. The manifest was taken before any move; a
            # row that changed since is a compare-and-swap failure, not a row
            # to overwrite from a stale snapshot.
            refused.append({"id": tid, "why": "row vanished between manifest "
                                              "and move"})
            out.append("  REFUSED %s — row vanished since the manifest" % tid)
            continue
        # THE MINT MAY NOW REFUSE, and its refusal is the liveness guard —
        # so it is reported per-row rather than raised, exactly like every
        # other compare-and-swap failure above.
        auth, mint_err = takeover.mint_seat_reassign(
            tid, prev, seat, to,
            {"kind": "seat-reassign", "from": seat, "to": to,
             "reason": reason}, force=force, disposition=disposition)
        if auth is None:
            refused.append({"id": tid, "why": mint_err})
            out.append("  REFUSED %s — %s" % (tid, mint_err))
            continue
        row, err = tasks.update(tid, takeover_auth=auth, owner=to)
        if err:
            refused.append({"id": tid, "why": err})
            out.append("  REFUSED %s — %s" % (tid, err))
        else:
            moved.append({"id": tid, "owner": (row or {}).get("owner")})
            out.append("  moved   %s -> %s" % (tid, to))
    return moved, refused


def reassign(source_token, to_token, reason=None, force=False, apply=False,
             outcome=None):
    """(rc, lines) — the one verb.

    DRY-RUN BY DEFAULT, like every other verb in helm that writes across
    ledgers. The manifest prints either way; `--apply` is what moves bytes.
    An operator who cannot see what WOULD move cannot tell a safe run from a
    surprising one, and this verb's population is exactly the moments nobody
    is confident about the fleet's state.

    `outcome`, when a dict is given, receives the same result the apply
    records on its audit event (moved, refused, remaining per surface) once
    the movers have run. A caller that reports a count reads it there: the
    census counts what the source was listed as holding, and only the movers
    know what moved. A run refused before any mover leaves it empty.
    """
    lines = []
    seat, session, how = resolve_source(source_token)
    if not seat:
        return 1, ["helm seat reassign: " + how]
    to, thow = resolve_target(to_token)
    if not to:
        return 1, ["helm seat reassign: " + thow]
    if to == seat:
        return 1, ["helm seat reassign: %s already holds these — naming the "
                   "same seat on both sides moves nothing" % to]
    lines.append("helm seat reassign: source %s (%s)" % (seat, how))
    lines.append("helm seat reassign: target %s (%s)" % (to, thow))

    state, why = source_disposition(seat)
    lines.append("helm seat reassign: source is %s — %s" % (state, why))
    if state == SOURCE_LIVE and not force:
        # THE MUST-MISS, and the only measured contradiction on the source
        # side. Everything else here refuses on absence, which this verb may
        # not do: it exists for the morning after a reboot, when absence is
        # the normal state of the evidence.
        return 1, lines + [
            "helm seat reassign: REFUSED — %s is measurably LIVE (%s). Moving "
            "work out from under a running agent is how two builders end up "
            "on one lane. Override with --force --reason '<why>' if you are "
            "the judgment seat making that call." % (seat, why)]
    if force and not reason:
        return 1, lines + ["helm seat reassign: --force needs --reason (it is "
                           "recorded on the event)"]

    if not apply:
        man, unread = holdings(seat)
        lines += manifest_lines(seat, to, man, unread)
        refusals = move_refusals(to, reason or _default_reason(seat, state,
                                                               why), man)
        lines += _move_refusal_lines(refusals)
        lines.append("helm seat reassign: DRY RUN — nothing moved. Re-run "
                     "with --apply.")
        # AN UNREADABLE SURFACE IS A NON-ZERO DRY RUN. A manifest that could
        # not read one of the four ledgers is not a preview of the whole move,
        # and rc 0 is the only part of this output a script reads. The same
        # holds for a target the apply would refuse.
        return (1 if unread or refusals else 0), lines

    # THE APPLY PATH RUNS UNDER THE SOURCE LOCK, and the census is taken INSIDE
    # it. A manifest read before the lock is a manifest another run could still
    # be mutating, so the moves would be planned against a snapshot the lock
    # was supposed to make stable. Census, moves and the final census are one
    # locked span; a dry run stays lock-free because it is read-only and
    # refusing a PREVIEW because a move is running would be unhelpful.
    # THE LOCK IS KEYED BY WHAT IT PROTECTS, AND THAT IS THE SEAT. Keying it
    # on the session made one seat's holdings reachable through as many
    # distinct locks as that seat has remembered sessions: `resolve_source`
    # hands back the EXACT historical sid the operator named — correctly, the
    # audit row must name the incarnation they named — so two runs spelling
    # one stable roster seat by two of its own sids took two different lock
    # files and did not exclude each other, while `holdings(seat)` handed both
    # of them the SAME rows. The same sid WAS excluded, which is why the lock
    # looked like it worked (task/2454).
    #
    # UNIFIED HERE AND NOWHERE ELSE. The seat is the subject of the move:
    # holdings are stored against a LABEL, as `_apply_locked` states, so the
    # label is the resource two operators can collide on. `session` keeps
    # travelling unchanged into the audit row, the fence and the identity
    # kind — unifying at the lock key must not unify at RESOLUTION, because
    # returning the seat's current binding instead of the named incarnation is
    # a defect this module already fixed once.
    lock_key = seat
    with source_lock(lock_key, to) as (held, lock_why):
        if not held:
            lines.append("helm seat reassign: REFUSED — %s" % lock_why)
            return 1, lines
        return _apply_locked(seat, session, to, state, why, reason, force,
                             lines, source_token, to_token, outcome)


def _apply_locked(seat, session, to, state, why, reason, force, lines,
                  source_token, to_token, outcome=None):
    """The mutating half, ONLY ever called with the source lock held.

    Split out so the lock cannot be forgotten: there is no path into the movers
    that does not pass through the `with` above. The previous cut had the lock
    defined and never entered — dead code that read as a guarantee — which is
    exactly the failure this shape removes.
    """
    # IDENTITY IS BOUND AT THE MOMENT OF ACTION, NOT AT PARSE TIME.
    #
    # `resolve_source` and `resolve_target` ran in the caller, before the
    # lock existed. A rename landing in that window leaves this run
    # holding a lock keyed on a name that has since moved, about to write to
    # seats that are no longer the ones the operator named — and every write
    # below would succeed, because each one is individually valid.
    #
    # MOVING THE RESOLVE INSIDE THE LOCK WOULD ONLY SHRINK THE WINDOW, not
    # close it: the lock key is DERIVED from the resolution, so the lock cannot
    # protect the thing it is derived from. What closes it is the same
    # compare-and-swap the takeover mints already use — re-read under the lock
    # and refuse if the answer changed. A refusal here costs a re-run; the
    # alternative costs a seat's holdings moved to the wrong owner.
    re_seat, re_session, re_how = resolve_source(source_token)
    re_to, re_thow = resolve_target(to_token)
    drift = []
    if not re_seat:
        drift.append("the source no longer resolves (%s)" % re_how)
    elif (re_seat, str(re_session or "")) != (seat, str(session or "")):
        drift.append("the source now resolves to @%s/%s, not @%s/%s"
                     % (re_seat, re_session or "-", seat, session or "-"))
    if not re_to:
        drift.append("the target no longer resolves (%s)" % re_thow)
    elif re_to != to:
        drift.append("the target now resolves to @%s, not @%s" % (re_to, to))
    if drift:
        lines.append("helm seat reassign: REFUSED — identity moved between "
                     "reading it and taking the lock: %s. Nothing was moved. "
                     "Re-run; it will act on whatever is true then."
                     % "; ".join(drift))
        return 1, lines

    man, unread = holdings(seat)
    lines += manifest_lines(seat, to, man, unread)

    # AN UNREADABLE SURFACE REFUSES THE APPLY, BEFORE ANYTHING MOVES. The
    # dry-run already returned non-zero for this, and apply did NOT: it moved
    # every surface it could see and then reported rc 1 at the END — so a run
    # blind to one of the four ledgers left the seat SPLIT across two owners,
    # with the moved half done and the unread half neither moved nor listed.
    # A partial reassignment is the one outcome this verb exists to prevent;
    # the whole point of the manifest is that a seat's holdings move together.
    # `--force` does not override this: force is an override of a LIVENESS
    # veto, which is a judgment about a seat, and no judgment makes an
    # unreadable ledger readable.
    if unread:
        lines.append("helm seat reassign: REFUSED — %d surface(s) could not be "
                     "read, so this move would be PARTIAL: %s"
                     % (len(unread),
                        "; ".join(unread_text(u) for u in unread[:3])))
        lines.append("  nothing was moved. Fix the unreadable ledger and "
                     "re-run; a seat split across two owners is worse than a "
                     "seat that has not moved.")
        return 1, lines
    # A TARGET, REASON OR IDENTITY A MOVER WILL REFUSE REFUSES THE APPLY,
    # HERE, for the same reason: each mover refuses only at its own turn,
    # after the earlier ones have written. `--force` overrides a liveness
    # veto, never this.
    why_moved = reason or _default_reason(seat, state, why)
    refusals = move_refusals(to, why_moved, man)
    if refusals:
        lines += _move_refusal_lines(refusals)
        lines.append("  nothing was moved. Every mover must accept the target, "
                     "the reason and this process's identity before any of "
                     "them runs; a seat split across two owners is worse than "
                     "a seat that has not moved.")
        return 1, lines

    result = {"source": seat, "source_session": session, "target": to,
              "source_state": state, "source_why": why, "reason": why_moved,
              "forced": bool(force), "unread": unread, "moved": {},
              "refused": {}}
    # ONE OP CORRELATES THE PAIR. `record` mints a fresh ledger-row id per
    # append, so intent and completion were unrelatable: an auditor finding an
    # orphan intent could not tell WHICH completion was missing once two
    # reassignments interleaved. The op is a SEPARATE field from the row id,
    # deliberately — `eventledger.checked_rows` requires a unique id per row,
    # so reusing it would collide the two rows of one operation.
    # IDENTITY IS THE QUERY GUARD, NOT A TRANSACTION KEY. Holdings are stored
    # against a LABEL, so the move is label-keyed no matter what we would
    # prefer. What identity buys is a REFUSAL: orphan-label mode is allowed
    # only while no current roster incarnation owns that label, so a REUSED
    # label refuses rather than inheriting its predecessor's work.
    incarnation = str(session or "").strip()
    identity_kind = "incarnation" if incarnation else "label"
    result["identity_kind"] = identity_kind
    if incarnation:
        result["source_incarnation"] = incarnation

    # THE DISPOSITION IS MINTED HERE — INSIDE THE LOCK, BEFORE ANY MOVER.
    #
    # It used to be minted BETWEEN the inbound dispatch move and the custody
    # move, which meant the liveness reading that authorizes the operation was
    # taken AFTER part of the operation had already happened. A seat that
    # revived in that window produced an UNFORCED SPLIT: dispatch rows already
    # relocated, then a fresh disposition refusing the rest. The verb's whole
    # contract is that a seat's holdings move together.
    #
    # And the pre-lock liveness reading further up is a COURTESY REFUSAL for
    # the obvious case — it is measured before the lock and can go stale in
    # exactly the window the lock exists to close, so it may not be what
    # authorizes anything. This one, under the lock and ahead of every write,
    # is the authorizing measurement.
    from . import takeover as _tk
    disp, disp_err = _tk.mint_source_disposition(seat)
    if disp is None:
        lines.append("  REFUSED — %s" % disp_err)
        result["refused"]["disposition"] = [{"why": disp_err}]
        return 1, lines

    # INBOUND ONLY. `dispatches.rebind` moves a row's RECIPIENT, which is
    # exactly right for a row addressed TO the seat we are emptying and exactly
    # WRONG for one the seat SENT: rebinding an outbound row hands somebody
    # else's review obligation to the successor while the orphaned sender —
    # the thing this verb exists to fix — stays orphaned. The tell is that the
    # manifest already stamps every outbound entry `role="sender"`: the fact
    # that distinguishes the two directions is sitting in the rows, so they
    # must never be concatenated into one list.
    moved, refused = _move_dispatch(man["dispatch_in"] or [], to,
                                    why_moved, lines)
    result["moved"]["dispatch"] = moved
    result["refused"]["dispatch"] = refused
    # ONE MEASUREMENT, MINTED ONCE, CARRIED. Every custody write below rides
    # the same disposition rather than re-deriving liveness per row — doors that each
    # measure can disagree about a seat that dies between two of them.
    # MEASURE ONCE FOR THE WHOLE COMMAND. Both mints used to derive liveness
    # independently — the custody mint from caller-supplied values, the task
    # mint by re-measuring per row — so one operator command could split
    # across a mid-loop transition and move some holdings under one
    # disposition and the rest under another.
    # AN INTENT ROW BEFORE ANYTHING MUTATES, because the completion row is
    # written AFTER the moves and therefore cannot describe a run that dies
    # during them. Without this, a failure between the first mutation and the
    # final append leaves holdings moved and the ledger silent — the auditor
    # sees no reassignment at all, which reads as "it never ran" rather than
    # "it ran and we do not know how far it got".
    #
    # THE INTENT IS NOT A PROMISE THAT THE MOVE HAPPENED. It records that this
    # seat, at this moment, began moving @source's holdings to @target. A
    # reader that finds an intent with no matching completion knows exactly
    # which pair to re-census, which is the only question worth asking after a
    # partial run. If the intent itself cannot be written we do NOT proceed:
    # mutating custody with no possibility of a record is the state this whole
    # function exists to make impossible.
    omoved, orefused = _move_custody(
        man["dispatch_out"] or [], seat, to, why_moved, lines,
        force=bool(force), disposition=disp)
    result["moved"]["dispatch_out"] = omoved
    result["refused"]["dispatch_out"] = orefused
    tmoved, trefused = _move_tasks(man["tasks"] or [], seat, to, why_moved,
                                   lines, force=bool(force), disposition=disp)
    result["moved"]["tasks"] = tmoved
    result["refused"]["tasks"] = trefused
    lmoved, lrefused = _move_leases(seat, to, lines)
    result["moved"]["leases"] = lmoved
    result["refused"]["leases"] = lrefused

    # THE FINAL CENSUS — RE-MEASURE THE SOURCE, DO NOT INFER IT EMPTY.
    #
    # Everything above reports what THIS COMMAND moved. That is a statement
    # about the manifest read at the start, and the source seat does not stop
    # existing while the moves run: a row dispatched to it, a lease it took, or
    # a task assigned to it DURING the pass is invisible to the opening
    # manifest and therefore to every count above. The command would then
    # report a complete reassignment while holdings sat on the source.
    #
    # It also gives the audit-write failure below a recovery surface. If the
    # event cannot be recorded, the moves have still happened and the ledger
    # cannot say what — but this census is a SECOND, INDEPENDENT reading of the
    # same question ("what does the source still hold"), taken after the fact,
    # so an operator has something to act on other than the moves' own claim.
    #
    # A CENSUS THAT CANNOT BE READ IS NOT AN EMPTY ONE. `holdings` already
    # separates unread surfaces from empty ones, and both outcomes here are
    # loud: leftovers refuse, and an unreadable re-read refuses.
    after, after_unread = holdings(seat)
    # A ROW WHOSE LEG ALREADY BELONGS TO THE TARGET IS NOT LEFT BEHIND.
    # The census matches the source label canonically, so a case-variant
    # label still lists rows the target now holds; counting them made every
    # run exit 1 on holdings that had nowhere left to go. Each surface asks
    # with its own mover's comparison: dispatch legs route canonically, and
    # a lease is the target's only when it is stored exactly as the target's
    # name (task/2524).
    after = dict(after or {})
    for key, held in (
            ("dispatch_in",
             lambda r, t: _already_addressed(r.get("recipient"), t)),
            ("dispatch_out",
             lambda r, t: _already_addressed(r.get("custodian"), t)),
            ("leases", _already_held)):
        after[key] = [r for r in (after.get(key) or []) if not held(r, to)]
    left = {k: v for k, v in after.items() if v}
    # TRI-STATE, ONE ENTRY PER SURFACE. The old shape reported a COUNT per
    # surface built from the readable manifest alone, so an unreadable surface
    # contributed 0 and read as clean — laundering "could not look" into
    # "there is nothing there". `holdings` returns the (manifest, unread) pair
    # precisely so a caller cannot confuse them, and this caller did. No
    # surface may now report a number it did not read.
    # THE MEMBERSHIP TEST USED TO BE UNSATISFIABLE. It asked whether a SURFACE
    # KEY was in a list of MESSAGE STRINGS ("leases: boom"), so it never
    # matched and every surface reported a number it had not read — the exact
    # false zero the tri-state was written to prevent, defeated by the shape of
    # its own input. The dispatch case could not have been rescued by parsing
    # the prefix either: entries said "dispatch" while the surfaces are
    # `dispatch_in` and `dispatch_out`. The surface is now DATA carried by the
    # entry, so there is nothing left to parse and nothing to drift.
    unknown = unread_surfaces(after_unread)
    result["remaining"] = {
        k: ("UNKNOWN" if k in unknown else len(left.get(k) or []))
        for k in SURFACES}
    # LEGACY-TOLERANT LIKE ITS TWO SIBLINGS, and it was not. `unread_text` and
    # `unread_surfaces` both accept a plain string, because rows written before
    # the structured shape live in the audit ledger forever — but `dict(u)`
    # RAISES on a string, so this one line would have crashed the record on
    # exactly the input the other two were built to survive. Wiring a new shape
    # into two of its three readers is how a change looks finished and is not.
    result["remaining_unread"] = [dict(u) if isinstance(u, dict)
                                  else _unread([], str(u))
                                  for u in (after_unread or [])]
    if left:
        lines.append("helm seat reassign: THE SOURCE STILL HOLDS %s after the "
                     "move (%s) — either a surface refused above, or work "
                     "arrived on @%s while this command ran. Re-run to move "
                     "it; nothing that already moved will move twice."
                     % (sum(len(v) for v in left.values()),
                        ", ".join("%s x%d" % (k, len(v))
                                  for k, v in sorted(left.items())), seat))
    if after_unread:
        lines.append("helm seat reassign: the final census could NOT read %s, "
                     "so whether @%s still holds anything there is UNKNOWN — "
                     "this is not a clean reassignment"
                     % (", ".join(unread_labels(after_unread)), seat))

    # ONE EVENT, WRITTEN AFTER THE MOVES AND RECORDING BOTH HALVES. A reader
    # of this ledger sees what moved AND what refused, because a manifest that
    # lists only successes reads as a complete reassignment.
    # COMPLETION IS EARNED, NOT ANNOUNCED. It closes the op only when the
    # census read every surface and found zero residuals. Anything else — a
    # leftover, an unreadable surface — leaves the op OPEN, which keeps the
    # source fenced and makes the state RESUMABLE rather than green. A verb
    # whose contract is "move this identity's holdings" may not exit 0 with
    # holdings behind; honest prose in the output does not make rc=0 true.
    # ONE AUDIT ROW, RECORDING WHAT THIS RUN DID. Not a completion — there is
    # no operation to complete. The verb reassigns what is CURRENTLY stale, and
    # the honest statement of a run is what it moved, what refused, and what is
    # still there afterwards. A later run answers the same question against
    # whatever is true then, which is why running it twice is harmless and why
    # nothing here has to be resumed.
    clean = not left and not after_unread
    recorded, err = record(result)
    # `left`, NOT `not clean`. A run can be unclean for two different reasons —
    # something REMAINS, or a surface could not be READ — and this sentence is
    # only true of the first. Gating it on `not clean` made an unreadable
    # census print "holdings REMAIN on @seat ()": an empty list, because the
    # counts are all zero, attached to a claim that contradicts the unread
    # line printed just above it. The unreadable case already has its own
    # sentence and does not need a false second one; rc is computed below from
    # both conditions independently, so narrowing this message changes what
    # the operator is told and not what a script reads.
    if left:
        lines.append("helm seat reassign: holdings REMAIN on @%s (%s). Nothing "
                     "is stuck — re-run and it will move what is stale then; "
                     "anything already moved is not moved twice."
                     % (seat, ", ".join(
                         "%s %s" % (k, v) for k, v in
                         sorted(result["remaining"].items()) if v)))
    if err:
        lines.append("helm seat reassign: the moves landed but the audit row "
                     "could NOT be recorded (%s), so this run is invisible to "
                     "the ledger. The moves themselves stand and a re-run "
                     "re-censuses from current state." % err)
    else:
        lines.append("helm seat reassign: recorded one event in %s"
                     % ledger_path())
    # rc IS THE ONLY PART A SCRIPT READS. Residuals, an unreadable surface, a
    # refusal or an unrecorded run all fail the command — but none of them
    # leave anything half-done, so the remedy is always the same: run it again.
    rc = 1 if (any(result["refused"].values()) or unread or err
               or left or after_unread) else 0
    if outcome is not None:
        outcome.update(result)
    if rc:
        lines.append("helm seat reassign: rc 1 — the refusals are listed "
                     "above; re-running is "
                     "always safe and always converges toward empty")
    return rc, lines


_USAGE = ("usage: helm seat reassign <seat-or-session> --to <seat> "
          "[--reason R] [--force] [--apply] [--json]")


def cmd_reassign(argv):
    import sys
    a = list(argv)
    # BARE `-h` IS A QUESTION, NOT A MALFORMED CALL. The dash test below runs
    # BEFORE guard_tail, so `helm seat reassign --help` was answered on stderr
    # with rc 2 — an existence probe told the caller the verb is broken rather
    # than telling it what the verb does. Only a dummy first argument reached
    # the real help. Help is answered on stdout, rc 0, like every other verb.
    if a and a[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    if not a or a[0].startswith("-"):
        print(_USAGE, file=sys.stderr)
        return 2
    src, a = a[0], a[1:]
    # THE SHARED TAIL CONTRACT, NOT A HAND-ROLLED ONE. The loop below already
    # closed-set refused an unknown token rc 2 before any work, so this is not
    # a new guarantee — but a hand-rolled equivalent is INVISIBLE to the
    # apply-reader census (it looks for `guard_tail`), and it reported a
    # valueless `--to` as "unknown arg '--to'" rather than as a flag missing
    # its value. Using the real door fixes the message, earns `-h` handling,
    # and lets the census see the guard instead of needing an exemption that
    # would have to be re-justified every time this verb grows a flag.
    from .cli import guard_tail
    rc = guard_tail("helm seat reassign", a,
                    flags=("--force", "--apply", "--json"),
                    valued=("--to", "--reason"), usage=_USAGE)
    if rc is not None:
        return rc
    to = reason = None
    force = apply_ = as_json = False
    while a:
        t = a.pop(0)
        if t == "--to" and a:
            to = a.pop(0)
        elif t == "--reason" and a:
            reason = a.pop(0)
        elif t == "--force":
            force = True
        elif t == "--apply":
            apply_ = True
        elif t == "--json":
            as_json = True
        else:
            print("helm seat reassign: unknown arg %r\n%s" % (t, _USAGE),
                  file=sys.stderr)
            return 2
    if not to:
        print("helm seat reassign: --to is required\n" + _USAGE,
              file=sys.stderr)
        return 2
    rc, lines = reassign(src, to, reason=reason, force=force, apply=apply_)
    if as_json:
        print(json.dumps({"rc": rc, "lines": lines}, indent=1, sort_keys=True))
    else:
        for l in lines:
            print(l)
    return rc
