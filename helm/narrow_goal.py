#!/usr/bin/env python3
"""The narrow-goal rung: before the first build act of a turn, the seat
answers the owner's question "am I optimizing on too narrow a goal?".

WHY IT IS A REFUSAL AND NOT A REMINDER. The owner had to ask that question
himself at least seven times in one morning: a review queue fed oldest-first
instead of most-unblocking-first, one seat hand-feeding a reviewer's queue
until it was a funnel, work read one item at a time instead of fanned out, a
fix made one level up instead of re-deriving the principle, changes merged
and never made live. The guidance already existed as store text
(`premise:am-i-optimizing-on-too-narrow-a-goal`) and was injected at
`helm dispatch send`, and the seat skimmed it. Ambient text gets skimmed. So
the seat must WRITE an answer, at the moment the answer can still change the
work: while planning, before the first act that builds. Asked at report time,
the narrow thing is already built.

THE MOMENT. A build act is one of: an Edit, Write or NotebookEdit; `helm work
claim`, `helm dispatch send` or `helm task add`; an Agent spawn (other than a
read-only type) or a Workflow; a Bash command that writes a file, unit or
script (`build_act`). Everything else, reads, chat reads and waits, never
reaches the transcript read below and never blocks.

FREE WHEN ANSWERED. The call passes when this turn's visible assistant text
already holds a line that begins `Wider goal:` (bullets and emphasis around
it are allowed), or when the seat is in an open meld for the task this call
builds: a meld is planning before building, so the meld is the plan. A seat
that answers unprompted pays one bounded tail read per turn and nothing else.
Thinking is not read: on the claude seats it is stored as a signature with
empty text, so a plan written only there is invisible to every reader.

ONCE PER TURN. Otherwise the call is refused ONCE with one line of at most
CAP characters (LINE), and the turn is latched, so the retry passes and so
does every later build act in the turn. The turn is the recorder's own edge:
`turn-opened-at` in the session's counters.json, written by `record.turn_open`
on each UserPromptSubmit. No turn edge (a session the recorder never saw) is
no block. The latch is written under the session dir's lock before the line
is printed, so two build calls made in parallel refuse one of them, never
both; an unwritable latch is no block (the stop guard's no-latch-no-block
law), because a refusal that cannot latch would refuse every retry.

WHO. A main-thread call of a session in helm's project, the scope the stop
guard runs in: a subagent (`agent_id` on the payload) builds what its seat
already planned, and a session in another project is not a helm seat.
HELM_NARROW_GOAL=0 (or off/no) turns the rung off.

COST. The transcript is never loaded whole: it is read backwards in CHUNK
blocks, at most TAIL_BYTES, line by line, and a line is parsed only when it
can matter (it names "wider goal", or it may be the prompt that began the
turn). FAIL-OPEN, TOTAL: any error is no block, because argv-guard runs on
every tool call and a guard that throws wedges the fleet.
"""
import json
import os
import re

SWITCH = "NARROW_GOAL"
CAP = 250
LATCH_FILE = "narrow-goal.turn"
TAIL_BYTES = 512 * 1024
CHUNK = 64 * 1024

LINE = ("[helm narrow-goal] Before you build: am I optimizing on too narrow a "
        "goal? Add a line 'Wider goal: <goal> · for: <what follows> · waits: "
        "<bottleneck> · exists: <prior art> · obvious: <expert's first "
        "question>', or meld a load-bearing plan; retry.")

#: The answer the rung looks for: a line that BEGINS with it, past any
#: bullet, quote or emphasis marker.
ANSWER = re.compile(r"(?mi)^[\s>*_\-+]*wider goal\s*:")

#: helm verbs that order or take work.
HELM_ACTS = frozenset((("work", "claim"), ("dispatch", "send"),
                       ("task", "add")))
#: Agent types whose tools cannot write: spawning one is a read.
READ_ONLY_AGENTS = frozenset(("Explore", "Plan", "claude-code-guide"))
#: Commands that write their destination operand(s).
WRITERS = frozenset(("tee", "cp", "mv", "install", "touch", "ln", "patch"))
#: A redirection on the code text (quotes, comments and heredoc bodies
#: masked): `>` or `>>`, not an fd duplication (`>&2`, `2>&1`), not a stderr
#: or both-stream log (`2>x`, `&>x`), not a process substitution `>(`.
_REDIRECT = re.compile(r"(?<![<>&\d=\-])>>?\|?(?![&(>])")
_TARGET = re.compile(r"\s*(\"[^\"]*\"|'[^']*'|[^\s;&|<>()]+)")
_TASK = re.compile(r"\btask[/-]([1-9][0-9]{0,8})\b")


def _off():
    from . import home
    return str(home.env(SWITCH) or "").lower() in ("0", "off", "no", "false")


def _scratch(target):
    """True for a destination that is not work: the null device, a temp or
    scratchpad path, an empty word."""
    t = str(target or "").strip("'\"")
    return (not t or t.startswith(("/dev/", "/tmp/", "/proc/"))
            or "scratchpad" in t or t.startswith(("$TMP", "${TMP")))


def _redirect_writes(cmd):
    """True when a command line redirects stdout into a file that is work."""
    if ">" not in cmd:
        return False
    from . import actsteer, chat
    code = actsteer._code(cmd)
    masked = chat._mask_quoted(code)[0]
    for m in _REDIRECT.finditer(masked):
        t = _TARGET.match(code, m.end())
        if t and not _scratch(t.group(1)):
            return True
    return False


def _argv_writes(argv):
    """The act name when one simple command writes or orders work."""
    prog = os.path.basename(argv[0])
    pos = [w for w in argv[1:] if not w.startswith("-")]
    if prog == "helm" and tuple(pos[:2]) in HELM_ACTS:
        return "helm " + " ".join(pos[:2])
    if prog in WRITERS:
        dest = pos if prog == "tee" else pos[-1:]
        if any(not _scratch(p) for p in dest):
            return prog
    if prog in ("sed", "perl") and any(
            w.startswith("-i") or (prog == "perl" and re.match(r"-\w*i", w))
            for w in argv[1:]):
        return prog + " -i"
    if prog == "git" and pos[:1] == ["apply"]:
        return "git apply"
    if prog == "systemctl" and pos[:1] and pos[0] in ("enable", "link", "edit"):
        return "systemctl " + pos[0]
    return None


def build_act(tool, tool_input):
    """The name of the build act this call performs, or None for a call that
    builds nothing."""
    tin = tool_input if isinstance(tool_input, dict) else {}
    if tool in ("Edit", "Write", "NotebookEdit"):
        return tool
    if tool == "Workflow":
        return tool
    if tool in ("Agent", "Task"):
        kind = str(tin.get("subagent_type") or "")
        return None if kind in READ_ONLY_AGENTS else "Agent"
    if tool != "Bash":
        return None
    cmd = str(tin.get("command") or "")
    if not cmd.strip():
        return None
    from . import record
    for argv in record._argvs(cmd):
        act = _argv_writes(argv)
        if act:
            return act
    return "a file write" if _redirect_writes(cmd) else None


def turn_key(session):
    """The open turn's identity (the recorder's `turn-opened-at`), or None
    when the recorder has no turn edge for this session."""
    from . import record
    opened = record.counters(session).get("turn-opened-at")
    return repr(float(opened)) if isinstance(opened, (int, float)) else None


def _reversed_lines(path, budget=TAIL_BYTES):
    """The file's lines, newest first, read in CHUNK blocks from the end and
    never more than `budget` bytes. A line cut by the budget is not given."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        floor = max(0, pos - budget)
        rest = b""
        while pos > floor:
            step = min(CHUNK, pos - floor)
            pos -= step
            f.seek(pos)
            block = f.read(step) + rest
            lines = block.split(b"\n")
            rest = lines[0]
            for ln in reversed(lines[1:]):
                yield ln
        if floor == 0 and rest:
            yield rest


def _prompt(d):
    """True for a record that began a turn: the person's (or a notice's)
    prompt, never a tool result, a meta record or a subagent's line."""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
        return False
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(c, dict) and c.get("type") == "tool_result"
        for c in content)


def answered(path):
    """True when this turn's visible assistant text holds a `Wider goal:`
    line. The turn is everything after the last prompt record, or the whole
    tail when the prompt lies beyond TAIL_BYTES."""
    if not path:
        return False
    for raw in _reversed_lines(path):
        low = raw.lower()
        goal = b"wider goal" in low
        if not goal and not (b'"type":"user"' in raw
                             and b"tool_result" not in raw):
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if _prompt(d):
            return False
        msg = d.get("message")
        if not goal or d.get("isSidechain") or not isinstance(msg, dict) \
                or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        texts = [content] if isinstance(content, str) else [
            c.get("text") for c in content or ()
            if isinstance(c, dict) and c.get("type") == "text"]
        if any(isinstance(t, str) and ANSWER.search(t) for t in texts):
            return True
    return False


def _lane_tasks(path):
    """{N} the lane worktree holding `path` records as its task, read from
    the repository's lane record (`taskkey.lane_records`)."""
    parts = os.path.abspath(str(path or "")).split(os.sep)
    for i, part in enumerate(parts[:-1]):
        if part.endswith("-wt") and parts[i + 1]:
            lane = parts[i + 1]
            from . import taskkey
            records, why = taskkey.lane_records(os.sep.join(parts[:i + 2]))
            if why:
                return set()
            return {int(m.group(1)) for v in records.get(lane, ())
                    for m in [_TASK.search(v)] if m}
    return set()


def meld_tasks(seat):
    """{N} for each OPEN meld this seat is in whose room is task N's room
    (`<scope>-<N>`, review_door's task room)."""
    from . import chat, pk, review_door
    from .seats_common import _seat_key
    suffix = ".meld.%s.json" % _seat_key(seat)
    d = chat.chat_dir()
    out = set()
    for name in os.listdir(d):
        if not name.endswith(suffix):
            continue
        st = pk.read_json(os.path.join(d, name), None)
        if not isinstance(st, dict) \
                or st.get("status") not in ("active", "invited") \
                or str(st.get("self") or "").casefold() != seat.casefold():
            continue
        room = str(st.get("room") or "")
        m = re.search(r"-([1-9][0-9]{0,8})\Z", room)
        if m and review_door._TASK_ROOM.fullmatch(room):
            out.add(int(m.group(1)))
    return out


def in_task_meld(payload):
    """True when this seat is in an open meld for the task this call builds:
    a task its text names (`task/N`), or the task recorded for the lane
    worktree the call writes in or runs from."""
    from .seats_common import own_name
    seat = own_name()
    if not seat:
        return False
    open_tasks = meld_tasks(seat)
    if not open_tasks:
        return False
    tin = payload.get("tool_input")
    tin = tin if isinstance(tin, dict) else {}
    text = " ".join(str(tin.get(k) or "") for k in (
        "command", "file_path", "notebook_path", "prompt", "description"))
    named = {int(m.group(1)) for m in _TASK.finditer(text)}
    if named & open_tasks:
        return True
    where = tin.get("file_path") or tin.get("notebook_path") \
        or payload.get("cwd") or ""
    return bool(_lane_tasks(where) & open_tasks)


def _outside_helm(cwd):
    """True when `cwd` is in a registered project that is not helm's."""
    from . import hooks
    from .inject._ledger import project_for_cwd
    mine = project_for_cwd(os.path.dirname(os.path.dirname(
        os.path.abspath(hooks.helm_bin()))))
    if mine is None:
        return False
    return project_for_cwd(cwd or os.getcwd()) != mine


def _latch(session, key):
    """Record that this turn has been asked. -> True when THIS call wrote the
    latch, False when the turn was already latched or no latch can be
    written."""
    from . import pk, record
    sd = record.session_dir(session)
    path = os.path.join(sd, LATCH_FILE)
    with record._counters_locked(sd) as held:
        if not held:
            return False
        try:
            with open(path) as f:
                if f.read().strip() == key:
                    return False
        except OSError:
            pass
        try:
            pk.atomic_write(path, key)
        except OSError:
            return False
    return True


def _latched(session, key):
    from . import record
    try:
        with open(os.path.join(record.session_dir(session), LATCH_FILE)) as f:
            return f.read().strip() == key
    except OSError:
        return False


def refusal(payload):
    """LINE when this call is the turn's first build act and the seat has
    not answered; None for every other call. Never raises."""
    try:
        d = payload if isinstance(payload, dict) else {}
        if _off() or d.get("agent_id"):
            return None
        if not build_act(d.get("tool_name"), d.get("tool_input")):
            return None
        session = str(d.get("session_id") or "")
        key = turn_key(session) if session else None
        if not key or _latched(session, key):
            return None
        if _outside_helm(d.get("cwd")):
            return None
        if answered(d.get("transcript_path")) or in_task_meld(d):
            _latch(session, key)
            return None
        return LINE if _latch(session, key) else None
    except Exception:                       # noqa: BLE001 — fail open
        return None
