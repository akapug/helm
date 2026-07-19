#!/usr/bin/env python3
"""pk — shared personal-knowledge store primitives (helm's copy of the pkstore
contract, kept format-compatible with the live store so existing hook consumers
and helm read/write the same files).

  - slug()                     filename-safe entry naming
  - parse_simple_frontmatter() the no-YAML-dep fenced key:value reader
  - atomic_write()             tmp+rename; a torn entry is never visible
  - now_ts()                   one timestamp format everywhere
  - event()/read_events()      the ONE mutation-receipt chokepoint: every
                               store-adjacent WRITE appends one line to
                               _global/.state/events.jsonl (fire-ledger laws)

Import-safe, side-effect-free at import.
"""
import os
import re
import time


def slug(s):
    return re.sub(r"[^A-Za-z0-9_-]", "-", (s or "")).strip("-").lower()[:60] or "default"


def now_ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_simple_frontmatter(path, defaults, list_keys=()):
    """One entry file -> dict from `defaults` (copied), filled from the fenced
    frontmatter's flat OR metadata-nested `key: value` lines. Fail-open:
    unreadable -> None. `list_keys` values split on '||'."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except Exception:
        return None
    e = dict(defaults)
    in_fm = False
    for line in raw.splitlines():
        if line.strip() == "---":
            if in_fm:
                break
            in_fm = True
            continue
        if not in_fm:
            continue
        km = re.match(r"^\s*([A-Za-z_]+):\s*(.*)$", line)
        if not km:
            continue
        k, v = km.group(1).lower(), km.group(2).strip().strip('"')
        if k in list_keys:
            e[k] = [x.strip() for x in v.split("||") if x.strip()] if v else []
        elif k in e:
            e[k] = v
    return e


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:  # never the locale's guess —
        f.write(text)                            # readers open utf-8 explicitly
    os.replace(tmp, path)


def read_json(path, default=None):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    import json
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


# ---------------------------------------------------------------------------
# events journal — mutation receipts through ONE chokepoint
# ---------------------------------------------------------------------------

EVENTS_MAX = 5 * 1024 * 1024  # journal rotates here (one .1 generation)


def events_path():
    from . import home
    return os.path.join(home.global_dir(), ".state", "events.jsonl")


def event(verb, target, summary, actor=None):
    """The mutation-receipt seam: a writer calls this AFTER its write lands and
    ONE {v, ts, actor, verb, target, summary} line lands on events.jsonl.
    Receipts, never truth — the files stay source and nothing may read the
    journal over them (a hand-edit is legal; it just has no receipt). The
    .state/ home is DELIBERATE (host-local telemetry, never ships — the
    AUTHORED/DERIVED split): a rotating lossy trail is not a durable record;
    durable provenance rides IN the artifacts (evidence_log/attest_*/
    tombstones), and shipping the journal would invite reading it as a third
    store. The fire-ledger's HARD LAWS apply: O(1) (one stat + one append, never a read),
    5MB one-generation rotation (-> .1), FAIL-OPEN — journal trouble must never
    block or fail the write it describes. actor: explicit arg, else
    $HELM_ACTOR, else $CLAUDE_SESSION_ID (the hook session), else 'cli'."""
    import json
    try:
        path = events_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > EVENTS_MAX:
                os.replace(path, path + ".1")
        except OSError:
            pass  # no journal yet
        row = {"v": 1, "ts": now_ts(),
               "actor": actor or os.environ.get("HELM_ACTOR")
               or os.environ.get("CLAUDE_SESSION_ID") or "cli",
               "verb": str(verb), "target": str(target),
               "summary": str(summary)[:200]}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def read_events(limit=20):
    """The recent trail, oldest-first within the last `limit` rows; spans the
    rotated generation when the current file runs short. Fail-open: garbled
    lines are skipped, any file trouble reads as fewer rows, never a raise."""
    import json
    try:
        n = max(int(limit), 0)
    except (TypeError, ValueError):
        n = 20
    path = events_path()
    lines = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        pass
    if len(lines) < n:
        try:
            with open(path + ".1", encoding="utf-8") as f:
                lines = f.read().splitlines() + lines
        except Exception:
            pass
    out = []
    for ln in lines[-n:] if n else []:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out
