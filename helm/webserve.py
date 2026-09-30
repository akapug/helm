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

WHAT EACH SERVER COSTS, AND WHOSE IT IS (task/3715). A server started by hand
from a lane worktree runs under no unit, so under no memory limit: one left
polling from a single open tab reached 4.5 GB in about 54 minutes. So `live`
also reads, per server, from /proc: its resident and swapped memory (RSS alone
hid 1.3-1.9 GB of the console's swap), the systemd unit it runs in (the owner's
console is the helm-web unit; any other server is ad hoc), its age when no
record states one, and the TREE it serves, meaning the checkout whose code it
runs. That is the record's `tree`, else the checkout of the `bin/helm` its
command line names. Each is None when it cannot be read, never a guess.
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


def _this_tree():
    """The checkout whose helm package this process imported."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def register(port, root=None, cwd=None, now=None, tree=None):
    """Record that THIS process is serving `port` -> True when a record landed.

    False means nothing was written. A caller may not read health into it: the
    file is the record, and a server that could not write one still serves.
    `tree` is the checkout whose code it serves, this module's own by default.
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
            "tree": pk.cut_marked(tree or _this_tree(), PATH_CHARS),
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
        out[pid] = dict(proc_facts(pid, proc_dir), pid=pid, cwd=cwd,
                        port=_argv_port(argv), tree=_argv_tree(argv, cwd))
    return out


def _argv_tree(argv, cwd):
    """The checkout a `helm web` command line runs: the tree of the
    `<tree>/bin/helm` it names (a relative one read from `cwd`, a link
    followed), or `cwd` for `python -m helm`. None when neither says."""
    for i, a in enumerate(argv):
        if os.path.basename(a) == "helm" and argv[i + 1:i + 2] == ["web"]:
            if os.sep not in a:
                return None     # found on PATH: which one is not recorded
            bindir = os.path.dirname(os.path.realpath(
                os.path.join(cwd or os.sep, a)))
            tree = os.path.dirname(bindir) \
                if os.path.basename(bindir) == "bin" else None
            break
        if a == "-m" and argv[i + 1:i + 2] == ["helm"]:
            tree = cwd or None
            break
    else:
        return None
    # A CHECKOUT CARRIES ITS PACKAGE. A `bin/helm` that is a wrapper script
    # rather than a link into a checkout names a directory that is not one.
    return tree if tree and os.path.isfile(
        os.path.join(tree, "helm", "__init__.py")) else None


def proc_facts(pid, proc_dir=None):
    """What /proc says one process costs and runs under ->
    {"rss_bytes", "swap_bytes", "unit", "age_s"}, each None when unreadable.

    `unit` is the systemd unit whose cgroup holds it (the `.service` its
    cgroup path ends in), None for a process in a scope or no unit at all.
    `age_s` is its start (/proc/<pid>/stat field 22) against /proc/uptime."""
    from . import beacons, procage
    out = {"rss_bytes": None, "swap_bytes": None, "unit": None, "age_s": None}
    raw, err = beacons._read(pid, "status", proc_dir)
    for line in ([] if err else raw.decode("utf-8", "replace").splitlines()):
        key, _, value = line.partition(":")
        field = {"VmRSS": "rss_bytes", "VmSwap": "swap_bytes"}.get(key)
        words = value.split()
        if field and words and words[0].isdigit():
            out[field] = int(words[0]) * 1024
    raw, err = beacons._read(pid, "cgroup", proc_dir)
    for line in ([] if err else raw.decode("utf-8", "replace").splitlines()):
        leaf = line.rsplit(":", 1)[-1].rstrip("/").rsplit("/", 1)[-1]
        if leaf.endswith(".service"):
            out["unit"] = leaf
    out["age_s"] = procage.process_age(pid, proc_dir=proc_dir)
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


def _ui_tree(root):
    """<tree> for a recorded UI directory <tree>/helm/web_ui, else None."""
    parts = os.path.normpath(root).split(os.sep)[-2:] if root else []
    return os.path.dirname(os.path.dirname(os.path.normpath(root))) \
        if parts == ["helm", "web_ui"] else None


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
        alive = beacons.pid_alive(rec["pid"], rec.get("pid_start"), proc_dir)
        if alive is False:
            stale += 1
            continue
        facts = proc_facts(rec["pid"], proc_dir)
        servers.append({
            "port": rec["port"],
            "pid": rec["pid"],
            "cwd": rec.get("cwd") or "",
            "root": rec.get("root") or "",
            "age_s": max(0.0, now - (rec.get("started") or now)),
            "alive": "live" if alive else "unknown",
            # A RECORD FROM BEFORE `tree` WAS RECORDED still names its UI
            # directory, which sits at <tree>/helm/web_ui.
            "tree": rec.get("tree") or _ui_tree(rec.get("root")),
            "rss_bytes": facts["rss_bytes"],
            "swap_bytes": facts["swap_bytes"],
            "unit": facts["unit"],
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
            "age_s": proc.get("age_s"), "alive": "live", "registered": False,
            "tree": proc.get("tree"), "rss_bytes": proc.get("rss_bytes"),
            "swap_bytes": proc.get("swap_bytes"), "unit": proc.get("unit"),
        })
    for x in servers:
        x.setdefault("registered", True)
    return {"dir": dir_path(), "servers": servers, "count": len(servers),
            "stale": stale, "unreadable": unreadable,
            "unregistered": unregistered}
