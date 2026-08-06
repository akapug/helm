#!/usr/bin/env python3
"""Harness session-home scanners — the auto-map's eyes. Harness-BROAD by law:
a project the user only ever touched in codex or opencode is still helm-known.

Each scanner yields uniform OBSERVATIONS:
    {"harness": str, "cwd": str, "sessions": int, "last_seen": float(epoch),
     "days": set(iso-date), "refs": [str]}
one per raw working directory, decoded from what each harness actually records:

  claude:   ~/.claude/projects/<slug>/*.jsonl — decode the REAL cwd from the
            newest session's `"cwd":` field, NEVER the lossy dir slug.
            (All ~/.claude-homes/<acct>/projects are symlinks to one dir —
            dedupe on realpath.)
  codex:    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (+ ~/.codex-homes/
            <acct>/sessions/**) — line 1 is session_meta with payload.cwd.
  opencode: ~/.local/share/opencode/storage/project/<hash>.json — `worktree`
            is already the real path.
  pi:       ~/.pi/agent/sessions/<slug>/<ts>_<uuid>.jsonl — line 1 is a
            {"type":"session"} record carrying the REAL cwd, same law as
            claude/codex: decode the path from inside, never from the slug.

Scanners are read-only and fail-open per file: one corrupt session never
breaks the map.
"""
import glob
import json
import os
import re
import time

from . import home

_CWD_RE = re.compile(r'"cwd"\s*:\s*"([^"]+)"')

# How much of a session file we read hunting for the cwd field. claude puts it
# on most message lines; codex on line 1. 256KB covers pathological preambles.
_SNIFF_BYTES = 256 * 1024


def _sniff_cwd(path):
    """First `"cwd":"..."` in the file. Reads a small chunk first (codex puts it
    on line 1, claude within the first message lines), widening only on miss."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for size in (8192, _SNIFF_BYTES):
                f.seek(0)
                m = _CWD_RE.search(f.read(size))
                if m:
                    return m.group(1)
        return None
    except Exception:
        return None


def _day(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


# --- codex cwd sidecar ------------------------------------------------------
# The codex scan globs ~6k rollout files and, without a cache, re-reads up to
# 256KB of each on EVERY `helm sync` just to recover the cwd on line 1. A rollout
# is append-only and its session_meta cwd is written once, so the sniff is
# perfectly cacheable: {path: [int(mtime), size, cwd]} keyed on the stat
# signature (cwd may be null — a negative result is cached too, so a cwd-less
# file is never re-sniffed). A file whose (mtime,size) still matches skips the
# read entirely; the sidecar is pruned to the paths seen this run, so vanished
# sessions never accrete. FAIL-OPEN: any cache trouble degrades to a full sniff,
# never an error.

def _codex_cache_path():
    base = home.env("CACHE_DIR") or os.path.join(os.path.expanduser("~"),
                                                 ".cache", "helm")
    return os.path.join(base, "codex-cwd-cache.json")


def _load_codex_cache():
    try:
        with open(_codex_cache_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_codex_cache(cache):
    try:
        path = _codex_cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())  # unique per writer; atomic replace
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    except OSError:
        pass


def claude_observations(claude_root=None):
    root = os.path.realpath(claude_root or os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        jsonls = []
        for n in os.listdir(d):
            if n.endswith(".jsonl"):
                try:
                    jsonls.append((os.path.getmtime(os.path.join(d, n)), os.path.join(d, n)))
                except OSError:
                    pass
        if not jsonls:
            continue
        jsonls.sort(reverse=True)
        cwd = None
        for _, p in jsonls[:3]:  # newest first; fall back if a session lacks cwd
            cwd = _sniff_cwd(p)
            if cwd:
                break
        if not cwd:
            continue
        out.append({
            "harness": "claude", "cwd": cwd, "sessions": len(jsonls),
            "last_seen": jsonls[0][0], "days": {_day(m) for m, _ in jsonls},
            "refs": [name],
        })
    return out


def codex_observations(roots=None):
    hm = os.path.expanduser("~")
    roots = roots or [os.path.join(hm, ".codex", "sessions")] + \
        sorted(glob.glob(os.path.join(hm, ".codex-homes", "*", "sessions"))) + \
        sorted(glob.glob(os.path.join(hm, ".codex-homes", ".archive*", "*", "sessions")))
    prior = _load_codex_cache()
    fresh, seen, by_cwd = {}, set(), {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for p in glob.iglob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True):
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)  # inode-dedupe symlinked/hardlinked dups
            if key in seen:               # the same rollout under two roots
                continue
            seen.add(key)
            sig = [int(st.st_mtime), st.st_size]
            ent = prior.get(p)
            cwd = ent[2] if ent and ent[:2] == sig else _sniff_cwd(p)
            fresh[p] = [sig[0], sig[1], cwd]  # cache the sniff (cwd or null)
            if not cwd:
                continue
            o = by_cwd.setdefault(cwd, {"harness": "codex", "cwd": cwd, "sessions": 0,
                                        "last_seen": 0.0, "days": set(), "refs": []})
            o["sessions"] += 1
            o["last_seen"] = max(o["last_seen"], st.st_mtime)
            o["days"].add(_day(st.st_mtime))
    _save_codex_cache(fresh)
    return list(by_cwd.values())


def opencode_observations(storage=None):
    root = storage or os.path.join(os.path.expanduser("~"), ".local", "share",
                                   "opencode", "storage", "project")
    if not os.path.isdir(root):
        return []
    out = []
    for p in glob.glob(os.path.join(root, "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        wt = d.get("worktree")
        if not wt or os.path.basename(p) == "global.json":
            continue
        t = d.get("time") or {}
        updated = (t.get("updated") or t.get("created") or 0) / 1000.0
        out.append({
            "harness": "opencode", "cwd": wt, "sessions": 1,
            "last_seen": updated, "days": {_day(updated)} if updated else set(),
            "refs": [os.path.basename(p)[:-5]],
        })
    return out


# --- pi ---------------------------------------------------------------------
# pi (earendil-works/pi-coding-agent) stores one JSONL per session under a
# path-slug directory, and line 1 is `{"type":"session","id":…,"cwd":…}` — the
# real working directory, recorded inside the file. So the same law that
# governs claude and codex applies unchanged: decode the cwd from the CONTENT,
# never from `--home-user-dev-project--`, which is lossy by construction (it
# cannot distinguish a hyphen in a directory name from a path separator).
#
# THE SESSION-DIR ENV VAR IS NOT A FIXED NAME. pi derives it from APP_NAME:
# `ENV_SESSION_DIR = ${APP_NAME.toUpperCase()}_CODING_AGENT_SESSION_DIR`, where
# APP_NAME comes from package.json's `piConfig.name` and defaults to "pi" — pi's
# own source comments name TAU_CODING_AGENT_DIR as the rebranded case. So the
# override is looked up under any *_CODING_AGENT_SESSION_DIR spelling present in
# the environment rather than a single hardcoded key, and the default path is
# used when none is set.

_PI_SESSION_ENV = "_CODING_AGENT_SESSION_DIR"


def pi_session_root(env=None):
    """pi's session home: an APP_NAME-derived env override, else ~/.pi/agent."""
    env = os.environ if env is None else env
    for key, val in env.items():
        if key.endswith(_PI_SESSION_ENV) and val:
            return val
    return os.path.join(os.path.expanduser("~"), ".pi", "agent", "sessions")


def pi_observations(root=None):
    root = root or pi_session_root()
    if not os.path.isdir(root):
        return []
    by_cwd = {}
    for p in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            st = os.stat(p)
        except OSError:
            continue
        cwd = _sniff_cwd(p)
        if not cwd:
            continue          # fail-open per file: one unreadable session
        o = by_cwd.setdefault(cwd, {                    # never breaks the map
            "harness": "pi", "cwd": cwd, "sessions": 0,
            "last_seen": 0.0, "days": set(), "refs": [],
        })
        o["sessions"] += 1
        o["last_seen"] = max(o["last_seen"], st.st_mtime)
        o["days"].add(_day(st.st_mtime))
        # the ref is the session UUID, which is what pi's own --session takes
        name = os.path.basename(p)[:-6]
        o["refs"].append(name.split("_", 1)[-1] if "_" in name else name)
    return list(by_cwd.values())


def all_observations():
    return (claude_observations() + codex_observations()
            + opencode_observations() + pi_observations())
