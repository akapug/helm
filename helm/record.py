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
                      stuck-signal/-streak, loop-streak + cmd-hash-chain,
                      stalled-turns + the open turn (turn-open/-notice/
                      -calls/-forward; see turn_open)
  command-log.jsonl   verify-grounding: test-runner invocations with REAL exit
                      codes + record-time semantic identity/cwd (token + digest,
                      never the raw command line)
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
import contextlib
import json
import os
import re
import shlex
import sys
import time

from . import home, pk

HOOK_EVENT = "PostToolUse"
FAIL_EVENT = "PostToolUseFailure"    # failed tools (nonzero Bash included) land here
HOOK_EVENTS = (HOOK_EVENT, FAIL_EVENT)
PASSIVE = ("Read", "Grep", "Glob")   # error text here is DATA — never arms stuck
EDITS = ("Edit", "Write", "NotebookEdit")
# the todo-mirror tools (todos.TOOLS, spelled here so the hot path never
# imports todos.py for the 99% of events that are not todo writes)
TASK_TOOLS = ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskUpdateTODO")
# forward progress, by TOOL NAME: an edit, a spawn, and a write to the seat's
# own task list, which is durable work (task/2970); + a git commit and the
# coordination writes below, by the PARSED command. TodoWrite counts on
# purpose: rewriting the task list is planning work, like TaskCreate and
# TaskUpdate.
FORWARD = EDITS + ("Agent", "Task") + TASK_TOOLS
DIRTYING = EDITS + ("Bash",)         # the only tools worth a git-status probe

# COORDINATION WRITES ARE FORWARD OPS (task/2970). A lead's output is a filed
# task, a posted row, a revised store entry, a verdict, a claimed lease.
# Measured on a live lead: stalled-driver called it circling after turns that
# filed four tasks, posted to chat and revised the store, because every one of
# those Bash calls counted as passive. The table names WRITE subverbs only, so
# a read (show, list, get, read) earns nothing; None means every form of the
# verb writes. Matched on a PARSED command (_argvs), so one that only mentions
# a verb (echo, grep) is not credited. A heredoc BODY line that parses as one
# is credited; the cost is one missed stall, the silent direction.
COORD_WRITES = {
    "chat": ("post", "dm", "reply", "react", "claim"),
    "task": ("add", "claim", "update", "close", "comment", "standdown",
             "takeover"),
    "store": ("add", "revise", "supersede", "retire", "rescope", "keywords",
              "gates", "gloss", "evidence", "confirm", "reject", "xrev-clear",
              "demote"),
    "dispatch": ("send", "add", "verdict", "cancel", "rebind", "retip",
                 "hold", "release"),
    "reflex": ("add", "retire", "rescope"),
    "premise": None,
}
# git's global options that take their value as the NEXT word; every other
# leading dash-word is a flag. The subcommand is the first word after them.
_GIT_OPT_ARG = frozenset(("-c", "-C", "--git-dir", "--work-tree", "--namespace",
                          "--super-prefix", "--config-env"))
_ASSIGN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*=")
HASH_WINDOW = 8                      # loop-thrash lookback (catches A-B-A-B too)
LOG_MAX = 1024 * 1024                # command-log / edit-targets rotate here (-> .1)

# Conservative stuck tells (the ancestor's field-proven set): infra + auth +
# environment failures only — never generic "error", which rides ordinary output.
STUCK_RE = re.compile(
    r"API Error|rate.?limit|temporarily (?:limiting|unavailable)"
    r"|internal server error|server error|invalid_grant|token_revoked"
    r"|not authenticated|unauthorized|login required|permission denied"
    r"|command not found|No such file or directory", re.I)

# Test-runner shapes whose exit codes ground "validation really ran". Package
# script targets retain suffixes: test:unit and test:e2e are different gates.
# Selection/verbosity args remain one identity (pytest a/b, cargo --lib/--doc):
# this rung tracks the runner operation, not every selected subset.
_OP_SUFFIX = r"(?:[:._-][A-Za-z0-9][\w.-]*)?"
RUNNER_RE = re.compile(
    r"\b(cargo\s+nextest|cargo\s+test|just\s+(?:test|check)"
    + r"|pytest|go\s+test|vitest|jest|npm\s+(?:run\s+)?test" + _OP_SUFFIX
    + r"|pnpm\s+(?:run\s+)?test" + _OP_SUFFIX
    + r"|yarn\s+(?:run\s+)?test" + _OP_SUFFIX
    + r"|bats|tox|phpunit|rspec|bundle\s+exec\s+rspec|make\s+test"
    + r"|python3?(?:\s+\S+){0,4}?\s+-m\s+unittest)\b", re.I)
_DIRECT_RUNNERS = frozenset(
    ("pytest", "vitest", "jest", "bats", "tox", "phpunit", "rspec"))
_SHELL_BREAKS = frozenset(("&&", "||", ";", "|", "&"))


def _shell_parts(text):
    lex = shlex.shlex(str(text or ""), posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _script_identity(path, base, resolve):
    path = os.path.expanduser(path.lstrip(":"))
    if os.path.isabs(path):
        norm = os.path.realpath(path) if resolve else os.path.normpath(path)
        pole = "script:" if resolve else "script-abs:"
        return pole + _home_relative(norm)
    if base:
        joined = os.path.join(base, path)
        norm = os.path.realpath(joined) if resolve else os.path.normpath(joined)
        pole = "script:" if resolve else "script-abs:"
        return pole + _home_relative(norm)
    return "script-rel:" + os.path.normpath(path).lower()


def _script_invocation(command, cwd=None, resolve=None):
    """(display token, identity) for the first shell test script command."""
    resolve = bool(cwd) if resolve is None else bool(resolve)
    base = os.path.abspath(os.path.expanduser(str(cwd))) if cwd else None
    try:
        words = _shell_parts(command)
    except ValueError:
        # Recorder regexes historically clipped the opening quote around a
        # script token. It is still provable when stripping that lone edge
        # yields exactly one test-script path.
        text = str(command or "").strip().strip("'\"")
        words = [text] if text else []
    segments, segment = [], []
    for word in words:
        if word in _SHELL_BREAKS:
            segments.append((segment, word))
            segment = []
        else:
            segment.append(word)
    segments.append((segment, None))
    for parts, following in segments:
        if not parts:
            continue
        exe = os.path.basename(parts[0]).lower()
        if exe == "cd" and len(parts) > 1:
            if following in ("&&", ";"):
                path = os.path.expanduser(parts[1])
                base = os.path.normpath(
                    path if os.path.isabs(path) else os.path.join(base or "", path))
            continue
        display, path = parts[0], parts[0].lstrip(":")
        if exe in ("bash", "sh"):
            args = parts[1:]
            if args and args[0] == "-c":
                return _script_invocation(
                    args[1] if len(args) > 1 else "", base, resolve)
            while args and args[0].startswith("-"):
                args = args[1:]
            if not args:
                continue
            display, path = "%s %s" % (parts[0], args[0]), args[0]
        if path.lower().endswith((".sh", ".bash")) \
                and "test" in path.lower():
            return display, _script_identity(path, base, resolve)
    return None


def _python_runner(parts):
    """Interpreter+module identity, refusing script argv before `-m`."""
    try:
        at = len(parts) - 1 - list(reversed(parts)).index("-m")
    except ValueError:
        return None
    if at + 1 >= len(parts) or parts[at + 1].lower() != "unittest":
        return None
    start = max((i + 1 for i, p in enumerate(parts[:at])
                 if p in _SHELL_BREAKS), default=0)
    cmd = parts[start:at]
    if not cmd or os.path.basename(cmd[0]).lower() not in ("python", "python3"):
        return None
    i = 1
    while i < len(cmd):
        if cmd[i] in ("-W", "-X") and i + 1 < len(cmd):
            i += 2
        elif cmd[i].startswith("-"):
            i += 1
        else:
            return None
    return os.path.basename(cmd[0]).lower() + " -m unittest"


def runner_identity(token, cwd=None, resolve=None):
    """Stable semantic identity owned where argv and invocation cwd coexist."""
    raw = " ".join(str(token or "").split()).lower()
    script = _script_invocation(token, cwd, resolve)
    if script:
        return script[1]
    try:
        parts = shlex.split(str(token or ""))
    except ValueError:
        return "token:" + raw
    py = _python_runner(parts)
    if py:
        return "command:" + py
    if not parts:
        return "token:" + raw
    exe = os.path.basename(parts[0]).lower()
    if exe in _DIRECT_RUNNERS:
        return "command:" + exe
    low = [p.lower() for p in parts]
    if exe in ("cargo", "go", "just", "make") and len(low) > 1:
        return "command:" + " ".join(low[:2])
    if exe in ("npm", "pnpm", "yarn") and len(low) > 1:
        at = 2 if low[1] == "run" and len(low) > 2 else 1
        return "command:%s %s" % (exe, low[at])
    if low[:3] == ["bundle", "exec", "rspec"]:
        return "command:bundle exec rspec"
    return "token:" + raw


def runner_record(command, cwd=None):
    """(token, identity, aliases) from the full hook command, or None."""
    script = _script_invocation(command, cwd, resolve=True)
    if script:
        token, identity = script
        aliases = [identity]
        lexical = _script_invocation(command, cwd, resolve=False)
        if lexical:
            aliases.append(lexical[1])
        # Migration bridge for old relative rows whose schema had no cwd.
        if "cd " not in str(command).lower():
            aliases.append(runner_identity(token, resolve=False))
        return token, identity, list(dict.fromkeys(aliases))
    m = RUNNER_RE.search(str(command or ""))
    if not m:
        return None
    token = " ".join(m.group(1).split())
    identity = runner_identity(token, cwd, resolve=True)
    return token, identity, [identity]


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


_DELIM_START = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_'\"\\")
_WORD_END = frozenset(" \t\n;&|<>()")
_WORD_OPENERS = frozenset(" \t\n;&|(")


def _heredoc_word(text, i):
    """(delimiter, index past it) for the heredoc delimiter word at text[i],
    or None where no delimiter word starts there (`$((1<<2))` is a shift)."""
    if text[i:i + 1] not in _DELIM_START:
        return None
    out, quote = [], None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            else:
                out.append(ch)
        elif ch in "'\"":
            quote = ch
        elif ch == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 1
        elif ch in _WORD_END:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out), i


def _heredoc_at(text, i):
    """(delimiter, strip-tabs, index past the operator's word) when an
    unquoted `<<`/`<<-` at text[i] opens a heredoc, else None."""
    if not text.startswith("<<", i) or text.startswith("<<<", i):
        return None
    j = i + 2
    strip = text[j:j + 1] == "-"
    j += strip
    while text[j:j + 1] in (" ", "\t"):
        j += 1
    read = _heredoc_word(text, j)
    return (read[0], strip, read[1]) if read else None


def _skip_bodies(text, i, pending):
    """From the newline at text[i], skip each pending heredoc body in order,
    terminators included; returns the index of the newline that ends the
    last terminator line (or the end of the text)."""
    n = len(text)
    for tag, strip in pending:
        while i < n:
            eol = text.find("\n", i + 1)
            eol = n if eol < 0 else eol
            line = text[i + 1:eol].rstrip("\r")
            i = eol
            if (line.lstrip("\t") if strip else line) == tag:
                break
    return i


def _skip_subst(text, i):
    """Index just past the `)` that closes a `$(` whose body starts at
    text[i]. A substitution nests its own quoting — `"$(git log
    --format="%h")"` is ONE word — and may hold a heredoc whose body carries
    any quote or paren (`-m "$(cat <<'EOF' ... don't (x) ... EOF)"`), so
    both are tracked here. The end of the text when it never closes.

    A LOOP WITH ITS OWN STACK, not recursion: each nested `$(` pushes the
    enclosing frame, so a command nested thousands deep costs a list, never
    a RecursionError in the recorder."""
    n = len(text)
    stack = []                     # enclosing frames: (depth, quote, pending)
    depth, quote, pending, prev = 1, None, [], "("
    while i < n:
        ch = text[i]
        opens = False
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = None
            elif text.startswith("$(", i):
                opens = True
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif text.startswith("$(", i):
            opens = True
        elif ch == "#" and prev in _WORD_OPENERS:
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif ch == "<" and _heredoc_at(text, i):
            tag, strip, i = _heredoc_at(text, i)
            pending.append((tag, strip))
            continue
        elif ch == "\n" and pending:
            i = _skip_bodies(text, i, pending)
            pending = []
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if not depth:
                if not stack:
                    return i + 1
                depth, quote, pending = stack.pop()
                prev = ")"
                i += 1
                continue
        if opens:
            stack.append((depth, quote, pending))
            depth, quote, pending, prev = 1, None, [], "("
            i += 2
            continue
        prev = ch
        i += 1
    return n


def _simple_commands(text):
    """Each simple command of a whole Bash command text, as its words.

    ONE WALK OVER THE WHOLE TEXT, never a line at a time. A quoted argument
    may span lines — `git commit -q -m "subject<NL><NL>body"` is the fleet's
    ordinary commit, and `-m "$(cat <<'EOF' ... EOF)"` puts the whole body
    inside one double-quoted word — and a per-line read raised on the first
    line and credited nothing (measured on the fleet's own transcripts: 62
    of 165 real local commits credited; 165 of 165 by this walk).
    Unquoted `;`, `&`, `|` and newlines end a command; quotes are removed; a
    `$(...)` is one piece of its word, however it nests (_skip_subst); a `#`
    that starts a word comments to the end of its line; and a heredoc's BODY
    is skipped to its terminator, because it is data on stdin, not commands
    (`cat > notes <<'EOF'` naming `git commit` commits nothing).

    A quote that never closes makes the command it opened unreadable: that
    command is dropped, the ones before it stand. The silent direction — a
    missed credit, never a false one."""
    out, seg, word = [], [], []
    in_word, quote, pending = False, None, []
    i, n = 0, len(text)

    def end_word():
        if in_word:
            seg.append("".join(word))
        del word[:]

    while i < n:
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                word.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and text[i + 1:i + 2] in ('"', "\\", "$", "`"):
                word.append(text[i + 1])
                i += 2
            elif ch == '"':
                quote = None
                i += 1
            elif text.startswith("$(", i):
                j = _skip_subst(text, i + 2)
                word.append(text[i:j])
                i = j
            else:
                word.append(ch)
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            word.append(text[i + 1])
            in_word = True
            i += 2
            continue
        if ch in "'\"":
            quote, in_word = ch, True
            i += 1
            continue
        if text.startswith("$(", i):
            j = _skip_subst(text, i + 2)
            word.append(text[i:j])
            in_word = True
            i = j
            continue
        if ch == "#" and not in_word:
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "<" and _heredoc_at(text, i):
            tag, strip, i = _heredoc_at(text, i)
            end_word()
            in_word = False
            pending.append((tag, strip))
            continue
        if ch in " \t\n;&|":
            end_word()
            in_word = False
            if ch not in " \t":
                if seg:
                    out.append(seg)
                seg = []
            if ch == "\n" and pending:
                i = _skip_bodies(text, i, pending)
                pending = []
            i += 1
            continue
        word.append(ch)
        in_word = True
        i += 1
    if quote is None:
        end_word()
        if seg:
            out.append(seg)
    return out


# Launchers that run the command after them. Only the ones seen in front of a
# real `git commit` in the fleet's own transcripts, each with the options that
# take a value: `env -u GIT_AUTHOR_NAME ... git commit` is the helm seats'
# ordinary commit.
_LAUNCH_OPTS = {
    "env": frozenset(("-u", "--unset", "-C", "--chdir", "-S",
                      "--split-string")),
    "sudo": frozenset(("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U")),
    "nice": frozenset(("-n", "--adjustment")),
    "timeout": frozenset(("-s", "--signal", "-k", "--kill-after")),
    "command": frozenset(), "exec": frozenset(), "nohup": frozenset(),
    "time": frozenset(),
}


def _unwrap(words):
    """`words` with any leading launchers (env, sudo, nice, timeout, time,
    command, exec, nohup), their options and env's NAME=value pairs removed.
    """
    # NAMED `words`, NOT `argv`: this reads a command the seat ran, and
    # the surface-wiring census treats a lookup keyed on argv[0] as helm's
    # own CLI accepting a subverb.
    while words and os.path.basename(words[0]) in _LAUNCH_OPTS:
        tool = os.path.basename(words[0])
        takes = _LAUNCH_OPTS[tool]
        i = 1
        while i < len(words):
            w = words[i]
            if w in takes:
                i += 2
            elif w.startswith("-") and w != "-":
                i += 1
            elif tool == "env" and _ASSIGN.match(w):
                i += 1
            else:
                break
        if tool == "timeout" and i < len(words):
            i += 1                          # the duration
        words = words[i:]
    return words


def _argvs(command):
    """Each simple command in a Bash command line, as its argv.

    The whole text is read at once (_simple_commands), after bash's own
    backslash-newline join; leading NAME=value assignments, a subshell's `(`
    and any launcher in front (_unwrap) are dropped. Text that is not a
    command — a quoted argument, a heredoc body, a comment — is never
    credited as one."""
    text = str(command or "").replace("\\\n", "")
    for words in _simple_commands(text):
        seg = []
        for w in words:
            if seg or not _ASSIGN.match(w):
                w = w if seg else w.lstrip("(")
                if w:
                    seg.append(w)
        seg = _unwrap(seg)
        if seg:
            yield seg


def _git_subcommand(argv):
    """The git subcommand of one argv, or None when it is not a git call."""
    if not argv or os.path.basename(argv[0]) != "git":
        return None
    i = 1
    while i < len(argv):
        if argv[i] in _GIT_OPT_ARG:
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return argv[i]
    return None


def git_commit(command):
    """True when a Bash command line RUNS `git commit`: the subcommand after
    git's global options (`git -c k=v commit`; `--amend` counts, `--dry-run`
    does not), never the word anywhere in the text (`git grep -n commit` is a
    read, and `echo "git commit"` or a heredoc body naming it runs nothing).
    The substring gate keeps every other Bash call off the walk."""
    if "commit" not in str(command or ""):
        return False
    # `--dry-run` reports what a commit WOULD do and commits nothing
    return any(_git_subcommand(a) == "commit" and "--dry-run" not in a
               for a in _argvs(command))


def coordination_write(command):
    """True when a Bash command runs a helm WRITE verb (COORD_WRITES)."""
    if "helm" not in str(command or ""):
        return False
    for argv in _argvs(command):
        if len(argv) < 2 or os.path.basename(argv[0]) != "helm":
            continue
        subs = COORD_WRITES.get(argv[1], ())
        if subs is None or (len(argv) > 2 and argv[2] in subs):
            return True
    return False


def _sidechain(event):
    """True when a subagent made this call. The harness sets agent_id on a
    subagent's hook payloads and never on the main thread's (the rule and its
    source: actors.SIDECHAIN_RULE); a subagent shares its seat's session_id,
    so this is the only field that tells its calls from the seat's."""
    agent = event.get("agent_id")
    return isinstance(agent, str) and bool(agent)


# ---------------------------------------------------------------------------
# THE TURN (task/2970). stalled-driver says "6 turns with no forward op", and
# it read passive-streak, which bumps on every non-forward CALL: six reads in
# one reviewing turn read as six stalled turns. Measured: 191 fires in 41h,
# 156 of them on turns a chat wake or a task notice began.
#
# `stalled-turns` counts what the sentence says. The UserPromptSubmit hook
# (inject.gather) calls turn_open at each turn's start, which CLOSES the turn
# before it:
#   * a forward op anywhere in it (turn-forward) -> the count is 0;
#   * a notice began it (turn-notice: a chat wake, a finished task) -> the
#     count stands — the harness chose that turn, the seat did not;
#   * the seat made no call in it (turn-calls 0) -> the count stands — a
#     turn spent answering is conversation, not circling;
#   * otherwise -> the count grows by one.
# A subagent's calls are not its seat's turn (they share the session id), so
# they never raise turn-calls; its forward ops still credit the turn, since
# delegated work landing is the seat's work moving.
#
# THE RECORDER'S EVIDENCE GATES IT. turn_open writes only where counters.json
# already exists, and closes only a turn it opened, so a session no recorder
# has seen gets no state from a prompt, and the turns before a session's first
# call are not counted: the silent direction, and the recorder census
# (`helm record status`) still reads only what the PostToolUse hook wrote.
# ---------------------------------------------------------------------------

def _closed(c):
    """stalled-turns once the open turn in `c` closes."""
    n = int(c.get("stalled-turns") or 0)
    if not c.get("turn-open"):
        return n                        # no turn this hook opened
    if c.get("turn-forward"):
        return 0
    if c.get("turn-notice") or not int(c.get("turn-calls") or 0):
        return n
    return n + 1


# ONE WRITER AT A TIME (task/2970). turn_open (the per-turn hook) and _record
# (every tool call, a subagent's included, and parallel calls race each other)
# each read, change and rewrite counters.json. Unserialized, a prompt that
# lands while a subagent's edit is being recorded erases that edit's credit
# and adds a stalled turn that never happened, or the edit's stale copy erases
# the turn boundary. So both hold the session dir's lock across read-to-write:
# the directory itself, flock'd (actors._store_lock's idiom), so taking it
# creates no file. The wait is BOUNDED because both writers are hooks: past
# it the write is skipped — one lost count, never a hung turn and never an
# unserialized write — and SAYS SO: a skip leaves the swallow breadcrumb
# (`helm record swallows`), and a timeout also marks the hook's latency span.
COUNTERS_LOCK_WAIT_S = 1.0


@contextlib.contextmanager
def _counters_locked(sd):
    """Yield True holding the session dir's lock, False when it cannot be
    had in COUNTERS_LOCK_WAIT_S or cannot be taken at all."""
    fd = None
    try:
        import fcntl
        os.makedirs(sd, exist_ok=True)
        fd = os.open(sd, os.O_RDONLY | os.O_DIRECTORY)
        end = time.monotonic() + COUNTERS_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    from . import hooklatency
                    hooklatency.mark("timeout")
                    raise TimeoutError("counters lock not had in %.1fs"
                                       % COUNTERS_LOCK_WAIT_S)
                time.sleep(0.005)
    except OSError as exc:
        swallow("record._counters_locked", exc)
        if fd is not None:
            os.close(fd)
            fd = None
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)                # closing the fd releases the lock


def turn_open(session, prompt):
    """Close the session's open turn and open this one. Never raises and
    never prints: it runs first in the per-turn hook."""
    try:
        sid = str(session or "")
        if not sid:
            return
        sd = session_dir(sid)
        path = os.path.join(sd, "counters.json")
        if not os.path.exists(path):
            return                      # no recorder evidence: write nothing
        from . import promptshape
        notice = int(promptshape.is_notice(prompt))
        with _counters_locked(sd) as held:
            c = pk.read_json(path, None) if held else None
            if not isinstance(c, dict):
                return
            c["stalled-turns"] = _closed(c)
            c.update({"turn-open": 1, "turn-calls": 0, "turn-forward": 0,
                      "turn-notice": notice})
            pk.write_json(path, c)
    except Exception as exc:
        swallow("record.turn_open", exc)


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


def _git_root(wd):
    """The TOP-LEVEL of the working tree `wd` sits in, or None.

    A worktree and its shared checkout answer with DIFFERENT roots, which is
    the whole point: it is what lets a reader ask "was that observation of the
    tree I am about to talk about?" rather than assuming. Best-effort by
    construction -- an unreadable answer is None and a reader treats it as
    UNKNOWN, never as a match."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", wd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def observed_here(root, here):
    """Was a dirty observation recorded in `root` taken in the tree at `here`?

    True also when the answer is UNKNOWN -- an unrecorded root (the pre-field
    state) or an unreadable one is not evidence of a MISMATCH, and a reader
    that treated it as one would silence every legacy observation and take its
    rung out of service for the case it exists to catch.

    It lives beside `_git_root` because it is that resolver's only question. A
    caller spelling "which tree is this" a second time drifts from the spelling
    that RECORDED the flag, and then the comparison answers something other
    than what it asks."""
    if not root:
        return True
    mine = _git_root(here or ".")
    if not mine:
        return True
    return os.path.realpath(root) == os.path.realpath(mine)


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
        from . import hooklatency
        hooklatency.mark("exception")


def swallow_log():
    """The one swallow breadcrumb file. GLOBAL, not session-keyed: a swallowed
    exception is a fact about CODE, and the question it answers ("does this
    handler ever fire, and with what?") is asked across sessions, not inside
    one."""
    return os.path.join(home.global_dir(), "swallowed.jsonl")


def unwritten_state(exc):
    """Does `exc` say a per-session state file HAS NOT BEEN WRITTEN YET?

    THE ONE DISCRIMINATION THE SWALLOW LEDGER CANNOT MAKE FOR ITSELF, and it
    has to live at the call site rather than inside `swallow`. A missing file
    is a NORMAL NEGATIVE at a reader polling per-session state that a fresh
    seat has not produced yet; the same FileNotFoundError at a site that
    opens a checked-in file, a configured binary or a ledger helm itself
    wrote is a DEFECT and must still be recorded. Only the caller knows
    which of the two it is standing in, so this is a predicate the caller
    applies, never a filter `swallow` applies to everybody.

    WHY IT IS NARROW ON PURPOSE. It answers True for ENOENT alone, which is
    exactly "nothing is there". It answers False for a path that exists and
    will not open — a permission, a truncated or corrupt read, a directory
    where a file belongs, and every non-OSError. Those are the states the
    ledger exists for, and the cost of this predicate being too WIDE is a
    real eaten error that nobody ever sees again, while the cost of it being
    too NARROW is one noisy row. The narrow direction is the safe one.

    ABSENT AND UNREADABLE MUST NOT SHARE A VALUE. They shared one here, and
    the measurement of what that cost is the reason this exists: the live
    ledger reached 3,976 records of which 3,976 were this exact expected
    absence from two polling readers, with a rotated 4,379 behind it in the
    same shape. Not a signal buried in noise — NO SIGNAL LEFT. A fleet-wide
    defect instrument that is 100 percent noise has stopped being an
    instrument, so the loss is the whole channel rather than some tolerable
    fraction of it.

    THIS CHANGES NO CONTROL FLOW. The caller still swallows, still returns
    its silent fail-closed answer, still refuses to raise. The only thing it
    decides is whether a breadcrumb is worth leaving.
    """
    return isinstance(exc, FileNotFoundError)


def swallow_unless_unwritten(where, exc):
    """`swallow`, but silent for a state file that was never written.

    THE CALL SITE STILL OWNS THE JUDGEMENT. This is deliberately not a filter
    inside `swallow` — a reader that POLLS for per-session state a fresh seat
    has not produced yet calls this one, and every other site keeps calling
    `swallow`, where a missing file is news. The name is the declaration:
    picking this verb is a site saying "absence is an ordinary state here".

    Returns None always and RAISES NEVER — same contract as `swallow`, for
    the same reason: it is called from inside an except handler.
    """
    if not unwritten_state(exc):
        swallow(where, exc)


def swallow(where, exc):
    """Leave a trace that a broad handler ate `exc` at `where`. Returns None
    always and RAISES NEVER.

    WHY THIS EXISTS. helm's stop-guard and work-offer rungs are deliberately
    FAIL-CLOSED: any state or ledger trouble yields silence, never a raise,
    never a louder lane — a whisper that crashes a seat's stop is worse than
    one that goes quiet, and that policy is correct. But the swallows left NO
    TRACE, so a genuine programming error and a legitimately absent ledger row
    rendered identically: the rung simply had nothing to say. Measured
    2026-08-11: a 2-tuple returned against a 3-tuple unpack raised ValueError
    inside _spiral_gate, the handler ate it, the spiral BLOCK came back EMPTY,
    and a fleet guard was disarmed for twenty minutes with a green suite and
    no record anywhere. Census that day: 20 broad handlers across the two
    modules, 20 of them with no call of any kind in the handler body.

    THIS DOES NOT CHANGE CONTROL FLOW. The caller still swallows, still returns
    its silent answer, still refuses to raise. The only difference is that
    afterwards somebody can ask what was eaten.

    IT MUST NOT RAISE, and that is not defensive habit — it is the whole
    contract. This function is called from INSIDE an except handler, so an
    exception here would convert a silently-swallowed bug into a crash on the
    stop path: strictly worse than the disease it diagnoses. Hence the bare
    guard, mirroring `record` above.

    NARROW HANDLERS ARE DELIBERATELY NOT WIRED. A try around one int() parse or
    one dict lookup is a precise, intentional guard whose silence carries no
    information loss; instrumenting those would bury the nine that can hide a
    programming error under eleven that cannot.
    """
    try:
        _append(swallow_log(), json.dumps({
            "ts": int(time.time()),
            "where": pk.cut_marked(where, 120),
            "exc": type(exc).__name__,
            # BOUNDED AND SAYING SO. The type is what discriminates — a
            # ValueError or TypeError here is almost always OUR bug, while an
            # OSError or KeyError is usually the absent-state case the handler
            # was written for — but the message is the EVIDENCE, and this
            # breadcrumb is the only copy of it anywhere. A prefix that reads
            # as the whole exception is how a diagnosis goes wrong on the one
            # file written precisely because nothing else was recorded.
            "msg": pk.cut_marked(exc, 200),
        }, sort_keys=True) + "\n")
    except Exception:
        pass


def swallows(limit=50):
    """The read API: most recent breadcrumbs first, [] when there are none.

    Returns rows rather than printing, so the operator surface and any test
    read the SAME thing — a renderer that reparses its own output is how a
    count and a list drift apart.
    """
    rows = []
    try:
        with open(swallow_log(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return list(reversed(rows))[:max(0, int(limit))]


def _record(event):
    sid = str(event.get("session_id") or "")
    tool = str(event.get("tool_name") or event.get("tool") or "")
    if not (sid and tool):
        return  # nothing to key on — state keyed wrong is worse than absent
    tin = event.get("tool_input")
    tin = tin if isinstance(tin, dict) else {}
    cmd = str(tin.get("command") or "")
    event_wd = str(tin.get("workdir") or tin.get("cwd")
                   or event.get("cwd") or "")
    resp = event.get("tool_response") if "tool_response" in event \
        else event.get("tool_result")
    failed = str(event.get("hook_event_name") or "") == FAIL_EVENT
    sd = session_dir(sid)

    # a FAILED tool made no progress: no forward credit, no commit credit
    is_commit = tool == "Bash" and not failed and git_commit(cmd)
    forward = (tool in FORWARD and not failed) or is_commit \
        or (tool == "Bash" and not failed and coordination_write(cmd))

    # dirty-streak's git probe runs ONLY on dirtying tools, in the tool's
    # workdir, and BEFORE the counters lock: it may take seconds, and the
    # per-turn hook must not wait on it. None = the probe failed.
    #
    # THE MAIN THREAD'S CALLS ONLY (task/2980 lane 5). uncommitted-drift
    # tells the SEAT to checkpoint, and 104 of its 119 measured streaks were
    # built by subagents, which hand back uncommitted work by design and
    # share the seat's session id — so their dirt read as the seat's own.
    # A subagent's call neither probes nor moves the streak or the cached
    # last-dirty, which would otherwise carry a lane's tree into the seat's.
    sidechain = _sidechain(event)
    probed = root = None
    if tool in DIRTYING and not sidechain:
        from . import seats
        wd = event_wd or seats.safe_cwd() or ""  # deleted cwd: probe fails
        # None -> cached last-dirty, instead of an eager-getcwd hook crash
        probed = _git_dirty(wd)
        if probed is not None:
            # AND WHERE IT WAS TAKEN. The flag alone is a bare boolean about
            # SOME tree: this probe runs in the tool's own workdir, which on a
            # seat holding lane rooms is routinely NOT the shared checkout. A
            # reader that cannot tell those apart attributes a lane's dirt to
            # the shared tree and prescribes over it -- measured, with the
            # shared `git diff --stat HEAD` empty at the time. Recording the
            # tree costs one string and is what makes the flag answerable.
            root = _git_root(wd) or ""

    # stuck-signal: action tools only — reads carry error text as data. A
    # failure event carries its tells in the top-level error, not a response.
    rtext = resp if isinstance(resp, str) else \
        json.dumps(resp) if resp is not None else ""
    if failed:
        rtext = "\n".join(x for x in (rtext, str(event.get("error") or "")) if x)
    stuck = tool not in PASSIVE and bool(rtext) and bool(STUCK_RE.search(rtext))

    # command-log: test-runner invocations with the REAL exit code — token +
    # digest, never the raw command line (ids/digests law).
    if tool == "Bash" and cmd:
        runner = runner_record(cmd, event_wd)
        if runner:
            import hashlib
            tok, identity, aliases = runner
            tok = " ".join(tok.split()).lower()[:80]
            row = {"token": tok, "identity": identity, "aliases": aliases,
                   "exit": _event_exit(event, resp, failed),
                   "ts": int(time.time()),
                   "digest": hashlib.sha1(cmd.encode()).hexdigest()[:12]}
            if event_wd:
                row["cwd"] = _home_relative(event_wd)
            _append(os.path.join(sd, "command-log.jsonl"),
                    json.dumps(row, separators=(",", ":")) + "\n")

    # edit-targets: the file a real edit landed on (basename only) — a
    # FAILED edit landed nowhere and must not ground verify.
    #
    # AND edit-paths: the same edit's DIRECTORY-BEARING path, home-relative.
    # The basename alone cannot answer the question that matters at a tool
    # boundary — WHERE did this write go. Live 2026-07-26: an integrator wrote
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

    # THE COUNTERS, read-to-write under the session dir's lock (see
    # _counters_locked): turn_open and every other call write this file too.
    with _counters_locked(sd) as held:
        if held:
            path = os.path.join(sd, "counters.json")
            c = pk.read_json(path, {}) or {}
            c["passive-streak"] = 0 if forward \
                else int(c.get("passive-streak") or 0) + 1

            # the open turn (turn_open): the seat's own calls count toward
            # it; any forward op credits it and clears the stall count at once
            if not sidechain:
                c["turn-calls"] = int(c.get("turn-calls") or 0) + 1
            if forward:
                c["turn-forward"] = 1
                c["stalled-turns"] = 0

            # dirty-streak: everything but a dirtying tool reads the cached
            # last-dirty, and so does a probe that FAILED — unknown never
            # masquerades as clean. A subagent's call leaves all three alone.
            if not sidechain:
                if probed is None:
                    dirty = bool(c.get("last-dirty"))
                else:
                    dirty = probed
                    c["last-dirty"] = int(probed)
                    c["last-dirty-root"] = root
                c["dirty-streak"] = 0 if (is_commit or not dirty) \
                    else int(c.get("dirty-streak") or 0) + 1

            # loop-thrash: a bounded chain of recent command hashes; a re-run
            # of a command still in the window grows the streak (A-A-A and
            # A-B-A-B alike).
            if tool == "Bash" and cmd:
                import hashlib
                h = hashlib.sha1(" ".join(cmd.split()).encode()).hexdigest()[:12]
                chain = [x for x in (c.get("cmd-hash-chain") or [])
                         if isinstance(x, str)]
                c["loop-streak"] = int(c.get("loop-streak") or 0) + 1 \
                    if h in chain else 0
                c["cmd-hash-chain"] = (chain + [h])[-HASH_WINDOW:]

            if tool not in PASSIVE:
                c["stuck-signal"] = int(stuck)
                c["stuck-streak"] = int(c.get("stuck-streak") or 0) + 1 \
                    if stuck else 0

            c.update(v=1, ts=pk.now_ts())
            c["last-tool"] = tool
            pk.write_json(path, c)

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
    from . import posttool
    owned = posttool.recognize(cmd)
    return (bool(owned and owned[0] == "posttool")
            or "record --hook-json" in cmd or "helm record" in cmd)


# The specs record OWNS. Declared here, beside hook_command, because record
# installs its own hook and must stay the authority on what that hook is — but
# DECLARED as a table so the one-spawn dispatcher (helm/hookrun.py) can see it.
# Before this existed, record was invisible to hooks.SPECS, which is why the
# highest-traffic hook in the fleet — every PostToolUse, 11851 calls on the
# measured day — could not be merged with its own event's siblings.
# INSTALL OWNERSHIP IS UNCHANGED: nothing here installs anything; _cmd_install
# still writes the hook exactly as it always did.
HOOK_SPECS = (
    {"name": "record", "event": HOOK_EVENT, "args": "record --hook-json",
     "timeout": 5, "own": ("record --hook-json", "helm record"),
     "matcher": None},
    {"name": "record-failure", "event": FAIL_EVENT,
     "args": "record --hook-json", "timeout": 5,
     "own": ("record --hook-json", "helm record"), "matcher": None},
)


def deployed_spec(event=HOOK_EVENT):
    """THE record spec AS DEPLOYED — one place, so no caller re-derives it.

    HOOK_SPECS declares timeout 5 for the in-process dispatcher's SIGALRM while
    the shell hook has always been generated at hooks.TIMEOUT_S (10). Both
    numbers are real and they are not the same number, which is precisely how
    the outer deadline went missing: `_canonical_entry(HOOK_SPECS[0])` computes
    a grace for an inner budget of 5 that no installed hook uses, and the
    installed hook — inner 10 — got none at all. Any caller that needs the
    DEPLOYED shape asks here and gets the 10.

    The 10s inner budget does not move; reconciling it with the dispatcher's 5
    is its own question and is not settled by this function."""
    from . import hooks
    base = next(sp for sp in HOOK_SPECS if sp["event"] == event)
    return dict(base, timeout=hooks.TIMEOUT_S)


def outer_deadline(event=HOOK_EVENT):
    """The harness deadline the installed record entry must carry (inner + 5).

    An alarm the outer runner kills before it can speak is dead code, and the
    recorder is the hook that fires on EVERY tool call."""
    from . import hooks
    return hooks._gate_outer_timeout(deployed_spec(event))


def hook_command(event=HOOK_EVENT):
    """The record hook's generated text — `hooks.spec_command` renders it.

    This used to hand-build `timeout N <abs bin/helm> record --hook-json ||
    true`, a second copy of the one-line advisory template. That copy is why
    this verb needed a separate fix when the advisory branch learned to
    ANNOUNCE a timed-out or missing helm: a hand-kept duplicate does not
    inherit its original's cures, and record is the hook that fires on every
    single tool call. One generator, one shape, one place to fix.
    """
    from . import hooks

    # THE TIMEOUT IS DELIBERATELY NOT HOOK_SPECS[0]'s. The shell hook has
    # always been generated at `hooks.TIMEOUT_S` (10s) while HOOK_SPECS
    # declares 5 for the in-process dispatcher's SIGALRM — the two paths have
    # disagreed for as long as both existed. Delegating naively HALVED the
    # budget of the hook that fires on EVERY tool call, which is a behaviour
    # change this commit has no business making; reconciling the two is its
    # own question. The spec copy keeps the deployed number visible instead of
    # moving it by accident.
    return hooks.spec_command(deployed_spec(event))


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
    """The program this hook command actually runs resolves to a real file.

    DELEGATED, NOT A SECOND COPY — the same rule this module already follows
    for `_fail_open` one function down. NOTHING WAS BROKEN BEFORE: the old scan
    took the word before `record` as the executable, and for every rendering
    helm had ever installed that word WAS the helm path, so it answered
    correctly. What moved is the command shape. `helm-hook lane record
    PostToolUse …` carries the spec NAME as an operand, so the word before the
    first `record` is the literal `lane` — a scan built on adjacency cannot
    survive a new prelude, and this module had its own copy of that scan, which
    is the reason a change one module over could reach it at all.
    `hooks._executed` is the single walk that knows every prelude, the wrapper
    included, so the next prelude is one row there rather than two scans to
    find."""
    from . import hooks
    return hooks._resolvable(cmd)


def _merge_hook(settings, cmd, event=HOOK_EVENT):
    """-> (merged_copy, ok|add|update). MERGE-preserving (hooks.py law): only
    OUR <event> entry is written; foreign hooks — the UserPromptSubmit
    inject entry included — and every other key survive byte-identical.

    EVERY entry of ours in the event is visited, not the first. This carried
    the same return-on-first defect hooks._merge_event did (see its docstring
    for the incident): a second recorder entry in one event was repaired by
    nobody while the harness ran it, so re-installing — the standard cure for a
    stale path — could never reach it."""
    from . import hooks, posttool
    if event == HOOK_EVENT:
        if cmd != hooks.spec_command(deployed_spec(event)):
            raise ValueError("custom recorder command cannot use installed pair planning")
        out, remaining, actions = posttool.prepare(
            settings, (deployed_spec(event),), executable=hooks.helm_bin())
        if not remaining:
            return out, actions["record"]
    else:
        out = json.loads(json.dumps(settings))
    hks = out.setdefault("hooks", {})
    if not isinstance(hks, dict):
        raise ValueError("existing 'hooks' key is not an object — fix it by hand")
    groups = hks.setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError("existing hooks.%s is not a list — fix it by hand" % event)
    mine = [h for g in groups if isinstance(g, dict)
            for h in (g.get("hooks") or [])
            if isinstance(h, dict) and _ours(str(h.get("command") or ""))
            and not (event == HOOK_EVENT and h.get("command") != cmd
                     and posttool.protected(h.get("command")))]
    if not mine:
        # no matcher: the recorder wants EVERY tool event
        groups.append({"hooks": [{"type": "command", "command": cmd,
                                  "timeout": outer_deadline(event)}]})
    action = "ok" if mine else "add"
    outer = outer_deadline(event)
    for h in mine:
        # THE DEADLINE IS PART OF THE ENTRY, not decoration. This wrote only
        # {type, command}, so every installed recorder leg carried NO outer
        # timeout and its rc-124 alarm could be killed by the harness default
        # before it spoke — on the hook that fires on every tool call. A
        # missing or stale deadline is an UPDATE, so re-installing (the
        # standard cure) actually reaches an entry written before this rail.
        if (h.get("command") != cmd or h.get("type") != "command"
                or h.get("timeout") != outer):
            h["command"] = cmd
            h["type"] = "command"
            h["timeout"] = outer
            action = "update"
    if event == HOOK_EVENT:
        converted, _remaining, _covered = posttool.prepare(
            out, (deployed_spec(event),), executable=hooks.helm_bin())
        if converted != out:
            return converted, "update"
    return out, action


def install_home(path, dry=False):
    """Install/refresh both recorder legs through bounded content-revision CAS."""
    import difflib
    from . import configs, posttool
    sp = os.path.join(path, "settings.json")

    def merge(cur):
        """The WHOLE merge, used by transform AND verify. The room repair
        mutates the candidate, so a step applied in only one of them would make
        every write fail its own verification."""
        from . import hooks
        out, actions = cur, []
        for ev in HOOK_EVENTS:
            out, action = _merge_hook(out, hook_command(ev), ev)
            actions.append(action)
        # The recorder owns this separate installer, including the failure
        # event's alarm envelope. Share the persistence rail explicitly;
        # `_ours` scopes it to recorder wiring and the exact installed pair.
        return out, actions, hooks.repair_lane_room_commands(out, sp, owns=_ours)

    def transform(cur):
        merged, actions, notes = merge(cur)
        return merged, {"actions": actions, "rooms_notes": tuple(notes),
                        "posttool_refusals": posttool.refusals(merged)}

    def verify(candidate, before, _metadata):
        from . import hooks
        expected, _actions, _notes = merge(before)
        return candidate == expected and all(
            _leg_live(candidate, ev, all_tools=False) for ev in HOOK_EVENTS) \
            and hooks._rooms_clean(candidate, owns=_ours)

    res = configs.transform_json_file(sp, transform, verify=verify, dry_run=dry)
    if not res.get("ok"):
        return "fail", res["error"]
    from . import hooks
    meta = res.get("metadata") or {}
    actions = [action for committed in hooks._install_metadata(res)
               for action in committed.get("actions") or []]
    rooms = "; ".join(filter(None, (
        hooks.lane_room_report(meta.get("rooms_notes") or ()),
        posttool.refusal_detail(path, meta.get("posttool_refusals") or ()))))
    # `ok` IS RESERVED FOR "I WROTE NOTHING" — the same law as hooks.install_home
    # (see the incident in its return site). A room repair rewrites bytes with
    # every recorder leg already current, so it must NOT report `ok`; the third
    # arm below exists for exactly that case and is not a spurious update.
    action = "add" if "add" in actions else \
        "update" if "update" in actions else \
        "update" if res.get("wrote") or res.get("action") == "dry" else "ok"
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


def _leg_live(settings, event, all_tools=True):
    """One recorder leg counts as live ONLY on its complete executable
    contract: command, type AND the outer deadline.

    `_event_cmds` yields command STRINGS, so type and timeout are gone before
    any caller can look at them. Status therefore reported a home fully covered
    while its installed deadline had been deleted or drifted AFTER install —
    the rc-124 alarm dead again, and nothing saying so. hooks._lane_live has
    held the same contract for the lane specs; record owns a separate
    installer and status, so it needs its own, and this is deliberately its
    shape rather than a call into it. Status requires all-tool coverage;
    the standalone install verifier uses all_tools=False to retain the legacy
    requested recorder repair under an existing tool-specific matcher.
    """
    from . import hooks, posttool
    if event == HOOK_EVENT and "record" in posttool.covered_members(
            settings, executable=hooks.helm_bin()):
        return True
    hks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hks.get(event) if isinstance(hks, dict) else None
    want_cmd, want_outer = hook_command(event), outer_deadline(event)
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict) or (all_tools and g.get("matcher") not in (None, "*")):
            continue
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and h.get("command") == want_cmd \
                    and h.get("type") == "command" \
                    and h.get("timeout") == want_outer:
                return True
    return False


def status_rows():
    """Per-claude-home recorder coverage, read-only (hooks.status_rows shape).
    hook=True demands BOTH event legs on their COMPLETE contract — command,
    type and outer deadline. Success-only wiring, and a leg whose deadline has
    been deleted since install, are both gaps the installer closes."""
    from . import hooks, posttool
    rows = []
    for name, path in hooks.claude_homes():
        cmd, s = None, {}
        try:
            with pk.open_regular(os.path.join(path, "settings.json"),
                                 encoding="utf-8") as f:
                s = json.load(f)
            per = [next((c for c in _event_cmds(s, ev) if _ours(c)
                         and not (ev == HOOK_EVENT and posttool.protected(c))), None)
                   for ev in HOOK_EVENTS]
            # BOTH legs, complete contract. `all(per)` alone was a claim about
            # command strings only, and a deadline deleted after install left
            # it reporting covered over a dead alarm.
            live = all(_leg_live(s, ev) for ev in HOOK_EVENTS)
            cmd = per[0] if (all(per) and live) else None
        except (OSError, ValueError):
            pass
        rows.append({"home": name, "path": path, "hook": bool(cmd), "command": cmd,
                     "posttool_refusals": posttool.refusals(s),
                     "refusal_detail": posttool.refusal_detail(path, posttool.refusals(s)),
                     "resolvable": bool(cmd) and _resolvable(cmd),
                     # hooks._fail_open, never a THIRD inline copy of the
                     # idiom. This tested for the bare fail-open tail — true
                     # while that tail was the only way to spell it, and a
                     # false alarm the moment hook_command started generating
                     # the rc-case: every home would have counted as NOT
                     # fail-open and coverage() would report 0 of N.
                     "fail_open": bool(cmd) and hooks._fail_open(cmd),
                     # THE LADDER EVERY ONE OF THOSE COMMANDS RUNS, measured by
                     # hooks' own function rather than a fourth copy of the
                     # rule. A home can carry both legs on their complete
                     # contract, resolve helm and satisfy fail-open — and still
                     # name a `helm-hook` that is not on disk, in which case
                     # every PostToolUse recorder exits 127 before a line of it
                     # runs and the harness reads ALLOW. This census printed
                     # `[OK] record coverage: 2 of 2 claude homes` over exactly
                     # that estate while the hooks census beside it correctly
                     # read 0 of 2 — one fact, two readers, one of them blind.
                     # `None` (a rendering that names no wrapper at all) is not
                     # a gap: that vintage never needed the file.
                     "wrapper": hooks._wrapper_state(hooks._all_hook_cmds(s))})
    return rows


def _ladder_rows(rows):
    """`status_rows` in the shape `hooks.wrapper_gone_message` reads.

    The MEASUREMENT is hooks' (`_wrapper_state`, called in `status_rows`) and
    the SENTENCE is hooks' (`WRAPPER_GONE_MSG`); the two censuses only spell
    the row's name key differently — `home` here, `label` there — so this
    renames and does nothing else."""
    return [{"label": r["home"], "wrapper": r["wrapper"]} for r in rows]


def coverage(rows=None):
    rows = status_rows() if rows is None else rows
    return (sum(1 for r in rows
                if r["hook"] and r["resolvable"] and r["fail_open"]
                and r.get("wrapper") is not False), len(rows))


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
    an idle estate is not a fault.

    ONE SNAPSHOT, BOTH CONSUMERS. The count and the ladder sentence come from
    a SINGLE `status_rows` read: taking them from two scans lets a live estate
    change between them and print a count over a population the sentence never
    described — hooks.inject_covered carries the same law for the same reason.
    """
    from . import hooks
    out = []
    try:
        rows = status_rows()
        n, m = coverage(rows)
    except Exception as e:
        return [("WARN", "record wiring unknown (%s: %s)" % (e.__class__.__name__, e))]
    if m:
        msg = "record coverage: %d of %d claude homes" % (n, m)
        out.append(("OK", msg) if n == m else
                   ("WARN", msg + " — `helm record install` closes the gap"))
    # AND THE REPAIR THE INSTALLER CANNOT MAKE. `helm record install` rewrites
    # entries; it cannot put back a `bin/helm-hook` that is not in the
    # checkout the entries name, so a home in that state needs its own
    # sentence or the reader is sent to a verb that changes nothing.
    gone = hooks.wrapper_gone_message("claude home", _ladder_rows(rows))
    if gone:
        out.append(("WARN", gone))
    ss = _sessions()
    if ss:
        out.append(("OK", "reflex-state: %d session%s, freshest %s ago" % (
            len(ss), "s"[:len(ss) != 1], _age(int(time.time() - ss[0][2])))))
    elif n:
        out.append(("WARN", "recorder wired but reflex-state is empty — no "
                            "PostToolUse event has landed yet"))
    out.extend(_swallow_rows())
    return out


def _swallow_rows():
    """[(level, msg)] for the swallow breadcrumbs — the OWNER-VISIBLE half.

    A trace nobody surfaces is the defect this cure was written against, one
    layer out: instrumenting nine handlers and then keeping the result in a
    file no verb reads would leave the estate exactly as blind as before, with
    more code. So the count rides `helm doctor`, which already runs read-only
    over everything.

    ABSENCE IS NOT ZERO. A missing file means the handlers never fired, which
    is real good news and said as OK. A file that exists and cannot be READ is
    a different fact and must not render as "none" — that is the
    missing-evidence-is-not-evidence-against shape, and it would make an
    unreadable log indistinguishable from a clean estate.
    """
    path = swallow_log()
    if not os.path.exists(path):
        return [("OK", "no swallowed exceptions recorded")]
    try:
        rows = swallows(limit=10000)
    except Exception as e:
        return [("WARN", "swallow log unreadable (%s: %s) — swallowed "
                         "exceptions are UNKNOWN, not zero"
                 % (e.__class__.__name__, e))]
    if not rows:
        return [("OK", "no swallowed exceptions recorded")]
    where = {}
    for r in rows:
        where[str(r.get("where") or "?")] = where.get(str(r.get("where") or "?"), 0) + 1
    top = sorted(where.items(), key=lambda kv: -kv[1])[0]
    return [("WARN", "%d swallowed exception%s recorded, %d site%s — most "
                     "frequent %s (%d): a broad handler ate a real error, and "
                     "the type tells you whose bug it is (ValueError/TypeError "
                     "are usually OURS). `helm record swallows` lists them"
             % (len(rows), "s"[:len(rows) != 1], len(where),
                "s"[:len(where) != 1], top[0], top[1]))]


_USAGE = """usage: helm record [--hook-json]                (PostToolUse event JSON on stdin)
       helm record status [--session S]
       helm record install [--dry] [--home NAME]
       helm record swallows [--limit N]        (exceptions broad handlers ate)"""


def _cmd_status(rest):
    session = rest[rest.index("--session") + 1] if "--session" in rest else None
    try:
        rows = status_rows()
        n, m = coverage(rows)
        line = "wiring: %d of %d claude homes (%s)" % (n, m, "+".join(HOOK_EVENTS))
        print("  " + (line if n == m or not m
                      else line + " — `helm record install` closes the gap"))
        for row in rows:
            if row.get("refusal_detail"):
                print("  " + row["refusal_detail"])
    except Exception:
        print("  wiring: unknown")
    ss = _sessions()
    if session:
        ss = [r for r in ss if r[0] == session_key(session)]
    if not ss:
        print("  no reflex-state yet — nothing recorded")
        return 0
    print("  %-36s %-5s %7s %7s %5s %5s %4s %4s %5s" % (
        "session", "age", "stalled", "passive", "dirty", "stuck", "loop",
        "cmds", "edits"))
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
        print("  %-36s %-5s %7s %7s %5s %5s %4s %4s %5s" % (
            key[:36], _age(int(now - mtime)), c.get("stalled-turns", 0),
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


def _cmd_swallows(rest):
    """Print the swallow breadcrumbs, newest first.

    ONE READ PATH, shared with the doctor row: both call `swallows()`, so the
    count in `helm doctor` and the list here can never disagree. A renderer
    that recomputed its own tally is how a surface starts contradicting the
    check that pointed at it.
    """
    limit = 50
    if "--limit" in rest:
        i = rest.index("--limit")
        try:
            limit = int(rest[i + 1])
        except (IndexError, ValueError):
            print("helm record: --limit needs an integer", file=sys.stderr)
            return 2
    path = swallow_log()
    rows = swallows(limit=limit)
    if not rows:
        # ABSENCE IS TWO DIFFERENT FACTS and the operator gets told which.
        if os.path.exists(path):
            print("helm record: %s exists but no readable rows — swallowed "
                  "exceptions are UNKNOWN, not zero" % path)
            return 1
        print("helm record: no swallowed exceptions recorded")
        return 0
    for r in rows:
        print("  %s  %-13s %-46s %s" % (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(r.get("ts") or 0))),
            str(r.get("exc") or "?")[:13],
            str(r.get("where") or "?")[:46],
            str(r.get("msg") or "")[:90]))
    print("helm record: %d swallowed exception%s (newest first, limit %d)"
          % (len(rows), "s"[:len(rows) != 1], limit))
    return 0


def cmd_record(args):
    """record [--hook-json] | status [--session S] | install [--dry] [--home NAME]
    | swallows [--limit N] — the session-keyed tool-outcome recorder
    (PostToolUse event JSON on stdin), plus the swallow-breadcrumb reader."""
    args = list(args)
    if args and args[0] == "status":
        return _cmd_status(args[1:])
    if args and args[0] == "install":
        return _cmd_install(args[1:])
    if args and args[0] == "swallows":
        return _cmd_swallows(args[1:])
    if args and args != ["--hook-json"]:
        print(_USAGE, file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        print(_USAGE, file=sys.stderr)
        return 2
    event = parse_event(sys.stdin.read())
    from . import hooklatency
    if event is None:
        hooklatency.mark("skipped")
        return 0  # garbled or keyless payload: record nothing, never block
    hooklatency.bind(event.get("session_id"), event.get("hook_event_name") or HOOK_EVENT)
    record(event)
    return 0
