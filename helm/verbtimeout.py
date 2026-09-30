#!/usr/bin/env python3
"""A helm verb killed by a timeout is a P0 diagnosis obligation (task/1822).

THE GAP. Hook timeouts already speak (hookalarm's rc-124 arm) and their spans
are measured (hooklatency), but a helm VERB a seat ran in a Bash call and lost
to a timeout left nothing behind: command-log recorded test runners only, so a
verb that hung was retried or routed around and never diagnosed. A helm verb
that cannot finish inside its caller's budget is a defect in helm, and the
seat that watched it die is the only one holding the evidence.

WHAT COUNTS AS TIMED OUT, matched in this order on one Bash event:
  * exit 124 — `timeout` killing the command, read through record's own
    `_event_exit` (an explicit exit key, or the failure leg's 'Exit code 124');
  * Claude Code's own Bash timeout — it kills the shell with 143, and the
    failure error is "Exit code 143", then "Command timed out after …" on the
    NEXT line (read off Claude Code 2.1.284). A plain 143 is any SIGTERM and
    is not matched;
  * another harness's timeout — a failure whose error's FIRST LINE says
    "timed out". The first line only: a nonzero exit puts 'Exit code N'
    there, so a verb that merely PRINTED "timed out" on stderr and exited 1
    is not mistaken for one the harness killed.
A killed `timeout -s KILL` (137) or any other signal exit is not matched: the
number does not say a clock did it. NOT COVERED: a command the harness moved
to the background when its time ran out fires no failure event at all, so
nothing here sees it.

WHICH COMMANDS. Only the command the line's status came from is blamed, and
only where the line proves which one that is: the LAST command of a line
joined by `;` and newlines alone. `&&`, `||`, a pipe, a background `&`, a
subshell, a group, `if` or a loop, errexit (`set -e`), an early `exit`, a
backtick, or a quote that never closes is UNKNOWN and blames nothing — a
false blame costs more than a missed one. For exit 124 that last command is
a helm argv (any path, behind any launcher record._argvs unwraps) that a
`timeout` launcher wrapped, or helm with no `timeout` anywhere on the line (a
verb that returns 124 itself): `timeout 1 helm doctor; timeout 1 sleep 10`
is the sleep's 124. A harness kill stops whatever was RUNNING, which is the
last command only when nothing ran before it, so it is blamed only on a line
that is the helm command alone. A non-helm command that times out raises
nothing here: that is the seat's own tool, and the red-gate rung already owns
a test runner's nonzero exit. A blocking read
is exempt — `chat wait`, `chat meld recv`, `chat meld invite --wait`,
anything with `--follow` — because running out the clock is what a bounded
wait is FOR. The verb is the root word, and a second word only when the
root has subverbs (cli_help's usage table), so `helm owed wisp` is `owed`.

THE OBLIGATION IS DERIVED, NEVER STORED. Both facts are command-log rows (the
recorder's one append path), and the open set is a fold over them: a
`verb-timeout` row opens the verb, a later `verb-diagnosis` row naming it
closes it. The same verb timing out twice is ONE obligation with a count, not
two. The log rotates at record.LOG_MAX, so an obligation older than one whole
generation of log is forgotten; the fold reads the live file only.

THE DISCHARGE IS THE FLEET'S EXISTING WRITE, not a new verb: a `helm task
add|comment|update` or `helm store add|revise` whose OWN exit status is
provably 0 — the line exited 0 and the write is the line alone or the
terminal command of a plain `;`/newline sequence, so `... || true`, `&&`, a
pipe or `; true` after it clears nothing — and whose OWN argument
words or heredoc body name the timed-out verb as its invocation, `helm chat
post` (any path to helm). The verb's bare words are not enough: `helm task
add 'chat post the release notes'` is a task, not a diagnosis. The rest of
the line never counts: a retry beside the write, or the write's own verb
words, is not a diagnosis.
"""
import hashlib
import json
import os
import re
import time

from . import record

TIMEOUT = "verb-timeout"
DIAGNOSIS = "verb-diagnosis"
EXIT_TIMEOUT = 124
# the diagnosis writes: a task row or a store note, the forms that carry text
DISCHARGES = {"task": ("add", "comment", "update"), "store": ("add", "revise")}
# a bounded wait running out its clock is the wait working
WAITS = (("chat", "wait"), ("chat", "meld", "recv"))
_WORD = re.compile(r"\A[a-z][a-z0-9-]*\Z")
_TIMED_OUT = re.compile(r"timed out", re.I)
EXIT_HARNESS = 143
_HARNESS_KILL = re.compile(r"\s*Exit code 143[ \t]*\r?\n\s*Command timed out after")
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][\w-]*)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?=\n|\Z)",
                      re.S)
_DISCHARGE = re.compile(r"\bhelm\s+(\w+)\s+(\w+)")
LABEL_MAX = 32


def _is_helm(argv):
    return len(argv) >= 2 and os.path.basename(argv[0]) == "helm" \
        and bool(_WORD.match(argv[1]))


def _commands(command):
    """[(launcher words, argv)] for each simple command, as record._argvs
    reads it, keeping which launchers (`timeout`, `env`, …) it stripped. An
    assignment-only command (`x=$(…)`) is kept with an empty argv: its status
    is its substitution's, so it can be the command a line's status came
    from."""
    out = []
    text = str(command or "").replace("\\\n", "")
    for words in record._simple_commands(text):
        seg = []
        for w in words:
            if seg or not record._ASSIGN.match(w):
                w = w if seg else w.lstrip("(")
                if w:
                    seg.append(w)
        argv = record._unwrap(seg)
        out.append((seg[:len(seg) - len(argv)], argv))
    return out


def _line_pieces(text):
    """The text of each `;`- or newline-separated piece of a command line,
    or None when anything else joins or wraps a command: `&&`, `||`, a pipe,
    a background `&`, a paren (subshell, function, process substitution), a
    backtick record's walker does not read, a `case` arm's `;;`, or a quote
    that never closes. Quotes, `$(…)`, comments and heredoc bodies are read
    as record._simple_commands reads them, so a separator inside them does
    not count. A redirection's `&` or `|` (`2>&1`, `<&3`, `&>f`, `>|f`) is
    dropped from its piece, because record's walker reads it as a
    separator."""
    out, drop = [], set()
    start, quote, pending, prev = 0, None, [], "\n"
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            quote = None if ch == "'" else quote
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = None
            elif text.startswith("$(", i):
                i, prev = record._skip_subst(text, i + 2), ")"
                continue
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif text.startswith("$(", i):
            i, prev = record._skip_subst(text, i + 2), ")"
            continue
        elif ch == "#" and prev in record._WORD_OPENERS:
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif ch == "<" and record._heredoc_at(text, i):
            tag, strip, i = record._heredoc_at(text, i)
            pending.append((tag, strip))
            continue
        elif ch == "&" and (prev in "<>" or text[i + 1:i + 2] == ">") \
                or ch == "|" and prev == ">":
            drop.add(i)
        elif ch in "&|`()" or ch == ";" and text[i + 1:i + 2] in (";", "&"):
            return None
        elif ch in ";\n":
            if ch == "\n" and pending:
                i, pending = record._skip_bodies(text, i, pending), []
            out.append((start, i))
            start = i + 1
        prev = ch
        i += 1
    if quote:
        return None
    out.append((start, n))
    return ["".join(c for k, c in enumerate(text[a:b], a) if k not in drop)
            for a, b in out]


# The first word of a compound command or a negation: its status is not the
# status of the simple command inside it.
_COMPOUND = frozenset(("if", "then", "elif", "else", "fi", "for", "while",
                       "until", "do", "done", "case", "esac", "select",
                       "function", "coproc", "{", "}", "!"))
# Words that can end the line before the commands after them run, or rewrite
# its status: a later command may never have run.
_FLOW = frozenset(("exit", "return", "logout", "exec", "source", ".", "eval",
                   "trap"))


def _errexit(argv):
    """Does this `set` turn on errexit (`set -e`, `set -euo pipefail`, `set
    -o errexit`)? Then any command's failure ends the line with its status."""
    return argv[:1] == ["set"] and (
        any(a[:1] == "-" and a[1:2] != "-" and "e" in a for a in argv[1:])
        or "errexit" in argv)


def _sequence(command):
    """[(launch, argv, piece text)] of a command line whose commands are
    joined only by `;` or newlines, where the LAST one's status is provably
    the line's; None when shell control flow cannot prove it (_line_pieces,
    a compound command, errexit, or an early exit before the last command)."""
    pieces = _line_pieces(str(command or "").replace("\\\n", ""))
    if pieces is None:
        return None
    seq = []
    for p in pieces:
        cmds = _commands(p)
        if len(cmds) > 1:               # the two walkers disagree: UNKNOWN
            return None
        seq += [(launch, argv, p) for launch, argv in cmds]
    for k, (launch, argv, _p) in enumerate(seq):
        head = argv[0] if argv else ""
        if head in _COMPOUND or _errexit(argv):
            return None
        if k < len(seq) - 1 and (head in _FLOW or any(
                os.path.basename(w) == "exec" for w in launch)):
            return None
    return seq or None


def _wrapped(launch):
    return any(os.path.basename(w) == "timeout" for w in launch)


def helm_argv(command, how=None):
    """The helm argv a timeout is blamed on, or None: UNKNOWN, no blame.

    Only the command the line's status came from is ever blamed: the LAST of
    a `;`/newline sequence (_sequence); any other shape proves nothing. For
    exit 124 that command is a helm argv `timeout` wrapped, or helm with no
    `timeout` anywhere on the line (a verb returning 124 itself). A harness
    kill stops whatever was RUNNING, which is the last command only when
    nothing ran before it, so it is blamed only on a line that is helm
    alone."""
    if "helm" not in str(command or ""):
        return None
    seq = _sequence(command)
    if not seq or not _is_helm(seq[-1][1]):
        return None
    launch, argv, _p = seq[-1]
    if how == "exit 124":
        # ANOTHER COMMAND WAS WRAPPED and helm was not: the 124 is most likely
        # that timeout's, so it is never blamed on helm
        if _wrapped(launch) or not any(_wrapped(x[0]) for x in seq):
            return argv
        return None
    return argv if len(seq) == 1 else None


def _subverbs(root):
    """The subverbs cli_help's usage line gives `root`, or an empty set.
    NO MODULE CACHE: it is read only on a timeout or a discharge write, and
    process state that outlives the call is what the gate's leak audit
    refuses."""
    from . import cli_help
    usage = str(cli_help._VERB_HELP.get(root) or "").split(" — ", 1)[0]
    words = set()
    for i, alt in enumerate(cli_help._alternatives(usage)):
        toks = cli_help._compact(alt).split()
        if i == 0 and toks and toks[0] == root:
            toks = toks[1:]
        if not toks:
            continue
        first = toks[0]
        if first.startswith("[") and first.endswith("]"):
            words.update(w for w in first[1:-1].split("|")
                         if cli_help._SUBVERB.match(w))
        elif cli_help._SUBVERB.match(first):
            words.add(first)
    return frozenset(words)


def verb_of(argv):
    """'chat post' for `helm chat post …`; 'owed' for `helm owed wisp`: a
    second word only when the root verb has subverbs."""
    words = [argv[1]]
    if len(argv) > 2 and _WORD.match(argv[2]) \
            and argv[2] in _subverbs(argv[1]):
        words.append(argv[2])
    return " ".join(words)[:LABEL_MAX]


def _waits(argv):
    """Is this a bounded wait running out its clock?"""
    return any(tuple(argv[1:1 + len(w)]) == w for w in WAITS) \
        or (tuple(argv[1:4]) == ("chat", "meld", "invite") and "--wait" in argv) \
        or "--follow" in argv


def timed_out(event, resp, failed):
    """'exit 124' | 'harness timeout' | None, for one Bash event."""
    code = record._event_exit(event, resp, failed)
    if code == EXIT_TIMEOUT:
        return "exit 124"
    if failed:
        err = str(event.get("error") or "")
        if code == EXIT_HARNESS and _HARNESS_KILL.match(err):
            return "harness timeout"
        first = err.strip().split("\n", 1)[0]
        if _TIMED_OUT.search(first):
            return "harness timeout"
    return None


def _write_text(command, argv):
    """The discharge write's OWN words: its arguments after `helm <root>
    <sub>`, and the bodies of heredocs its own command opens."""
    text = str(command or "")
    parts = [" ".join(argv[3:])]
    for m in _HEREDOC.finditer(text):
        head = text[:m.start()]
        cut = max(head.rfind(c) for c in "\n;&|")
        own = _DISCHARGE.search(head[cut + 1:])
        if own and own.group(1) == argv[1] and own.group(2) == argv[2]:
            parts.append(m.group(3))
    return " ".join(" ".join(parts).split()).lower()


def _names(text, verb):
    """Does `text` name the INVOCATION `helm <verb>` (by any path to helm)?
    The verb's bare words are English too: a task to 'chat post the release
    notes' is no diagnosis of `helm chat post`."""
    return bool(re.search(r"(?<![\w-])helm %s(?![\w-])" % re.escape(verb),
                          text))


def rows(session, command, event, resp, failed):
    """The command-log rows one Bash event adds: [] for nearly every call.
    The cheap tests run first, so an ordinary helm call parses nothing."""
    how = timed_out(event, resp, failed)
    if not how and (failed or not any(v in command for v in DISCHARGES)):
        return []
    digest = hashlib.sha1(str(command).encode()).hexdigest()[:12]
    if how:
        argv = helm_argv(command, how)
        if not argv or _waits(argv):
            return []
        return [{"kind": TIMEOUT, "verb": verb_of(argv), "how": how,
                 "ts": int(time.time()), "digest": digest}]
    # ONLY A WRITE PROVABLY EXITING 0 DISCHARGES: the line's status is the
    # write's only when the write is the line's terminal command (_sequence),
    # so `helm task add ... || true`, whose add failed, files nothing and
    # clears nothing. Its words are read from its OWN piece of the line, so
    # an earlier write's heredoc is not credited to it.
    seq = _sequence(command)
    if not seq or record._event_exit(event, resp, failed) != 0:
        return []
    _launch, a, piece = seq[-1]
    if len(a) < 3 or os.path.basename(a[0]) != "helm" \
            or a[2] not in DISCHARGES.get(a[1], ()):
        return []
    opened, _err = open_timeouts(session)
    own = _write_text(piece, a)
    named = [o["verb"] for o in opened if _names(own, o["verb"])]
    if named:
        return [{"kind": DIAGNOSIS, "verbs": named,
                 "ts": int(time.time()), "digest": digest}]
    return []


def open_timeouts(session):
    """([{verb, count, first, last, how}], error) — newest first.

    `error` is None or one short sentence for a log that exists and cannot be
    read; an absent log is an ordinary empty answer. Only lines carrying a
    `verb-` kind are parsed, so a garbled runner row costs nothing here."""
    p = os.path.join(record.session_dir(session), "command-log.jsonl")
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        record.swallow("verbtimeout.open_timeouts", exc)
        return [], "command-log unreadable (%s)" % type(exc).__name__
    opened = {}
    for ln in raw.decode("utf-8", "replace").splitlines():
        if '"verb-' not in ln:
            continue
        # ONE BAD ROW COSTS ONE ROW: an overflowing ts, a non-string verb or
        # any other shape the fold cannot read is skipped, never raised.
        try:
            r = json.loads(ln)
            ts = int(r.get("ts") or 0)
            if r.get("kind") == TIMEOUT and isinstance(r.get("verb"), str):
                o = opened.setdefault(r["verb"], {
                    "verb": r["verb"], "count": 0, "first": ts, "how": ""})
                o["count"] += 1
                o["last"], o["how"] = ts, str(r.get("how") or "")
            elif r.get("kind") == DIAGNOSIS and isinstance(r.get("verbs"), list):
                for v in r["verbs"]:
                    if isinstance(v, str) and v in opened \
                            and opened[v]["last"] <= ts:
                        del opened[v]
        except Exception:                   # noqa: BLE001 — one row, skipped
            continue
    return sorted(opened.values(), key=lambda o: -o["last"]), None


def stop_candidate(session, now=None):
    """(fp, line) for the Stop whisper ladder, or None.

    The fp carries the newest open timeout's verb, time and count, so a
    fresh timeout re-arms a latched line — even one in the same second, which
    the time alone would not tell apart — and a discharge retires it."""
    if not session:
        return None
    opened, err = open_timeouts(session)
    if err:
        return ("verbtimeout:unreadable",
                "%s — timed-out helm verbs are UNKNOWN, not zero; read "
                "`helm record status` before stopping" % err)
    if not opened:
        return None
    o = opened[0]
    now = time.time() if now is None else now
    more = len(opened) - 1
    return ("verbtimeout:%s:%d:%d" % (o["verb"], o["last"], o["count"]),
            "P0: `%s` timed out (%s) %s ago%s%s. Diagnose it; file "
            "the cause naming it: helm task add|comment or store add"
            % ("helm " + o["verb"], o["how"] or "timeout",
               _age(now - o["last"]),
               " x%d" % o["count"] if o["count"] > 1 else "",
               " (+%d more)" % more if more else ""))


def _age(secs):
    """'7m', '5h', '3d' — the when, at the width a whisper can afford."""
    m = max(0, int(secs)) // 60
    return "%dm" % m if m < 120 else "%dh" % (m // 60) if m < 2880 \
        else "%dd" % (m // 1440)
