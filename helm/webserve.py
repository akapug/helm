#!/usr/bin/env python3
"""WHICH `helm web` SERVERS ARE RUNNING ON THIS HOST, recorded by each server
about itself.

THE HOLE THIS FILLS. Nothing in helm knew a web server existed. `helm doctor`
reasons about "the live `helm web`" only through the board-read record, which
answers whether SOMETHING served the board and can name neither how many
servers are up nor where they are serving from. So a server started against a
lane worktree and left behind after that lane's work ended was invisible: it
went on polling its own endpoints, and the only instrument that could notice
was the load average.

That is not hypothetical. Three servers have run at once on an eight-core host
— the owner's console plus two lane servers nobody had shut down — taking the
load average past four times the core count. Seats lost their injected context
to hook timeouts, and the owner's own turns were among them. Nothing reported
the extra servers because nothing could; they were found only because he said
the box felt slow, and shutting them down halved the load.

WHY A FILE PER SERVER AND NOT ONE REGISTRY FILE. Servers start and stop
independently and share no lock. A single read-modify-write registry would let
two of them race and drop an entry, and a registry that silently loses a server
is worse than none: it would answer "one server" with authority while two ran.
Each server owns exactly one file, named for the port it holds, and writes and
removes only that file, so there is nothing to race over.

A RECORD IS NOT A CLAIM THAT THE SERVER LIVES. A process can die without
removing its file — SIGKILL, a box reset, a full disk. So every record carries
its pid AND that pid's incarnation key, and `live()` asks whether that exact
incarnation is still running before reporting anything. A pid alone would let a
recycled number resurrect a dead server. Where the liveness read merely fails,
the answer is UNKNOWN and the record is kept: an instrument that cannot see is
never evidence of death.

REGISTERING MUST NEVER COST A SERVER. `register` and `forget` swallow their own
failures and answer False. A console that refused to start because it could not
write its own bookkeeping would trade the thing the owner uses for the thing
that describes it.
"""
import json
import os
import time

from . import home, pk

SCHEMA = 1
#: Bounded because they are written into a record an operator reads, and the
#: cut is MARKED so a truncated path can never be mistaken for a real one.
PATH_CHARS = 300


def dir_path():
    return os.path.join(home.helm_home(), "_global", ".state", "web-servers")


def path(port):
    return os.path.join(dir_path(), "%d.json" % int(port))


def _starttime(pid):
    """This process's incarnation key, or None when /proc cannot answer.

    None is honest and usable: a record with no key is treated as
    alive-but-unverifiable rather than as proof of anything."""
    from . import beacons
    return beacons.proc_starttime(pid)


def register(port, root=None, cwd=None, now=None):
    """Record that THIS process is serving `port` -> True when a record landed.

    False means nothing was written. A caller may not read health into it: the
    file is the record, and a server that could not write one still serves.
    """
    now = time.time() if now is None else now
    try:
        pid = os.getpid()
        os.makedirs(dir_path(), exist_ok=True)
        pk.atomic_write(path(port), json.dumps({
            "schema": SCHEMA,
            "port": int(port),
            "pid": pid,
            "pid_start": _starttime(pid),
            "started": now,
            # WHERE IT IS SERVING FROM, which is the whole point of the record:
            # a server whose cwd is a lane worktree is the shape that gets left
            # behind, and a reader cannot tell that from a port number.
            "cwd": pk.cut_marked(cwd if cwd is not None else _safe_cwd(),
                                 PATH_CHARS),
            "root": pk.cut_marked(root, PATH_CHARS) if root else None,
        }), mode=0o600)
    except Exception:          # noqa: BLE001 — bookkeeping never costs a server
        return False
    return True


def forget(port):
    """Remove this port's record -> True when the file is gone afterwards.

    A missing file is success, not an error: the caller's goal is the absence,
    and a server that already unregistered must not report a failure."""
    try:
        os.unlink(path(port))
    except FileNotFoundError:
        return True
    except Exception:          # noqa: BLE001 — see the module docstring
        return False
    return True


def _safe_cwd():
    """The working directory, or "" when it has been deleted under us."""
    try:
        return os.getcwd()
    except OSError:
        return ""


def _records(proc_dir=None):
    """Every parsable record on this host, newest first. Unparsable files are
    COUNTED, never skipped silently — a registry that cannot read itself must
    not answer as though the servers it could not read do not exist."""
    out, unreadable = [], 0
    try:
        names = sorted(os.listdir(dir_path()))
    except OSError:
        return out, unreadable
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = pk.read_json(os.path.join(dir_path(), name), None)
        if not isinstance(rec, dict) or rec.get("schema") != SCHEMA \
                or not isinstance(rec.get("pid"), int) \
                or not isinstance(rec.get("port"), int):
            unreadable += 1
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("started") or 0, reverse=True)
    return out, unreadable


def observed(proc_dir=None):
    """Every `helm web` process ACTUALLY RUNNING, read from /proc.

    WHY THE REGISTRY IS NOT ENOUGH. A registry only knows the servers that
    registered, which means servers started after this module existed. The ones
    that made this necessary were already running, and a check that could not
    see them would have reported a quiet host on the night the box was at load
    34. It is also the only way to see a server whose own record failed to
    write — registering is best-effort by design, so the file's absence is not
    evidence of the server's.

    NOT `pgrep -f`. That matches the full command line of EVERY process
    including the shell running the pgrep, whose argv contains the pattern just
    typed, so it reports itself. This reads /proc directly and skips its own
    pid.

    Returns {pid: {"pid", "cwd", "port"}}. `port` is None when the argv does
    not name one — the server is running on the default, and guessing which
    would invent a fact.
    """
    out, me = {}, os.getpid()
    root = proc_dir or "/proc"
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            with open(os.path.join(root, name, "cmdline"), "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue            # gone, or not ours to read: never a finding
        argv = [a.decode("utf-8", "replace") for a in argv if a]
        if not _is_web_argv(argv):
            continue
        try:
            cwd = os.readlink(os.path.join(root, name, "cwd"))
        except OSError:
            cwd = ""
        out[pid] = {"pid": pid, "cwd": cwd, "port": _argv_port(argv)}
    return out


def _is_web_argv(argv):
    """Is this argv a `helm web`? The verb must FOLLOW a helm entry point, so a
    grep, an editor holding the file open, or this very check's own shell do not
    match on the words alone."""
    for i, a in enumerate(argv[:-1]):
        base = os.path.basename(a)
        if base == "helm" or (base.startswith("python") and "helm" in argv[i + 1:i + 2]):
            rest = argv[i + 1:]
            if rest and rest[0] == "-m":
                rest = rest[2:]
            elif rest and rest[0] == "helm":
                rest = rest[1:]
            return bool(rest) and rest[0] == "web"
    return False


def _argv_port(argv):
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
        if a.startswith("--port="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return None
    return None


def live(now=None, proc_dir=None):
    """What is serving on this host, as a dict.

    `servers` carries only records whose EXACT recorded incarnation is still
    running, each with an `alive` word passed through from the liveness read:
    "live", or "unknown" where the read itself failed. A record whose process
    is gone is dropped from `servers` and counted in `stale`, because a file
    left behind by a killed server is a fact about the past.

    `unreadable` is kept separate from `stale` and from an empty list. Those
    three mean different things — a registry that could not parse a file, a
    server that died without cleaning up, and a host serving nothing — and
    collapsing any pair of them is how a reader concludes "nothing is running"
    from an instrument that simply could not look.
    """
    from . import beacons
    now = time.time() if now is None else now
    recs, unreadable = _records(proc_dir=proc_dir)
    servers, stale = [], 0
    for rec in recs:
        alive = beacons.pid_alive(rec["pid"], rec.get("pid_start"))
        if alive is False:
            stale += 1
            continue
        servers.append({
            "port": rec["port"],
            "pid": rec["pid"],
            "cwd": rec.get("cwd") or "",
            "root": rec.get("root") or "",
            "age_s": max(0.0, now - (rec.get("started") or now)),
            "alive": "live" if alive else "unknown",
        })
    # THE PROCESS TABLE IS THE AUTHORITY, the registry is the detail. A server
    # that is running and never registered still counts, or this check would
    # have reported a quiet host on the night three of them pegged the box.
    seen = {x["pid"] for x in servers}
    unregistered = 0
    for pid, proc in sorted(observed(proc_dir=proc_dir).items()):
        if pid in seen:
            continue
        unregistered += 1
        servers.append({
            "port": proc["port"] if proc["port"] is not None else -1,
            "pid": pid, "cwd": proc["cwd"], "root": "",
            "age_s": None, "alive": "live", "registered": False,
        })
    for x in servers:
        x.setdefault("registered", True)
    return {"dir": dir_path(), "servers": servers, "count": len(servers),
            "stale": stale, "unreadable": unreadable,
            "unregistered": unregistered}
