"""`helm.dispatches_carriage` -- DID THIS ROW'S WORK REACH TRUNK BY CARRIAGE.

ONE QUESTION, asked for a `carried` close: did the row's work reach trunk --
replayed onto it, or reached by ancestry -- measured under a history view this
module PINS rather than trusts. `carriage_proof` is the door; the trunk-object
resolution, the shallow refusal, the replay and reached-trunk witnesses and
the complete-object-database test are its ladder. The block comment below
says why no witness answer is stored.

MEASURED AT THE CUT, NOTHING LEFT IN THE LEDGER CALLS IN: the remaining
`dispatches` module names none of these definitions. The close ladders
(`dispatches_close`, `landreq_close`), the row world and both checkpoints
reach the door as `dispatches.carriage_proof`, which still answers. The
vocabulary a carried close RECORDS (`CARRIAGE_REPLAY`, `REACHED_TRUNK`,
`CARRIED_WITNESSES`) stayed in the ledger beside the close-event fields it
belongs to.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `dispatches.NAME` AT
ITS CALL SITE -- 15 reads of 12 distinct names; no other byte of a moved line
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
from . import carriageckpt, projscope


# THE HISTORY VIEW EVERY CARRIAGE WITNESS RUNS UNDER, WHY NO WITNESS ANSWER IS
# STORED ON ITS OWN (helm task/2394, round four), AND HOW THE FOLD THAT CARRIES
# ITS CLOSE MAY BE REUSED (task/2770).
#
# THE COST THAT STARTED THIS, measured on the live 14,444-event 8,166,754-byte
# dispatch ledger. A cold `dispatches.snapshot()` cost 5.384s, of which 2.362s
# was two `carried`-close `carriage_proof` re-derivations (18 git subprocesses,
# one `merge-tree --write-tree` at 1.183s and one `git cherry` at 0.619s), and
# the stop guard's dispatch-ledger rung ended at or past its local reserve on 22
# of 175 recorded ladders, which is the cut that publishes COVERAGE UNKNOWN.
#
# NO VERDICT HERE IS PERSISTED, AND THAT IS A DESIGN RULE RATHER THAN AN
# OMISSION. A durable memo of the reached-trunk verdict is the obvious cure for
# that cost, and it cannot be made correct: git answers about an id through a
# HISTORY VIEW, and `refs/replace`, `info/grafts` and a shallow boundary each
# reinterpret the same immutable ids without changing one of them. So a stored
# answer needs BOTH a semantic generation that refuses every entry written under
# a different interpretation AND a view held immutable across the whole interval
# between the eligibility probe and the witness — and a cache whose answer gates
# a `carried` close pays that in correctness, not in speed. Nothing is stored:
# there is no entry to invalidate and no interval to bind.
#
# WHAT task/2770 CHANGED, AND WHY IT IS NOT THAT MEMO. The WHOLE dispatch fold
# is now a maintained checkpoint (`helm/foldckpt.py`), and a checkpoint holds
# every close this function decided, accepted or refused. It is never consulted
# per verdict: it is reused only after EVERY git answer the fold read is asked
# again — each object and ref expression through one `cat-file --batch-check`
# under this same pinned view, and a fingerprint of the shallow state, the
# replacement refs, the grafts and attributes files, the listed config and the
# git binary — and any difference discards the whole checkpoint for a full
# replay, which derives every verdict here again. The semantic generation the
# paragraph above asks for is that fingerprint. The interval it could not bind
# is now the one between the re-verification and the read's answer, which is
# the interval every live replay already has between two of its own git calls.
#
# THE VERDICT IS RE-DERIVED ON EVERY READ, under a history view this module PINS
# rather than measures:
# `rowworld._scrubbed_env`, the overlay behind every git read in that module,
# disables replacement objects AND points `GIT_GRAFT_FILE` at `os.devnull`. That
# is the view `landreq._object_view` already defines, measured there on git
# 2.53.0, and this module reuses its spelling rather than inventing a third one.
#
# A SHALLOW BOUNDARY IS REFUSED INSTEAD OF PINNED, because it is the one
# rewriter with no bypass: the parent objects behind it are genuinely absent.
# `_carriage_shallow_refusal` answers UNKNOWN there and never affirms, which is
# the same refusal `landreq._is_parentless` makes on the same measurement.
#
# AND THE COST THE CUT LEAVES BEHIND IS MEASURED ON THE FINAL TREE, NOT ASSUMED.
# Timed through `dispatches.snapshot()`, the read the Stop guard's
# dispatch-ledger rung then made (that fold has since moved off every hook path
# into the stop-facts resident) — on THE TREE THIS COMMENT SHIPS IN, identified by the
# property that decides the cost: no witness store is present anywhere under
# `helm/` and both witness families are re-derived. The exact tree of the run is
# recorded on the lane's review row. Against the live
# 14,567-event 8,349,380-byte ledger and the real helm home, read-only, five cold
# processes of one call each: at load average 29.7-30.7 the rung is 3.351s
# median, 52% of the 6.5s admission cost that rung then carried, min 1.578s and max
# 4.452s — so EVERY sample fit the budget on a box near load 30. Two carriage
# witnesses are derived per read, counted at this function's own call.
#
# THE FIGURES THAT MOTIVATED THE CUT ARE AN EARLIER, DIFFERENT COMPARISON —
# store-DISABLED against store-ENABLED arms on the predecessor tree, the
# remembering write replaced by a no-op, author-reported at that time against a
# 14,540-event 8,301,262-byte ledger, five cold processes per arm interleaved so
# box load is common-mode. At load 10.4-10.8 the DISABLED arm was 1.532s median
# against 1.058s enabled, and the two carried rows it witnesses spent 0.795s
# median in the witnesses against 0.374s. At load 9-15 the DISABLED arm was the
# FASTER of the two (4.470s median against 5.425s), and at load 27-51 the ENABLED
# median fit the budget (6.161s under 6.5s) while the DISABLED median did not
# (8.384s), with one enabled run producing a 12.867s TAIL sample rather than a
# failing median. That box load rather than the memo sets the absolute scale is
# the INFERENCE those interleaved observations support, not a controlled
# isolation of load; the cut rests on it together with the correctness class the
# store carried, not on a claim that the store never mattered.
#
# AND THE REPLAY WITNESS'S OWN ANSWER NOW GOES THROUGH THAT SAME DOOR FOR A
# READER THAT DECLARES ONE (`helm/carriageckpt.py`, task/2813), which is this
# paragraph's two conditions met rather than an exception to it. The off-frontier
# census re-derived the replay 141 times per board read, 19.7s, for questions
# the previous read had answered against the same objects — and the bill was
# measured to be irreducible within one projection (no droppable row class, two
# families that partition by the routed classification, a cheap twin that does
# not predict the expensive one, and a disjoint question set from the World
# build's). The SEMANTIC GENERATION is `foldckpt.policy()`, the code this
# process executes. The IMMUTABLE VIEW is re-established on every read by that
# module's own re-verification: one `cat-file --batch-check` per repository over
# every object and ref expression the witness read, plus the fingerprint of the
# git binary, the shallow state, the listed config, the replacement refs and the
# attributes and grafts files. And the trunk OBJECT the proof is about — the
# head `_carriage_trunk_sha` resolves for the ladder and the writer, or the
# recorded `closing_trunk_sha` a replayed close is measured against while that
# commit is still history of trunk (task/3056) — is part of the KEY, so a land
# is a different question rather than an entry somebody must remember to
# invalidate. It is served only inside a declared region, which no writer opens.
#
# A PER-INVOCATION MEMO WAS REFUSED ON THE SAME MEASUREMENT. The live ledger
# holds two `carried` closes; one stop evaluation asks `carriage_proof` exactly
# twice, once per row, and those two rows bind DIFFERENT tips — so they are two
# different questions and a memo scoped to one read would have held ZERO hits.
# (The instrument that says so counts the calls and their row ids, and it
# recorded two distinct ids, never one id twice.) A mechanism with no measured
# hit is invention rather than a cure.


def _carriage_trunk_sha(gitdir, trunk_ref):
    """The trunk object this proof is ABOUT, resolved ONCE. -> sha or None.

    ITS CALLER RESOLVES IT BEFORE EITHER HALF OF THE PROOF, and that ordering
    is a round-one finding of task/2394: two resolutions of one ref name are two
    questions, so a trunk that moved between them lets one family answer about
    trunk A while the other answers about trunk B and the caller reports a
    single verdict. Resolving once and handing the OBJECT down removes the
    window rather than narrowing it.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    rc, out = rowworld._git(gitdir, "rev-parse", "--verify", "--quiet",
                            str(trunk_ref or "") + "^{commit}")
    sha = out.strip().lower() if rc == 0 else ""
    return sha if dispatches._FULL_TIP.fullmatch(sha) else None


def _carriage_shallow_refusal(gitdir):
    """Why this repository cannot witness carriage at all. -> reason or None

    A SHALLOW BOUNDARY IS THE ONE HISTORY-VIEW REWRITER WITH NO BYPASS, and
    that is why it is a REFUSAL rather than something the read pins off.
    Replacement objects and a legacy grafts file are both disabled at every
    read (`rowworld._scrubbed_env`, the view `landreq._object_view` defines),
    so under them git reads the object an id names. A `.git/shallow` boundary
    is different in kind: the parent objects behind it are genuinely ABSENT, so
    no variable and no flag restores the range `git cherry` would have walked,
    and a truncated range that comes back uniformly `-` is an artifact of the
    truncation rather than a patch-identity match. `landreq._is_parentless`
    refuses outright for this reason on the same measurement.

    UNKNOWN, NEVER A NEGATIVE AND NEVER AN AFFIRMATION. The caller returns
    `(None, reason)`, which `_close_event_error`'s `carried` arm reads as trunk
    no longer affirming the work — the fail-safe direction, because a close
    whose proof cannot be re-derived goes INERT rather than being accepted.

    AND AN UNREADABLE ANSWER IS A REFUSAL TOO: a git that cannot say whether it
    is shallow is exactly the case where this proof must not speak.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    rc, out = rowworld._git(gitdir, "rev-parse", "--is-shallow-repository")
    shallow = out.strip() if rc == 0 else ""
    if shallow == "false":
        return None
    if shallow == "true":
        return ("%s is SHALLOW: its boundary truncates every traversal and the "
                "parent objects behind it are absent, so no flag restores the "
                "range this witness would have walked" % gitdir)
    return ("git did not say whether %s is shallow (%r), and a history view "
            "that cannot be read is not one a proof may run in"
            % (gitdir, shallow))


def _carriage_replay_witness(gitdir, base, tip, trunk_ref, trunk_sha):
    """WITNESS FAMILY 1, DERIVED LIVE ON EVERY READ. -> (answer, detail)

    `(None, prose)` when this family has nothing to say, and that prose is the
    sentence the second family quotes inside its own refusal.

    IT RUNS FIRST AND IT RUNS EVERY TIME, and the ordering is the contract.
    `rowworld._carriage` reaches `merge-tree --write-tree` and asks about trunk
    HEAD's CONTENT, which is the stronger question; the second family asks about
    trunk's HISTORY. The fallthrough is one-directional — only SILENCE here may
    reach it — because a measured True or False from the stronger question must
    not be second-guessed by a weaker one.

    AND ITS ANSWER IS NOT A FUNCTION OF THE IDS IT WAS HANDED, which is why
    nothing in this module has ever been allowed to persist it: git resolves
    content through its MERGE MACHINERY, so a merge driver (an external
    program), an attributes file (content behind a pathname) and repeated config
    records (resolved by last-value precedence) all move this answer while every
    id stands still. No answer of this witness is stored on its own; the fold
    checkpoint that carries the close it decided is reused only after the
    config, the attributes files and the git binary are fingerprinted again and
    match — see the note above `_carriage_trunk_sha`. The RAW `merge-tree`
    answer beneath it — exit code and stdout, git's answer and not this
    witness's verdict — is kept by `gitfacts` under exactly that fingerprint
    (task/3056), so a view that moved is a different key; the verdict is still
    derived here, from it, on every read.

    AND THE SAME DOOR NOW CARRIES THE ANSWER ACROSS PROJECTIONS, for the reader
    that declared one (`carriageckpt.replayed`). A census re-derived this
    relation 141 times per board read for questions the previous read had
    already answered against the same objects; inside a declared region the
    answer is served only after that module re-asks git every question this
    witness put to it, under the same fingerprint, and a trunk that moved is a
    different key rather than a stale entry. Outside a region — the ladder that
    authorizes a real close, and the writer that re-derives one under the lock
    — this is byte-identical to the derivation below.
    """
    if not (base and tip):
        return None, "no immutable (base, tip) to replay from"
    from . import rowworld              # DEFERRED — rowworld imports us.
    answer = carriageckpt.replayed(
        gitdir, base, tip, trunk_sha,
        lambda: rowworld._carriage(gitdir, base, tip, trunk_sha or trunk_ref))
    if answer is not None:
        return answer, {"gitdir": gitdir, "trunk_ref": trunk_ref,
                        "trunk_sha": trunk_sha, "base": base, "tip": tip,
                        "witness": dispatches.CARRIAGE_REPLAY}
    return None, ("no content witness at %s could see %s..%s"
                  % (trunk_ref, base[:12], tip[:12]))


def _reached_by_ancestry(gitdir, tip, trunk):
    """Is `tip` literally reachable from `trunk`? -> True/False

    FOR THE SENTENCE ONLY, NEVER FOR THE GATE: `rowworld._reached_trunk`
    answers `(None, None)` for an ancestor and for five other conditions
    alike, so this re-asks the one cheap probe and leaves that contract alone.
    FALSE ON AN UNREADABLE PROBE, deliberately: a failed read falls back to
    the older, weaker sentence and never manufactures a claim of ancestry.
    THAT IS WHY THIS ONE MUST NOT AUTHORIZE ANYTHING -- see
    `_ancestry_authorizes`, the tri-state sibling a gate uses, where an
    unreadable probe takes its own value instead of reading as a measured no.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    sha = rowworld._sha(tip)
    if not sha or not trunk:
        return False
    rc, _out = rowworld._git(gitdir, "merge-base", "--is-ancestor",
                             sha, str(trunk))
    return rc == 0


def _work_tip_of(row, rows):
    """The one tip `carriage_proof` would measure for this row: the chain's
    work pair tip, else the reviewed tip, else the sha the row was filed at.
    -> full sha or "" """
    from . import rowworld              # DEFERRED — rowworld imports us.
    _base, tip = rowworld._work_pair(row, rows, dispatches._CarrierView(rows))
    tip = tip or rowworld._sha((row or {}).get("reviewed_tip")) \
        or rowworld._sha((row or {}).get("tip"))
    return str(tip or "")


def _ancestry_authorizes(gitdir, tip, trunk):
    """Is `tip` literally reachable from `trunk`? -> True / False / None

    THE GATE-GRADE SIBLING OF `_reached_by_ancestry`, and the whole
    difference is the THIRD ANSWER. That one collapses an unreadable probe
    to False on purpose and says so, because it only picks a refusal's
    WORDING -- a failed read there costs a weaker sentence and nothing else.
    This one AUTHORIZES, so an unreadable probe must never be spelled the
    same as a measured no: `unreadable` and `empty` cannot share a value,
    or a git that could not run would silently read as "the work is not on
    trunk" and refuse a close that is perfectly good.

    None therefore means THE QUESTION COULD NOT BE ASKED, and the caller
    refuses naming that rather than guessing in either direction. The two
    functions are deliberately not merged: one may lie towards the weaker
    sentence and the other may not lie at all.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    sha = rowworld._sha(tip)
    if not sha or not trunk:
        return None
    rc, out = rowworld._git(gitdir, "rev-parse", "--verify", "--quiet",
                            sha + "^{commit}")
    if rc != 0 or not rowworld._sha(out.strip()):
        return None
    rc, _out = rowworld._git(gitdir, "merge-base", "--is-ancestor",
                             sha, str(trunk))
    # git answers 0 for yes and 1 for no; ANYTHING ELSE IS THE PROBE FAILING,
    # not an answer about history, and it takes the third value.
    return True if rc == 0 else (False if rc == 1 else None)


def _measured_trunk_is_history(gitdir, measured, trunk_ref):
    """Is the trunk a carried close was MEASURED against still history of the
    trunk this read resolves? -> True / False / None

    WHY A CARRIED CLOSE IS REPLAYED AGAINST ITS RECORDED TRUNK (task/3056).
    A proven close records the trunk commit it was proven against
    (`closing_trunk_sha`). While that commit is still history of trunk, no
    later edit can change what replaying the row's work onto it answers: the
    operands are three immutable ids, and git's view of them is held by the
    same fingerprint the fold checkpoint re-verifies. Replaying against the
    CURRENT head instead re-asked every close after every land -- 253
    `merge-tree` runs and about 17 s per cold fold, measured on an isolated
    copy of the live ledger -- to re-derive answers that no land can move.

    THE ANCESTRY IS WHAT LICENSES THE RECORDED TRUNK, and it is asked here
    rather than assumed. A trunk that was rewritten or force-moved no longer
    holds the commit the close was proven against, and then the close is
    re-derived against the head exactly as before (`False`). So the one
    thing that moves the recorded replay's answer is trunk's history losing
    the measured commit. Ancestry licenses HISTORY, not content: a revert,
    or a `merge -s ours` of the measured commit, is history that dropped the
    content, and the close stays closed through it -- so this answer never
    says that trunk carries the work now.

    A MEASURED COMMIT THE REPOSITORY NO LONGER HOLDS IS `False`, when the
    repository is complete (`_absent_from_a_complete_odb`). A rewrite followed
    by `gc --prune=now` removes the commit, and then git cannot ask about its
    ancestry at all (`rev-parse --verify` answers 1, `merge-base
    --is-ancestor` 128). But an object that a complete object database does
    not hold cannot be history of trunk, so the close falls back to the head,
    where the task/2863 reachability check can still keep a row whose tip
    trunk merged. In an incomplete database the object may be history git
    never fetched, and that stays None.

    ONCE PER DISTINCT MEASURED TRUNK PER FOLD. Every fold runs inside one
    `projscope.scope()`, and the live ledger's 509 carried closes name only 8
    distinct trunks, so the memo below asks 8 questions where the rows would
    ask 509. The answer is the gate-grade tri-state `_ancestry_authorizes`
    gives, reused rather than re-spelled. None -- an unresolvable trunk, a
    measured commit git cannot read in an incomplete repository, a probe
    that failed -- is never remembered, so one failed spawn does not refuse
    every later row in the fold; the caller refuses the row it was asked
    for, by name."""
    key = ("dispatches._measured_trunk_is_history", str(gitdir),
           str(measured or ""), str(trunk_ref or ""))

    def ask():
        if not dispatches._FULL_TIP.fullmatch(str(measured or "")):
            return None
        trunk_sha = dispatches._carriage_trunk_sha(gitdir, trunk_ref)
        if not trunk_sha:
            return None
        held = dispatches._ancestry_authorizes(gitdir, measured, trunk_sha)
        if held is None and dispatches._absent_from_a_complete_odb(gitdir, measured):
            return False
        return held
    held = projscope.memo(key, ask)
    if held is None:
        projscope.forget(key)
    return held


# THE CONFIG THAT MAKES AN OBJECT DATABASE PARTIAL. A partial clone names its
# promisor remote in `extensions.partialClone`, and any remote can be marked
# `remote.<name>.promisor`; either one means an object git lacks may be
# history it never fetched. git matches section and key names lower-cased.
_PARTIAL_ODB_CONFIG = r"^(extensions\.partialclone|remote\..*\.promisor)$"


def _absent_from_a_complete_odb(gitdir, sha):
    """Is `sha` MISSING from an object database that holds all of its history?
    -> True, or None when that cannot be said.

    WHY AN ABSENCE CAN BE AN ANSWER (task/3056). A complete object database
    holds every object its refs reach, so an object it does not hold is not
    history of any of them. `_measured_trunk_is_history` reads that as
    `False` for a carried close's measured trunk after a rewrite and a prune.

    COMPLETE MEANS THREE THINGS, and all three are asked BEFORE the object is:
    not shallow (the boundary's parents are absent and still history), no
    `extensions.partialClone` and no `remote.*.promisor` (objects that are
    absent and still history, and asking for one can fetch it). Measured on
    git 2.53: `cat-file -e <id>` answers 1 for a missing object and 128 for a
    name it cannot parse, so only 1 is an absence.

    THE CONFIG READ IS HIDDEN FROM A FOLD'S RECORDER, as `gitfacts._view`
    hides its own. `config` is not a question the fold checkpoint can
    re-verify, so recording it would make every fold that meets a pruned
    measured trunk unsaveable. Hiding it loses nothing: the checkpoint's
    fingerprint (`foldckpt.fingerprint`) hashes every config entry git lists,
    these included, so a restore still sees a promisor remote appear. The
    shallow and existence questions are ones the checkpoint re-verifies, and
    they are recorded."""
    from . import rowworld, vcs         # DEFERRED — rowworld imports us.
    rc, out = rowworld._git(gitdir, "rev-parse", "--is-shallow-repository")
    if rc != 0 or out.strip() != "false":
        return None
    with vcs.observed(None):
        rc, _out = rowworld._git(gitdir, "config", "-z", "--get-regexp",
                                 dispatches._PARTIAL_ODB_CONFIG)
    if rc != 1:                         # 0 is a partial odb; else unreadable
        return None
    rc, _out = rowworld._git(gitdir, "cat-file", "-e", sha)
    return True if rc == 1 else None


def _carriage_reached_witness(gitdir, work_tip, trunk_ref, trunk_sha,
                              replay_gap):
    """WITNESS FAMILY 2, ANCESTRY AND PATCH IDENTITY. -> (answer, detail)

    A FUNCTION OF THE COMMIT IDS IT IS HANDED, UNDER A HISTORY VIEW THE READ
    PINS. `git cherry` and `rev-parse` reach no merge policy at all — but git
    interprets an id through a VIEW rather than in a vacuum, and three
    mechanisms rewrite that view without changing one id:

    1. REPLACEMENT IS DISABLED AT EVERY READ. `rowworld._scrubbed_env` sets
       `GIT_NO_REPLACE_OBJECTS`, so a `refs/replace/<oid>` entry cannot
       substitute a different object for one the ids name. Without it,
       replacing the landed twin whose patch identity is the whole of the match
       turns a uniformly `-` range into `+` with every id unchanged.
    2. GRAFTS ARE DISABLED AT EVERY READ. The same overlay points
       `GIT_GRAFT_FILE` at `os.devnull` — the spelling `landreq._object_view`
       established and measured, because `--no-replace-objects` does NOT cover
       a legacy grafts file and a graft rewrites parentage outright.
    3. A SHALLOW BOUNDARY IS REFUSED, not pinned. There is no bypass: the
       parent objects are genuinely absent. `carriage_proof` asks
       `_carriage_shallow_refusal` before either family runs and answers
       UNKNOWN there.

    THE GIT READS TAKE THE RESOLVED SHA, THE PROSE TAKES THE REF (round-one
    finding 1). `trunk_sha` is the object this measurement is about; `trunk_ref`
    is what the operator called it and appears only in the sentences and in the
    detail beside the sha. An unresolvable ref keeps the old behaviour — the
    witness runs against the name and answers silence.
    """
    from . import rowworld              # DEFERRED — rowworld imports us.
    reached, unmatched = rowworld._reached_trunk(gitdir, work_tip,
                                                 trunk_sha or trunk_ref)
    if reached is True:
        return True, {"gitdir": gitdir, "trunk_ref": trunk_ref,
                      "trunk_sha": trunk_sha, "base": None, "tip": work_tip,
                      "witness": dispatches.REACHED_TRUNK}
    if unmatched:
        # NAMED, AND NOT CALLED ABSENT. `git cherry` marks the right side of
        # a symmetric difference, so a lane that merged trunk after its own
        # work landed loses the twin it would have matched against and reads
        # `+` for commits trunk demonstrably has. The refusal stands either
        # way — an unmatched commit is never affirmed — but the sentence says
        # what was measured instead of what would have been convenient.
        return None, ("%s could not be matched onto %s by patch identity "
                      "(%s) — either it never landed, or this tip has merged "
                      "%s since, which leaves `git cherry` nothing upstream "
                      "to match against"
                      % ("%d commit(s) up to %s" % (len(unmatched),
                                                    work_tip[:12]),
                         trunk_ref,
                         ", ".join(sha[:12] for sha in unmatched[:4]),
                         trunk_ref))
    # PATCH IDENTITY IS NOT ALWAYS ASKED, SO IT MUST NOT ALWAYS BE BLAMED.
    # `_reached_trunk` short-circuits on ancestry and never runs `git cherry`
    # for an ancestor, so nothing unmatched here has two OPPOSITE causes. A
    # row's `tip` is its filing sha, so an ancestor tip is the ordinary case.
    # THE GATE IS UNCHANGED: only the sentence learns which question went
    # unanswered.
    if dispatches._reached_by_ancestry(gitdir, work_tip, trunk_sha or trunk_ref):
        return None, ("%s, and %s is ALREADY history of %s — `git cherry` "
                      "was never asked, because an ancestor leaves it an "
                      "empty range and an empty range affirms every possible "
                      "trunk. This witness cannot speak for this tip; it is "
                      "NOT a finding that the work did not land"
                      % (replay_gap, work_tip[:12], trunk_ref))
    return None, ("%s, and %s did not reach %s by patch identity either"
                  % (replay_gap, work_tip[:12], trunk_ref))


def carriage_proof(row, rows, carriers, gitdir, trunk_ref, measured=None):
    """Is this row's work CARRIED AT TRUNK HEAD RIGHT NOW -- or, given
    `measured`, at that trunk commit? -> (True/False/None, detail)

    `measured` IS FOR REPLAY ONLY (task/3056). The `carried` arm of
    `_close_event_error` hands in the close's recorded `closing_trunk_sha`
    once `_measured_trunk_is_history` has said that commit is still history
    of trunk, and every witness below is then asked about that commit instead
    of the head. Nothing else changes: the same witnesses run in the same
    order, so a close whose first witness answered at close time is answered
    by the first witness again. The ladder that authorizes a close passes
    none, and measures the head as before.

    ONE derivation, three callers — the `discharging_row` shape, and for the
    same reason: the ladder authorizes with it, the writer re-derives it under
    the lock, and replay re-derives it again, so a forged `close` event is
    INERT rather than merely unlikely. A proof each caller computes its own
    way is a proof the weakest caller defines.

    IT DELEGATES AND DOES NOT REIMPLEMENT. `rowworld` owns the relation —
    replay the work onto HEAD from its chain-bound base and ask whether the
    result IS HEAD — and this module must not grow a second, weaker answer to
    the same question. The import is DEFERRED because `rowworld` imports this
    module at load time; a module-level import here would be the cycle.

    THE TRI-STATE SURVIVES THE TRIP, deliberately. True affirms. False is a
    MEASURED refusal against a POSITIVE anti-carriage artifact — which the
    relation does not currently mint, so in practice a non-affirming answer
    arrives as None. None means NO WITNESS AFFIRMED, which includes both "the
    question could not be asked" and "no witness could see it". A caller that
    reads either as "absent" converts unmeasurable into gone, which is the
    whole bug class this verb exists inside — and the one that cost 98 of 105
    known-landed rows a false FALSE before the revised ruling. `detail` names the (base, tip) the
    answer was computed from so a close can record its own re-derivation.

    TWO WITNESS FAMILIES, TRIED IN STRENGTH ORDER, and the second one is why
    this door had a floor above zero for weeks (task/close-ladder). The
    replay family asks about trunk HEAD's CONTENT and needs an immutable
    (base, tip); the REACHED-TRUNK family asks about trunk's HISTORY and
    needs only a tip. Neither implies the other and the fallthrough is
    one-directional: only SILENCE from the replay family reaches the second,
    because a measured True or False from the stronger question is the
    answer and must not be second-guessed by a weaker one. BOTH families are
    derived on every FOLD and NEITHER answer is ever stored on its own — see
    the note above `_carriage_trunk_sha` for why a memo of either one cannot be
    made correct, and for the fold checkpoint that reuses the close only after
    re-asking git every question this function asked.

    WHY A SECOND FAMILY RATHER THAN A LOOSER FIRST ONE. Both content
    witnesses go quiet the moment trunk edits the same files again, which is
    every land older than about an hour on a busy lane. And `_work_pair`
    correctly returns no pair at all for a `--new-work` review row — it is
    its own chain root, so there IS no base to replay from and none can be
    invented. Loosening either witness would have bought those rows with a
    proof that no longer discriminated; `git cherry` buys them with a
    STRONGER git proof than `landed` already accepts (a uniformly `-` range,
    against `landed`'s single-commit patch id).

    `detail["witness"]` NAMES WHICH FAMILY ANSWERED, and the close records
    it. Relief that cannot say which proof carried it records a measurement
    nobody can re-run — the same law the landed doors were held to."""
    from . import rowworld              # DEFERRED — rowworld imports us.
    base, tip = rowworld._work_pair(row, rows, carriers)
    work_tip = tip or rowworld._sha((row or {}).get("reviewed_tip"))
    if not work_tip:
        # THE SHA THE ROW WAS FILED AT IS CHAIN-BOUND TOO (task/2090). A
        # review row awaiting its FIRST verdict has no `reviewed_tip` — that
        # field is populated by the verdict alone — and no carrier, so the
        # rung above refused it "bound to no immutable tip" while its own
        # `tip`, the immutable sha the dispatch itself bound, sat beside the
        # refusal unread. That is not a recomputed base, which is the vacuity
        # carriage-replay-base-must-be-chain-bound forbids: it is the
        # chain-bound dispatched sha itself, exactly the anchor that premise
        # names. It reaches only the reached-trunk family below — there is
        # still no immutable base, so nothing can be replayed — which is
        # also the family that answers "did this work reach trunk" without
        # needing one. A row with neither field still refuses, unchanged.
        work_tip = rowworld._sha((row or {}).get("tip"))
    if not work_tip:
        return None, ("this row is bound to no immutable tip: neither its "
                      "own reviewed tip, a carrier's, nor the sha it was "
                      "filed at, and a proof needs something immutable to "
                      "be about")
    rc, _out = rowworld._git(gitdir, "cat-file", "-e",
                             work_tip + "^{commit}")
    if rc != 0:
        return None, ("the tip object %s is not in %s — carriage cannot "
                      "be measured across repositories, and this is the "
                      "gap rather than a reason to weaken the proof"
                      % (work_tip[:12], gitdir))
    if base and tip:
        rc, _out = rowworld._git(gitdir, "cat-file", "-e",
                                 base + "^{commit}")
        if rc != 0:
            return None, ("the base object %s is not in %s — carriage "
                          "cannot be measured across repositories, and this "
                          "is the gap rather than a reason to weaken the "
                          "proof" % (base[:12], gitdir))
    # ONE RESOLUTION FEEDS BOTH FAMILIES, and it happens HERE rather than
    # inside either of them: the sha below is what every git read behind the
    # witnesses is handed, so there is no window in which trunk can move
    # between the question and the answer (round-one finding 1). A MEASURED
    # trunk is already an object, so it is not resolved again.
    trunk_sha = str(measured).lower() if dispatches._FULL_TIP.fullmatch(
        str(measured or "").lower()) else dispatches._carriage_trunk_sha(gitdir, trunk_ref)
    # AND A SHALLOW REPOSITORY IS REFUSED BEFORE EITHER FAMILY RUNS. Replacement
    # and grafts are pinned off at every read; a shallow boundary cannot be,
    # because the parent objects behind it are absent, so it is the one history
    # view this proof declines to run in at all.
    shallow = dispatches._carriage_shallow_refusal(gitdir)
    if shallow:
        return None, shallow
    answer, detail = dispatches._carriage_replay_witness(gitdir, base, tip, trunk_ref,
                                             trunk_sha)
    if answer is not None:
        return answer, detail
    # AND THE SECOND FAMILY IS DERIVED TOO. No answer of it is stored on its
    # own, which is what leaves no entry to invalidate; the fold checkpoint
    # that carries the close re-asks every git question below before it is
    # reused (see the note above `_carriage_trunk_sha`).
    return dispatches._carriage_reached_witness(gitdir, work_tip, trunk_ref, trunk_sha,
                                     detail)


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
