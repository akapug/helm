#!/usr/bin/env python3
"""A PULL THE SEAT CHOSE IS A DELIVERY: `helm chat read` discharges the rows
it printed in full, for the seat it runs as and for no other.

WHY. The beacon doorbell rings with counts and leaves the rows it rang owed to
the tool-boundary hook, and its ring names a pull (`helm chat read --room R
--since K`). That pull moved no cursor, so a seat that ran it and read its
rows was rung again by the backstop every 12 minutes while its hook showed
each of those rows again, one per tool boundary.

WHAT COUNTS AS PRINTED IN FULL. A row inside the read's window (`--since`,
`--limit`) whose whole line reached stdout: a write or a flush that fails ends
the count at the row it cut. And the output must be one a harness shows
whole: a read of at most FULL_BYTES and FULL_LINES counts every row it
printed, and a longer read counts only the rows that end inside its first
HEAD_BYTES, the part a harness shows of a long output. Every other row stays
owed, and the durable log keeps every row reachable. A line that printed its
row in part, a short read's row cut at its stated length (helm.chatshort), is
a `Partial`: it is sized like any line, and its row is not counted.

A FILTER IS NOT A READER. `helm chat read | tail -25` writes every line into
the pipe without an error, and tail drops the front: the rows the seat saw are
not the rows the read printed. So when stdout is a pipe that another process
of this process's session holds open (`filtered`), the read delivers nothing
and says so in one stderr line, which names what does: a later read that is
not filtered (and, when this one was too long to be shown whole or cut a
row, one that prints its rows whole: `FITS`), or an ack of a row the seat
already handled. The session, not
the process group: a pipeline's members share a group only in a shell
without job control, and
`timeout N helm chat read | tail` moves the read into a group of its own
(coreutils timeout calls setpgid) while tail stays in the shell's; both stay
in the session. ACCEPTED: `setsid helm chat read | tail` leaves the session
and its tail is not seen. Scanning every process on the box would see it, at
~100 ms per read on a 1,000-process box (measured), for a shape no read
takes on purpose. The check
runs before the first row is printed, while a `head` is still reading. A
regular file (what Claude Code hands a command) or a tty is read whole, and a
pipe that only this process or an ancestor holds (a harness reading the
command's output itself) is not a filter. A stdout or a process table that
cannot be examined delivers nothing. A pipe also held by a process that only
writes to it (a background job of the same command) reads as a filter too:
the safe direction, since a missed delivery costs a ring and a false one
costs a row.

WHOSE CURSORS. Only the seat the read runs as: the actor helm.actors admits
for this process's session (the door every cursor move takes), and only in a
room where that session already holds its delivery and wake cursors, since a
read never creates one. A read as another seat (`--dm --seat S` naming
someone else) is refused by that door and discharges nothing, and a room this
session holds no cursor in is left alone.

A DELEGATE IS NOT ITS SEAT, and nothing it carries says so: a subagent or a
Workflow agent inherits the seat's session and name, so every door reads its
read as the seat's, and what it prints reaches the delegate's context, never
the seat's. Only the PreToolUse payload's agent_id tells them apart
(actors.SIDECHAIN_RULE), so the argv-guard marks the session at every
delegate Bash or Monitor call whose command RUNS a chat read or ack
(`mark_delegate`, asked by `chat.runs_a_seat_read`: `helm chat read`,
`./bin/helm chat --room R read`, a bare `helm chat`, `python3 -m helm chat
ack ...`), and a read in a session marked within DELEGATE_S discharges
nothing: the tool-boundary hook delivers nothing at a delegate's boundary
for the same reason. The mark cannot say which read is whose, so the seat's
own read inside it discharges nothing either, its rows stay owed to its
hook, and the read says so on stderr (MARKED), as a filtered read does. A
call that only NAMES chat (`sed -n 1,40p tests/test_chat_argv_guard.py`,
`git grep ... -- helm/chat.py`) marks nothing: it once did, and a seat that
runs Workflows had no read delivered, silently, while its ring rang again
at every backstop (task/3696). Nor does a helm invocation holding `--help`
or `-h` (`helm chat read --help`, or a verb a runtime value supplies), which
helm answers before any read or ack runs. A program a runtime value names
(`$H chat read --help`) still marks: nothing proves it is helm, and a program
that ignores the word would read. The MCP chat_read tool never
passes the argv-guard and delivers nothing anyway. ACCEPTED: a delegate
whose command runs `helm chat read` through a program the shell reader does
not read as helm is not marked, the words in the command or not (a script,
`python3 -c`, `watch`, `find -exec`, a here-string to a shell), and its read
delivers to the seat.

THE SAME CROSSING, NOT A SECOND CURSOR. The discharge is the tool-boundary
hook's own pass, `seats_delivery.deliver`, handed the printed rows as `shown`:
it crosses each of them the way it crosses a row it showed, through the same
paired commit, drops their holds from the wake cursor's `held`, emits
nothing, and stops before the first row the seat is still owed that the read
did not print. A printed row past that row keeps the delivery cursor behind
it, and loses its hold: the hook then passes it without showing it, the stop
guard does not count it, and the doorbell reads it as read
(`beacon_doorbell._released`). The beacon census asks what would have woken
the seat, which a pull does not change.
"""
import hashlib
import io
import os
import stat
import sys
import time

from . import chat, home

#: The largest output a harness shows whole, in bytes and in lines.
FULL_BYTES = 10 * 1024
FULL_LINES = 256
#: How much of a longer output a harness shows from its head.
HEAD_BYTES = 2 * 1024
#: Hook passes one discharge may take; each crosses one bounded window.
PASSES = 64
#: How long a delegate's call marks its session: past the longest foreground
#: Bash call a harness runs, so a delegate's read is always inside its mark.
DELEGATE_S = 900.0
#: Why a read inside the mark delivers nothing, as its one stderr line says.
MARKED = ("a delegate of this session (a subagent or a Workflow agent) ran a "
          "chat read or ack within the last %d minutes, and a read cannot "
          "tell its seat's own run from the delegate's")
#: The read that does deliver, by (piped, inside the mark), as the line
#: names it.
DELIVERS = {(False, False): "a read",
            (True, False): "a read whose output is not piped",
            (False, True): "a read once the mark lapses",
            (True, True): "a read whose output is not piped, once the mark "
                          "lapses,"}
#: What that read must print, named when this read printed more than a
#: harness shows whole or cut a row: such a read delivers only the rows at
#: its head (`whole`), and none it cut (`Partial`), piped or not.
FITS = (" if it prints them whole in at most %d KiB and %d lines (`helm chat "
        "read --id <id>` prints one row)" % (FULL_BYTES // 1024, FULL_LINES))


class Partial(str):
    """A read's line that printed its row in part (helm.chatshort cut its
    body). It prints, and `whole` sizes it, exactly as the plain string; the
    discharge does not count its row, which stays owed to the seat's
    tool-boundary hook as a row outside the window does."""


def whole(lines):
    """How many of the printed `lines`, from the first, a harness showed whole.
    A None is a row printed in the line before it (a read folds a run of
    acks into one line, chat.ack_runs): it costs no bytes and no line, and
    it is whole exactly when that line is."""
    sizes = [0 if x is None else len(x.encode("utf-8", "replace")) + 1
             for x in lines]
    if sum(sizes) <= FULL_BYTES \
            and sum(x.count("\n") + 1 for x in lines if x is not None) \
            <= FULL_LINES:
        return len(lines)
    n = total = 0
    for size in sizes:
        total += size
        if total > HEAD_BYTES:
            break
        n += 1
    return n


def _delegate_path(session):
    key = hashlib.blake2b(str(session).encode("utf-8"), digest_size=8)
    return os.path.join(chat.chat_dir(), "pulldelegate." + key.hexdigest())


def mark_delegate(session):
    """A delegate of `session` is running a Bash or Monitor call now (the
    argv-guard read agent_id on its payload). Never raises: the guard runs on
    every call and must fail open."""
    if not session:
        return
    try:
        path = _delegate_path(session)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError:
        pass


def _delegate_marked(session):
    """Did a delegate of `session` run a call within DELEGATE_S? A mark that
    cannot be read answers yes: the discharge then does nothing."""
    try:
        return time.time() - os.stat(_delegate_path(session)).st_mtime \
            < DELEGATE_S
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _stat(proc, pid):
    """(ppid, session) of `pid` from its stat line; the command name may hold
    spaces and parentheses, so the fields are read after its last ')'. Raw
    reads, because the scan reads one per process on the box."""
    fd = os.open(os.path.join(proc, str(pid), "stat"), os.O_RDONLY)
    try:
        rest = os.read(fd, 4096).rsplit(b")", 1)[1].split()
    finally:
        os.close(fd)
    return int(rest[1]), int(rest[3])


def _pipe_holders(st):
    """The pids, other than this process and its ancestors, in this process's
    session that hold the pipe `st` open. Raises OSError when the table
    cannot be read: the ancestry, the listing, or a member of the session
    whose descriptors cannot be listed. A process that exits mid-scan is
    skipped."""
    proc = home.env("PROC") or "/proc"
    mine, pid = set(), os.getpid()
    while pid > 1 and pid not in mine:
        mine.add(pid)
        pid = _stat(proc, pid)[0]
    session, link, out = os.getsid(0), "pipe:[%d]" % st.st_ino, []
    for name in os.listdir(proc):
        if not name.isdigit() or int(name) in mine:
            continue
        try:
            if _stat(proc, name)[1] != session:
                continue
            fds = os.listdir(os.path.join(proc, name, "fd"))
        except (FileNotFoundError, ProcessLookupError):
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(proc, name, "fd", fd)) == link:
                    out.append(int(name))
                    break
            except (FileNotFoundError, ProcessLookupError):
                continue
    return out


def filtered(stream):
    """Why the rows printed to `stream` may not reach the reader whole, or
    None when nothing filters them. A stream with no descriptor is a buffer
    this process reads itself."""
    try:
        fd = stream.fileno()
    except (AttributeError, ValueError, io.UnsupportedOperation):
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISFIFO(st.st_mode):
            return None
        holders = _pipe_holders(st)
    except (OSError, ValueError, IndexError) as exc:
        return "its stdout could not be examined (%s)" % type(exc).__name__
    return ("its output is piped to another program (pid %d), which may not "
            "show every row" % holders[0]) if holders else None


def _snapshot(room):
    """(dev, ino, [(row, start, end)]) for every complete line of the room
    that `chat.read` parses as a row, in its order, from one fstat'd read:
    the byte positions the delivery cursor names."""
    with open(chat.room_path(room), "rb") as f:
        st = os.fstat(f.fileno())
        data = f.read()
    out, pos = [], 0
    for chunk in data.split(b"\n")[:-1]:       # the last piece has no newline
        end = pos + len(chunk) + 1
        row = chat._msg(chunk.decode("utf-8", "replace")) if chunk else None
        if row is not None:
            out.append((row, pos, end))
        pos = end
    return st.st_dev, st.st_ino, out


def discharge(room, printed, claimed=None, session=None, cut=None):
    """Deliver, to the seat this read runs as, the rows `printed` showed whole.

    `printed` is [(index, row, line)] in print order: the row's index in
    `chat.read(room)` and the line the read wrote for it, None for a row the
    read folded into the line before it (`whole`). `claimed` is the
    `--seat` the read named, and `cut` what `filtered` said of stdout before
    the first row was printed. Never raises: a read that cannot discharge
    says so on stderr, and its rows stay owed."""
    try:
        _discharge(room, printed, claimed, session or home.session_id(), cut)
    except Exception as exc:                            # noqa: BLE001
        print("[helm chat] the rows this read printed stay owed (%s: %s)"
              % (type(exc).__name__, exc), file=sys.stderr)


def _discharge(room, printed, claimed, session, cut):
    had_rows = bool(printed)
    first = printed[0][2] if printed else None
    n = whole([line for _i, _row, line in printed])
    fits = n == len(printed)
    printed = printed[:n]
    # a row folded into a Partial line (None) was printed in part with it
    part, keep = False, []
    for i, row, line in printed:
        part = isinstance(line, Partial) if line is not None else part
        if not part:
            keep.append((i, row, line))
    fits = fits and len(keep) == len(printed)
    printed = keep
    if not session or not had_rows:
        return
    # a first row no read shows whole, printed alone or not: no read
    # delivers it, so the notice names the ack rather than a read (FITS)
    never = not printed and not isinstance(first, Partial) \
        and whole([first]) == 0
    from . import actors
    from .seats_cursor import _occurrence
    from .seats_delivery import _cursor, deliver
    actor, err = actors.resolve_actor(session, asserted=claimed,
                                      act="deliver the rows a read printed")
    if err or actor is None:
        return
    seat = actor.canonical_name
    if _cursor(room, seat, session) is None \
            or _cursor(room, seat, session, beacon=True) is None:
        return
    # SAID, NEVER SILENT: a read that delivers nothing says why, so the seat
    # that ran its ring's pull knows the ring will ring again and what ends
    # it (task/3696: a read inside the mark returned without a word, and its
    # seat was rung at every backstop for a row it had read).
    # A read both piped and inside the mark names both, since the unpiped
    # read the pipe alone would name delivers nothing inside the mark either.
    # A read too long to be shown whole, or one that cut a row, names what
    # the read that delivers prints (FITS): run again as it is, it would
    # deliver only its head. One whose FIRST row was not shown whole
    # delivers nothing at all and says so the same way, and when that row
    # alone is longer than any read shows whole, it names the ack instead.
    marked = _delegate_marked(session)
    if cut or marked or not printed:
        why = "; ".join(x for x in (
            cut, marked and MARKED % round(DELEGATE_S / 60),
            not printed and "no printed row was shown whole") if x)
        if never:
            print("[helm chat] this read delivers nothing: %s; no read shows "
                  "a row over %d KiB or %d lines whole, so it stays owed to "
                  "%s's tool-boundary hook until `helm chat ack <id>` clears "
                  "it once you have handled it"
                  % (why, FULL_BYTES // 1024, FULL_LINES, seat),
                  file=sys.stderr)
            return
        print("[helm chat] this read delivers nothing: %s; its rows stay "
              "owed to %s's tool-boundary hook, %s delivers them%s, and "
              "`helm chat ack <id>` clears a row you already handled"
              % (why, seat, DELIVERS[bool(cut), marked],
                 "" if fits else FITS), file=sys.stderr)
        return
    dev, ino, entries = _snapshot(room)
    shown = frozenset(_occurrence(dev, ino, entries[i][1])
                      for i, row, _line in printed
                      if i < len(entries) and entries[i][0] == row)
    at = None
    for _ in range(PASSES):
        cur = _cursor(room, seat, session) or {}
        now = (cur.get("dev"), cur.get("ino"), cur.get("off"))
        if not shown or now == at:
            return
        at = now
        deliver(session=session, room=room, seat=seat, shown=shown)
