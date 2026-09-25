"""helm route — who should take this work right now, and the owner's own
sentence for every step of the answer.

THE DEFECT THIS CLOSES. An agent deciding where to send a review holds a
context window full of the work and empty of the fleet. It guesses, or it
spends its own turn measuring, and both answers age. The measured cost, the
hour this module was specified: one integrator wrote a whole family off on a
dark reading it had taken FOUR HOURS EARLIER and ran every adversarial review
as an expensive delegated agent, while that family sat idle with its own
reading healthy. Nothing was broken. Everything the answer needed was already
on disk.

NO ROUTING SENTENCE IS WRITTEN IN THIS FILE. `EDGES` is a table of (node,
source id, effect tag); the REASON a reader is shown is resolved at render
time out of the typed store, through the same `find_typed` resolver every
entry-taking verb reads. Edit the store and the reason this verb gives
changes; supersede an entry and the edge that quotes it follows. An id that
does not resolve prints UNRESOLVED and makes the whole answer PARTIAL — it is
never silently dropped, and it is never replaced by a sentence an agent wrote
about what it thought the owner meant.

THE GRAPH IS `reviewer_eligibility.LADDER`'S DISCIPLINE APPLIED TO FAMILIES.
Repair distance descending, short-circuiting, and the expensive read asked
only of survivors:

    N0 ASK       the kind, the asking family, the project, an optional row
    N1 POLICY    kind -> candidate families                 [owner rulings]
    N2 BENCH     intersect the project's own bench          [roster home_room]
    N3 FLAG      RED drops; ORANGE is critical-path-only; GREY is admitted
                 and SAYS SO                                [helm burn]
    N4 LIVE      the usability join, or `helm reviewers` itself for a row
    N5 CAP       how many delegates this family may start right now
    N6 RANK      judgment-bound up SMARTS, volume-bound up SPEED, then
                 idleness, then a known expiry over an unknown one

WHAT THIS VERB NEVER DOES: spawn, probe a vendor, write, or block. Every
reading is a file another pass already wrote. A vendor outage cannot stall a
routing answer, and an agent that asks this question pays milliseconds.

IT RECOMMENDS; IT NEVER ACTS. The write doors gate themselves — `dispatches`
refuses a walled family, `reviewers` excludes one — so an agent that never
asks this verb loses answer QUALITY and never safety.
"""

import json
import time

from . import burnflags, review_independence

# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------

N0, N1, N2 = "N0 ASK", "N1 POLICY", "N2 BENCH"
N3, N4, N5, N6 = "N3 FLAG", "N4 LIVE", "N5 CAP", "N6 RANK"
NODES = (N0, N1, N2, N3, N4, N5, N6)

KINDS = ("review", "build", "verify", "delegate", "research", "council")

# THE OTHER VERB. `helm router` is an HTTP relay that has been here for
# months; `helm route` is this. The collision is refused in BOTH directions
# rather than renamed: `helm router`'s in-tree references are cheap, but a
# systemd unit or a shell script OUTSIDE this repository is a blast radius
# nothing in here can measure.
ROUTER_SUBVERBS = ("up", "run", "down", "status", "line", "probes")

# `--from fable` is a MODEL, not a family: the scoped 7d-fable window rides
# the same account row as everything else claude-side. The verb answers about
# the FAMILY and names the window the caller asked under.
FROM_ALIASES = {"fable": burnflags.NATIVE_FAMILY,
                "opus": burnflags.NATIVE_FAMILY,
                "sonnet": burnflags.NATIVE_FAMILY,
                "haiku": burnflags.NATIVE_FAMILY,
                "claude": burnflags.NATIVE_FAMILY}

# THE TWO RATING AXES, owner-stated and STRICT, collapsed onto families
# because fable and opus are two models of one credential. A family ABSENT
# from an axis is UNRATED and sorts last carrying that reason — never
# collapsed to a number, which is the one thing the owner's own hedge about
# grok forbids. Source: E7.
SMARTS = {burnflags.NATIVE_FAMILY: 0, "codex": 1, "kimi": 2, "ds4pro": 3,
          "gemini": 4}
SPEED = {"gemini": 0, "ds4pro": 1, "kimi": 1, "codex": 2,
         burnflags.NATIVE_FAMILY: 3}

# THE ROUTING QUESTION IS NEVER "WHO IS BEST" BUT "IS THIS JUDGMENT-BOUND OR
# VOLUME-BOUND" — the owner's own words in E7. Design, adversarial review and
# settling a disagreement go UP the smarts axis and pay the latency. Work
# whose answer is SELF-EVIDENCING — grounding claims against named file:line,
# sweeping for a predicate, mechanical triage — goes UP the speed axis,
# because a fast model's wrong answer is caught by the evidence it was
# required to cite. `verify` is the second kind by construction: it exists to
# ground a claim, and a verification that cites nothing is not one.
JUDGMENT_KINDS = ("review", "build", "council")
VOLUME_KINDS = ("verify", "research", "delegate")

# The kinds where the answer CLOSES something and the approval tier therefore
# binds. E3's own IMPLEMENTATION FLAG rides every refusal made here.
TIER_KINDS = ("review", "verify")
# E3's admitted set. The entry named on that edge is the authority and the
# rendered reason quotes it; this tuple is only the membership the
# predicate tests. `claude` here is the native credential's family name.
APPROVAL_TIER = (burnflags.NATIVE_FAMILY, "codex", "ds4pro", "kimi", "grok")

# Kinds that carry a lane, a lease or a multi-hour obligation. A seat the
# owner declared short-tasks-only must never be handed one of these. E26.
OBLIGATION_KINDS = ("build", "delegate", "research", "review")

# E16 / E17: four simultaneous subagents per master, three for codex-family
# seats, and about four opus agents PER SEAT. ONE cap lives here; the
# fleet-wide spelling of that second cap is retired in the store rather than
# carried here as a second number.
MASTER_CAP = 4
CODEX_CAP = 3
# E18: a brief to a proxy seat names a TOTAL budget for the task and forbids
# fork-type subagents. Default until measured otherwise.
TASK_TOTAL = 8


def _e(eid, node, tag, *sources, **kw):
    """One edge. `tag` is a machine handle for the effect, never the reason —
    the reason is whatever the store says today."""
    row = {"id": eid, "node": node, "tag": tag, "sources": tuple(sources),
           "kind": kw.pop("kind", "store")}
    row.update(kw)
    return row


EDGES = (
    _e("E32", N0, "preauthorized", "tlas-are-preauthorized-for-subagents"),
    _e("E1", N1, "not-the-author",
       "review-rounds-go-to-cross-family-seats-not-same-family-subagents"),
    _e("E2", N1, "any-other-family",
       "cross-family-review-is-any-other-family-not-codex"),
    _e("E3", N1, "approval-tier", "approval-tier-2026-08-11-owner-revised",
       stamp="tier_caveat"),
    _e("E4", N2, "own-bench", "each-project-runs-its-own-bench-of-families"),
    _e("E5", N2, "not-another-projects-seats",
       "one-tla-pair-per-project-numbered-codex-seats-are-not-a-review-pool"),
    _e("E6", N1, "sa-default-codex",
       "sa-routing-precedence-codex-first-then-fable-never-opus"),
    _e("E6b", N1, "sa-default-claude-side", "sa-default-is-fable-workflow",
       "sa-always-fable"),
    _e("E7", N6, "ratings", "model-ratings-owner-strict-order-2026-08-03-r2"),
    _e("E8", N1, "opus-legs-are-not-subagents",
       "opus-implementation-legs-run-as-workflows-from-a-fable-seat"),
    _e("E9", N1, "fable-is-high-stakes-only",
       "fable-reviews-are-high-stakes-only-not-the-default"),
    _e("E10", N3, "policy-axis",
       "fable-is-the-orchestrator-not-the-subagent-opus-two-to-one"),
    _e("E10b", N6, "not-the-agent-tool", "fable-via-workflow-not-agent-tool"),
    # THE COLOUR EDGE CARRIES NO SENTENCE OF ITS OWN. What RED, ORANGE and
    # GREY mean is the owner's wording in `burnflags.BEHAVIOUR`, resolved at
    # render time for the colour actually read — one copy of those words in
    # the tree, in the module that mints them.
    _e("E11", N3, "colour-admission", kind="behaviour"),
    _e("E12", N3, "grey-is-unmeasured", kind="behaviour"),
    _e("E13", N4, "can-not-may", "liveness-is-not-routing-authority"),
    _e("E14", N4, "rank-on-idleness-never-exclude",
       "route-to-idle-first-and-busy-never-declines"),
    _e("E15", N4, "stale-reading-is-partial",
       "a-proxy-minted-429-is-a-belief-not-a-wall"),
    _e("E16", N5, "per-master-cap", "max-four-subagents-codex-caps-at-three"),
    _e("E17", N5, "per-seat-cap", "fleet-cap-four-workflow-agents-no-polling",
       retired=("fleet-wide-cap-of-four-simultaneous-workflow-agents",)),
    _e("E18", N5, "task-total-budget",
       "a-brief-to-a-proxy-seat-carries-a-total-subagent-budget"),
    _e("E19", N5, "fanout-is-multiplicative",
       "fanout-is-multiplicative-seat-count-is-additive"),
    _e("E20", N5, "gate-slots-are-fleet-wide",
       "gate-slots-are-fleet-wide-a-local-ps-cannot-see-them"),
    _e("E21", N5, "slots-are-not-context",
       "fanout-slots-are-not-context-headroom"),
    _e("E22", N6, "hops-acceptance-test",
       "fewest-hops-ideation-to-landing-without-losing-xfam"),
    _e("E23", N6, "cure-goes-to-the-finder",
       "delegation-checks-whose-context-is-hot-first"),
    _e("E24", N6, "cheapest-rung-that-changes-basin",
       "route-the-review-to-the-hottest-sufficient-basin-r2"),
    # UNEXPRESSED, WITH ITS SOURCE. openrouter's bar is a daily dollar rate on
    # a reader nothing in this tree builds, so no threshold ships. Naming the
    # gap costs one line; shipping a number for an unbuilt reader is the
    # defect that every honesty rule in this repository was written after.
    _e("E25", N3, "openrouter-dollars", kind="unexpressed",
       unexpressed=("openrouter-twenty-dollars-a-day-fails-the-extremely-"
                    "low-cost-bar",
                    "cheap-reviewers-must-beat-the-max-plus-kimi-baseline-"
                    "per-quality")),
    _e("E26", N3, "declared-short-tasks-only",
       "grok-seat-is-councils-and-short-tasks-only", family="grok"),
    _e("E27", N6, "cure-is-cost-routing",
       "max-accounts-are-base-load-and-a-token-bridge-is-the-one-exception"),
    _e("E28", N3, "abundant-workforce",
       "codex-is-the-abundant-aggressive-workforce", family="codex"),
    _e("E29", N1, "ds4pro-first-for-bulk", "prior-ds4pro-for-bulk-extraction"),
    _e("E30", N6, "cheap-models-council",
       "cheap-diverse-models-council-to-max-quality"),
    _e("E31", N5, "size-the-fanout-to-the-runway",
       "workflow-runway-check-before-fanout", "usage-routing-by-family-budget"),
)

EDGE = {row["id"]: row for row in EDGES}

# THE LAWS THAT ARE TRUE OF THE WHOLE ANSWER, not of one family. They are
# carried on every row in the report — a JSON consumer gets the complete
# graph — and rendered ONCE, under `--explain`. Repeating fourteen fleet-wide
# sentences under each of three candidates is how a surface teaches its
# readers to skip it, which costs more than the lines save.
GENERAL_EDGES = frozenset((
    "E32", "E7", "E13", "E14", "E16", "E17", "E18", "E19", "E20", "E21",
    "E22", "E23", "E24", "E27", "E30", "E31"))

_USAGE = ("helm route <kind> [--from <family>] [--project P] [--row <id>] "
          "[--explain] [--json]   (kind: %s)" % "|".join(KINDS))


# ---------------------------------------------------------------------------
# the store resolver — the ONLY place a reason comes from
# ---------------------------------------------------------------------------

def resolve(source_id, project=None, find=None):
    """(statement, resolved?) for one store id.

    TYPED-FIRST, through the tree's one resolver. `find_typed` accepts both
    `prior:id` and a bare slug and applies the typed-first law, so an id that
    a printed remediation could carry is an id this verb can quote.

    AN UNRESOLVABLE ID IS AN ANSWER, NOT A GAP. It renders UNRESOLVED, names
    itself, and makes the whole reply PARTIAL. The alternative — dropping the
    edge — is a routing answer that silently lost one of the owner's rules and
    still printed exit 0.
    """
    fn = find
    if fn is None:
        from .store import load as store_load
        fn = store_load.find_typed
    try:
        entry = fn(source_id, project=project)
    except Exception:                                   # noqa: BLE001
        entry = None
    if not isinstance(entry, dict):
        return None, False
    text = str(entry.get("statement") or "").strip()
    if not text:
        return None, False
    return text, True


def _first_sentence(text):
    """The firing half of a long statement. Reuses the injector's reducer so
    one rule about where a sentence ends serves both surfaces."""
    from .inject._entries import _first_sentence as reduce_one
    return reduce_one(text)


def reasons(ctx, *ids, **kw):
    """[{edge, source, says, resolved}] for the named edges, resolved NOW.

    `colour` selects the behaviour wording for the colour edges; everything
    else comes out of the store. A caller never passes text in.
    """
    colour = kw.pop("colour", None) or burnflags.GREY
    out = []
    for eid in ids:
        row = EDGE[eid]
        if row["kind"] == "behaviour":
            say = burnflags.BEHAVIOUR.get(colour, {}).get("say")
            out.append({"edge": eid, "general": eid in GENERAL_EDGES,
                        "source": "burnflags.BEHAVIOUR[%s]" % colour,
                        "says": say, "resolved": bool(say)})
            continue
        if row["kind"] == "unexpressed":
            out.append({"edge": eid, "general": eid in GENERAL_EDGES,
                        "source": ", ".join(row.get("unexpressed") or ()),
                        "says": None, "resolved": None})
            ctx.setdefault("unexpressed", []).append(eid)
            continue
        for sid in row["sources"]:
            text, ok = resolve(sid, project=ctx.get("store_project"),
                               find=ctx.get("find"))
            if not ok:
                ctx.setdefault("unresolved", []).append(sid)
                ctx["partial"] = True
            out.append({"edge": eid, "source": sid, "says": text,
                        "general": eid in GENERAL_EDGES, "resolved": ok})
    return out


# ---------------------------------------------------------------------------
# N1 + N2 + N3 — who is even a candidate
# ---------------------------------------------------------------------------

def candidate_families(kind, frm, bench, flags, ctx):
    """({family: [reasons]}, [refusals]) — the families this kind may use.

    ONE PASS OVER THE OWNER'S RULINGS, in edge order, short-circuiting on the
    first refusal so a family is named under ONE reason rather than a list a
    reader has to rank. That is `reviewer_eligibility`'s law, and the reason
    its EXCLUDED block is readable.
    """
    admitted, refused = {}, []

    def drop(family, node, *ids, **kw):
        row = {"family": family, "node": node,
               "reasons": reasons(ctx, *ids, **kw)}
        flag = flags.get(family)
        row["colour"] = (flag or {}).get("colour")
        row["until"] = (flag or {}).get("expires_at")
        for eid in ids:
            stamp = EDGE[eid].get("stamp")
            if stamp:
                row[stamp] = True
        refused.append(row)

    for family in burnflags.families():
        if family not in bench:
            drop(family, N2, "E4", "E5")
            continue
        if kind in TIER_KINDS and frm and family == frm:
            # FAMILY IS A PREFERENCE HERE, NOT THE REFUSAL. What makes a read
            # independent is a fresh context and a different resolved model;
            # the author's own family, read by a different seat on a different
            # session with different weights, is a legitimate second opinion
            # and was the only one available whenever the cross-family bench
            # was walled. So this candidate is ADMITTED and DEMOTED: E1 and E2
            # still print under it, and `rank` sorts it behind every other
            # family. The refusal that belongs to this question lives in
            # `review_independence`, which asks about the SEAT, the SESSION
            # and the RESOLVED MODEL rather than about the vendor.
            #
            # THE EDGES RESOLVE HERE, AT DETECTION, not in the admit branch
            # below. A same-family candidate that a LATER edge drops — a RED
            # flag, an ORANGE native credential — would otherwise never touch
            # E1 or E2 at all, and an id neither one resolved is an id whose
            # absence from the store this verb cannot report.
            ctx.setdefault("same_family", {})[family] = reasons(
                ctx, "E1", "E2")
        if kind in TIER_KINDS and family not in APPROVAL_TIER:
            drop(family, N1, "E3")
            continue
        if family == burnflags.NATIVE_FAMILY and kind != "verify":
            drop(family, N1, "E9")
            continue
        if EDGE["E26"].get("family") == family and kind in OBLIGATION_KINDS:
            drop(family, N3, "E26")
            continue
        flag = flags.get(family)
        colour = (flag or {}).get("colour") or burnflags.GREY
        if colour == burnflags.RED:
            drop(family, N3, "E11", colour=colour)
            continue
        if family == burnflags.NATIVE_FAMILY and colour not in (
                burnflags.GREEN, burnflags.YELLOW):
            drop(family, N1, "E9")
            continue
        # ONE COLOUR EDGE PER COLOUR. GREY's own wording is already both
        # halves of what E12 says — "not measured" and "said out loud" — so
        # firing E11 beside it would print the same sentence twice under two
        # edge ids, which reads as two independent reasons and is one.
        why = list(reasons(ctx, "E12" if colour == burnflags.GREY else "E11",
                           colour=colour))
        if (flag or {}).get("axis") == "policy":
            why += reasons(ctx, "E10")
        if EDGE["E28"].get("family") == family and colour in (
                burnflags.GREEN, burnflags.YELLOW):
            why += reasons(ctx, "E28")
        if family == "openrouter":
            why += reasons(ctx, "E25")
        if kind == "research":
            why += reasons(ctx, "E29")
        if kind in ("build", "delegate"):
            why += reasons(ctx, "E6", "E6b", "E8")
        why += (ctx.get("same_family") or {}).get(family, [])
        admitted[family] = why
    return admitted, refused


# ---------------------------------------------------------------------------
# N5 — the cap
# ---------------------------------------------------------------------------

def cap(family, colour, running=None, measured=False):
    """How many delegates this family may start RIGHT NOW.

        may_run = cap x delegate_factor - running

    `cap` is four per master and THREE for codex-family seats, because codex
    windows are the smallest quota in the fleet and a wide wave empties them
    fastest. `delegate_factor` is the colour's own rationing term, read off
    the flag rather than re-derived here.

    A NULL `running` REPORTS THE CAP AND STOPS. Subtracting an unmeasured
    zero would report a saturated seat's full cap as free headroom, which is
    the direction that spends money — and it is the exact inversion
    `fanout.live` refuses one layer down.
    """
    base = CODEX_CAP if family == "codex" else MASTER_CAP
    factor = burnflags.BEHAVIOUR.get(colour or burnflags.GREY,
                                     burnflags.BEHAVIOUR[burnflags.GREY])
    allowed = int(base * factor["delegate_factor"])
    out = {"family": family, "cap": base, "colour": colour,
           "delegate_factor": factor["delegate_factor"], "allowed": allowed,
           "running": running if measured else None,
           "measured": bool(measured), "task_total": TASK_TOTAL,
           "may_run": None}
    if measured and running is not None:
        out["may_run"] = max(0, allowed - int(running))
    return out


# ---------------------------------------------------------------------------
# N6 — the rank
# ---------------------------------------------------------------------------

def _axis(kind):
    return SPEED if kind in VOLUME_KINDS else SMARTS


def rank(rows, kind):
    """The answer rows, best first.

    KEY ORDER, and each term names the edge it serves: a family the owner's
    ORANGE wording tells you to route AROUND sorts below one it does not
    [E11]; a family with no rating on this kind's axis sorts last and says so
    rather than being given a number [E7]; then the axis itself [E7]; then
    reachability — a seat helm can wake now, then one inside its beacon's
    re-arm grace, then a DEAF one the door still files for (task/3055) —
    and like idleness it only orders, never excludes; then
    idleness, and load NEVER excludes [E14]; then a known expiry above an
    unknown one at equal colour, because an answer you can plan around beats
    one you cannot; then the name, so two equal seats order the same way
    twice.

    THE AUTHOR'S OWN FAMILY SORTS LAST AMONG ITS PEERS [E1, E2]. Family is a
    preference here rather than a refusal, and this demotion carries the
    whole force of that preference: a cross-family reader is taken first
    whenever one is equally usable, and the author's own family is what the
    board gets instead of nothing when none is. It sits AHEAD of the rating
    axis, because ranking a same-family seat above a cross-family one on
    smarts alone inverts the preference entirely; and BEHIND the ORANGE term,
    because a family the owner rationed is not an equally usable peer and
    promoting it over the asker's own healthy credential would spend a
    reserved window to satisfy a preference.
    """
    order = _axis(kind)

    def key(row):
        fam = row["family"]
        return (1 if row.get("colour") == burnflags.ORANGE else 0,
                1 if row.get("family_preference")
                == review_independence.SAME_FAMILY else 0,
                0 if fam in order else 1,
                order.get(fam, len(order)),
                row.get("reach_rank", 0),
                row.get("pane_rank", 2),
                0 if row.get("until") else 1,
                fam)
    return sorted(rows, key=key)


# ---------------------------------------------------------------------------
# the answer
# ---------------------------------------------------------------------------

_PANE_RANK = {"IDLE": 0, "LIVE": 1, "RUNNING": 2}


def _reach_rank(jrow):
    """0 reachable (or not measured), 1 WAKING, 2 DEAF-only (task/3055).

    A seat inside its beacon's re-arm grace answers in seconds, and a DEAF
    seat answers once it re-arms or is nudged; both can take the row, and a
    seat helm can wake NOW is the better pick. A secondary preference like
    idleness, never an exclusion."""
    from . import seat_usability
    jrow = jrow or {}
    if seat_usability.deaf_only(jrow):
        return 2
    if jrow.get("reachable") is None \
            and jrow.get("reachable_state") == seat_usability._WAKING:
        return 1
    return 0


def _seat_family(name, row, entry=None, seatmod=None):
    """The family behind a seat name — three doors, most-measured first.

    The joined row's `family` is the VERIFIED RUNTIME family the proxy watch
    measured. The spawn-register resolver answers for a project seat the watch
    does not cover. Neither can answer for a NATIVE seat: it has no proxy, so
    no upstream row and no proxy spawn record — and measured live, that left
    the native credential with no bench at all, which dropped the asking
    family at the BENCH node instead of at the policy that actually excludes
    it.

    THE THIRD DOOR IS THE REGISTER'S OWN VERIFIED RUNTIME, AND ITS SPELLING IS
    NOT OURS. The seat register records the harness word (`claude`); the flag
    vocabulary records the credential (`anthropic`). One alias table already
    holds that mapping for the caller's `--from`, and it is the same mapping,
    so it is read here rather than copied. UNVERIFIED RUNTIME IS NOT EVIDENCE:
    a mirror row written under another seat's name seeds display evidence and
    no authority, so it is skipped rather than believed.
    """
    fam = (row or {}).get("family")
    if fam:
        return fam
    from . import seat as _seat
    seatmod = seatmod or _seat
    try:
        fam, _err = seatmod.registered_seat_family(name)
    except Exception:                                   # noqa: BLE001
        fam = None
    if fam:
        return fam
    entry = entry if isinstance(entry, dict) else {}
    if not entry.get("runtime_verified"):
        return None
    spelling = str((entry.get("runtime") or {}).get("family") or "").lower()
    return FROM_ALIASES.get(spelling, spelling) or None


def answer(kind, frm=None, project=None, row=None, now=None, seams=None):
    """(report, why-not) — the whole routing answer for one ask.

    THE REPORT IS THE CONTRACT AND THE RENDER IS A CONSUMER OF IT, the law
    `reviewer_eligibility.eligibility` states: a caller that re-derives what a
    row MEANS will eventually derive it differently.

    Every reader is a SEAM with a default, so the arms run against frozen
    captures and this module never touches the live fleet under test.
    """
    seams = dict(seams or {})
    now = time.time() if now is None else now
    asked = str(frm or "").strip().lower() or None
    frm_family = FROM_ALIASES.get(asked, asked)
    report = {"kind": kind, "from": frm_family, "from_asked": asked,
              "project": project, "row": row, "measured_at": now,
              "reading_age_s": None, "partial": False, "partial_why": [],
              "answer": [], "refused": [], "unmeasured": [],
              "unresolved": [], "unexpressed": [], "hops": [],
              "reviewers": None}
    ctx = {"find": seams.get("find"), "store_project": project,
           "partial": False, "unresolved": [], "unexpressed": []}

    if kind not in KINDS:
        return None, ("%r is not a kind this verb answers (%s)"
                      % (kind, ", ".join(KINDS)))

    # N0 — the ask itself. Recorded as an edge so `--explain` can show that
    # the authorization question was asked and answered from the store.
    report["ask"] = reasons(ctx, "E32")

    # N3's input, read once. `cached_flags` NEVER probes: a snapshot past the
    # watchdog's own staleness bound yields nothing rather than an old
    # colour, so this verb cannot answer GREEN off a file nobody refreshed.
    flags_fn = seams.get("cached_flags") or burnflags.cached_flags
    flags, age = flags_fn(now=now)
    flags = flags or {}
    report["reading_age_s"] = age
    if not flags:
        ctx["partial"] = True
        report["partial_why"].append(
            "no fresh burn-flag snapshot — the reader never probes, so a "
            "stale or absent fold yields nothing rather than an old colour; "
            "`helm proxywatch --post` writes it")

    # N2 — the project's own bench, read through the verb that already owns
    # the roster and its laundering door.
    from . import reviewer_eligibility as re_mod
    bench_fn = seams.get("bench") or re_mod.bench
    seats_by_name, bench_why = bench_fn(project=project,
                                        seams=seams.get("bench_seams"))
    if bench_why:
        ctx["partial"] = True
        report["partial_why"].append(bench_why)
        seats_by_name = seats_by_name or {}

    join_fn = seams.get("join")
    if join_fn is None:
        from . import seat_usability
        join_fn = seat_usability.join
    try:
        joined = join_fn(seats=sorted(seats_by_name), now=now) or {}
    except Exception as exc:                            # noqa: BLE001
        joined = {}
        ctx["partial"] = True
        report["partial_why"].append(
            "the seat usability join failed (%s: %s)"
            % (exc.__class__.__name__, exc))

    bench = {}
    for name in sorted(seats_by_name):
        fam = _seat_family(name, joined.get(name), seats_by_name.get(name),
                           seams.get("seatmod"))
        if fam:
            bench.setdefault(fam, []).append(name)
    report["bench"] = {f: [re_mod.label_seat(n) for n in names]
                       for f, names in sorted(bench.items())}

    admitted, refused = candidate_families(kind, frm_family, bench, flags, ctx)
    report["refused"] = refused

    fresh_s = seams.get("fresh_s")
    if fresh_s is None:
        from . import proxywatch
        fresh_s = proxywatch.UPSTREAM_CACHE_FRESH_S
    live_fn = seams.get("fanout_live")
    if live_fn is None:
        from . import fanout
        live_fn = fanout.live

    rows = []
    for family, why in sorted(admitted.items()):
        flag = flags.get(family) or {}
        colour = flag.get("colour") or burnflags.GREY
        if family not in flags:
            report["unmeasured"].append(family)
        # N4 — LIVE. A seat that cannot work at all is not a candidate; a
        # seat that is merely BUSY still is, ranked lower. Excluding on load
        # is how a fleet comes to route everything to the one seat nobody has
        # given work to yet.
        # A DEAF SEAT IS A LAST-RANKED CANDIDATE, NOT A DROPPED ONE (task/3055).
        # The dispatch door files a row for a seat whose only refusal is that
        # helm cannot wake it (`seat_usability.deaf_only`), because the ledger
        # holds the row until the beacon re-arms; the router must agree with
        # that door or it names a family the door would accept as having no
        # seat. It ranks after a WAKING seat, which ranks after a reachable
        # one. Every other refusal still drops the seat.
        from . import seat_usability
        seats = []
        for name in bench.get(family, ()):
            jrow = joined.get(name) or {}
            if jrow.get("can_take_work") is False \
                    and not seat_usability.deaf_only(jrow):
                continue
            seats.append((name, jrow))
        if not seats:
            refused.append({"family": family, "node": N4,
                            "reasons": reasons(ctx, "E13"), "colour": colour,
                            "until": flag.get("expires_at"),
                            "seats": [re_mod.label_seat(s)
                                      for s in bench.get(family, ())]})
            continue
        seats.sort(key=lambda p: (_reach_rank(p[1]),
                                  _PANE_RANK.get((p[1] or {}).get("pane"), 2),
                                  p[0]))
        name, jrow = seats[0]
        # THE RAW KEY DRIVES THE MATCH, THE LAUNDERED ONE IS EMITTED — the
        # roster's own law, through the roster verb's own door. A register
        # key is unvalidated at the join seam and every name below reaches an
        # operator's terminal.
        label = re_mod.label_seat(name)
        why = list(why) + reasons(ctx, "E13", "E14")
        # E15 — the proof case's own edge. A reading older than the watch's
        # freshness bound makes the answer PARTIAL; it does NOT drop the
        # family. Four hours of an integrator's time were spent on the other
        # behaviour.
        reading_age = flag.get("reading_age_s")
        stale = reading_age is not None and reading_age > fresh_s
        if stale:
            ctx["partial"] = True
            why += reasons(ctx, "E15")
            report["partial_why"].append(
                "%s's reading is %ds old, past the %ds freshness bound — "
                "PARTIAL, not dropped" % (family, reading_age, fresh_s))
        live = live_fn(name, family=family)
        rows.append({
            "family": family, "seat": label,
            "family_preference": (review_independence.SAME_FAMILY
                                  if family in ctx.get("same_family", ())
                                  else review_independence.OTHER_FAMILY),
            "pane": jrow.get("pane"), "verdict": jrow.get("verdict"),
            "holding": jrow.get("holding"),
            "pane_rank": _PANE_RANK.get(jrow.get("pane"), 2),
            "reach_rank": _reach_rank(jrow),
            "colour": colour, "until": flag.get("expires_at"),
            "reading_age_s": reading_age, "stale_reading": stale,
            "critical_path_only": colour == burnflags.ORANGE,
            "flag": {"colour": colour, "expires_at": flag.get("expires_at"),
                     "cause": flag.get("cause"),
                     "cause_id": flag.get("cause_id"),
                     "axis": flag.get("axis")},
            "live": live,
            "cap": cap(family, colour, running=live.get("running"),
                       measured=live.get("measured")),
            "reasons": why + reasons(ctx, "E16", "E17", "E18", "E19", "E20",
                                     "E21", "E31"),
        })

    ranked = rank(rows, kind)
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
        r["reasons"] = r["reasons"] + reasons(ctx, "E7", "E24", "E23")
        if r["family"] == burnflags.NATIVE_FAMILY:
            r["reasons"] += reasons(ctx, "E10b")
        if not r["cap"]["measured"]:
            r["reasons"] += reasons(ctx, "E27")
    report["answer"] = ranked
    report["hops"] = reasons(ctx, "E22", "E30")
    report["cap"] = cap(frm_family or "",
                        (flags.get(frm_family) or {}).get("colour")
                        or burnflags.GREY)

    if row:
        report["reviewers"] = _agree_with_reviewers(row, ranked, refused,
                                                    ctx, seams)

    report["partial"] = bool(ctx["partial"])
    report["unresolved"] = sorted(set(ctx["unresolved"]))
    report["unexpressed"] = sorted(set(ctx.get("unexpressed") or ()))
    return report, None


def _agree_with_reviewers(rid, ranked, refused, ctx, seams):
    """Do this verb and `helm reviewers` name the same seats for one row?

    THE REVIEWERS VERB IS ONE EDGE OF THIS GRAPH, NOT A RIVAL. It is CALLED
    rather than re-derived — anything re-derived here would be the second
    census its own docstring indicts. What this adds is the FAMILY layer it
    has no opinion about, so the two answers can legitimately differ; when
    they do, the difference is printed with the node that caused it rather
    than left for a reader to notice.
    """
    from . import reviewer_eligibility as re_mod
    fn = seams.get("eligibility") or re_mod.eligibility
    try:
        report, err = fn(rid)
    except Exception as exc:                            # noqa: BLE001
        err, report = ("the eligibility read failed (%s: %s)"
                       % (exc.__class__.__name__, exc)), None
    if err or not report:
        ctx["partial"] = True
        return {"row": rid, "eligible": None, "unreadable": err,
                "difference": []}
    eligible = list(report.get("eligible") or ())
    mine = [r["seat"] for r in ranked]
    dropped = {}
    for d in refused:
        for seat in (d.get("seats") or ()):
            dropped[seat] = d["node"]
    diff = []
    for seat in eligible:
        if seat not in mine:
            diff.append({"seat": seat, "side": "reviewers-only",
                         "node": dropped.get(seat, N3)})
    for seat in mine:
        if seat not in eligible:
            diff.append({"seat": seat, "side": "route-only", "node": N4})
    if report.get("unreadable"):
        ctx["partial"] = True
    return {"row": rid, "eligible": eligible, "unreadable": None,
            "difference": diff,
            "partial_inputs": sorted(report.get("unreadable") or ())}


# ---------------------------------------------------------------------------
# the render
# ---------------------------------------------------------------------------

_SAY_CAP = 160


def _say(reason, full=False):
    if reason.get("says") is None and reason.get("resolved") is None:
        return "UNEXPRESSED — no reader exists (%s)" % reason["source"]
    if not reason.get("resolved"):
        return "UNRESOLVED %s — this answer is PARTIAL" % reason["source"]
    text = reason["says"]
    if full:
        return text
    text = _first_sentence(text)
    return text if len(text) <= _SAY_CAP else text[:_SAY_CAP - 1] + "…"


def _until(at, now):
    return burnflags._when(at, now) if at else "—"


def render(report, now=None, explain=False):
    """The rendered answer. One TAKE block per admitted family, one NOT line
    per refusal naming the ONE node that dropped it, and the hops acceptance
    test — which is the question a reader is actually deciding."""
    now = time.time() if now is None else now
    head = "helm route %s" % report["kind"]
    if report.get("from_asked"):
        head += " --from %s" % report["from_asked"]
        if report["from_asked"] != report["from"]:
            head += " (family %s)" % report["from"]
    age = report.get("reading_age_s")
    head += " — %s" % ("flags measured %ds ago" % age if age is not None
                       else "NO FRESH FLAG SNAPSHOT")
    if report["partial"]:
        head += ", PARTIAL"
    out = [head]
    for r in report["answer"]:
        # THE DEDUP IS WITHIN ONE CANDIDATE, NEVER ACROSS THEM. A reason that
        # is true of THIS family must print under THIS family: suppressing it
        # because a sibling printed the same edge makes the second candidate
        # look like it was admitted for no reason at all.
        seen = set()
        line = ("  TAKE  %-10s @%-14s flag %-6s until %s"
                % (r["family"], r["seat"], r["colour"],
                   _until(r["until"], now)))
        c = r["cap"]
        line += ("   delegates %s" % c["may_run"] if c["may_run"] is not None
                 else "   delegates UNMEASURED (cap %d)" % c["allowed"])
        line += "   family %s" % r.get(
            "family_preference", review_independence.OTHER_FAMILY)
        out.append(line)
        if r.get("family_preference") == review_independence.SAME_FAMILY:
            out.append("        THE AUTHOR'S OWN FAMILY — admitted and "
                       "ranked last; independence is the reader's fresh "
                       "context and different resolved model, not its vendor")
        if r["critical_path_only"]:
            out.append("        critical path only — one delegate at a "
                       "time, prefer another family")
        if r["flag"].get("cause"):
            out.append("        %s   [%s]" % (r["flag"]["cause"],
                                              r["flag"].get("cause_id")
                                              or "burnflags"))
        for reason in r["reasons"]:
            if reason.get("general") and not explain:
                continue
            key = (reason["edge"], reason["source"])
            if key in seen and not explain:
                continue
            seen.add(key)
            out.append("        %s   [%s %s]" % (_say(reason, full=explain),
                                                 reason["edge"],
                                                 reason["source"]))
    for d in report["refused"]:
        first = d["reasons"][0] if d["reasons"] else None
        tail = ""
        if d.get("colour"):
            tail = " %s until %s" % (d["colour"], _until(d.get("until"), now))
        out.append("  NOT   %-10s %s%s" % (d["family"], d["node"], tail))
        for reason in (d["reasons"] if explain else
                       ([first] if first else [])):
            out.append("        %s   [%s %s]" % (_say(reason, full=explain),
                                                 reason["edge"],
                                                 reason["source"]))
        if d.get("tier_caveat"):
            out.append("        tier_caveat: the live tier check is "
                       "FAMILY-KEYED and cannot express a per-model exclusion")
    for reason in report.get("hops") or ():
        out.append("  hops  %s   [%s %s]" % (_say(reason, full=explain),
                                             reason["edge"], reason["source"]))
    rv = report.get("reviewers")
    if rv:
        if rv.get("unreadable"):
            out.append("  row   helm reviewers %s did not read: %s"
                       % (rv["row"], rv["unreadable"]))
        else:
            out.append("  row   helm reviewers %s: %s"
                       % (rv["row"], ", ".join(rv["eligible"]) or "nobody"))
            for d in rv["difference"]:
                out.append("        differs at %s: %s (%s)"
                           % (d["node"], d["seat"], d["side"]))
    for why in report["partial_why"]:
        out.append("  part  %s" % why)
    for sid in report["unresolved"]:
        out.append("  ???   UNRESOLVED %s — edit the store or supersede "
                   "the edge" % sid)
    if not report["answer"]:
        out.append("  none  no family on this bench can take this work right "
                   "now — every candidate is named above")
    return out


def exit_code(report):
    """0 an answer, 1 measured-nobody, 3 PARTIAL or could-not-tell, 2 usage.

    BOTH 1 AND 3 ARE NON-ZERO, so a caller that gates on this verb refuses
    identically whichever it gets; the split exists for the AGENT, which owes
    a different next move to "park it" than to "re-measure".
    """
    if report.get("partial"):
        return 3
    return 0 if report.get("answer") else 1


# ---------------------------------------------------------------------------
# the verb
# ---------------------------------------------------------------------------

def _flag_value(args, name):
    if name not in args:
        return None, args, None
    at = args.index(name)
    if at + 1 >= len(args) or args[at + 1].startswith("-"):
        return None, args, "%s needs a value" % name
    return args[at + 1], args[:at] + args[at + 2:], None


def cmd_route(args):
    """route <kind> [--from F] [--project P] [--row R] [--explain] [--json] —
    who should take this work right now, with the owner's own reasons."""
    import sys
    args = list(args or ())
    if not args:
        print("usage: %s" % _USAGE, file=sys.stderr)
        return 2
    kind = args[0]
    if kind in ROUTER_SUBVERBS:
        # THE DISAMBIGUATOR, and it is bidirectional: `helm router`'s own
        # usage names this verb. A relay subverb typed here is a typo with a
        # confident-looking answer available, which is the worst shape.
        print("helm route takes a KIND (%s) — did you mean `helm router "
              "%s`?" % ("|".join(KINDS), kind), file=sys.stderr)
        return 2
    if kind.startswith("-") or kind not in KINDS:
        print("helm route: %r is not a kind\nusage: %s" % (kind, _USAGE),
              file=sys.stderr)
        return 2
    rest = args[1:]
    as_json = "--json" in rest
    explain = "--explain" in rest
    rest = [a for a in rest if a not in ("--json", "--explain")]
    opts = {}
    for flag, key in (("--from", "frm"), ("--project", "project"),
                      ("--row", "row")):
        value, rest, err = _flag_value(rest, flag)
        if err:
            print("helm route: %s\nusage: %s" % (err, _USAGE), file=sys.stderr)
            return 2
        opts[key] = value
    if rest:
        print("helm route: unknown argument %r\nusage: %s"
              % (rest[0], _USAGE), file=sys.stderr)
        return 2
    if opts.get("project") is None:
        import os
        from .inject._ledger import project_for_cwd
        try:
            opts["project"] = project_for_cwd(os.getcwd())
        except Exception:                               # noqa: BLE001
            opts["project"] = None
    report, err = answer(kind, frm=opts["frm"], project=opts["project"],
                         row=opts["row"])
    if err:
        print("helm route: %s\nusage: %s" % (err, _USAGE), file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(report, indent=1, sort_keys=True, default=str))
    else:
        for text in render(report, explain=explain):
            print(text)
    return exit_code(report)
