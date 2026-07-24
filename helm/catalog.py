#!/usr/bin/env python3
"""Session catalog — one row per local claude/codex session.

Incremental: rows are cached per file keyed by (mtime, size); a refresh only
re-reads files that changed, so after the first full build a rescan is seconds.
Row ids are FULL session ids (claude uuid / codex rollout uuid) — they must be
resumable, not just displayable.
"""
import json, os, glob, re, subprocess, sys, time

HOME = os.path.expanduser("~")
CACHE_DIR = os.path.join(HOME, ".cache", "helm")
CACHE = os.path.join(CACHE_DIR, "catalog-cache.json")

# extra transcript roots (colon-separated) via env — machine-local paths never live in code
# ~/.claude-homes/<acct>/projects is scanned too: usually a symlink back to
# ~/.claude/projects (inode dedup folds it) but a REAL per-account store must
# still count — the roster gc trusts this catalog as its transcript truth.
CLAUDE_ROOTS = [f"{HOME}/.claude/projects", f"{HOME}/.claude-homes/*/projects"] + \
    [r for r in os.environ.get("HELM_CLAUDE_ROOTS", "").split(":") if r]
CODEX_ROOTS = [f"{HOME}/.codex/sessions", f"{HOME}/.codex-homes"] + \
    [r for r in os.environ.get("HELM_CODEX_ROOTS", "").split(":") if r]


def _files():
    """(path, harness) for every transcript, symlink-deduped by inode."""
    seen, out = set(), []
    for r in CLAUDE_ROOTS:
        for f in glob.glob(f"{r}/*/*.jsonl"):
            if ".flat." in f:
                continue
            try:
                st = os.stat(f)
                key = (st.st_dev, st.st_ino)  # inode numbers are per-DEVICE
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append((f, "claude"))
    for r in CODEX_ROOTS:
        for f in glob.glob(f"{r}/**/rollout-*.jsonl", recursive=True):
            try:
                st = os.stat(f)
                key = (st.st_dev, st.st_ino)
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append((f, "codex"))
    return out


def _first_user_text(msg):
    c = msg.get("content", "")
    if isinstance(c, list):
        c = " ".join(b.get("text", "") for b in c
                     if isinstance(b, dict) and b.get("type") == "text")
    return str(c).strip()


def _scan_claude(f):
    cwd = branch = title = created = ""
    n = 0
    try:
        with open(f, errors="ignore") as fh:
            for line in fh:
                n += 1
                if n > 80:
                    break
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                cwd = cwd or r.get("cwd", "")
                branch = branch or r.get("gitBranch", "")
                created = created or r.get("timestamp", "")
                if not title:
                    m = r.get("message", {})
                    if isinstance(m, dict) and m.get("role") == "user":
                        t = _first_user_text(m)
                        if t and not t.startswith(("<", "Caveat:", "[", "{")) \
                           and "tool_result" not in t[:40] and "task-notification" not in t[:40] \
                           and "Stop hook" not in t[:20] and "command-name" not in t[:40] \
                           and "system-reminder" not in t[:40]:
                            title = t[:120]
                if title and cwd:
                    break
    except OSError:
        pass
    return cwd, branch, title, created


def _scan_codex(f):
    cwd = title = created = ""
    n = 0
    try:
        with open(f, errors="ignore") as fh:
            for line in fh:
                n += 1
                if n > 60:
                    break
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not cwd:
                    m = re.search(r'"cwd"\s*:\s*"([^"]+)"', line)
                    if m:
                        cwd = m.group(1)
                created = created or r.get("timestamp", "") or r.get("ts", "")
                if not title:
                    for key in ("text", "content", "instructions"):
                        v = r.get(key)
                        if isinstance(v, str) and 8 < len(v.strip()) < 400 \
                           and not v.startswith(("<", "{")):
                            title = v.strip()[:120]
                            break
                if title and cwd:
                    break
    except OSError:
        pass
    return cwd, "", title, created


def _count_lines(f):
    n = 0
    try:
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                n += chunk.count(b"\n")
    except OSError:
        pass
    return n


def _session_id(path, harness):
    base = re.sub(r"\.jsonl$", "", os.path.basename(path))
    if harness == "codex":
        # rollout-2026-07-11T15-30-00-<uuid>  ->  <uuid>. The timestamp is
        # anchored EXACTLY: a greedy [\d-]* ate into the uuid whenever its
        # first group was all digits, minting an unresumable truncated id.
        base = re.sub(r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-", "", base)
    return base


def _short(p):
    return p.replace(HOME, "~") if p else "(unknown)"


# synthetic/reference sessions (daily-memory summarizers etc.) — kept fully in the
# corpus (training data + searchable), but flagged so RESUME search doesn't drown in
# them and the UI can collapse them. Detection is title-signature based, not cwd.
_SYNTHETIC_TITLE_PREFIXES = (
    "You are summarizing a Claude Code session",
    "You are a daily memory",
)


def _classify(row):
    """Add `syn` (synthetic/reference) and `xl` (oversized single/few-message —
    resume-risk: content likely exceeds the context window and can't compact)."""
    t = row.get("t", "")
    row["syn"] = any(t.startswith(pre) for pre in _SYNTHETIC_TITLE_PREFIXES)
    # oversize/resume-risk = a giant payload concentrated in very few units (one
    # giant prompt: ~205k+ tokens that won't resume or compact). Keyed on
    # bytes-PER-UNIT so it's correct whether `m` is cv's true message count
    # (primary path) or the scanner's line count (fallback): a giant-single-prompt
    # session averages >300KB/unit; a normal many-message/many-line session is KBs.
    z, m = row.get("z", 0), row.get("m", 0)
    row["xl"] = z > 1_500_000 and (z / max(1, m)) > 300_000
    return row


def _row(path, harness, st):
    cwd, branch, title, created = (_scan_claude(path) if harness == "claude"
                                   else _scan_codex(path))
    return _classify({
        "h": harness, "i": _session_id(path, harness), "c": _short(cwd),
        "b": branch, "t": title or "(untitled)", "z": st.st_size,
        "m": _count_lines(path), "cr": created[:10] if created else "",
        "u": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)),
        "mt": int(st.st_mtime), "p": path, "cwd": cwd,
    })


def _load_cache():
    try:
        with open(CACHE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _epoch(iso):
    """ISO-8601 (with or without tz) -> unix seconds int, else 0."""
    if not iso:
        return 0
    import calendar
    s = str(iso)
    try:
        t = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 0
    return int(calendar.timegm(t) if ("Z" in s[19:] or "+00:00" in s[19:]) else time.mktime(t))


def _row_from_cv(o):
    """One `cv ls --json` object -> a catalog row (same schema the scanner emits)."""
    cwd = o.get("cwd") or ""
    updated, created = str(o.get("updatedAt") or ""), str(o.get("createdAt") or "")
    mt = _epoch(updated)
    git = o.get("git") if isinstance(o.get("git"), dict) else {}
    return _classify({
        "h": o.get("harness") or "?", "i": o.get("id") or "", "c": _short(cwd),
        "b": git.get("branch", "") or "", "t": o.get("title") or "(untitled)",
        "z": o.get("sizeBytes") or 0, "m": o.get("messageCount") or 0,
        "cr": created[:10],
        "u": updated[:10] or (time.strftime("%Y-%m-%d", time.localtime(mt)) if mt else ""),
        "mt": mt, "p": o.get("path") or "", "cwd": cwd,
    })


SYN_CACHE = os.path.join(CACHE_DIR, "syn-cache.json")


_SYN_PREFIX_BYTES = tuple(p.encode()[:24] for p in _SYNTHETIC_TITLE_PREFIXES)


def _peek_synthetic(path):
    """True iff the session's FIRST message is a synthetic/summarizer prompt. cv
    leaves these UNTITLED (it filters the system-y prompt), so recover the signal by
    peeking — but only the first ~8KB (the summarizer prompt IS the first line, so
    no need to parse the whole file). Byte-substring test avoids a full JSON parse."""
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    return any(pre in head for pre in _SYN_PREFIX_BYTES)


def _backfill_syn(rows):
    """Re-detect `syn` for cv rows (whose empty title breaks the prefix match), by a
    cached first-bytes peek of UNTITLED rows only, PARALLELIZED (I/O-bound). Cache
    keyed by (path, mtime), so the corpus is peeked once, not every build."""
    import concurrent.futures
    try:
        with open(SYN_CACHE) as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}
    todo, fresh = [], {}
    for r in rows:
        title = r.get("t") or ""
        if title and title != "(untitled)":
            continue  # cv extracted a real first-user message → not a summarizer
        p, mt = r.get("p") or "", r.get("mt", 0)
        ent = cache.get(p)
        if ent and ent[0] == mt:
            r["syn"] = ent[1]
            fresh[p] = ent
        else:
            todo.append(r)
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
            for r, syn in zip(todo, ex.map(lambda r: _peek_synthetic(r.get("p") or ""), todo)):
                r["syn"] = syn
                fresh[r.get("p") or ""] = [r.get("mt", 0), syn]
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{SYN_CACHE}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(fresh, fh)
        os.replace(tmp, SYN_CACHE)
    except OSError:
        pass
    return rows


def _build_from_cv():
    """Primary path: `cv ls --json` IS the catalog (cv 0.10+ carries sizeBytes +
    synthesized titles + true messageCount — the fields that let the predecessor's transitional
    scanner retire). Returns (rows, stats) or None if cv can't answer (→ scanner
    fallback). The one remaining gap vs the scanner is git branch, which cv doesn't
    emit yet (emberian/cv#15) — non-load-bearing, blank until it lands."""
    if os.environ.get("HELM_CATALOG") == "scanner":
        return None
    import subprocess
    try:
        from . import home
        p = subprocess.run(["cv", "ls", "--json", "--limit", "1000000"],
                           capture_output=True, text=True, timeout=90,
                           env=home.cv_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0 or not p.stdout.strip().startswith("["):
        return None
    try:
        objs = json.loads(p.stdout)
    except ValueError:
        return None
    # the catalog covers claude + codex (the harnesses helm mints resume commands for and
    # tracks quota for) — matching the scanner's scope, so retiring the scanner is a
    # behavior-preserving swap. cv also lists hermes/grok/gemini/… (and reports a
    # shared state.db size for some, e.g. hermes) — out of the predecessor's catalog scope.
    rows = [_row_from_cv(o) for o in objs
            if isinstance(o, dict) and o.get("id") and o.get("harness") in ("claude", "codex")]
    _backfill_syn(rows)  # recover synthetic-session detection (cv leaves them untitled)
    rows.sort(key=lambda r: r["u"], reverse=True)
    return rows, {"source": "cv ls --json", "files": len(rows), "rescanned": 0, "rows": len(rows)}


def build(progress=None):
    """Return (rows, stats). cv ls --json is the authority (cv 0.10+); the built-in
    scanner is the degraded-mode fallback for machines without a capable cv."""
    cv_result = _build_from_cv()
    if cv_result is not None:
        return cv_result
    cache = _load_cache()
    fresh, rows, changed = {}, [], 0
    files = _files()
    for idx, (path, harness) in enumerate(files):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size < 200:
            continue
        ent = cache.get(path)
        if ent and ent.get("mt") == int(st.st_mtime) and ent.get("sz") == st.st_size \
                and ent.get("mtns", st.st_mtime_ns) == st.st_mtime_ns:
            row = ent["row"]
            if "mt" not in row:  # rows cached before mt became a row field
                row["mt"] = ent["mt"]
            if "syn" not in row:  # backfill classification onto pre-existing cache
                _classify(row)
            sid = _session_id(path, harness)
            if row.get("i") != sid:  # repair ids cached by the greedy pre-fix regex
                row["i"] = sid
        else:
            row = _row(path, harness, st)
            changed += 1
        fresh[path] = {"mt": int(st.st_mtime), "mtns": st.st_mtime_ns, "sz": st.st_size, "row": row}
        rows.append(row)
        if progress and idx % 2000 == 0:
            progress(idx, len(files), changed)
    rows.sort(key=lambda r: r["u"], reverse=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = f"{CACHE}.{os.getpid()}.tmp"  # unique per writer; atomic replace
    with open(tmp, "w") as fh:
        json.dump(fresh, fh)
    os.replace(tmp, CACHE)
    return rows, {"files": len(files), "rescanned": changed, "rows": len(rows)}


def seed_from(catalog_json):
    """One-time cache seed from a prior full-scan catalog (old row schema ok).
    Derives FULL session ids from paths; only stats files, no content reads."""
    with open(catalog_json) as fh:
        old = json.load(fh)
    cache = {}
    for r in old:
        path = os.path.expanduser(r["path"]) if r["path"].startswith("~") else r["path"]
        try:
            st = os.stat(path)
        except OSError:
            continue
        harness = r["harness"]
        cache[path] = {"mt": int(st.st_mtime), "sz": st.st_size, "row": {
            "h": harness, "i": _session_id(path, harness), "c": r["cwd"],
            "b": r.get("branch", ""), "t": r["title"], "z": r["bytes"],
            "m": r["msgs"], "cr": r.get("created", ""), "u": r["updated"],
            "p": path, "cwd": os.path.expanduser(r["cwd"]) if r["cwd"].startswith("~") else r["cwd"],
        }}
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE, "w") as fh:
        json.dump(cache, fh)
    return len(cache)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "seed":
        print("seeded", seed_from(sys.argv[2]), "rows into", CACHE, file=sys.stderr)
    else:
        t0 = time.time()
        rows, stats = build(progress=lambda i, n, c: print(f"  {i}/{n} ({c} rescanned)", file=sys.stderr))
        print(f"{stats} in {time.time()-t0:.1f}s", file=sys.stderr)
