"""helm dispatch — the CLI verb table, dispatched from `cli.VERBS`.

THE LEDGER'S VERB SURFACE, NOT THE LEDGER. Nothing in `dispatches` calls
anything here: measured at the cut, this module has ZERO in-edges. It is a
leaf CONSUMER of the ledger API, which is what makes it the honest place to
cut a file that had 4,094 bytes of headroom under the never-track ceiling
while three more cures were queued to land in it.

THE CODE MOVED WHOLE, AND EVERY LEDGER NAME IS SPELLED `dispatches.NAME`
AT ITS CALL SITE. An import list would bind each object once at import, so a
test that patches an attribute on the ledger and then drives a verb would
reach the original here and measure nothing; the module spelling keeps the
lookup at CALL TIME, exactly as a bare global did inside the ledger.

THE CYCLE IS BROKEN THE WAY THIS PACKAGE ALREADY BREAKS IT, and the direction
is the one the ledger's other satellites use: `rowworld` and `landreq` import
`dispatches` EAGERLY at module level, and `dispatches` defers its own imports
of them into function bodies. This module does the same — it imports the
ledger eagerly, and `dispatches` binds these names at the END of its own
module body, after every name they need exists.
"""
import json
import re
import sys
import time

from . import dispatches


_IMPERFECT_FLAG = "--imperfect"

#: Words a seat types for "show me this row", each answered exactly as
#: `triage <id>` (task/3382: 78 calls to show/read/get/status/brief on the
#: local seats, 47 of them `show`, each answered with the whole usage).
TRIAGE_ALIASES = ("show", "read", "get", "status", "brief")


def synopses(usage=None):
    """{subverb: its ONE usage line}, cut from `dispatches.USAGE` itself so
    the two cannot drift: the clauses split at a top-level ` | `, and every
    parenthesised explanation is dropped. The triage aliases each get the
    line `show` has, under their own name.

    WHY (task/3382): an unknown subverb, and a known one given bad arguments,
    printed all 8,645 characters of USAGE; 7 of the 11 episodes measured on
    the local seats never recovered from it."""
    text = (usage or dispatches.USAGE).split("usage: helm dispatch ", 1)[-1]
    clauses, buf, depth, i = [], [], 0, 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(" | ", i):
            clauses.append("".join(buf))
            buf = []
            i += 3
            continue
        elif depth == 0:
            buf.append(ch)
        i += 1
    clauses.append("".join(buf))
    out = {}
    for clause in clauses:
        words = " ".join(clause.split())
        if words:
            out.setdefault(words.split()[0], "usage: helm dispatch " + words)
    base = out.get("show")
    for alias in TRIAGE_ALIASES if base else ():
        out[alias] = base.replace("dispatch show ", "dispatch %s " % alias,
                                  1) + "  (the same as triage <id>)"
    return out


def synopsis(verb):
    """One subverb's usage line, or the whole USAGE for a name it lacks."""
    return synopses().get(verb) or dispatches.USAGE


def _typed_tips(rid, tip, patch):
    """(reviewed, patch, rc): the two commit ids a verdict binds, each as the
    FULL id, or rc when one cannot be.

    A FULL-LENGTH TOKEN PASSES THROUGH UNTOUCHED, so `mark_verdict` sees the
    same argument it saw before this existed and every check it runs on what
    is bound (the full-id shape, the stale-tip comparison, the patch
    ancestry) decides exactly as before. Only a SHORTER token is new: it
    resolves in the row's repository through `_resolve_tip`, the resolver
    the send door binds its tip with, which takes exactly one object and
    peels it to a commit; a ref name is never a prefix. The resolved id is
    handed on, so the ledger never stores a prefix.

    MEASURED (task/3382): `verdict` demanded 40 hex while every listing
    prints 12, and the local seats padded the 12 out; odd-length tokens
    named nothing 51-69 % of the time."""
    wanted = [t for t in (tip, patch) if t is not None
              and not dispatches._FULL_TIP.fullmatch(str(t).strip().lower())]
    if not wanted:
        return tip, patch, None
    current, unavailable = dispatches.snapshot()
    if unavailable:
        print("helm dispatch verdict: the ledger is unavailable, so a commit "
              "prefix cannot be resolved (%s); type the full commit id"
              % unavailable, file=sys.stderr)
        return None, None, 1
    # DEFAULT-CLOSED, as the writer's own resolution is: a row the writer
    # would refuse is refused here in the writer's words, before any git.
    row, why = dispatches._resolve_row(current, rid)
    if why:
        print("helm dispatch: " + why, file=sys.stderr)
        return None, None, 1
    repo = row.get("repo_root") or row.get("repo_id")
    out = []
    for name, token in (("the reviewed tip", tip), ("--patch-tip", patch)):
        typed = None if token is None else str(token).strip().lower()
        if typed is None or dispatches._FULL_TIP.fullmatch(typed):
            out.append(token)
            continue
        full = None
        if repo and re.fullmatch(r"[0-9a-f]{7,63}", typed):
            full = dispatches._resolve_tip(repo, typed,
                                           infer_sha_branch=False)[0]
        if not full:
            print("helm dispatch verdict: %s %s is not one commit's id or "
                  "unique prefix (at least 7 hex) in %s%s" % (
                      name, token, repo or "a repository this row names",
                      dispatches._typedids().tip_hint(repo, typed, known=(
                          (row.get("tip"),) if name == "the reviewed tip"
                          else ()))),
                  file=sys.stderr)
            return None, None, 1
        out.append(full)
    return out[0], out[1], None


def _unnamed_tip_hint(rid, tip):
    """The candidate a refused FULL-LENGTH tip could have meant, or ''.

    Asked only after `mark_verdict` refused, and only of a token that is not
    the row's own tip, so an accepted verdict never pays for it. A padded
    12-hex prefix is a full-length token that names no object: the refusal
    stays the writer's, and this adds the commit its first 12 name."""
    typed = str(tip or "").strip().lower()
    if not dispatches._FULL_TIP.fullmatch(typed):
        return ""
    current, unavailable = dispatches.snapshot()
    row, why = (None, unavailable) if unavailable else \
        dispatches._resolve_row(current, rid)
    if why or typed == row.get("tip"):
        return ""
    return dispatches._typedids().tip_hint(
        row.get("repo_root") or row.get("repo_id"), typed,
        known=(row.get("tip"),))


def _unknown_subverb(verb):
    """rc 2 and at most two lines: the subverbs, and the closest one's usage.
    The whole grammar stays on `helm dispatch --help`."""
    import difflib
    lines = synopses()
    print("helm dispatch: unknown subverb %r (subverbs: %s; `helm dispatch "
          "--help` prints the full usage)" % (verb, ", ".join(lines)),
          file=sys.stderr)
    near = difflib.get_close_matches(str(verb), list(lines), n=1)
    if near:
        print("closest: " + lines[near[0]], file=sys.stderr)
    return 2


def patch_note(row):
    """The REVIEWER PATCH line a row carrying a reviewer's cure earns, or None.

    ONE OWNER FOR THE SENTENCE, because the row shows up on two triage surfaces
    — the named-id answers and the cure census — and a second copy of this
    format is how two sections of one table come to disagree about what a
    column means (`_triage_line`'s own lesson, one screen down).
    """
    tip = str((row or {}).get("patch_tip") or "")
    if not isinstance(row, dict) or not dispatches._FULL_TIP.fullmatch(tip):
        return None
    return ("  REVIEWER PATCH %s by @%s — a committed cure off the reviewed "
            "tip; merge that exact tip into a new composition commit, keeping "
            "both reviewed shas reachable, and credit both authors" % (
                tip[:12], row.get("patch_author") or row.get("recipient") or "?"))


UNPLACEABLE_SCOPE_CLASSES = ("origin_unknown", "project_unresolved")


def _unplaceable_classes(scope_project):
    """Which `_scope_class` answers are OUTSIDE the claim a listing is making.

    THE BUCKET EXISTS TO STOP A PROJECT CLAIMING A ROW, so which classes belong
    in it depends on whether a project is being claimed. A scope that resolves
    to a PROJECT claims one, and neither a row with no repository nor a row in a
    repository no project claims can be inside it. A scope with NO project —
    `cwd_scope`'s documented fallback, a seat standing in an unregistered
    checkout — claims nothing but a repository, and there `project_unresolved`
    is the OLD ANSWER and must stay: exiling every unclaimed repository's rows
    from the listing of a reader who is themselves in an unclaimed repository
    would empty the board of a team using helm before they ever ran `helm sync`.
    A row with NO repository at all is unplaceable either way, because absence
    of a repository is not evidence of the reader's."""
    return UNPLACEABLE_SCOPE_CLASSES if scope_project else ("origin_unknown",)


def _holdings_set_aside(rows, owed_ids):
    """The part of a `--to SEAT` listing's rows that `--open` sets aside and
    `helm seat reassign` counts as holdings, by kind (task/3881)."""
    from . import query
    held = sum(1 for r in rows if r.get("id") not in owed_ids
               and query.query_is_held(r))
    carried = sum(1 for r in rows if r.get("id") not in owed_ids
                  and not query.query_is_held(r) and dispatches._open(r))
    parts = [text for n, text in ((held, "%d held" % held),
                                  (carried, "%d carried by a successor"
                                   % carried)) if n]
    return ("; of this seat's holdings it sets aside %s (`helm seat "
            "reassign` counts both)" % " and ".join(parts)) if parts else ""


def _scope_class(scope_repo, scope_project):
    """(row -> "local" | "origin_unknown" | "project_unresolved" | "foreign")

    WHICH BUCKET a row standing here belongs in — and the three answers are
    THREE, never two.

    THE POLICY IS THE CLASSIFIER'S, NEVER REPOSITORY EQUALITY (task/2437 round
    two). Both CLI narrowings shipped as `_real(row["repo_id"]) == _real(scope)`
    while their own clause advertised a PROJECT, and a project is not a
    repository: the registry records several paths per project
    (`cv_scope.cwd_prefixes` — the multi-prefix producer `inject._ledger`
    resolves against), so a seat standing in one of a project's repositories was
    shown none of its SIBLING repositories' rows under a header naming the whole
    project. Equality also EXILED every legacy row: a row with no `repo_id` — the
    true reading of a v1 row — matched no scope at all, so the oldest
    obligations in the ledger were the ones a scoped listing could never print.

    AND KEEPING A ROW IS NOT THE SAME CLAIM AS OWNING IT (task/2468). A
    BOOLEAN collapses the three reasons a row can be kept into one, so a caller
    whose clause names a PROJECT cannot tell "this project owns it" from "nobody
    can place it" — and a listing from any checkout then files every unplaceable
    row under whatever project the reader is standing in. The failure mode is
    one obligation with as many owners as the registry has projects, worst on
    the smallest project: a checkout with three rows of its own reads as a
    checkout with twelve, nine of them another repository's work. Absence of a
    repository is not
    evidence of local origin: defaulting it to "mine" silently adopts exactly
    the rows least entitled to a home, which is `landreq._mark_foreign_rows`'
    own paragraph, and that function has stamped `origin_unknown` as ITS OWN
    STATE since task/974. So this returns the state rather than a verdict, in
    the vocabulary that function already writes, and the callers bucket on it.

    SO THIS IS `landreq._mark_foreign_rows`' POLICY, EXPRESSED AS A CLASSIFIER,
    and the two must agree because they answer one question on two surfaces
    (the board marks, the CLI buckets). Its four states:
      - same repository, or a repository of the SAME registered project
                                                              -> `local`
      - a row with NO repo_id                       -> `origin_unknown`
      - a repository no project claims (or one whose path cannot even be
        resolved)                               -> `project_unresolved`
      - a repository owned by a DIFFERENT project           -> `foreign`
    A row the registry cannot place is still never adopted NOR exiled — the
    UNMARKED pattern both ends obey. It is PRINTED, under a heading of its own
    that says nobody can place it, which is the only rendering that is true:
    hiding it loses work, and filing it under the reader's project states a
    provenance no event in the ledger records.

    THE REF IS NOT RE-RESOLVED HERE, and the reason is the registry's SIZE
    rather than a preference: probing an unplaceable row's ref against every
    registered repository costs one `git cat-file` spawn per registered project,
    and this box's registry holds 236 of them — on a surface the resume-turn
    hook runs on every compaction. A clone also shares objects with its parent,
    so a hit proves the ref is REACHABLE from a repository and never that the
    row
    was written there — the answer would be ambiguous exactly where it matters.
    UNKNOWN, said out loud, is the cheap true answer.

    `_project_of` is memoised per repository for this one listing, because the
    classification runs per row over a ledger whose rows share a handful of
    repositories and the resolver reads the registry each time."""
    scope_real = dispatches._real(scope_repo)
    resolved = {}

    def classify(row):
        theirs = str((row or {}).get("repo_id") or "").strip()
        if not theirs:
            return "origin_unknown"
        real = dispatches._real(theirs)
        if real is None:
            return "project_unresolved"
        if real == scope_real:
            return "local"
        if real not in resolved:
            resolved[real] = dispatches._project_of(real)
        their_project = resolved[real]
        if their_project is None:
            return "project_unresolved"
        return "local" if their_project == scope_project else "foreign"

    return classify


def _unplaceable_fact(project):
    """(the noun phrase, the qualification) — the ONE fact every surface that
    renders or stamps an unplaceable row says, in ONE wording (round three).

    A CORRECTED HEADING CANNOT RETRACT AN ASSERTION MADE ABOVE IT. Round two
    qualified the UNKNOWN bucket's heading — a `None` from `_project_of` is a
    FAILED LOOKUP and never proof of non-membership — and left two other
    sentences about the same rows asserting the opposite: the list narrowing
    clause said "a row naming no repository this registry can place is in NO
    project", and `--json`'s accounting said "row(s) no registered project
    places". Both are claims about the world; what this code measured is an
    empty or failed LOOKUP.

    AND THE FAILURE IS ORDINARY, NOT HYPOTHETICAL. `cwd_scope` resolves the
    caller's project through one registry read, and each later per-row lookup
    is ANOTHER read — `registry.load` reacquires its lock and re-reads the file
    every time, so there is no per-listing snapshot. `project_for_cwd` with
    `strict=False` swallows an unreadable registry to `None` (that is its
    documented fail-open), so a listing can resolve its own scope and then
    classify a REGISTERED sibling repository as `project_unresolved` one
    millisecond later. Under the old wording that listing told its reader the
    sibling's row was in NO project, contradicting its own heading and, on
    `--json`, with no heading anywhere to contradict.

    SO ONE HELPER FEEDS BOTH RENDERED SURFACES AND THE HEADING, because two
    wordings of one fact is how they came to disagree in the first place. The
    noun phrase is what the row IS ("could not place", an answer about this
    registry read); the qualification carries the three things a reader can act
    on — the lookup failed or came back empty, the row is not counted in this
    project, and none of that proves the row is in no project.

    The noun phrase is a RELATIVE CLAUSE ("this registry could not place") so
    one string serves both "a row <clause> is listed below" and "3 row(s)
    <clause> are RETAINED": a pluralizable noun would have needed two spellings
    of the phrase, which is the defect this helper exists to close."""
    return ("this registry could not place",
            "the project lookup for its repository FAILED or came back empty "
            "— and a row recording no repository had nothing to look up — so "
            "it is NOT counted among project %s's rows, and nothing here "
            "proves the row is in NO project" % project)


def _unplaceable_heading(rows, project):
    """The ONE sentence both listings print above the UNKNOWN bucket.

    Two verbs rendering this bucket in two sentences is how they come to
    disagree about what it MEANS, which is the defect `cwd_scope` was extracted
    to close one layer up.

    EACH SUB-BUCKET SAYS WHAT ITS OWN ROWS CARRY (round two, finding 2). One
    shared sentence said the rows record nothing about where their work lives
    and promised a `helm sync` would place them, and BOTH halves were false of
    the `project_unresolved` half: that row DOES carry a canonical `repo_id`
    (only its PROJECT is unknown), and `_project_of` returns None for a
    repository nobody registered, a path that cannot be resolved AND a registry
    that cannot be read alike — the ordinary resolver catches Exception. So a
    None is a FAILED LOOKUP and never proof of non-membership, and a surface
    that reads it as proof sends the reader to run `helm sync` against a
    registration that may already exist. The one fact a reader can act on is
    WHICH lookup came back empty, so that is what this prints."""
    origin = sum(1 for row in rows
                 if not str((row or {}).get("repo_id") or "").strip())
    unresolved = len(rows) - origin
    detail = []
    if origin:
        detail.append("%d row(s) record NO repository — the row carries no "
                      "`repo_id` (written before the repository stamp), so "
                      "nothing in the ledger records where that work lives"
                      % origin)
    if unresolved:
        detail.append("%d row(s) DO record a repository, and the project "
                      "registry lookup for it came back empty — no registered "
                      "project claims that repository, or its path could not "
                      "be resolved, or the registry could not be read, which "
                      "are ONE answer here: what this reports is the failed "
                      "LOOKUP, not that the checkout is unregistered"
                      % unresolved)
    return ("UNKNOWN PROVENANCE — %d row(s) NOT counted in project %s: %s. "
            "In every case %s. The reader's directory is not evidence of "
            "where any of them was written."
            % (len(rows), project, "; ".join(detail),
               _unplaceable_fact(project)[1]))


def _json_scope_accounting(withheld, classes, project):
    """The sentences `--json`'s stderr owes about rows it did NOT return, and
    about rows it DID (round two, finding 1).

    WITHHELD AND RETAINED ARE TWO FACTS AND ONE SENTENCE CANNOT CARRY BOTH. The
    first cut printed "this array is the rows <clauses> — NOT the whole ledger"
    whenever EITHER count was non-zero, so a listing over one local and one
    `origin_unknown` row — nothing withheld, both rows returned — told a script
    its array was incomplete and named `--all-projects` as the way to see rows
    it was already holding. A foreign row is ABSENT from the array and
    `--all-projects` is how you reach it; an unplaceable row is PRESENT, stamped
    `scope_class`, and a filter on that field is how you exclude it. So
    "withheld" is said only of rows this array does not contain, and the
    retained classes are NAMED so the reader can spell the filter.

    `classes` is the `scope_class` of each retained row (duplicates welcome —
    the count comes from its length), never the rows themselves: this producer
    must not be able to read a body."""
    said = []
    if withheld:
        said.append("%d row(s) in a repository another project claims are "
                    "WITHHELD from it, so it is NOT the whole ledger — "
                    "`--all-projects` widens the project axis" % withheld)
    if classes:
        # AND IT SAYS WHAT THE REGISTRY ACTUALLY ANSWERED (round three). "no
        # registered project places" is a claim about the world; this surface
        # measured a LOOKUP, which can come back empty because nobody
        # registered the repository, because its path would not resolve, or
        # because the registry could not be read at all — and `--json` has no
        # heading anywhere to qualify it. `_unplaceable_fact` is the one wording
        # the text clause reads too, so the two surfaces cannot drift apart.
        noun, why = _unplaceable_fact(project)
        said.append("%d row(s) %s are RETAINED in it, stamped `scope_class` "
                    "%s — "
                    "filter on that field to exclude them: %s"
                    % (len(classes), noun, " / ".join(sorted(set(classes))),
                       why))
    return said


def _hold_holder_nudge(row):
    """A source-clean hold hands the next move to the integrator — say so.

    THE THREE HOLDS ANSWER THE SAME QUESTION AND ONLY TWO OF THEM WOKE
    ANYBODY. `--owner-gated` reaches him through the owner-ask surfaces;
    a bare hold moves the next move to nobody new and correctly wakes
    nobody. `--source-clean` names the INTEGRATOR, and nothing told them:
    the reviewer finished, the row moved to a plate its holder could not
    see, and the holder learned about it when someone said so in chat.
    THE FAILURE MODE IT REMOVES: a row goes source-clean, its holder is
    never told, and the holder discovers it only when someone says so in
    chat — by which time a train car has been waiting on a row its owner
    did not know was theirs.

    IT NUDGES ONLY THE HOLD THAT MOVES THE PLATE. A bare hold gets no DM,
    because waking someone for a row that is still exactly where they left
    it is how a notifier earns the reputation that makes the next one
    ignored.

    IT IS CALLED FROM THE VERBS, NOT FROM `mark_hold`, which is the shape
    `_verdict_land_nudge` already uses and for the reason its own comment
    gives: `mark_hold`'s non-test callers are the hold verb and a
    fresh-context clean read's verdict (task/3658), both here, so the verbs
    cover every hold helm records, and the ledger writer stays a ledger
    writer. A delivery inside the write would put a chat transport
    between a lock and its release — the trap that surface has already
    measured once.

    Never raises and never blocks: `_nudge` swallows its own transport
    failures, and the hold is durable before this is reached at all."""
    tip = row.get("source_clean_tip")
    if not tip:
        return False
    lane = row.get("lane") or "?"
    reviewer = row.get("recipient") or "a reviewer"
    # WHAT IT IS WAITING ON, not merely that it waits. A holder told only
    # that a row is theirs goes to a second tool to find out why; the one
    # thing this hold means is that an approve needs a whole-suite token
    # this reviewer cannot mint, so the sentence says that.
    #
    # AND THE NEXT MOVE IS ONE THE HOLDER CAN MAKE (task/3053). "Gate it,
    # then the approve can bind" names a move nobody is positioned to make:
    # the reviewer is not woken after the gate, and the integrator is often
    # the lane's author. The land closes the row instead, on the hold plus
    # the gate, through the verb named here — and the gate is stated as what
    # the door checks: a verified whole-suite receipt on a commit containing
    # the tip, not necessarily the one the land pushed.
    rid12 = row["id"][:12]
    from . import landreq                    # DEFERRED — landreq imports us.
    rehold = landreq.source_clean_rehold(row)
    if rehold:
        # A HOLD ITS HOLDER RUNG REFUSES IS THE REVIEWER'S (task/3053: NO
        # HOLDER in the read's finding f; a stranger's or a lane author's
        # stamp in the author's ruling 3, round 4). It reaches here only
        # through an idempotent re-run of a hold written before the door bound
        # its holder, and no land can close it: the one move is the re-hold,
        # named in full by the one predicate, and the nudge goes to the seat
        # that owes it — never a close that refuses.
        return dispatches._nudge(
            row.get("recipient") or dispatches._default_lander(),
            "SOURCE-CLEAN HOLD on %s (%s): no land can close it on your clean "
            "read — %s." % (rid12, lane, rehold["why"]),
            "%s: SOURCE-CLEAN at %s — no land can close this hold; re-hold it"
            % (lane, str(tip)[:12]))
    # A CAR AUTO-LAND TAKES BY ITSELF WAKES NOBODY (task/4215). With the
    # switch ON, `helm train auto` composes, gates, lands and folds every car
    # its planner admits, so a DM naming the land as the integrator's move
    # cost a wake turn for a move nobody had to make (~15 a day, measured).
    # Such a hold is said once as a retained, non-waking row in auto-land's
    # room instead. When the switch is OFF the DM is today's; when it is ON
    # and the planner turns the car away, the DM names the planner's reason,
    # because then the integrator really must act.
    car, excluded = _auto_land_admission(row)
    if car:
        return _auto_land_notice(row, car)
    stays = (" AUTO-LAND IS ON BUT WILL NOT TAKE THIS CAR: %s." % excluded
             if excluded else "")
    return dispatches._nudge(
        dispatches._default_lander(),
        "SOURCE-CLEAN HOLD on %s (%s) — @%s read the delta and found nothing, "
        "and cannot mint an approve because one binds a whole-suite token only "
        "your land gate produces. The next move is YOURS: land %s under a "
        "whole-suite gate, then `helm lr foldcheck <head> --gate gate:<id> "
        "--apply`, naming a verified whole-suite receipt on a commit "
        "containing the tip, closes this row (--reason source-clean-landed) — "
        "no approve is needed.%s" % (rid12, lane, reviewer, str(tip)[:12],
                                     stays),
        "%s: SOURCE-CLEAN at %s — review complete, awaiting your whole-suite "
        "gate%s" % (lane, str(tip)[:12],
                    " (auto-land will not take it)" if excluded else ""))


def _auto_land_admission(row):
    """(car, excluded) for a source-clean hold, asked of auto-land and the
    train planner, never re-derived: `car` is the tip `helm train auto` will
    take by itself (the switch is ON and `landwindow.car_admission` admits
    it); `excluded` is the planner's reason it will not, with the switch ON.
    (None, None) when the switch is OFF or anything cannot be read: the hold
    then wakes its integrator exactly as before. Never raises."""
    try:
        from . import autoland, landwindow
        root, car, why = landwindow.car_admission(row["id"])
        if root is None or autoland.off_reason(root):
            return None, None
        return (car, None) if car else (None, why)
    except Exception:                                   # noqa: BLE001
        return None, None


def _auto_land_notice(row, tip):
    """Say a hold auto-land will land as a RETAINED, NON-WAKING row in
    auto-land's room -> False (no DM was sent). `ambient` is the chat class
    for a line a reader pulls and no seat is woken by. It is signed as every
    `dispatches` row is (machine_senders). Never raises."""
    from . import autoland, chat
    text = ("SOURCE-CLEAN HOLD on %s (%s) at %s: FYI, nothing to do. "
            "Auto-land is ON and its planner admits this car, so `helm train "
            "auto` composes, gates and lands it, and its fold closes the row "
            "source-clean-landed." % (row["id"][:12], row.get("lane") or "?",
                                      str(tip)[:12]))  # noqa: SILENT_CAP — short ids for the reader, as every hold line prints them
    try:
        chat.post(text, room=autoland.ROOM, who="dispatches", ambient=True)
    except Exception:                                   # noqa: BLE001
        pass                  # a notice must never block the hold it reports
    return False


def _rc(why):
    """Exit code for one refusal from the writer. 2 = USAGE, 1 = the operation
    failed.

    The RULE has one owner — `_resolve_chain` — and this maps its two USAGE
    refusals to the usage exit code by EXACT IDENTITY against the exported
    constants, never by sniffing the prose. Re-checking the flags here would be
    a second rejecter of the same input, which measures neither.

    Missing `--kind` already exits 2, so a missing work identity exiting 1 would
    make one required flag rc2 and the other rc1 for the same class of mistake.
    """
    return 2 if why in (dispatches.CHAIN_REQUIRED, dispatches.CHAIN_EXCLUSIVE) else 1


def list_scope_argv(rest):
    """(remaining, to_value, err) — lift the ONE `--to SEAT` pair out of `list`.

    `list` parses its options as a SET of bare flags, and a set cannot hold a
    value: without this, `list --to codex-3` reports BOTH tokens as unknown
    options (the shape `list --limit 10` already produces). Consuming the pair
    first keeps that grammar exactly as it was for every other flag.

    A `--to` with no value REFUSES. It must never degrade to the unfiltered
    list, for the same reason `--mine` refuses an unresolved identity: this
    verb exists so a seat can ask WHOSE rows these are, and a filter that
    silently does not filter answers that question wrongly and confidently.
    Two DIFFERENT `--to` values refuse too — one listing answers for one
    recipient, and picking either would be a guess about which one was meant.
    """
    out, seat, i = [], None, 0
    while i < len(rest):
        arg = rest[i]
        if arg != "--to":
            out.append(arg)
            i += 1
            continue
        if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
            return out, None, ("--to wants a seat name (e.g. `--to codex-3`); "
                               "it never means 'every seat'")
        if seat is not None and rest[i + 1] != seat:
            return out, None, ("--to given twice (%s and %s) — one listing "
                               "answers for ONE recipient"
                               % (seat, rest[i + 1]))
        seat = rest[i + 1]
        i += 2
    return out, seat, None


def retip_argv_ok(argv):
    """The retip arm's EXACT admission, one home for the grammar, for the same
    reason rebind's is (the meld rebind_argv_ok cites): a second partial
    grammar beside the parser drifts, and the one that drifts certifies shapes
    the parser refuses. Returns (ok, pos, opts, perr); the CLI arm consumes
    this same tuple."""
    rest = [a for a in argv if a not in ("--json",)]
    pos, opts, perr = dispatches._parse(rest, ("--ref", "--reason", "--repo"))
    ok = not perr and len(pos or ()) == 1 and bool((opts or {}).get("--ref"))
    return ok, pos, opts, perr


def send_names(verb):
    """The valued options `add` and `send` take. ONE SET, read by the door
    and by the corrected send line (task/3382 F3), so the two can never
    disagree about which word of an argv is an option."""
    names = {"--ref", "--note", "--deadline", "--repo", "--kind",
             "--supersedes", "--review-mode"}
    # THE TASK A NEW CHAIN SERVES (task/3643): recorded on its first row.
    names.add("--task")
    # --part is the new counterpart of --whole (task/3938): a chain naming a
    # task must pick exactly one path to that task's ask, and the one that
    # does not is the other flag, never both and never neither.
    names.add("--part")
    names.add("--posture-na")
    names.add("--read-only-because")
    names.add("--no-owner-surface-because")
    # THE REVIEW DOOR'S FLAGS (helm/review_door.py): a recorded reason to
    # skip the meld, the findings the author disputes, and the design
    # meld a first irreversible build row cites.
    names.update(("--async-because", "--disputes", "--meld"))
    # THE REPAIR WORK `--force` ADMITS past a broken seat (task/3546,
    # helm/seat_hold.py): recorded beside the seat's hold.
    names.add("--reason")
    # THE PATCHES A SUPERSEDING REF DECLINES BY NAME, and the recorded reason
    # (task/3288, dispatches._undelivered_patch).
    names.add(dispatches.DECLINE_PATCH_FLAG)
    # THE REASONS A FIRST ROW GOES AHEAD OF A BIGGER LEVER OR OF STARTED
    # WORK SITTING UNLANDED (task/3821).
    names.update(("--because", "--start-anyway"))
    if verb == "send":
        names.add("--key")
    return names


def _parse_send(rest, names=None, lenient=False):
    """Parse send flags without consuming an exact ``--force`` in prose.

    Value options keep `_parse`'s historical anywhere-in-argv grammar, and
    unknown options still refuse. Only ``--force`` is ambiguous with message
    prose: it becomes authority when it belongs to the complete trailing option
    block, while an earlier occurrence stays positional text. ``--new-work``
    keeps its pre-existing bare-flag behaviour. `lenient` is the corrected
    line's reading of a refused send (`dispatches._parse`), through this same
    partition: never a second grammar.
    """
    names = send_names("send") if names is None else names
    # --part (task/3938) is a bare flag beside --whole and --force; like them
    # it is authority over which path a chain's task takes and only the trailing
    # option block may carry it, not an earlier token in the prose.
    bare = {"--new-work", "--force", "--whole", "--part"}
    cut = len(rest)
    while cut:
        if rest[cut - 1] in bare:
            cut -= 1
            continue
        if cut >= 2 and rest[cut - 2] in names \
                and not rest[cut - 1].startswith("--"):
            cut -= 2
            continue
        break
    force_at = {i for i in range(cut, len(rest)) if rest[i] == "--force"}
    # `--whole` (task/3746) is authority over the task's close, so like
    # `--force` only the trailing option block may carry it; `--part` is the
    # new counterpart (task/3938) and obeys the same rule.
    whole_at = {i for i in range(cut, len(rest)) if rest[i] == "--whole"}
    part_at = {i for i in range(cut, len(rest)) if rest[i] == "--part"}
    flags = {"--new-work"} if "--new-work" in rest else set()
    if force_at:
        flags.add("--force")
    if whole_at:
        flags.add("--whole")
    if part_at:
        flags.add("--part")
    parsed = [a for i, a in enumerate(rest)
              if a != "--new-work" and i not in force_at | whole_at | part_at]
    pos, opts, err = dispatches._parse(parsed, names,
                                       positional_flags=("--force", "--whole",
                                                         "--part"),
                                       lenient=lenient)
    return pos, opts, flags, err


def _steer_lines(steer):
    """Print the two first-row checks' steer lines. The check marks a line
    said once per session, task and lever when it makes it, so this runs
    before a refusal returns: a refusal after the check must not swallow the
    one time the line is said."""
    for line in steer:
        print("helm dispatch: " + line, file=sys.stderr)


def _lever_lines(row):
    """Say what the two first-row checks recorded on a written row: that one
    could not run, or what the row went ahead of and the reason given; and
    put a reason the seat gave on the chosen task as one comment
    (`taskkey.choice_comment`; a row that took its reasons from its lane
    leaves that to the lane's claim). Their steer lines are `_steer_lines`'s."""
    from . import taskkey, tasks
    for unknown, mark in (("lever_unknown", tasks.LEVER_UNKNOWN_MARK),
                          ("finish_unknown", taskkey.FINISH_UNKNOWN_MARK)):
        if row.get(unknown):
            print("helm dispatch: %s: %s — the check did not run, so this "
                  "went ahead" % (mark, row[unknown]), file=sys.stderr)
    for over, reason in (("lever_skipped", "lever_because"),
                         ("start_anyway_over", "start_anyway")):
        if row.get(over) and row.get(reason) != tasks.NO_REASON:
            print('helm dispatch: %s went ahead of %s: "%s" (recorded on the '
                  'row%s)' % (row.get("task") or "this row",
                              tasks.named(row[over]), row.get(reason) or "",
                              ", from " + row["reasons_from"]
                              if row.get("reasons_from") else ""),
                  file=sys.stderr)
    if row.get("reasons_from"):
        return
    _written, why = taskkey.choice_comment(
        row.get("task"), "dispatch %s" % str(row.get("id") or "")[:12],
        lever=(row["lever_skipped"], row.get("lever_because"))
        if row.get("lever_skipped") else None,
        start=(row["start_anyway_over"], row.get("start_anyway"))
        if row.get("start_anyway_over") else None, by=row.get("sender"))
    if why:
        print("helm dispatch: WARNING: the row records the reason, but it was "
              "NOT added to %s as a comment: %s" % (row.get("task"), why),
              file=sys.stderr)


def _findings_lines(row):
    """The qwen27 findings note's lines for one row, or [] when it carries
    none. A renderer failure prints one line saying so rather than breaking
    the verb that asked, because the note is reading material and never the
    verb's own answer."""
    try:
        from . import findingspass
        return findingspass.note_lines(row)
    except Exception as exc:                          # noqa: BLE001
        return ["findings pass: the note could not be rendered (%s)"
                % type(exc).__name__]


def _review_door(verb, kind, target, pos, opts):
    """The review door's plan for this send/add (helm/review_door.py).

    FAIL-OPEN except where the door itself refuses (T0): an unreadable ledger,
    an unresolvable tip or an unknown author opens no meld and blocks
    nothing, because the row is the universal transport."""
    from . import review_door
    empty = {"trigger": None, "action": None, "lines": [], "brief": None,
             "row": {}, "invite": None, "refuse": None,
             "pair": {"room": None, "topic": None}}
    try:
        sender, why = dispatches._acting_author()
        info = dispatches._repo_info(opts.get("--repo"))
        if why or not info:
            return empty
        tip = (dispatches._resolve_tip(info["repo"], opts["--ref"])
               or ("", None))[0] or ""
        disputes = [d.strip() for d in
                    str(opts.get("--disputes") or "").split(";") if d.strip()]
        # A FIRST ROW'S PAIR MELD CAN BE NAMED BEFORE THE ROW EXISTS only
        # when its lane or note names the task: the chain root it would
        # otherwise be keyed on is this row's own id, minted at the write.
        hint = None
        if not opts.get("--supersedes"):
            room, key = review_door.pair_room(
                {"lane": pos[1], "note": opts.get("--note"),
                 "repo_id": info["repo_id"]})
            hint = room if str(key or "").startswith("task/") else None
        return review_door.plan(
            kind, sender, target, pos[1], tip,
            supersedes=opts.get("--supersedes"),
            body=" ".join(pos[2:]) if verb == "send" else opts.get("--note"),
            async_because=opts.get("--async-because"), disputes=disputes,
            cited_meld=opts.get("--meld"), beacon=review_door.live_beacon,
            pair_room_hint=hint)
    except Exception as exc:                          # noqa: BLE001
        empty["lines"].append("helm dispatch: the review door could not "
                              "read this chain (%s); no meld was opened"
                              % type(exc).__name__)
        return empty


def _record_forced(target, row, forced, reason):
    """Record a `--force` past a broken seat's facts once its row exists
    (task/3546), and say so. A log that will not take the record is said
    loudly: the row stands, its admission is unrecorded."""
    if not forced:
        return
    from . import seat_hold
    _event, err = seat_hold.record_forced(target, row["id"], forced, reason)
    print("helm dispatch: FORCED past %s on @%s for the repair work: %s — %s"
          % ("+".join(f["fact"] for f in forced), target, reason,
             "recorded (`helm seat hold` lists it)" if not err
             else "NOT RECORDED: " + err), file=sys.stderr)


def _door_after(door, row):
    """After the row exists: say what the door decided, and name the pair
    meld round the write opened. The row is already written, so nothing here
    can undo it; a round that could not open leaves the row (which carries
    the BAR when T2 fired) as the conversation.

    T2 OPENS NO ROOM OF ITS OWN any more: the bar is the topic of the round
    this send opened in the chain's pair meld (P2), so one chain has one
    room whatever the door decides."""
    for line in door["lines"]:
        print(line)
    pair = (row or {}).get(dispatches._PAIR_MELD) or {}
    for line in pair.get("lines") or ():
        print("helm dispatch: " + line)
    if not door["invite"]:
        return
    peer, _topic = door["invite"]
    if not pair.get("room"):
        print("helm dispatch: T2 could not open the meld (%s); row %s carries "
              "the BAR and is the conversation"
              % (pair.get("error") or "no pair meld round", row["id"][:12]))
        return
    print("helm dispatch: T2 — round %s of chain %s: MELD OPENED %s with @%s "
          "to agree the BAR, as this round of the task's pair meld (the row "
          "is the async fallback)."
          % (door["row"]["meld_door"].get("rounds"),
             door["row"]["meld_door"].get("chain"), pair["room"], peer))


def _cmd_melds(rest):
    """`helm dispatch melds [--hours N] [--json]` — the review door's two
    falsifiers, measured read-only (docs/MELD_REVIEW_DOOR.md)."""
    import json as _json
    from . import review_door
    hours = 168
    flags = set()
    at = 0
    while at < len(rest):
        flag = rest[at]
        if flag in flags or flag not in ("--hours", "--json"):
            print("helm dispatch melds: unknown or repeated argument %s" % flag,
                  file=sys.stderr)
            return 2
        flags.add(flag)
        if flag == "--hours":
            try:
                hours = int(rest[at + 1])
            except (IndexError, ValueError):
                print("helm dispatch melds: --hours wants a positive whole number",
                      file=sys.stderr)
                return 2
            if hours < 1:
                print("helm dispatch melds: --hours wants a positive whole number",
                      file=sys.stderr)
                return 2
            at += 1
        at += 1
    got = review_door.census(hours=hours)
    if "--json" in flags:
        print(_json.dumps(got, indent=2, sort_keys=True))
        return 0 if "error" not in got else 1
    if "error" in got:
        print("helm dispatch melds: " + got["error"], file=sys.stderr)
        return 1
    t2, t1, retro = got["t2"], got["t1"], got["retro"]
    print("review door falsifiers, last %dh:" % got["window_h"])
    print("  (a) T2 join rate within %ds: %s/%s measured (%d door-opened "
          "rows, %d rooms unreadable) — %s" % (
              t2["entry_window_s"], t2["joined_in_window"],
              t2["rooms_measured"], t2["opened_rows"], t2["unreadable"],
              t2["reading"]))
    print("  (b) T1 after an all-mechanical patch FIX: %d of %d firings — %s"
          % (t1["on_all_mechanical"], t1["fired"], t1["reading"]))
    prior = got["prior"]
    print("  prior for (a): %s of %s melds convened in the window had the "
          "reader join within the entry window (%d unreadable)" % (
              prior["joined_in_window"], prior["convened"],
              prior["unreadable"]))
    print("  replay: %d continuing review sends; T1 silent after %d "
          "all-mechanical FIX, would offer after %d design-class FIX, %d "
          "continued no FIX" % (
              retro["continuing_sends"],
              retro["after_all_mechanical_fix_t1_silent"],
              retro["after_design_class_fix_t1_offers"],
              retro["after_no_fix"]))
    pair = got["pair"]
    cost = pair["cost"]
    print("pair meld, last %dh (docs/MELD_REVIEW_DOOR.md, The pair meld):"
          % got["window_h"])
    print("  (a) tasks whose pair meld reached AGREED on a row: %d of %d "
          "(bar %d%% over %d tasks) — %s; pairings %s" % (
              pair["agreed"], pair["tasks"], int(pair["floor"] * 100),
              pair["bar_tasks"], pair["reading"],
              ", ".join("%s=%d" % kv for kv in sorted(pair["pairings"].items()))
              or "none"))
    keys = pair["keys"]
    print("  keys: %d by task, %d by the chain-root fallback (%d named no "
          "task, %d named two or more): the fallback count is what the "
          "task/N parse missed" % (
              keys["task"], keys["chain_fallback"], keys["no_task"],
              keys["two_tasks"]))
    print("  (g) bytes per converged chain: pair meld median %s over %d; rows "
          "alone median %s over %d" % (
              cost["pair_median_bytes"], cost["pair_converged"],
              cost["row_only_median_bytes"], cost["row_only_converged"]))
    trial = got["mode_ab"]
    print("review mode A/B, last %dh: %d enrolled chain(s)" % (
        got["window_h"], len(trial["chains"])))
    for row in trial["chains"]:
        elapsed = ("%ds" % row["send_to_hold_s"]
                   if isinstance(row["send_to_hold_s"], int) else "UNKNOWN")
        print("  %s %s: enrolled round %s; %s active read round(s); %s cure "
              "cycle(s); first send->hold %s; reviewer tokens %s; author "
              "tokens %s" % (
                  row["chain"][:12], row["mode"], row["enrollment_round"],
                  row["active_review_rounds"], row["cure_cycles"], elapsed,
                  row["reviewer_tokens"], row["author_tokens"]))
    if not trial["chains"]:
        print("  no PATCH/MELD-DIFF chain enrolled in this window")
    print("  tokens: " + trial["token_reading"])
    return 0


def _meld_said_back(row):
    """One line naming the meld a verdict or hold recorded, and whether it
    switches the spiral rung off (only an AGREED meld both sides spoke in)."""
    if not row.get("meld_room"):
        return
    outcome = str(row.get("meld_outcome") or "?").upper()
    print("helm dispatch: meld %s — %s%s" % (
        row["meld_room"], outcome,
        " (the spiral rung reads this chain as melded)" if outcome == "AGREED"
        else " (not agreed: the spiral rung still counts this chain)"))


def _chain_note(row):
    """SAY WHAT WORK THIS ROW JOINED, at the moment it is minted.

    The writer is the only person who can catch a wrong `--supersedes`, and they
    can only catch it if the surface tells them which chain they just extended.
    A relation nobody is shown is a relation nobody corrects."""
    root = str(row.get("chain_root") or "")[:12] or "-"
    parent = str(row.get("supersedes") or "")[:12]
    if parent:
        return "chain %s — CONTINUES %s" % (root, parent)
    return "chain %s — NEW WORK (roots its own chain)" % root


#: The class a `collisions` line opens with. A LIVE row is open or held and
#: carried by no successor; every other row has ended, and its dropped event
#: is HISTORY. `helm doctor` WARNs on the first and counts the second.
COLLISION_LIVE, COLLISION_HISTORY = "LIVE", "HISTORY"


def _collisions(rest):
    """`dispatch collisions [--json]` — every event the fold dropped because
    it reused a seq, live rows first.

    THE DRILLDOWN THE DOCTOR ROW NAMES. The doctor names each dropped event on
    a live row and only COUNTS the ones on rows that have ended, so this verb
    is where the counted ones are listed, one line each. It prints every
    collision `dispatches.seq_collisions` returns and splits on the same
    `ended` field the doctor row splits on, so its HISTORY lines are the
    doctor's count. `--json` is that list whole, `ended` and `carried_by`
    included."""
    unknown = [a for a in rest if a != "--json"]
    if unknown:
        print("helm dispatch: collisions accepts only [--json]: %s"
              % " ".join(unknown), file=sys.stderr)
        return 2
    found, unavailable = dispatches.seq_collisions()
    if unavailable:
        print("helm dispatch: ledger unavailable; the seq-collision census is "
              "UNKNOWN: %s" % unavailable, file=sys.stderr)
        return 1
    if "--json" in rest:
        print(json.dumps(found, indent=2, sort_keys=True))
        return 0
    if not found:
        print("dispatch seq collisions: none — every event the fold declined "
              "carries a seq no applied event holds")
        return 0
    live = [c for c in found if not c.get("ended")]
    history = [c for c in found if c.get("ended")]
    # THE DENOMINATOR FIRST, then each class under its own word, so a reader
    # of either half knows the size of the other.
    print("dispatch seq collisions: %d dropped event%s — %d %s (the row is "
          "open or held and no successor carries it), %d %s (the row has "
          "ended: closed, retired or superseded)"
          % (len(found), "" if len(found) == 1 else "s", len(live),
             COLLISION_LIVE, len(history), COLLISION_HISTORY))
    label = dispatches._event_kind_label
    for word, group in ((COLLISION_LIVE, live), (COLLISION_HISTORY, history)):
        for c in group:
            ended = ""
            if c.get("ended"):
                ended = "  ended: %s" % label(c["ended"])
                if c.get("carried_by"):
                    ended += ", carried by %s" % label(c["carried_by"])[:12]
            print("%-7s  %s  %s seq %d, reusing %s's  at %s  (ledger position "
                  "%d)%s" % (word, label(c.get("id") or "?"),
                             label(c.get("event") or "?"), c["seq"],
                             label(c.get("reused") or "?"),
                             label(c.get("ts") or "?"), c["position"], ended))
    return 0


_RETRACT_USAGE = ("usage: helm dispatch retract <id-or-unique-prefix> --reason R "
                  "--reads source-clean|fix|supersede|unknown "
                  "--measured|--inferred [--reissue|--successor ID] [--json]  "
                  "(withdraw a wrong verdict's authority without rewriting "
                  "it; the verdict's author seat, the integrator or the owner "
                  "only)")


def _cmd_retract(rest):
    """`helm dispatch retract` (task/3060): parse, call `dispatches.retract`,
    and say what now carries the review."""
    valued = {"--reason": None, "--reads": None, "--successor": None}
    bare = {"--measured", "--inferred", "--reissue", "--json"}
    pos, seen = [], set()
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-h", "--help"):
            print(_RETRACT_USAGE)
            return 0
        if arg in valued:
            if i + 1 >= len(rest) or rest[i + 1].startswith("--"):
                print("helm dispatch retract: %s wants a value" % arg,
                      file=sys.stderr)
                return 2
            valued[arg] = rest[i + 1]
            i += 2
            continue
        if arg in bare:
            seen.add(arg)
        elif arg.startswith("--"):
            print("helm dispatch retract: unknown option %s (%s)"
                  % (arg, _RETRACT_USAGE), file=sys.stderr)
            return 2
        else:
            pos.append(arg)
        i += 1
    if len(pos) != 1:
        print(_RETRACT_USAGE, file=sys.stderr)
        return 2
    for flag, what in (("--reason", "why the verdict was wrong"),
                       ("--reads", "what the review now reads")):
        if not valued[flag]:
            print("helm dispatch retract: %s is required (%s)" % (flag, what),
                  file=sys.stderr)
            return 2
    bases = sorted(seen & {"--measured", "--inferred"})
    if len(bases) != 1:
        print("helm dispatch retract: declare exactly one basis, --measured or "
              "--inferred (how you know the verdict was wrong)",
              file=sys.stderr)
        return 2
    if "--reissue" in seen and valued["--successor"]:
        print("helm dispatch retract: --reissue mints the successor and "
              "--successor names an existing one: pass one", file=sys.stderr)
        return 2
    row, why = dispatches.retract(
        pos[0], valued["--reason"], valued["--reads"], bases[0][2:],
        reissue="--reissue" in seen, successor=valued["--successor"])
    if why:
        print("helm dispatch: " + why, file=sys.stderr)
        return 1
    if "--json" in seen:
        print(json.dumps(row, sort_keys=True, default=str))
        return 0
    successor = str(row.get("retract_successor") or "")
    print("helm dispatch: %s — VERDICT %s RETRACTED by @%s (%s); reads %s (%s)"
          % (row["id"], str(row.get("retracted_polarity") or "?").upper(),
             row.get("retract_seat") or "?", row.get("retract_role") or "?",
             str(row.get("retract_reads") or "?").upper(),
             row.get("retract_basis") or "?"))
    for line in dispatches.review_findings.closed_lines(row):
        print("helm dispatch: " + line)
    if successor:
        print("helm dispatch: successor %s carries the review (@%s)"
              % (successor, row.get("recipient") or "?"))
        if row.get("retract_reads") == "source-clean":
            # THE CLAIM HAS A HOME. A source-clean reading waits on the
            # integrator's land gate, and the hold is where the integrator's
            # own listing finds it.
            print("helm dispatch: to record the clean read where the "
                  "integrator finds it: helm dispatch hold %s --source-clean "
                  "%s <reason>" % (successor[:12], row.get("reviewed_tip")
                                   or "<tip>"))
    else:
        print("helm dispatch: no successor carries the review; re-request it "
              "with `helm dispatch send %s %s --ref %s --kind %s --supersedes "
              "%s` (body on stdin)"
              % (row.get("recipient") or "<reviewer>",
                 row.get("lane") or "<lane>", row.get("tip") or "<tip>",
                 row.get("kind") or "review", row["id"][:12]))
    return 0


def _uncarried_line(outcome, would=False):
    """The sentence EVERY cancel prints: what it leaves carried by nothing.

    A ZERO IS PRINTED TOO, because the line is the answer to a question the
    operator could not otherwise ask, and a silent cancel read the same
    whether it stranded nothing or seven rounds (task/3357)."""
    opened = list(outcome.get("uncarried_open") or ())
    fixes = list(outcome.get("uncarried_fix") or ())
    chained = bool(outcome.get("chained"))

    def count(ids, noun, tail=""):
        name = noun if len(ids) == 1 else noun + "s"
        if not ids:
            return "0 %s" % name
        return "%d %s (%s%s)" % (len(ids), name,
                                 ", ".join(i[:12] for i in ids), tail)

    line = "this cancel %s %s and %s" % (
        "would un-carry" if would else "un-carries",
        count(opened, "open round", (" — would be cancelled by --chain"
                                     if would else " — cancelled by --chain")
              if chained else ""), count(fixes, "FIX verdict"))
    if opened and not chained:
        if would:
            line += (". Nothing would carry those rounds then, so idle-dispatch "
                     "would read each STRANDED and nag its sender: re-run with "
                     "`--chain` and without --dry-run to cancel them too, with "
                     "this reason")
        else:
            line += (". Nothing carries those rounds now, so idle-dispatch "
                     "reads each STRANDED and nags its sender: `--chain` also "
                     "cancels them with this reason, and re-running this cancel "
                     "with it does that now")
    if fixes:
        line += ((". `helm owed` would bill each FIX again" if would else
                  ". `helm owed` bills each FIX again")
                 + "; a FIX is answered by its landed cure or a successor "
                 "dispatch (--supersedes), never by a cancel")
    return line + "."


def _cmd_dispatch(args):
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(dispatches.USAGE, file=sys.stderr)
        return 2
    from . import review_done
    review_done._stdin_brief = None
    verb, rest = args[0], args[1:]
    if verb in TRIAGE_ALIASES:
        # A ROW, NAMED: the bulk re-measure behind a bare `triage` is not
        # what a seat typing `show` asked for, so an alias with no id is a
        # usage error rather than the whole board.
        if not [a for a in rest if not a.startswith("--")]:
            print(synopsis(verb), file=sys.stderr)
            return 2
        verb = "triage"
    if verb == "mix":
        return dispatches.cmd_mix(rest)
    if verb == "collisions":
        return _collisions(rest)
    if verb in ("add", "send"):
        names = send_names(verb)
        # `send` owns a prose tail. An exact `--force` inside that prose must
        # not silently become authority to mint a fork, so only its complete
        # trailing option block is parsed as flags. `add` has no prose and can
        # remove bare flags directly before the value-option parser.
        if verb == "send":
            pos, opts, flags, err = _parse_send(rest, names)
            new_work = "--new-work" in flags
            force = "--force" in flags
            whole = "--whole" in flags
            part = "--part" in flags
        else:
            new_work = "--new-work" in rest
            force = "--force" in rest
            whole = "--whole" in rest
            part = "--part" in rest
            rest = [a for a in rest
                    if a not in ("--new-work", "--force", "--whole",
                                  "--part")]
            pos, opts, err = dispatches._parse(rest, names)
        # THE ASK'S PATH (task/3938): a chain that names a task picks EXACTLY
        # ONE of --whole / --part — the whole ask (its land leaves the task
        # owing a seen-working check, helm/observed.py)
        # or a piece of it. Neither says which path is meant, both do, so a
        # chain with --task and no flag is refused, as is one with both. The
        # refusal names the two flags and the exact retry, and it is the door
        # every seat meets at `work claim` and `dispatch send` alike.
        # AN ORPHAN --part (a path with no task) is refused at the send door
        # too: --part names the path to the task that --task names, and
        # nothing downstream records it, so without --task it picks a path
        # for no ask. An orphan --whole is the row writer's to judge (below).
        # A parse error leaves opts None, so both path gates (which read opts)
        # run only after a successful parse; on error the usage gate below
        # owns the reply. `not err` first: _parse returns pos=None and opts
        # None on an unknown option or a value-less one.
        if not err:
            # --part ONLY: an orphan --whole reaches the row writer, which
            # resolves the task from the lane's record and refuses only when
            # there is none (`_base`'s "WHOLE ask" sentence).
            if part and not opts.get("--task"):
                print("helm dispatch %s: REFUSED — --part names the path to "
                      "the task that --task names; give --task task/N with it"
                      % verb, file=sys.stderr)
                return 1
            if opts.get("--task") and not (whole ^ part):
                state = ("both --whole and --part were given"
                         if (whole and part)
                         else "neither --whole nor --part was given")
                print("helm dispatch %s: REFUSED — --task names a task's ask, and "
                      "it must pick one path to it. %s. add --whole if this "
                      "chain finishes the task, or --part if it does a piece of "
                      "it; give exactly one of --whole or --part"
                      % (verb, state), file=sys.stderr)
                return 1
        # STDIN BODY — literal when piped or fed by a quoted-delimiter heredoc,
        # times in one evening across two different model families.
        #
        # A dispatch body is PROSE, and prose contains backticks. Passed as argv
        # inside a double-quoted shell string, bash performs COMMAND SUBSTITUTION
        # on them: a message explaining `kind` silently became a message with
        # `kind` executed and its (empty) output spliced in, plus a stray
        # "kind: command not found" on stderr. Observed on four of my own
        # dispatches and once on another seat's, whose reply carried its own
        # "disregard its mangled command examples".
        #
        # It is not only mangling — it is arbitrary command execution driven by
        # the CONTENT of a message. `helm chat post` already solved this exactly
        # here, with a stdin path; the asymmetry was that `dispatch send` never
        # grew the same door. A QUOTED heredoc delimiter costs the caller nothing
        # and cannot expand; an unquoted one still performs shell substitution.
        # `not err` FIRST: _parse returns pos=None on an unknown option or a
        # value-less one, so len(pos) tracebacks before the error check two lines
        # below ever runs. I inserted this branch ABOVE that check and turned
        # three clean usage errors into a TypeError — `--bogus`, a value-less
        # `--ref`, and a literal `--` all crashed. Live probes found it; the
        # ordering was the whole defect.
        #
        # AND NON-TTY IS NOT THE SAME AS HAS-A-BODY. A bare `sys.stdin.read()`
        # here BLOCKS FOREVER on any fd that never reaches EOF, and under an
        # agent harness stdin is exactly that: a live UNIX socket with no
        # writer closing it. The verb then prints NOTHING, creates NO row, and
        # sits reading like a slow queue — a socket fd parked in
        # `unix_stream_data_wait`, indistinguishable from contention, whose
        # only symptom to a caller is a row that never appears.
        #
        # THE DISCRIMINATOR IS CONTENT, NEVER MERE NON-TTY-NESS, and it is not
        # invented here: `chat.py`'s body door already carries that law and its
        # bounded `select`, written for the sibling defect where a positional
        # body silently discarded a piped one. Its accepted terms apply
        # unchanged — a heredoc, a file and any producer ready at fork answer
        # the select at once, `/dev/null` answers READY and reads empty, and a
        # pipe whose first byte lands after the window is read as no-stdin.
        # A socket with no data answers NOT-READY, so this falls through to the
        # usage error, which is loud, instant and true.
        #
        # THE READ AFTER THE SELECT IS STILL BLOCKING, deliberately: once one
        # byte is ready the producer exists, and a bounded read would truncate
        # a body being written slowly. The window guards ENTRY to the read, not
        # the read itself.
        if verb == "send" and not err and pos and len(pos) >= 2 \
                and not sys.stdin.isatty():
            if len(pos) == 2:
                if dispatches._stdin_has_a_body_fd(sys.stdin):
                    piped = sys.stdin.read().strip()
                    if piped:
                        pos = list(pos) + [piped]
                        # The brief is in no argv word: the corrected line
                        # re-pipes it and can ask the answers its text owes.
                        review_done._stdin_brief = piped
            elif dispatches._stdin_has_a_body_fd(sys.stdin, unselectable=False) \
                    and sys.stdin.read(1):
                # TWO BODIES, ONE MESSAGE: REFUSE RATHER THAN CHOOSE, the law
                # chat.resolve_one_body holds (task/3510). A message given AND
                # a body waiting on stdin means one of them would silently
                # never leave the caller's shell. One byte is enough to know a
                # body is there, and only a MEASURED fd is peeked: a stream
                # with no descriptor is never read, so a message beside it is
                # sent as before.
                print("helm dispatch send: REFUSING — both a positional "
                      "message and piped/heredoc stdin are present, and "
                      "choosing either silently discards the other. Send ONE "
                      "body: drop the positional message to use stdin, or "
                      "close stdin to use the message.", file=sys.stderr)
                return 2
        if err or len(pos) < (3 if verb == "send" else 2):
            print("helm dispatch: " + (err or synopsis(verb)), file=sys.stderr)
            return 2
        if not opts.get("--ref"):
            print("helm dispatch: %s requires --ref TIP" % verb, file=sys.stderr)
            return 2
        # An ABSENT --deadline stays None all the way to `_base3`, which is the
        # only place that knows the row's KIND. Substituting the review default
        # here is what put 2700s on every build row ever dispatched from the
        # CLI: the flag was optional, so the omission was silent and universal.
        deadline = opts.get("--deadline")
        if deadline is not None:
            deadline, err = dispatches._deadline(deadline)
            if err:
                print("helm dispatch: " + err, file=sys.stderr)
                return 2
        kind, err = dispatches.clean_kind(opts.get("--kind"))
        if err:
            print("helm dispatch: " + err, file=sys.stderr)
            return 2
        # REQUIRED AT THE OWNER LAYER, optional in the library — and the split
        # is the whole point. `clean_kind(None)` must stay legal because every
        # row written before this field existed carries no kind, and the report
        # has to show those as UNKNOWN rather than invent one.
        #
        # But an OPTIONAL flag makes the alarm VACUOUS, which is the finding:
        # the inverted night this feature exists to catch reproduces exactly by
        # omitting --kind, and `mix_alarm` stays deliberately silent on an
        # all-UNKNOWN window. A guard that any caller disarms by not typing a
        # flag is not a guard. New rows must declare; history stays honest.
        if kind is None:
            print("helm dispatch: %s requires --kind build|review — an "
                  "unrecorded kind makes the capacity alarm silent, which is "
                  "the exact failure it exists to catch" % verb,
                  file=sys.stderr)
            return 2
        # THE POSTURE GUARD (task/1346, helm/posture.py) lives in send() and
        # add() — the invariant, not this door; the CLI forwards
        # --posture-na and renders the refusal at exit 2. It is checked here
        # too, BEFORE the recipient and ref resolve, because it is the
        # cheapest refusal and the one the sender can answer without the
        # fleet — the same function, never a second predicate.
        from . import posture
        refused = posture.check(
            "helm dispatch " + verb,
            (" ".join(pos[2:]) if verb == "send" else "") + "\n"
            + str(opts.get("--note") or ""),
            posture_na=opts.get("--posture-na"))
        if refused:
            print(refused, file=sys.stderr)
            return 2
        # THE READ-ONLY GUARD AT THE SAME DOOR AND FOR THE SAME REASON: the
        # cheapest refusal, answerable by the sender alone, and the same
        # function the invariant runs rather than a second predicate.
        refused = dispatches.check_read_only(
            "helm dispatch " + verb,
            (" ".join(pos[2:]) if verb == "send" else "") + "\n"
            + str(opts.get("--note") or ""),
            kind=kind, because=opts.get("--read-only-because"))
        if refused:
            print(refused, file=sys.stderr)
            return 2
        review_mode = opts.get("--review-mode")
        if review_mode is not None:
            clean_mode = review_mode.strip().upper()
            if clean_mode not in ("PATCH", "MELD-DIFF"):
                print("helm dispatch: --review-mode must be PATCH or MELD-DIFF (got %s)"
                      % review_mode, file=sys.stderr)
                return 2
            if kind != "review":
                print("helm dispatch: --review-mode requires --kind review", file=sys.stderr)
                return 2
            review_mode = clean_mode
        for w in dispatches._ref_sanity(opts["--ref"], pos[1], opts.get("--repo"),
                             kind=kind):
            print("helm dispatch: " + w, file=sys.stderr)
        cap, unroutable = dispatches._recipient_gate(pos[0], force, verb)
        if unroutable:
            print("helm dispatch: " + unroutable, file=sys.stderr)
            return 2
        target, target_err = dispatches._canonical_recipient(cap["canonical"])
        if target_err:
            print("helm dispatch: " + target_err, file=sys.stderr)
            return 1
        # THE BROKEN-SEAT DOOR'S FORCE LEG (task/3546): the library rung
        # refuses a held or storming seat; `--force` past it, or past a walled
        # family, must say which repair work it admits, and is recorded once
        # the row exists.
        from . import seat_hold
        forced, refused = seat_hold.force_leg(target, force, opts.get("--reason"))
        if refused:
            print("helm dispatch: " + refused, file=sys.stderr)
            return 2
        door = _review_door(verb, kind, target, pos, opts)
        if door["refuse"]:
            print(door["refuse"], file=sys.stderr)
            return 2
        if door["brief"] and verb == "send":
            body = " ".join(pos[2:])
            joined = body + "\n\n" + door["brief"]
            if len(joined) <= dispatches.MESSAGE_ARG_CAP:
                pos = list(pos[:2]) + [joined]
            else:
                door["lines"].append(
                    "helm dispatch: the BAR did not fit in this brief; send "
                    "it to the reader yourself:\n" + door["brief"])
        # THE LINES THE FIRST-ROW CHECKS SAY (task/3821): they steer and
        # never refuse, and each is said once per session, task and lever.
        steer = []
        if verb == "send":
            # THE CANONICAL FROM THE CAPABILITY, never raw argv. The gate
            # proved membership for cap["canonical"]; handing the writer
            # pos[0] made it RESOLVE AGAIN, so the row could store a key
            # the proof was never about — #116's own unowned-row class,
            # recreated inside #116's fix. One resolution, one identity.
            row, why, sent = dispatches.send(target, pos[1], " ".join(pos[2:]),
                                  opts["--ref"], note=opts.get("--note"),
                                  deadline_s=deadline, key=opts.get("--key"),
                                  repo=opts.get("--repo"), kind=kind,
                                  new_work=new_work,
                                  supersedes=opts.get("--supersedes"),
                                  force=force,
                                  posture_na=opts.get("--posture-na"),
                                  read_only_because=opts.get(
                                      "--read-only-because"),
                                  owner_surface_because=opts.get(
                                      "--no-owner-surface-because"),
                                  door=door["row"], pair_meld=door["pair"],
                                  decline_patch=opts.get(
                                      dispatches.DECLINE_PATCH_FLAG),
                                  task=opts.get("--task"),
                                  lever_because=opts.get("--because"),
                                  start_anyway=opts.get("--start-anyway"),
                                  weigh=True, steer=steer, whole=whole,
                                  review_mode=review_mode)
            _steer_lines(steer)
            if row is None:
                print("helm dispatch: " + why, file=sys.stderr)
                return _rc(why)
            _lever_lines(row)
            for note in row.get(dispatches._ADMISSION_NOTES, ()):
                print("helm dispatch: RECIPIENT: " + note, file=sys.stderr)
            for warning in row.get(dispatches._WRITE_WARNINGS, ()):
                print("helm dispatch: WARNING: " + warning, file=sys.stderr)
            _record_forced(target, row, forced, opts.get("--reason"))
            if why:
                print("helm dispatch: " + why, file=sys.stderr)
                return 1
            # KEYED ON THE MEMBERSHIP ENUM, never on truthiness. This comment
            # used to describe a `joined is True` boolean-or-None and outlived
            # it — the exact stale-comment hazard this lane has been punished
            # for twice, so it is rewritten rather than left to read as
            # authoritative. UNKNOWN is its own arm and can never fall through
            # to a delivery claim.
            #
            # SAYS "NO RECIPIENT", NOT "NOT DELIVERED", AND THE DIFFERENCE IS
            # DELIBERATE. The ledger's own `delivered` event means the mention
            # or DM row was PUBLISHED — add()'s path states it outright: "THE
            # MENTION IS THIS PATH'S DELIVERY". A forced send to a pre-join
            # address genuinely does publish, so it genuinely does mark
            # delivered, and a line here reading NOT DELIVERED would contradict
            # `dispatch list` using the ledger's own vocabulary for a different
            # question. This line answers the question we actually measured —
            # whether any seat holds that address — and leaves "was it
            # published" to the field that owns it.
            # THREE ARMS FOR A THREE-STATE VALUE. This was `is False` / else,
            # which is a TWO-way branch — so joined=None fell to the else and
            # printed "(delivery observed)", the exact thing the comment above
            # forbids. The comment was right and the code below it was wrong,
            # which is the worst combination: a reader checks the invariant,
            # finds it stated correctly, and stops looking. Enumerate the arms
            # so a tri-state cannot collapse into a binary again.
            if cap["membership"] == "JOINED":
                tail = " (delivery observed)" if sent else ""
            elif cap["membership"] == "ABSENT":
                tail = " (NO RECIPIENT — @%s holds no roster row)" % dispatches._recipient_label(row)
            else:
                tail = (" (recipient membership UNKNOWN — %s, so delivery is"
                        " unproven)"
                        % ("the roster could not be read"
                           if cap["evidence"] == "read-failed"
                           else "no seat has joined this box yet"))
            print("helm dispatch: %s @%s %s — %s%s" % (
                row["id"], dispatches._recipient_label(row), row["lane"],
                dispatches._label(row), tail))
            # THE SIZE, ON EVERY SEND, NOT ONLY ON A BIG ONE. What was
            # invisible was not that a brief was large — it was that the door
            # had an opinion about size at all. A line that appears only past
            # the cap teaches nobody what the cap is until the day it bites;
            # printed always, the number is the sender's own feedback loop and
            # the cut this lane cured would have been visible the first night.
            #
            # READ OFF THE ROW, never recomputed from argv: a reconciled retry
            # returns the EXISTING row, and a line measuring what this
            # invocation typed would describe a brief that was never stored.
            nbytes = row.get("brief_bytes")
            if isinstance(nbytes, int):
                kept = dispatches._cut_kept_bytes(row.get("body"))
                kept_str = "%d" % kept if isinstance(kept, int) else "fewer than %d" % dispatches.MESSAGE_BODY_CAP
                print("helm dispatch: brief %d bytes, stored whole by "
                      "reference%s" % (
                          nbytes,
                          " (%d over the %d-byte row cap; the recipient sees "
                          "the first %s bytes unless it follows the reference)"
                          % (nbytes - dispatches.MESSAGE_BODY_CAP,
                             dispatches.MESSAGE_BODY_CAP, kept_str)
                          if nbytes > dispatches.MESSAGE_BODY_CAP else ""))
            else:
                # NO REFERENCE MEANS A ROW FROM BEFORE THIS STORE — a
                # reconciled retry against an operation minted by an older
                # binary. Say what is true of THAT row rather than inventing a
                # size for it.
                print("helm dispatch: brief size UNKNOWN for this row — it "
                      "was minted before briefs were stored whole, so only "
                      "its bounded copy exists")
            print("helm dispatch: %s" % _chain_note(row))
            # ON EVERY REVIEW SEND, NOT ONLY A SUSPICIOUS ONE. The procedure a
            # brief does not state is the procedure its reader improvises, and
            # the improvised one has the reader reporting a defect it was
            # standing next to and could have cured. One line, always, so the
            # sender reads what they are asking for.
            if kind == "review":
                procedure = dispatches.REVIEW_MODE_LINES.get(
                    row.get("review_mode"), dispatches.REVIEW_READER_FIXES_LINE)
                print("helm dispatch: REVIEW PROCEDURE: " + procedure)
                if row.get("round_whisper"):
                    print("helm dispatch: " + row["round_whisper"])
            _door_after(door, row)
            return 0
        # `kind=kind` — ABSENT HERE UNTIL NOW, and it is the flag's whole point.
        # The parser accepted --kind on `add`, clean_kind VALIDATED it, and this
        # call then dropped it on the floor: every `dispatch add` recorded
        # UNKNOWN no matter what the operator typed, with rc 0 and no warning.
        # `send` two branches up always passed it, so the field looked wired.
        row, why = dispatches.add(
            # THE CANONICAL, not raw argv — same reason as send: the gate's
            # membership proof is about cap["canonical"], so that is the only
            # identity the row may store.
            target, pos[1], opts["--ref"], note=opts.get("--note"),
            deadline_s=deadline, repo=opts.get("--repo"), kind=kind,
            new_work=new_work, supersedes=opts.get("--supersedes"),
            force=force, _reason=True, posture_na=opts.get("--posture-na"),
            read_only_because=opts.get("--read-only-because"),
            owner_surface_because=opts.get("--no-owner-surface-because"),
            door=door["row"], pair_meld=door["pair"],
            decline_patch=opts.get(dispatches.DECLINE_PATCH_FLAG),
            task=opts.get("--task"), lever_because=opts.get("--because"),
            start_anyway=opts.get("--start-anyway"), weigh=True, steer=steer,
            whole=whole, review_mode=review_mode)
        _steer_lines(steer)
        if row is None:
            print("helm dispatch: " + (why or "dispatch NOT recorded"),
                  file=sys.stderr)
            return _rc(why)
        _lever_lines(row)
        for note in row.get(dispatches._ADMISSION_NOTES, ()):
            print("helm dispatch: RECIPIENT: " + note, file=sys.stderr)
        for warning in row.get(dispatches._WRITE_WARNINGS, ()):
            print("helm dispatch: WARNING: " + warning, file=sys.stderr)
        _record_forced(target, row, forced, opts.get("--reason"))
        # SAY THAT NOBODY WAS TOLD. `add` deliberately does not notify — that is
        # its whole difference from `send`, and it is the right primitive for an
        # obligation the recipient already agreed to out of band. But the line
        # it printed, "PENDING VERDICT / NEEDS CONFIRMATION", reads as a STATUS
        # the ledger will resolve on its own, so the writer walks away believing
        # the hand-off happened.
        #
        # LIVE 2026-07-27, by me, one hour after I called the same defect on
        # another lane: I minted a re-gate row for a codex seat with `add`, told
        # the room it was minted, and another seat had to go read that pane to
        # discover it was never in the recipient's task list. An obligation
        # exists that its holder does not know about — which is the whole bug
        # class the lr delivery leg is open on ("add() does not notify"), in
        # this file.
        #
        # The fix is NOT to make `add` send; that would delete the primitive.
        # It is to make the silence LOUD at the surface, so the next line the
        # writer reads tells them what they still owe.
        shown = dispatches._recipient_label(row)
        print("helm dispatch: %s -> @%s %s — PENDING VERDICT / NEEDS CONFIRMATION"
              % (row["id"], shown, row["lane"]))
        print("helm dispatch: %s" % _chain_note(row))
        # THREE OUTCOMES, THREE MESSAGES — because THIS MERGE makes `add` notify.
        #
        # fea7232 ("dispatch: `add` mints an obligation and tells NOBODY —
        # say so out loud") landed an unconditional "add records the
        # obligation but sends NOTHING" warning, which was exactly true
        # against a main where add()
        # never notified. The other lane gives add() a public @mention, so that
        # same line became a LIE the moment these two met: the recipient WAS
        # told and the CLI said they were not. Fixed IN the merge commit rather
        # than after it, so main is never once in a state where the code
        # notifies and its own surface denies it.
        #
        # The outcome is read from the DURABLE marker, not a return value.
        # _notify_public computes post_ok and add() discards it; threading it
        # out would make the truth a transient. _record_notify_failed already
        # writes a notify-failed event and _notify_failed_for reads it back, so
        # the ledger is the canonical answer — which is also what
        # docs/DISPATCH-ADD-CONTRACT.md (landing in this same merge) tells a
        # caller to do.
        #
        # notify=False keeps the original warning verbatim: there it is true.
        nf = dispatches._notify_failed_for(row["id"])
        if nf:
            print("helm dispatch: the mention FAILED (%s) — @%s has NOT been "
                  "told, though the obligation is recorded. `helm chat post` an "
                  "@%s mention naming %s."
                  % (str(nf.get("reason") or "unknown")[:120], shown,
                     shown, row["id"][:12]), file=sys.stderr)
        elif cap["membership"] == "JOINED":
            print("helm dispatch: @%s was mentioned in #main — their beacon "
                  "picks it up on next wake. Delivery is unconfirmed until they "
                  "read it, which is what NEEDS CONFIRMATION means."
                  % shown)
        else:
            # THE PICKUP PROMISE IS FALSE FOR A SEAT THAT HAS NOT JOINED, and
            # falsely REASSURING, which is worse than silence. A joining seat
            # BASELINES the public rooms at EOF — only its DM lane starts at 0
            # — so a mention posted before it joins is skipped forever, not
            # queued. `is True` and not truthiness: an unreadable roster (None)
            # cannot promise a pickup it did not establish either.
            if cap["membership"] == "ABSENT":
                # KNOWN ABSENT: the roster is readable, non-empty, and lacks
                # them. The skip is a fact, so state it as one.
                print("helm dispatch: @%s was mentioned in #main, but they "
                      "hold no roster row at assignment — a seat that joins "
                      "LATER baselines public rooms at EOF, so a public "
                      "mention is not a durable pre-join path. DM them, which "
                      "does start at 0, or re-send once they join."
                      % shown)
            else:
                # UNKNOWN. The previous wording said the mention WILL be
                # SKIPPED, which is the OPPOSITE CERTAINTY — unknown
                # membership means they may already be joined and take it
                # normally. Withhold the promise; never invent its negation.
                print("helm dispatch: @%s was mentioned in #main, but %s, so "
                      "PICKUP IS UNPROVEN either way — already joined and "
                      "their beacon takes it; joining later and public rooms "
                      "baseline at EOF, so it is missed. Confirm at the "
                      "recipient, or DM them, which starts at 0."
                      % (shown,
                         "the roster could not be read"
                         if cap["evidence"] == "read-failed"
                         else "no seat has joined this box yet"))
        if door["brief"]:
            door["lines"].append("helm dispatch: `add` carries no brief; "
                                 "hand the reader the BAR:\n" + door["brief"])
        _door_after(door, row)
        return 0
    if verb == "melds":
        return _cmd_melds(rest)
    if verb == "retract":
        return _cmd_retract(rest)
    if verb == "verdict":
        # Polarity is a FLAG, not a positional, so it can never be swallowed by
        # the free-text evidence tail. Omitting it is REFUSED for a new write —
        # see the measured rationale at the required-check below. (This comment
        # used to say omitting it "stays legal"; that was true before 2026-07-26
        # and contradicted the check twelve lines down, which is exactly the
        # doc-drift class reviews have been filing against this file.)
        # FLAGS ARE POSITIONAL — THEY PRECEDE THE EVIDENCE, AND THE EVIDENCE
        # IS WHATEVER REMAINS, VERBATIM (a blast-radius lens).
        #
        # The first cut scanned the WHOLE argv for anything starting with
        # "--", which reaches into the free-text evidence tail. Measured: a
        # reviewer who FORGETS the flag and writes "I could not run it so this
        # is --unverified at best" got a basis MINTED FROM THEIR PROSE — a
        # confidence claim they never made — and their evidence MUTILATED to
        # "I could not run it so this is at best". Both halves are worse than
        # the refusal the flag exists to produce.
        #
        # AND I ARGUED THE EXACT OPPOSITE when I chose a bare flag over
        # `--basis X`: I said a valued flag "would have to be pulled out
        # before the free-text evidence tail is joined, which is exactly how a
        # value gets swallowed into prose". The direction was backwards. A
        # positionless scan does not let prose swallow a value; it lets a
        # value be swallowed FROM prose.
        #
        # THE POLARITY HALF HAD THE SAME BUG AND IT PREDATES THIS LANE —
        # "...this is a --fix at best" in evidence would mint a polarity the
        # same way. One parse, one cure, because splitting it would leave the
        # older half of the same defect standing.
        flags, tail = dispatches.partition_verdict_flags(rest)
        # THE TERMINATOR IS HONOURED DOWNSTREAM TOO (task/3382 F4): the
        # partition consumes a bare `--`, and the free-text tail below then
        # re-scanned the evidence and refused one starting `-h` or `--x` —
        # so the escape its refusal teaches could never work, and a corrected
        # line carrying `-- '-h foo'` refused forever.
        escaped = rest[2 + len(flags):3 + len(flags)] == ["--"]
        rest = rest[:2] + tail
        # BASIS and POLARITY remain bare flags. The exit question has one
        # deliberately valued arm: a claim that BLOCKS must pay for a named,
        # checkable path rather than one more ceremonial boolean.
        bas, pol, worse, imperfect, unknown = [], [], [], 0, []
        observations = {}
        declared = set()      # observation fields declared UNKNOWN (lever 6)
        meld = []             # the meld room this verdict records (A4)
        design = []           # design findings beside a cure (T1 mixed case)
        patch = []
        no_patch = []
        diff_handoff = []
        # THE REVIEW'S FINDINGS, CARRIED FINDINGS AND NOTES, each repeatable
        # (helm/review_findings.py); mark_verdict owns every rule about them.
        review = {flag: [] for flag in dispatches.review_findings.FLAGS}
        # A MODEL RUN'S READ, recorded by this seat on its behalf (task/2948):
        # one value each, once. mark_verdict owns every rule about them.
        behalf = {}
        i = 0
        while i < len(flags):
            flag = flags[i]
            if flag in (dispatches._REVIEWER_MODEL_FLAG,
                        dispatches._REVIEWER_RUN_FLAG,
                        dispatches._AUTHOR_MODEL_FLAG):
                key = flag[2:].replace("-", "_")
                if key in behalf or i + 1 >= len(flags) \
                        or flags[i + 1].startswith("--"):
                    print("helm dispatch verdict: %s needs one value, once"
                          % flag, file=sys.stderr)
                    return 2
                behalf[key] = flags[i + 1]
                i += 2
                continue
            if flag in dispatches.BASIS_FLAGS:
                bas.append(flag)
            elif flag in ["--" + p for p in dispatches.POLARITIES]:
                pol.append(flag)
            elif flag == _IMPERFECT_FLAG:
                imperfect += 1
            elif flag in dispatches._FINDING_FLAGS:
                key = flag[2:].replace("-", "_")
                if key in observations or key in declared \
                        or i + 1 >= len(flags) \
                        or flags[i + 1].startswith("--"):
                    print("helm dispatch verdict: %s needs one value, once"
                          % flag, file=sys.stderr)
                    return 2
                value = flags[i + 1]
                if value.upper() == dispatches.UNKNOWN_OBSERVATION:
                    declared.add(key)
                    i += 2
                    continue
                if key == "finding_count":
                    value = dispatches.typed_finding_count(value)
                    if value is None:
                        print("helm dispatch verdict: finding_count needs a "
                              "non-negative integer (at most 9 digits)",
                              file=sys.stderr)
                        return 2
                observations[key] = value
                i += 1
            elif flag == dispatches._WORSE_THAN_MAIN_FLAG:
                if i + 1 >= len(flags) or flags[i + 1].startswith("--"):
                    print("helm dispatch verdict: --worse-than-main needs a "
                          "project-relative PATH", file=sys.stderr)
                    return 2
                worse.append(flags[i + 1])
                i += 1
            elif flag == dispatches._PATCH_TIP_FLAG:
                if i + 1 >= len(flags) or flags[i + 1].startswith("--") \
                        or patch:
                    print("helm dispatch verdict: --patch-tip needs one full "
                          "commit id, once — the cure YOU committed off the "
                          "reviewed tip", file=sys.stderr)
                    return 2
                patch.append(flags[i + 1])
                i += 1
            elif flag == dispatches._DESIGN_FINDING_FLAG:
                if i + 1 >= len(flags) or flags[i + 1].startswith("--"):
                    print("helm dispatch verdict: --design-finding needs the "
                          "finding, ONE quoted argv token", file=sys.stderr)
                    return 2
                design.append(flags[i + 1])
                i += 1
            elif flag == dispatches._MELD_FLAG:
                if i + 1 >= len(flags) or flags[i + 1].startswith("--") \
                        or meld:
                    print("helm dispatch verdict: --meld needs the meld ROOM, "
                          "once", file=sys.stderr)
                    return 2
                meld.append(flags[i + 1])
                i += 1
            elif flag == dispatches._NO_PATCH_BECAUSE_FLAG:
                if i + 1 >= len(flags) or flags[i + 1].startswith("--") \
                        or no_patch:
                    print("helm dispatch verdict: --no-patch-because needs "
                          "ONE argv token, once — quote the reason",
                          file=sys.stderr)
                    return 2
                no_patch.append(flags[i + 1])
                i += 1
            elif flag == dispatches._DIFF_HANDOFF_FLAG:
                if i + 1 >= len(flags) or flags[i + 1].startswith("--") \
                        or diff_handoff:
                    print("helm dispatch verdict: --diff-handoff needs one "
                          "exact pair meld ROOM/MSGID, once", file=sys.stderr)
                    return 2
                diff_handoff.append(flags[i + 1])
                i += 1
            elif flag in review:
                if i + 1 >= len(flags) or flags[i + 1].startswith("--"):
                    print("helm dispatch verdict: %s needs its value, ONE "
                          "quoted argv token each time" % flag,
                          file=sys.stderr)
                    return 2
                review[flag].append(flags[i + 1])
                i += 1
            else:
                unknown.append(flag)
            i += 1
        if len(pol) > 1 or unknown:
            print("helm dispatch verdict: polarity is one of %s (at most one)"
                  % " ".join("--" + p for p in dispatches.POLARITIES), file=sys.stderr)
            return 2
        if len(bas) > 1:
            print("helm dispatch verdict: basis is one of %s (at most one)"
                  % " ".join("--" + b for b in dispatches.BASES), file=sys.stderr)
            return 2
        if imperfect > 1:
            print("helm dispatch verdict: --imperfect may appear at most once",
                  file=sys.stderr)
            return 2
        if len(rest) < 3:
            print(synopsis("verdict"), file=sys.stderr)
            return 2
        # A NEW verdict must DECLARE its direction. Omitting the flag used to be
        # legal and recorded UNDECLARED — "the honest state, never an implied
        # approval" — which is right for REPLAY of rows written before polarity
        # existed, and wrong as a default for a fresh write nobody intends.
        #
        # Measured 2026-07-26: 28 of 77 verdicts (36%) are UNDECLARED, by SEVEN
        # different seats across five model families — and the integrator.
        # Two arrived within the hour: gemini and
        # grok each wrote APPROVE in chat and recorded UNDECLARED in the ledger.
        # A verdict is immutable, so all 28 are permanently unclassifiable and
        # 10 land loops can never be stall-checked. When every agent omits a
        # flag, the flag is not optional — it is missing a requirement.
        #
        # Same split the `kind` field already landed on: REQUIRED at the CLI for
        # new writes, still accepted as None by mark_verdict() so historical
        # replay and projection are untouched.
        if not pol:
            print("helm dispatch verdict: DECLARE the polarity — %s.\n"
                  "  A verdict is a decision and it is IMMUTABLE: omitting the "
                  "flag records UNDECLARED, which fails closed (never read as "
                  "an approval) and can never be corrected.\n"
                  "  36%% of verdicts on this ledger are already UNDECLARED, "
                  "by every seat in the fleet."
                  % " ".join("--" + p for p in dispatches.POLARITIES), file=sys.stderr)
            return 2
        # DECLARE HOW YOU KNOW. task/338, owner ask: "always making legible
        # how much doubt should go into a conversation". Captured as a premise
        # at certainty 1.00 and never delivered — 2.34% adoption DECAYING TO
        # ZERO, 221 verdicts unmarked BY CONSTRUCTION, and exactly 0% among
        # every non-Claude family. Prose is not model-family-proof; a refusing
        # verb is. Same split as polarity and kind: required for a NEW write,
        # permissive in mark_verdict so replay and projection are untouched.
        #
        # THE REFUSAL DEFINES THE THREE WORDS, because a reviewer meeting it
        # mid-verdict should not have to go read a premise to answer it.
        if not bas:
            print("helm dispatch verdict: DECLARE HOW YOU KNOW — %s.\n"
                  "  measured   = a tool ran and produced this finding\n"
                  "  inferred   = reasoned from code or output you read\n"
                  "  unverified = you have not checked\n"
                  "  A claim with no basis is UNVERIFIED, never a decorative "
                  "number. 2.34%% of verdicts on this ledger carry one, and "
                  "the rest are unmarked because nothing ever asked."
                  % " ".join("--" + b for b in dispatches.BASES), file=sys.stderr)
            return 2
        polarity = pol[0][2:]
        # THE THIRD TEACHING REFUSAL. APPROVE already ends the loop, and CONCUR
        # endorses without blocking, so asking either is friction on the desired
        # outcome. FIX/SUPERSEDE hand the work back and therefore owe the one
        # discriminator that makes another round rational: a named touched path
        # where this tip is actually WORSE THAN MAIN. Real imperfections on a
        # strictly better tip are APPROVE plus separately filed remainder, not
        # an adverse immutable verdict.
        if polarity in dispatches._EXIT_QUESTION_POLARITIES:
            if imperfect and worse:
                print("helm dispatch verdict: choose exactly one exit answer — "
                      "--worse-than-main PATH or --imperfect", file=sys.stderr)
                return 2
            # IMPERFECT NOW HAS ONE ADMITTED SHAPE, AND IT IS THE ONE THAT
            # CARRIES A CURE. A reader can find a tip no worse than main and
            # still commit a real improvement to it; approve refuses the patch
            # field, so without this arm that patch has no door at all and
            # travels by chat, where nothing records that the author owes it an
            # answer. WITHOUT a patch the refusal stands unchanged: that case
            # really is approve plus separately filed remainder.
            if imperfect and not patch:
                print("helm dispatch verdict: IMPERFECT IS NOT A BLOCK.\n"
                      "  --worse-than-main PATH = this named path touched by "
                      "the tip regresses relative to main; repeat it for every "
                      "blocking path. Only that answer earns FIX/SUPERSEDE.\n"
                      "  --imperfect = findings remain true, but the tip is not "
                      "worse than main on its touched paths. Use APPROVE with "
                      "a verified gate and file the remaining findings as "
                      "dispatch rows.\n"
                      "  --imperfect --patch-tip SHA is the one exception, and "
                      "it is not a block either: it records the cure YOU "
                      "committed off a tip you found no worse than main, and "
                      "what it asks for is the author's agreement on that "
                      "patch.\n"
                      "  The finding rate is not evidence that continuing is "
                      "correct.", file=sys.stderr)
                return 2
            if not worse and not imperfect:
                print("helm dispatch verdict: ANSWER THE EXIT QUESTION — is "
                      "this tip WORSE THAN MAIN ON A PATH IT TOUCHES, or merely "
                      "imperfect?\n"
                      "  --worse-than-main PATH = this named touched path "
                      "regresses relative to main; repeat it for every blocking "
                      "path. Only that answer earns FIX/SUPERSEDE.\n"
                      "  --imperfect = findings remain true, but the tip is not "
                      "worse than main on its touched paths. That is not a "
                      "block: use APPROVE with a verified gate and file the "
                      "remaining findings as dispatch rows.\n"
                      "  The finding rate is not evidence that continuing is "
                      "correct.", file=sys.stderr)
                return 2
        elif imperfect or worse:
            print("helm dispatch verdict: --worse-than-main PATH and "
                  "--imperfect answer only FIX/SUPERSEDE; APPROVE already ends "
                  "the loop and CONCUR is non-blocking endorsement",
                  file=sys.stderr)
            return 2
        # THE FOURTH TEACHING REFUSAL. A FIX hands the work back, and the
        # reader who found the defect is standing in the tree that has it —
        # so the default answer to "did you cure it?" is YES, and a FIX with
        # no cure states which of the two reasons applies. A DESIGN finding
        # bound for a meld is one of them and is recorded like any other.
        if polarity == "fix" and not patch and not no_patch:
            print("helm dispatch verdict: A READER FIXES WHAT IT FINDS.\n"
                  "  Commit your cure off %s — the exact tip you read (a "
                  "subagent reviewer has no room of its own by default: use a "
                  "scratch clone unless your parent assigned its sole delegate "
                  "a registered BUILD room) — do not push, and name it "
                  "with --patch-tip SHA; the row then carries two authors and "
                  "the author reviews your patch.\n"
                  "  If this finding is not yours to patch — a DESIGN "
                  "disagreement belongs in a meld, not in one side's commit — "
                  "pass --no-patch-because REASON (ONE argv token; quote it) "
                  "and the row records which it was."
                  % (str(rest[1])[:12]), file=sys.stderr)
            return 2
        # THE FIFTH TEACHING REFUSAL (lever 6). A FIX hands the work back,
        # and whether the next read is converging or spiralling is read off
        # two typed observations: how many findings, and how they relate to
        # the previous read's. Optional, they were on 36 of 61 FIX verdicts,
        # and the rung that tells a spiral from a convergence read every
        # chain as UNKNOWN. UNKNOWN is still an answer; silence is not. A
        # model run's ADVISORY read records no observations, so it is not
        # asked for them.
        # A FIX THAT NAMES ITS FINDINGS HAS COUNTED THEM (task/3742): the
        # count is derived, and a typed one that disagrees refuses here.
        findings = review[dispatches.review_findings.FINDING_FLAG]
        carried = review[dispatches.review_findings.CARRIED_FLAG]
        counted, why = dispatches.review_findings.derive_count(
            observations.get("finding_count"), declared,
            {"findings": findings, "findings_carried": carried})
        if why:
            print("helm dispatch verdict: " + why, file=sys.stderr)
            return 2
        if counted is not None:
            observations["finding_count"] = counted
        # NO COUNT-ONLY FIX GUARD HERE: mark_verdict owns it, and runs it only
        # after an exact retry of a standing verdict has reconciled. A copy at
        # this door refused that retry before mark_verdict could see it.
        if polarity == "fix" and not behalf:
            missing = [flag for flag, key in zip(dispatches._FINDING_FLAGS,
                                                 dispatches._FINDING_FIELDS)
                       if key not in observations and key not in declared]
            if missing:
                print("helm dispatch verdict: A FIX COUNTS WHAT IT FOUND — "
                      "missing %s.\n"
                      "  --finding-count N = the findings this read raises\n"
                      "  --prior-relation new|uncured|regression-of-cure = how "
                      "the newest findings relate to the previous read's cure "
                      "(new on the first read of a chain)\n"
                      "  Either may be the literal UNKNOWN, which is recorded "
                      "as declared; omitting one is refused, because the "
                      "spiral rung reads a chain with no counts as UNKNOWN."
                      % " and ".join(missing), file=sys.stderr)
                return 2
            if "finding_count" in declared and "prior_relation" in observations:
                print("helm dispatch verdict: --prior-relation describes "
                      "COUNTED findings; with --finding-count UNKNOWN it is "
                      "UNKNOWN too", file=sys.stderr)
                return 2
        note, rc = dispatches.freetext.tail("helm dispatch", "verdict",
                                 ["--"] * escaped + rest[2:],
                                 "the verdict note")
        if rc is not None:
            return rc
        reviewed, patch_tip, rc = _typed_tips(rest[0], rest[1],
                                              patch[0] if patch else None)
        if rc is not None:
            return rc
        row, why = dispatches.mark_verdict(rest[0], reviewed, note or "",
                                polarity=polarity,
                                basis=bas[0][2:] if bas else None,
                                bind_author=True,
                                worse_than_main_paths=worse,
                                patch_tip=patch_tip,
                                imperfect=bool(imperfect),
                                no_patch_because=no_patch[0] if no_patch
                                else None,
                                diff_handoff=diff_handoff[0] if diff_handoff
                                else None,
                                **behalf, **observations,
                                **({"declared_unknown": tuple(sorted(declared))}
                                   if declared else {}),
                                **({"meld_room": meld[0]} if meld else {}),
                                **({"design_findings": design} if design
                                   else {}),
                                findings=findings, findings_carried=carried,
                                notes=review[
                                    dispatches.review_findings.NOTE_FLAG])
        if why:
            print("helm dispatch: " + why + _unnamed_tip_hint(rest[0], reviewed),
                  file=sys.stderr)
            return 1
        # A MODEL RUN'S READ IS ADVISORY (task/2948): said back as what it is,
        # and nothing below it runs, because no verdict was written, no row
        # closed and nobody is owed a nudge for it. Said in the SAME lines
        # `lr show` and `dispatch triage` print for it (task/3081), so the
        # seat that recorded it sees exactly what a later reader will.
        if behalf:
            # ONE RUN IN EITHER SPELLING: a retry spelled agent-<id> is the
            # read the row already carries, and is said back as that read.
            run = dispatches._advisory_run_key(behalf.get("reviewer_run"))
            read = next((r for r in reversed(row.get("advisory_reads") or ())
                         if dispatches._advisory_run_key(r.get("reviewer_run"))
                         == run), None)
            # A FRESH-CONTEXT CLEAN READ COUNTS (task/3658), so this verb also
            # records the source-clean hold it carries, through the hold's
            # own door: one verb makes an author's own fresh subagent's read
            # land-ready, with no third seat. A refusal leaves the read
            # recorded and says why nothing is held.
            held = refused = None
            if read and read.get("independence") == "fresh-context" \
                    and read.get("polarity") == "concur" \
                    and row.get("status") == "open":
                held, refused = dispatches.mark_hold(
                    row["id"], "SOURCE-CLEAN: fresh-context run %s read %s "
                    "clean" % (run, reviewed[:12]), source_clean_tip=reviewed)
                row = held or row
            said = dispatches.advisory_read_lines(
                dict(row, advisory_reads=(read,) if read else ()))
            for n, line in enumerate(said):
                print("helm dispatch: %s%s" % ("%s — " % row["id"] if n == 0
                                               else "", line))
            for line in _findings_lines(row):
                print("helm dispatch: " + line)
            if refused:
                print("helm dispatch: the read is recorded and holds nothing: "
                      + refused, file=sys.stderr)
            elif held:
                _hold_holder_nudge(held)
            return 0
        # THE MARKER IS SAID BACK. A basis nothing renders is a basis nobody
        # mints — the 2.34%-adoption lesson in one line.
        print("helm dispatch: %s — VERDICT (%s/%s) at %s" % (
            row["id"], (row.get("polarity") or "UNDECLARED").upper(),
            (row.get("basis") or "UNMARKED").upper(),
            row["tip"][:12]))
        if row.get("pin_warning"):
            print("helm dispatch: warning: " + row["pin_warning"],
                  file=sys.stderr)
        said = row.get(dispatches.DECLARED_UNKNOWN) or ()
        print("helm dispatch: findings: %s; prior relation: %s" % tuple(
            row.get(key, "UNKNOWN (declared)" if key in said else "UNKNOWN")
            for key in dispatches._FINDING_FIELDS))
        print("helm dispatch: exit question: %s" % dispatches.verdict_exit_answer(row))
        # SAID BACK, because a co-author record nothing renders is a co-author
        # record nobody mints — the 2.34%-basis-adoption lesson, applied before
        # it can repeat.
        if row.get("patch_tip"):
            print("helm dispatch: reviewer patch: %s by @%s — this row now has "
                  "TWO authors; merge that exact tip as a parent of a new "
                  "composition commit" % (
                      row["patch_tip"][:12],
                      row.get("patch_author") or row.get("recipient") or "?"))
        # SAID BACK FOR THE SAME REASON THE PATCH IS: a recorded answer nobody
        # renders is an answer nobody checks.
        if row.get("no_patch_because"):
            print("helm dispatch: no cure committed, because: %s"
                  % row["no_patch_because"])
        if dispatches._has_diff_handoff(row):
            receipt = row["diff_handoff"]
            print("helm dispatch: exact diff handoff: %s/%s at meld round %s "
                  "(sha256 %s)" % (receipt["room"], receipt["msg_id"],
                                   receipt["epoch"], receipt["sha256"]))
        for finding in row.get("design_findings") or ():
            print("helm dispatch: design finding (meld, not patch): %s"
                  % finding)
        _meld_said_back(row)
        if row.get("polarity") == "fix":
            from . import dispatches_announce
            standing = dispatches_announce.standing_room_line(row)
            if standing:
                print("helm dispatch: " + standing)
        print("helm dispatch: gate: %s" % dispatches.gate_state(row))
        print("helm dispatch: attestation: %s" % row.get("announce", "n/a"))
        # THE REVIEW'S FINDINGS AND NOTES, where they were filed (task/3742).
        for line in dispatches.review_findings.verdict_lines(row):
            print("helm dispatch: " + line)
        # THE qwen27 FINDINGS NOTE, said back to the reviewer who just
        # verdicted: a note is for them to adjudicate, and it authorized
        # nothing about the verdict above.
        for line in _findings_lines(row):
            print("helm dispatch: " + line)
        # BOTH POLES WAKE SOMEBODY, and the else is the whole fix. This was
        # `if approve: nudge` with no other branch, so fix / supersede /
        # concur / UNDECLARED all fell through and woke nobody. Every one of
        # those hands the row BACK, so the author is exactly who needs the
        # news, and none of them is rarer than approve: 21 of the 22 verdicts
        # measured on the night this was written were fix.
        #
        # THIS IS THE ONLY PRODUCTION DOOR, and that was measured rather than
        # assumed before hooking here: mark_verdict has exactly ONE non-test
        # caller in the tree, four lines above. Every other occurrence is a
        # docstring. A hook here therefore covers every verdict helm records.
        if row.get("polarity") == "approve":
            dispatches._verdict_land_nudge(row)
        else:
            dispatches._verdict_author_nudge(row)
        return 0
    if verb == "retip":
        as_json = "--json" in rest
        ok, pos, opts, perr = retip_argv_ok(rest)
        if not ok:
            if perr:
                print("helm dispatch retip: " + perr, file=sys.stderr)
            print("usage: helm dispatch retip <id-or-unique-prefix> --ref "
                  "NEW_TIP --reason R [--repo PATH] [--json]", file=sys.stderr)
            return 2
        out, why = dispatches.retip(pos[0], opts["--ref"], reason=opts.get("--reason"),
                         repo=opts.get("--repo"))
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        hop = out["retips"][-1]
        if as_json:
            print(json.dumps(dispatches.redact_bodies(out),
                              ensure_ascii=False, indent=1))
        else:
            # Rendered from the PERSISTED hop, not from the writer's return
            # value, so the operator reads the same fact a later replay does.
            print("helm dispatch: %s RETIPPED %s -> %s [identity %s%s] — %s"
                  % (out["id"][:12], str(hop["old_tip"])[:12],
                     out["tip"][:12], out["identity"],
                     ", BASE REPLACED — rebase work in progress"
                     if hop.get("base_replaced") else "", hop["reason"]))
        return 0
    if verb == "attach-task":
        # ONE append-only event on the chain's FIRST row (task/4000): the
        # chain now serves --task, and a later round files and carries its
        # findings under it. The library door owns every refusal.
        pos, opts, err = dispatches._parse(rest, ("--task",))
        if err or len(pos) != 1 or not opts.get("--task"):
            print("usage: helm dispatch attach-task <id-or-unique-prefix> "
                  "--task task/N", file=sys.stderr)
            return 2
        out, why = dispatches.attach_task(pos[0], opts["--task"])
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: chain %s ATTACHED to %s by @%s (%s) — later "
              "rounds file and carry their findings under it; the chain's "
              "history, findings and pair room are unchanged"
              % (out["id"][:12], out["attached_task"],
                 out.get("attached_task_by") or "?",
                 out.get("attach_role") or "?"))
        return 0
    if verb == "rebind":
        # --force/--json are bare booleans; --to/--reason/--repo carry values
        force = "--force" in rest
        as_json = "--json" in rest
        ok, pos, opts, perr = dispatches.rebind_argv_ok(rest)
        if not ok:
            if perr:
                print("helm dispatch rebind: " + perr, file=sys.stderr)
            print("usage: helm dispatch rebind <id-or-unique-prefix> --to "
                  "<seat> [--force] [--reason R] [--repo PATH] [--json]",
                  file=sys.stderr)
            return 2
        # THE SAME ROSTER DOOR AS send/add — rebind() calls resolve_recipient,
        # which NORMALISES and never checks membership, so without this a
        # rebind hands a LIVE obligation to an address nobody holds. That is
        # strictly worse than the send case it shares a class with: send
        # invents an unowned row, rebind takes work a seat is already carrying
        # and strands it. Measured before fixing — the move succeeded and helm
        # then advised "Re-brief @claude directly", naming a seat that does not
        # exist.
        #
        # DELIBERATELY NOT force-BYPASSED, unlike send. Here --force attests
        # that the recipient is STARVED, not that the caller knows the target
        # is pre-join; and since every manual rebind already carries --force to
        # clear the starvation gate, honouring it here would disable this door
        # on the exact path that needs it. A genuine pre-join handoff is
        # cancel + `send --force`, which states that intent explicitly.
        _cap, unroutable = dispatches._recipient_gate(opts["--to"], False, "rebind")
        if unroutable:
            print("helm dispatch rebind: " + unroutable, file=sys.stderr)
            return 2
        target, target_err = dispatches._canonical_recipient(_cap["canonical"])
        if target_err:
            print("helm dispatch rebind: " + target_err, file=sys.stderr)
            return 1
        # This typed TARGET is the identity the gate proved; rebind must not
        # mistake it for raw argv and resolve it into a different key.
        # pair_meld={}: the new reader is invited into the chain's pair meld,
        # the same room its earlier rounds are in.
        out, why = dispatches.rebind(pos[0], target,
                          reason=opts.get("--reason"), force=force,
                          repo=opts.get("--repo"), pair_meld={})
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        if as_json:
            # The machine receipt for the automation caller rebind()'s own
            # docstring anticipates ("a verb that can later be CALLED
            # automatically"): the full {old,new,reason} move on stdout; the
            # re-brief NOTE below stays on stderr where it never corrupts a
            # JSON consumer.
            print(json.dumps(dispatches.redact_bodies(out),
                              ensure_ascii=False, indent=1))
        else:
            print("helm dispatch: %s REBOUND to @%s -> %s — %s"
                  % (out["old"]["id"][:12], out["new"]["recipient"],
                     out["new"]["id"][:12], out["reason"]))
            # THE NEW ROW'S ADMISSION NOTES REACH THE OPERATOR HERE OR NOWHERE.
            # The write door computes them for every caller, and the send
            # branch prints them; without this loop a rebind computed the
            # recipient's tier and reachability and then discarded both one
            # frame before a human saw either — the shape where a tool KNOWS
            # and does not SAY. The JSON branch above already carries them as
            # data, so this is the stderr half of the same answer.
            for note in out["new"].get(dispatches._ADMISSION_NOTES, ()):
                print("helm dispatch: RECIPIENT: " + note, file=sys.stderr)
            for line in (out["new"].get(dispatches._PAIR_MELD) or {}).get(
                    "lines") or ():
                print("helm dispatch: " + line)
        # THE ROOM DOES NOT MOVE WITH THE OBLIGATION (#203). stderr in BOTH
        # branches: the JSON receipt already carries room_fence as data, and a
        # warn on stdout would corrupt the automation consumer this verb's own
        # docstring anticipates.
        for f in (out.get("room_fence") or []):
            left = f.get("remaining")
            print("helm dispatch: NOTE — @%s still holds the room %s%s. The "
                  "REBIND MOVED THE OBLIGATION, NOT THE LEASE, so the new "
                  "builder cannot claim it."
                  % (f.get("holder"), f.get("lane"),
                     "" if left is None else " for %ss more" % left),
                  file=sys.stderr)
            print("helm dispatch:   Fresh room: `helm work claim %s-r2` — "
                  "that unblocks in one line and needs no force-release, which "
                  "matters because a walled seat is not a dead one and its "
                  "room may hold real work."
                  % str(f.get("lane") or "").rsplit(":", 1)[-1],
                  file=sys.stderr)
            print("helm dispatch:   THEN EXPECT A LANE-LABEL/BRANCH "
                  "DIVERGENCE: the row keeps naming a branch that will never "
                  "contain the work, so `dispatch send` will warn `--ref is "
                  "NOT one of lane X's own commits` and every surface "
                  "resolving lane->branch reads the wrong one. Tell the "
                  "reviewer which branch the tip is really on.",
                  file=sys.stderr)
        # WHAT REACHED THE NEW RECIPIENT, MEASURED PER ROW.
        #
        # MEASURED 2026-07-31: I rebound two rows off a dark family and BOTH
        # recipients came back asking for the brief. One of them named the gap
        # itself: "the superseding row carries only lane + base... please
        # resend". `send` now stores the body (capped, and saying so when it is
        # capped), so on a row minted after 2026-08-27 the brief TRAVELS — and
        # this line says which of the three cases actually happened instead of
        # asking for a re-brief every time, including when nothing was lost.
        print("helm dispatch: NOTE — " + dispatches._brief_travel_note(out["old"],
                                                            out["new"]),
              file=sys.stderr)
        return 0
    if verb == "mark-delivered":
        if len(rest) < 2:
            print("usage: helm dispatch mark-delivered <id-or-unique-prefix> "
                  "<delivery-ref>  (update delivery_ref after a send -- retry "
                  "evidence, changed mechanism, manual confirmation)",
                  file=sys.stderr)
            return 2
        row, why = dispatches.mark_delivered(rest[0], rest[1])
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        warnings = row.get(dispatches._WRITE_WARNINGS, ())
        if warnings:
            print("helm dispatch: %s -- DELIVERY UPDATED" % row["id"])
            for w in warnings:
                print("helm dispatch: WARNING: " + w, file=sys.stderr)
        else:
            print("helm dispatch: %s -- DELIVERED (%s)" % (
                row["id"], row.get("delivery_ref") or "none"))
        return 0
    if verb == "cancel":
        # `--chain` IS A FLAG ONLY BEFORE A `--` ESCAPE, so a reason that
        # must say the word still can.
        cut = rest.index("--") if "--" in rest else len(rest)
        chain = "--chain" in rest[:cut]
        dry_run = "--dry-run" in rest[:cut]
        rest = [a for i, a in enumerate(rest)
                if i >= cut or a not in ("--chain", "--dry-run")]
        if not rest:
            print("usage: helm dispatch cancel <id-or-unique-prefix> [--chain] "
                  "[--dry-run] <reason...>  "
                  "(honestly abandon a stranded dispatch — recipient gone / "
                  "work moot; never a substitute for a real verdict. It ALSO "
                  "closes a reviewed row whose verdict declared NO polarity: "
                  "that verdict authorized nothing and demanded nothing, so "
                  "the close is recorded as an ADVISORY CLOSE whose reason "
                  "names it. A verdict WITH a polarity is still refused. "
                  "Every cancel names the open rounds and FIX verdicts it "
                  "leaves carried by nothing; --chain also cancels those open "
                  "rounds with the same reason, and never a verdict. "
                  "--dry-run runs every check and writes nothing)",
                  file=sys.stderr)
            return 2
        reason, rc = dispatches.freetext.tail("helm dispatch", "cancel", rest[1:],
                                   "a cancel reason",
                                   known=("--chain", "--dry-run"))
        if rc is not None:
            return rc
        outcome = {}
        row, why = dispatches.mark_cancel(rest[0], reason or "", chain=chain,
                                          outcome=outcome, dry_run=dry_run)
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        # A DRY RUN LEADS WITH ITS BANNER AND SAYS WOULD: a reader or a
        # script taking the first status line must never see a cancel that
        # did not happen.
        if dry_run:
            print("helm dispatch: DRY RUN — nothing written; re-run without "
                  "--dry-run to cancel. What the cancel would record:")
        if dry_run and row.get("already_cancelled"):
            state = "ALREADY CANCELLED, nothing left to cancel for this row"
        elif row.get("cancel_advisory"):
            state = "WOULD ADVISORY-CLOSE" if dry_run else "ADVISORY-CLOSED"
        else:
            state = "WOULD CANCEL" if dry_run else "CANCELLED"
        print("helm dispatch: %s — %s (%s)" % (
            row["id"], state, row.get("cancel_reason") or ""))
        for pid in outcome.get("chained", ()):
            print("helm dispatch: %s — %s (%s) by --chain"
                  % (pid, "WOULD CANCEL" if dry_run else "CANCELLED",
                     reason or ""))
        print("helm dispatch: " + _uncarried_line(outcome, would=dry_run))
        return 0
    if verb == "hold":
        owner_gated = "--owner-gated" in rest
        rest = [a for a in rest if a != "--owner-gated"]
        source_clean_tip = None
        if "--source-clean" in rest:
            at = rest.index("--source-clean")
            if at + 1 >= len(rest):
                print("helm dispatch hold: --source-clean needs the exact tip "
                      "you read clean, so the integrator can gate that tree",
                      file=sys.stderr)
                return 2
            source_clean_tip = rest[at + 1]
            del rest[at:at + 2]
        meld_room = None
        if dispatches._MELD_FLAG in rest:
            at = rest.index(dispatches._MELD_FLAG)
            if at + 1 >= len(rest):
                print("helm dispatch hold: --meld needs the meld ROOM whose "
                      "outcome this clean read records", file=sys.stderr)
                return 2
            meld_room = rest[at + 1]
            del rest[at:at + 2]
        if not rest:
            print("usage: helm dispatch hold <id-or-unique-prefix> <reason...> "
                  "[--owner-gated] [--source-clean TIP [--meld ROOM[@EPOCH]]]  "
                  "(--meld names the meld whose MELD OUTCOME this clean read "
                  "records; acknowledge an "
                  "obligation gated on an external dependency; --owner-gated "
                  "when that dependency is a DECISION ONLY THE OWNER CAN MAKE, "
                  "which keeps the row out of the machine stall count and names "
                  "him as the holder; --source-clean TIP when your source read "
                  "found nothing and the only thing left is the integrator's "
                  "land gate on the rebased tree, which is what an approve must "
                  "bind — only the row's recipient may, or its sender on a "
                  "fresh-context read it recorded at that tip, at a tip "
                  "descending from the dispatched ref. "
                  "`helm dispatch list --held --source-clean` is the "
                  "integrator's listing of those rows)",
                  file=sys.stderr)
            return 2
        reason, rc = dispatches.freetext.tail("helm dispatch", "hold", rest[1:],
                                   "a hold reason")
        if rc is not None:
            return rc
        row, why = dispatches.mark_hold(rest[0], reason or "",
                             owner_gated=owner_gated,
                             source_clean_tip=source_clean_tip,
                             meld_room=meld_room)
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        held_on = ""
        rehold = None
        if row.get("source_clean_tip") and not row.get("owner_gated"):
            from . import landreq            # DEFERRED — landreq imports us.
            rehold = landreq.source_clean_rehold(row)
        if row.get("owner_gated"):
            held_on = " ON THE OWNER"
        elif rehold:
            # An idempotent re-run of a hold written before the door bound
            # its holder returns that hold unchanged; say so, with the move
            # that clears it (task/3053), rather than hand the row to the
            # integrator — asked of the one holder predicate.
            held_on = (" SOURCE-CLEAN at %s — but no land can close this "
                       "hold: %s" % (row["source_clean_tip"], rehold["why"]))
        elif row.get("source_clean_tip"):
            held_on = (" SOURCE-CLEAN at %s — ON THE INTEGRATOR'S LAND GATE"
                       % row["source_clean_tip"])
        print("helm dispatch: %s — HELD%s (%s)" % (
            row["id"], held_on, row.get("hold_reason") or ""))
        if row.get("pin_warning"):
            print("helm dispatch: warning: " + row["pin_warning"],
                  file=sys.stderr)
        for line in dispatches.review_findings.closed_lines(row):
            print("helm dispatch: " + line)
        _meld_said_back(row)
        # THE HOLD THAT MOVES THE PLATE WAKES ITS NEW HOLDER. Only the
        # source-clean one does: the owner reaches his asks through the
        # owner-ask surfaces, and a bare hold moves the row to nobody new.
        _hold_holder_nudge(row)
        if row.get("source_clean_tip") \
                and row.get("source_clean_tip") != (row.get("tip") or ""):
            # A DESCENDANT'S DIVERGENCE IS SHOWN, NEVER REFUSED: a cure round
            # moves the tip past the dispatched ref and that is the ordinary
            # case. What would be wrong is letting it pass unseen, because the
            # land closes on the tip named here and not the one the row was
            # sent at. A tip OUTSIDE the dispatched ref's history never gets
            # here: `mark_hold` refuses it (task/3053).
            print("helm dispatch: the row was dispatched at %s; this hold "
                  "declares %s clean" % (row.get("tip") or "an unknown tip",
                                         row["source_clean_tip"]))
        return 0
    if verb == "applied":
        fab_receipt = None
        if "--fab-receipt" in rest:
            at = rest.index("--fab-receipt")
            if at + 1 >= len(rest):
                print("helm dispatch applied: --fab-receipt needs the OK "
                      "focused fab receipt on the applied tip",
                      file=sys.stderr)
                return 2
            fab_receipt = rest[at + 1]
            del rest[at:at + 2]
        if len(rest) != 2:
            print("usage: helm dispatch applied <parent-id-or-unique-prefix> "
                  "<tip> [--fab-receipt R]  (record the author's UNCHANGED "
                  "applied MELD-DIFF cure on the ORIGINAL row: the applied "
                  "diff's verbatim patch-id must equal the posted diff's, tip^ "
                  "must be the reviewed tip, and an OK focused fab receipt "
                  "must bind at the tip. ONE diff-applied event, no child "
                  "row, no reviewer wake. A CHANGED cure refuses and prints "
                  "the delta child send with `git range-diff` for the "
                  "reviewer instead)",
                  file=sys.stderr)
            return 2
        row, why = dispatches.mark_applied(rest[0], rest[1],
                                           fab_receipt=fab_receipt)
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        proof = row.get("diff_applied") or {}
        print("helm dispatch: %s — DIFF-APPLIED at %s (patch-id %s, receipt "
              "%s) on the original row; no child minted, the reviewer is "
              "not woken" % (row["id"], str(proof.get("tip"))[:12],
                             str(proof.get("patch_id"))[:12],
                             str(proof.get("receipt_sha256"))[:12]))
        return 0
    if verb == "release":
        if not rest:
            print("usage: helm dispatch release <id-or-unique-prefix>  "
                  "(return a HELD row to OPEN -- the dependency is resolved)",
                  file=sys.stderr)
            return 2
        row, why = dispatches.mark_release(rest[0])
        if why:
            print("helm dispatch: " + why, file=sys.stderr)
            return 1
        print("helm dispatch: %s — RELEASED to OPEN" % row["id"])
        return 0
    if verb == "triage":
        # ON-DEMAND re-measure, where the git cost is the point of the visit
        # (the list renderer deliberately dropped it: 0.60s/row against 0.25s
        # for the whole listing). Each open row's measurable claims are
        # re-checked against the tree checked out RIGHT NOW — file:line by
        # CONTENT against the filing-era tree, count claims against the live
        # ledger, cited shas by ancestry AND patch-identity — and the row
        # prints its verdict beside its id so a seat picking work up reads
        # the decay before it reads the prose.
        current, unavailable = dispatches.snapshot()
        if unavailable:
            print("helm dispatch: ledger unavailable; triage cannot run: %s"
                  % unavailable, file=sys.stderr)
            return 1
        from . import clearspan
        # TRIAGE IS ACTIONABLE WORK, so it reads the owed frontier: offering
        # both a superseded parent and its successor asks a human to
        # re-measure the same obligation twice.
        selected = dispatches.owed(current)
        # EVERY NAMED ID GETS AN ANSWER, THROUGH ONE RESOLUTION DOOR, BEFORE
        # ANY FILTERING. Measured 2026-08-11T00:20Z: a verdicted row and a
        # garbage id both answered rc 0 with ZERO BYTES — byte-identical —
        # while an open row printed its line, so a typo'd id read exactly
        # like a clean answer and finished work read like nothing. Silence
        # is the one failure mode nobody re-checks. And the first cut of
        # this cure kept the old PREFIX filter ahead of the resolver
        # (a FIX verdict on that cut): an ambiguous prefix grazed every open
        # row it matched and printed them all at rc 0, so _resolve_row's
        # ambiguity refusal existed and was unreachable — a prefix path
        # AROUND the door. Every token now resolves against the FULL
        # snapshot first: no row or more than one row refuses on stderr
        # (naming the token, and for ambiguity the candidate count), and
        # only resolved full ids reach the owed filter and the cured scan —
        # which also takes the guesswork out of the cured filter's own
        # startswith matching. What triage MEASURES for a resolved open row
        # is untouched.
        # ONE PROJECT'S PICKUP WORK, FOR THE BULK LISTING ONLY (task/2437).
        # A NAMED id is a question about THAT row and is always answered — the
        # law this verb already states — and the answer stays correct for a
        # foreign row because `clearspan.re_measure` resolves the tree from
        # `row["repo_id"]`, never from the caller's cwd. So this is a RELEVANCE
        # filter, not a correctness one: the bulk listing is what a seat reads
        # to pick work up, and offering another project's obligations to a seat
        # standing in this checkout is offering work it cannot do.
        #
        # THE SET-ASIDE IS PRINTED, never silent. Silently skipping is the
        # failure this verb's own comment names one paragraph up: an absence
        # nobody re-checks. The count and the escape ride together.
        #
        # THROUGH THE CLASSIFIER'S POLICY, NOT REPOSITORY EQUALITY (round two,
        # finding 2): `_scope_class` keeps a SIBLING repository of this project
        # and never exiles a legacy row whose provenance is unrecorded. The
        # equality test this replaces advertised a project and delivered a
        # repository, so a project registering two checkouts saw half its
        # pickup work, and the oldest rows in the ledger — the ones with no
        # repo_id at all — could be triaged from nowhere.
        #
        # AND UNPLACEABLE IS ITS OWN BUCKET, NOT THIS PROJECT'S (task/2468).
        # Keeping those rows is right; filing them under the cwd's project is
        # not, and a boolean answer cannot express the difference — which is why
        # this takes the CLASS. They stay in the pickup listing — a row nobody
        # can place
        # is still owed by the seat it names — under a heading that says nobody
        # can place it.
        all_projects = "--all-projects" in rest
        rest = [a for a in rest if a != "--all-projects"]
        classify, scope_keep, scope_label = None, None, None
        unplaceable = []
        if not all_projects:
            scope_repo, scope_project, _why = dispatches.cwd_scope()
            if scope_repo:
                classify = _scope_class(scope_repo, scope_project)
                scope_label = scope_project or scope_repo

                # THE CURE CENSUS KEEPS THE UNPLACEABLE ROWS (task/2468). Its
                # question is which cures to MEASURE, not which project label a
                # row wears, and `cured_by_repo` already answers
                # repository-blind for a row it cannot resolve a checkout for.
                # Narrowing them out here would have counted them in the
                # "outside project X" line below, which is the one thing
                # provenance-unknown demonstrably is NOT.
                def scope_keep(row, _classify=classify):
                    return _classify(row) != "foreign"
        if not rest and classify is not None:
            placed = {r["id"]: classify(r) for r in selected}
            outside = _unplaceable_classes(scope_project)
            unplaceable = [r for r in selected if placed[r["id"]] in outside]
            kept = [r for r in selected if placed[r["id"]] != "foreign"
                    and placed[r["id"]] not in outside]
            aside = len(selected) - len(kept) - len(unplaceable)
            selected = kept
            if aside:
                print("helm dispatch triage: %d row%s outside project %s "
                      "set aside — run triage from that project's checkout, "
                      "or `--all-projects` to measure them all here"
                      % (aside, "" if aside == 1 else "s", scope_label),
                      file=sys.stderr)
        unresolved, named = [], {}
        if rest:
            for token in dict.fromkeys(rest):
                row, why = dispatches._resolve_row(current, token, allow_retired=True,
                                                   allow_unknown_kinds=True)
                if why:
                    print("helm dispatch triage: " + why, file=sys.stderr)
                    unresolved.append(token)
                    continue
                named[row["id"]] = row
            selected = [r for r in selected if r["id"] in named]
        if not selected and not unplaceable and not rest:
            print("helm dispatch triage: no matching open rows")

        def _triage_line(row):
            """ONE row's measured line. Extracted because the UNKNOWN bucket
            below prints the same measurement, and a second copy of this format
            is how two sections of one table come to disagree about what a
            column means."""
            verdict, detail = clearspan.re_measure(row)
            # WHICH MODEL ANSWERED, beside the seat that answered. A row that
            # carries a verdict carries its author's recorded route; an open
            # row has no verdict and prints nothing, which is the honest
            # difference rather than a blank column on every line.
            model = dispatches.verdict_resolved_model(row)
            # AND WHERE THE FAMILY THE APPROVAL TIER JUDGED CAME FROM, on the
            # rows where it was NOT the model above. A measured route prints
            # its model and nothing else; a roster stamp and an unreadable
            # envelope each print their own word, because a tier computed from
            # a weaker input must not look identical to one computed from a
            # measurement, and a blank cannot tell the two apart.
            axis = dispatches.verdict_family_axis_note(row)
            # A ROW THIS HELM CANNOT READ IN FULL IS STILL MEASURED, and says
            # so: triage is where a seat picks work up, and the state it reads
            # here stopped at an event this binary has no arm for.
            unread = dispatches.unknown_kinds_note(row)
            return "%s %-7s %-12s %-28s %s%s%s%s" % (
                row["id"][:12], verdict.upper(), dispatches._recipient_label(row),
                str(row.get("lane") or "-")[:28], detail,
                "  [model %s]" % model if model else "",
                "  [family %s]" % axis if axis else "",
                "  [%s]" % unread if unread else "")

        def _read_lines(row, claims=False):
            """The row's advisory reads, then every read on a cancelled row
            it continues under that row's id (task/3081) — ONE list for the
            three places a row prints its reads, off this triage's snapshot.
            `claims` puts the row's hand-back claim lines (task/3540) between
            the two, where `lr show` prints them, so a claim of THIS row never
            sits under a FROM header naming another."""
            own = dispatches.advisory_read_lines(row)
            if claims:
                from . import handback_claims
                own = own + handback_claims.lines(row)
            return own + dispatches.superseded_read_lines(
                dispatches.superseded_reads(row, current))

        for row in sorted(selected, key=lambda r: str(r.get("ts") or "")):
            print(_triage_line(row))
            # THE BRIEF, AND ONLY ON A NAMED ROW. Storing the body and never
            # rendering it would be the same defect one layer down: the point of
            # persisting it is that the seat who INHERITS a rebound obligation
            # can read the instruction, and this verb is where a seat picks work
            # up. The `rest` gate is load-bearing — the bulk listing is a
            # one-line-per-row table by design and a multi-line brief under
            # every row would destroy it, so the brief appears exactly when a
            # human asked about specific ids.
            #
            # THREE STATES, THE SAME THREE `_brief_travel_note` prints, because
            # "no brief" and "brief unknown" are different facts and a reader
            # deciding whether to go ask the sender needs to know which.
            #
            # KNOWN-EMPTY PRINTS NOTHING, and that is a contract, not a taste:
            # `TriageAnswersEveryNamedIdTest.test_an_open_row_is_measured_
            # exactly_as_before` pins a named open row at EXACTLY ONE LINE, and
            # my first cut added a second one to every `dispatch add` row. It
            # was right to go red — an `add` row never had a DM, so its lane,
            # ref and note ARE the whole ask and a line saying "no brief" on the
            # commonest row in the ledger is pure noise. The two states worth a
            # line are the ones that change what the reader does next: here is
            # the instruction, or the instruction is UNKNOWN and you must ask.
            if not rest:
                continue
            # THE PRE-READ, WHEN ONE EXISTS. A council of cheap readers can
            # leave a candidate-findings file under the helm home for this
            # row, and a reading nobody is pointed at is a file nobody opens.
            # It prints under a NAMED row only, for the same reason the brief
            # does: the bulk listing is one line per row by design. NOTHING
            # prints when the file is absent — a row with no pre-read must
            # look exactly as it did, and the line is a pointer, never an
            # authority (a pre-read mints no verdict).
            try:
                from . import preread
                pre = preread.triage_line(str(row.get("id") or ""))
            except Exception:                            # noqa: BLE001
                pre = ""      # a pointer must never break the measured table
            if pre:
                print("  " + pre)
            # THE qwen27 FINDINGS NOTE (task/2960), for the reviewer who will
            # adjudicate it: named rows only, like the pre-read, and nothing
            # at all on a row that carries none.
            for line in _findings_lines(row):
                print("  " + line)
            # EVERY MODEL RUN'S ADVISORY READ (task/3081): named rows only,
            # and nothing at all on a row that carries none. The row stays
            # owed, and the lines say so. AND THE HAND-BACK'S CLAIMS, each
            # CHECKED against a receipt for this row's own tree or UNBOUND
            # with its reason (task/3540): named review rows only, and
            # nothing on a brief that claims nothing.
            for line in _read_lines(row, claims=True):
                print("  " + line)
            # BY REFERENCE WHEN THE ROW HAS ONE. `body_of` reads the row's
            # BOUNDED copy; `brief_of` reads the file the row names and proves
            # it, falling back to that bounded copy with a LOUD line when it
            # cannot. This is the surface the defect was measured on — a seat
            # picking work up read a brief cut inside its own coverage list and
            # had no way to know — so it is the surface that most owes the
            # whole text.
            brief, absent, problem = dispatches.brief_of(row)
            if problem:
                print("  " + problem)
            if brief:
                is_ref_read = not problem and row.get("brief_ref") is not None
                is_cut = dispatches.brief_was_cut(row) or dispatches.BODY_TRUNCATED_MARK in brief
                if is_ref_read:
                    label = "as sent, stored whole, read by reference"
                elif is_cut:
                    kept = dispatches._cut_kept_bytes(brief) or len(brief.encode("utf-8"))
                    sent = dispatches._cut_sent_bytes(brief) or row.get("brief_bytes")
                    if sent and kept:
                        label = "TRUNCATED bounded copy, %d of %d bytes stored on the row" % (kept, sent)
                    else:
                        label = "TRUNCATED bounded copy stored on the row"
                else:
                    label = "as sent, stored on the row"
                print("  BRIEF (%s):" % label)
                # Only the NOTICE carries the `<id>` placeholder helm wrote; the
                # brief above it is the sender's own words, and briefs quote
                # `<id>` as an instruction, so the substitution never reaches it.
                head, mark, notice = brief.rpartition(dispatches.BODY_TRUNCATED_MARK)
                if mark:
                    brief = head + mark + notice.replace(
                        "<id>", str(row.get("id") or "")[:12])
                for line in brief.splitlines():
                    print("    " + line)
            elif absent == dispatches.BODY_UNRECORDED:
                print("  BRIEF: UNKNOWN — this row predates body storage, so "
                      "its brief is unrecoverable here. Ask @%s."
                      % (row.get("sender") or "its sender"))
        if unplaceable:
            # AFTER THIS PROJECT'S WORK, AND SEPARATED FROM IT. The bulk
            # listing is what a seat reads to pick work up, and these rows are
            # pickable — they name a seat and they are open — but they are not
            # this project's, because no event in the ledger says whose they
            # are. Printing them inside the table above is the claim that
            # produced task/2468.
            print("\n" + _unplaceable_heading(unplaceable, scope_label))
            for row in sorted(unplaceable, key=lambda r: str(r.get("ts") or "")):
                print(_triage_line(row))
        # CURED FIXES RIDE TRIAGE, NOT A FLAG. These rows are CLOSED, so
        # `owed()` above cannot see them by construction — a FIX verdict means
        # the author owes, and the moment the author cures without
        # re-dispatching the row is finished work with no reader. Putting them
        # behind `--cured` would fix nothing: the defect is that NOBODY LOOKS,
        # and a flag you have to already suspect is a flag nobody types.
        # Printed AFTER the owed frontier because owed work is due now and
        # this is due to somebody else. (Computed BEFORE the named-id answers
        # below, because a cured row's CURED line is already its answer and
        # must not be doubled by a second line saying the same row's state.)
        if not rest and scope_keep is not None:
            # THE CURE CENSUS IS SCOPED TOO, AND ITS SET-ASIDE IS COUNTED
            # (round two, finding 6). The owed frontier above was narrowed to
            # this project and then the WHOLE snapshot was handed to
            # `cured_by_repo`, whose own default is every eligible FIX row — so
            # the one surface that had just declared a project offered another
            # project's cures back under the same header, with no line saying
            # how many or how to see them all. The whole ledger still reaches
            # `cured_by_repo` for the per-repository checkout resolution it does
            # inside; only the ELIGIBLE population is scoped, through the one
            # predicate the count is taken with.
            cure_pool = dispatches.cure_eligible(current)
            in_scope = [str(row.get("id") or "") for row in cure_pool
                        if scope_keep(row)]
            aside_cures = len(cure_pool) - len(in_scope)
            cured, cure_problems, cure_facts = dispatches.cured_by_repo(
                current, ids=sorted(in_scope))
            if aside_cures:
                print("helm dispatch triage: %d cure candidate%s outside "
                      "project %s not measured — `--all-projects` measures "
                      "them here"
                      % (aside_cures, "" if aside_cures == 1 else "s",
                         scope_label), file=sys.stderr)
        elif not rest:
            cured, cure_problems, cure_facts = dispatches.cured_by_repo(current)
        elif named:
            cured, cure_problems, cure_facts = dispatches.cured_by_repo(
                current, ids=sorted(named))
        else:
            # every named token was refused above; scanning the whole ledger
            # for cures nobody named would answer a question nobody asked
            cured, cure_problems = [], []
            cure_facts = {"eligible": 0, "scanned_repos": 0,
                          "blind_repos": 0, "blind_rows": 0, "ambiguous": 0}
        if rest:
            spoken = {r["id"] for r in selected}
            spoken.update(row["id"] for row, _where in cured or ())
            for rid, row in named.items():
                if rid in spoken:
                    continue
                label, reason = dispatches.untriaged(row, current)
                print("%s %-7s %-12s %-28s %s" % (
                    row["id"][:12], label, dispatches._recipient_label(row),
                    str(row.get("lane") or "-")[:28], reason))
                note = patch_note(row)
                if note:
                    print(note)
                # A SKIPPED ROW'S FINDINGS PASS TOO (task/3382): the pass
                # stops on a row that can never take its note and says so
                # here, and a verdicted row is one; a held or carried row
                # prints the note it took.
                for line in _findings_lines(row):
                    print("  " + line)
                # A SKIPPED ROW'S ADVISORY READS TOO (task/3081): a verdicted
                # or carried row is where a model run's read was last looked
                # for and not found — the live case was a CARRIED row.
                # AND ITS HAND-BACK CLAIMS (task/3540): a verdicted review
                # row's reader still asks what its brief proved.
                for line in _read_lines(row, claims=True):
                    print("  " + line)
        blind = cure_facts["blind_rows"]
        ambiguous = cure_facts["ambiguous"]
        if cure_problems:
            detail = "; ".join(str(problem) for problem in cure_problems)
            if cure_facts["scanned_repos"] == 0 and cure_facts["eligible"]:
                print("helm dispatch triage: cured-fix scan UNAVAILABLE (%s) — "
                      "this says nothing about whether any cures exist" % detail,
                      file=sys.stderr)
            else:
                print("helm dispatch triage: cured-fix scan PARTIAL (%s) — %d "
                      "row(s) are UNKNOWN; measured cures still follow"
                      % (detail, blind + ambiguous), file=sys.stderr)
        total = len(cured) + blind + ambiguous
        if total and cure_facts["scanned_repos"]:
            print("\nCURE AWAITING REVIEW — %d row(s): %d measured, %d "
                  "ambiguous, %d repository-blind." % (
                      total, len(cured), ambiguous, blind))
            if cured:
                print("These measured rows were cured — by the lane owner, or "
                      "by the reviewer whose patch tip the row records — and "
                      "never re-dispatched; nobody is waiting on them.")
            for row, (branch, tip, ahead) in cured:
                print("%s %-7s %-12s %-28s cure at %s +%d on %s — re-dispatch "
                      "or `helm dispatch rebind`" % (
                          row["id"][:12], "CURED", dispatches._recipient_label(row),
                          str(row.get("lane") or "-")[:28], tip[:12], ahead,
                          branch))
                note = patch_note(row)
                if note:
                    print(note)
                # A CURED ROW'S ADVISORY READS TOO (task/3081): a FIX-verdicted
                # row whose author cured on the lane is listed HERE and not in
                # the skipped loop above, and it is the one verdicted shape
                # that printed no read while `lr show` printed it.
                for line in _read_lines(row):
                    print("  " + line)
                # AND ITS FINDINGS PASS, under a NAMED row only, as every
                # other row prints it (task/3382).
                for line in (_findings_lines(row) if rest else ()):
                    print("  " + line)
        # rc 2, the same code every verb answers a malformed/unknown argument
        # with — AFTER the loops above, so one typo'd token never suppresses
        # the real rows named beside it.
        return 2 if unresolved else 0
    if verb == "briefs":
        # THE CENSUS OF WHAT THE CAP ALREADY ATE. Read-only, and it exists
        # because this cure is NOT retroactive: a row sent before the brief
        # file store kept a prefix and the tail went out in the DM only. The
        # integrator's move is to RE-SEND those rows, and nobody re-sends a
        # population nobody can enumerate — which is how a measured defect
        # comes to be "fixed" while every row it already damaged stays damaged.
        #
        # THE PREDICATE EXCLUDES ROWS STORED BY REFERENCE ON PURPOSE. A row may
        # carry the truncation mark in its bounded copy AND a whole brief on
        # disk; that row has lost nothing and listing it would bury the rows
        # that did (see `brief_was_cut`).
        unknown = [a for a in rest if a != "--cut"]
        if unknown:
            print("helm dispatch: briefs accepts only [--cut]: %s"
                  % " ".join(unknown), file=sys.stderr)
            return 2
        current, unavailable = dispatches.snapshot()
        if unavailable:
            print("helm dispatch: ledger unavailable; the cut-brief census is "
                  "UNKNOWN: %s" % unavailable, file=sys.stderr)
            return 1
        rows, unavailable = dispatches.brief_census(current)
        # --cut IS THE TABLE ALONE, so the census can be piped; bare `briefs`
        # leads with the DENOMINATOR. A count of damaged rows with no
        # population beside it cannot be read: four cut rows out of five open
        # briefs and four out of four hundred call for different actions, and
        # the table is identical in both.
        if "--cut" not in rest:
            whole = byref = 0
            for row in (current or {}).values():
                if str(row.get("status") or "") != "open":
                    continue
                if row.get("brief_ref") is not None:
                    byref += 1
                elif (dispatches.body_of(row)[0] or "") and not dispatches.brief_was_cut(row):
                    whole += 1
            print("OPEN briefs: %d stored WHOLE by reference, %d whole on "
                  "their row, %d CUT with no whole copy."
                  % (byref, whole, len(rows)))
        if not rows:
            # NOT A SILENT ZERO. An empty census and an unread one look
            # identical at a terminal unless the empty one says which it is.
            print("no OPEN row's brief survives only as a cut copy — every "
                  "open brief is either whole on its row or stored whole by "
                  "reference.")
            return 0
        print("%-14s %-16s %-26s %9s %9s" % (
            "ROW", "RECIPIENT", "LANE", "STORED", "SENT"))
        for r in rows:
            print("%-14s %-16s %-26s %9d %9s" % (
                r["id"][:12], r["recipient"][:16], r["lane"][:26],
                r["stored_bytes"],
                r["sent_bytes"] if r["sent_bytes"] is not None else "UNKNOWN"))
        print("%d OPEN row(s) carry a CUT brief and no whole copy. The tail "
              "went out in the original DM only; re-send each one — the brief "
              "is stored whole from now on." % len(rows))
        return 0
    if verb == "list":
        # BOUND BEFORE THE READ, not after it. The instant must not POSTDATE
        # the rows it stamps: snapshot() used to run first and read_now was
        # taken below it, so a row that appeared between the two was absent
        # from a listing whose stamp said it was read later than that row
        # existed. The stamp then makes a stronger claim than the read
        # supports — an absence claim about a moment the read never saw.
        # (a review of the cure that bound this instant in the first
        # place: the ORDER was the residual half.)
        read_now = time.time()
        current, unavailable = dispatches.snapshot()
        if unavailable:
            print("helm dispatch: ledger unavailable; obligations UNKNOWN: %s"
                  % unavailable, file=sys.stderr)
            return 1
        # NAME THE OFFENDING TOKEN, and keep the two rejections DISTINCT.
        # Both used to print the bare USAGE, so `list --limit 10` answered with
        # a 900-character wall that never contained the string "--limit" and the
        # reader had to diff their command against the whole grammar to find it.
        # `send` already names its unknown option; only this path did not.
        # The two causes are also different mistakes with different repairs —
        # a typo/absent flag versus two selectors that cannot both hold — so
        # collapsing them into one message loses the repair, not just the token.
        bare, to_seat, scope_err = list_scope_argv(rest)
        if scope_err:
            print("helm dispatch list: %s" % scope_err, file=sys.stderr)
            return 2
        flags = set(bare)
        unknown = sorted(flags - {"--open", "--overdue", "--held", "--json",
                                  "--mine", "--issued", "--orphaned",
                                  "--source-clean", "--no-holder",
                                  "--all-projects"})
        if unknown:
            print("helm dispatch list: unknown option%s %s (accepts --open, "
                  "--overdue, --held, --source-clean, --no-holder, --mine, "
                  "--issued, --orphaned, --all-projects, --to SEAT, --json)\n%s"
                  % ("" if len(unknown) == 1 else "s", " ".join(unknown),
                     synopsis("list")),
                  file=sys.stderr)
            return 2
        if len(flags & {"--open", "--overdue", "--held"}) > 1:
            print("helm dispatch list: --open and --overdue select different "
                  "rows and cannot be combined — pass one, or neither for all "
                  "rows", file=sys.stderr)
            return 2
        # --source-clean IS A NARROWING, NOT A FOURTH STATE, for the same
        # reason --overdue is a subset of --open: a source-clean row is HELD by
        # construction, so the flag selects within that state rather than
        # beside it. Combined with --held it means the same thing, and combined
        # with --open it would name an empty set, which is a question the
        # caller cannot have meant.
        if flags & {"--source-clean", "--no-holder"} \
                and flags & {"--open", "--overdue"}:
            print("helm dispatch list: %s selects HELD rows — a "
                  "source-clean row is held by construction, so it cannot be "
                  "combined with --open or --overdue"
                  % ("--no-holder" if "--no-holder" in flags
                     else "--source-clean"), file=sys.stderr)
            return 2
        # A THIRD DISTINCT REJECTION, for the same reason the two above are
        # kept apart: --mine and --to are both recipient filters, and honouring
        # either one silently would answer a question the caller did not ask.
        if "--mine" in flags and to_seat is not None:
            print("helm dispatch list: --mine and --to name two different "
                  "recipients and cannot be combined — pass --mine for your "
                  "own rows, or --to %s for that seat's" % to_seat,
                  file=sys.stderr)
            return 2
        # WHOSE ROWS — the operand this verb never had (task/1007). The
        # resume-turn hook fires on EVERY compaction and tells every seat its
        # live obligations are ONLY the OPEN rows NAMING IT, and no flag could
        # express "naming me": measured from a seat with zero obligations,
        # `list --open` returned two rows naming two OTHER seats and that seat
        # adopted one of them. Both failure directions — adopting a stranger's
        # row, and losing your own among many — are the same missing operand,
        # and they arrive at the reader with the least context to catch it.
        #
        # ...AND `--mine` ALONE STILL ASSERTED A COMPLETENESS IT DID NOT HAVE
        # (measured on one author seat). `--mine` is RECIPIENT-scoped, so for
        # an AUTHOR seat the hook's sentence is FALSE: that seat held an OPEN
        # row it had SENT, two hours past deadline, that only it was positioned
        # to chase — and `list --open --mine` printed "no matching rows naming
        # @<seat>", which reads as "you are free" to a seat holding live work,
        # at the exact moment it has lost the context that would
        # have reminded it. The issuer's obligation is not a definitional
        # quibble: the integrator ruled the same night that what a sender owes
        # is the DELIVERY LEG — that the recipient knows the row exists and
        # what it needs — so a row sent but never delivered, or delivered to a
        # seat that went quiet, is the SENDER's to chase.
        #
        # THE CURE IS AN ACTUATOR, NOT A WIDER `--mine`. Relaxing the recipient
        # scoping, or sending the hook back to a bare `list --open`, reopens
        # task/1007 exactly. `--issued` is a SECOND filter on the OTHER axis,
        # resolved through the SAME identity door and failing loud the same
        # way, and the two TOGETHER are a UNION.
        #
        # THE UNION IS DELIBERATE, over naming two commands in the hook. The
        # defect's mechanism is a reassuring answer that STOPS the reader; two
        # commands rebuild it at execution time (run the first, read "no
        # matching rows", stop) — and this reader is by construction the one
        # with the least context to notice a second sentence. Both halves bind
        # to the SAME resolved identity, so the union can never show a third
        # seat's row: the adoption failure is structurally unreachable through
        # it. (The INTERSECTION reading — "rows I sent to myself" — would be a
        # confidently-empty trap, the worst of the three.) `_mine_or_unprovable`
        # states the direction this module already chose: over-showing is the
        # status quo; under-showing is the harm. The one hazard the union does
        # add — reading a row you SENT as one you must DO, duplicate work, the
        # mirror of adoption — is closed by MARKING those rows below, not by
        # hiding them.
        want_mine, want_issued = "--mine" in flags, "--issued" in flags
        # RESOLVED BEFORE THE ROWS ARE READ, like every other operand here, so
        # one directory answers one way for the whole listing.
        scope_repo, scope_project = None, None
        if "--all-projects" not in flags:
            scope_repo, scope_project, _scope_why = dispatches.cwd_scope()
        me, scope, scope_shown = None, None, ""
        if want_mine or want_issued:
            asked = ("filter this listing to the rows you hold AND the rows "
                     "you SENT" if want_mine and want_issued else
                     "filter this listing to your own rows" if want_mine else
                     "filter this listing to the rows you SENT")
            me, ident_err = dispatches._acting_author(asked)
            # THE REFUSAL NAMES THE FLAGS ACTUALLY TYPED, so a caller who asked
            # the issuer question is not told about a flag they did not use.
            given = " ".join(f for f in ("--mine", "--issued") if f in flags)
            if ident_err:
                # FAIL LOUD, PRINT NOTHING. A silent fall-back to the
                # unfiltered list IS the defect --mine exists to close: the
                # caller would read every seat's obligations as its own, which
                # is precisely the adoption this flag was built to stop. So the
                # unresolved identity is named, the repair is named, and zero
                # rows are printed. `--issued` inherits this without softening
                # it — an unresolved author is exactly as unable to say which
                # rows it SENT as which rows name it.
                print("helm dispatch list %s: %s" % (given, ident_err),
                      file=sys.stderr)
                print("helm dispatch list %s: NO ROWS PRINTED — this does "
                      "not fall back to the unfiltered list. A listing the "
                      "reader believes is 'mine' while it names every seat is "
                      "the exact failure this flag exists to close. Fix the "
                      "identity above, or ask about a named seat with "
                      "`--to SEAT`." % given, file=sys.stderr)
                return 1
        # `scope` IS THE `--to` AXIS AND NOTHING ELSE. The first cut of this
        # lane also fed `--mine` through it, "since scope IS me there" — and
        # that re-intersected the union back down to the recipient rows, so
        # `--open --mine --issued` printed NOTHING for the very seat and the
        # very row this lane exists for, while `--open --issued` printed it.
        # Measured, not reasoned: the identical command in the hook's own text.
        # An OR and an AND over the same field do not commute, and the seat
        # filter has ONE home now (the three arms below).
        if to_seat is not None:
            # THE SAME canonical resolution send/rebind route through
            # (_recipient_operand -> seats.resolve_recipient): @-strip,
            # casefold, exact-token, and the roster's own alias evidence. A
            # second matching rule here would drift from how the ledger ROUTES,
            # so a seat could be told it has no rows under a spelling that
            # delivery accepts.
            scope, ref_err = dispatches._recipient_operand(to_seat)
            if ref_err:
                print("helm dispatch list --to: %s" % ref_err, file=sys.stderr)
                return 2
        # The owed frontier, computed ONCE with a single successor index. Every
        # selector below and the overdue LABELLING both read it, so no two
        # surfaces can disagree about who owes what — which is the whole point
        # of owed() and was exactly what the raw-row selectors defeated. Doing
        # it per row also rebuilt the index each time, which is the quadratic
        # shape over a 1,300-row ledger that owed() exists to remove.
        owed_ids = {r.get("id") for r in dispatches.owed(current)}
        # ONE INSTANT FOR THE WHOLE LISTING, bound ABOVE — before snapshot() —
        # and threaded through the selector chain. This surface used to sample
        # the clock THREE times — once inside the --overdue arm, once for the
        # header stamp, once for the per-row ages — so the FILTER and the AGES
        # it rendered were measured against different clocks, and a row could
        # be selected as overdue then printed with an age that did not agree.
        # `_read_stamp` already took a `now` for exactly this.
        selected = sorted(current.values(),
                          key=lambda r: (str(r.get("ts") or ""), r["id"]))
        from . import seats as _seats
        if scope is not None:
            scope_shown = _seats._seat_label(
                getattr(scope, "display", None) or scope)
        me_shown = _seats._seat_label(me) if me is not None else ""

        def _held_by_me(row):
            return _seats.recipient_matches(row.get("recipient"), me)

        # WHOSE ROWS, BEFORE THE STATUS SELECTORS, so every one of them
        # composes with it — `--open --mine --issued` is the combination the
        # resume-turn hook names, and a seat filter bolted onto only one arm
        # would answer `--overdue --mine` for the whole fleet. `owed_ids` above
        # stays a WHOLE-LEDGER frontier on purpose: whether a row is carried by
        # a live successor is a fact about the ledger, not about who is reading
        # it.
        # NARROWING AND DESCRIBING ARE ONE ACT FROM HERE DOWN.
        searched = []

        def _narrow(rows, clause, keep):
            """Drop rows AND state which question was asked, in one step.

            THE NOTE IS A BYPRODUCT OF FILTERING, NEVER A SECOND DESCRIPTION
            OF IT. This surface has printed an absence that was true of a
            NARROWER question than the reader was asking three times over:
            first it named no seat, then no direction, then no STATE. Each
            cure added one more clause to a note written BESIDE the filters,
            and beside-ness is the defect rather than any one missing clause.
            The proof is structural rather than a matter of care: the note
            was BUILT BEFORE the state selectors ran, so it could not have
            named them whatever clauses it held. Its `--to` spelling shows the
            same shape one step earlier — `--issued --to SEAT` is TWO
            narrowings and the note fused them into one phrase, so the count
            of axes searched could not be read off the sentence that claimed
            to list them.

            So the door is the fix. A filter that does not describe itself
            cannot be written here, and a fourth clause cannot be forgotten
            because there is nowhere to forget it from.

            AND THE CLAUSE CARRIES ITS SIZE, because naming the question a
            filter asked tells the reader WHICH axis narrowed and cannot tell
            them whether that axis was EMPTY. A seat holding two HELD rows
            reads this surface printing
            "OPEN only -- a HELD, cancelled or verdicted row is NOT open and
            is outside this listing" WORD FOR WORD as it prints it when zero
            are held, so the seat read itself clear against its own debt. The
            clause was already honest; it was simply the same sentence in both
            worlds.

            THE COUNT COMES OFF THE SAME SNAPSHOT, never a second read.
            `rows` is what this filter was handed, so the subtraction is
            bound to the ONE instant this listing measured -- the rule the
            --open/--overdue neighbours already state about lateness, applied
            to the number that says how much was set aside."""
            out = [r for r in rows if keep(r)]
            dropped = len(rows) - len(out)
            searched.append(clause + (" (%d set aside)" % dropped
                                      if dropped else ""))
            return out

        if want_mine and want_issued:
            # THE UNION, and it is the ONLY arm that ORs. Every other selector
            # in this chain narrows; this one is the whole point of the pair.
            selected = _narrow(selected, "naming or sent by @%s" % me_shown,
                               lambda r: _held_by_me(r) or dispatches._sent_by(r, me))
        elif want_mine:
            selected = _narrow(selected, "naming @%s" % me_shown, _held_by_me)
        elif want_issued:
            selected = _narrow(selected, "sent by @%s" % me_shown,
                               lambda r: dispatches._sent_by(r, me))
        # WHICH REPOSITORY'S ROWS (task/2437). This ledger holds rows for every
        # registered project now, and 98% of the live ledger being one project's
        # is what kept an unscoped listing readable — 3775 of 3855 rows. So the
        # listing scopes to the repository the caller is STANDING IN and says so
        # in the same clause it filters with, which is how the absence line and
        # the header both carry it.
        #
        # AN IDENTITY-SCOPED LISTING IS NEVER NARROWED BY REPOSITORY, and that
        # is the whole of the resume-turn hook's protection. `--mine`/`--issued`
        # answer "what do I owe" — a row that NAMES you is yours wherever its
        # code lives, and hiding it because you are standing in another checkout
        # rebuilds task/1007 on a new axis: the compacted reader with the least
        # context reads "no matching rows" as "you are free" while holding live
        # work. The union already guarantees every surviving row is that seat's,
        # so skipping the filter here costs nothing and closes that direction.
        #
        # `--to SEAT` IS AN IDENTITY QUESTION AND TAKES THE SAME EXEMPTION
        # (round two, finding 3). It asks "what does that seat owe", which is
        # the same question `--mine` asks about the caller, and the answer must
        # not depend on which checkout the asker is standing in. The proof this
        # was load-bearing rather than tidy: the `--mine` identity REFUSAL a few
        # lines up sends the caller to `--to SEAT` as its recovery, so the one
        # flag the shipped text recommends was the one narrowing hid work behind.
        #
        # AND THE POLICY IS THE CLASSIFIER'S (round two, finding 2) — a sibling
        # repository of this project stays, and a legacy row with no `repo_id`
        # is never exiled by a filter naming a project it cannot be placed in.
        # HOW MANY ROWS THE UNASKED-FOR AXIS TOOK. Every other clause in this
        # chain answers a flag the caller TYPED; the project narrowing is the
        # one nobody asked for, so it is the one a machine reader has to be
        # told about — and only when it actually held something back.
        #
        # AND UNPLACEABLE IS NOT THIS PROJECT'S (task/2468): the round-two
        # filter returned ONE BOOLEAN for three different reasons, so "kept"
        # and "this project's" became the same word and the header claimed the
        # second while meaning the first. An unplaceable row leaves the PROJECT
        # rows for a bucket of its own rather than being hidden.
        scope_withheld = 0
        placed, unplaceable = None, []
        if scope_repo is not None and not (want_mine or want_issued
                                           or to_seat is not None):
            classify = _scope_class(scope_repo, scope_project)
            placed = {r["id"]: classify(r) for r in selected}
            shown_project = scope_project or scope_repo
            before = len(selected)
            selected = _narrow(
                selected,
                "in project %s (--all-projects lists every project's rows)"
                % shown_project,
                lambda r: placed[r["id"]] != "foreign")
            scope_withheld = before - len(selected)
        if "--orphaned" in flags:
            # THE ONE SELECTOR THAT IS NOT IDENTITY-SCOPED, ON PURPOSE. Every
            # other narrowing here answers WHOSE rows these are; this one
            # answers which rows are NOBODY'S, and by construction no seat can
            # ask that about itself — the recipient does not exist to ask. So
            # it is deliberately visible from any seat, and it filters rather
            # than adopting: the listing is a MEASUREMENT of the orphan set,
            # which is what makes disposition a decision instead of a grep.
            reach_sel = dispatches.recipient_reach(dispatches._recipient_key(r) for r in selected)
            selected = _narrow(
                selected, "whose recipient has no roster row",
                lambda r: reach_sel.get(dispatches._recipient_key(r)) == "ABSENT")
        if scope is not None:
            # The `--to` axis, applied SEPARATELY so `--issued --to SEAT`
            # intersects honestly ("what did I send to that seat") instead of
            # one filter quietly replacing the other. It gets its OWN clause
            # for the same reason it gets its own filter: it COMPOSES with
            # `--mine` instead of replacing it, so a note that folded it into
            # the direction clause left `--mine --to SEAT` narrowed on an axis
            # the reader was never told about.
            selected = _narrow(
                selected, "addressed to @%s" % scope_shown,
                lambda r: _seats.recipient_matches(r.get("recipient"), scope))
        if "--open" in flags:
            # A SUPERSEDED PARENT IS NOT ACTIONABLE (441c4491). It stays OPEN on
            # purpose — a BUILD parent closes through `landed`/`discharged` on
            # its successor's PROOF, and cancelling it here would destroy that
            # door (measured: 18 close-ladder tests). So the row keeps its
            # status and this SURFACE reads the annotation instead.
            #
            # FILTERED HERE, NOT IN `_open`: that predicate is shared, and a
            # superseded parent is still genuinely open to every door that
            # needs it. Only the ACTIONABLE-WORK question has a different
            # answer, and only this surface is asking it.
            # ...and only while that successor is ALIVE: a parent whose child
            # was cancelled (an aborted rebind) is owed again, and hiding it
            # would replace duplicate debt with hidden debt.
            #
            # AND THE CLAUSE NAMES BOTH AXES, because this filter narrows on
            # two, and a clause confessing to one under-describes it.
            # `owed_ids` excludes a row for a STATUS reason (held, cancelled,
            # verdicted) or for a CHAIN reason (a successor holds it, or a
            # discharging one ended it), and the second reason is the larger
            # one by an order of magnitude — measured on the live ledger, 180
            # of the 199 open rows sit outside this listing for chain reasons
            # and none for status. A reader told only about status reads an
            # empty answer as "no such row exists" instead of "every one of
            # them is accounted for somewhere else", which is a different fact
            # and a different next move.
            # `--to SEAT` ALSO NAMES WHAT IT SET ASIDE BY KIND (task/3881):
            # `seat reassign` counts a held row and a carried one as that
            # seat's holdings, so a bare count left the two answers apart.
            selected = _narrow(
                selected,
                "OPEN AND STILL OWED only — a HELD, cancelled or verdicted "
                "row is not open, and an open row whose chain a successor "
                "holds or a discharging one ended is accounted for "
                "elsewhere; both are outside this listing"
                + (_holdings_set_aside(selected, owed_ids)
                   if to_seat is not None else ""),
                lambda r: r.get("id") in owed_ids)
        elif "--overdue" in flags:
            # OWED FIRST, THEN LATE. Filtering raw rows by age alone resurrected
            # an already-carried parent as pickup work: --open showed the child
            # and --overdue showed the PARENT, from one snapshot, in the same
            # breath. Overdue is a property of a debt, so a row that owes
            # nothing cannot be late for it. Late is measured against the ONE
            # instant this listing bound, not a fresh sample taken here.
            selected = _narrow(
                selected,
                "OVERDUE only — a row that owes nothing cannot be late for it, "
                "so this is a subset of OPEN and not a second state",
                lambda r: r.get("id") in owed_ids and dispatches._is_overdue(r, read_now))
        elif flags & {"--source-clean", "--no-holder"}:
            # THE INTEGRATOR'S LISTING. These are the rows whose delta reads
            # clean and which cannot carry an approve, because an approve
            # binds a verified whole-suite token that only the land gate on
            # the rebased tree produces. Without this narrowing they are
            # ordinary holds carrying the claim in free prose, which no
            # surface can select on.
            from . import query
            selected = _narrow(
                selected,
                "SOURCE-CLEAN HOLDS only — held rows whose reviewer read the "
                "delta clean and owe nothing but the integrator's land gate",
                lambda r: query.query_is_held(r) and r.get("source_clean_tip"))
            if "--no-holder" in flags:
                # THE BACKLOG `helm train` COUNTS ON ONE LINE (the author's
                # ruling 6, round 4): the held source-clean rows whose hold
                # records NO HOLDER, which no land closes and no train takes
                # until each recipient re-holds. Selected by the holder
                # rung's own kind, never by a second reading of the stamp.
                # It is a SUPERSET of the train's count: the train asks the
                # discharge rung first, so a NO HOLDER row that rung refuses
                # gets its own EXCLUDED line there and is listed here too.
                from . import landreq
                selected = _narrow(
                    selected,
                    "NO HOLDER only — source-clean holds that record no "
                    "holder, each its recipient's to re-hold",
                    lambda r: getattr(landreq.source_clean_holder_error(r),
                                      "kind", None)
                    == landreq.SourceCleanRefusal.NO_HOLDER)
        elif "--held" in flags:
            # THROUGH THE CANONICAL PREDICATE, not the raw status word.
            # Retirement preserves `status` and only adds `retired_admin`, so
            # `status == "held"` still matched rows the retire door had just
            # cleared — the listing a human is pointed at to inspect HELD
            # candidates offered back work nobody can act on. query_is_held
            # carries the retired rung (and normalises case/whitespace, which
            # the raw compare did not).
            from . import query
            selected = _narrow(
                selected,
                "HELD only — an OPEN row is outside this listing",
                query.query_is_held)
        # The projection runs AFTER the whole selector chain, never inside it:
        # trunk added it beside --open/--overdue and this lane added --held as
        # another arm, so a resolution that kept only one side would either
        # drop the new filter or leave --held rows unprojected — the same
        # verdict polarity every other arm gets.
        selected = dispatches.with_verdict_projections(selected)
        # THE UNKNOWN BUCKET IS SPLIT OFF LAST, AFTER EVERY SELECTOR AND THE
        # PROJECTION (task/2468). Taking it beside the project narrowing put the
        # split BEFORE `--open`/`--overdue`/`--held`/`--orphaned`, so a bucket
        # lifted out early would have printed rows those flags had just excluded
        # — a listing answering `--held` with an open row under a second
        # heading. Whatever survived the chain is what gets bucketed, and these
        # rows carry the same verdict projection as the rows above them.
        if placed is not None:
            unplaceable = [r for r in selected
                           if placed[r["id"]]
                           in _unplaceable_classes(scope_project)]
            if unplaceable:
                # A SECOND CLAUSE, AND ONLY WHEN IT MOVED SOMETHING. `_narrow`'s
                # law is that filtering and describing are one act, so rows
                # leaving the project table owe the reader the sentence saying
                # where they went — but a clause appended on every listing in
                # every checkout would put a paragraph about a bucket nobody has
                # in front of every reader.
                #
                # AND THE CLAUSE NAMES WHERE THOSE ROWS WENT ON *THIS* SURFACE
                # (round two, finding 1). "in its own bucket below" is true of
                # the text listing and false of `--json`, which has no headings
                # and no below — it carries them in the SAME array under a
                # `scope_class` field. This clause is re-read verbatim into
                # `--json`'s stderr disclosure, so one wording for both surfaces
                # is one of them lying to its reader.
                #
                # AND THE CLAUSE STATES THE LOOKUP, NEVER A VERDICT ON
                # MEMBERSHIP (round three). "this registry can place is in NO
                # project" would be a claim about the world, asserted from a
                # `None` that `project_for_cwd` also returns for a registry it
                # could not read — so the clause states the failed LOOKUP
                # instead. `_unplaceable_fact` is that sentence's ONE source and
                # `--json`'s accounting reads the same helper, so the corrected
                # heading below can no longer be contradicted by the clause
                # above it on either surface. On `--json` the qualification
                # therefore appears twice in one stderr line — once here and
                # once in `_json_scope_accounting` — and that is the deliberate
                # trade: dropping it from one of the two is how the surfaces
                # came to hold two wordings of one fact.
                _noun, _why = _unplaceable_fact(scope_project or scope_repo)
                selected = _narrow(
                    selected,
                    "PLACED in project %s — a row %s is %s: %s"
                    % (scope_project or scope_repo, _noun,
                       "RETAINED in this array under its own `scope_class` "
                       "rather than counted inside this one"
                       if "--json" in flags else
                       "listed in its own bucket below rather than inside "
                       "this one", _why),
                    lambda r: placed[r["id"]]
                    not in _unplaceable_classes(scope_project))
        # WHAT THIS LISTING ACTUALLY SEARCHED, in the reader's words, and it
        # is JOINED AFTER THE WHOLE CHAIN — the note the state selectors
        # silently escaped was assembled before they ran. Printed by BOTH the
        # populated header and the absence line: those two are the same claim
        # about the same read, and a scope shown on one but not the other is
        # how "no matching rows" came to mean something narrower than it said.
        scope_note = (" " + ", ".join(searched)) if searched else ""
        if "--json" in flags:
            # THE BRIEF DOES NOT RIDE THE LISTING. `--json` dumps whole rows,
            # and this lane put `message_body` ON the row — so a verb that used
            # to emit metadata started emitting the full text of every brief
            # the ledger holds, including CLOSED rows, to anyone who can run
            # it. The listing verb predates the feature; the BOUNDARY is the
            # feature's to owe, and shipping the field without it would make
            # every historical brief readable by a command written when there
            # was nothing to read.
            #
            # The body stays reachable per-row through `dispatch triage <id>`,
            # which is scoped to one obligation the caller names. A count is
            # published in its place so the listing still says a brief EXISTS
            # — absence and privacy are different facts and this must not
            # collapse them.
            #
            # AND THE SCOPE IS DISCLOSED HERE TOO (round two, finding 4). This
            # return sits ABOVE both surfaces that print `scope_note`, so the
            # one caller who cannot see the narrowing is the one reading
            # MACHINE output: the array is one project's rows while nothing in
            # the output says so, and the idle-dispatch watchdog's own recovery
            # text sends a seat that cannot find its row to exactly this flag. The ARRAY stays byte-compatible — the disclosure goes to
            # stderr where it cannot enter what a script parses — and it names
            # the escapes, because a narrowing a reader cannot widen is the same
            # dead end the write door's refusal was.
            #
            # ONLY WHEN THE PROJECT AXIS ACTUALLY HELD SOMETHING BACK (round
            # four). Gating this on `searched` made the line print on EVERY
            # `--json` call from inside any Git checkout, because the project
            # clause is appended whether it set aside one row or none — so a
            # verb whose documented stderr is EMPTY started writing a paragraph
            # to it on the happy path, and two arms that assert `(0, "")` on
            # this exact call went red. The disclosure exists to explain rows
            # that are MISSING from the array; when nothing was withheld there
            # is nothing missing to explain, and a warning printed in that case
            # is noise a script's error channel has to learn to ignore. Every
            # other clause in `searched` names a flag the caller typed, so the
            # count that decides is the project narrowing's own.
            #
            # AND IT ACCOUNTS FOR WITHHELD AND RETAINED SEPARATELY (round two,
            # finding 1): the unplaceable rows are IN this array under their own
            # `scope_class`, so calling them withheld sends a script looking for
            # rows it is already holding. `_json_scope_accounting` owns both
            # sentences; "NOT the whole ledger" is said only when a row is
            # actually missing from it.
            if scope_withheld or unplaceable:
                print("helm dispatch list --json: this array is the rows %s. "
                      "%s. `--mine`/`--issued` and `--to SEAT` are never "
                      "narrowed by project."
                      % (", ".join(searched),
                         ". ".join(_json_scope_accounting(
                             scope_withheld,
                             [placed[r["id"]] for r in unplaceable],
                             scope_project or scope_repo))),
                      file=sys.stderr)
            # THE MACHINE READER GETS THE BUCKET AS A FIELD (task/2468). The
            # human surfaces below print a heading; `--json` has no headings, so
            # a consumer could only ever have read an unplaceable row as one of
            # this project's — which is the defect, one layer down and harder to
            # see. Every row carries `scope_class` whenever the project axis ran,
            # so `scope_class == "local"` IS the project's rows and the
            # unplaceable ones are filterable rather than missing: dropping them
            # from the array instead would make the machine surface the one place
            # they are invisible, and an absence is what nobody re-checks.
            #
            # THE ARRAY STAYS AN ARRAY OF ROWS, because that is what every
            # existing consumer parses — a top-level object would have been the
            # tidier shape and a breaking change to a surface the idle-dispatch
            # watchdog reads. Stamped on `redact_bodies`' COPIES, never on the
            # projection: a read must not write, not even in memory a caller
            # shares.
            payload = dispatches.redact_bodies(selected + unplaceable)
            if placed is not None:
                for row, source in zip(payload, selected + unplaceable):
                    row["scope_class"] = placed[source["id"]]
            print(json.dumps(payload, ensure_ascii=False, indent=1))
            return 0
        if not selected and not unplaceable:
            # AN ABSENCE IS THE CLAIM MOST IN NEED OF AN INSTANT, and it was
            # the one line without one: the POPULATED header two lines below
            # has been stamped all along. "no matching rows" with no read time
            # cannot be told apart from a stale empty — the reader has nothing
            # to measure the nothing against.
            #
            # AND IT NAMES THE RECIPIENT IT IS AN ABSENCE FOR. A bare "no
            # matching rows" answered under --mine is indistinguishable from a
            # filter that matched nothing because it resolved the wrong seat —
            # and "you owe nothing" is the single most consequential sentence
            # this verb prints to a compacted reader.
            #
            # ...AND IT NAMES THE DIRECTION, NOT JUST THE SEAT. "no matching
            # rows naming @<seat>" was TRUE and still misread as "you owe nothing"
            # while that seat held an open row it had SENT: the sentence answered a
            # narrower question than the reader was asking. The scope note now
            # says which axes were actually searched, so an absence can only be
            # read as an absence of what was looked for.
            print("helm dispatch: no matching rows%s  (read %s)"
                  % (scope_note, dispatches._read_stamp(read_now)))
            return 0
        # EVERY ROW THIS LISTING PRINTS, both sections. The advisories below are
        # claims about what the reader is LOOKING AT — an absent recipient, an
        # unverifiable attest, a row needing check-in — so scoping them to the
        # project section would have printed an unplaceable row and swallowed
        # the footer that says what to do about it (task/2468).
        rendered = selected + unplaceable
        if selected:
            print("helm dispatch — %d logical row%s%s  "
                  "(verdict polarity: %s; attestation: %s; read %s)" % (
                      len(selected), "" if len(selected) == 1 else "s",
                      scope_note,
                      dispatches._source_label(dispatches.POLARITY_SOURCE),
                      dispatches._source_label(dispatches.ATTEST_SOURCE),
                      dispatches._read_stamp(read_now)))
        else:
            # THE PROJECT HAS NOTHING AND THE BUCKET HAS SOMETHING, which is a
            # real state on the live ledger: a checkout whose only matching rows
            # are unplaceable ones. The absence line is the same sentence the
            # early return prints, because the claim is the same claim — this
            # project owes nothing that was looked for — and it must not be
            # silently omitted just because something else follows it.
            print("helm dispatch: no matching rows%s  (read %s)"
                  % (scope_note, dispatches._read_stamp(read_now)))
        now = read_now
        # `rendered` already carries the one-read typed projection. Derive the
        # marker set from it rather than reading the sidecar a second time.
        unverifiable = frozenset(
            str(row["id"]) for row in rendered
            if row.get("attest_state") == "unverifiable")
        # ONE PROBE PER DISTINCT RECIPIENT in this listing, never per row.
        reach = dispatches.recipient_reach(dispatches._recipient_key(row) for row in rendered)
        # ONE INDEX FOR THE WHOLE LISTING, and ONE question asked of it.
        # task/2861: both legs below judged a row by its OWN state and never
        # asked whether its chain had already ended, so a row whose successor
        # had LANDED was tagged as debt. Measured on the live board, 107 of
        # 230 open/held rows are in that state. `dispatches.ended` is
        # `carrier`'s sibling and the only implementation of the question --
        # two legs each answering it their own way is how these drifted apart.
        _chain_index = dispatches._successor_index(current)
        # AND ONE CYCLE MAP FOR IT. `ended` and `carrier` build the map from
        # the index when none is passed, so a per-row call without it re-ran
        # the whole-graph pass once per row: quadratic in the ledger.
        _chain_cycles = dispatches._cycle_components(_chain_index)
        _ended_by = {}

        def _still_awaiting(r):
            """Has this row anything left to wait for? OPEN, or HELD on a
            named dependency. Everything else -- verdicted, cancelled,
            closed, retired -- is finished and owes no delivery leg."""
            return (dispatches._open(r)
                    or str(r.get("status") or "") == "held")

        def _ended(r):
            rid = str(r.get("id") or "")
            if rid not in _ended_by:
                _ended_by[rid] = dispatches.ended(r, current, _chain_index, _chain_cycles)
            return _ended_by[rid]

        def _handed_on(r):
            """Has a live successor TAKEN this row's obligation from it?

            `ended` asks whether anyone still owes; this asks whether the one
            who owes is THIS ROW. They are different questions and the chase
            marker needs the second: an open row that has been SUPERSEDED is
            retired by nothing, so every terminality question answers no,
            while the work has already moved to its successor.

            `carrier` is the only implementation of "who holds this now", and
            asking it here rather than re-deriving it is what keeps the two
            from drifting -- it already walks the SUCCESSOR SET rather than
            the frozen `superseded_by` pointer, and it already treats a
            cancelled, withdrawn or abandoned successor as a PASS-THROUGH,
            which is the case that makes the naive cure wrong: when the
            successor did not finish, this row really does still owe.

            NONE MEANS THIS ROW STILL HOLDS IT, and `carrier` resolves every
            unknown -- no successors, an unreadable row, a cycle -- to None.
            So this clause can only ever REMOVE a marker from a row whose
            successor was positively identified as live, and a ledger this
            cannot read keeps shouting. That direction is deliberate: the
            whole defect class here is rows that stopped being visible while
            still being owed."""
            return dispatches.carrier(r, current, _chain_index,
                                      _chain_cycles) is not None

        def _late(r):
            # A ROW THAT OWES NOTHING CANNOT BE LATE. That law was already
            # here by way of `owed_ids`; a finished chain is the second way a
            # row can owe nothing, and it was not asked about.
            return (r.get("id") in owed_ids and not _ended(r)
                    and dispatches._is_overdue(r, now))

        def _chase(r):
            """A row in THIS listing because I SENT it, not because I hold it.

            The one hazard the union adds, closed at the row rather than by
            hiding it: without this, a seat reading a mixed listing can do work
            it dispatched to someone else — duplicate work, the exact mirror of
            the adoption failure `--mine` exists to stop. Only ever true when
            `--issued` was asked for, so no existing listing grows a marker."""
            # ...AND NOT A ROW WHOSE CHAIN IS FINISHED. Chasing is a verb
            # about the DELIVERY LEG, and there is no leg left to own once a
            # successor has landed: the reader sent after it finds the work
            # already on trunk, which is exactly how a false close gets
            # written: measured at 542 minutes against a 45-minute deadline
            # with the successor LANDED. (The row id lives in task/2861; it
            # is a LEDGER row and not a commit, so citing it here would read
            # as a dead sha in a fresh clone -- the same reason `carrier`'s
            # docstring keeps its own repro ids out of the source.)
            # ASKED POSITIVELY, BECAUSE THE NEGATIVE LIST NEVER CLOSES.
            # The marker's own legend says it means the issuer owns the
            # DELIVERY LEG, and only a row still AWAITING something has a leg
            # to own. The first cut for this added `not _close_retired_by(r)`
            # to catch a closed row; measured over the rows one seat sent,
            # that list STILL marked 222 of 650 -- 131 verdicted and 80
            # cancelled, none of which owe anything -- because every way a
            # row can stop being live needs its own clause and the next one
            # is always missing. Asked the other way round it is 11.
            #
            # BOTH HALVES ARE REQUIRED and neither implies the other: a live
            # row whose CHAIN already ended owes nothing (task/2861, 12 of
            # these), and a row that is no longer live owes nothing whatever
            # its chain says (627). Dropping the chain half would reopen the
            # defect that cure closed.
            # THREE CLAUSES, AND THE THIRD IS A DIFFERENT QUESTION FROM THE
            # SECOND. `_ended` asks whether anyone still owes; `_handed_on`
            # asks whether the one who owes is this row. A chain that is still
            # live keeps every one of its open links marked under the first
            # two clauses alone, so ONE obligation is presented as several and
            # a seat's owed count grows with the length of its chains.
            # Measured on the live board before this clause: 18 rows marked,
            # only 2 of them chain tips, and 4 chains had BOTH a row and its
            # successor marked -- including one row that was both a marked row
            # and a marked row's successor.
            return (want_issued and not _held_by_me(r)
                    and _still_awaiting(r) and not _ended(r)
                    and not _handed_on(r))

        # THE RECIPIENT'S TURN (task/3118): one idle reading per recipient of
        # a row still owed (helm/seat_idle.py, one roster read). An owed row
        # whose seat has sat IDLE ten minutes on it reads IDLE-OWING; a late
        # row that is not says whether its seat is busy, briefly idle, or
        # unreadable, so an overdue listing can tell busy from stuck.
        from . import seat_idle
        idle = seat_idle.readings(
            {dispatches._recipient_key(r) for r in rendered
             if r.get("id") in owed_ids and not _ended(r)}, now=now)

        def _marked(line, r):
            """`line` with the recipient's idle marker on its FIRST line (a
            held rung's second line stays where _fmt put it)."""
            if r.get("id") not in owed_ids or _ended(r):
                return line
            mark = seat_idle.list_suffix(
                idle.get(dispatches._recipient_key(r)),
                dispatches._age_s(r, now), _late(r))
            head, sep, tail = line.partition("\n")
            return head + mark + sep + tail

        for row in selected:
            # BOTH PROPERTIES. `_late` (from trunk) is owed-aware, so a row
            # that owes nothing cannot be late; the `now` this lane made
            # REQUIRED and positional is the listing's one instant, so the age
            # printed and the marker beside it are measured against the same
            # clock. The marker can now disagree with neither the age nor the
            # debt.
            print(_marked(dispatches._fmt(row, now, _late(row), unverifiable,
                                          _chase(row), reach, _ended(row)), row))
        if unplaceable:
            # ITS OWN HEADING, BELOW THIS PROJECT'S WORK. The rows are printed
            # in the same format because they are the same kind of obligation;
            # what is different is that nothing in the ledger says whose they
            # are, and only a heading can say that.
            print("\n" + _unplaceable_heading(unplaceable,
                                              scope_project or scope_repo))
            for row in unplaceable:
                print(_marked(dispatches._fmt(row, now, _late(row), unverifiable,
                                              _chase(row), reach, _ended(row)),
                              row))
        if any(_late(r) for r in rendered):
            print("NEEDS CHECK-IN is advisory only; do not reassign on age alone")
        if any(seat_idle.is_owing(idle.get(dispatches._recipient_key(r)),
                                  dispatches._age_s(r, now))
               for r in rendered if r.get("id") in owed_ids and not _ended(r)):
            print("IDLE-OWING: the recipient's turn ended and nothing has run "
                  "since while it owed the row (its own hooks, "
                  "helm/seat_idle.py); `helm seat idle-dispatch` rings it, on "
                  "a backoff")
        if any(_chase(r) for r in rendered):
            # The marker is three words on a row addressed to someone else, and
            # the reader is usually a seat that has just lost its context. Say
            # what it obliges, because "yours" without a verb is exactly how a
            # sent row gets rebuilt by the seat that sent it.
            print("YOURS TO CHASE: you SENT these — the issuer owns the "
                  "DELIVERY LEG (that the recipient knows the row exists and "
                  "what it needs). Chase, rebind or cancel them; do NOT do the "
                  "work yourself.")
        gone = sorted({dispatches._recipient_key(r) for r in rendered
                       if reach.get(dispatches._recipient_key(r)) == "ABSENT"})
        if gone:
            # A MARKER WITHOUT A DISPOSITION IS AN ALARM. The reader's first
            # instinct on seeing their own old name here is to adopt the row,
            # and that is the one move this surface must not invite: resolving
            # a live seat into a retired one is a substitution, and a listing
            # that silently adopts an orphan is worse than one that cannot see
            # it. So the footer names the two legitimate exits and neither is
            # "treat it as yours".
            print("RECIPIENT ABSENT (%d row(s), %d name(s): %s): the roster "
                  "carries no row for these names, so nothing can deliver to "
                  "them and no seat's --mine will ever show them. DO NOT "
                  "adopt one because the name looks like yours — a rename is "
                  "not a claim. The exits are: the SENDER re-issues or cancels "
                  "its own, or an operator cancels a row whose sender is gone "
                  "too. `helm dispatch list --orphaned` is this set on its "
                  "own." % (sum(1 for r in rendered
                                if reach.get(dispatches._recipient_key(r)) == "ABSENT"),
                            len(gone), ", ".join(gone)))
        if any(reach.get(dispatches._recipient_key(r)) == "UNKNOWN" for r in rendered):
            # UNREADABLE IS NOT EMPTY, and the difference is the whole reason
            # this probe distinguishes them: a bad file handle must not read
            # as an orphaned estate.
            print("RECIPIENT UNKNOWN: the roster could not be read for some "
                  "rows, so their reachability is UNMEASURED — not absent. "
                  "Re-run once the roster is readable before acting on any of "
                  "them.")
        if unverifiable:
            # The marker is two words; a reader who has never seen it needs to
            # know it is NOT a work item and NOT a corruption, or the honest
            # response is alarm followed by a wasted hour.
            print("ATTEST UNVERIFIABLE (%d): the VERDICT STANDS — what cannot "
                  "be verified is the signed delivery record, because the "
                  "attest is bound to a different evidence string than the row "
                  "now carries (a re-mint). Nothing is blocked and nothing is "
                  "owed; the attest path is at-most-once and never re-signs."
                  % len(unverifiable))
        return 0
    return _unknown_subverb(verb)


# ---------------------------------------------------------------------------
# PUBLISH: bind what this module owns back onto `dispatches`
# ---------------------------------------------------------------------------
# THE `web_compat` MOVE, for the same reason it exists there: every name below
# was importable from `dispatches` before the split and still is, so no caller
# changed. Four arms depend on it -- three resolve `dispatches._cmd_dispatch`
# through `inspect.getsource` (which follows a re-bound function to THIS file)
# and one registry is keyed by the bare name, its own comment saying the name
# FOLLOWS THE BODY.
#
# PUBLISHING FROM HERE rather than importing names THERE is what keeps both
# import orders working: whichever module the process reaches first, this runs
# after every name it owns exists.
def owned():
    """The names this module owns, read from the LEDGER'S declaration.

    ONE SOURCE OF TRUTH. `dispatches._OWNER_NAMES` is both the tuple the
    retired-name rung reads and the tuple this publish loop walks, so a name
    added here without a declaration is not silently bound, and a declared
    name this module does not define fails loudly at publish rather than
    quietly at a caller.
    """
    from . import dispatches as _facade
    token = __name__.rsplit(".", 1)[-1]
    for module, names in _facade._OWNER_NAMES:
        if module == token:
            return names
    return ()


def _publish():
    """Bind the declared names onto the ledger module. Once, at import."""
    from . import dispatches as _facade
    for _name in owned():
        setattr(_facade, _name, globals()[_name])


_publish()
