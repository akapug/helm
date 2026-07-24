"""helm work — shared constants + leaf helpers (the _common cluster).

Module-level constants and the no-dependency leaf readers every other
cluster leans on. Moved verbatim from the pre-split helm/work.py.
"""
import json
import os
import re


DEFAULT_TTL = 4 * 3600          # a build lane, not a chat lock
RECENT_WRITE_SECONDS = 15 * 60  # display bucket only; never a reap authorization
LANE_RE = re.compile(r"[A-Za-z0-9._-]{1,64}$")
_AGENT_ROOM_RE = re.compile(r"agent-[A-Za-z0-9_-]{8,64}$")
_WORKFLOW_ROOM_RE = re.compile(r"(wf_[A-Za-z0-9_-]{3,64})-([1-9][0-9]*)$")
_VALUE_FLAGS = ("--repo", "--seat", "--lease", "--ttl")


def _read_small_nofollow(path, limit=4096):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError("metadata file too large")
    return data


def _load_json_nofollow(path, limit=2 * 1024 * 1024):
    return json.loads(_read_small_nofollow(path, limit=limit))


def _claude_homes():
    try:
        from .. import skillsync
        return [p for _label, p in skillsync.config_dirs()]
    except Exception:
        return None
