"""helm work — the cli cluster: the `helm work` verb dispatcher.
Moved verbatim from the pre-split helm/work.py.
"""
import sys

from .. import seats
from ._common import DEFAULT_TTL, LANE_RE, RECENT_WRITE_SECONDS
from ._lanes import _worktree_records, find_root, unguarded_inventory
from ._gc import gc_enact, gc_scan, list_rows
from ._claims import _infer_lane, _positional, claim, release_lane
from ._guard import install_guard


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = """usage: helm work <verb> [--repo PATH] [--seat S]
  claim <lane> [--ttl N] [--lease ID]   check in: lease + private room —
                                        prints path<TAB>branch<TAB>lease<TAB>ttl
  release [<lane>] --lease ID [--park]  check out: dirty refuses (--park
                                        WIP-commits), key surrendered, room removed
  gc [--apply]                          housekeeping: keep/remove/rescue table
                                        (dry-run default; rescue never discards)
  list                                  the room board: worktree registry x claims
  install-guard [--apply]               composed deterministic git guards (dry/apply)"""


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
        ttl = seats._flag(rest, "--ttl")
        rc, line = claim(root, pos[0], seat,
                         ttl=int(ttl) if ttl else DEFAULT_TTL,
                         lease=seats._flag(rest, "--lease"), session=session)
        print(line, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if verb == "release":
        pos = _positional(rest)
        lane = pos[0] if pos else _infer_lane(root)
        if not lane:
            print("usage: helm work release <lane> --lease ID [--park] "
                  "(lane infers only from inside its room)", file=sys.stderr)
            return 2
        rc, lines = release_lane(root, lane, seat,
                                 lease=seats._flag(rest, "--lease"),
                                 session=session, park="--park" in rest)
        for ln in lines:
            print(ln, file=sys.stdout if rc == 0 else sys.stderr)
        return rc
    if verb == "gc":
        # `work gc --bogus --apply` must refuse before scan/enact, not run
        # the rescue-commit sweep under an arg that does not exist.
        from ..cli import guard_tail
        rc = guard_tail("helm work gc", rest, flags=("--apply",),
                        valued=("--repo", "--seat"),
                        usage="work gc [--apply] [--repo PATH]")
        if rc is not None:
            return rc
        rows = gc_scan(root)
        if not rows:
            print("helm work gc: no lane rooms under %s-wt/" % root)
            return 0
        enforcing = "--apply" in rest
        print("helm work gc — %d room%s under %s-wt/ (%s)" % (
            len(rows), "s"[:len(rows) != 1], root,
            "APPLYING" if enforcing else "dry-run; --apply enforces — dirty "
            "rooms are rescue-committed to their branch, never discarded"))
        w = max(len(r["lane"]) for r in rows)
        for r in rows:
            print("  %-7s %-*s  %s" % (r["verdict"].upper(), w, r["lane"], r["why"]))
            if enforcing:
                for ln in gc_enact(root, r):
                    print("        " + ln)
        return 0
    if verb == "list":
        from ..cli import guard_tail
        rc = guard_tail("helm work list", rest, valued=("--repo", "--seat"),
                        usage="work list [--repo PATH]")
        if rc is not None:
            return rc
        registered, registry_error = _worktree_records(root)
        rows = list_rows(root, registered=registered)
        loose, discovery_errors = unguarded_inventory(
            root, registered=registered, registry_error=registry_error)
        if not rows and not loose and not discovery_errors:
            print("helm work: no lane rooms — `helm work claim <lane>` opens "
                  "one at %s-wt/<lane>" % root)
            return 0
        if rows:
            print("GUARDED lane rooms — claims and leases apply:")
            w = max(len(r["lane"]) for r in rows)
            for r in rows:
                hold = ("%s %ds" % (r["holder"], r["remaining"])
                        if r["holder"] else "-")
                print("  GUARDED %-*s  %-24s  %-5s  +%s/-%s%s  path=%s" % (
                    w, r["lane"], hold, "dirty" if r["dirty"] else "clean",
                    r["ahead"], r["behind"], "  locked" if r["locked"] else "",
                    ascii(r["path"])))
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
    print(USAGE, file=sys.stderr)
    return 2
