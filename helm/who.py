#!/usr/bin/env python3
"""helm who — the pid→cred attribution table.

Which running agent processes sit on which cred: /proc/<pid>/comm finds the
claude/codex CLIs, /proc/<pid>/environ names the cred home (CLAUDE_CONFIG_DIR
/ CODEX_HOME, defaulting to ~/.claude / ~/.codex) — same-uid processes only,
which is exactly the local fleet. The missing link for a third-party rotation
executor: a rebalance names an account; `who` names the pids ON it, and the
executor maps pid→pane to stop + resume on the new home.

Attribution is EVIDENCE, never a guess: an environ that READ OK but lacks the
key is positive evidence of the provider default home (attribution=default);
an UNREADABLE environ (pid died, permissions, race) is NO evidence — the row
stays visible with home/account None and attribution=environ-unreadable, so a
rotation executor never acts on a default-attributed ghost. Each pid's stat
starttime is captured before and rechecked after its per-pid file reads; a
changed or unreadable starttime (pid reuse mid-scan) discards the row.

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
import errno
import glob
import json
import os
import sys

from . import runtime_config

PROC = "/proc"  # module-level so tests point it at a fixture tree


def read_environ(pid):
    """/proc/<pid>/environ as a dict, or None when UNREADABLE. The two states
    must never conflate: a read-OK environ MISSING a key is evidence the
    process runs on the provider default; an unreadable one is no evidence."""
    try:
        with open("%s/%d/environ" % (PROC, pid), "rb") as f:
            raw = f.read()
    except OSError:
        return None
    env = {}
    for kv in raw.split(b"\0"):
        k, sep, v = kv.partition(b"=")
        if sep:
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


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


def scan(accounts=None, status=None):
    """One row per live claude/codex process, cred-attributed.

    ``status`` is an optional dict populated with ``listing_failed`` and
    ``failed_pids``. That completeness channel is consumed by the session
    census: a per-pid stat/comm/ancestry failure must never look like a
    successful negative who attribution. The public row-only return stays
    backward compatible for ``helm who`` and existing callers.
    """
    if accounts is None:
        accounts = _accounts()
    if status is not None:
        status.clear()
        status.update({"listing_failed": False, "failed_pids": set()})

    def failed(pid):
        if status is not None:
            status["failed_pids"].add(pid)

    def gone(e):
        return e.errno in (errno.ENOENT, errno.ESRCH)

    by_home = {}
    for a in accounts:
        if a.get("home"):
            by_home.setdefault(os.path.realpath(a["home"]),
                               a.get("name") or a.get("account"))
    try:
        names = os.listdir(PROC)
    except OSError:
        names = []
        if status is not None:
            status["listing_failed"] = True
    procs = []
    for name in names:
        if not name.isdigit():
            continue
        pid, entry = int(name), os.path.join(PROC, name)
        try:
            if os.stat(entry).st_uid != os.geteuid():
                continue
            with open(os.path.join(entry, "comm")) as f:
                comm = f.read().strip()
        except OSError as e:
            if not gone(e):
                failed(pid)
            continue
        # THE SAME COMM BLINDNESS, AND HERE IT ALSO PICKS THE CRED HOME. comm
        # is the kernel's copy of the executable BASENAME, and the launcher
        # turned that into a VERSION (`2.1.238`) when it moved to
        # `claude/versions/<release>` — so this branch stopped matching every
        # agent on the estate and `helm who` answered "no live agent
        # processes" on a box running twenty-one.
        #
        # STRAIGHT TO procid, THE OWNER, AND AT **THIS** MODULE'S PROC ROOT.
        # A first draft routed through `session._is_agent`, which reads
        # session.PROC — so a hermetic `who` fixture could not exercise the
        # versioned path at all, and no arm could have covered it. procid
        # imports only os and re, so there is no cycle to route around.
        #
        # AND ITS THIRD STATE IS NOT A NO. `is_claude` returns None for a pid
        # whose comm looks versioned and whose exe we were not PERMITTED to
        # read; the draft put that call in an `or` and a None fell out of the
        # truthy branch as "not an agent", so the pid vanished and `helm who`
        # printed a confident "no live agent processes" over it. That is the
        # exact collapse this lane exists to end, committed by its own cure one
        # file over. An undecidable pid is a FAILED PROBE: it marks the scan
        # incomplete so the surface refuses to certify emptiness.
        from . import procid
        agent = procid.is_claude(pid, comm.encode(), PROC)
        if agent is None:
            failed(pid)
            continue
        if agent:
            provider, env_key = "anthropic", "CLAUDE_CONFIG_DIR"
        elif comm == "codex":
            provider, env_key = "codex", "CODEX_HOME"
        else:
            continue
        start = starttime_of(pid)
        if start == float("inf"):
            failed(pid)
            continue  # identity cannot be pinned across reads
        ppid = ppid_of(pid)
        env = read_environ(pid)
        env_failed = env is None
        harness = "claude" if provider == "anthropic" else provider
        # Public `helm who` preserves the process's explicit spelling. Internal
        # joins canonicalize at their comparison sites; changing the public row
        # would silently rewrite a longstanding machine/CLI contract.
        home, source = runtime_config.resolve(harness, env, canonical=False)
        home_failed = not env_failed and home is None
        attribution = ("environ-unreadable" if env_failed else
                       "env" if source == env_key else
                       "default" if source == runtime_config.DEFAULT_SOURCE else
                       "home-unproven")
        try:
            cwd = os.readlink(os.path.join(entry, "cwd"))
            cwd_failed = False
        except OSError:
            cwd, cwd_failed = None, True
        if provider == "codex":
            session, cands = codex_session_from_fds(pid), []
        elif home and cwd:
            session, cands = claude_sessions(home, cwd)
        else:
            session, cands = None, []
        final = starttime_of(pid)
        if final == float("inf"):
            try:
                os.stat(entry)
            except OSError as e:
                if not gone(e):
                    failed(pid)
            else:
                failed(pid)
            continue
        if final != start:
            continue  # proven pid reuse, not a failed probe
        if env_failed or home_failed or cwd_failed:
            failed(pid)
        procs.append({"pid": pid, "ppid": ppid, "child": False,
                      "provider": provider, "home": home,
                      "attribution": attribution,
                      "account": by_home.get(os.path.realpath(home))
                      if home else None,
                      "cwd": cwd, "session": session, "_start": start,
                      "session_candidates": cands, "session_shared": False})
    pid_set = {p["pid"] for p in procs}
    for p in procs:
        # walk the FULL ancestor chain: an agent spawned through an
        # intermediate shell/node is still a child (direct-ppid misses it).
        # An unreadable live ancestor makes this row's top-level status
        # unprovable, so it enters failed_pids instead of defaulting to parent 0.
        a, hops = p["ppid"], 0
        while a > 1 and hops <= 25:
            if a in pid_set:
                p["child"] = True
                break
            parent = ppid_of(a)
            if parent == 0:
                try:
                    os.stat(os.path.join(PROC, str(a)))
                except OSError:
                    pass
                else:
                    failed(p["pid"])
                break
            a, hops = parent, hops + 1
        if hops > 25:
            failed(p["pid"])
    # SESSION DEDUPE: two independent processes on one session would
    # double-resume/split-brain an executor. The OLDEST bracketed process owns
    # the session; never re-read starttime and race the attribution afterward.
    by_session = {}
    for i, p in enumerate(procs):
        if not p["child"] and p["session"]:
            by_session.setdefault((p["provider"], p["session"]), []).append(i)
    for (_, sid), idxs in by_session.items():
        if len(idxs) < 2:
            continue
        owner = min(idxs, key=lambda i: procs[i]["_start"])
        for i in idxs:
            procs[i]["session_shared"] = True
            if i != owner:
                procs[i]["session"] = None
                if sid not in procs[i]["session_candidates"]:
                    procs[i]["session_candidates"].insert(0, sid)
    for p in procs:
        p.pop("_start", None)
    procs.sort(key=lambda p: (p["provider"], p["account"] or "", p["pid"]))
    return procs


def cmd_who(args):
    """who [--json] — pid→cred attribution table for the live fleet."""
    # flags-only membership reader — guard the tail before scan():
    # `who --bogus` silently printed the table and exited 0.
    from .cli import guard_tail
    rc = guard_tail("helm who", args, flags=("--json",), usage="who [--json]")
    if rc is not None:
        return rc
    # THE SCAN'S INCOMPLETENESS HAS TO REACH THE SENTENCE, or the tri-state
    # dies at the last inch. `scan()` was called with NO status dict, so every
    # pid it could not classify — a versioned comm whose exe we were not
    # PERMITTED to read — was collected into a bucket nobody asked for, and the
    # renderer then printed a CONFIDENT EMPTY FLEET over it. That is the same
    # sentence the outage was reported through, one layer above the predicate
    # that caused it.
    status = {}
    rows = scan(status=status)
    # BOTH CHANNELS, ENUMERATED FROM scan() RATHER THAN SAMPLED. `status` has
    # exactly two: `failed_pids` (this pid could not be classified) and
    # `listing_failed` (the process table itself could not be READ). A first
    # draft read only the first, so the WORST case — os.listdir(PROC) raising,
    # which means we saw NOTHING AT ALL — arrived with failed_pids empty and
    # reported `complete: true, unclassified: []`. An unreadable table
    # certifying a complete scan is the confident-empty claim in its purest
    # form, and I found it by reading every write to `status` instead of the
    # one I already knew about.
    unclassified = sorted(status.get("failed_pids") or ())
    listing_failed = bool(status.get("listing_failed"))
    incomplete = bool(unclassified or listing_failed)
    if incomplete:
        # STDERR, and on BOTH paths: the JSON body stays a plain array so no
        # machine consumer's shape changes, but a run that could not see the
        # whole process table must never LOOK complete on either surface.
        # NAMED GAP: a --json consumer that reads only stdout still cannot tell.
        # Carrying it in the payload means turning the array into an object,
        # which is a contract change and does not belong inside a P0.
        why = ("THE PROCESS TABLE COULD NOT BE READ"
               if listing_failed else
               "%d pid%s could not be classified (%s)"
               % (len(unclassified), "s"[:len(unclassified) != 1],
                  ", ".join(str(q) for q in unclassified[:6])))
        print("helm who: SCAN INCOMPLETE — %s: this listing is a FLOOR, not "
              "the fleet." % why, file=sys.stderr)
    if "--json" in args:
        # THE MACHINE CONTRACT CARRIES THE STATE, because stderr is not part of
        # it. A first draft left this a bare array and warned on stderr, with
        # the gap named honestly in a comment — and the naming did not help: a
        # consumer running `helm who --json 2>/dev/null`, which is the ordinary
        # shape, gets BYTE-IDENTICAL OUTPUT for a complete empty scan and an
        # incomplete one. Identical bytes, opposite meanings, and the second is
        # the confident-empty claim this whole lane exists to end.
        #
        # So the body is an OBJECT now. That is a deliberate contract change,
        # taken on review rather than smuggled: `rows` is the same list it
        # always was, and `scan.complete` is the fact a consumer must be able to
        # read before treating an empty `rows` as a proven-empty fleet.
        print(json.dumps({"rows": rows,
                          "scan": {"complete": not incomplete,
                                   "listing_failed": listing_failed,
                                   "unclassified": unclassified}}, indent=2))
        return 0
    if not rows:
        if incomplete:
            print("helm who: NO ATTRIBUTABLE AGENT PROCESSES, and %s — this is "
                  "NOT a proven-empty fleet."
                  % ("THE PROCESS TABLE COULD NOT BE READ AT ALL"
                     if listing_failed
                     else "the scan could not classify every pid"))
            return 0
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
                         " [SHARED]" if r["session_shared"] else "",
                         " [ENVIRON-UNREADABLE]"
                         if r["attribution"] == "environ-unreadable" else ""))
        print("  %-8d %-9s %-28s %-26s %-40s %s%s" % (
            r["pid"], r["provider"], (r["account"] or "-")[:28],
            tilde(r["home"])[:26], sess[:40], tilde(r["cwd"]), flags))
    return 0
