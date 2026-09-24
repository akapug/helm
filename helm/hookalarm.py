"""One timeout line per hook class per window — the noise half of the fail-open law.

THE FAIL-OPEN LAW DOES NOT MOVE AND NEITHER DOES ITS ALARM. A per-tool-call
guard that times out still exits 0, and it still SAYS the tool call went
unchecked: a silent fail-open is worse than a loud one, and helm has measured
the cost of a quiet one — four credential homes running unguarded while every
turn looked clean. What was wrong was the RATE. The owner read a seat this morning
where every per-tool-call hook printed its own timeout banner several times
per turn, on every project — the same sentence, the same consequence, a dozen
times before anything else could be read. An alarm nobody can finish reading
is the same silence with extra steps.

SO THE FACT IS KEPT AND THE REPETITION IS NOT. The first timeout of a class
inside a window speaks in full. Every repeat inside that window prints nothing
and increments a counter, and the next line that does speak carries the
arrears — "(+4 suppressed since 18:30Z)" — so the count is never lost, only
deferred.

BOTH HALVES READ THE COUNTER, and the shell half did not. It wrote a byte per
repeat and never drained anything, so for the two banners the owner actually
read — the ones that come from the sh ladder BY CONSTRUCTION, because a
timeout means the helm process that would have printed from Python was killed
— the count was DROPPED, not deferred, while this file and docs/HOOKS.md both
promised otherwise. A promise the code does not keep is worse than no promise:
it stops the reader looking for the number. Each half now drains the keys it
writes, and every arm below compares the two renderings byte for byte.

THE KEYS ARE PER CLASS AND THE HALVES DO NOT SHARE THEM, which is correct and
worth stating because it reads like a bug. The sh ladder keys on the SPEC name
('deliver'), `hookrun` on 'handler-<name>' and `posttoolrun` on
'posttoolrun-<phase>' — three different failures with three different
sentences: the whole wrapper killed, one in-process handler over its budget,
one phase of the composite. Shared keys would make one of them silence the
others. What has to be shared is the FORMAT, so that each half can read back
what it itself wrote after a restart, and that is what is tested.

THE SINCE-STAMP IS MEASURED, NOT ASSUMED. A fixed "in the last 10 min" is a
bound this drain does not enforce: `_drain_older` sums every bucket it can see
with no age limit, so a counter six hours old reads as ten minutes old and the
error always overstates recency. The oldest bucket that contributed is right
there in the filename, on both sides, so the clause names it.

THE STATE FORMAT IS SHARED WITH GENERATED SHELL, which is why it is this
crude. A hook that timed out is DEAD: the process helm would print from has
been killed, so the line comes from the `case` ladder in the wrapper
`hooks.spec_command` writes, and that ladder is POSIX sh with no helm in it.
Both sides therefore agree on files and nothing else:

    <base>/helm-hookalarm-<user>/<key>.<bucket>

  * `bucket` is a UTC clock stamp with its last digit dropped —
    `hw=$(date -u +%Y%m%d%H%M); ${hw%?}` in sh, `strftime(...)[:-1]` in
    Python. It is a STRING on both sides on purpose: see WINDOW_MIN.
  * The file EXISTING means this class already spoke in this window.
  * Its SIZE is how many repeats were suppressed: each repeat appends one byte
    (`printf . >> "$f"`), which sh can do without a fork.

There is no locking and none is wanted. Two hooks racing the same class in the
same window can only make one of them speak twice or one byte go missing; the
cost of either is one extra line, and a lock in a fail-open alarm path is a
new way to wedge a turn.

FAIL-OPEN, LIKE EVERYTHING ON THIS PATH. Any error here — unwritable runtime
dir, a full disk, a hostile file where a directory should be — answers SPEAK.
Losing the state costs a duplicate line; refusing to speak because the counter
broke would lose the unchecked-tool-call fact itself, which is the one thing
this module exists to preserve.
"""
import os
import sys
import time

# TEN MINUTES, AND THE NUMBER IS THE SHELL'S, not a preference. The owner asked
# for "the same turn or within five minutes". A turn has no identifier these
# processes share — the sh ladder sees no session id at all — so the clock is
# the only half both sides can agree on. Five minutes needs an integer
# division, and `$(( … ))` is on helm's OWN `_UNMODELLED` list in hooks.py:
# generating it would make `_segments_ex` abstain on every hook command helm
# writes, and the callers that abstain DISOWN the entry, so helm would stop
# recognising its own installed hooks across the whole estate. A window the
# clock can spell without arithmetic is `date -u +%Y%m%d%H%M` with its last
# digit dropped, which is ten minutes aligned to :00, :10, … — strictly
# quieter than five, and the arrears clause carries every swallowed line
# forward, so nothing is lost by the wider window.
#
# UTC ON BOTH SIDES. Local time would make the two halves disagree the moment
# a hook inherited a different TZ, which splits the state in half and prints
# every line twice.
WINDOW_MIN = 10


def _user():
    """A per-user path token, from the environment both sides can read.

    NOT `getpass.getuser()` and not `os.getuid()`: the sh ladder has neither
    without a fork, and the two sides must land in the SAME directory or the
    suppression state splits in half and every line prints twice.
    """
    return os.environ.get("LOGNAME") or os.environ.get("USER") or "helm"


# THE ONE OVERRIDE, read by both halves. A caller that wants a private window
# — every test arm that exercises this door, and any operator reproducing a
# suppression — names its own directory and gets the real mechanism against
# state nobody else shares.
DIR_ENV = "HELM_HOOK_ALARM_DIR"

# A SUITE IS NOT A FLEET, and this seam was MEASURED into its current shape.
# Suppression is durable state shared across processes — which is exactly what
# makes it useful in production and exactly what makes it poison under a test
# runner, where many arms share one process and one ten-minute window: the
# first arm to blow an alarm silences every later one, and the failure reads as
# a missing diagnostic rather than as a leaked channel. Measured: 19 failures
# and 6 errors across `test_posttoolrun` and `test_hookrun` on the first
# focused run, every one of them an arm asserting a line an earlier arm had
# already spent.
#
# THE FIRST SEAM WAS KEYED ON `HELM_GATE_SUITE_CAP` AND IT DID NOT FIRE. That
# variable is set by helm's gate runner, and a focused `fab test` run is not
# the gate runner — so the condition was true of the environment I imagined and
# false of the one the arms ran in. `unittest in sys.modules` is a fact about
# THIS process rather than about how it was launched, so it holds under the
# gate, under `fab test`, and under anything else that runs these arms. A
# production hook never imports it: the argv-guard's whole module set is
# measured in tests/test_chat_argv_guard.py and unittest is not in it.
#
# FALSE POSITIVE IS THE SAFE DIRECTION. A process that imports unittest for
# some other reason loses the rate limit and prints every line — which is
# exactly the behaviour that shipped before this module existed.
def _under_a_test_runner():
    return "unittest" in sys.modules


def state_dir():
    """The runtime dir both sides write, with no private path in tracked text.

    DIR_ENV first — one explicit answer beats three fallbacks. Then
    XDG_RUNTIME_DIR, because it is per-user and cleared on logout, which is
    exactly the lifetime a suppression counter wants. TMPDIR next, /tmp last —
    and /tmp is shared, so the user token is part of the name rather than a
    subdirectory nobody could create twice.
    """
    named = os.environ.get(DIR_ENV)
    if named:
        return named
    base = (os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("TMPDIR") or "/tmp")
    return os.path.join(base, "helm-hookalarm-" + _user())


def _bucket(now):
    """The window token, spelled the way the sh ladder spells it."""
    return time.strftime("%Y%m%d%H%M", time.gmtime(now))[:-1]


def speak(key, now=None):
    """(should_print, suppressed_before, since) for one alarm class.

    `suppressed_before` counts repeats swallowed in EARLIER windows and is
    non-zero only on a line that is about to print — the arrears travel with
    the next line that speaks, never with one that stays quiet. Those earlier
    counters are removed as they are reported, so no count is ever read twice.
    `since` is the START of the oldest window that contributed, so the clause
    states a bound it measured rather than one it assumed.
    """
    if not os.environ.get(DIR_ENV) and _under_a_test_runner():
        return (True, 0, "")           # see _under_a_test_runner
    now = time.time() if now is None else now
    try:
        d = state_dir()
        os.makedirs(d, exist_ok=True)
        cur = os.path.join(d, "%s.%s" % (key, _bucket(now)))
        try:
            fd = os.open(cur, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # A REPEAT. One byte, appended, no line. The byte is the count the
            # next speaking line will report.
            with open(cur, "ab") as f:
                f.write(b".")
            return (False, 0, "")
        os.close(fd)
        total, oldest = _drain_older(d, key, _bucket(now))
        return (True, total, _since(oldest))
    except Exception:                  # noqa: BLE001 — see the module docstring
        return (True, 0, "")


def _drain_older(d, key, bucket):
    """(repeats, oldest bucket) for windows that have closed, removed as read."""
    total, oldest = 0, ""
    prefix = key + "."
    try:
        names = os.listdir(d)
    except OSError:
        return (0, "")
    for name in sorted(names):
        if not name.startswith(prefix):
            continue
        # THE TOKENS SORT LEXICALLY THE WAY THEY SORT IN TIME — that is what a
        # zero-padded UTC stamp buys, and it is why no parsing happens here. A
        # name this module did not write compares as a foreign string and is
        # simply left alone rather than guessed at. `sorted` is what makes the
        # FIRST surviving name the oldest, which is the same order the sh
        # half's glob expands in — the two halves have to name the same window
        # or the clause they print differs by implementation.
        if name[len(prefix):] >= bucket:
            continue
        path = os.path.join(d, name)
        try:
            size = os.path.getsize(path)
            os.unlink(path)
        except OSError:
            continue
        if size and not oldest:
            oldest = name[len(prefix):]
        total += size
    return (total, oldest)


def _rstrip1(s):
    """`${s%?}` — the shell removes one trailing character, or none from ''."""
    return s[:-1]


def _lstrip(s, n):
    """`${s#????}` — POSIX strips NOTHING when the pattern is longer than the
    value, which is not what a Python slice does. The two halves render this
    clause from the same token and have to agree on every token, including the
    ones neither half writes."""
    return s[n:] if len(s) >= n else s


def _since(bucket):
    """'HH:MMZ' for a window token, spelled the way the sh half spells it.

    The sh half has no strftime and no arithmetic, so it cuts the token with
    parameter expansion: `hs=${hs#????????}; ${hs%?}:${hs#??}0Z`. This mirrors
    those three cuts exactly rather than reimplementing the intent — an
    implementation of the INTENT is how two halves come to disagree about a
    stamp neither of them is wrong about on its own inputs.
    """
    if not bucket:
        return ""
    t = _lstrip(bucket, 8)
    return "%s:%s0Z" % (_rstrip1(t), _lstrip(t, 2))


def arrears(count, since=""):
    """The clause a speaking line appends, or '' when nothing was swallowed."""
    if count <= 0:
        return ""
    if not since:
        return " (+%d suppressed)" % count
    return " (+%d suppressed since %s)" % (count, since)


def line(key, text, now=None):
    """The whole decision for a Python-side alarm: the text to print, or None.

    One door, so a caller cannot take the suppression and forget the arrears —
    the two halves have to move together or the counter is written and never
    read.
    """
    spoke, before, since = speak(key, now=now)
    return (text + arrears(before, since)) if spoke else None


# THE SHELL HALF OF THE SAME FORMAT. Kept here, beside the Python half, because
# they are one contract: a fragment living in hooks.py next to the printf it
# guards would drift from this file the first time a name or a window changed,
# and the failure would be invisible (two directories, every line printed
# twice, nothing broken enough to notice).
#
# `date` and `cat` are the two forks, and both are on the timeout path only —
# a path that has already spent its whole budget, once per class per window.
# `printf` is a shell builtin and every cut below is parameter expansion.
#
# NO SPECIAL BUILT-IN TAKES A REDIRECTION HERE, and that sentence is the whole
# reason this template was rewritten. The first cut created the marker with
# `: > "$hf"`. `:` is a POSIX SPECIAL BUILT-IN, and POSIX says a redirection
# error on one makes a non-interactive shell EXIT — so under /bin/sh (dash,
# which is what this ladder actually runs under on a Debian box) an alarm
# directory that could not be written killed the wrapper at rc 2 with the helm
# line never printed. Measured: HELM_HOOK_ALARM_DIR at an uncreatable path,
# a read-only directory, or a non-directory — dash rc=2, silent, on all three,
# where bash rc=0 and printed. And rc 2 out of a PreToolUse or Stop wrapper is
# BLOCK: a hook that merely TIMED OUT would have become a refused tool call, or
# a turn the agent cannot end, with no sentence naming the hook. That converts
# an internal failure of the ALARM into a refusal the owner cannot diagnose —
# the exact inversion of the fail-open law this module exists to serve.
# `printf ''` is a regular built-in, so the same failure sets $? and the script
# walks on to speak. `{ : ; } > "$hf"` also survives dash and is UNUSABLE:
# `hooks._segments_ex` reports 'brace group', callers that abstain DISOWN the
# entry, and helm would stop recognising the hooks it wrote across the estate.
#
# THE ARREARS ARE READ HERE, not only written. The glob expands in lexical
# order, which for a zero-padded UTC stamp is chronological order, so `set --`
# puts the OLDEST contributing bucket in `$1` for free — no loop, no second
# fork — and `${#hc}` is the total byte count without arithmetic. `$hf` cannot
# be among them: this branch runs only when `[ -e "$hf" ]` said it does not
# exist. An unmatched glob leaves the pattern literal, `cat` fails into
# /dev/null, `hc` is empty and no clause is rendered.
ARREARS_MARK = "@@arrears@@"
ARREARS_SHELL = '"$ha"'

_SHELL = (
    'hd=${HELM_HOOK_ALARM_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}'
    '/helm-hookalarm-${LOGNAME:-${USER:-helm}}}; mkdir -p "$hd" 2>/dev/null; '
    'hw=$(date -u +%%Y%%m%%d%%H%%M); hp=$hd/%s; hf=$hp.${hw%%?}; ha=; '
    'if [ -e "$hf" ]; then printf . >> "$hf" 2>/dev/null; else '
    'set -- "$hp".*; hc=$(cat "$@" 2>/dev/null); rm -f "$@" 2>/dev/null; '
    'if [ -n "$hc" ]; then hs=${1##*.}; hs=${hs#????????}; '
    'ha=" (+${#hc} suppressed since ${hs%%?}:${hs#??}0Z)"; fi; '
    'printf \'\' > "$hf" 2>/dev/null; %s; fi')


def shell_suppressed(key, speaking):
    """`speaking` shell, run only on the first alarm of `key` in this window.

    `speaking` may carry `ARREARS_MARK`; the caller that renders it is
    responsible for splicing that marker to `ARREARS_SHELL`, which is the
    variable this fragment sets. Both names live here because both halves of
    the format do.

    The key must be a bare filename token — it is spliced into a path, and
    every caller passes a spec name helm itself wrote, so this refuses rather
    than quotes anything else: a path separator here would write outside the
    state dir.
    """
    if not key or "/" in key or key.startswith("."):
        raise ValueError("hook alarm key is not a bare filename token: %r" % key)
    return _SHELL % (key, speaking)
