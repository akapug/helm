#!/usr/bin/env python3
"""The working set: hand a seat back what it was holding when it compacted.

THE MEASUREMENT (task/4054, 43 compactions of one lead). For about 50 tool
calls after a compaction a seat slips more: strict slips rose from 7.0 to 9.5
per 100 calls, and wrong verb spellings and wrong paths rose about 4x. About
half of those slips name a value the seat itself used in its last few hundred
calls, for example `chat read --seat` nine times within 23 calls of one
compaction. The compaction summary is prose a model wrote. It keeps the plan
and drops the exact spellings, paths and ids. The recorder already saw every
one of them.

TWO HALVES, ONE FILE PER SESSION.

  RECORDER (`note`). record._record calls it on every PostToolUse and
  PostToolUseFailure of the MAIN thread. It appends one small row to a ring,
  <reflex-state>/<session>/working-set.jsonl: the tool, ok or failed, paths,
  the verb spelling of a known CLI (program, verb, subverb and flag NAMES),
  task and dispatch-row ids, and a background task id. It costs one append
  and no subprocess. Past TRIM_BYTES the next append keeps the newest
  RING_ROWS rows; the reader also reads only the newest RING_ROWS.

  BUILDER (`build`, `hook`). `helm now show --hook-json` runs on SessionStart
  with matcher `compact` and returns additionalContext of at most MAX_CHARS
  characters (Claude Code caps hook context at 10,000 and past that saves it
  to a file and shows only a preview). Newest first: paths that still exist,
  spellings that worked, spellings that failed with a usage error marked
  DON'T, ids with how many calls ago each was touched, background tasks and
  whether each has an output file (a Monitor has none), and scratch dirs.

WHAT IS NEVER RECORDED. No command text, no flag values, no file contents,
no tool output, no chat text. A flag is kept by its name only (`--room`,
never what followed it). A path is kept only when it is an absolute or
home-relative path word with no shell or query characters in it. Ids are
matched by fixed patterns (task/N, a 12-hex dispatch row id).

WHO GETS IT. Only the main thread of the session that compacted. A subagent
shares its seat's session id, so its calls are not recorded (`note` is not
called for them), and a subagent's compaction gets nothing: the payload's
agent_id, the PreCompact record's agent, and resumeturn.compacting_thread
each rule it out. When nothing can prove which thread compacted, the set is
still given, with one line saying it is the main thread's.

FAIL OPEN. Every error in either half is swallowed. The hook prints nothing
and exits 0 on any fault, a corrupt ring included.

THE PRECOMPACT NAG MOVED HERE. Claude Code gives PreCompact stdout to the
summarizer as custom instructions, so `helm handoff check`'s nag became a
"next step" in 27 of 43 summaries. handoff.py now writes that nag to
NAG_FILE instead, and this hook hands it to the seat after the compaction,
once, as its own section. The kill switch and a builder fault still hand
the nag over alone. A seat whose settings predate this hook (no `helm hooks
install` and restart yet) gets it from inject on its next prompt instead
(task/4070): whichever reader comes first takes the file, so it arrives once.
"""
import json
import os
import re
import sys
import tempfile
import time

RING_FILE = "working-set.jsonl"
NAG_FILE = "precompact-nag.txt"
RING_ROWS = 400            # the rows a build reads: the last few hundred calls
TRIM_BYTES = 160 * 1024    # past this the next append keeps RING_ROWS rows
NAG_FRESH_S = 3600         # a nag older than this describes another compaction
NAG_HEAD = "The PreCompact handoff check said:"
MAX_CHARS = 7000           # Claude Code's hook-context cap is 10,000
LINE_CHARS = 220
SCAN_CHARS = 4000          # how much tool output is searched for ids
CAPS = {"paths": 30, "ok": 25, "bad": 12, "ids": 15, "bg": 8, "scratch": 6,
        "nag": 4}

#: The CLIs whose spellings are worth handing back. Their verbs and flags
#: are where a compacted seat slips; anything else is not recorded.
PROGS = frozenset(("helm", "git", "gh", "fab", "orca", "cv"))
# git commands whose next word is a subcommand, not a value: `git worktree
# list` must not come back as `git worktree --porcelain`, a spelling that
# fails. Any other git word after the subcommand (a branch, a file, a
# remote's name) is a value and stays out.
GIT_SUBVERBS = frozenset(("worktree", "stash", "remote", "submodule", "notes",
                          "bisect", "sparse-checkout", "reflog", "maintenance"))
_FLAG = re.compile(r"\A--?[A-Za-z][A-Za-z0-9-]{0,30}\Z")
_WORD = re.compile(r"\A[a-z][a-z-]{1,23}\Z")
_NUM = re.compile(r"\A\d{1,6}\Z")
_HEX12 = re.compile(r"\A[0-9a-f]{12}\Z")
_TASK = re.compile(r"\btask/(\d{1,6})\b")
_ROW = re.compile(r"\brow (?:id )?([0-9a-f]{12})\b")
_BGID = re.compile(r"with ID: ([A-Za-z0-9_-]{4,40})")
_BGOUT = re.compile(r"Output is being written to: (/[^\s'\"]+?\.output)\b")
_SAFE_ID = re.compile(r"\A[A-Za-z0-9_-]{4,40}\Z")
_PATH_BAD = re.compile(r"[\s=?&@*\"'`$<>|;{}()\[\]\\]")
#: A failure is a SPELLING failure only when its text says so. Any other
#: nonzero exit (grep found nothing, a check said unmet) is not a reason to
#: tell a seat never to type that command again.
_USAGE = re.compile(
    r"unknown (?:verb|subverb|sub-verb|subcommand|flag|option|argument|command)"
    r"|unrecognized (?:arguments?|option)|no such (?:option|subcommand|command)"
    r"|invalid choice|did you mean|(?:\A|\n)\s*usage:"
    r"|is not a (?:git|gh) command|command not found|requires an argument"
    r"|expected one argument", re.I)
_BG_KEYS = ("backgroundTaskId", "taskId", "task_id", "shellId", "monitorId")


# ---------------------------------------------------------------------------
# recorder
# ---------------------------------------------------------------------------

def ring_path(sd):
    return os.path.join(sd, RING_FILE)


def _helm_verbs():
    """helm's own verb table. A verb outside it is a value or a typo, never
    a spelling to hand back. The record hook runs inside helm's CLI, so the
    import is already done and this is one dict lookup."""
    try:
        from . import cli
        return cli.VERBS
    except Exception:                         # noqa: BLE001 — fail open
        return None


def spelling(argv):
    """`prog verb [subverb] --flag ...` for one argv of a known CLI, or None.

    Words are kept only while they look like verbs (lowercase letters and
    hyphens) and only before the first flag, so a value, an id or free text
    never becomes part of a spelling. Flags keep their NAME: `--room=x` and
    `--room x` both record `--room`. Flags past `--` belong to the command
    being run, not this CLI, and are left out."""
    from . import record
    if not argv:
        return None
    prog = os.path.basename(argv[0])
    if prog not in PROGS:
        return None
    if prog == "git":
        sub = record._git_subcommand(argv)
        if not sub or not _WORD.match(sub):
            return None
        words, rest = [sub], argv[argv.index(sub) + 1:]
        if sub in GIT_SUBVERBS and rest and _WORD.match(rest[0]):
            words.append(rest[0])
            rest = rest[1:]
    else:
        words = []
        for w in argv[1:3]:
            if not _WORD.match(w):
                break
            words.append(w)
        if not words:
            return None
        if prog == "helm":
            verbs = _helm_verbs()
            if verbs is not None and words[0] not in verbs:
                return None
        rest = argv[1 + len(words):]
    flags = []
    for w in rest:
        if w == "--":
            break
        if w.startswith("-"):
            f = w.split("=", 1)[0]
            # A short flag can carry its value glued on (-bSECRET, -mTEXT):
            # keep only its letter, so a value never reaches the ring.
            if not f.startswith("--"):
                f = f[:2]
            if _FLAG.match(f) and f not in flags:
                flags.append(f)
    return " ".join([prog] + words + flags[:8])


def sensitive_path(p):
    """True for an env or credential-shaped path: `*.env`, `.env*`,
    `credentials*`, `*secret*`, `*.pem`, `*.key`, anything under `~/.aws`.
    Its name is not a value, but a seat handed it back is one step from
    reading it into its context; the set leaves it out, at the recorder and
    again at the builder (a ring written before this rule)."""
    full = os.path.expanduser(p).lower()
    base = os.path.basename(full.rstrip("/"))
    aws = os.path.join(os.path.expanduser("~").lower(), ".aws")
    return (base.endswith((".env", ".pem", ".key")) or base.startswith(
        (".env", "credentials")) or "secret" in full
        or full == aws or full.startswith(aws + "/"))


def _path_word(w):
    """An absolute or home-relative path in one argv word, or None."""
    if not (w.startswith("/") or w.startswith("~/")) or len(w) < 2 \
            or len(w) > 240 or _PATH_BAD.search(w) \
            or w.startswith(("/dev/", "/proc/", "/sys/")) \
            or sensitive_path(w):
        return None
    return w.rstrip("/") or "/"


def _ids_from_argv(argv, out):
    for w in argv:
        for m in _TASK.finditer(w):
            out.append("task/" + m.group(1))
    if os.path.basename(argv[0]) != "helm" or len(argv) < 2:
        return
    pos = [w for w in argv[1:] if not w.startswith("-")]
    if pos[:1] == ["task"] and len(pos) > 2 and _NUM.match(pos[2]):
        out.append("task/" + pos[2])
    if pos[:1] == ["dispatch"]:
        out.extend("row " + w for w in pos[2:] if _HEX12.match(w))


def _mints(argv):
    """True when this argv runs a helm WRITE verb, the only kind whose output
    names an id worth keeping (task add prints the new task's id). A read's
    output (task list) would flood the set with ids nobody touched."""
    from . import record
    if len(argv) < 3 or os.path.basename(argv[0]) != "helm":
        return False
    subs = record.COORD_WRITES.get(argv[1], ())
    return subs is None or argv[2] in subs


def row(tool, tin, resp, rtext, failed, cmd):
    """One ring row for a main-thread tool call. Pure: reads the event only."""
    from . import record
    r = {"ts": int(time.time()), "t": tool[:40], "ok": 0 if failed else 1}
    paths, ids, spells = [], [], []
    for k in ("file_path", "notebook_path", "path"):
        v = tin.get(k)
        if isinstance(v, str) and v:
            p = _path_word(v)
            if p:
                paths.append(p)
    mints = False
    if tool == "Bash" and cmd:
        for argv in record._argvs(cmd):
            s = spelling(argv)
            if s and s not in spells:
                spells.append(s)
            _ids_from_argv(argv, ids)
            mints = mints or _mints(argv)
            for w in argv[1:]:
                p = _path_word(w)
                if p and p not in paths:
                    paths.append(p)
    text = (rtext or "")[:SCAN_CHARS]
    if mints and not failed:
        ids.extend("task/" + m.group(1) for m in _TASK.finditer(text))
        ids.extend("row " + m.group(1) for m in _ROW.finditer(text))
    if paths:
        r["p"] = paths[:4]
        if tool in record.EDITS and not failed:
            r["e"] = 1
    if spells:
        r["v"] = spells[:4]
        if failed and _USAGE.search(text):
            # Name the spelling the error text names; one spelling alone is
            # the culprit by elimination. Several and none named: no DON'T.
            named = [s for s in spells
                     if any(w in text for w in s.split()[1:] if len(w) > 2)]
            bad = named or (spells if len(spells) == 1 else [])
            if bad:
                r["x"] = bad[:4]
    uniq = []
    for i in ids:
        if i not in uniq:
            uniq.append(i)
    if uniq:
        r["id"] = uniq[:6]
    bg = _bg(tool, tin, resp, text)
    if bg:
        r["bg"], r["bk"] = bg
        m = _BGOUT.search(text)
        if m and _path_word(m.group(1)):
            r["bo"] = m.group(1)
    return r


def _bg(tool, tin, resp, text):
    """(id, kind) when this call started a background task, else None."""
    kind = "monitor" if tool == "Monitor" else (
        "agent" if tool in ("Agent", "Task") else "bash")
    if kind == "agent" and not tin.get("run_in_background"):
        return None
    if kind == "bash" and tool != "Bash":
        return None
    keys = _BG_KEYS + (("agentId",) if kind == "agent" else ())
    if isinstance(resp, dict):
        for k in keys:
            v = resp.get(k)
            if isinstance(v, str) and _SAFE_ID.match(v):
                return v, kind
    m = _BGID.search(text)
    return (m.group(1), kind) if m else None


def note(sd, tool, tin, resp, rtext, failed, cmd):
    """Record one main-thread call. The caller swallows any error."""
    append(sd, row(tool, tin, resp, rtext, failed, cmd))


def append(sd, r):
    """One append; a trim only when the file has grown past TRIM_BYTES."""
    os.makedirs(sd, exist_ok=True)
    path = ring_path(sd)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(r, separators=(",", ":")) + "\n")
        size = f.tell()
    if size > TRIM_BYTES:
        trim(path)


def trim(path):
    """Keep the newest RING_ROWS rows. A row appended by a parallel call
    between this read and the replace is lost: one row of a few hundred."""
    from . import pk
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines(True)
    pk.atomic_write(path, "".join(lines[-RING_ROWS:]))


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------

def ring(sid):
    """The newest RING_ROWS rows for one session, oldest first. A missing or
    unreadable ring is []. A line that is not a JSON object is skipped."""
    from . import record
    path = ring_path(record.session_dir(sid))
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - 2 * TRIM_BYTES)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if start:
        lines = lines[1:]                   # the first line may be cut
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("t"), str):
            out.append(d)
    return out[-RING_ROWS:]


def _strs(v):
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _ago(n):
    return "last call" if n == 0 else "%d ago" % n


def _scratch(p):
    i = p.rfind("/scratchpad")
    if i < 0:
        return None
    base = p[:i + len("/scratchpad")]
    first = p[len(base):].lstrip("/").split("/")[0]
    sub = os.path.join(base, first) if first else base
    return sub if first and os.path.isdir(sub) else base


def build(rows, tasks_dir=None, nag=None, caveat=False):
    """The additionalContext text for one ring, or '' when there is nothing
    to say. At most MAX_CHARS characters, newest first in every section."""
    n = len(rows)
    paths, ok, bad, ids, bgs = {}, {}, {}, {}, {}
    for i in range(n - 1, -1, -1):
        r, ago = rows[i], n - 1 - i
        good = r.get("ok") == 1
        for p in _strs(r.get("p")):
            e = paths.setdefault(p, [ago, 0])
            if r.get("e") and good:
                e[1] = 1
        for s in _strs(r.get("v")):
            if good:
                ok.setdefault(s, [ago, 0])[1] += 1
        for s in _strs(r.get("x")):
            bad.setdefault(s, [ago, 0])[1] += 1
        for d in _strs(r.get("id")):
            ids.setdefault(d, [ago, 0])[1] += 1
        b = r.get("bg")
        if isinstance(b, str) and _SAFE_ID.match(b) and b not in bgs:
            bo = r.get("bo") if isinstance(r.get("bo"), str) else None
            bgs[b] = (ago, str(r.get("bk") or "bash"), bo)
    sec = []
    lines, scratch = [], []
    for p, (ago, edited) in paths.items():
        full = os.path.expanduser(p)
        if sensitive_path(p) or not os.path.exists(full):
            continue                         # a deleted path is a wrong path
        s = _scratch(full)
        if s and s not in scratch:
            scratch.append(s)
        lines.append("  %s%s  (%s%s)" % (p, "/" if os.path.isdir(full) else "",
                                         _ago(ago), ", edited" if edited else ""))
    sec.append(("Paths that exist now:", lines[:CAPS["paths"]]))
    sec.append(("Spellings that worked:", [
        "  %s  (x%d, %s)" % (s, c, _ago(a))
        for s, (a, c) in list(ok.items())[:CAPS["ok"]]]))
    lines = []
    for s, (a, c) in bad.items():
        if s in ok and ok[s][0] < a:
            continue                         # it worked since it failed
        head = " ".join(s.split()[:3])
        use = next((o for o in ok if o != s and o.startswith(head)), None)
        lines.append("  DON'T %s  (failed x%d, %s)%s"
                     % (s, c, _ago(a), ("  use: " + use) if use else ""))
    sec.append(("Spellings that FAILED with a usage error:",
                lines[:CAPS["bad"]]))
    sec.append(("Ids you touched:", [
        "  %s  (%s, x%d)" % (d, _ago(a), c)
        for d, (a, c) in list(ids.items())[:CAPS["ids"]]]))
    lines = []
    for b, (a, kind, bo) in list(bgs.items())[:CAPS["bg"]]:
        if kind == "monitor":
            lines.append("  %s  (Monitor, %s): no output file; its events "
                         "arrive as notifications" % (b, _ago(a)))
            continue
        out = bo or (os.path.join(tasks_dir, b + ".output") if tasks_dir
                     else None)
        if out and os.path.exists(out):
            lines.append("  %s  (%s, %s): output %s" % (b, kind, _ago(a), out))
        elif out:
            lines.append("  %s  (%s, %s): NO output file at %s"
                         % (b, kind, _ago(a), out))
        else:
            lines.append("  %s  (%s, %s): output file unknown" % (b, kind, _ago(a)))
    sec.append(("Background tasks:", lines))
    sec.append(("Scratch dirs:", ["  " + s for s in scratch[:CAPS["scratch"]]]))
    sec.append((NAG_HEAD, ["  " + x for x in (nag or [])[:CAPS["nag"]]]))
    sec = [(t, [_cut(x) for x in ls]) for t, ls in sec if ls]
    if not sec:
        return ""
    head = [_cut("Working set: facts from this session's last %d tool calls "
                 "before the compaction, read from helm's recorder, not the "
                 "summary. Newest first; 'N ago' counts tool calls." % n)]
    if caveat:
        head.append("This is the main thread's set. A subagent reading it: "
                    "none of this is yours.")
    return _fit(head, sec)


def _cut(s):
    return s if len(s) <= LINE_CHARS else s[:LINE_CHARS - 3] + "..."


def _render(head, sec):
    out = list(head)
    for t, ls in sec:
        out.append(t)
        out.extend(ls)
    return "\n".join(out)


def _fit(head, sec):
    """Drop the oldest line of the longest section until the text fits."""
    sec = [(t, list(ls)) for t, ls in sec]
    text = _render(head, sec)
    while len(text) > MAX_CHARS:
        t, ls = max(sec, key=lambda s: len(s[1]))
        ls.pop()
        sec = [s for s in sec if s[1]]
        text = _render(head, sec)
    return text


# ---------------------------------------------------------------------------
# the PreCompact nag's channel
# ---------------------------------------------------------------------------

def put_nag(sid, text):
    """Keep the PreCompact nag for the SessionStart after it."""
    from . import pk, record
    if sid and text:
        pk.atomic_write(os.path.join(record.session_dir(sid), NAG_FILE), text)


def nag_text(lines, head=None):
    """The nag alone: what a seat gets when no working set is built (the
    kill switch, a builder fault) and what inject hands a seat whose
    SessionStart(compact) hook is not installed yet (task/4070)."""
    if not lines:
        return ""
    return "\n".join([head or NAG_HEAD]
                     + [_cut("  " + x) for x in lines[:CAPS["nag"]]])


def take_nag(sid, now=None):
    """The nag the PreCompact check left, once: read, then removed. A nag
    older than NAG_FRESH_S belongs to another compaction and is dropped."""
    from . import record
    path = os.path.join(record.session_dir(sid), NAG_FILE)
    try:
        fresh = (now or time.time()) - os.stat(path).st_mtime < NAG_FRESH_S
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        os.remove(path)
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()] if fresh else []


# ---------------------------------------------------------------------------
# the SessionStart(compact) hook
# ---------------------------------------------------------------------------

LEAD, CHILD, UNKNOWN = "lead", "child", "unknown"


def thread(sid, transcript, now):
    """Which thread compacted: LEAD, CHILD or UNKNOWN.

    The SessionStart payload cannot say (resumeturn's block above
    compacting_thread has the measurement), so two other readers decide.
    The PreCompact record handoff.py wrote for this session carries the
    compacting thread's agent_id: a string there is a subagent. Otherwise
    the transcripts decide, through resumeturn.compacting_thread; when they
    cannot prove a child, a fresh main-thread record proves the lead."""
    from . import resumeturn, seats
    entry = resumeturn._peek(resumeturn.compaction_key(seats.own_name(), sid))
    entry = entry if isinstance(entry, dict) else {}
    rec = entry.get("precompact")
    fresh = False
    if isinstance(rec, dict) and rec.get("session") == sid:
        at = rec.get("at")
        fresh = isinstance(at, (int, float)) \
            and 0 <= now - at < resumeturn.precompact_window_s()
        if fresh and isinstance(rec.get("agent"), str) and rec["agent"]:
            return CHILD
    found, _why = resumeturn.compacting_thread(transcript, sid, entry, now)
    if found == resumeturn.THREAD_CHILD:
        return CHILD
    if found == resumeturn.THREAD_LEAD or (fresh and rec.get("agent") is None):
        return LEAD
    return UNKNOWN


def tasks_dir(sid, transcript):
    """Where Claude Code writes background task output for this session:
    <tmp>/claude-<uid>/<project slug>/<session>/tasks. The slug is the
    directory the transcript sits in."""
    slug = os.path.basename(os.path.dirname(transcript)) if transcript else ""
    if not slug or not sid:
        return None
    root = os.environ.get("CLAUDE_CODE_TMPDIR") or tempfile.gettempdir()
    return os.path.join(root, "claude-%d" % os.getuid(), slug, sid, "tasks")


def hook(payload, now=None):
    """One SessionStart payload -> the additionalContext text, or ''."""
    from . import actors, home
    if not isinstance(payload, dict):
        return ""
    if str(payload.get("hook_event_name") or "SessionStart") != "SessionStart":
        return ""
    if str(payload.get("source") or "") != "compact":
        return ""
    sid = str(payload.get("session_id") or "")
    if not sid or actors.sidechain_agent(payload):
        return ""
    now = now or time.time()
    transcript = str(payload.get("transcript_path") or "")
    who = thread(sid, transcript, now)
    if who == CHILD:
        return ""
    # THE NAG IS THE HANDOFF CONTRACT'S, NOT THE WORKING SET'S (task/4070).
    # It is taken before the kill switch and before the builder runs, and
    # take_nag removes the file, so neither the kill switch nor a builder
    # fault may swallow it: each still hands the seat the nag alone.
    nag = take_nag(sid, now)
    if home.env("WORKING_SET", "1") == "0":
        return nag_text(nag)
    try:
        return build(ring(sid), tasks_dir=tasks_dir(sid, transcript),
                     nag=nag, caveat=who == UNKNOWN)
    except Exception:                         # noqa: BLE001 — fail open
        return nag_text(nag)


def cmd_hook(stdin=None):
    """`helm now show --hook-json`: print the SessionStart envelope, or
    nothing. Exit 0 on every path."""
    try:
        payload = json.loads((stdin or sys.stdin).read() or "")
        text = hook(payload)
        if text:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception:                         # noqa: BLE001 — fail open
        pass
    return 0
