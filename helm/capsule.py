#!/usr/bin/env python3
"""helm capsule — a session plus the git era its work ended on.

RESOLVE -> MATERIALIZE: find the commit the session's repo was on at its last
activity, and print the exact commands to stand that era up in a worktree and
resume the session inside it. Pure composition (catalog row x git history x
rehome x resume); prints commands, mutates nothing.
"""
import os
import sys
from datetime import datetime

from . import transcripts, vcs


def cmd_capsule(args):
    """capsule <sid-prefix> — the session's era sha + worktree/resume commands."""
    if not args:
        print("usage: helm capsule <session-id-prefix>", file=sys.stderr)
        return 2
    row, err = transcripts._resolve_sid(args[0])
    if err:
        print("helm capsule: " + err["error"], file=sys.stderr)
        return 1
    sid, cwd = row["i"], row.get("cwd") or ""
    id8 = sid[:8]
    print("helm capsule %s (%s): %s" % (id8, row["h"], (row.get("t") or "")[:70]))
    if not (cwd and os.path.isdir(os.path.join(cwd, ".git"))):
        print("  session cwd is not a git repo (or vanished) — no era sha")
        print("  resume:  helm sessions resume " + id8)
        return 0
    when = datetime.fromtimestamp(row["mt"]).strftime("%Y-%m-%d %H:%M:%S") if row.get("mt") else None
    ref = row.get("b") if row.get("b") and row.get("b") != "HEAD" else "HEAD"
    sha = vcs.backend(cwd).head_sha(cwd, ref=ref, before=when)
    if not sha:
        print("  no commit on %s before the session's last activity" % ref)
        print("  resume:  helm sessions resume " + id8)
        return 0
    wt = "%s-capsules/%s-%s" % (cwd.rstrip("/"), id8, sha[:8])
    print("  era: %s on %s (%s)" % (sha[:12], ref, when or "undated"))
    print("  materialize:  git -C %s worktree add %s %s" % (cwd, wt, sha))
    print("  resume in place (safest):   helm sessions resume " + id8)
    print("  resume inside the era:      helm rehome %s %s && helm sessions resume %s"
          % (id8, wt, id8))
    return 0
