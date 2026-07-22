#!/usr/bin/env python3
"""helm fleet — the ONE composition-truth table.

Born 2026-07-22, the morning the integrator hand-rolled the same /proc census
five times, each slightly differently, and every fleet mistake that day was a
stale-mental-model error: text injected into the wrong pane on a remembered
title, a seat declared running on a request-sent, an identity assembled from
three eras of belief. The design answer is not care — it is that COMPOSITION
QUESTIONS GET ANSWERED BY RUNNING THIS VERB, never from memory, and answers
given to anyone (owner included) quote its output.

ONE TRUTH OWNER: this verb never re-derives what another module already
proves. SID resolution is helm/session.py's (procStart-bound pid record via
_proc_snapshot/_session_record, then its fail-closed _resume_sid argv parse);
fleet only composes those answers into rows.

One row per live claude process:
  pid, seat (HELM_CHAT_NAME or roster reverse-lookup), sid (authoritative
  pid-record first), home (environ truth), daemon (ppid-walk to an orca
  daemon; NONE = headless/invisible-to-owner), pane (orca terminal handle
  when the join is unambiguous; '?' when un-mappable — never guessed),
  stamps (child-session trio count), deck (MC = stale MC skill-deck env,
  helm = repointed, - = unset).

Every column comes from a live probe; nothing is cached. A FAILED probe is
UNKNOWN, never an absence fact (premise failed-probe-not-absence): unreadable
environ renders home/deck/seat as '?', marks the row UNKNOWN, and the footer
counts such rows. The verb is read-only and safe to run at any moment.
"""
import glob
import json
import os
import subprocess
import sys

from . import session

_DAEMON_ENTRY = "daemon-entry.js"


def _environ(pid):
    """Full environ dict, or None when the probe FAILED. None is not {}:
    an unreadable environ must surface as UNKNOWN, never as unset-vars."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            return dict(kv.split("=", 1) for kv in
                        f.read().decode("utf-8", "replace").split("\0")
                        if "=" in kv)
    except OSError:
        return None


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


def _is_daemon_argv(argv):
    """daemon-entry.js as an EXACT argv element (cmdline split on NUL), by
    itself or as the last path component — never a substring of the raw
    cmdline, so `tail -f daemon-entry.js.log`, `not-daemon-entry.js`, and
    flag-embedded lookalikes are not daemons. Flag-shaped elements never
    match: a path passed as a --flag=value is an argument, not the script."""
    for a in argv:
        if a == _DAEMON_ENTRY:
            return True
        if not a.startswith("-") and os.path.basename(a) == _DAEMON_ENTRY:
            return True
    return False


def _daemon_pids():
    out = set()
    for entry in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(entry, "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
            if _is_daemon_argv(argv):
                out.add(int(entry.split("/")[2]))
        except (OSError, ValueError):
            continue
    return out


def _stat_ppid(pid):
    """ppid from /proc/<pid>/stat, VALIDATED per parse: comm is parenthesized
    and may itself contain spaces and ')' — field counting starts after the
    LAST close-paren (same discipline as session._starttime_from_stat, the
    canonical field-22 reader). Any malformed payload is None, never a wild
    int from a hostile comm."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    _comm, sep, tail = raw.rpartition(b")")
    if not sep:
        return None
    fields = tail.split()
    # tail: state ppid pgrp ... — need both, and ppid must be a pure decimal
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


def _daemon_for(pid, daemons):
    """Nearest orca-daemon ancestor via a bracketed ppid walk: every hop is a
    validated /proc/<pid>/stat parse; a single unparsable hop ends the walk
    (None = cannot prove a daemon host, not proof of headlessness)."""
    cur = pid
    for _ in range(12):
        ppid = _stat_ppid(cur)
        if ppid is None or ppid <= 1:
            return None
        if ppid in daemons:
            return ppid
        cur = ppid
    return None


def _orca_terminals():
    """`orca terminal list --json`, best-effort. [] on ANY failure — pane
    mapping then degrades to '?', never to a guessed handle."""
    try:
        p = subprocess.run(["orca", "terminal", "list", "--json"],
                           capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return []
        terms = (json.loads(p.stdout).get("result") or {}).get("terminals")
        return [t for t in terms or [] if isinstance(t, dict)]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _pane_for(cwd, terminals, shared_cwds):
    """BEST-EFFORT orca pane handle for one daemon-hosted process. The
    terminal list carries no pids, so the only honest join is process cwd ==
    terminal worktreePath, and only when UNIQUE on both sides: exactly one
    terminal at that path AND no sibling claude row sharing the cwd.
    Anything ambiguous is None (rendered pane=?) — never a guess."""
    if not cwd or cwd in shared_cwds:
        return None
    hits = [t.get("handle") for t in terminals
            if t.get("worktreePath") == cwd and t.get("handle")]
    return hits[0] if len(hits) == 1 else None


def _sid_for(pid, snap, declared, record_pids):
    """SID for one process, ANSWERED BY helm/session.py (the one truth owner,
    never re-derived here):
      rung 1 — `declared`, session._session_record's procStart+uid+schema-
        guarded pid record (validated against /proc stat field 22);
      rung 2 — session._resume_sid over the bracketed snapshot argv
        (fail-closed: bare/trailing --resume, a flag consumed as the value,
        --resume= forms, and conflicting repeats all resolve to None).
    An argv sid is 'argv~ancestor' ONLY when a DIFFERENT live pid's record
    already owns that sid — then this process provably forked and argv names
    the ancestor. A direct resume with no competing record holder is plain
    'argv': a resume is not an ancestor claim by itself."""
    if declared:
        return declared, "record"
    if snap is None:
        return None, None
    resume = session._resume_sid(snap["argv"])
    if resume:
        holder = record_pids.get(resume)
        return resume, ("argv~ancestor" if holder not in (None, pid)
                        else "argv")
    return None, None


def _sids_for(pids):
    """{pid: (sid, src)} for the census, via session.py's bracketed snapshot
    (uid + procStart + re-read guards) and record reader. Records resolve in
    a first pass so the ancestor test can see every proven holder."""
    snaps = {p: session._proc_snapshot(p) for p in pids}
    declared = {}
    for p, snap in snaps.items():
        if snap and snap["environ"] is not None:
            env = snap["env"] or {}
            declared[p] = session._session_record(
                p, env.get("CLAUDE_CONFIG_DIR"), env.get("HOME"),
                snap["uid"], snap["start"])[0]
    record_pids = {sid: p for p, sid in declared.items() if sid}
    return {p: _sid_for(p, snaps[p], declared.get(p), record_pids)
            for p in pids}


def _roster():
    """(roster, failed). A roster exception is a FAILED PROBE — it must
    surface as seat_src='roster-error', never masquerade as an empty roster."""
    try:
        from . import seats
        return seats.roster() or {}, False
    except Exception:
        return {}, True


def _seat_for(pid, env, roster, roster_err):
    name = (env or {}).get("HELM_CHAT_NAME")
    if name:
        return name, "env"
    if roster_err:
        return None, "roster-error"
    for seat, row in (roster or {}).items():
        if str(row.get("pid") or "") == str(pid):
            return seat, "roster"
    return None, None


def rows():
    daemons = _daemon_pids()
    roster, roster_err = _roster()
    stamp_keys = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                  "CLAUDE_CODE_BRIDGE_SESSION_ID")
    pids = _claude_pids()
    sids = _sids_for(pids)
    tilde = lambda p: p.replace(os.path.expanduser("~"), "~")  # noqa: E731
    out, raw_cwds = [], {}
    for pid in pids:
        env = _environ(pid)
        unknown = env is None  # failed probe, NOT an empty environment
        sid, sid_src = sids.get(pid, (None, None))
        seat, seat_src = _seat_for(pid, env, roster, roster_err)
        deck = (env or {}).get("HELM_SKILL_DECK", "")
        try:
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            cwd = None
        raw_cwds[pid] = cwd
        out.append({
            "pid": pid,
            "seat": seat, "seat_src": seat_src,
            "sid": sid, "sid_src": sid_src,
            "unknown": unknown,
            "home": "?" if unknown else
                    tilde(env.get("CLAUDE_CONFIG_DIR") or "~/.claude"),
            "cwd": tilde(cwd) if cwd else "?",
            "daemon": _daemon_for(pid, daemons),
            "pane": None,
            "stamps": None if unknown else
                      sum(1 for k in stamp_keys if k in env),
            "deck": "?" if unknown else
                    ("MC" if "mission-control" in deck or "/.mc/" in deck
                     else "helm" if deck else "-"),
        })
    hosted = [r for r in out if r["daemon"]]
    if hosted:
        terminals = _orca_terminals()
        counts = {}
        for r in hosted:
            c = raw_cwds[r["pid"]]
            counts[c] = counts.get(c, 0) + 1
        shared = {c for c, n in counts.items() if c and n > 1}
        for r in hosted:
            r["pane"] = _pane_for(raw_cwds[r["pid"]], terminals, shared)
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
        host = ("daemon %d pane=%s" % (r["daemon"], r["pane"] or "?")
                if r["daemon"] else "HEADLESS/no-pane")
        seat = r["seat"] or ("?" if r["unknown"] else "(no seat)")
        stamps = "?" if r["stamps"] is None else str(r["stamps"])
        print("  pid %-8d %-18s sid=%s%s%s"
              % (r["pid"], seat, sid8, tag,
                 " UNKNOWN" if r["unknown"] else ""))
        print("       %-13s stamps=%s deck=%-5s home=%s cwd=%s"
              % (host, stamps, r["deck"], r["home"], r["cwd"]))
    ghosts = [r for r in table if not r["daemon"]]
    if ghosts:
        print("  ⚠ %d process(es) have NO orca pane — the owner cannot see or "
              "type at them" % len(ghosts))
    unknowns = [r for r in table
                if r["unknown"] or r["seat_src"] == "roster-error"]
    if unknowns:
        print("  ? %d row(s) carry UNKNOWN columns — failed probes, not "
              "absence; verify by hand before acting" % len(unknowns))
    return 0
