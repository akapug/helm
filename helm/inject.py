#!/usr/bin/env python3
"""helm inject — the ONE active-fire surface. A harness hook calls this once
per turn with the prompt text; helm returns the context worth injecting:

  1. the pinned lane   (load_class=always priors, byte-budget-capped)
  2. the JIT lane      (typed-store entries whose specific keywords match)
  3. reflex steers     (signals live this turn)

One resolver, every harness — claude/codex/opencode/hermes hooks all call the
same verb, which is what makes helm's knowledge fire wherever the operator
works. Salience law: no match -> EMPTY output (a silent turn costs nothing).

Wiring (examples):
  claude   UserPromptSubmit hook: helm inject --project <p> < prompt.txt
           -> stdout becomes additionalContext
  codex    notify/turn hook: same call, same stdout

PERF (the per-prompt hot path): the store is parsed ONCE per call, not once per
lane — both lanes are fed from a persistent parsed-entry cache keyed by the
stat signature of every store file (the catalog-cache.json pattern), so the
steady state is ~N stat() calls + one JSON read instead of two full
frontmatter parses of the ~880-file adopted store. Cache lives under
$HELM_CACHE_DIR (default ~/.cache/helm); any cache trouble falls back to a
direct parse. The DURABLE fix — an entries= parameter threading one load
through store.pinned/resolve_prompt — is a store.py change deferred to that
lane; until it lands the shared list is installed here (see _lanes).

FAIL OPEN (docs/HOOKS.md law): a hook that cannot run helm must inject nothing,
never block — a store or reflex failure yields an empty lane and rc 0.
"""
import hashlib
import json
import os
import sys
import threading

from . import home, reflex

PINNED_BUDGET = 1200  # bytes for the always lane — keep the constant tax tiny
JIT_CAP = 4
LINE_CAP = 400        # per-entry cap — the gloss fires, the full entry stays on disk

_CACHE_VERSION = 1    # bump when store parsing/derivation changes entry shape
_lanes_lock = threading.Lock()


def _entry_line(e):
    line = _entry_line_full(e)
    return line if len(line) <= LINE_CAP else line[:LINE_CAP - 1] + "…"


def _entry_line_full(e):
    t = e.get("type")
    if t == "prior":
        tag = "PREMISE" if e.get("class") == "certain" else "PRIOR %.2f" % e["confidence"]
        return "%s %s: %s" % (tag, e["id"], e.get("statement") or "")
    if t == "lexicon":
        return "TERM %s: %s" % (e.get("term") or e["id"], e.get("definition") or e.get("statement") or "")
    if t == "heuristic":
        return "MOVE %s: %s" % (e["id"], e.get("statement") or "")
    if t == "reference":
        return "REF %s: %s" % (e["id"], e.get("statement") or "")
    return "%s: %s" % (e["id"], e.get("statement") or "")


def _cache_file(project=None):
    """One cache file per physical-root set (the dirs are in the key), so a
    tmp-store test or a --project call never collides with the live store's."""
    from . import store
    base = home.env("CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "helm")
    key = hashlib.sha1(("%s|%r" % (project or "", store.roots(project))).encode()).hexdigest()[:12]
    return os.path.join(base, "store-cache-%s.json" % key)


def _store_sig(project=None):
    """Every store file's [path, mtime_ns, size] — the cache validity key.
    ~N stat() calls (~3-5ms on the live 880-file store) vs ~100ms of parse.
    Lists, not tuples, so equality survives the JSON round-trip."""
    from . import store
    sig = []
    for root, _scope, d in store.roots(project):
        for _name, path in store._entry_files(root, d):
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig.append([path, st.st_mtime_ns, st.st_size])
    sig.sort()
    return sig


def load_entries(project=None):
    """store.load_all(), ONCE, through the persistent parsed-entry cache.
    Any cache trouble (missing, stale, torn, unwritable) degrades to a direct
    parse — a cache is never worth failing a turn over."""
    from . import store
    sig = _store_sig(project)
    path = _cache_file(project)
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("v") == _CACHE_VERSION and c.get("sig") == sig:
            return c["entries"]
    except Exception:
        pass
    entries = store.load_all(project=project)
    try:
        from . import pk
        pk.atomic_write(path, json.dumps(
            {"v": _CACHE_VERSION, "sig": sig, "entries": entries}, ensure_ascii=False))
    except Exception:
        pass
    return entries


def _lanes(text, project=None):
    """(pinned_entries, jit_entries) off ONE store parse. store.pinned and
    store.resolve_prompt each call load_all() internally (that seam is owned by
    the store lane — the durable fix is an entries= parameter there); until it
    lands, the shared list is served by swapping store.load_all around the two
    calls, applying load_all's exact post-filters. Lock-guarded + restored in
    finally, so the swap can never leak out of this call."""
    from . import store
    entries = load_entries(project)
    real = store.load_all

    def shared(project=None, include_retired=False, include_dormant=True, types=None):
        if include_retired:  # not an inject shape — stay truthful, hit disk
            return real(project=project, include_retired=True,
                        include_dormant=include_dormant, types=types)
        if isinstance(types, str):
            types = (types,)
        return [e for e in entries
                if (include_dormant or e.get("load_class") != "dormant")
                and (not types or e["type"] in types)]

    with _lanes_lock:
        store.load_all = shared
        try:
            return (store.pinned(project=project),
                    store.resolve_prompt(text, project=project, cap=JIT_CAP))
        finally:
            store.load_all = real


def gather(text, project=None):
    """-> dict {pinned: [line], jit: [line], reflex: [line]} (each may be empty).
    Fail-open per lane: a raising store/reflex yields that lane empty."""
    try:
        pinned_entries, jit_entries = _lanes(text, project=project)
    except Exception:
        pinned_entries, jit_entries = [], []
    pinned_lines = []
    used = 0
    for e in pinned_entries:
        line = _entry_line(e)
        if used + len(line) > PINNED_BUDGET:
            break
        pinned_lines.append(line)
        used += len(line)
    jit = [_entry_line(e) for e in jit_entries]
    try:
        steers = ["REFLEX: " + e["steer"] for e in reflex.fire(text, project=project)]
    except Exception:
        steers = []
    return {"pinned": pinned_lines, "jit": jit, "reflex": steers}


def render(sections):
    lines = sections["pinned"] + sections["jit"] + sections["reflex"]
    return "\n".join(lines)


def cmd_inject(args):
    """inject [--project P] [--json] — prompt text on stdin -> context lines."""
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    text = "" if sys.stdin.isatty() else sys.stdin.read()
    for a in args:
        if not a.startswith("--") and a != project:
            text = a  # allow inline text for quick tests
    sections = gather(text, project=project)
    if "--json" in args:
        print(json.dumps(sections, ensure_ascii=False))
        return 0
    out = render(sections)
    if out:
        print(out)
    return 0
