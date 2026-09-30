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
    N0b LIGHT    the project's authored light: red starts nothing new
                                                            [helm projects state]
    N1 POLICY    kind -> candidate families                 [owner rulings]
    N2 BENCH     intersect the project's own bench: its authored team, by
                 role, else the roster home_room            [helm team]
    N3 FLAG      RED drops; ORANGE is critical-path-only; GREY is admitted
                 and SAYS SO; a short family reads the project's OWN colour
                 from its share                             [helm burn, E33]
    N4 LIVE      the usability join, or `helm reviewers` itself for a row,
                 less a seat the dispatch door refuses as BROKEN
                                                            [helm seat hold]
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
import re
import time

from . import burnflags, review_independence

# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------

N0, N0B, N1, N2 = "N0 ASK", "N0b LIGHT", "N1 POLICY", "N2 BENCH"
N3, N4, N5, N6 = "N3 FLAG", "N4 LIVE", "N5 CAP", "N6 RANK"
NODES = (N0, N0B, N1, N2, N3, N4, N5, N6)

KINDS = ("review", "build", "verify", "delegate", "research", "council")

# THE KINDS THAT START NEW WORK, for the light (task/3156). A red project
# starts nothing new — its own sentence is "finish or park what is running" —
# so a build or a delegate is refused at N0b and a review, a verification, a
# council or research on work already in flight is admitted, the same split
# `dispatches._project_light_rung` makes between a fresh chain and a
# continuation.
NEW_WORK_KINDS = ("build", "delegate")

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
       "opus55-is-the-default-fable-is-the-crossmodel-reviewer-of-last-resort",
       retired=("fable-reviews-are-high-stakes-only-not-the-default",)),
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
    # THE PROJECT'S SHARE OF A SHORT FAMILY (task/3156). While a family reads
    # ORANGE, a project with an authored team reads its OWN colour for it —
    # inside its budget YELLOW, up to twice it ORANGE, past twice it RED — and
    # this edge says why, out of the store like every other.
    _e("E33", N3, "project-share", "project-shares-ration-a-short-family"),
    # For review and verify this replaces E9's family-wide drop: claude is
    # admitted ONLY as a fresh-context Opus seat; E9 rides every refusal.
    _e("E34", N1, "claude-reads-only-as-fresh-opus",
       "approval-tier-2026-08-11-owner-revised"),
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
        if (flag or {}).get("share"):
            row["share"] = flag["share"]
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
        if family == burnflags.NATIVE_FAMILY and kind not in TIER_KINDS:
            drop(family, N1, "E9")
            continue
        if EDGE["E26"].get("family") == family and kind in OBLIGATION_KINDS:
            drop(family, N3, "E26")
            continue
        flag = flags.get(family)
        colour = (flag or {}).get("colour") or burnflags.GREY
        rationed = bool(((flag or {}).get("share") or {}).get("rationed"))
        if colour == burnflags.RED:
            # A RED THIS PROJECT EARNED ON ITS SHARE names the share first:
            # the family is short for everyone, and this project is past twice
            # its budget on it.
            if rationed and flag.get("family_colour") != burnflags.RED:
                drop(family, N3, "E33", "E11", colour=colour)
            else:
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
        if rationed or ((flag or {}).get("share") or {}).get("queued"):
            why += reasons(ctx, "E33")
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
        if family == burnflags.NATIVE_FAMILY:
            why += reasons(ctx, "E34")
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
    [E11], and so does a slot family whose next row would QUEUE behind the
    project's lanes [E33]; a family with no rating on this kind's axis sorts
    last and says so rather than being given a number [E7]; then the axis
    itself [E7]; then reachability — a seat helm can wake now, then one
    inside its beacon's re-arm grace, then a DEAF one the door still files
    for (task/3055) — and like idleness it only orders, never excludes; then
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

    A DOOR READ (review, verify) RANKS ON THE QUEUE ("no lane waits while a
    qualified reader is idle"): ORANGE, reach, `reviewer_eligibility`'s queue
    and context buckets, the author's own family, another lane before an
    Opus seat, the family's 5-hour share of review sends ("don't overuse
    codex"), and only then the rating axis [E34, E3].
    """
    order = _axis(kind)

    def key(row):
        fam = row["family"]
        same = 1 if row.get("family_preference") \
            == review_independence.SAME_FAMILY else 0
        rating = (0 if fam in order else 1, order.get(fam, len(order)))
        until = 0 if row.get("until") else 1
        # A QUEUED slot family ranks with ORANGE: its seat is still offered,
        # but one more row waits behind the project's lanes (task/3156).
        orange = 1 if row.get("colour") == burnflags.ORANGE \
            or (row.get("share") or {}).get("queued") else 0
        if kind not in TIER_KINDS:
            return (orange, same) + rating + (
                row.get("reach_rank", 0), row.get("pane_rank", 2), until, fam)
        held = row.get("holding")
        return (orange, row.get("reach_rank", 0), row.get("queue_bucket", 2),
                row.get("context_bucket", 1), same,
                1 if fam == burnflags.NATIVE_FAMILY else 0,
                row.get("review_share_5h") or 0) + rating + (
            row.get("pane_rank", 2), held if isinstance(held, int) else 0,
            until, fam)
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


_CLAUDE_MODEL = re.compile(r"(?:^|[^a-z])(opus|fable|sonnet|haiku)(?:[^a-z]|$)")


def _seat_bar(name, family, kind, barred, model_fn, report):
    """(edge ids that bar this seat from the pick, its resolved model).

    A DOOR READ never goes to the row's author or an out-of-tier seat (the
    `--row` bars), to an input-only model [E3], or to a claude seat whose
    resolved model is not Opus [E34, E9]. An UNREADABLE model bars nothing —
    a native seat whose transcript names no one model now (task/3508) — and
    the row says so beside the seat."""
    from . import reviewer_eligibility as re_mod
    bar = barred.get(re_mod.label_seat(name))
    if bar or kind not in TIER_KINDS:
        return bar, None
    model = re_mod.read_model(name, model_fn)
    word = _CLAUDE_MODEL.search(str(model or "").lower())
    if family == burnflags.NATIVE_FAMILY and model \
            and (word.group(1) if word else None) != "opus":
        # A FABLE SEAT names E9 first: that is the Fable ruling itself.
        return (("E9", "E34") if word and word.group(1) == "fable"
                else ("E34", "E9")), model
    if re_mod.input_only(model, family)[0]:
        report["input"].append({"seat": re_mod.label_seat(name),
                                "family": family, "model": model})
        return ("E3",), model
    return None, model


def _review_sends(families, now=None):
    """{family: review rows its seats were sent in the last 5 h}, or None
    when the ledger would not read — `family_sends.count`, the tally the
    RECIPIENT note already prints, so "don't overuse codex" ranks on the
    number a sender sees."""
    from . import dispatches, family_sends
    try:
        current, unavailable = dispatches.snapshot()
        if unavailable:
            return None
        resolve_one = family_sends._family_resolver()

        def family_of(name):
            fam = resolve_one(name)
            return FROM_ALIASES.get(fam, fam)
        return {f: family_sends.count(current, f, family_of,
                                      now or time.time())[1]
                for f in families}
    except Exception:                                   # noqa: BLE001
        return None


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


def _n4_drops(jrow):
    """Does N4 drop this seat? It cannot work at all, and not only because
    helm cannot wake it (a DEAF seat is a last-ranked candidate, task/3055).
    ONE PREDICATE for N4 and for the team's dark fallback at N2, so the two
    can never disagree about which seats are dark."""
    from . import seat_usability
    jrow = jrow or {}
    return jrow.get("can_take_work") is False \
        and not seat_usability.deaf_only(jrow)


def _team_refused_why(team, kind, takers, joined, bench, refused, label):
    """The sentence N2's fallback says: which of the team's takers for
    `kind` could not take it, and what refused each (its family, the node
    and the edge that dropped it, or N4 when it was dark)."""
    head = "every member of team v%d who takes %s" % (team["v"], kind)
    if not takers:
        return "no member of team v%d takes %s" % (team["v"], kind)
    family_of = {n: fam for fam, names in bench.items() for n in names}
    dropped = {d["family"]: d for d in refused}
    parts, dark = [], []
    for name in takers:
        fam = family_of.get(name)
        d = dropped.get(fam) if fam else None
        if d and d.get("node") != N4:
            edge = (d.get("reasons") or [{}])[0].get("edge")
            colour = " %s" % d["colour"] if d.get("node") == N3 \
                and d.get("colour") else ""
            parts.append("@%s (%s%s, %s%s)" % (
                label(name), fam, colour, d["node"].split()[0],
                " " + edge if edge else ""))
        elif _n4_drops(joined.get(name)):
            dark.append(name)
            parts.append("@%s dark (N4)" % label(name))
        else:
            parts.append("@%s (%s)" % (label(name), fam or "no family"))
    if len(dark) == len(takers):
        return "%s is dark (%s)" % (head, ", ".join("@" + label(n)
                                                   for n in dark))
    return "%s is refused: %s" % (head, "; ".join(parts))


def _read_light(project):
    """(registry.light for `project`, why-not). A project the registry does
    not hold has no light, and a reader that failed is a note, never a no:
    `registry.admits` keeps the same law at the doors."""
    if not project:
        return None, None
    try:
        from . import registry
        reg = registry.load(strict=True)
        rec = (reg.get("projects") or {}).get(project)
        if rec is None:
            return None, None
        return registry.light(project, rec), None
    except Exception as exc:                            # noqa: BLE001
        return None, ("the project light did not read (%s: %s); admitted "
                      "unverified" % (exc.__class__.__name__, exc))


def light_row(lit, why, kind):
    """N0b's record: the light, and whether it admits this KIND of work.

    ONE TABLE: `registry.LIGHT_ADMITS`, the one the doors grade with, read as
    (new work, continuation). Only an AUTHORED light binds, as at every door:
    the scan's half of the light is an observation, not the owner's word."""
    from . import registry
    new_work = kind in NEW_WORK_KINDS
    if not lit:
        return {"colour": None, "authored": False, "new_work": new_work,
                "admits": True, "refuses": False, "says": None,
                "reason": "", "by": "", "ts": None, "unread": why}
    colour = lit.get("colour")
    admits = True
    if lit.get("authored") and colour in registry.LIGHT_ADMITS:
        admits = registry.LIGHT_ADMITS[colour][0 if new_work else 1]
    return {"colour": colour, "authored": bool(lit.get("authored")),
            "new_work": new_work, "admits": admits, "refuses": not admits,
            "says": registry.LIGHT_SAYS.get(colour)
            if lit.get("authored") else None,
            "reason": lit.get("reason") or "", "by": lit.get("by") or "",
            "ts": lit.get("ts"), "unread": why}


def share_flags(flags, shares):
    """The flags this project reads: each family's fleet colour, with the
    project's OWN colour in its place where its share rations a short family,
    and where a local family's lanes are measured (inside its slots YELLOW,
    over them ORANGE; a RED family stays RED). The fleet colour rides as
    `family_colour` and the share as `share`, so nothing downstream has to
    re-derive either."""
    out = dict(flags or {})
    for family, row in (shares or {}).items():
        if family not in out or not isinstance(row, dict):
            continue
        flag = dict(out[family])
        flag["family_colour"] = flag.get("colour")
        flag["share"] = row
        if (row.get("rationed") or row.get("capacity_measured")) \
                and row.get("colour"):
            flag["colour"] = row["colour"]
        out[family] = flag
    return out


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

    # N0b — THE PROJECT'S LIGHT, before any family is weighed (task/3156).
    # Until now route never read it, so the light bound only at `work claim`
    # and `dispatch send` and a routing answer could recommend a build in a
    # project the owner had stopped. Capacity is never permission: a red
    # project starts nothing new however green its families are.
    #
    # AN UNOPTED PROJECT ANSWERS AS TRUNK DOES, byte for byte (round 3,
    # ruling a): the light rides the report only when the owner AUTHORED it
    # or it could not be read, the team, its shares and the bench source only
    # with an authored team (or light), and a row's share keys only with a
    # team. A project nobody opted in grows no key and no line.
    light_fn = seams.get("light") or _read_light
    lit, lit_why = light_fn(project)
    light = light_row(lit, lit_why, kind)
    if light["authored"] or light["unread"]:
        report["light"] = light
    if light["refuses"]:
        report["partial"] = False
        return report, None

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

    # N2 — the project's own bench. THE AUTHORED TEAM FIRST (task/3156): the
    # owner's record of which seats serve this project, filtered by what each
    # member's role takes. With no authored team the bench is derived, as it
    # always was, through the verb that owns the roster and its laundering
    # door — and the answer says which it read.
    from . import reviewer_eligibility as re_mod
    team_fn = seams.get("team")
    if team_fn is None:
        from . import teams as teams_mod
        team_fn = teams_mod.authored_only
    try:
        team = team_fn(project)
    except Exception as exc:                            # noqa: BLE001
        team = None
        ctx["partial"] = True
        report["partial_why"].append(
            "the project's team did not read (%s: %s); the derived bench "
            "answered instead" % (exc.__class__.__name__, exc))
    if team:
        from . import teams as teams_mod
        # THE FAMILY DOOR RULES THE BENCH (design read D3 on task/3156): a
        # member is benched on the family it SPENDS where `teams.family_of`
        # names one, and on its typed family only where it names none. The
        # authored word alone put a native seat written as codex on the
        # codex bench, as the cross-family reviewer.
        # STRICT (design read D6): a roster that did not read is FAILED
        # here, never an empty roster that leaves every typed family standing
        fams_fn = seams.get("families_of") or (
            lambda seats: teams_mod.live_families(seats, strict=True))
        members = team.get("members") or []
        try:
            # the flag's word for each family (`claude` is `anthropic`)
            live = {seat: teams_mod._alias(fam) for seat, fam in (
                fams_fn([m["seat"] for m in members]) or {}).items() if fam}
        except Exception as exc:                        # noqa: BLE001
            live = {}
            ctx["partial"] = True
            report["partial_why"].append(
                "the team's seat families FAILED (%s: %s); each member is "
                "benched on its typed family" % (exc.__class__.__name__, exc))
        seats_by_name, filtered = teams_mod.bench(team, kind, live=live)
        report["bench_source"] = "team v%d" % team["v"]
        report["team"] = {"v": team["v"], "by": team.get("by") or "",
                          "filtered": [{"seat": re_mod.label_seat(seat),
                                        "role": role}
                                       for seat, role in filtered],
                          "family_differs": [
                              {"seat": re_mod.label_seat(m["seat"]),
                               "authored": m["family"],
                               "spends": live[m["seat"]]}
                              for m in members if live.get(m["seat"])
                              and live[m["seat"]] != m["family"]],
                          "fallback": None}
    else:
        bench_fn = seams.get("bench") or re_mod.bench
        seats_by_name, bench_why = bench_fn(project=project,
                                            seams=seams.get("bench_seams"))
        if report.get("light"):
            report["bench_source"] = "derived bench"
        if bench_why:
            ctx["partial"] = True
            report["partial_why"].append(bench_why)
            seats_by_name = seats_by_name or {}

    join_fn = seams.get("join")
    if join_fn is None:
        from . import seat_usability
        join_fn = seat_usability.join

    broken_fn = seams.get("broken")
    if broken_fn is None:
        from . import seat_hold
        broken_fn = seat_hold.broken_seats

    def join_seats(names):
        try:
            joined = join_fn(seats=sorted(names), now=now) or {}
        except Exception as exc:                        # noqa: BLE001
            ctx["partial"] = True
            report["partial_why"].append(
                "the seat usability join failed (%s: %s)"
                % (exc.__class__.__name__, exc))
            return {}
        # A SEAT HELM KNOWS IS BROKEN (task/3546: an operator hold or a drop
        # storm) is refused by the dispatch door, so N4 drops it like a
        # seat that cannot work, through the same row `_n4_drops` reads.
        for name, facts in sorted((broken_fn(sorted(names)) or {}).items()):
            row = dict(joined.get(name) or {}, seat=name)
            row["can_take_work"] = False
            row["refusals"] = tuple(row.get("refusals") or ()) + ("BROKEN",)
            row["broken"] = list(facts)
            joined[name] = row
            report.setdefault("broken", []).append(
                {"seat": re_mod.label_seat(name), "facts": list(facts)})
        return joined
    joined = join_seats(seats_by_name)

    # N3's share half (task/3156): with an authored team, each family the
    # project's share rations reads the project's OWN colour. No team, no
    # share — the family colours stand exactly as before.
    shares = {}
    if team:
        alloc_fn = seams.get("allocation")
        if alloc_fn is None:
            from . import teams as teams_mod

            # STRICT (design read D6): a reader that raises is FAILED here,
            # never read as "nothing measured, nothing rationed"
            def alloc_fn(p):
                return teams_mod.project_row(p, None, now=now, strict=True)
        try:
            shares = alloc_fn(project) or {}
        except Exception as exc:                        # noqa: BLE001
            shares = {}
            ctx["partial"] = True
            report["partial_why"].append(
                "the project's shares FAILED (%s: %s); the family colours "
                "stand, so this answer is PARTIAL"
                % (exc.__class__.__name__, exc))
        report["shares"] = shares
    flags = share_flags(flags, shares)

    fresh_s = seams.get("fresh_s")
    if fresh_s is None:
        from . import proxywatch
        fresh_s = proxywatch.UPSTREAM_CACHE_FRESH_S
    live_fn = seams.get("fanout_live")
    if live_fn is None:
        from . import fanout
        live_fn = fanout.live
    # --row IS READ ONCE, BEFORE THE PICK: the row's author, its out-of-tier
    # and its input-only seats are barred from every family here, and the
    # same read is diffed against the answer below.
    fetched = _read_eligibility(row, seams) if row else (None, None)
    barred = {r["seat"]: _ROW_BAR[r.get("conjunct")]
              for r in (fetched[0] or {}).get("seats") or ()
              if r.get("conjunct") in _ROW_BAR}
    ctx_fn = seams.get("context") or re_mod.read_context
    from . import autocompact
    threshold = autocompact.threshold_pct()
    report["input"] = []

    def weigh(seats_by_name, joined):
        """N2's bench through N1, N3 and N4 for one set of seats ->
        (bench, refused, rows, unmeasured)."""
        bench = {}
        for name in sorted(seats_by_name):
            entry = seats_by_name.get(name) or {}
            fam = _seat_family(name, joined.get(name), entry,
                               seams.get("seatmod"))
            # A TEAM MEMBER'S FAMILY where nothing measured one — a native
            # seat, or a seat the lead has not started yet — is the family
            # door's answer, and the typed word only where the door names
            # none (`teams.bench`).
            if not fam and entry.get("team"):
                fam = entry.get("family")
            if fam:
                bench.setdefault(fam, []).append(name)
        admitted, refused = candidate_families(kind, frm_family, bench, flags,
                                               ctx)
        rows, unmeasured = [], []
        for family, why in sorted(admitted.items()):
            flag = flags.get(family) or {}
            colour = flag.get("colour") or burnflags.GREY
            if family not in flags:
                unmeasured.append(family)
            # N4 — LIVE. A seat that cannot work at all is not a candidate;
            # a seat that is merely BUSY still is, ranked lower. Excluding on
            # load is how a fleet comes to route everything to the one seat
            # nobody has given work to yet.
            # A DEAF SEAT IS A LAST-RANKED CANDIDATE, NOT A DROPPED ONE
            # (task/3055). The dispatch door files a row for a seat whose
            # only refusal is that helm cannot wake it
            # (`seat_usability.deaf_only`), because the ledger holds the row
            # until the beacon re-arms; the router must agree with that door
            # or it names a family the door would accept as having no seat.
            # It ranks after a WAKING seat, which ranks after a reachable
            # one. Every other refusal still drops the seat.
            # WITHIN A FAMILY the pick is reach, then the queue the seat
            # holds, then its context, then its pane. A seat a door read may
            # not go to is barred first and names the edge that bars it.
            seats, bars = [], []
            for name in bench.get(family, ()):
                jrow = joined.get(name) or {}
                if _n4_drops(jrow):
                    # A RESTING SEAT IS LISTED, NEVER SILENTLY DROPPED
                    # (task/3280): a family with another seat would otherwise
                    # say nothing about the one the owner paused.
                    # The key exists only when a seat rests, so an answer
                    # with none is byte-for-byte what it always was.
                    rest = jrow.get("rest")
                    if isinstance(rest, dict) and not any(
                            r["seat"] == re_mod.label_seat(name)
                            for r in report.get("resting") or ()):
                        report.setdefault("resting", []).append({
                            "seat": re_mod.label_seat(name), "family": family,
                            "reason": rest.get("reason")})
                    continue
                bar, model = _seat_bar(name, family, kind, barred,
                                       seams.get("runtime_model"), report)
                if bar:
                    bars += bar
                    continue
                seats.append((name, jrow, model, ctx_fn(name)))
            if not seats:
                refused.append({"family": family, "node": N1 if bars else N4,
                                "reasons": reasons(ctx, *(tuple(
                                    dict.fromkeys(bars)) or ("E13",))),
                                "colour": colour,
                                "until": flag.get("expires_at"),
                                "seats": [re_mod.label_seat(s)
                                          for s in bench.get(family, ())]})
                continue
            seats.sort(key=lambda p: (
                _reach_rank(p[1]), re_mod.queue_bucket(p[1].get("holding")),
                re_mod.context_bucket(p[3], threshold),
                _PANE_RANK.get(p[1].get("pane"), 2), p[0]))
            name, jrow, model, pct = seats[0]
            # THE RAW KEY DRIVES THE MATCH, THE LAUNDERED ONE IS EMITTED —
            # the roster's own law, through the roster verb's own door. A
            # register key is unvalidated at the join seam and every name
            # below reaches an operator's terminal.
            label = re_mod.label_seat(name)
            why = list(why) + reasons(ctx, "E13", "E14")
            # E15 — the proof case's own edge. A reading older than the
            # watch's freshness bound makes the answer PARTIAL; it does NOT
            # drop the family. Four hours of an integrator's time were spent
            # on the other behaviour.
            reading_age = flag.get("reading_age_s")
            stale = reading_age is not None and reading_age > fresh_s
            if stale:
                ctx["partial"] = True
                why += reasons(ctx, "E15")
                report["partial_why"].append(
                    "%s's reading is %ds old, past the %ds freshness bound — "
                    "PARTIAL, not dropped" % (family, reading_age, fresh_s))
            live = live_fn(name, family=family)
            rows.append(dict({
                "family": family, "seat": label,
                "family_preference": (review_independence.SAME_FAMILY
                                      if family in ctx.get("same_family", ())
                                      else review_independence.OTHER_FAMILY),
                "pane": jrow.get("pane"), "verdict": jrow.get("verdict"),
                "holding": jrow.get("holding"), "model": model,
                "queue_bucket": re_mod.queue_bucket(jrow.get("holding")),
                "context_pct": re_mod.split_context(pct)[0],
                "context_bucket": re_mod.context_bucket(pct, threshold),
                "pane_rank": _PANE_RANK.get(jrow.get("pane"), 2),
                "reach_rank": _reach_rank(jrow),
                "colour": colour, "until": flag.get("expires_at"),
                "reading_age_s": reading_age, "stale_reading": stale,
                "critical_path_only": colour == burnflags.ORANGE,
                "flag": {"colour": colour,
                         "expires_at": flag.get("expires_at"),
                         "cause": flag.get("cause"),
                         "cause_id": flag.get("cause_id"),
                         "axis": flag.get("axis")},
                "live": live,
                "cap": cap(family, colour, running=live.get("running"),
                           measured=live.get("measured")),
                "reasons": why + reasons(ctx, "E16", "E17", "E18", "E19",
                                         "E20", "E21", "E31"),
            }, **({"family_colour": flag.get("family_colour") or colour,
                   "share": flag.get("share")} if team else {}),
                # An unknown context says why (task/3534), and only then: a
                # measured or unread one answers exactly as it always did.
                **({"context_unknown": re_mod.split_context(pct)[1]}
                   if re_mod.split_context(pct)[1] else {})))
        return bench, refused, rows, unmeasured

    bench, refused, rows, unmeasured = weigh(seats_by_name, joined)

    # N2's FALLBACK (design reads D5 and round 3's b on task/3156): an
    # authored team that offers NOBODY for this kind — no member takes it,
    # or every member who does is dark (dropped at N4), refused by the
    # owner's policy at N1 or on a RED family at N3 — left the answer empty
    # while the project's other seats sat idle, and said nothing about them.
    # The derived bench answers instead, WITHOUT the team's own seats (their
    # role, state or family already answered), and the answer names each
    # taker and what refused it. An empty answer is never silent.
    if team and not rows:
        why = _team_refused_why(team, kind, sorted(seats_by_name), joined,
                                bench, refused, re_mod.label_seat)
        bench_fn = seams.get("bench") or re_mod.bench
        derived, bench_why = bench_fn(project=project,
                                      seams=seams.get("bench_seams"))
        if bench_why:
            ctx["partial"] = True
            report["partial_why"].append(bench_why)
        own = {m["seat"] for m in team.get("members") or ()}
        seats_by_name = {n: r for n, r in (derived or {}).items()
                         if n not in own}
        joined = join_seats(seats_by_name)
        report["bench_source"] = "derived bench (%s)" % why
        report["team"]["fallback"] = why
        # A FAMILY THE TEAM'S PASS REFUSED PAST N2 KEEPS THAT REFUSAL: with
        # the team's own seats left out, the derived bench often has no seat
        # of it at all, and "not on this bench" would hide that it is RED on
        # the project's share, refused by policy, or dark.
        kept = {d["family"]: d for d in refused if d["node"] != N2}
        bench, refused, rows, unmeasured = weigh(seats_by_name, joined)
        refused = [kept.get(d["family"], d) if d["node"] == N2 else d
                   for d in refused]

    report["bench"] = {f: [re_mod.label_seat(n) for n in names]
                       for f, names in sorted(bench.items())}
    report["refused"] = refused
    report["unmeasured"] = unmeasured

    if kind in TIER_KINDS and rows:
        sends = (seams.get("review_sends") or _review_sends)(
            sorted({r["family"] for r in rows}), now=now) or {}
        total = sum(n for n in sends.values() if isinstance(n, int))
        for r in rows:
            r["review_sends_5h"] = sends.get(r["family"])
            r["review_share_5h"] = (r["review_sends_5h"] or 0) / total \
                if total else 0.0
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
                                                    ctx, fetched)

    report["partial"] = bool(ctx["partial"])
    report["unresolved"] = sorted(set(ctx["unresolved"]))
    report["unexpressed"] = sorted(set(ctx.get("unexpressed") or ()))
    return report, None


# A --row conjunct that bars a seat from the pick -> the edge it cites.
_ROW_BAR = {"chain": ("E1",), "tier": ("E3",), "input": ("E3",)}


def _read_eligibility(rid, seams):
    """(`helm reviewers` report, why-not) for one row — asked ONCE."""
    from . import reviewer_eligibility as re_mod
    fn = seams.get("eligibility") or re_mod.eligibility
    try:
        return fn(rid)
    except Exception as exc:                            # noqa: BLE001
        return None, ("the eligibility read failed (%s: %s)"
                      % (exc.__class__.__name__, exc))


def _agree_with_reviewers(rid, ranked, refused, ctx, fetched):
    """Do this verb and `helm reviewers` name the same seats for one row?

    THE REVIEWERS VERB IS ONE EDGE OF THIS GRAPH, NOT A RIVAL. It is CALLED
    rather than re-derived — anything re-derived here would be the second
    census its own docstring indicts. What this adds is the FAMILY layer it
    has no opinion about, so the two answers can legitimately differ; when
    they do, the difference is printed with the node that caused it rather
    than left for a reader to notice.
    """
    report, err = fetched
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
    lt = report.get("light") or {}
    age = report.get("reading_age_s")
    if lt.get("refuses"):
        head += " — %s's light refuses new work" % report.get("project")
    else:
        head += " — %s" % ("flags measured %ds ago" % age if age is not None
                           else "NO FRESH FLAG SNAPSHOT")
    if report["partial"]:
        head += ", PARTIAL"
    out = [head]
    project = report.get("project") or "this project"
    # N0b — the light, when somebody set it (task/3156)
    if lt.get("authored"):
        out.append("  light %-8s %s — %s. Set%s: %s   [N0b]" % (
            str(lt["colour"]).upper(), project, lt.get("says") or "",
            " by " + lt["by"] if lt.get("by") else "",
            lt.get("reason") or "no reason recorded"))
    elif lt.get("unread"):
        out.append("  light UNKNOWN  %s   [N0b]" % lt["unread"])
    if lt.get("refuses"):
        out.append("  NOT   %-10s %s — a %s project starts nothing new; "
                   "finish or park what is running, or change the light "
                   "(`helm projects state %s <colour> --reason ...`)"
                   % (report["kind"], N0B, lt["colour"], project))
        out.append("  none  %s's light refuses %s work; review, verify and "
                   "council on work in flight are still admitted"
                   % (project, report["kind"]))
        return out
    # N2 — which bench answered
    team = report.get("team")
    if team and team.get("fallback"):
        out.append("  bench derived bench — %s; the derived bench answered, "
                   "without the team's own seats (`helm team %s`)   [N2]"
                   % (team["fallback"], project))
    elif team:
        filtered = team.get("filtered") or []
        out.append("  bench team v%d of %s (`helm team %s`)%s   [N2]" % (
            team["v"], project, project,
            "; not taking %s by role: %s" % (report["kind"], ", ".join(
                "@%s (%s)" % (f["seat"], f["role"]) for f in filtered))
            if filtered else ""))
    if team:
        for d in team.get("family_differs") or ():
            out.append("  family @%s is on the team as %s and spends %s, so "
                       "it is benched on %s (the family door rules; `helm "
                       "team %s` names the repair)   [N2]"
                       % (d["seat"], d["authored"], d["spends"], d["spends"],
                          project))
    elif report.get("bench_source"):
        out.append("  bench derived bench — %s has no authored team, so its "
                   "seats are read from their home rooms (`helm team %s`)"
                   "   [N2]" % (project, project))
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
        line += "   holding %s" % ("UNKNOWN" if r.get("holding") is None
                                   else r["holding"])
        out.append(line)
        if report["kind"] in TIER_KINDS and r["family"] \
                == burnflags.NATIVE_FAMILY and not r.get("model"):
            out.append("        resolved model UNREADABLE (its transcript "
                       "names no one model now) — send it only as a "
                       "fresh-context Opus read")
        if r.get("family_preference") == review_independence.SAME_FAMILY:
            out.append("        THE AUTHOR'S OWN FAMILY — admitted and "
                       "ranked last among equals; independence is the "
                       "reader's fresh context and different resolved model, "
                       "not its vendor")
        if r.get("share"):
            from . import teams
            out.append("        " + teams.line(project, r["family"],
                                               r["share"]))
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
        if d.get("share") and d.get("node") == N3:
            from . import teams
            out.append("        " + teams.line(project, d["family"],
                                               d["share"]))
        for reason in (d["reasons"] if explain else
                       ([first] if first else [])):
            out.append("        %s   [%s %s]" % (_say(reason, full=explain),
                                                 reason["edge"],
                                                 reason["source"]))
        if d.get("tier_caveat"):
            out.append("        tier_caveat: the live tier check is "
                       "FAMILY-KEYED and cannot express a per-model exclusion")
    for i in report.get("input") or ():
        out.append("  INPUT %-10s @%s runs %s — a read there is cheap input, "
                   "never the approving read" % (i["family"], i["seat"],
                                                 i["model"]))
    for r in report.get("resting") or ():
        out.append("  REST  %-10s @%s %s — never picked while it rests"
                   % (r["family"], r["seat"], r["reason"]))
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
