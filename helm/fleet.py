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
proves. SID truth is helm/session.py's WHOLE census (_proc_claude_rows: the
procStart-bound pid record, fail-closed --resume argv, who attribution, cwd
candidate set, and the final generation recheck) — fleet consumes those rows
verbatim and only composes them with its own display probes. The census also
exports each row's bracketed cwd and canonical trusted config root, so fleet
never re-derives a config home either.

One row per live claude process:
  pid, seat (HELM_CHAT_NAME or roster reverse-lookup), sid (census identity
  ladder; a sid proven live in MULTIPLE pids is flagged DOUBLE-OPEN — that is
  what a second holder proves, never that this process is a fork), home
  (session's canonical config root; '?' when untrusted/unproven), daemon
  (ppid-walk to an orca daemon whose incarnation is re-proven at match time),
  pane (orca terminal handle when the join is unambiguous; '?' otherwise —
  never guessed), stamps (child-session trio count), deck (MC = stale MC
  skill-deck env, helm = repointed, - = unset).

Every column comes from a live probe; nothing is cached. A FAILED probe is
UNKNOWN, never an absence fact (premise failed-probe-not-absence): HEADLESS is
a PROVEN verdict (a fully-parsed ppid walk that reached init); an unparsable
hop, exhausted walk, stale daemon evidence, failed daemon scan, unreadable
environ/cwd, missing census row, or failed terminal list renders host/columns
as '?' and marks the row UNKNOWN. The verb is read-only and safe to run at
any moment.
"""
import glob
import json
import os
import subprocess

from . import session

_DAEMON_ENTRY = "daemon-entry.js"
# the runtimes that actually execute the orca daemon script (orca-ide is the
# packaged electron binary; node/electron cover dev shapes)
_ORCA_RUNTIMES = {"orca-ide", "orca", "electron", "node"}
_SID_SRC = {"declared": "record", "resume": "argv", "who": "who"}
# census reasons that mean the record/environ PROBE failed (vs an affirmative
# fact like record-missing/record-stale): an unresolved sid under one of these
# is UNKNOWN, not a proven blank
_FAILED_PROBE_REASONS = {"environ-unreadable", "record-unreadable",
                         "record-replaced"}


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


def _census():
    """{pid: row} straight from session._proc_claude_rows() — the ONE sid
    truth owner (record/argv/who/cwd rungs, generation recheck, canonical
    config root). Fleet never re-derives any of it."""
    return {r["pid"]: r for r in session._proc_claude_rows()}


def _cmdline_argv(pid):
    """NUL-split argv, or None when the probe failed (never [] — a failed
    read must not look like an empty command line)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return None


def _is_daemon_argv(argv):
    """The ACTUAL orca daemon argv shape, nothing looser: a known orca/JS
    runtime (argv[0] basename) whose SCRIPT argument — the first non-flag
    element after argv[0] — is daemon-entry.js, or the script executed
    directly. `cat /tmp/daemon-entry.js`, `python worker.py daemon-entry.js`,
    substring lookalikes, and flag-embedded paths are NOT daemons."""
    argv0 = os.path.basename(argv[0]) if argv and argv[0] else ""
    if argv0 == _DAEMON_ENTRY:
        return True
    if argv0 not in _ORCA_RUNTIMES:
        return False
    for a in argv[1:]:
        if not a or a.startswith("-"):
            continue
        return os.path.basename(a) == _DAEMON_ENTRY
    return False


def _daemon_pids():
    """({pid: starttime}, scan_failed). Every daemon pid is BRACKETED with
    its starttime (session._proc_start, the canonical field-22 reader) so a
    later membership hit can re-prove the same incarnation instead of
    trusting a bare number across PID reuse. scan_failed=True means the /proc
    listing itself failed — daemon truth is then UNKNOWN for every row."""
    out = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return {}, True
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        argv = _cmdline_argv(pid)
        if argv is None or not _is_daemon_argv(argv):
            continue
        start = session._proc_start(pid)
        if start:
            out[pid] = start
    return out, False


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
    """('daemon', pid) | ('headless', None) | ('unknown', None).

    HEADLESS is only PROVEN by a fully-parsed ppid walk that reached init.
    A daemon hit is only PROVEN by re-reading the candidate at match time:
    its argv must still be daemon-shaped AND its live starttime must equal
    the one bracketed at scan time — stale set membership across PID reuse
    is contradictory evidence, not a host. Any unparsable hop, exhausted
    depth, or failed recheck is UNKNOWN (cannot prove), never an absence."""
    cur = pid
    for _ in range(12):
        ppid = _stat_ppid(cur)
        if ppid is None:
            return "unknown", None
        if ppid <= 1:
            return "headless", None
        if ppid in daemons:
            argv = _cmdline_argv(ppid)
            if (argv is not None and _is_daemon_argv(argv)
                    and session._proc_start(ppid) == daemons[ppid]):
                return "daemon", ppid
            return "unknown", None
        cur = ppid
    return "unknown", None


def _orca_terminals():
    """(terminals, failed). `orca terminal list --json`; a non-zero exit or
    any exception is failed=True — pane truth is then UNKNOWN for hosted
    rows, never silently identical to 'no terminals exist'."""
    try:
        p = subprocess.run(["orca", "terminal", "list", "--json"],
                           capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return [], True
        terms = (json.loads(p.stdout).get("result") or {}).get("terminals")
        return [t for t in terms or [] if isinstance(t, dict)], False
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [], True


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
    daemons, daemons_failed = _daemon_pids()
    roster, roster_err = _roster()
    stamp_keys = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                  "CLAUDE_CODE_BRIDGE_SESSION_ID")
    census = _census()
    live = session.live_sids(list(census.values()))
    pids = sorted(set(_claude_pids()) | set(census))
    tilde = lambda p: p.replace(os.path.expanduser("~"), "~")  # noqa: E731
    out, raw_cwds = [], {}
    for pid in pids:
        env = _environ(pid)
        env_unknown = env is None  # failed probe, NOT an empty environment
        sr = census.get(pid)
        sid = sr["session"] if sr else None
        sid_src = _SID_SRC.get(sr["identity"]) if sr else None
        candidates = list(sr["possible_sessions"]) if sr else []
        # a census row can itself be a failed probe: unresolved because the
        # record/environ READ failed, not because evidence proved a blank
        sid_unknown = sr is None or (
            sid is None and sr.get("declared_reason") in _FAILED_PROBE_REASONS)
        double_open = bool(sid) and len(live.get(sid) or ()) > 1
        seat, seat_src = _seat_for(pid, env, roster, roster_err)
        deck = (env or {}).get("HELM_SKILL_DECK", "")
        cwd = sr["cwd"] if sr else None  # the census's BRACKETED cwd first
        if cwd is None:
            try:
                cwd = os.readlink("/proc/%d/cwd" % pid)
            except OSError:
                cwd = None
        raw_cwds[pid] = cwd
        if daemons_failed:
            daemon_state, daemon = "unknown", None
        else:
            daemon_state, daemon = _daemon_for(pid, daemons)
        root = sr["root"] if sr else None
        out.append({
            "pid": pid,
            "seat": seat, "seat_src": seat_src,
            "sid": sid, "sid_src": sid_src,
            "candidates": candidates,
            "double_open": double_open,
            "unknown": (env_unknown or sid_unknown or cwd is None
                        or daemon_state == "unknown"),
            "home": tilde(root) if root else "?",
            "cwd": tilde(cwd) if cwd else "?",
            "daemon": daemon, "daemon_state": daemon_state,
            "pane": None,
            "stamps": None if env_unknown else
                      sum(1 for k in stamp_keys if k in env),
            "deck": "?" if env_unknown else
                    ("MC" if "mission-control" in deck or "/.mc/" in deck
                     else "helm" if deck else "-"),
        })
    hosted = [r for r in out if r["daemon_state"] == "daemon"]
    if hosted:
        terminals, terms_failed = _orca_terminals()
        counts = {}
        for r in hosted:
            c = raw_cwds[r["pid"]]
            counts[c] = counts.get(c, 0) + 1
        shared = {c for c, n in counts.items() if c and n > 1}
        for r in hosted:
            r["pane"] = _pane_for(raw_cwds[r["pid"]], terminals, shared)
            if terms_failed:
                r["unknown"] = True  # pane truth unavailable = failed probe
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
        if r["sid_src"] in ("argv", "who"):
            tag = " (%s)" % r["sid_src"]
        elif r["sid_src"] == "record":
            tag = ""
        elif r["candidates"]:
            tag = " (%d cwd-candidate(s))" % len(r["candidates"])
        else:
            tag = " (UNRESOLVED)"
        if r["double_open"]:
            tag += " DOUBLE-OPEN"
        host = ("daemon %d pane=%s" % (r["daemon"], r["pane"] or "?")
                if r["daemon_state"] == "daemon" else
                "HEADLESS/no-pane" if r["daemon_state"] == "headless"
                else "host=?")
        seat = r["seat"] or ("?" if r["unknown"] else "(no seat)")
        stamps = "?" if r["stamps"] is None else str(r["stamps"])
        print("  pid %-8d %-18s sid=%s%s%s"
              % (r["pid"], seat, sid8, tag,
                 " UNKNOWN" if r["unknown"] else ""))
        print("       %-13s stamps=%s deck=%-5s home=%s cwd=%s"
              % (host, stamps, r["deck"], r["home"], r["cwd"]))
    ghosts = [r for r in table if r["daemon_state"] == "headless"]
    if ghosts:
        print("  ⚠ %d process(es) have NO orca pane — the owner cannot see or "
              "type at them" % len(ghosts))
    unknowns = [r for r in table
                if r["unknown"] or r["seat_src"] == "roster-error"]
    if unknowns:
        print("  ? %d row(s) carry UNKNOWN columns — failed probes, not "
              "absence; verify by hand before acting" % len(unknowns))
    doubles = sorted({r["sid"] for r in table if r["double_open"]})
    if doubles:
        print("  ‼ %d sid(s) are live in MULTIPLE pids (DOUBLE-OPEN) — "
              "resolve before resuming or injecting" % len(doubles))
    return 0
