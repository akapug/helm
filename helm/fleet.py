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
proves. SID truth is helm/session.py's WHOLE census (_proc_claude_census:
the procStart-bound pid record, fail-closed --resume argv, who attribution,
cwd candidate set, and the final generation recheck) — fleet consumes those
rows verbatim and only composes them with its own display probes. The census
also exports each row's bracketed cwd, canonical trusted config root,
bracket generation (start) and bracketed WHOLE environ, so fleet never
re-derives a config home and never re-opens a proc environ file: seat, deck
and stamp facts come from the same coherent read the census proved, never
from a later moment a reused pid could answer. Consumed WHOLE means the
census is also the ONLY row source: no second /proc comm scan may resurrect
a pid the census rejected (its generation recheck failed — no coherent facts
exist for it), and a census cwd=None is never patched with a later
unbracketed /proc/<pid>/cwd read, because that would compose two process
generations into one row and could join a pane across them. And it is
consumed WITH its completeness channel: a failed /proc enumeration surfaces
as census_failed — the verb prints estate-UNKNOWN and exits 1, never
certifying an empty estate it never read — and a failed who scan marks every
sub-declared/resume row sid-UNKNOWN instead of letting the vanished rung
read as a proven blank. The same discipline holds PER PID below those
estate-wide bits: a mandatory probe that failed AFTER comm proved claude
arrives as a probe_failed UNKNOWN row (rendered, counted, exit 1 — never
dropped as proven absence), and one that failed BEFORE comm could prove or
refute claude arrives as census_partial — the estate total prints as a
floor ("at least N, CENSUS PARTIAL") and the verb exits 1. EVERY estate-wide
failed probe — who scan, daemon scan, terminal list — travels the same way
as census_failed/census_partial: a named completeness bit in the --json
envelope and exit 1, so a machine consumer keying on rc or the estate bits
can never read PASS while that truth went unprobed.

The one display probe that DOES re-read /proc after the census — the
daemon/host ppid walk — is only composed in after a FINAL generation
recheck (_generation_intact: live starttime == the row's bracketed start),
run after all display probes: a pid reused between census and walk must
never combine the old row's sid/home/cwd with the new process's ancestry,
so a failed recheck kills daemon/pane to UNKNOWN.

One row per census (same-uid live claude) process:
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
a PROVEN verdict (a fully-parsed ppid walk that reached init, touching no pid
the daemon scan left unproven); an unparsable hop, exhausted walk, stale or
unprovable daemon evidence, failed daemon scan, unreadable environ/cwd,
untrusted config root, failed roster, failed/misshapen terminal list, failed
who scan, or failed final generation recheck
renders host/columns as '?' and marks the row UNKNOWN. The verb is read-only
and safe to run at any moment.
"""
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
                         "record-replaced", "probe-failed"}


def _census():
    """({pid: row}, census_failed, who_failed, census_partial) straight from
    session._proc_claude_census() — the ONE sid truth owner (record/argv/who/
    cwd rungs, generation recheck, canonical config root, bracketed environ).
    Fleet never re-derives any of it. census_failed means the /proc
    enumeration itself failed: the empty table is a FAILED PROBE, not a
    proven-empty estate. who_failed means the who rung was never probed.
    census_partial means a mandatory per-pid probe failed before the pid's
    comm could prove or refute claude: the row count is a FLOOR ('at least
    N'), never a certified estate total."""
    c = session._proc_claude_census()
    return ({r["pid"]: r for r in c["rows"]}, c["listing_failed"],
            c["who_failed"], c["census_partial"])


def _generation_intact(pid, start):
    """The census generation still owns the pid NOW. Run AFTER the row's
    display probes: the census row was proven coherent at return time, but
    the host walk re-reads /proc later — if the pid was reused in between,
    those reads describe a DIFFERENT process and must not be composed onto
    the census facts. start=None never verifies (an unbracketed row cannot
    be re-proven)."""
    return start is not None and session._proc_start(pid) == start


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
    """({pid: starttime}, unproven_pids, scan_failed). Every daemon pid is
    BRACKETED with its starttime (session._proc_start, the canonical field-22
    reader) so a later membership hit can re-prove the same incarnation
    instead of trusting a bare number across PID reuse. scan_failed=True
    means the /proc listing itself failed — daemon truth is then UNKNOWN for
    every row. UNPROVEN pids are PARTIAL probe failures — an unreadable
    cmdline (cannot be ruled a daemon OR ruled out) or a daemon-shaped argv
    whose starttime read failed (a daemon that cannot be incarnation-proven).
    They must never silently vanish behind scan_failed=False: a ppid walk
    that reaches one answers UNKNOWN, never walks through it to init and
    claims a proven HEADLESS."""
    out, unproven = {}, set()
    try:
        names = os.listdir("/proc")
    except OSError:
        return {}, set(), True
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        argv = _cmdline_argv(pid)
        if argv is None:
            unproven.add(pid)
            continue
        if not _is_daemon_argv(argv):
            continue
        start = session._proc_start(pid)
        if start:
            out[pid] = start
        else:
            unproven.add(pid)
    return out, unproven, False


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


def _daemon_for(pid, daemons, unproven):
    """('daemon', pid) | ('headless', None) | ('unknown', None).

    HEADLESS is only PROVEN by a fully-parsed ppid walk that reached init.
    A daemon hit is only PROVEN by re-reading the candidate at match time:
    its argv must still be daemon-shaped AND its live starttime must equal
    the one bracketed at scan time — stale set membership across PID reuse
    is contradictory evidence, not a host. Any unparsable hop, exhausted
    depth, failed recheck, or hop through an UNPROVEN pid (the daemon scan
    could not read its cmdline or prove its incarnation) is UNKNOWN (cannot
    prove), never an absence."""
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
        if ppid in unproven:
            return "unknown", None
        cur = ppid
    return "unknown", None


def _orca_terminals():
    """(terminals, failed). `orca terminal list --json`; a non-zero exit,
    any exception, or a successful reply whose JSON is NOT the documented
    {result: {terminals: [dict...]}} shape is failed=True — pane truth is
    then UNKNOWN for hosted rows, never silently identical to 'no terminals
    exist' and never a crash of the whole truth verb."""
    try:
        p = subprocess.run(["orca", "terminal", "list", "--json"],
                           capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return [], True
        data = json.loads(p.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [], True
    result = data.get("result") if isinstance(data, dict) else None
    terms = result.get("terminals") if isinstance(result, dict) else None
    if not isinstance(terms, list) or not all(
            isinstance(t, dict) for t in terms):
        return [], True
    return terms, False


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
    census, census_failed, who_failed, census_partial = _census()
    # the daemon scan runs AFTER the census bracket: a daemon that started
    # between the two scans — whose freshly-spawned claude IS censused — is
    # then in the set, so the ppid walk can never pass through the missing
    # pid to init and read a false proven-HEADLESS for a live-pane process
    daemons, unproven, daemons_failed = _daemon_pids()
    roster, roster_err = _roster()
    stamp_keys = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                  "CLAUDE_CODE_BRIDGE_SESSION_ID")
    live = session.live_sids(list(census.values()))
    tilde = lambda p: p.replace(os.path.expanduser("~"), "~")  # noqa: E731
    out, raw_cwds = [], {}
    for pid in sorted(census):  # the census is the ONLY row source
        sr = census[pid]
        # the census's BRACKETED environ, verbatim — never a live re-read
        # that a pid reused since the census could answer
        env = sr["environ"]
        env_unknown = env is None  # failed probe, NOT an empty environment
        sid = sr["session"]
        sid_src = _SID_SRC.get(sr["identity"])
        candidates = list(sr["possible_sessions"])
        # a census row can itself be a failed probe: unresolved because the
        # record/environ READ failed — or because the who rung was never
        # probed at all — not because evidence proved a blank
        # a mandatory probe that failed AFTER comm proved claude: the pid is
        # known, every fact on it is unprovable — the row is UNKNOWN and the
        # verb's exit status must say so, never a silent drop
        probe_failed = bool(sr.get("probe_failed"))
        sid_unknown = sid is None and (
            sr.get("declared_reason") in _FAILED_PROBE_REASONS
            or (who_failed and not sr.get("child")))
        double_open = bool(sid) and len(live.get(sid) or ()) > 1
        seat, seat_src = _seat_for(pid, env, roster, roster_err)
        deck = (env or {}).get("HELM_SKILL_DECK", "")
        # the census's BRACKETED cwd, verbatim: cwd=None is a failed probe
        # and stays UNKNOWN — a later /proc read would be a different
        # process-generation moment and must never be composed in
        cwd = sr["cwd"]
        raw_cwds[pid] = cwd
        if daemons_failed:
            daemon_state, daemon = "unknown", None
        else:
            daemon_state, daemon = _daemon_for(pid, daemons, unproven)
        root = sr["root"]
        out.append({
            "pid": pid,
            "seat": seat, "seat_src": seat_src,
            "sid": sid, "sid_src": sid_src,
            "candidates": candidates,
            "double_open": double_open,
            "probe_failed": probe_failed,
            # the row-level bit carries EVERY failed probe the row rests on
            # (home/config root and seat included) so JSON consumers never
            # receive a false known-row bit the footer contradicts
            "unknown": (probe_failed or env_unknown or sid_unknown
                        or cwd is None
                        or root is None or seat_src == "roster-error"
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
    terms_failed = False
    if hosted:
        terminals, terms_failed = _orca_terminals()
        counts = {}
        # the ambiguity set spans EVERY sibling claude row not PROVEN
        # pane-less: an unknown-host sibling is not refuted daemon-hosted —
        # it may sit in that very terminal — so it makes the cwd join
        # ambiguous; only a proven-HEADLESS sibling (walked to init) is
        # excludable. Guessing here is the founding failure (text injected
        # into the wrong pane).
        for r in out:
            if r["daemon_state"] == "headless":
                continue
            c = raw_cwds[r["pid"]]
            counts[c] = counts.get(c, 0) + 1
        shared = {c for c, n in counts.items() if c and n > 1}
        for r in hosted:
            r["pane"] = _pane_for(raw_cwds[r["pid"]], terminals, shared)
            if terms_failed:
                r["unknown"] = True  # pane truth unavailable = failed probe
    # FINAL generation recheck, AFTER every display probe: the host walk
    # re-read /proc later than the census bracket. If the pid's generation
    # changed in between, those fresh reads describe a DIFFERENT process
    # (PID reuse) — their conclusions (daemon, proven HEADLESS, pane) must
    # die UNKNOWN, never be composed onto the census facts. Env facts need
    # no recheck: they come from the census's own bracket.
    for r in out:
        if _generation_intact(r["pid"], census[r["pid"]]["start"]):
            continue
        r["daemon"], r["daemon_state"], r["pane"] = None, "unknown", None
        r["unknown"] = True
    return out, sorted(daemons), {
        "census_failed": census_failed, "census_partial": census_partial,
        "who_failed": who_failed, "daemons_failed": daemons_failed,
        "terms_failed": terms_failed}


def cmd_fleet(args):
    """fleet [--json] — every live claude process, composition truth."""
    # flags-only membership reader — guard the tail before the census scan so
    # `fleet --bogus` refuses rc 2 instead of silently printing the estate and
    # exiting 0 (an unknown flag must never ride through as if it existed)
    from .cli import guard_tail
    rc = guard_tail("helm fleet", args, flags=("--json",), usage="fleet [--json]")
    if rc is not None:
        return rc
    table, daemons, flags = rows()
    probe_failed = [r for r in table if r["probe_failed"]]
    # EVERY completeness bit gates the exit code: a failed who scan, daemon
    # scan, or terminal list is an estate-wide failed probe exactly like a
    # failed/partial census — a scripted consumer keying on rc (or on the
    # JSON estate bits) must never read PASS while that truth went unprobed
    rc = 1 if probe_failed or any(flags.values()) else 0
    if "--json" in args:
        print(json.dumps({"rows": table, "daemons": daemons, **flags},
                         indent=2))
        return rc
    census_failed, census_partial = (flags["census_failed"],
                                     flags["census_partial"])
    if census_failed:
        print("helm fleet — CENSUS FAILED: /proc could not be enumerated. "
              "The estate is UNKNOWN, not empty — zero rows is a failed "
              "probe, never proven absence.")
        return 1
    print("helm fleet — %s live claude process(es), %d orca daemon(s)%s"
          % (("at least %d" % len(table)) if census_partial else len(table),
             len(daemons), " — CENSUS PARTIAL" if census_partial else ""))
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
    unknowns = [r for r in table if r["unknown"]]
    if unknowns:
        print("  ? %d row(s) carry UNKNOWN columns — failed probes, not "
              "absence; verify by hand before acting" % len(unknowns))
    estate = [name for name, bit in (("who scan", flags["who_failed"]),
                                     ("daemon scan", flags["daemons_failed"]),
                                     ("terminal list", flags["terms_failed"]))
              if bit]
    if estate:
        print("  ! estate-wide probe(s) FAILED: %s — the affected columns "
              "are UNKNOWN, never proven blanks" % ", ".join(estate))
    if probe_failed:
        print("  ! pid(s) %s PROVED claude but a mandatory census probe "
              "FAILED — facts unprovable (UNKNOWN), never proven absence"
              % ", ".join(str(r["pid"]) for r in probe_failed))
    if census_partial:
        print("  ! CENSUS PARTIAL: a same-uid pid failed a mandatory probe "
              "before its comm could prove or refute claude — the total "
              "above is a floor, not a certified estate")
    doubles = sorted({r["sid"] for r in table if r["double_open"]})
    if doubles:
        print("  ‼ %d sid(s) are live in MULTIPLE pids (DOUBLE-OPEN) — "
              "resolve before resuming or injecting" % len(doubles))
    return rc
