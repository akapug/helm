#!/usr/bin/env python3
"""helm record — the session-keyed tool-outcome recorder. A hook pipes each
tool event's FULL JSON here (claude first; codex when its hook surface lands);
helm keeps tiny per-session counters that upgrade steering from static to
situational — the state the stuck-hook, dynamic reflexes, mentor observe
triggers, and evolve's behavior observers read.

TWO legs on claude (contract probed live 2026-07-19): a SUCCESSFUL tool fires
PostToolUse (Bash tool_response = stdout/stderr/interrupted — NO exit code;
success IS exit 0 by construction), a FAILED tool — nonzero Bash included —
fires PostToolUseFailure (no tool_response; top-level error e.g. 'Exit code
1' + is_interrupt). One event leg alone is blind: without the failure leg no
failed run is ever recorded and every logged exit reads -1.

State, under <helm home>/_global/.state/reflex-state/<session_id>/:
  counters.json       passive-streak, dirty-streak + cached last-dirty,
                      stuck-signal/-streak, loop-streak + cmd-hash-chain
  command-log.jsonl   verify-grounding: test-runner invocations with REAL exit
                      codes (token + digest, never the raw command line)
  edit-targets.log    verify-grounding: basenames actually edited (a real edit
                      vs prose that merely mentioned a filename)
  edit-paths.log      the SAME edits, home-relative and DIRECTORY-BEARING —
                      what a per-toolcall whisper needs to answer "where did
                      this write go" (a lesson into a private memory dir vs
                      `helm store`; a doc vs the shared checkout). A basename
                      cannot answer it, and every write was reduced to one
                      before any watcher saw it.
  todos.json          the seat todo mirror (todos.py): the CURRENT todo list
                      off TodoWrite/Task*, pull-read by `helm todos` and the
                      roster — digest+pull, never a firehose

Laws:
  * SESSION-keyed from the payload's session_id, never pane/env — sessions are
    helm's key; there is no pane assumption. No session_id -> record nothing.
  * Pure-Python verb: the harness runs `helm record --hook-json`, argv only —
    structurally kills the ancestor's apostrophe-breaks-python-c silent-noop
    class (no shell string ever interpolates tool content).
  * FAIL-OPEN, NO OUTPUT: any trouble is swallowed, rc 0, stdout empty — a
    recorder must never block or noise a turn.
  * Perf: the passive hot path (Read/Grep/Glob) is one JSON read + one atomic
    write, NO subprocess — `git status` runs only on dirtying tools, in the
    tool's own workdir. Appends are O(1) (one stat + append, fire-ledger
    rotation pattern).
  * Harness-agnostic: parses event JSON only, unknown keys tolerated.
"""
import json
import os
import re
import sys
import time

from . import home, pk

HOOK_EVENT = "PostToolUse"
FAIL_EVENT = "PostToolUseFailure"    # failed tools (nonzero Bash included) land here
HOOK_EVENTS = (HOOK_EVENT, FAIL_EVENT)
PASSIVE = ("Read", "Grep", "Glob")   # error text here is DATA — never arms stuck
EDITS = ("Edit", "Write", "NotebookEdit")
FORWARD = EDITS + ("Agent", "Task")  # forward progress; + git commit below
DIRTYING = EDITS + ("Bash",)         # the only tools worth a git-status probe
# the todo-mirror tools (todos.TOOLS, spelled here so the hot path never
# imports todos.py for the 99% of events that are not todo writes)
TASK_TOOLS = ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskUpdateTODO")
HASH_WINDOW = 8                      # loop-thrash lookback (catches A-B-A-B too)
LOG_MAX = 1024 * 1024                # command-log / edit-targets rotate here (-> .1)

# Conservative stuck tells (the ancestor's field-proven set): infra + auth +
# environment failures only — never generic "error", which rides ordinary output.
STUCK_RE = re.compile(
    r"API Error|rate.?limit|temporarily (?:limiting|unavailable)"
    r"|internal server error|server error|invalid_grant|token_revoked"
    r"|not authenticated|unauthorized|login required|permission denied"
    r"|command not found|No such file or directory", re.I)

# Test-runner shapes whose exit codes ground "validation really ran".
RUNNER_RE = re.compile(
    r"\b(cargo\s+nextest|cargo\s+test|just\s+test|just\s+check|pytest|go\s+test"
    r"|vitest|jest|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test"
    r"|bats|tox|phpunit|rspec|bundle\s+exec\s+rspec|make\s+test"
    r"|python3?(?:\s+\S+){0,4}?\s+-m\s+unittest)\b", re.I)
RUNNER_SH_RE = re.compile(r"((?:\./|bash\s+|sh\s+)?\S*test\S*\.sh)\b", re.I)


def state_root():
    return os.path.join(home.global_dir(), ".state", "reflex-state")


def session_key(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(sid))[:80]


def session_dir(sid):
    return os.path.join(state_root(), session_key(sid))


def counters(sid):
    """One session's counters, {} when absent — the read API the reflex/mentor/
    evolve consumers call instead of hardcoding paths."""
    return pk.read_json(os.path.join(session_dir(sid), "counters.json"), {}) or {}


def parse_event(raw):
    """Hook event JSON -> dict, or None when unusable (malformed, non-object,
    or no session_id/tool to key on). None means record NOTHING, rc 0."""
    try:
        d = json.loads(raw or "")
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    sid = str(d.get("session_id") or "")
    tool = str(d.get("tool_name") or d.get("tool") or "")
    return d if sid and tool else None


def _git_dirty(wd):
    """`git status --porcelain` in the TOOL's workdir -> True/False, or None
    when git itself failed (timeout/error) — the caller keeps its cached
    last-dirty then; unknown must never read as clean. Dirtying tools only —
    the caller gates; passive ops must never reach a subprocess."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", wd, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return bool(out.stdout.strip())


def _exit_code(resp):
    """An explicit exit code out of a tool_response dict; -1 when the payload
    carries none (codex-shaped payloads carry one; claude's does NOT)."""
    if isinstance(resp, dict):
        for k in ("exitCode", "exit_code", "returncode", "code"):
            if isinstance(resp.get(k), int):
                return resp[k]
    return -1


def _event_exit(event, resp, failed):
    """The truthful exit for a runner event. Explicit int keys win; else the
    EVENT KIND decides (claude's probed contract, module docstring): a
    success event with a response dict is exit 0 by construction — nonzero
    fired the failure event — and a failure event parses 'Exit code N' from
    its error (1 when unparseable; -1 on an interrupt: killed, no verdict)."""
    code = _exit_code(resp)
    if code != -1:
        return code
    if failed:
        if event.get("is_interrupt"):
            return -1
        m = re.search(r"exit code (\d+)", str(event.get("error") or ""), re.I)
        return int(m.group(1)) if m else 1
    return 0 if isinstance(resp, dict) else -1


def _home_relative(path):
    """`~/x/y.md` for a path under $HOME, else the path unchanged.

    Home-relative rather than absolute so the log stays short and carries no
    account name — the DIRECTORY is the decision-relevant part, the homedir
    spelling never is. Falls back to the input on any resolution failure: a
    watcher reading a slightly-odd path is strictly better than a watcher
    reading nothing, and this must never raise inside a hook."""
    try:
        ap = os.path.abspath(os.path.expanduser(path))
        hm = os.path.abspath(os.path.expanduser("~"))
        if ap == hm or ap.startswith(hm + os.sep):
            return "~" + ap[len(hm):]
        return ap
    except Exception:
        return str(path)


def _append(path, line):
    """O(1): one stat (rotation, fire-ledger pattern) + one append."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass  # no file yet
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def record(event):
    """The one write path. Fail-open TOTAL: any exception is swallowed —
    a recorder crash must never surface into a turn."""
    try:
        _record(event)
    except Exception:
        pass


def _record(event):
    sid = str(event.get("session_id") or "")
    tool = str(event.get("tool_name") or event.get("tool") or "")
    if not (sid and tool):
        return  # nothing to key on — state keyed wrong is worse than absent
    tin = event.get("tool_input")
    tin = tin if isinstance(tin, dict) else {}
    cmd = str(tin.get("command") or "")
    resp = event.get("tool_response") if "tool_response" in event \
        else event.get("tool_result")
    failed = str(event.get("hook_event_name") or "") == FAIL_EVENT
    sd = session_dir(sid)
    c = pk.read_json(os.path.join(sd, "counters.json"), {}) or {}

    # a FAILED tool made no progress: no forward credit, no commit credit
    is_commit = bool(re.search(r"\bgit\b.*\bcommit\b", cmd)) and not failed
    forward = (tool in FORWARD and not failed) or is_commit
    c["passive-streak"] = 0 if forward else int(c.get("passive-streak") or 0) + 1

    # dirty-streak: git probed ONLY on dirtying tools, in the tool's workdir;
    # everything else reads the cached last-dirty. A probe that FAILED (None:
    # timeout, git error) keeps the cache — unknown never masquerades as clean.
    if tool in DIRTYING:
        from . import seats
        wd = str(tin.get("workdir") or tin.get("cwd") or event.get("cwd") or "") \
            or seats.safe_cwd() or ""  # deleted cwd: _git_dirty("") fails to
        # None -> cached last-dirty, instead of an eager-getcwd hook crash
        dirty = _git_dirty(wd)
        if dirty is None:
            dirty = bool(c.get("last-dirty"))
        else:
            c["last-dirty"] = int(dirty)
    else:
        dirty = bool(c.get("last-dirty"))
    c["dirty-streak"] = 0 if (is_commit or not dirty) \
        else int(c.get("dirty-streak") or 0) + 1

    # loop-thrash: a bounded chain of recent command hashes; a re-run of a
    # command still in the window grows the streak (A-A-A and A-B-A-B alike).
    if tool == "Bash" and cmd:
        import hashlib
        h = hashlib.sha1(" ".join(cmd.split()).encode()).hexdigest()[:12]
        chain = [x for x in (c.get("cmd-hash-chain") or []) if isinstance(x, str)]
        c["loop-streak"] = int(c.get("loop-streak") or 0) + 1 if h in chain else 0
        c["cmd-hash-chain"] = (chain + [h])[-HASH_WINDOW:]

    # stuck-signal: action tools only — reads carry error text as data. A
    # failure event carries its tells in the top-level error, not a response.
    rtext = resp if isinstance(resp, str) else \
        json.dumps(resp) if resp is not None else ""
    if failed:
        rtext = "\n".join(x for x in (rtext, str(event.get("error") or "")) if x)
    if tool not in PASSIVE:
        stuck = bool(rtext) and bool(STUCK_RE.search(rtext))
        c["stuck-signal"] = int(stuck)
        c["stuck-streak"] = int(c.get("stuck-streak") or 0) + 1 if stuck else 0

    # command-log: test-runner invocations with the REAL exit code — token +
    # digest, never the raw command line (ids/digests law).
    if tool == "Bash" and cmd:
        m = RUNNER_RE.search(cmd)
        tok = m.group(1) if m else None
        if not tok:  # script fallback must actually look like a test script
            m = RUNNER_SH_RE.search(cmd)
            if m and re.search(r"test", m.group(1), re.I):
                tok = m.group(1)
        if tok:
            import hashlib
            _append(os.path.join(sd, "command-log.jsonl"), json.dumps(
                {"token": " ".join(tok.split()).lower()[:80],
                 "exit": _event_exit(event, resp, failed), "ts": int(time.time()),
                 "digest": hashlib.sha1(cmd.encode()).hexdigest()[:12]},
                separators=(",", ":")) + "\n")

    # edit-targets: the file a real edit landed on (basename only) — a
    # FAILED edit landed nowhere and must not ground verify.
    #
    # AND edit-paths: the same edit's DIRECTORY-BEARING path, home-relative.
    # The basename alone cannot answer the question that matters at a tool
    # boundary — WHERE did this write go. In a live incident an integrator wrote
    # five durable lessons into its own private memory dir instead of `helm
    # store`, taught a teammate the same lesson twice because the fleet never
    # saw them, and NOTHING could notice, because every write had been reduced
    # to a filename before any watcher saw it. A per-toolcall whisper can only
    # be as specific as the data it reads.
    #
    # A SEPARATE FILE on purpose: edit-targets.log is read by the verify rung
    # (seats.py) and the handoff snapshot (handoff.py), both of which expect
    # bare basenames. Widening it in place would have made this a
    # format-breaking change to two surfaces for no reason; the sibling costs
    # one line and breaks nothing.
    if tool in EDITS and not failed:
        fp = tin.get("file_path") or tin.get("path") or tin.get("notebook_path")
        if fp:
            _append(os.path.join(sd, "edit-targets.log"),
                    os.path.basename(str(fp)) + "\n")
            _append(os.path.join(sd, "edit-paths.log"),
                    _home_relative(str(fp)) + "\n")

    c.update(v=1, ts=pk.now_ts())
    c["last-tool"] = tool
    pk.write_json(os.path.join(sd, "counters.json"), c)

    # The seat todo mirror — purely additive, and DEAD LAST on purpose.
    # Walled off by its own try/except (a broken mirror can never cost the
    # counters, command-log or edit-targets that back the stop-whisper), and
    # ordered AFTER the counters write because the mirror's push leg appends
    # to a chat room under that room's flock: an exception is not the only
    # way to lose a write, a BLOCK is, and the stop-whisper's ground truth
    # must already be on disk before this can wait on anyone.
    if tool in TASK_TOOLS and not failed:
        try:
            from . import todos
            todos.capture(event, sid, tool, tin, resp)
        except Exception:
            pass


# ── wiring (claude first): install/status/doctor around the PostToolUse hook ──

def _ours(cmd):
    return "record --hook-json" in cmd or "helm record" in cmd


def hook_command():
    """`timeout N <abs bin/helm> record --hook-json || true` — fail-open by
    construction, same rails as the inject hook (docs/HOOKS.md law)."""
    import shlex
    from . import hooks
    return "timeout %d %s record --hook-json || true" % (
        hooks.TIMEOUT_S, shlex.quote(hooks.helm_bin()))


def _event_cmds(settings, event=HOOK_EVENT):
    """Every <event> hook command string in a settings dict (shape-tolerant)."""
    hks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hks.get(event) if isinstance(hks, dict) else None
    out = []
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict):
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.append(str(h["command"]))
    return out


def _resolvable(cmd):
    """The helm token before `record` resolves to a real executable."""
    import shlex
    import shutil
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return False
    for i, t in enumerate(toks):
        if t == "record" and i:
            h = toks[i - 1]
            if os.path.sep in h:
                return os.path.isfile(h) and os.access(h, os.X_OK)
            return bool(shutil.which(h))
    return False


def _merge_hook(settings, cmd, event=HOOK_EVENT):
    """-> (merged_copy, ok|add|update). MERGE-preserving (hooks.py law): only
    OUR <event> entry is written; foreign hooks — the UserPromptSubmit
    inject entry included — and every other key survive byte-identical.

    EVERY entry of ours in the event is visited, not the first. This carried
    the same return-on-first defect hooks._merge_event did (see its docstring
    for the incident): a second recorder entry in one event was repaired by
    nobody while the harness ran it, so re-installing — the standard cure for a
    stale path — could never reach it."""
    out = json.loads(json.dumps(settings))
    hks = out.setdefault("hooks", {})
    if not isinstance(hks, dict):
        raise ValueError("existing 'hooks' key is not an object — fix it by hand")
    groups = hks.setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError("existing hooks.%s is not a list — fix it by hand" % event)
    mine = [h for g in groups if isinstance(g, dict)
            for h in (g.get("hooks") or [])
            if isinstance(h, dict) and _ours(str(h.get("command") or ""))]
    if not mine:
        # no matcher: the recorder wants EVERY tool event
        groups.append({"hooks": [{"type": "command", "command": cmd}]})
        return out, "add"
    action = "ok"
    for h in mine:
        if h.get("command") != cmd or h.get("type") != "command":
            h["command"] = cmd
            h["type"] = "command"
            action = "update"
    return out, action


def install_home(path, dry=False):
    """Install/refresh both recorder legs through bounded content-revision CAS."""
    import difflib
    from . import configs
    sp = os.path.join(path, "settings.json")
    cmd = hook_command()

    def merge(cur):
        """The WHOLE merge, used by transform AND verify. The room repair
        mutates the candidate, so a step applied in only one of them would make
        every write fail its own verification."""
        from . import hooks
        out, actions = cur, []
        for ev in HOOK_EVENTS:
            out, action = _merge_hook(out, cmd, ev)
            actions.append(action)
        # The recorder composes its OWN command text (hook_command above) and
        # never goes through hooks.spec_command, so it needs the persistence
        # rail by name — precisely the "a new caller bypasses the derivation"
        # case it exists for. `_ours` is the recorder's, so the repair is
        # scoped to the entries this installer is responsible for and a foreign
        # hook living in a worktree is reported, never touched.
        return out, actions, hooks.repair_lane_room_commands(out, sp, owns=_ours)

    def transform(cur):
        merged, actions, notes = merge(cur)
        return merged, {"actions": actions, "rooms_notes": tuple(notes)}

    def verify(candidate, before, _metadata):
        from . import hooks
        expected, _actions, _notes = merge(before)
        return candidate == expected and all(
            cmd in _event_cmds(candidate, ev) for ev in HOOK_EVENTS) \
            and hooks._rooms_clean(candidate, owns=_ours)

    res = configs.transform_json_file(sp, transform, verify=verify, dry_run=dry)
    if not res.get("ok"):
        return "fail", res["error"]
    from . import hooks
    meta = res.get("initial_metadata") or res.get("metadata") or {}
    actions = meta.get("actions") or []
    rooms = hooks.lane_room_report(meta.get("rooms_notes") or ())
    # `ok` IS RESERVED FOR "I WROTE NOTHING" — the same law as hooks.install_home
    # (see the incident in its return site). A room repair rewrites bytes with
    # every recorder leg already current, so it must NOT report `ok`; the third
    # arm below exists for exactly that case and is not a spurious update.
    action = "add" if "add" in actions else \
        "update" if "update" in actions else \
        "update" if rooms and "repointed" in rooms else "ok"
    if action == "ok":
        return "ok", "hook up to date" + ("; " + rooms if rooms else "")
    if dry:
        diff = difflib.unified_diff(
            res["before"].splitlines(), res["after"].splitlines(),
            sp, sp + " (after install)", lineterm="")
        return "dry-" + action, "\n".join(diff) + (("\n" + rooms) if rooms else "")
    return action, "backup: %s; CAS attempts: %d%s" % (
        res.get("backup") or "none — new file", res["attempts"],
        "; " + rooms if rooms else "")


def status_rows():
    """Per-claude-home recorder coverage, read-only (hooks.status_rows shape).
    hook=True demands BOTH event legs — success-only wiring is a gap the
    installer closes."""
    from . import hooks
    rows = []
    for name, path in hooks.claude_homes():
        cmd = None
        try:
            with open(os.path.join(path, "settings.json"), encoding="utf-8") as f:
                s = json.load(f)
            per = [next((c for c in _event_cmds(s, ev) if _ours(c)), None)
                   for ev in HOOK_EVENTS]
            cmd = per[0] if all(per) else None
        except (OSError, ValueError):
            pass
        rows.append({"home": name, "path": path, "hook": bool(cmd), "command": cmd,
                     "resolvable": bool(cmd) and _resolvable(cmd),
                     "fail_open": bool(cmd) and "|| true" in cmd})
    return rows


def coverage():
    rows = status_rows()
    return (sum(1 for r in rows
                if r["hook"] and r["resolvable"] and r["fail_open"]), len(rows))


def _age(secs):
    if secs < 120:
        return "%ds" % secs
    if secs < 7200:
        return "%dm" % (secs // 60)
    if secs < 172800:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def _sessions():
    """[(key, counters_path, mtime)] newest first, counters.json present only."""
    root = state_root()
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for n in names:
        p = os.path.join(root, n, "counters.json")
        try:
            out.append((n, p, os.stat(p).st_mtime))
        except OSError:
            continue
    return sorted(out, key=lambda r: -r[2])


def doctor_rows():
    """[(level, msg)] for helm doctor: recorder wired per claude home + state
    fresh. WARN only on real gaps (unwired homes; wired-but-never-fired) —
    an idle estate is not a fault."""
    out = []
    try:
        n, m = coverage()
    except Exception as e:
        return [("WARN", "record wiring unknown (%s: %s)" % (e.__class__.__name__, e))]
    if m:
        msg = "record coverage: %d of %d claude homes" % (n, m)
        out.append(("OK", msg) if n == m else
                   ("WARN", msg + " — `helm record install` closes the gap"))
    ss = _sessions()
    if ss:
        out.append(("OK", "reflex-state: %d session%s, freshest %s ago" % (
            len(ss), "s"[:len(ss) != 1], _age(int(time.time() - ss[0][2])))))
    elif n:
        out.append(("WARN", "recorder wired but reflex-state is empty — no "
                            "PostToolUse event has landed yet"))
    return out


_USAGE = """usage: helm record [--hook-json]                (PostToolUse event JSON on stdin)
       helm record status [--session S]
       helm record install [--dry] [--home NAME]"""


def _cmd_status(rest):
    session = rest[rest.index("--session") + 1] if "--session" in rest else None
    try:
        n, m = coverage()
        line = "wiring: %d of %d claude homes (%s)" % (n, m, "+".join(HOOK_EVENTS))
        print("  " + (line if n == m or not m
                      else line + " — `helm record install` closes the gap"))
    except Exception:
        print("  wiring: unknown")
    ss = _sessions()
    if session:
        ss = [r for r in ss if r[0] == session_key(session)]
    if not ss:
        print("  no reflex-state yet — nothing recorded")
        return 0
    print("  %-36s %-5s %7s %5s %5s %4s %4s %5s" % (
        "session", "age", "passive", "dirty", "stuck", "loop", "cmds", "edits"))
    now = time.time()
    for key, path, mtime in ss[:12]:
        c = pk.read_json(path, {}) or {}
        d = os.path.dirname(path)
        def lines(name):
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    return sum(1 for _ in f)
            except OSError:
                return 0
        print("  %-36s %-5s %7s %5s %5s %4s %4s %5s" % (
            key[:36], _age(int(now - mtime)),
            c.get("passive-streak", 0), c.get("dirty-streak", 0),
            c.get("stuck-streak", 0), c.get("loop-streak", 0),
            lines("command-log.jsonl"), lines("edit-targets.log")))
    if len(ss) > 12:
        print("  (+%d more session%s)" % (len(ss) - 12, "s"[:len(ss) - 12 != 1]))
    return 0


def _cmd_install(rest):
    from . import hooks
    dry = "--dry" in rest
    name = rest[rest.index("--home") + 1] if "--home" in rest \
        and rest.index("--home") + 1 < len(rest) else None
    targets, err = hooks._select_homes(name)
    if err:
        print("helm record: " + err, file=sys.stderr)
        return 1
    if not targets:
        print("helm record: no claude homes found — `helm homes prepare claude <email>` starts one")
        return 0
    print("helm record: command: " + hook_command())
    failed = 0
    for hname, path in targets:
        action, detail = install_home(path, dry=dry)
        failed += action == "fail"
        if action.startswith("dry-"):
            print("  %-28s %s (dry — nothing written)" % (hname, action[4:]))
            if detail:
                print("    " + detail.replace("\n", "\n    "))
        else:
            print("  %-28s %-6s %s" % (hname, action, detail))
    if not dry:
        n, m = coverage()
        print("helm record: %d of %d claude homes covered" % (n, m))
    return 1 if failed else 0


def cmd_record(args):
    """record [--hook-json] | status [--session S] | install [--dry] [--home NAME]
    — the session-keyed tool-outcome recorder (PostToolUse event JSON on stdin)."""
    args = list(args)
    if args and args[0] == "status":
        return _cmd_status(args[1:])
    if args and args[0] == "install":
        return _cmd_install(args[1:])
    if args and args != ["--hook-json"]:
        print(_USAGE, file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        print(_USAGE, file=sys.stderr)
        return 2
    event = parse_event(sys.stdin.read())
    if event is None:
        return 0  # garbled or keyless payload: record nothing, never block
    record(event)
    return 0
