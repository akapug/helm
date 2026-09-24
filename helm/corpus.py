#!/usr/bin/env python3
"""Training-corpus transcript backup — `helm corpus backup|status`.

The transcripts-are-training-corpus premise made executable: every local
session, subagent, and workflow transcript (claude + codex, every cred-home,
the reboot-ephemeral /tmp estate) is COPIED into a dated archive under
`HELM_CORPUS_DEST` (default `~/corpus-archive` — point it at a bigger local
mount for your training store). Incremental like the catalog cache: a
manifest keyed by source path skips unchanged files by (size, mtime_ns), so a
re-run costs stats, not bytes; a copy hashes en route, and a touched-but-
identical file refreshes the cursor without a second body. HARD LAWS:

  * COPY-ONLY. No code path renames, rewrites, or removes a source — sources
    are opened read-only, full stop. The archive is append-only the other way
    too: a changed transcript lands in TODAY's date dir; older snapshots stay
    (a later prune/truncation upstream can never reach back and lose bytes).
  * FAIL-OPEN per file: an unreadable transcript reports and the run goes on;
    the manifest records only landed copies, so failures retry next run.
  * FAIL-SAFE on space: the whole plan must fit the dest filesystem (plus
    margin) or the run aborts before the first byte — a half-written archive
    on a full disk is worse than a loud "point HELM_CORPUS_DEST elsewhere".

Layout: `<dest>/<YYYY-MM-DD>/<label>/<path relative to its root>`; state in
`<dest>/.helm-corpus/` (manifest.json = the cursor, runs.jsonl = run trail) —
a projection of the manifest's own dest, rebuildable by re-copy (law 3).
"""
import glob
import hashlib
import json
import os
import sys
import time
from . import localnames, pk

HOME = os.path.expanduser("~")
GB = 1 << 30
MARGIN = GB  # the dest fs must fit the plan plus this

# Root patterns, expanded at scan time (a /tmp/claude-* appears per boot; a
# new cred-home appears per account). The cred-home projects dirs are usually
# symlinks to ~/.claude/projects — the inode dedup collapses them, and a home
# that someday stops symlinking is covered the day it does. Extra roots ride
# the SAME env knobs the session catalog reads (one config surface).
CLAUDE_ROOT_GLOBS = [f"{HOME}/.claude/projects", f"{HOME}/.claude-homes/*/projects"] + \
    [r for r in os.environ.get("HELM_CLAUDE_ROOTS", localnames.legacy_env(
        "CLAUDE_ROOTS", "")).split(":") if r]
CODEX_ROOT_GLOBS = [f"{HOME}/.codex/sessions", f"{HOME}/.codex-homes"] + \
    [r for r in os.environ.get("HELM_CODEX_ROOTS", localnames.legacy_env(
        "CODEX_ROOTS", "")).split(":") if r]
TMP_ROOT_GLOBS = ["/tmp/claude-*"]

def source_specs():
    """label -> (root globs, file patterns under each root), read at call time
    (tests rebind the root lists; catalog precedent). tmp is pattern-scoped to
    transcript-shaped subtrees (projects/ + subagents/) so scratch jsonl
    exhaust (ledgers, fixtures) stays out of the corpus."""
    return (
        ("claude", CLAUDE_ROOT_GLOBS, ("**/*.jsonl",)),
        ("codex", CODEX_ROOT_GLOBS, ("**/rollout-*.jsonl",)),
        ("tmp", TMP_ROOT_GLOBS, ("**/projects/**/*.jsonl", "**/subagents/**/*.jsonl")),
    )


def dest_root(flag=None):
    return os.path.expanduser(flag or os.environ.get("HELM_CORPUS_DEST")
                              or os.path.join(HOME, "corpus-archive"))


def _state_path(dest, name):
    return os.path.join(dest, ".helm-corpus", name)


def scan():
    """Every transcript on the estate -> rows {label, rel, p, sz, mtns},
    inode-deduped (8 cred-homes symlink one projects dir) with the canonical
    root listed first so it wins. `.flat.` siblings are derived projections of
    bytes already captured — skipped. Fail-open per file."""
    seen, rels, rows = set(), set(), []
    for label, root_globs, pats in source_specs():
        for rg in root_globs:
            for root in sorted(glob.glob(rg)):
                for pat in pats:
                    for p in glob.glob(os.path.join(glob.escape(root), pat), recursive=True):
                        if ".flat." in os.path.basename(p) or not os.path.isfile(p):
                            continue
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        key = (st.st_dev, st.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                        rel = os.path.join(label, os.path.relpath(p, root))
                        if rel in rels:  # two roots, one relpath, different bytes
                            rel = os.path.join(label, hashlib.sha256(root.encode()).hexdigest()[:8],
                                               os.path.relpath(p, root))
                        rels.add(rel)
                        rows.append({"label": label, "rel": rel, "p": p,
                                     "sz": st.st_size, "mtns": st.st_mtime_ns})
    return rows


def load_manifest(dest):
    try:
        with pk.open_regular(_state_path(dest, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        return man if isinstance(man, dict) else {}
    except Exception:
        return {}


def _save_manifest(dest, man):
    path = _state_path(dest, "manifest.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f)
    os.replace(tmp, path)


def plan(rows, man):
    """(pending, unchanged): pending = rows the cursor says are new or grown —
    the (size, mtime_ns) key of the catalog cache."""
    pending, unchanged = [], []
    for r in rows:
        ent = man.get(r["p"])
        if ent and ent.get("sz") == r["sz"] and ent.get("mtns") == r["mtns"]:
            unchanged.append(r)
        else:
            pending.append(r)
    return pending, unchanged


def _copy_one(row, dest, date, man):
    """One transcript into the dated archive: stream-copy hashing en route,
    tmp-write + atomic replace on the DEST side only (the source is opened
    read-only and never touched). A hash identical to the recorded one means
    the bytes are already archived (mtime-only churn) — the scratch copy is
    discarded and the cursor refreshed. Returns 'copied' | 'refreshed'."""
    prev = man.get(row["p"])
    dst = os.path.join(dest, date, row["rel"])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = "%s.%d.part" % (dst, os.getpid())
    h = hashlib.sha256()
    try:
        with open(row["p"], "rb") as src, open(tmp, "wb") as out:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
        sha = h.hexdigest()
        if prev and prev.get("sha") == sha:
            os.remove(tmp)  # dest-side scratch only
            state, dest_rel = "refreshed", prev["dest"]
        else:
            os.replace(tmp, dst)
            os.utime(dst, ns=(row["mtns"], row["mtns"]))  # provenance mtime
            state, dest_rel = "copied", os.path.join(date, row["rel"])
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    man[row["p"]] = {"sz": row["sz"], "mtns": row["mtns"], "sha": sha,
                     "dest": dest_rel,
                     "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return state


def _free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d%s" if unit == "B" else "%.1f%s") % (n, unit)
        n /= 1024.0


def _by_label(rows):
    out = {}
    for r in rows:
        n, b = out.get(r["label"], (0, 0))
        out[r["label"]] = (n + 1, b + r["sz"])
    return out


def _last_run(dest):
    try:
        with pk.open_regular(_state_path(dest, "runs.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


def backup(dest, dry=False, now=None):
    """The verb's engine -> a report dict; copies only when not dry. Per-file
    fail-open; the manifest is saved even on a partial run (landed copies
    never re-pay), and errored files stay out of it so they retry."""
    rows = scan()
    man = load_manifest(dest)
    pending, unchanged = plan(rows, man)
    date = time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
    rep = {"rows": rows, "pending": pending, "unchanged": unchanged, "date": date,
           "bytes": sum(r["sz"] for r in pending), "copied": 0, "refreshed": 0,
           "errors": [], "aborted": None}
    if dry:
        return rep
    os.makedirs(dest, exist_ok=True)
    if pending:
        free = _free_bytes(dest)
        if rep["bytes"] + MARGIN > free:
            rep["aborted"] = ("plan %s + %s margin exceeds free %s on %s — point "
                              "HELM_CORPUS_DEST at a larger mount"
                              % (_human(rep["bytes"]), _human(MARGIN), _human(free), dest))
            return rep
        for r in pending:
            try:
                rep[_copy_one(r, dest, date, man)] += 1
            except OSError as exc:
                rep["errors"].append("%s (%s)" % (r["p"], exc))
        _save_manifest(dest, man)
    run = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "date": date,
           "copied": rep["copied"], "refreshed": rep["refreshed"],
           "unchanged": len(unchanged), "bytes": rep["bytes"],
           "errors": len(rep["errors"])}
    try:
        with open(_state_path(dest, "runs.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(run) + "\n")
    except OSError:
        pass  # trail trouble never fails the backup it describes
    return rep


def _flag(args, name, has_value=False):
    if name not in args:
        return None if has_value else False
    i = args.index(name)
    args.pop(i)
    if has_value:
        return args.pop(i) if i < len(args) else None
    return True


def cmd_corpus(args):
    """corpus backup [--dry] [--dest DIR] | corpus status [--dest DIR] —
    training-corpus transcript backup: copy-only incremental archive of every
    session/subagent/workflow transcript (claude + codex + /tmp estate) into
    dated dirs under HELM_CORPUS_DEST (default ~/corpus-archive). Sources are
    NEVER moved or deleted; re-runs skip unchanged files by (size, mtime_ns).
    A daily systemd --user timer ships in scripts/ (see VERBS.md)."""
    args = list(args or [])
    dest = dest_root(_flag(args, "--dest", has_value=True))
    dry = bool(_flag(args, "--dry"))
    sub = args[0] if args else None
    if sub == "status":
        from .cli import guard_tail
        rc = guard_tail("helm corpus status", args[1:],
                        usage="corpus status [--dest DIR]")
        if rc is not None:
            return rc
        rows = scan()
        man = load_manifest(dest)
        pending, unchanged = plan(rows, man)
        live = {r["p"] for r in rows}
        retired = sum(1 for p in man if p not in live)
        print("helm corpus status — dest %s" % dest)
        for label, (n, b) in sorted(_by_label(rows).items()):
            print("  %-7s %5d transcripts  %s" % (label, n, _human(b)))
        pct = 100.0 * len(unchanged) / len(rows) if rows else 100.0
        print("  archived: %d of %d (%.1f%%) · pending %d (%s) · %d retired "
              "sources kept in archive"
              % (len(unchanged), len(rows), pct, len(pending),
                 _human(sum(r["sz"] for r in pending)), retired))
        last = _last_run(dest)
        print("  last run: " + ("%(ts)s — copied %(copied)d, refreshed %(refreshed)d, "
                                "%(errors)d errors" % last if last
                                else "never (enable scripts/helm-corpus.timer, or run "
                                     "`helm corpus backup`)"))
        return 0
    if sub != "backup" or args[1:]:
        print("usage: helm corpus backup [--dry] [--dest DIR] | corpus status",
              file=sys.stderr)
        return 2
    rep = backup(dest, dry=dry)
    mode = "dry-run — reporting only" if dry else "copying"
    print("helm corpus backup — %d transcripts on the estate (%s) -> %s"
          % (len(rep["rows"]), mode, dest))
    for label, (n, b) in sorted(_by_label(rep["rows"]).items()):
        pn, pb = _by_label(rep["pending"]).get(label, (0, 0))
        print("  %-7s %5d transcripts  %s — %d new/changed (%s)"
              % (label, n, _human(b), pn, _human(pb)))
    if rep["aborted"]:
        print("helm corpus: ABORTED before any copy — " + rep["aborted"])
        return 1
    if dry:
        print("helm corpus: %d would be copied (%s) into %s/%s — nothing copied (--dry)"
              % (len(rep["pending"]), _human(rep["bytes"]), dest, rep["date"]))
        return 0
    for e in rep["errors"]:
        print("  ERR " + e)
    summary = "copied %d (%s), refreshed %d, %d unchanged, %d errors -> %s/%s" % (
        rep["copied"], _human(rep["bytes"]), rep["refreshed"],
        len(rep["unchanged"]), len(rep["errors"]), dest, rep["date"])
    if rep["copied"] or rep["refreshed"] or rep["errors"]:
        from . import pk
        pk.event("corpus", "backup", summary)
    print("helm corpus: " + summary)
    return 1 if rep["errors"] else 0
