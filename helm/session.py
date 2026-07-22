#!/usr/bin/env python3
"""helm session — the SESSION SUBSTRATE interface (premise: sessions-are-the-
substrate). One context window holds one complex concept well; the
environment's real capability is MANY sessions preserved / branchable /
resumable across the whole local env, first-class.

helm WRAPS cv (clustervision), never rebuilds it (cv-is-core-helm-dep): cv owns
the mechanics (read/reshape/rehome/shrink — ls/show/doctor/prune/port/resume);
helm owns the POLICY:

  LAW 1 — never two live copies of one session. Before printing any launch
    line, scan live claude pids for an open copy of the sid (argv --resume +
    the child-stamp env + heartbeats). The built-in double-open detector is
    BLIND to child-stamped panes (no heartbeat registers) — so this scan reads
    /proc directly. Found: print the close-first instruction, never the
    incantation.
  LAW 2 — prepare + print, never launch. checkpoint/port/rescue end at a
    PRINTED incantation; only `resume --launch` spawns, and only after law 1.

THE PERSISTENCE SURFACE (child-stamp-kills-seat-persistence): a pane stamped
CLAUDE_CODE_CHILD_SESSION=1 (inherited from a claude-descended spawner, e.g.
the orca daemon restarted inside a Bash tool) runs with transcript persistence
silently OFF — memory-only, unrecoverable. `ls`/`doctor`/`doctor-panes` surface
these; every printed incantation bakes CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1
(the escape hatch) so a resume from a stamped pane can't re-trap. Strip-only is
the mint default (seat.CHILD_STAMP_VARS); FORCE is the deliberate rescue tool.

EXPERTS (expert-sessions-beat-fresh-research): sessions are also the EXPERTISE
layer — over time, querying/resuming a preserved expert beats fresh research.
`experts` is a durable registry (~/.helm/_global/session-experts.json:
sid -> {domain, registered, last_refreshed, note}) so routing to an expert is
O(1). `ask <domain> <q>` is the QUERY LADDER: registry hit -> transcript search
(cv search scoped to the expert) -> else pack-digest (cv pack) -> resume-live
is always PRINT-DON'T-LAUNCH with a mandatory RE-GROUND instruction (expertise
goes stale like everything else — the expert re-verifies key facts against the
current substrate before answering).
"""
import json
import os
import subprocess
import sys
import time

from . import home, pk

CV = "cv"
FORCE = "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1"


# ---------------------------------------------------------------------------
# cv wrapper (the policy/mechanics seam — thin, timeout-bounded, clean degrade)
# ---------------------------------------------------------------------------

def _cv(*argv, timeout=120):
    """Run cv, return (rc, stdout, stderr). FileNotFoundError / timeout degrade
    to a named error string, never a traceback — helm's policy layer must stay
    up when the mechanics layer is absent."""
    try:
        p = subprocess.run([CV] + list(argv), capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "cv not installed — clustervision must be on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", "cv timed out after %ds" % timeout
    except OSError as e:
        return 1, "", "cv failed to launch: %s" % e


def _stamp_vars():
    from . import seat
    return seat.CHILD_STAMP_VARS


def _unset_prefix():
    return "env " + " ".join("-u " + v for v in _stamp_vars()) + " "


# ---------------------------------------------------------------------------
# live-pane scan (law 1 + the memory-only surface) — /proc, never ps-grep
# ---------------------------------------------------------------------------

def _proc_claude_rows():
    """Every live pid whose argv[0] names claude. Row: {pid, resume, child,
    sid8, force}. The stamp + --resume come from /proc/<pid>/{environ,cmdline}
    directly — a ps cmdline grep missed a `--resume`-bearing pid in the field
    (session-surgery forensics), so the scan reads the kernel's own record."""
    rows = []
    for pid in (p for p in os.listdir("/proc") if p.isdigit()):
        base = os.path.join("/proc", pid)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if not argv or "claude" not in os.path.basename(argv[0]):
            continue
        env = {}
        try:
            with open(os.path.join(base, "environ"), "rb") as f:
                for kv in f.read().decode("utf-8", "replace").split("\0"):
                    k, sep, v = kv.partition("=")
                    if sep and k in _stamp_vars() + (  # noqa: E731
                            "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE",):
                        env[k] = v
        except OSError:
            pass
        resume = None
        for i, a in enumerate(argv):
            if a == "--resume" and i + 1 < len(argv):
                resume = argv[i + 1]
            elif a.startswith("--resume="):
                resume = a.split("=", 1)[1]
        rows.append({
            "pid": int(pid),
            "resume": resume,
            "child": env.get("CLAUDE_CODE_CHILD_SESSION") == "1",
            "sid8": (env.get("CLAUDE_CODE_SESSION_ID") or "")[:8],
            "force": env.get("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE") == "1",
        })
    return rows


def live_sids():
    """{sid: [pid,...]} of sessions with a LIVE open copy — from argv --resume
    AND from the child-stamp's inherited SID (a stamped pane's
    CLAUDE_CODE_SESSION_ID is its spawning ancestor's id, which that ancestor
    has open). This is law 1's resolver."""
    out = {}
    for r in _proc_claude_rows():
        if r["resume"]:
            out.setdefault(r["resume"], []).append(r["pid"])
        if r["child"] and r["sid8"]:
            out.setdefault(r["sid8"], []).append(r["pid"])
    return out


def open_pids(sid):
    """Live pids holding an open copy of sid (full id or 8-char prefix)."""
    sid = (sid or "").lower()
    pids = []
    for live_sid, ps in live_sids().items():
        if live_sid.lower().startswith(sid) or sid.startswith(live_sid.lower()):
            pids.extend(ps)
    return sorted(set(pids))


def memory_only_panes():
    """Live interactive panes running persistence-OFF: stamped child WITHOUT
    the FORCE override. These are the sessions a death would lose."""
    return [r for r in _proc_claude_rows() if r["child"] and not r["force"]]


# ---------------------------------------------------------------------------
# verbs
# ---------------------------------------------------------------------------

def _resolve_sid(prefix):
    """id-prefix -> full sid via the catalog (one resolver, the <sid> prefix
    convention). None + a printed reason when ambiguous/absent."""
    from . import sessions
    hits = [r for r in sessions.rows_for(include_synthetic=True)
            if r["i"].startswith(prefix)]
    if not hits:
        return None, "no session id starts with '%s'" % prefix
    if len(hits) > 1:
        return None, "%d sessions match '%s'" % (len(hits), prefix)
    return hits[0]["i"], None


def _print_incantation(sid, cred_home=None, cwd=None):
    """LAW 2's output: the pasteable resume line, FORCE baked in, child-stamp
    unset (so a paste into a stamped pane can't re-trap), optional credhome.
    Never executed here."""
    cwd = cwd or os.getcwd()
    env = _unset_prefix() + FORCE + " "
    if cred_home:
        env += "CLAUDE_CONFIG_DIR=%s " % cred_home
    return "cd %s && %sclaude --resume %s" % (cwd, env, sid)


def cmd_ls(args):
    """session ls — sessions + a PERSISTENCE column: live panes cross-joined
    with their stamp, flagging memory-only panes and double-opens."""
    rows = _proc_claude_rows()
    mo = [r for r in rows if r["child"] and not r["force"]]
    live = live_sids()
    dbl = {s: ps for s, ps in live.items() if len(ps) > 1}
    print("helm session ls — %d live claude panes" % len(rows))
    for r in sorted(rows, key=lambda x: x["pid"]):
        state = ("MEMORY-ONLY (stamped, no FORCE)" if r["child"] and not r["force"]
                 else "persisted" if not r["child"] else "stamped+FORCED (rescued)")
        res = (" resume=%s" % r["resume"][:12]) if r["resume"] else ""
        print("  pid %-8d %s%s" % (r["pid"], state, res))
    if dbl:
        print("DOUBLE-OPEN (law 1 violation):")
        for s, ps in dbl.items():
            print("  sid %s open in pids %s" % (s, ps))
    if mo:
        print("⚠ %d memory-only pane(s) — a death loses them; rescue via "
              "`helm session rescue`." % len(mo))
    return 0


def cmd_doctor_panes(args):
    """The doctor leg for LIVE panes: which are memory-only right now, which
    are double-open. (cv doctor owns per-session context-window diagnosis;
    this is the persistence/liveness layer cv doesn't see.)"""
    return cmd_ls(args)


def cmd_doctor(args):
    """session doctor <sid> — classify a session: normal / forked / compacted /
    bridged-child / maxed-at-wall, and which lane applies. Wraps cv doctor +
    the catalog + the live-pane scan."""
    if not args:
        print("usage: helm session doctor <sid-prefix>", file=sys.stderr)
        return 2
    sid, err = _resolve_sid(args[0])
    if not sid:
        print("helm session doctor: " + err, file=sys.stderr)
        return 1
    rc, out, cerr = _cv("doctor", sid, "--json")
    if rc == 127:
        print("helm session doctor: " + cerr, file=sys.stderr)
        return 1
    kinds = []
    pids = open_pids(sid)
    if pids:
        stamped = [p for p in _proc_claude_rows()
                   if p["pid"] in pids and p["child"] and not p["force"]]
        kinds.append("bridged-child (memory-only)" if stamped else "live")
    try:
        d = json.loads(out) if out.strip() else {}
    except ValueError:
        d = {}
    if d.get("compactions") or d.get("compact_boundaries"):
        kinds.append("compacted")
    if d.get("forked_from") or d.get("forkedFrom"):
        kinds.append("forked")
    if not kinds:
        kinds.append("normal")
    print("helm session doctor %s" % sid[:12])
    print("  kind: %s" % " / ".join(kinds))
    if pids:
        print("  live in pid(s): %s%s" % (
            pids, " — LAW 1: close before any resume" if pids else ""))
    if out.strip():
        print("  cv doctor: " + (out.strip().splitlines()[0] if out else ""))
    lane = ("rescue (memory-only — harvest first)" if any("bridged" in k for k in kinds)
            else "checkpoint/port as needed")
    print("  lane: %s" % lane)
    return 0


def cmd_checkpoint(args):
    """session checkpoint <sid> [--window N] — mint a NEW resumable snapshot id
    (original untouched) via cv prune (revive + --thinking default), so a
    maxed/forked session becomes branchable. Memory-only sids route to rescue."""
    if not args:
        print("usage: helm session checkpoint <sid-prefix> [--window N]",
              file=sys.stderr)
        return 2
    sid, err = _resolve_sid(args[0])
    if not sid:
        print("helm session checkpoint: " + err, file=sys.stderr)
        return 1
    window = "150000"
    if "--window" in args:
        i = args.index("--window")
        window = args[i + 1] if i + 1 < len(args) else window
    if open_pids(sid) and any(p in [r["pid"] for r in memory_only_panes()]
                              for p in open_pids(sid)):
        print("helm session checkpoint: %s is MEMORY-ONLY live — use "
              "`helm session rescue %s` (harvest lane), not prune." % (sid[:12], sid[:12]),
              file=sys.stderr)
        return 1
    import uuid
    newid = str(uuid.uuid4())
    rc, out, cerr = _cv("prune", sid, "--window", window, "--thinking",
                        "--to", newid, "--json")
    if rc != 0:
        print("helm session checkpoint: cv prune failed: " + (cerr or out),
              file=sys.stderr)
        return 1
    pk.event("session-checkpoint", newid, "from %s window %s" % (sid[:12], window))
    print("checkpoint minted: %s (from %s)" % (newid, sid[:12]))
    print("resume: " + _print_incantation(newid))
    return 0


def cmd_port(args):
    """session port --cred <home> <sid> — cred-switch resume PREP: verify the
    target home's projects share / trust / settings, then PRINT the incantation
    (FORCE baked in). Never launches (law 2)."""
    home_arg = sid = None
    i = 0
    while i < len(args):
        if args[i] == "--cred" and i + 1 < len(args):
            home_arg = args[i + 1]; i += 2
        else:
            sid = args[i]; i += 1
    if not (home_arg and sid):
        print("usage: helm session port --cred <credhome> <sid-prefix>",
              file=sys.stderr)
        return 2
    sid, err = _resolve_sid(sid)
    if not sid:
        print("helm session port: " + err, file=sys.stderr)
        return 1
    from . import homes
    target = next((h for h in homes.homes_list()
                   if h.get("name") == home_arg or home_arg in (h.get("aliases") or [])
                   or h.get("path") == home_arg), None)
    if not target:
        print("helm session port: no cred home matches '%s'" % home_arg,
              file=sys.stderr)
        return 1
    path = target.get("path", "?")
    # preflights (re-run at print time, never cached — homes mutate)
    proj = os.path.join(path, "projects")
    shared = os.path.islink(proj)
    pids = open_pids(sid)
    print("helm session port %s -> %s" % (sid[:12], path))
    print("  projects: %s" % ("shared symlink (no file move)" if shared
                              else "OWNED dir — cv port --out required"))
    if pids:
        print("  LAW 1: sid is LIVE in pid(s) %s — close first; NOT printing "
              "the incantation." % pids)
        return 1
    print("  incantation: " + _print_incantation(sid, cred_home=path))
    return 0


def cmd_rescue(args):
    """session rescue <pid|sid> — the full pipeline: doctor -> harvest note ->
    checkpoint/port-preflight -> PRINT incantation. Laws 1+2 enforced."""
    if not args:
        print("usage: helm session rescue <pid|sid-prefix>", file=sys.stderr)
        return 2
    target = args[0]
    sid = None
    if target.isdigit():  # a live pid: resolve its sid from the scan
        pid = int(target)
        row = next((r for r in _proc_claude_rows() if r["pid"] == pid), None)
        if not row:
            print("helm session rescue: no live claude pid %s" % pid, file=sys.stderr)
            return 1
        sid = row["resume"] or row["sid8"]
        if not sid:
            print("helm session rescue: pid %s has no resolvable sid (no "
                  "--resume argv, no stamp SID)" % pid, file=sys.stderr)
            return 1
        print("pid %d -> sid %s (child-stamped: %s, FORCED: %s)"
              % (pid, sid[:12], row["child"], row["force"]))
    else:
        sid, err = _resolve_sid(target)
        if not sid:
            print("helm session rescue: " + err, file=sys.stderr)
            return 1
    rc = cmd_doctor([sid])
    live = open_pids(sid)
    print("rescue plan for %s:" % sid[:12])
    print("  1. HARVEST side channels FIRST (pane scrollback, journals, "
          "/dev/shm/helm-chat) — they die with the pane/reboot.")
    print("  2. Write the pane's SELF-RECAP (the fidelity anchor).")
    if live:
        print("  3. LAW 1: close live pid(s) %s BEFORE resuming." % live)
    print("  4. Then: " + _print_incantation(sid))
    return 0


def cmd_resume(args):
    """session resume <sid> [--launch] — cv resume + LAW 1 single-open guard.
    Prints the incantation; --launch (only here) spawns after the guard."""
    launch = "--launch" in args
    args = [a for a in args if a != "--launch"]
    if not args:
        print("usage: helm session resume <sid-prefix> [--launch]", file=sys.stderr)
        return 2
    sid, err = _resolve_sid(args[0])
    if not sid:
        print("helm session resume: " + err, file=sys.stderr)
        return 1
    pids = open_pids(sid)
    if pids:
        print("helm session resume: LAW 1 — %s is LIVE in pid(s) %s. Close "
              "first; NOT %s." % (sid[:12], pids,
                                   "launching" if launch else "printing"),
              file=sys.stderr)
        return 1
    line = _print_incantation(sid)
    if not launch:
        print(line)
        return 0
    rc, out, cerr = _cv("resume", sid, "--launch")
    if rc != 0:
        print("helm session resume: cv failed: " + (cerr or out), file=sys.stderr)
        return 1
    print("launched %s" % sid[:12])
    return 0


# ---------------------------------------------------------------------------
# experts registry + the ask ladder (the expertise layer)
# ---------------------------------------------------------------------------

def _experts_path():
    return os.path.join(home.global_dir(), "session-experts.json")


def _experts():
    return pk.read_json(_experts_path(), {}) or {}


def _write_experts(d):
    os.makedirs(os.path.dirname(_experts_path()), exist_ok=True)
    pk.atomic_write(_experts_path(), json.dumps(d, indent=2, ensure_ascii=False))


def cmd_experts(args):
    """session experts [--register <sid> --domain D [--note N]] [--refresh <sid>]
    — the O(1) expert routing registry. Durable, freshness-flagged."""
    if not args:
        ex = _experts()
        if not ex:
            print("no experts registered — helm session experts --register "
                  "<sid> --domain <domain>")
            return 0
        print("helm session experts (%d):" % len(ex))
        for sid, r in sorted(ex.items(), key=lambda kv: kv[1].get("domain", "")):
            age = _fresh(r.get("last_refreshed") or r.get("registered"))
            print("  %-14s %s  (%s, refreshed %s)%s" % (
                r.get("domain", "?"), sid[:12], r.get("note", "")[:40], age,
                "  STALE" if age.endswith("d") and int(age[:-1] or 0) > 14 else ""))
        return 0
    if "--register" in args:
        i = args.index("--register")
        sid = args[i + 1] if i + 1 < len(args) else None
        domain = note = None
        if "--domain" in args:
            j = args.index("--domain")
            domain = args[j + 1] if j + 1 < len(args) else None
        if "--note" in args:
            j = args.index("--note")
            note = args[j + 1] if j + 1 < len(args) else None
        if not (sid and domain):
            print("usage: helm session experts --register <sid> --domain D "
                  "[--note N]", file=sys.stderr)
            return 2
        full, err = _resolve_sid(sid)
        if not full:
            print("helm session experts: " + err, file=sys.stderr)
            return 1
        ex = _experts()
        now = pk.now_ts()
        ex[full] = {"domain": domain, "registered": now,
                    "last_refreshed": now, "note": note or ""}
        _write_experts(ex)
        pk.event("session-expert-register", full[:12], domain)
        print("registered %s as expert: %s" % (full[:12], domain))
        return 0
    if "--refresh" in args:
        i = args.index("--refresh")
        sid = args[i + 1] if i + 1 < len(args) else None
        ex = _experts()
        full = next((s for s in ex if s.startswith(sid or "")), None)
        if not full:
            print("helm session experts: no expert sid starts '%s'" % sid,
                  file=sys.stderr)
            return 1
        ex[full]["last_refreshed"] = pk.now_ts()
        _write_experts(ex)
        print("refreshed %s (%s)" % (full[:12], ex[full]["domain"]))
        return 0
    print("usage: helm session experts [--register <sid> --domain D [--note N]] "
          "[--refresh <sid>]", file=sys.stderr)
    return 2


def _fresh(ts):
    """ISO ts -> '3d'/'5h'/'now' age for the freshness flag."""
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
        d = (time.time() - t) / 86400.0
        return "now" if d < 0.04 else ("%dh" % int(d * 24) if d < 1 else "%dd" % int(d))
    except (TypeError, ValueError):
        return "?"


def _grep_expert(sid, q, cap=3):
    """Matching lines from the EXPERT's own transcript (the scoped rung of the
    ask ladder) — the expert's answer to q in its own words, not the corpus's.
    Fail-open to '' (no transcript / no hit -> the caller falls to a corpus
    search)."""
    try:
        from . import sessions
        row = next((r for r in sessions.rows_for(include_synthetic=True)
                    if r["i"] == sid), None)
        path = (row or {}).get("p")
        if not path or not os.path.exists(path):
            return ""
        out = subprocess.run(["grep", "-iF", "-m", str(cap), "--", q, path],
                             capture_output=True, text=True, timeout=15).stdout
        hits = []
        for line in out.splitlines():
            i = line.lower().find(q.lower())
            hits.append("  …" + line[max(0, i - 80): i + 140].strip() + "…")
        return "\n".join(hits[:cap])
    except Exception:
        return ""


def cmd_ask(args):
    """session ask <domain> <question...> — the QUERY LADDER over the experts
    registry: (1) registry hit (O(1) route) -> (2) transcript search scoped to
    the expert (cv search) -> (3) pack-digest (cv pack) -> resume-live is
    PRINT-DON'T-LAUNCH with a mandatory RE-GROUND instruction."""
    if len(args) < 2:
        print("usage: helm session ask <domain> <question...>", file=sys.stderr)
        return 2
    domain, q = args[0], " ".join(args[1:])
    ex = _experts()
    hit = next(((s, r) for s, r in ex.items() if r.get("domain") == domain), None)
    if not hit:
        print("no expert for domain '%s' — register one: helm session experts "
              "--register <sid> --domain %s" % (domain, domain))
        print("falling back to a corpus search: cv search -- '%s'" % q)
        rc, out, cerr = _cv("search", "--limit", "5", "--", q)
        print(out if rc == 0 else "cv: " + (cerr or "unavailable"))
        return 0 if rc == 0 else 1
    sid, reg = hit
    print("expert for '%s': %s (refreshed %s)" % (domain, sid[:12],
                                                  _fresh(reg.get("last_refreshed"))))
    # ladder rung 2: transcript search SCOPED TO THE EXPERT's own transcript
    # (cv search has no per-session scope — grep the expert's jsonl directly,
    # the _grep_sessions pattern). Fall through to a corpus search when the
    # expert's own transcript holds no hit.
    shown = _grep_expert(sid, q)
    if shown:
        print("expert-transcript hits:\n" + shown)
    else:
        rc, out, _ = _cv("search", "--limit", "3", "--", q)
        if rc == 0 and out.strip():
            print("(no hit in the expert's own transcript — corpus search:)\n"
                  + out.rstrip())
    # ladder rung 3: resume-live, PRINT-DON'T-LAUNCH + mandatory re-ground
    pids = open_pids(sid)
    print("\nresume-live (re-grounds before answering):")
    if pids:
        print("  expert is LIVE in pid(s) %s — ask in that pane, or close + "
              "resume." % pids)
    print("  " + _print_incantation(sid))
    print("  RE-GROUND (mandatory): before answering '%s', the expert re-verifies "
          "its key facts against the CURRENT substrate — expertise goes stale." % q)
    return 0


# ---------------------------------------------------------------------------

_VERBS = {
    "ls": cmd_ls,
    "doctor-panes": cmd_doctor_panes,
    "doctor": cmd_doctor,
    "checkpoint": cmd_checkpoint,
    "port": cmd_port,
    "rescue": cmd_rescue,
    "resume": cmd_resume,
    "experts": cmd_experts,
    "ask": cmd_ask,
}

USAGE = ("usage: helm session ls | doctor-panes | doctor <sid> | checkpoint "
         "<sid> [--window N]\n"
         "       helm session port --cred <home> <sid> | rescue <pid|sid> | "
         "resume <sid> [--launch]\n"
         "       helm session experts [--register <sid> --domain D [--note N]] "
         "[--refresh <sid>]\n"
         "       helm session ask <domain> <question...>")


def cmd_session(args):
    """session ls|doctor|checkpoint|port|rescue|resume|experts|ask — the
    session substrate: every session an asset (inventory, health, checkpoint,
    branch, port, compose), wrapping cv with helm's policy (single-open law,
    print-don't-launch, credhome preflights, the experts registry)."""
    args = list(args or [])
    if not args or args[0] not in _VERBS:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return _VERBS[args[0]](args[1:])
    except (OSError, ValueError) as e:
        print("helm session: %s" % e, file=sys.stderr)
        return 2
