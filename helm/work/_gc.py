"""helm work — the gc cluster: git verdict helpers, the live-lease read,
the rescue commit, housekeeping (scan/enact/orphans), and the room board.
Moved verbatim from the pre-split helm/work.py.
"""
import os
import re
import signal
import threading
import time

from .. import gitfacts, pk, projscope, seats, vcs
from ._lanes import (
    _disposable_worktree_occupant, _occupants, _occupants_many,
    _panes_bound_to, _room_status, _worktree_records,
    find_root, lane_branch, lane_rows, managed_room_kind,
    auto_rows, resource, worktrees,
)


def _dirty(path):
    """Uncommitted/untracked bytes in the room? An unreadable room reads as
    DIRTY — callers must fail toward rescue, never toward discard.

    A PROJECTION of `_room_status`, the same way `_wrote_ago` is, because this
    used to be a SECOND status authority: its own `git status --porcelain` call
    answering only a boolean. That is how `helm work list` kept printing "dirty"
    for a room sitting in a DANGLING CONFLICT even after `_room_status` learned to
    name it — the board read the cruder oracle. Caught by dogfooding the real
    state, not by the unit tests, which tested `_room_status` directly. One
    question, one authority."""
    return _room_status(path)["dirty"]


def _base(root):
    """The integration branch NAME — for BRANCHING FROM and for naming. Not
    the landedness authority; `_trunk` is. Two different questions that were
    one function until 2026-07-30, which is how the reaper came to ask whether
    work was contained in a ref nobody else reads."""
    return vcs.backend(root).base_branch(root)


def _trunk(root):
    """The ref that answers 'does the FLEET have this?' — `origin/<base>` when
    a remote exists, else the local name. See `vcs.trunk_ref` for why asking
    the local ref is wrong in both directions and destructive in one."""
    return vcs.backend(root).trunk_ref(root)


def _has_branch(root, branch):
    return vcs.backend(root).has_branch(root, branch)


def _merge_state(root, branch):
    """vcs.ANCESTOR / PATCH_EQUIVALENT / NOT_ANCESTOR / UNKNOWN — did the lane's
    WORK reach the trunk? THE authority; `_merged` is its boolean projection
    (the `_dirty`/`_room_status` pattern: one question, one authority).

    FOUR states, not three, and the fourth is why every well-behaved lane used
    to accumulate. This asked `ancestry` alone, which is SHA IDENTITY, while
    our protocol lands work REBASED — the integrator rebases the chain onto
    current trunk and gates the rebased tree, so the landed commit carries a
    different object id. Ancestry then answers "no" TRUTHFULLY, the lane reads
    unlanded, and the agent that followed the rule correctly keeps its branch
    forever. Measured on this repo 2026-08-03: of 107 `lane/*` branches, 20
    were ancestry-reachable and 7 more were entirely on trunk under other
    shas — invisible here, uncollectable by any reaper.

    `vcs.landed_state` adds the content answer via `git cherry` (git's own
    patch-id comparison), keeps ancestry as the decisive fast path, and folds
    NOTHING: a mixed lane (some commits landed, some not — 8 of the same 107)
    reads NOT_ANCESTOR and keeps, and every unreadable case reads UNKNOWN.
    `gc_scan` switches on this directly, where "landed as itself", "landed as
    content", "no" and "cannot see" all triage apart.

    Asks the TRUNK ref, never the local branch: "landed" means the fleet has
    it, and this predicate gates a branch deletion."""
    return vcs.backend(root).landed_state(root, branch, _trunk(root))


RETIRABLE = (vcs.ANCESTOR, vcs.PATCH_EQUIVALENT)


def _proof_word(state):
    """The AUDIT phrase for one landedness state, in ONE line.

    A silent reap is indistinguishable from data loss, so every retirement says
    which of the two proofs carried it — and every refusal says which of the
    two FAILURES it was, because "the work is not on the trunk" and "I could
    not finish the read" are different facts and only the first is about the
    work. Kept short on purpose: these render one-per-row on a board the owner
    reads, beside a triage line that already carries tip/age/ref."""
    return {
        vcs.ANCESTOR: "LANDED by ancestry (the tip itself is on the trunk)",
        vcs.PATCH_EQUIVALENT: "LANDED by patch identity (every commit is on "
                              "the trunk under a rebased sha; ancestry alone "
                              "says no)",
        vcs.NOT_ANCESTOR: "NOT landed by ancestry or patch identity",
        vcs.UNKNOWN: "landedness UNKNOWN (unreadable object, a commit `git "
                     "cherry` cannot speak for, or a range past the cap)",
    }.get(state, "landedness UNKNOWN")


def _merged(root, branch):
    """Boolean projection of `_merge_state` for callers whose only safe branch
    is the positive (release, envtidy's worktree/orphan rows): True is PROOF of
    containment BY OBJECT OR BY CONTENT; False is 'no proof' — a clean
    negative OR an unreadable one, and on those surfaces both read KEEP.
    UNKNOWN can never read as merged here, so it can never authorize a
    delete."""
    return _merge_state(root, branch) in RETIRABLE


RETIRED_NS = "refs/helm-retired/"


def _delete_lane_branch(root, branch, state=None):
    """THE branch-retirement actuator: [lines], one per outcome, always naming
    the proof. Every caller that deletes a lane branch comes through here.

    A PROOF THE DELETE CANNOT ACT ON IS NOT A FIX. `git branch -d` refuses on
    REACHABILITY, which is the same wrong question `landed_state` corrects — a
    rebased-and-landed branch fails `-d` forever. So the patch-identity proof
    spends `-D`.

    ANCESTRY AND PATCH IDENTITY ARE NOT THE SAME GRADE OF FACT, and this
    function is where that difference is paid for rather than argued about.
    Ancestry is git's own reachability: after `-d` the tip is still on the
    trunk, so nothing can be lost and nothing more is owed. Patch identity says
    every commit's DIFF is upstream — which preserves the bytes but not the
    commit objects, their messages or their authorship, and `-D` makes those
    unreachable and eventually collectable.

    So the patch-identity path DEMOTES rather than deletes: the tip is written
    to `refs/helm-retired/<branch>` FIRST, and the branch is removed only if
    that write succeeded. The objects stay permanently reachable and immune to
    `git gc`; the owner's sidebar (which renders `refs/heads/*`) is clean;
    `git for-each-ref refs/helm-retired/` is the audit trail; and restore is
    one command. That turns a destructive step into a reversible one, which is
    the only honest way to spend a weaker grade of evidence.

    ORDER IS THE WHOLE SAFETY PROPERTY: save, verify the save, then delete. A
    failed or unverifiable save KEEPS the branch and says so. Reversibility is
    a PRECONDITION of the forced delete, never a courtesy after it.

    Re-reads the state unless handed one, so a caller cannot spend a stale
    scan verdict on a branch that moved."""
    state = _merge_state(root, branch) if state is None else state
    v = vcs.backend(root)
    if state not in RETIRABLE:
        return ["KEPT branch %s — %s" % (branch, _proof_word(state))]
    if state == vcs.ANCESTOR:
        # Reachability already vouches: the tip is ON the trunk after this.
        rc, _out, err = v.delete_branch(root, branch)
        if rc == 0:
            return ["deleted branch %s — %s" % (branch, _proof_word(state))]
        return ["KEPT branch %s (safe -d refused: %s)" %
                (branch, err or "unknown error")]
    rc, sha, _err = v.text(root, "rev-parse", "--verify", "-q", branch)
    sha = (sha or "").strip()
    if rc != 0 or not sha:
        return ["KEPT branch %s — patch-identical to the trunk, but its tip "
                "sha is UNREADABLE, so it could not be preserved and no "
                "forced delete was attempted" % branch]
    keep_ref = RETIRED_NS + branch
    rc, _out, err = v.text(root, "update-ref", keep_ref, sha)
    if rc != 0:
        return ["KEPT branch %s — could not write the retirement ref %s (%s), "
                "so the forced delete was not attempted" %
                (branch, keep_ref, (err or "unknown error").strip()[:120])]
    rc, saved, _err = v.text(root, "rev-parse", "--verify", "-q", keep_ref)
    if rc != 0 or (saved or "").strip() != sha:
        return ["KEPT branch %s — the retirement ref %s did not read back as "
                "%s, so the tip is not provably preserved and nothing was "
                "deleted" % (branch, keep_ref, sha[:12])]
    # DELETE EXACTLY WHAT WAS PRESERVED, OR NOTHING. Every guard above is about
    # the retirement REF; none of them bound the DELETE to the sha they proved.
    # `-D` removes whatever the branch points at when it runs, so a branch that
    # advanced after the read-back on line above had its NEW tip deleted while
    # the ref preserved the OLD one — the commits in between survived nowhere.
    # `expect` makes this a compare-and-delete: it refuses the moved branch.
    rc, _out, err = v.delete_branch(root, branch, force=True, expect=sha)
    if rc != 0:
        return ["KEPT branch %s — the forced delete was REFUSED (%s). If the "
                "branch moved after %s was preserved, that refusal is the "
                "guard working: the new tip is still on the branch and the "
                "next pass re-reads it" %
                (branch, (err or "unknown error").strip()[:160], sha[:12])]
    return ["retired branch %s — %s; tip %s PRESERVED at %s — restore: "
            "git branch %s %s"
            % (branch, _proof_word(state), sha[:12], keep_ref, branch, sha)]


def _age_word(seconds):
    """Compact owner-facing age; precision beyond the largest useful unit only
    makes a triage row noisier, not truer."""
    seconds = max(0, int(seconds))
    for unit, width in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= width:
            return "%d%s" % (seconds // width, unit)
    return "%ds" % seconds


_ROWED = None            # ({lane label}, None) | (None, reason) — once per process


def _rowed_lanes():
    """({lane labels carrying a ledger row}, None) — or (None, reason).

    THE GAP THIS CLOSES: `helm work claim` mints a lease, a branch and a room
    (helm/work/_claims.py:85) and never touches the dispatch ledger; the row
    comes from a separate verb and nothing requires the pair. That coupling is
    absent BY CONSTRUCTION, not by accident, so a lane nobody came back to is
    invisible to every stall detector, nag and burn-down helm owns. Measured
    2026-08-25: 23 of 65 triage rooms had no row at all, which is why the
    estate number read 63-65 all week while seats worked.

    STRICT, AND THE CHOICE IS NOT MINE TO MAKE. helm/eventledger.py:212-216
    already rules it: a projection caller keeps the tolerant historical read,
    but a DECISION caller — one that will file, refuse, or CLASSIFY on the
    answer — poisons the whole read on a corrupt complete row rather than
    folding it as known-empty. This line classifies, so it opts in. Tolerant
    here would silently drop the malformed rows and then assert their lanes
    have none, which is the manufactured-false-row failure this instrument
    exists to prevent.

    THREE STATES, NEVER TWO. A MISSING ledger is a PROVEN empty one
    (checked_events returns [], None) and every lane then reads unrowed, which
    is TRUE — no rows exist anywhere to be found. An unreadable or corrupt one
    returns a reason and the caller must render UNKNOWN. Absence and cannot-look
    must not share a representation: an instrument that says "no row" when it
    means "I could not look" is worse than the blindness it replaces, because
    it manufactures confident false rows for someone to chase.

    CANONICAL STATE, NOT RAW EVENTS, and the first version got this wrong in
    the direction that reassures. It collected `lane` off every parseable
    event, so ANY event carrying the label suppressed the marker — a
    cross-family probe fed it a bare `{"id": "x", "event": "close", "lane":
    "ghost-lane"}` and the lane read as TRACKED. A close is not a genesis; a
    row nothing ever dispatched is not a row. eventledger validates STRUCTURE
    (is this a well-formed event) while dispatches owns IDENTITY (is this a
    real dispatch), and only the second question is the one being asked here.
    So the read stays strict and the projection is dispatches' own, which
    drops anything failing `_valid_identity` or carrying no recipient, and
    which REFUSES rather than skipping when a genesis kind is present but
    unreadable - a future schema version being the live case.
    """
    global _ROWED
    if _ROWED is None:
        from .. import dispatches, eventledger
        events, err = eventledger.checked_events(
            dispatches.ledger_path(), strict=True)
        if err:
            _ROWED = (None, err)
        else:
            # THE SECOND UNKNOWN, and it is a DIFFERENT one from `err` above.
            # `err` says the LEDGER could not be read; this says the ledger
            # read fine and one GENESIS in it could not be understood. Both
            # must reach the caller as UNKNOWN, because either one means the
            # label set may be incomplete and an absence cannot be asserted
            # from it. Collapsing the second into "no row" is exactly the
            # regression a probe measured against the cure this replaces.
            lanes, unreadable = dispatches.genesis_lanes(events)
            _ROWED = (None, unreadable) if unreadable else (lanes, None)
    return _ROWED


def _tracking_mark(lane):
    """The clause `_branch_triage` appends, or "" when the lane IS tracked.

    THE LABEL CAVEAT IS IN THE STRING ON PURPOSE. This lookup is keyed on the
    lane NAME, and helm is explicit elsewhere that a lane name is a free-text
    LABEL and never work identity — a row filed under a renamed continuation
    is real tracking this predicate cannot see. So the marker says UNDER THIS
    LABEL rather than the flat "no ledger row" it would be tempting to print:
    the honest claim is about the label, and a reader who acts on the wider
    claim would go file a duplicate row for work already tracked.
    """
    rowed, err = _rowed_lanes()
    if err:
        return " [LEDGER UNREADABLE — tracking UNKNOWN: %s]" % err
    return "" if lane in rowed else " [NO LEDGER ROW UNDER THIS LABEL]"


class EnactResult(list):
    """The enact lines, PLUS what enact decided, as data.

    THE SENTENCE IS NOT THE CHANNEL. Two cross-family reviewers reached this
    independently: a substring detector over gc_enact's output - however
    carefully anchored, however faithfully sharing one constant - is still
    parsing PROSE to recover a fact the emitter already knew structurally.
    Sharing a constant removes DRIFT between emitter and reader; it does not
    stop a blocker whose worktree PATH contains the marker from being
    classified as triage, and this module already documents a path as
    attacker-shaped text once it reaches a report. So the fact travels as an
    attribute and no detector exists.

    A LIST SUBCLASS BECAUSE THE RETURN CONTRACT IS LOAD-BEARING: gc_enact's
    result is consumed as a list of lines in ~14 test call sites, in
    helm/work/_cli.py and in helm/envtidy.py, and `assertEqual(result, [])`
    must keep passing. A list subclass compares equal to a list, so every
    existing consumer is untouched and only the ones that need the decision
    read it.
    """

    def __init__(self, lines=(), reclassified=False):
        super().__init__(lines)
        self.reclassified = bool(reclassified)


def was_reclassified(result):
    """Did enact turn a scan-time REMOVE into a triage room?

    THE DEFAULT IS FALSE AND THAT IS A REAL RISK, named rather than hidden: a
    future reclassifying path that returns a plain list reads as not-
    reclassified and silently undercounts, which is the same failure this pair
    exists to close. The arm in tests/test_work.py walks gc_enact's SOURCE and
    requires every keep-for-triage emitter to construct an EnactResult, so
    that omission is a red test rather than a quiet wrong number.
    """
    return bool(getattr(result, "reclassified", False))


def _branch_triage(root, lane, branch, now=None, state=None,
                   disposition="worktree + branch kept"):
    """Evidence for work Git cannot prove landed: name, tip, commit age, committer.

    This is reporting evidence only. A failed read never becomes permission to
    remove; the caller keeps the room and prints UNKNOWN fields instead.
    Note: git committer identity in shared seats is a shared git identity (%cn <%ce>),
    not a seat/agent provenance signal.

    `state` REPLACES the old blanket "landing unproven" with the specific
    verdict (`_proof_word`), so a keep row says whether the work is genuinely
    absent from the trunk or whether the read could not be finished. Passed by
    every caller that already computed it; without it the wording stays the
    conservative generic one, which is right for the callers that never asked."""
    rc, out, _err = vcs.backend(root).text(
        root, "show", "-s", "--format=%H%x00%ct%x00%cn <%ce>", branch)
    parts = out.split("\0", 2) if rc == 0 else []
    if len(parts) != 3:
        tip, age, committer = "unreadable", "unknown", "unknown"
    else:
        tip, stamp, committer = parts
        try:
            age = _age_word((time.time() if now is None else now) - int(stamp))
        except (TypeError, ValueError):
            age = "unknown"
    # NAME THE REF IT WAS MEASURED AGAINST. A remote-tracking ref is itself a
    # local snapshot, so "unproven" can mean "genuinely not landed" OR "this
    # checkout has not fetched since it landed" — and those read identically
    # without the ref and its sha. Printing them is what makes a stale verdict
    # diagnosable at a glance instead of a mystery that costs an hour.
    ref = _trunk(root)
    rc2, head, _e2 = vcs.backend(root).text(root, "rev-parse", "--short=12", ref)
    against = "%s @ %s" % (ref, head) if rc2 == 0 and head else "%s (UNREADABLE)" % ref
    # RE-DERIVED, not resolved to a side: trunk taught this line to say whether
    # the committer is a ROSTERED SEAT (a shared identity is not provenance),
    # and this lane taught it to report WHICH PROOF answered the landedness
    # question instead of the hardcoded "landing unproven". Both are live and
    # neither subsumes the other — taking either side whole silently reverts a
    # shipped feature, which is the whole reason this conflict is semantic.
    # `seats` is ALREADY bound module-level at the top of this file, so this
    # local re-import is vestigial rather than load-bearing — and neither
    # direction is circular, because seats.py imports helm.work only from
    # inside functions. Left exactly as it stood: it is not what was wrong.
    from .. import seats
    c_name = committer.split(" <", 1)[0].strip()
    # THE AUTHORITATIVE COMPARATOR, never `c_name in seats.roster()`: @-strip
    # is unconditional while casefolding is roster-dependent (#134), so a raw
    # membership test reintroduces drift already fixed elsewhere.
    # recipient_matches is exact canonical-token equality — never substring,
    # never slug — which is the strictness a provenance claim needs.
    #
    # NO EXCEPT AT ALL, AND THAT IS THE POINT. This line shipped DEAD: it
    # called `seats.is_rostered_name`, which exists in no revision of this
    # repo, and a bare `except Exception` turned that AttributeError into
    # is_seat=False on every call. Every committer — a genuinely rostered
    # seat included — printed as "not seat provenance", and every test still
    # passed, because only the negative branch was ever asserted. A missing
    # or renamed comparator must CRASH, so nothing here is caught.
    #
    # The first cure kept an `except OSError` around `seats.roster()`, and
    # a post-bind audit measured why that was vacuous at the artifact
    # boundary: roster() is the fail-OPEN reader — it swallows storage and
    # parse failures itself and answers {}, so the except could never fire
    # and an unreadable roster was indistinguishable from a measured-empty
    # one. roster_checked() is the probe-carrying read built for exactly
    # this: missing is a proven empty roster, unreadable/malformed is a
    # FAILED probe, and a failed probe must render UNKNOWN — a triage line
    # that says "not seat provenance" off a parse failure asserts a negative
    # nobody measured.
    roster, roster_failed = seats.roster_checked()
    is_seat = not roster_failed and any(
        seats.recipient_matches(c_name, r) for r in roster)
    # DISPLAY IS LAUNDERED, IDENTITY IS NOT. %cn <%ce> is arbitrary bytes an
    # author chose; ESC[2J clears the operator's screen and U+202E reverses
    # it. _seat_label is the same Cc/Cf scrubber every seat surface uses;
    # an all-control string that scrubs to nothing prints UNRENDERABLE
    # rather than an empty hole. The MATCH above still uses the raw c_name:
    # scrubbing before comparison would let a control-stuffed committer
    # alias into a rostered seat's canonical token.
    shown = seats._seat_label(committer) or "UNRENDERABLE"
    if roster_failed:
        comm_text = ("git committer: %s — roster unreadable, seat "
                     "provenance UNKNOWN" % shown)
    elif is_seat:
        comm_text = "git committer: %s" % shown
    else:
        comm_text = ("git committer, shared across seats: %s — not seat "
                     "provenance" % shown)
    verdict = _proof_word(state) if state else "landing unproven"
    # TRACKING IS A SEPARATE AXIS FROM LANDEDNESS AND GOES LAST. Every other
    # clause on this line answers a GIT question; this one answers a LEDGER
    # question, and a room can fail either independently — landed work with no
    # row, unlanded work that is tracked and stalling. Appending rather than
    # weaving keeps the git verdict readable exactly as it was.
    # THE DISPOSITION IS THE CALLER'S FACT, NOT THIS READER'S. Every clause
    # above answers a question about the WORLD — what the tip is, how old, who
    # committed it, what the landedness read says — and this last one states
    # what the CALLER DID about it. Hardcoding it was true for every caller
    # that keeps the room, and became FALSE the moment a caller retired one:
    # `work release --superseded` removes the worktree and then appended this
    # evidence, so the line ended by saying the room was kept. Measured by
    # dogfooding that verb on lane/gate-receipt-cache, task/1956 — the room was
    # already gone when the sentence claimed otherwise. The default keeps every
    # existing caller byte-identical.
    return ("TRIAGE %s (%s): tip %s, age %s (%s) — "
            "%s vs %s; %s%s" %
            (lane, branch, tip[:12], age, comm_text, verdict, against,
             disposition, _tracking_mark(lane)))


def refresh_trunk(root):
    """(ok, why) — refresh the remote-tracking ref BEFORE a destructive pass.

    `gc_scan` decides "landed" against a REMOTE-TRACKING ref, which is itself a
    local snapshot: a lane that landed an hour ago reads UNLANDED in a checkout
    that has not fetched since, and reads identically to one that genuinely
    never landed. That direction is safe on its own — a stale ref usually keeps
    branches — but the INVERSE is the destructive one: origin/main can also be
    stale in a way that makes an unlanded branch look contained, and `--apply`
    deletes on exactly that proof.

    vcs.py:405 already states the rule as prose — "callers that act
    destructively must fetch first" — and nothing did. This is that sentence,
    made mechanical.

    UNKNOWN REFUSES. A fetch that fails leaves the proof unrefreshed, and an
    unrefreshed proof must not authorize a deletion. Same law as `lr land`'s
    deletion guard: an unscannable deletion set is not a known-safe one.

    A trunk with no remote component (a local-only branch) has nothing to
    refresh and is not a failure."""
    ref = _trunk(root)
    if "/" not in (ref or ""):
        return True, "trunk %s is local — nothing to refresh" % (ref or "?")
    remote = ref.split("/", 1)[0]
    rc, _out, err = vcs.backend(root).text(root, "fetch", "--quiet", remote)
    if rc != 0:
        return False, "git fetch %s failed: %s" % (
            remote, (err or "").strip()[:160] or "rc %s" % rc)
    return True, "refreshed %s" % remote


def trunk_sync(root, enforcing):
    """([lines], post_error) — converge the shared checkout's OWN base branch
    onto the trunk ref, and NAME every state it cannot lawfully converge.

    THE INCIDENT (2026-08-03): the fleet's shared checkout sat FORKED from
    origin/main for ~80 minutes — a local-only commit raced a land, the ff
    silently stopped, and every seat kept measuring "trunk" on the stale
    side while the integrator announced a land none of them had. It was
    found by reflog archaeology, not by an instrument. `refresh_trunk`
    already makes the REMOTE-TRACKING ref honest before destructive passes;
    this leg makes the LOCAL base honest against it, on the same cadence,
    in the only direction that is lawful without judgment:

      IN SYNC  -> silence (gc's idiom: healthy states say nothing).
      BEHIND   -> fast-forward on --apply; the dry run names it. The ref
                  guard ADMITS an ff of the shared base structurally, and
                  the landlock check refuses one beside a staged merge —
                  a refusal is git's own words forwarded, never overridden
                  (NO integrator env here: this walks the front door).
      AHEAD    -> named, kept. Unpushed base commits are someone's mid-land
                  window or a stray direct commit; a PUSH completes them and
                  publishing is a person's call — gc never pushes.
      DIVERGED -> named, kept, POSTED to the room (--apply only). Whose
                  commit rides the repair is a judgment call, and automating
                  a judgment call is how a guard becomes the next incident.

    HEAD off the base branch, unreadable refs, or UNKNOWN ancestry are
    named and left alone: a sync that cannot prove what it is standing on
    must not move it."""
    v = vcs.backend(root)
    trunk = _trunk(root)
    if "/" not in (trunk or ""):
        return [], None            # no remote: local base IS the trunk
    base = trunk.split("/", 1)[1]
    rc, head_ref, _e = v.text(root, "symbolic-ref", "-q", "HEAD")
    head_ref = (head_ref or "").strip()
    if rc != 0 or head_ref != "refs/heads/" + base:
        return ["TRUNK-SYNC kept: shared checkout HEAD is %s, not '%s' — a "
                "sync that cannot prove what it stands on must not move it"
                % (head_ref or "detached/unreadable", base)], None
    rc_l, local, _el = v.text(root, "rev-parse", "--verify",
                              "refs/heads/" + base)
    rc_t, remote, _et = v.text(root, "rev-parse", "--verify", trunk)
    local, remote = (local or "").strip(), (remote or "").strip()
    if rc_l != 0 or rc_t != 0 or not local or not remote:
        return ["TRUNK-SYNC kept: cannot read '%s' vs %s — refs unreadable"
                % (base, trunk)], None
    if local == remote:
        return [], None
    fwd = v.ancestry(root, local, trunk)
    back = v.ancestry(root, remote, "refs/heads/" + base)
    if fwd == vcs.ANCESTOR:                                   # BEHIND
        if not enforcing:
            return ["TRUNK-BEHIND '%s' is behind %s (%s -> %s) — --apply "
                    "fast-forwards (a dry run does not fetch, so even this "
                    "verdict may be stale)"
                    % (base, trunk, local[:12], remote[:12])], None
        rc_m, _out, err = v.text(root, "merge", "--ff-only", trunk)
        if rc_m != 0:
            return ["TRUNK-BEHIND kept: fast-forward refused — %s"
                    % ((err or "").strip()[:200] or "rc %s" % rc_m)], None
        return ["TRUNK-SYNC fast-forwarded '%s': %s -> %s"
                % (base, local[:12], remote[:12])], None
    if back == vcs.ANCESTOR:                                  # AHEAD
        _rc, n, _e2 = v.text(root, "rev-list", "--count",
                             "%s..%s" % (trunk, local))
        _rc, tops, _e3 = v.text(root, "log", "--format=%h %s",
                                "--max-count=3", "%s..%s" % (trunk, local))
        return ["TRUNK-AHEAD '%s' carries %s unpushed commit(s) past %s: %s "
                "— a push or a land completes them; gc never publishes"
                % (base, (n or "?").strip(), trunk,
                   "; ".join((tops or "").strip().splitlines())
                   or "unreadable")], None
    if fwd == vcs.NOT_ANCESTOR and back == vcs.NOT_ANCESTOR:  # DIVERGED
        line = ("TRUNK-DIVERGED '%s' and %s have FORKED (local %s / trunk "
                "%s) — every seat in this checkout is measuring the wrong "
                "trunk. gc never repairs a fork; a deputy reconciles, then "
                "this cadence re-converges."
                % (base, trunk, local[:12], remote[:12]))
        return [line], (post_gc_summary(line) if enforcing else None)
    return ["TRUNK-SYNC kept: ancestry UNKNOWN between '%s' (%s) and %s "
            "(%s) — an unreadable answer moves nothing"
            % (base, local[:12], trunk, remote[:12])], None


def format_gc_summary(root, removed, kept, triage, planned=None, blocked=None,
                      unrowed=None):
    """The one-line estate verdict, and it must distinguish CLEAN from JAMMED.

    "removed=0 kept=40" is literally true of an estate with nothing to remove
    AND of one where every planned removal failed — and it reads as the first.
    Measured 2026-08-04: a run planned TWO removals, completed ZERO, printed
    `removed=0 kept=40 triage=31`, and real time was spent believing the estate
    was clean. The counts were all correct; the sentence was still false.

    `planned`/`blocked` are optional so the dry-run and the phantom/peek paths
    keep their existing shape — a summary only grows the clause when there is
    an unfinished intention to report.

    `unrowed` splits that same falsity out of the TRIAGE count, and it is the
    same lesson one axis over: "triage=65" is literally true of an estate whose
    65 rooms are all tracked and stalling, AND of one where a third of them are
    work no instrument can see — and it reads as the first. An int prints the
    count; the string "UNKNOWN" prints when the ledger could not be read, which
    must never collapse into 0. None omits the clause entirely, for the callers
    (the no-lane-rows branch, envtidy) that have no lane population to measure
    and would otherwise report a measured zero they never took."""
    line = "worktree gc %s: removed=%d" % (
        os.path.basename(root.rstrip(os.sep)), removed)
    if blocked:
        # NAME THE REASON, not just the count: "2 blocked" sends someone
        # hunting, "2 blocked: bound pane" is already the diagnosis.
        why = sorted(set(blocked))
        line += " (%d planned, %d blocked: %s)" % (
            planned if planned is not None else removed + len(blocked),
            len(blocked), "; ".join(why))
    line += " kept=%d triage=%d" % (kept, triage)
    if unrowed is not None:
        line += " (unrowed=%s)" % unrowed
    return line


def post_gc_summary(line):
    """One owner-visible room line per APPLY pass. Mutations are already
    complete, so a chat outage reports to the caller but never rolls them back."""
    try:
        from .. import chat
        chat.post(line, who="worktree-gc")   # a subsystem names itself
        return None
    except Exception as exc:
        return "worktree gc: summary post failed — %s" % exc


def _live():
    """resource -> {holder, remaining}: a lockless TRUE read of the desk
    ledger (seats' own sweep semantics, ZERO writes — claims_list's GC-on-
    read leg stays on the surfaces that own it; a report/scan here, incl.
    the `helm gc` policy row, must never churn .claims.json)."""
    now = seats._now_mono()
    c = seats._sweep(pk.read_json(seats.claims_path(), {}) or {})
    return {r: {"holder": v.get("holder"),
                "remaining": int(v.get("exp_mono", now) - now)}
            for r, v in c.items() if r != "_fence" and isinstance(v, dict)}


def _head(path):
    """The room's HEAD sha, or None. Reported so a human inspecting a
    manual-only room knows where to look; it never authorizes a removal."""
    return vcs.backend(path).head_sha(path)


def _runtime_source_in(path):
    """Whether this running GC imported its own code from `path`.

    Cwd occupancy cannot see imported source. Removing that worktree mid-command
    leaves already-loaded functions alive but makes any later lazy import fail —
    exactly how self-reap completed its mutations and then lost the required chat
    summary. Unknown path relationships KEEP: a later GC launched from another
    checkout can retire the room safely."""
    try:
        room = os.path.realpath(path)
        source = os.path.realpath(__file__)
        return os.path.commonpath((source, room)) == room
    except (OSError, ValueError):
        return True


_OP_MARKERS = (("merge", "MERGE_HEAD"), ("rebase", "rebase-merge"),
               ("rebase", "rebase-apply"), ("cherry-pick", "CHERRY_PICK_HEAD"),
               ("revert", "REVERT_HEAD"), ("bisect", "BISECT_LOG"))


def _current_branch(path):
    """(branch, unreadable) — THREE states, because two of them were one.

    Read at ENACT time, never trusted from the scan: the gap between judging a
    room and deleting it is exactly where a detach+commit hides.

    DETACHED AND UNREADABLE ARE OPPOSITE FACTS AND THEY SHARED None. git says
    so itself and this dropped it: `symbolic-ref --quiet` exits 1 on a
    genuinely detached HEAD and 128 when it cannot read the repository at all
    (measured, both). "This room is on no branch" licenses a write — there is
    no peer lane to advance. "I could not determine this room's identity" must
    NOT, and under the collapsed reading it did: a forced branch-read failure
    still returned a committed write with an empty error. `unreadable` is the
    reason string, or None when the answer is trustworthy."""
    rc, out, err = vcs.backend(path).text(path, "symbolic-ref", "--quiet",
                                          "--short", "HEAD")
    if rc == 0 and out.strip():
        return out.strip(), None
    if rc == 1:
        return None, None                      # genuinely detached
    return None, ((err or "").strip()
                  or "git could not read this room's HEAD (rc %s)" % rc)


def _operation_state(path):
    """Name of the git operation in progress, or None.

    A room mid-merge/rebase/cherry-pick/revert/bisect holds sequencer state
    that is not a commit and not a branch, so neither `-d` nor any reachability
    walk speaks for it. A review named this alongside the detached case and it
    gets the same answer: manual-only. Unreadable reads as IN AN OPERATION —
    the direction that keeps the room."""
    b = vcs.backend(path)
    rc, admin, _e = b.text(path, "rev-parse", "--absolute-git-dir")
    admin = (admin or "").strip()
    if rc != 0 or not admin:
        return "unreadable-gitdir"
    for name, marker in _OP_MARKERS:
        try:
            if os.path.exists(os.path.join(admin, marker)):
                return name
        except OSError:
            return "unreadable-gitdir"
    return None


# A room written to seconds ago is not abandoned work. The window is
# deliberately short: rescue COMMITS and KEEPS, so deferring costs one sweep
# cycle, while sweeping under a live author costs their provenance.
_RESCUE_ACTIVE_S = 900

ACTIVE, IDLE, ACTIVITY_UNKNOWN = "active", "idle", "unknown"


def _room_activity(path, st=None):
    """(state, why) — is somebody still in this room? THREE answers.

    A TRI-STATE, NOT AN AGE, and that is a correction to my own first cut
    (a review on e8336b68699c). I returned "a reason to defer, or None", which
    collapsed "nobody has touched this in an hour" and "I could not read the
    clock" into one word — so a room whose dirty paths were just DELETED, or
    whose only dirty thing is a SUBMODULE (no bounded file clock), or which
    raced a stat, read as ABANDONED and was committed immediately. That is the
    exact provenance loss this whole lane exists to prevent, produced by the
    guard written to prevent it. It is also the same mistake I had just fixed
    one lane over, where a two-state interpreter check turned "I could not
    look" into "it is fine".

    CLOCK SKEW IS UNKNOWN, NOT FRESH. `_room_status` clamps a future mtime to
    age 0 (`max(0, int(now - newest))`) and reports `clock_skew` beside it. A
    binary reader sees 0 < 900 and defers FOREVER while printing "written 0s
    ago", which is a sentence about a duration that does not exist. Nothing
    read that flag until a review named it.

    UNKNOWN REFUSES AN AUTONOMOUS WRITE AND COSTS NOTHING, because the
    operator's door is separate: `helm work release --park` is a person saying
    they want this write. My earlier reasoning for proceeding on an unreadable
    clock — that deferring strands dirty work in a room nobody can vouch for —
    assumed that door did not exist. It does, so the fail-open was buying
    nothing the explicit door does not already buy, and it was paying for it
    in provenance.
    """
    st = _room_status(path) if st is None else st
    if not st.get("dirty"):
        return IDLE, "the room is clean"
    if st.get("clock_skew"):
        return ACTIVITY_UNKNOWN, (
            "a dirty file's mtime is in the FUTURE, so its age was clamped to "
            "zero and no duration measured here means anything")
    if st.get("unknown"):
        return ACTIVITY_UNKNOWN, (
            "the write clock is incomplete (%s), so the newest mtime is a "
            "lower bound on staleness rather than a measurement of it"
            % st["unknown"])
    ago = st.get("wrote_ago")
    if ago is None:
        return ACTIVITY_UNKNOWN, "no dirty path offered a readable write clock"
    if ago < _RESCUE_ACTIVE_S:
        return ACTIVE, ("the room was written %ds ago; a lapsed lease is not "
                        "an abandoned room" % ago)
    return IDLE, "last written %ds ago" % ago


def _wip_commit(path, msg, autonomous=True):
    """The lost-and-found write: stage EVERYTHING, commit --no-verify onto
    the lane's own branch under the janitor identity. The only authored-bytes
    write in this module.

    LOSS-REDUCING ONLY WHILE THE BRANCH IS THIS ROOM'S ALONE, which is why
    `_shared_branch_rooms` gates every caller. Git normally refuses a second
    checkout of one branch, but a forced one puts two rooms on one ref — and
    then this commit advances the OTHER room's lane to whatever THIS room
    happened to be holding. Measured (task/1008): a rescue in a reviewer's
    forced checkout moved the author's HEAD BACKWARDS by two commits while
    their gate was running, so the receipt bound the right tree and the branch
    pointed at an older one. A worktree LOCK does not prevent it: the lock
    guards the ROOM and the hazard is the REF.

    THE GUARD LIVES HERE, AT THE WRITE, AND NOT AT ONE CALLER. The first cut
    put it in `gc_enact`'s rescue branch and left the other two authored
    writes open — `_claims.release_lane(park=True)` WIP-commits onto the lane
    on the park path, and `envtidy` rescues before its own gc. Same function,
    same hazard, two doors nobody guarded. A predicate placed at one call site
    is a predicate the next call site does not inherit, and every one of these
    returns (rc, out, err) already, so refusing here reaches all three with no
    caller changes."""
    # `autonomous` SAYS WHO WANTED THIS WRITE, and it is the only axis on
    # which the preconditions differ. A sweep decided by itself that a room
    # looked abandoned; `helm work release --park` is an operator naming this
    # exact write. Every correctness clause below applies to both — a shared
    # branch or a conflicted index corrupts state whoever asked — but the
    # ACTIVITY clause is about PROVENANCE, and an operator saying "park it"
    # has already supplied the provenance a sweep has to guess at.
    #
    # THE WHOLE PRECONDITION, ENUMERATED ONCE. Four findings on this lane
    # arrived as "one more case", which is the signature of a per-case handler
    # that can never be completed by adding cases. So the question this write
    # must answer is stated as one object rather than grown a clause at a
    # time: (1) is its own branch IDENTITY readable, (2) is this room
    # MID-OPERATION or holding a DANGLING conflict, (3) does a PEER room hold
    # that branch, (4) for an AUTONOMOUS write only, is somebody still in the
    # room. Any unknown refuses.
    #
    # THE ORDER IS LOAD-BEARING AND I GOT IT WRONG FIRST. `_operation_state`
    # answers `unreadable-gitdir` for a path git cannot read at all — correct
    # for its original caller, which fails CLOSED and keeps the room — but
    # asked FIRST at this door it put an unreadable repository behind the
    # words "finish or abort the unreadable-gitdir", instructing the operator
    # to end an operation that does not exist. Asking identity first sends rc
    # 128 to the sentence about identity and leaves rc 1, a genuinely
    # detached room, to the operation check. Caught by the arm written for
    # the round before this one.
    branch, blind = _current_branch(path)
    if blind:
        # IDENTITY FIRST, and this refusal comes BEFORE the registry read: if
        # this room's own branch is unknown there is nothing to compare peers
        # against, and proceeding would be a write whose blast radius nobody
        # measured. Detached is not this case — it is a KNOWN answer of "no
        # branch", and it proceeds.
        return 1, "", (
            "refusing to WIP-commit: this room's own branch could not be read "
            "(%s), so whether the write would advance another room's lane is "
            "UNKNOWN" % blind)
    op = _operation_state(path)
    if op:
        # A SEQUENCER OWNS THIS HEAD. Mid-rebase/merge/cherry-pick the room is
        # DETACHED, so the branch check ABOVE reads "no branch to protect" and
        # would wave the write through — measured: a conflicted rebase gives
        # operation=rebase, branch=(None, None), and the commit LANDED on the
        # sequencer's detached HEAD, where the rebase then discards or
        # conflicts with it. gc_scan already refuses to route such a room to
        # rescue; `release_lane(park=True)` and envtidy do not, and both reach
        # this door.
        return 1, "", (
            "refusing to WIP-commit: this room is mid-%s, so its HEAD belongs "
            "to that operation and a commit here would be written onto state "
            "git is about to rewrite. Finish or abort the %s first; the "
            "uncommitted work is untouched" % (op, op))
    shared, unreadable = _shared_branch_rooms(find_root(path), path, branch)
    if unreadable:
        return 1, "", (
            "refusing to WIP-commit onto %s: the worktree registry could not "
            "be read (%s), so whether another room holds this branch is "
            "UNKNOWN" % (branch or "this room's branch", unreadable))
    if shared:
        return 1, "", (
            "refusing to WIP-commit onto %s: it is ALSO checked out at %s, so "
            "this write would advance THAT room's lane. The uncommitted work "
            "is untouched" % (branch, ", ".join(sorted(shared))))
    st = _room_status(path)
    if st.get("conflicts") and not op:
        # A DANGLING CONFLICT — stages in the index with NO operation in
        # progress, the shape a conflicted `git stash apply/pop` leaves. The
        # operation check above cannot see it (git reports no operation), and
        # `git add -A` would stage the CONFLICT MARKERS and commit a
        # hand-resolution-in-progress under an anonymous message. This refuses
        # for an explicit park too: the operator asked for a park, not for
        # their half-resolved merge to be buried.
        return 1, "", (
            "refusing to WIP-commit onto %s: %d path(s) hold conflict stages "
            "with NO operation in progress, so `git add -A` would stage the "
            "markers themselves. Resolve them per-path (`git restore`) — a "
            "tree-wide reset would destroy the resolution AND every unrelated "
            "change in the room" % (branch or "this room's branch",
                                    st["conflicts"]))
    if autonomous:
        state, why = _room_activity(path, st=st)
        if state is not IDLE:
            return 1, "", (
                "refusing to AUTONOMOUSLY WIP-commit onto %s: %s. A sweep may "
                "not commit authored bytes under a live author, and it may not "
                "read an unreadable clock as an absent one. The operator's own "
                "door is open and unchanged: `helm work release --park` names "
                "this write instead of guessing at it"
                % (branch or "this room's branch", why))
    return vcs.backend(path).wip_commit(path, msg)


def _branch_name(ref):
    """`refs/heads/lane/x` and `lane/x` are ONE branch. Git's worktree registry
    speaks the full ref and helm's lane rows speak the short name, so every
    comparison between them goes through here rather than through whichever
    spelling the caller happened to hold."""
    ref = str(ref or "")
    head = "refs/heads/"
    return ref[len(head):] if ref.startswith(head) else ref


def _shared_branch_rooms(root, path, branch):
    """(others, unreadable) — the OTHER rooms holding `branch` right now.

    THE REGISTRY IS GIT'S, ASKED THROUGH THE DOOR THAT SURFACES FAILURE.
    `worktrees()` keeps [] on error for legacy safety callers, and [] here
    would read as NOBODY ELSE HOLDS IT — an all-clear minted out of a failed
    look, on the one question that decides whether an authored-bytes write is
    safe. `_worktree_records` returns the error instead, so a registry this
    cannot read refuses rather than proceeds.

    A DETACHED ROOM CANNOT COLLIDE and is never counted: it holds no branch,
    so nothing this writes can move it.

    TWO VOCABULARIES, AND ASKING NEITHER OF THEM IS HOW THIS FAILED OPEN. The
    scan row carries the SHORT name (`lane/x`, what `lane_branch` mints) and
    git's registry carries the FULL ref (`refs/heads/lane/x`). The first cut
    compared them raw, so the equality was false for every input and the guard
    could only ever answer NOBODY ELSE HOLDS IT — a refusal that never
    refuses, which is worse than no guard because it reads as one. Caught by
    the arm below, which asserts the branch did not move rather than that the
    right sentence was printed."""
    if not branch:
        return [], None
    rows, err = _worktree_records(root)
    if err:
        return [], err
    here = os.path.abspath(path)
    want = _branch_name(branch)
    return [w["path"] for w in rows
            if _branch_name(w.get("branch")) == want
            and os.path.abspath(w["path"]) != here], None


def _removal_blocker(root, path, lane=None, stale_lease_ok=False,
                     disposable_ok=False, panes_ok=False):
    """Current-state refusal reason, or None when removal may proceed. Called
    at enact time (and again immediately before remove) so a scan cannot
    authorize deleting a newly leased/locked/occupied room. `disposable_ok` is
    used only before the narrow Orca shell retirement leg; UNKNOWN and every
    other process class remain blockers.

    `panes_ok` DEFERS the bound-pane refusal — it does not waive it. It exists
    for exactly one caller: the enact path asks once with it set, so it learns
    whether anything OTHER than a pane blocks removal before spending a close
    on a room it could not remove anyway. That path then closes the panes and
    asks again with NOTHING relaxed, and only a None from that second call
    removes. A pane that did not actually go away therefore still refuses,
    which keeps this a SATISFIED guard rather than a bypassed one."""
    cur = next((w for w in worktrees(root) if w["path"] == path), None)
    if cur is None:
        return "no longer a registered worktree"
    if _runtime_source_in(path):
        return "RUNNING this helm GC binary from the room"
    if lane:
        held = _live().get(resource(root, lane))
        if held:
            return "lease live — %s holds it" % held["holder"]
    occupied = _occupants(path)
    if occupied and not (disposable_ok and all(
            _disposable_worktree_occupant(pid) for pid in occupied)):
        return "OCCUPIED by cwd pid(s) %s" % ",".join(occupied)
    # A PANE OUTLIVES ITS SHELL, so /proc alone cannot clear a room. Deleting a
    # worktree the metaharness still has a terminal in leaves the operator a
    # pane whose cwd no longer exists — it renders as a bare command prompt,
    # and from the outside it is indistinguishable from someone having killed
    # the agent. That is the 2026-07-30 incident: the owner reported seats
    # "killed back to a CWD" repeatedly and was told the seats were fine,
    # because every instrument consulted was a /proc scan. No amount of
    # recovery is cheap here, so this leg refuses on EITHER a bound pane or an
    # unanswerable host — `disposable_ok` deliberately does NOT relax it, since
    # a disposable SHELL says nothing about the PANE wrapped around it.
    if not panes_ok:
        panes, pane_err = _panes_bound_to(path)
        if pane_err:
            return "cannot prove the room is pane-free — %s" % pane_err
        if panes:
            return ("metaharness pane(s) %s are bound to this room"
                    % ",".join(panes))
    if cur["locked"] and not (stale_lease_ok
                              and cur["reason"].startswith("lease:")):
        return "LOCKED: %s" % (cur["reason"] or "no reason")
    return None


def _retire_bound_panes(path):
    """Close the metaharness panes bound to `path` -> ([handles], error).

    THE DEADLOCK THIS BREAKS. The room cannot be removed because a pane is
    bound to it; the pane persists in the owner's sidebar because the room
    exists. helm could already hang up a disposable SHELL
    (_retire_disposable_occupants) but nothing could close the PANE wrapped
    around it, so neither side ever moved and the worktree count only grew.
    Measured 2026-08-04: a reaper run planned two removals, completed ZERO,
    and reported `removed=0` — both blocked on exactly this.

    THE GUARD IS NOT RELAXED, IT IS SATISFIED. _removal_blocker still refuses
    on a bound pane; this leg makes the pane genuinely go away first, and the
    caller then re-asks with NO relaxation. Deleting a worktree out from under
    a live pane leaves the operator a bare prompt indistinguishable from a
    killed agent — the 2026-07-30 incident — so the ordering (close, re-verify,
    THEN remove) is the whole safety property. Reversed, it reproduces that
    incident exactly.

    EVERY UNCERTAINTY KEEPS THE ROOM. A pane list that cannot be read, an
    adapter that will not close, a handle that survives, a close that cannot
    be re-verified: all return an error and the room stays. No SIGKILL, no
    escalation, no second attempt — cleanup never earns the right to guess
    harder, and the cost of a wrong delete here is an owner who cannot tell
    tidying from an agent being killed."""
    panes, err = _panes_bound_to(path)
    if err:
        return [], "cannot prove the room is pane-free — %s" % err
    if not panes:
        return [], None
    from .. import harness
    ad = harness.detect()
    if ad is None:
        # Panes were reported but no adapter answers now: the two readings
        # disagree, and a delete is not the way to resolve that.
        return [], ("metaharness reported pane(s) %s but no adapter is "
                    "available to close them" % ",".join(panes))
    closed = []
    for handle in panes:
        try:
            ad.stop(handle)
        except Exception as exc:          # HarnessError/OSError and anything
            return closed, ("could not close metaharness pane %s: %s: %s"
                            % (handle, type(exc).__name__, exc))
        closed.append(handle)
    # RE-VERIFY AGAINST THE ADAPTER, never against our own success. `stop`
    # returning cleanly says the command was accepted, not that the pane is
    # gone; only a fresh read of the binding proves the room is free.
    still, err2 = _panes_bound_to(path)
    if err2:
        return closed, ("closed pane(s) %s but cannot re-verify the room is "
                        "pane-free — %s" % (",".join(closed), err2))
    if still:
        return closed, ("pane(s) %s are STILL bound after close"
                        % ",".join(still))
    return closed, None


def _retire_disposable_occupants(path):
    """Stop the exact Orca shell-ready placeholder class, or return a refusal.

    SIGHUP models the terminal hangup Orca's interactive placeholder expects;
    Bash ignores SIGTERM in that state. A process that changes shape, gains a
    child, cannot be inspected, or survives the bounded wait is meaningful by
    default and keeps the room. No SIGKILL escalation: cleanup never earns the
    right to guess harder."""
    occupied = _occupants(path)
    if not occupied:
        return [], None
    if not all(_disposable_worktree_occupant(pid) for pid in occupied):
        return [], "OCCUPIED by non-disposable cwd pid(s) %s" % ",".join(occupied)
    stopped = []
    for pid in occupied:
        try:
            os.kill(int(pid), signal.SIGHUP)
            stopped.append(pid)
        except ProcessLookupError:
            continue
        except OSError as exc:
            return stopped, "could not stop disposable Orca shell %s: %s" % (pid, exc)
    pending = set(stopped)
    for _ in range(20):
        pending = {pid for pid in pending
                   if os.path.exists(os.path.join("/proc", pid, "cwd"))}
        if not pending:
            break
        time.sleep(0.05)
    if pending:
        return stopped, "disposable shell(s) %s ignored SIGHUP" % ",".join(sorted(pending))
    left = _occupants(path)  # one full census catches a newcomer after the wait
    if left:
        return stopped, "new occupant(s) %s" % ",".join(left)
    return stopped, None


# ---------------------------------------------------------------------------
# housekeeping — the four-row verdict table (the dumpster is not in it)
# ---------------------------------------------------------------------------

def gc_scan(root, registered=None):
    """One row per room, verdict ∈ keep|triage|rescue|remove.

    Removal requires the complete retirement proof: no live lease, no meaningful
    occupant, no out-of-band lock, an attached branch, a clean tree, and branch
    tip ancestry to the integration base. Unlanded work keeps BOTH its branch and
    worktree and carries owner-facing triage evidence. Dirty work is rescue-
    committed but also kept: the rescue itself makes the branch unlanded.

    Harness-minted rooms use the same proof. Seeing more never means doing more."""
    live = _live()
    rows = []
    rooms = lane_rows(root, registered=registered) \
        + auto_rows(root, registered=registered)
    for w in rooms:
        if _provably_absent(w["path"]):
            continue                  # registry residue rides the phantom pass
        lane = w["lane"]
        held = live.get(resource(root, lane))
        branch = (w["branch"] or "")[len("refs/heads/"):] or None
        occupied = _occupants(w["path"])
        disposable = [pid for pid in occupied if _disposable_worktree_occupant(pid)]
        blocking = [pid for pid in occupied if pid not in disposable]
        r = {"lane": lane, "path": w["path"], "branch": branch}
        if held:
            r.update(verdict="keep", why="lease live — %s holds it, %ds left"
                     % (held["holder"], held["remaining"]))
        elif _runtime_source_in(w["path"]):
            r.update(verdict="keep",
                     why="RUNNING this helm GC binary from the room — rerun "
                         "from another checkout")
        elif blocking:
            r.update(verdict="keep", why="OCCUPIED by cwd pid(s) %s — never remove"
                     % ",".join(blocking))
        elif w["locked"] and not w["reason"].startswith("lease:"):
            r.update(verdict="keep", why="locked out-of-band (%s)"
                     % (w["reason"] or "no reason"))
        else:
            # "Clean" answers only whether uncommitted bytes exist. An attached
            # branch gives Git a lossless authority (`branch -d`); a detached or
            # mid-operation room has no equivalent and remains manual-only.
            op = _operation_state(w["path"])
            if branch is None or op:
                head = _head(w["path"])
                r.update(verdict="keep", manual_only=True, head=head,
                         why=("DETACHED%s — helm cannot prove removing this "
                              "room is lossless (no branch for `-d` to protect), "
                              "so it is never auto-reaped. Inspect and remove by "
                              "hand: HEAD %s"
                              % ((" + mid-%s" % op) if op else "",
                                 (head or "unreadable")[:12])))
            else:
                dirty = _dirty(w["path"])
                if dirty:
                    facts = _branch_triage(root, lane, branch)
                    if occupied:
                        r.update(verdict="triage",
                                 why="idle Orca shell pid(s) %s kept because work "
                                     "is not reapable; %s" %
                                     (",".join(disposable), facts))
                    else:
                        # The rescue commit is necessarily unlanded, so the room
                        # remains too; no ancestry subprocess is needed here.
                        r.update(verdict="rescue",
                                 why="lease-less + DIRTY on %s — rescue-commit, "
                                     "then KEEP room + branch for integration; %s"
                                     % (branch, facts))
                else:
                    # FOUR states, not two. Landed-as-itself and landed-as-
                    # content both retire and SAY WHICH; UNKNOWN is not a clean
                    # negative and can never authorize deleting room or branch.
                    anc = _merge_state(root, branch)
                    if anc in RETIRABLE:
                        r.update(verdict="remove", proof=anc,
                                 why="lease-less + clean + %s — remove room + "
                                     "delete branch" % _proof_word(anc)
                                     + (" after stopping disposable Orca shell pid(s) %s"
                                        % ",".join(disposable) if disposable else ""))
                    else:
                        r.update(verdict="triage", proof=anc,
                                 why=_branch_triage(root, lane, branch,
                                                    state=anc))
        rows.append(r)
    return rows


def gc_enact(root, row):
    """Enforce ONE scan row -> [lines]. Only a fresh, complete LANDED proof
    removes. Rescue writes a commit and then KEEPS the newly-unlanded room.
    Triage rows are report-only. Every destructive premise is re-read."""
    if row["verdict"] in ("keep", "triage"):
        return []
    v = vcs.backend(root)
    if row["verdict"] == "rescue":
        blocked = _removal_blocker(root, row["path"], row["lane"],
                                   stale_lease_ok=True)
        if blocked:
            return ["SKIPPED %s (%s) — kept" % (row["path"], blocked)]
        # THE GUARD IS NOT HERE ANY MORE, and moving it is the fix. It used to
        # live at this call site, which left the two OTHER authored writes
        # through `_wip_commit` — `envtidy._enact_worktree` and
        # `release_lane(park=True)` — sweeping under live authors while this
        # one deferred. `_wip_commit`'s own docstring already said a predicate
        # placed at one call site is a predicate the next call site does not
        # inherit; I wrote the guard here anyway.
        rc, _out, err = _wip_commit(
            row["path"], "wip: rescued %s %s" % (row["lane"], pk.now_ts()))
        if rc != 0:
            return ["SKIPPED %s (%s) — room kept" % (row["path"], err)]
        return ["rescued dirty work -> %s; room kept" % row["branch"],
                _branch_triage(root, row["lane"], row["branch"])]

    # ASK FIRST WITH THE PANE DEFERRED, so a room blocked for some OTHER reason
    # never costs a pane close. Nothing is waived here: the unrelaxed call
    # below is the one that authorizes the delete.
    blocked = _removal_blocker(root, row["path"], row["lane"],
                               stale_lease_ok=True, disposable_ok=True,
                               panes_ok=True)
    if blocked:
        return ["SKIPPED %s (%s) — kept" % (row["path"], blocked)]
    # PANE BEFORE SHELL, and both before any removal. The pane is the object
    # the owner SEES; hanging up the shell first would leave a live pane on a
    # room we are about to delete, which is the 2026-07-30 incident in the
    # order that causes it.
    closed, pane_error = _retire_bound_panes(row["path"])
    lines = (["closed metaharness pane(s) %s" % ",".join(closed)]
             if closed else [])
    if pane_error:
        return lines + ["SKIPPED %s (%s) — kept" % (row["path"], pane_error)]
    stopped, stop_error = _retire_disposable_occupants(row["path"])
    if stopped:
        lines.append("stopped disposable Orca shell pid(s) %s" % ",".join(stopped))
    if stop_error:
        return lines + ["SKIPPED %s (%s) — kept" % (row["path"], stop_error)]
    # NOTHING RELAXED. A pane that did not actually close, an occupant that
    # reappeared, a lease taken in the meantime — all refuse here, and this is
    # the only call whose None permits the delete.
    blocked = _removal_blocker(root, row["path"], row["lane"],
                               stale_lease_ok=True)
    if blocked:
        return lines + ["SKIPPED %s (%s) — kept" % (row["path"], blocked)]

    # Re-prove the exact scan premise. Current branch identity alone is not
    # enough: the branch can advance after scan, or the tree can become dirty.
    cur, cur_blind = _current_branch(row["path"])
    if cur != row.get("branch"):
        # This caller already fails CLOSED on both states — a None never
        # equals a named branch, so the room is kept either way. What it got
        # wrong was the SENTENCE: it reported DETACHED for a room whose HEAD
        # it simply could not read, sending the reader to look for a detach
        # that never happened.
        now = cur or ("UNREADABLE (%s)" % cur_blind if cur_blind else "DETACHED")
        return lines + [
            "SKIPPED %s (was on %s at scan, now %s — the room moved under "
            "the scan) — kept" % (row["path"], row.get("branch"), now)]
    if _dirty(row["path"]):
        return EnactResult(
            lines + ["SKIPPED %s (became DIRTY after scan) — kept for triage"
                     % row["path"]], reclassified=True)
    branch = row.get("branch")
    anc = _merge_state(root, branch) if branch else vcs.UNKNOWN
    if anc not in RETIRABLE:
        facts = _branch_triage(root, row["lane"], branch or "HEAD", state=anc)
        changed = ("landedness became UNKNOWN after scan" if anc == vcs.UNKNOWN
                   else "branch became unlanded after scan")
        return EnactResult(
            lines + ["SKIPPED %s (%s) — %s" % (row["path"], changed, facts)],
            reclassified=True)

    v.unlock_worktree(root, row["path"])            # stale lease tag, if any
    rc, _out, err = v.remove_worktree(root, row["path"])
    if rc != 0:
        return lines + ["SKIPPED %s (%s)" % (row["path"], err)]
    lines.append("removed " + row["path"])
    # The branch delete carries the FRESH state (`anc`), never the scan's — and
    # it names which of the two proofs retired the lane, so the decision is
    # auditable after the fact instead of an unexplained disappearance.
    return lines + _delete_lane_branch(root, row["branch"], anc)


def gc_orphans(path=None):
    """Lease-less lane rooms needing the sweep — `helm gc`'s report-row feed
    (report-only there; `helm work gc --apply` is the actuator). [] outside
    a repo; fail-open total — a janitor row must never error the janitor."""
    try:
        root = find_root(path)
        if not root:
            return []
        return [r["path"] for r in gc_scan(root) if r["verdict"] != "keep"]
    except Exception:
        return []


def _provably_absent(path):
    """True ONLY when the path is provably gone. Any other answer is False.

    The r4 FIX on this lane named the hazard exactly:
    "unreadable live dirs prune". os.path.isdir() SWALLOWS OSError and answers
    False, so a directory that exists but cannot be read — a permission-denied
    tree, an NFS mount that is timing out, a disk that is failing — was
    indistinguishable from one that is gone, and the enact leg deleted its
    registry record. That is fail-open in the destructive direction, which is
    the one direction a prune may never fail open in.

    So the question is asked as a stat and every answer is classified:
      FileNotFoundError / NotADirectoryError -> provably absent, prunable
      any other OSError                      -> CANNOT SEE, never prunable
      stat succeeds                          -> something is there, never prunable
    The last line also narrows the old behaviour deliberately: a FILE sitting
    at a worktree path used to count as phantom. It no longer does. A prune is
    registry loss and a file there is a state nobody predicted — the
    conservative read costs manual triage, while the permissive one costs a
    record that a live tree needed."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


_PHANTOM_OWNER_KINDS = {
    "work": ("lane", "harness"),
    "estate": (None,),
}


def _is_phantom_record(root, row, owner):
    return managed_room_kind(root, row["path"]) in _PHANTOM_OWNER_KINDS[owner] \
        and not row["locked"] and _provably_absent(row["path"])


def phantom_scan(root, owner="work", registered=None):
    """(approved, excluded, error) from one registry snapshot for one owner.

    The excluded set is evidence too: apply verifies those records survived, so
    a future actuator cannot quietly reintroduce the global-prune failure while
    still reporting only the approved candidates."""
    if registered is None:
        registered, error = vcs.backend(root).worktrees(root)
        if error:
            return [], [], error
    owned = [r for r in registered
             if managed_room_kind(root, r["path"])
             in _PHANTOM_OWNER_KINDS[owner]]
    approved = [r["path"] for r in owned if _is_phantom_record(root, r, owner)]
    allowed = set(approved)
    return approved, [r["path"] for r in owned if r["path"] not in allowed], None


def phantom_records(root, registered=None):
    """Missing records owned by `helm work gc`: lane + harness rooms.

    A record with no directory is registry residue: Git keeps it indefinitely
    and metaharnesses render it as a ghost room. The cleanup-owner classifier is
    the boundary — unmanaged estate belongs to `helm worktree gc`, while peeks
    belong to `helm work peek --drop`."""
    return phantom_scan(root, registered=registered)[0]


def estate_phantom_records(root, registered=None):
    """Missing, unlocked records in the remaining unmanaged estate."""
    return phantom_scan(root, owner="estate", registered=registered)[0]


def prune_phantom_records(root, approved, excluded=None, owner="work"):
    """(removed_paths, error, unknown) for an approved path set.

    Every path is re-authorized against a fresh registry read immediately before
    the VCS seam removes that exact administrative record without touching the
    checkout path. A global `worktree prune` is never used: it would let one
    approved phantom authorize deletion of every record
    Git considers stale, including unreadable live records excluded by the scan.

    The final stat-to-record interval is not transactional, so a revival may
    require metadata repair; the record-only VCS operation makes that race
    incapable of deleting authored bytes. The final registry read is the
    evidence source."""
    approved = list(dict.fromkeys(approved or ()))
    excluded = list(dict.fromkeys(excluded or ()))
    if not approved:
        return [], None, False
    v, failures = vcs.backend(root), []
    for index, path in enumerate(approved, 1):
        rows, read_error = v.worktrees(root)
        if read_error:
            failures.append("registry re-read failed before approved record "
                            "#%d: %s" %
                            (index, str(read_error).strip()[:120]))
            break
        row = next((r for r in rows if r["path"] == path), None)
        if row is None:
            continue                         # a concurrent cleanup already won
        if not _is_phantom_record(root, row, owner):
            failures.append("approved record #%d no longer clears the removal "
                            "proof" % index)
            continue
        rc, _out, err = v.remove_worktree_record(root, path)
        if rc != 0:
            failures.append("approved record #%d: %s" %
                            (index, (err or "remove refused").strip()[:120]))
    rows, read_error = v.worktrees(root)
    if read_error:
        return [], ("targeted worktree removal completed but the registry "
                    "re-read failed, so removal is UNKNOWN: %s" %
                    str(read_error).strip()[:160]), True
    registered = {r["path"] for r in rows}
    gone = [p for p in approved if p not in registered]
    collateral = [p for p in excluded if p not in registered]
    remaining = len(approved) - len(gone)
    if collateral:
        failures.insert(0, "%d excluded record(s) disappeared during the "
                        "targeted pass" % len(collateral))
    if remaining or failures:
        detail = "; ".join(failures[:3])
        suffix = ("; " + detail) if detail else ""
        return gone, ("%d approved phantom record(s) remain registered%s" %
                      (remaining, suffix)), False
    return gone, None, False


# ---------------------------------------------------------------------------
# the room board + the rail
# ---------------------------------------------------------------------------

def held_lane_files(root, exclude=None, registered=None):
    """[(lane, [files])] — every ACTIVELY-HELD lane and the files it authored.

    THE DISCLOSURE HALF OF lane_overlaps, and it exists because that function
    cannot answer at the moment the question is asked. lane_overlaps COMPARES
    two lanes' authored diffs, so it needs both to have commits; a lane being
    CLAIMED has none, and asking there returns silence — a guard that says
    "clear" exactly when you consult it is worse than no guard.

    So the claim seam discloses instead of comparing: here are the lanes that
    are live and here is what they are in. The claimer knows what they are
    about to touch and this is the one fact they cannot get from the board.

    Measured need, 2026-08-04: THREE seats built the #183 tree-warning fix in
    parallel under three different lane labels — which-helm-warning-scope,
    treewarn-fires-only-in-a-helm-checkout, fix-183-tree-warning-scope — all in
    helm/cli.py. The label guard cannot see a shared FILE, and lane_overlaps
    could, but it lives in `gc`: it reports the collision to whoever runs
    housekeeping, after the duplicate work is written.

    Same held-lease filter as lane_overlaps (an unheld room is nobody racing
    you) and the same _authored fileset, deliberately reusing both so the two
    surfaces cannot drift into disagreeing about who is live."""
    v = vcs.backend(root)
    live = _live()
    trunk = _trunk(root)
    cache, out = {}, []
    for w in lane_rows(root, registered=registered):
        lane = w["lane"]
        if lane == exclude:
            continue
        branch = (w["branch"] or "")[len("refs/heads/"):]
        if not branch or not live.get(resource(root, lane)):
            continue
        # TRI-STATE, because `or ()` COLLAPSED AN UNKNOWN INTO A DENIAL.
        # _authored returns _EVERYTHING when it could not look — no trunk
        # resolvable, merge-base non-zero, diff non-zero — and _EVERYTHING is
        # an EMPTY frozenset subclass, so it is FALSEY. `sorted(x or ())` then
        # produced [], indistinguishable from a branch that genuinely authored
        # nothing, and the claim seam rendered "CLAIMED, nothing authored yet"
        # about a lane that may have authored plenty. A cap, a timeout or a
        # failed read must yield UNKNOWN and never a confident answer; this
        # one manufactured the confident answer out of the failure itself.
        # (Found in review.)
        authored = _authored(v, root, trunk, branch, cache)
        files = None if authored is _EVERYTHING else sorted(authored)
        # A LANE THAT HAS AUTHORED NOTHING IS STILL DISCLOSED, and it is the
        # one that matters most. The docstring above says a guard that answers
        # "clear" exactly when you consult it is worse than no guard — and
        # `if files:` reintroduced that silence on the OTHER side of the same
        # seam. The lane being CLAIMED has no commits, which is why this
        # function discloses instead of comparing; every lane it discloses had
        # to have commits, so two seats who both noticed one defect minutes
        # apart, neither having written a line, each got told the coast was
        # clear. That is not the edge case, it is the CENTRAL case: the
        # collision window is exactly the interval before either side writes
        # code. Measured 2026-08-05 on 13 live lanes — 12 disclosed, 1 dropped,
        # and the dropped one was an actively-worked lane with an open
        # dispatch (helm#1, and a duplicate claim by two seats that hour).
        out.append((lane, files))
    # ORDER BY URGENCY, three classes. UNKNOWN first — a lane whose history
    # could not be read is the one a claimer can conclude least about and so
    # must see. Then authored-nothing, whose bare NAME is the only signal that
    # exists before either side writes code. Files-bearing lanes last: they
    # carry the most information per row and a claimer can compare them.
    # The caller budgets PER CLASS rather than slicing this list, so a run of
    # one class can no longer starve another.
    out.sort(key=lambda lf: (0 if lf[1] is None else 1 if not lf[1] else 2,))
    return out


def moved_lane_targets(root, registered=None):
    """[(lane, [paths])] — files a LIVE lane authored that TRUNK NO LONGER HAS.

    THE SIBLING OF held_lane_files, ONE AXIS OVER. That one answers "who else
    is editing this file NOW"; this one answers "did the file I am editing
    MOVE OR DIE ON TRUNK SINCE I BRANCHED". Both are disclosures at the same
    seam and they deliberately share _authored, _trunk and the held-lease
    filter so the two cannot drift into disagreeing about who is live.

    MEASURED NEED, 2026-08-05: a lane edited helm/web.py:254; the web split
    landed and moved that code to helm/web_core.py, leaving web.py a 156-line
    facade. TWO OUTCOMES WERE AVAILABLE and only one is loud. The rebase
    conflicted, which is git working correctly. The SILENT branch is the
    expensive one: had the hunks not collided, the edit would have applied to
    a file that no longer holds the caller, the real caller would have kept
    calling a name the same commit deleted, and the owner's endpoint would
    have raised on first click — with a GREEN GATE the whole way, because the
    gate measured the OLD base. It was caught because an agent read a chat
    post carefully. That is a coincidence, not a mechanism.

    A CREATED FILE IS ABSENT FROM TRUNK BY DESIGN, so absence alone is the
    wrong test and would warn on every new module — this function's own lane
    added helm/tasks.py. The discriminator is the lane's MERGE-BASE: a path
    that existed where the lane branched and does NOT exist on trunk is a
    path trunk moved or deleted. A path absent from both is simply new.

    DISCLOSURE, NEVER REFUSAL. The legitimate answer is often "yes, I know, I
    am the one splitting it" — a refusal would have blocked the very split
    that motivated this. Say the thing; let the reader judge.

    CANNOT-LOOK IS NOT CLEAR, the same rule every guard here follows. No
    resolvable trunk, no merge-base, an unreadable diff, and an UNKNOWN
    authored set all yield SILENCE for that lane rather than an all-clear.

    THE `is _EVERYTHING` CHECK IS DEFENCE, NOT LOAD-BEARING TODAY, and an
    earlier draft of this docstring overstated it. _EVERYTHING is an EMPTY
    frozenset, so an UNKNOWN set already intersects to nothing and the lane is
    already skipped — a truthiness test would behave identically RIGHT NOW.
    What the identity check buys is (a) it survives the sentinel ever gaining
    members, which is the only reading under which the two differ, and (b) it
    skips a merge-base spawn. Measured by a reviewer who broke it three ways.

    IT DOES NOT TAKE `exclude`, UNLIKE ITS SIBLING, AND THAT IS DELIBERATE.
    held_lane_files answers "who ELSE is live" so it drops the claimer;
    this answers "did trunk move a file THIS lane is in", and the holder is
    exactly the person who can act on that. Re-claiming your own lane should
    name your own moved targets."""
    v = vcs.backend(root)
    trunk = _trunk(root)
    if not trunk:
        return []
    live = _live()
    cache, out = {}, []
    for w in lane_rows(root, registered=registered):
        lane = w["lane"]
        branch = (w["branch"] or "")[len("refs/heads/"):]
        if not branch or not live.get(resource(root, lane)):
            continue
        files = _authored(v, root, trunk, branch, cache)
        if files is _EVERYTHING:
            continue
        rc, base, _e = v.text(root, "merge-base", branch, trunk)
        base = base.strip()
        if rc != 0 or not base:
            continue
        # ONE SUBPROCESS PER LANE, NOT TWO PER AUTHORED PATH. The first cut
        # asked cat-file twice per file and cost ~600 spawns on a fifteen-lane
        # board — paid on EVERY claim, before the claimer's own success line.
        # --diff-filter=D over base..trunk is both halves of the discriminator
        # in one call: a path git reports DELETED existed at the branch point
        # (or it could not be deleted) and does not exist on trunk.
        #
        # --no-renames IS LOAD-BEARING. With rename detection on, git pairs the
        # delete with the add and the moved path VANISHES from the output —
        # the command returns empty for exactly the case this function exists
        # to catch. Measured both ways.
        # DELETION IS THE RARE CASE; DRAINAGE IS THE ONE THAT HAPPENS.
        # The first cut asked only --diff-filter=D and was SILENT on its own
        # founding incident: the real web split left helm/web.py in place as a
        # 156-line facade, so the path exists at BOTH refs and no delete is
        # ever reported. Measured on 6e965966->4b39c7ec: 3805 lines -> 156,
        # +36/-3685, --diff-filter=D empty. A guard blind to the case that
        # motivated it is decoration, and only pointing the predicate at the
        # REAL commit pair found that — my fixture had used `git mv`, a shape
        # production does not mint.
        #
        # NO THRESHOLD, AND NO COMPARISON EITHER. A later cut asked whether
        # trunk removed MORE than the lane changed; a reviewer named it as a
        # tuned threshold wearing a ratio of 1.0, which makes detection depend
        # on HOW MUCH THE LANE TYPED rather than on what happened to the file.
        # So this states the FACT and declines to judge it: trunk touched a
        # path this lane is also editing, and here is what it did. The reader
        # decides whether -3685 matters — the same disclosure discipline the
        # sibling follows. Naturally rare: it needs BOTH sides to have touched
        # the same path.
        #
        # TWO SUBPROCESSES PER LANE, NEITHER PER PATH. numstat carries every
        # changed path with its counts; the deletion probe is a second call
        # only because numstat CANNOT tell a deleted file from one emptied in
        # place — both read 0/N — and mislabelling that sends a reader hunting
        # a file that is still there. --no-renames is load-bearing on both:
        # with rename detection on, git pairs the delete with the add and the
        # moved path vanishes from the output entirely.
        rc_d, stat, _e4 = v.text(root, "diff", "--numstat", "--no-renames",
                                 base, trunk, "--")
        if rc_d != 0:
            continue
        rc_g, killed, _e6 = v.text(root, "diff", "--name-only",
                                   "--diff-filter=D", "--no-renames",
                                   base, trunk, "--")
        if rc_g != 0:
            # CANNOT-LOOK IS NOT CLEAR, and the first cut laundered it: an
            # unreadable deletion probe became `set()`, which is
            # indistinguishable from "nothing was deleted" and flows straight
            # into the drainage branch — so a genuinely DELETED file would be
            # reported as merely drained, with a label the evidence does not
            # support. This function's own docstring promises silence on an
            # unreadable input and this line broke that promise two calls
            # later. Skip the lane; an unlabelled event is worse than none.
            continue
        deleted_paths = set(killed.split("\n")) - {""}
        gone = ["%s (deleted on trunk)" % q
                for q in sorted(deleted_paths & set(files))]
        for row in stat.split("\n"):
            cols = row.split("\t")
            if len(cols) != 3 or cols[2] not in files:
                continue
            add, dele, path = cols
            if path in deleted_paths:
                continue                      # already named, once
            a = int(add) if add.isdigit() else 0
            d = int(dele) if dele.isdigit() else 0
            if not (a or d):
                continue
            gone.append("%s (trunk +%d/-%d since you branched)" % (path, a, d))
        if gone:
            out.append((lane, sorted(gone)))
    return out


def lane_overlaps(root, registered=None):
    """[(lane_a, lane_b, [shared files])] — open lanes whose OWN work touches
    the same file. The board's only answer to 'is someone else already on
    this?'

    A LEASE ANSWERS A NARROWER QUESTION THAN THE ONE PEOPLE ASK OF IT. It
    guards a worktree+branch, so it answers 'is anyone else editing this
    ROOM' — never 'is anyone else fixing this DEFECT'. Two seats can each hold
    a valid, uncontested lease on two different lanes aimed at one bug and the
    board reads perfectly healthy. That is not a hypothetical: 2026-07-30 it
    happened THREE times in one day, and every time a human reading chat
    caught it, never a surface.

      roster-test-owns-its-globals x roster-test-owns-the-cockpit-stamp
        -> both rewrote tests/test_seats.py for the same red; main stayed
           broken for hours while each seat deferred to the other
      context-recovery x the landed seat-identity fix
        -> both rebuilt the same fixture in tests/test_seat_identity.py; found
           only when the merge conflicted, after both were written and gated

    FILE OVERLAP IS A PROXY, NOT THE THING, and it is stated as one: two lanes
    can fix one defect in different files, and two can share a file
    legitimately. It is the strongest signal available from state the board
    ALREADY has, and it is checked against both controls — it fires on both
    real incidents above and stays silent on lr-land x never-track, which
    genuinely never collided.

    Diffs each tip against the PAIR'S OWN merge-base, never against the trunk:
    once a lane lands, merge-base(tip, trunk) IS the tip and its diff is empty,
    so a trunk-based probe reports zero overlap for every historical pair. That
    is how the first version of this check scored both known incidents clean.
    BOTH LANES MUST BE ACTIVELY HELD, and that filter is what makes this a
    signal instead of noise. Raw file overlap across every open room is O(N^2)
    and hub-dominated: measured on the live board it produced 26 pairs, most of
    them "we both touched tests/test_seats.py", which is true of nearly every
    lane and tells nobody anything. A PARKED lane is not racing you — its
    holder is gone. Two LIVE leases whose work has converged on one file is the
    shape that actually cost a day: both roster lanes were held by active
    seats, both mid-flight, both certain the other was handling it.

    THE LIMITATION, stated rather than papered over: a lane can only collide
    here with another LANE. The ctx-recovery duplicate collided with work that
    had already LANDED on the trunk, which no lease can represent, so this
    would not have caught it. It catches the live-vs-live case, which is 2 of
    today's 3.

    THE PAIR-BASE IS THE CANDIDATE SET, NOT THE ANSWER — a narrowing added
    2026-08-03 after the false-positive rate was measured rather than argued.
    When two lanes sit on DIFFERENT trunk points, merge-base(a, b) is the older
    of the two bases, so the fresher lane's diff against it includes every
    trunk commit it merely INHERITED by being rebased later. Measured on
    lane/gemini-window-750k against lane/worktree-reap-cadence: 30 files in its
    "own" diff, 29 of them landed trunk commits, 4 genuinely its own. Across
    the whole live board that produced 29 reported pairs of which 10 — 34% —
    shared nothing but inherited landings. It inflates worst exactly when the
    fleet lands fast, which is when the board is read most.

    SO EACH LANE'S FILES ARE INTERSECTED WITH ITS OWN DIFF AGAINST ITS OWN
    MERGE-BASE WITH TRUNK, which is the set it actually authored. Verified over
    all 29 live pairs: it removed all 10 false ones and kept all 19 real ones.

    AND THE FALLBACK IS THE POINT, NOT AN EDGE CASE. Once a lane LANDS,
    merge-base(tip, trunk) IS the tip and its own-diff is EMPTY — the exact
    reason the first version of this check was pair-based at all, and why a
    trunk-based probe scored both founding incidents clean. So an empty
    own-diff falls back to the pair-base set rather than erasing the lane.
    In practice only actively-held lanes reach here and a landed lane is not
    held, but the historical controls run on pairs that HAVE landed, and this
    is what keeps them green.
    """
    v = vcs.backend(root)
    live = _live()
    trunk = _trunk(root)
    files, own, order = {}, {}, []
    for w in lane_rows(root, registered=registered):
        branch = (w["branch"] or "")[len("refs/heads/"):]
        if not branch or not live.get(resource(root, w["lane"])):
            continue          # unheld room: nobody is racing you for it
        order.append((w["lane"], branch))
    out = []
    for i, (lane_a, br_a) in enumerate(order):
        for lane_b, br_b in order[i + 1:]:
            rc, base, _e = v.text(root, "merge-base", br_a, br_b)
            if rc != 0 or not base:
                continue          # unrelated histories: no shared question
            # keyed by (branch, base) because the SAME branch has a different
            # own-diff against a different pair-base — caching on branch alone
            # would answer the second pair with the first pair's files
            for br in (br_a, br_b):
                if (br, base) not in files:
                    rc2, out2, _e2 = v.text(root, "diff", "--name-only",
                                            base + ".." + br)
                    files[(br, base)] = (set(out2.split("\n")) - {""}
                                         if rc2 == 0 else set())
            a = _authored(v, root, trunk, br_a, own) & files[(br_a, base)]
            b = _authored(v, root, trunk, br_b, own) & files[(br_b, base)]
            shared = sorted(a & b)
            if shared:
                out.append((lane_a, lane_b, shared))
    return out


# The seam rung's file filter. Deliberately the SAME extension set the stop
# ladder's `_CODE_EDIT_RE` already uses, for the reason that module states in
# as many words: a rung that fires on prose edits is wallpaper. Two lanes
# colliding in docs/VERBS.md have a text merge, which git resolves or reports;
# they do not have an untested COMPOSITION, which is a property of code that
# calls other code. Measured on the 34 green lanes of the 2026-08-23 board:
# 160 pairs share an authored file, 117 share an authored CODE file.
_SEAM_CODE = re.compile(
    r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|go|rb|sh|bash|zsh|c|h|cc|cpp|hpp"
    r"|java|kt|kts|swift|php|pl|lua|sql|proto|toml|yaml|yml|json)$", re.I)
# NO TRAILER SHAPE CONSTANTS LIVE HERE ANY MORE. `_SEAM_WORD` and
# `SEAM_OWNER_WORDS` stood at this spot after the trailer discharge was deleted
# — a word-shape regex and a word-count floor, unreferenced by any caller. They
# were the residue of three rounds of hardening a SHAPE check against a CONTENT
# property, and leaving them is how round four happens: the next reader finds a
# ready-made vocabulary for "a well-formed trailer" and rewires it. A discharge
# is a receipt bound to the composed tree; there is no shape a seat can type.
_SEAM_OID = re.compile(r"[0-9a-f]{40,64}")


def _own_commits(v, root, trunk, branch, cache):
    """The shas `branch` alone carries (trunk..branch), or [] if unreadable.

    UNREADABLE COLLAPSES TO EMPTY HERE ON PURPOSE, which is the opposite of
    `_authored`'s rule and needs saying. `_authored` widens on failure because
    a missed collision cost a day; this list is used only to ask "has this half
    ever been GREEN", and an empty list makes that answer NO, which makes the
    seam rung silent. A read failure must not manufacture a block."""
    if branch in cache:
        return cache[branch]
    out = []
    if trunk:
        rc, txt, _e = v.text(root, "rev-list", trunk + ".." + branch)
        if rc == 0:
            out = [x.strip() for x in txt.split("\n") if x.strip()]
    cache[branch] = out
    return out


def seam_rooms(root, registered=None):
    """(rooms, err) — every worktree of `root` with liveness and holder resolved
    from a UNION OF PRODUCERS; rows are
    {path, branch, holder, seats, live, why, holder_why}.

    `err` IS THE COULD-NOT-TELL CHANNEL and it is not decoration: every input
    here can fail, and each failure used to read as an ANSWER — an unreadable
    registry as "no rooms", a failed /proc walk as "nobody home". Both silently
    delete peers, which is the one direction this rung must never fail in.

    THE UNION IS THE WHOLE POINT, AND IT IS A CORRECTION. The first cut keyed
    on `lane_rows` + the claim ledger, i.e. on HELM BOOKKEEPING, and a
    read-only measurement killed it: `helm work list` returns NO LANE ROOMS for
    the sibling project this rung was asked for, which has zero rows on every
    structured helm surface — 0 of 2,422 dispatches, 0 of 2,425 gate receipts,
    0 of 43 land requests, 0 work claims. A rung on that signal fires for helm
    and never for the project whose incidents are the entire rationale. A guard
    that only works where the bookkeeping is already good is a guard for the
    team that needs it least.

    So the ROOM SET is `git worktree list` — every repo has one — and LIVENESS
    comes from whichever producer can speak:

      OCCUPANCY, universal: a live process whose cwd is inside the room. One
        /proc pass for every room at once (24ms measured over 89 worktrees).
        Measured 2026-08-23: 7 of helm's 89 worktrees occupied, 2 of the
        sibling's 6 — a filter that curates itself in both repos, where the
        registry alone does not (helm keeps 89 rooms, most of them parked).
      THE HELM LEASE, where the ledger has rows: a claimed lane counts live
        even with no process inside it, which is the delegated-build window
        `_delegated_build` exists for.

    THE ROSTER NEVER MAKES A ROOM LIVE, and that is deliberate: rows go stale
    (one seat's `last_seen` was ~25h old while this was written) and
    liveness must be earned by a positive present-tense signal. It is used for
    ATTRIBUTION only.

    THE HOLDER IS RESOLVED IN A STATED ORDER, AND THE ORDER IS WHY THIS SURVIVES
    THE PREMISE THAT KILLS ITS LAST LEG. `seat-liveness-is-environ-not-cwd-and-
    not-comm` says, in as many words, that reading HELM_CHAT_NAME from
    /proc/<pid>/environ "cannot see a claude-native seat AT ANY INSTANT, because
    those seats never declare that variable" — and it is right, measured here
    again: this lane's own room shows FOUR occupying pids and ZERO declared
    names, the seat running the measurement being exactly the seat the probe
    cannot see. Environ is therefore the LAST leg, never the only one:

      1. THE LEASE holder (helm's own claims ledger). This is what makes a
         claude-native seat attributable at all — the integrator seat resolves on
         this box from its lane lease while its environ says nothing.
      2. THE ROSTER (`helm chat seats`' own store), matched by the seat's
         recorded cwd landing inside the room. The premise names this surface as
         the instrument that works, and it covers every family: measured
         2026-08-23, all 15 rostered seats carry a cwd, claude-native included.
      3. ENVIRON, last, for a live process no roster row explains.

    The premise's must-hit — SEED EVERY SCAN WITH YOURSELF — is what this
    ordering answers: legs 1 and 2 both see the seat doing the seeing, and leg 3
    does not, so leg 3 may never be the only voter.

    `seats` IS THE TRI-STATE, because `holder=None` had two opposite causes and
    collapsing them hid the more interesting one. Measured on the same box:
    this lane's room declares NOTHING (the claude-native hole above), while the
    SHARED CHECKOUT declares THREE names at once. Absence and disagreement are
    not the same fact and the second is a signal in its own right, so the list
    is carried out whole and `holder_why` names which leg spoke.

    THE ROSTER LEG MAY NAME A HOLDER BUT MAY NEVER EXCLUDE ONE, and that split
    is a review's finding 4. A roster row outlives its process, so a STALE row
    naming this seat inside a peer's room was enough to grant the same-seat
    exemption and drop that peer — an exclusion earned by a stale artifact,
    which is the exact rule this module quotes the liveness premise for.
    `holder_why` is therefore load-bearing downstream: attribution accepts all
    three legs, the EXEMPTION accepts only the present-tense ones.

    `lane_rows` IS DELIBERATELY NOT USED, and not only for the sibling project:
    it matches direct children of `<root>-wt/` only, so helm's OWN seat rooms at
    `helm-wt/seats/<seat>` — five of the seven occupied rooms on this box —
    were invisible to it too. The narrow reader was wrong about both repos.

    THE ARITY IS (rows, err, degraded), AND THE THIRD IS NOT DECORATION. `err`
    means NO ANSWER — the room set itself is unreadable, so the caller must say
    UNKNOWN and stop. `degraded` means AN ANSWER WITH A HOLE IN IT: the rooms
    resolved, but one of the attribution inputs did not, so the seat sets below
    are NARROWER than the world. That case had no representation at all, and
    narrower is the dangerous direction here: the co-tenancy disclosure fires on
    "more than one seat in this room", so an input that silently names fewer
    seats turns a blind spot into a clean bill — the exact inversion this whole
    module exists to refuse, committed in its own plumbing. A caller that
    ignores `degraded` reads a partial census as a complete one."""
    degraded = []
    projscope.spend_or_raise("composition seam lease census")
    try:
        live = _live()
        projscope.spend_or_raise("composition seam lease census result")
    except projscope.Expired:
        raise
    except Exception as e:                 # noqa: BLE001
        # THE LEASE LEDGER IS AN INPUT LIKE ANY OTHER, and it was the one read
        # standing OUTSIDE this guard. A raise here left the function through
        # the stop gate's blanket handler, which renders as (None, None) — a
        # clean stop. The ledger supplies the delegated-build liveness leg, so
        # losing it deletes every leased-but-empty peer room.
        return [], "the lease ledger could not be read (%s)" \
            % type(e).__name__, []
    try:
        projscope.spend_or_raise("composition seam worktree registry")
        rows = registered if registered is not None else worktrees(root)
        projscope.spend_or_raise("composition seam worktree registry result")
    except projscope.Expired:
        raise
    except Exception as e:                 # noqa: BLE001
        # AN UNREADABLE REGISTRY IS NOT AN EMPTY ONE (a review's finding 3). This
        # raised through the stop gate's blanket handler, which turned "I could
        # not list the rooms" into total silence — indistinguishable from "there
        # are no rooms". Say so instead.
        return [], "the worktree registry could not be read (%s)" \
            % type(e).__name__, []
    paths = [os.path.abspath(w["path"]) for w in rows]
    try:
        projscope.spend_or_raise("composition seam process census")
        occ, census_ok = _occupants_many(paths)
        projscope.spend_or_raise("composition seam process census result")
    except projscope.Expired:
        raise
    except Exception as e:                 # noqa: BLE001
        occ, census_ok = {}, False
    if not census_ok:
        # SAME RULE ONE INPUT OVER. An empty `occ` reads exactly like "nobody is
        # anywhere", which silently deletes every occupancy-live peer — and
        # occupancy is the ONLY liveness producer outside helm's own ledger, so
        # in a repo with no leases this failure erases the whole rung while
        # looking like a clean board.
        return [], "the room occupancy census could not be read", []
    projscope.spend_or_raise("composition seam roster census")
    rostered, roster_err = _rostered_seats(paths)
    projscope.spend_or_raise("composition seam roster census result")
    if roster_err:
        # THE ROSTER IS THE LEG THAT SEES THE CLAUDE-NATIVE FAMILY AT ALL, so
        # losing it does not just lose names — it loses exactly the names no
        # environ scan can recover, and the room those seats share is the one
        # the rung is already blind inside. Not fatal (rooms and occupancy still
        # resolved), so it degrades rather than erroring; but it may never pass
        # as an answer.
        degraded.append(roster_err)
    out = []
    for index, (w, path) in enumerate(zip(rows, paths)):
        projscope.spend_or_raise("composition seam room %d" % index)
        branch = (w["branch"] or "")[len("refs/heads/"):]
        if not branch:
            continue                       # a detached room authors no half
        # The lease leg, asked only of a room that could carry one: see
        # `_seam_held`, which the stop-facts witness shares.
        held = _seam_held(root, path, live)
        pids = [p for p in (occ.get(path) or []) if str(p).isdigit()]
        declared, unread = _occupant_seats(pids)
        if unread:
            # A PID WHOSE ENVIRON WOULD NOT OPEN IS NOT A PID DECLARING
            # NOTHING. Both used to produce the same empty contribution, so a
            # room full of unreadable processes resolved to "no seats here" —
            # and this rung reads a one-name room as unshared. Named per room
            # so the disclosure can say which room it could not finish reading.
            degraded.append("%d process(es) in %s would not answer for a seat "
                            "name — that room's seat set is a FLOOR, not a "
                            "census" % (unread, os.path.basename(path)))
        seats_here = sorted(set(rostered.get(path) or ()) | set(declared))
        holder = str((held or {}).get("holder") or "").strip() or None
        holder_why = "lease" if holder else ""
        if not holder and len(seats_here) == 1:
            holder = seats_here[0]
            holder_why = ("roster" if seats_here[0] in
                          (rostered.get(path) or ()) else "environ")
        elif not holder:
            # NAME THE TWO KINDS OF SILENCE. `shared` means several seats
            # answer to this room and no single one holds it; `unknown` means
            # nothing could be read at all. A caller that cannot tell them
            # apart cannot tell "I must not guess" from "there is nobody here".
            holder_why = "shared" if len(seats_here) > 1 else "unknown"
        out.append({"path": path, "branch": branch, "holder": holder,
                    "seats": seats_here, "holder_why": holder_why,
                    "live": bool(held) or bool(pids),
                    "why": "lease" if held else ("occupied" if pids else "")})
    projscope.spend_or_raise("composition seam room census completion")
    return out, None, degraded


def _seam_held(root, path, live):
    """The lease row that makes a room live without an occupant, or None.

    THE LEASE LEG IS ASKED ONLY OF A ROOM THAT COULD CARRY ONE. A lane
    resource is `worktree:<project>:<basename>`, so a nested room like
    `helm-wt/seats/codex` collides with a LANE named `codex` and would
    inherit that lane's holder — attributing a seam to the wrong seat.
    `managed_room_kind` is the registry's own answer to which rooms are
    lanes; everything else is live by occupancy alone."""
    if managed_room_kind(root, path) != "lane":
        return None
    return live.get(resource(root, os.path.basename(path)))


def seam_lease_marks(root, paths, live):
    """{room path: [held, holder]} — what `seam_rooms`' lease leg reads for
    each lane room among `paths`, and nothing that moves on its own (a
    lease's remaining time is not in it). The stop-facts resident records it
    beside the census and the stop re-derives it from the claims file, so a
    lease taken, released, handed or expired since the census reads STALE."""
    if not root:
        return {}
    out = {}
    for path in paths or ():
        path = os.path.abspath(path)
        if managed_room_kind(root, path) != "lane":
            continue
        held = _seam_held(root, path, live or {})
        out[path] = [bool(held),
                     str((held or {}).get("holder") or "").strip()]
    return out


def _rostered_seats(paths):
    """{room path: [seat names]} from the ROSTER's own recorded cwd.

    HELM'S OWN SURFACE, WHICH THE LIVENESS PREMISE SAYS OUTRANKS ANY PROCESS
    SCAN — and the leg that makes the claude-native family attributable, since
    those seats never export HELM_CHAT_NAME for an environ scan to find.
    Longest-prefix match, so a seat sitting in a lane room is attributed to that
    room and not to the checkout above it, and a seat sitting outside every room
    (two rostered seats sit in $HOME) is attributed to none.

    ATTRIBUTION ONLY, NEVER LIVENESS: a roster row outlives the process it
    describes, so this may name a seat that has gone. The caller keeps liveness
    on the lease and the /proc pass, and this only answers WHO a live room
    belongs to.

    -> ({room path: [seat names]}, err). THE ERR IS THE CORRECTION. This used to
    fail to a bare `{}`, which is byte-identical to a roster that was read
    perfectly and named nobody — so an unreadable roster silently NARROWED the
    seat set of every room. Narrowing is not a neutral failure here: the
    co-tenancy disclosure fires on a room with more than one seat, so fewer
    names means fewer blind-spot warnings, and the instrument's own failure
    renders as a cleaner world. A stale roster does the same thing one degree
    down and is left ALONE on purpose — a stale row still NAMES, and this leg's
    standing rule is that the roster may name a holder but may never exclude
    one, so ageing a row out would be the same narrowing with a timestamp for
    an excuse."""
    try:
        rows = pk.read_json(seats.roster_path(), {}) or {}
    except Exception as e:                 # noqa: BLE001
        return {}, ("the seat roster could not be read (%s) — the seats in "
                    "each room are UNKNOWN, not absent" % type(e).__name__)
    if not isinstance(rows, dict):
        return {}, ("the seat roster is not a table of rows — the seats in "
                    "each room are UNKNOWN, not absent")
    real = {}
    for p in paths:
        try:
            real[p] = os.path.realpath(p).rstrip(os.sep)
        except OSError:
            continue
    out = {}
    for name, row in rows.items():
        if not isinstance(row, dict):
            continue
        cwd = row.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        try:
            here = os.path.realpath(cwd).rstrip(os.sep)
        except OSError:
            continue
        best = None
        for p, rp in real.items():
            if (here == rp or here.startswith(rp + os.sep)) and \
                    (best is None or len(rp) > len(real[best])):
                best = p
        if best:
            out.setdefault(best, []).append(str(name))
    return {k: sorted(v) for k, v in out.items()}, None


def _occupant_seats(pids):
    """([seat names], unreadable count) the room's occupants DECLARE.

    A LIST, NOT A NAME, because the two ways this can fail to produce one need
    opposite readings and an earlier cut returned None for both. Measured
    2026-08-23: each single-seat room resolves to exactly one declared name
    (codex, codex-3, codex-4, gemini, grok) beside unlabelled helper processes,
    which is why unlabelled pids are DROPPED rather than counted; the SHARED
    CHECKOUT resolves to three at once; and this lane's own room resolves to
    NONE while four pids sit in it, because a claude-native seat never exports
    the variable (`seat-liveness-is-environ-not-cwd-and-not-comm`).

    So the caller gets the whole set and decides. This function is the LAST
    holder leg for exactly that reason: it is blind to the family running it.

    THE SECOND RETURN IS THE THIRD STATE. A pid whose environ would not open
    and a pid that opened and declared nothing both contributed exactly
    nothing, so "I could not read these processes" and "these processes are
    not seats" were one observable. They are not the same fact: the first
    means the room's seat set is a FLOOR and the co-tenancy disclosure may be
    silent because the instrument failed, not because the room is unshared.
    Counted rather than named, because a pid this function cannot open is a
    pid it also cannot describe.

    A PID THAT HAS SIMPLY EXITED IS COUNTED TOO, and that is deliberate: the
    census that produced this list was taken moments ago, so a vanished pid is
    still a process this pass could not answer for, and the honest reading of
    the room is the same either way — the set below is a floor."""
    names, unreadable = set(), 0
    for pid in pids[:64]:                  # bounded: a busy room has dozens
        try:
            with open("/proc/%s/environ" % pid, "rb") as f:
                blob = f.read(65536)
        except OSError:
            unreadable += 1
            continue
        for kv in blob.split(b"\0"):
            if kv.startswith(b"HELM_CHAT_NAME="):
                got = kv.split(b"=", 1)[1].decode("utf-8", "replace").strip()
                if got:
                    names.add(got)
    return sorted(names), unreadable


def seam_candidates(root, mine, holder=None, registered=None, green=None,
                    rooms=None, memo=None):
    """(rows, err) — TWO GREEN HALVES WHOSE COMPOSITION NOBODY RAN.

    Owner, 2026-08-23: "Two green halves with an untested composition — this
    seems to be the biggest failure mode across projects." This is the
    measurable projection of that sentence, and every clause is a filter that
    was chosen against a counted population rather than argued for.

    `mine` is a set of ABSOLUTE ROOM PATHS this seat is working in — its cwd's
    worktree, plus any lane rooms it holds a helm lease on. A row is
    `{path, branch, peer_path, peer, peer_holder, files, tree}` and means all
    six of:

      1. Both rooms are LIVE (`seam_rooms`: occupied, or helm-leased) and the
         peer's holder is not KNOWN to be mine. Two rooms driven by one seat
         are not a seam — that seat can see both — but an UNKNOWN holder never
         buys that exemption; see the clause where it is applied.
      2. NEITHER branch's work has already LANDED (`_merged`, the authority
         `helm work release` prints LANDED-by-ancestry and LANDED-by-patch-
         identity from). A landed branch on an unreaped room is not a half, and
         this clause is what keeps the rung's noise from scaling with the number
         of stale rooms the fleet keeps. Asked of the BRANCH rather than each
         commit, deliberately and following that instrument's own reasoning: a
         stack with some commits landed and some not IS still a half, and
         `_merge_state` answers NOT_ANCESTOR there, so the branch-level question
         is already the right one.
      3. BOTH branches have AUTHORED something (`_authored`, the same set
         `lane_overlaps` and `held_lane_files` use, so the surfaces cannot
         drift about who wrote what). A branch with no commits has no half.
      3. They share an authored CODE file (`_SEAM_CODE`).
      4. BOTH halves are GREEN, meaning each branch's CURRENT TREE carries
         admissible gate evidence — one bar, no tiers. THIS IS ALSO THE ARM
         PREDICATE: requiring it of both halves is what proves the receipt
         mechanism is live on the objects being measured, which is the honest
         capability question. A "banked" tier used to sit here (own commits
         plus a clean room, where the repo mints no receipts) and it is GONE:
         that is evidence a half EXISTS, never evidence about a COMPOSITION,
         so accepting it in exactly the repos that cannot observe testing was
         laundering by tier. The cost is real — this rung is SILENT in a repo
         that mints no receipts rather than blocking with a cure that proves
         nothing (task/1378 carries the honest replacement).
      5. NO GREEN RECEIPT EXISTS AT THE COMPOSED TREE, where that tree can be
         computed. `git merge-tree --write-tree` yields the exact tree the two
         halves make together, and a receipt records the TREE it ran against —
         so "an arm was run against the composed tip" is a set membership, not
         a story. Of the 40 clean-merging pairs on the 2026-08-23 helm board,
         THREE already had one: the discharge is not a hypothetical shape, it
         is something the fleet already does. `merges` says whether the tree
         existed, because it changes what the caller may promise.
      6. A CONFLICTING MERGE IS STILL A SEAM, AND AN EARLIER CUT OF THIS
         FUNCTION HAD THAT BACKWARDS. It filtered conflicts out as "the loud
         case — git will make somebody look", which is true about the TEXT and
         says nothing about the BEHAVIOUR: a hand-resolved merge is an untested
         composition by construction. The filter was defended as noise control
         (77 of 117 helm pairs conflict) and the measurement behind that number
         was the bad one — it treated all 34 lane rooms as live. With liveness
         measured for real, helm has SEVEN live rooms and four code-overlapping
         pairs among them, so the conflict filter buys nothing it is worth
         paying for. And it cost everything on the other side: EVERY
         code-overlapping pair in the sibling project conflicts — four of four, on
         branches ~830 commits divergent — so the filter made the rung
         structurally unable to fire for the project that asked for it. It is
         gone; the merge outcome now selects the MESSAGE, never membership.
      7. NEITHER HALF CONTAINS THE OTHER, ASKED OF THE BRANCHES AND NOT OF
         ANCESTRY ALONE. "Containment falls out of clause 6" holds for the
         sha-identity case only: if the peer's tip is an ancestor of mine,
         merge-tree returns MY tree and my tree is green, so no row forms. An
         integrator's compose train is the case that breaks it — it carries
         both halves REBASED, so its commits are patch-identical and
         object-different, merge-base is no longer the branch point, and
         merge-tree composes a tree nobody ever gated. Measured: FIVE
         collisions on one seat, every one of them against a compose train
         that ALREADY CARRIED the colliding lanes, so the rung refused that
         seat's stop for work a green train had already composed.

         So containment asks `vcs.landed_state` — the SAME authority clause 2
         asks of the trunk, ancestry first and `git cherry` behind it — of the
         two BRANCHES, and the discharge is the container's CURRENT tree
         carrying an admissible same-repo receipt. `local`, never `trees`:
         the rule clause 6 states, for the same reason. A container whose tree
         is NOT green discharges nothing and the row still forms — the train
         exists, and nothing has run it.

    THE DISCHARGE IS MEASURED, AND THERE ARE TWO DOORS ONTO ONE IDEA: a
    same-repo receipt bound to the tree the composition ACTUALLY IS. Where the
    halves are separate that tree is the merge (clause 6); where one already
    carries the other it is the container's own tree (clause 7), and computing
    a merge there answers a question nobody asked. A `Seam: <branch>` commit
    trailer is NOT a third door and cannot become one: prose cannot prove
    testing or accountable transfer, and a SHAPE check on a CONTENT property
    hardens forever without ever deciding the question — a bare token clears,
    then a token plus filler, then whitespace. CAPABILITY AND DISCHARGE
    ARE ABOUT DIFFERENT TREES (clause 4 asks the half trees, clauses 6 and 7
    ask the tree of the composition), which is why they do not collapse into a
    rung that can only fire where there is nothing left to block.

    (rows, fatal error, warning): a fatal error accompanies no rows. Legacy-
    placement UNKNOWN may accompany rows already proven by canonical bindings;
    callers keep those rows and surface the narrower warning separately. UNKNOWN
    is never an all-clear and never erases authority proved by the canonical ledger.

    COST, because this runs at every Stop of every seat. Each clause gates the
    next and the cheap ones run first: no room of mine, or no other live room,
    returns after one /proc pass and one `git worktree list`. Beyond that the
    receipt read measured 57ms over 2,425 rows, the tier probe 8-40ms, and
    each surviving peer ~23ms of merge-base+diff plus one merge-tree. Clause 7
    adds one `landed_state` per pair that reaches it — ancestry first, so the
    `git cherry` behind it is paid only where ancestry says no — and it is
    asked BEFORE the merge-tree it makes pointless, on the pairs that would
    otherwise block.

    `memo` SHARES THE PER-BRANCH AND PER-PAIR READS ACROSS CALLS, and only
    the stop-facts resident passes one (helm/stopfacts_resident.py). It asks
    this function once per live room, so without it every room re-derives
    every peer's landedness, authored set and tip tree. Every entry is keyed
    by branch name, so a memo is valid only while no ref moves: the resident
    builds a fresh one per refresh, which is the lifetime these caches
    already have inside one call."""
    projscope.spend_or_raise("composition seam setup")
    v = vcs.backend(root)
    trunk = _trunk(root)
    if not trunk:
        return [], "no trunk ref resolvable — seams are UNKNOWN, not absent", None
    mine_paths = {os.path.abspath(p) for p in (mine or ())}
    # `rooms` is accepted so the STOP GATE can compute the room census once and
    # spend one /proc pass per stop instead of two — it wants the same census to
    # answer the co-tenancy disclosure below.
    if rooms is None:
        projscope.spend_or_raise("composition seam room read")
        rooms, rerr, _degraded = seam_rooms(root, registered=registered)
        projscope.spend_or_raise("composition seam room result")
        if rerr:
            return [], rerr, None
    ours = [r for r in rooms if r["path"] in mine_paths]
    peers = [r for r in rooms if r["path"] not in mine_paths and r["live"]]
    if not ours or not peers:
        return [], None, None  # measured, and genuinely nobody to compose with
    # TWO SETS, AND THE SECOND IS SMALLER ON PURPOSE. `trees` arms a half —
    # unplaceable evidence counts, because excluding a half on the strength of a
    # reaped room is an exclusion earned by silence. `local` discharges — only
    # evidence provably minted in THIS repository may end a block. See
    # `green_receipts` for why one filter cannot serve both doors.
    if green is None:
        from .. import gateimport
        projscope.spend_or_raise("composition seam receipt read")
        trees, local, gerr, gwarn = green_receipts(
            root, binding_budget_s=gateimport.BINDING_CANDIDATE_BUDGET_S)
        projscope.spend_or_raise("composition seam receipt result")
        if gerr:
            return [], gerr, None
    else:
        trees, local = green if isinstance(green, tuple) else (green, green)
        gwarn = None
    # NO REPO TIER. `_repo_has_receipts` used to sit here and pick the
    # "strongest evidence THIS repo produces", and a review killed both halves of
    # that idea across three exchanges.
    #
    # IT WAS UNSOUND: it answered by asking `cat-file` whether ANY receipt tree
    # resolved as an object here, and helm worktrees SHARE ONE OBJECT STORE, so
    # a foreign receipt could flip a whole clone's tier. Capability must never be
    # inferred from object presence.
    #
    # AND THE WEAKER TIER WAS NEVER EVIDENCE ABOUT A COMPOSITION. "banked" meant
    # the branch has its own commits and its room is clean — that a half EXISTS
    # and its author put it down. This rung blocks on an UNTESTED COMPOSITION, so
    # accepting proof-of-existence in exactly the repos that cannot observe
    # testing was laundering by tier, inside the guard written to complain about
    # laundering.
    #
    # WHAT REPLACES IT is not a second query over the same tree. Capability and
    # discharge are about DIFFERENT TREES, which is why they do not collapse:
    # this rung ARMS when BOTH CURRENT HALF TREES carry admissible evidence —
    # which is what proves the receipt mechanism is live on the objects actually
    # being measured — and BLOCKS while the exact composed tree lacks it. An
    # earlier cut of mine made capability "an admissible receipt exists for the
    # tree being concluded about", and a review refused it as vacuous: the
    # composed tree lacks a receipt BY DEFINITION, so that rung could only arm
    # where there was nothing left to block.
    #
    # A repo that mints no receipts therefore has no green halves and the rung is
    # SILENT there rather than blocking with a cure that proves nothing. That is
    # a real cost, not a free simplification — it means this rung does not serve
    # a no-receipt project at all, and task/1377 (a receipt carries no durable
    # repo identity) is where the honest version of that question lives.
    memo = {} if memo is None else memo
    auth, ranges, pairfiles, tips, landed, contained, bases, merged = (
        memo.setdefault(k, {}) for k in ("auth", "ranges", "pairfiles",
                                         "tips", "landed", "contained",
                                         "bases", "merged"))

    def _landed_room(room):
        """A room whose work has REACHED THE TRUNK is not a half.

        MEASURED FALSE POSITIVE, 2026-08-24, and it fired on this very lane. The
        rung reported `lane/composition-stop-rung` against `seat/codex-3` on
        helm/seats.py. That branch carries exactly ONE commit ahead of trunk, two
        weeks old, and `git cherry origin/main seat/codex-3` prints it with a
        MINUS — helm's own notation for ALREADY ON TRUNK BY PATCH IDENTITY.
        That half is not a half; it is landed work sitting on an unreaped
        branch, and there is nothing to compose.

        IT SCALES, WHICH IS WHY IT IS NOT ONE BAD ROW. The fleet keeps 74
        worktrees and most carry branches whose work landed long ago; if a
        landed half counts as a half, this rung's noise grows with the number of
        unreaped rooms, which is precisely how a correct guard gets muted and
        then ignored. On this board it is the difference between firing on TWO
        rooms and firing on twenty.

        `_merged` IS THE EXISTING AUTHORITY AND IS CALLED RATHER THAN COPIED.
        It is the boolean projection of `_merge_state`, which `helm work
        release` already trusts to print LANDED by ancestry and LANDED by patch
        identity — ancestry first, then `git cherry` with a count cross-check,
        because our protocol lands work REBASED so almost nothing arrives under
        the sha its author wrote. A second implementation of one question is
        exactly the defect class this rung exists to police, and shipping the
        rung committing it would be the joke writing itself.

        UNKNOWN KEEPS THE ROOM, and that falls out of the instrument rather
        than being re-decided here: `_merged`'s own contract is that True is
        PROOF of containment and False covers a clean negative AND an
        unreadable one, so an unreadable branch stays a candidate half. Same
        rule as every other exclusion in this rung — earned by a positive
        signal, never by silence."""
        br = room["branch"]
        if br not in landed:
            try:
                projscope.spend_or_raise("composition seam landedness")
                landed[br] = _merged(root, br)
                projscope.spend_or_raise("composition seam landedness result")
            except projscope.Expired:
                raise
            except Exception:              # noqa: BLE001
                landed[br] = False         # UNKNOWN keeps the room
        return landed[br]

    def _contained(inner, outer):
        """Is every commit of `inner` already in `outer`, by object OR by
        patch identity? True is PROOF; False covers a clean negative AND an
        unreadable one, and both keep the pair — the same rule `_merged` states
        for the trunk question, because an exclusion must be earned by a
        positive signal and never by silence."""
        key = (inner, outer)
        if key not in contained:
            try:
                projscope.spend_or_raise("composition seam containment")
                contained[key] = v.landed_state(root, inner, outer) in RETIRABLE
                projscope.spend_or_raise("composition seam containment result")
            except projscope.Expired:
                raise
            except Exception:              # noqa: BLE001
                contained[key] = False     # UNKNOWN keeps the pair
        return contained[key]

    def _tip_tree(branch):
        """The branch's CURRENT tree oid, or None."""
        if branch not in tips:
            rc, out, _e = v.text(root, "rev-parse", branch + "^{tree}")
            out = out.strip()
            tips[branch] = out if rc == 0 and _SEAM_OID.fullmatch(out) else None
        return tips[branch]

    def _green_half(room):
        own = _own_commits(v, root, trunk, room["branch"], ranges)
        if not own:
            return False      # nothing of its own: no half to compose
        # THE HALF IS GREEN IFF ITS CURRENT TREE WAS TESTED, and there is no
        # longer any other way to be green. This asked "does ANY commit in this
        # branch's history carry a receipt", which is how a gate on commit one of
        # five marked all five green (a review's finding 1, "historical receipts
        # read falsely GREEN"). A TREE rather than a head because a commit that
        # does not change content — an empty one — moves the head and leaves the
        # tree alone, so keying on heads would make such a commit look like
        # losing the greenness.
        #
        # THIS IS ALSO THE ARM PREDICATE. `seam_candidates` calls it on BOTH
        # rooms and requires both, so "both current half trees carry admissible
        # evidence" is expressed by this returning True twice rather than by a
        # separate capability query. The tier branch that used to live below it
        # is gone; see the note at `trees` for why.
        tip = _tip_tree(room["branch"])
        return bool(tip) and tip in trees

    out = []
    for ours_index, ours_room in enumerate(ours):
        projscope.spend_or_raise(
            "composition seam own room %d" % ours_index)
        br_a = ours_room["branch"]
        if _landed_room(ours_room):
            continue          # my work is already on the trunk: nothing to compose
        a_files = _authored(v, root, trunk, br_a, auth)
        if a_files is _EVERYTHING or not a_files or not _green_half(ours_room):
            continue
        for peer_index, peer in enumerate(peers):
            projscope.spend_or_raise(
                "composition seam peer %d" % peer_index)
            br_b = peer["branch"]
            if br_b == br_a:
                continue      # two rooms, one branch: not two halves
            # THE EXEMPTION IS EARNED, NEVER ASSUMED. Skipping a peer is an
            # EXCLUSION — the outcome that leaves no trace — and helm's own
            # liveness premise states the rule for those in as many words:
            # "exclusion ... must be earned by a POSITIVE signal ..., never by
            # silence or by inequality". So a peer whose holder is UNKNOWN
            # (`holder_why` shared or unknown) is KEPT, and the rung stays WIDE
            # where it cannot tell. The cost of keeping is one dischargeable
            # block on a seam that may be one seat's own; the cost of dropping
            # is silence on the exact case the rung exists for, and only the
            # second failure is invisible.
            if seam_peer_exempt(peer, holder):
                continue      # both rooms are mine: one seat sees both halves
            if _landed_room(peer):
                continue      # a landed branch on an unreaped room is not a half
            b_files = _authored(v, root, trunk, br_b, auth)
            if b_files is _EVERYTHING or not b_files:
                continue
            if (br_a, br_b) not in bases:
                rc, base, _e = v.text(root, "merge-base", br_a, br_b)
                bases[(br_a, br_b)] = base.strip() if rc == 0 else ""
            base = bases[(br_a, br_b)]
            if not base:
                continue      # unrelated histories: no shared question
            for br in (br_a, br_b):
                if (br, base) not in pairfiles:
                    rc2, out2, _e2 = v.text(root, "diff", "--name-only",
                                            base + ".." + br)
                    pairfiles[(br, base)] = (set(out2.split("\n")) - {""}
                                             if rc2 == 0 else set())
            shared = sorted((a_files & pairfiles[(br_a, base)])
                            & (b_files & pairfiles[(br_b, base)]))
            shared = [f for f in shared if _SEAM_CODE.search(f)]
            if not shared or not _green_half(peer):
                continue
            # DISCHARGED BY CONTAINMENT, ASKED BEFORE THE MERGE-TREE THAT
            # CANNOT SEE IT. When one branch already carries the other's work,
            # the composition is not a tree to be computed — it is the
            # container's own tree, and clause 7 says why merge-tree answers
            # the wrong question about a rebased train. Placed here rather
            # than at the top of the pair loop because it costs a `git cherry`
            # over the smaller branch: every cheaper filter above has already
            # dropped the pairs that were never going to form a row, so this
            # runs only on pairs that would otherwise BLOCK.
            container = (br_b if _contained(br_a, br_b) else
                         br_a if _contained(br_b, br_a) else None)
            # THE TREE IS BOUND AND TESTED, matching the clause below rather
            # than coercing None into "" and leaning on `green_receipts`
            # filtering empty trees two functions away. A membership test a
            # distant filter makes safe is a fail-open waiting for that filter
            # to change.
            ctree = _tip_tree(container) if container else None
            if ctree and ctree in local:
                continue      # DISCHARGED: the composition is the container's
                              # tree and an arm ran against it, in this repo.
            if (br_a, br_b) not in merged:
                rc3, tree, _e3 = v.text(root, "merge-tree", "--write-tree",
                                        br_a, br_b)
                tree = tree.strip().split("\n")[0].strip()
                # rc 0 is a CLEAN merge and the tree is the composition. rc 1
                # is a CONFLICT — git also prints a tree there, and reading it
                # as the composition would let a conflicted merge discharge
                # itself. rc 128 is an error, or a git too old for
                # --write-tree.
                merged[(br_a, br_b)] = (tree if rc3 == 0
                                        and _SEAM_OID.fullmatch(tree)
                                        else None)
            composed = merged[(br_a, br_b)]
            if composed and composed in local:
                continue      # DISCHARGED: an arm ran against the composed tree
                              # HERE. `local`, never `trees`: a receipt nobody
                              # can place, or one placed in another repository,
                              # must not be able to END a block.
            # NO TRAILER DISCHARGE. It stood here for two rounds and a review
            # killed it on the third, correctly: PROSE CANNOT PROVE TESTING OR
            # ACCOUNTABLE TRANSFER. Round 1 a bare `Seam: peer` cleared; round 2
            # `Seam: peer` plus any three filler words cleared; round 3 would
            # have been whitespace or a near-miss token, because every cure was
            # SHAPE-based where the property is CONTENT-based. A string a seat
            # can type is not evidence that anyone composed and ran the two
            # halves, and hardening the string check a third time would have
            # bought a fourth round.
            #
            # THE RULED REPLACEMENT is a canonical same-repo receipt bound to
            # the exact composed tree — the clause immediately above — or a
            # DURABLE helm obligation bound to this composed_id and accepted by
            # a resolvable live seat. MEASURED: no such obligation primitive is
            # wired. `obligation.py` binds a LAND ROW through
            # `landreq.ball_holder`, keyed on rid/state/owed_since, with no
            # composed_id and no acceptance verb; `handoff.py` is
            # compaction-continuity prose. The dispatch ledger IS a durable
            # obligation accepted by a live seat, but it binds a TIP rather than
            # a composed_id and nothing mints one for a seam. So the second
            # discharge does not exist yet and is NOT pretended into existence
            # here — task/1378 carries it. Until it lands, the composed-tree
            # receipt is the only discharge, and the remediation text says so
            # rather than offering a trailer that would clear nothing.
            # THE COMPOSED IDENTITY, for the caller's latch (finding
            # 2). The latch keyed on the (branch, peer) PAIR, so when a half
            # MOVED the composition became a different one and the latch
            # suppressed the very re-arm it exists to permit. This names WHAT
            # the seam contains: the merged tree where one exists, and the two
            # tip trees where the merge conflicts and there is no merged tree
            # to name. Either way a moved half changes it.
            composed_id = composed or "%s+%s" % (_tip_tree(br_a) or "?",
                                                 _tip_tree(br_b) or "?")
            out.append({"composed_id": composed_id,
                        "path": ours_room["path"], "branch": br_a,
                        "peer_path": peer["path"], "peer_branch": br_b,
                        "peer_holder": peer["holder"], "peer_why": peer["why"],
                        # `peer_branch`, not `peer`: the display-launder
                        # tripwire treats a `.get("peer")` as a CHAT-ROW
                        # identity read and would have listed this module in a
                        # registry whose law is about laundering chat rows.
                        # This value is a git ref, and naming it so keeps a
                        # security registry free of a false entry — and reads
                        # better beside `branch`.
                        "files": shared, "tree": composed,
                        "merges": composed is not None})
    projscope.spend_or_raise("composition seam completion")
    return out, None, gwarn


def seam_peer_exempt(peer, holder):
    """Does `holder` provably drive the peer room too? Then the pair is one
    seat's to see, never a seam. ONE PREDICATE for `seam_candidates` and
    `seam_assemble`, so the stop's reading of the resident's facts cannot
    exempt a peer the rung itself would have kept: the exemption is EARNED by
    a present-tense holder leg (a lease, or the occupant's own declaration),
    never by the roster, which outlives its process, and never by silence."""
    return bool(holder) and isinstance(peer, dict) \
        and peer.get("holder") == str(holder).strip() \
        and peer.get("holder_why") in ("lease", "environ")


def seam_assemble(rooms, per_room, mine, holder=None, root_err=None,
                  green_err=None, green_warn=None):
    """`seam_candidates(root, mine, holder, rooms=rooms)`'s answer, rebuilt
    from that function's own answers for ONE room at a time.

    WHY IT EXISTS. The stop-facts resident cannot know which rooms a stopping
    seat will call its own — that comes from the stop's cwd and its leases —
    so it asks `seam_candidates(root, {room}, holder=None, rooms=rooms)` for
    every LIVE room and records the rows (`per_room`). A pair's row depends on
    the two rooms only; what depends on the seat is WHICH pairs count: the
    peers are the live rooms outside `mine`, and a peer this seat provably
    drives is exempt. So the seat's answer is the union, over its rooms, of
    those rows whose peer is outside `mine` and not exempt — in the order the
    one-call version yields them (own rooms in census order, peers in census
    order). An arm pins that the two agree on a real repository.

    `root_err` is the call's answer with NO room of mine (the trunk check,
    which runs before anything else); `green_err`/`green_warn` are
    `green_receipts`' error and legacy warning, which the one-call version
    reports only once it has a room of mine and a peer. A row whose peer is
    not in `rooms` is KEPT: an exclusion must be earned by a positive signal.

    -> (rows, err, warning), or None when a room of mine that has a peer was
    not computed (`per_room` has no entry for it) — the caller says UNKNOWN.
    """
    if root_err:
        return [], root_err, None
    mine_paths = {os.path.abspath(p) for p in (mine or ())}
    ours = [r for r in rooms if r["path"] in mine_paths]
    peers = [r for r in rooms if r["path"] not in mine_paths and r["live"]]
    if not ours or not peers:
        return [], None, None
    if green_err:
        return [], green_err, None
    by_path = {r["path"]: r for r in rooms}
    out = []
    for room in ours:
        got = per_room.get(room["path"])
        if got is None:
            return None
        for row in got:
            peer_path = row.get("peer_path")
            if peer_path in mine_paths:
                continue
            if seam_peer_exempt(by_path.get(peer_path), holder):
                continue
            out.append(row)
    return out, None, green_warn


def _focused_inadmissible(row, consuming_repo=None):
    """Why a receipt that is NOT a whole-suite run proves nothing, or None
    for a VERIFIABLE FOCUSED one.

    Verifiable means the kind whose content id binds its scope (v6, the one
    `helm gate run --focus` mints), a runner-reported RAN set that is a
    non-empty list, a positive test count, an interpreter helm chose, and
    every row clause gate owns (clean bracket, exit code). A custom `--`
    command carries none of that, and a pre-v6 row wearing a focus block
    carries a scope nothing protects: both stay inadmissible, at both doors."""
    try:
        from .. import gate
    except Exception as e:                       # noqa: BLE001
        return "gate module unimportable (%s) — admissibility UNKNOWN" \
            % type(e).__name__
    focus = row.get("focus")
    if row.get("v") != gate.FOCUSED_VERSION or not isinstance(focus, dict):
        return "not a whole-suite run, and no verifiable focused scope " \
            "(a custom command, or a focus block no content id protects)"
    executed = focus.get("executed")
    if not isinstance(executed, list) or not executed \
            or not all(isinstance(m, str) and m for m in executed):
        return "a focused run whose runner reported no module ran proves " \
            "nothing about any tree"
    ran = row.get("ran")
    if type(ran) is not int or ran <= 0:
        return "reports %r executed tests — a focused run that executed " \
            "none proves nothing about any tree" % (ran,)
    if not gate._ident_of(row).get("name"):
        return "the focused run names no interpreter helm chose"
    return gate.row_refusal(row, about=str(row.get("tree") or "")[:12],
                            consuming_repo=consuming_repo)


def receipt_inadmissible(row, consuming_repo=None):
    """Why a gate receipt PROVES nothing about a whole tree, or None.

    `consuming_repo` is THE REPOSITORY WHOSE TREES THIS ANSWER DISCHARGES, and
    it is handed straight to `gate.row_refusal`: a row carrying a DECLARED
    command is admitted only where an authored declaration covers the
    repository about to spend it, and the row's own `repo_id` is a field of the
    row. Without it a receipt labelled with another repository's path could
    bring that repository's declaration here.

    THE DEFECT THIS CLOSES IS THAT A DISCHARGE WAS MERELY PRESENT, NEVER
    VERIFIED (reproduced four ways). A row was admitted on `status ==
    "OK"` alone, so a HISTORICAL receipt on an early commit marked a whole
    branch green, a DIRTY-tree receipt did, a receipt whose tree MOVED under
    the run did, and a FOCUSED receipt did. Each of those is a row that exists
    without proving the thing the rung claims, and "it exists" is the weakest
    possible reading of "this half passed".

    So one predicate, consulted at BOTH discharge doors, and every clause is a
    property of the row rather than a judgement about it:

      status OK — the run concluded and passed.
      suite is EXACTLY True — a whole-suite run — OR a VERIFIABLE FOCUSED
        run (`_focused_inadmissible`, task/3039). A focused claim is its
        SCOPE, not its tree, which is why one cannot authorize a land. It
        still answers this rung's question: a lane's rounds are focused and
        `helm gate run` refuses a lane whole suite, so a whole-suite-only
        reading would find no green half in any lane and no discharge the
        block could print. A focused run on the MERGED tree selects the merged diff's
        consumers, both halves together, which is the composition's own
        question. `is True` rather than truthiness because a custom-argv row
        records `suite` false too, and it stays refused.
      not dirty, before or after — a run over uncommitted bytes measured
        something no tree records, so no tree can inherit its result.
      head and tree UNMOVED across the run — the clean bracket. If the tree
        moved while the suite ran, the recorded tree is not what was tested.

    A row this refuses is not a lie and not a failure; it is evidence about a
    narrower claim than the one being made of it."""
    if row.get("status") != "OK":
        return "status %r" % (row.get("status"),)
    if row.get("suite") is not True:
        # A VERIFIABLE FOCUSED RECEIPT COUNTS (task/3039). A lane's rounds are
        # focused now, and `helm gate run` refuses a lane whole suite, so a
        # rung that read only whole-suite receipts would find no green half in
        # any lane and no discharge the block could print. It stays ONE
        # predicate for both doors: a focused run on a half's tree is that
        # half's verification, and one on the merged tree tests the
        # composition, since its selection is the merged diff's consumers.
        return _focused_inadmissible(row, consuming_repo)
    # THE `suite` FLAG IS A CLAIM AND THE ARGV IS THE RUN. Nothing cross-checked
    # them, so a row could assert `suite: true` beside a command naming ONE test
    # module and be read as "everything passed" — a WEAK receipt wearing the
    # whole-suite word, which is this clause's entire subject. `gate` already
    # owns the parser that answers it and it is ASKED rather than copied;
    # `runner=None` is the right door here because stored whole-suite receipts
    # name two runners: `-m unittest`, which the gate spawns, and the legacy
    # `-m helm.gaterunner discover`, accepted and never produced (see
    # gate._STORED_SUITE_RUNNERS). The single-module form rejects those
    # legitimate legacy receipts; the pair admits both runners that ever
    # minted a whole-suite receipt and still refuses argv that names
    # test ids. A sliced (v10) row answers from its validated evidence
    # instead, through the same `gate.stored_whole_suite` door. It is a CLOSED PAIR and not "any module with a
    # discovery-shaped tail": a whole-suite claim is worth exactly as much as
    # the runner that could have produced it, and this reader can neither
    # inspect nor re-run an arbitrary command, so a receipt naming one has
    # nothing behind its claim and must arm and discharge nothing.
    try:
        from .. import gate as _gate
        shaped = _gate.stored_whole_suite(row)
    except Exception as e:                       # noqa: BLE001
        return "the recorded command could not be read (%s) — whether this " \
            "was a whole-suite run is UNKNOWN" % type(e).__name__
    if not shaped:
        return "claims a whole-suite run but its command names explicit " \
            "targets (%s)" % " ".join(str(a) for a in (row.get("argv") or ()))
    # A RUN THAT EXECUTED NOTHING PASSED NOTHING. `ran` is the runner's own
    # count and a whole-suite OK row with zero of them is a green light for an
    # empty set — the purest form of the vacuity this rung exists to refuse.
    # Measured: zero OK rows in the ledger report ran<=0, and all 19 suite rows
    # that do are already UNKNOWN, so this refuses nothing honest.
    ran = row.get("ran")
    if type(ran) is not int or ran <= 0:
        return "reports %r executed tests — a whole-suite run that executed " \
            "none proves nothing about any tree" % (ran,)
    # `executed` IS THE IMPORTED SHAPE'S OWN ADMISSION. Only v5 rows carry it
    # (6 in the ledger), so it is asked ONLY when present: a missing key is a
    # version that never spoke, and refusing on absence would delete every
    # v1-v4 receipt the fleet has.
    if "executed" in row and row.get("executed") is not True:
        return "the recorded run says it did not execute (executed %r)" \
            % (row.get("executed"),)
    # EVERY REMAINING CLAUSE IS GATE'S, ASKED THROUGH GATE (round 2).
    # This function used to re-state them and had already diverged twice, both
    # times ADMITTING a row gate refuses: it had no `rc` clause, so status OK
    # with a nonzero runner exit passed; and its bracket test compared
    # `tree != tree_after`, which on a PRE-BRACKET row compares None with None,
    # is False, and passed the very row that cannot show the worktree held
    # still. Two readers of one question, diverging silently — this module's
    # own subject matter, committed by this module. What stays local above is
    # genuinely NOT gate's question: `status` and whole-suite `suite` are this
    # rung declaring which STRENGTH it needs, the same job `bind`'s `need`
    # parameter does for its callers.
    try:
        from .. import gate
    except Exception as e:                       # noqa: BLE001
        # UNIMPORTABLE IS UNKNOWN, NOT ADMISSIBLE. Returning None here would
        # silently promote every row to green the moment the import broke.
        return "gate module unimportable (%s) — admissibility UNKNOWN" \
            % type(e).__name__
    return gate.row_refusal(row, about=str(row.get("tree") or "")[:12],
                            consuming_repo=consuming_repo)


def receipt_repo_scope(row, mine, cache=None, root=None, binding_ids=None):
    """Is this receipt authorized for THIS repository? -> True | False | None

    THREE STATES BECAUSE THERE ARE THREE, and reading all of them as True is
    how a foreign receipt became evidence about this repo. A row records
    `repo_id`, and measured over the live 2,439-row ledger that value is the
    ROOM the gate ran in — 898 distinct paths, of which 3 resolve to this
    repository, 1 resolves elsewhere, and 894 no longer exist because the room
    was reaped. So:

      True  — the room still exists and its common git dir is this repo's, OR
              a canonical import binding names this importing common-dir.
              A peer worktree counts: `--git-common-dir` is shared, which is
              exactly why authority is asked of the COMMON dir.
      False — the room exists and belongs to a DIFFERENT repository. This is
              the foreign receipt, and it is the state that had no
              representation at all.
      None  — the room is gone, so nothing can place the row. 2,404 of the
              2,439 rows are in this state; refusing them wholesale would
              make the rung blind to every half gated in a since-reaped room,
              and admitting them silently is what let the foreign case in.
              The CALLER decides, per door, and the two doors decide
              differently — see `green_receipts`.

    A remotely minted receipt carries its ORIGIN in `repo_id`; that origin may
    be a reaped fab scratch repository or a different live clone. Import gives
    it local authority through gateimport's durable binding for the importing
    common-dir repository. That canonical binding is checked before a foreign
    or missing origin is returned, otherwise the importer writes authority no
    production reader can consume.

    `cache` memoises both origin repo_id and (receipt, importing repository).
    A nonexistent origin path short-circuits with no spawn."""
    projscope.spend_or_raise("receipt repository scope")
    rid = str(row.get("repo_id") or "").strip()
    cache = {} if cache is None else cache
    if rid and rid not in cache:
        try:
            if not os.path.isdir(rid):
                cache[rid] = None
            else:
                from .. import rowworld
                ident, err = rowworld.repo_identity(rid)
                projscope.spend_or_raise("receipt origin identity result")
                cache[rid] = None if err or not ident else (ident == mine)
        except projscope.Expired:
            raise
        except Exception:              # noqa: BLE001 — unplaceable, not foreign
            cache[rid] = None
    origin = cache.get(rid) if rid else None
    if origin is True or not root:
        return origin
    receipt_id = str(row.get("id") or "")
    if binding_ids is not None and receipt_id not in binding_ids:
        return origin
    key = ("gate-import-binding", receipt_id, mine)
    if key not in cache:
        try:
            from .. import gateimport
            projscope.spend_or_raise("receipt repository authorization")
            authorized, _err = gateimport.repository_authorization(
                row, root, migrate=False)
            projscope.spend_or_raise("receipt authorization result")
            cache[key] = authorized is True
        except projscope.Expired:
            raise
        except Exception:              # noqa: BLE001 — no proven binding
            cache[key] = False
    projscope.spend_or_raise("receipt repository scope result")
    return True if cache[key] else origin


def green_receipts(root=None, binding_budget_s=None):
    """(ARM trees, DISCHARGE trees, fatal error, legacy warning).

    TREES, AND NO LONGER HEADS. Both doors this feeds ask a question about
    CONTENT — "was this exact tree tested" — and the head set was what let a
    historical receipt vouch for a branch that had moved past it. A tree also
    survives the one commit that SHOULD NOT disarm the rung: an empty trailer
    commit changes the head and leaves the tree alone.

    TWO SETS, BECAUSE THE TWO DOORS FAIL IN OPPOSITE DIRECTIONS. The ledger is
    GLOBAL — one file for every repository on the box — and a receipt that
    cannot be placed in a repository used to enter this one silently. That is
    the foreign-receipt defect, and it cannot be closed by one filter, because
    admitting an unplaceable row and refusing one are each safe at exactly one
    door:

      ARMING (is this half green?) — an unplaceable row may arm. Refusing it
        would EXCLUDE a half on the strength of a reaped room, and exclusion
        in this rung must be earned by a positive signal, never by silence.
        The cost of arming wrongly is one dischargeable block.
      DISCHARGING (did an arm run against the composed tree?) — only a row
        PROVABLY minted in this repository may clear. This is the direction
        that ends a block, so a row nobody can place must not be able to end
        one; that is laundering, and it is what the tier probe did when it
        asked `cat-file` whether an object resolved and let a shared object
        store answer for provenance.

    A FOREIGN row (provably another repository) is in NEITHER set. It is not
    weak evidence about this repo, it is evidence about a different one.

    `root` is this repository. Without it no row can be placed, so the scope
    question is UNKNOWN rather than answered — and UNKNOWN is returned as
    `err` rather than as a permissive default.

    Imported lazily because `helm.gate` reaches back into this package; the
    stop ladder's own rungs use the same deferral for the same reason."""
    projscope.spend_or_raise("green receipt ledger")
    try:
        from .. import gate
    except projscope.Expired:
        raise
    except Exception as e:                       # noqa: BLE001
        return frozenset(), frozenset(), \
            "gate module unimportable (%s)" % type(e).__name__, None
    try:
        rows, unavailable, _skipped = gate.receipts()
        projscope.spend_or_raise("green receipt ledger result")
    except projscope.Expired:
        raise
    except Exception as e:                       # noqa: BLE001
        return frozenset(), frozenset(), \
            "receipt ledger raised (%s)" % type(e).__name__, None
    if unavailable:
        # A LEDGER THAT CANNOT BE READ MAKES EVERY HALF LOOK UNGATED, which
        # would make this rung silently inert rather than wrong — the failure
        # mode the spiral rung's `err` leg exists to make audible.
        return frozenset(), frozenset(), \
            "gate receipts unavailable — greenness is UNKNOWN, not absent", None
    try:
        from .. import rowworld
        mine, ident_err = rowworld.repo_identity(root) if root else (None, None)
        projscope.spend_or_raise("green receipt repository identity result")
    except projscope.Expired:
        raise
    except Exception as e:                       # noqa: BLE001
        mine, ident_err = None, type(e).__name__
    if not mine:
        return frozenset(), frozenset(), (
            "this repository has no resolvable identity (%s) — whether a "
            "receipt was minted HERE is UNKNOWN, so no receipt is read"
            % (ident_err or "no root given")), None
    try:
        from .. import gateimport
        projscope.spend_or_raise("green receipt binding census")
        binding_ids, binding_err, binding_warn = \
            gateimport.binding_candidate_ids(root, budget_s=binding_budget_s)
        projscope.spend_or_raise("green receipt binding result")
    except projscope.Expired:
        raise
    except Exception as e:                       # noqa: BLE001
        binding_ids, binding_err, binding_warn = (
            frozenset(), type(e).__name__, None)
    if binding_err:
        return frozenset(), frozenset(), (
            "canonical import bindings are UNKNOWN (%s)" % binding_err), None
    arm, discharge, cache = set(), set(), {}
    # ONE GIT PROCESS FOR EVERY TREE THIS PASS WILL ASK ABOUT. The placement
    # check under `repository_authorization` asks `<head>^{tree}` of THIS
    # repository, and one `git rev-parse` per receipt puts the whole rung's
    # cost on the RECEIPT COUNT: on a ledger of a few thousand rows that is
    # hundreds of git processes and nearly all of the rung's seconds. The
    # heads are known before the loop, so they are asked together — see
    # `gateimport.tree_batch` for why a pass-scoped answer cannot admit a
    # tree the repository could not produce.
    #
    # THE SET IS THE ONE THAT CAN REACH PLACEMENT, and it is derived from the
    # same predicate the loop uses rather than transcribed: `receipt_repo_scope`
    # consults `gateimport` only for a receipt whose id is in `binding_ids`, so
    # a head outside that set is never asked and pre-resolving it would be
    # paying git to answer a question nobody puts. Over-asking would only cost;
    # under-asking only falls back to the per-head read, so neither direction
    # can change an answer.
    heads = {str(row.get("head") or "") for row in rows
             if str(row.get("id") or "") in binding_ids}
    with gateimport.tree_batch(root, heads):
        for index, row in enumerate(rows):
            projscope.spend_or_raise("green receipt row %d" % index)
            tree = row.get("tree")
            if not tree or receipt_inadmissible(row, consuming_repo=root):
                continue
            scope = receipt_repo_scope(
                row, mine, cache, root=root, binding_ids=binding_ids)
            if scope is False:
                continue               # another repository's evidence entirely
            arm.add(tree)
            if scope is True:
                discharge.add(tree)
    projscope.spend_or_raise("green receipt fold completion")
    return frozenset(arm), frozenset(discharge), None, binding_warn


def _authored(v, root, trunk, branch, cache):
    """The files THIS branch actually wrote — its diff against its OWN
    merge-base with trunk.

    LOOKED-AND-FOUND-NOTHING IS NOT COULD-NOT-LOOK, and conflating them is
    what made the first cut of this narrowing WORSE than no narrowing. A
    branch with no commits of its own — freshly claimed, just rebased, work
    still uncommitted — has an empty own-diff, and the first version treated
    that as "cannot narrow" and handed back the un-narrowed pair-base set,
    which for such a branch is 100% inherited trunk landings. Measured: 29
    pairs / 10 false became 30 pairs / 12 false. The narrowing generated the
    exact noise it was written to remove.

    AN EMPTY OWN-DIFF HAS TWO CAUSES AND THEY NEED OPPOSITE ANSWERS. A branch
    that has authored NOTHING YET must drop out of every pair; a branch whose
    work has REACHED THE TRUNK while its lease is still held must NOT, because
    it still collides with an open lane on the same file — the whole point of
    `test_overlap_survives_one_lane_already_being_on_the_trunk`, and the
    merged-but-room-not-yet-reaped window is real.

    THE BRANCH'S OWN BIOGRAPHY SEPARATES THEM. Tip equality is only a moment,
    and trunk first-parent membership still includes a lane landed by fast-
    forward. The branch reflog answers the actual question: creation/rebase
    bookkeeping with no authored action returns EMPTY; commit/merge/cherry-
    pick/revert/rebase-pick evidence preserves the pair-base set. An unreadable,
    truncated, or unfamiliar biography is UNKNOWN and takes that same
    conservative preserve-overlap path.

    A FAILED COMPUTATION takes that same no-op path: no trunk resolvable,
    merge-base non-zero, diff non-zero. Same rule as every other guard here —
    a check that could not look must not answer as if it had — and un-narrowed
    is the safe side, because a missed collision cost a day and an extra pair
    costs a glance."""
    if branch in cache:
        return cache[branch]
    authored = _EVERYTHING
    if trunk:
        rc, base, _e = v.text(root, "merge-base", branch, trunk)
        if rc == 0 and base.strip():
            rc2, out2, _e2 = v.text(root, "diff", "--name-only",
                                    base.strip() + ".." + branch)
            if rc2 == 0:
                found = set(out2.split("\n")) - {""}
                if found:
                    authored = found
                elif _branch_authored(v, root, branch) is False:
                    authored = found
    cache[branch] = authored
    return authored


def _branch_authored(v, root, branch):
    """True/False/None from the branch's OWN reflog biography.

    An FF-landed lane and an untouched stale lane both sit on trunk's
    first-parent line. The branch reflog separates them: a lane-created commit,
    merge, cherry-pick, revert, or rebase pick is positive authorship evidence;
    creation/rebase bookkeeping alone proves no authored commit. Unknown
    actions, an unreadable log, or a log with no creation boundary return None
    and keep the conservative wide overlap set.
    """
    rc, out, _e = v.text(root, "reflog", "show", "--format=%gs", branch)
    if rc != 0:
        return None
    actions = [x.strip() for x in out.splitlines() if x.strip()]
    if not actions or not any(x.startswith("branch: Created from ")
                              for x in actions):
        return None
    authored = ("commit", "merge ", "merge:", "cherry-pick", "revert",
                "rebase (pick)")
    if any(x.startswith(authored) for x in actions):
        return True
    bookkeeping = ("branch: Created from ", "rebase (start)",
                   "rebase (finish)")
    return False if all(x.startswith(bookkeeping) for x in actions) else None


class _Everything(frozenset):
    """Intersects to a no-op. A sentinel rather than a flag because the call
    site is one expression and a flag would have to be threaded through it."""

    def __and__(self, other):
        return other

    def __rand__(self, other):
        return other


_EVERYTHING = _Everything()


# ---------------------------------------------------------------------------
# held-lane landedness — the ONE answer every lane board reads
# ---------------------------------------------------------------------------
#
# WHY IT EXISTS. A land does not release the leases of the lanes it carried:
# only each holder can, with `helm work release`, and that verb prints
# "LANDED by ancestry". A board that reads the lease alone draws every held
# lane as BUILDING, and `+0/-N` leaves the reader to infer the rest — so a
# landed lane reads as work in flight. The question "is this held lane's work
# on the trunk?" therefore gets ONE producer here, and `helm work list`, the
# web board and the land report all read it.
#
# IT COMPOSES THE TWO READS THAT ALREADY EXIST; IT IS NOT A THIRD PREDICATE.
# `landed_state` is `_merge_state` — the authority `helm work release` retires
# on — bound to the resolved tip and trunk shas instead of their names, so the
# answer can be cached per (tip, trunk sha) and a board read asks it once.
# `_branch_authored` is the claim seam's reflog biography. The second is needed
# because ANCESTRY CANNOT TELL A LANDED LANE FROM ONE CLAIMED A MINUTE AGO:
# `helm work claim` mints `lane/<name>` AT the trunk, so an untouched lane is
# an ancestor of the trunk exactly as a merged one is — often the trunk's own
# tip. Calling that LANDED is the mirror image of calling a landed lane
# BUILDING.
#
# A BOARD POLL MAKES NO GIT CALL WHEN NOTHING MOVED. Each lane's verdict is
# kept with a stat stamp of every ref file it depends on (the `_TRUNK_MEMO`
# pattern `web_board._trunk_tip` already uses): packed-refs, HEAD, the trunk
# ref, the lane ref, its reflog and its retirement ref. An unmoved stamp is
# answered from memory; a moved one is re-read, and the (tip, trunk sha) pair
# under it is re-asked only if IT moved. A reftable repository has no loose
# ref files to stamp, so it is never memoised. UNKNOWN is never memoised.
#
# WHAT IT DOES NOT READ: the room's dirtiness. That is a `git status` per room
# per poll with nothing to stamp it by, so it stays with the callers that
# already pay for it (`list_rows` carries `dirty`; the land report reads it).

LANE_LANDED = "landed"          # the lane's committed work is on the trunk
LANE_UNLANDED = "unlanded"      # it carries commits the trunk does not have
LANE_UNSTARTED = "unstarted"    # at the trunk, and the lane authored nothing
LANE_GONE = "gone"              # no lane branch and no retired tip to ask
LANE_UNKNOWN = "unknown"        # a read failed — never a verdict

_LANDED_LOCK = threading.Lock()
_LANDED_MEMO = {}       # (common, lane) -> (files, stamp, verdict)
_LANDED_STATE = {}      # (repo, tip, trunk sha) -> vcs landed_state
_LANDED_AUTHORED = {}   # (repo, lane, tip, reflog stamp) -> (carried, dropped)

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_LANDED_MEMO": (
        "keyed by repository and lane, checked against the ref files' stamp"),
    "_LANDED_STATE": (
        "keyed by repository, tip and trunk sha, which never change meaning"),
    "_LANDED_AUTHORED": "keyed by repository, lane, tip and reflog stamp",
    "_ROWED": (
        "the dispatch ledger's lane labels once per process; the arms that "
        "read them (test_work) reset it first"),
}
_LANDED_CAP = 4096

# The reflog actions by which a lane AUTHORS a commit — `_branch_authored`'s
# own list minus `merge`, because a fast-forward merge of the trunk INTO a
# lane records a trunk commit as its new sha, which the lane did not write.
_OWN_COMMIT = ("commit", "cherry-pick", "revert", "rebase (pick)")
# The actions that move a lane's tip WITHOUT the lane writing anything: its
# creation, a rebase's two ends, and a fast-forward (`merge`/`pull`) of the
# trunk in, whose new sha is a trunk commit. A lane whose whole biography is
# these has not started. Anything else (`reset`, a real merge, `pull` with
# a merge) is neither authorship nor bookkeeping and reads UNKNOWN.
_BOOKKEEPING = ("branch: Created from ", "rebase (start)", "rebase (finish)")
_FAST_FORWARD = ": Fast-forward"


def _reflog_biography(v, root, branch):
    """(created, own, bookkeeping_only) from the branch's OWN reflog, or None
    when it cannot be read. `created` is the sha the branch was minted at
    (None when the creation line is gone — an expired reflog); `own` is every
    sha the lane WROTE (an `_OWN_COMMIT` action), newest first;
    `bookkeeping_only` says the rest of the biography is `_BOOKKEEPING`."""
    rc, out, _e = v.text(root, "reflog", "show", "--format=%H %gs", branch)
    if rc != 0:
        return None
    rows = [l.partition(" ") for l in out.splitlines() if l.strip()]
    created = next((sha for sha, _sp, what in rows
                    if what.startswith(_BOOKKEEPING[0])), None)
    own = [sha for sha, _sp, what in rows if what.startswith(_OWN_COMMIT)]
    rest = [what for _sha, _sp, what in rows
            if not what.startswith(_OWN_COMMIT)]
    return created, own, all(w.startswith(_BOOKKEEPING)
                             or w.endswith(_FAST_FORWARD) for w in rest)


def _authored_at(v, root, branch, tip):
    """(carried, dropped) for a lane whose tip is ON the trunk, or None when
    its reflog cannot say. `carried` is True when the tip still carries a
    commit the lane wrote — the landed shape — and False when the lane wrote
    nothing (bookkeeping only). `dropped` is the NEWEST commit the lane wrote
    that its tip no longer carries, when it carries none of them: a branch
    RESET back to the trunk after committing sits at the trunk exactly as a
    landed lane does, and ancestry alone reads it LANDED — the harmful
    direction, since the work is then only in the reflog and a release
    deletes that."""
    bio = _reflog_biography(v, root, branch)
    if bio is None or bio[0] is None:
        return None
    created, own, bookkeeping_only = bio
    if not own:
        return (False, None) if bookkeeping_only else None
    rc, out, _e = v.text(root, "rev-list", tip, "^" + created)
    if rc != 0:
        return None
    reached = set(out.split())
    if any(sha in reached for sha in own):
        return True, None
    return None, own[0]


def _stamp(common, names):
    """The stat identity of each named file under the common git dir. A file
    that is absent is part of the answer: a ref appearing moves the stamp."""
    out = []
    for name in names:
        try:
            st = os.stat(os.path.join(common, name))
        except OSError:
            out.append((name, None))
            continue
        out.append((name, st.st_ino, st.st_size, st.st_mtime_ns))
    return tuple(out)


def _full_ref(name):
    """`origin/main` -> `refs/remotes/origin/main`, `main` ->
    `refs/heads/main`: the two spellings `vcs.trunk_ref` answers in."""
    return ("refs/remotes/" + name) if name.startswith("origin/") \
        else "refs/heads/" + name


def _lane_files(lane, trunk):
    """Every file one lane's verdict depends on. `refs/heads/main` and HEAD
    are here because `vcs.base_branch` decides the trunk NAME from them, and
    BOTH spellings of the base are, because `vcs.trunk_ref` picks
    `origin/<base>` over `<base>` by whether `refs/remotes/origin/<base>`
    resolves: a checkout that gains a remote flips the trunk's name, and a
    stamp blind to that file keeps serving a land on LOCAL main as LANDED
    against an origin that does not have it."""
    branch = lane_branch(lane)
    base = trunk[len("origin/"):] if trunk.startswith("origin/") else trunk
    return tuple(dict.fromkeys((
        "packed-refs", "HEAD", "refs/heads/main", "refs/heads/master",
        "refs/heads/" + base, "refs/remotes/origin/" + base,
        "refs/heads/" + branch, "logs/refs/heads/" + branch,
        RETIRED_NS + branch)))


def _kept(table, key, value=None):
    """Read `key` (value None) or store it, under the one lock, bounded."""
    with _LANDED_LOCK:
        if value is None:
            return table.get(key)
        table[key] = value
        while len(table) > _LANDED_CAP:
            table.pop(next(iter(table)))
        return value


def _lane_verdict(state, proof, tip=None, trunk=None, trunk_sha=None,
                  retired=False):
    return {"state": state, "proof": proof, "tip": tip, "trunk": trunk,
            "trunk_sha": trunk_sha, "retired": retired}


def _state(v, root, key, sha, trunk_sha, content):
    """The landed state of one commit against the trunk, cached. With
    `content` False the question is ANCESTRY ONLY: a kept full answer is still
    served (it is the stronger one), but a missing one is asked of
    `v.ancestry` and never kept, because the memo means patch identity was
    asked and an ancestry-only NOT_ANCESTOR has not asked it."""
    state = _kept(_LANDED_STATE, (key, sha, trunk_sha))
    if state is None:
        state = v.landed_state(root, sha, trunk_sha) if content \
            else v.ancestry(root, sha, trunk_sha)
        if content and state != vcs.UNKNOWN:
            _kept(_LANDED_STATE, (key, sha, trunk_sha), state)
    return state


# WHAT AN ANCESTRY-ONLY READ SAYS WHERE ONLY PATCH IDENTITY COULD TELL: a lane
# whose tip the trunk does not hold by object id may still have landed under
# another sha (a rebased train), so that read answers UNKNOWN, never UNLANDED.
_CONTENT_NOT_ASKED = ("the lane carries commits the trunk does not hold by "
                      "object id, and patch identity was not asked")


def _lane_judged(v, root, key, common, lane, tip, retired, trunk, trunk_sha,
                 content=True):
    """One lane's verdict from its resolved tip. `key` names the repository
    in the caches; the tip and trunk shas name everything else. `content`
    False asks ancestry and authorship only (see `lanes_landed`)."""
    if not tip:
        return _lane_verdict(
            LANE_GONE, "no lane branch and no retired tip — there is nothing "
            "under this lease to ask the trunk about", None, trunk, trunk_sha)
    state = _state(v, root, key, tip, trunk_sha, content)
    word = _proof_word(state)
    if retired:
        word += " — the branch is retired; this is its preserved tip"
    if state == vcs.PATCH_EQUIVALENT or (state == vcs.ANCESTOR and retired):
        # a patch-identity proof needs commits in the range, and a retired
        # tip was preserved only because it carried some: both authored work
        return _lane_verdict(LANE_LANDED, word, tip, trunk, trunk_sha, retired)
    if state == vcs.NOT_ANCESTOR:
        return _lane_verdict(LANE_UNLANDED if content else LANE_UNKNOWN,
                             word if content else _CONTENT_NOT_ASKED,
                             tip, trunk, trunk_sha)
    if state != vcs.ANCESTOR:
        return _lane_verdict(LANE_UNKNOWN, word, tip, trunk, trunk_sha)
    branch = lane_branch(lane)
    reflog = _stamp(common, ("logs/refs/heads/" + branch,)) if common \
        else None
    bio = _kept(_LANDED_AUTHORED, (key, lane, tip, reflog)) \
        if reflog else None
    if bio is None:
        bio = _authored_at(v, root, branch, tip)
        if bio is not None and reflog:
            _kept(_LANDED_AUTHORED, (key, lane, tip, reflog), bio)
    carried, dropped = bio or (None, None)
    if carried:
        return _lane_verdict(LANE_LANDED, word, tip, trunk, trunk_sha)
    if carried is False:
        return _lane_verdict(
            LANE_UNSTARTED, "at the trunk with nothing authored yet — "
            "claimed, not landed", tip, trunk, trunk_sha)
    if dropped:
        # THE LANE WROTE COMMITS ITS BRANCH NO LONGER CARRIES (a reset back
        # to the trunk). The tip says nothing about them; the newest one
        # does, asked the same cached question as a tip: on the trunk under
        # any sha and the work landed before the reset; not there and the
        # work is only in the reflog, which is what a release deletes.
        state = _state(v, root, key, dropped, trunk_sha, content)
        where = "%s, which %s no longer carries" % (dropped[:12], branch)
        if state in RETIRABLE:
            return _lane_verdict(
                LANE_LANDED, "%s — judged by its last authored commit %s"
                % (_proof_word(state), where), tip, trunk, trunk_sha)
        if state == vcs.NOT_ANCESTOR and content:
            return _lane_verdict(
                LANE_UNLANDED, "the branch sits at the trunk, but the lane "
                "wrote %s, and that work is on neither the branch nor the "
                "trunk — only in the reflog, which a release deletes"
                % where, tip, trunk, trunk_sha)
        return _lane_verdict(
            LANE_UNKNOWN, "the branch sits at the trunk, but the lane wrote "
            "%s, and %s" % (where, _proof_word(state)), tip, trunk, trunk_sha)
    return _lane_verdict(
        LANE_UNKNOWN, "the tip is on the trunk, but the lane's reflog cannot "
        "say whether it authored anything, so landed and never-started "
        "cannot be told apart", tip, trunk, trunk_sha)


def _lanes_read(root, common, lanes, stampable, content=True):
    """The verdicts for `lanes`, read from git: ONE `for-each-ref` for every
    tip and the trunk, then the cached per-(tip, trunk) question per lane. The
    stamp is taken BEFORE the read, so a ref that moves during it leaves a
    stamp that no longer matches and is re-read next time — never the reverse.
    An ancestry-only read (`content` False) keeps no verdict: the memo is the
    full producer's, and serving a weaker answer from it would change what
    `helm work list` prints."""
    v = vcs.backend(root)
    trunk = _trunk(root)
    files = {lane: _lane_files(lane, trunk) for lane in lanes}
    stamps = {lane: _stamp(common, files[lane]) for lane in lanes} \
        if stampable else {}
    ref = _full_ref(trunk)
    rc, raw, _err = v.text(
        root, "for-each-ref", "--format=%(objectname) %(refname)", ref,
        *["refs/heads/" + lane_branch(l) for l in lanes],
        *[RETIRED_NS + lane_branch(l) for l in lanes])
    if rc != 0:
        why = "the refs could not be read (git for-each-ref rc %d)" % rc
        return {l: _lane_verdict(LANE_UNKNOWN, why, trunk=trunk)
                for l in lanes}
    refs = {name: sha for sha, _sp, name in
            (line.partition(" ") for line in raw.splitlines()) if name}
    trunk_sha = refs.get(ref)
    if not trunk_sha:
        why = "the trunk %s does not resolve here" % trunk
        return {l: _lane_verdict(LANE_UNKNOWN, why, trunk=trunk)
                for l in lanes}
    key = common or os.path.realpath(root)
    out = {}
    for lane in lanes:
        branch = lane_branch(lane)
        tip = refs.get("refs/heads/" + branch)
        retired = None if tip else refs.get(RETIRED_NS + branch)
        verdict = _lane_judged(v, root, key, common, lane, tip or retired,
                               bool(retired), trunk, trunk_sha, content)
        out[lane] = verdict
        if content and stampable and verdict["state"] != LANE_UNKNOWN:
            _kept(_LANDED_MEMO, (common, lane),
                  (files[lane], stamps[lane], dict(verdict)))
    return out


def lanes_landed(root, lanes, content=True):
    """{lane: verdict} — is each held lane's committed work on the trunk?

    `verdict` is {state, proof, tip, trunk, trunk_sha, retired}; `state` is
    one of LANE_LANDED / LANE_UNLANDED / LANE_UNSTARTED / LANE_GONE /
    LANE_UNKNOWN and `proof` is the sentence that decided it — for a landed
    lane, `_proof_word`'s own "LANDED by ancestry" / "LANDED by patch
    identity", the words `helm work release` prints. A lane with no branch
    is asked through its `refs/helm-retired/` tip when it has one, and is
    GONE when it has neither: absence of a branch is not proof of a land.

    See the block above for why it is one producer, why it reads authorship,
    and why an unmoved repository costs no git call at all.

    `content=False` IS THE READ A PROJECTION CAN AFFORD FOR EVERY ROW. The
    patch-identity leg runs `git cherry` against the trunk, which walks every
    trunk patch since the lane's base — measured on the live repository, the
    stale lanes of the open BUILD rows the land projection asks about cost
    minutes cold. Without it the verdict is ancestry and the lane's own
    reflog: LANDED, UNSTARTED and GONE are answered exactly as the full read
    answers them, and a tip the trunk does not hold by object id is UNKNOWN,
    because only patch identity could say whether it landed rebased. A kept
    full verdict is still served, and this read keeps nothing."""
    lanes = sorted({str(l) for l in lanes or () if l})
    if not lanes:
        return {}
    if not root or not os.path.exists(os.path.join(root, ".git")):
        # NOT HANDED TO GIT, which would walk up and answer for whatever
        # repository encloses the path — `web_board._trunk_tip`'s rule.
        return {l: _lane_verdict(LANE_UNKNOWN, "no git checkout at %s"
                                 % (root or "an unnamed path"))
                for l in lanes}
    common = gitfacts._common_dir(root)
    stampable = bool(common) and not os.path.isdir(
        os.path.join(common, "reftable"))
    out, ask = {}, []
    for lane in lanes:
        hit = _kept(_LANDED_MEMO, (common, lane)) if stampable else None
        if hit and _stamp(common, hit[0]) == hit[1]:
            out[lane] = dict(hit[2])
        else:
            ask.append(lane)
    if ask:
        out.update(_lanes_read(root, common, ask, stampable, content))
    return out


def _carried_at(v, root, branch, trunk_sha):
    """The commit up to which the trunk ALREADY carries this unlanded lane,
    or None. The lane's merge-base with the trunk is a commit the LANE wrote
    (its own reflog records authoring it) only when a land took part of it —
    an ordinary lane's merge-base is the trunk commit it was branched from."""
    rc, base, _e = v.text(root, "merge-base", branch, trunk_sha)
    if rc != 0 or not base:
        return None
    bio = _reflog_biography(v, root, branch)
    return base if bio and base in bio[1] else None


def landed_leases(root, registered=None):
    """(release, kept) — what the integrator is told after a land.

    `release` is every held, live lane room whose committed work is on the
    trunk (`lanes_landed` says LANDED) and whose room is clean: its holder
    can retire it with `helm work release`. `kept` is [(row, why)] for the
    rooms the land TOUCHED that must not be released yet — a landed tip under
    a DIRTY room, and a lane the trunk carries only part of (`_carried_at`),
    whose room holds commits beyond the landed ones.

    IT RELEASES NOTHING. A lease is bound to its holder's token, and the
    claims ledger publishes no token but the caller's own; a land that
    released the leases it carried would need an authority over every seat's
    lease that no verb has, and should not. So this names each lease and its
    exact command, and the holder spends it."""
    rows = [r for r in list_rows(root, registered=registered, gc=False,
                                 only_held=True) if not r.get("stale")]
    v = vcs.backend(root)
    release, kept = [], []
    for r in rows:
        verdict = r.get("landed") or {}
        if verdict.get("state") == LANE_LANDED:
            if r.get("dirty"):
                kept.append((r, "its committed work is on the trunk, but the "
                                "room is DIRTY — commit or --park before "
                                "release"))
            else:
                release.append(r)
        elif verdict.get("state") == LANE_UNLANDED and r.get("branch"):
            at = _carried_at(v, root, r["branch"], verdict.get("trunk_sha"))
            if at:
                kept.append((r, "the trunk carries it up to %s and it has %s "
                                "commit(s) beyond — still building"
                             % (at[:12], r.get("ahead"))))
    return release, kept


def rooms_dirty(root, lanes):
    """{lane: dirty} for the named lanes' rooms — the `dirty` `list_rows`
    carries, from the same registry (`lane_rows`) and the same reader
    (`_dirty`, one `git status` per room), so a surface that asks about a
    few lanes pays for those rooms and never for every claim. The web board
    asks it for its LANDED lanes only, because a landed lane under a dirty
    room is the one `helm work list` prints as DIRTY and `landed_leases`
    keeps rather than offers for release.

    A lane with no registered room is False: there is no checkout holding
    anything uncommitted. A registry that cannot be read is None for every
    lane — unread, never clean."""
    want = sorted({str(l) for l in lanes or () if l})
    if not want:
        return {}
    rows, err = _worktree_records(root)
    if err:
        return {lane: None for lane in want}
    rooms = {w["lane"]: w["path"] for w in lane_rows(root, registered=rows)}
    return {lane: bool(_dirty(rooms[lane])) if lane in rooms else False
            for lane in want}


def release_command(row):
    """The exact line that releases one `list_rows` row's lease: with the
    token when it is the caller's own, else naming where its holder reads
    it."""
    return "helm work release %s --lease %s" % (
        row["lane"], row.get("lease") or "<%s's lease, on its `helm work "
        "list`>" % (row.get("holder") or "its holder"))


def list_rows(root, registered=None, gc=True, only_held=False):
    """The shadow board: registry ⋈ claims, computed — lane, holder,
    remaining, dirty, ahead/behind the base, `landed` (the `lanes_landed`
    verdict, held rows only; None elsewhere), and `lease` for the rows THIS
    seat holds (seats.own_leases — never another holder's; see its docstring
    for why that is a strand-fix and not a disclosure). The lease join is
    keyed on the stored resource name, so the token printed here is the exact
    token `helm work release` will accept.

    `gc=False` READS WITHOUT SWEEPING, and it exists because this table is now
    read by a SURFACE as well as by the desk. `claims_list`'s own docstring
    carries the rule — a watched roster must never churn .claims.json — and
    `helm web` polls the land-pipeline card on a timer, so the board that
    DISPLAYS the leases must not be the thing that expires them. The returned
    rows are identical either way (the sweep still drops expired rows from the
    table in memory); only the persist is dropped.

    `only_held=True` NARROWS THE POPULATION BEFORE THE PER-ROOM READS, and it
    is a cost argument rather than a taste. Each row costs a `git status` and
    an ahead/behind pair — about 20ms measured over 118 lane rooms on this
    tree, so 2.3s for the full board against 0.3s for the six rooms that
    actually hold a claim. A caller that only has a question about HELD rooms
    (the pipeline card's building band) can ask it at response time at that
    price and not at the other one. It changes WHICH rows are returned and
    nothing about what a row IS, so every field below means exactly what it
    means on the full board."""
    # ahead/behind is an OWNER-FACING distance to the trunk, so it measures
    # against the trunk ref too — a lane reported "behind 6" against a stale
    # local main tells the owner to rebase onto something the fleet never had.
    base, v = _trunk(root), vcs.backend(root)
    # THE TOKENS AND THE ROWS ARE ONE READ: tokens read first let a lane
    # released and re-claimed by another seat show that seat's name beside
    # this seat's released token and "(yours)" (task/2541).
    swept = []
    table = seats.claims_list(gc=gc, swept=swept)
    own = seats.own_leases(swept=swept[0])
    # ONE READ FOR BOTH COLUMNS. `_live()` and `claims_list()` were two
    # separate reads, so the HOLDER on a row could come from one instant and
    # its LIVENESS from another — a row could show a holder that the
    # classification no longer knew about, or vice versa. And `stale_by` kept
    # only the stale ones, which FLATTENED UNKNOWN back into healthy on the
    # board: exactly the collapse this lane exists to end, surviving on the
    # one surface that lists lanes. A non-blocking finding on review
    # row 18a49b86cf1f, fixed in the same pass as the producer cases. THE ROW
    # AND NOT THE REVIEWED SHA: that tip was a lane commit this lane has since
    # been rebased past, no branch contains it, and a fresh clone cannot
    # resolve it — a citation only the author's machine can follow is not
    # provenance. The ledger row outlives every rebase.
    # ONLY THE LANE'S OWN ROW SPEAKS FOR THE LANE. `claim` stores a resource
    # as given, so `worktree:p:lane ` is a row of its own that PUBLISHES as the
    # lane's resource, and keying on the published name let that row, sorted
    # last, write another seat's holder, TTL and liveness over the lane's row,
    # beside this seat's own token. The lease join below is on the stored
    # key, and a lane key is always clean, so the rows read here are the ones
    # whose stored resource is the published one (task/2528).
    claims = [c for c in table if c["resource_exact"]]
    live = {c["resource"]: {"holder": c["holder"],
                            "remaining": c["remaining"]} for c in claims}
    liveness_by = {c["resource"]: c.get("liveness") for c in claims}
    rooms = lane_rows(root, registered=registered)
    # IS A HELD LANE'S WORK ALREADY ON THE TRUNK — asked once for every held
    # room whose branch IS its lane's, through the one producer the web board
    # reads too. An unheld room is `gc`'s question, not this board's, and a
    # room on some other branch is not the lane `lanes_landed` would ask about.
    landed = lanes_landed(root, [
        w["lane"] for w in rooms if live.get(resource(root, w["lane"]))
        and w["branch"] == "refs/heads/" + lane_branch(w["lane"])])
    rows = []
    for w in rooms:
        res = resource(root, w["lane"])
        held = live.get(res)
        if only_held and not held:
            continue        # skipped BEFORE the two per-room git reads
        branch = (w["branch"] or "")[len("refs/heads/"):]
        counts = v.ahead_behind(root, base, branch or "HEAD")
        behind, ahead = counts if counts else ("?", "?")
        rows.append({"lane": w["lane"], "branch": branch, "path": w["path"],
                     "holder": held["holder"] if held else None,
                     "remaining": held["remaining"] if held else None,
                     "lease": own.get(res) if held else None,
                     "liveness": liveness_by.get(res),
                     "stale": liveness_by.get(res) == "stale",
                     "landed": landed.get(w["lane"]),
                     # ONE status call, then every field it answers — the board
                     # needs the detail, not just the boolean
                     **{k: v for k, v in _room_status(w["path"]).items()
                        if k in ("dirty", "conflicts", "operation",
                                 "dangling_conflict")},
                     "locked": w["locked"],
                     "ahead": ahead, "behind": behind})
    return rows
