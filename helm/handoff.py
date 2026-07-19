#!/usr/bin/env python3
"""helm handoff + now — compaction continuity: the wheel survives the window.

Two legs on one PreCompact trigger (the handoff-now card):

  handoff (AUTHORED, primary) — the handoff contract. `write` turns stdin
  DONE/REMAINING/NEXT prose into a typed journal entry on the project shelf
  (~/.helm/<project>/journal/) — greppable, cv-searchable, ship/pull-able.
  `check` asks ONE question: does a handoff artifact for THIS session exist
  (a journal entry carrying the session id, or a repo HANDOFF_NEXT_SESSION.md)
  newer than session start? Hook-wired on PreCompact + SessionEnd it NAGS in a
  few lines when unmet — a nag, never a capture: the agent authors audited
  prose, helm never invents a summary. `recover <sid>` re-reads the span a
  compaction discarded by wrapping the ONE recall index (`cv show <sid>
  --pre-compaction`, architecture law 4 — record how to query, never a
  second index).

  now (AUTOMATIC safety net) — `capture` snapshots session id + project +
  git branch/status/changed paths + this session's recorded edits and reflex
  counters into _global/now.md: NEWEST-FIRST, 40-line cap. `show` prints it
  ONLY while <48h fresh (a stale now.md actively misleads — the gate is
  load-bearing, not polish), shaped for SessionStart additionalContext;
  legacy ~/.remember/now.md is honored as a read fallback until retired.

Laws:
  * SESSION-keyed from the payload's session_id, never pane (record.py law).
  * Hook mode is FAIL-OPEN TOTAL: capture never blocks a compaction — git
    probes carry a 3s timeout, every exception is swallowed, rc 0 always,
    SILENT when the contract is satisfied (salience law).
  * now.md is DERIVED telemetry (rebuildable, never ships, no receipt); the
    handoff entry is AUTHORED (journal shelf, mutation receipt, ships).
"""
import json
import os
import re
import sys
import time

from . import home, pk

FRESH_H = 48          # the one freshness constant: show's gate AND check's
NOW_LINES = 40        # unknown-session-start window (a stale artifact misleads)
GIT_TIMEOUT = 3
REPO_FILE = "HANDOFF_NEXT_SESSION.md"
# argv-safety (transcripts.py law): a sid riding subprocess argv must look
# like an id — never like a flag.
_SAFE_SID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
# a section heading counts only as `# DONE ...` / `**DONE**` / `DONE: ...` —
# bare prose starting with the word never false-matches.
_SECTION = re.compile(r"^\s*(#+\s*)?\**(DONE|REMAINING|NEXT)\b\**\s*([:\-—].*)?$",
                      re.I)


def now_path():
    return os.path.join(home.global_dir(), "now.md")


def legacy_now_path():
    """~/.remember/now.md — the convention this snapshot replaces; read
    fallback until retired. HELM_REMEMBER_DIR overrides (tests)."""
    d = home.env("REMEMBER_DIR") or os.path.join(os.path.expanduser("~"), ".remember")
    return os.path.join(os.path.expanduser(d), "now.md")


def _git(cwd, *args):
    """git -C <cwd> <args> -> stripped stdout; None on ANY trouble (no git,
    nonzero, timeout) — a probe must never block a compaction."""
    import subprocess
    try:
        p = subprocess.run(["git", "-C", cwd or "."] + list(args),
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _hook(raw):
    """Hook event JSON -> dict, {} on garbage — unknown keys tolerated;
    PreCompact / SessionEnd / SessionStart payloads all parse the same."""
    try:
        d = json.loads(raw or "")
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _fresh(path, hours=FRESH_H):
    """Age in seconds while inside the freshness window, else None."""
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        return None
    return age if age < hours * 3600 else None


def _age(secs):
    secs = int(secs)
    if secs < 120:
        return "%ds" % secs
    if secs < 7200:
        return "%dm" % (secs // 60)
    return "%dh" % (secs // 3600)


def _session_start(transcript):
    """Session start epoch from the transcript's first timestamped lines
    (claude's opening lines are unstamped bookkeeping — scan a bounded head);
    file ctime fallback; None when nothing is known."""
    if not transcript:
        return None
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f):
                if i >= 80:
                    break
                m = re.search(r'"timestamp"\s*:\s*"(\d{4})-(\d{2})-(\d{2})'
                              r'[T ](\d{2}):(\d{2}):(\d{2})', ln)
                if m:
                    import calendar
                    return float(calendar.timegm(
                        tuple(int(x) for x in m.groups()) + (0, 0, 0)))
        return os.stat(transcript).st_ctime
    except OSError:
        return None


def _project(cwd, explicit=None):
    """Explicit --project wins; else the registry's longest-prefix owner of
    cwd (inject's one derivation — never a second resolver)."""
    if explicit:
        return explicit
    from . import inject
    return inject.project_for_cwd(cwd)


# ---------------------------------------------------------------------------
# now — the automatic safety net
# ---------------------------------------------------------------------------

def snapshot(sid, cwd):
    """One session's now-block, <=5 terse lines — a missing source (no git,
    no reflex state) drops its line, never errs."""
    from . import record
    lines = ["## %s sid=%s project=%s"
             % (pk.now_ts(), (sid or "-")[:8], _project(cwd) or "-")]
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        lines.append("   cwd %s  (no git)" % (cwd or "-"))
    else:
        porc = (_git(cwd, "status", "--porcelain") or "").splitlines()
        lines.append("   cwd %s  branch %s  %s" % (
            cwd or "-", branch, ("dirty %d" % len(porc)) if porc else "clean"))
        if porc:
            # split, never a column offset: _git strips stdout, so the FIRST
            # porcelain line loses its leading status space (live-smoke catch)
            lines.append("   changed: " + " ".join(
                l.split(None, 1)[1] for l in porc[:4] if len(l.split(None, 1)) > 1))
    if sid:
        try:
            with open(os.path.join(record.session_dir(sid), "edit-targets.log"),
                      encoding="utf-8") as f:
                seen = []
                for name in reversed(f.read().splitlines()):
                    if name and name not in seen:
                        seen.append(name)
                    if len(seen) == 5:
                        break
            if seen:
                lines.append("   edited: " + " ".join(seen))
        except OSError:
            pass
        c = record.counters(sid)
        if c:
            lines.append("   reflex: passive=%s dirty=%s stuck=%s loop=%s" % (
                c.get("passive-streak", 0), c.get("dirty-streak", 0),
                c.get("stuck-streak", 0), c.get("loop-streak", 0)))
    return lines


def capture(sid, cwd):
    """Prepend one snapshot block to _global/now.md, NEWEST-FIRST, capped at
    NOW_LINES. FAIL-OPEN TOTAL -> path or None: capture never raises and
    never blocks the compaction that triggered it. DERIVED telemetry — no
    event receipt, and ship never carries it."""
    try:
        block = snapshot(sid, cwd)
        path = now_path()
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read().splitlines()
        except OSError:
            old = []
        pk.atomic_write(path, "\n".join(
            (block + ([""] if old else []) + old)[:NOW_LINES]) + "\n")
        return path
    except Exception:
        return None


def show():
    """The freshest snapshot text, or '' — _global/now.md while <48h fresh,
    else the legacy ~/.remember/now.md (read fallback until retired). Stale
    or absent -> EMPTY: a stale now.md actively misleads; silence is the
    truthful output (and SessionStart injects nothing)."""
    for path in (now_path(), legacy_now_path()):
        if _fresh(path) is None:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            continue
        if text:
            return "helm now (session-continuity snapshots, newest first):\n" + text
    return ""


# ---------------------------------------------------------------------------
# handoff — the authored contract
# ---------------------------------------------------------------------------

def _summaries(text):
    """DONE/REMAINING/NEXT one-liners out of handoff prose: a heading's inline
    remainder, else its first non-empty following line."""
    out = {}
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        m = _SECTION.match(ln)
        if not m or not (m.group(1) or m.group(3) is not None):
            continue
        key = m.group(2).lower()
        if key in out:
            continue
        rest = (m.group(3) or "")[1:].strip().lstrip("*").strip()
        if not rest:
            rest = next((l.strip().lstrip("-* ") for l in lines[i + 1:]
                         if l.strip()), "")
        out[key] = re.sub(r"\s+", " ", rest)
    return out


def write_entry(text, project, sid):
    """stdin prose -> the typed journal entry on the project shelf
    (<date>-handoff-<sid8>.md; a same-day same-session re-write lands on the
    same path — the newest handoff wins). Frontmatter carries the one-line
    DONE/REMAINING/NEXT summaries + the session id, so the entry is
    greppable, searchable, and ships with the authored chain.
    -> (path, missing-section-names)."""
    s = _summaries(text)
    ts = pk.now_ts()
    tag = "%s-handoff-%s" % (ts[:10], (sid or "session")[:8])
    path = os.path.join(home.project_dir(project), "journal", tag + ".md")
    head = next((s[k] for k in ("next", "done", "remaining") if s.get(k)), "") \
        or next((l.strip() for l in text.splitlines() if l.strip()), "")
    body = [
        "---",
        "name: " + tag,
        'description: "handoff: ' + head.replace('"', "'")[:150] + '"',
        "metadata:",
        "  type: handoff",
        "  session_id: " + (sid or ""),
        "  project: " + project,
        "  ts: " + ts,
        "  done: " + s.get("done", ""),
        "  remaining: " + s.get("remaining", ""),
        "  next: " + s.get("next", ""),
        "---", "", text.rstrip(), ""]
    pk.atomic_write(path, "\n".join(body))
    pk.event("handoff.write", sid or project, project + " — " + head)
    return path, [k for k in ("done", "remaining", "next") if not s.get(k)]


def check(sid, cwd, start):
    """The contract question -> the satisfying artifact's path, or None.
    A journal entry on cwd's project shelf CARRYING the session id, or the
    repo's HANDOFF_NEXT_SESSION.md — modified after session start. start=None
    reads as now-48h (the one freshness constant): with no known start only
    a RECENT artifact can satisfy — an old handoff must never read as done."""
    if start is None:
        start = time.time() - FRESH_H * 3600
    project = _project(cwd)
    if project and sid:
        d = os.path.join(home.project_dir(project), "journal")
        try:
            names = sorted(os.listdir(d), reverse=True)  # newest date first
        except OSError:
            names = []
        for n in names:
            p = os.path.join(d, n)
            try:
                if not n.endswith(".md") or os.stat(p).st_mtime < start:
                    continue
                with open(p, encoding="utf-8", errors="replace") as f:
                    if sid in f.read(65536):
                        return p
            except OSError:
                continue
    top = _git(cwd, "rev-parse", "--show-toplevel") or cwd
    rp = os.path.join(top, REPO_FILE) if top else None
    try:
        if rp and os.stat(rp).st_mtime >= start:
            return rp
    except OSError:
        pass
    return None


def _nag(sid, captured):
    """The PreCompact nag — a FEW lines (PreCompact fires late), a nag never
    a capture: the agent authors the audited prose itself."""
    out = ["helm handoff: NO handoff artifact for session %s — the next window "
           "starts blind." % ((sid or "?")[:8]),
           "  author one NOW: helm handoff write   (stdin: DONE/REMAINING/NEXT prose "
           "-> the project journal shelf)"]
    if captured:
        out.append("  automatic snapshot taken — the next session reads it via: "
                   "helm now show")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = """usage: helm handoff check [--hook-json] [--session S]
       helm handoff write [--project P] [--session S]   (stdin: DONE/REMAINING/NEXT prose)
       helm handoff recover <sid>"""

_NOW_USAGE = """usage: helm now capture [--hook-json] [--session S]
       helm now show"""


def _opt(rest, flag):
    if flag in rest:
        i = rest.index(flag)
        v = rest[i + 1] if i + 1 < len(rest) else None
        del rest[i:i + 2]
        return v


def cmd_handoff(args):
    """handoff check|write|recover <sid> — the compaction-continuity contract."""
    args = list(args)
    if not args:
        print(_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    session = _opt(rest, "--session")
    project = _opt(rest, "--project")

    if verb == "check":
        if "--hook-json" in rest:
            # PreCompact/SessionEnd: fail-open TOTAL, rc 0 always, silent when
            # satisfied; a garbled payload nags nobody (record.py's gate).
            try:
                d = _hook(sys.stdin.read())
                if not d:
                    return 0
                sid = str(d.get("session_id") or "") or session
                cwd = str(d.get("cwd") or "") or os.getcwd()
                captured = capture(sid, cwd)  # leg 2 rides the same trigger
                if check(sid, cwd,
                         _session_start(str(d.get("transcript_path") or ""))) is None:
                    print(_nag(sid, captured))
            except Exception:
                pass
            return 0
        sid = session or os.environ.get("CLAUDE_SESSION_ID")
        path = check(sid, os.getcwd(), None)
        if path:
            print("helm handoff: contract satisfied — %s (%s old)"
                  % (path, _age(time.time() - os.stat(path).st_mtime)))
            return 0
        print(_nag(sid, None))
        return 1

    if verb == "write":
        if sys.stdin.isatty():
            print(_USAGE, file=sys.stderr)
            return 2
        text = sys.stdin.read()
        if not text.strip():
            print("helm handoff write: empty stdin — nothing to hand off",
                  file=sys.stderr)
            return 2
        proj = _project(os.getcwd(), project)
        if not proj:
            print("helm handoff: no registry project claims this cwd — pass "
                  "--project <name>", file=sys.stderr)
            return 1
        sid = session or os.environ.get("CLAUDE_SESSION_ID")
        path, missing = write_entry(text, proj, sid)
        if missing:
            print("helm handoff: WARNING no %s section parsed — the contract "
                  "wants DONE/REMAINING/NEXT" % "/".join(missing).upper(),
                  file=sys.stderr)
        print("helm handoff: journal entry landed — " + path)
        print("  the next window confirms via `helm handoff check`; "
              "`helm ship` carries it cross-machine")
        return 0

    if verb == "recover":
        sid = next((a for a in rest if not a.startswith("--")), None)
        if not sid or not _SAFE_SID.match(sid):
            print(_USAGE, file=sys.stderr)
            return 2
        import subprocess
        cmdv = ["cv", "show", sid, "--pre-compaction"]
        try:
            return subprocess.run(cmdv, timeout=120).returncode
        except FileNotFoundError:
            print("helm handoff recover: cv not on PATH — the one recall index "
                  "(architecture law 4); run: " + " ".join(cmdv), file=sys.stderr)
            return 1
        except Exception as e:
            print("helm handoff recover: cv failed (%s)" % e, file=sys.stderr)
            return 1

    print("helm handoff: unknown subverb %r" % verb, file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2


def cmd_now(args):
    """now capture|show — the automatic session-continuity snapshot."""
    args = list(args)
    if not args:
        print(_NOW_USAGE, file=sys.stderr)
        return 2
    verb, rest = args[0], args[1:]
    session = _opt(rest, "--session")

    if verb == "capture":
        hook = "--hook-json" in rest
        sid, cwd = None, os.getcwd()
        if hook and not sys.stdin.isatty():
            d = _hook(sys.stdin.read())
            sid = str(d.get("session_id") or "") or None
            cwd = str(d.get("cwd") or "") or cwd
        sid = sid or session or os.environ.get("CLAUDE_SESSION_ID")
        path = capture(sid, cwd)
        if hook:
            return 0  # a safety net is silent and never blocks
        if path is None:
            print("helm now: capture failed (fail-open — nothing written)",
                  file=sys.stderr)
            return 1
        print("helm now: snapshot -> " + path)
        return 0

    if verb == "show":
        text = show()
        if text:
            print(text)
        return 0

    print("helm now: unknown subverb %r" % verb, file=sys.stderr)
    print(_NOW_USAGE, file=sys.stderr)
    return 2
