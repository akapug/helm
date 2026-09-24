#!/usr/bin/env python3
"""helm seats — the UNTESTED-COMPOSITION stop rung (b4).

ITS OWN MODULE BECAUSE THE SPLIT CONTRACT SAYS SO. `seats_stop_signals` was
already at 914 lines against a 1000-line budget that `test_seats_split_contract`
enforces as a ratchet, and this rung is the only one whose SIGNAL lives outside
the seats package entirely — it reads `helm.work`'s lane machinery and the gate
receipt ledger, neither of which any other stop signal touches. Grouping it with
the cheap per-turn probes would have put the ladder's most expensive read in the
file whose docstring promises everything in it is cheap.

WHAT IT IS. Owner, 2026-08-23: "Two green halves with an untested composition —
this seems to be the biggest failure mode across projects, please /learn
globally to prevent that via whispers and or stophooks." The whisper half is the
store's `composition-untested-on-split` reflex; this is the stop half, and it
exists because a reflex fires on LANGUAGE — it can only reach a seat that
already said the word "split". The expensive shape is the one nobody named.

THE LIMITATION, FIRST, BECAUSE A READER MUST MEET IT BEFORE THE CODE: THIS RUNG
IS KEYED ON THE WORKTREE, SO TWO SEATS SHARING ONE ROOM ARE INVISIBLE TO IT.
They produce no comparable pair — one room is one branch, one authored set, one
half — so two seats banking adjacent halves inside a single checkout is exactly
the two-green-halves case and exactly the case this cannot see. That is not a
corner: helm's own SHARED CHECKOUT is where every claude-native seat works, and
the roster records FOUR seats (three claude seats and the
integrator) with their cwd inside it, three of them with live processes
there right now. Per-worktree is still the right key — it is the only one that
carries a branch, a diff and a merge — and the shared-room case wants a
different mechanism, not a wider version of this one. What this rung owes it is
DISCLOSURE, and `_cotenancy_warn` pays that: a seat stopping in a room several
seats answer to is told, once, that the rung is blind between them, because a
can-tell-nothing must never read as a clean bill.
"""

import hashlib
import os
import re

from . import chat, pk, projscope, record
from .seats_common import SEAT_BYTES, STATUS_BYTES, _clip, _scrub
from .seats_identity import safe_cwd
# Downward edge, like every other rung: this READS what stop_signals maintains
# (the latch path, the kill-switch reader, the lane-name shape) and writes none
# of it. `helm.work` is imported LAZILY inside the functions, because that
# package imports `helm.seats` and a module-level edge here would be a cycle.
from .seats_cursor import _probe_stop_latch, _write_stop_latch
from .seats_stop_signals import _SPIRAL_LANE, _off, _stop_fp_path

SEAM_LATCH = "stopseam"
# One ref-name shape for the whole stop ladder. It admits `/` because a
# BRANCH is the identity here, not a helm lane label: helm lanes render as
# `lane/<name>` and the sibling project branches as `codex/<name>`, and the trailer a
# seat writes has to name something both repos have.
_SEAM_REF = _SPIRAL_LANE
SEAM_FILES_SHOWN = 2          # one seam is the point; a list is wallpaper
# The room path is INTERPOLATED INTO A COMMAND THE SEAT IS TOLD TO RUN, so it
# gets the beacon block's treatment rather than the sink's: validate a safe
# shape here, and if it does not clear, say plainly that no command is offered
# instead of pasting a laundered path that would run somewhere else.
_SEAM_PATH = re.compile(r"[A-Za-z0-9._/@+=-]{1,400}\Z")


def _seam_rooms(session, seat, cwd=None):
    """({my absolute room paths}, root) — the rooms THIS seat is working in.

    CWD FIRST, LEASE SECOND, AND THAT ORDER IS THE CORRECTION. The first cut
    resolved "my lane" from the helm claims ledger alone, which measured empty
    for the project that asked for this rung: the sibling project carries zero work
    claims and zero lane rooms, so a seat there could never be party to a seam
    and the gate returned before it looked. The room a seat is STANDING IN is
    derivable from git in every repo — the Stop hook's own JSON carries the
    cwd — so that is the primary, and a held lane lease only ADDS rooms for the
    delegated-build case where nobody's cwd is inside the room.

    SESSION-BOUND ON THE LEASE LEG, unchanged: a display name alone must never
    reach a rung that blocks. Rows match on the session that minted them or on
    this seat as holder, the pair the lease arm already resolves, because the
    receiver of a handed lane is still the seat that would be composing."""
    try:
        from .work import _lanes
        from .seats_common import claims_path, _sweep
        # THE OBSERVATION CWD, NOT THIS PROCESS'S — the hook JSON carries the
        # stopping seat's directory and `_lease_worktree` already resolves lane
        # rooms through it for exactly this reason. A Stop hook can run from
        # anywhere; the room it is about is the one the seat is standing in.
        where = cwd or safe_cwd()
        root = _lanes.find_root(where)
    except projscope.Expired:
        raise
    except Exception:
        return set(), None
    if not root:
        return set(), None
    mine = set()
    # THE ROOM CONTAINING cwd, by longest-prefix over the registry rather than
    # by `git rev-parse --show-toplevel`: one git spawn saved, and the registry
    # is the same row set `seam_rooms` will pair against, so the two cannot
    # disagree about which room a path belongs to.
    try:
        here = os.path.realpath(where).rstrip(os.sep)
        best = ""
        for w in _lanes.worktrees(root):
            p = os.path.realpath(w["path"]).rstrip(os.sep)
            inside = here == p or here.startswith(p + os.sep)
            if inside and len(p) > len(best):
                best = os.path.abspath(w["path"])
        if best:
            mine.add(best)
    except projscope.Expired:
        raise
    except Exception:
        pass
    project = os.path.basename(root.rstrip(os.sep))
    try:
        rows = _sweep(pk.read_json(claims_path(), {}) or {})
    except projscope.Expired:
        raise
    except Exception:
        rows = {}
    for res, v in sorted(rows.items()):
        if res == "_fence" or not isinstance(v, dict):
            continue
        parts = str(res).split(":", 2)
        if len(parts) != 3 or parts[0] != "worktree" or parts[1] != project:
            continue
        ours = (session and str(v.get("session")) == str(session)) or \
            (seat and str(v.get("holder") or "").strip() == str(seat).strip())
        if ours and parts[2]:
            try:
                mine.add(os.path.abspath(_lanes.lane_path(root, parts[2])))
            except projscope.Expired:
                raise
            except Exception:
                continue
    return mine, root


def _join(*parts):
    """The warn slot takes ONE string, and two findings must not eat each other.

    `_seam_gate` can have a blind-spot disclosure AND a measurement warning in
    the same stop, and returning either alone would silently drop the other —
    the shape this module exists to complain about, committed by this module.
    None-safe both ways; returns None when nothing has anything to say."""
    kept = [p for p in parts if p]
    return "\n".join(kept) if kept else None


COTENANCY_LATCH = "stopseamshare"

# ARMED-BUT-UNCOMMITTED latches for THIS process, in order, as
# (latch path, fingerprint, THE TEXT THAT MUST BE SEEN). Process local and
# deliberately not durable: a stop hook is one short-lived process, so anything
# still pending when it exits was never printed and must stay un-latched so the
# next stop re-arms it. See `_cotenancy_warn` for why the commit cannot happen
# at construction.
#
# THE TEXT IS THE THIRD MEMBER BECAUSE PROCESS DEATH WAS DOING THE WORK. The
# pair form relied on the hook exiting to discard an un-emitted arming, and two
# exits break that: the block exit returns without emitting, and the
# guard-could-not-run exit returns 0 the same way — so a later emission in the
# same process committed a fingerprint for a line nobody had printed, spending
# the arrangement and silencing it forever. That is the ORIGINAL defect through
# a second door. `commit_disclosures` now matches each arming against what was
# actually put on the stream, so the promise is kept by construction instead of
# by the process ending in time.
_PENDING_DISCLOSURES = []

# THE TEXTS THAT MUST REACH THE SEAT EVEN WHEN THE STOP REFUSES, declared at
# the ARMING because survival is a property of the FINDING and not of the exit
# that happens to be taken. A disclosure about what an instrument CANNOT SEE is
# true no matter which rung, if any, blocks; routing it onto a channel that one
# exit discards makes its delivery depend on somebody else's verdict.
#
# Drained by `commit_disclosures` on the same rule as the queue beside it: any
# emission ends the arming, so a text still listed here was never put in front
# of a reader and the next stop re-arms it.
_SURVIVES_REFUSAL = []


def survives_refusal(text):
    """Declare `text` deliverable on BOTH exits. -> the text, for chaining.

    THE LIST ABOVE IS THIS MODULE'S, AND A SECOND RUNG NEEDED IT. The seam
    rung armed its own disclosure by appending inline, which reads as a private
    detail of that rung rather than as the seam's contract — so the next rung
    with the same property (a finding true of the STOP rather than of any
    rung's verdict) had no name to reach for and would have grown a second
    spelling of the same list. One door, so the drain rule in
    `commit_disclosures` keeps covering every armer.

    IT IS NOT A LATCH AND IT SPENDS NOTHING. A caller that wants once-per-
    arrangement still queues a `_PENDING_DISCLOSURES` entry; a caller whose
    finding must repeat until the world changes simply calls this each stop.
    """
    if text:
        _SURVIVES_REFUSAL.append(text)
    return text


def _latch_dir_writable(path, seat, session):
    """Writable proof plus current seat-incarnation token, or None.

    NAMED FOR WHAT IT PROVES, WHICH IS LESS THAN THE COMMIT SUCCEEDING. It
    writes and removes a probe beside the latch, so it is a real write rather
    than a permission guess — `os.access` answers about mode bits and lies on
    a full or read-only filesystem, which is exactly when this matters. What
    it CANNOT prove is that the later commit will succeed: the disk can fill
    between here and there. That residual is honest and bounded — the line was
    already printed, so a failed commit means the disclosure repeats at the
    next stop rather than being silently spent, which is the safe direction.
    """
    try:
        return _probe_stop_latch(
            path + ".armprobe", seat, session=session)
    except OSError:
        return None


def emit_warns(warns, stream=None):
    """Print the ALLOW exit's advisories, THEN commit their latches. -> count

    ONE OPERATION BECAUSE THEY ARE ONE FACT. A once-per-arrangement latch may
    only be spent on a line that was actually SEEN, so the print and the commit
    cannot be separated without reintroducing exactly the defect this module
    was corrected for: a disclosure latched while being constructed, then
    suppressed by an exit that never printed it, and silent forever after.

    IT LIVES HERE RATHER THAN IN THE CLI because `seats_cli` is a FACADE under a
    split contract that says it only ever shrinks — and the contract caught this
    lane at 1014 lines against its 1000 budget. A facade holding a loop plus a
    rationale is the shape that contract exists to refuse; delegating is both
    smaller there and better here, where the reason can be stated in full.
    """
    return _emit(list(warns or ()) + _owner_chart(), stream)


def _owner_chart():
    """[chart] while the owner is away, else [] — the ALLOW exit's courtesy.

    IT LIVES ON THIS SIDE OF THE SEAM RATHER THAN IN THE CALLER, and that is
    not tidiness. `emit_blocks` does not call it, so "the chart never rides a
    refusal" stops being a rule somebody has to remember at a branch and
    becomes a property of which function you are in. The first version of this
    put the logic in seats_cli beside the clean-path return, where it was one
    edit away from being copied into the refusal branch — and every exit-2
    emission renders as a red "Stop hook error:", so a progress dashboard
    stapled to a blocker is the loudest possible version of that mistake.

    THE FACADE IS ALSO WHY. seats_cli is an extracted module under a 1000-line
    budget and it sat at 999; the 36 lines this replaced took it to 1035 and
    the split contract failed the gate, correctly. A facade holding a loop plus
    a rationale is the shape that contract exists to refuse, exactly as
    emit_warns' own docstring says two functions up.

    NEVER RAISES AND NEVER BLOCKS A STOP. A chart is a courtesy; if the board
    is unreadable, the marker unreachable or the renderer broken, the seat
    stops cleanly and says nothing. That is also why the away test comes first
    and costs one os.path.exists on the overwhelmingly common not-away path.
    """
    try:
        from . import ownerchart
        if not ownerchart.away():
            return []
        import os as _os, time as _time
        from . import board as _board, pk as _pk
        projscope.spend_or_raise("reading owner chart")
        path = _board.path()
        # THE CLOCK AND THE BOARD'S AGE ARE SUPPLIED HERE, NEVER READ INSIDE
        # THE RENDERER. A surface that reaches for its own time can make claims
        # nobody handed it; given neither value the renderer omits ages rather
        # than inventing them.
        try:
            as_of = _os.stat(path).st_mtime
        except OSError:
            as_of = None
        body = _pk.read_json(path, default=None) or {}
        projscope.spend_or_raise("rendering owner chart")
        chart = ownerchart.render(body, now=_time.time(), as_of=as_of)
        projscope.spend_or_raise("rendering owner chart")
        return [chart] if chart else []
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("seats_stop_seam._owner_chart", _swallowed)
        return []


def emit_blocks(blocks, stream=None):
    """Print the REFUSAL exit's blockers, THEN commit their latches. -> count

    THE SECOND EXIT, AND IT OWNS LATCHES TOO. `seats_cli` prints blocks and
    returns 2 while suppressing every advisory — the owner watched an evening
    of red "Stop hook error:" lines in 2026-07-31 and that suppression is
    correct. What was NOT correct is that the refusal exit spent nothing and
    committed nothing, so this rung's own latches were either written before
    anyone had seen the line (the block latch) or left armed for a later
    emission to bank (the disclosure latch). Both exits now do the same one
    thing: print, then commit exactly what was printed.

    A rung whose finding must survive this exit therefore puts that finding IN
    a block rather than beside it; the warn channel does not reach a reader
    here and never did.

    AND WHEN THE REFUSAL IS SOMEBODY ELSE'S, THAT IS THIS DOOR'S JOB. Folding
    the disclosure into a block only covers the stop where THIS rung refuses.
    The sibling case — this rung blind, no block of its own, another rung
    refusing — left the line on the warn channel, which this exit drops no
    matter whose verdict took it there, so a seat learned nothing about the
    place the instrument cannot look on exactly the stops where something else
    was already wrong. Delivery is decided HERE, at the one door the refusal
    exit passes through, rather than by each rung guessing which other rung
    might fire: a text armed as surviving is printed ahead of the blockers
    unless a blocker already carries it, and then it is spent by the same
    print-then-commit rule as everything else."""
    blocks = [b for b in (blocks or ()) if b]
    carried = "\n".join(blocks)
    return _emit([t for t in _SURVIVES_REFUSAL if t and t not in carried]
                 + blocks, stream)


def fallback_lines(lines, include_survivors=False):
    """Drain one interrupted response without writes or stale process state."""
    kept = [line for line in (lines or ())
            if isinstance(line, str) and line.strip()]
    if include_survivors:
        carried = "\n".join(kept)
        kept = [line for line in _SURVIVES_REFUSAL
                if isinstance(line, str) and line.strip()
                and line not in carried] + kept
    del _SURVIVES_REFUSAL[:]
    del _PENDING_DISCLOSURES[:]
    return kept


def _emit(lines, stream=None):
    lines = [ln for ln in (lines or ()) if ln]
    import sys as _sys
    out = stream or _sys.stderr
    for line in lines:
        projscope.spend_or_raise("publishing stop response line")
        print(line, file=out)
    projscope.spend_or_raise("committing stop response latches")
    return commit_disclosures("\n".join(lines))


def commit_disclosures(seen=""):
    """Write the latches whose lines were ACTUALLY PRINTED. -> count written

    `seen` IS WHAT WENT ON THE STREAM, and matching against it is the whole
    mechanism. An arming whose text is not in `seen` was not shown to anybody,
    so it is DISCARDED rather than written: the arrangement stays un-latched
    and the next stop re-arms it, which is the degradation this module already
    chose everywhere else. Discarding rather than leaving it queued matters
    because the queue outlives one stop inside a long-running process, and a
    later unrelated emission would otherwise bank it.

    Never raises: a latch that cannot be written leaves the disclosure
    un-latched, so it speaks again next stop — the same degradation an
    unwritable latch has always had, and strictly better than spending an
    arrangement on a line nobody read.
    """
    written = 0
    # THE SURVIVAL LIST DIES WITH THE ARMING IT BELONGS TO. Both are armed in
    # one breath and both mean "not yet seen", so an emission that ends one
    # ends the other; leaving it standing would let a later, unrelated refusal
    # in the same process print a disclosure that was already delivered.
    del _SURVIVES_REFUSAL[:]
    while _PENDING_DISCLOSURES:
        path, fp, text, seat, session, incarnation = \
            _PENDING_DISCLOSURES.pop(0)
        if text and text not in (seen or ""):
            continue                  # never emitted -> never spent
        projscope.spend_or_raise("committing stop response latch")
        try:
            written += bool(_write_stop_latch(
                path, seat, fp, session=session, incarnation=incarnation))
        except OSError as _swallowed:
            record.swallow("seats_stop_seam.commit_disclosures", _swallowed)
    return written


def _cotenancy_warn(rooms, mine, room, seat, session):
    """The DISCLOSURE this rung owes the case it cannot see, or None.

    A seat stopping in a room that SEVERAL seats answer to is told, once, that
    the seam rung is structurally blind between them. It is a WARN and never a
    block: nothing here is evidence that an untested composition EXISTS — only
    that this instrument could not have found one — and a guard may not wall a
    turn on its own blind spot. But it may not stay quiet either, because the
    alternative is that a can-tell-nothing reads exactly like a clean bill,
    which is the failure this whole module is about one level up.

    THE FACT IS ALREADY IN HAND, WHICH IS WHY THIS COSTS NOTHING. `seam_rooms`
    resolves each room's seat set from the roster's recorded cwd and the
    occupants' environ; more than one name in one room IS the co-tenancy
    signal, and an earlier cut computed it and threw it away by collapsing the
    set to a single holder-or-None.

    ROSTERED, NOT PROVEN PRESENT: the roster leg outlives the process it
    describes, so the named set can overstate who is there right now. The line
    says "answer to this room" rather than "are live in it", because
    understating a blind spot is the worse error and inventing precision about
    it is the other one.

    Latched per (room, seat set) so a standing arrangement speaks once per
    session and a NEW co-tenant re-arms it. An unwritable latch simply skips —
    a disclosure that cannot remember must not become per-stop wallpaper, and
    unlike a block there is nothing here to degrade TO.

    THE LATCH IS ARMED HERE AND COMMITTED ONLY AFTER THE LINE IS SEEN
    (the reason is a composition of two correct
    decisions). This function used to WRITE the fingerprint and then return
    the text. `seats_cli` deliberately suppresses advisories on the exit-2
    branch — Claude Code renders every exit-2 stop-hook emission as a red
    "Stop hook error:", and the owner watched that red line all evening on
    2026-07-31 — and justifies it by promising the advisory is RECOMPUTED on
    the re-stop that follows the cure. That promise held for every warn
    except this one: latching at construction spent the arrangement on a stop
    where the text was then suppressed, so the re-stop matched the latch and
    returned None FOREVER. Neither decision was wrong alone; together they
    destroyed the disclosure exactly when another rung fired. Committing after
    emission makes the suppression's own promise true for the first time, so
    printing on rc2 — which would reintroduce the owner's red line — is not
    needed to fix this."""
    shared = [r for r in rooms
              if r["path"] in mine and len(r.get("seats") or ()) > 1]
    if not shared:
        return None
    worst = max(shared, key=lambda r: len(r["seats"]))
    names = [n for n in worst["seats"] if _SEAM_REF.fullmatch(str(n))]
    if len(names) < 2:
        return None           # cannot be quoted inertly -> not said at all
    fp = hashlib.blake2b(("%s\x1f%s" % (worst["path"], "|".join(sorted(names))))
                         .encode("utf-8"), digest_size=8).hexdigest()
    path = _stop_fp_path(room, seat, session, kind=COTENANCY_LATCH)
    try:
        with open(path) as f:
            if f.read().strip() == fp:
                return None                # already disclosed this arrangement
    except OSError:
        pass
    # ARM, DO NOT COMMIT. Writability is proven NOW — an unlatchable
    # disclosure must still skip rather than become per-stop wallpaper, and
    # discovering that only after printing would leave the line un-latched and
    # repeating. But the fingerprint is banked in memory and written by
    # `commit_disclosures()`, which the ALLOW exit calls AFTER the warns are
    # actually printed. See the docstring for why the write cannot happen here.
    incarnation = _latch_dir_writable(path, seat, session)
    if not incarnation:
        return None           # unlatchable -> skip; never per-stop wallpaper
    text = ("[helm stop-guard] SEAM RUNG BLIND SPOT — %d seats answer to this "
            "one room (%s): %s. The untested-composition rung keys on the "
            "WORKTREE, so it compares your room against OTHER rooms and can "
            "see no seam between seats inside this one — one room is one "
            "branch, one authored set, one half. That is not a clean bill, it "
            "is a place the instrument cannot look: if you and a co-tenant "
            "have each banked a half here, nothing below will say so. Not "
            "blocking. Said once per arrangement; a new co-tenant re-arms it."
            % (len(names), _clip(_scrub(str(worst["path"])), STATUS_BYTES),
               _clip(_scrub(", ".join(names)), STATUS_BYTES)))
    _PENDING_DISCLOSURES.append(
        (path, fp, text, seat, session, incarnation))
    # AND IT IS DECLARED AS SURVIVING THE REFUSAL EXIT. What this line reports
    # is that the instrument CANNOT LOOK inside this room, which is true of the
    # stop rather than of any rung's verdict — so whether the seat hears it may
    # not depend on whether some other rung happened to refuse. `emit_blocks`
    # honours the declaration; the warn channel carries it on the exits that
    # read warns.
    return survives_refusal(text)


def _seam_gate(session, room, seat, cwd=None, blocks=None, warns=None):
    """APPENDS to `blocks`/`warns` when they are given, and returns the pair
    either way. The append form exists so the caller in `stop_guard` is ONE
    line: this rung's own fail-open try/except and its two conditional
    appends belonged here beside the rung they guard, not spread across the
    ladder — and the ladder is on a 1000-line budget the rung pushed past.
    Fail-open is TOTAL and lives here: a rung that decides whether a seat may
    stop must never raise into the stop path."""
    try:
        block, warn = _seam_gate_inner(session, room, seat, cwd)
    except projscope.Expired:
        raise
    except Exception:            # noqa: BLE001 — see the fail-open law above
        block, warn = None, None
    if blocks is not None and block:
        blocks.append(block)
    if warns is not None and warn:
        warns.append(warn)
    return block, warn


def _seam_gate_inner(session, room, seat, cwd=None):
    """(block | None, warn | None) for the UNTESTED-COMPOSITION rung.

    OWNER REQUEST, 2026-08-23, verbatim: "Two green halves with an untested
    composition — this seems to be the biggest failure mode across projects,
    please /learn globally to prevent that via whispers and or stophooks." The
    whisper half is the store's `composition-untested-on-split` reflex. This is
    the stop half, and it exists because the whisper CANNOT reach the case that
    matters: a reflex fires on language, so it only reaches a seat that already
    said the word "split". The expensive shape is the one nobody named — two
    seats, two lanes, one file, both green, and neither of them ever wrote a
    sentence a matcher could catch.

    THE SIGNAL IS `work.seam_candidates`, AND EVERY CANDIDATE WAS MEASURED
    RATHER THAN ARGUED — including the two that were rejected.

      `helm chat claims`, the advisory claim a seat posts to a room: read on
        this box 2026-08-23 and carrying ONE row, the reading seat's own lease.
        An advisory claim depends on somebody choosing to declare, which is the
        same dependency that makes the whisper insufficient.
      `work.lane_overlaps` + the claim ledger, i.e. HELM BOOKKEEPING: rejected
        after it was measured against the project that asked for this rung.
        THE SIBLING PROJECT has zero helm lane rooms and zero rows on every structured
        helm surface, so the rung would have fired for helm and never for it.
        The same shape as the first rejection, one layer down: an input nobody
        outside this repo populates.

    So the signal is derived from GIT plus /proc — the worktree registry, the
    processes standing in those rooms, and the branches' own diffs — with helm
    bookkeeping ADDED where it exists rather than required. Measured live: 7 of
    helm's 89 worktrees occupied, 2 of that project's 6.

    ONE SEAM PER BLOCK. A busy seat can sit on several, and a list of them is
    the wallpaper this ladder refuses everywhere else. The block names the
    worst one (most shared files) and counts the rest.

    Latched once per COMPOSED SEAM SET — each pair PLUS what it composes to.
    An earlier cut keyed on the pairs alone and defended it as sparing the seat
    a nag about a peer's typing; that was wrong, and a probe measured it: when
    a half moves, the composition is a DIFFERENT untested composition, and the
    pair key made the latch swallow that re-arm. An unwritable latch DEGRADES
    to the compact WARN, the spiral rung's law: a gate that cannot remember may
    not block every stop forever. Fail-open TOTAL;
    HELM_STOP_GUARD_SEAM=0 disables."""
    if _off("STOP_GUARD_SEAM") or not seat:
        return None, None
    projscope.spend_or_raise("composition seam census")
    # TWO TRY BLOCKS, BECAUSE ONE OF THEM ATE A FINDING (a review's finding 5).
    # The disclosure and the measurement shared a handler, so a predicate that
    # RAISED discarded the blind-spot line with it — and the blind-spot line is
    # precisely what a seat needs when the instrument has just failed. The
    # census and the disclosure are computed first and kept; the predicate's
    # failure can now cost only the predicate.
    blind, census, root, cdeg = None, None, None, ()
    try:                      # fail-open TOTAL — a Stop rung must never wedge
        mine, root = _seam_rooms(session, seat, cwd)
        if not mine or not root:
            return None, None
        from .work import _gc
        # ONE ROOM CENSUS PER STOP, shared by the predicate and the blind-spot
        # disclosure: both want the same /proc pass and the same seat sets, and
        # two passes could disagree about who is in a room.
        census, cerr, cdeg = _gc.seam_rooms(root)
        projscope.spend_or_raise("composition seam census result")
        blind = _cotenancy_warn(census, mine, room, seat, session)
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("seats_stop_seam._seam_gate.census", _swallowed)
        return None, None
    if cdeg:
        # A PARTIAL CENSUS IS A THIRD STATE AND IT RIDES BESIDE THE FINDING.
        # `cerr` means the rung has no answer; this means it has one that is
        # NARROWER than the world — an unreadable roster, processes that would
        # not name themselves. Narrow is the dangerous direction for a rung
        # whose blind-spot disclosure fires on a room having MORE than one
        # seat: a leg that quietly names fewer of them makes an unread room
        # look unshared, so the failure of the instrument would render as a
        # clean bill. It is a warn and never a block — nothing here is evidence
        # a composition is untested, only that the census was incomplete.
        blind = _join(blind, "[helm stop-guard] composition-seam rung read a "
                      "PARTIAL room census — %s. The seat sets below are a "
                      "floor, so a room may be shared where this says it is "
                      "not." % _clip(_scrub("; ".join(str(d) for d in cdeg)),
                                     STATUS_BYTES))
    if cerr:
        # AN INPUT THAT COULD NOT BE READ IS NOT AN EMPTY BOARD. The registry
        # and the /proc census are what SUPPLY the peers, so a failure here
        # deletes every peer silently — the shape this rung exists to refuse,
        # one layer down in its own plumbing.
        return None, _join(blind, "[helm stop-guard] composition-seam rung "
                           "could not read the room census: %s — peers are "
                           "UNKNOWN, not absent."
                           % _clip(_scrub(str(cerr)).strip(), STATUS_BYTES))
    try:
        projscope.spend_or_raise("composition seam candidates")
        rows, err, partial = _gc.seam_candidates(
            root, mine, holder=seat, rooms=census)
        projscope.spend_or_raise("composition seam candidate result")
    except projscope.Expired:
        raise
    except Exception as _swallowed:
        record.swallow("seats_stop_seam._seam_gate", _swallowed)
        return None, blind
    if err:
        # UNKNOWN SEAMS ARE NOT ZERO SEAMS — the spiral rung's own correction,
        # applied at birth here rather than after a seat runs ten rounds inside
        # the silence. Still fail-open, now audible.
        return None, _join(blind, "[helm stop-guard] composition-seam rung "
                           "could not measure this seat: %s — an untested "
                           "composition is UNKNOWN, not absent."
                           % _clip(_scrub(str(err)).strip(), STATUS_BYTES))
    if partial:
        # A LEGACY PLACEMENT CENSUS IS NOT THE CANONICAL BINDING LEDGER. The
        # latter already proved the ids carried in `rows`; losing those rows on
        # a timeout/malformed old pointer revokes durable authority because a
        # compatibility scan could not finish. Keep the measured verdict and
        # surface the narrower uncertainty beside it — folded into the block
        # below when one exists, because stop-block exits suppress WARNs.
        blind = _join(
            blind, "[helm stop-guard] composition-seam canonical bindings were "
            "measured, but legacy import placement is UNKNOWN: %s"
            % _clip(_scrub(str(partial)).strip(), STATUS_BYTES))
    rows = [r for r in rows
            if _SEAM_REF.fullmatch(str(r.get("branch") or ""))
            and _SEAM_REF.fullmatch(str(r.get("peer_branch") or ""))]
    if not rows:
        # NO SEAM FOUND IS NOT NOTHING TO SAY. The blind-spot line rides here
        # precisely because this is the exit a co-tenant reaches: the rung
        # looked, found no cross-room seam, and the one place it could not look
        # is the room the reader is standing in.
        return None, blind    # measured, and genuinely no CROSS-ROOM seam
    rows.sort(key=lambda r: (-len(r["files"]), r["branch"], r["peer_branch"]))
    worst = rows[0]
    branch, peer = worst["branch"], worst["peer_branch"]
    who = str(worst.get("peer_holder") or "").strip()
    shown = ", ".join(worst["files"][:SEAM_FILES_SHOWN])
    if len(worst["files"]) > SEAM_FILES_SHOWN:
        shown += " +%d more" % (len(worst["files"]) - SEAM_FILES_SHOWN)
    # ONE EVIDENCE WORD, BECAUSE THERE IS ONE TIER. This read `green_by` and
    # picked between GREEN and BANKED — and when the tier was deleted the field
    # went with it, so `worst.get("green_by")` became permanently None and this
    # rendered the BANKED branch on EVERY seam: the rung told every reader that
    # its repo mints no gate receipts, at exactly the moment a receipt on both
    # half trees became MANDATORY for the row to exist at all. Not leftover
    # prose — an inversion, and one that only a rendered-message arm could
    # catch, which is what did. A row now exists only when both halves carry
    # admissible evidence, so the sentence is unconditional.
    proof = "both halves are GREEN (an admissible gate receipt on each)"
    # THE MERGE OUTCOME PICKS THE SENTENCE, NEVER WHETHER TO SPEAK. A clean
    # merge is the SILENT hazard: nothing downstream complains. A conflicting
    # one is loud about the TEXT and silent about the BEHAVIOUR — somebody will
    # resolve it by hand, and a hand-resolved merge is an untested composition
    # by construction. Every code-overlapping pair in the sibling project is the second
    # kind, which is why this rung refuses to treat it as somebody else's job.
    how = ("they merge CLEANLY, so nothing downstream will complain, and "
           "nothing has run against the tree they make together"
           if worst.get("merges") else
           "they CONFLICT, so someone will resolve them BY HAND — and a "
           "hand-resolved merge is an untested composition by construction; "
           "git is loud about the text and silent about the behaviour")
    line = ("'%s' and LIVE '%s'%s both authored %s — %s; %s"
            % (branch, peer,
               " (%s)" % _clip(_scrub(who), SEAT_BYTES) if who else
               " (%s, holder unknown)" % _scrub(str(worst.get("peer_why")
                                                    or "live")),
               _clip(_scrub(shown), STATUS_BYTES), proof, how))
    # THE LATCH KEYS ON WHAT THE SEAM CONTAINS, NOT ON WHICH PAIR IT IS
    # (a review's finding 2). Keyed on (branch, peer) alone, a half that MOVED
    # produced a NEW untested composition under the OLD fingerprint, so the
    # latch suppressed exactly the re-arm it exists to permit — and the earlier
    # commit defending the pair key as "not a nag about somebody else's typing"
    # was wrong: a moved half is not typing, it is a different composition that
    # nothing has run. `composed_id` is the merged tree, or both tip trees where
    # the merge conflicts and no merged tree exists.
    fp = hashlib.blake2b(
        "|".join(sorted("%s\x1f%s\x1f%s" % (r["branch"], r["peer_branch"],
                                            r.get("composed_id") or "?")
                        for r in rows)).encode("utf-8"),
        digest_size=8).hexdigest()
    path = _stop_fp_path(room, seat, session, kind=SEAM_LATCH)
    try:
        with open(path) as f:
            last = f.read().strip()
    except OSError:
        last = None
    if last == fp:
        return None, blind                # already gated this exact seam set
    # WRITABILITY IS PROVEN NOW, THE FINGERPRINT IS BANKED FOR THE EXIT.
    # `pk.atomic_write` used to stand here, which spent the latch at the moment
    # the sentence was BUILT rather than the moment it was READ — and this rung
    # has two exits that build a block nobody sees. The guard-could-not-run exit
    # returns 0 after a later rung raises, and any caller that drops the pair on
    # the floor does the same; either way the composition was latched, the
    # re-stop matched the fingerprint, and the seat was never told once. A latch
    # that fires before its line is read is a guard that fires once and blesses
    # everything after it, which is the property this fingerprint exists to
    # deny. `_latch_dir_writable` keeps the degrade-to-warn branch honest: it is
    # a real write beside the latch, so an unwritable directory still falls back
    # to the compact warn rather than blocking every stop forever.
    incarnation = _latch_dir_writable(path, seat, session)
    if not incarnation:
        return None, _join(blind,
                           "[helm stop-guard] UNTESTED COMPOSITION — " + line)
    room_a = str(worst.get("path") or "")
    # ONE DISCHARGE, AND THE TEXT MAY NOT OFFER A SECOND. The trailer cure that
    # stood here was deleted with the trailer discharge itself (prose
    # cannot prove testing or accountable transfer), and LEAVING IT IN THE
    # REMEDIATION WOULD HAVE BEEN WORSE THAN THE ORIGINAL BUG — helm would have
    # instructed an operator to write a commit trailer that clears nothing, and
    # they would have written it, re-stopped, and found the block still there
    # with no idea why. A cure a guard prints is a promise; there is exactly one
    # left to promise.
    #
    # THE NO-RECEIPT BRANCH IS GONE TOO, and not because it was wrong: it is now
    # UNREACHABLE. A half is green only if its current tree carries admissible
    # evidence, so a repo that mints no receipts has no green halves, produces no
    # rows, and never reaches this renderer. `task/1378` carries the honest
    # replacement — a durable accepted obligation — and until it lands this rung
    # is SILENT in such a repo rather than blocking with a ceremonial cure.
    if not worst.get("merges"):
        # A CONFLICTING PAIR HAS NO COMPOSED TREE TO GATE, so the merge command
        # would send a seat into a resolution it cannot finish from a stop hook.
        # The discharge is still reachable — resolve, commit, gate the resolved
        # tree — so say that rather than implying there is no way out.
        cures = ("  THESE TWO CANNOT BE MERGED AUTOMATICALLY, so there is no "
                 "composed tree to gate yet and nothing here can measure the "
                 "composition for you. Whoever resolves %s against this branch "
                 "must run the suite ON THE RESOLVED TREE, not on either half; "
                 "a receipt at that tree is what ends the block.\n" % peer)
    elif _SEAM_PATH.fullmatch(room_a):
        cures = ("  DISCHARGE IT: run the arm against the COMPOSED tip. The "
                 "receipt binds the merged TREE, so this clears even if you "
                 "throw the merge away afterwards:\n"
                 "    git -C %s merge --no-ff %s && fab gate --repo %s\n"
                 % (room_a, peer, room_a))
    else:
        # A COMMAND THAT CANNOT BE QUOTED INERTLY IS NOT OFFERED. Laundering the
        # room path would produce a command that runs somewhere else, which is
        # worse than no command; the seat still gets the finding and the one
        # discharge described in words it can act on.
        cures = ("  DISCHARGE IT by merging the peer lane into yours and "
                 "gating — the receipt binds the merged TREE, so it clears "
                 "even if you throw the merge away. This room's path cannot be "
                 "quoted inertly, so no command is offered rather than a "
                 "laundered one that would run somewhere else.\n")
    block = ("[helm stop-guard] UNTESTED COMPOSITION — " + line + ".\n"
             "  A PASSING RUN IS TRUE ABOUT ITS OWN TREE AND SILENT ABOUT EVERY "
             "OTHER. Your half was verified, theirs was verified, and nothing "
             "has ever run the two together. Owner, 2026-08-23: two green "
             "halves with an untested composition is the biggest failure mode "
             "across projects.\n"
             + cures +
             "  Blocks once per seam set — a re-stop passes, a NEW peer re-arms "
             "it, and the discharge above ends it for good. "
             "HELM_STOP_GUARD_SEAM=0 disables.\n"
             "  " +
             (("%d further seam(s) on this seat are covered by the same "
               "latch and are NOT listed — `git worktree list` names every "
               "room that could be the other half." % (len(rows) - 1))
              if len(rows) > 1 else
              "File overlap is a PROXY for one composition, never proof. "
              "If these two really are independent there is no longer a way "
              "to declare it here — a commit trailer used to clear this and "
              "was removed because prose cannot prove a composition was "
              "tested. Gating the merged tree still clears it, and task/1378 "
              "carries the durable way to hand the seam to a named owner."))
    # THE DISCLOSURE RIDES THE BLOCK WHEN THERE IS ONE, AND THAT IS THE WHOLE
    # CURE FOR IT. The blind-spot line went out on the WARN channel, which the
    # refusal exit suppresses by design — so on every stop where this rung
    # actually blocked, the one sentence saying "and I cannot see inside your
    # own room" was dropped. The suppression is right (an advisory beside a
    # block renders as red error text) and the finding is right; what was wrong
    # is the channel. A rung whose finding must survive exit 2 puts it IN the
    # block, which is one emission rather than an advisory beside one. The warn
    # channel keeps it on the exits where the warn channel is read.
    emitted = _join(blind, block)
    # ARMED WITH THE TEXT THAT WILL ACTUALLY BE PRINTED, so `commit_disclosures`
    # can tell a block that reached a reader from one that was built and
    # dropped. Both latches ride the same emission, which is why the disclosure
    # was folded in above rather than left on a channel this exit discards.
    _PENDING_DISCLOSURES.append(
        (path, fp, emitted, seat, session, incarnation))
    return emitted, None
