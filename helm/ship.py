#!/usr/bin/env python3
"""helm ship — the authored chain over git; `ship pull` merges + re-derives.

The overlay-not-store constitution makes cross-machine continuity cheap: the
ONLY thing worth syncing is the AUTHORED chain — premises/heuristics/lexicon/
prd/journal/evals/archive, know-your-user, reflexes, priors/references, and
registry-authored.json (lineage edges + notes: unrebuildable) — all
git-friendly files an operator or agent WROTE. Everything DERIVED re-derives
per host via `helm sync` and never ships: registry.json projections (any
depth — the _global master AND every per-project mirror), .state, caches,
seat runtime (seats/ carries provider AUTH). RAM-canon (chat rooms, tmpfs
node state) never even touches this disk — always out of scope by design.

Laws:
  - dry-run default (the drain law): bare `helm ship` reports what WOULD
    ship; only --apply touches the estate (git init/commit/push).
  - nothing-secret-staged: every staged byte is scanned for token/key
    patterns BEFORE any commit exists; a hit refuses loudly and unstages.
  - remote must be PRIVATE (journal/premise content is operator-internal);
    no remote configured = commit-only, said explicitly, never an error.
  - adopted-by-symlink homes are host-local: the symlink
    is ignored — that chain ships from its own repo, never from here.
  - per-host observation blocks (_global/hosts/<host>.json) carry what THIS
    host observed — the one projection-derived artifact that ships, because
    it IS the cross-host payload. One file per host: merges never conflict.
  - merges are append-only by construction: entries are per-file, two hosts
    revising the same knowledge meet through the store's supersede-chains
    (tombstone + replacement file), not three-way markdown conflicts. pull
    never resolves content — git merges files, `helm sync` re-derives the
    local projections, the store's status filter picks live over tombstone.
"""
import os
import re
import socket
import subprocess
import sys

from . import home, pk

# The derived set, as .gitignore lines (the classifier — the python-side
# matcher in _derived() must mirror it exactly).
DERIVED = (
    "registry.json",   # projection + per-project mirrors; registry-authored.json SHIPS
    ".state/",
    "seats/",          # host-local seat runtime — carries provider auth
    "__pycache__/",
    "*.pyc",
    "*.tmp",
    ".DS_Store",
)

_BEGIN = "# >>> helm ship (managed — derived never ships) >>>"
_END = "# <<< helm ship <<<"

SECRET_PATTERNS = (
    ("anthropic key", rb"sk-ant-[A-Za-z0-9_-]{8,}"),
    ("api key", rb"\bsk-[A-Za-z0-9]{20,}"),
    ("github token", rb"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    ("github pat", rb"github_pat_[A-Za-z0-9_]{20,}"),
    ("aws access key", rb"\bAKIA[0-9A-Z]{16}\b"),
    ("slack token", rb"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("private key", rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("jwt", rb"\beyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}"),
    ("bearer header", rb"[Aa]uthorization['\"]?\s*[:=]\s*['\"]?Bearer\s+[A-Za-z0-9_.~+/=-]{16,}"),
    # runbook fix #6 — defense-in-depth behind the wholesale seats/ ignore:
    # a seat token mints as 64 hex (secrets.token_hex(32)) and rides INLINE
    # in launch.sh as ANTHROPIC_AUTH_TOKEN=<hex>; if either ever escapes the
    # ignore it must still be refused here, never reach git history.
    ("seat token env", rb"ANTHROPIC_AUTH_TOKEN\s*=\s*\S{16,}"),
    ("bare seat token", rb"(?m)^\s*[0-9a-f]{64}\s*$"),
)


def _git(hh, *args):
    return subprocess.run(("git", "-C", hh) + args, capture_output=True, text=True)


def _ident(hh):
    """Identity flags when the repo/host has none — a helm home must ship even
    on a box where git was never introduced to its operator."""
    if _git(hh, "config", "user.email").stdout.strip():
        return ()
    return ("-c", "user.name=helm", "-c", "user.email=helm@" + socket.gethostname())


def _is_repo(hh):
    return os.path.isdir(os.path.join(hh, ".git"))


def _branch(hh):
    return _git(hh, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"


def _remote(hh):
    return _git(hh, "remote", "get-url", "origin").stdout.strip()


def _symlink_homes(hh):
    """Top-level symlinked project homes (adoption law) — host-local, ignored."""
    try:
        names = sorted(os.listdir(hh))
    except OSError:
        return []
    return [n for n in names if os.path.islink(os.path.join(hh, n))]


def gitignore_lines(hh):
    return list(DERIVED) + ["/" + n + "/" for n in _symlink_homes(hh)] \
        + ["/" + n for n in _symlink_homes(hh)]


def write_gitignore(hh):
    """The managed block, idempotent; operator lines outside it survive."""
    path = os.path.join(hh, ".gitignore")
    block = "\n".join([_BEGIN] + gitignore_lines(hh) + [_END])
    cur = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cur = f.read()
    if _BEGIN in cur and _END in cur:
        new = re.sub(re.escape(_BEGIN) + r".*?" + re.escape(_END), block,
                     cur, count=1, flags=re.S)
    else:
        new = (cur.rstrip() + "\n\n" if cur.strip() else "") + block + "\n"
    if new != cur:
        pk.atomic_write(path, new)
    return path


def _derived(rel):
    """The python-side twin of DERIVED (dry mode classifies without git)."""
    parts = rel.split(os.sep)
    if set(parts[:-1]) & {".git", ".state", "seats", "__pycache__"}:
        return True
    base = parts[-1]
    return base in ("registry.json", ".DS_Store") or base.endswith((".pyc", ".tmp"))


def ship_set(hh):
    """(would-ship relpaths, {rule: skipped-count}) — symlinked homes pruned."""
    files, skipped = [], {}
    links = set(_symlink_homes(hh))
    for root, dirs, names in os.walk(hh):
        rel_root = os.path.relpath(root, hh)
        if rel_root == ".":
            dirs[:] = [d for d in dirs if d not in links]
            for n in links:
                skipped["adopted-symlink"] = skipped.get("adopted-symlink", 0) + 1
        pruned = [d for d in dirs if d in (".git", ".state", "seats", "__pycache__")]
        for d in pruned:
            skipped[d + "/"] = skipped.get(d + "/", 0) + 1
        dirs[:] = [d for d in dirs if d not in pruned]
        for n in names:
            rel = os.path.normpath(os.path.join(rel_root, n))
            if _derived(rel):
                skipped[n if n == "registry.json" else "misc"] = \
                    skipped.get(n if n == "registry.json" else "misc", 0) + 1
                continue
            if not os.path.islink(os.path.join(root, n)):
                files.append(rel)
    files.sort()
    return files, skipped


def scan_secrets(hh, rels):
    """[(relpath, pattern-label)] over raw bytes — the nothing-secret-staged
    check. Runs on the exact byte set that would land in history."""
    hits = []
    for rel in rels:
        try:
            with open(os.path.join(hh, rel), "rb") as f:
                data = f.read()
        except OSError:
            continue
        for label, pat in SECRET_PATTERNS:
            if re.search(pat, data):
                hits.append((rel, label))
                break
    return hits


def _host_block(hh):
    """What THIS host observed, distilled from the local projection. Written
    only when the observation CHANGED (shipped_ts alone never dirties the
    tree — ship stays idempotent on a quiet estate)."""
    reg = pk.read_json(home.registry_path(), {}) or {}
    projects = {}
    for name, rec in sorted((reg.get("projects") or {}).items()):
        projects[name] = {"status": rec.get("status"),
                          "last_seen": rec.get("last_seen"),
                          "sessions": sum((rec.get("sessions") or {}).values()),
                          "path": rec.get("path")}
    path = os.path.join(home.global_dir(), "hosts", socket.gethostname() + ".json")
    cur = pk.read_json(path, {}) or {}
    if {k: v for k, v in cur.items() if k != "shipped_ts"} != \
            {"host": socket.gethostname(), "projects": projects}:
        pk.write_json(path, {"host": socket.gethostname(),
                             "shipped_ts": pk.now_ts(), "projects": projects})
    return path


def _staged(hh):
    out = _git(hh, "ls-files", "--cached", "-z").stdout
    return [p for p in out.split("\0") if p]


def _refuse(hh, hits, dry):
    verb = "would REFUSE" if dry else "REFUSED"
    print("helm ship: %s — secret-shaped bytes in the ship set "
          "(nothing-secret-staged law):" % verb, file=sys.stderr)
    for rel, label in hits:
        print("  %s  <- %s" % (rel, label), file=sys.stderr)
    if not dry:
        _git(hh, "reset", "-q")
        print("  staged files were UNSTAGED; nothing committed. Scrub (or move "
              "the file under a derived dir) and re-run.", file=sys.stderr)
    return 1


def ship(apply=False, remote=None, message=None):
    hh = home.helm_home()
    if not os.path.isdir(hh):
        print("helm ship: no helm home at %s (run `helm sync` first)" % hh,
              file=sys.stderr)
        return 1
    inited = _is_repo(hh)
    files, skipped = ship_set(hh)

    if not apply:
        rem = _remote(hh) if inited else ""
        size = sum(os.path.getsize(os.path.join(hh, r))
                   for r in files if os.path.exists(os.path.join(hh, r)))
        print("helm ship --dry (nothing touched):")
        print("  home:   " + hh)
        print("  git:    " + ("initialized, branch " + _branch(hh) if inited
                              else "not initialized (would `git init -b main`)"))
        print("  remote: " + (rem or "none configured — would COMMIT ONLY "
                              "(add --remote <private-url>)"))
        print("  would ship: %d authored files, %.1fMB" % (len(files), size / 1e6))
        drv = " ".join("%s x%d" % (k, v) for k, v in sorted(skipped.items()))
        print("  derived (never ships): " + (drv or "none present"))
        links = _symlink_homes(hh)
        if links:
            print("  adopted symlink homes (host-local): " + ", ".join(links))
        hits = scan_secrets(hh, files)
        if hits:
            return _refuse(hh, hits, dry=True)
        print("  secret scan: clean (%d patterns over %d files)"
              % (len(SECRET_PATTERNS), len(files)))
        print("  apply with: helm ship --apply [--remote <private-url>]")
        return 0

    if not inited:
        r = _git(hh, "init", "-q", "-b", "main")
        if r.returncode:
            print("helm ship: git init failed: " + r.stderr.strip(), file=sys.stderr)
            return 1
        print("helm ship: initialized " + os.path.join(hh, ".git"))
    write_gitignore(hh)
    _host_block(hh)
    if remote:
        _git(hh, "remote", "remove", "origin")
        _git(hh, "remote", "add", "origin", remote)
    _git(hh, "add", "-A")
    hits = scan_secrets(hh, _staged(hh))
    if hits:
        return _refuse(hh, hits, dry=False)
    delta = [p for p in _git(hh, "diff", "--cached", "--name-only", "-z").stdout.split("\0") if p]
    if not delta:
        print("helm ship: clean — nothing new to ship")
    else:
        msg = message or "helm ship: %s %s" % (socket.gethostname(), pk.now_ts())
        r = _git(hh, *_ident(hh), "commit", "-q", "-m", msg)
        if r.returncode:
            print("helm ship: commit failed: " + r.stderr.strip(), file=sys.stderr)
            return 1
        print("helm ship: committed %d file%s (%s)"
              % (len(delta), "s"[:len(delta) != 1],
                 _git(hh, "rev-parse", "--short", "HEAD").stdout.strip()))
    rem = _remote(hh)
    if not rem:
        print("helm ship: no remote configured — committed locally ONLY. "
              "Add a PRIVATE one: helm ship --apply --remote <url>")
        return 0
    r = _git(hh, "push", "-q", "-u", "origin", _branch(hh))
    if r.returncode:
        err = r.stderr.strip()
        hint = " — another host shipped first: `helm ship pull`, then re-ship" \
            if "rejected" in err or "fetch first" in err else ""
        print("helm ship: push to %s FAILED%s\n  %s" % (rem, hint, err),
              file=sys.stderr)
        return 1
    print("helm ship: pushed to " + rem)
    return 0


def _resync():
    """Regenerate the local projections after a merge (helm sync). Split out
    so hermetic tests can stub the harness scan."""
    from . import registry
    reg, report = registry.sync()
    return len(reg["projects"]), len(report.get("new") or [])


def pull():
    hh = home.helm_home()
    if not _is_repo(hh):
        print("helm ship pull: %s is not shipped yet — bootstrap with "
              "`helm ship --apply --remote <url>` (or clone your helm repo "
              "to ~/.helm)" % hh, file=sys.stderr)
        return 1
    if not _remote(hh):
        print("helm ship pull: no remote — add one: helm ship --apply "
              "--remote <private-url>", file=sys.stderr)
        return 1
    before = _git(hh, "rev-parse", "HEAD").stdout.strip()
    r = _git(hh, *_ident(hh), "pull", "--no-rebase", "--no-edit", "-q",
             "origin", _branch(hh))
    if r.returncode:
        conflicted = _git(hh, "diff", "--name-only", "--diff-filter=U").stdout.split()
        if conflicted:
            print("helm ship pull: CONFLICT in %s — supersede-chains should "
                  "make this impossible for store entries; resolve by hand, "
                  "then `helm ship --apply`" % ", ".join(conflicted), file=sys.stderr)
        else:
            print("helm ship pull: " + r.stderr.strip(), file=sys.stderr)
        return 1
    after = _git(hh, "rev-parse", "HEAD").stdout.strip()
    if before == after:
        print("helm ship pull: already up to date")
    elif not before:  # unborn branch: the bootstrap pull
        print("helm ship pull: merged (initial) -> " + after[:7])
    else:
        n = _git(hh, "rev-list", "--count", before + ".." + after).stdout.strip()
        print("helm ship pull: merged %s commit%s (%s -> %s)"
              % (n, "s"[:n != "1"], before[:7], after[:7]))
    total, new = _resync()
    print("helm ship pull: projections regenerated — %d project%s known (%d new)"
          % (total, "s"[:total != 1], new))
    return 0


def hosts():
    """The cross-host view the observation blocks exist for: who observed
    what, when — live-on-host-a, dormant-on-host-b."""
    d = os.path.join(home.global_dir(), "hosts")
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        names = []
    if not names:
        print("helm ship hosts: no observation blocks yet (helm ship --apply "
              "writes this host's; pull brings the others')")
        return 0
    for n in names:
        b = pk.read_json(os.path.join(d, n), {}) or {}
        projs = b.get("projects") or {}
        live = sum(1 for p in projs.values() if p.get("status") == "active")
        print("  %-12s %s  %d projects (%d active)"
              % (b.get("host", n[:-5]), b.get("shipped_ts", "?"), len(projs), live))
    return 0


_USAGE = """usage: helm ship [--apply] [--remote URL] [--message M] | pull | hosts
  (bare)                dry-run: report what WOULD ship, scan for secrets, touch nothing
  --apply               git-init ~/.helm, refresh .gitignore, commit; push when a remote is set
  --remote URL          set the PRIVATE origin (with --apply)
  --message M           commit message override
  pull                  fetch + merge the authored chain, then regenerate projections (helm sync)
  hosts                 per-host observation blocks: who observed what, when"""


def cmd_pull(args):
    """ship pull — merge the authored chain from origin + re-derive locally."""
    if args:
        print(_USAGE, file=sys.stderr)
        return 2
    return pull()


def cmd_ship(args):
    """ship [--apply] [--remote URL] | ship pull — the authored chain over git."""
    args = list(args)
    if args and args[0] == "pull":
        return cmd_pull(args[1:])
    if args and args[0] == "hosts":
        from .cli import guard_tail
        rc = guard_tail("helm ship hosts", args[1:], usage="ship hosts")
        if rc is not None:
            return rc
        return hosts()
    apply_flag, remote, message = False, None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--apply":
            apply_flag = True
        elif a == "--dry":
            apply_flag = False
        elif a == "--remote" and i + 1 < len(args):
            remote = args[i + 1]
            i += 1
        elif a == "--message" and i + 1 < len(args):
            message = args[i + 1]
            i += 1
        else:
            print(_USAGE, file=sys.stderr)
            return 2
        i += 1
    return ship(apply=apply_flag, remote=remote, message=message)
