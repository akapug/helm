"""helm work — the stash cluster: address a stash by its MESSAGE, never by
its position.

WHY THIS EXISTS (live incident, 2026-07-24 — shared-tree incident #5):
`stash@{0}` is a POSITION in a stack, not an identifier for a thing. Dropping
any entry shifts every later one down, so a reference that named your own work
one minute names somebody else's the next. That is exactly what happened: a
redundant council WIP was verified and dropped at ~21:00 — correct on its own
terms — and from that moment `stash@{0}` resolved to codex-3's cv-autocompact
entry instead. Whatever pops `stash@{0}` had been re-applying that stash into
MAIN ever since, producing repeated dangling conflict states with no MERGE_HEAD
(so neither --continue nor --abort worked) that the integrator had to unpick by
hand more than once.

The stack is shared mutable state indexed by ordinal. Nobody who wrote
`stash@{0}` was careless; the reference was simply never stable enough to mean
what it looked like it meant.

WHAT THIS CAN AND CANNOT DO — stated plainly, because a guard that cannot fire
is worse than no guard (it buys false confidence). Git exposes NO pre-stash
hook: there is no supported point at which `git stash pop stash@{0}` can be
intercepted the way the reference-transaction hook intercepts ref updates. So
this is THE SAFE PATH, not a barrier around the unsafe one:

  * every verb here REFUSES an `stash@{N}` argument outright and says why;
  * a stash is named by a substring of its MESSAGE, which does not move;
  * an ambiguous message refuses rather than guessing — two entries matching
    is precisely the situation where picking one silently is the bug;
  * the match is RE-RESOLVED under the caller's own eyes immediately before
    acting, because the stack can shift between the moment you list it and the
    moment you act on it, and the resolved message is echoed so the operator
    sees which entry they actually hit.

Composes helm's existing doctrine that `git stash` on a shared checkout is not
swarm-safe at all (harness.py, seat.py): the durable answer is a private room
per lane via `helm work claim <lane>`. This verb is for the stashes that
already exist and still have to be handled safely.
"""
import re

from ._lanes import _git

# `stash@{0}`, `stash@{ 12 }`, and the bare `@{0}` git also accepts
_POSITIONAL = re.compile(r"(?:stash)?@\{\s*\d+\s*\}")


def _entries(root):
    """[(ref, message)] newest-first, exactly as git orders the stack."""
    rc, out, _err = _git(root, "stash", "list", "--format=%gd\t%gs")
    if rc != 0:
        return None
    rows = []
    for line in (out or "").splitlines():
        if "\t" in line:
            ref, msg = line.split("\t", 1)
            rows.append((ref.strip(), msg.strip()))
    return rows


def resolve(root, needle):
    """(ref, message, err) — the ONE resolution path.

    Refuses a positional reference, refuses an ambiguous message, refuses a
    miss. Returns a ref that was true at the instant it was read; callers act
    immediately and echo the message so the operator can see what was hit.
    """
    needle = str(needle or "").strip()
    if not needle:
        return None, None, ("name the stash by a distinctive part of its "
                            "MESSAGE (helm work stash list)")
    if _POSITIONAL.search(needle):
        return None, None, (
            "REFUSED: '%s' is a POSITION in the stash stack, not an identifier "
            "for a stash. Dropping any entry shifts every later one down, so "
            "this reference names a different stash the moment anyone else "
            "drops one — it is how a redundant WIP drop silently redirected a "
            "pop into MAIN (2026-07-24). Name it by MESSAGE instead: "
            "helm work stash list" % needle)
    rows = _entries(root)
    if rows is None:
        return None, None, "cannot read the stash list — treat this as UNKNOWN"
    hits = [(ref, msg) for ref, msg in rows if needle.lower() in msg.lower()]
    if not hits:
        return None, None, ("no stash message contains %r (helm work stash "
                            "list)" % needle)
    if len(hits) > 1:
        listing = "\n".join("    %s" % m for _r, m in hits)
        return None, None, ("AMBIGUOUS: %d stashes match %r — refusing to "
                            "guess which one you meant:\n%s" %
                            (len(hits), needle, listing))
    ref, msg = hits[0]
    return ref, msg, None


def list_lines(root):
    rows = _entries(root)
    if rows is None:
        return ["cannot read the stash list — UNKNOWN, not empty"]
    if not rows:
        return ["no stashes"]
    out = ["%d stash(es) — address these by MESSAGE, never by position:" % len(rows)]
    for _ref, msg in rows:
        out.append("  %s" % msg)
    return out


def act(root, action, needle):
    """(lines, err) — apply/pop/drop/show the stash whose MESSAGE matches.

    The ref is resolved and used in the same breath: any gap between resolving
    a position and acting on it is the whole bug this module exists to close.
    """
    if action not in ("apply", "pop", "drop", "show"):
        return None, "unknown stash action %r" % action
    ref, msg, err = resolve(root, needle)
    if err:
        return None, err
    args = ["stash", action]
    if action == "show":
        args.append("-p")
    args.append(ref)
    rc, out, gerr = _git(root, *args)
    if rc != 0:
        return None, ("git stash %s failed on %s (%s): %s"
                      % (action, ref, msg, (gerr or out or "").strip()))
    return ["%s %s" % (action, msg), (out or "").rstrip()], None
