#!/usr/bin/env python3
"""helm lr — the LAND REQUEST lifecycle as a read-only VIEW over the dispatch
ledger plus one git observation of trunk.

An LR is a single dispatch seen as a land loop. It binds the author, the
lane/branch, the exact review commit (the dispatch tip), the reviewer verdict
(the dispatch verdict), and the landed evidence (the reviewed tip observed
reaching trunk). There is NO second ledger and NO new event: the dispatch ledger
is the durable truth, and landing is OBSERVED from git, never gated. Landing
authority in v1 is the existing integrator merge; the LR only watches it.

Because every wait is now a named, queryable state with a DWELL time, a loop
sitting in a non-terminal state past its per-stage threshold IS the stall signal
— the owner's workflow-gap-finder ("where are we missing delays and
opportunities to optimize workflows"):

  OPEN             dispatched, delivery to the reviewer not yet confirmed
  AWAITING_REVIEW  delivered to the reviewer, no verdict yet (review loop)
  READY            verdict in hand, reviewed tip not yet on trunk (land loop)
  MERGED_LOCAL     tip on local trunk but not the upstream trunk (push loop)
  LANDED           tip on the upstream trunk — terminal; local trunk is the
                   landing target when no upstream is configured

Landing is observed only once a verdict is attached (READY is where landing
arms), matching the dispatch ledger's own grammar. A CANCELLED dispatch is
abandoned, never a land loop — excluded upstream, before it reaches a state.
Verdict polarity
(PASS vs REFUTE) and SUPERSEDED are not expressible on the reduced dispatch
ledger, so REFUTED/SUPERSEDED are deferred to a later revision. Stdlib-only,
import-safe, read-only: list/show/stalls never mutate.
"""
import json
import subprocess
import sys
import time

from . import dispatches

# Land-side stall thresholds (seconds), measured — like every LR dwell — from
# the VERDICT stamp, because git records no moment for the local merge or the
# push: MERGED_LOCAL/LANDED are OBSERVED, so their timeline ts is None and the
# only clock a land-side loop has is its verdict. The thresholds are therefore
# CUMULATIVE from the verdict and MONOTONIC in stage order — land within the
# land budget of the verdict, reach upstream within a further push budget of
# it. Because MERGED_LOCAL >= READY, forward progress (READY -> MERGED_LOCAL)
# can never manufacture a stall on a loop that was healthy in READY: the merge
# CLEARS the land-side wait and re-arms the push budget rather than flipping a
# one-second-old push loop to STALLED. (A future revision could date the merge
# from the trunk commit's committer date; v1 is observe-only, never gating.)
# The review-side stages (OPEN, AWAITING_REVIEW) instead honor the dispatch
# row's own advisory deadline_s. Terminal LANDED never stalls.
_LAND_BUDGET_S = 3600      # land within 1h of the verdict
_PUSH_BUDGET_S = 1800      # reach upstream within a further 30m
LAND_STALL_S = {"READY": _LAND_BUDGET_S,
                "MERGED_LOCAL": _LAND_BUDGET_S + _PUSH_BUDGET_S}
TERMINAL = ("LANDED",)
STAGE_ORDER = {"OPEN": 0, "AWAITING_REVIEW": 1, "READY": 2,
               "MERGED_LOCAL": 3, "LANDED": 4}

# Trunk is main-or-master; the landing target is the upstream trunk when a
# remote publishes one, else the local trunk (David's local-first estate never
# pushes, so local trunk IS the landing target there).
LOCAL_TRUNK = ("refs/heads/main", "refs/heads/master")
UPSTREAM_TRUNK = ("refs/remotes/origin/main", "refs/remotes/origin/master",
                  "refs/remotes/origin/HEAD")


def _git(gitdir, *args):
    try:
        return subprocess.run(["git", "--git-dir", gitdir, *args],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _resolve_ref(gitdir, names):
    for name in names:
        p = _git(gitdir, "rev-parse", "--verify", "--quiet", name)
        if p is not None and p.returncode == 0 and p.stdout.strip():
            return name
    return None


def _is_ancestor(gitdir, tip, ref):
    p = _git(gitdir, "merge-base", "--is-ancestor", tip, ref)
    return p is not None and p.returncode == 0


def _trunk_refs(gitdir, cache):
    """(local_trunk_ref, upstream_trunk_ref) for one git dir, resolved once."""
    if gitdir not in cache:
        cache[gitdir] = (_resolve_ref(gitdir, LOCAL_TRUNK),
                         _resolve_ref(gitdir, UPSTREAM_TRUNK))
    return cache[gitdir]


def _observe(gitdir, tip, cache):
    """OBSERVE landing from git only — never a ledger write, never a crash.

    Landing is OBSERVABLE only when the git dir is known, a trunk ref resolves,
    and the reviewed tip is actually in that object database. When it is not
    (a legacy row with no repo binding, a pruned worktree, a missing object),
    the honest answer is 'unobservable' — never 'landed' and never a false
    'stalled', so the gap-finder does not cry wolf on loops it cannot see.
    """
    if not gitdir or not tip:
        return {"observable": False, "local": False, "upstream": False,
                "has_upstream": False}
    local, upstream = _trunk_refs(gitdir, cache)
    p = _git(gitdir, "cat-file", "-e", tip + "^{commit}")
    if not (local or upstream) or p is None or p.returncode != 0:
        return {"observable": False, "local": False, "upstream": False,
                "has_upstream": bool(upstream)}
    return {"observable": True,
            "local": bool(local) and _is_ancestor(gitdir, tip, local),
            "upstream": bool(upstream) and _is_ancestor(gitdir, tip, upstream),
            "has_upstream": bool(upstream)}


def _transitions(events):
    """(open_ts, delivered_ts, verdict_ts) read from a row's raw ledger events —
    the existing per-event timestamps, no new clock. Legacy snapshot rows that
    carry a verdict without a discrete event fall back to the opening stamp.
    Events are the row's already-grouped slice, so project() reads the ledger
    once, not once per row (that per-row history() reparse was O(N^2))."""
    open_ts = delivered_ts = verdict_ts = None
    for ev in events:
        event, ts = ev.get("event"), ev.get("ts")
        if event in ("dispatch", "add", "posting", None) and open_ts is None:
            open_ts = ts
        elif event == "delivered" and delivered_ts is None:
            delivered_ts = ts
        elif event == "verdict" and verdict_ts is None:
            verdict_ts = ts
    return open_ts, delivered_ts, verdict_ts


def _threshold(state, deadline_s):
    if state == "LANDED":
        return None
    if state in ("OPEN", "AWAITING_REVIEW"):
        try:
            return int(deadline_s)
        except (TypeError, ValueError):
            return None
    return LAND_STALL_S.get(state)


def _timeline(state, open_ts, delivered_ts, verdict_ts):
    tl = [{"state": "OPEN", "ts": open_ts}]
    if delivered_ts:
        tl.append({"state": "AWAITING_REVIEW", "ts": delivered_ts})
    if verdict_ts:
        tl.append({"state": "READY", "ts": verdict_ts})
    if state in ("MERGED_LOCAL", "LANDED"):
        tl.append({"state": state, "ts": None})    # git-observed, no ledger ts
    return tl


def _lr(row, events, cache, now):
    rid = row["id"]
    tip = row.get("tip")
    open_ts, delivered_ts, verdict_ts = _transitions(events)
    closed = row.get("status") == "verdict"
    # Landing arms only at READY, so git is only consulted for a verdict'd row.
    obs = (_observe(row.get("repo_id"), tip, cache) if closed
           else {"observable": False, "local": False, "upstream": False,
                 "has_upstream": False})
    observable = obs["observable"]
    landed = (obs["upstream"] if obs["has_upstream"] else obs["local"]) \
        if observable else False
    merged_local = observable and obs["local"] and not landed
    post_verdict = verdict_ts or delivered_ts or open_ts
    if closed and landed:
        state, entered = "LANDED", post_verdict
    elif closed and merged_local:
        state, entered = "MERGED_LOCAL", post_verdict
    elif closed:
        state, entered = "READY", post_verdict
    elif row.get("delivery") == "observed":
        state, entered = "AWAITING_REVIEW", delivered_ts or open_ts
    else:
        state, entered = "OPEN", open_ts
    dwell = dispatches._age_s({"ts": entered}, now)
    threshold = _threshold(state, row.get("deadline_s"))
    # A land-side stall is only assertable when landing is OBSERVABLE; the
    # review-side states are pure ledger facts and always assertable.
    assertable = observable or state in ("OPEN", "AWAITING_REVIEW")
    return {"id": rid, "state": state, "observable": observable,
            "author": row.get("sender"), "reviewer": row.get("recipient"),
            "lane": row.get("lane"), "branch": row.get("ref"),
            "review_sha": tip, "repo_id": row.get("repo_id"),
            "verdict_ref": row.get("verdict_ref"),
            "reviewed_tip": row.get("reviewed_tip"),
            "entered_ts": entered, "dwell_s": dwell,
            "deadline_s": row.get("deadline_s"),
            "stall_threshold_s": threshold,
            "stalled": bool(threshold and dwell >= threshold and assertable),
            "terminal": state in TERMINAL,
            "landed": landed, "merged_local": merged_local,
            "has_upstream": obs["has_upstream"],
            "timeline": _timeline(state, open_ts, delivered_ts, verdict_ts)}


def project(now=None):
    """{id: lr} for every land loop, or ({}, unavailable). Ref-less legacy rows
    (needs-redispatch, no exact tip) are not land loops and are skipped."""
    now = time.time() if now is None else now
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return {}, unavailable
    by_id = dispatches.events_by_id()   # one grouped ledger read, not N reparses
    cache, out = {}, {}
    for rid, row in current.items():
        if row.get("migration") or not row.get("tip") \
                or row.get("status") == "cancelled":
            continue        # a cancelled dispatch is abandoned — no land loop
        try:
            out[rid] = _lr(row, by_id.get(rid, ()), cache, now)
        except Exception:
            continue
    return out, None


def _order(lr):
    return (STAGE_ORDER.get(lr["state"], 9), -lr["dwell_s"])


def loops(include_landed=False, now=None):
    """(sorted land loops, unavailable). In-flight (non-terminal) by default."""
    lrs, unavailable = project(now)
    if unavailable:
        return None, unavailable
    vals = [lr for lr in lrs.values() if include_landed or not lr["terminal"]]
    return sorted(vals, key=_order), None


def stalls(now=None):
    """(non-terminal loops past their per-stage threshold, unavailable) —
    the workflow-gap-finder, longest-stalled first."""
    lrs, unavailable = project(now)
    if unavailable:
        return None, unavailable
    out = [lr for lr in lrs.values() if lr["stalled"] and not lr["terminal"]]
    return sorted(out, key=lambda lr: -lr["dwell_s"]), None


def get(rid, now=None):
    lrs, unavailable = project(now)
    if unavailable:
        return None, unavailable
    rid = str(rid or "")
    if rid in lrs:
        return lrs[rid], None
    hits = [lr for k, lr in lrs.items() if k.startswith(rid)] if rid else []
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, "ambiguous id prefix: %s (helm lr list)" % rid
    return None, "no such land request: %s (helm lr list)" % rid


def board_section(now=None):
    """The data a console renders as a 'land loops + where they're stalling'
    panel. console-design owns the actual anchor; this exposes the rows in a
    stable, brief-shaped section dict (title + loops + the stalled subset)."""
    loops_, unavailable = loops(now=now)
    if unavailable:
        return {"title": "LAND LOOPS", "unavailable": unavailable,
                "loops": [], "stalled": []}
    stalled_, _ = stalls(now=now)

    def card(lr):
        return {"id": lr["id"], "state": lr["state"], "lane": lr["lane"],
                "branch": lr["branch"], "review_sha": (lr["review_sha"] or "")[:12],
                "author": lr["author"], "reviewer": lr["reviewer"],
                "dwell_s": lr["dwell_s"], "stalled": lr["stalled"],
                "observable": lr["observable"]}
    return {"title": "LAND LOOPS", "unavailable": None,
            "loops": [card(lr) for lr in loops_],
            "stalled": [card(lr) for lr in (stalled_ or [])]}


# ------------------------------------------------------------------------ CLI

USAGE = ("usage: helm lr list [--all] [--json] | show <id> [--json] | "
         "stalls [--json]")


def _fmt_dwell(s):
    s = int(s or 0)
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd%02dh" % (s // 86400, (s % 86400) // 3600)


def _line(lr):
    if lr["stalled"]:
        mark = "  STALLED"
    elif lr["state"] in ("READY", "MERGED_LOCAL") and not lr["observable"]:
        mark = "  (landing unobservable)"
    else:
        mark = ""
    return "  %-12s %-15s %-20s %7s  %s%s" % (
        lr["id"][:12], lr["state"], (lr["lane"] or "-")[:20],
        _fmt_dwell(lr["dwell_s"]), (lr["review_sha"] or "-")[:12], mark)


def _render_show(lr):
    out = ["LAND REQUEST %s   %s%s" % (
        lr["id"], lr["state"], "  STALLED" if lr["stalled"] else "")]
    out.append("  author    %s  ->  reviewer %s" % (
        lr["author"] or "-", lr["reviewer"] or "-"))
    out.append("  lane      %s" % (lr["lane"] or "-"))
    out.append("  branch    %s" % (lr["branch"] or "-"))
    out.append("  review    %s" % (lr["review_sha"] or "-"))
    out.append("  verdict   %s   reviewed %s" % (
        lr["verdict_ref"] or "(none)", lr["reviewed_tip"] or "-"))
    if lr["state"] in ("MERGED_LOCAL", "LANDED"):
        target = "upstream trunk" if lr["has_upstream"] else "local trunk"
        out.append("  landed    %s (%s)" % (
            "yes" if lr["landed"] else "local only, NOT pushed", target))
    elif lr["state"] == "READY" and not lr["observable"]:
        out.append("  landed    UNOBSERVABLE — no repo binding or trunk to "
                   "watch (never inferred landed)")
    out.append("  timeline:")
    for step in lr["timeline"]:
        out.append("    %-16s %s" % (
            step["state"], step["ts"] or "(observed)"))
    threshold = lr["stall_threshold_s"]
    tail = ""
    if threshold is not None:
        tail = "  (threshold %s — %s)" % (
            _fmt_dwell(threshold), "STALLED" if lr["stalled"] else "ok")
    out.append("  dwell     %s in %s%s" % (
        _fmt_dwell(lr["dwell_s"]), lr["state"], tail))
    return "\n".join(out)


def _unavailable(unavailable):
    print("helm lr: dispatch ledger unavailable; land loops UNKNOWN: %s"
          % unavailable, file=sys.stderr)
    return 1


def cmd_lr(args):
    args = list(args or [])
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    from .cli import guard_tail, suggest
    if verb == "list":
        rc = guard_tail("helm lr list", rest, flags=("--all", "--json"),
                        usage=USAGE)
        if rc is not None:
            return rc
        rows_, unavailable = loops(include_landed="--all" in rest)
        if unavailable:
            return _unavailable(unavailable)
        if "--json" in rest:
            print(json.dumps(rows_, ensure_ascii=False, indent=1))
            return 0
        if not rows_:
            print("helm lr: no land loops in flight")
            return 0
        print("helm lr — %d land loop%s in flight" % (
            len(rows_), "" if len(rows_) == 1 else "s"))
        for lr in rows_:
            print(_line(lr))
        if any(lr["stalled"] for lr in rows_):
            print("STALLED = past its per-stage threshold (helm lr stalls)")
        return 0
    if verb == "stalls":
        rc = guard_tail("helm lr stalls", rest, flags=("--json",), usage=USAGE)
        if rc is not None:
            return rc
        rows_, unavailable = stalls()
        if unavailable:
            return _unavailable(unavailable)
        if "--json" in rest:
            print(json.dumps(rows_, ensure_ascii=False, indent=1))
            return 0
        if not rows_:
            print("helm lr: no stalled land loops — every loop is inside its threshold")
            return 0
        print("helm lr — %d STALLED land loop%s (workflow gaps):" % (
            len(rows_), "" if len(rows_) == 1 else "s"))
        for lr in rows_:
            print(_line(lr) + "  (>= %s in %s)" % (
                _fmt_dwell(lr["stall_threshold_s"]), lr["state"]))
        return 0
    if verb == "show":
        if not rest:
            print(USAGE, file=sys.stderr)
            return 2
        rid = rest[0]
        rc = guard_tail("helm lr show", rest[1:], flags=("--json",), usage=USAGE)
        if rc is not None:
            return rc
        lr, err = get(rid)
        if err:
            print("helm lr: " + err, file=sys.stderr)
            return 1
        if "--json" in rest[1:]:
            print(json.dumps(lr, ensure_ascii=False, indent=1))
            return 0
        print(_render_show(lr))
        return 0
    print("helm lr: unknown subverb '%s'%s (%s)" % (
        verb, suggest(verb, ("list", "show", "stalls")), USAGE), file=sys.stderr)
    return 2
