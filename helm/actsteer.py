#!/usr/bin/env python3
"""Act steers: the guidance that belongs to an ACT is said at the act.

THE MOMENT IS AN ACT, NOT A TOPIC (task/2980, design section 1). A rule about
`pkill -f` that rides every prompt whose words look like process cleanup
reaches a seat on dozens of turns that never kill anything, and misses the one
turn that does. The act itself is the exact moment, and argv-guard already sees
every Bash, Monitor, Write and Edit call. So this module owns three kinds of
rung, all read by `chat.cmd_argv_guard`:

  DENY    an act that must not run as written. The call is refused with one
          line that says what was found, why, and the command that works:
          a `pkill -f` whose pattern matches its own shell (the kill ends the
          seat's own turn at exit 144), and a `gh pr|issue` body that carries
          an AI authoring line (owner canon: none reaches GitHub).
  STEER   advice about an act that has ALREADY RUN. A PreToolUse
          `additionalContext` reaches the model with the call's RESULT, not
          before the call (TRACED in f897d7b595), so a steer is worded as
          "you just did X; now check Y", never "about to".
          Once per (context, steer): the latch is chat.steer_unfired, and
          chat.forget_steers clears it at a context boundary.
  RETIRE  the prompt-lane trigger a rung replaces, in the same change, so the
          rule stops riding every turn. MOVED below trims store keyword cells;
          reflex.REKEYED moves the replaced reflexes to reflex.ACT_SIGNAL.

A STEER IS ONE SHORT LINE, NEVER A WALL. STEER_CAP and DENY_CAP are the whole
rendered line, prefix included, and tests/test_actsteer.py holds every line
this module can print under them. The full rule stays in the store.

FAIL-OPEN, TOTAL: every public function returns an empty answer on any error.
argv-guard runs on every tool call, and a guard that throws wedges the fleet.
"""
import os
import re
import shlex

# The whole rendered line: "[helm steer] " + text, one line (design 5.2).
STEER_CAP = 250
# The whole rendered refusal: "[helm argv-guard] BLOCKED: " + reason.
DENY_CAP = 300

STEER_PREFIX = "[helm steer] "
DENY_PREFIX = "[helm argv-guard] BLOCKED: "

# One command-position boundary for every rung here: start of text, blank,
# or a shell operator. A word inside another word (`--timeout`, `mypgrep`)
# never counts.
_AT = r"(?:^|(?<=[\s;&|(`]))"


class _Rx(object):
    """A regex compiled on its first use.

    argv-guard is a fresh process on every tool call, and compiling this
    module's patterns at import measured 2.6 ms — paid by every Bash call,
    including the ones no rung here ever reads. Deferred, each call pays only
    for the patterns its own gates let it reach (0.4-0.7 ms import)."""

    __slots__ = ("_args", "_rx")

    def __init__(self, *args):
        self._args, self._rx = args, None

    def __getattr__(self, name):
        if self._rx is None:
            self._rx = re.compile(*self._args)
        return getattr(self._rx, name)


def _runs(text, head, opener):
    """True when the command word at `text[head]` RUNS: no quote is open
    there, or it opens a command substitution inside double quotes (`opener`
    is the index of the `$(` or backtick that did, else -1).

    A QUOTED BODY IS DATA. A `gh` or `chat post` body that quotes a command
    across a literal newline would otherwise read as a command line of its
    own, and the rungs here refuse or steer on command lines. The quote
    state is argv-guard's own reader (chat._mask_quoted), run on the prefix,
    so the two surfaces share one grammar. `text` must be the code text
    (_code): a heredoc body is not shell, so an apostrophe in one would open
    a quote that never closes and hide every command after it.

    ACCEPTED HOLES: a command inside ANY quoted script string, single or
    double (`bash -c '...'`, `sh -c "..."`, `ssh host '...'`), is data to this
    reader, so it is neither refused nor steered here; only a `$(` or
    backtick substitution inside double quotes runs."""
    from . import chat
    quote = chat._mask_quoted(text[:head])[1]
    if quote is None:
        return True
    return quote == '"' and opener >= 0 and text[opener:opener + 1] in "$`"


_SHELL_READERS = frozenset(("bash", "sh", "zsh", "dash", "ksh"))
_LAUNCHERS = frozenset(("sudo", "env", "nice", "nohup", "command", "exec",
                        "time"))
_PIPE_TO_SHELL = _Rx(r"\|\s*(?:sudo\s+)?(?:\S*/)?(?:ba|z|da|k)?sh\b")


def _feeds_shell(line, at):
    """True when the heredoc opened at `line[at]` is a SCRIPT a shell runs:
    its command word is a shell (`bash <<EOF`, `sudo sh -s <<'EOF'`), or the
    command it feeds pipes into one (`cat <<EOF | bash`)."""
    from . import chat
    masked = chat._mask_quoted(line)[0]
    head = masked[:at]
    for sep in ";|&(":
        head = head.rsplit(sep, 1)[-1]
    # the command word: past NAME=value assignments, launchers and their
    # options (`sudo -E bash`, `env X=1 sh`)
    words = head.split()
    while words and ("=" in words[0] or words[0].startswith("-")
                     or os.path.basename(words[0]) in _LAUNCHERS):
        words = words[1:]
    if words and os.path.basename(words[0]) in _SHELL_READERS:
        return True
    return bool(_PIPE_TO_SHELL.search(masked[at:]))


def _code(text):
    """`text` with every heredoc BODY blanked, offsets kept: each body
    character becomes a space and its newlines stay, so an index into the
    result indexes the command too.

    A HEREDOC BODY IS DATA, whatever its tag: `<<'EOF'`, `<<EOF` and
    `<<-EOF` all hand their lines to a program's stdin. Read as shell, an
    apostrophe in one (`don't`) opened a quote that never closed, and every
    command after it — a `pkill -f`, a `gh pr comment` — read as quoted text
    and went unrefused (found in review: the base refused them). THE ONE
    EXCEPTION is a body a shell runs as its script (_feeds_shell), which
    stays code and is read like any other command line. Fail-open to the
    text unchanged."""
    from . import chat
    try:
        lines = text.split("\n")
        out = list(lines)
        for i, openers in chat._heredoc_openers(text):
            for match, _quoted, start, _end, resume in openers:
                if _feeds_shell(lines[i], match.start()):
                    continue
                for k in range(start, min(resume, len(lines))):
                    out[k] = " " * len(lines[k])
        return "\n".join(out)
    except Exception:                          # noqa: BLE001 — fail open
        return text


def _opener_before(text, at):
    """Index of the `$(` or backtick directly before `text[at]` (blanks
    skipped), else -1."""
    i = at
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    if text[i - 1:i] == "`":
        return i - 1
    if text[i - 2:i] == "$(":
        return i - 2
    return -1


def _cut(text, width):
    """`text` on one line, cut to `width` characters and marked when cut."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


# ---------------------------------------------------------------------------
# DENY 1: pkill -f whose pattern matches its own shell.
#
# pkill -f matches the FULL command line of every process, and the shell that
# runs the command carries the command text in its argv. So a pattern that
# matches the command text matches that shell, and the kill ends the seat's
# own turn at exit 144 with everything chained after it discarded. MEASURED:
# 32 self-kills while this was a once-per-session steer, which cannot reach
# the model before the first kill (the advice arrives with the result).
#
# THE TEST IS THE DEFINITION: does the pattern match the text that carries it?
# A bracketed pattern (`[h]elm web`) does not, an anchored one (`^node`) does
# not (the shell's argv starts with the shell), `-x` requires a whole-line
# match, and a pattern held in a variable is not in the text. Each of those
# passes because it is safe, not because it is on a list.
# ---------------------------------------------------------------------------
# AT A COMMAND POSITION ONLY: the head of a segment (after `;`, `&`, `|`, a
# newline, a subshell paren or a command substitution), past any leading
# assignments and wrappers. `echo pkill -f x` names the tool and runs nothing.
_KILL_WORDS = _Rx(r"(?:^|[;&|\n(`]|\$\()\s*"
                  r"(?:(?:sudo|nohup|nice|command|exec|env|time"
                  r"|timeout\s+(?:-\S+\s+)*\S+|xargs(?:\s+-\S+)*"
                  r"|[A-Za-z_]\w*=\S*)\s+)*"
                  r"(?:/usr/bin/|/bin/)?(pkill|pgrep)\s+([^|;&)`\n]*)")
# procps-ng options that take a value, short and long.
_SHORT_VALUED = set("dFgGPrstuUO")
_LONG_VALUED = {"--delimiter", "--pidfile", "--pgroup", "--group", "--parent",
                "--session", "--terminal", "--euid", "--uid", "--ns",
                "--nslist", "--signal", "--older", "--runstates", "--env",
                "--cgroup", "--queue"}
_FEEDS_KILL = _Rx(r"\|\s*xargs\b[^|;&]*\bkill\b")
_KILL_WRAPS = _Rx(_AT + r"kill\b[^;&|\n]*(?:\$\(|`)\s*$")


def _kill_call(argtext):
    """(full, exact, pattern) for one pkill/pgrep argument string, or None
    when it cannot be read. `pattern` is None when there is none."""
    try:
        words = shlex.split(argtext)
    except ValueError:
        return None
    full = exact = False
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--":
            return full, exact, words[i + 1] if i + 1 < len(words) else None
        if w.startswith("--"):
            key = w.split("=", 1)[0]
            full = full or key == "--full"
            exact = exact or key == "--exact"
            i += 2 if key in _LONG_VALUED and "=" not in w else 1
            continue
        if w.startswith("-") and len(w) > 1:
            flags = w[1:]
            if flags.isdigit() or flags.isupper():
                i += 1                     # a signal: -9, -KILL, -SIGTERM
                continue
            skip = 1
            for n, ch in enumerate(flags):
                if ch in _SHORT_VALUED:
                    skip = 1 if n + 1 < len(flags) else 2
                    break
                full = full or ch == "f"
                exact = exact or ch == "x"
            i += skip
            continue
        return full, exact, w
    return full, exact, None


def _matches_itself(pattern, command):
    """True when `pattern`, as a pkill regex, matches the command text that
    carries it. An unreadable regex is read as a literal."""
    if not pattern or pattern.startswith("^") or "$" in pattern:
        return False                       # anchored, or a variable
    try:
        return re.search(pattern, command) is not None
    except re.error:
        return pattern in command


def bracketed(pattern):
    """`pattern` with its first plain letter or digit bracketed, the one-edit
    rewrite that stops a pattern matching its own text: `helm web` becomes
    `[h]elm web`. A character after a backslash or inside a class is skipped."""
    depth, esc = 0, False
    for i, ch in enumerate(pattern):
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
        elif not depth and ch.isalnum():
            return pattern[:i] + "[" + ch + "]" + pattern[i + 1:]
    return pattern


def _self_kills(command):
    """[(tool, pattern, kills, fixed)] for every pkill/pgrep -f call whose
    pattern matches its own command text. `kills` is True for pkill and for a
    pgrep whose output feeds a kill (`| xargs kill`, `kill $(pgrep ...)`).
    `fixed` is the bracketed pattern, or None when bracketing cannot help
    (the pattern also stands elsewhere in the command, or is one character)."""
    out = []
    scan = _code(command)
    for m in _KILL_WORDS.finditer(scan):
        opener = m.start() if scan[m.start():m.start() + 1] in "$`" else -1
        if not _runs(scan, m.start(1), opener):
            continue                   # quoted text naming the tool
        read = _kill_call(m.group(2))
        if not read:
            continue
        full, exact, pattern = read
        if not full or exact or not _matches_itself(pattern, command):
            continue
        tool = m.group(1)
        kills = tool == "pkill" or bool(
            _FEEDS_KILL.match(scan, m.end())
            or _KILL_WRAPS.search(scan[:m.start(1)]))
        fixed = bracketed(pattern)
        # THE CURE IS PROVEN ON THE COMMAND IT WOULD PRODUCE, not on the one
        # that was typed: the typed one always contains the old pattern.
        after = (command[:m.start(2)] +
                 m.group(2).replace(pattern, fixed, 1) + command[m.end(2):])
        out.append((tool, pattern, kills,
                    None if _matches_itself(fixed, after) else fixed))
    return out


# The widest bracketed pattern the refusal prints whole: the fixed prose plus
# a 40-character echo of the typed pattern leave this much of DENY_CAP.
_WHOLE_CURE = 80


def pkill_refusal(command):
    """The refusal line for a self-matching kill, or None."""
    try:
        if "pkill" not in command and "pgrep" not in command:
            return None
        from . import chat
        text = command.replace("\\\n", "")
        for tool, pattern, kills, fixed in _self_kills(text):
            if not kills:
                continue
            # THE CURE IS NEVER CUT: a cut command is a broken one. A pattern
            # too long to print whole gets the one-character instruction.
            if not fixed:
                cure = ("Bracketing cannot stop it matching this command, "
                        "so kill a pid you resolved first.")
            elif len(shlex.quote(fixed)) <= _WHOLE_CURE:
                cure = "Bracket one character: %s -f %s" % (
                    tool, shlex.quote(fixed))
            else:
                cure = ("Bracket one character of the pattern so it cannot "
                        "match itself, e.g. its first: %s"
                        % bracketed(pattern.lstrip("\\^.*[]()")[:1] or "x"))
            return _cut(DENY_PREFIX + "%s -f %s matches its own shell, whose "
                        "command line holds the pattern, so the kill ends "
                        "this turn at exit 144. %s"
                        % (tool, shlex.quote(_cut(pattern, 40)), cure),
                        DENY_CAP)
    except Exception:                          # noqa: BLE001 — fail open
        return None
    return None


PGREP_STEER = ("That pgrep -f also matched your own shell, whose command line "
               "holds the pattern, so one pid it printed is that shell. "
               "Bracket one character (e.g. '[h]elm web') before you trust "
               "the list.")


# ---------------------------------------------------------------------------
# DENY 2: an AI authoring line in a GitHub PR or issue body.
#
# Owner canon: no AI authoring line reaches GitHub, in any repo. Commits
# are refused by the commit-msg trailer rung (helm/trailer_rung.py); a PR or
# issue body never passes through git, and a squash body written by
# `gh pr merge --body` becomes a commit on the server.
# This rung judges those bodies with the trailer rung's own reader, so the
# two surfaces agree on what an authoring line is: column zero, trailer
# grammar, a model or harness in the value, or a Generated-with footer. Prose
# that mentions Claude is not a trailer, and the informal sign-off a public
# post ends with is not one either.
#
# WHAT IS READ: for `gh pr|issue create|edit|comment|review|merge|close|
# reopen`, a --body/-b value and the file --body-file/-F names; for `gh api`
# on an issues or pulls endpoint, a `body=` field (-f, -F, --field,
# --raw-field; `-F body=@file` reads the file) and the JSON file --input
# names; and, for both, the whole command text, which carries any heredoc
# body gh reads from stdin.
#
# ACCEPTED HOLES, not claimed as covered: a body gh reads from a PIPE
# (`--body-file -` or `--input -` fed by another command), a body the shell
# computes (`--body "$(cat f)"`, a variable), `gh release create|edit
# --notes`, and a GraphQL mutation through `gh api graphql`. A file this rung
# cannot open is not judged (fail-open).
# ---------------------------------------------------------------------------
_GH_BODY = _Rx(_AT + r"gh\s+(?:(?:pr|issue)\s+(?:create|edit|comment|review|"
               r"merge|close|reopen)\b|api\b)")
_BODY_FLAGS = ("--body", "-b", "--comment", "-c")
_FILE_FLAGS = ("--body-file", "-F")
_API_FIELDS = ("-f", "-F", "--field", "--raw-field")
_API_BODY_PATH = _Rx(r"(?:^|/)(?:issues|pulls)(?:/|$)")
_SESSION_LINK = _Rx(r"claude\.ai/code/session", re.I)
_BODY_FILE_MAX = 256 * 1024


def _read_file(path, cwd):
    """The text of a body file gh would read, or None (`-` is stdin)."""
    if not path or path == "-":
        return None
    path = os.path.expanduser(path)
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    try:
        with open(path, "rb") as f:
            return f.read(_BODY_FILE_MAX).decode("utf-8", "replace")
    except OSError:
        return None


def _json_body(text):
    """The `body` string of a JSON document, else the whole text."""
    import json
    try:
        doc = json.loads(text)
    except ValueError:
        return text
    body = doc.get("body") if isinstance(doc, dict) else None
    return body if isinstance(body, str) else text


def _pr_issue_bodies(words, cwd):
    out = []
    for i, w in enumerate(words):
        name, eq, value = w.partition("=")
        if name in _BODY_FLAGS and w.startswith("--") and eq:
            out.append(value)
        elif w in _BODY_FLAGS and i + 1 < len(words):
            out.append(words[i + 1])
        path = value if (name in _FILE_FLAGS and eq) else (
            words[i + 1] if w in _FILE_FLAGS and i + 1 < len(words) else None)
        text = _read_file(path, cwd)
        if text is not None:
            out.append(text)
    return out


def _api_bodies(words, cwd):
    """`gh api` bodies, only on an issues or pulls endpoint: a comment, an
    issue or a PR, created or edited."""
    if not any(_API_BODY_PATH.search(w) for w in words if
               not w.startswith("-")):
        return []
    out = []
    for i, w in enumerate(words):
        name, eq, value = w.partition("=")
        field = None
        if w in _API_FIELDS and i + 1 < len(words):
            field = words[i + 1]
        elif name in ("--field", "--raw-field") and eq:
            field = value
        if field is not None:
            key, eq2, val = field.partition("=")
            if key == "body" and eq2:
                if val.startswith("@") and w in ("-F", "--field"):
                    val = _read_file(val[1:], cwd)
                if val is not None:
                    out.append(val)
        path = value if (name == "--input" and eq) else (
            words[i + 1] if w == "--input" and i + 1 < len(words) else None)
        text = _read_file(path, cwd)
        if text is not None:
            out.append(_json_body(text))
    return out


def _gh_bodies(command, cwd):
    """Every body text the gh commands in `command` hand GitHub, as a list.

    Each gh invocation that RUNS (not one named inside a quoted body) is
    parsed on its own words, so `-F` means --body-file to `gh pr` and a field
    to `gh api`. The flag values are read with quoted heredoc bodies cut out,
    because an apostrophe in a body would otherwise end the parse. The whole
    command is the last text, which is how a heredoc body fed on stdin is
    judged; it is read only when some body-carrying gh command runs."""
    from . import chat
    text = _code(command.replace("\\\n", ""))
    masked = chat._mask_quoted(text)[0]
    texts, ran = [], False
    for m in _GH_BODY.finditer(text):
        if not _runs(text, m.start(), _opener_before(text, m.start())):
            continue
        ran = True
        end = len(text)
        for i in range(m.end(), len(masked)):
            if masked[i] in ";|&\n":
                end = i
                break
        try:
            words = shlex.split(text[m.end():end])
        except ValueError:
            continue
        api = m.group(0).split()[-1] == "api"
        texts += (_api_bodies if api else _pr_issue_bodies)(words, cwd)
    if ran:
        # the whole command LAST: a flag value names its offending line more
        # exactly than the command line that carries it
        texts.append(command)
    # A double-quoted "\n" is two characters to the shell and a line break to
    # the reader of the posted body.
    return [t.replace("\\n", "\n") for t in texts]


def authoring_lines(text):
    """[line] — every AI authoring line in one body: the trailer rung's
    reader, plus a Claude session link at column zero."""
    from . import trailer_rung
    hits = [line for _n, line in trailer_rung.offending(text)]
    for raw in text.splitlines():
        if raw[:1].isspace() or raw.strip() in hits:
            continue
        if _SESSION_LINK.search(raw):
            hits.append(raw.strip())
    return hits


def trailer_refusal(command, cwd=None):
    """The refusal line for a gh PR/issue body with an AI authoring line, or
    None."""
    try:
        if "gh" not in command:
            return None
        for text in _gh_bodies(command, cwd):
            hits = authoring_lines(text)
            if hits:
                return _cut(DENY_PREFIX + "this gh body carries an AI "
                            "authoring line: %s. Owner canon: nothing on "
                            "GitHub names a model as its author. Delete it "
                            "and send again (a quoted example passes "
                            "indented)." % _cut(hits[0], 70), DENY_CAP)
    except Exception:                          # noqa: BLE001 — fail open
        return None
    return None


def refusal(command, cwd=None):
    """The first deny this Bash command earns, or None."""
    return pkill_refusal(command or "") or trailer_refusal(command or "", cwd)


# ---------------------------------------------------------------------------
# COMPUTED STEERS on Bash. Each is gated by a substring test first, and reads
# at most one small local file on a match.
# ---------------------------------------------------------------------------

# A hand-read of a session transcript (405 hand reads against 28 cv calls,
# MEASURED by the designer). The memory dir sits under projects/ and is not a
# transcript; listing the dir is not reading it.
_TRANSCRIPT = _Rx(r"\.(?:claude(?:-homes/[\w.-]+)?|codex)/"
                  r"(?:projects|sessions)(?:/[^\s'\"|;&<>)]*)?")
_READER = _Rx(_AT + r"(?:/usr/bin/)?(?:rg|grep|ugrep|jq|find|cat|tail|head|"
              r"sed|awk|zcat|less|python3?)\b")
TRANSCRIPT_STEER = ("You just read a session transcript by hand. For what was "
                    "said or decided, cv recall or cv search answers first and "
                    "names the span; open a jsonl only to check that span.")


def _reads_transcript(command):
    for m in _TRANSCRIPT.finditer(command):
        path = m.group(0)
        if "/memory" in path:
            continue
        if ".jsonl" in path:
            return True
        head = max(command.rfind(c, 0, m.start()) for c in ";|&\n") + 1
        if _READER.search(command[head:m.start()]):
            return True
    return False


# Timing a short command under /usr/bin/timeout, which here is uutils and
# rounds the whole run up to a ~100 ms tick (prior usr-bin-timeout-is-uutils-
# and-quantises-to-a-100ms-tick). BOTH halves at a COMMAND POSITION in the
# unquoted text: `timeout` itself (after any wrappers such as `time`), and a
# timer. Prose that quotes "time timeout 5 true" runs nothing, and a
# `--timeout` flag is not the binary. One quoted form does run, a command
# string handed to hyperfine, so that wrapper is read inside its quotes.
_CMD_AT = r"(?:^|[;&|\n(`]|\$\()\s*"
_WRAP = (r"(?:(?:time|/usr/bin/time(?:\s+-\S+)*|perf\s+stat(?:\s+-\S+)*"
         r"|nice|sudo|env|command|exec|[A-Za-z_]\w*=\S*)\s+)*")
_TIMEOUT_AT = _Rx(_CMD_AT + _WRAP + r"(?:/usr/bin/)?timeout\s+(?:-\S+\s+)*\d")
_TIMER_AT = _Rx(_CMD_AT + r"(?:[A-Za-z_]\w*=\S*\s+)*"
                r"(?:time|/usr/bin/time|hyperfine|perf\s+stat)\b"
                r"|\btimeit\b")
_TIMER_EXPR = _Rx(r"%N|\bEPOCHREALTIME\b")
_HYPERFINE_TIMEOUT = _Rx(r"\bhyperfine\b[^;&|\n]*?['\"]\s*(?:/usr/bin/)?"
                         r"timeout\s+(?:-\S+\s+)*\d")
TIMEOUT_STEER = ("You just timed a command under /usr/bin/timeout. Here it is "
                 "uutils and rounds the whole run up to a ~100 ms tick, so a "
                 "short timing reads as the tick. Time the bare command.")


def _times_under_timeout(command):
    from . import chat
    masked = chat._mask_quoted(command)[0]
    if _TIMEOUT_AT.search(masked):
        return bool(_TIMER_AT.search(masked) or _TIMER_EXPR.search(command))
    m = _HYPERFINE_TIMEOUT.search(command)
    return bool(m) and _runs(command, m.start(), -1) and \
        bool(_TIMER_AT.search(masked))


# A claim post with no CL% (design act.verdict). The pinned rule alone gives
# 8.6% compliance on claim posts (78 of 904, MEASURED by the designer). The
# claim words are upper case on purpose: "done" in prose is not a claim.
#
# THE CLAIM OPENS THE BODY. A claim word anywhere in the text fired on prose
# ("the READY row from yesterday is stale") and on a chat verb named inside
# another command's quotes. The body is the post's positional text, or the
# quoted heredoc it reads; before the claim word may stand only basis tags
# ([MEASURED]), @mentions and task/N references.
_CHAT_POST = _Rx(_AT + r"helm\s+chat\s+(post|reply|dm)\b")
_CLAIM_START = _Rx(r"\A\s*(?:(?:\[[A-Za-z -]+\]|@[\w.-]+:?|task/\d+:?)\s+)*"
                   r"(?:VERDICT|READY|LANDED|LAND|DONE|SHIPPED)\b")
# chat post/reply/dm flags that take a value; every other flag is boolean
_CHAT_VALUED = ("--room", "--seat", "--dm", "--reply-to")
_SCORED = _Rx(r"\bCL\s*%?\s*[:=]?\s*\d{1,3}\b|\bUNVERIFIED\b|--unverified\b",
              re.I)
CL_STEER = ("That post makes a claim with no CL%. Append CL 0-100 (mean of "
            "completeness, robustness, alignment, edge coverage) and act on "
            "it: under 50 stop, 50-84 probe first, 85+ act and name what "
            "would falsify it.")

# A long foreground command on a non-claude seat, which holds the turn lock
# there, so monitor and chat events wait until it returns. 44% of the old
# keyword fires were on claude seats, where it does not apply (designer).
_LONG = _Rx(_AT + r"(?:fab\s+(?:gate|test|build|run)\b|helm\s+gate\b"
            r"|helm\s+(?:chat|dispatch)\s+wait\b"
            r"|sleep\s+(?:[3-9]\d|\d{3,})(?![\d.])"
            r"|(?:cargo|go)\s+(?:build|test)\b|make\b|docker\s+build\b"
            r"|(?:npm|pnpm|yarn)\s+(?:run|test|install|build)\b"
            r"|(?:until|while)\b[^\n]*\bsleep\b)")
LONG_TIMEOUT_MS = 300000
# opus46 is a proxy family that serves a Claude model; the lock this steer
# is about is the non-claude seats' (prior foreground-command-lock-...).
_CLAUDE_PROXIES = frozenset(("opus46",))
FOREGROUND_STEER = ("That long command ran in the foreground on a %s seat, "
                    "which holds the turn lock: monitor and chat events wait "
                    "until it returns. Run long work with run_in_background.")


def _chat_bodies(raw, command):
    """The body text of every `helm chat post|reply|dm` in the command that
    RUNS: its positional words (after the id of a reply or the seat of a
    dm), else the quoted heredoc its line opens."""
    from . import chat
    masked = chat._mask_quoted(command)[0]
    heredocs = chat._quoted_heredoc_bodies(raw)
    out = []
    for m in _CHAT_POST.finditer(command):
        if not _runs(command, m.start(), _opener_before(command, m.start())):
            continue
        end = len(command)
        for i in range(m.end(), len(masked)):
            if masked[i] in ";|&\n":
                end = i
                break
        line = command[m.end():end]
        if "<<" in chat._mask_quoted(line)[0]:
            out += [body for opener, body in heredocs
                    if line.strip() and line.strip() in opener]
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        pos, i = [], 0
        while i < len(words):
            w = words[i]
            if w in _CHAT_VALUED:
                i += 2
                continue
            if w.startswith("--"):
                i += 1
                continue
            pos.append(w)
            i += 1
        if m.group(1) in ("reply", "dm"):
            pos = pos[1:]
        if pos:
            out.append(" ".join(pos))
    return out


def _seat_family():
    """The non-claude proxy family of this seat's name, or None. Name
    grammar only, no file read: `codex`, `codex-3`, and a project seat
    `<project>-<family>[-N]`. Fail-open to None."""
    from . import chat, home
    try:
        name = str(home.chat_name() or "")
    except Exception:                          # noqa: BLE001 — fail open
        return None
    parts = [p for p in name.split("-") if p]
    while parts and parts[-1].isdigit():
        parts.pop()
    family = parts[-1] if parts else ""
    if family in chat._SPAWN_FAMILIES and family not in _CLAUDE_PROXIES:
        return family
    return None


# An outward act on a repo this checkout's origin does not own, or a repo
# made public (design act.push.public). A plain push to origin is the land
# path and stays silent.
_OUTWARD = _Rx(_AT + r"(?:git\s+(?:-C\s+\S+\s+)?push\b"
               r"|gh\s+(?:pr|issue)\s+create\b"
               r"|gh\s+repo\s+(?:create|edit)\b)")
_MADE_PUBLIC = _Rx(r"--public\b|--visibility[=\s]+public\b")
_GITHUB = _Rx(r"github\.com[:/]([\w.-]+)/[\w.-]+")
_REPO_FLAG = _Rx(r"(?:--repo|-R)[=\s]+([\w.-]+)/[\w.-]+")
PUBLIC_STEER = ("That act reached a repo your origin does not own, or made "
                "one public. An outward push, PR or issue needs the owner's "
                "go unless canon exempts that repo; if he gave none, tell him "
                "now.")


def _git_config(cwd):
    """The text of the git config that governs `cwd`, or ''. Follows a
    worktree's `.git` file to its common dir."""
    try:
        d = os.path.abspath(cwd or ".")
        for _ in range(40):
            dot = os.path.join(d, ".git")
            if os.path.isdir(dot):
                cfg = os.path.join(dot, "config")
                break
            if os.path.isfile(dot):
                with open(dot, encoding="utf-8") as f:
                    gitdir = f.read().split("gitdir:", 1)[-1].strip()
                gitdir = os.path.join(d, gitdir)
                common = os.path.join(gitdir, "commondir")
                if os.path.isfile(common):
                    with open(common, encoding="utf-8") as f:
                        gitdir = os.path.join(gitdir, f.read().strip())
                cfg = os.path.join(gitdir, "config")
                break
            up = os.path.dirname(d)
            if up == d:
                return ""
            d = up
        else:
            return ""
        with open(cfg, encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return ""


def _remotes(config):
    """{remote name: github owner} from one git config text."""
    out, name = {}, None
    for line in config.splitlines():
        m = re.match(r'\s*\[remote "([^"]+)"\]', line)
        if m:
            name = m.group(1)
            continue
        if line.lstrip().startswith("["):
            name = None
        elif name and line.strip().startswith("url"):
            g = _GITHUB.search(line)
            if g:
                out[name] = g.group(1).lower()
    return out


def _outward(command, cwd):
    """True when the command makes a repo public, or pushes, opens a PR or
    opens an issue on a GitHub owner other than origin's."""
    remotes = None
    for m in _OUTWARD.finditer(command):
        verb = " ".join(m.group(0).split())
        rest = re.split(r"[;&|\n]", command[m.end():])[0]
        if verb.startswith("gh repo"):
            if _MADE_PUBLIC.search(rest):
                return True
            continue
        if remotes is None:
            remotes = _remotes(_git_config(cwd))
        own = remotes.get("origin")
        if not own:
            return False               # no owner to compare against
        named = {o.lower() for o in _GITHUB.findall(rest) +
                 _REPO_FLAG.findall(rest)}
        if not named and verb.startswith("git"):
            try:
                words = shlex.split(rest)
            except ValueError:
                continue
            target = next((w for w in words if not w.startswith("-")),
                          "origin")
            named = {remotes.get(target, own)}
        elif not named and verb == "gh pr create":
            # a PR from a fork opens against its parent by default
            named = {o for r, o in remotes.items() if r == "upstream"}
        if any(o != own for o in named):
            return True
    return False


# A lane claim: the Bash moment of sweep-before-you-build (the Write moment
# is write_steers; one id, so one latch covers both).
_LANE_CLAIM = _Rx(_AT + r"helm\s+work\s+claim\b")


def bash_steers(raw, command, tool_input=None, cwd=None):
    """[(steer-id, text)] for one Bash call. `raw` is the whole command with
    its heredoc bodies; `command` is the text argv_steers reads, the bodies
    of quoted heredocs cut out."""
    out = []
    try:
        tin = tool_input if isinstance(tool_input, dict) else {}
        joined = raw.replace("\\\n", "")
        code = _code(joined) if any(
            w in raw for w in ("pgrep", "chat", "timeout", "claim")) else ""
        if "pgrep" in command and any(
                t == "pgrep" and not k for t, _p, k, _f in _self_kills(joined)):
            out.append(("pgrep-f-matches-your-own-shell", PGREP_STEER))
        if (".claude" in command or ".codex" in command) \
                and _reads_transcript(command):
            out.append(("transcript-read", TRANSCRIPT_STEER))
        if "chat" in command and not _SCORED.search(raw) and any(
                _CLAIM_START.match(b) for b in _chat_bodies(raw, code)):
            out.append(("claim-without-cl", CL_STEER))
        if "timeout" in command and _times_under_timeout(code):
            out.append(("timeout-timing", TIMEOUT_STEER))
        # the seat's family first: it is an env read, and on a claude seat
        # (most of the fleet) the long-command pattern is never compiled
        family = None if tin.get("run_in_background") else _seat_family()
        if family:
            try:
                long_call = int(tin.get("timeout") or 0) >= LONG_TIMEOUT_MS
            except (TypeError, ValueError):
                long_call = False
            if long_call or _LONG.search(command):
                out.append(("foreground-long-on-proxy-seat",
                            FOREGROUND_STEER % family))
        if ("push" in command or "gh " in command) and _outward(command, cwd):
            out.append(("public-push", PUBLIC_STEER))
        if "claim" in command and any(
                _runs(code, m.start(), _opener_before(code, m.start()))
                for m in _LANE_CLAIM.finditer(code)):
            out.append(("sweep-before-you-build", SWEEP_STEER))
    except Exception:                          # noqa: BLE001 — fail open
        return out
    return out


# ---------------------------------------------------------------------------
# COMPUTED STEERS on Write and Edit.
# ---------------------------------------------------------------------------

# A new source module: the Write moment of sweep-before-you-build. Tests,
# scratch and non-source files are not modules, and neither is a file outside
# a git work tree.
_SOURCE = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs",
           ".rb", ".java", ".kt", ".scala", ".swift", ".c", ".cc", ".cpp",
           ".h", ".hpp", ".sh", ".bash", ".lua", ".php", ".cs", ".ex", ".exs",
           ".zig")
_TEST_PATH = _Rx(r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]*$"
                 r"|_test\.[^/]+$|\.(?:test|spec)\.[^/]+$")
_SCRATCH = ("/tmp/", "/var/tmp/", "/dev/shm/", "/.claude/", "/.helm/",
            "/.cache/", "/scratchpad/")
SWEEP_STEER = ("New module or new lane: sweep first. Grep the repo for the "
               "noun and the symptom, read the siblings, git log --grep the "
               "concept, then write one line: prior-art: X@file:line -> "
               "build, extend or move.")


def _in_git_tree(path):
    d = os.path.dirname(path)
    for _ in range(40):
        if os.path.exists(os.path.join(d, ".git")):
            return True
        up = os.path.dirname(d)
        if up == d:
            return False
        d = up
    return False


def _new_module(path):
    if not path.endswith(_SOURCE) or os.path.exists(path):
        return False
    if _TEST_PATH.search(path) or any(s in path for s in _SCRATCH):
        return False
    return _in_git_tree(path)


# A script edited in place while a shell runs it: bash reads a script as it
# goes, so the running copy resumes mid-token and blames its caller
# (prior concurrent-edit-of-live-running-script, a measured fab failure).
_SHELLS = (b"sh", b"bash", b"dash", b"zsh")
LIVE_SCRIPT_STEER = ("You just rewrote %s in place while pid %s runs it. bash "
                     "reads a script as it goes, so that run may now execute "
                     "a mangled token and blame its caller. Next time write "
                     "a temp file and mv it over.")


def _is_shell_script(path):
    if path.endswith((".sh", ".bash")):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        return False
    first = head.split(b"\n", 1)[0].rstrip()
    return any(first.endswith(b"/" + s) or first.endswith(b" " + s)
               for s in _SHELLS)


def _runner_of(path, proc="/proc"):
    """A pid of a SHELL whose argv names `path`, or None. Only a shell reads a
    script as it goes, so an editor or a `tail` holding the name is not a
    runner. A relative argument is resolved against that process's own cwd.
    Only called for a shell script that exists, so the /proc walk is paid on
    those edits alone."""
    real = os.path.realpath(path).encode()
    base = os.path.basename(real)
    me = os.getpid()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if not argv or os.path.basename(argv[0]) not in _SHELLS:
            continue
        for arg in argv[1:]:
            if os.path.basename(arg) != base:
                continue
            if not os.path.isabs(arg):
                try:
                    arg = os.path.join(os.readlink(
                        os.path.join(proc, pid, "cwd")).encode(), arg)
                except OSError:
                    continue
            if os.path.realpath(arg) == real:
                return pid
    return None


def write_steers(tool, tool_input, cwd=None):
    """[(steer-id, text)] for one Write or Edit call."""
    out = []
    try:
        path = str((tool_input or {}).get("file_path") or "")
        if not path:
            return out
        if not os.path.isabs(path) and cwd:
            path = os.path.join(cwd, path)
        if tool == "Write" and _new_module(path):
            out.append(("sweep-before-you-build", SWEEP_STEER))
        elif os.path.isfile(path) and _is_shell_script(path):
            pid = _runner_of(path)
            if pid:
                out.append(("live-script-edit", LIVE_SCRIPT_STEER
                            % (_cut(os.path.basename(path), 40), pid)))
    except Exception:                          # noqa: BLE001 — fail open
        return out
    return out


# ---------------------------------------------------------------------------
# RETIRE: the store keyword cells whose moment an act now owns.
#
# Each row names the entry, the rung that now says it, the word cells that
# made it ride every prompt, and (`add`) any symptom probe it gains. What it
# keeps are its few rare symptom probes (heuristic store-keywords-few-short-
# rare: 3-6 short, rare probes; a measurable moment gets a rung). THE ROW
# APPLIES ONLY WHILE THE ENTRY STILL CARRIES EVERY `drop` CELL, the reflex
# REKEYED law: once applied it is a no-op, and an operator who has since
# edited the cells is never overwritten. Applied by `helm sync`
# (cli.cmd_sync), the verb that also applies the reflex re-keys.
# ---------------------------------------------------------------------------
MOVED = (
    {"id": "git-stat-truncates-use-name-only", "rung": "git-stat-grep",
     "drop": ("coverage", "existence", "present", "checks", "status",
              "output", "misses")},
    {"id": "usr-bin-timeout-is-uutils-and-quantises-to-a-100ms-tick",
     "rung": "timeout-timing",
     "drop": ("hooks feel slow", "why is every tool call slow",
              "timeout wrapper cost", "identical benchmark numbers",
              "no regression but identical", "wrapped measurement",
              "per tool call latency", "hook latency", "hooks", "feel",
              "slow", "hooks feel", "feel slow", "tool", "call", "every tool",
              "tool call", "call slow", "timeout", "wrapper", "no regression",
              "benchmark shows no change", "candidate and baseline match",
              "two measurements the same", "before and after are equal",
              "shows no difference")},
    {"id": "concurrent-edit-of-live-running-script", "rung": "live-script-edit",
     "drop": ("running", "editing", "process", "script", "incrementally",
              "concurrent", "unexpected", "executing", "mangled", "dev/null",
              "mtime", "deploy", "atomic", "rename", "in-place", "corrupt",
              "fab", "gate died")},
    {"id": "foreground-command-lock-delays-monitor-events",
     "rung": "foreground-long-on-proxy-seat",
     "drop": ("locking clears", "foreground command", "background command",
              "monitor", "events", "don't", "fire", "monitor events",
              "events don't", "don't fire", "messages", "appeared", "command",
              "finished", "messages appeared", "appeared after",
              "after command", "command finished")},
    {"id": "event-notify-supersedes-paneinject", "rung": "pane-send",
     "drop": ("inotify", "subscription", "notify", "wake", "deprecate",
              "spool", "a2a", "fast-dm", "non-interrupting")},
    {"id": "pgrep-f-matches-your-own-shell", "rung": "pkill-f deny",
     "drop": ("pkill", "pgrep", "kill", "cleanup", "background", "server",
              "process", "exit", "144", "exited", "code", "killed", "shell",
              "self", "command", "died", "vanished"),
     "add": ("exit 144", "exit code 144")},
)


def retire_moved(entries=None, apply=None):
    """Apply MOVED to the live store. -> [(id, state)], state one of
    'applied', 'done' (already applied), 'held' (edited since, or the write
    was refused; left alone) and 'absent'. `entries` and `apply` are seams
    for tests; the defaults are the store's own load and retag. Never
    raises."""
    out = []
    try:
        from . import pk
        if entries is None or apply is None:
            from . import store
            entries = store.load_all() if entries is None else entries
            apply = apply or (lambda row: store.retag(
                row["id"], pk.now_ts(), remove=",".join(row["drop"]),
                add=",".join(row.get("add") or ()) or None))
        by_id = {pk.slug(str(e.get("id") or "")): e for e in entries}
        for row in MOVED:
            e = by_id.get(pk.slug(row["id"]))
            if e is None:
                out.append((row["id"], "absent"))
                continue
            have = {c.strip().lower()
                    for c in str(e.get("keywords") or "").split(",")}
            drop = {c.lower() for c in row["drop"]}
            if not drop & have:
                out.append((row["id"], "done"))
            elif drop <= have:
                _e, err = apply(row)
                out.append((row["id"], "held" if err else "applied"))
            else:
                out.append((row["id"], "held"))
    except Exception:                          # noqa: BLE001 — fail open
        return out
    return out
