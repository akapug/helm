#!/usr/bin/env python3
"""helm web — the OWNER'S BURN-DOWN surface (task/955).

WHY THIS EXISTS, in the owner's own words: "get us ABLE TO BURN DOWN ALL ROWS."
Until this endpoint, the answer to "what does the fleet still owe?" was reachable
ONLY by running the obligation ledger's own verb in a terminal — and that verb
is not even on trunk, it lives on lane/obligation-delivery-seam. The owner is
GUI-first. A verb only agents run does not discharge a request to SEE the
backlog, and the fleet spent a night improving a surface he cannot open.

LAND-ORDER INDEPENDENT, deliberately, and this is the whole reason the endpoint
looks the way it does. `helm.obligation` may land AFTER this surface. Rather
than couple the two lanes and hand the integrator a sequencing problem, the
import is defensive and its absence is a NAMED STATE. When obligation lands,
this lights up with no change here. Same contract `_api_tasks` already
established for `helm.tasks`; this is that idiom, not a new one.

THE STATE TRIAD IS LOAD-BEARING and it is the reason this is not three lines:
  empty       -> the fleet genuinely owes nothing
  unavailable -> we CANNOT SEE what it owes
Drawing those the same way tells the owner the pipeline is clear at the exact
moment it lost the ability to answer, which is the failure this codebase has
already paid for more than once.

FORKS ARE REPORTED, NEVER SILENTLY FOLDED. The owed verb excludes forked-subtree
descendants per a design ruling, while `helm dispatch triage` does not know
about forks — so for at least one row the two verbs DISAGREE. Rendering one
answer as though it were settled would make a contested reading look like fact
in the one place the owner trusts. So the count of excluded forks travels WITH
the rows: the owner sees "N owed, M forked chains excluded", and can tell that
a judgement was applied rather than discovering it later.

SERVED STALE WHILE IT REBUILDS, and that is the third law this file answers to.
The body costs a full fold of the dispatch ledger plus a git walk per cured
row; measured on the owner's box it ran about a minute, against a card that
fetches with an eight second deadline every forty-five seconds. So the card
NEVER rendered: every poll aborted and printed a timeout, and because each
abort left a build running, one open tab kept the web process folding the
ledger forever — which slowed every OTHER endpoint down with it (a configs
tree that answers in milliseconds idle took seconds underneath it).

The cure is the one `_api_lr` already uses for the same flap one module over:
`web_cache._cached_swr`. A previous body is served IMMEDIATELY and age-stamped
while ONE background rebuild runs; a cold cache answers `warming` rather than
an alarm about a ledger nothing has read yet; past a hard cap the read blocks
again rather than serving an unbounded past. Nothing here decides freshness on
its own — see `_owed_build` for the floor, the cap and why each number is what
it is.

ONE FOLD PER BUILD. The two buckets are two questions over ONE ledger reading,
and the reading is taken once in `_owed_build` and handed down. A separate
reading per bucket — `_cured_rows` folding the ledger and
`obligation.unanswered_fixes` folding it again — costs a second fold of an
eleven-megabyte file for the same bytes, and it lets the two halves rendered
side by side describe two different instants: a row cured between the folds is
absent from one and present in the other, under one heading.
"""

import os
import time

ROW_CAP = 200

# HOW OFTEN THE LEDGER MAY BE FOLDED, and it is derived from the build's own
# cost rather than picked. A floor BELOW the build duration is not a floor at
# all: the entry is already older than it the moment the build that filled it
# finishes, so the next poll kicks another one and the fold runs forever —
# which is the state the owner's open tab actually produced. Measured here at
# roughly forty seconds per build after the double fold was removed, so this
# is about six builds' worth: the fold occupies well under a fifth of the
# time, and the owner's forty-five second poll never waits for one.
_OWED_FLOOR_S = 240

# PAST THIS THE READ BLOCKS, exactly like `_cached`. A rebuild that has not
# succeeded in half an hour has lost the right to keep serving its past — the
# same bound `_cached_swr` states for /api/lr, scaled to a build that costs an
# order of magnitude more than that one. It is far enough above the floor that
# several consecutive failed rebuilds are needed to reach it.
_OWED_HARD_TTL_S = 1800

# HOW LONG AN UNMOVED LEDGER MAY HOLD THE BODY WITHOUT A REBUILD. Deliberately
# below `_OWED_HARD_TTL_S` by more than one build, so a declining key can never
# age out of the STALE regime into the blocking one — the law `_cached_swr`
# states for this option and the reason /api/lr's cap sits under its own hard
# ttl. It is also the bound on what the fingerprint cannot see: the cured half
# reads git, and a trunk that moves with no ledger write is picked up within
# this window rather than never.
_OWED_UNCHANGED_MAX_S = 900

# HOW LONG A COLD READ WAITS FOR THE FIRST BUILD BEFORE ANSWERING `warming`.
#
# IT MUST EXCEED THE BUILD, OR IT IS A PLACEHOLDER GENERATOR. At 5 seconds it
# was strictly SHORTER than the thing it waits for, so the first view after
# every restart was guaranteed to answer `{"warming": true}` — the owner paid
# five seconds to be told nothing was ready, every single time, and the body
# he wanted landed in the cache one second after he stopped looking at it.
# A wait shorter than its own fill is the same defect as a TTL shorter than
# its own fill, one state earlier.
#
# MEASURED, AND MEASURE THE RIGHT BUILD. `_owed_build` called in a loop
# inside one warm process answers in 5.70-9.14s over fifteen builds, and that
# number is NOT the one this constant is about: the build this waits for is
# the FIRST one in a FRESH process, with the page cache cold, which is the
# only build a cold read ever sees. Measured that way, four fresh processes:
# 8.85s, 9.81s, 10.24s — and 42.09s once, on a box that had been doing other
# work. The second build in each of those same processes cost 5.92-9.85s, so
# the gap is the cold read of the ledger and the git objects behind it.
#
# 14 CLEARS THE TYPICAL COLD BUILD AND DELIBERATELY NOT THE OUTLIER. No wait
# worth having covers 42s — the owner would hold a blank card for most of a
# minute — so past this bound `warming` is the right answer and the card
# re-asks for it (see the retry in 50-work.js.part). What 14 buys is the
# ordinary restart: the body now lands in the FIRST view instead of being
# guaranteed to miss it.
#
# AND IT STAYS STRICTLY INSIDE THE CARD'S FETCH DEADLINE, which is the other
# half of the same rule: a wait that outlived the fetch would abort the very
# build it exists to let finish. That deadline was ALSO 8 seconds — below the
# fill — so the fetch could not have carried a cold answer at any wait; it is
# 20s at helm/web_ui/scripts/50-work.js.part (`j("/api/owed", 20000)`), which
# leaves six seconds of response budget past this wait. The two numbers move
# together or not at all.
_OWED_COLD_WAIT_S = 14


def _rows_from(items):
    """One flat row per obligation, newest-relevant fields only.

    The lane, the seat that owes it, and how long it has been waiting are the
    three things that make a backlog actionable rather than decorative. Age is
    carried as a NUMBER so the client can sort and colour it; formatting a
    duration into a string here would force the renderer to parse prose.
    """
    out = []
    for r in items:
        if not isinstance(r, dict):
            continue
        out.append({
            "row": str(r.get("row") or "")[:12],
            "lane": str(r.get("lane") or "") or None,
            "seat": str(r.get("owed_seat") or "") or None,
            "since": str(r.get("owed_since") or "") or None,
            "kind": str(r.get("kind") or "") or None,
            "repo": str(r.get("repo_id") or "") or None,
            "what": str(r.get("what") or "")[:400] or None,
        })
    return out


def _cured_rows(snap, unavailable):
    """(rows, unavailable, unplaceable, fatal, ambiguous) — cured, never
    re-dispatched — from a ledger reading TAKEN BY THE CALLER.

    THE SECOND OBLIGATION TYPE, and the console was silent about it. `helm owed`
    answers "which chains carry a FIX nobody has answered"; this answers "which
    rows did their author cure and never hand back". They are not competing
    answers to one question — measured on row 3e4f26966ba8, owed correctly omits
    it (there is no unanswered FIX) while triage correctly names it (the cure was
    never re-dispatched). Rendering only the first left nine rows invisible, and
    invisible is where they stay: `dispatch list --open` excludes CURED by
    construction, so every routine sweep reports zero for all of them.

    REUSED, NEVER RE-DERIVED. dispatches.cured_unwitnessed already owns this and
    its docstring carries the measurement that shapes it: of 67 cured rows on the
    live ledger, 57 HAVE a chain successor — a reviewer is holding them, healthy
    in-flight work — and only 10 have nobody waiting. A panel rendering "a cure
    exists" would be 85% false, and a surface that cries stranded at in-flight
    work is muted within a day. This renders the THIRD set only.

    TWO DIAGNOSTIC CHANNELS, neither laundered. An unreadable snapshot is fatal
    because there is no population to classify. A repository walk may return
    BOTH rows and diagnostics: those rows remain visible, the diagnostic is
    surfaced as PARTIAL, and the other repositories still run. The producer
    filters ledger-ineligible rows before Git, so an empty eligible population
    is honestly empty rather than an invented repository failure.

    THE READING IS HANDED IN, NEVER TAKEN, and that is a correctness rule
    wearing a cost rule's clothes. A call to `dispatches.snapshot()` here — with
    `obligation.unanswered_fixes` making its own for the other bucket — folds an
    eleven-megabyte ledger TWICE per response, and the two buckets beside each
    other on the owner's screen then describe two different instants: a row
    cured between the folds is absent from one half and present in the other,
    under one heading. `_owed_build` takes the reading once and hands the same
    rows to both.
    """
    from . import dispatches
    if unavailable:
        return (), "the dispatch ledger could not be read: %s" % (unavailable,), 0, True, 0

    # ONE PER-REPOSITORY CONTRACT FOR EVERY CONSUMER. The dispatch CLI used to
    # be the third spelling and silently classified foreign rows against its
    # process cwd. dispatches.cured_by_repo now owns grouping, persisted checkout
    # verification, blind-repo attribution, and first-class ambiguity counts.
    cured, problems, facts = dispatches.cured_by_repo(snap)
    diagnostics = [str(problem) for problem in problems]
    unplaceable = facts["blind_rows"]
    ambiguous = facts["ambiguous"]
    fatal = bool(facts["eligible"] and not facts["scanned_repos"] and problems
                 and not all("no verified checkout" in str(p) for p in problems))

    out = []
    for entry in cured or ():
        try:
            row, (branch, tip, ahead) = entry
        except (TypeError, ValueError):
            continue
        out.append({
            "row": str(row.get("id") or "")[:12],
            "lane": str(row.get("lane") or "") or None,
            # SENDER, NOT `to` — and `to` NEVER EXISTED (measured
            # live: all 8 cards rendered "unassigned"). A dispatch row carries
            # `sender` and `recipient`; my fixture INVENTED `to`, so the gate
            # was green against a field production does not have. The browser
            # check showed "unassigned" on every card and I read it as a real
            # value rather than as the absence it was.
            #
            # SENDER is also the semantically right one here, which is the part
            # a rename would have missed: on a cured-but-unhanded-back row the
            # person who owes the next action is the AUTHOR, not the reviewer.
            # The reviewer is carried separately as context.
            "seat": str(row.get("sender") or "") or None,
            "reviewer": str(row.get("recipient") or "") or None,
            "since": str(row.get("ts") or "") or None,
            "branch": str(branch or "") or None,
            "tip": str(tip or "")[:12] or None,
            "ahead": ahead if isinstance(ahead, int) else None,
        })
    return (tuple(out), "; ".join(diagnostics) if diagnostics else None,
            unplaceable, fatal, ambiguous)


def _cured_block(snap, unavailable):
    """The cured bucket as its own object, with its own state.

    INDEPENDENT OF THE OWED HALF ON PURPOSE. These are two different questions
    over two different sources, and one going blind must not blank the other —
    an owner who loses the unanswered-FIX list should still see the nine rows
    whose authors cured and never handed back. Coupling them would mean a git
    hiccup in one walk erases a list that was answerable.
    """
    try:
        rows, unavailable, unplaceable, fatal, ambiguous = _cured_rows(
            snap, unavailable)
    except Exception as exc:                           # noqa: BLE001
        return {"unavailable": True,
                "why": "the cured-fix bucket failed to render: %s" % (exc,)}
    if fatal:
        return {"unavailable": True, "why": str(unavailable)}
    rows = list(rows)
    shown = rows[:ROW_CAP]
    return {"unavailable": False, "partial": bool(unavailable),
            "why": str(unavailable) if unavailable else None,
            "rows": shown, "total": len(rows) + unplaceable + ambiguous,
            "measured": len(rows), "ambiguous": ambiguous,
            "shown": len(shown),
            "truncated": max(0, len(rows) - len(shown)),
            # THE THIRD STATE, carried to the surface rather than folded into
            # the total: rows whose repository could not be placed, so we
            # cannot say whether they are cured. Silently shrinking the count
            # by these is the failure this whole lane exists to stop.
            "unplaceable": unplaceable}


def _owed_body(snap, unavailable):
    """The burn-down, as JSON, from ONE ledger reading. Never 500, never an
    empty-looking backlog.

    TWO BUCKETS, TWO OBLIGATION TYPES, ONE FETCH, ONE FOLD: `rows` are chains
    carrying a FIX nobody answered; `cured` are rows whose author cured and
    never re-dispatched. They are not rival answers to one question — see
    _cured_rows — but they ARE two questions over the same rows, so they get
    the same rows rather than two readings of the file they came from.
    """
    cured = _cured_block(snap, unavailable)
    try:
        try:
            from . import obligation
        except ImportError:
            return {"unavailable": True,
                    "why": "helm.obligation is not on this trunk yet — the "
                           "burn-down source lands with lane/"
                           "obligation-delivery-seam",
                    "cured": cured}
        try:
            # BOTH ARGUMENTS OR NEITHER — that function's own contract, and
            # the reason the failure is passed down beside the rows rather
            # than being re-discovered here. Handing it the reading is what
            # makes this build ONE fold instead of two.
            items, forks, unreadable = obligation.unanswered_fixes(
                snap, unavailable)
        except Exception as exc:                       # noqa: BLE001
            return {"unavailable": True,
                    "why": "the obligation ledger could not be read: %s"
                           % (exc,),
                    "cured": cured}
        if unreadable:
            return {"unavailable": True, "why": str(unreadable),
                    "cured": cured}

        items = list(items or ())
        # UNDECLARED IS NOT OWED, AND MERGING IT ASSERTED THAT UNKNOWNS ARE
        # DEBTS (post-land review of lane 23). cmd_owed defines an
        # undeclared verdict as NEITHER owed nor clear — a third state, born
        # because the two-way reading is what made 26 of these vanish from a
        # burn-down whose whole job is completeness. Rendering one count over
        # both put them back, in the opposite direction: the owner read 116
        # debts where 91 were owed and 26 were unanswerable.
        #
        # It over-reported in the SAFE direction, which is exactly why it would
        # have survived: nobody is alarmed by a backlog that looks too long.
        # Measured on the live ledger at cure time: 117 = 91 + 26.
        # Partitioned BY KIND in one pass rather than by membership: `r not in
        # undeclared` compares dicts by value, so two genuinely distinct rows
        # that happen to carry equal fields would collapse into one bucket.
        undeclared_items, owed_items = [], []
        for r in items:
            kind = str((r or {}).get("kind") or "")
            (undeclared_items if kind == obligation.UNDECLARED_VERDICT
             else owed_items).append(r)
        rows = _rows_from(owed_items)
        total = len(rows)
        # NO SILENT CAP. A truncated list that does not say it was truncated
        # reads as the whole backlog, and the owner would burn down a number
        # that was never the number.
        shown = rows[:ROW_CAP]
        return {
            "unavailable": False,
            "rows": shown,
            "total": total,
            "shown": len(shown),
            "truncated": total - len(shown),
            "forks_excluded": len(list(forks or ())),
            "undeclared": {
                "rows": _rows_from(undeclared_items)[:ROW_CAP],
                "total": len(undeclared_items),
                "truncated": max(0, len(undeclared_items) - ROW_CAP),
            },
            "cured": cured,
        }
    except Exception as exc:                           # noqa: BLE001
        # The endpoint's own failure is still a THIRD state, not an empty
        # backlog — see the module docstring.
        return {"unavailable": True,
                "why": "the burn-down surface failed to render: %s" % (exc,),
                "cured": cured}


def _owed_fingerprint():
    """The identity of the FILE this body is computed from — the term
    `_cached_swr` compares to decide whether a rebuild is due.

    THE STAT IS `web_cache`'s, DELIBERATELY, and the same split `_lr_fingerprint`
    argues for one module over: this reading is taken OUTSIDE the build, to
    decide whether to build, so it carries mode, inode and ctime as well as
    mtime and size — a `chmod 000` that makes the ledger unreadable moves this
    term and therefore rebuilds, which is the direction that can only cost work.

    THE FILES ARE THE ONES THE BUILD WAS MEASURED OPENING, not the ones it
    looked like it would read. A build traced through `open` touched exactly
    two paths under the helm root: the dispatch ledger once, and the gate
    epoch marker on nearly every folded row. The marker names which founding
    event the fold trusts, so a marker that moves changes what this body says
    — listing the ledger alone would have left a real input unwatched, and the
    guess that "it only reads the ledger" is precisely what the trace refuted.

    GIT IS NOT A FILE AND THE OMISSION IS BOUNDED RATHER THAN OVERLOOKED. The
    cured half reads a branch, a tip and a distance ahead of trunk, and no file
    identity can see a ref move. That is what `_OWED_UNCHANGED_MAX_S` is for: a
    match may hold the body only for that window, so a change this term cannot
    see is picked up inside it instead of never.

    None is NO IDENTITY, never a matching one: a caller that cannot say what
    it read must rebuild."""
    from . import dispatches, web_cache
    try:
        return web_cache._input_fingerprint(
            [dispatches.ledger_path(), dispatches.epoch_path()])
    except Exception:                                  # noqa: BLE001
        return None


def _owed_build():
    """ONE ledger reading, ONE memo scope, one body. Never raises.

    THE SCOPE IS THE TREE'S, NOT A NEW CACHE. `projscope.scope()` is helm's
    one-instant memo, the same one `helm dispatch`'s read verbs open around a
    listing; inside it the vcs seam answers a byte-identical git READ once per
    pass instead of once per asker, and `rowworld`'s head-tree resolution is
    asked once. Entering starts an empty cache and LEAVING DROPS IT, so no
    answer here can outlive the build that asked for it — there is no ttl and
    no process-lifetime memo, which is exactly why a scope is safe where a new
    cache beside it would not be.

    NEVER RAISES, because what this returns is what gets CACHED. A build that
    escaped with an exception would leave `_cached_swr` with no entry to
    replace, and the next reader would fall through to the blocking path and
    pay the whole fold to learn the same thing. Every failure is a body that
    SAYS it is unavailable, which is the state the console already draws.

    `read_ts` is the instant of the READ, not of the response: the body may be
    served for as long as the hard cap allows, and `_api_owed` turns this into
    the age the card prints."""
    from . import dispatches, projscope
    started = time.time()
    with projscope.scope():
        try:
            snap, unavailable = dispatches.snapshot()
        except Exception as exc:                       # noqa: BLE001
            # A READ THAT THREW IS UNAVAILABLE, NEVER AN EMPTY LEDGER. Both
            # buckets are handed the failure, so neither can render a
            # confident zero over a fold that did not happen.
            snap, unavailable = {}, ("the dispatch ledger could not be read "
                                     "(%s: %s)" % (type(exc).__name__, exc))
        try:
            body = _owed_body(snap, unavailable)
        except Exception as exc:                       # noqa: BLE001
            body = {"unavailable": True,
                    "why": "the burn-down surface failed to render: %s" % (exc,),
                    "cured": {"unavailable": True,
                              "why": "the burn-down surface failed to render"}}
    body["read_ts"] = started
    return body


def _api_owed():
    """The burn-down, as JSON — SERVED STALE WHILE IT REBUILDS.

    THE FLAP THIS CLOSES, measured on the owner's box: the build costs about a
    minute and the card fetches it with an eight second deadline every
    forty-five seconds, so the burn-down ALWAYS rendered "timed out" — and each
    abandoned fetch left a fold running, so one open tab kept the web process
    folding the ledger permanently and every other endpoint slowed down behind
    it. Serving the previous body immediately is what breaks that loop: one
    background rebuild per floor, and the poll gets an answer in milliseconds.

    THE AGE IS RESOLVED AT RESPONSE TIME, not at build time, because the body
    may be up to `_OWED_HARD_TTL_S` plus one build old and the card's whole honesty rests on
    saying so. A body with no `read_ts` — the cold `warming` placeholder, or a
    body built before this field existed — gets NO age rather than a
    fabricated zero: the renderer prints "unknown", which is true.

    NOTHING IS PERSISTED. `_cached_swr` writes a body to disk only for a caller
    that supplies a read-set snapshot able to identify what it served, and this
    endpoint has none — so a server restart costs one cold build, answered as
    `warming`, instead of restoring a body nobody can date against its input.
    """
    from . import web_cache
    started = time.time()
    try:
        # COLD ANSWERS "WARMING", NEVER "UNREADABLE": a cache that has not
        # COMPUTED the burn-down and a ledger that cannot be READ are
        # different facts, and only the server can tell them apart. The card
        # checks `warming` before `unavailable` for exactly that reason.
        body = dict(web_cache._cached_swr(
            "owed", _OWED_FLOOR_S, _OWED_HARD_TTL_S, _owed_build,
            cold_body={"warming": True},
            cold_wait=_OWED_COLD_WAIT_S,
            # THE REBUILD FIRES ON A CHANGE, NOT ONLY ON A CLOCK. An idle
            # ledger costs three thousand git spawns a fold, and paying that
            # every floor for a file nobody wrote to is the shape /api/lr
            # already measured and refused. The cap is what keeps this from
            # becoming the mtime memo that surface killed — read both
            # `_owed_fingerprint` and `_cached_swr` before touching either.
            fingerprint=_owed_fingerprint,
            unchanged_max=_OWED_UNCHANGED_MAX_S))
    except Exception as exc:                           # noqa: BLE001
        # THE ENDPOINT MAY NOT 500 AND MAY NOT LOOK EMPTY. `_owed_build` does
        # not raise, so reaching here means the CACHE itself failed; the answer
        # is still the named third state rather than a happy board.
        return {"unavailable": True,
                "why": "the burn-down could not be served: %s" % (exc,),
                "cured": {"unavailable": True,
                          "why": "the burn-down could not be served"}}
    read_ts = body.pop("read_ts", None)
    if not isinstance(read_ts, (int, float)):
        body["read_age_s"] = body["projected_age_s"] = None
        return body
    # THE READING'S AGE, NOT THE PROJECTION'S — the same split /api/lr makes:
    # a fingerprint match that confirmed the ledgers unmoved is a read of them,
    # and the burn-down's freshness bound judges the newest read while the
    # projection's own age stays beside it.
    body["projected_age_s"] = max(0, int(started - read_ts))
    body["read_age_s"] = max(0, int(
        started - max(read_ts, web_cache.verified_at("owed") or 0)))
    return body
