"""Age a worktree room by git's own clock, for the sweep arms that need an
ABANDONED room rather than a fresh one.

A sweep keeps a room nobody has committed in until git's reflog says nobody
has moved it for the grace (`helm/work/_gc.py` `_UNSTARTED_GRACE_S`). An arm
that means "an abandoned claim" says so by back-dating the lines that clock
reads: the room's own HEAD log and its branch's log. Only the timestamp field
is rewritten, never a sha, an identity or a message.
"""
import os
import subprocess

# FAR PAST ANY GRACE A SWEEP GIVES AN UNSTARTED ROOM. An aged room here is one
# whose clock reads this long ago, so the only thing that can still keep it is
# the rule under test, never its youth. The arms that use it assert it outruns
# the real constant, so a widened grace cannot quietly swallow the fixture.
AGED_S = 30 * 86400


def _git(path, *args):
    r = subprocess.run(("git",) + args, cwd=path, capture_output=True,
                       text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError("git %s in %s: %s"
                             % (" ".join(args), path, r.stderr))
    return r.stdout.strip()


def reflogs(path):
    """[room HEAD log, branch log] — the two files the room clock reads."""
    admin = _git(path, "rev-parse", "--absolute-git-dir")
    common = _git(path, "rev-parse", "--path-format=absolute",
                  "--git-common-dir")
    branch = _git(path, "symbolic-ref", "--short", "HEAD")
    return [os.path.join(admin, "logs", "HEAD"),
            os.path.join(common, "logs", "refs", "heads", *branch.split("/"))]


def age_room(path, by=AGED_S):
    """Back-date every line of the room's two reflogs by `by` seconds."""
    aged = 0
    for log in reflogs(path):
        with open(log) as f:
            lines = f.read().splitlines()
        out = []
        for line in lines:
            head, tab, msg = line.partition("\t")
            ident, ts, tz = head.rsplit(" ", 2)
            out.append("%s %d %s%s%s" % (ident, int(ts) - by, tz, tab, msg))
            aged += 1
        with open(log, "w") as f:
            f.write("\n".join(out) + "\n")
    if not aged:
        raise AssertionError("no reflog line to age in %s" % path)
