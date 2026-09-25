#!/usr/bin/env python3
"""Is THIS seat qualified to land THIS lane, right now — computed, not judged.

WHY THIS EXISTS. Landing was one agent's job by convention. The owner asked
whether that is an unnecessary single point of failure and the meld
(the owner, the integrator and a builder seat) answered YES for the
MECHANISM and NO for the ROLE:

  * LEADERSHIP is one agent holding the ACCOUNT of what is landing and why.
    Two leads is contradiction, not redundancy. Unchanged.
  * THE ACT OF LANDING needs SERIALISATION, not a single person. Git gives
    ff-only on one trunk, which forces an ORDER, not an OWNER — and
    `landlock:helm` already serialises it.

The measured cost of conflating them: an integrator's attention became the
queue for every merge AND every finding, so a correct refusal reached its
author ~40 minutes after the executor found it, batched behind 91 rows.

WHAT "QUALIFIED" MEANS, and the whole point is that it is a PREDICATE rather
than a judgement — these are the checks the integrator already ran by hand:

  (i)   holds landlock:helm
  (ii)  a gate minted on the POST-REBASE TREE it is actually landing
  (iii) an approving cross-family verdict at the exact REVIEWED tip
  (iv)  a changed-file set DISJOINT from every other approved-unlanded lane
  (v)   freeze state admits, and announce + rearm run as steps of the verb

(ii) AND (iii) BIND DIFFERENT TREES ON PURPOSE. The verdict is about what a
human REVIEWED; the gate is about what actually BECOMES TRUNK. Today that
final post-rebase gate exists only because it is the integrator's last step,
and in a self-land world it is nobody's unless the verb owns it. Collapsing
them to one tip is the mistake this module exists to make impossible.

(iv) CAME FROM A SECOND READ; IT IS THE ONE MY FIRST THREE MISSED. (i)-(iii)
prove a lane is INDIVIDUALLY sound and say nothing about whether it COMPOSES.
Measured: three lanes each edited the same vcs.py spawn-count docstring
(22->25, 22->23, true merged 26); every one would have passed (i)-(iii), and a
mutex cannot see it. Disjoint file sets are a cheap PRE-FILTER, not a
composition proof — cross-file semantic coupling still exists and only the
post-rebase gate catches it, which is why (ii) must not be softened later on
the grounds that (iv) already covers this.

FAIL-CLOSED, AND THAT IS THE OPPOSITE OF helm/board.py ON PURPOSE. The board
guard fails OPEN because a board write is recoverable and the guard is for an
honest mistake. A land is IRREVERSIBLE on a shared trunk, so anything this
module cannot prove is a REFUSAL, never a pass. UNKNOWN is not qualified.
"""

import json
import os
import sys

from . import home, pk, vcs

OK = "ok"
REFUSE = "refuse"
UNKNOWN = "unknown"          # treated as REFUSE; kept distinct so the caller
                             # can say "I could not tell" rather than "no"

LANDLOCK = "landlock:helm"


def _git(repo, *args):
    """(ok, stdout) through the VCS SEAM, never a raw subprocess.

    The first cut spawned git directly and DirectSpawnAuditTest caught it —
    the same audit I cited as the precedent for this lane's own closure tests,
    failing on my code. It is right: a direct spawn here would be a second
    place that decides how helm talks to git, and the seam exists so there is
    one. `probe` returns stripped stdout on success and None when it could not
    speak, and '' IS an answer (a clean diff), which is why the caller must
    distinguish None from empty rather than treating both as falsey."""
    out = vcs.backend(repo).probe(repo, *args, timeout=30)
    return (out is not None), (out or "")


def holds_landlock(seat, claims=None):
    """(state, detail) — clause (i)."""
    if not seat:
        return UNKNOWN, "caller declares no seat, so the lock cannot be matched"
    try:
        from . import seats
        raw = pk.read_json(seats.claims_path(), None) if claims is None else claims
        if not isinstance(raw, dict):
            return UNKNOWN, "claims ledger unreadable — holding is unprovable"
        live = seats._sweep(raw)
    except Exception:        # noqa: BLE001
        return UNKNOWN, "claims ledger unreadable — holding is unprovable"
    row = live.get(LANDLOCK)
    if not isinstance(row, dict):
        return REFUSE, "%s is not held by anyone; claim it before landing" % LANDLOCK
    holder = str(row.get("holder") or "").strip()
    if holder != seat:
        return REFUSE, "%s is held by %s, not %s" % (LANDLOCK, holder, seat)
    return OK, "%s held by %s" % (LANDLOCK, seat)


def gate_binds_tree(receipt, tree, gates=None, repo=None, tip=None,
                    provenance=None, where=None):
    """(state, detail) — clause (ii): the receipt must name the tree BEING
    LANDED, not the tree that was reviewed.

    A receipt for the lane tip is the reviewer's evidence and is NOT this
    check: after a rebase the tree differs, and the difference is exactly where
    cross-file coupling shows up.

    THE TREE IS DERIVED, NEVER TRUSTED (task/285). The first cut compared the
    receipt against a `tree` the CALLER supplied — a gate that can be fed its
    own answer, where feeding it wrong yields a PASS, and its both-directions
    prefix match made even an honest short sha vacuous. When `repo` and `tip`
    are given, the tree being landed is computed here (`rev-parse tip^{tree}`)
    and compared at FULL length; the receipt's recorded tree is resolved to
    its full-length form too, so a 12-char receipt prefix cannot borrow
    equality from truncation, and a receipt naming a tree that does not
    resolve is UNKNOWN, never a pass. The caller's `tree`, when also
    supplied, is only a cross-check: disagreeing with the derived tree is a
    REFUSE that names both, because one of the two inputs is lying and the
    clause cannot tell which.

    AND ITS LAST QUESTION IS PROVENANCE (task/3066): after every content
    clause, which authenticated door placed the receipt in `repo`
    (`gate.land_provenance`). A receipt handed in through `gates` is still a
    receipt somebody must prove ran, so an arm that injects the row injects
    that answer too (`provenance`, a callable (row, repo) -> the same
    (True|False|None, why)); no production caller passes either. `where` is
    the CHECKOUT the land happens in, when `repo` names the repository by
    its shared admin dir: a declared command's scope is read against the
    location's own declaration."""
    if not receipt:
        return REFUSE, "no gate receipt cited for the post-rebase tree"
    derived = None
    if repo and tip:
        ok, out = _git(repo, "rev-parse", "%s^{tree}" % tip)
        if not ok or not out:
            return UNKNOWN, ("cannot derive the tree of %s — the tree being "
                             "landed is unmeasured" % str(tip)[:12])
        derived = out
    if derived is not None and tree:
        # The caller's cross-check value is NORMALIZED before it is judged
        # (a FIX on the reviewed tip): compared raw, an abbreviated --tree
        # refuses against the full derived tree with both displayed sides
        # truncated to the SAME 12 chars — a refusal that cannot say what
        # it saw. Resolve it like the receipt's tree; an unresolvable
        # cross-check is UNKNOWN, not a mismatch.
        ok, out = _git(repo, "rev-parse", "--verify", "--quiet",
                       str(tree) + "^{tree}")
        if not ok or not out:
            return UNKNOWN, ("the caller-supplied tree %s does not resolve "
                             "here — an unmeasurable cross-check, not a "
                             "mismatch" % str(tree)[:12])
        tree = out
        if tree != derived:
            return REFUSE, ("caller says the tree being landed is %s but %s "
                            "resolves to %s — one of those is wrong and this "
                            "clause will not guess which"
                            % (tree[:12], str(tip)[:12], derived[:12]))
    tree = derived or tree
    if not tree:
        return UNKNOWN, "the tree about to be landed is unknown"
    rec = (gates or {}).get(receipt)
    if rec is None:
        try:
            from . import gate as gatemod
            rec, err = gatemod.by_id(receipt)
        except Exception:    # noqa: BLE001
            return UNKNOWN, "gate ledger unreadable"
        if err:
            # by_id REFUSES an ambiguous prefix rather than picking the newest,
            # and that refusal must stay a refusal here: a token naming two
            # runs names neither, and landing on "probably that one" is the
            # shape this module exists to prevent.
            return (UNKNOWN if "unavailable" in err else REFUSE), err
    if not isinstance(rec, dict):
        return REFUSE, "gate receipt %s does not resolve" % receipt
    if str(rec.get("status") or "").upper() != "OK":
        return REFUSE, "gate %s is %s, not OK" % (receipt, rec.get("status"))
    if not rec.get("suite"):
        # A FOCUSED or custom receipt proves the tests it selected and
        # nothing about the rest — and this clause authorizes an IRREVERSIBLE
        # merge. Before focused receipts existed this hole was only
        # theoretical (a custom command that happens to print a green
        # unittest summary); now that a focused run is a first-class cheap
        # verb, the clause must say out loud that the land bar is the whole
        # suite.
        kind = "FOCUSED" if isinstance(rec.get("focus"), dict) else "custom"
        return REFUSE, ("gate %s is a %s receipt — it proves only the tests "
                        "it selected, and a land is authorized only by a "
                        "WHOLE-SUITE run on the tree being landed; run `helm "
                        "gate run`" % (receipt, kind))
    # AND ONLY A SERIAL ONE: the receipt's kind is gate.py's question, asked
    # here because this clause authorizes the merge.
    from . import gate as gatemod
    refusal = gatemod.land_refusal(rec)
    if refusal:
        return REFUSE, refusal
    got = str(rec.get("tree") or "")
    if not got:
        return UNKNOWN, "gate %s records no tree" % receipt
    if repo:
        ok, _out = _git(repo, "rev-parse", "--verify", "--quiet",
                        got + "^{tree}")
        if not ok or not _out:
            return UNKNOWN, ("gate %s records tree %s, which does not resolve "
                             "here — a receipt naming a tree that does not "
                             "exist proves nothing" % (receipt, got[:12]))
        got = _out      # full-length, so a 12-char receipt prefix cannot
                        # borrow equality from truncation
    if got != tree:
        return REFUSE, ("gate %s binds tree %s but the tree being landed is %s "
                        "— a receipt for a DIFFERENT tree proves nothing about "
                        "this merge" % (receipt, got[:12], tree[:12]))
    answer = provenance(rec, repo) if provenance \
        else gatemod.land_provenance(rec, repo, where=where)
    refusal = gatemod.land_provenance_refusal(rec, repo, answer=answer)
    if refusal:
        return (UNKNOWN if answer[0] is None else REFUSE), refusal
    return OK, "gate %s binds the post-rebase tree %s%s" % (
        receipt, tree[:12], " (%s)" % answer[1] if answer[1] else "")


def changed_files(repo, base, tip):
    """(paths, err) — --name-only, never --stat: --stat TRUNCATES long paths,
    so a path-set built from it silently omits files that ARE present."""
    ok, out = _git(repo, "diff", "--name-only", "%s...%s" % (base, tip))
    if not ok:
        return None, "could not diff %s...%s" % (base[:12], tip[:12])
    return set(out.split()), None


def disjoint_from_queue(repo, base, tip, queue, landed=None):
    """(state, detail) — clause (iv). `queue` is [(lane, tip)] of the OTHER
    approved-unlanded lanes, or None when the caller did not gather it.

    NOT-SUPPLIED AND GATHERED-EMPTY ARE DIFFERENT ANSWERS, and conflating them
    was a fail-open in the one direction this module forbids. `[]` means "I
    looked and there are no other approved-unlanded lanes" — a real OK. `None`
    means "nobody looked", which is exactly what a caller that FORGOT to gather
    passes, and it must be UNKNOWN.

    LANDED IS A LADDER, NEVER ANCESTRY ALONE (task/284). `landed`, when given,
    is called per competitor and returns True/False/None — over the WHOLE
    lane range base..tip (a tip-only proof can call a multi-commit
    lane landed when only its final commit was cherry-picked, dropping a
    competitor whose earlier commit still overlaps; use a range-aware ladder
    such as vcs.landed_state, or an adapter covering the range). An
    ancestry-only answer mis-reads every rebased-then-landed row as still
    competing: its old tip is no ancestor of trunk (the land came in under
    a new sha), so it blocks its files FOREVER — the #72 trap. A competitor
    proven LANDED is dropped from the queue — its content is already on
    trunk, so there is nothing left to be disjoint FROM. A competitor whose
    landedness is UNREADABLE (None) is STILL DIFFED: disjointness does not
    depend on landedness, so an uncertain competitor that overlaps REFUSES
    like any other, and only after every diff comes back clean does the
    accumulated uncertainty turn the clause UNKNOWN."""
    if queue is None:
        return UNKNOWN, ("the approved-unlanded queue was not supplied — "
                         "nobody looked, which is not the same as looking and "
                         "finding none")
    mine, err = changed_files(repo, base, tip)
    if err:
        return UNKNOWN, err
    live = []
    uncertain = []
    for lane, other_tip in queue:
        if landed is not None:
            st = landed(other_tip)
            if st is True:
                continue        # on trunk already — nothing to collide with
            if st is None:
                uncertain.append((lane, other_tip))
                continue
        live.append((lane, other_tip))
    uncertain_overlap = []
    for lane, other_tip in live + uncertain:
        theirs, err = changed_files(repo, base, other_tip)
        if err:
            return UNKNOWN, "%s (lane %s) — overlap unprovable" % (err, lane)
        overlap = sorted(mine & theirs)
        if overlap and (lane, other_tip) in uncertain:
            # Uncertainty is debt ONLY where overlap exists.
            uncertain_overlap.append((lane, overlap))
            continue
        if overlap:
            return REFUSE, ("overlaps lane %s on %s — the integrator "
                            "adjudicates an intersecting pair, because only a "
                            "holder of BOTH can say what the merged value "
                            "should be" % (lane, ", ".join(overlap[:4])))
    if uncertain_overlap:
        lane, overlap = uncertain_overlap[0]
        return UNKNOWN, ("lane %s overlaps on %s and whether it already "
                         "landed is UNREADABLE — the overlap is only debt "
                         "if it is genuinely unlanded, and this clause "
                         "cannot tell" % (lane, ", ".join(overlap[:4])))
    return OK, "changed-file set disjoint from %d approved-unlanded lane(s)" % len(live)


def freeze_admits(board=None):
    """(state, detail) — clause (v)'s read half. Freeze is a NARRATIVE board
    key: the integrator's to set, everyone's to consult."""
    try:
        from . import board as boardmod
        b = board if board is not None else pk.read_json(boardmod.path(), None)
    except Exception:        # noqa: BLE001
        return UNKNOWN, "board unreadable — freeze state unprovable"
    if not isinstance(b, dict):
        return UNKNOWN, "board unreadable — freeze state unprovable"
    state = str(b.get("freeze") or "").strip().lower()
    if state in ("", "open", "none", "off"):
        return OK, "freeze open"
    return REFUSE, ("land window is FROZEN (%s) — only an anomaly-draining "
                    "land is admitted while frozen" % state)


def qualify(seat, repo, base, tip, tree, receipt, queue, board=None,
            claims=None, verdict_ok=None, gates=None, landed=None,
            provenance=None):
    """The five clauses -> (bool, [(clause, state, detail)]).

    Qualified ONLY when every clause is OK. UNKNOWN never qualifies: a land is
    irreversible on a shared trunk, so what cannot be proven is refused."""
    clauses = [
        ("i-landlock", holds_landlock(seat, claims)),
        ("ii-gate-binds-landed-tree",
         gate_binds_tree(receipt, tree, gates, repo=repo, tip=tip,
                         provenance=provenance)),
        ("iii-cross-family-approve", verdict_ok
         if verdict_ok is not None
         else (UNKNOWN, "no verdict state supplied by the caller")),
        ("iv-disjoint-from-queue",
         disjoint_from_queue(repo, base, tip, queue, landed=landed)),
        ("v-freeze-admits", freeze_admits(board)),
    ]
    rows = [(name, st, detail) for name, (st, detail) in clauses]
    return all(st == OK for _n, st, _d in rows), rows


def render(rows):
    """One line per clause — a refusal must say WHICH clause and WHY, or the
    verb teaches nothing when it says no."""
    return "\n".join("  %-28s %-8s %s" % (n, st.upper(), d) for n, st, d in rows)


_USAGE = ("usage: helm landgate --lane L --tip SHA --gate ID "
          "[--tree SHA] [--base REF] [--repo P]\n"
          "  READ-ONLY. Answers 'may THIS seat land THIS lane right now', one "
          "line per clause,\n"
          "  and answers it the same way whether it says yes or no — a refusal "
          "that does not\n"
          "  name its clause teaches nothing. It NEVER lands: the actuator is a "
          "separate verb\n"
          "  with a separate bar, so a seat can ask the question without being "
          "able to act on it.\n"
          "  The landed tree is DERIVED from --tip; --tree is only an optional "
          "cross-check\n"
          "  and a disagreement refuses, naming both.")


def cmd_landgate(args):
    """helm landgate — ask the five clauses, land nothing.

    WIRED DELIBERATELY, and the stop-guard is why. It refused this lane with
    "you added 1 module(s) that NOTHING can reach — built, not wired:
    helm/landgate.py", and it was right in a way I had argued myself out of:
    I shipped a predicate with no caller and called that a clean separation.
    A predicate nobody can invoke is not reviewable in practice — a reader
    cannot run it against a real lane and see what it says. wiring.ALLOWED
    exists for modules that are legitimately unreachable; this one is not, it
    was merely unfinished, and declaring it would have been the "I forgot"
    outcome that list explicitly refuses."""
    args = list(args or [])
    if not args or args[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0 if args else 2

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args \
            and args.index(name) + 1 < len(args) else default

    repo = opt("--repo") or os.getcwd()
    lane, tip = opt("--lane"), opt("--tip")
    tree, receipt = opt("--tree"), opt("--gate")
    base = opt("--base", "origin/main")
    # --tree is NOT required: the tree being landed is derived from --tip
    # (task/285 — a gate fed its own answer is not a gate). Supplying it asks
    # for a cross-check, and a disagreement refuses naming both.
    if not (lane and tip and receipt):
        print(_USAGE, file=sys.stderr)
        return 2

    from . import seats
    # ASK THE RESOLVER THAT ANSWERS. `own_name()` is the process's DECLARED
    # identity alone ($HELM_CHAT_NAME) and returns None when nothing declared
    # one — which is the ordinary state of a claude-direct seat, mine included.
    # Clause (i) then reads "caller declares no seat, so the lock cannot be
    # matched" -> UNKNOWN, and UNKNOWN never qualifies. So the self-land
    # predicate could not evaluate its FIRST clause for an entire class of
    # seat, and told them nothing they could act on.
    #
    # `acting_seat` is not a looser answer, it is the SAME answer plus the
    # fallbacks: own declared name FIRST (so the cross-seat contamination its
    # own docstring records — a session id sitting in a stranger's roster row
    # hijacking this process's identity — stays closed), then the session->row
    # map, then the auto-name floor. seats.py:3016 already asks it this way on
    # the stop path; landgate was the caller that did not.
    #
    # Measured 2026-08-02 on a live seat with HELM_CHAT_NAME unset:
    #   own_name()  -> None            -> clause (i) UNKNOWN
    #   acting_seat -> 'helm-claude-2' -> clause (i) refuse (a real answer)
    # REFUSE is the correct verdict there — the seat genuinely holds no lock —
    # and it is the one a reader can act on. A live seat hit it as the fourth
    # face of an identity finding; the other three were the identity floor and
    # this one was a call site.
    #
    # SAFE HERE BY CONSTRUCTION TOO: landgate is READ-ONLY and never lands, so
    # a misresolved identity misprints a report rather than authorizing a
    # merge. The actuator that eventually does land still owes its own check.
    seat = seats.acting_seat(cwd=seats.safe_cwd())
    # THE CLI CANNOT ANSWER "QUALIFIED", AND SAYS SO INSTEAD OF PRETENDING.
    # It does not gather two of the five inputs: the approved-unlanded QUEUE
    # (clause iv) and the cross-family VERDICT state (clause iii). The first
    # cut passed queue=[] and verdict_ok=None and then printed
    # "QUALIFIED"/"NOT qualified" off all(OK) — which made QUALIFIED
    # UNREACHABLE and rc 1 unconditional, even in a perfect state, with a NOTE
    # explaining a branch that could never run (gate 79b508b87b1ca6c5).
    # A reporter that renders a verdict it structurally cannot reach is the
    # same category error as a gate receipt that binds the wrong tree.
    ok, rows = qualify(seat=seat, repo=repo, base=base, tip=tip, tree=tree,
                       receipt=receipt, queue=None, verdict_ok=None)
    print("helm landgate: CLAUSE REPORT for %s as %s — this verb does NOT "
          "answer 'qualified'" % (lane, seat or "(no seat)"))
    print(render(rows))
    print("\n  WHY NOT: clause (iii) is UNKNOWN because this verb does not "
          "gather verdict state, and clause (iv) is UNKNOWN because it does "
          "not gather the approved-unlanded set — it passes queue=None, and "
          "an unsupplied queue is UNKNOWN, never a vacuous OK. Those two are "
          "inputs an actuator supplies. Everything above them is measured "
          "and real.")
    # rc reports whether the REPORT succeeded, never whether the lane
    # qualifies — conflating those is how a caller would read a structural
    # UNKNOWN as a refusal about its own work.
    return 0
