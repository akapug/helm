"""Which GitHub repositories a checkout pushes to, and whether each is public.

WHAT IT IS FOR. The owner's board draws, on each project row, a public or
private badge per repository, linking to it: "things like private/public repo
status (and links to those for the gits associated)". Two readings make that
badge and they cost very different amounts, so they are two functions.

THE REMOTES ARE LOCAL AND CHEAP. `remotes(path)` asks the checkout's own git
config for every `remote.<name>.url`, once per change of that config file (a
stat fingerprint, the shape `web_board._trunk_tip` uses for refs). A remote
on GitHub yields its `owner/name`; anything else yields no name at all, and
the page says "unknown" rather than guessing where it points. A fork carries
two remotes — the fork and the repository it came from — and both are
returned, fork first.

A REMOTE URL CAN CARRY A CREDENTIAL (`https://user:token@host.example/owner/name`), so
the raw URL never leaves this module. What leaves is the parsed `owner/name`
and a link built from it.

VISIBILITY IS ONE NETWORK CALL PER REPOSITORY, SO IT IS ASKED ONCE A DAY.
`visibility(slugs)` answers from a small on-disk cache under the helm home
and never waits on the network: a repository with no answer yet reads
`pending` and is queued for ONE background worker, which asks
`gh repo view <owner/name> --json visibility` and writes the answer back. An
answer is kept for a day; a failed ask (gh missing, not logged in, no such
repository) is recorded as `unknown` WITH the reason and asked again after an
hour, so a transient failure neither sticks for a day nor hammers gh. A
visibility is never inferred from anything else: no answer from gh is
`unknown`, never `public` and never `private`.

`ask=False` answers from the cache alone and queues nothing — the board uses
it for projects it folds as quiet, which the owner is not looking at.
"""
import json
import os
import re
import subprocess
import threading
import time

from . import gitfacts, home, pk

CACHE_NAME = "repo-visibility.json"
#: how long a visibility gh answered stays the answer
VISIBILITY_TTL_S = 24 * 3600
#: how long a FAILED ask stands before it is asked again
FAILURE_TTL_S = 3600
GH_TIMEOUT_S = 20
_KNOWN = ("public", "private", "internal")

# owner/name out of the three spellings a GitHub remote takes: scp-style
# `git@github.com:o/n.git`, `https://[cred@]github.com/o/n[.git]` and
# `ssh://git@github.com/o/n.git`.
_GITHUB = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?github\.com[:/]"
                     r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/*$")


def slug_of(url):
    """`owner/name` for a GitHub remote URL, else None."""
    m = _GITHUB.match(str(url or "").strip())
    return "%s/%s" % (m.group(1), m.group(2)) if m else None


def cache_path():
    """Beside the burn-flag snapshot, anchored on the helm home, so one env
    var isolates it and no test can read or write this machine's cache."""
    return os.path.join(home.helm_home(), home.GLOBAL, ".state", CACHE_NAME)


# -- the remotes --------------------------------------------------------------

_REMOTES_MEMO = {}

# THE SLICE RUNNER'S DATA AUDIT (helm/gateslice.py) reports any module
# data a test unit leaves behind; these names are process-wide by design.
_GATESLICE_MUTABLE = {
    "_REMOTES_MEMO": (
        "remotes per common git dir, checked against its config stamp"),
}
_REMOTES_LOCK = threading.Lock()
_REMOTES_MEMO_CAP = 1024


def _config_stamp(common):
    try:
        st = os.stat(os.path.join(common, "config"))
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def remotes(path):
    """([{remote, slug, url}], None) for a checkout, or (None, why).

    `origin` first, then the rest by name. ONE git spawn when the checkout's
    config changed, NONE when it did not. A path that is not itself a
    checkout root is refused rather than handed to git, which would walk up
    and answer for whatever repository encloses it."""
    if not path or not os.path.exists(os.path.join(path, ".git")):
        return None, "no git checkout at the registered path"
    common = gitfacts._common_dir(path)
    if not common:
        return None, "the checkout's git directory cannot be read"
    stamp = _config_stamp(common)
    with _REMOTES_LOCK:
        hit = _REMOTES_MEMO.get(common)
    if stamp is not None and hit and hit[0] == stamp:
        return [dict(r) for r in hit[1]], None
    from . import vcs
    rc, out, _err = vcs.backend(path).text(
        path, "config", "--get-regexp", r"^remote\..*\.url$", timeout=10)
    # `--get-regexp` exits 1 when nothing matched: a checkout with no remote
    if rc not in (0, 1) or (rc == 1 and out.strip()):
        return None, "git could not read the remotes (rc %d)" % rc
    rows = []
    for line in out.splitlines():
        key, _sp, url = line.partition(" ")
        name = key[len("remote."):-len(".url")] if key.startswith(
            "remote.") and key.endswith(".url") else ""
        if not name:
            continue
        slug = slug_of(url)
        rows.append({"remote": name, "slug": slug,
                     "url": "https://github.com/" + slug if slug else None})
    rows.sort(key=lambda r: (r["remote"] != "origin", r["remote"]))
    if stamp is not None:
        with _REMOTES_LOCK:
            _REMOTES_MEMO[common] = (stamp, rows)
            while len(_REMOTES_MEMO) > _REMOTES_MEMO_CAP:
                _REMOTES_MEMO.pop(next(iter(_REMOTES_MEMO)))
    return [dict(r) for r in rows], None


# -- the visibility -----------------------------------------------------------

_LOCK = threading.Lock()
_QUEUE = []
_INFLIGHT = set()
_WORKER = [None]
_IDLE = threading.Condition(_LOCK)


def _gh_visibility(slug):
    """(VISIBILITY, None) from gh, or (None, why). The one network call."""
    try:
        p = subprocess.run(("gh", "repo", "view", slug, "--json",
                            "visibility"), capture_output=True, text=True,
                           timeout=GH_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return None, "gh is not installed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "gh did not answer (%s)" % type(exc).__name__
    if p.returncode != 0:
        said = (p.stderr or p.stdout or "").strip().splitlines()
        return None, "gh: " + (said[0][:160] if said else "rc %d" % p.returncode)
    try:
        got = str(json.loads(p.stdout).get("visibility") or "")
    except (ValueError, AttributeError):
        return None, "gh answered something that is not the visibility"
    if got.lower() not in _KNOWN:
        return None, "gh answered a visibility this board does not know (%s)" \
            % got[:40]
    return got.upper(), None


def _load():
    data = pk.read_json(cache_path(), default=None)
    repos = data.get("repos") if isinstance(data, dict) else None
    return repos if isinstance(repos, dict) else {}


def _record(slug, vis, why):
    entry = {"visibility": vis.lower() if vis else "unknown",
             "why": None if vis else why, "checked_at": time.time()}
    with _LOCK:
        repos = _load()
        repos[slug] = entry
        pk.write_json(cache_path(), {"version": 1, "repos": repos})


def _work():
    while True:
        with _LOCK:
            if not _QUEUE:
                _WORKER[0] = None
                _IDLE.notify_all()
                return
            slug = _QUEUE.pop(0)
        try:
            vis, why = _gh_visibility(slug)
        except Exception as exc:        # noqa: BLE001 — recorded, never lost
            vis, why = None, "the ask raised (%s)" % type(exc).__name__
        try:
            _record(slug, vis, why)
        except OSError:
            pass                        # the next read asks again
        with _LOCK:
            _INFLIGHT.discard(slug)


def _enqueue(slug):
    with _LOCK:
        if slug in _INFLIGHT:
            return
        _INFLIGHT.add(slug)
        _QUEUE.append(slug)
        if _WORKER[0] is None:
            _WORKER[0] = threading.Thread(target=_work, name="repo-visibility",
                                          daemon=True)
            _WORKER[0].start()


def _fresh(entry, now):
    at = entry.get("checked_at")
    if not isinstance(at, (int, float)):
        return False
    ttl = VISIBILITY_TTL_S if entry.get("visibility") in _KNOWN \
        else FAILURE_TTL_S
    return now - at < ttl


def visibility(slugs, now=None, ask=True):
    """{slug: {visibility, why, checked_at, stale}} for each slug, answered
    from the cache and never from the network.

    `visibility` is public, private or internal as gh said it; `unknown` when
    gh could not say, with `why`; `pending` while the first ask is queued;
    `unasked` when `ask` is False and nothing is cached. An answer past its
    time is returned with `stale: True` and asked again."""
    now = time.time() if now is None else now
    with _LOCK:
        repos = _load()
    out = {}
    for slug in slugs:
        entry = repos.get(slug)
        fresh = isinstance(entry, dict) and _fresh(entry, now)
        if isinstance(entry, dict):
            out[slug] = {"visibility": entry.get("visibility") or "unknown",
                         "why": entry.get("why"),
                         "checked_at": entry.get("checked_at"),
                         "stale": not fresh}
        else:
            out[slug] = {"visibility": "pending" if ask else "unasked",
                         "why": None, "checked_at": None, "stale": False}
        if ask and not fresh:
            _enqueue(slug)
    return out


def drain(timeout):
    """Wait until every queued ask has been answered and written. True when
    the queue emptied inside `timeout` seconds. For tests and shutdown."""
    end = time.time() + timeout
    with _LOCK:
        while _WORKER[0] is not None or _QUEUE or _INFLIGHT:
            left = end - time.time()
            if left <= 0:
                return False
            _IDLE.wait(left)
    return True
