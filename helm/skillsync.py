#!/usr/bin/env python3
"""helm skills sync — universal skill distribution: ONE canonical skills
source, every claude-code config dir a symlink to it.

The stranding class this kills: per-skill symlink farms are point-in-time
copies of the canonical SET — a skill added later (or dropped into one home by
hand) exists only where it landed. Proven live 2026-07-21: a personal skill sat
in one credhome, invisible to the owner's main session, because only some homes
were whole-dir symlinks to the shared hub.

Canonical = an external skills hub (HELM_SKILLS_CANONICAL env, else the host's
authored `skills_canonical`; empty by default): its entries are per-skill
symlinks into a source repo, so skill edits propagate via git with no re-sync —
the source repo stays the physics owner, helm owns DISTRIBUTION. It lives
outside every credhome (survives archive/recreate), and a helm-owned second
copy would just mint a two-writer drift problem. Lean-configs: canonical holds
the one approved set; a home symlinked to it CANNOT grow private bloat.

Sync is two phases, dry-run by default (the sweep/drain/gc law):
  1. MERGE — union every stray (an entry in a REAL skills dir that canonical
     lacks, or that outnews canonical's copy: newest-wins by content mtime)
     INTO canonical. Real dirs are copied, symlinks re-minted at their
     resolved target. A dangling canonical link is repaired only when the
     estate resolves one unique target; zero or several fail before mutation.
     Displaced canonical entries are backed up first.
  2. WIRE — repoint each config dir's skills/ -> canonical. A real dir is
     MOVED whole into the backup root (the backup IS the original — inodes,
     never a lossy copy), then the symlink lands tmp+rename (atomic). An
     already-direct link is a no-op (idempotent); a link elsewhere — even a
     chain that resolves to canonical through an alias — is normalized to a
     direct link (chains die when the intermediate home is archived).
     Post-swap superset check per home: every skill name visible before must
     be visible after, or the move rolls back. NEVER lose a skill.

Config dirs covered: every real credhome under ~/.claude-homes (alias
symlinks fold onto their target — cto-example and cto-example-invalid are
one home), the default ~/.claude, and every seat CLAUDE_CONFIG_DIR including
per-instance dirs (smoke-claude excluded: recreated per smoke run).

Birth is the other door. link_canonical() is THE one primitive that gives a
config dir its `skills -> canonical` link at the moment it is made: seat mint
calls it for every seat config dir, `helm homes prepare` for every new
credhome, and `helm launch --home H` before the exec. A credhome that reaches
its first session without that link loses every skill silently — the seat
prints no error, /learn and /premise are simply not there — so the link is
minted where the dir is minted, and `helm doctor` names any config dir whose
skills entry is missing or points elsewhere. `helm skills sync` remains the
deliberate repair (a real dir moved, a foreign link normalized).
"""
import collections
import errno
import os
import shutil
import stat
import sys
import time

from . import home as _home
from . import registry

BACKUP_ROOT = os.path.join(os.path.expanduser("~"), ".skills-premerge-backup")

# link_canonical's answer — TWO independent facts, never one field for both:
#   source_failure — the canonical source could not be used (authored
#     registry unreadable, configured hub missing), with the reason. Set
#     whenever that is TRUE, whatever happened at the destination: a real dir
#     left alone beside a corrupt registry is still a corrupt registry.
#   degraded — the entry NOW points at the host fallback BECAUSE of that
#     failure. Set only on linked / ok / relinked delivering the fallback;
#     computed from the OUTCOME, never from the intent (a real dir, a failed
#     link, a foreign link delivered nothing from the fallback).
# `link` is the skills entry the answer is about (None when no source), so a
# printer names what the entry NOW points at rather than a prior detail.
Link = collections.namedtuple("Link", "action detail degraded link source_failure")


def canonical():
    """The canonical skills source, or None when none is configured — the public
    default, which makes skill sync OPT-IN. Resolution: HELM_SKILLS_CANONICAL
    env, else the host's authored `skills_canonical` (registry-authored.json
    `host` block). No site-specific path ships in code. The source should be a
    gitignored hub whose entries symlink INTO their repo (repo skills stay
    repo-owned, local strays stay local, one distribution point) — deliberately
    NOT a repo's own tracked skills dir, since merging strays into a tracked
    as-public dir is exactly the leak sync must never make.

    REFUSES (propagates registry.AuthoredUnreadable) rather than returning None
    when the authored layer EXISTS but is unreadable — a corrupt file must not
    read as 'no hub configured' and silently no-op the sync."""
    c = _home.env("SKILLS_CANONICAL")
    if not c:
        c = registry.authored_host().get("skills_canonical")
    if not c:
        return None
    # Realpath the source: a symlinked hub resolves to its physical store, and
    # mint (canonical()) vs sync (which realpaths) must agree on the path STRING
    # or a wired home relinks off the raw path on the next sync.
    return os.path.realpath(os.path.expanduser(c))


def link_canonical(cdir, relink=True, host_fallback=False):
    """Give ONE config dir its `skills -> canonical` link at birth. -> Link
    (action, detail, degraded), no exception. action: `ok` (already the direct link), `linked` (was
    absent, now points at canonical), `relinked` (a link elsewhere was
    normalized — relink=True only, the seat-mint policy: a stale or indirect
    link at mint is a bug to cure, not an estate to respect), `indirect` (a
    link that RESOLVES to canonical through another path, left alone and
    named — relink=False), `foreign` (a link elsewhere, LEFT ALONE and named
    — relink=False, the policy for a home that already exists: prepare and
    launch report, never overwrite), `real` (a REAL skills dir, never
    clobbered here — that repair is `helm skills sync --apply`'s backup-first
    job), `none` (no canonical source is configured: skill distribution is
    opt-in, and this is the one QUIET answer), `unavailable` (a hub IS
    configured but cannot be used — the path is missing, or the authored
    layer is unreadable — with the reason; a caller says this out loud,
    because the home is otherwise born without skills and nothing else
    complains), and `error` (the OSError text).

    host_fallback mirrors the minting host's own CLAUDE_CONFIG_DIR skills when
    no canonical can be used — seat mint's behaviour on a foreign machine
    with no hub; a credhome never takes it, since its deck (HELM_SKILL_DECK)
    is that host's answer. When the fallback serves BECAUSE the canonical
    source is unusable (authored layer unreadable, configured path missing),
    `degraded` carries that reason beside the successful action: the seat
    has skills, and the operator still hears that the canonical read failed.
    A deliberately unconfigured host that takes the fallback is not
    degraded (degraded=None) — there was nothing to read."""
    # UNCONFIGURED is quiet (opt-in); UNAVAILABLE is not: a hub the host
    # names but that cannot be resolved (configured path missing, authored
    # layer unreadable) is a home silently born without skills, and the two
    # must never share one answer.
    why = None
    try:
        src = canonical()
    except registry.AuthoredUnreadable as e:
        why = "authored layer unreadable (%s)" % e
        src = None
    if src and not os.path.isdir(src):
        why = "%s is configured but MISSING" % src
        src = None
    fallback = None
    if not src:
        if host_fallback:
            base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
                os.path.expanduser("~"), ".claude")
            src = os.path.join(base, "skills")
            fallback = why
        if not src or not os.path.isdir(src):
            if why:
                return Link("unavailable", why, None, None, why)
            return Link("none", "no canonical skills source configured",
                        None, None, None)
    link = os.path.join(cdir, "skills")

    def answer(action, detail, delivered):
        """`why` is reported whatever the outcome; `fallback` rides along only
        when the outcome DELIVERED the fallback (the entry now points at src)."""
        return Link(action, detail, fallback if delivered else None, link, why)

    try:
        if os.path.islink(link):
            old = os.readlink(link)
            if old.rstrip(os.sep) == src.rstrip(os.sep):
                return answer("ok", src, True)
            if not relink:
                if os.path.realpath(link) == src:
                    return answer("indirect", old, False)
                return answer("foreign", old, False)
            os.unlink(link)
            os.symlink(src, link)
            return answer("relinked", "was -> " + old, True)
        if os.path.exists(link):
            return answer("real", link, False)
        os.makedirs(cdir, exist_ok=True)
        os.symlink(src, link)
        return answer("linked", src, True)
    except OSError as e:
        return answer("error", str(e), False)


def failure_line(res):
    """The one sentence every printing caller says whenever the canonical
    source could not be used, WHATEVER happened at the destination — a real
    dir left alone, a link that failed, a foreign link kept, or a fallback
    delivered (then degraded_line follows it). None when the canonical source
    served or nothing was configured, and None for `unavailable`, whose own
    line already carries this exact reason — one report, not two."""
    if not res.source_failure or res.action == "unavailable":
        return None
    return ("the canonical skills registry is unusable: %s; restore it, then "
            "`helm skills sync --apply`" % res.source_failure)


def degraded_line(res):
    """The one sentence every printing caller says when a link came from the
    host fallback because the canonical source could not be read — it names
    the degradation WITHOUT saying the home has no skills (it has the
    fallback's), and names the source as what the entry NOW points at (read
    back from the link, never a prior detail such as relinked's "was -> old").
    None when the canonical source served, nothing was configured, or
    nothing was delivered (a real dir left alone, a link that failed) — the
    failure itself is failure_line's, said on its own."""
    if not res.degraded:
        return None
    return ("skills come from the host fallback (%s) because of that failure"
            % os.readlink(res.link))


class Census(list):
    """config_dirs' answer: the [(label, config_dir)] it FOUND, plus
    `unlisted` — [(path, errno_name)] for every root or subtree it could NOT
    enumerate. A forgiving census (glob swallows the OSError and returns
    fewer entries) reads exactly like a smaller estate, and a health row
    built on it certifies dirs it never saw; a consumer that claims
    completeness reads `unlisted` first. A plain list handed in by a test
    seam is a complete census."""
    def __init__(self, found=(), unlisted=()):
        super().__init__(found)
        self.unlisted = list(unlisted)


def _entries(path, unlisted):
    """Sorted child names of path, or [] — ABSENT is a legitimately empty
    root (glob's answer, kept), any other OSError is recorded on the census
    instead of swallowed."""
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        return []
    except NotADirectoryError:
        return []
    except OSError as e:
        unlisted.append((path, errno.errorcode.get(e.errno, e.__class__.__name__)))
        return []


def _isdir(path, unlisted):
    """os.path.isdir that records a stat it could not make (EACCES on a
    parent, ELOOP, EIO) rather than answering False for it."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as e:
        unlisted.append((path, errno.errorcode.get(e.errno, e.__class__.__name__)))
        return False


def config_dirs(claude_root=None, default_claude=None, seats_root=None):
    """Census — every claude-code config dir on this host: real credhomes
    (deduped on realpath; alias symlinks fold onto their target), the default
    ~/.claude, seat + seat-instance CLAUDE_CONFIG_DIRs. Returns a Census:
    list-shaped, with `.unlisted` naming every subtree it could not
    enumerate and why — never a silently shorter list. Parameterized so
    tests run against a fake estate."""
    from . import homes as credhomes
    claude_root = claude_root or credhomes.ROOTS["claude"]
    default_claude = default_claude or credhomes.DEFAULTS["claude"]
    seats_root = seats_root or os.path.join(_home.global_dir(), "seats")
    out, seen, unlisted = [], set(), []
    for name in _entries(claude_root, unlisted):
        p = os.path.join(claude_root, name)
        real = os.path.realpath(p)
        if os.path.islink(p) or not _isdir(real, unlisted) or real in seen:
            continue
        seen.add(real)
        out.append((name, real))
    d = os.path.realpath(default_claude)
    if _isdir(d, unlisted) and d not in seen:
        seen.add(d)
        out.append(("default-claude", d))

    def seat(cdir, label):
        real = os.path.realpath(cdir)
        if not _isdir(real, unlisted) or real in seen:
            return
        seen.add(real)
        out.append((label, real))

    for fam in _entries(seats_root, unlisted):
        fdir = os.path.join(seats_root, fam)
        seat(os.path.join(fdir, "claude"), "seat:" + fam)
        inst_root = os.path.join(fdir, "instances")
        for inst in _entries(inst_root, unlisted):
            seat(os.path.join(inst_root, inst, "claude"),
                 "seat:" + os.path.join(fam, inst))
    return Census(out, unlisted)


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


def _dangling_repairs(canon, dirs):
    """({name: merge candidate}, failures) for broken canonical skill links.

    A relative per-skill link can be valid only from the home where it was
    authored, then become dangling when copied into the canonical hub. Resolve
    it against every known config skills/ anchor and against same-name entries
    still present in real home dirs. Exactly one physical directory is proof;
    zero or several are a loud refusal, never a silent skip or newest-wins guess.
    Targets under a managed home are copied because wire() moves that home;
    external targets are re-minted as absolute links.
    """
    repairs, failures = {}, []
    canon_real = os.path.realpath(canon)
    anchors, managed = [], []
    for label, cdir in dirs:
        sdir = os.path.join(cdir, "skills")
        anchors.append((label, sdir))
        if not os.path.islink(sdir) and os.path.isdir(sdir):
            managed.append(os.path.realpath(sdir))

    def inside(path, root):
        return path == root or path.startswith(root + os.sep)

    for name, link in _skill_entries(canon):
        if not os.path.islink(link) or os.path.exists(os.path.realpath(link)):
            continue
        candidates = {}
        raw = os.readlink(link)
        if os.path.isabs(raw):
            detail = ("dangling canonical link %s -> %s has an absolute owner "
                      "that is unavailable; refusing name-only reassignment"
                      % (name, raw))
            failures.append(("canonical:" + name, "FAIL", detail))
            continue

        def add(path, label):
            target = os.path.realpath(path)
            if (not os.path.isdir(target)
                    or inside(target, canon_real)
                    or inside(canon_real, target)):
                return
            try:
                st = os.stat(target)
            except OSError:
                return
            candidates.setdefault((st.st_dev, st.st_ino), (target, label))

        for label, sdir in anchors:
            if not os.path.islink(sdir) and os.path.isdir(sdir):
                add(os.path.join(sdir, name), label)
            add(os.path.normpath(os.path.join(sdir, raw)),
                label + " re-anchored")
        if len(candidates) != 1:
            targets = sorted(target for target, _label in candidates.values())
            detail = ("dangling canonical link %s -> %s has %s resolvable "
                      "targets%s" % (
                          name, raw, len(candidates),
                          ": " + ", ".join(targets) if targets else ""))
            failures.append(("canonical:" + name, "FAIL", detail))
            continue
        target, label = next(iter(candidates.values()))
        kind = "dir" if any(inside(target, root) for root in managed) else "link"
        repairs[name] = {
            "src": target, "kind": kind, "target": target, "mtime": 0,
            "home": label + " dangling repair", "displaces": True}
    return repairs, failures


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
    try:
        canon = canon or canonical()
    except registry.AuthoredUnreadable as e:
        return {"error": "authored layer unreadable (%s) — refusing sync; the "
                         "config is recoverable from its .corrupt backup" % e}
    if not canon:
        return {"error": "no canonical skills source configured (set "
                         "HELM_SKILLS_CANONICAL or the host `skills_canonical` "
                         "authored field) — skill sync is opt-in"}
    canon = os.path.realpath(canon)
    backup_root = backup_root or BACKUP_ROOT
    if not os.path.isdir(canon):
        return {"error": "canonical skills dir missing: %s" % canon}
    if dirs is None:
        dirs = config_dirs()
    unlisted = list(getattr(dirs, "unlisted", ()))
    repairs, dangling_failed = _dangling_repairs(canon, dirs)
    if dangling_failed:
        return {"canonical": canon, "backup_root": backup_root, "apply": apply,
                "merged": [], "wired": [], "failed": dangling_failed,
                "unlisted": unlisted}
    strays = plan_merge(canon, dirs)
    strays.update(repairs)
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
            "merged": merged, "wired": wired, "failed": failed,
            "unlisted": unlisted}


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
    # an estate the census could not fully list is NOT a wired estate: a
    # config dir hidden in an unlistable subtree was neither merged nor
    # wired, and a clean tally here would say it was
    for path, why in r["unlisted"]:
        print("  UNLISTED %s (%s) — not censused, not wired; fix the listing "
              "and rerun" % (path, why), file=sys.stderr)
    if apply and not r["failed"] and not r["unlisted"]:
        print("backups: " + r["backup_root"])
    return 1 if r["failed"] or r["unlisted"] else 0
