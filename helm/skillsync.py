#!/usr/bin/env python3
"""helm skills sync — universal skill distribution: ONE canonical skills
source, every claude-code config dir a symlink to it.

The stranding class this kills: per-skill symlink farms are point-in-time
copies of the canonical SET — a skill added later (or dropped into one home by
hand) exists only where it landed. Proven live: a personal skill sat in one
credhome, invisible to the owner's other session, because only some homes were
whole-dir symlinks to the shared hub.

Canonical = a shared skills hub (default `~/.helm/skills-canonical`;
HELM_SKILLS_CANONICAL overrides — e.g. to point at an external physics-owner's
hub whose entries are per-skill symlinks into that source repo, so upstream
skill edits propagate via git with no re-sync). It lives outside every
credhome (survives archive/recreate), and a per-home second copy would just
mint a two-writer drift problem. Lean-configs: canonical holds the one
approved set; a home symlinked to it CANNOT grow private bloat.

Sync is two phases, dry-run by default (the sweep/drain/gc law):
  1. MERGE — union every stray (an entry in a REAL skills dir that canonical
     lacks, or that outnews canonical's copy: newest-wins by content mtime)
     INTO canonical. Real dirs are copied, symlinks re-minted at their
     resolved target. Displaced canonical entries are backed up first.
  2. WIRE — repoint each config dir's skills/ -> canonical. A real dir is
     MOVED whole into the backup root (the backup IS the original — inodes,
     never a lossy copy), then the symlink lands tmp+rename (atomic). An
     already-direct link is a no-op (idempotent); a link elsewhere — even a
     chain that resolves to canonical through an alias — is normalized to a
     direct link (chains die when the intermediate home is archived).
     Post-swap superset check per home: every skill name visible before must
     be visible after, or the move rolls back. NEVER lose a skill.

Config dirs covered: every real credhome under ~/.claude-homes (alias
symlinks fold onto their target — admin and admin-example-com are
one home), the default ~/.claude, and every seat CLAUDE_CONFIG_DIR including
per-instance dirs (smoke-claude excluded: recreated per smoke run). Seat mint
(seat._link_skills) calls wire() at birth, so a NEW seat is born canonical;
`helm skills sync` re-run wires any newly-created credhome — one command,
safe forever.
"""
import glob
import os
import shutil
import sys
import time

from . import home as _home

# The default hub (see module docstring; HELM_SKILLS_CANONICAL overrides).
DEFAULT_CANONICAL = os.path.join(
    os.path.expanduser("~"), ".helm", "skills-canonical")

BACKUP_ROOT = os.path.join(os.path.expanduser("~"), ".skills-premerge-backup")


def canonical():
    """The canonical skills source: HELM_SKILLS_CANONICAL env else the default
    hub. Deliberately NOT HELM_SKILL_DECK (home_create's content-source knob):
    a deck may point at a git-tracked skills dir — merging strays (personal/
    local skills) into a tracked as-public repo dir is exactly the leak sync
    must never make. A well-formed hub is a gitignored dir whose entries can
    symlink INTO a source repo: repo skills stay repo-owned, local strays stay
    local, one distribution point."""
    c = _home.env("SKILLS_CANONICAL")
    if c:
        return os.path.realpath(os.path.expanduser(c))
    # Realpath the default too: a hub may itself be a symlink to a physical
    # store, and mint (canonical()) vs sync (which realpaths) must agree on the
    # path STRING or a wired home relinks off the raw path on the next sync.
    return os.path.realpath(DEFAULT_CANONICAL)


def config_dirs(claude_root=None, default_claude=None, seats_root=None):
    """[(label, config_dir)] — every claude-code config dir on this host:
    real credhomes (deduped on realpath; alias symlinks fold onto their
    target), the default ~/.claude, seat + seat-instance CLAUDE_CONFIG_DIRs.
    Parameterized so tests run against a fake estate."""
    from . import homes as credhomes
    claude_root = claude_root or credhomes.ROOTS["claude"]
    default_claude = default_claude or credhomes.DEFAULTS["claude"]
    seats_root = seats_root or os.path.join(_home.global_dir(), "seats")
    out, seen = [], set()
    for p in sorted(glob.glob(os.path.join(claude_root, "*"))):
        real = os.path.realpath(p)
        if os.path.islink(p) or not os.path.isdir(real) or real in seen:
            continue
        seen.add(real)
        out.append((os.path.basename(p), real))
    d = os.path.realpath(default_claude)
    if os.path.isdir(d) and d not in seen:
        seen.add(d)
        out.append(("default-claude", d))
    for pat, tag in (("*/claude", "seat:%s"),
                     ("*/instances/*/claude", "seat:%s")):
        for c in sorted(glob.glob(os.path.join(seats_root, pat))):
            real = os.path.realpath(c)
            if not os.path.isdir(real) or real in seen:
                continue
            seen.add(real)
            rel = os.path.relpath(os.path.dirname(c), seats_root)
            out.append((tag % rel.replace(os.sep + "instances", ""), real))
    return out


def _newest(path):
    """Newest content mtime under path (symlinks followed) — the newest-wins
    clock for merge conflicts."""
    latest = 0.0
    for root, _dirs, files in os.walk(path, followlinks=True):
        for f in files:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    if latest:
        return latest
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _skill_entries(d):
    """(name, path) per non-hidden entry that looks like a skill (dir or link
    to one). Hidden entries (.system) stay canonical-private."""
    if not os.path.isdir(d):
        return
    for name in sorted(os.listdir(d)):
        if name.startswith("."):
            continue
        p = os.path.join(d, name)
        if os.path.isdir(p) or os.path.islink(p):
            yield name, p


def plan_merge(canon, dirs):
    """The union pass, read-only: every stray across every REAL skills dir.
    A stray = an entry canonical lacks, or whose content outnews canonical's
    (newest-wins). Same name from several homes: the newest candidate wins.
    -> {name: {src, kind, target, mtime, home, displaces}}"""
    canon_real = os.path.realpath(canon)
    strays = {}
    for label, cdir in dirs:
        sdir = os.path.join(cdir, "skills")
        if os.path.islink(sdir) or not os.path.isdir(sdir):
            continue                      # symlinked homes hold nothing of their own
        for name, p in _skill_entries(sdir):
            real = os.path.realpath(p)
            if not os.path.exists(real):
                continue                  # broken link — census flags it, merge skips
            have = os.path.join(canon, name)
            if os.path.lexists(have) and os.path.realpath(have) == real:
                continue                  # same artifact already canonical
            if real == canon_real or real.startswith(canon_real + os.sep):
                continue                  # points into canonical already
            mtime = _newest(real)
            displaces = os.path.lexists(have)
            if displaces and _newest(os.path.realpath(have)) >= mtime:
                continue                  # canonical copy is newer or as new — keep
            best = strays.get(name)
            if best and best["mtime"] >= mtime:
                continue
            strays[name] = {
                "src": p, "kind": "link" if os.path.islink(p) else "dir",
                "target": real, "mtime": mtime, "home": label,
                "displaces": displaces}
    return strays


def merge(canon, strays, backup_root, apply=False):
    """Union the strays (plan_merge's dict) into canonical. Real dirs copied
    (symlinks preserved inside), symlink entries re-minted at their RESOLVED
    target (absolute — survives any home's death). A displaced canonical
    entry is moved into the backup root first. -> [(name, action)]"""
    actions = []
    for name, cand in sorted(strays.items()):
        verb = "adopt" if not cand["displaces"] else "replace(newest-wins)"
        actions.append((name, "%s from %s [%s]" % (verb, cand["home"], cand["kind"])))
        if not apply:
            continue
        dst = os.path.join(canon, name)
        if cand["displaces"]:
            shelf = os.path.join(backup_root, "canonical-displaced")
            os.makedirs(shelf, exist_ok=True)
            shutil.move(dst, os.path.join(shelf, "%s-%d" % (name, int(time.time()))))
        if cand["kind"] == "link":
            os.symlink(cand["target"], dst)
        else:
            shutil.copytree(cand["src"], dst, symlinks=True)
    return actions


def _swap_symlink(sdir, canon):
    """Land skills -> canon atomically: tmp link + rename, never a window
    where the path is missing on a re-read."""
    tmp = sdir + ".helm-sync-%d" % os.getpid()
    os.symlink(canon, tmp)
    try:
        os.rename(tmp, sdir)
    except OSError:
        os.unlink(tmp)
        raise


def wire(label, cdir, canon, backup_root, apply=False, pending=()):
    """Repoint ONE config dir's skills/ at canonical. -> (action, detail).
    actions: ok (already direct), relink, adopt (was absent), move+link
    (real dir backed up first), FAIL (superset check refused the swap).
    `pending` = names a dry-run merge WOULD union, so a dry wire of a
    stray-holding home previews move+link instead of a false FAIL."""
    sdir = os.path.join(cdir, "skills")
    if os.path.islink(sdir):
        if os.readlink(sdir).rstrip(os.sep) == canon.rstrip(os.sep):
            return "ok", "already canonical"
        old = os.readlink(sdir)
        if apply:
            _swap_symlink(sdir, canon)
            _log(backup_root, "%s relink %s -> %s (was %s)" % (label, sdir, canon, old))
        return "relink", "was -> " + old
    if not os.path.exists(sdir):
        if apply:
            os.makedirs(cdir, exist_ok=True)
            _swap_symlink(sdir, canon)
        return "adopt", "no skills dir existed"
    # a REAL dir: back up by MOVING it whole (the original survives intact),
    # then swap, then prove the new view still shows every prior skill name.
    before = {n for n, _ in _skill_entries(sdir)}
    missing = before - {n for n, _ in _skill_entries(canon)} - set(pending)
    if missing:
        # merge should have unioned these; refuse rather than shadow a skill
        return "FAIL", "would lose %s — run merge first" % ", ".join(sorted(missing))
    if not apply:
        return "move+link", "real dir, %d entries -> backup" % len(before)
    shelf = os.path.join(backup_root, label)
    os.makedirs(shelf, exist_ok=True)
    saved = os.path.join(shelf, "skills-%d" % int(time.time()))
    shutil.move(sdir, saved)
    try:
        _swap_symlink(sdir, canon)
        after = {n for n, _ in _skill_entries(sdir)}
        lost = before - after
        if lost:
            raise OSError("superset check: lost " + ", ".join(sorted(lost)))
    except Exception as e:                # roll the original back — never lose
        if os.path.islink(sdir):
            os.unlink(sdir)
        shutil.move(saved, sdir)
        return "FAIL", str(e)
    _log(backup_root, "%s move+link %s (original: %s)" % (label, sdir, saved))
    return "move+link", "original -> " + saved


def _log(backup_root, line):
    os.makedirs(backup_root, exist_ok=True)
    with open(os.path.join(backup_root, "sync-log.txt"), "a") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), line))


def sync(canon=None, dirs=None, backup_root=None, apply=False):
    """The whole cycle: merge strays into canonical, then wire every config
    dir. Idempotent — a fully-wired estate reports zero changes. -> report."""
    canon = os.path.realpath(canon or canonical())
    backup_root = backup_root or BACKUP_ROOT
    if not os.path.isdir(canon):
        # a missing CUSTOM hub is a loud error; a missing DEFAULT hub = fresh install
        if canon != os.path.realpath(DEFAULT_CANONICAL):
            return {"error": "canonical skills dir missing: %s (set "
                             "HELM_SKILLS_CANONICAL or restore the canonical hub)" % canon}
        if not apply:  # a dry-run must NOT mutate a fresh HOME
            return {"changes": [], "note": "fresh install \u2014 run `helm skillsync "
                    "--apply` to seed the canonical hub at %s" % canon}
        os.makedirs(canon, exist_ok=True)  # fresh install: seed the hub on apply
    if dirs is None:
        dirs = config_dirs()
    strays = plan_merge(canon, dirs)
    merged = merge(canon, strays, backup_root, apply=apply)
    pending = () if apply else set(strays)
    wired, failed = [], []
    for label, cdir in dirs:
        sdir = os.path.join(cdir, "skills")
        if not os.path.islink(sdir) and os.path.realpath(sdir) == canon:
            continue                      # the canonical dir itself, if listed
        action, detail = wire(label, cdir, canon, backup_root,
                              apply=apply, pending=pending)
        (failed if action == "FAIL" else wired).append((label, action, detail))
    return {"canonical": canon, "backup_root": backup_root, "apply": apply,
            "merged": merged, "wired": wired, "failed": failed}


def cmd_sync(args):
    """skills sync [--apply] — merge strays into canonical + repoint every
    claude-code config dir (credhomes, ~/.claude, seats). Dry-run default."""
    from .cli import guard_tail
    rc = guard_tail("helm skills sync", args, flags=("--apply",),
                    usage="skills sync [--apply]")
    if rc is not None:
        return rc
    apply = "--apply" in args
    r = sync(apply=apply)
    if "error" in r:
        print("helm skills sync: " + r["error"], file=sys.stderr)
        return 1
    mode = "APPLIED" if apply else "dry-run (--apply to execute)"
    print("helm skills sync [%s] — canonical: %s" % (mode, r["canonical"]))
    for name, action in r["merged"]:
        print("  merge  %-24s %s" % (name, action))
    changed = 0
    for label, action, detail in r["wired"]:
        if action == "ok":
            continue
        changed += 1
        print("  wire   %-24s %-9s %s" % (label, action, detail))
    steady = len(r["wired"]) - changed
    print("helm skills sync: %d merged, %d homes changed, %d already canonical"
          % (len(r["merged"]), changed, steady))
    for label, _a, detail in r["failed"]:
        print("  FAIL   %-24s %s" % (label, detail), file=sys.stderr)
    if apply and not r["failed"]:
        print("backups: " + r["backup_root"])
    return 1 if r["failed"] else 0
