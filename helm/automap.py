#!/usr/bin/env python3
"""The auto-map: raw harness observations -> the real project list.

Pipeline (each step kills a class of noise the raw stores carry):
  1. FILTER   drop scratch/isolation cwds (/tmp and friends) — on this corpus
              that alone removes ~94% of claude project slugs (judge-iso dirs).
  2. COLLAPSE canonicalize each cwd to its project root: git worktrees fold
              into their main repo (via --git-common-dir), dead paths fold by
              worktree-pattern stripping + nearest existing git ancestor.
  3. GROUP    merge observations per canonical root, union harnesses/days.
  4. PROMOTE  a candidate becomes helm-known when it is a real git repo OR
              recurs (>= PROMOTE_SESSIONS sessions or >= PROMOTE_DAYS distinct
              active days). One-off non-repo dirs never register.
  5. RANK     newest activity first; dormant (> DORMANT_DAYS) greys out but is
              never dropped — promotion is idempotent and additive.

Identity law (from the session-format research): sessionId is the key and cwd
is an attribute — the slug dir is a jail we decode out of, never trust.
"""
import os
import re
import time

from . import harnesses, vcs

# cwd prefixes that are scratch/isolation, never projects.
NOISE_PREFIXES = ("/tmp/", "/var/tmp/", "/dev/shm/", "/run/")

# worktree dir-name patterns: a path segment matching one of these means
# "everything from here on is a worktree checkout of the project before it".
_WORKTREE_SEG = re.compile(r"^(worktrees?|wt|\.worktrees)$")
_WORKTREE_SUFFIX = re.compile(r"^(?P<proj>.+?)-(worktrees?|wt)$")

PROMOTE_SESSIONS = 3
PROMOTE_DAYS = 2
DORMANT_DAYS = 21


def _is_noise(cwd):
    if not cwd.startswith("/"):
        return True
    tmp = os.environ.get("TMPDIR")
    if tmp and cwd.startswith(tmp.rstrip("/") + "/"):
        return True
    return any(cwd == p.rstrip("/") or cwd.startswith(p) for p in NOISE_PREFIXES)


def _git_root(path):
    """The MAIN repo root for a path inside a repo or linked worktree, else None.
    --git-common-dir points at the main repo's .git even from a worktree.

    Through the VCS seam (helm/vcs.py `common_dir`). Selection cannot cycle back
    through here: `vcs.detect` reads `.jj`/`.git` markers off the filesystem and
    calls nothing in this module."""
    common = vcs.backend(path).common_dir(path)
    if common and os.path.basename(common) == ".git":
        return os.path.dirname(common)
    return None  # unreadable, bare, or odd layout — not a project checkout


def _strip_worktree(path):
    """Fold worktree-shaped paths to the project they check out:
    .../<proj>/worktrees/<x>/...  -> .../<proj>
    .../<proj>-worktrees/<x>/...  -> .../<proj>   (sibling-dir convention)"""
    parts = path.split("/")
    for i, seg in enumerate(parts):
        if _WORKTREE_SEG.match(seg) and i > 1:
            return "/".join(parts[:i])
        m = _WORKTREE_SUFFIX.match(seg)
        if m and i > 0:
            return "/".join(parts[:i] + [m.group("proj")])
    return path


def canonicalize(cwd, home=None):
    """cwd -> canonical project root, or None if it cannot anchor to one.
    The user's home dir itself is never a project (it hosts the global store)."""
    home = home or os.path.expanduser("~")
    cwd = os.path.normpath(cwd)
    if cwd in ("/", home):
        return None
    if os.path.isdir(cwd):
        root = _git_root(cwd)
        if root:
            root = _strip_worktree(root)
            return None if root in ("/", home) else root
        stripped = _strip_worktree(cwd)
        if stripped != cwd and os.path.isdir(stripped):
            return _git_root(stripped) or stripped
        return cwd  # existing non-repo dir: a candidate, promotion will gate it
    # dead path: strip worktree shapes, then anchor to nearest existing ancestor
    p = _strip_worktree(cwd)
    while p not in ("/", home) and not os.path.isdir(p):
        p = os.path.dirname(p)
    if p in ("/", home):
        return None
    return _git_root(p) or None  # a dead non-repo path has nothing to anchor to


def _name_for(root, taken, home):
    """basename, qualified by parent dirs only on collision. Deterministic."""
    rel = os.path.relpath(root, home) if root.startswith(home + "/") else root.lstrip("/")
    parts = rel.split("/")
    for depth in range(1, len(parts) + 1):
        name = "-".join(parts[-depth:])
        if taken.get(name) in (None, root):
            return name
    return re.sub(r"[^A-Za-z0-9_.-]", "-", rel)


# Repo-scan (the second discovery tier): every git repo under these roots is
# helm-known as a SHELF project — on disk, no observed agent activity. Shelf
# nodes serve lineage + the archive report; they never scaffold a home and the
# default project list hides them (signal first).
def _scan_roots():
    """The repo-scan roots: HELM_SCAN_ROOTS env (colon-separated), else `~/dev`.
    No org-specific path ships — the two-tier walk finds both `~/dev/<repo>` and
    `~/dev/<org>/<repo>`, so a bare `~/dev` covers an org checkout without naming
    it. Matches HELM_CONFIG_ROOTS' convention (both default to `~/dev`)."""
    raw = os.environ.get("HELM_SCAN_ROOTS")
    roots = raw.split(":") if raw else [os.path.join(os.path.expanduser("~"), "dev")]
    return [os.path.expanduser(r) for r in roots if r]


_SHELF_SKIP = re.compile(r"(worktrees?$|-wt$|RETIRED|^references$|^archive$|^\.)")


def scan_repos(roots=None):
    """{path: name} of git repos under the scan roots — immediate children
    AND one level deeper (so both `<root>/<repo>` and `<root>/<org>/<repo>`
    are found). Deeper nesting is not walked; the project registry can still
    register those repos explicitly."""
    out = {}
    for root in roots if roots is not None else _scan_roots():
        if not os.path.isdir(root):
            continue
        for n in sorted(os.listdir(root)):
            if _SHELF_SKIP.search(n):
                continue
            p = os.path.join(root, n)
            if os.path.isdir(os.path.join(p, ".git")):
                out[p] = n
            elif n not in (".", "..") and not n.startswith(".") \
                    and os.path.isdir(p):
                # depth-2: <root>/<org>/<repo>
                for n2 in sorted(os.listdir(p)):
                    if _SHELF_SKIP.search(n2):
                        continue
                    p2 = os.path.join(p, n2)
                    if os.path.isdir(os.path.join(p2, ".git")):
                        out[p2] = n2
    return out


def build_map(observations=None, home=None, now=None):
    """observations -> {name: project-record}. Pure derivation, no writes."""
    home = home or os.path.expanduser("~")
    now = now or time.time()
    obs = observations if observations is not None else harnesses.all_observations()

    roots = {}
    canon_cache = {}
    for o in obs:
        cwd = o["cwd"]
        if _is_noise(cwd):
            continue
        if cwd not in canon_cache:
            canon_cache[cwd] = canonicalize(cwd, home)
        root = canon_cache[cwd]
        if not root:
            continue
        r = roots.setdefault(root, {"path": root, "harnesses": {}, "days": set(),
                                    "last_seen": 0.0, "cwds": set()})
        h = r["harnesses"].setdefault(o["harness"], {"sessions": 0, "last_seen": 0.0, "refs": []})
        h["sessions"] += o["sessions"]
        h["last_seen"] = max(h["last_seen"], o["last_seen"])
        h["refs"] = sorted(set(h["refs"]) | set(o["refs"]))
        r["days"] |= o["days"]
        r["last_seen"] = max(r["last_seen"], o["last_seen"])
        r["cwds"].add(cwd)

    taken = {}
    projects = {}
    for root in sorted(roots):
        r = roots[root]
        is_git = os.path.isdir(os.path.join(root, ".git")) or _git_root(root) == root
        sessions = sum(h["sessions"] for h in r["harnesses"].values())
        if not (is_git or sessions >= PROMOTE_SESSIONS or len(r["days"]) >= PROMOTE_DAYS):
            continue
        name = _name_for(root, taken, home)
        taken[name] = root
        age_days = (now - r["last_seen"]) / 86400.0 if r["last_seen"] else None
        projects[name] = {
            "name": name,
            "path": root,
            "kind": "git" if is_git else "dir",
            "status": "dormant" if (age_days is None or age_days > DORMANT_DAYS) else "active",
            "last_seen": r["last_seen"],
            "active_days": len(r["days"]),
            "sessions": {h: v["sessions"] for h, v in r["harnesses"].items()},
            "harness_refs": {h: v["refs"] for h, v in r["harnesses"].items()},
            "cwds": sorted(r["cwds"]),
        }
    return projects
