"""Land-pipeline projection for :mod:`helm.web`."""
import sys

_web = sys.modules.get(__package__ + ".web")
if _web is not None:
    globals().update({name: value for name, value in vars(_web).items()
                      if not (name.startswith("__") and name.endswith("__"))})



def _lr_project(now, newest):
    """One landreq.project_raw() walk fanned out into the card's lists.

    The raw rows carry cancelled chain transit and must belong to the same
    ledger instant as the projection. They let every chain-aware classifier,
    including the primary loop list, apply one validated frontier rather than
    rebuilding a narrower or later graph.
    """
    from . import landreq
    lrs, raw, unavailable = landreq.project_raw(now)
    out = {"read_ts": now, "ledger_mtime": newest, "unavailable": unavailable,
           # gate.receipts()' skipped-row count; the gate lane fills this in,
           # and the card prints it only when it is a number.
           "receipts_skipped": None,
           # None, not a dict of zeros: the filed split is UNKNOWN until the
           # projection below actually reads the population (law 1). The card
           # prints the strip only when the numbers exist.
           "filed": None,
           "loops": [], "stalled_ids": [], "unmeasurable": [],
           "closed_recent": [], "closed_total": 0, "closed_unknown_when": 0,
           # BEFORE the unavailable bail on purpose: recent lands read TRUNK
           # and the native chain reads the premise store — neither goes
           # UNKNOWN just because the dispatch ledger did. That independence
           # got stronger, not weaker, when the lands stopped being read out
           # of the receipt log: git answers what landed even when both
           # ledgers are unreadable.
           "recent_lands": _lr_recent_lands(),
           "native_chain": _lr_native_chain()}
    if unavailable:
        return out                       # UNKNOWN, and it says so. No rows.
    try:
        out["loops"] = [landreq.card(lr)
                        for lr in landreq._loop_rows(lrs, raw)]
        out["stalled_ids"] = [lr["id"]
                              for lr in landreq._stalled_rows(lrs, raw)]
        out["unmeasurable"] = [{"id": lr["id"], "reason": reason}
                               for lr, reason in
                               landreq._unmeasurable_rows(lrs, raw)]
    except landreq._ChainUntrustworthy as e:
        out["unavailable"] = str(e)
        return out
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
    out["filed"] = landreq.filed_split(lrs, raw)
    # REVERSE-SKEW ALIAS, ON THE WIRE ONLY — and I cured the forward direction
    # first and called skew handled (a review caught the reverse). Forward is
    # old-SERVER/new-browser,
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
    # (reproduced in review). `closed_ts` is the closure instant when the record
    # carries one, and None for a git-OBSERVED landing, which carries none
    # anywhere.
    closed, unknown_when = [], 0
    for lr in lrs.values():
        if not lr["terminal"]:
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



def _lr_build():
    """One projection, at most once per _LR_FLOOR_S.

    There is deliberately NO "the ledger mtimes did not move, reuse the last
    report" short-circuit here, and that is a correction, not an omission: the
    first version had one and a browser found the hole in minutes. `chmod 000`
    makes the dispatch ledger unreadable while touching NEITHER its mtime nor
    its size, so the mtime-keyed memo went on serving the last healthy board
    indefinitely — every row green, nothing on screen changed — over a record
    that could no longer be read at all. A cache that can pin a stale board
    over an UNKNOWN one defeats the single property this surface exists for.
    The floor bounds the COST; nothing below it may bound the TRUTH."""
    now = time.time()
    try:
        newest = _lr_newest_mtime()
    except Exception:
        newest = None       # a display term, never a reason to skip the read
    return _lr_project(now, newest)



def _api_lr(qs):
    """LAND PIPELINE — every land loop as the dispatch ledger and git report
    it, for the card at the top of the home tab. Read-only; the CLI equivalents
    are `helm lr list` / `helm lr stalls`.

    Fail-LOUD, not fail-open: a surprise still answers 200 (the page must not
    die), but it answers with a NAMED `unavailable` and no rows, because the
    one thing this endpoint may never do is report a healthy empty pipeline it
    did not actually read."""
    now = time.time()
    try:
        # Serve-stale-while-revalidating: the projection runs 22-28s and the
        # card aborts its fetch at 12s, so the blocking `_cached` path made
        # every TTL lapse render as pipeline UNKNOWN. Stale bodies are served
        # age-stamped (read_age_s below) while the rebuild runs; past the hard
        # cap the read blocks again rather than serving an unbounded past.
        # COLD START ANSWERS "WARMING", NEVER "UNREADABLE" — see _cached_swr.
        body = dict(_cached_swr("lr", _LR_FLOOR_S, _LR_HARD_TTL_S, _lr_build,
                                cold_body={"warming": True},
                                cold_wait=_LR_COLD_WAIT_S))
    except Exception as e:
        reason = ("the land-pipeline read failed inside helm web "
                  "(%s) — pipeline UNKNOWN" % type(e).__name__)
        body = {"read_ts": now, "ledger_mtime": None, "receipts_skipped": None,
                "unavailable": reason, "filed": None,
                "loops": [], "stalled_ids": [], "unmeasurable": [],
                "closed_recent": [], "closed_total": 0,
                "closed_unknown_when": 0,
                "recent_lands": {"rows": [], "window_total": None,
                                 "window_s": _LR_LANDS_WINDOW_S,
                                 "unavailable": reason},
                "native_chain": {"count": None, "verified": None, "detail": "",
                                 "head_index": None, "unavailable": reason}}
    # Ages are resolved at RESPONSE time, not build time: the body may be up to
    # _LR_FLOOR_S old and the card's whole freshness header depends on saying
    # so. Sending ages rather than epochs also keeps the browser's clock out of
    # it — the one comparison that could silently misreport this.
    body["read_age_s"] = max(0, int(now - (body.get("read_ts") or now)))
    mtime = body.pop("ledger_mtime", None)
    body["ledger_age_s"] = None if mtime is None else max(0, int(now - mtime))
    # recent-lands ages resolve at response time too, and onto FRESH row dicts —
    # the cached body is shared across responses, and stamping ages in place
    # would let two concurrent reads tear each other's numbers. A body with no
    # recent_lands at all (a pre-field cached body surviving a half-live
    # deploy) is UNKNOWN, never a clean zero-lands reading.
    lands = body.get("recent_lands")
    if not isinstance(lands, dict):
        lands = {"rows": [], "window_total": None,
                 "window_s": _LR_LANDS_WINDOW_S,
                 "unavailable": "this pipeline body predates the "
                 "recent-lands reading — UNKNOWN, not zero lands"}
    rows = []
    for r in (lands.get("rows") or []):
        ts = _lr_epoch(r.get("ts"))
        rows.append(dict(r, age_s=None if ts is None else max(0, int(now - ts))))
    body["recent_lands"] = dict(lands, rows=rows)
    return body, 200
del _web
