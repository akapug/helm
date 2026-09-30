"""The pin a tip reviewed BEFORE the pin shipped never got (task/3627).

Since task/2383 the verdict and source-clean hold writers pin the tip they
record at `refs/helm-reviewed/<row id>[-patch|-source-clean]` before the event
is durable (`dispatches.pin_reviewed_tips`), and a close moves those pins to
`refs/helm-retired/reviewed/` (`dispatches.retire_review_pins`). That began at
the land. A row reviewed earlier names a tip that only its lane branch keeps,
so a branch delete and one `git gc --prune` destroy the review's evidence while
the row still names it (a 09-13 census: 147 open REVIEWED rows, 67 already
pruned).

THE PIN ITSELF IS THE LANDED ONE. This module finds the tips and hands them to
the two functions above; it writes no ref of its own. An open row's tips go
through `pin_reviewed_tips`. A closed row's go through `pin_reviewed_tips` and
then `retire_review_pins`, which is the pair its verdict and its close would
have run had the pin existed then.

WHICH ROWS: every row of the dispatch ledger whose projection carries a
reviewed, patch or source-clean tip, in every repository the ledger names.
There is no date cutoff. A tip already pinned where this pass would put it
reads ALREADY and is not written, so a row reviewed after the land costs one
ref lookup, and a second `--apply` writes nothing.

CLOSED ROWS ARE INCLUDED, AND THEIR PINS GO TO THE RETIRED NAMESPACE, never to
the live one. A close now moves a row's pins there, so without this every row
closed after the land keeps its tip and every row closed before it does not:
the coverage of the retired namespace would depend on the date of the close. A
closed row can pass through no close door again, so this pass is its only way
to get the pin. The retired namespace changes no reading (landreq's
`_reaching_ref` skips both namespaces, and nothing in helm sweeps
`refs/helm-retired/`), and the count is one ref per row and role, the bound
the writer already accepts. "Closed" is any terminal: the close doors' own
exclusivity predicate (`dispatches._close_retired_by`) or a cancel.

EACH ROW'S OWN REPOSITORY, MEASURED, NEVER A GUESSED ONE. The pin goes where
`dispatches._pin_repo` finds the row's repository: the one resolver the writer
uses too, which asks the path found for its common dir, with GIT_DIR and the
other selection variables removed, and accepts it only when it IS the
`repo_id` the ledger recorded. A repository that no longer exists, that git
cannot read, or whose checkout path now holds another repository is
UNREADABLE with the reason, and nothing is written for it anywhere.

A CLOSED ROW WITH A LIVE PIN IS MOVED. A live pin on a closed row is one its
close could not move (or one on a role the row no longer carries); this pass
reads it as a tip still to retire, never as already pinned.

THE STATUS IS ONE WORD. COMPLETE (exit 0): every tip seen was read and every
pin written held. INCOMPLETE (exit 1): some tip could not be read, so its pin
is UNKNOWN, and a run where it can be read would pin it. FAILED (exit 1): a
pin did not hold. A tip LOST before the pass leaves it COMPLETE: it is
reported as a fact, and no re-run can pin it.

A LOST TIP IS COUNTED, NEVER PINNED AND NEVER AN ERROR. A tip that does not
resolve to a commit is already gone; the pass counts it and goes on. It is not
handed to the pin, which would only fail on it and write a failure receipt.

THE DRY RUN WRITES NO REF AND NO RECEIPT: one `for-each-ref` and one `cat-file
--batch-check` per repository (the batch-check may not fetch), and one
common-dir read per checkout path. Its ledger read is STRICT
(`dispatches.retire_read`), so a complete corrupt line refuses the pass by
name. That read, like every read of the ledger, may refresh helm's own fold
checkpoint under the helm home; a read that could not would replay the whole
ledger cold. The verb is `helm lr backfill-review-pins [--apply] [--json]`.
"""
import json
import os
import sys

from . import dispatches

#: What happened to one tip. TO_PIN is the dry run's answer for a tip that
#: --apply turns into PINNED or FAILED.
TO_PIN, PINNED, ALREADY, LOST, UNREADABLE, FAILED = (
    "to-pin", "pinned", "already", "lost", "unreadable", "failed")
OUTCOMES = (TO_PIN, PINNED, ALREADY, LOST, UNREADABLE, FAILED)

#: Where a tip's pin goes: an open row's stays live, a closed row's retires.
LIVE, RETIRED = "live", "retired"

#: The pass's one word. COMPLETE: every tip it saw was read, and every pin
#: it wrote held. INCOMPLETE: some tip could not be read, so its pin is
#: UNKNOWN. FAILED: a pin this pass wrote did not hold. A lost tip leaves the
#: pass COMPLETE: it was pruned before the pass, and no re-run can pin it.
COMPLETE, INCOMPLETE, FAILED_PASS = "COMPLETE", "INCOMPLETE", "FAILED"

#: Each pin role and the projection field that holds its tip. The roles and
#: their ref suffixes are `dispatches._PIN_ROLES`.
TIP_FIELDS = (("reviewed", "reviewed_tip"), ("patch", "patch_tip"),
              ("source-clean", "source_clean_tip"))


def closed(row):
    """Did a terminal end this row? Any terminal the close doors refuse over,
    or a cancel."""
    return bool(dispatches._close_retired_by(row)) \
        or row.get("status") in ("cancelled", "closed")


def _first_line(err, default):
    return ((err or "").strip().splitlines() or [default])[0][:160]


def _pins(root):
    """({ref: sha} for every review pin in `root`, None) or (None, why).
    Under the pin's own overlay, so an ambient GIT_DIR cannot answer for
    another repository."""
    from . import vcs
    rc, out, err = vcs.backend(root).text(
        root, "for-each-ref", "--format=%(refname) %(objectname)",
        dispatches.REVIEWED_PIN_NS, dispatches.RETIRED_PIN_NS,
        env=dispatches.pin_env())
    if rc != 0:
        return None, _first_line(err, "git could not list its refs")
    return dict(line.split(" ", 1) for line in out.splitlines()), None


def _alive(root, shas):
    """({sha: resolves to a commit}, None) from one `cat-file --batch-check`
    that may not fetch (a tip a partial clone lacks is LOST here, never
    downloaded), or (None, why)."""
    from . import foldckpt
    env = dict(dispatches.pin_env(), GIT_NO_LAZY_FETCH="1")
    lines = foldckpt.batch_check(root, env, shas)
    if lines is None:
        return None, "git could not say which tips resolve"
    return {s: foldckpt._commit_oid(lines[s]) == s for s in shas}, None


def _held(pins, name, sha, ended):
    """Is `sha` already pinned where this pass would put it? The live pin
    for an open row. For a closed row a retired pin under either name
    `retire_review_pins` uses, and NO live pin under `name`: a live pin on a
    closed row is one its close could not move, and this pass moves it."""
    if not ended:
        return pins.get(dispatches.REVIEWED_PIN_NS + name) == sha
    return dispatches.REVIEWED_PIN_NS + name not in pins and sha in (
        pins.get(dispatches.RETIRED_PIN_NS + name),
        pins.get(dispatches.RETIRED_PIN_NS + name + "-" + sha[:12]))


def _tips(row, carried, pins, ended):
    """(role, sha, live pin?) for each tip of the row this pass looks at:
    for a closed row every LIVE pin it still has first (one its close could
    not move, or on a role the row no longer carries), then the tips the
    row carries."""
    rid, extra = str(row.get("id") or ""), []
    for role, suffix in dispatches._PIN_ROLES if ended and pins else ():
        live = pins.get(dispatches.REVIEWED_PIN_NS + rid + suffix)
        if live and (role, live) not in carried:
            extra.append((role, live, True))
    return extra + [(role, sha, False) for role, sha in carried]


def _pin(row, todo, ended):
    """Pin each tip of `todo` through the writer's own function, one role a
    call so each answer names its tip; then retire a closed row's pins as
    its close would have."""
    for tip in todo:
        why = dispatches.pin_reviewed_tips(row, [(tip["role"], tip["tip"])])
        tip.update({"outcome": FAILED, "why": why} if why
                   else {"outcome": PINNED})
    pinned = [tip for tip in todo if tip["outcome"] == PINNED]
    why = dispatches.retire_review_pins(row) if ended and pinned else None
    for tip in pinned if why else ():
        tip.update(outcome=FAILED, why=why)


def _repo(key, members, apply, measured):
    """One repository's entry: its rows, each tip's outcome and the counts.
    Every row's repository is measured to be the one its `repo_id` names
    (`dispatches._pin_repo`, the resolver the pin itself uses); a row whose
    repository fails that is UNREADABLE with the reason, and nothing is read
    or written for it anywhere else."""
    suffix = dict(dispatches._PIN_ROLES)
    placed = [(row, tips) + dispatches._pin_repo(row, measured)
              for row, tips in sorted(members,
                                      key=lambda m: str(m[0].get("id")))]
    root = next((r for _row, _t, r, _w in placed if r), None)
    pins, why = _pins(root) if root else (
        None, next(w for _row, _t, _r, w in placed if w))
    listed = [(row, _tips(row, tips, pins, closed(row)), own, own_why)
              for row, tips, own, own_why in placed]
    shas = sorted({sha for _row, tips, own, _w in listed if own
                   for _role, sha, _l in tips
                   if dispatches._FULL_TIP.fullmatch(sha)})
    alive, why = _alive(root, shas) if pins is not None else (None, why)
    entry = {"repo": key or None, "root": root, "readable": alive is not None,
             "why": why, "rows": len(members), "open": 0, "closed": 0,
             "tips": []}
    for row, tips, own, own_why in listed:
        rid, ended = str(row.get("id") or ""), closed(row)
        entry["closed" if ended else "open"] += 1
        todo = []
        for role, sha, live in tips:
            tip = {"id": rid, "role": role, "tip": sha,
                   "namespace": RETIRED if ended else LIVE}
            if live:
                tip["live_pin"] = True
            entry["tips"].append(tip)
            if not dispatches._PIN_ROW_ID.fullmatch(rid):
                tip.update(outcome=UNREADABLE,
                           why="row id %r is not a ref name" % rid)
            elif not dispatches._FULL_TIP.fullmatch(sha):
                tip.update(outcome=UNREADABLE,
                           why="the tip is not a full commit id")
            elif not own or alive is None:
                tip.update(outcome=UNREADABLE, why=own_why or why)
            elif not alive[sha]:
                tip["outcome"] = LOST
            elif _held(pins, rid + suffix[role], sha, ended):
                tip["outcome"] = ALREADY
            else:
                tip["outcome"] = TO_PIN
                todo.append(tip)
        if apply and todo:
            _pin(row, todo, ended)
    entry["counts"] = {o: sum(1 for t in entry["tips"] if t["outcome"] == o)
                       for o in OUTCOMES}
    for ns in (LIVE, RETIRED):
        entry[ns] = sum(1 for t in entry["tips"] if t["namespace"] == ns
                        and t["outcome"] in (TO_PIN, PINNED))
    return entry


def census(apply=False):
    """(report, err). Every tip a row of the dispatch ledger names, grouped
    by the repository the row names, with its outcome and the pass's
    `status`; `apply` pins each TO_PIN tip and reports it PINNED or FAILED.

    THE READ IS STRICT (`dispatches.retire_read`): this pass claims the
    WHOLE record, and a complete corrupt line the lenient reader skips could
    be a row whose tip it then never saw. Such a ledger refuses the pass by
    name instead of answering for the rows that survived."""
    rows, unavailable = dispatches.retire_read()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable
    groups = {}
    for row in rows.values():
        tips = [(role, str(row.get(field)).strip().lower())
                for role, field in TIP_FIELDS if row.get(field)]
        if tips:
            key = str(row.get("repo_id") or row.get("repo_root") or "")
            groups.setdefault(key, []).append((row, tips))
    measured = {}
    repos = [_repo(key, members, apply, measured)
             for key, members in sorted(groups.items())]
    counts = {o: sum(r["counts"][o] for r in repos) for o in OUTCOMES}
    status = FAILED_PASS if counts[FAILED] \
        else INCOMPLETE if counts[UNREADABLE] else COMPLETE
    return {"applied": bool(apply), "status": status,
            "rows": sum(r["rows"] for r in repos), "counts": counts,
            "repos": repos}, None


def _plural(n, one, many):
    return "%d %s" % (n, one if n == 1 else many)


def _tally(counts, applied, entry=None):
    split = " (%d live, %d retired)" % (entry[LIVE], entry[RETIRED]) \
        if entry and entry[LIVE] and entry[RETIRED] else ""
    head = ("%d pinned%s" % (counts[PINNED], split) if applied
            else "%d to pin%s" % (counts[TO_PIN], split))
    tail = ", %d failed" % counts[FAILED] if applied else ""
    return ("%s, %d already pinned, %d lost before this pass, "
            "%d unreadable%s" % (head, counts[ALREADY], counts[LOST],
                                 counts[UNREADABLE], tail))


def render(report):
    """The census as the operator reads it: one line per repository, and
    each tip that failed or could not be read below its repository."""
    applied, repos = report["applied"], report["repos"]
    counts = report["counts"]
    head = "helm lr backfill-review-pins — %s: %s in %s: %s" % (
        report["status"],
        _plural(report["rows"], "reviewed row", "reviewed rows"),
        _plural(len(repos), "repository", "repositories"),
        _tally(counts, applied))
    if not applied:
        head += ("  [dry run: no ref written, no receipt; the ledger read may "
                 "refresh helm's fold checkpoint; --apply pins them]")
    out = [head]
    if counts[UNREADABLE]:
        out.append("  INCOMPLETE: %s could not be read (named below), so "
                   "their pins are UNKNOWN; nothing was written for them"
                   % _plural(counts[UNREADABLE], "tip", "tips"))
    if counts[LOST]:
        out.append("  %s already pruned before this pass: a fact to report, "
                   "not a failure of it; nothing can pin them now"
                   % _plural(counts[LOST], "tip was", "tips were"))
    for entry in repos:
        name = entry["repo"] or "(no repository recorded)"
        if not entry["readable"]:
            out.append("  %s  UNKNOWN (%s): %s, %s not pinned" % (
                name, entry["why"], _plural(entry["rows"], "row", "rows"),
                _plural(len(entry["tips"]), "tip", "tips")))
            continue
        out.append("  %s  %s (%d open, %d closed): %s" % (
            name, _plural(entry["rows"], "row", "rows"), entry["open"],
            entry["closed"], _tally(entry["counts"], applied, entry)))
        out.extend("    %-10s %s %s %s: %s" % (
            t["outcome"].upper(), t["id"][:12], t["role"], t["tip"][:12],
            t["why"]) for t in entry["tips"]
            if t["outcome"] in (FAILED, UNREADABLE))
    return "\n".join(out)


USAGE = ("usage: helm lr backfill-review-pins [--apply] [--json]  (a dry run "
         "by default: counts, per repository, the reviewed, patch and "
         "source-clean tips of every dispatch row that would be pinned, are "
         "already pinned, were lost before this pass, or cannot be read; "
         "--apply pins them through the verdict writer's own pin, a closed "
         "row's into refs/helm-retired/reviewed/. Exit 0 COMPLETE, 1 "
         "INCOMPLETE or FAILED or an unreadable ledger, 2 usage)")


def cmd(rest):
    """`helm lr backfill-review-pins` -> rc: 0 when the pass is COMPLETE
    (every tip it saw was read and every pin it wrote held; lost tips are
    reported and do not change it), 1 when it is INCOMPLETE or FAILED or the
    ledger could not be read in full, 2 on a usage error. INCOMPLETE is not
    zero because a re-run where those repositories can be read would pin
    what this one could not; a lost tip no re-run can pin, so it is not."""
    from .cli import guard_tail
    rc = guard_tail("helm lr backfill-review-pins", rest,
                    flags=("--apply", "--json"), usage=USAGE)
    if rc is not None:
        return rc
    report, err = census(apply="--apply" in rest)
    if err:
        print("helm lr backfill-review-pins: %s" % err, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1)
          if "--json" in rest else render(report))
    return 0 if report["status"] == COMPLETE else 1
