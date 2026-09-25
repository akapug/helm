"""Land-pipeline projection for :mod:`helm.web`."""
# `os` is imported rather than inherited from the web fanout below: the
# building band resolves this helm's own checkout from `__file__`, and a
# NameError inside its try would have been reported to the owner as "the lane
# rooms could not be read" — a true-sounding sentence about the wrong world.
import os
import sys
# EXPLICIT, NOT INHERITED — the rule web_core already states one name at a
# time, applied to the whole family. Each name below reached this module
# ONLY through the globals() splice under this block, so it was bound when
# web.py had already been imported and ABSENT on a direct `from helm import
# <this module>`: a NameError at the first call, or a NameError swallowed by
# a fail-open. Measured in web_common, whose code_drift answered "no drift"
# from an unbound `time` — the half-live detector silenced by an import
# order. The binding is identical either way (web.py imports the same module
# object, and the facade fanout rebinds it over this one), so naming it here
# costs nothing and removes the ordering dependency.
import time

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})


_SCHEDULER_GROUP_LIMIT = 50
_SCHEDULER_OWNER_LIMIT = 20


def _lr_project(now, newest, all_projects=False):
    """One landreq.project_raw() walk fanned out into the card's lists.

    The raw rows carry cancelled chain transit and must belong to the same
    ledger instant as the projection. They let every chain-aware classifier,
    including the primary loop list, apply one validated frontier rather than
    rebuilding a narrower or later graph.

    PROJECT-SCOPED BY DEFAULT (task/974): ONE scope resolution feeds both the
    marking and the `withheld` disclosure, so the rows were classified
    against exactly the identity the card names. `all_projects=True` is the
    escape (`/api/lr?all_projects=1`): the same lists unfiltered, every
    foreign row carrying its project label.

    THE SAME BINDING FOR GIT, AND IT IS THE READ-SET RATHER THAN A PIN BESIDE
    ONE. `recent_lands` below reads trunk, every row here is proved against
    trunk, and the freshness witness used to fingerprint trunk before either —
    three readings of a ref a fetch can move, so a body could mix two trunks and
    be stored under a witness describing a third. `project_raw`'s `cache`
    argument IS the read-set, so `_trunk_refs` and `_resolved_ref` memoise the
    rows' own trunk readings into the record the witness is projected from.
    Outside a snapshot this is a throwaway read-set and `project_raw` behaves
    exactly as it always has for `helm lr` and every non-web caller.

    AND THE ATTEST SIDECAR IS IDENTIFIED HERE. `project_raw` reads it per row —
    attestation rides the badges — and an append changes what this card says
    with no ledger write, because the sidecar is a separate file precisely so it
    can move independently. The read-set records the identity of the file this
    build consumed; nothing else in the body has an inode to record.
    """
    from . import landreq, web_land_model
    scope = landreq.board_scope()
    reads = web_land_model._lr_reads()
    reads.identity("attest")
    # TRUNK IS PINNED ONCE FOR THE WHOLE BUILD, HERE, AND IT IS THE BUILD'S PIN
    # RATHER THAN ANY ONE LEG'S. No leg in this body reads trunk for its own
    # sake: `recent_lands` reads the ledger, and the only git reading left is
    # the landedness proof per row. Without an explicit pin the resolution
    # happens only as a SIDE EFFECT of there being rows to prove, so a body with
    # NO rows resolves no trunk at all and its saved witness covers the ledgers
    # and the premise store while omitting `refsha` — a force-fetch then cannot
    # age that body out. The pin belongs to the build for exactly that reason.
    #
    # ONE DOOR, NOT A SECOND READING: `reads` IS the read-set every leg below
    # goes through, so this resolves the commit the rows are then proved against
    # rather than taking a reading beside them. An unresolvable repo or ref is
    # left alone — each leg already answers UNKNOWN in its own words for that,
    # and a pin that raised here would take a whole board down for a fact the
    # board can state.
    try:
        _gitdir, _trunk = reads.repo()
        if _gitdir and _trunk:
            reads.refsha(_gitdir, _trunk)
    except Exception:                    # noqa: BLE001 — every leg reports it
        pass
    lrs, raw, unavailable = landreq.project_raw(now, scope=scope, cache=reads)
    out = {"read_ts": now, "ledger_mtime": newest, "unavailable": unavailable,
           # gate.receipts()' skipped-row count; the gate lane fills this in,
           # and the card prints it only when it is a number.
           "receipts_skipped": None,
           # None, not a dict of zeros: the filed split is UNKNOWN until the
           # projection below actually reads the population (law 1). The card
           # prints the strip only when the numbers exist.
           "filed": None,
           # None, not zeros, SAME LAW (task/974): the withheld disclosure is
           # a claim about a population this body has not read yet. On every
           # unavailable body it stays None, so the card says scope UNKNOWN
           # rather than "0 withheld" — never a confident narrower board.
           "withheld": None,
           "all_projects": bool(all_projects),
           # PRIVATE warm payload: every non-terminal card from this SAME
           # project_raw snapshot. _api_lr consumes it into the canonical
           # scheduler model and removes it before the response; keeping it in
           # the cached body avoids both a second ledger walk and a partial
           # frontier-only graph.
           "_scheduler_rows": [], "_scheduler_active_ids": [],
           "loops": [], "stalled_ids": [], "unmeasurable": [],
           "closed_recent": [], "closed_total": 0, "closed_unknown_when": 0,
           # THE NATIVE CHAIN STAYS INDEPENDENT: it reads the premise store,
           # so it does not go UNKNOWN because the dispatch ledger did.
           #
           # `recent_lands` DELIBERATELY DOES NOT, and the dependency is the
           # property rather than a cost. A lands reading taken ahead of this
           # bail would need a source of its own, and a second source is free to
           # disagree with the ledger `helm lr list` renders — which is how a
           # lands card comes to show weeks-old rows on a day full of lands. The
           # owner's rule is that every card renders from the same source the
           # command line reads, so the lands come off `lrs` below, after the
           # projection, and an unreadable ledger makes them UNKNOWN in the same
           # words as every other list in this body.
           "recent_lands": None,
           "native_chain": _lr_native_chain()}
    if unavailable:
        # THE LANDS LEG RELAYS THE PROJECTION'S OWN REASON. Leaving the field
        # None would reach the renderer as "the server predates this reading",
        # which is a fact about the SERVER when the truth is a fact about the
        # RECORD.
        out["recent_lands"] = web_land_model._lr_recent_lands(
            unavailable=unavailable)
        return out                       # UNKNOWN, and it says so. No rows.
    try:
        loop_rows = landreq._loop_rows(lrs, raw, all_projects=all_projects)
        # THE RAW ROW PRODUCT RIDES THE BODY TOO (task/1821, owner-directed).
        # `helm lr list --json` promises raw rows, and until this field the
        # warm path could not honor that promise, so the machine-readable
        # question paid the cold replay every time — measured 90s against the
        # card's 307ms over the same ledger. These are the SAME objects
        # `_loop_rows` returned from the SAME project_raw snapshot: no second
        # walk, no second instant. On any refusal below the field is REMOVED
        # rather than emptied — an absent field reads "fall back cold", an
        # empty list would read "zero rows" confidently.
        out["rows"] = loop_rows
        # EVERY NON-TERMINAL ROW, not only the carrying chain frontiers in
        # `loops`. Build each card ONCE from this projection's already-computed
        # truth, then let both list and graph select from the same objects.
        scheduler_rows = [lr for lr in lrs.values()
                          if not lr["terminal"] and
                          (all_projects or landreq._this_boards_row(lr))]
        cards = {lr["id"]: landreq.card(lr) for lr in scheduler_rows}
        out["_scheduler_rows"] = list(cards.values())
        out["loops"] = [cards[lr["id"]] for lr in loop_rows]
        out["_scheduler_active_ids"] = [
            card["id"] for card in out["loops"] if not card.get("honored")]
        out["stalled_ids"] = [lr["id"]
                              for lr in landreq._stalled_rows(
                                  lrs, raw, all_projects=all_projects)]
        # Wire name retained for older clients; this is nonbillable dwell,
        # not projection health. New clients display the explicit categories.
        out["unmeasurable"] = [{"id": lr["id"], "reason": reason,
                                "kind": landreq.review_hold_kind(lr)}
                               for lr, reason in
                               landreq._unmeasurable_rows(
                                   lrs, raw, all_projects=all_projects)]
    except landreq._ChainUntrustworthy as e:
        # ABSENT, NEVER EMPTY (task/1821): a `rows` list attached before this
        # refusal would survive it as a confident zero-or-partial answer.
        out.pop("rows", None)
        out["unavailable"] = str(e)
        return out
    # THE DISCLOSURE RIDES EVERY RENDERED BODY, from the same scope that
    # classified the rows (one resolution above). Dict exactly when the board
    # rendered; None on every unavailable body — filed's law, same reason.
    out["withheld"] = landreq.withheld_split(lrs, scope)
    # FILED — the whole all-time population behind the board, from the SAME
    # project_raw snapshot (no second ledger walk; the recompute floor holds).
    # The owner asked "how many rows were ever filed here?" and until this
    # term the surface only knew a 24h closed window. The derivation has ONE
    # owner — landreq.filed_split, which `helm lr list`'s headers render too,
    # so the two surfaces cannot disagree about one record — and it runs only
    # past every refusal above, so the invariant is one sentence: `filed` is
    # a dict exactly when the board rendered, and None on every unavailable
    # body — never a confident zero (law 1). Every term is knowable here
    # because project_raw refuses wholesale on a partial read.
    #
    # THE CENSUS IS WALKED ONCE AND READ THREE TIMES. `census_verdicts` is
    # the walk `filed_split` takes for the header's split; the same answer is
    # stamped onto each card as `frontier` / `frontier_rung`, where the
    # scheduler's collapsed lines and the kanban's read it (task/2381: the
    # owner's board lists live obligations and folds the rest into one line
    # per class). The cards are the objects `loops` and `_scheduler_rows`
    # share, so the list, the graph and the strip describe one walk of one
    # instant, and a card the census never classified carries None — no
    # frontier claim at all, never a guess.
    verdicts = landreq.census_verdicts(lrs, raw)
    for rid, card in cards.items():
        verdict = verdicts.get(rid) or {}
        card["frontier"] = verdict.get("reason")
        card["frontier_rung"] = verdict.get("rung")
    out["filed"] = landreq.filed_split(lrs, raw, verdicts=verdicts)
    # THE LANDED CARD, FROM THIS SAME SNAPSHOT (task/2355). One walk feeds the
    # in-flight lists, the closed footer and the lands card, so the three
    # cannot describe three different instants of one ledger.
    out["recent_lands"] = web_land_model._lr_recent_lands(
        lrs, all_projects=all_projects)
    # REVERSE-SKEW ALIAS, ON THE WIRE ONLY — and I cured the forward direction
    # first and called skew handled. Forward is old-SERVER/new-browser,
    # which the console's own fallback covers. THE OTHER DIRECTION IS WHAT A
    # RESTART PRODUCES: a browser open since before the rename still asks for
    # `filed.in_flight`, the new server emits only `open`, and the strip renders
    # "? in flight" until someone reloads. That is a REGRESSION this lane
    # introduced — that client worked before it.
    #
    # IT LIVES HERE, NOT IN filed_split, because filed_split's buckets PARTITION
    # the ledger exactly and its own docstring leans on that ("sum the strip and
    # you get the total, so no bucket can silently leak rows"). A duplicate key
    # inside the partition would break the property to solve a transport
    # problem. Compatibility is a wire concern; the projection stays clean.
    #
    # SAME NUMBER, NOT A SECOND PREDICATE: both keys answer "how much is on the
    # books", and nothing renders both — the rendered WORD is what the
    # one-noun-one-predicate law governs, and this is a key. An old client shows
    # the number under its old label, exactly as it did before this lane, so no
    # worse; a new client reads `open` and is right.
    # REMOVABLE once no browser session predates the rename — a reload, not a
    # deploy.
    out["filed"]["in_flight"] = out["filed"]["open"]
    # NOT "landed_recent": TERMINAL covers SUPERSEDED, withdrawn, ABANDONED
    # and closed-by-landing rows too, and a withdrawn lane counted under "landed
    # today" is a congratulation the record never issued. Each row prints its
    # own state; the footer only claims they are CLOSED.
    #
    # THE WINDOW IS MEASURED FROM THE CLOSURE, NOT FROM THE VERDICT. Dating it
    # from `entered_ts` asked the wrong question: an approve from three days ago
    # whose change merges NOW leaves the in-flight list at the same instant and
    # was ALSO outside a 24h window measured from its verdict, so it vanished
    # off the card entirely while the footer said "nothing closed in the last
    # 24h" — a confident claim over a lane that closed a minute earlier
    # (reproduced). `closed_ts` is the closure instant when the record
    # carries one, and None for a git-OBSERVED landing, which carries none
    # anywhere.
    closed, unknown_when = [], 0
    for lr in lrs.values():
        if not lr["terminal"]:
            continue
        # SAME SCOPE AS THE LISTS ABOVE (task/974): a foreign lane closing is
        # not this board's closure to announce. Withheld terminals are inside
        # the `withheld` numbers already — counted over every marked row of
        # this same projection — so nothing leaves silently.
        if not (all_projects or landreq._this_boards_row(lr)):
            continue
        ts = _lr_epoch(lr.get("closed_ts"))
        where = _lr_window(ts, now)
        if where == "in":
            closed.append((ts, landreq.card(lr)))
            continue
        if where == "out":
            continue                     # dated, and genuinely older than 24h
        # NO USABLE CLOSURE INSTANT — no stamp at all, one that is not a
        # timestamp, or one dated in the future. `entered_ts` is still a LOWER
        # BOUND, because a loop cannot close before it entered the state it
        # closed from. A lower bound inside the window therefore PROVES the
        # closure is inside it. Outside — or unusable in its own right — the
        # closure is any instant between then and now, and this card may not
        # pick one: the row is counted as closed-at-an-unknown-time instead of
        # being dropped.
        lower = _lr_epoch(lr.get("entered_ts"))
        if _lr_window(lower, now) == "in":
            closed.append((lower, landreq.card(lr)))
        else:
            unknown_when += 1
    closed.sort(key=lambda row: -row[0])
    # THE TOTAL IS SENT SEPARATELY FROM THE CAPPED LIST. The card used to print
    # `closed_recent.length`, so a 13th closed lane rendered as the number 12 —
    # a silent truncation presented as a count. A cap is a display budget; it
    # may never edit the number.
    out["closed_total"] = len(closed)
    out["closed_recent"] = [c for _ts, c in closed[:_LR_CLOSED_CAP]]
    out["closed_unknown_when"] = unknown_when
    return out



def _lr_fingerprint():
    """The identity of every FILE the land projection is computed from.

    TWO HALVES IN TWO MODULES, DELIBERATELY. `web_land_model` enumerates the
    paths, because that is where the projection's inputs are already named; the
    STAT is `web_cache`'s, because this reading is taken outside the build, to
    decide whether to build, and a reading the body never made must never enter
    the body's witness. Putting the stat in the model would also be a read of a
    moving input taken AROUND the read-set, which that module forbids and a
    scanner enforces.

    None is NO IDENTITY, never a matching one: an input whose location will not
    resolve leaves the caller unable to say what it read, and a reader that
    cannot say what it read must rebuild."""
    from . import web_cache, web_land_model
    return web_cache._input_fingerprint(web_land_model._lr_fingerprint_paths())


def _lr_build():
    """One projection, at most once per _LR_FLOOR_S, and only when an input
    moved.

    THERE IS STILL NO SHORT-CIRCUIT IN HERE, and that has not changed: this
    function performs the walk unconditionally every time it is called. What
    decides whether it is CALLED lives one layer out, in `_cached_swr`, and the
    distinction is the whole safety argument.

    A "the ledger mtimes did not move, reuse the last report" short-circuit
    HERE is forbidden, and the failure it produces is measured rather than
    feared: `chmod 000` makes the dispatch ledger unreadable while touching
    NEITHER its mtime nor its size, so an mtime-keyed memo inside this function
    serves the last healthy board indefinitely — every row green, nothing on
    screen changed — over a record that can no longer be read at all. A cache
    that can pin a stale board over an UNKNOWN one defeats the single property
    this surface exists for.

    THE INPUT FINGERPRINT THAT GATES THE REBUILD IS NOT THAT, and it is not in
    this function. It carries mode, inode and ctime as well as mtime and size,
    so the chmod above moves it; and it is capped by `_LR_UNCHANGED_MAX_S`, so
    no match — and no input the fingerprint failed to consider — can hold the
    board past that window. See `_lr_fingerprint` and `_cached_swr`.

    The floor bounds the COST, the fingerprint bounds the REBUILD, and the cap
    is what keeps either from bounding the TRUTH."""
    now = time.time()
    try:
        newest = _lr_newest_mtime()
    except Exception:
        newest = None       # a display term, never a reason to skip the read
    return _lr_project(now, newest)



def _lr_build_all():
    """The `--all-projects` escape body — its own build under its OWN cache
    key, because the two answer DIFFERENT questions and a cache that serves
    one as the other is the capped-partial failure with a query string on it.
    Built only when someone asks; the default card never pays for it."""
    now = time.time()
    try:
        newest = _lr_newest_mtime()
    except Exception:
        newest = None
    return _lr_project(now, newest, all_projects=True)



_LR_BUILDING_CAP = 12


def _lr_building(filed_lanes, root=None):
    """WHAT HANDS ARE DOING RIGHT NOW, from the lane rooms rather than the
    dispatch ledger — the band `helm work list` prints and this card did not.

    WHY IT EXISTS, and it is the owner's own reading. He opened this card over
    a fleet with three live builds on it and read "0 in flight", then asked
    whether that could be true while work was plainly ongoing. Both halves
    were true: the card counts LAND REQUESTS, a land request is FILED at the
    end of a build, and nothing had been filed yet. A card that says zero
    while hands are working teaches him not to trust it, and an untrusted card
    is worth less than no card — so the number he could not see gets a reading
    of its own rather than a redefinition of the one he could.

    IT IS A SECOND SOURCE ON PURPOSE, AND THAT IS THE WHOLE VALUE. The rest of
    this body is the dispatch ledger; this is `helm work list` — the claims
    table joined to the worktree registry, ahead/behind measured against the
    trunk ref. An unreadable ledger cannot silence it, which is exactly the
    direction the owner needs: the worse the ledger is, the more this band is
    the only thing on screen that knows hands are moving.

    READ AT RESPONSE TIME, NOT IN THE CACHED BODY. `_lr_project` runs behind a
    fingerprint that covers the LEDGER files; a lane commit and a lease move
    neither of them, so a `building` term inside that body could be up to
    `_LR_UNCHANGED_MAX_S` old while presenting as the same reading as the rows
    beside it. The owner asks queue is read at response time for the same
    reason one screen down. The cost is what makes that affordable:
    `only_held=True` skips the per-room git reads for every room with no claim
    on it (measured 0.3s over the six held rooms here against 2.3s for all
    118), and `gc=False` keeps a card that polls on a timer from being the
    thing that expires the leases it is displaying.

    BUILDING IS: a GUARDED lane room, holding a claim whose holder is not
    provably dead, whose branch is measurably AHEAD of the trunk ref, and whose
    lane is NOT already among the live in-flight rows. `filed_lanes` is that
    last term — the lane names the header counts as "in flight" — so the two
    numbers beside each other partition the work instead of double-counting it,
    and a lane whose request is filed LEAVES this band in the same read that
    puts it in the other one.

    AND A LANE WHOSE DISTANCE COULD NOT BE MEASURED IS NEITHER COUNTED NOR
    DROPPED. `list_rows` answers "?" for ahead/behind when git could not say,
    and "?" is not "0": counting it as building asserts commits nobody read,
    dropping it asserts none exist. It is counted under `unmeasured` and said
    out loud, the same law the closed-window footer takes for a row it cannot
    date.

    `root` IS AN ARGUMENT SO IT CAN BE MEASURED. The default resolves this
    helm's own checkout; an arm passes a fixture tree, so the predicate above is
    exercised against real lane rooms, real claims and a real trunk rather than
    against a hand-built list of dicts that agrees with whatever this function
    happens to do.

    -> {"rows": [...], "total": N, "unmeasured": N, "source": "helm work list",
        "unavailable": None or WHY}. `rows` is capped for display; `total` is
    never the length of a capped list."""
    out = {"rows": [], "total": 0, "unmeasured": 0,
           "source": "helm work list", "unavailable": None}
    try:
        from . import work
        # THE TREE THIS HELM CAME FROM, never the process cwd: `helm web` is a
        # long-running server and `find_root` folds a lane room back to the
        # shared checkout. Same resolution `ready.signal_checkout` takes, and
        # for the same reason.
        root = root or work.find_root(
            os.path.dirname(os.path.abspath(__file__)))
        if not root:
            out["unavailable"] = ("this helm did not come from a git checkout "
                                  "— lane rooms UNKNOWN")
            return out
        rows = work.list_rows(root, gc=False, only_held=True)
    except Exception as e:                   # noqa: BLE001 — named, never empty
        out["unavailable"] = ("the lane rooms could not be read (%s) — what is "
                              "BUILDING is UNKNOWN, not zero"
                              % type(e).__name__)
        return out
    building = []
    for r in rows:
        if r.get("stale"):
            # A claim whose holder is PROVABLY DEAD. `helm work list` splits
            # these out under their own heading and offers `release --stale`;
            # calling them building would put a live-work claim on the owner's
            # home screen for a seat that is gone.
            continue
        if r.get("lane") in filed_lanes:
            continue
        if (r.get("landed") or {}).get("state") == work.LANE_LANDED \
                and not r.get("dirty"):
            # ITS WORK IS ON THE TRUNK, and a lane landed by patch identity is
            # still AHEAD by object id, so `ahead` alone calls it building.
            # Same verdict `helm work list` prints beside the row. A DIRTY
            # room is not left out: uncommitted work is in flight, and the CLI
            # prints it DIRTY and `helm lr foldcheck` keeps it.
            continue
        ahead = r.get("ahead")
        try:
            n = int(ahead)
        except (TypeError, ValueError):
            out["unmeasured"] += 1
            continue
        if n <= 0:
            continue                         # sitting at trunk: not building
        building.append({"lane": r.get("lane"), "holder": r.get("holder"),
                         "ahead": n,
                         # WHAT THE RECORD ACTUALLY CARRIES. A claim row has an
                         # expiry and a LAST-RENEWED stamp; it does not carry
                         # the instant the lease was first taken, so there is no
                         # honest "lease age" to render. This is the same number
                         # `helm work list` prints, under the same words.
                         "lease_remaining_s": r.get("remaining"),
                         "dirty": bool(r.get("dirty")),
                         "liveness": r.get("liveness")})
    building.sort(key=lambda row: (-row["ahead"], row["lane"] or ""))
    out["total"] = len(building)
    out["rows"] = building[:_LR_BUILDING_CAP]
    return out


def _api_lr(qs):
    """LAND PIPELINE — every land loop as the dispatch ledger and git report
    it, for the card at the top of the home tab. Read-only; the CLI equivalents
    are `helm lr list` / `helm lr stalls`.

    Fail-LOUD, not fail-open: a surprise still answers 200 (the page must not
    die), but it answers with a NAMED `unavailable` and no rows, because the
    one thing this endpoint may never do is report a healthy empty pipeline it
    did not actually read."""
    started = time.time()
    from . import web_land_model
    # THE ESCAPE PARAM (task/974): ?all_projects=1 serves the unfiltered
    # view under its OWN cache key — two different questions, two bodies.
    all_p = _q1(qs, "all_projects", "") not in ("", "0")
    key, build = ("lr_all", _lr_build_all) if all_p else ("lr", _lr_build)
    try:
        # Serve-stale-while-revalidating: the projection runs 22-28s and the
        # card aborts its fetch at 12s, so the blocking `_cached` path made
        # every TTL lapse render as pipeline UNKNOWN. Stale bodies are served
        # age-stamped (read_age_s below) while the rebuild runs; past the hard
        # cap the read blocks again rather than serving an unbounded past.
        # COLD START ANSWERS "WARMING", NEVER "UNREADABLE" — see _cached_swr.
        body = dict(_cached_swr(key, _LR_FLOOR_S, _LR_HARD_TTL_S, build,
                                cold_body={"warming": True},
                                cold_wait=_LR_COLD_WAIT_S,
                                # THIS ENDPOINT'S READ-SET — ONE DOOR, TWO
                                # PRODUCTS. It is held open around the build, so
                                # every moving input the body consumes is served
                                # and recorded once; the witness is then a PURE
                                # PROJECTION of that record rather than a second
                                # reading taken beside it. It is also what opts
                                # this key into persistence at all: a snapshot
                                # that cannot identify what it served neither
                                # saves nor restores, the safe direction in both
                                # halves.
                                #
                                # THIS LINE IS WHERE THE CURE LIVES. Everything
                                # in web_land_model degrades to a throwaway
                                # read-set when no snapshot is open, so a
                                # version of this fix that forgot the wiring
                                # would still READ as bound and prove nothing —
                                # which is why there is ONE argument here and
                                # not a witness plus a scope that can be half
                                # supplied.
                                snapshot=web_land_model._lr_snapshot,
                                # THE REBUILD FIRES ON A CHANGE, NOT ON A
                                # CLOCK. The floor asks only how OLD the body
                                # is; a projection that costs more than the
                                # floor is therefore ALWAYS due, and the web
                                # process rebuilt continuously — measured at a
                                # full core and about eleven git spawns a
                                # second on a box whose per-toolcall hooks have
                                # a two second budget. These two arguments say
                                # "and only when an input moved, and never
                                # longer than this". Read both docstrings
                                # before touching either: the fingerprint is
                                # not the mtime memo `_lr_build` killed, and
                                # the cap is why it cannot become one.
                                fingerprint=_lr_fingerprint,
                                unchanged_max=web_land_model._LR_UNCHANGED_MAX_S))
    except Exception as e:
        reason = ("the land-pipeline read failed inside helm web "
                  "(%s) — pipeline UNKNOWN" % type(e).__name__)
        body = {"read_ts": started, "ledger_mtime": None, "receipts_skipped": None,
                "unavailable": reason, "filed": None,
                "withheld": None, "all_projects": all_p,
                "loops": [], "stalled_ids": [], "unmeasurable": [],
                "closed_recent": [], "closed_total": 0,
                "closed_unknown_when": 0,
                "recent_lands": {"rows": [], "total": None,
                                 "rows_truncated": False,
                                 "source": "helm lr list",
                                 "unavailable": reason},
                "native_chain": {"count": None, "verified": None, "detail": "",
                                 "head_index": None, "unavailable": reason}}
    # Owner asks are a separate small board input. Read them after the warm body,
    # then sample ONE response clock after BOTH reads. Sampling before `_cached_swr`
    # let a cold rebuild run past the clock; sampling before this queue read could
    # make a concurrently filed ask look future-dated in the same response.
    from . import scheduler, web_cache
    try:
        asks, asks_error = scheduler.read_owner_asks()
    except Exception as e:
        asks, asks_error = [], "owner asks could not be read (%s)" % type(e).__name__
    now = time.time()
    # THE ONLY PLACE ANYTHING BUT THE OWNER'S EYE LEARNS OF A DEAD BOARD.
    # Every card on the home tab is fanned out from this one answer, so one
    # `unavailable` blanks most of his screen at once. This record is the ONLY
    # trace of that: the endpoint logs no line and keeps no counter, and
    # nothing else on the fleet reads the board at all. `helm doctor` reads it
    # back and reports a LIVE server whose board reads are failing as a fault,
    # which is a thing no renderer can do: the renderer is the surface that
    # died.
    #
    # THE OUTCOME IS READ OFF THE BODY, not off the except arm above: a body
    # can carry `unavailable` with nothing having raised (a refused chain, an
    # unreadable ledger) and those are failed board reads too. It rides the
    # RESPONSE clock rather than sampling its own, so the record is dated by
    # the same instant the body's ages are. Best-effort by contract: `record`
    # swallows its own failures, so the recorder can never be the reason the
    # board is down.
    from . import boardread
    boardread.record(
        "failed" if body.get("unavailable") else
        ("warming" if body.get("warming") else "ok"),
        reason=body.get("unavailable"), now=now)
    # Ages are resolved at RESPONSE time, not build time: the body may be up to
    # _LR_FLOOR_S old and the card's whole freshness header depends on saying
    # so. Sending ages rather than epochs also keeps the browser's clock out of
    # it — the one comparison that could silently misreport this.
    # TWO AGES, BECAUSE THE CARD ASKS ONE QUESTION AND THE BODY ANSWERS
    # ANOTHER. `projected_age_s` is how old the PROJECTION is; `read_age_s` is
    # how long ago its inputs were last READ — the build, or the newest
    # fingerprint match that confirmed nothing had moved since (see
    # `web_cache.verified_at`). The cards judge their freshness bound on the
    # reading, and a body whose inputs were confirmed unmoved twelve seconds
    # ago is a current reading whatever its build clock says.
    built = body.get("read_ts") or now
    body["projected_age_s"] = max(0, int(now - built))
    body["read_age_s"] = max(0, int(now - max(built, web_cache.verified_at(key) or 0)))
    mtime = body.pop("ledger_mtime", None)
    body["ledger_age_s"] = None if mtime is None else max(0, int(now - mtime))
    # THE GRAPH IS A RESPONSE-TIME ADAPTER OVER THE SAME WARM BODY. Its LR rows
    # and typed active membership were built together in _lr_project from that
    # one project_raw snapshot; no ledger or git read happens here. The public
    # `loops` frontier is only a consistency witness now — never the source from
    # which active membership is reconstructed.
    scheduler_rows = body.pop("_scheduler_rows", None)
    active_ids = body.pop("_scheduler_active_ids", None)
    scheduler_vintage = None
    if not isinstance(scheduler_rows, list):
        scheduler_vintage = \
            "this warm pipeline body predates scheduler rows — graph UNKNOWN"
    elif not isinstance(active_ids, list):
        scheduler_vintage = \
            "this warm pipeline body predates scheduler membership — graph UNKNOWN"
    loops = body.get("loops")
    loop_active = []
    if scheduler_vintage is None and not isinstance(loops, list):
        scheduler_vintage = "warm pipeline loop frontier is malformed — graph UNKNOWN"
    if scheduler_vintage is None:
        for card in loops:
            if not isinstance(card, dict) or not isinstance(card.get("id"), str) \
                    or not isinstance(card.get("honored"), bool):
                scheduler_vintage = \
                    "warm pipeline loop frontier has an untyped row — graph UNKNOWN"
                break
            if not card["honored"]:
                loop_active.append(card["id"])
    if scheduler_vintage is None and active_ids != loop_active:
        scheduler_vintage = \
            "warm scheduler membership disagrees with its loop frontier — graph UNKNOWN"
    scheduler_rows = scheduler_rows if isinstance(scheduler_rows, list) else []
    active_ids = active_ids if isinstance(active_ids, list) else []
    body["scheduler"] = scheduler.project(
        scheduler_rows,
        stalled_ids=body.get("stalled_ids") or [],
        unmeasurable=body.get("unmeasurable") or [],
        active_ids=active_ids,
        owner_asks=asks,
        owner_asks_unavailable=asks_error,
        now=now,
        projection_age_s=body.get("read_age_s"),
        limit_per_group=_SCHEDULER_GROUP_LIMIT,
        owner_limit=_SCHEDULER_OWNER_LIMIT,
        unavailable=body.get("unavailable") or
                    ("pipeline is warming" if body.get("warming") else None) or
                    scheduler_vintage,
    )
    # WHAT IS BUILDING — a SECOND SOURCE, read here rather than in the cached
    # body for the reason `_lr_building` states: the fingerprint that holds the
    # body covers ledger files, and neither a lane commit nor a lease moves one.
    # The lanes it excludes are the ones the header counts as IN FLIGHT, from
    # this same response's frontier, so the two numbers beside each other
    # partition the work instead of double-counting it.
    body["building"] = _lr_building(
        {card.get("lane") for card in loops
         if isinstance(card, dict) and not card.get("honored")}
        if isinstance(loops, list) else set())
    # recent-lands ages resolve at response time too, and onto FRESH row dicts —
    # the cached body is shared across responses, and stamping ages in place
    # would let two concurrent reads tear each other's numbers. A body with no
    # recent_lands at all (a pre-field cached body surviving a half-live
    # deploy) is UNKNOWN, never a clean zero-lands reading.
    lands = body.get("recent_lands")
    if not isinstance(lands, dict):
        lands = {"rows": [], "total": None, "rows_truncated": False,
                 "source": "helm lr list",
                 "unavailable": "this pipeline body predates the "
                 "recent-lands reading — UNKNOWN, not zero lands"}
    elif not ("total" in lands and "source" in lands):
        # A VINTAGE MISMATCH, AND IT IS REACHABLE WITHOUT A HALF-LIVE DEPLOY —
        # MEASURED, not reasoned about. `_cached_swr` PERSISTS this body, so a
        # body built by the PREVIOUS producer is restored from disk into a
        # process running the new one. Its rows are fold commits carrying
        # `fold_only` and `witnessed`, they pass every shape guard the renderer
        # has, and the card would present the trunk walk's output — the exact
        # 38-day-old list the owner complained about — under the new card's
        # sentences, with its own source line vouching for it.
        #
        # NAMED, NOT REPAIRED. The rows cannot be translated: they were derived
        # from a source this build no longer reads, so there is no honest way to
        # answer them as ledger closes. Same law the `withheld` and `rows`
        # vintage checks take one screen up: say which reading is missing and
        # answer UNKNOWN. The next rebuild past the floor replaces it.
        missing = [key for key in ("total", "source") if key not in lands]
        lands = {"rows": [], "total": None, "rows_truncated": False,
                 "source": "helm lr list",
                 "unavailable": "this pipeline body was built by the previous "
                 "lands reader (no %s) and its rows are fold commits, not "
                 "ledger closes — UNKNOWN, not zero lands; it is replaced on "
                 "the next rebuild" % " or ".join(missing)}
    # THE PREDECESSOR'S ANSWER TO THIS SAME HAZARD IS RECORDED HERE BECAUSE IT
    # IS WHY THE BRANCH ABOVE IS SHAPED THE WAY IT IS. That reader's envelope
    # carried two DISCLOSURE fields (`rows_truncated`, `fold_only`), and a
    # partially old body — a dict, carrying rows, missing one of them — passed
    # every shape guard while `undefined` read as a measured `false` in the
    # browser. Its cure named the missing field and LEFT THE ROWS, because
    # `rows_truncated` was three-state and normalising an absence to None would
    # have made a MEASURED unknown and a MISSING FIELD identical.
    #
    # THE ROWS CANNOT BE KEPT NOW, and the difference is the source rather than
    # the taste. Then, the old rows were still lands read the same way, with one
    # disclosure missing. Now they are FOLD COMMITS: derived from a reader this
    # build no longer has, so there is no honest sentence to put beside them —
    # they are not ledger closes and cannot be made into any. So the branch
    # above answers UNKNOWN and names which reading is missing, which is the
    # same law applied to a body that is wrong rather than incomplete.
    rows = []
    for r in (lands.get("rows") or []):
        ts = _lr_epoch(r.get("ts"))
        rows.append(dict(r, age_s=None if ts is None else max(0, int(now - ts))))
    body["recent_lands"] = dict(lands, rows=rows)
    return body, 200
del _web
