#!/usr/bin/env python3
"""helm inject — the ONE active-fire surface. A harness hook calls this once
per turn with the prompt text; helm returns the context worth injecting:

  1. the pinned lane   (load_class=always priors, byte-budget-capped)
  2. the JIT lane      (typed-store entries whose specific keywords match)
  3. reflex steers     (signals live this turn)

One resolver, every harness — claude/codex/opencode/hermes hooks all call the
same verb, which is what makes helm's knowledge fire wherever the operator
works. Salience law: no match -> EMPTY output (a silent turn costs nothing).

Wiring (`helm hooks install` writes this; docs/HOOKS.md has the manual recipe):
  claude   UserPromptSubmit hook: helm inject --hook-json  <- the FULL hook
           JSON on stdin (prompt/cwd/session_id, unknown keys tolerated);
           stdout becomes additionalContext. The project scope is DERIVED from
           the hook's cwd via the registry (longest-prefix over project paths,
           drain's longest-first law) — global-only when no project claims it.
  codex    notify/turn hook: same verb, plain prompt text on stdin

PERF (the per-prompt hot path): the store is parsed ONCE per call, not once per
lane — both lanes are fed from a persistent parsed-entry cache keyed by the
stat signature of every store file (the catalog-cache.json pattern), so the
steady state is ~N stat() calls + one JSON read instead of two full
frontmatter parses of the ~880-file adopted store. Cache lives under
$HELM_CACHE_DIR (default ~/.cache/helm); any cache trouble falls back to a
direct parse. The cached list feeds both lanes EXPLICITLY through
store.pinned/resolve_prompt's entries= parameter (the durable seam — no
monkeypatch, so a dropped cache regresses loudly in tests, not silently here).

FIRE-LEDGER (the measurement spine): every gather() appends ONE JSON line to
<helm home>/_global/.state/inject-ledger.jsonl — v:1, ts, project, session
(when the hook supplied one), fired entry IDS per lane (never prompt text),
bytes per lane, pre-cap candidate count, elapsed_ms; a no-fire turn logs
{"silent": true} instead of per-entry fields.
O(1) append, 5MB one-generation rotation (-> .1), and fail-open: ledger
trouble never blocks or slows the hook. `helm inject --explain` is the read
side — what WOULD fire for stdin text and why (the pinned budget walk, which
keyword matched per JIT hit) — and writes NO ledger row.

FAIL OPEN (docs/HOOKS.md law): a hook that cannot run helm must inject nothing,
never block — a store or reflex failure yields an empty lane and rc 0.
"""
import hashlib
import json
import os
import sys
import time

from . import home, pk, reflex

PINNED_BUDGET = 1200  # bytes for the always lane — keep the constant tax tiny
JIT_CAP = 4
LINE_CAP = 400        # per-entry cap — the gloss fires, the full entry stays on disk
LEDGER_MAX = 5 * 1024 * 1024  # ledger rotates here (one .1 generation)

_CACHE_VERSION = 1    # bump when store parsing/derivation changes entry shape


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
        pk.atomic_write(path, json.dumps(
            {"v": _CACHE_VERSION, "sig": sig, "entries": entries}, ensure_ascii=False))
    except Exception:
        pass
    return entries


def _lanes(text, project=None):
    """(pinned_entries, ALL ranked jit matches) off ONE store parse — the
    cached list feeds both lanes explicitly through the entries= seam. JIT
    comes back UNCAPPED so gather can both cap the lane and ledger the pre-cap
    candidate count."""
    from . import store
    entries = load_entries(project)
    return (store.pinned(project=project, entries=entries),
            store.resolve_prompt(text, project=project, cap=len(entries),
                                 entries=entries))


def parse_hook_json(raw):
    """Claude Code UserPromptSubmit hook JSON -> (prompt, cwd, session). Unknown
    keys are tolerated (the hook payload grows); missing keys read as empty.
    Malformed/non-object input -> (None, None, None): the caller must FAIL OPEN
    (inject nothing, rc 0) — a garbled payload never blocks a turn."""
    try:
        d = json.loads(raw)
    except Exception:
        return None, None, None
    if not isinstance(d, dict):
        return None, None, None
    return (str(d.get("prompt") or ""),
            str(d.get("cwd") or "") or None,
            str(d.get("session_id") or "") or None)


def project_for_cwd(cwd):
    """cwd -> registry project name by LONGEST-prefix match over project paths
    (drain's longest-first law: the deepest registered path that contains cwd
    wins). None = no project claims it — the caller stays global-only.
    Read-only registry access; fail-open (any trouble -> None)."""
    if not cwd:
        return None
    from . import registry
    try:
        want = os.path.abspath(os.path.expanduser(str(cwd)))
        best = None
        for key, rec in (registry.load().get("projects") or {}).items():
            path = str(rec.get("path") or "").rstrip("/")
            if path and (want == path or want.startswith(path + "/")) \
                    and len(path) > len(best[0] if best else ""):
                best = (path, str(rec.get("name") or key))
        return best[1] if best else None
    except Exception:
        return None


def _ledger_path():
    return os.path.join(home.global_dir(), ".state", "inject-ledger.jsonl")


def _ledger_append(row):
    """ONE appended JSON line per inject call — the measurement spine. HARD
    LAWS: O(1) (one stat + one append, never a read), entry IDS never prompt
    text, 5MB one-generation rotation, and FAIL-OPEN — a ledger that cannot be
    written must never block or slow the hook."""
    try:
        path = _ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > LEDGER_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass  # no ledger yet
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def gather(text, project=None, session=None):
    """-> dict {pinned: [line], jit: [line], reflex: [line]} (each may be empty).
    Fail-open per lane: a raising store/reflex yields that lane empty. Every
    call appends one fire-ledger row (silent turns log {"silent": true});
    session (the hook's session_id) rides the row when supplied."""
    t0 = time.time()
    try:
        pinned_entries, jit_all = _lanes(text, project=project)
    except Exception:
        pinned_entries, jit_all = [], []
    pinned_lines, pinned_ids = [], []
    used = 0
    for e in pinned_entries:
        line = _entry_line(e)
        if used + len(line) > PINNED_BUDGET:
            break
        pinned_lines.append(line)
        pinned_ids.append(str(e["id"]))
        used += len(line)
    jit_entries = jit_all[:JIT_CAP]
    jit = [_entry_line(e) for e in jit_entries]
    try:
        fired_reflex = reflex.fire(text, project=project)
    except Exception:
        fired_reflex = []
    steers = ["REFLEX: " + e["steer"] for e in fired_reflex]
    sections = {"pinned": pinned_lines, "jit": jit, "reflex": steers}
    row = {"v": 1, "ts": pk.now_ts(), "project": project,
           "elapsed_ms": round((time.time() - t0) * 1000, 1)}
    if session:
        row["session"] = session
    fired = {"pinned": pinned_ids, "jit": [str(e["id"]) for e in jit_entries],
             "reflex": [str(e["id"]) for e in fired_reflex]}
    if any(fired.values()):
        row.update({"fired": fired,
                    "bytes": {k: sum(len(l) for l in sections[k]) for k in sections},
                    "candidates": len(pinned_entries) + len(jit_all) + len(fired_reflex)})
    else:
        row["silent"] = True
    _ledger_append(row)
    return sections


def render(sections):
    lines = sections["pinned"] + sections["jit"] + sections["reflex"]
    return "\n".join(lines)


def _explain(text, project=None):
    """--explain: what WOULD fire for this text and WHY — the pinned budget
    walk, each JIT hit's matching keyword(s), live reflex signals. A dry look:
    NO ledger row (an explain must never count as a turn)."""
    from . import store
    pinned_entries, jit_all = _lanes(text, project=project)
    low = (text or "").lower()
    used = 0
    cut = False
    if pinned_entries:
        print("pinned (%d candidate%s, budget %dB):" % (
            len(pinned_entries), "s"[:len(pinned_entries) != 1], PINNED_BUDGET))
    for e in pinned_entries:
        line = _entry_line(e)
        cut = cut or used + len(line) > PINNED_BUDGET  # greedy walk: first overflow ends the lane
        if cut:
            print("  - %s (over budget)" % e["id"])
        else:
            used += len(line)
            print("  + " + line)
    if jit_all:
        print("jit (%d hit%s, cap %d):" % (len(jit_all), "s"[:len(jit_all) != 1], JIT_CAP))
    for i, e in enumerate(jit_all):
        matched = store._probe_hits(e, low)[2]
        mark, over = ("  + ", "") if i < JIT_CAP else ("  - ", " (over cap)")
        print(mark + str(e["id"]) + " [matched: " + " ".join(matched) + "]" + over)
    fired_reflex = reflex.fire(text, project=project)
    if fired_reflex:
        print("reflex:")
    for e in fired_reflex:
        print("  + %s [%s]: %s" % (e["id"], e.get("signal") or "prompt", e["steer"]))
    if not (pinned_entries or jit_all or fired_reflex):
        print("silent turn — nothing fires (salience law)")
    return 0


def cmd_inject(args):
    """inject [--project P] [--json] [--explain] [--hook-json] — prompt text on
    stdin -> context lines. --hook-json reads the harness hook's FULL JSON on
    stdin instead (prompt/cwd/session_id) and derives --project from the cwd
    via the registry; malformed hook JSON injects nothing, rc 0 (fail-open).
    --explain prints what WOULD fire and why, sans ledger row."""
    project = None
    if "--project" in args:
        project = args[args.index("--project") + 1]
    session = scope_via = None
    if "--hook-json" in args:
        text, cwd, session = parse_hook_json(
            "" if sys.stdin.isatty() else sys.stdin.read())
        if text is None:
            return 0  # garbled hook payload: inject nothing, never block
        if project is None:
            project = project_for_cwd(cwd)
            scope_via = cwd if project else None
    else:
        text = "" if sys.stdin.isatty() else sys.stdin.read()
        for a in args:
            if not a.startswith("--") and a != project:
                text = a  # allow inline text for quick tests
    if "--explain" in args:
        if scope_via:
            print("[scope: %s via %s]" % (project, scope_via))
        return _explain(text, project=project)
    sections = gather(text, project=project, session=session)
    if "--json" in args:
        print(json.dumps(sections, ensure_ascii=False))
        return 0
    out = render(sections)
    if out:
        print(out)
    return 0
