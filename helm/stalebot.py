"""helm.stalebot — the standing housekeeping loop over aged open work.

THE GAP (owner-ruled P0, task/445): detection and terminals both exist —
`helm lr stalls` renders the per-stage thresholds, `helm dispatch triage`
re-measures a row's claims on demand, cancel-with-reason and
`lr close --reason superseded` are the attested terminals — but every one of
them is PULL-ONLY. Nobody types the pull, so a 2-day orphaned build row and
3-day pending reviews both needed a human hand. This module is the PUSH: a
cadenced sweep that walks aged open work, re-measures each row with the
machinery that already owns that question, and PROPOSES a terminal to the
row's owner.

PROPOSES, NEVER EXECUTES. The output is chat digests, one per row-owner per
sweep (task/431's async-convergence form: every finding with a proposed
disposition, requiring one CONCUR/OVERRULE reply per line). No ledger write,
cancel, close, retip, or citation edit happens in the sweep. The separate
`stale redispatch` actuator records exactly one proof-checked cured successor;
the loop itself only points at the measured next action.

COMPOSED, NOT NET-NEW — every measurement is a reuse:
  · aged lr loops    -> landreq.stalls() (the per-stage STALLED thresholds,
                        already relieved- and terminal-filtered)
  · aged dispatches  -> dispatches.owed() x _is_overdue() (the open frontier,
                        triage's own selection, so a superseded parent and its
                        successor are never both billed)
  · aged task rows   -> tasks.open_rows() past an untouched bound
  · cured, unwitnessed -> dispatches.cured_unwitnessed (bare triage's OWN
                        classifier, called not re-derived — a second census
                        inherits none of the first's scars: the 57-of-67
                        successor filter and the ancestry-not-name resolution
                        both live in that one function)
  · claim re-measure -> clearspan.re_measure (the triage machinery, verbatim)
  · landed-on-trunk  -> landreq's _close_repo/_close_trunk/_landing_proof trio
                        (the same predicate seats_work_offer._offer_landing_state
                        composes; ancestry fast path, patch-id fallback)
  · base drift       -> the lr projection's own base_behind against
                        landreq.STALE_BASE_BEHIND (task/266's measured bar) —
                        never a second drift measurement
The cadence/latch/state skeleton is idle_dispatch.py's (fcntl lock, per-row
latch, re-arm on resolve); the timer install is tasksmirror.ensure_timer's;
the env hygiene in the unit is repo-watch's measured identity-dispute lesson.

A BUILD ROW'S REF IS A BASE, NEVER ITS CONTENT. Measured on the live ledger
2026-08-06 (landreq's _landed_marker archaeology): 48 of 52 resolvable BUILD
refs were already ancestors of trunk, so a raw landing probe would call nearly
every build row ever written "superseded". A build row without an explicit
reviewed_tip therefore stays UNKNOWN here, exactly as _offer_landing_state
rules, and UNKNOWN never proposes a terminal.

DISPATCH CANCEL NEEDS TWO INDEPENDENT MEASUREMENTS, not one. A rotted
file:line alone is a claim about PROSE (the line moved); content absent from
trunk alone is a claim about one tip. Only both together — claims STALE and
the reviewed content measured ABSENT by ancestry AND patch-id — read "the
work outran the row", and even then it is a proposal, never an act. A TASK's
rotted anchors have no reviewed-content identity to supply that second proof:
they request re-anchoring and can never propose cancellation by themselves.

ATTENTION BUDGET: one digest per owner per sweep, rows an owner has already
been asked about re-propose only after REPROPOSE_S or when their PROPOSED
terminal CHANGES (a keep that becomes a supersede is news; the same supersede
twice in a day is noise). Unowned rows ride ONE integrator digest. An
undelivered digest never latches its rows — repo-watch's cursor lesson: a
watcher that fails silent while its cursor marches on has dropped the window.
"""
import fcntl
import hashlib
import json
import os
import shlex
import sys
import time

from . import (clearspan, dispatches, home, landreq, obligation, pk,
               seats_integrator, tasks, vcs)

BOT = "stale-bot"
ROOM = "helm"
# Unowned/role-owed rows ride ONE digest to the integrator seat — the same
# addressee repo-watch delivers to; a row nobody owns still needs a reader.
# WHO THAT IS gets ASKED, never spelled: a module-level
# `INTEGRATOR = "<a seat name>"` keeps addressing that name after the roster
# stops carrying it, and nothing fails loudly — the constant is truthy, the
# digest is keyed, the post is written, and the sweep reports a delivery it
# never made. seats_integrator is the one door that reads it at the moment of
# use. See _integrator_for below for the snapshot rule.
# THE ROLE TOKEN a latch key uses for an unowned row, which is NOT an
# addressee. A key exists to answer "is this the same ask", and a resolved
# seat name makes every latched row re-propose the day the integrator is
# renamed — a rename does not change the ask.
#
# CHANGING THIS VALUE INVALIDATES EVERY LATCH THAT CARRIES THE OLD ONE, so
# COUNT BEFORE YOU CHANGE IT. `_latched` compares a STORED fingerprint against
# a freshly computed one, so any edit to a fingerprint INPUT re-proposes every
# affected row on the first sweep after it ships — the one-time nag burst
# `_latched`'s own docstring names for the legacy no-fingerprint case, and the
# whole cost REPROPOSE_S exists to prevent. The count is one grep of the
# stale-bot state file for entries whose stored fingerprint carries the old
# actor value; do it, and say the number, because a mechanism published
# without its population cannot tell anyone whether to worry.
UNOWNED_ACTOR = "<integrator>"
TASK_UNTOUCHED_S = 3 * 86400   # the ruled default: a task row untouched 3d is aged
REPROPOSE_S = 3 * 86400        # one ask per row per three days at daily cadence
MAX_LINES = 12                 # digest cap; the remainder is COUNTED, never dropped
CARRIER_WINDOW = 500           # trunk commits patch-id-scanned to NAME a carrier
_STATE = "stale_bot.json"

# The PROPOSED dispositions. Names are the contract the digest speaks and the
# tests pin; terminal candidates carry the owner-operated door beside them.
SUPERSEDE = "supersede-candidate"    # content on trunk; carrier named when found
CANCEL = "cancel-with-reason"        # claims rotted AND content proven absent
RETIP = "retip-candidate"            # base moved past the measured bar
REANCHOR = "reanchor-needed"         # citations rotted; work truth stays unknown
KEEP = "still-live-keep"             # no terminal proposed; evidence says why
REDISPATCH = "redispatch-candidate"  # author CURED, nobody waiting; author's move
PROXY_REDISPATCH = "redispatch-by-proxy"   # integrator must route/operate it
SOURCE_UNAVAILABLE = "source-unavailable" # a whole source needs an obligated reader


class _SourceProblem(str):
    """Human evidence carrying a stable machine source identity."""

    def __new__(cls, source_key, detail):
        out = str.__new__(cls, detail)
        out.source_key = source_key
        return out


_USAGE = """usage: helm stale sweep [--dry-run] [--quiet] [--json] [--ensure-timer]
       helm stale redispatch <dispatch-id> --reviewer <current-seat> [--repo <checkout>]
  Walk aged open work — lr loops past their per-stage STALLED threshold,
  open dispatch rows past their deadline with no visible progress, task rows
  untouched past 3d, PLUS rows whose author CURED and never re-dispatched
  (bare triage's own cured-unwitnessed classifier; these rows are CLOSED and
  invisible to every open-frontier instrument) — re-measure each with the
  triage machinery, and address one digest per row-owner proposing a disposition
  per row (CONCUR/OVERRULE per line). A cured row routes to one current rostered
  actor only when its source-session identity and provider wall are readable;
  absent/ambiguous/walled actors, an unavailable reviewer, and unreadable
  sources become explicit integrator obligations. The sweep proposes and never
  executes; `stale redispatch` records and delivers one exact successor after its
  safety checks. Every other terminal or citation-edit verb stays the owner's.
  A digest capped at 12 lines latches ONLY what it
  rendered and declares the remainder, which rides the next sweep.
    --dry-run       classify and print the digests; post nothing, write no state
    --quiet         sweep and record state without posting
    --json          the full machine report
    --ensure-timer  install/refresh the daily systemd user timer and exit
"""


# ---------------------------------------------------------------------------
# collection — the three aged populations, each from its owning module
# ---------------------------------------------------------------------------

def collect(now=None, task_path=None, repo=None, trunk=None):
    """([item], [unavailable], no_deadline_count) — every aged open row, plus
    every CURED-never-redispatched one.

    Each item: {kind: lr|dispatch|task|cured, id, row, lr?, age_s, why_aged}.
    An unavailable SOURCE is reported, never silently skipped — a sweep whose
    whole job is surfacing forgotten work must not forget a whole ledger it
    could not read (tasksmirror's law, inherited verbatim). A dispatch row
    with no deadline field predates the deadline vocabulary and can never be
    overdue by the ledger's own clock; it is COUNTED rather than dropped.

    THE CURED POPULATION HAS NO AGE GATE, deliberately. Aged-ness is what
    makes an OPEN row a housekeeping case; a cured-never-redispatched row is
    a defect STATE the moment it exists (task/983: the pool grew 8 -> 11
    across one night with zero drainage because no routine instrument could
    see it — `dispatch list --open` excludes CURED by construction, this
    sweep walked open rows, idle-dispatch crosses open rows). The population
    is dispatches.cured_unwitnessed — bare triage's own classifier, CALLED
    rather than re-derived — with triage's own defaults when no repo/trunk is
    given, so the sweep and the bare verb can never disagree about membership.
    """
    now = time.time() if now is None else now
    items, unavailable, lr_ids = [], [], set()
    no_deadline = 0
    try:
        stalled, err = landreq.stalls(now)
    except Exception as e:               # noqa: BLE001 — a source that cannot
        stalled, err = None, str(e)      # be read is REPORTED, never a crash
    if stalled is None:
        unavailable.append("lr stalls: %s" % (err or "unreadable"))
        stalled = []
    try:
        store = dispatches.rows()
    except Exception as e:               # noqa: BLE001 — same law as above
        store = {}
        unavailable.append("dispatch store: %s" % e)
    for lr in stalled:
        rid = str(lr.get("id") or "")
        lr_ids.add(rid)
        items.append({
            "kind": "lr", "id": rid, "row": store.get(rid) or {"id": rid},
            "lr": lr, "age_s": int(lr.get("dwell_s") or 0),
            "why_aged": "%s past its %s stage threshold" % (
                _fmt_age(lr.get("dwell_s") or 0), lr.get("state"))})
    try:
        current, un = dispatches.snapshot()
    except Exception as e:               # noqa: BLE001 — same law as above
        current, un = None, str(e)
    if current is None or un:
        unavailable.append("dispatch ledger: %s" % (un or "unreadable"))
    else:
        try:
            live = dispatches.live_claims()
        except Exception:                # noqa: BLE001 — _is_overdue's own
            live = None                  # contract: unreadable claims leave
        for r in dispatches.owed(current):   # the clock verdict standing
            rid = str(r.get("id") or "")
            if rid in lr_ids:
                continue                 # the lr projection already carries it
            if r.get("deadline_s") is None:
                no_deadline += 1         # pre-deadline-era row: counted, not aged
                continue
            if not dispatches._is_overdue(r, now, live):
                continue
            items.append({
                "kind": "dispatch", "id": rid, "row": r, "lr": None,
                "age_s": dispatches._age_s(r, now),
                "why_aged": "%s old, past its %ss deadline, no visible progress"
                            % (_fmt_age(dispatches._age_s(r, now)),
                               r.get("deadline_s"))})
        # THE CURED BUCKET rides the SAME snapshot the overdue walk just read.
        # An empty ledger has zero cured rows by construction, so the git-priced
        # index is never built for one (and the hermetic collect tests that
        # patch snapshot() to ({}, None) stay hermetic).
        if current:
            cured, cure_unavailable = _cured_population(current, repo, trunk)
            unavailable.extend(cure_unavailable)
            # POSITION map, not the item: replacing by `items.index(prior)`
            # would compare dicts by VALUE, and the first equal element wins —
            # a needless way to overwrite the wrong row.
            at = {it["id"]: i for i, it in enumerate(items)}
            for row, where in cured:
                rid = str(row.get("id") or "")
                branch, tip, ahead = where
                entry = {
                    "kind": "cured", "id": rid, "row": row, "lr": None,
                    "where": (branch, str(tip), int(ahead or 0)),
                    "age_s": dispatches._age_s(row, now),
                    "why_aged": "author CURED at %s (+%d on %s), review never "
                                "re-dispatched" % (str(tip)[:12],
                                                   int(ahead or 0), branch)}
                pos = at.get(rid)
                if pos is None:
                    at[rid] = len(items)
                    items.append(entry)
                    continue
                prior = items[pos]
                # THE CURE OUTRANKS THE STALL, and dropping it was a
                # blocker: `if rid in lr_ids: continue` meant a row that was
                # BOTH an aged lr loop and a cured-unwitnessed row got only its
                # lr classification — which knows nothing about cures and
                # proposes keep/retip — so the cure stayed invisible on exactly
                # the rows that had been stalled longest. One id still yields
                # ONE proposal (one id, one latch key, never two rival asks):
                # the cured entry REPLACES the stalled one and carries the
                # stall's own aging clause forward, so nothing that was said
                # before is lost from the digest line.
                entry["why_aged"] = "%s; also %s" % (entry["why_aged"],
                                                     prior["why_aged"])
                entry["lr"] = prior.get("lr")
                items[pos] = entry
    try:
        trows = tasks.open_rows(task_path)
    except Exception as e:               # noqa: BLE001 — same law as above
        trows = []
        unavailable.append("task ledger: %s" % e)
    for t in trows:
        touched = float(t.get("last_updated") or t.get("ts") or now)
        age = now - touched
        if age < TASK_UNTOUCHED_S:
            continue
        items.append({
            "kind": "task", "id": str(t.get("id") or ""), "row": t, "lr": None,
            "age_s": int(age),
            "why_aged": "untouched %s (bound %dd)" % (
                _fmt_age(age), TASK_UNTOUCHED_S // 86400)})
    return items, unavailable, no_deadline


def _verified_placement(repo_id, candidate):
    """A caller-supplied checkout whose common-dir is exactly `repo_id`."""
    if not isinstance(candidate, str) or not candidate or "\0" in candidate:
        return None
    info = dispatches._repo_info(candidate)
    return info.get("repo") if info and info.get("repo_id") == repo_id else None


def _worktree_for(repo_id, repo_root=None):
    """A recorded checkout that proves its common-dir identity, or legacy fallback."""
    placed = _verified_placement(repo_id, repo_root)
    if placed:
        return placed
    # Legacy rows predate durable placement. The shared resolver can recover only
    # the plain <root>/.git shape; separate-git-dir honestly remains unavailable.
    return obligation._root_for_repo(repo_id)


def _git_authority_for(repo_id, repo_root=None):
    """A verified checkout placement; a raw common-dir cannot choose a trunk."""
    return _worktree_for(repo_id, repo_root)


def _cured_population(snap, repo=None, trunk=None):
    """([(row, where)], [unavailable]) — cured rows across EVERY repo the
    ledger spans, each measured against ITS OWN git index.

    THE LEDGER IS GLOBAL; A GIT INDEX IS NOT. The first cut called
    `cured_unwitnessed` once with a single root — the sweep's cwd — so every
    row belonging to any OTHER repository was resolved against a tree that has
    never contained its branches. `_cure_index` answers "no live branch carries
    this tip" for those, which `cure_state` reads as NO BRANCH: the cure is
    reported not to exist. A silent, whole-repo false negative on a sweep whose
    entire job is finding rows nobody can see — and it would have grown
    invisibly with every additional repo the fleet works in.

    So rows are GROUPED BY repo_id and each group is measured against its own
    verified checkout. Its successor graph is scoped to that same repository:
    a foreign row may name a local parent in the global ledger, but that edge has
    no authority to suppress the owning repository's cure.

    TWO VERIFIED CHECKOUTS UNDER ONE repo_id IS THE NORMAL CASE, not an
    ambiguity, and refusing it as UNKNOWN blinded the scheduled sweep — which
    passes no explicit repo — for a WHOLE repository the moment a second seat
    wrote a row. helm gives every seat its own worktree and every worktree of
    a repository shares ONE common-dir, so the second placement is guaranteed
    rather than exotic; the blinding grew with the fleet and fell on exactly
    the rows this sweep exists to surface. A linked worktree also shares
    refs/heads and the object store, so `_cure_index` reads the SAME answer
    from any of them: there is nothing to disambiguate. The first CARRIED path
    that verifies against this repo_id is the placement — the rule
    `dispatches.cured_by_repo`, the other canonical consumer of this
    population, already used, so the two now agree instead of contradicting
    each other on one snapshot. A carried path that does NOT verify is still
    refused, which is the check that was ever load-bearing.

    A row with NO repo_id is UNKNOWN and is REPORTED, never judged against the
    sweep's own tree — the fleet's own no-cwd-fallback law (an absent binding
    resolved against whatever tree the sweep happens to stand in is how one
    repo's history gets used to answer another's question). An explicit `repo`
    narrows to rows already bound to that repository; it never lends the named
    repository's authority to unbound historical rows."""
    unavailable, out = [], []
    eligible = [row for row in snap.values() if isinstance(row, dict)
                and row.get("polarity") == "fix" and row.get("reviewed_tip")
                and not dispatches._close_retired_by(row)]
    if not eligible:
        return out, unavailable
    if repo:
        info = dispatches._repo_info(repo)
        if not info:
            source = "cured-fix:explicit:%s" % os.path.abspath(str(repo))
            return [], [_SourceProblem(
                source, "cured-fix scan: explicit repository is unreadable")]
        ids = [str(row.get("id") or "") for row in eligible
               if row.get("repo_id") == info["repo_id"]]
        if not ids:
            return out, unavailable
        scoped = {rid: row for rid, row in snap.items()
                  if isinstance(row, dict)
                  and row.get("repo_id") == info["repo_id"]}
        # Bind the trunk resolver to the same explicit repository the selected
        # rows already name; no foreign or unbound row may borrow this authority.
        exemplar = {"repo_id": info["repo_id"]}
        try:
            resolved_trunk, _pin, _target, terr = landreq._close_trunk(
                exemplar, info["repo_id"], trunk)
            if terr:
                cured, err = [], "trunk unreadable: %s" % terr
            else:
                cured, err = dispatches.cured_unwitnessed(
                    scoped, ids=ids, root=info["repo"], trunk=resolved_trunk)
        except Exception as e:                # noqa: BLE001 — a source that
            cured, err = [], str(e)           # cannot be read is REPORTED
        if err:
            unavailable.append(_SourceProblem(
                "cured-fix:%s" % info["repo_id"],
                "cured-fix scan: %s" % err))
        return list(cured or ()), unavailable
    groups, invalid, unbound = {}, {}, 0
    for row in eligible:
        rid = str(row.get("id") or "")        # classifier still owns cure membership
        raw_binding = row.get("repo_id")
        binding = raw_binding if isinstance(raw_binding, str) else ""
        if not binding:
            unbound += 1
            continue
        if "\x00" in binding or binding != binding.strip():
            invalid[binding] = invalid.get(binding, 0) + 1
            continue
        groups.setdefault(binding, []).append(rid)
    if unbound:
        unavailable.append(_SourceProblem(
            "cured-fix:unbound",
            "cured-fix scan: %d FIX row(s) carry no repo_id — their cure "
            "state is UNKNOWN (never resolved against this sweep's own tree)"
            % unbound))
    for binding, count in sorted(invalid.items()):
        digest = hashlib.blake2b(
            binding.encode("utf-8"), digest_size=12).hexdigest()
        unavailable.append(_SourceProblem(
            "cured-fix:invalid-repo-id:%s" % digest,
            "cured-fix scan: repo_id %r is unreadable — %d row(s) UNKNOWN"
            % (binding, count)))
    for binding, ids in sorted(groups.items()):
        scoped = {rid: row for rid, row in snap.items()
                  if isinstance(row, dict) and row.get("repo_id") == binding}
        # SORTED, so the placement does not depend on ledger insertion order:
        # every verified worktree of this repo_id answers identically, but the
        # SCAN still has to name the same one twice in a row.
        carried, root, exemplar = {}, None, None
        for candidate in scoped.values():
            hint = candidate.get("repo_root")
            if isinstance(hint, str) and hint:
                carried.setdefault(hint, candidate)
        for hint in sorted(carried):
            placed = _verified_placement(binding, hint)
            if placed:
                root, exemplar = os.path.realpath(placed), carried[hint]
                break
        if root is None:
            root = _git_authority_for(binding)
            exemplar = next((snap[i] for i in ids
                             if isinstance(snap.get(i), dict)),
                            {"repo_id": binding})
        if not root:
            unavailable.append(_SourceProblem(
                "cured-fix:%s" % binding,
                "cured-fix scan: repo %r is unreadable or has no verified "
                "checkout placement — %d row(s) UNKNOWN"
                % (binding, len(ids))))
            continue
        try:
            resolved_trunk, _pin, _target, terr = landreq._close_trunk(
                exemplar, binding, trunk)
            if terr:
                cured, err = [], "trunk unreadable: %s" % terr
            else:
                cured, err = dispatches.cured_unwitnessed(
                    scoped, ids=sorted(ids), root=root, trunk=resolved_trunk)
        except Exception as e:                # noqa: BLE001 — same law
            cured, err = [], str(e)
        out.extend(cured or ())
        if err:
            unavailable.append(_SourceProblem(
                "cured-fix:%s" % binding,
                "cured-fix scan (%r): %s" % (binding, err)))
    return out, unavailable


# ---------------------------------------------------------------------------
# the re-measurement + the classifier — reuse, in a fixed precedence
# ---------------------------------------------------------------------------

def _landing_state(row, repo=None, trunk=None):
    """(proof, tip, pinned, gitdir) — where this row's reviewed CONTENT is.

    proof is landreq._landing_proof's vocabulary: ancestor | patch-equivalent |
    absent | unknown. The composition is exactly
    seats_work_offer._offer_landing_state's, kept separate only because the
    classifier needs the PROOF WORD (ancestor names its own carrier;
    patch-equivalent needs a search) and the gitdir for that search, both of
    which the offer rung's True/False/None projection discards.

    The BUILD rule is preserved verbatim: a build row's ref is the base the
    work was dispatched FROM, never its content, so without an explicit
    reviewed_tip the answer is unknown — and unknown proposes nothing."""
    tip = str(row.get("reviewed_tip") or "").strip()
    if not tip and row.get("kind") == "review":
        tip = str(row.get("ref") or "").strip()
    if not tip:
        return "unknown", None, None, None
    try:
        gitdir, err = landreq._close_repo(row, repo)
        if err:
            return "unknown", tip, None, None
        _ref, pinned, _target, err = landreq._close_trunk(row, gitdir, trunk)
        if err:
            return "unknown", tip, None, gitdir
        return landreq._landing_proof(gitdir, tip, pinned), tip, pinned, gitdir
    except Exception:                    # noqa: BLE001 — a probe that cannot
        return "unknown", tip, None, None    # measure must say unknown, never verdict


def _carrier(gitdir, tip, pinned):
    """The trunk sha CARRYING tip's change under a new object, or None.

    _landing_proof said patch-equivalent: the change is on trunk but the
    reviewed object is reachable from nothing (the fleet lands by
    cherry-pick). `git cherry` proves presence without naming WHERE, and a
    supersede proposal that cannot name its carrier asks the owner to go find
    it — so this names it: patch-id the tip once, then one `log -p` window
    over trunk patch-id'd in a single stream. Two seam reads per side, no
    per-commit spawn. A carrier beyond the window returns None and the
    evidence line says so; an honest "somewhere on trunk" beats a fabricated
    sha (the never-type-a-sha law)."""
    try:
        back = vcs.backend(gitdir)
        rc, patch, _err = back.text(gitdir, "show", tip)
        if rc != 0:
            return None
        rc, out, _err = back.text(gitdir, "patch-id", "--stable", stdin=patch)
        if rc != 0 or not out.split():
            return None
        want = out.split()[0]
        rc, log, _err = back.text(gitdir, "log", "-p", "--no-merges",
                                  "-%d" % CARRIER_WINDOW, pinned, timeout=60)
        if rc != 0:
            return None
        rc, ids, _err = back.text(gitdir, "patch-id", "--stable", stdin=log,
                                  timeout=60)
        if rc != 0:
            return None
        for line in ids.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == want:
                return parts[1]
    except Exception:                    # noqa: BLE001 — no carrier is a fact
        return None                      # the evidence line states honestly
    return None


def classify_dispatch(row, lr=None, repo=None, trunk=None):
    """(terminal, evidence, door) for one aged dispatch/lr row.

    PRECEDENCE IS THE CLASSIFIER, spelled once:
      1. content ON TRUNK (ancestry or patch-id)      -> SUPERSEDE, carrier named
      2. content PROVEN ABSENT and claims ROTTED      -> CANCEL, rot quoted
      3. READY base >= the measured STALE_BASE_BEHIND -> RETIP
      4. everything else                              -> KEEP, evidence stated
    Landed outranks retip because a base drifting under finished work is
    housekeeping the close already performs; absent+rotted outranks retip
    because a moot row re-tipped is the same moot row with a fresher clock.
    An UNKNOWN landing never cancels: cancel requires the ABSENT proof, so an
    unreadable repo degrades to KEEP with the failure named."""
    rid = str(row.get("id") or "")
    proof, tip, pinned, gitdir = _landing_state(row, repo, trunk)
    if proof in ("ancestor", "patch-equivalent"):
        carrier = tip if proof == "ancestor" else _carrier(gitdir, tip, pinned)
        where = ("carried at %s (%s)" % (carrier[:12], proof)) if carrier else \
            ("patch-equivalent on trunk at %s (carrier beyond the %d-commit "
             "window)" % (str(pinned)[:12], CARRIER_WINDOW))
        return (SUPERSEDE, "reviewed work IS on trunk — %s" % where,
                "helm lr close %s --reason superseded" % rid[:12])
    verdict, detail = clearspan.re_measure(row)
    if proof == "absent" and verdict == clearspan.STALE:
        return (CANCEL,
                "content on neither trunk nor any measured object AND its "
                "claims rotted: %s" % detail,
                "helm dispatch cancel %s <reason>" % rid[:12])
    behind = (lr or {}).get("base_behind")
    if behind is not None and behind >= landreq.STALE_BASE_BEHIND:
        return (RETIP,
                "base %d commits behind trunk (measured bar %d) — the work "
                "may still be wanted, on a fresh tip" % (
                    behind, landreq.STALE_BASE_BEHIND),
                "helm dispatch retip %s --ref <new-tip> --reason base-moved"
                % rid[:12])
    # BEFORE DEFAULTING TO KEEP, ASK WHETHER THE WORK LANDED OFF-CHAIN
    # (task/644, consuming task/430's detector). A build row whose lane never
    # recurs and whose work reached trunk under a DIFFERENT lane label reads
    # UNKNOWN to every landedness probe keyed on the row's own ref — which is
    # this branch — so the bot proposed still-live-keep on a finished job.
    # MEASURED SPECIMEN: row 652d9794 (parked-dispatch-rebind), proposed
    # still-live-keep with "landing unknown" while its work was on trunk under
    # dispatch-rebind-novel-parts.
    #
    # IT CONSUMES THE DETECTOR RATHER THAN REBUILDING IT. `offchain_landing`
    # is the same probe `landreq._landed_marker` renders; one detector, two
    # readers. A second enumerator would start at zero on every edge this one
    # already paid for — the lane-label rename, the dash-join specificity, the
    # hit-sound/miss-unknown asymmetry.
    #
    # THE PROPOSAL IS SUPERSEDE-CANDIDATE, NOT A TERMINAL. The bot proposes and
    # never executes, and a citation hit is evidence rather than proof: it says
    # WHICH carrier to look at and leaves the verb to the owner.
    if proof == "unknown":
        try:
            # THE SAME REPO AND TRUNK THIS FUNCTION ALREADY JUDGED
            # LANDEDNESS AGAINST (review blocker 1). Calling the probe
            # bare let it resolve its OWN local ref, so a proposal could
            # judge landing on origin/main and then take its carrier
            # evidence from a LOCAL main nobody else can see — a
            # local-only citation mints a false supersede, an origin-only
            # carrier is missed. `_landing_state` above returned `gitdir`;
            # one authority per question, and it is that one.
            # ONE AUTHORITY, RESOLVED HERE (review blocker 1, reproduced
            # twice). Passing `gitdir`/`trunk` straight through was still a
            # SPLIT: on the motivating no-reviewed_tip path `_landing_state`
            # returns BEFORE resolving anything, so `gitdir` is None and
            # `trunk` is the RAW CALLER STRING — and the probe then fell back
            # to its own local ref. Two repros: an upstream-only authority
            # with a citation on local main still proposed supersede, and a
            # caller repo that `_close_repo` REJECTS still had the stored
            # repo searched. So the probe's repo and ref are resolved through
            # the same doors `_landing_state` uses, and a resolution failure
            # KEEPS rather than proposing — a citation nobody else can see is
            # not evidence.
            probe_dir, rerr = landreq._close_repo(row, repo)
            if rerr or not probe_dir:
                found = None
            else:
                ref, _pinned, _target, terr = landreq._close_trunk(
                    row, probe_dir, trunk)
                found = None if terr else landreq.offchain_landing(
                    row, gitdir=probe_dir, trunk=ref)
        except Exception:                             # noqa: BLE001
            found = None
        if found:
            sha, matched, exact = found
            return (SUPERSEDE,
                    "landing unknown by this row's own ref, but trunk %s cites "
                    "%s %r while no later dispatch does — the work may have "
                    "landed under another lane label"
                    % (sha[:12], "lane" if exact else "lane STEM", matched),
                    "helm lr close %s --reason superseded" % rid[:12])
    return (KEEP, "landing %s; claims %s: %s" % (proof, verdict, detail), "")


def classify_task(row, root, now=None):
    """(terminal, evidence, door) for one aged task-ledger row.

    A task row is prose plus whatever clearspan can parse from title+note,
    and the claim families AGE DIFFERENTLY for a task than for a dispatch, so
    they are read separately rather than through re_measure's folded verdict:
      · cited commits answer the LANDED question — but only when EVERY cited
        commit is on trunk (ancestry or patch-id). One landed sha beside one
        absent is IN-FLIGHT work: neither supersede (half the citations deny
        it) nor cancel may ride it — for a dispatch a not-on-trunk sha claim
        is rot, but for a task it is usually the lane tip still being built,
        the OPPOSITE of moot.
      · file:line anchors answer only whether the CITATIONS still resolve —
        every anchor rotted requests re-anchoring; a partially-fresh note stays
        live. Neither state says the task itself is done or moot.
      · count claims are clearspan's dispatch-population re-count and mean
        nothing measured against the task ledger; deliberately unread here."""
    now = time.time() if now is None else now
    tid = str(row.get("id") or "")
    text = " ".join(s for s in (row.get("title"), row.get("note")) if s)
    age = _fmt_age(now - float(row.get("last_updated") or row.get("ts") or now))
    fls, _counts, shas = clearspan.parse_claims(text)
    door = "helm task close %s <reason>" % tid
    # ONE TREE FOR BOTH RUNGS, resolved here rather than inside each. A
    # cannot-look HEAD means neither rung may speak: SUPERSEDE and REANCHOR
    # are both claims ABOUT trunk, and a reader that cannot find trunk has
    # no standing to make either. Falls through to KEEP, which is the
    # honest answer when nothing measurable could be measured.
    head = clearspan._head_sha(root) if root else None
    if shas and root and head:
        results = clearspan._check_shas(shas, root, head)
        fresh = [d for r, d in results if r == clearspan.FRESH]
        if fresh and len(fresh) == len(results):
            return (SUPERSEDE,
                    "every cited commit is on trunk: %s" % "; ".join(fresh[:2]),
                    door)
    if fls and root and head:
        results = clearspan._check_file_lines(
            fls, root, head, filed_ts=_iso(row.get("ts")))
        rotted = [d for r, d in results if r == clearspan.STALE]
        if results and len(rotted) == len(results):
            return (REANCHOR,
                    "untouched %s and every cited anchor rotted; re-anchor the "
                    "citations before judging whether the task is done or moot: "
                    "%s" % (age, "; ".join(rotted[:2])), "")
    return (KEEP,
            "untouched %s; nothing measurable says it is done or moot" % age,
            "")


def _seat_roster():
    """THE ONE fail-closed roster acquisition for a whole sweep."""
    from . import seats as seatsmod
    return seatsmod.roster_checked()


def _live_roster_seats(roster):
    """Canonical addresses with attributable, non-absent current presence."""
    from . import seats as seatsmod
    conflicts = seatsmod.unverified_seats(roster or {})
    return {str(name).casefold() for name, row in (roster or {}).items()
            if seatsmod.presence_with_identity(
                seatsmod.last_seen(name, row), conflicts.get(name))
            not in ("absent", seatsmod.UNVERIFIED)}


def _actor_for_name(name, roster, failed=False, live=None):
    """(canonical_key, row, why) from one roster snapshot; ambiguity refuses."""
    from . import seats as seatsmod
    if failed:
        return None, None, "roster unreadable"
    canonical, err = seatsmod._canonical_recipient(name)
    if err:
        return None, None, err
    hits = [(key, value) for key, value in (roster or {}).items()
            if seatsmod.recipient_matches(key, canonical)]
    if len(hits) == 1:
        # Route by the validated canonical operand, never by the roster's raw
        # display key. The latter is untrusted join-era state and may contain
        # characters that are neither a DM address nor safe command text.
        address = str(canonical)
        if live is not None and address not in live:
            return None, None, "rostered seat has no attributable current presence"
        return address, hits[0][1], None
    if len(hits) > 1:
        return None, None, "identity is ambiguous by case (%s)" % ", ".join(
            sorted(str(key) for key, _row in hits))
    return None, None, "no rostered seat currently holds this address"


def _actor_for_dispatch(row, roster, failed=False, session_index=None,
                        live=None):
    """The current actor for a historical dispatch; a source SID is authority."""
    from . import seats as seatsmod
    if failed:
        return None, None, "roster unreadable"
    source = str(row.get("source") or "").strip()
    if source and source != "cli":
        owners = (session_index if session_index is not None
                  else seatsmod.session_owners(roster or {})).get(source, ())
        if len(owners) == 1:
            # Re-enter the canonical name resolver: a unique SID owner can still
            # share its casefolded address with a corrupt sibling roster row.
            return _actor_for_name(owners[0], roster, failed, live)
        if len(owners) > 1:
            return None, None, "source session is remembered by %s" % ", ".join(
                sorted(str(owner) for owner in owners))
        return None, None, "source session has no current roster owner"
    return _actor_for_name(row.get("sender"), roster, failed, live)


def _family_walled(name, family, wall_snapshot=None):
    """Canonical proxywatch verdict for one already-resolved provider family."""
    from . import proxywatch
    if not family:
        return False, "@%s has no proxy family — provider walls do not apply" % name
    if wall_snapshot is None:
        try:
            wall_snapshot = proxywatch.upstream_snapshot()
        except Exception as e:               # noqa: BLE001 — same law
            wall_snapshot = (None, "proxywatch snapshot failed (%s)" % e)
    snap, serr = wall_snapshot
    if serr or not isinstance(snap, dict):
        return None, str(serr or "proxywatch snapshot unreadable")
    rec, rerr = proxywatch.upstream_record({"upstream": snap}, family)
    if rerr:
        return None, rerr
    state = rec.get("state")
    if proxywatch.beacon_paused(rec):
        return True, "family %s %s%s since %s per proxywatch" % (
            family, state, " (dark latch held)" if state == "UNKNOWN" else "",
            rec.get("since") or "?")
    if state == "HEALTHY":
        return False, "family %s HEALTHY per proxywatch" % family
    return None, "family %s reads %s per proxywatch — not a recovery verdict" % (
        family, state or "?")


def _author_walled(row, actor, wall_snapshot=None):
    """(True|False|None, why) using this dispatch's canonical session runtime."""
    from . import seat as smod
    from . import seats as seatsmod
    name, roster_row = actor
    try:
        source = str(row.get("source") or "").strip()
        runtime, verified = seatsmod.runtime_for_session(
            roster_row, None if source == "cli" else source or None)
        family, err = smod.family_for(name, runtime, verified)
    except Exception as e:                   # noqa: BLE001 — unreadable is UNKNOWN
        return None, "family resolution failed (%s)" % e
    if err:
        return None, "family resolution unavailable (%s)" % err
    return _family_walled(name, family, wall_snapshot)


def _wake_walled(name, roster_row, wall_snapshot=None):
    """Provider wall for every attributable live session behind a seat beacon."""
    from . import beacons, seat as smod, seats as seatsmod
    live = beacons.live_sessions()
    if not isinstance(live, dict):
        return None, "live session census is unreadable"
    known = set()
    if isinstance(roster_row, dict):
        for sid in (roster_row.get("session"), *(roster_row.get("sessions") or ())):
            if isinstance(sid, str) and sid:
                known.add(sid)
        known.update(str(sid) for sid in (roster_row.get("runtime_sessions") or {}))
    active = sorted(known.intersection(live))
    if not active:
        return None, "live beacon session has no exact roster runtime binding"
    families = set()
    for sid in active:
        runtime, verified = seatsmod.runtime_for_session(roster_row, sid)
        family, err = smod.family_for(name, runtime, verified)
        if err:
            return None, "session %s family unavailable (%s)" % (sid[:8], err)
        families.add(family)
    if len(families) != 1:
        shown = ", ".join(sorted(str(family or "none") for family in families))
        return None, "live beacon sessions span provider families %s" % shown
    return _family_walled(name, next(iter(families)), wall_snapshot)


def _redispatch_door(row, reviewer):
    """A shell-safe owner-layer command; free-text lane/brief never enter argv."""
    return shlex.join(["helm", "stale", "redispatch", str(row.get("id") or ""),
                       "--reviewer", reviewer])


def _spent_successors(snap, parent_id, tip):
    """The successors of THIS exact cure that MOVED NOTHING.

    A cancelled, withdrawn, abandoned or stranded child is a pass-through to
    every other reader (`dispatches.carrier`), so the parent stays
    cure-awaiting for as long as one is all it has. The operation identity has
    to advance past them or the only door that can witness the cure reconciles
    onto a corpse and reports success, forever."""
    return tuple(sorted(
        str(kid.get("id") or "") for kid in snap.values()
        if isinstance(kid, dict) and kid.get("supersedes") == parent_id
        and str(kid.get("tip") or "") == str(tip)
        and dispatches.moved_nothing(kid)))


def _cure_operation_key(parent_id, tip, spent):
    """The stale-cure operation identity, advanced once per SPENT successor.

    DERIVED, NEVER A NONCE. The same ledger state derives the same key, so a
    genuine double-send still collides as one operation and the never-resend
    contract is untouched; only a successor the ledger itself records as dead
    moves it. DIGESTED rather than listed because the key caps at 256
    characters and a long-lived cure can outlive more than five 32-hex ids."""
    key = "stale-cure:%s:%s" % (parent_id, tip)
    if not spent:
        return key
    return "%s:after:%d:%s" % (key, len(spent), hashlib.blake2b(
        "\0".join(spent).encode("utf-8"), digest_size=12).hexdigest())


def redispatch_cured(rid, reviewer, repo=None):
    """Execute one current cured successor through dispatches.send's owner layer."""
    current, unavailable = dispatches.snapshot()
    if unavailable:
        return None, "dispatch ledger unavailable: %s" % unavailable, False
    row, err = dispatches._resolve_row(current, rid)
    if err:
        return None, err, False
    # THE SAME ADMISSION AS THE WRITE DOOR, NEVER A SECOND ONE (task/2437).
    # This was the second producer of "run it from that repository's own helm"
    # and it gated on equality with the repository the helm PACKAGE lives in —
    # so a cured row belonging to a registered project could never be
    # redispatched, and the redispatch `_base` would have refused it anyway.
    # One predicate, one refusal text, one place that widens.
    if not row.get("repo_id"):
        return None, ("row records no repository, so its cure cannot be "
                      "placed — a row predating repo_id is redispatched by "
                      "hand"), False
    _project, scope_why = dispatches.write_scope(row.get("repo_id"))
    if scope_why:
        return None, scope_why, False
    root = _verified_placement(row.get("repo_id"), repo) if repo else \
        _worktree_for(row.get("repo_id"), row.get("repo_root"))
    if not root:
        return None, ("row repository has no verified checkout placement; pass "
                      "--repo <checkout> whose common-dir matches the row"), False
    trunk_ref, _pin, _target, terr = landreq._close_trunk(
        row, row["repo_id"], None)
    if terr:
        return None, "row trunk is unreadable: %s" % terr, False
    # The outer Git read supplies only the immutable operation key. Every
    # mutable admission fact is re-derived by the dispatch owner under its lock.
    index, ierr = dispatches._cure_index(root=root, trunk=trunk_ref)
    if ierr:
        return None, "cure identity is unreadable: %s" % ierr, False
    state, where = dispatches.cure_state(
        row, index, reviewed=dispatches.chain_reviewed_tips(row, current))
    if state != dispatches.CURE_AWAITING:
        return None, "row has no unique current cure identity", False
    tip = where[1]
    spent = _spent_successors(current, row["id"], tip)
    key = _cure_operation_key(row["id"], tip, spent)
    message = ("Review the cure for dispatch %s on exact tip %s. Read the parent "
               "FIX verdict, inspect this successor, and return a verdict on the "
               "exact dispatched tip." % (row["id"], tip))

    from . import proxywatch, seats as seatsmod

    validated = {}

    def validate(locked):
        fresh, err = dispatches.cured_unwitnessed(
            locked, ids=[row["id"]], root=root, trunk=trunk_ref)
        hit = [pair for pair in fresh if pair[0].get("id") == row["id"]]
        if err:
            return err
        if len(hit) != 1 or hit[0][1][1] != tip:
            return "row is no longer uniquely cure-awaiting"
        # THE KEY IS DERIVED OUTSIDE THE LOCK, so the set it encodes is a
        # fallible read like every other one here: a successor cancelled in
        # the window would mint this retry under a stale generation.
        if _spent_successors(locked, row["id"], tip) != spent:
            return ("this cure's spent successors changed between the scan "
                    "and the lock — re-run redispatch")
        roster, failed = seatsmod.roster_checked()
        if failed:
            return "reviewer roster is unreadable"
        live = _live_roster_seats(roster)
        target, reviewer_row, why = _actor_for_name(reviewer, roster, False, live)
        if not target or target != reviewer_token:
            return "reviewer is not current and usable: %s" % why
        validated["target"] = target
        waiters, wake_trouble = seatsmod.beacon_procs(target, strict=True)
        if wake_trouble or not waiters:
            return "reviewer has no proven live wake route: %s" % (
                wake_trouble or "no armed beacon")
        try:
            wall_snapshot = proxywatch.upstream_snapshot()
        except Exception as exc:                 # noqa: BLE001 — UNKNOWN refuses
            wall_snapshot = (None, "proxywatch snapshot failed (%s)" % exc)
        walled, wall_why = _wake_walled(target, reviewer_row, wall_snapshot)
        return "reviewer is not provider-healthy: %s" % wall_why \
            if walled is not False else None

    reviewer_token = str(reviewer).strip().lstrip("@").casefold()
    successor, why, sent = dispatches.send(
        reviewer_token, row.get("lane"), message, tip,
        note="stale-bot cured successor", kind="review", repo=root,
        supersedes=row["id"], key=key, unique_key=True,
        _cured_operation={"validate": validate})
    if successor is None or why or not sent:
        return successor, why, sent
    target = validated.get("target", successor.get("recipient"))
    still_live, after_trouble = seatsmod.beacon_procs(target, strict=True)
    if after_trouble or not still_live:
        return successor, ("successor recorded and its DM queued, but reviewer "
                           "beacon was no longer proven live after append: %s"
                           % (after_trouble or "none armed")), False
    return successor, None, True


def _integrator_for(roster, roster_failed):
    """(seat, why) for the live integrator, resolved against THIS pass's roster.

    NO SECOND READ. The caller was handed the snapshot every other actor in
    the classification was resolved against, so the integrator is resolved
    against the SAME one. A fresh read here could disagree with the one the
    reviewer and author were measured from, and the sentence would then name
    two facts about two different rosters.

    AN UNREADABLE ROSTER IS NOT AN ABSENT INTEGRATOR, which is why
    `roster_failed` answers before the lookup rather than falling through to
    an empty snapshot: {} is a PROVEN-EMPTY roster and would report the
    integrator as not resolving, which is a claim, when the honest answer is
    that nothing was measured.
    """
    if roster_failed:
        return None, ("the roster could not be read, so no seat in it can be "
                      "confirmed live")
    return seats_integrator.integrator_seat(roster or {})


def classify_cured(row, where, roster=None, roster_failed=False,
                   wall_snapshot=None, session_index=None, live=None):
    """(terminal, evidence, door, owner), with actor and reviewer independent."""
    from . import seats as seatsmod
    branch, tip, ahead = where
    cure = "cure at %s (+%d ahead on %s)" % (str(tip)[:12], ahead, branch)
    reviewer, reviewer_row, reviewer_why = _actor_for_name(
        row.get("recipient"), roster or {}, roster_failed, live)
    if not reviewer:
        return (PROXY_REDISPATCH,
                "%s; the historical reviewer is not currently actionable (%s). "
                "INTEGRATOR OBLIGATION: choose one current usable reviewer, "
                "then run the owner-layer redispatch" % (cure, reviewer_why),
                "", "")
    try:
        waiters, wake_trouble = seatsmod.beacon_procs(reviewer, strict=True)
    except Exception as e:                    # noqa: BLE001 — UNKNOWN refuses
        waiters, wake_trouble = (), "%s: %s" % (e.__class__.__name__, e)
    if wake_trouble or not waiters:
        return (PROXY_REDISPATCH,
                "%s; reviewer @%s has no proven live wake route (%s). "
                "INTEGRATOR OBLIGATION: choose a current wakeable reviewer"
                % (cure, reviewer, wake_trouble or "no armed beacon"), "", "")
    reviewer_walled, reviewer_wall_why = _wake_walled(
        reviewer, reviewer_row, wall_snapshot)
    if reviewer_walled is not False:
        return (PROXY_REDISPATCH,
                "%s; reviewer @%s is not currently usable (%s). INTEGRATOR "
                "OBLIGATION: choose a provider-healthy reviewer"
                % (cure, reviewer, reviewer_wall_why), "", "")
    if not _worktree_for(row.get("repo_id"), row.get("repo_root")):
        return (PROXY_REDISPATCH,
                "%s; repository checkout placement is UNKNOWN. INTEGRATOR "
                "OBLIGATION: run redispatch with --repo <matching-checkout>"
                % cure, "", "")
    author, author_row, author_why = _actor_for_dispatch(
        row, roster or {}, roster_failed, session_index, live)
    if author and seatsmod.recipient_matches(author, reviewer):
        return (PROXY_REDISPATCH,
                "%s; current actor @%s is also reviewer @%s, so the advertised "
                "self-delivery would refuse before persistence. INTEGRATOR "
                "OBLIGATION: choose another current reviewer"
                % (cure, author, reviewer), "", "")
    integrator, integrator_why = _integrator_for(roster, roster_failed)
    # WHETHER THE OPERATOR IS ALSO THE REVIEWER IS A THREE-STATE QUESTION, and
    # the doors below need a NAME to ask it. With no author, or a walled one,
    # the operator IS the integrator — so an unresolvable integrator does not
    # make the comparison False, it makes it UNANSWERABLE.
    #
    # AN UNANSWERABLE QUESTION MAY NOT COST THE ANSWER TO A DIFFERENT ONE. An
    # earlier shape of this refused outright when the integrator would not
    # resolve, and that DESTROYED A WORKING DOOR: the branches further down
    # return the same PROXY_REDISPATCH terminal WITH a runnable redispatch
    # door, and a roster that cannot name an integrator does not stop that
    # door from working — it only means nobody has checked whether the
    # operator collides with the reviewer. So the collision branches REQUIRE a
    # resolved name (an unresolved one can never match, and recipient_matches
    # is never handed a None), flow continues to the branches that carry the
    # door, and the unanswered question rides along as a CLAUSE rather than
    # replacing the answer.
    unmeasured_operator = ("" if integrator else
                           "; and WHO would operate is UNKNOWN (%s), so "
                           "whether that operator is also the reviewer was "
                           "never measured" % integrator_why)
    if not author and integrator and seatsmod.recipient_matches(integrator,
                                                                reviewer):
        return (PROXY_REDISPATCH,
                "%s; the integrator @%s would be both operator and reviewer, "
                "so self-delivery would refuse. Choose another current reviewer"
                % (cure, integrator), "", "")
    door = _redispatch_door(row, reviewer)
    if not author:
        return (PROXY_REDISPATCH,
                "%s; the recorded author is not currently addressable (%s) — "
                "the integrator must operate the successor%s"
                % (cure, author_why, unmeasured_operator), door, "")
    walled, why = _author_walled(
        row, (author, author_row), wall_snapshot=wall_snapshot)
    if walled is not False and integrator and seatsmod.recipient_matches(
            integrator, reviewer):
        return (PROXY_REDISPATCH,
                "%s; proxy operator @%s is also the reviewer, so self-delivery "
                "would refuse. Choose another current reviewer"
                % (cure, integrator), "", "")
    if walled:
        return (PROXY_REDISPATCH,
                "%s; author @%s is WALLED (%s) — send the review on their "
                "behalf%s" % (cure, author, why, unmeasured_operator),
                door, "")
    if walled is None:
        return (PROXY_REDISPATCH,
                "%s; author @%s wall-state UNKNOWN (%s) — the integrator must "
                "operate rather than bill an unproven actor%s"
                % (cure, author, why, unmeasured_operator), door, "")
    return (REDISPATCH,
            "%s and NOBODY is waiting on it — current actor @%s is reachable "
            "(%s); re-dispatch the review on the cured tip"
            % (cure, author, why), door, author)


def _iso(epoch):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch)))
    except (TypeError, ValueError):
        return None


def _fmt_age(s):
    s = max(0, int(s or 0))
    d, rem = divmod(s, 86400)
    if d:
        return "%dd%dh" % (d, rem // 3600)
    return "%dh%02dm" % (rem // 3600, (rem % 3600) // 60)


# ---------------------------------------------------------------------------
# ownership — who each proposal is addressed to
# ---------------------------------------------------------------------------

def owner_of_item(it):
    """The seat this item's digest addresses, or "" for the integrator bucket.

    An lr row's debtor comes from its own owed_by ROLE (landreq's OWED_BY —
    reviewer/builder sit on the recipient, author on the sender); land-side
    and integrator roles have no per-seat addressee and fold into the
    integrator digest, as does anything unknown. A plain overdue dispatch is
    owed by its recipient; a task row by tasks.owner_of (the one place that
    decides, so placeholder owners read unowned here too)."""
    if it["kind"] == "task":
        return tasks.owner_of(it["row"])
    lr = it.get("lr")
    if lr is not None:
        role = str(lr.get("owed_by") or "")
        if role in ("reviewer", "builder"):
            return str(lr.get("reviewer") or "")
        if role == "author":
            return str(lr.get("author") or "")
        return ""
    return str(it["row"].get("recipient") or "")


# ---------------------------------------------------------------------------
# the digest — task/431's async-convergence form, one per owner per sweep
# ---------------------------------------------------------------------------

def _short_id(it):
    return it["id"] if it["kind"] == "task" else it["id"][:12]

def rendered(its):
    """(shown, dropped) — THE ONE DECISION about what this digest displays.

    A DISPLAY CAP MAY NEVER EDIT THE RECORD (a P1 finding, measured on the
    first cut: `digest_text` sliced to MAX_LINES while the latch loop walked
    `by_owner[owner]` WHOLE, so every row past the cap was recorded
    proposed-at-now — delivered — while the owner never saw it. Silent loss of
    exactly the rows this loop exists to surface, and the more backlog an owner
    carried the more of it vanished.)

    The cure is a single authority both readers call, rather than two places
    that each decide what "shown" means and agree only by coincidence. The
    caller latches `shown`; `digest_text` renders `shown` and DISCLOSES
    `dropped` by count — web_land's closed_total shape, where the capped list
    and the true total travel together so a reader can never mistake the page
    for the population."""
    its = list(its)
    return its[:MAX_LINES], its[MAX_LINES:]


def digest_text(owner, its):
    """One owner's digest: every aged row, its PROPOSED disposition, one
    evidence line, and any terminal door — requiring one CONCUR/OVERRULE reply
    per line
    (task/431: a durable exchange naming every finding with a proposed
    decision beats a synchronous meld the recipient cannot attend). Capped at
    MAX_LINES via `rendered`, with the remainder COUNTED and explicitly
    declared UNLATCHED — a digest that silently omits rows is the forgetting
    this loop exists to end, and a digest that omits them while the state file
    calls them delivered is worse than the forgetting."""
    shown, dropped = rendered(its)
    lines = ["@%s [%s] %d aged or cure-awaiting row(s) on your name — "
             "PROPOSED dispositions below. Reply CONCUR/OVERRULE per line; "
             "this bot proposes and "
             "never executes, so nothing moves without you."
             % (owner, BOT, len(its))]
    for i, it in enumerate(shown, 1):
        door = (" — door: %s" % it["door"]) if it["door"] else ""
        lines.append("%d. %s (%s, %s) PROPOSED %s: %s%s" % (
            i, _short_id(it), it["kind"], it["why_aged"], it["terminal"],
            it["evidence"], door))
    if dropped:
        lines.append("… +%d more row(s) on your name NOT shown here and NOT "
                     "recorded as asked — they ride the NEXT sweep rather "
                     "than being latched behind this cap. `helm stale sweep "
                     "--json` lists every one now." % len(dropped))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# state — the latch, the re-arm, and the loop's own liveness record
# ---------------------------------------------------------------------------

def _fingerprint(it):
    """What this proposal SAYS, not merely which word it uses.

    THE TERMINAL ALONE IS TOO WEAK A LATCH KEY. A cured row whose
    author pushes ANOTHER cure commit is a different fact — new tip, more work
    ahead, possibly a different reviewer's problem — but its disposition is
    still `redispatch-candidate`, so a terminal-only comparison latched it as
    "the same ask" and the newer cure went unmentioned for the whole window.
    Same defect one kind over: a retipped dispatch keeps its terminal while the
    content under it changed.

    So the key carries the terminal, semantic actor/reviewer, whether an exact
    owner-layer action is currently runnable, AND the row's content identity:
    the cure tip for a cured row, the reviewed tip/ref for a dispatch or lr row. A task
    row has no content identity of that shape and keeps the terminal alone —
    keying it on last_updated would re-propose on every touch, which is the
    nag this budget exists to prevent."""
    term = str(it.get("terminal") or "")
    # THE ROLE, NOT THE SEAT. This field is an IDENTITY FOR COMPARISON and
    # never an addressee, so it carries the role token: keying an unowned row
    # on the integrator's current NAME re-proposes every latched row the day
    # that seat is renamed, and a rename does not change the ask.
    actor = str(it.get("owner") or UNOWNED_ACTOR)
    reviewer = str(it.get("reviewer") or "")
    action = "runnable" if it.get("door") else "blocked"
    identity = "%s|actor=%s|reviewer=%s|action=%s" % (
        term, actor, reviewer, action)
    if it["kind"] == "cured":
        where = it.get("where") or ("", "", 0)
        return "%s@%s+%s" % (identity, str(where[1])[:40], where[2])
    if it["kind"] in ("task", "source"):
        return identity
    row = it.get("row") or {}
    tip = str(row.get("reviewed_tip") or row.get("ref") or "")
    return "%s@%s" % (identity, tip[:40])


def _source_alert_identity(problem):
    """Stable source identity supplied by producers, with legacy string fallback."""
    source = getattr(problem, "source_key", None)
    if source:
        return str(source)
    text = str(problem or "")
    if text.startswith("cured-fix scan ("):
        return text.split("): ", 1)[0] + ")"
    if text.startswith("cured-fix scan: repo "):
        return text.split(" is unreadable", 1)[0]
    if text.startswith("cured-fix scan:"):
        return "cured-fix scan"
    return text.split(":", 1)[0] or "unknown source"


def _latch_holds(entry, it, now):
    """True when this row was already asked about, unchanged, inside the window.

    LEGACY ENTRIES FALL BACK TO THE TERMINAL, deliberately. A state file
    written before `_fingerprint` existed carries no fingerprint key, and
    treating that absence as a mismatch would re-propose EVERY currently
    latched row across the whole fleet the first time this ships — spending
    the entire attention budget on a schema change. A legacy entry is at most
    one window (REPROPOSE_S) behind on a changed tip and then self-heals,
    because the entry it writes on re-propose carries the new key."""
    if now - float(entry.get("proposed_at") or 0) >= REPROPOSE_S:
        return False
    stored = entry.get("fingerprint")
    if stored is None:
        return entry.get("terminal") == it["terminal"]
    return stored == _fingerprint(it)


def _state_path():
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", _STATE)


def read_state(path=None):
    return pk.read_json(path or _state_path(), {}) or {}


def sweep(now=None, post=True, quiet=False, task_path=None, state_path=None,
          repo=None, trunk=None):
    """One full pass: collect -> classify -> latch -> digest -> post -> record.

    post=False (--dry-run) decides and writes NOTHING — not the state file,
    not a chat row — so the loop can be inspected before it is trusted.
    quiet=True records state without posting (the sweep ran; the room was
    spared), which is the doctor's probe mode. A digest whose POST FAILS does
    not latch its rows: the next sweep re-covers the window rather than
    marching past an undelivered ask (repo-watch's cursor lesson)."""
    now = time.time() if now is None else now
    items, unavailable, no_deadline = collect(now, task_path,
                                              repo=repo, trunk=trunk)
    from . import work
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = repo or work.find_root(here) or here
    # ONE canonical roster and ONE wall snapshot per sweep. Every cured line
    # must describe the same actors and provider fact; a file replacement or
    # freshness transition between rows cannot split one digest's routing.
    seat_roster, roster_failed = {}, False
    session_index, live_seats = {}, set()
    wall_snapshot = None
    if any(it["kind"] == "cured" for it in items):
        try:
            seat_roster, roster_failed = _seat_roster()
            if not roster_failed:
                from . import seats as seatsmod
                session_index = seatsmod.session_owners(seat_roster)
                live_seats = _live_roster_seats(seat_roster)
        except Exception:                    # noqa: BLE001 — UNKNOWN, never empty
            seat_roster, roster_failed = {}, True
            session_index, live_seats = {}, set()
        try:
            from . import proxywatch
            wall_snapshot = proxywatch.upstream_snapshot()
        except Exception as e:               # noqa: BLE001 — same law
            wall_snapshot = (None, "proxywatch snapshot failed (%s)" % e)
    for it in items:
        if it["kind"] == "task":
            term, ev, door = classify_task(it["row"], root, now)
            owner = owner_of_item(it)
        elif it["kind"] == "cured":
            # the cured arm owns its ADDRESSEE too: the author when reachable,
            # the integrator when the author is walled/unknown — owner_of_item
            # reads recipients, and on a cured row the recipient is the
            # reviewer who already answered.
            term, ev, door, owner = classify_cured(
                it["row"], it["where"], seat_roster, roster_failed,
                wall_snapshot, session_index, live_seats)
            it["reviewer"] = (_actor_for_name(
                it["row"].get("recipient"), seat_roster, roster_failed,
                live_seats)[0] or "")
        else:
            term, ev, door = classify_dispatch(it["row"], it.get("lr"),
                                               repo, trunk)
            owner = owner_of_item(it)
        it.update({"terminal": term, "evidence": ev, "door": door,
                   "owner": owner, "latched": False})

    # Source failures are themselves obligations. stderr/state are pull-only;
    # each distinct unavailable fact therefore joins the integrator's addressed
    # digest and wake path, under a stable synthetic id so the same failure can
    # spend the attention budget without becoming a daily nag.
    alert_map = {}
    for problem in unavailable:
        source = _source_alert_identity(problem)
        digest = hashlib.blake2b(source.encode("utf-8"), digest_size=12).hexdigest()
        rid = "source-" + digest
        prior = alert_map.get(rid)
        evidence = str(problem) if prior is None else \
            prior["evidence"] + "; " + str(problem)
        alert_map[rid] = {"kind": "source", "id": rid,
                          "row": {}, "lr": None, "age_s": 0,
                          "why_aged": "stale-bot source is UNAVAILABLE",
                          "terminal": SOURCE_UNAVAILABLE, "evidence": evidence,
                          "source_key": source, "reviewer": "",
                          "door": "", "owner": "", "latched": False}
    alerts = list(alert_map.values())
    actionable = items + alerts
    p = state_path or _state_path()
    posted, failed, rearmed = [], [], 0

    def _plan(rows_st):
        for candidate in actionable:
            candidate["latched"] = False
        due, held = [], 0
        for candidate in actionable:
            entry = rows_st.get(candidate["id"])
            if entry and _latch_holds(entry, candidate, now):
                candidate["latched"] = True
                held += 1
            else:
                due.append(candidate)
        by_owner = {}
        unowned_owner = None
        for order, candidate in enumerate(due):
            owner = candidate["owner"]
            if not owner:
                # RESOLVED LAZILY AND ONCE PER PLAN. This one IS an addressee —
                # the digest is DM'd to it — so it takes the loud-default door
                # rather than the role token: a digest needs a name a send can
                # reach, and `integrator_seat_or_default` announces the
                # substitution once per process instead of making it silently.
                if unowned_owner is None:
                    unowned_owner = seats_integrator.integrator_seat_or_default()
                owner = unowned_owner
            entry = rows_st.get(candidate["id"])
            # NEVER-ASKED FIRST. At daily cadence with a three-day latch, source
            # order otherwise lets the first 12 expire just as rows 37+ reach the
            # cap, so those first 12 consume every fourth digest forever.
            priority = (1 if entry else 0,
                        float((entry or {}).get("proposed_at") or 0), order)
            by_owner.setdefault(owner, []).append((priority, candidate))
        by_owner = {owner: [candidate for _key, candidate in sorted(bucket)]
                    for owner, bucket in by_owner.items()}
        digests = {owner: digest_text(owner, bucket)
                   for owner, bucket in by_owner.items()}
        dropped = sum(len(rendered(bucket)[1]) for bucket in by_owner.values())
        never = [candidate["age_s"] for candidate in items
                 if candidate["id"] not in rows_st]
        return (by_owner, digests, dropped, held,
                max(never) if never else 0)

    if not post:
        rows_st = (read_state(p).get("rows") or {})
        by_owner, digests, dropped_total, latched, oldest_unproposed = _plan(
            rows_st)
    else:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".lock", "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            st = read_state(p)
            rows_st = st.get("rows") or {}
            by_owner, digests, dropped_total, latched, oldest_unproposed = _plan(
                rows_st)
            # A public append is a record, not a wake. _post returns true only
            # when the addressed DM leg reached the current actor; only then may
            # the rendered rows spend the attention budget and latch.
            for owner, text in ({} if quiet else digests).items():
                ok = _post(owner, text)
                (posted if ok else failed).append(owner)
                if ok:
                    for candidate in rendered(by_owner[owner])[0]:
                        rows_st[candidate["id"]] = {
                            "proposed_at": now,
                            "terminal": candidate["terminal"],
                            "fingerprint": _fingerprint(candidate)}
            seen = {candidate["id"] for candidate in actionable}
            recovered_alerts = [key for key in rows_st
                                if key.startswith("source-") and key not in seen]
            for key in recovered_alerts:
                rows_st.pop(key, None)
            if unavailable:
                rearmed = len(recovered_alerts)
            else:
                stale_keys = [key for key in rows_st if key not in seen]
                for key in stale_keys:
                    rows_st.pop(key, None)
                rearmed = len(recovered_alerts) + len(stale_keys)
            st = {"last_run": now, "rows": rows_st, "swept": len(items),
                  "proposed": sum(len(rendered(by_owner[owner])[0])
                                  for owner in posted),
                  "latched": latched, "digests_posted": len(posted),
                  "digests_failed": len(failed),
                  "capped_unlatched": dropped_total,
                  "rearm_suspended": bool(unavailable), "rearmed": rearmed,
                  "oldest_unproposed_s": oldest_unproposed,
                  "unavailable": unavailable}
            pk.write_json(p, st)

    return {"swept": len(items), "no_deadline": no_deadline,
            "items": [{k: it[k] for k in ("kind", "id", "owner", "age_s",
                                          "why_aged", "terminal", "evidence",
                                          "door", "latched")}
                      for it in items],
            "alerts": [{k: it[k] for k in ("kind", "id", "owner", "age_s",
                                           "why_aged", "terminal", "evidence",
                                           "door", "latched")}
                       for it in alerts],
            "digests": digests, "posted": posted, "failed": failed,
            "latched": latched, "capped_unlatched": dropped_total,
            "rearm_suspended": bool(unavailable),
            "oldest_unproposed_s": oldest_unproposed,
            "unavailable": unavailable, "state_path": p}


def _post(owner, text):
    """Prove room+DM appends and a still-live armed actor after the queued DM."""
    from . import chat, proxywatch, seats
    roster, failed = seats.roster_checked()
    live = set() if failed else _live_roster_seats(roster)
    target, row, why = _actor_for_name(owner, roster, failed, live)
    if not target or seats.recipient_matches(BOT, target):
        print("helm stale sweep: wake target @%s unusable: %s" % (
            owner, why or "self-delivery"), file=sys.stderr)
        return False
    try:
        wall_snapshot = proxywatch.upstream_snapshot()
    except Exception as e:                    # noqa: BLE001 — UNKNOWN refuses
        wall_snapshot = (None, "proxywatch snapshot failed (%s)" % e)
    walled, wall_why = _author_walled(
        {"source": "cli"}, (target, row), wall_snapshot)
    if walled is not False:
        print("helm stale sweep: wake target @%s unavailable: %s" % (
            target, wall_why), file=sys.stderr)
        return False
    # beacon_procs is fail-open for stop-guards, where an unproven beacon must
    # not stop a healthy seat. A delivery latch is the opposite boundary:
    # UNKNOWN must remain visible, so trouble and proven emptiness both refuse.
    waiters, trouble = seats.beacon_procs(target, strict=True)
    if trouble or not waiters:
        print("helm stale sweep: wake target @%s has no proven live beacon: %s" % (
            target, trouble or "none armed"), file=sys.stderr)
        return False
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
    episode = int(time.time() // REPROPOSE_S)
    public_key = "stale-bot:%s:%s" % (digest, episode)
    try:
        public = chat.post(text, room=ROOM, who=BOT, sign=False,
                           event_id=public_key + ":room")
    except Exception as e:                    # noqa: BLE001 — both legs required
        print("helm stale sweep: room record failed: %s" % e, file=sys.stderr)
        return False
    if not isinstance(public, dict) or not public.get("id"):
        print("helm stale sweep: room record returned no proof", file=sys.stderr)
        return False
    try:
        private = chat.post(text, who=BOT, sign=False, dm=target,
                            dm_display=target, event_id=public_key + ":dm")
    except Exception as e:                    # noqa: BLE001 — unlatched retry
        print("helm stale sweep: wake to @%s failed: %s" % (target, e),
              file=sys.stderr)
        return False
    if not isinstance(private, dict) or not private.get("id"):
        print("helm stale sweep: wake to @%s returned no proof" % target,
              file=sys.stderr)
        return False
    still_live, after_trouble = seats.beacon_procs(target, strict=True)
    if after_trouble or not still_live:
        print("helm stale sweep: queued DM to @%s, but its beacon was no longer "
              "proven live after append: %s" % (
                  target, after_trouble or "none armed"), file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# cadence — the daily systemd user timer (external, no demons)
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_S = 86400   # housekeeping is daily business: aged means DAYS,
                             # and a tighter loop would only re-bill attention

_UNIT_SERVICE = """[Unit]
Description=helm stale-bot housekeeping sweep (sweep proposes; redispatch mutates)

[Service]
Type=oneshot
WorkingDirectory=%(cwd)s
Environment=HELM_CHAT_NAME=stale-bot
UnsetEnvironment=CLAUDE_CODE_SESSION_ID CLAUDE_SESSION_ID CODEX_SESSION_ID
ExecStart=%(helm)s stale sweep
"""
# Environment + UnsetEnvironment together are repo-watch's measured env
# hygiene: a clean identity needs a clean env, because an inherited session id
# makes the declared name a DISPUTE against the session's rostered seat (the
# guard refused repo-watch's install test from the integrator's own shell).

_UNIT_TIMER = """[Unit]
Description=helm stale-bot cadence (external, no demons)

[Timer]
OnBootSec=1800
OnUnitActiveSec=%(interval)ss

[Install]
WantedBy=timers.target
"""


def _timer_units(interval=DEFAULT_INTERVAL_S):
    # Same law as tasksmirror._timer_units, same reason: a persistent unit
    # must never capture a DISPOSABLE WORKTREE's path — the binary is the
    # stable install, the cwd is the lane folded back to the shared checkout.
    from . import work
    helm_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "helm")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = work.find_root(here) or here
    udir = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
    return (os.path.join(udir, "helm-stale-bot.service"),
            _UNIT_SERVICE % {"helm": helm_bin, "cwd": cwd},
            os.path.join(udir, "helm-stale-bot.timer"),
            _UNIT_TIMER % {"interval": interval})


def ensure_timer(interval=DEFAULT_INTERVAL_S):
    """Install/refresh and enable the daily cadence -> (ok, detail).
    Idempotent: re-running is the refresh path (tasksmirror's shape)."""
    import shutil
    import subprocess
    if interval < 1:
        return False, "interval must be at least 1 second"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False, ("systemctl unavailable; run `helm stale sweep` from "
                       "another scheduler")
    spath, service, tpath, timer = _timer_units(interval)
    try:
        os.makedirs(os.path.dirname(spath), exist_ok=True)
        pk.atomic_write(spath, service)
        pk.atomic_write(tpath, timer)
    except OSError as e:
        return False, "unit write failed: %s" % e
    for cmd in ([systemctl, "--user", "daemon-reload"],
                [systemctl, "--user", "enable", "--now",
                 "helm-stale-bot.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return False, "%s failed: %s" % (
                " ".join(cmd), (r.stderr or r.stdout or "").strip()[:200])
    return True, "stale-bot cadence enabled every %ds (%s)" % (interval, tpath)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_stale(args):
    args = list(args or [])
    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    from . import cli
    if args and args[0] == "redispatch":
        rest = args[1:]
        if rest == ["-h"] or rest == ["--help"]:
            print(_USAGE)
            return 0
        rc = cli.guard_tail(
            "helm stale redispatch", rest[1:] if rest else (),
            valued=("--reviewer", "--repo"), usage=_USAGE)
        if rc is not None:
            return rc
        if not rest or len(rest[1:]) not in (2, 4):
            print(_USAGE, file=sys.stderr)
            return 2
        opts = {}
        tail = rest[1:]
        for i in range(0, len(tail), 2):
            flag, value = tail[i:i + 2]
            if flag not in ("--reviewer", "--repo") or not value or flag in opts:
                print(_USAGE, file=sys.stderr)
                return 2
            opts[flag] = value
        if "--reviewer" not in opts:
            print(_USAGE, file=sys.stderr)
            return 2
        row, why, sent = redispatch_cured(
            rest[0], opts["--reviewer"], repo=opts.get("--repo"))
        if row is None or why:
            print("helm stale redispatch: %s" % (why or "successor not recorded"),
                  file=sys.stderr)
            return 1
        print("helm stale redispatch: %s -> @%s %s%s" % (
            row["id"], row.get("recipient"), row.get("lane"),
            " (delivery observed)" if sent else " (delivery needs confirmation)"))
        return 0
    if not args or args[0] != "sweep":
        print("helm stale: unknown subverb %r (want: sweep|redispatch)\n%s"
              % (args[0] if args else "", _USAGE), file=sys.stderr)
        return 2
    rest = args[1:]
    rc = cli.guard_tail("helm stale sweep", rest,
                        flags=("--dry-run", "--quiet", "--json",
                               "--ensure-timer"), usage=_USAGE)
    if rc is not None:
        return rc
    if "--ensure-timer" in rest:
        ok, detail = ensure_timer()
        print("helm stale sweep: %s" % detail,
              file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    rep = sweep(post="--dry-run" not in rest, quiet="--quiet" in rest)
    if "--json" in rest:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        for it in rep["items"]:
            print("%-12s %-8s @%-14s %-20s %s%s" % (
                _short_id(it), it["kind"], it["owner"] or "(integrator)",
                it["terminal"], it["why_aged"],
                " (latched)" if it["latched"] else ""))
        if "--dry-run" in rest:
            for owner, text in rep["digests"].items():
                print("\n--- would post (@%s) ---\n%s" % (owner, text))
        print("helm stale sweep: %d aged row(s), %d proposal digest(s) %s, "
              "%d latched, oldest unproposed %s"
              % (rep["swept"], len(rep["digests"]),
                 "would post" if "--dry-run" in rest else
                 ("posted" if rep["posted"] or not rep["digests"] else "FAILED"),
                 rep["latched"], _fmt_age(rep["oldest_unproposed_s"])))
        if rep["no_deadline"]:
            print("  %d open row(s) predate the deadline field — never "
                  "age-measurable, counted not dropped" % rep["no_deadline"])
    for u in rep["unavailable"]:
        print("helm stale sweep: source UNAVAILABLE — %s (its rows are "
              "UNKNOWN, not fine)" % u, file=sys.stderr)
    for owner in rep["failed"]:
        print("helm stale sweep: digest to @%s FAILED to post — its rows "
              "stay unlatched and re-propose next sweep" % owner,
              file=sys.stderr)
    if rep["unavailable"] and not rep["swept"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cmd_stale(sys.argv[1:]))
