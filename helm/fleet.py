#!/usr/bin/env python3
"""helm fleet — the ONE composition-truth table.

Born 2026-07-22, the morning the integrator hand-rolled the same /proc census
five times, each slightly differently, and every fleet mistake that day was a
stale-mental-model error: text injected into the wrong pane on a remembered
title, a seat declared running on a request-sent, an identity assembled from
three eras of belief. The design answer is not care — it is that COMPOSITION
QUESTIONS GET ANSWERED BY RUNNING THIS VERB, never from memory, and answers
given to anyone (owner included) quote its output.

One row per live claude process:
  pid, seat (HELM_CHAT_NAME or roster reverse-lookup), sid (authoritative
  pid-record first), home (environ truth), daemon (ppid-walk to an orca
  daemon; NONE = headless/invisible-to-owner), pane (orca handle when the
  daemon's terminal list can name it), stamps (child-session trio count),
  deck (MC = stale MC skill-deck env, helm = repointed, - = unset).

Every column comes from a live probe; nothing is cached. The verb is
read-only and safe to run at any moment.
"""
import glob
import json
import os
import sys

from . import sessions


def _environ(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            return dict(kv.split("=", 1) for kv in
                        f.read().decode("utf-8", "replace").split("\0")
                        if "=" in kv)
    except OSError:
        return {}


def _claude_pids():
    out = []
    for entry in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(entry, "rb") as f:
                if f.read().strip() != b"claude":
                    continue
            out.append(int(entry.split("/")[2]))
        except (OSError, ValueError):
            continue
    return sorted(out)


def _daemon_pids():
    out = []
    for entry in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(entry, "rb") as f:
                argv = f.read().decode("utf-8", "replace")
            if "daemon-entry.js" in argv:
                out.append(int(entry.split("/")[2]))
        except (OSError, ValueError):
            continue
    return set(out)


def _daemon_for(pid, daemons):
    cur = pid
    for _ in range(12):
        try:
            with open("/proc/%d/stat" % cur, "rb") as f:
                ppid = int(f.read().decode("utf-8", "replace")
                           .rpartition(")")[2].split()[1])
        except (OSError, IndexError, ValueError):
            return None
        if ppid <= 1:
            return None
        if ppid in daemons:
            return ppid
        cur = ppid
    return None


def _sid_for(pid, env):
    home = env.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    path = os.path.join(home, "sessions", "%d.json" % pid)
    try:
        with open(path) as f:
            rec = json.load(f)
        if rec.get("pid") == pid and rec.get("sessionId"):
            return rec["sessionId"], "record"
    except (OSError, ValueError):
        pass
    # argv rung, labeled as the weaker source it is (resume FORKS: argv names
    # the ancestor — see premise resume-forks-argv-sid-is-the-ancestor)
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = f.read().decode("utf-8", "replace").split("\0")
        for flag in ("--resume", "-r"):
            if flag in argv:
                i = argv.index(flag)
                if i + 1 < len(argv):
                    return argv[i + 1], "argv~ancestor"
    except OSError:
        pass
    return None, None


def _seat_for(pid, env, roster):
    name = env.get("HELM_CHAT_NAME")
    if name:
        return name, "env"
    for seat, row in (roster or {}).items():
        if str(row.get("pid") or "") == str(pid):
            return seat, "roster"
    return None, None


def rows():
    daemons = _daemon_pids()
    try:
        from . import seats
        roster = seats.roster()
    except Exception:
        roster = {}
    stamp_keys = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                  "CLAUDE_CODE_BRIDGE_SESSION_ID")
    out = []
    for pid in _claude_pids():
        env = _environ(pid)
        sid, sid_src = _sid_for(pid, env)
        seat, seat_src = _seat_for(pid, env, roster)
        deck = env.get("HELM_SKILL_DECK", "")
        home = env.get("CLAUDE_CONFIG_DIR") or "~/.claude"
        try:
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            cwd = "?"
        out.append({
            "pid": pid,
            "seat": seat, "seat_src": seat_src,
            "sid": sid, "sid_src": sid_src,
            "home": home.replace(os.path.expanduser("~"), "~"),
            "cwd": cwd.replace(os.path.expanduser("~"), "~"),
            "daemon": _daemon_for(pid, daemons),
            "stamps": sum(1 for k in stamp_keys if k in env),
            "deck": ("MC" if "mission-control" in deck or "/.mc/" in deck
                     else "helm" if deck else "-"),
        })
    return out, sorted(daemons)


def cmd_fleet(args):
    """fleet [--json] — every live claude process, composition truth."""
    table, daemons = rows()
    if "--json" in args:
        print(json.dumps({"rows": table, "daemons": daemons}, indent=2))
        return 0
    print("helm fleet — %d live claude process(es), %d orca daemon(s)"
          % (len(table), len(daemons)))
    for r in table:
        sid8 = (r["sid"] or "?")[:8]
        tag = "" if r["sid_src"] == "record" else (
            " (%s)" % r["sid_src"] if r["sid_src"] else " (UNRESOLVED)")
        host = ("daemon %d" % r["daemon"]) if r["daemon"] else "HEADLESS/no-pane"
        print("  pid %-8d %-18s sid=%s%s" % (r["pid"], r["seat"] or "(no seat)",
                                             sid8, tag))
        print("       %-13s stamps=%d deck=%-5s home=%s cwd=%s"
              % (host, r["stamps"], r["deck"], r["home"], r["cwd"]))
    ghosts = [r for r in table if not r["daemon"]]
    if ghosts:
        print("  ⚠ %d process(es) have NO orca pane — the owner cannot see or "
              "type at them" % len(ghosts))
    return 0
