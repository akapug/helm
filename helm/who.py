#!/usr/bin/env python3
"""helm who — the pid→cred attribution table.

Which running agent processes sit on which cred: /proc/<pid>/comm finds the
claude/codex CLIs, /proc/<pid>/environ names the cred home (CLAUDE_CONFIG_DIR
/ CODEX_HOME, defaulting to ~/.claude / ~/.codex) — same-uid processes only,
which is exactly the local fleet. The missing link for a third-party rotation
executor: a rebalance names an account; `who` names the pids ON it, and the
executor maps pid→pane to stop + resume on the new home.

Session attribution: codex holds its rollout jsonl OPEN, so the fd scan's
filename uuid IS the session id (exact); claude sessions are exact only when
the home+cwd project dir holds a single LIVE candidate (written within 5
minutes), else newest-first candidates are listed for the consumer to judge.
Two live processes on ONE session would double-resume/split-brain an executor,
so only the OLDEST process keeps `session` and every party carries the loud
session_shared marker. Subagents/helpers (an agent anywhere in the ancestor
chain) are marked child — rotation targets the top-level session, not its
children. Reads /proc and transcript FILENAMES only — never token contents.
"""
import glob
import json
import os

PROC = "/proc"  # module-level so tests point it at a fixture tree


def environ_var(pid, key):
    try:
        with open("%s/%d/environ" % (PROC, pid), "rb") as f:
            raw = f.read()
    except OSError:
        return None
    prefix = (key + "=").encode()
    for kv in raw.split(b"\0"):
        if kv.startswith(prefix):
            return kv[len(prefix):].decode("utf-8", "replace")
    return None


def _stat_field(pid, n, default):
    """Field n of /proc/<pid>/stat COUNTING FROM the state field — comm may
    contain spaces/parens, so parse after the LAST ')'."""
    try:
        with open("%s/%d/stat" % (PROC, pid)) as f:
            s = f.read()
        return int(s.rsplit(")", 1)[1].split()[n])
    except (OSError, IndexError, ValueError):
        return default


def ppid_of(pid):
    return _stat_field(pid, 1, 0)


def starttime_of(pid):
    """Process start (clock ticks since boot) — the session-dedupe tiebreak.
    Unreadable -> +inf so a ghost can never win session ownership."""
    return _stat_field(pid, 19, float("inf"))


def codex_session_from_fds(pid):
    """codex holds its rollout open: fd scan -> `rollout-<ts>-<uuid>.jsonl`
    -> the trailing 36-char uuid IS the session id."""
    for fd in glob.glob("%s/%d/fd/*" % (PROC, pid)):
        try:
            name = os.path.basename(os.readlink(fd))
        except OSError:
            continue
        if name.startswith("rollout-") and name.endswith(".jsonl"):
            stem = name[:-len(".jsonl")]
            if len(stem) > 36:
                return stem[-36:]
    return None


def claude_sessions(home, cwd):
    """(exact session | None, newest-first candidates) — jsonl files under
    `<home>/projects/<slug(cwd)>/` (homes symlink one shared store; the cwd
    slug is the discriminator). A single LIVE candidate (written within 5
    minutes) is exact; otherwise the consumer judges from the list."""
    import time
    slug = cwd.replace("/", "-").replace(".", "-")
    d = os.path.join(home, "projects", slug)
    now = time.time()
    cands = []
    try:
        entries = os.listdir(d)
    except OSError:
        return None, []
    for name in entries:
        if not name.endswith(".jsonl") or len(name) != 36 + len(".jsonl"):
            continue
        try:
            mt = os.path.getmtime(os.path.join(d, name))
        except OSError:
            continue
        cands.append((mt, name[:-len(".jsonl")], now - mt < 300))
    cands.sort(reverse=True)
    live = [c for c in cands if c[2]]
    exact = live[0][1] if len(live) == 1 else None
    return exact, [c[1] for c in cands[:3]]


def _accounts():
    from .providers import default_provider
    try:
        return default_provider().accounts()
    except Exception:
        return []


def scan(accounts=None):
    """One row per live claude/codex process, cred-attributed. Accounts map
    by realpath'd home (the provider's account rows carry the homes, default
    stores included) — no match stays None, never guessed."""
    if accounts is None:
        accounts = _accounts()
    defaults = {"anthropic": os.path.expanduser("~/.claude"),
                "codex": os.path.expanduser("~/.codex")}
    by_home = {}
    for a in accounts:
        if a.get("home"):
            by_home.setdefault(os.path.realpath(a["home"]),
                               a.get("name") or a.get("account"))
    procs = []
    for entry in glob.glob(os.path.join(PROC, "[0-9]*")):
        try:
            pid = int(os.path.basename(entry))
            with open(os.path.join(entry, "comm")) as f:
                comm = f.read().strip()
        except (OSError, ValueError):
            continue
        if comm == "claude":
            provider, env_key = "anthropic", "CLAUDE_CONFIG_DIR"
        elif comm == "codex":
            provider, env_key = "codex", "CODEX_HOME"
        else:
            continue
        home = environ_var(pid, env_key) or defaults[provider]
        try:
            cwd = os.readlink(os.path.join(entry, "cwd"))
        except OSError:
            cwd = None
        if provider == "codex":
            session, cands = codex_session_from_fds(pid), []
        else:
            session, cands = claude_sessions(home, cwd) if cwd else (None, [])
        procs.append({"pid": pid, "ppid": ppid_of(pid), "child": False,
                      "provider": provider, "home": home,
                      "account": by_home.get(os.path.realpath(home)),
                      "cwd": cwd, "session": session,
                      "session_candidates": cands, "session_shared": False})
    pid_set = {p["pid"] for p in procs}
    for p in procs:
        # walk the FULL ancestor chain: an agent spawned through an
        # intermediate shell/node is still a child (direct-ppid misses it)
        a, hops = p["ppid"], 0
        while a > 1 and hops <= 25:
            if a in pid_set:
                p["child"] = True
                break
            a, hops = ppid_of(a), hops + 1
    # SESSION DEDUPE: two independent processes on one session would
    # double-resume/split-brain an executor. The OLDEST process owns the
    # session; the rest demote it to a candidate; both sides carry the marker.
    by_session = {}
    for i, p in enumerate(procs):
        if not p["child"] and p["session"]:
            by_session.setdefault((p["provider"], p["session"]), []).append(i)
    for (_, sid), idxs in by_session.items():
        if len(idxs) < 2:
            continue
        owner = min(idxs, key=lambda i: starttime_of(procs[i]["pid"]))
        for i in idxs:
            procs[i]["session_shared"] = True
            if i != owner:
                procs[i]["session"] = None
                if sid not in procs[i]["session_candidates"]:
                    procs[i]["session_candidates"].insert(0, sid)
    procs.sort(key=lambda p: (p["provider"], p["account"] or "", p["pid"]))
    return procs


def cmd_who(args):
    """who [--json] — pid→cred attribution table for the live fleet."""
    rows = scan()
    if "--json" in args:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("helm who: no live claude/codex agent processes.")
        return 0
    home = os.path.expanduser("~")
    tilde = lambda p: p.replace(home, "~", 1) if p else "-"
    print("helm who: %d agent process%s (pid → cred)" % (
        len(rows), "es"[:2 * (len(rows) != 1)]))
    print("  %-8s %-9s %-28s %-26s %-40s %s" % (
        "pid", "provider", "account", "home", "session", "cwd"))
    for r in rows:
        sess = r["session"] or (", ".join(r["session_candidates"][:2]) + "?"
                                if r["session_candidates"] else "-")
        flags = "".join((" [child]" if r["child"] else "",
                         " [SHARED]" if r["session_shared"] else ""))
        print("  %-8d %-9s %-28s %-26s %-40s %s%s" % (
            r["pid"], r["provider"], (r["account"] or "-")[:28],
            tilde(r["home"])[:26], sess[:40], tilde(r["cwd"]), flags))
    return 0
