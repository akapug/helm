#!/usr/bin/env python3
"""helm seats — the work offer, and the ladder that ranks every stop rung.

THE ACTUATOR AND THE SORTER LIVE TOGETHER BECAUSE THEY ARE ONE DECISION. The
work offer is the only rung that can CHANGE the world rather than describe it
— it can claim a lane on a seat's behalf — and _whisper_candidates is what
decides whether that rung, or any other, is the one thing a seat hears before
it idles. Separating them would put the most consequential action in one file
and the judgement about whether to take it in another.

SALIENCE IS THE SCARCE RESOURCE, NOT CORRECTNESS. Every rung here is
individually right; the ladder exists because a seat that hears six true
things hears none of them. _stop_whisper picks exactly one and latches it on
a FINGERPRINT of the state that produced it, so an unchanged world stays
quiet. A rung that re-fires on the same facts is how an advisory surface
teaches its reader to skip it — and a skipped advisory is worse than none,
because it was counted as delivered.

_solo_load_candidate is the odd one and deliberately so: every other rung is
about a THING (a gate, a row, a dirty tree, an offer), and this one is about
the SHAPE of the seat's own load — a wide queue held with no delegate. helm's
delegation machinery had only ever been read to EXCUSE a stop, never to shape
one, and that asymmetry is what let one seat hold a wide queue for hours with
zero subagent calls while three seats sat near-idle.
"""

import hashlib
import json
import os
import time

from . import chat, home, pk, projscope, record
from .seats_common import (UNVERIFIED, _canonical_recipient, _clip, _scrub,
                           _sweep, claims_path)
from .seats_identity import _delivery_pause, _git_project, safe_cwd
from .seats_roster import last_seen, roster_checked
from . import seats_delegation
from .seats_delegation import (_ACTIVITY_PREFIX, _AGENT_KEY_PREFIX,
                               _STOPPED_PREFIX)
from .seats_claims import claim
from .seats_report import presence_with_identity, unverified_seats
from .seats_stop_signals import (DIRTY_AT, PENDING_STALE_S, SOLO_LOAD_AT,
                                 STOP_WHISPER_CAP, STUCK_AT, _WHISPER_FIRED_CAP,
                                 _ask_candidate, _deletions_fp,
                                 _deletions_named, _dispatch_candidate,
                                 _edited_code, _gate_candidate, _rows_fp,
                                 _runner_latest, _staged_deletions,
                                 _stop_fp_path, _unbanked_candidate,
                                 _unverified_candidate, beacon_procs)

def _live_seats():
    """Casefolded seat names with a recent presence beat (fresh/quiet, not
    absent) — one roster read. This is the ownership truth for the offer
    filter: an absent recipient's work is stranded, a live recipient's is
    in-flight and off-limits.

    An UNVERIFIED row is NOT live: its beat may belong to another process, so
    treating it as an owner would strand real work behind a seat nobody can
    prove is listening (work-routing must never route to an unattributable
    dot — 2026-07-24).

    None WHEN THE ROSTER CANNOT BE READ, and that is the difference between
    this function and a poaching machine. roster() fails open to an empty
    dict for a corrupt or unreadable file exactly as it does for a missing
    one, so the loop below never runs and every seat comes back NOT live.
    Measured on the live fleet: ELEVEN live seats through the real roster,
    ZERO through a fail-open read. By this function's own sentence above —
    an absent recipient's work is stranded — that makes every seat's work
    offerable to whoever asks next because one file could not be parsed.

    The guards below are all sound and all sit BELOW a read that has already
    thrown the answer away; each correctly concludes nothing about a fleet it
    cannot see. So the tri-state is taken HERE, at the read that DECIDES, and
    the caller fails closed exactly as it already does for an unreadable
    claims file three lines above this call."""
    r, unreadable = roster_checked()
    if unreadable:
        return None
    unver = unverified_seats(r)
    live = set()
    for seat, row in r.items():
        if presence_with_identity(last_seen(seat, row),
                                  unver.get(seat)) not in ("absent", UNVERIFIED):
            live.add(str(seat).casefold())
            continue
        sessions = [s for s in ([row.get("session")] +
                                list(row.get("sessions") or [])) if s] or [None]
        if not any(_delivery_pause(seat, s) for s in dict.fromkeys(sessions)):
            continue
        # Suppressing .seen is presence honesty, not abandonment. A proven-live
        # armed waiter still owns its dispatch while credentials are walled; an
        # unprovable process census also fails closed against poaching. Only a
        # clean "no live waiter" result makes the work offerable as stranded.
        pids, trouble = beacon_procs(seat, strict=True)
        if pids or trouble:
            live.add(str(seat).casefold())
    return live
def _live_claims():
    """The live (unexpired) claim leases as a swept dict, or None when the
    claims file EXISTS but cannot be read/parsed — 'unsure', which every caller
    fails closed on. A MISSING file is not unsure: no file ⇒ no claims ⇒ {}
    (pk.read_json masks a corrupt file as {}, so this reads directly to tell the
    two apart).

    AND NAMING THE FILE IS PART OF READING IT — the same split the roster
    reader needed, found in this function by the reviewer who found it there.
    claims_path() resolves the chat dir, and under a relative HELM_HOME with
    the process cwd removed os.path.abspath raises FileNotFoundError.
    Resolved INSIDE the try below, that landed in the missing-file branch and
    returned {} — PROVEN NO CLAIMS — so _session_holds_claim answered False
    and this rung read an unreadable estate as an idle one. Measured: exactly
    the state its own fail-closed contract promises to refuse.

    That this function was the PRECEDENT for the roster fix, and carried the
    identical hole one layer up, is the whole lesson: copying a pattern
    copies its blind spot."""
    try:
        path = claims_path()
    except projscope.Expired:
        raise
    except Exception:                       # noqa: BLE001 — cannot even NAME
        return None                         # the file: unsure, never empty
    try:
        with pk.open_regular(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("seats_work_offer._live_claims.read", _swallowed)
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return _sweep(raw)
    except projscope.Expired:
        raise
    except Exception:
        return None
def _session_holds_claim(session):
    """Does THIS session hold any live claim lease? Mid-claim ⇒ not idle ⇒ no
    offer. FAIL-CLOSED: a claims file we cannot read reads as busy (never offer
    on uncertainty)."""
    if not session:
        return False
    c = _live_claims()
    if c is None:
        return True
    return any(isinstance(v, dict) and v.get("session") == str(session)
               for r, v in c.items() if r != "_fence")
def _row_project(repo_id):
    """The HELM project a dispatch row belongs to, or None.

    Rows store a GIT DIR (…/helm/.git); _git_project wants a path inside the
    work tree, and it is worktree-agnostic and registry-backed — so BOTH
    sides of the comparison reduce through the SAME primitive and a lane
    worktree matches its parent project instead of reading as foreign. That
    equivalence is the whole risk in a scoping filter: get it wrong and every
    row looks foreign, the rung goes silent, and a silent rung is
    indistinguishable from an empty backlog.

    No new git shell-out: _git_project is already on the stop-hook path via
    homing, so this costs nothing a stop was not already paying."""
    p = str(repo_id or "").strip().rstrip(os.sep)
    if not p:
        return None
    if os.path.basename(p) == ".git":
        p = os.path.dirname(p)
    return _git_project(p) if p else None
def _offer_rows(seat, dispatch_snapshot=None):
    """Ranked (oldest/highest-priority first) UNOWNED, unclaimed dispatch
    backlog this idle `seat` could take:
    [(id8, line, claim_cmd, mine, kind, raw_row)].
    open_rows() is already (ts, id)-sorted, so rows[0] is the head. The claim
    resource is `dispatch:<id8>` — a stable key any idle seat computes
    identically, so two seats offered the same head collide on the claim and
    only one takes it. [] on any trouble, or when the live-claim set is
    UNKNOWN (fail-closed: an unreadable claims file must never let us poach).

    `mine` is the STRUCTURED recipient==this-seat fact (council #1's
    unambiguous/ambiguous line), carried as a flag precisely so no caller
    ever re-derives it by sniffing the display `line` for "[assigned:" — the
    line is presentation, scrubbed+clipped, and a lane could legally contain
    that literal text. `kind` is the row's recorded kind VERBATIM (build /
    review / None-for-unrecorded / historical junk) — this function reports
    facts; whether a kind is safe to self-assign is the caller's policy."""
    from . import dispatches
    if dispatch_snapshot is None:
        rows = dispatches.open_rows()
    elif dispatch_snapshot[1]:
        return []
    else:
        rows = dispatches.open_rows(dispatch_snapshot[0])
    # AN EMPTY DISPATCH BACKLOG IS NOT AN EMPTY WORLD ANY MORE. This used to
    # `return []` right here, and wiring the task ledger in below that made the
    # second producer unreachable in EXACTLY the case it exists for: no open
    # dispatch rows is what an idle fleet looks like, which is when the backlog
    # pool matters most. Caught by this lane's own first arm, which planted a
    # task row and no dispatch row and got nothing. The loop below handles an
    # empty `rows` by not running, so the early return bought nothing but a
    # silent hole.
    c = _live_claims()
    if c is None:
        return []                    # fail-closed for BOTH producers: an
                                     # unreadable claims file must never let
                                     # either of them poach
    live = _live_seats()
    if live is None:
        return []                    # SAME LAW, SECOND INPUT. An unreadable
                                     # ROSTER is exactly as poach-enabling as
                                     # an unreadable claims file: it reports
                                     # every seat absent, and this rung offers
                                     # absent seats' work away. Measured —
                                     # eleven live seats become zero through a
                                     # fail-open read. The claims half has
                                     # failed closed since it was written;
                                     # the roster half was taken on trust.
    claimed = {r for r in c if r != "_fence"}
    me, _ = _canonical_recipient(seat)
    me = str(me or "")
    mine_proj = _git_project(safe_cwd())
    out = []
    for r in rows:
        rid = str(r.get("id") or "")
        if len(rid) < 8:
            continue
        res = "dispatch:" + rid[:8]
        if res in claimed:
            continue                     # already taken by another idle seat
        # REPO SCOPE. Every dispatch row carries the repo it belongs to, and
        # this rung never asked: a seat standing in project-platform was
        # offered a HELM backlog item and spent a turn declining it (owner
        # 2026-07-29: "why does the project cwd agent get helm-related
        # stophooks"). Unknown on EITHER side stays offerable — a row with no
        # repo_id is legacy, and a seat whose cwd names no repo has nothing
        # to be foreign to; only a PROVEN mismatch excludes.
        row_proj = _row_project(r.get("repo_id"))
        if mine_proj and row_proj and row_proj != mine_proj:
            continue                     # another repository's backlog
        recip, _ = _canonical_recipient(r.get("recipient"))
        recip = str(recip or "")
        if recip and recip != me and recip in live:
            continue                     # in-flight to another live seat — theirs
        line = _clip(_scrub(str(r.get("lane") or "review")).strip(), 48) \
            or "review"
        # ASSIGNED ROWS STAY OFFERABLE BUT MUST SAY SO. A named recipient who
        # is not live is the STRANDED-WORK case this rung exists to rescue, so
        # excluding it would starve the rung (and break the tests that pin the
        # rescue). What was wrong is the FRAMING: presented as "top of
        # backlog", three seats in one afternoon each spent a turn discovering
        # it belonged to someone else. Naming the recipient turns that turn
        # into a glance — and surfaces the real anomaly, a recipient whose
        # presence beat went stale while its process kept running.
        if recip and recip != me:
            shown = r.get("recipient_display") or recip
            line += " [assigned: " + _clip(_scrub(shown), 16) + "]"
        # `mine` demands a NON-EMPTY recipient: a no-recipient row is the
        # unowned POOL, and pool rows are ambiguous by definition (any idle
        # seat could argue fit) — they stay offers, never auto-claims.
        out.append((rid[:8], line, "helm chat claim " + res,
                    bool(recip) and recip == me, r.get("kind"), r))
    out.extend(_task_offers(seat, claimed, live, mine_proj))
    return out


def _ledger_project():
    """The helm project the TASK LEDGER belongs to, or None when it cannot be
    derived. Derived from this package's own location rather than named, so no
    project label is hardcoded into portable logic — and it reduces through the
    SAME registry-backed, worktree-agnostic primitive as the asking seat's cwd,
    which is the equivalence _row_project's docstring calls the whole risk in a
    scoping filter: get it wrong and every row looks foreign, the rung goes
    silent, and a silent rung is indistinguishable from an empty backlog."""
    return _git_project(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def _task_offers(seat, claimed, live, mine_proj):
    """The task ledger's contribution to this seat's offers, or [].

    THE SECOND PRODUCER. This rung had exactly ONE source — the dispatch
    ledger — and a dispatch row is tip-bound by construction, so an un-started
    plan could never reach the surface whose whole job is handing work to an
    idle seat. The task ledger is that backlog; without this call it reaches
    nobody but the seat that filed it (measured after it landed: 15 rows filed
    by 2 seats, both authors).

    SCOPED HARDER THAN THE DISPATCH ROWS ABOVE, AND DELIBERATELY SO. Those
    carry a repo_id, so their rule is "only a PROVEN mismatch excludes" and
    unknown-on-either-side stays offerable. Task rows carry NO project at all
    and the ledger is GLOBAL, so porting that rule would exclude NOTHING —
    every row reads as legacy-unknown — and the filter would be a no-op that
    LOOKS like a guard, which is worse than no guard because the next reader
    stops asking. Same predicate, opposite effect, because the populations
    differ.

    So the proof runs once, at the module level, and it must be POSITIVE: task
    rows are offered only when the asking seat's project is provably the
    ledger's own. Unknown on either side offers nothing, because the failure
    being prevented is the owner's own report — a seat standing in
    project-platform offered a HELM backlog item and spending a turn declining
    it (2026-07-29, "why does the project cwd agent get helm-related
    stophooks"). `live` and `claimed` are passed straight through: liveness is
    what separates the stranded-work rescue from poaching a seat that is
    building right now, and the claim set is shared so two idle seats offered
    the same row collide on one resource."""
    if not mine_proj:
        return []
    ledger_proj = _ledger_project()
    if not ledger_proj or ledger_proj != mine_proj:
        return []
    try:
        from . import tasks
        return tasks.offer_rows(seat=seat, claimed=claimed, live=live)
    except projscope.Expired:
        raise
    except Exception:                        # noqa: BLE001
        return []                            # a second producer never breaks
                                             # the first one's offers


def _offer_landing_state(row):
    """(True/False/None, pinned trunk sha) for one offer's reviewed work.

    Reuse landreq's ONE ancestry -> patch-identity predicate. A REVIEW row's
    ref is the reviewed tip; a BUILD row's ref is only the base the work was
    dispatched FROM, so it is never content identity. BUILD without an
    explicit reviewed_tip therefore stays UNKNOWN rather than claiming that
    its usually-on-trunk base proves the work landed.

    This is called only after the own-assigned rung WINS selection, preserving
    the stop-whisper budget: candidate enumeration stays local-only, then one
    bounded proof runs for the single row otherwise about to say START. Every
    failure is UNKNOWN, never absent and never permission to build."""
    try:
        from . import landreq
        tip = row.get("reviewed_tip")
        if not tip and row.get("kind") == "review":
            tip = row.get("ref")
        if not tip:
            return None, None
        gitdir, err = landreq._close_repo(row, None)
        if err:
            return None, None
        _ref, pinned, _target, err = landreq._close_trunk(row, gitdir, None)
        if err:
            return None, None
        projscope.spend_or_raise("work offer landing proof")
        proof = landreq._landing_proof(gitdir, tip, pinned)
        projscope.spend_or_raise("work offer landing proof result")
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("seats_work_offer._offer_landing_state", _swallowed)
        return None, None
    if proof in ("ancestor", "patch-equivalent"):
        return True, pinned
    if proof == "absent":
        return False, pinned
    return None, pinned
def _finalize_work_offer(session, seat, offer, line, actor=None):
    """Finalize one winning own-offer as CLOSE, UNKNOWN, or claimed START.

    `offer` is the exact raw tuple selected during cheap candidate enumeration.
    Carrying it here avoids a second full backlog fold; claim() remains the
    lease mutation boundary and resolves a competing claim to silence.

    `actor` IS THE ACTUATOR KEY AND A SEAT NAME IS NOT. This function is
    reached from `seats_stop_guard`, whose local use of the name is warn text —
    two hops of what reads as render ending in a LEASE. So taking work requires
    an `AutoClaimCapability`, which binds an admitted actor to the per-kind
    policy THIS layer owns: a caller that merely resolved a name cannot reach
    `claim()` through here, and a caller holding an actor still cannot widen
    the kinds. Without one, the row is still SURFACED (CLOSE and UNKNOWN are
    reports, not acts) — only the lease is withheld."""
    if not isinstance(offer, (list, tuple)) or len(offer) < 6:
        return None
    rid, row = offer[0], offer[5]
    projscope.spend_or_raise("work offer landing state")
    landed, trunk = _offer_landing_state(row)
    projscope.spend_or_raise("work offer landing state result")
    if landed is True:
        # OPEN ROW != UNFINISHED WORK. Content can reach trunk through a sibling
        # chain, fold, or rework while this row stays open. Surface closure
        # without taking a lease or saying START.
        return ("landed:%s:%s" % (rid, trunk[:12]),
                "dispatch %s (%s) is OPEN, but its reviewed work appears "
                "on trunk at %s; it likely needs CLOSING, not starting."
                % (rid, offer[1], trunk[:12]))
    if landed is None:
        # BUILD refs are starting bases, never produced content. Read failures
        # also stay UNKNOWN; neither case is permission to rebuild.
        return ("landing-unknown:%s" % rid,
                "dispatch %s (%s) is OPEN, but its landing state is UNKNOWN — "
                "verify before starting; no lease was claimed." % (rid, offer[1]))
    if landed is not False:
        return None
    from . import actors
    try:
        cap = actors.AutoClaimCapability(actor, offer[4], rid,
                                         _AUTOCLAIM_KINDS)
    except actors.ActorRefused:
        return None      # no admissible identity: surface, never actuate
    try:
        projscope.spend_or_raise("work offer lease claim")
        ok, _m, _l = claim("dispatch:" + cap.ref, seat, session=session)
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("seats_work_offer._finalize_work_offer", _swallowed)
        ok = False
    return ("autoclaim:%s" % rid, line) if ok else None
# The kinds a seat may hand ITSELF (the auto-claim gate). Deliberately a
# LOCAL literal, not dispatches.KINDS: those two sets agree today, but if the
# ledger ever grows a kind (deploy, migrate, …) the actuator must not widen
# with it silently — self-assignment is a per-kind POLICY choice made here,
# and anything unrecognised (including None, the honest UNKNOWN of a
# historical row, and junk a hand-planted ledger row could carry) fails
# closed to an ordinary offer.
_AUTOCLAIM_KINDS = ("build", "review")
def _work_offer_candidate(session, seat, ask, dsp, pending, inbox_blocked,
                          dispatch_snapshot=None):
    """The BOTTOM rung, now with the ACTUATOR (council #1, unanimous): the
    fleet had a full sensor array — offers, presence, idle-dispatch,
    deadlines — and no muscle: 19 autonomous work-offers in 48h, 0 converted
    to a self-claim; every conversion came from a judgment seat by hand. So
    the UNAMBIGUOUS case now acts instead of asking: when the backlog HEAD
    was dispatched TO this very seat (the structured `mine` flag from
    _offer_rows, never sniffed out of the display line) AND its kind is one
    a seat may hand itself (_AUTOCLAIM_KINDS), this rung ACQUIRES the
    `dispatch:<id8>` lease itself — the SAME resource, through the SAME
    flocked claim() the offer's printed command would reach — and the
    whisper flips from take-it-or-pass to here-is-your-task. Everything
    AMBIGUOUS stays an OFFER, unchanged: a row assigned to someone else
    (stranded work — routing it is a judgment call that stays surfaced), a
    no-recipient pool row, an unrecorded/unrecognised kind, or ANY claim
    trouble. A lost claim race is NORMAL: claim() is the mutex (check+sweep+
    write under one flock), so two idle seats racing the same head resolve
    to exactly one holder and the loser merely offers.

    fp discipline: autoclaim:<id8> is DISTINCT from offer:<id8> so neither
    latch shadows the other — a head offered yesterday and dispatched to me
    today still actuates. And the acquired lease is self-reinforcing: on the
    next stop _session_holds_claim() silences this rung and the guard's
    session-lease BLOCK names the held dispatch, so a seat that idles past
    the whisper is told, louder, that it is idling ON ITS OWN TASK. The lease
    is minted only when this rung WINS selection (mint-on-win — see
    _stop_whisper), so a higher own-work rung outranking the offer can never
    leave a stray claim behind.

    IDLE GATE — the OWN-WORK legs are the gate, and the inbox is not one of
    them. A seat is idle-for-actuation when the ask and dispatch rungs are
    empty and this session holds no live claim lease. UNREAD CHAT DOES NOT
    COUNT, and removing it is the fix for a starvation measured 2026-07-30
    with three seats parked on assigned work at once.

    The old gate was `or pending or inbox_blocked`. Two compounding effects,
    both fatal: (1) a dispatch's OWN arrival made its recipient not-idle, so
    the actuator refused to claim the very row the notification announced —
    send work, and the sending is what stops the work being taken; (2) a row
    stays `pending` until CONSUMED, and a working seat's inbox never empties
    (one seat measured at 108 undelivered rows), so the gate was permanently
    shut for every seat with a backlog. Together they meant the actuator could
    not fire for ANYONE, which is exactly what the fleet looked like: healthy
    seats, assigned work, nobody claiming.

    Unread chat is AMBIENT INFORMATION, never a work obligation — the
    stop-guard already surfaces it, and this rung only ever claims a row that
    is ALREADY ASSIGNED TO THIS SEAT. Taking your own assigned work while
    messages wait is correct; the alternative is a fleet that waits to be told
    twice. The own-work legs still fail closed, so a seat with a live ask, an
    owed dispatch, or a held lease is still refused. FAIL-CLOSED to None."""
    if not seat or ask:
        return None
    # `dsp` IS NOT AN IDLE SIGNAL AND MUST NOT GATE THIS RUNG. It is
    # _dispatch_candidate(), which takes NO seat argument: it scans the WHOLE
    # ledger and returns the oldest OVERDUE row anywhere in the fleet — work
    # SOMEONE handed to SOMEONE and has not checked on. A single stuck row
    # therefore disabled auto-claim for whatever session was running, fleet-wide
    # and indefinitely (MEASURED 2026-07-30: one row at delivery=needs-
    # confirmation was gating it live while every seat sat parked). Checking in
    # on another seat's overdue row is a NAG, not an obligation that conflicts
    # with taking your OWN assigned work — it stays a whisper rung of its own,
    # ranked above this one, so it still speaks; it just no longer silences the
    # actuator. The genuinely conflicting own-work signals (`ask`, a held claim
    # lease) still fail closed above and below.
    if _session_holds_claim(session):
        return None
    try:
        rows = _offer_rows(seat, dispatch_snapshot=dispatch_snapshot)
    except projscope.Expired:
        raise
    except Exception:
        return None
    if not rows:
        return None
    # OWN WORK FIRST, ANYWHERE IN THE BACKLOG — not just at its head. rows is
    # (ts, id)-sorted, so a seat's OWN assigned row can sit behind an unowned
    # pool row; reading only rows[0] meant that seat starved exactly like the
    # inbox gate starved it, one position over (cross-family review finding,
    # 2026-07-30). Auto-claimable own work is preferred wherever it sits; the
    # OFFER still speaks for the true head, because an offer is about the
    # backlog and a claim is about this seat.
    own = next((r for r in rows if r[3] and r[4] in _AUTOCLAIM_KINDS), None)
    rid, line, take = (own or rows[0])[:3]
    own_assigned = own is not None
    if (pending or inbox_blocked) and not own_assigned:
        # UNREAD MAIL SUPPRESSES AN OFFER, NEVER AN AUTO-CLAIM. An offer is an
        # interruption that asks for a judgment (pool work, or a row assigned
        # to someone else) — piling that on a seat with undelivered messages is
        # noise, so the inbox still owns that stop. An auto-claim is the seat
        # picking up work ALREADY ASSIGNED TO IT, which unread chat has no
        # bearing on.
        return None
    if own_assigned:
        # PROPOSE ONLY. Candidate enumeration is salience bookkeeping, not a
        # permission boundary: a higher red/stuck/dirty rung can still win and
        # discard this candidate. _stop_whisper carries this selected row to the
        # offer finalizer only after autoclaim:<id8> WINS; proof and mint live there.
        return ("autoclaim:%s" % rid,
                "auto-claimed dispatch %s (%s) — it was dispatched TO YOU and "
                "you are idle. The lease is yours: START it now." % (rid, line),
                own)
    return ("offer:%s" % rid,
            "you're free — top of backlog is %s: %s. Take it (%s) or pass."
            % (rid, line, take))
def _session_delegated(session):
    """Has THIS session handed work to a subagent AT ALL? One listdir of
    chat.chat_dir(), no locks, no git, no snapshot fold (reflex law).

    TWO delegation file families answer this and only one of them is honest
    on its own:

      * `.deleg_stopped.<s_hash>.<a_hash>` — written by SubagentStop for
        EVERY subagent, with no room, lane or claim binding (see
        _clear_posttool_delegation: it checks the event name and two string
        fields, nothing else), and NOTHING ever unlinks it. That makes it the
        durable record. Measured on this host 2026-08-05: 2659 tombstones
        across 29 distinct session hashes.
      * `.deleg_activity.<r>.<w>.<s_hash>` — the LIVE half, and a bare
        filename match on it would be a lie. Its `live_scan` records are any
        same-uid process whose cwd is the claimed room and whose ancestry
        carries the session id — a Bash-tool `pytest` qualifies. Measured the
        same day: 13 of the 14 live activity files carried `live_scan` and
        nothing else. Counting those would read a BUILD as a DELEGATE and
        silence this rung on exactly the seat it exists for. Only the
        `_AGENT_KEY_PREFIX` records — a documented subagent PostToolUse —
        count, and only the ones this session's s_hash names.

    The answer is their UNION because a still-running delegate and a finished
    one prove the identical thing for the throughput question: this seat is
    not carrying alone. The activity leg exists solely to cover a session
    whose FIRST subagent is live and has not stopped yet; without it a seat
    would be told to delegate in the same minute it did.

    Also note what is NOT unlinked: these tombstones accumulate for the life
    of the tmpfs, so a session that delegated once at hour 1 still reads
    delegated at hour 9. That is deliberate for THIS rung — it exists to
    break the ZERO case the owner measured (eight hours, no subagent calls),
    not to police a ratio — but it means the rung cannot be reused as a
    "delegate more" nag without a different signal.

    FAIL-CLOSED TO True (== delegated == SILENCE). An unreadable chat dir
    makes delegation UNKNOWN, and this table's law is that trouble yields
    nothing, never a louder lane."""
    if not session:
        return True
    s_hash = hashlib.sha256(str(session).encode("utf-8")).hexdigest()
    stopped = _STOPPED_PREFIX + "%s." % s_hash
    suffix = "." + s_hash
    live = []
    # BOTH roots for the compat window: a seat still running pre-namespace
    # code writes markers flat, and missing them answers "never delegated"
    # for a session that did — the rung then tells it to delegate again.
    #
    # FAIL CLOSED WHEN NOTHING IS READABLE. The single-root version returned
    # True on OSError, and splitting the read into two roots must not turn
    # that into a fall-through: `continue` past both leaves live empty and
    # answers NEVER DELEGATED, which is the loud direction on a seat we
    # cannot see. An unreadable root is UNKNOWN, and this table's law is
    # that trouble yields nothing, never a louder lane.
    unread = False
    for d in chat.state_scan_dirs("deleg"):
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            # NOT-YET-CREATED IS EMPTY, NOT UNREADABLE. The family subdir is
            # minted by _ensure_dir, so a home that has not written a marker
            # yet has no such directory — and treating that as UNKNOWN makes
            # the rung fail closed FOREVER on a fresh install, which is the
            # over-quiet mirror of the bug this guard exists for. Absent is a
            # measured "nothing here"; only a real I/O error is unknown.
            continue
        except OSError:
            unread = True        # ANY root, not just all of them
            continue
        for name in names:
            if name.startswith(stopped):
                return True      # the cheap answer, and the common one
            if name.startswith(_ACTIVITY_PREFIX) and name.endswith(suffix):
                live.append(os.path.join(d, name))
    if unread:
        # ONE unreadable root is already UNKNOWN. A finding in a root we DID
        # read short-circuits above and is a definite answer; reaching here
        # means we found nothing, and "nothing" from a partial read cannot be
        # told from "nothing" from a whole one — the root we could not open
        # is exactly where the tombstone might be.
        return True
    for path in live:            # bounded by leases held, never by fleet size
        # THE SAME ONE DOOR the writer uses: strict v2, and a truthy
        # non-dict envelope reads as empty instead of raising. This rung
        # bypassed the version check entirely, so a v1 or torn file's
        # records could prove delegation the writer would never have
        # accepted.
        records = seats_delegation.v2_records(pk.read_json(path, {}))
        if records and any(
                str(k).startswith(_AGENT_KEY_PREFIX) for k in records):
            return True
    return False
def _solo_load_candidate(session, seat):
    """The THROUGHPUT rung — the only one in this table that reads the SHAPE
    of the seat's own load instead of the state of some thing.

    THE STRUCTURE THAT PERMITTED THE GAP. helm has had rich delegation
    machinery all along (_delegated_build, _get_delegation_activity,
    _record_delegation_activity) and every reader of it asks ONE question: is
    a delegate live, so that this stop is not really idle? It is purely
    PROTECTIVE — it EXCUSES a stop, it never SHAPES one. So nothing in helm
    had ever observed the opposite state: a seat carrying a wide load ALONE.
    Owner-measured 2026-08-03: the integrator held a wide queue for eight
    hours, made ZERO subagent calls, three seats sat near-idle, and the owner
    ask list grew ~46 -> 73 overnight. The defect on his screen was a missing
    RUNG, not a wrong line.

    WHAT IT MEASURES, AND ONLY THAT: live claim leases connected to this seat
    the way the guard's own lease sermon defines connected — minted by this
    session, or naming this seat as holder. That is one _live_claims() read,
    measured at 0.23ms on the live claims file.

    WHAT IT DELIBERATELY DOES NOT COUNT, both rejected on measurement:
      * `pending` rows. They are UNREAD CHAT, not obligations, and this
        module already records that a working seat's inbox never empties
        (ds4pro at 108 undelivered rows — see _work_offer_candidate). A count
        that includes them fires on every seat forever, which is the
        always-fires noise this table's own comments warn about.
      * owed dispatches. dispatches.snapshot() folds 1435 events and measures
        121ms on the live ledger — two orders of magnitude past everything
        else here, and _dispatch_candidate already pays it once per stop. A
        second fold to reach a COUNT would double the hot path for a number
        the whisper does not need. So the claim this rung makes is narrow and
        true — "N leases held" — rather than wide and unmeasured.

    ORDER: the lease count is checked FIRST because it is 0.23ms and the
    delegation listdir is ~4ms over 5728 entries. A seat under the threshold
    never pays the scan.

    The fp buckets by N // SOLO_LOAD_AT exactly as stuck/dirty do, so a queue
    growing 3 -> 6 re-fires and a queue drifting 3 -> 4 does not; delegating
    once returns None outright, which is the genuine silence."""
    if not session:
        return None
    c = _live_claims()
    if not c:
        return None      # {} = holding nothing; None = UNKNOWN, and UNKNOWN
                         # obligations may not accuse a seat of hoarding them
    me = str(seat).strip() if seat else ""
    held = sum(1 for r, v in c.items()
               if r != "_fence" and isinstance(v, dict)
               and (v.get("session") == str(session)
                    or (me and str(v.get("holder") or "").strip() == me)))
    if held < SOLO_LOAD_AT or _session_delegated(session):
        return None
    # MEASURED 2026-08-05: composes to 231 bytes at N=3 and 233 at a 3-digit
    # N, against STOP_WHISPER_CAP 240 — this one FITS, unlike the dirty rungs
    # beside it (247/243, both clipping their own boilerplate). The remedy
    # still sits mid-body rather than last, per the ordering law: the trailing
    # "Alone, a queue only grows." is emphasis and is the designated overflow,
    # so the two named verbs survive any future growth into the 7-byte margin.
    return ("solo:%d" % (held // SOLO_LOAD_AT),
            "%d leases held, 0 subagents spawned this session — hand 2 out "
            "NOW: spawn a subagent (Agent tool) or `helm dispatch send "
            "<seat> <lane>`. Alone, a queue only grows." % held)
def _whisper_candidates(session, seat, pending, inbox_blocked, cwd=None,
                        dispatch_snapshot=None):
    """LIVE whisper candidate tuples, salience-ordered: owner-ask >
    stuck > red-gate > stale-pending > unverified > unbanked-green > dirty >
    solo-load > work-offer. The first two fields are always (fp, line);
    autoclaim alone carries its selected raw offer as a third field for
    winner finalization.
    Signals are cheap local reads only (reflex law): the session's record.py
    counters + verify-grounding logs (command-log/edit-targets) + the
    pending rows the guard already computed + one claims read and one chat-dir
    listing for the solo-load rung. The one enumeration-time git probe
    is checkout staged-deletion state, armed only for bank/commit advice (`cwd`
    from the Stop payload; None skips it and keeps the legacy wording). Review
    ancestry/patch identity is deferred until autoclaim WINS in _stop_whisper,
    so a higher rung never pays for a proof whose output it discards. Each fp
    carries a LEVEL bucket so
    a worsening streak re-fires (reflex escalate law) and a new pending set,
    red run, or green state re-arms. The work-offer rung sits LAST (own work
    before offered work) and only fires for a genuinely idle seat."""
    out = []
    ask = _ask_candidate()   # owner-ask rung: unsurfaced owner debt outranks all
    if ask:
        out.append(ask)
    dsp = _dispatch_candidate(dispatch_snapshot)  # then: handed-out work
    if dsp:
        out.append(dsp)
    c = {}
    if session:
        try:
            from . import record
            got = record.counters(session)
            c = got if isinstance(got, dict) else {}
        except projscope.Expired:
            raise
        except Exception as _swallowed:
            record.swallow("seats_work_offer._whisper_candidates.counters",
                           _swallowed)
            c = {}

    def n(k):
        try:
            return int(c.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    stuck, dirty = n("stuck-streak"), n("dirty-streak")
    if stuck >= STUCK_AT:
        out.append(("stuck:%d" % (stuck // STUCK_AT),
                    "stopping while wedged — %d repeated infra/auth failures "
                    "this session; surface the blocker or check creds before "
                    "idling (pull: helm reflex smoke --session %s)"
                    % (stuck, session)))
    # the verify-grounding rungs (slice 2): one bounded read of record.py's
    # command-log + edit-targets — red gate > (…pending…) > unverified >
    # unbanked-green, each mutually exclusive by construction.
    latest = _runner_latest(session)
    edits = _edited_code(session)
    dirty_now = bool(c.get("last-dirty"))
    gate = _gate_candidate(latest)
    if gate:
        out.append(gate)
    if pending and not inbox_blocked:
        try:  # STALE rows only — reflex._fresh fails open to fresh, which
            from . import reflex  # fails the whisper CLOSED (silence) here
            stale = [(rm, r) for rm, r in pending
                     if not reflex._fresh(r.get("ts"), PENDING_STALE_S)]
        except projscope.Expired:
            raise
        except Exception:
            stale = []
        if stale:
            out.append(("pending:" + _rows_fp(stale),
                        "%d owner/mention row(s) unlanded >%dm (pointed-at "
                        "once, no longer re-blocking) — land or explicitly "
                        "route them (pull: helm chat read)"
                        % (len(stale), PENDING_STALE_S // 60)))
    uv = _unverified_candidate(dirty_now, edits, latest)
    if uv:
        out.append(uv)
    # ONE staged-deletion probe, armed only when a rung below would otherwise
    # speak a bank/commit verb — the two rungs that jointly cover the "is this
    # dirt yours?" decision share one read of the same state, never two probes
    # and never a probe on a stop that has nothing to bank.
    staged = _staged_deletions(cwd) \
        if ((dirty_now and edits and latest) or dirty >= DIRTY_AT) else None
    ub = _unbanked_candidate(dirty_now, edits, latest, staged,
                             root=c.get("last-dirty-root"), here=cwd)
    if ub:
        out.append(ub)
    if dirty >= DIRTY_AT:
        # THE SIBLING RUNG, and it was WORSE than _unbanked_candidate. That one
        # at least verified every latest gate was green before saying so; this
        # one never reads `latest` at all and still said "bank the GREEN slice."
        # It also counts dirtying OPS, not work — a mutation-test cp/sed loop
        # runs the counter up without producing anything worth committing.
        #
        # FIXED THE SIBLING BECAUSE FIXING ONE GUARD IS NOT FIXING THE CLASS.
        # I repaired _unbanked_candidate's shared-checkout claim and left this
        # rung asserting the same thing two screens down, which is exactly the
        # watchdogs-correct-composition-holed failure: per-rung correctness
        # guarantees nothing when the rungs jointly cover one decision. It fired
        # at me minutes after that fix, on a tree holding another seat's work,
        # with everything of mine already pushed.
        # THE NUMBER IS NOT WHAT THE OLD TEXT SAID IT WAS. record.py increments
        # dirty-streak on EVERY tool call while `last-dirty` is true — not on
        # dirtying ops. Reads, greps and one-liners all bump it. So "N dirtying
        # ops" was false, and it was false in a way that hides the real story:
        # when the dirt belongs to ANOTHER SEAT in the shared checkout, this
        # seat cannot clear it, so the count grows without bound and every
        # future stop is billed for a tree it does not own. Measured 2026-07-27:
        # it reached 41 against exactly 2 dirty paths, both another seat's,
        # while everything of mine was committed and pushed.
        #
        # I wrote the false version at 6c2713b ("whisper: fixed one rung's
        # shared-checkout claim and left its sibling asserting the same
        # thing") while fixing this very rung — inherited the wording from
        # the text I was replacing and restated it with more confidence than
        # the original. Say what the number IS.
        # MEASURED TO FIT, twice, and the budget forced a real priority call.
        # The first rewrite CLIPPED — the 240-byte cap ate "NOT yours -> ...",
        # the third time today I lengthened a whisper and pushed its own advice
        # off the end. My second attempt fit by DROPPING `stash create`, which
        # was the wrong thing to cut: the remedy outranks emphasis. "tool calls
        # with the tree DIRTY" already corrects the false label on its own, so
        # the parenthetical went instead and the remedy stayed. Verified at a
        # 4-digit N, which is reachable — this counter climbs on every call
        # while another seat's dirt sits in the shared tree.
        # SAME staged-deletion refusal as _unbanked_candidate (the reasoning
        # lives there) — and it must live HERE TOO, per this rung's own
        # lesson above: per-rung correctness guarantees nothing when the
        # rungs jointly cover one decision. "yours -> commit" banks staged
        # deletions exactly as `add -u` would: they are ALREADY staged, a
        # bare commit ships them.
        if staged and staged[0] and staged[1]:
            out.append(("dirty-del:%d:%s" % (dirty // DIRTY_AT,
                                             _deletions_fp(staged[1])),
                        "%d tool calls, tree DIRTY + %d STAGED deletion(s) "
                        "in this SHARED checkout: %s — scrub or accident? "
                        "one seat can't tell: do NOT stage/commit; act "
                        "per-path"
                        % (dirty, len(staged[1]),
                           _deletions_named(staged[1]))))
        # SAME venue honesty as _unbanked_candidate's fallback (the reasoning
        # lives there): say only what the probe measured — LANE when it read
        # not-shared, UNKNOWN when it read nothing, SHARED only when it saw
        # shared. The flat "SHARED checkout" here fired on lane rooms too.
        elif staged is not None and not staged[0]:
            out.append(("dirty:%d" % (dirty // DIRTY_AT),
                        "%d tool calls with the tree DIRTY, not %d edits. "
                        "LANE room — a lane owns its own index: commit in "
                        "your room (or stash create) to clear it"
                        % (dirty, dirty)))
        elif staged is None:
            out.append(("dirty:%d" % (dirty // DIRTY_AT),
                        "%d tool calls with the tree DIRTY, not %d edits. "
                        "Shared or lane? UNKNOWN — read `git diff --stat "
                        "HEAD`: yours -> commit; NOT yours -> stash create, "
                        "may never clear"
                        % (dirty, dirty)))
        else:
            out.append(("dirty:%d" % (dirty // DIRTY_AT),
                        "%d tool calls with the tree DIRTY, not %d edits. "
                        "SHARED checkout — read `git diff --stat HEAD`: "
                        "yours -> commit; NOT yours -> stash create, may "
                        "never clear"
                        % (dirty, dirty)))
    # THE THROUGHPUT RUNG, one step above the bottom. It sits BELOW everything
    # correctness-shaped on purpose: a red gate, an unverified edit or a dirty
    # shared tree are all "you may be WRONG", and being wrong outranks being
    # slow. It sits ABOVE the offer because taking on MORE work while already
    # carrying a wide load alone is the precise failure the offer would cause.
    #
    # NOTE the overlap, because the placement mostly does not decide anything:
    # NO THROUGHPUT RUNG HERE ANY MORE. Solo-load lived one step above the
    # bottom of this ladder for exactly one day (2026-08-05) before the owner
    # named it a conditional stopbook — it is now _ndp_gate, which owns its
    # own emission. Re-adding it here would give one predicate two mouths and
    # two latches, and the ladder seat is the one that STARVES: on the seat
    # the rung exists for, some higher rung is unlatched at almost every stop.
    off = _work_offer_candidate(
        session, seat, ask, dsp, pending, inbox_blocked,
        dispatch_snapshot=dispatch_snapshot)
    if off:                  # the BOTTOM rung: offered work, only when idle
        out.append(off)
    return out
def _stop_whisper(session, room, seat, pending, inbox_blocked, cwd=None,
                  actor=None, dispatch_snapshot=None):
    """ONE budgeted contextual continuation for this stop, or None. The
    highest-salience signal whose (signal, level) fingerprint has NOT fired
    for this (seat, session) wins; firing latches it (fired-set JSON, capped)
    and appends one measurability row to the stop-whisper ledger (ids only,
    never text — the fire-ledger law). FAIL-CLOSED TO NOTHING: any state or
    ledger trouble yields silence, never a raise, never a louder lane."""
    projscope.spend_or_raise("stop whisper candidates")
    cands = _whisper_candidates(
        session, seat, pending, inbox_blocked, cwd,
        dispatch_snapshot=dispatch_snapshot)
    projscope.spend_or_raise("stop whisper candidate result")
    if not cands:
        return None
    path = _stop_fp_path(room, seat, session, kind="stopwhisper")
    from .seats_cursor import seat_state_lock
    with seat_state_lock(seat, session=session) as current:
        if not current:
            return None
        d = pk.read_json(path, {}) or {}
        fired = ([str(x) for x in d.get("fired") or []]
                 if isinstance(d, dict) else [])
        hit = next((c for c in cands if c[0] not in fired), None)
        if not hit:
            return None
        fp, line = hit[:2]
        claimed = fp.startswith("autoclaim:")
        if claimed:
            final = _finalize_work_offer(
                session, seat, hit[2] if len(hit) > 2 else None, line,
                actor=actor)
            if not final:
                return None
            fp, line = final
            if fp in fired:
                return None
        try:
            # Auto-claim was admitted at its mutation door; from there through
            # publication this is one transaction and may not be interrupted.
            if not hit[0].startswith("autoclaim:"):
                projscope.spend_or_raise("stop whisper latch write")
            chat._ensure_dir()
            pk.write_json(path, {"v": 1, "ts": pk.now_ts(),
                                 "fired": (fired + [fp])[-_WHISPER_FIRED_CAP:]})
        except projscope.Expired:
            raise
        except Exception as _swallowed:
            record.swallow("seats_work_offer._stop_whisper.latch", _swallowed)
            if not claimed:
                return None
    try:  # measurability rides the fire (fail-open; ids only)
        from . import inject
        inject._append_jsonl(
            os.path.join(home.global_dir(), ".state", "stop-whisper-ledger.jsonl"),
            {"v": 1, "ts": pk.now_ts(), "id": fp, "seat": seat,
             **({"session": str(session)} if session else {})},
            inject.LEDGER_MAX)
    except Exception:
        pass
    return _clip("[helm stop-whisper] " + line +
                 " This holds once per state — a re-stop passes.",
                 STOP_WHISPER_CAP)
