"""helm work — the cli cluster: the `helm work` verb dispatcher.
Moved verbatim from the pre-split helm/work.py.
"""
import json
import os
import re
import sys

from .. import seats
from ._common import DEFAULT_TTL, LANE_RE, RECENT_WRITE_SECONDS
from ._lanes import _worktree_records, find_root, unguarded_inventory
from ._gc import (format_gc_summary, gc_enact, gc_scan, lane_overlaps,
                  phantom_scan, prune_phantom_records, refresh_trunk,
                  list_rows, post_gc_summary, trunk_sync)
from ._guard import stale_guard_hooks
from ._claims import (_infer_lane, _positional, claim, release_lane,
                      release_stale_lane)
from ._guard import install_guard


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """usage: helm work <verb> [--repo PATH] [--seat S]
  claim <lane> [--ttl N] [--lease ID]   check in: lease + private room —
                                        prints path<TAB>branch<TAB>lease<TAB>ttl
  release [<lane>] --lease ID [--park] [--stale]  check out: dirty refuses (--park
                                        WIP-commits); landed room retires,
                                        unlanded room + branch stay for triage.
                                        Lost the id? `helm work list` reprints
                                        your own.
  peek <committish> [--json]            read-only look: disposable DETACHED
                                        worktree at exactly that commit under
                                        <repo>-wt/peeks/<sha12> — no lease, no
                                        branch, no lock, and it passes the ref
                                        guard with NO env (the reviewer door;
                                        prints path<TAB>sha)
  peek --drop <path-or-committish>      retire a peek room (occupied, pane-
                                        bound, or dirty REFUSES; never --force)
  gc [--apply]                          housekeeping: keep/triage/rescue/remove
                                        (dry-run; only clean LANDED rooms retire)
  list                                  the room board: worktree registry x
                                        claims, plus YOUR OWN lease ids
  install-guard [--apply]               composed deterministic git guards (dry/apply):
                                        shared-tree rail + pre-commit never-track
                                        staged-set scan (helm/nevertrack.py)
  stash list|apply|pop|drop|show <msg>  address a stash by its MESSAGE — a
                                        stash@{N} argument is REFUSED (a
                                        position shifts when anyone drops an
                                        entry; an ambiguous message refuses
                                        rather than guessing)"""


_DANGLING_NOTE = (
    "    !! DANGLING CONFLICT: %d unmerged path(s) with NO git operation in\n"
    "       progress — the shape a conflicted `git stash apply/pop` leaves.\n"
    "       `git merge --abort` WILL REFUSE (git sees no merge in progress), so\n"
    "       do NOT reach for it. Before anything else, check that every\n"
    "       conflicted file still has its <<<<<<< markers: if any are resolved,\n"
    "       that is SOMEONE'S WORK and clearing it would destroy it.\n"
    "       Then clear ONLY THE CONFLICTED PATHS, one at a time:\n"
    "         git -C %s restore --staged --worktree -- <conflicted-path>\n"
    "       and re-apply the stash in ITS OWN room.\n"
    "       Do NOT use `reset --hard HEAD` — the prohibition is on this line on\n"
    "       purpose, so grepping for the command cannot surface a line that\n"
    "       reads like an instruction. The marker check above covers the\n"
    "       CONFLICTED paths, but a hard reset discards every OTHER uncommitted\n"
    "       change in the tree too, which the check never looked at. The mismatch\n"
    "       is not hypothetical — it nearly destroyed an unrelated modified test\n"
    "       file in a lane whose conflict had nothing to do with it (2026-07-24).\n"
    "       Refer to a stash by MESSAGE, never `stash@{N}`: an index is a\n"
    "       POSITION and shifts under everyone when any stash is dropped.")


def _blocked_reason(lines):
    """The SKIPPED reason from one gc_enact result, compressed for a summary.

    A planned removal that did not happen has its reason in the enact lines and
    nowhere else; the count alone cannot say whether the estate is clean or
    jammed. Falls back to "unreported" rather than inventing one — a summary
    that guesses why is worse than one that admits it does not know."""
    for ln in reversed(lines or []):
        if "SKIPPED " not in ln:
            continue
        # DO NOT REQUIRE THE CLOSING ") — kept". Measured 2026-08-04 on the
        # first live run of this very feature: an adapter error carried a
        # multi-line JSON body, so the parens never balanced on one line and
        # the summary printed "blocked: unreported" while the reason sat two
        # characters away. A summary that cannot name a reason it HAS is the
        # same defect this function exists to fix, one layer in.
        why = ln.split(" (", 1)[1] if " (" in ln else ln
        why = why.split(" — kept")[0].strip()
        low = why.lower()
        if "pane" in low:
            return "bound pane" if "bound to this room" in low \
                else "pane close failed"
        if "occupied" in low:
            return "occupied"
        if "lease" in low:
            return "lease live"
        if "locked" in low:
            return "locked"
        return why.splitlines()[0][:40].strip() or "unreported"
    return "unreported"


def _show(path):
    """A worktree path is ATTACKER-SHAPED TEXT once it reaches a report.

    codex-2's fourth finding on this lane (its r4 review round): "raw newline
    paths spoof summary output". A path may legally contain a newline, and
    every line this verb prints is a single-line claim an operator reads as one
    fact — so one embedded newline forges an entire extra "pruned phantom
    record ..." line for a record that was never touched. Same shape as the
    meld-room launder: the value need not be hostile, the FRAME it rides in is
    single-line, and only the emitter can defend it.

    Escaped via repr rather than stripped, because the operator still has to be
    able to identify the complete path. A silently-shortened path is a
    different lie. Printability is the boundary, not a hand-picked control
    list: ESC and the other terminal controls can forge one line without a
    newline."""
    return path if path.isprintable() else repr(path)


def _tree_word(row):
    """The tree's state in ONE word. CONFLICT and DANGLING are called out rather
    than folded into "dirty", because that folding is exactly what hid a dangling
    conflict in the shared checkout five times in one day — a room with edits and
    a room stuck mid-conflict need completely different remedies."""
    if row.get("dangling_conflict"):
        return "DANGLING"
    if row.get("conflicts"):
        return "CONFLICT"
    return "dirty" if row["dirty"] else "clean"


def cmd_work(args):
    """work claim|release|gc|list|install-guard — worktree lifecycle on the
    claims lane: private room per lane, the shared checkout stays the
    integrator's, abandoned dirty work is rescued, never discarded."""
    args = list(args or [])
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    repo = seats._flag(rest, "--repo")
    root = find_root(repo) if repo else find_root()
    if not root:
        print("helm work: not inside a git repo (--repo PATH names one)",
              file=sys.stderr)
        return 2
    seat = seats._flag(rest, "--seat") or seats.derive_seat(None)
    session = seats._env_session()
    if verb == "claim":
        pos = _positional(rest)
        if not pos or not LANE_RE.match(pos[0]):
            print("usage: helm work claim <lane>  (lane = [A-Za-z0-9._-]{1,64};"
                  " keep the printed lease id — it is the room key)",
                  file=sys.stderr)
            return 2
        # `<root>-wt/seats/` is the per-seat HOME container; a lane by that name
        # would try to check a room out ON TOP of it and fail obscurely. The
        # peek container is reserved for the same reason.
        from ..harness import SEAT_HOME_DIRNAME
        from ._common import PEEK_DIRNAME
        if pos[0] == SEAT_HOME_DIRNAME:
            print("helm work: '%s' is reserved — it is the per-seat home "
                  "worktree container (<repo>-wt/%s/<seat>); pick another lane "
                  "name" % (SEAT_HOME_DIRNAME, SEAT_HOME_DIRNAME),
                  file=sys.stderr)
            return 2
        if pos[0] == PEEK_DIRNAME:
            print("helm work: '%s' is reserved — it is the read-only peek "
                  "container (<repo>-wt/%s/<sha12>); pick another lane name"
                  % (PEEK_DIRNAME, PEEK_DIRNAME), file=sys.stderr)
            return 2
        ttl = seats._flag(rest, "--ttl")
        rc, line = claim(root, pos[0], seat,
                         ttl=int(ttl) if ttl else DEFAULT_TTL,
                         lease=seats._flag(rest, "--lease"), session=session)
        print(line, file=sys.stdout if rc == 0 else sys.stderr)
        if rc == 0:
            # WHO ELSE IS LIVE, AND IN WHAT. A lease answers "is anyone in this
            # ROOM", never "is anyone already fixing this DEFECT" — and the
            # board's answer to the second question (lane_overlaps) lives in
            # `gc`, so it reaches whoever runs housekeeping AFTER the duplicate
            # is written. 2026-08-04: three seats built one fix under three
            # different lane labels, all in helm/cli.py.
            #
            # It DISCLOSES rather than compares, because the lane just claimed
            # has no commits and nothing can compare an empty diff. The claimer
            # knows what they are about to touch; this is the half they cannot
            # get. Fail-open and last: a git hiccup must never cost a claim
            # that already succeeded.
            try:
                from ._gc import moved_lane_targets
                # DID TRUNK MOVE THE FILE OUT FROM UNDER A LIVE LANE. Sibling
                # of the disclosure below, one axis over: that one says who
                # else is in this file NOW, this one says the file you are in
                # is no longer where trunk keeps that code. Printed FIRST
                # because it is rarer and strictly more urgent — an unnoticed
                # moved target lands an edit on a facade and the real caller
                # keeps calling a name the same commit deleted.
                for lane, gone in moved_lane_targets(root)[:6]:
                    shown = ", ".join(gone[:4])
                    if len(gone) > 4:
                        shown += " (+%d more)" % (len(gone) - 4)
                    # THE WRAPPER MUST NOT ASSERT THE EVENT — the entry
                    # already names it, and this sentence used to contradict
                    # it. It read "trunk NO LONGER HAS it" directly after
                    # printing "(trunk -3685, yours 1)", which shows trunk DOES
                    # have it and merely shrank it: a reader sent hunting for a
                    # deleted file that is still there. The producer learned to
                    # tell deleted from drained and the surface collapsed them
                    # back into one false claim, which is the seam class in a
                    # single sentence. Now the wrapper states only what is true
                    # of BOTH events and the entry carries which one.
                    print("helm work: lane %s — trunk changed code this lane "
                          "edits: %s; rebase before trusting a gate on it"
                          % (lane, shown), file=sys.stderr)
            except Exception:
                pass
            # A SECOND try, NOT a shared one. A reviewer measured the shared
            # version: with the moved-target call raising, the live-lane
            # disclosure below DISAPPEARED ENTIRELY and the claim still
            # returned 0 — a new guard silently deleting an older one, which
            # is worse than the gap it was added to close. Each disclosure
            # fails open ALONE.
            try:
                from ._gc import held_lane_files
                # NEVER OMIT AN IDENTITY, ONLY ABBREVIATE EVIDENCE.
                #
                # Per-class caps were the wrong shape and @codex reproduced
                # why: 5 UNKNOWN / 8 bare / 5 file-bearing kept 3/4/4 rows and
                # a generic count, so the fourth UNKNOWN lane and the fifth
                # file-bearing lane's files were simply gone. Worse, the
                # overflow line told the reader to run `helm work list` to
                # recover them — and that surface renders NEITHER the UNKNOWN
                # class NOR filenames. A pointer to a surface that cannot
                # answer is not a disclosure, it is a second silence with a
                # sentence in front of it.
                #
                # So the cap moves off the LANE and onto the FILE LIST. Every
                # live lane is named, always — the name is the whole signal a
                # claimer needs to recognise their own subject under someone
                # else's label, and it costs one line. Only the file evidence
                # is abbreviated, and that abbreviation is counted in place,
                # where the reader can see which lane it belongs to.
                FILES_SHOWN = 4
                for lane, files in held_lane_files(root, exclude=pos[0]):
                    if files is None:
                        # UNKNOWN IS ITS OWN SENTENCE. Rendering it as
                        # "nothing authored yet" would state as fact the one
                        # thing the read failed to establish.
                        print("helm work: live lane %s — authorship UNKNOWN, "
                              "its history could not be read; treat it as "
                              "possibly overlapping yours" % lane,
                              file=sys.stderr)
                    elif not files:
                        print("helm work: live lane %s CLAIMED, nothing "
                              "authored yet — read the NAME: if it is your "
                              "subject under another label, ask before you "
                              "build" % lane, file=sys.stderr)
                    else:
                        shown = ", ".join(files[:FILES_SHOWN])
                        if len(files) > FILES_SHOWN:
                            shown += " (+%d more file(s))" % (
                                len(files) - FILES_SHOWN)
                        print("helm work: live lane %s is in %s"
                              % (lane, shown), file=sys.stderr)
            except Exception:
                pass
            # AND WHETHER THE RAIL AROUND THIS NEW ROOM IS ACTUALLY ARMED.
            # stale_guard_hooks has exactly ONE production caller — `gc`,
            # which prints it above a 45-room listing. So a landed-but-inert
            # guard is discoverable only by someone running a housekeeping
            # verb and reading past the rooms, and MEASURED 2026-08-04 it sat
            # unnoticed long enough that the shared checkout was missing the
            # worktree-birth traversal refusal and the pre-commit in-flight
            # gate outright, across all 45 rooms (worktrees share one hook
            # dir) while nine seats committed through them.
            #
            # CLAIM IS THE MOMENT, not a convenient one: the drifted rules
            # guard worktree BIRTH and COMMITS, and claim is where a worktree
            # is born and a room's commits begin. It cannot live in the
            # pre-commit hook itself, because the stale thing IS that hook —
            # nothing can be trusted to report its own absence.
            #
            # EVERY non-fresh state, unlike the landed-close rung which
            # refuses on STALE alone. The asymmetry is deliberate and is about
            # what being wrong COSTS: that rung blocks a land, so it must be
            # narrow enough that a repo which never opted into helm's rail is
            # never held up by a stranger's opinion of its hooks. This one
            # prints a line to stderr and blocks nothing, so it can afford to
            # say "not armed" for missing and unreadable too — which at CLAIM
            # time is the more useful reading anyway, because the room being
            # born right now gets no birth guard in any of those states.
            try:
                drift = stale_guard_hooks(root)
                if drift:
                    print("helm work: GUARD RAIL NOT ARMED for this room — "
                          "%s. These hooks are SHARED by every worktree, so "
                          "this is the whole checkout, not just your lane. "
                          "Arm them: `helm work install-guard --apply`"
                          % "; ".join("%s is %s" % (n, s.lower())
                                      for s, n, _w in drift),
                          file=sys.stderr)
            except Exception:
                pass
        return rc
    if verb == "release":
        pos = _positional(rest)
        lane = pos[0] if pos else _infer_lane(root)
        if not lane:
            print("usage: helm work release <lane> --lease ID [--park] | "
                  "--stale (lane infers only from inside its room; `helm work "
                  "list` reprints your own lease id; --stale is the "
                  "dead-holder escape — liveness proof, no lease required)",
                  file=sys.stderr)
            return 2
        if "--stale" in rest:
            rc, lines = release_stale_lane(root, lane, seat, session=session)
        else:
            rc, lines = release_lane(root, lane, seat,
                                     lease=seats._flag(rest, "--lease"),
                                     session=session, park="--park" in rest)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if verb == "peek":
        # A positional-taking verb, so no guard_tail (the claim/release
        # precedent). --drop and <committish> are two different requests and
        # carrying both is ambiguous, so it refuses rather than picking.
        from ._peek import peek, peek_drop
        drop = seats._flag(rest, "--drop")
        pos = _positional(rest)
        if drop is not None and pos:
            print("usage: helm work peek <committish> [--json] | "
                  "helm work peek --drop <path-or-committish>  (not both)",
                  file=sys.stderr)
            return 2
        if drop is not None:
            rc, lines = peek_drop(root, drop)
            for ln in lines:
                print(ln, file=sys.stdout if rc == 0 else sys.stderr)
            return rc
        if len(pos) != 1:
            print("usage: helm work peek <committish> [--json]  (a read-only "
                  "detached room at exactly that commit; --drop retires it)",
                  file=sys.stderr)
            return 2
        rc, payload = peek(root, pos[0])
        if rc != 0:
            print(json.dumps(payload) if "--json" in rest else payload["error"],
                  file=sys.stderr)
            return rc
        print(json.dumps(payload) if "--json" in rest
              else "%s\t%s" % (payload["path"], payload["sha"]))
        return 0
    if verb == "gc":
        # `work gc --bogus --apply` must refuse before scan/enact, not run
        # the rescue-commit sweep under an arg that does not exist.
        from ..cli import guard_tail
        rc = guard_tail("helm work gc", rest, flags=("--apply",),
                        valued=("--repo", "--seat"),
                        usage="work gc [--apply] [--repo PATH]")
        if rc is not None:
            return rc
        # FETCH BEFORE THE SCAN, NOT AFTER IT. gc_scan decides landedness
        # against a remote-tracking ref, and --apply deletes branches on that
        # decision — so the refresh has to happen before the proof is computed,
        # not before the deletion. A failed fetch REFUSES the whole apply: an
        # unrefreshed proof must not authorize a deletion.
        enforcing = "--apply" in rest
        if enforcing:
            ok, why = refresh_trunk(root)
            print("helm work gc: %s" % why)
            if not ok:
                print("helm work gc: REFUSING to apply — the trunk ref could "
                      "not be refreshed, so 'landed' is UNPROVEN and this verb "
                      "deletes branches on that proof. Re-run when the remote "
                      "is reachable, or use the dry run.", file=sys.stderr)
                return 1
        # THE SHARED CHECKOUT ITSELF RIDES THE CADENCE (2026-08-03: the
        # fleet measured trunk on a checkout that had silently FORKED from
        # origin/main for ~80 minutes). refresh_trunk just made the
        # remote-tracking ref honest; this converges the local base onto it
        # — ff only, every unconvergeable state named, a fork posted loud.
        sync_lines, sync_post_error = trunk_sync(root, enforcing)
        for line in sync_lines:
            print("helm work gc: %s" % line)
        if sync_post_error:
            print("helm work gc: %s" % sync_post_error, file=sys.stderr)
        # A LANDED GUARD THAT IS NOT INSTALLED IS INERT, and until now
        # nothing looked. Measured 2026-08-04: this checkout ran hook v3
        # while trunk generated v4, so v4's fixes had never been armed here.
        # Report only — installing rewrites an executable in someone's .git
        # and must never be a side effect of a read pass.
        for _state, _name, _why in stale_guard_hooks(root):
            print("helm work gc: GUARD-%s %s — %s; run `helm work "
                  "install-guard --apply`" % (_state, _name, _why))
        rows = gc_scan(root)

        # REGISTRY RESIDUE RIDES THE SAME PASS. A record whose directory is
        # GONE has no room, so the room scan cannot see it — but git keeps the
        # record indefinitely, `worktree add` refuses its path, and a
        # metaharness that mirrors the registry (Orca's sidebar) renders it as
        # a ghost room forever. The dry run names each one; --apply prunes
        # RECORDS ONLY — never files, never branches, locked records immune.
        def phantom_pass():
            phantoms, excluded, scan_error = phantom_scan(root)
            if scan_error:
                print("helm work gc: worktree registry unavailable: %s" %
                      scan_error, file=sys.stderr)
                return 1, None, None
            if not phantoms:
                return 0, 0, 0
            if not enforcing:
                for p in phantoms:
                    print("  PHANTOM %s — registered but the directory is "
                          "gone (registry residue renders as a ghost room in "
                          "a metaharness sidebar); --apply prunes the record"
                          % _show(p))
                return 0, 0, len(phantoms)
            pruned, error, unknown = prune_phantom_records(
                root, phantoms, excluded=excluded)
            for p in pruned:
                print("  phantom record no longer registered " + _show(p))
            if error:
                suffix = "" if unknown else " — phantom records kept"
                print("helm work gc: %s%s" % (error, suffix), file=sys.stderr)
                return ((1, None, None) if unknown else
                        (1, len(pruned), len(phantoms) - len(pruned)))
            return 0, len(pruned), 0

        # STALE PEEKS RIDE THE SAME PASS (#157, the owner's fifth ask for
        # the same recurrence): peeks are minted per review/gate and nothing
        # retired them, so a metaharness sidebar accreted a ghost project per
        # peek forever. TTL detection lives in _peek.stale_peeks; REMOVAL
        # stays peek_drop's alone — every live-room refusal (cwd occupant,
        # bound pane, dirty tree) fires there and a refusal here is a KEPT
        # room, printed, never an error.
        def peek_pass():
            from ._peek import PEEK_TTL_S, peek_drop, stale_peeks
            dropped = kept = 0
            for path, idle in stale_peeks(root):
                name = os.path.basename(path)
                if not enforcing:
                    print("  PEEK-STALE %s — idle %dh%02dm past its TTL; "
                          "--apply drops it (occupied/pane-bound rooms "
                          "refuse and are kept)"
                          % (name, idle // 3600, idle % 3600 // 60))
                    kept += 1
                    continue
                rc_d, lines = peek_drop(root, path, stale_ttl_s=PEEK_TTL_S)
                for ln in lines:
                    print("        " + ln)
                if rc_d == 0:
                    dropped += 1
                else:
                    kept += 1
            return dropped, kept

        if not rows:
            print("helm work gc: no lane rooms under %s-wt/" % root)
            # PEEKS ARE THEIR OWN POPULATION in the summary, both branches:
            # this branch DISCARDED peek_pass's counts, so a pass that
            # dropped a peek reported removed=0 (codex-2's REWORK on
            # dispatch 766a5bf761f0, first repro).
            peek_dropped, peek_kept = peek_pass()
            phantom_rc, phantom_removed, phantom_kept = phantom_pass()
            if enforcing and phantom_removed is not None:
                line = format_gc_summary(
                    root, peek_dropped + phantom_removed,
                    peek_kept + phantom_kept, phantom_kept)
                print(line)
                error = post_gc_summary(line)
                if error:
                    print(error, file=sys.stderr)
                    return 1
            return phantom_rc
        print("helm work gc — %d room%s under %s-wt/ (%s)" % (
            len(rows), "s"[:len(rows) != 1], root,
            "APPLYING" if enforcing else "dry-run; --apply enforces — only "
            "clean LANDED rooms retire; dirty/unlanded work stays for triage, "
            "never discarded"))
        w = max(len(r["lane"]) for r in rows)
        removed = 0
        # PLANNED-VS-DONE, because "removed=0" is literally true and reads as
        # "nothing needed removing". Measured 2026-08-04: a run PLANNED two
        # removals, completed ZERO (both blocked on a bound pane), and said
        # `removed=0 kept=40` — a summary that cannot distinguish a clean estate
        # from a jammed one, and real time was burned believing the first.
        planned, blocked_why = 0, []
        for r in rows:
            print("  %-7s %-*s  %s" % (r["verdict"].upper(), w, r["lane"], r["why"]))
            if enforcing:
                planned += r["verdict"] == "remove"
                out = gc_enact(root, r)
                for ln in out:
                    print("        " + ln)
                gone = not os.path.exists(r["path"])
                removed += gone
                if r["verdict"] == "remove" and not gone:
                    blocked_why.append(_blocked_reason(out))
        # `removed` stays a LANE quantity: folding peek drops into it made
        # `len(rows) - removed` subtract peeks from the LANE kept count —
        # two dropped peeks beside one kept lane printed kept=-1 (codex-2's
        # REWORK, dispatch 766a5bf761f0, second repro). Peeks join the summary as
        # their own population instead.
        peek_dropped, peek_kept = peek_pass()
        phantom_rc, phantom_removed, phantom_kept = phantom_pass()
        if enforcing and phantom_removed is not None:
            triage = (sum(r["verdict"] in ("triage", "rescue") for r in rows)
                      + phantom_kept)
            line = format_gc_summary(
                root, removed + peek_dropped + phantom_removed,
                len(rows) - removed + peek_kept + phantom_kept, triage,
                planned=planned, blocked=blocked_why)
            print(line)
            error = post_gc_summary(line)
            if error:
                print(error, file=sys.stderr)
                return 1
        return phantom_rc
    if verb == "list":
        from ..cli import guard_tail
        rc = guard_tail("helm work list", rest, valued=("--repo", "--seat"),
                        usage="work list [--repo PATH]")
        if rc is not None:
            return rc
        from ._peek import peek_rows
        registered, registry_error = _worktree_records(root)
        rows = list_rows(root, registered=registered)
        peeks = peek_rows(root, registered=registered)
        loose, discovery_errors = unguarded_inventory(
            root, registered=registered, registry_error=registry_error)
        if not rows and not peeks and not loose and not discovery_errors:
            print("helm work: no lane rooms — `helm work claim <lane>` opens "
                  "one at %s-wt/<lane>" % root)
            return 0
        if rows:
            # Separate stale rows so the fleet sees dead holders without scanning
            stale = [r for r in rows if r.get("stale")]
            live_rows = [r for r in rows if not r.get("stale")]
            if live_rows:
                print("GUARDED lane rooms — claims and leases apply:")
                w = max(len(r["lane"]) for r in rows)
                # THE DURATION SAYS WHICH DIRECTION IT RUNS. It rendered as
                # a bare "<seat> 13378s", which reads equally well as "held
                # for" and as "left", and the field behind it is literally
                # named `remaining`. Two seats misread it in the same hour on
                # 2026-08-05 — one nearly raised a false alarm on their own
                # lane — which makes it a surface defect rather than
                # carelessness. Line 554 of this same file already writes
                # "%ds ago" for the other direction, so the convention
                # existed and this column simply missed it.
                holds = [("%s %ds left" % (r["holder"], r["remaining"])
                          if r["holder"] else "-") for r in live_rows]
                # width DERIVED from the data, like the lane column above:
                # padding a longer seat name into a fixed 24 silently ragged
                # the rows that most needed reading.
                hw = max([len(h) for h in holds] + [len("holder")])
                for r, hold in zip(live_rows, holds):
                    # the release token, for THIS seat's own rows only — the board
                    # that lists the obligation now also hands back what closing
                    # it requires (a holder who lost the token to compaction used
                    # to have no route but reading .claims.json by hand)
                    mine = ("  lease=%s (yours)" % r["lease"]) if r.get("lease") else ""
                    print("  GUARDED %-*s  %-*s  %-9s  +%s/-%s%s  path=%s%s" % (
                        w, r["lane"], hw, hold, _tree_word(r),
                        r["ahead"], r["behind"], "  locked" if r["locked"] else "",
                        ascii(r["path"]), mine))
                    if r.get("dangling_conflict"):
                        print(_DANGLING_NOTE % (r["conflicts"], ascii(r["path"])))
            if stale:
                print("STALE claims — holder is provably dead; release with:")
                for r in stale:
                    print("  helm work release %s --stale  (was %s)"
                          % (r["lane"], r["holder"]))
            # THE DISCOVERY PROBLEM, third instance of the day's one cure
            # (#289): a lease expires, the room and its committed delta do
            # not — and nothing LISTED a holderless room with work in it, so
            # the risk is not loss (a branch survives forever) but a seat
            # SILENTLY REDOING it. Two seats claimed the same gc-triage fix
            # under different names on 2026-08-05; the lease dedupes a NAME,
            # never the WORK. Empty-section law: silent unless real.
            def _delta(r):
                try:
                    return int(r.get("ahead") or 0)
                except (TypeError, ValueError):
                    return 0
            holderless = [r for r in live_rows
                          if not r["holder"] and _delta(r) > 0]
            if holderless:
                print("HOLDERLESS WITH DELTA — nobody holds these and their "
                      "branches carry commits; check for your subject here "
                      "BEFORE claiming a new lane:")
                for r in holderless:
                    print("  %-*s  +%s/-%s  resume: helm work claim %s" % (
                        w, r["lane"], r["ahead"], r["behind"], r["lane"]))
            # THE QUESTION THE LEASE COLUMN CANNOT ANSWER. Every row above can
            # be a valid, uncontested lease and two of them can still be aimed
            # at one defect — which happened three times on 2026-07-30, caught
            # each time by a human reading chat and never by this board. Empty
            # section stays silent (the empty-section law); it only speaks when
            # two open lanes have actually touched the same file.
            try:
                overlaps = lane_overlaps(root, registered=registered)
            except Exception:      # noqa: BLE001 — the board never dies on it
                overlaps = []
            if overlaps:
                print("OVERLAPPING LANES — a lease guards a ROOM, not a DEFECT; "
                      "these touch the same files:")
                for a, b, shared in overlaps:
                    print("  %s x %s — %s%s" % (
                        a, b, ", ".join(shared[:4]),
                        " +%d more" % (len(shared) - 4) if len(shared) > 4 else ""))
                print("  file overlap is a PROXY for 'same defect', not proof — "
                      "check with the other holder before building")
        if peeks:
            # its OWN section, not extra GUARDED rows: a peek has no lane,
            # holder, or lease, and printing it in that table would invite
            # exactly the claim/release verbs that must never aim here
            print("PEEK rooms — read-only, disposable "
                  "(`helm work peek --drop <path>` retires):")
            for r in peeks:
                print("  PEEK %s  %s  path=%s%s" % (
                    r["name"],
                    "DETACHED" if not r["branch"]
                    else "!! on branch %s (no longer a clean peek)" % r["branch"],
                    ascii(r["path"]), "  locked" if r["locked"] else ""))
        for error in discovery_errors:
            print("WARNING: UNGUARDED discovery UNKNOWN — %s; retain and inspect "
                  "manually." % ascii(error))
        if loose:
            print("WARNING: %d UNGUARDED Agent/Workflow room%s — visibility and "
                  "advisory evidence only. No claim/lease protects these rooms, "
                  "and this output never authorizes cleanup:" %
                  (len(loose), "s"[:len(loose) != 1]))
            for r in loose:
                if r["occupants"]:
                    state = "OCCUPIED"
                    evidence = "cwd pid(s) " + ",".join(r["occupants"])
                elif r["harness"] == "live":
                    state, evidence = "LIVE-HARNESS", r["harness_note"]
                elif r["hard_unknown"]:
                    state, evidence = "UNKNOWN", r["unknown"]
                elif r["wrote_ago"] is not None \
                        and r["wrote_ago"] <= RECENT_WRITE_SECONDS:
                    state = "RECENT-WRITE"
                    evidence = "%ds ago%s; advisory, not ownership proof" % (
                        r["wrote_ago"], " (future mtime/clock skew)"
                        if r["clock_skew"] else "")
                elif r["dirty"]:
                    state = "DIRTY"
                    evidence = "uncommitted work; last timestamp %s" % (
                        "%dm ago" % (r["wrote_ago"] // 60)
                        if r["wrote_ago"] is not None else "unknown")
                elif r["unknown"]:
                    state, evidence = "UNKNOWN", r["unknown"]
                else:
                    state = "CLEAN"
                    evidence = ("no cwd or live harness evidence; this is not "
                                "proof that no writer will resume")
                print("  UNGUARDED %-16s id=%-30s tree=%-8s lock=%-8s "
                      "branch=%s path=%s evidence=%s" % (
                          state, r["id"], "DIRTY" if r["dirty"] else "CLEAN",
                          "LOCKED" if r["locked"] else "UNLOCKED",
                          ascii(r["branch"] or "(detached)"), ascii(r["path"]),
                          ascii(evidence)))
        return 0
    if verb == "install-guard":
        from ..cli import guard_tail
        grc = guard_tail("helm work install-guard", rest, flags=("--apply",),
                         valued=("--repo", "--seat"),
                         usage="work install-guard [--apply] [--repo PATH]")
        if grc is not None:
            return grc
        rc, lines = install_guard(root, apply="--apply" in rest)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if verb == "stash":
        from ._stash import act, list_lines
        sub = rest[0] if rest and not rest[0].startswith("--") else "list"
        if sub == "list":
            print("\n".join(list_lines(root)))
            return 0
        needle = " ".join(x for x in rest[1:] if not x.startswith("--"))
        lines, err = act(root, sub, needle)
        if err:
            print("helm work stash: " + err, file=sys.stderr)
            return 2
        print("\n".join(x for x in lines if x))
        return 0
    print(USAGE, file=sys.stderr)
    return 2
