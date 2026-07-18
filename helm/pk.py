#!/usr/bin/env python3
"""pk — shared personal-knowledge store primitives (helm's copy of the pkstore
contract, kept format-compatible with the live store so existing hook consumers
and helm read/write the same files).

  - slug()                     filename-safe entry naming
  - parse_simple_frontmatter() the no-YAML-dep fenced key:value reader
  - atomic_write()             tmp+rename; a torn entry is never visible
  - now_ts()                   one timestamp format everywhere

Import-safe, side-effect-free.
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
    with open(tmp, "w") as f:
        f.write(text)
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
