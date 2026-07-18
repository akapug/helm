#!/usr/bin/env python3
"""helm transcripts — reading INSIDE sessions, and the two safe mutations.
ABSORBED from the predecessor (sesh/server/sesh.py), behavior-preserving, per
the dissolve-into-helm law (see ATTRIBUTION.md).

Four capabilities, one module:
  * deep_search    — content search inside transcripts (scoped grep or cv
                     full-text, work-vs-synthetic classified)
  * get_session    — a readable window of one transcript, newest-last;
                     find=<term> centers the window on the last match
  * cwd_override   — re-home a session's cwd (metadata, never identity); for
                     claude, symlink the session file into the new cwd's
                     project-slug dir so `claude --resume` resolves there
  * prune_session  — the resume studio: derive a NEW smaller still-resumable
                     COPY via cv prune; the original is never mutated
plus make_cmd (the pasteable account-aware resume command) because the same
line range owns it and the web slice's /api/cmd needs it.

Signatures are IDENTICAL to the source module — the slice-B web routes call
these directly. State lives under ~/.cache/helm/ (catalog.CACHE_DIR seeds
itself once from the legacy ~/.cache/sesh/, so cwd-overrides and mints carry
over). Thread-safe: the same single-flight cache the server used, so the CLI
and an embedding web server share one code path.
"""
import json
import os
import shlex
import sys
import threading
import time

from . import catalog
from .providers import ProviderError

HOME = os.path.expanduser("~")
OVERRIDES_PATH = os.path.join(catalog.CACHE_DIR, "cwd-overrides.json")
MINTS_PATH = os.path.join(catalog.CACHE_DIR, "mints.jsonl")
# claude --resume resolves sessions from per-cwd project-slug dirs under here
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")

_lock = threading.Lock()
_state = {}
_inflight = {}

_PROVIDER = None  # lazy singleton; only make_cmd pays for it


def _provider():
    global _PROVIDER
    if _PROVIDER is None:
        from . import providers
        _PROVIDER = providers.default_provider()
    return _PROVIDER


def _cached(key, ttl, fn):
    """Single-flight: concurrent misses on one key share one build instead of
    racing (two catalog builds interleaving one temp file = corrupt cache)."""
    while True:
        with _lock:
            ent = _state.get(key)
            if ent and time.time() - ent[0] < ttl:
                return ent[1]
            ev = _inflight.get(key)
            if ev is None:
                _inflight[key] = threading.Event()
                break
        ev.wait(timeout=300)
    try:
        val = fn()  # computed outside the lock; other keys stay readable
        with _lock:
            _state[key] = (time.time(), val)
        return val
    finally:
        with _lock:
            _inflight.pop(key).set()


# ---------------------------------------------------------------- cwd overrides

def _load_cwd_overrides():
    try:
        with open(OVERRIDES_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# always re-read before use/mutation: the CLI and a running web server are both
# writers of this file — a snapshot-at-import would go stale and clobber.
_cwd_overrides = _load_cwd_overrides()


def _refresh_cwd_overrides():
    global _cwd_overrides
    _cwd_overrides = _load_cwd_overrides()
    return _cwd_overrides


def _save_cwd_overrides():
    import fcntl
    os.makedirs(os.path.dirname(OVERRIDES_PATH), exist_ok=True)
    lockp = OVERRIDES_PATH + ".lock"
    with open(lockp, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)  # CLI and server are both writers
        tmp = f"{OVERRIDES_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
        json.dump(_cwd_overrides, open(tmp, "w"))
        os.replace(tmp, OVERRIDES_PATH)


def get_catalog(refresh=False):
    def build():
        rows, stats = catalog.build()
        return {"rows": rows, "stats": stats, "scanned_at": time.time()}
    if refresh:
        with _lock:
            _state.pop("catalog", None)
    cat = _cached("catalog", 10**9, build)
    # cwd overrides: applied at read time (never baked into the cache) so they
    # survive rescans; cwd is metadata, never identity.
    with _lock:
        ov = dict(_refresh_cwd_overrides())
    if not ov:
        return cat
    rows = [dict(r, cwd=ov[r["i"]], c=catalog._short(ov[r["i"]]), cwdOverride=True)
            if r["i"] in ov else r for r in cat["rows"]]
    return {**cat, "rows": rows}


def _resolve_sid(sid):
    """Exact id, else exactly-one prefix match. Empty or ambiguous input is an error —
    a mutation must never land on 'the first row that happened to match'."""
    sid = (sid or "").strip()
    if len(sid) < 6:
        return None, {"error": "need a session id (or a unique prefix, 6+ chars)"}
    rows = get_catalog()["rows"]
    exact = next((r for r in rows if r["i"] == sid), None)
    if exact:
        return exact, None
    pref = [r for r in rows if r["i"].startswith(sid)]
    if len(pref) == 1:
        return pref[0], None
    if len(pref) > 1:
        return None, {"error": f"ambiguous prefix {sid} matches {len(pref)} sessions"}
    return None, {"error": f"unknown session {sid} (not in catalog)"}


# ------------------------------------------------------------------ deep search

def _grep_sessions(query, rows, cap=60):
    """Direct grep over specific session transcript files — ranking-free and instant
    for a scoped set. Returns hits with a centered snippet. Used for project-scoped
    content search, where cv's global ranking is drowned by synthetic transcripts."""
    import subprocess as _sp, os as _os
    hits = []
    paths = [r["p"] for r in rows if r.get("p") and _os.path.exists(r["p"])]
    by_path = {r["p"]: r for r in rows}
    if not paths:
        return hits
    try:
        # -l first for the file list (fast), then pull one matching line per file
        out = _sp.run(["grep", "-rilF", query, *paths], capture_output=True,
                      text=True, timeout=60).stdout
    except (_sp.TimeoutExpired, OSError):
        return hits
    for path in out.splitlines():
        r = by_path.get(path)
        if not r:
            continue
        snip = ""
        try:
            line = _sp.run(["grep", "-iF", "-m1", query, path], capture_output=True,
                           text=True, timeout=10).stdout.strip()
            i = line.lower().find(query.lower())
            if i >= 0:
                snip = line[max(0, i - 90): i + 120]
        except (_sp.TimeoutExpired, OSError):
            pass
        hits.append({"harness": r["h"], "id8": r["i"][:8], "date": r["u"],
                     "title": r["t"][:120], "snippet": snip or "(match in transcript)",
                     "sid": r["i"], "cvid": r["i"], "syn": r.get("syn", False)})
        if len(hits) >= cap:
            break
    return hits


def deep_search(query, limit=40, scope=None, include_synthetic=False):
    """Content search. scope=<cwd substring> greps that project's real transcripts
    directly (ranking-free, surfaces work sessions the synthetic flood would bury).
    Unscoped: cv full-text, classified into work vs synthetic so the UI shows work
    first and collapses reference transcripts (kept in corpus as training data)."""
    import re as _re, subprocess as _sp
    rows_all = get_catalog()["rows"]
    if scope:
        scoped = [r for r in rows_all
                  if scope.lower() in (r.get("c", "") + " " + r.get("cwd", "")).lower()
                  and (include_synthetic or not r.get("syn"))]
        hits = _grep_sessions(query, scoped, cap=limit)
        return {"hits": hits, "source": f"scoped-grep ({len(scoped)} sessions in {scope})",
                "scope": scope}

    def _split(hits):
        by = {r["i"]: r for r in rows_all}
        by8 = {}
        for r in rows_all:
            by8.setdefault(r["i"][:8], r["i"])
        work, syn = [], 0
        for h in hits:
            sid = h.get("sid") or by8.get(h.get("id8"))
            r = by.get(sid) if sid else None
            if r and r.get("syn"):
                syn += 1
                if not include_synthetic:
                    continue
            h["syn"] = bool(r and r.get("syn"))
            work.append(h)
        return work, syn
    try:
        p = _sp.run(["cv", "search", query, "--json", "--limit", str(limit)],
                    capture_output=True, text=True, timeout=45)
        if p.returncode == 0 and p.stdout.strip().startswith("["):
            hits = [{"harness": h.get("harness", "?"), "id8": (h.get("id") or "")[:8],
                     "date": (h.get("updatedAt") or "")[:10], "title": (h.get("title") or "")[:120],
                     "snippet": (h.get("snippet") or "")[:300], "sid_full": h.get("id")}
                    for h in json.loads(p.stdout)]
            rows = get_catalog()["rows"]
            have = {r["i"] for r in rows}
            for h in hits:
                full = h.pop("sid_full")
                h["cvid"] = full  # always the full id when cv --json provided it
                h["sid"] = full if full in have else None
            work, syn = _split(hits)
            return {"hits": work[:limit], "synthetic_hidden": syn, "source": "cv --json"}
    except FileNotFoundError:
        return {"error": "cv not installed — deep search needs clustervision on PATH"}
    except (_sp.TimeoutExpired, ValueError):
        pass  # fall through to the table parse
    try:
        p = _sp.run(["cv", "search", query, "--limit", str(limit)],
                    capture_output=True, text=True, timeout=60)
    except _sp.TimeoutExpired:
        return {"error": "cv search timed out"}
    if p.returncode != 0:
        return {"error": f"cv search failed: {(p.stderr or '').strip()[:200]}"}
    hits, cur = [], None
    head = _re.compile(r"^(\w+)\s+([0-9a-f]{8})\s+(\d{4}-\d\d-\d\d)\s+(.*)$")
    for line in p.stdout.splitlines():
        m = head.match(line)
        if m:
            cur = {"harness": m.group(1), "id8": m.group(2), "date": m.group(3),
                   "title": m.group(4).strip(), "snippet": ""}
            hits.append(cur)
        elif cur is not None and line.strip():
            cur["snippet"] = (cur["snippet"] + " " + line.strip()).strip()[:300]
    # join to catalog rows so hits become resumable
    rows = get_catalog()["rows"]
    by8 = {}
    for r in rows:
        by8.setdefault(r["i"][:8], r["i"])
    for h in hits:
        h["sid"] = by8.get(h["id8"])
        h["cvid"] = None  # text-parse shim has no full id
    work, syn = _split(hits)
    return {"hits": work[:limit], "synthetic_hidden": syn}


# ------------------------------------------------------------- transcript reads

def _cv_show(sid, rng=None, harness=None):
    import subprocess as _sp
    args = ["cv", "show", sid, "--json"] + (["--range", rng] if rng else []) \
        + (["--harness", harness] if harness else [])
    p = _sp.run(args, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise ProviderError(f"cv show failed: {(p.stderr or '').strip()[:200]}")
    return json.loads(p.stdout)


def _session_total(sid, line_bound, harness=None):
    """True IR message count via bisect of 1-message windows (windowed reads are cheap;
    line count is a guaranteed upper bound). Cached per (sid, line_bound)."""
    key = f"total:{sid}:{line_bound}:{harness}"
    with _lock:
        if key in _state:
            return _state[key][1]
    lo, hi = 0, max(1, line_bound)  # invariant: total in (lo, hi]
    if not _cv_show(sid, f"{hi-1}-{hi}", harness=harness)["messages"]:
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if _cv_show(sid, f"{mid}-{mid+1}", harness=harness)["messages"]:
                lo = mid
            else:
                hi = mid
    total = hi
    with _lock:
        _state[key] = (time.time(), total)
    return total


def _simplify_msg(m):
    """One IR message -> display items (the same simplification the drawer renders)."""
    items = []
    def _flatten(c):
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(_flatten(x) for x in c if x)
        if isinstance(c, dict):
            return c.get("text") or _flatten(c.get("content")) or ""
        return "" if c is None else str(c)
    for b in m.get("content", []):
        k = b.get("kind")
        if k == "text" and (b.get("text") or "").strip():
            items.append({"k": "text", "t": b["text"]})
        elif k == "thinking" and (b.get("text") or "").strip():
            items.append({"k": "think", "t": b["text"]})
        elif k in ("tool_use", "toolUse"):
            inp = json.dumps(b.get("input", {}), ensure_ascii=False)
            items.append({"k": "tool", "name": b.get("name", "?"),
                          "t": inp[:400] + ("…" if len(inp) > 400 else "")})
        elif k in ("tool_result", "toolResult"):
            t = _flatten(b.get("content") if b.get("content") is not None else b.get("text")).strip()
            if t:
                items.append({"k": "result", "err": bool(b.get("is_error")),
                              "t": t[:1200] + ("…" if len(t) > 1200 else "")})
    return items


def _items_match(items, needle):
    """0 = no match, 1 = some terms, 2 = all terms — over what the drawer renders.
    FTS-style: a content hit may have the words scattered across messages, so the
    caller prefers an all-terms message but falls back to the best partial."""
    terms = [w for w in needle.split() if len(w) > 1] or [needle]
    text = " ".join((it.get("t") or "") + " " + (it.get("name") or "") for it in items).lower()
    n = sum(1 for t in terms if t in text)
    return 2 if n == len(terms) else (1 if n else 0)


FIND_WINDOW = 200      # messages per backward search window
FIND_MAX_WINDOWS = 8   # cap the walk at 1600 messages from the tail


def get_session(sid, before=None, limit=60, find=None, harness=None):
    """A readable window of a session's transcript, newest-last. Content is simplified
    for display; nothing is truncated on disk — 'load earlier' pages backward.
    find=<term>: backward search from the tail; the window comes back CENTERED on the
    last match, match.index pointing at it inside the returned messages array.
    Non-catalog sids (hermes/grok/other cv-visible harnesses) read via cv show
    directly — resumable: false in the session header."""
    rows = get_catalog()["rows"]
    row = next((r for r in rows if r["i"] == sid or r["i"].startswith(sid)), None)
    if row:
        sid = row["i"]
        line_bound = row["m"] or 1
    else:
        # not in helm's catalog but cv can still read it; no line-count bound,
        # so bisect from a generous ceiling (~16 probes, cached)
        line_bound = 50000
    match_abs = note = None
    try:
        total = _session_total(sid, line_bound, harness=None if row else harness)
        if find:
            needle = find.lower()
            wend = total
            for _ in range(FIND_MAX_WINDOWS):
                wstart = max(0, wend - FIND_WINDOW)
                wmsgs = _cv_show(sid, f"{wstart}-{wend}", harness=None if row else harness).get("messages", [])
                partial = None
                for j in range(len(wmsgs) - 1, -1, -1):
                    m = _items_match(_simplify_msg(wmsgs[j]), needle)
                    if m == 2:
                        match_abs = wstart + j
                        break
                    if m == 1 and partial is None:
                        partial = wstart + j
                if match_abs is None and partial is not None:
                    match_abs = partial  # best any-term message in the newest window that has one
                if match_abs is not None or wstart == 0:
                    break
                wend = wstart
            if match_abs is None:
                end = total
                start = max(0, end - limit)
                note = f"no match in the last {FIND_WINDOW * FIND_MAX_WINDOWS} messages"
            else:
                start = max(0, match_abs - limit // 2)
                end = min(total, start + limit)
        else:
            end = min(before if before is not None else total, total)
            start = max(0, end - limit)
        ir = _cv_show(sid, f"{start}-{end}", harness=None if row else harness)
    except ProviderError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    msgs, match = [], None
    for k, m in enumerate(ir.get("messages", [])):
        items = _simplify_msg(m)
        if items:
            if match_abs is not None and start + k == match_abs:
                match = {"index": len(msgs)}
            msgs.append({"role": m.get("role", "?"), "ts": m.get("timestamp"), "items": items})
    if row:
        session = {"id": sid, "title": ir.get("title") or row["t"], "harness": row["h"],
                   "cwd": ir.get("cwd") or row["cwd"], "model": ir.get("model"),
                   "branch": (ir.get("git") or {}).get("branch"), "resumable": True}
    else:
        session = {"id": ir.get("id") or sid, "title": ir.get("title") or "(untitled)",
                   "harness": ir.get("harness") or "?", "cwd": ir.get("cwd"),
                   "model": ir.get("model"),
                   "branch": (ir.get("git") or {}).get("branch"), "resumable": False}
    out = {"session": session, "total": total, "start": start, "end": end, "messages": msgs}
    if find:
        out["match"] = match
        if note:
            out["note"] = note
    return out


# ----------------------------------------------------------------------- rehome

def cwd_override(body):
    """Set or remove a session's cwd override (cwd is metadata, never identity).
    claude: also symlink the session file into the new cwd's project-slug dir so
    `claude --resume` resolves there. codex: resume is global-by-UUID — no link,
    the override only drives the cd prefix on the command."""
    row, err = _resolve_sid((body or {}).get("sid"))
    if err:
        return err
    sid = row["i"]
    if "cwd" not in body:
        return {"error": "need cwd: absolute path to set, null to remove"}
    cwd = body["cwd"]
    if cwd is None:
        with _lock:
            _refresh_cwd_overrides()
            had = _cwd_overrides.pop(sid, None) is not None
            _save_cwd_overrides()
        note = ("override removed; any project-slug symlink is left in place (harmless)"
                if had else "no override was set for this session")
        return {"ok": True, "cwd": None, "linked": False, "note": note}
    cwd = os.path.expanduser(str(cwd))
    if not os.path.isabs(cwd):
        return {"error": f"cwd must be an absolute path: {cwd}"}
    if not os.path.isdir(cwd):
        return {"error": f"not an existing directory: {cwd}"}
    cwd = cwd.rstrip("/") or "/"
    linked, note = False, ""
    if row["h"] == "claude":
        # claude --resume resolves a session only from a cwd whose project-slug dir
        # contains the session file; slug = abs path with / and . replaced by -
        slug = cwd.replace("/", "-").replace(".", "-")
        proj = os.path.join(CLAUDE_PROJECTS, slug)
        target = os.path.join(proj, f"{sid}.jsonl")
        real = row["p"]
        if os.path.exists(target) and os.path.realpath(target) == os.path.realpath(real):
            linked = True
            note = ("session file already resolvable from that cwd"
                    + (" (existing symlink)" if os.path.islink(target) else ""))
        elif os.path.islink(target):
            os.remove(target)  # stale link to some other file — safe to re-point
            os.symlink(real, target)
            linked = True
            note = f"re-pointed existing symlink in {slug} at this session's file"
        elif os.path.exists(target):
            return {"error": f"a real file already exists at {target} — refusing to clobber"}
        else:
            os.makedirs(proj, exist_ok=True)
            os.symlink(real, target)
            linked = True
            note = f"symlinked into project slug {slug} so `claude --resume` resolves from the new cwd"
        if linked and not os.path.exists(target):
            return {"error": f"symlink at {target} does not resolve — real file missing?"}
    else:
        note = "codex resume is global-by-UUID (cwd-free); override drives the cd prefix only"
    with _lock:
        _refresh_cwd_overrides()
        _cwd_overrides[sid] = cwd
        _save_cwd_overrides()
    return {"ok": True, "cwd": cwd, "linked": linked, "note": note}


# --------------------------------------------------------------- resume command

def _log_mint(row, account, model):
    """Seat→home attribution log: one append-only jsonl row per minted resume
    command — the forward-going source for 'which session ran under which
    account/home'. Best-effort by design (a logging failure must never break
    command minting); token-free by construction (identity metadata only)."""
    try:
        home = None
        if account != "(default)":
            home = next((a.get("home") for a in _provider().accounts()
                         if a.get("name") == account), None)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "sid": row["i"], "harness": row["h"], "account": account,
               "home": home or None, "model": model or None,
               "cwd": row.get("cwd") or None}
        os.makedirs(catalog.CACHE_DIR, exist_ok=True)
        with open(MINTS_PATH, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _native_cmd(row, model=None):
    """The harness's own resume invocation with no provider in the loop —
    helm degrades to a single-account tool instead of a broken one."""
    if row["h"] == "claude":
        return "claude" + (f" --model {shlex.quote(model)}" if model else "") + f" --resume {row['i']}"
    return f"codex resume {row['i']}"


def make_cmd(account, sid, model=None):
    """The pasteable command. The provider owns the cred/home/model half; the
    catalog's recorded true-cwd supplies the claude cd-prefix."""
    row, err = _resolve_sid(sid)
    if err:
        return err
    sid = row["i"]
    fallback_note = None
    if account == "(default)":
        out, fallback_note = _native_cmd(row, model), \
            "no quota provider — command runs under this machine's default account"
    else:
        try:
            out = _provider().launch_cmd(account, sid, model)
        except ProviderError as e:
            out = _native_cmd(row, model)
            fallback_note = f"quota provider unavailable ({e}) — falling back to the default account"
    cmd, warn = out, []
    if row.get("xl"):
        mb = row.get("z", 0) / 1e6
        warn.append(f"OVERSIZED: this is a single/few-message session (~{mb:.0f}MB in ~{row.get('m',0)} lines) "
                    f"— its content likely exceeds the 200k context limit and a single exchange cannot be "
                    f"compacted, so plain resume will fail. This is common for daily-memory/summarizer sessions.")
    elif row.get("syn"):
        warn.append("REFERENCE session (daily-memory summarizer) — meant as a read/training artifact, "
                    "not a resumable work session.")
    if row["h"] == "claude":
        cwd = row.get("cwd") or ""
        if cwd and os.path.isdir(cwd):
            cmd = f"cd {shlex.quote(cwd)} && {out}"
        elif cwd:
            warn.append(f"recorded cwd no longer exists: {cwd} (resume from any dir may fork a fresh session)")
        else:
            warn.append("session records no cwd; claude resume is cwd-scoped — command may not resolve")
    elif row.get("cwdOverride"):
        # codex runs fine from anywhere, but an overridden session should open
        # its terminal where the user chose
        cwd = row.get("cwd") or ""
        if cwd and os.path.isdir(cwd):
            cmd = f"cd {shlex.quote(cwd)} && {out}"
        else:
            warn.append(f"override cwd no longer exists: {cwd}")
    if fallback_note:
        warn.append(fallback_note)
    _log_mint(row, account, model)  # command built — log the seat→home mint (best-effort)
    agent = "claude" if row["h"] == "claude" else "codex"
    if account == "(default)":
        return {"cmd": cmd, "preflight": {"preflight_error": "no provider — resumability unverified"},
                "warnings": warn,
                "session": {"id": sid, "title": row["t"], "harness": row["h"], "cwd": row["c"]}}
    try:
        pf = _provider().preflight(account, sid, agent)
        if not pf:
            pf = {"preflight_error": "preflight returned nothing — treat as UNVERIFIED, not clear"}
    except ProviderError as e:
        pf = {"preflight_error": f"preflight unavailable ({e}) — treat as UNVERIFIED, not clear"}
    return {"cmd": cmd, "preflight": pf, "warnings": warn,
            "session": {"id": sid, "title": row["t"], "harness": row["h"], "cwd": row["c"]}}


# ------------------------------------------------------------------------ prune

PRUNE_PRESETS = {
    # preset -> (cv prune flags, one line on what it drops)
    "lean": (["--thinking"],
             "snips tool payloads >2KB into a retrievable sidecar ([PRUNED] markers, tool pairs intact)"
             " + flattens old thinking; last 25 turns stay verbatim; lossless"),
    "window20k": (["--window", "20000"],
                  "keeps only the newest turns totalling ~20k REAL tokens (Claude's recorded usage,"
                  " not estimates) + sidecar-snips bulk; lossy copy — the source keeps full history"),
}


def _cv_prune_help():
    """The installed cv prune's help text (cached) — feature-detects flags so presets
    self-adapt across cv upgrades instead of assuming today's surface."""
    import subprocess as _sp
    def build():
        try:
            p = _sp.run(["cv", "prune", "--help"], capture_output=True, text=True, timeout=15)
            return (p.stdout or "") + (p.stderr or "")
        except Exception:
            return ""
    return _cached("prune-help", 3600, build)


def prune_session(sid, preset="lean", dry=False, tokens=None):
    """Resume studio: derive a NEW, smaller, still-resumable session via cv prune.
    cv is the transformation engine (sidecar snipping, [PRUNED] markers, tool_use/
    tool_result pairs intact, revive-by-default so the resume gate passes); helm only
    orchestrates and never mutates the original. claude-only today (cv's limit)."""
    import re as _re, subprocess as _sp
    row, err = _resolve_sid(sid)
    if err:
        return err
    sid = row["i"]
    if row["h"] != "claude":
        return {"error": f"cv prune supports claude sessions only (this is {row['h']})"}
    if preset == "window" and tokens:
        try:
            tokens = max(2000, min(180000, int(tokens)))
        except (TypeError, ValueError):
            return {"error": "tokens must be an integer"}
        flags, note = ["--window", str(tokens)], \
            f"keeps the newest ~{tokens:,} real tokens; older turns drop (originals untouched)"
    elif preset in PRUNE_PRESETS:
        flags, note = PRUNE_PRESETS[preset]
    else:
        return {"error": f"unknown preset {preset!r} (lean | window20k | window+tokens)"}
    missing = [f for f in flags if f.startswith("--") and f not in _cv_prune_help()]
    if missing:
        return {"error": f"preset unavailable in installed cv (needs {' '.join(missing)})"}
    cmd = ["cv", "prune", sid] + flags
    if dry:
        return {"dry": True, "sid": sid, "preset": preset,
                "estimate": {"beforeBytes": row["z"], "beforeMsgs": row["m"], "note": note},
                "willRun": shlex.join(cmd)}
    try:
        before = os.path.getsize(row["p"])
    except OSError:
        before = row["z"]
    try:
        p = _sp.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return {"error": "cv not installed — prune needs clustervision on PATH"}
    except _sp.TimeoutExpired:
        return {"error": "cv prune timed out (300s)"}
    report = (p.stdout or "") + (p.stderr or "")  # cv prune reports on stderr (0.9.x quirk)
    if p.returncode != 0:
        return {"error": f"cv prune failed: {report.strip()[-400:]}"}
    m = _re.search(r"resume with: claude --resume ([0-9a-f-]{36})", report) \
        or _re.search(r"new session:\s+.*/([0-9a-f-]{36})\.jsonl", report)
    if not m:
        return {"error": "cv prune succeeded but reported no new session id: "
                         + report.strip()[-400:]}
    new_sid = m.group(1)
    path_m = _re.search(r"new session:\s+(\S+)", report)
    try:
        after = os.path.getsize(path_m.group(1)) if path_m else None
    except OSError:
        after = None
    with _lock:
        _state.pop("catalog", None)  # the new session must appear on the next catalog read
    summary = "; ".join(l.strip().lstrip("✦ ").strip() for l in report.splitlines()
                        if l.strip() and not l.strip().startswith(("new session:", "sidecar:",
                                                                   "resume with:")))
    return {"ok": True, "newSid": new_sid, "preset": preset,
            "beforeBytes": before, "afterBytes": after, "note": summary or note}


# ------------------------------------------------------------------- CLI verbs

def _hhmm(ts):
    if not ts:
        return "--:--"
    if isinstance(ts, (int, float)):
        return time.strftime("%H:%M", time.localtime(ts / (1000 if ts > 1e12 else 1)))
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return str(ts)[11:16] or "--:--"


def _trunc(s, n=200):
    s = " ".join(str(s).split())  # one line, collapsed whitespace
    return s if len(s) <= n else s[:n] + "…"


def _mb(n):
    if n is None:
        return "?"
    return f"{n/1048576:.1f}MB" if n >= 1048576 else f"{n/1024:.0f}KB"


def _flag(args, name, has_value=False):
    """Pop --name [value] from args; returns value (or True/None)."""
    if name not in args:
        return None if has_value else False
    i = args.index(name)
    args.pop(i)
    if has_value:
        return args.pop(i) if i < len(args) else None
    return True


def cmd_search(args):
    """search <text> [--scope P] [--refs] — content search inside transcripts.
    --scope P greps only sessions whose cwd contains P; --refs includes
    synthetic/reference (summarizer) transcripts."""
    args = list(args or [])
    scope = _flag(args, "--scope", has_value=True)
    refs = _flag(args, "--refs")
    limit = _flag(args, "--limit", has_value=True)
    if not args:
        print("usage: helm search <text> [--scope P] [--refs]", file=sys.stderr)
        return 2
    query = " ".join(args)
    res = deep_search(query, limit=int(limit) if limit else 40,
                      scope=scope, include_synthetic=bool(refs))
    if "error" in res:
        print("helm search: %s" % res["error"], file=sys.stderr)
        return 1
    hits = res.get("hits") or []
    src = res.get("source") or "cv"
    hidden = res.get("synthetic_hidden") or 0
    print("helm search: %d hit%s for %r (%s)%s" % (
        len(hits), "s"[:len(hits) != 1], query, src,
        "  [+%d reference hidden — --refs]" % hidden if hidden and not refs else ""))
    for h in hits:
        mark = "~" if h.get("syn") else " "
        print(" %s %-7s %-8s %-10s %s" % (mark, h["harness"], h["id8"], h["date"],
                                          h["title"][:70]))
        if h.get("snippet"):
            print("     %s" % _trunc(h["snippet"], 160))
    if hits:
        print("read one: helm transcript <id> --find <term>")
    return 0


def cmd_transcript(args):
    """transcript <sid> [--find T] [--limit N] [--harness H] — a readable,
    role-tagged window of one session, newest-last; --find centers on the last
    matching message (marked »)."""
    args = list(args or [])
    find = _flag(args, "--find", has_value=True)
    limit = _flag(args, "--limit", has_value=True)
    harness = _flag(args, "--harness", has_value=True)
    if not args:
        print("usage: helm transcript <sid> [--find T] [--limit N]", file=sys.stderr)
        return 2
    res = get_session(args[0], limit=int(limit) if limit else 60,
                      find=find, harness=harness)
    if "error" in res:
        print("helm transcript: %s" % res["error"], file=sys.stderr)
        return 1
    s = res["session"]
    flags = "" if s["resumable"] else "  NOT-RESUMABLE (outside helm's catalog)"
    print("helm transcript: %s  ·  %s  ·  %s  ·  msgs %d–%d of %d%s" % (
        s["title"], s["harness"], s.get("cwd") or "(no cwd)",
        res["start"], res["end"], res["total"], flags))
    if res.get("note"):
        print("# %s" % res["note"])
    match_i = (res.get("match") or {}).get("index")
    for i, m in enumerate(res["messages"]):
        head = ("»" if i == match_i else " ") + "[%s %s]" % (m["role"], _hhmm(m.get("ts")))
        pad = " " * len(head)
        first = True
        for it in m["items"]:
            lead = head if first else pad
            first = False
            if it["k"] == "text":
                lines = it["t"].strip().splitlines() or [""]
                print("%s %s" % (lead, lines[0]))
                for l in lines[1:]:
                    print("%s %s" % (pad, l))
            elif it["k"] == "think":
                print("%s ~ %s" % (lead, _trunc(it["t"], 120)))
            elif it["k"] == "tool":
                print("%s ⚙ %s %s" % (lead, it["name"], _trunc(it["t"], 160)))
            elif it["k"] == "result":
                print("%s   ↳%s %s" % (lead, "!" if it.get("err") else "", _trunc(it["t"])))
    return 0


def cmd_rehome(args):
    """rehome <sid> <new-cwd> | rehome <sid> --reset — move a session's home cwd
    (metadata override + claude project-slug symlink so --resume resolves there)."""
    args = list(args or [])
    reset = _flag(args, "--reset")
    if not args or (len(args) < 2 and not reset) or (len(args) > 1 and reset):
        print("usage: helm rehome <sid> <new-cwd>  |  helm rehome <sid> --reset",
              file=sys.stderr)
        return 2
    res = cwd_override({"sid": args[0], "cwd": None if reset else args[1]})
    if "error" in res:
        print("helm rehome: %s" % res["error"], file=sys.stderr)
        return 1
    print("helm rehome: %s" % res["note"])
    if res.get("cwd"):
        print("  cwd override -> %s" % res["cwd"])
    return 0


def cmd_prune(args):
    """prune <sid> [--preset lean|window20k] [--tokens N] [--dry] — derive a
    smaller resume-optimized COPY via cv prune (original untouched)."""
    args = list(args or [])
    preset = _flag(args, "--preset", has_value=True) or "lean"
    tokens = _flag(args, "--tokens", has_value=True)
    dry = _flag(args, "--dry")
    if not args:
        print("usage: helm prune <sid> [--preset lean] [--dry]", file=sys.stderr)
        return 2
    if tokens:
        preset = "window"
    res = prune_session(args[0], preset=preset, dry=bool(dry), tokens=tokens)
    if "error" in res:
        print("helm prune: %s" % res["error"], file=sys.stderr)
        return 1
    if res.get("dry"):
        e = res["estimate"]
        print("helm prune: %s  preset=%s  now %s / %s msgs (dry)" % (
            res["sid"], res["preset"], _mb(e["beforeBytes"]), e["beforeMsgs"]))
        print("  %s" % e["note"])
        print("  will run: %s" % res["willRun"])
    else:
        print("helm prune: new session %s  (%s -> %s, preset=%s; original untouched)" % (
            res["newSid"], _mb(res["beforeBytes"]), _mb(res.get("afterBytes")), res["preset"]))
        print("  %s" % res["note"])
        print("  resume it: helm sessions resume %s" % res["newSid"][:8])
    return 0
