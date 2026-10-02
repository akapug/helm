"""`helm.dispatches_rebind` -- MAY AN OPEN ROW MOVE TO ANOTHER RECIPIENT.

ONE QUESTION. `rebind` moves one open row to a new recipient behind an
evidence gate (proxywatch starvation, or the recipient's context wall); the
room fence, the child disown `retract` shares, the note that tells the new
reader where the brief went, and the superseded-parent sweep that finds a
parent its successor already finished are the machinery it and its callers
use.

MEASURED AT THE CUT, NOTHING LEFT IN THE LEDGER CALLS IN. The one caller
outside this file inside the old ledger was `retract`'s disown path, which
moved in the same change to `dispatches_retract`. The verb table, seat
reassignment and the liveness rung reach `dispatches.rebind`, which still
answers.

A MUTATION WITNESS BINDS TO THIS FILE BY PATH:
tests/test_unreached_guarantees.py (M5) mutates `_successor_finished` here.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `dispatches.NAME` AT
ITS CALL SITE -- 35 reads of 27 distinct names; no other byte of a moved line
changed. A `from` import, or a bare global left behind, binds the object ONCE
at import, so an arm that patches an attribute on `dispatches` and then drives
this code would reach the original and measure nothing, while every structural
guard stayed green. The module spelling keeps the lookup at CALL TIME, exactly
as a bare global did inside the ledger. Names this file OWNS are spelled that
way too, because they are published back onto `dispatches` and the suites
patch them there.

THE ONE EXCEPTION IS A READ THAT RUNS AT IMPORT, and it is the faithful
spelling rather than a gap: the default `_required=_PARENT_REQUIRED` on
`_successor_finished`. Such a read binds once in either spelling, as it did in
the ledger, and at that moment only this module's own global exists -- the
publish loop at the bottom has not run yet, so `dispatches.NAME` there would
raise.

THE CYCLE IS BROKEN THE WAY THE LEDGER'S OTHER SATELLITES BREAK IT: this
module imports `dispatches` EAGERLY, and `dispatches` imports this one at the
END of its own body, after every name it needs exists. A module object is in
`sys.modules` from the first line of its execution, so either import order
resolves.
"""
import os

from . import dispatches
from . import pk, rebind_liveness


_STARVED_SIGNALS = ("down", "hang")


def _proxy_evidence(recipient):
    """(starved-reason, None) when proxywatch measures the recipient's PROXY
    as unable to serve it, else (None, unavailable-note). The signal surface is
    health()'s row for the seat: proxy probe down/hang, log refusal streak
    (STARVED), or a live pane with a transcript that has stopped growing
    (HANG?). An unreadable health pass is NOT evidence — rebind by default
    requires the measured signal, so a blind watch means no rebinding, which
    is the safe polarity (the judgment path is --force)."""
    from . import proxywatch
    try:
        # Rebind evidence is LOCAL ability-to-act evidence. It must never spend
        # authenticated family-canary tokens or wait on sibling corroboration.
        rep = proxywatch.health(seats=[recipient], include_upstream=False)
    except Exception as e:
        return None, "proxywatch health unreadable (%s)" % e.__class__.__name__
    rows = rep.get("seats") or []
    if not rows:
        return None, "proxywatch has no row for %s" % recipient
    row = rows[0]
    if isinstance(row.get("down"), dict):
        # An operator stood this seat's proxy down (`helm seat down`), so the
        # watch no longer probes it — and a proxy stood down cannot serve the
        # work, which is the same measured answer the down probe gave.
        from . import seat_down
        return ("proxy %s" % seat_down.describe(row["down"])), None
    if row.get("probe") in dispatches._STARVED_SIGNALS:
        return ("proxy probe %s (%s)" % (row["probe"], row.get("probe_detail"))), None
    if row.get("log") == "streak":
        return ("proxy refusing streak (%s)" % row.get("log_detail")), None
    if row.get("hang_candidate") and row.get("turn_state") == "hung":
        # Raw transcript age can still be a HANG? warning when the host's
        # suspend placement is unknown; only the measured verdict authorizes
        # moving another seat's work.
        age = row.get("transcript_age_s") or 0
        return ("pane live, transcript silent %dm" % (age // 60)), None
    return None, None


CONTEXT_WALL_PCT = 100.0

# autocompact.read()'s `status` is an elif CHAIN, so an earlier verdict MASKS
# every later check. Only these two are reached AFTER the freshness test, and
# so are the only statuses whose pct is provably THIS pane's CURRENT context.
# `claude-model` in particular short-circuits BEFORE the age check, so a
# claude-model row may be arbitrarily stale while still carrying a pct.
_CONTEXT_FRESH_STATUSES = ("ok", "session-unbound")


def _context_wall(recipient):
    """(wall-reason, None) when the recipient's CONTEXT WINDOW is measurably
    exhausted, else (None, None).

    THE BLINDNESS THIS CURES. Rebind's only evidence surface was proxywatch,
    which measures the PROXY. A seat at 100% of its context window has a
    perfectly healthy proxy and cannot take a turn, so the gate refused every
    rebind off it and the judgment path (--force) was the only way through.
    Measured: one codex seat climbed 82% -> 116% over twenty minutes across
    21 consecutive autocompact refusals — proxywatch reported it healthy the
    whole time, because it WAS healthy. The instrument was sound and pointed at
    the wrong subject.

    THE CLASSIFIER ALREADY EXISTS — autocompact.read() is the owner of the
    context question and idle_dispatch._context_pressure already composes it
    read-only. This asks that one owner rather than growing a second gauge,
    which is the same choice _provider_wall made one file over.

    PRESENT-TENSE ABILITY IS THE QUESTION, not recoverability. A context-full
    seat may well be rescued by a later /compact, and that does not make it able
    to act NOW — conflating the two is what kept this arm from existing. Rebind
    is also non-destructive (cancel-as-REBOUND plus a superseding re-add), so a
    seat that recovers a minute later has lost nothing.

    A STALE READING IS NOT A MEASUREMENT OF NOW, which is why the status
    allowlist is narrow and derived from reading autocompact's own elif chain
    rather than from the status names sounding trustworthy."""
    try:
        from . import autocompact
        row = autocompact.read(recipient) or {}
    except Exception:
        return None, None
    if row.get("status") not in dispatches._CONTEXT_FRESH_STATUSES:
        return None, None
    pct, win = row.get("pct"), row.get("window")
    if pct is None or not win or pct < dispatches.CONTEXT_WALL_PCT:
        return None, None
    return ("context window exhausted: %.1f%% of %dk (autocompact status %s)"
            % (pct, int(win) // 1000, row.get("status"))), None


def _recipient_evidence(recipient):
    """(starved-reason, None) when the recipient is measurably unable to act,
    else (None, unavailable-note). TWO INDEPENDENT SURFACES, because a seat can
    be blocked by its transport OR by its own window and neither one can see
    the other: proxywatch answers "is the proxy serving it", autocompact
    answers "has it any window left".

    THE CONTEXT ARM RUNS EVEN WHEN PROXYWATCH IS BLIND. A proxywatch that
    cannot be read is exactly the moment a second, independent measurement is
    worth most, so its unavailable-note is carried and only returned when BOTH
    arms come back silent — never as an early exit that suppresses the other."""
    reason, note = dispatches._proxy_evidence(recipient)
    if reason:
        return reason, None
    wall, _ = dispatches._context_wall(recipient)
    if wall:
        return wall, None
    return None, note


_PARENT_REQUIRED = object()


def _successor_finished(kid, cache, parent=_PARENT_REQUIRED):
    """Does this successor hold a verdict whose work is PROVABLY on trunk?

    THE AUTHORIZATION RUNG. Everything else in the sweep keys on SHAPE (open,
    has a successor, unannotated), and shape is a thing the fleet keeps
    producing: a live sweep run 2026-08-04 matched a row that had entered the
    shape 54 SECONDS earlier. So an id allowlist cannot authorize this sweep —
    it expires in minutes. This predicate can, because it asks whether the
    work is FINISHED, which is the harm statement itself.

    BOUND TO `reviewed_tip`, AND THAT BINDING IS THE RUNG. `verdict_ref` reads
    like the field for this and is NOT: measured over the live ledger, it
    holds free-text evidence that merely BEGINS with a gate token — the shape
    is `gate:<token> VERIFIED <tree> host=<node>. <prose>`, never a bare sha,
    and the prose runs to the evidence budget. Fed to `merge-base` it is UNKNOWN
    for every row, and UNKNOWN fails closed — so bound there this sweep would
    have selected NOTHING, silently, forever, with a green suite, because a
    fixture supplies its own `verdict_ref` in whatever shape its author
    imagined. Only the live population could refute it.

    PATCH IDENTITY COUNTS HERE, unlike the `resolved` door's ancestry-only
    rung, and the difference is the QUESTION. There it was "did this exact
    object reach trunk"; here it is "is the work finished" — and the
    historical rebases and cherry-picks carried patch-identical work under
    different objects. Today's exact-sha merges instead preserve ancestry;
    patch identity still recognises older finished chains that ancestry misses.

    POLARITY IS DELIBERATELY NOT A RUNG. The fact being annotated — that this
    parent was superseded — was declared by the successor's author at write
    time; the successor's REVIEW outcome does not revoke it. Landedness is
    what proves the parent is finished, and a tip nobody approved does not
    reach trunk."""
    if not isinstance(kid, dict) or kid.get("status") != "verdict":
        return False
    tip = str(kid.get("reviewed_tip") or "").strip().lower()
    repo = kid.get("repo_id")
    if not dispatches._FULL_TIP.fullmatch(tip) or not isinstance(repo, str) \
            or not os.path.isdir(repo):
        return False
    # ONE REPOSITORY'S TRUNK PROVES NOTHING ABOUT ANOTHER'S ROW. Everything
    # below measures the KID against the KID's trunk, and the caller then
    # annotates the PARENT — so without this the sweep discharges a row in
    # repo A on the strength of work that landed in repo B. Mechanism A
    # settled the identical invariant for carriers (spec A-REPO, the
    # `cross-repo` proof value); this is that rung applied to the sweep,
    # which never had it.
    #
    # repo_id IS THE KEY, NEVER THE PROJECT LABEL: `_repo_project` is NOT
    # injective, and the live ledger proves it rather than the docs
    # asserting it — TWO checked-out copies of one upstream sit at
    # different paths whose BASENAME is identical, so both derive the
    # same label. A label comparison would call those one repository.
    #
    # FAIL CLOSED ON ABSENCE, because this authorizes a WRITE: a parent whose
    # repo_id is missing or unreadable is not proven same-repo, and unproven
    # is not permission. Measured 2026-08-05: 667 live parent/successor pairs,
    # ALL same-repo, zero cross-repo and zero missing — so this refuses
    # nothing that happens today and refuses the first thing that does.
    # THE DEFAULT IS A REFUSAL, NOT A SKIP. `parent=None` would have made
    # "caller forgot" indistinguishable from "no parent to check", and the
    # forgetting is silent — a future consumer would inherit zero protection
    # and nothing would say so. A sentinel makes the omission FAIL CLOSED, so
    # the guarantee is a contract of this rung rather than of whoever calls it.
    prepo = parent.get("repo_id") if isinstance(parent, dict) else None
    if not isinstance(prepo, str) or not prepo or prepo != repo:
        return False
    try:
        from . import vcs
        be = cache.get(repo)
        if be is None:
            be = cache[repo] = vcs.backend(repo)
        return be.landed_state(repo, tip, be.trunk_ref(repo)) in (
            vcs.ANCESTOR, vcs.PATCH_EQUIVALENT)
    except Exception:                       # a write this authorizes fails CLOSED
        return False


def superseded_parent_sweep(apply=False):
    """([(parent_id, successor_id)], err) — OPEN rows a successor supersedes
    that carry no annotation yet AND whose successor's work is FINISHED.
    Dry-run by DEFAULT; `apply` annotates each exactly as the write path does.

    AUTHORIZED BY PREDICATE, NEVER BY AN ID LIST (integrator ruling
    2026-08-04): "open + successor holds a verdict + the successor's work is
    ON TRUNK". `_successor_finished` is that last clause and carries the
    argument. A predicate is also what makes this safe to run UNATTENDED — an
    allowlist for a shape-matching selector is stale before it is typed.

    SCOPED TO PRESENTED-AS-ACTIONABLE ROWS, on the integrator's ruling and
    against my own first instinct. Measured 2026-08-04: 124 rows are
    structurally unterminated (a successor exists, no close_reason), but only
    OPEN rows are OFFERED by any enumeration surface — the lr projection
    already suppresses superseded parents, and ZERO appear there. The brief's
    harm is "told seats to redo finished work", a claim about ENUMERATION: a
    row no surface offers has told nobody anything. The 124-row bulk write was
    REFUSED on the record; append-only writes are justified by OCCURRING harm.

    ANNOTATES, NEVER CANCELS — the same law the write path learned the hard
    way: a BUILD parent must keep its status so `landed`/`discharged` can close
    it on its successor's PROOF.

    IDEMPOTENT BY CONSTRUCTION, not by a flag: it selects rows WITHOUT
    `superseded_by`, and applying sets it, so a second run selects nothing."""
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    succ = {}
    for r in current.values():
        if isinstance(r, dict) and r.get("supersedes"):
            succ.setdefault(str(r["supersedes"]), []).append(r["id"])
    shaped = sorted((r["id"], sorted(succ[str(r["id"])])[0])
                    for r in current.values()
                    if isinstance(r, dict) and r.get("status") == "open"
                    and not r.get("superseded_by")
                    and succ.get(str(r.get("id")) or ""))
    cache = {}
    hits = [(pid, kid) for pid, kid in shaped
            if dispatches._successor_finished(current.get(kid), cache,
                                   parent=current.get(pid))]
    if not apply:
        return hits, None
    path = dispatches.ledger_path()

    def attempt(txn):
        # PER TRY: a refusal counted on a read the write then discarded must
        # not be counted twice.
        done, refused = [], []
        if not txn.held:
            return done, "ledger unwritable (%s) — sweep NOT recorded" % path
        fresh, unavailable = dispatches.snapshot()
        if unavailable:
            return done, "dispatch ledger unavailable: %s" % unavailable
        for pid, kid in hits:
            parent = fresh.get(pid)
            if not isinstance(parent, dict) or parent.get("superseded_by"):
                continue            # re-checked on the read the write binds
            # A PARENT THIS HELM CANNOT READ IS NOT ANNOTATED, AND IS NAMED.
            # One such row refuses only itself: the sweep's other parents are
            # rows this helm reads in full.
            refusal = dispatches.unknown_kinds_refusal(parent)
            if refusal:
                refused.append(refusal)
                continue
            if not txn.append({
                    "v": 3, "event": "superseded",
                    "seq": (parent.get("seq") or 0) + 1,
                    "id": pid, "ts": pk.now_ts(), "successor": kid}):
                return done, "ledger unwritable (%s)" % path
            done.append((pid, kid))

        def finish():
            return done, ("; ".join(refused) if refused else None)
        return txn.then(finish)
    return dispatches._ledger_write(attempt, path)


def rebind_room_fence(row, old_recipient):
    """[{lane, holder, remaining_s}] — rooms the OLD recipient still holds for
    this row's lane FAMILY. WARN-only data; this never releases anything.

    THE SURPRISE IT EXISTS TO END, measured 2026-08-04: a rebind moves the
    OBLIGATION and leaves the WORKTREE LEASE with the old recipient. Row
    069406da7cf6 was rebound to a live seat while the room stayed fenced under
    the WALLED seat it came from, so the new builder could not start the work
    they had just been handed. The integrator's fix is a FRESH ROOM (`<lane>-r2`)
    — one line, no force-release, no risk to a walled seat's tree — and that
    workaround costs a LANE-LABEL/BRANCH DIVERGENCE the builder must be told
    about: the row's lane label keeps naming a branch that will never contain
    the work, so every surface resolving lane->branch reads the wrong one.
    `dispatch send` catches it ("--ref is NOT one of lane X's own commits") and
    nothing downstream does.

    WHY WARN AND NOT RELEASE (integrator ruling, #203): a walled seat is not a
    dead one, and its room may hold real work in the general case. Transfer is
    a separate lane. This rung only makes the fence VISIBLE at the moment the
    rebind is decided, which is the moment the information is free.

    Every reader here is prior art, deliberately: `_lane_family_names` already
    walks the -rN stems, `claims_list` is the ONE scrubbed publish boundary for
    holder/remaining, and `_lanes.resource` owns the key shape. Re-deriving any
    of them would be a second spelling of an identity that must stay single."""
    lane = str(row.get("lane") or "").strip()
    gitdir = str(row.get("repo_id") or "").strip()
    if not lane or not old_recipient:
        return []
    try:
        from . import seats
        from .work import _lanes
        from . import landreq
    except Exception:                       # noqa: BLE001 — a warn never raises
        return []
    root = os.path.dirname(gitdir.rstrip(os.sep)) if gitdir else ""
    if not root:
        return []
    # DIRECTION MATTERS AND THE FIRST CUT HAD IT BACKWARDS. I pre-computed
    # `_lane_family_names(lane)` and looked those resources up — but that walks
    # from a name to BROADER stems, while the fresh-room workaround creates
    # NARROWER ones (`<lane>-r2`). So the rung went quiet for exactly the rooms
    # the workaround mints, which is precisely when it matters. Caught by the
    # -rN test the ruling asked for. The fix is to ask the question of each
    # HELD room instead: does its lane stem-match the row's? `_stems_match` is
    # generous by design ("exact equality always; containment either way above
    # the floor"), and over-matching here costs one read of a warn nobody has
    # to act on, while under-matching costs the whole rung.
    prefix = _lanes.resource(root, "")
    try:
        mine = landreq._stem(lane)
        held = seats.claims_list()
    except Exception:                       # noqa: BLE001 — a warn never raises
        return []
    # SHAPE MEASURED, NOT ASSUMED: claims_list() returns a LIST of
    # {resource, holder, fence, remaining, liveness, stale}. The first cut of
    # this read a `remaining_s`/`left` key that does not exist and hedged on a
    # dict-vs-list return that never happens — both would have produced a
    # silent None rather than an error, which is the failure mode a warn rung
    # can least afford: it would print a fence with a blank TTL and read as
    # noise.
    out = []
    for c in (held or []):
        if not isinstance(c, dict) or c.get("holder") != old_recipient:
            continue
        res = str(c.get("resource") or "")
        # SCOPED TO THIS REPOSITORY by the resource prefix: another project's
        # identically-named lane is not this rebind's business, and a warn that
        # names someone else's room is the kind that gets skimmed.
        if not res.startswith(prefix):
            continue
        if landreq._stems_match(mine, landreq._stem(res[len(prefix):])):
            out.append({"lane": res, "holder": c.get("holder"),
                        "remaining": c.get("remaining"),
                        "liveness": c.get("liveness")})
    return out


def _rebind_disown_child(child_id, why):
    """Cancel a successor that must NOT outlive the source it replaced, and
    say what actually happened to it.

    A rebind writes TWO rows under TWO separately-acquired locks. Whenever the
    second write does not happen, the first one has already created a child
    that claims an obligation which never moved. Leaving it OPEN is the #178
    stranding: a row telling a seat it owes work nobody can discharge.

    THE REASON IS CLAMPED, AND THAT CLAMP IS LOAD-BEARING. mark_cancel runs
    _clean(reason, "cancel reason", 256), which refuses on length AND on any
    control character. The residual path used to interpolate the source's own
    error text into this reason — and that error is exactly where a long repo
    path or an embedded newline lives ("ledger unwritable (<278-char path>)"),
    so the child's cancel was REFUSED precisely in the failure this function
    exists to clean up, leaving BOTH rows open (found in review of
    the #178 rebind-race lane).
    Callers now pass a bounded reason AND the clamp holds the floor, so no
    future caller can reintroduce the fault from a distance.

    This REPORTS rather than assumes. The child's own cancel can fail for other
    reasons too (the ledger that refused the source's cancel is the same file),
    and an abort that swears the child is gone when it is still OPEN would hide
    exactly the row an operator has to go clean up by hand.
    """
    why = " ".join(str(why or "").split())        # newlines/controls -> spaces
    if len(why) > dispatches._CANCEL_REASON_CAP:
        why = why[:dispatches._CANCEL_REASON_CAP - 1] + "…"
    # THE CHILD MAY ALREADY HAVE CHILDREN. If it was itself rebound before this
    # cleanup ran, cancelling only the id we were handed cancels an INTERMEDIATE
    # and leaves the grandchild OPEN, still claiming an obligation that never
    # moved — the same stranding one generation down (an adversarial sweep
    # of the #178 rebind-race lane). Walk the whole descent and cancel every row
    # still able to be cancelled; a chain that cycles or runs away stops here and
    # is REPORTED rather than silently half-cleaned.
    snap = dispatches.snapshot()[0] or {}
    chain, sid, seen, unreached = [], str(child_id), set(), None
    while sid:
        if sid in seen:
            unreached = "the chain repeats at %s" % sid[:12]
            break
        seen.add(sid)
        chain.append(sid)
        kid = snap.get(sid)
        if not isinstance(kid, dict):
            # We cannot read this row, so we cannot know what lies BEYOND it.
            # Anything past here is an unwalked frontier and must be reported.
            unreached = "%s is unreadable, so its descendants were not walked" \
                        % sid[:12]
            break
        nxt = kid.get("superseded_by")
        sid = str(nxt) if nxt else None
    done, failed = [], []
    for rid in chain:
        row = snap.get(rid)
        if isinstance(row, dict) and row.get("status") not in dispatches.CANCELLABLE_STATES:
            # PRESERVE A LEGITIMATE TERMINAL STATE. A descendant that reached a
            # VERDICT earned it, and a cleanup has no business rewriting real
            # history to CANCELLED; the invariant is that no reachable
            # descendant remains OPEN, not that every one reads cancelled.
            continue
        _, err = dispatches.mark_cancel(rid, why)
        (failed if err else done).append(
            "%s (%s)" % (rid[:12], err) if err else rid[:12])
    # A TRUNCATED WALK IS A FAILED CLEANUP, NEVER A SUCCESS LIST. Reporting
    # "cancelled A, B, C" while an unwalked frontier is still OPEN is the same
    # laundering this whole lane exists to remove, one level up: the caller
    # believes the obligation is contained when it is not.
    if failed or unreached:
        parts = []
        if failed:
            parts.append("could NOT cancel %s — still OPEN, cancel by hand"
                         % ", ".join(failed))
        if unreached:
            parts.append("CLEANUP INCOMPLETE: %s" % unreached)
        if done:
            parts.append("cancelled " + ", ".join(done))
        return "; ".join(parts)
    if not done:
        return "child %s was already terminal, nothing to cancel" % child_id[:12]
    return "cancelled " + ", ".join(done)


def _brief_travel_note(old, new):
    """One sentence for the rebinder about WHAT REACHED the new recipient.

    THE OLD NOTE FIRED UNCONDITIONALLY and said one thing in three different
    situations, only one of which it described. It told the operator "no
    recoverable DM message body traveled ... (send rows retain only a hash)"
    even when the superseded row was an `add` that never had a DM in the first
    place — nothing was lost, and the note asked for a re-brief anyway.

    Three states, three sentences, and the two ABSENCES are not merged:
      the brief travelled          — say so, and whether it was truncated
      there was never a brief      — KNOWN-EMPTY, nothing is owed
      the row predates storage     — UNKNOWN, and this is the one that owes a
                                     re-brief (`message_hash` present, no text)
    """
    # THROUGH THE REFERENCE DOOR. A rebind is the one move whose whole purpose
    # is that the instruction arrives with the obligation, so it is the last
    # place that may report a bounded copy as "the original brief".
    brief, _why, problem = dispatches.brief_of(new)
    if brief and not problem and new.get("brief_ref") is not None:
        return ("the ORIGINAL BRIEF TRAVELED WHOLE with this rebind — %d "
                "UTF-8 bytes, stored by reference and read back proven, so @%s "
                "starts with the instruction, not just a lane label."
                % (len(brief.encode("utf-8")), new["recipient"]))
    if brief:
        return ("the ORIGINAL BRIEF TRAVELED with this rebind — %d characters "
                "stored on %s%s, so @%s starts with the instruction, not just "
                "a lane label.%s"
                % (len(brief), new["id"][:12],
                   " (TRUNCATED; the stored text says exactly where it stops)"
                   if dispatches.BODY_TRUNCATED_MARK in brief else "",
                   new["recipient"], (" " + problem) if problem else ""))
    _body, why = dispatches.body_of(old)
    if why == dispatches.BODY_NONE or not old.get("message_hash"):
        # KNOWN-EMPTY. An `add` row carries no DM by construction, so its hash
        # is None — and that is true of an add row from BEFORE body storage too,
        # which is why the hash decides here and not the key's presence.
        return ("no brief travelled because THERE NEVER WAS ONE: %s was filed "
                "with `dispatch add`, which sends no DM. @%s has the lane, the "
                "ref and the rebind reason, and nothing was lost."
                % (old["id"][:12], new["recipient"]))
    return ("NO recoverable DM message body travelled with this rebind: %s "
            "PREDATES body storage — it carries a message_hash and no text, so "
            "its brief is UNKNOWN to this ledger rather than empty. Re-brief "
            "@%s directly, or they start without the original brief."
            % (old["id"][:12], new["recipient"]))


def rebind(rid, to, reason=None, force=False, repo=None, notify=True,
           pair_meld=None):
    """Move one OPEN dispatch to a new recipient, atomically in intent:
    cancel the old row as REBOUND (not abandoned — the trail says where the
    obligation went) and open a NEW row preserving lane/ref/kind/note/deadline,
    to `to`.

    EVIDENCE-GATED (council 0.3 gap G1): the default path REFUSES unless
    either proxywatch measures the current recipient starved/hung/down OR
    autocompact proves its fresh context window exhausted. The arms are
    independent: an unreadable proxywatch is not evidence by itself and never
    suppresses a current context measurement; only UNKNOWN across BOTH arms
    refuses. The manual 3-4 step re-route a judgment seat did by hand becomes
    one verb, and a verb that can later be CALLED automatically must require a
    measured signal by default. --force overrides with a mandatory reason (a
    judgment seat's prerogative, recorded).

    AND A LIVE READER IS REFUSED EARLIER, BY NAME (helm/rebind_liveness.py).
    The evidence gate asks whether the recipient CAN act; it answers "not
    measurably unable" for every healthy seat and names nobody, then
    advertises the override in the same breath. A row whose recipient is
    measurably live AND measurably mid-read is therefore refused by its own
    rung first, naming the reader, the evidence that they are live and the
    row's state. Under --force that rung writes instead of refusing: the
    recorded reason says it WAS an override and names the seat the row came
    from, which is the one sentence the party who lost it needs.

    The two ledger writes are NOT transactional. The linked successor is
    appended first so the writer's locked duplicate check can refuse without
    cancelling the source row; only then is the source cancelled. Between the
    writes both rows may be OPEN, but the supersedes edge makes the child the
    one active frontier rather than inventing unrelated work. A cancel failure
    is surfaced with the recorded child id for bounded recovery.

    THE NEW READER IS INVITED INTO THE CHAIN'S PAIR MELD, never a new room: a
    reader that went dark leaves the conversation where the next reader
    arrives, and the row stays owed (falsifier (e)). `pair_meld` asks for the round
    whatever the chain's state (the CLI); left None, the round opens exactly
    when the chain's pair meld has already opened, so an automated reassign
    continues a conversation and never invents one."""
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    row, err = dispatches._resolve_row(current, rid)
    if err:
        return None, err
    if row["status"] != "open":
        return None, ("dispatch %s is %s — only an OPEN row can be rebound"
                      % (row["id"], row["status"]))
    # OPEN IS NOT THE SAME AS OWED. A parent whose successor already carried
    # the work to a verdict is still status=open, so rebinding it minted a NEW
    # sibling against finished work and resurrected a discharged obligation.
    # Resolved against the SAME snapshot the row came from, so the answer
    # cannot drift between the read and the write.
    _held_by = dispatches.carrier(row, current)
    if _held_by is not None:
        # THE PHRASE IS LOAD-BEARING. The writer's own duplicate-fork refusal
        # already said "old row remains OPEN", and an operator reading a
        # refusal needs to know the parent was not left in some half-state.
        # This guard REPLACES that path because it is strictly broader and
        # fires before any write: measured, the writer refuses only for an
        # OPEN successor, so a parent whose successor reached a VERDICT was
        # rebound and MINTED A SIBLING against finished work with no refusal
        # at all. Same sentence, earlier, and now covering the terminal case.
        return None, ("rebind NOT started; old row remains OPEN because %s "
                      "already carries this work (status %s). Rebinding would "
                      "mint a sibling against work that has moved on; act on "
                      "that row instead."
                      % (str(_held_by.get("id"))[:12], _held_by.get("status")))
    to, terr = dispatches._recipient_operand(to)
    if terr:
        return None, terr
    if to == row.get("recipient"):
        return None, "dispatch %s is already addressed to %s" % (row["id"], to)
    # THE LIVE-READER RUNG, in a sibling module because this file is past its
    # split budget. Asked BEFORE the capacity gate below because it is the
    # more specific question: the capacity gate can only say the recipient is
    # not measurably unable to act, which is true of every healthy seat and
    # names nobody. Asked on the --force path too, where it produces nothing
    # but the sentence recorded on the ledger — the reader who loses the row
    # is the one party the cancel reason must be legible to.
    reading = rebind_liveness.live_reader(row)
    if reading and not force:
        return None, rebind_liveness.refusal(reading)
    if not force:
        evidence, unavailable_note = dispatches._recipient_evidence(row["recipient"])
        if not evidence:
            why = ("; proxywatch: %s; context arm produced no fresh-exhaustion "
                   "evidence" % unavailable_note) if unavailable_note else (
                       " — neither proxywatch starvation/hang nor fresh context "
                       "exhaustion was measured")
            return None, ("rebind REFUSED: %s is not measurably unable to act%s. "
                          "The default path requires either measured proxy "
                          "starvation/hang or fresh context exhaustion so the "
                          "verb can later be called automatically; a judgment "
                          "seat overrides with --force --reason '...'"
                          % (row.get("recipient"), why))
        reason = reason or evidence
    if not (reason or "").strip():
        return None, "rebind needs a reason (--reason, or the measured one)"
    if reading:
        # Only reachable under --force: the refusal above returns on every
        # other path. The reason is rewritten rather than appended to so the
        # override survives the cap the cancel reason is cut at.
        reason = rebind_liveness.override_reason(reading, reason)
    # A rebind moves the SAME obligation, so its repo identity is immutable:
    # --repo may only spell an alternate path to the repo already recorded on
    # the row. A same-tip foreign clone resolves to a different .git and is
    # refused BEFORE the cancel — a silent identity swap at rc0 was the
    # finding on 6a8f9530. On a validated row the RECORDED root is what
    # flows onward (checking the caller's path and then re-resolving that
    # same path in add() would be a check/use hole — a retargeted symlink
    # between the two resolutions swaps the identity the check just blessed).
    # Rows older than repo_id carry none and keep the caller-supplied path
    # (legacy, never guessed).
    repo_path = str(row.get("repo_id") or "")[:-5] or None
    if repo is not None:
        want = row.get("repo_id")
        info = dispatches._repo_info(repo) if want else None
        if want and (not info or info["repo_id"] != want):
            return None, ("rebind REFUSED: --repo %s resolves to %s but the "
                          "obligation is bound to %s — the same obligation "
                          "cannot change repos; name a path inside the "
                          "recorded repo or re-dispatch"
                          % (repo, (info or {}).get("repo_id", "no git repo"),
                             want))
        if not want:
            repo_path = repo
    reason = ("rebound to %s: %s" % (to, str(reason).strip()))[:256]
    # The new row is an `add` (persist + public notice), never a `send`: this
    # verb moves an obligation, it does not deliver a second DM. What it CAN do
    # now is carry the brief — `_preserve_origin` copies the parent's stored
    # `message_body` onto the child, so a row sent after 2026-08-27 arrives with
    # its instruction attached instead of a lane label and a ref. Nothing is
    # invented: a parent that stored no body still hands the child None, and the
    # CLI says WHICH of the two absences it is (`_brief_travel_note`).
    #
    # AND THE AUTHOR TRAVELS WITH IT. Before `_preserve_origin` the child was
    # stamped with whoever ran the rebind, which `landreq` projects straight
    # into `author` — a silent re-authoring of the land request. The sender is
    # INHERITED off the parent row (never accepted as an argument, see
    # `_inherited_origin`) and the mover is recorded as `acted_by`.
    # The ref validates against the row's OWN repo (repo_id is the .git
    # dir recorded at dispatch time) — the caller's cwd is irrelevant.
    # SUPERSEDES, never --new-work: a rebind is the SAME OBLIGATION addressed to
    # a different seat, and stamping it as new work would fabricate a second
    # piece of work out of one — the precise lie the required field exists to
    # prevent, committed by helm itself. Append the child FIRST: its writer lock
    # sees any existing live child and refuses before the old row is touched.
    # Rebind's --force is only recipient-health authority; it must never double
    # as duplicate-fork authority. The branch binding moves with the SAME
    # obligation too: preserve the parent's write-time evidence (including
    # None), rather than manufacturing identity from today's topology.
    # WARN-ONLY, COMPUTED BEFORE THE MOVE because it is a fact about the OLD
    # recipient and the row stops naming them the moment the cancel lands.
    # Never raises and never blocks: a rebind whose fence probe fails is still
    # a correct rebind, and #203 is explicitly a warn rung, not a gate.
    fence = dispatches.rebind_room_fence(row, row.get("recipient"))
    if pair_meld is None:
        try:
            from . import review_door
            if review_door.pair_room_if_open(row, current):
                pair_meld = {}
        except Exception:                               # noqa: BLE001
            pair_meld = None              # a round we cannot name is not owed
    new, add_err = dispatches.add(
        to, row.get("lane"), ref=row.get("tip") or row.get("ref"),
        note=row.get("note"),
        deadline_s=int(row["deadline_s"]) if row.get("deadline_s")
        else None,              # no stated deadline -> the new row's KIND decides
        repo=repo_path, kind=row.get("kind"), notify=notify,
        supersedes=row["id"], _reason=True, _refusal_door="rebind",
        _ref_branch=row.get("ref_branch"), _preserve_origin=dispatches._MOVE_MINT,
        pair_meld=pair_meld)
    if new is None:
        return None, ("rebind NOT started; old row remains OPEN because the "
                      "replacement was refused: %s"
                      % (add_err or "dispatch NOT recorded"))
    # THE TWO-LOCK RACE (#178). `add` appended the child under the WRITER lock
    # and released it; `mark_cancel` takes the EVENTLEDGER lock separately. A
    # verdict landing BETWEEN the two writes terminalizes the source, the cancel
    # is then REFUSED, and the child survives OPEN pointing at a source nobody
    # can discharge — the #173/#177 stranded-obligation class arriving by RACE
    # instead of by a missing verb.
    #
    # ONE SHARED LOCK IS NOT THE CURE. Both writes take fcntl.flock(LOCK_EX) on
    # the SAME sibling lock file — eventledger.locked() opens `<ledger>.lock`,
    # never the ledger itself — through SEPARATE fds, and flock is per-fd:
    # holding the first across the second blocks forever on our own lock. The window is
    # inherent to two independently-locked writes, so the honest cure is to
    # notice and refuse to leave a child behind.
    #
    # The predicate is CANCELLABLE_STATES, NOT `status != "open"`: a source that
    # went HELD mid-rebind is still perfectly cancellable, and aborting on it
    # would kill a rebind that was about to succeed. An unreadable row fails the
    # test too, which is the safe direction — a source we cannot see is a source
    # we cannot cancel.
    fresh = dispatches.snapshot()[0].get(row["id"]) or {}
    if fresh.get("status") not in dispatches.CANCELLABLE_STATES:
        reached = fresh.get("status") or "unreadable"
        fate = dispatches._rebind_disown_child(
            new["id"], "rebind aborted: source %s reached %s mid-rebind"
            % (row["id"][:12], reached))
        return None, ("rebind aborted: dispatch %s reached %s while the "
                      "replacement was being written, so it can no longer be "
                      "cancelled; %s" % (row["id"][:12], reached, fate))
    cancelled, err = dispatches.mark_cancel(row["id"], reason)
    if err:
        # The recheck above narrows this window, it does not close it: the
        # recheck reads WITHOUT the lock, so the source can still terminalize
        # between that read and the cancel's own acquisition. Whatever the
        # reason the cancel did not happen, the obligation did not move, so the
        # child must not go on claiming it did.
        # BOUNDED BY CONSTRUCTION: the source's own error does NOT go in the
        # cancel reason. It is unbounded prose that can carry a long path or a
        # newline, and mark_cancel refuses on either — which used to make this
        # disown fail in exactly the case it exists for. The full error still
        # reaches the caller in the message returned below, which has no cap.
        fate = dispatches._rebind_disown_child(
            new["id"], "rebind incomplete: source %s was not cancelled"
            % row["id"][:12])
        return {"old": row, "new": new, "reason": reason,
                "room_fence": fence}, (
            "replacement %s was recorded, but old row %s was NOT cancelled: "
            "%s; %s" % (new["id"], row["id"], err, fate))
    return {"old": cancelled, "new": new, "reason": reason,
            "room_fence": fence}, None


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
