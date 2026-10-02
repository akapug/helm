"""The hook resident (task/1825): a per-tool-call guard answered by a warm,
forked copy of helm instead of a cold interpreter.

WHY. Every Bash, Monitor, Write, Edit and Agent call on every seat starts a
new interpreter for `helm chat argv-guard`, and that start is most of the
hook: a cold argv-guard costs well over 100 ms of CPU even with the
interpreter floor, and at fleet scale the hook processes alone can hold half
of the agents slice (docs/HOOKS.md, "The hook resident", has the
measurements). A warm process answers the same call for a small fraction of
that.

THE SHAPE, and why each part is what it is:

  bin/helm-hook (sh) ── served argv, interpreter recorded, resident dir ──>
  bin/helm-hookres (bash, starts no python) ── TCP 127.0.0.1 + token ──>
  this resident: accept, fork, the child checks the call and says `ready`;
  on the client's `go` it runs helm's own `cli.main` as the caller and says
  `rc N`.

* FORK PER CALL, NOT A LOOP IN ONE PROCESS. The parent imports helm once and
  never runs a hook itself, so every child starts from the same post-import
  state a cold interpreter reaches: no cache, latch or memo carries from one
  call to the next. Each child can take the caller's environment and cwd
  without touching anyone else's, a crash or a hang costs one call, and calls
  run side by side.

* THE CALLER'S OWN PROCESS STATE, never the resident's. The client sends its
  pid; the child reads that process's environment (`/proc/PID/environ`), cwd,
  umask, and takes its stdin, stdout and stderr as the SAME open files
  (`pidfd_getfd`, else a reopen of a pipe or device through `/proc`). The
  hook reads the harness's payload from the harness's pipe and writes to the
  harness's pipes, so a pass and a refusal are byte for byte what a cold run
  writes, by construction rather than by copying.

* THE PID IS THE SENDER'S, AND STAYS ITS. The child opens one pidfd on the
  named pid first, and refuses unless that process holds this connection's
  client end on its fd 3 (where the client opens it): its socket's inode is
  the one the kernel's socket table gives this connection's peer. A token
  holder cannot name another process. Every read through `/proc/PID` is then
  checked against the same pidfd (the process has not exited, so the number
  was not reused), and fd 2 is taken through it after `go`.

* WHAT A FORK CANNOT CHANGE IS CHECKED, AND A MISMATCH RUNS COLD. A module
  that reads the environment WHILE IT IMPORTS keeps the resident's value; the
  resident records every key read while its modules import and refuses a
  caller whose value differs, as it does for any PYTHON* variable, TZ, the
  locale's character set, another interpreter, another checkout, or code
  older than the tree. The record starts at the FIRST import: by the time
  the resident runs, helm, cli, hooks and hookres are loaded, so their reads
  (helm/__init__ reads HELM_GATE_LOADS_DIR) are taken by a fresh interpreter
  that imports everything this process has loaded with the recorder
  installed before its first line (`_import_env`), beside the in-process
  record of the warm set (`_warm`).

* EXACTLY ONCE, WARM OR COLD, NEVER NEITHER. Every refusal comes before the
  caller's stdin is read, so the client can still run the cold path on the
  untouched payload. The client's `go` is the commit point, and it is the
  CLIENT's: the child holds the caller's stdin and stdout from admission but
  reads and writes them only after `go`, so a resident too slow to say
  `ready` in time can never run the hook beside the cold run the client has
  fallen back to. Between `go` and the hook the child takes fd 2 and becomes
  the caller; a failure there has still read nothing, so it is a refusal and
  the client runs cold. Once the hook runs, a resident that dies is an
  unknown outcome the client names, never a silent pass.

* AUTH, BOTH WAYS: TCP on 127.0.0.1 is reachable by every local user. The
  resident's endpoint file (0600, in a 0700 directory) holds two secrets.
  The TOKEN proves the caller to the resident: a request without it is
  refused before anything is read. The PROOF proves the resident to the
  caller: it rides every answer the resident gives a caller that sent the
  token (`ready PROOF`, `refuse PROOF WHY`), and never an answer to one
  that did not. An endpoint outlives a resident killed without cleanup, and
  anyone may then listen on its port; the client trusts no answer without
  the proof and prints none of its text.

* IT FOLLOWS ITS CODE. The resident is `stopfacts_resident.Follower`'s other
  user: once the tree's digest moves and settles it stops serving, checks the
  new tree imports, and re-execs onto it. Between a land and that poll, each
  call compares the checkout's HEAD witness with the one the resident loaded,
  so a trunk change is refused at once rather than served from older code.

* IT FAILS OPEN, AND SAYS SO. Down, slow, refused or mismatched: the client
  names the reason on stderr and runs exactly the cold path the wrapper
  would have run. No endpoint file (the resident is not serving this
  checkout for this helm home) is silent, and so is an answer without the
  proof (a busy resident refuses before it reads the token, so its refusal
  carries none).

* IT DOES NOT START WHERE IT CANNOT TAKE A SOCKET. Claude Code gives a hook
  sockets for its stdin, stdout and stderr, and only `pidfd_getfd` can take
  a socket from another process (a pipe or a device can be reopened through
  /proc; a socket cannot). Where the kernel refuses pidfd_getfd on a process
  that is not the caller's descendant (kernel.yama.ptrace_scope 1 or more),
  a resident would refuse every such call and only slow it. So at start it
  probes pidfd_getfd against its PARENT, a same-user process that is not its
  descendant (a probe against itself always passes: the kernel never
  refuses a process access to itself), and on a refusal it exits
  HOST_REFUSED with one line saying why, writes no endpoint, and its
  supervisor does not restart it. Every call then runs cold in silence, as
  with no resident; `helm hooks resident --status` names the reason.

* IT BEATS, AND A RESIDENT THAT DOES NOT IS NEVER DIALLED. A resident can be
  alive and read as running (a frozen cgroup, a serve loop stuck in the
  kernel) and still not accept. Its accept queue then fills, a connect
  blocks until the wrapper's budget, and a gate's timeout ALLOWS the call
  unchecked, on every seat at once. So every poll (once a second) writes
  the epoch second to `<endpoint>.beat`, and the client runs cold, named,
  without connecting, when that beat is missing or more than 3 s old. A
  current beat is not proof of an accept: the client bounds its connect too,
  and the parent stops serving on a non-transient accept failure.

WHO STARTS IT. The console `helm web` (the one systemd supervises) runs
`supervise`, which keeps one `helm hooks resident` alive beside it with the
interpreter hooks actually run. So the resident lives in the web unit's
cgroup and stops with it.
"""
import atexit
import errno
import fcntl
import gc
import hmac
import importlib
import json
import os
import secrets
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time

from . import home, stopfacts, stopfacts_resident

#: The wire protocol's name and version: the client's first field.
PROTO = "helm-hookres/1"
#: The hook argv tails (after `<checkout>/bin/helm`) this resident serves.
#: bin/helm-hook tries the resident for exactly these and nothing else, and
#: tests/test_hookres.py holds the two lists to each other.
#:
#: ONLY argv-guard, deliberately. It reads the payload, the caller's
#: environment and cwd, and files; nothing on its path asks what process it
#: is. The PostToolUse composite (`hooks run PostToolUse --installed`) is the
#: next candidate, but the delivery and seat-identity code under it reads
#: this process's ancestry and session (`seats_common`, `pull_delivery`,
#: `beacons`), which a forked child answers with the resident's, so it stays
#: cold until each of those reads takes the caller's pid.
SERVED = frozenset({("chat", "argv-guard", "--hook-json")})
#: What the parent imports before it serves: every helm module a served hook
#: loads on its common paths (a pass, a refusal, the tree and act steers).
#: A module the hook imports that is not here is imported by the child at
#: call time, with the caller's environment, which is correct and slower.
WARM = ("helm.cli", "helm.chat", "helm.hooks", "helm.hooklatency",
        "helm.hookoutcome", "helm.hookalarm", "helm.actors", "helm.actsteer",
        "helm.seats", "helm.seats_cli", "helm.inject", "helm.record",
        "helm.pull_delivery", "helm.delegate_grant", "helm.procage",
        "helm.projscope", "helm.registry", "helm.vcs", "helm.hookwindow")
#: The modules a re-exec's import check loads (`Follower.PREFLIGHT`).
PREFLIGHT = ("helm.cli", "helm.hooks", "helm.hookres") + WARM
#: How often the parent checks its code and reaps children.
POLL_S = 1.0
#: A request must arrive WHOLE within this, however it trickles in, or the
#: child gives up on it (a peer without the token is refused sooner: as soon
#: as its token field arrives).
REQUEST_S = 5.0
MAX_REQUEST = 1 << 16
#: Calls in flight at once; one more is refused and runs cold.
MAX_CHILDREN = 64
#: The supervisor's restart delay, first and longest.
RESTART_S = (5.0, 60.0)
#: The resident's exit status on a host that refuses what it needs
#: (`_host_refusal`): the supervisor does not restart it.
HOST_REFUSED = 4
#: Recorded when a warm import iterated the whole environment, or read the cwd.
ALL = "*"
CWD = "*cwd*"
#: In a child serving a call, the caller's pid: the process the hook stands
#: in for. Code that asks which processes run THIS hook (actsteer's
#: `_own_chain`) takes that process's chain too. None everywhere else.
CALLER = None


class Refused(Exception):
    """A call the child hands back to the cold path, with the reason."""


def _why(exc):
    """A refusal's text for any exception the child meets before the hook."""
    if isinstance(exc, Refused):
        return str(exc)
    return "raised %s before the call" % type(exc).__name__


# ---------------------------------------------------------------------------
# where it is
# ---------------------------------------------------------------------------

def directory():
    """The endpoint directory: `_global/.state/hookres` in the helm home, the
    same spelling bin/helm-hook and the client use."""
    return os.path.join(home.helm_home(), "_global", ".state", "hookres")


def key(bin_dir):
    """The endpoint's file name for a checkout's bin directory. The client
    spells it `${bin//\\//_}`; this is the same substitution."""
    return bin_dir.replace(os.sep, "_")


def own_bin():
    """The bin directory beside the code this process runs, as the installed
    hook names it (the real path of the checkout)."""
    return os.path.join(os.path.dirname(stopfacts.code_root()), "bin")


def endpoint(bin_dir=None):
    return os.path.join(directory(), key(bin_dir or own_bin()))


def _read_endpoint(p):
    """{port, token, pid, starttime, proof} from an endpoint file, or None."""
    try:
        with open(p) as f:
            parts = f.read(512).split()
        return {"port": int(parts[0]), "token": parts[1],
                "pid": int(parts[2]), "starttime": int(parts[3]),
                "proof": parts[4]}
    except (OSError, ValueError, IndexError):
        return None


def status(bin_dir=None):
    """(state, line): "serving", "down" or "absent", and one line saying so.
    Reads the endpoint and the resident's status file; never raises."""
    p = endpoint(bin_dir)
    ep = _read_endpoint(p)
    if ep is None:
        if os.path.exists(p):
            return "down", "hook resident: its endpoint %s cannot be read" % p
        refused = _host_refusal()
        if refused:
            return "absent", ("hook resident: not running for %s, and it does "
                              "not start on this host: %s. argv-guard runs "
                              "cold on every call, as with no resident"
                              % (bin_dir or own_bin(), refused))
        return "absent", ("hook resident: not running for %s (no endpoint at "
                          "%s); argv-guard starts cold on every call. The "
                          "console `helm web` starts it"
                          % (bin_dir or own_bin(), p))
    if not stopfacts._alive(ep):
        return "down", ("hook resident DOWN: its endpoint names pid %d, which "
                        "is gone; every served hook call runs cold and prints "
                        "a line. Restart the console `helm web` "
                        "(systemctl --user restart helm-web)" % ep["pid"])
    info = {}
    try:
        with open(p + ".json") as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    keys = info.get("import_env") or []
    return "serving", ("hook resident: serving %s (pid %d, port %d, python "
                       "%s); import-time environment it holds callers to: %s"
                       % (bin_dir or own_bin(), ep["pid"], ep["port"],
                          info.get("python") or "?",
                          ", ".join(keys) if keys else "none"))


# ---------------------------------------------------------------------------
# the warm imports, and what they read
# ---------------------------------------------------------------------------

class _Reading(os._Environ):
    """`os._Environ` that records the keys read. Installed on `os.environ`
    and `os.environb` (by class, so a module that bound either at import is
    recorded too) only while the warm set imports."""

    seen = None

    def __getitem__(self, name):
        if _Reading.seen is not None:
            _Reading.seen.add(os.fsdecode(name))
        return os._Environ.__getitem__(self, name)

    def __iter__(self):
        if _Reading.seen is not None:
            _Reading.seen.add(ALL)
        return os._Environ.__iter__(self)


def _warm(modules=WARM):
    """Import `modules`; -> the environment keys they read while importing
    (ALL when one walked the whole environment, CWD when one read the cwd)."""
    seen = set()
    real_getcwd = os.getcwd

    def getcwd():
        seen.add(CWD)
        return real_getcwd()

    classes = (os.environ.__class__, os.environb.__class__)
    _Reading.seen = seen
    os.environ.__class__ = _Reading
    os.environb.__class__ = _Reading
    os.getcwd = getcwd
    try:
        for name in modules:
            importlib.import_module(name)
    finally:
        os.environ.__class__, os.environb.__class__ = classes
        os.getcwd = real_getcwd
        _Reading.seen = None
    return frozenset(seen)


#: `_import_env`'s child: the same recorder as `_Reading`, installed before
#: the child's first import of anything it is asked to record.
_PROBE = r"""
import importlib, json, os, sys
seen = set()
real = (os.environ.__class__, os.environb.__class__, os.getcwd)


class Reading(os._Environ):
    def __getitem__(self, name):
        seen.add(os.fsdecode(name))
        return os._Environ.__getitem__(self, name)

    def __iter__(self):
        seen.add(%(all)r)
        return os._Environ.__iter__(self)


def getcwd():
    seen.add(%(cwd)r)
    return real[2]()


spec = json.loads(sys.argv[1])
sys.path[:0] = spec["paths"]
os.environ.__class__ = os.environb.__class__ = Reading
os.getcwd = getcwd
try:
    for name in spec["modules"]:
        importlib.import_module(name)
finally:
    os.environ.__class__, os.environb.__class__, os.getcwd = real
sys.stdout.write("\n" + json.dumps(sorted(seen)) + "\n")
""" % {"all": ALL, "cwd": CWD}


def _import_env(modules, paths=None):
    """(keys, None) or (None, why): the environment keys (ALL, CWD as in
    `_warm`) a FRESH interpreter reads while it imports `modules`, recorded
    from its first import. `paths` go first on its sys.path (default: the
    directory this helm package is in). Run with this interpreter, -S, this
    environment and cwd, so it reads what this process read."""
    import subprocess
    if paths is None:
        paths = (os.path.dirname(stopfacts.code_root()),)
    spec = json.dumps({"modules": list(modules), "paths": list(paths)})
    try:
        r = subprocess.run([sys.executable, "-S", "-c", _PROBE, spec],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, ("could not record what its modules read while "
                      "importing (%s)" % (getattr(exc, "strerror", None)
                                          or type(exc).__name__))
    if r.returncode:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, ("could not record what its modules read while "
                      "importing: %s" % (tail[-1] if tail else
                                         "exit %d" % r.returncode))
    try:
        keys = json.loads(r.stdout.decode("utf-8", "replace")
                          .rstrip("\n").rpartition("\n")[2])
    except ValueError:
        return None, ("could not record what its modules read while "
                      "importing (an unreadable answer)")
    return frozenset(keys), None


def charset(env):
    """The character set Python's standard streams would take from `env`:
    "c" for the C or POSIX locale (which Python coerces), else the codeset
    of LC_ALL, LC_CTYPE or LANG, whichever speaks first."""
    v = env.get("LC_ALL") or env.get("LC_CTYPE") or env.get("LANG") or ""
    if v in ("", "C", "POSIX"):
        return "c"
    code = v.split(".", 1)[1].split("@", 1)[0] if "." in v else v
    return code.lower().replace("-", "").replace("_", "")


def differs(env, env0, keys):
    """The first thing about a caller's environment `env` that the resident
    (started with `env0`, whose warm imports read `keys`) cannot stand in
    for, or None."""
    if ALL in keys:
        return None if env == env0 else "environment (a warm import read all of it)"
    names = [k for k in sorted(keys) if k not in (ALL, CWD)]
    names += sorted({k for k in list(env) + list(env0)
                     if k.startswith("PYTHON")} - set(names))
    for k in names + ["TZ"]:
        if env.get(k) != env0.get(k):
            return k
    if charset(env) != charset(env0):
        return "locale character set (LC_ALL/LC_CTYPE/LANG)"
    return None


# ---------------------------------------------------------------------------
# the caller
# ---------------------------------------------------------------------------

def _environ(pid):
    """(pairs, {str: str}) of a process's environment, first value of a
    duplicated name winning, as CPython builds os.environ."""
    with open("/proc/%d/environ" % pid, "rb") as f:
        raw = f.read()
    pairs, env = [], {}
    for item in raw.split(b"\0"):
        name, eq, value = item.partition(b"=")
        if not eq or not name:
            continue
        k = os.fsdecode(name)
        if k in env:
            continue
        env[k] = os.fsdecode(value)
        pairs.append((name, value))
    return pairs, env


def _umask(pid):
    with open("/proc/%d/status" % pid) as f:
        for line in f:
            if line.startswith("Umask:"):
                return int(line.split()[1], 8)
    raise Refused("its caller's umask could not be read")


_PIDFD_GETFD = 438      # the same number on every Linux architecture
#: libc through ctypes, loaded ONCE by the parent (`_libc`): importing ctypes
#: in every child measured most of a call's cost.
_LIBC = []


def _libc():
    if not _LIBC:
        import ctypes
        _LIBC.append((ctypes, ctypes.CDLL(None, use_errno=True)))
    return _LIBC[0]


def _getfd(pidfd, fd):
    """pidfd_getfd(2): the caller's open file itself, whatever it is."""
    ctypes, libc = _libc()
    got = libc.syscall(_PIDFD_GETFD, pidfd, fd, 0)
    if got < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return got


#: How long the child waits for the client's fd to come back from the
#: socket a builtin briefly redirected it to (bin/helm-hookres writes with
#: `printf ... >&3`, which moves fd 1 for the length of the write).
SETTLE_TRIES, SETTLE_S = 200, 0.0005


def _is_peer(fd, peer):
    """Is `fd` the client's own end of this call's connection?"""
    if not stat.S_ISSOCK(os.fstat(fd).st_mode):
        return False
    probe = socket.socket(fileno=os.dup(fd))
    try:
        return probe.getsockname() == peer
    except OSError:
        return False
    finally:
        probe.close()


#: sock_diag: the kernel's socket table, asked for ONE TCP socket by its
#: four-tuple (NETLINK_SOCK_DIAG, SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST, and the
#: cookie that means "any").
_SOCK_DIAG, _BY_FAMILY, _NLM_F_REQUEST, _NO_COOKIE = 4, 20, 1, 0xFFFFFFFF


def _tcp_inode(local, remote):
    """The inode of the IPv4 TCP socket bound to `local` and connected to
    `remote` (each (host, port)), or None when the kernel has no such
    socket. One netlink request and one reply; no table walk."""
    req = (struct.pack("=BBBBI", socket.AF_INET, socket.IPPROTO_TCP, 0, 0,
                       0xFFFFFFFF)
           + struct.pack("!HH", local[1], remote[1])
           + socket.inet_aton(local[0]) + bytes(12)
           + socket.inet_aton(remote[0]) + bytes(12)
           + struct.pack("=III", 0, _NO_COOKIE, _NO_COOKIE))
    nl = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _SOCK_DIAG)
    try:
        nl.settimeout(1.0)
        nl.sendall(struct.pack("=IHHII", 16 + len(req), _BY_FAMILY,
                               _NLM_F_REQUEST, 1, 0) + req)
        data = nl.recv(8192)
    finally:
        nl.close()
    # nlmsghdr (16 bytes), then inet_diag_msg, whose inode is at byte 68.
    if len(data) < 16 + 72 \
            or struct.unpack_from("=H", data, 4)[0] != _BY_FAMILY:
        return None
    return struct.unpack_from("=I", data, 16 + 68)[0]


def _sent_by(pid, conn):
    """Does process `pid` hold this connection's client end on its fd 3?"""
    try:
        st = os.stat("/proc/%d/fd/3" % pid)
        ino = _tcp_inode(conn.getpeername(), conn.getsockname())
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_ino == ino


def _exited(pidfd):
    """Has the process behind `pidfd` exited? Until it has, its pid names
    it and no other, so a /proc read by number read it."""
    return bool(select.select([pidfd], [], [], 0)[0])


def _host_refusal():
    """None when this process may take another same-user process's open
    files with pidfd_getfd, else why not.

    PROBED AGAINST THE PARENT, never against this process: the kernel never
    refuses a process access to itself, so a self-probe passes everywhere.
    The parent is a same-user process that is not this one's descendant,
    which is what every caller is, and what kernel.yama.ptrace_scope 1 or
    more refuses. EBADF (the parent has no fd 0) means the kernel allowed
    the take."""
    try:
        pidfd = os.pidfd_open(os.getppid())
    except (AttributeError, OSError) as exc:
        return "pidfd_open is not available (%s)" % (
            getattr(exc, "strerror", None) or type(exc).__name__)
    try:
        os.close(_getfd(pidfd, 0))
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return None
        try:
            with open("/proc/sys/kernel/yama/ptrace_scope") as f:
                scope = f.read().strip()
        except OSError:
            scope = "absent"
        return ("the kernel refuses pidfd_getfd on another process (%s; "
                "kernel.yama.ptrace_scope=%s), and a hook's stdio from Claude "
                "Code is sockets, which nothing else can take"
                % (exc.strerror or exc, scope))
    finally:
        os.close(pidfd)
    return None


def _take(pid, pidfd, n):
    """The caller's fd `n` as an fd of this process. Taken whole with
    pidfd_getfd; where that is not permitted, a pipe or a device is reopened
    through /proc (a regular file is not: a reopen gets its own offset and
    would write over what the harness already has)."""
    if pidfd is not None:
        try:
            return _getfd(pidfd, n)
        except OSError:
            pass
    p = "/proc/%d/fd/%d" % (pid, n)
    st = os.stat(p)
    if stat.S_ISSOCK(st.st_mode):
        # Maybe the client's own socket, mid-write: reopening a socket
        # through /proc is not possible, so say "not now" and be asked again.
        return None
    if not (stat.S_ISFIFO(st.st_mode) or stat.S_ISCHR(st.st_mode)):
        raise Refused("its caller's fd %d cannot be shared (pidfd_getfd "
                      "refused, and it is not a pipe or a device)" % n)
    fd = os.open(p, (os.O_RDONLY if n == 0 else os.O_WRONLY)
                 | os.O_NONBLOCK | os.O_CLOEXEC)
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
    return fd


def _take_settled(pid, pidfd, n, peer):
    """The caller's fd `n` as an fd of this process.

    A caller fd that is the client's own end of this call's connection
    (`peer` is its address) is the client mid-write, and is taken again
    until it is not (SETTLE_TRIES x SETTLE_S): a hook reading from it would
    wait on itself forever."""
    for _ in range(SETTLE_TRIES):
        fd = _take(pid, pidfd, n)
        if fd is not None and not _is_peer(fd, peer):
            return fd
        if fd is not None:
            os.close(fd)
        time.sleep(SETTLE_S)
    raise Refused("its caller's fd %d stayed this call's own connection" % n)


def _stdio(pid, pidfd, peer, ns):
    """The caller's fds `ns` as fds of this process, in that order, taken
    while the process behind `pidfd` is still the one `pid` names."""
    out = []
    try:
        for n in ns:
            out.append(_take_settled(pid, pidfd, n, peer))
        if _exited(pidfd):
            raise Refused("its caller, pid %d, exited while its stdio was "
                          "taken" % pid)
    except BaseException:
        for fd in out:
            os.close(fd)
        raise
    return out


def _request(conn, token, deadline):
    """The request's NUL-terminated fields:
    PROTO, token, pid, bin dir, interpreter, argc, argv...

    WHOLE BY `deadline` (a time.monotonic() value): each recv waits only
    what is left of it, so a peer trickling a byte at a time holds a child
    no longer than the request's own bound. The protocol and the `token`
    are judged the moment each field arrives, so a peer without the token is
    refused before it can send more."""
    buf = b""
    while True:
        parts = buf.split(b"\0")
        if len(parts) > 1 and parts[0] != PROTO.encode():
            raise Refused("speaks %s, and the client spoke another protocol"
                          % PROTO)
        if len(parts) > 2 and not (token and
                                   hmac.compare_digest(parts[1], token)):
            raise Refused("refused the call's token (it is not the one in "
                          "the endpoint file)")
        if len(parts) > 6:
            try:
                argc = int(parts[5])
            except ValueError:
                raise Refused("sent a malformed request")
            if not 1 <= argc <= 32:
                raise Refused("sent a malformed request")
            if len(parts) > 6 + argc:
                return parts[:6 + argc]
        if len(buf) > MAX_REQUEST:
            raise Refused("sent a request over %d bytes" % MAX_REQUEST)
        left = deadline - time.monotonic()
        if left <= 0:
            raise Refused("sent no complete request within %gs" % REQUEST_S)
        conn.settimeout(left)
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            raise Refused("sent no complete request within %gs" % REQUEST_S)
        if not chunk:
            raise Refused("sent no complete request")
        buf += chunk


def _line(conn):
    """One short line from the client, without its newline; b"" at EOF."""
    buf = b""
    while not buf.endswith(b"\n") and len(buf) < 64:
        chunk = conn.recv(1)
        if not chunk:
            break
        buf += chunk
    return buf.rstrip(b"\n")


def _send(conn, text):
    try:
        conn.sendall(text.encode("utf-8", "replace"))
    except OSError:
        pass


def _exit_status(code):
    """What the interpreter makes of `sys.exit(code)`."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code & 0xFF
    try:
        sys.stderr.write("%s\n" % (code,))
    except Exception:                        # noqa: BLE001 — as CPython does
        pass
    return 1


def _finish_the_call(_sig, _frame):
    """SIGTERM in a child that has said `ready`: noted by doing nothing."""


def _watch(conn):
    """The client is gone (its budget ran out and `timeout` killed it): end
    this call the way a killed cold hook ends, at once."""
    try:
        conn.recv(1)
    except OSError:
        pass
    os._exit(1)


# ---------------------------------------------------------------------------
# the resident
# ---------------------------------------------------------------------------

class Resident(object):
    """One serving process for one checkout under one helm home."""

    def __init__(self):
        self.sock = None
        self.lock_fd = None
        self.token = None
        self.proof = None
        self.children = set()
        self.stale = False
        self.accept_failed = False
        self.bin_dir = own_bin()
        self.path = endpoint(self.bin_dir)
        self.trace = home.env("HOOK_RESIDENT_TRACE")

    # -- start --------------------------------------------------------------

    def start(self):
        """Take the lock, warm, bind and publish. -> None, or why not."""
        if not sys.flags.no_site:
            return ("start it with -S, the way bin/helm-hook starts the hooks "
                    "it stands in for; a site stage changes what helm imports")
        if None in (sys.stdin, sys.stdout, sys.stderr):
            return "its standard streams must be open (they may be /dev/null)"
        os.makedirs(directory(), mode=0o700, exist_ok=True)
        os.chmod(directory(), 0o700)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
                     0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            ep = _read_endpoint(self.path)
            return "already served (%s)" % ("pid %d" % ep["pid"] if ep
                                            else "by the lock's holder")
        self.lock_fd = fd
        self.follower = Follower(self)
        # EVERY helm module already loaded (the entry chain imported them
        # before this ran) and the warm set, recorded from a fresh
        # interpreter's first line; the warm set also in-process.
        loaded = tuple(sorted(m for m, mod in list(sys.modules.items())
                              if mod is not None
                              and (m == "helm" or m.startswith("helm."))))
        self.keys = _warm()
        first, why = _import_env(loaded + WARM)
        if why:
            self.close()
            return why
        self.keys |= first
        _libc()
        if threading.active_count() != 1:
            self.close()
            return ("a warm import started a thread, and a process with "
                    "threads cannot fork a child safely")
        self.env0 = dict(os.environ)
        self.cwd0 = os.getcwd()
        self.py = os.path.realpath(sys.executable)
        self.root = os.path.dirname(self.bin_dir)
        self.witness = stopfacts.head_witness(self.root)[1]
        self.loaded = stopfacts_resident.LOADED_POLICY
        if sys.stdout.isatty():
            sys.stdout.reconfigure(line_buffering=False)
        gc.freeze()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.token = secrets.token_hex(16)
        self.proof = secrets.token_hex(16)
        port = self.sock.getsockname()[1]
        from . import pk
        pk.atomic_write(self.path + ".json", json.dumps({
            "pid": os.getpid(), "port": port, "python": self.py,
            "code_root": stopfacts.code_root(), "loaded_policy": self.loaded,
            "import_env": sorted(self.keys), "started_at": time.time()},
            sort_keys=True), mode=0o600)
        self._beat()
        pk.atomic_write(self.path, "%d %s %d %d %s\n" % (
            port, self.token, os.getpid(), stopfacts.own_starttime() or 0,
            self.proof), mode=0o600)
        signal.signal(signal.SIGTERM, self._terminated)
        _say("serving %s on 127.0.0.1:%d (pid %d)"
             % (self.bin_dir, port, os.getpid()))
        return None

    def _terminated(self, _sig, _frame):
        self.close()
        os._exit(0)

    def close(self):
        """Stop serving: the socket, the endpoint (only if it is still ours)
        and the lock. Safe to call more than once."""
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        ep = _read_endpoint(self.path)
        if ep is not None and self.token and ep["token"] == self.token:
            for p in (self.path, self.path + ".json", self.path + ".beat"):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None

    def _beat(self):
        """The heartbeat: this second, as epoch seconds, in <endpoint>.beat,
        replaced whole (a client never reads half of it). Never raises: a
        beat that cannot be written goes stale, and clients run cold."""
        tmp = "%s.beat.%d.tmp" % (self.path, os.getpid())
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, b"%d\n" % int(time.time()))
            finally:
                os.close(fd)
            os.replace(tmp, self.path + ".beat")
        except OSError:
            pass

    # -- the loop -----------------------------------------------------------

    def serve(self):
        """Accept and fork until the code moves; never returns normally."""
        next_poll = 0.0
        while True:
            self._reap()
            now = time.monotonic()
            if now >= next_poll:
                self._poll()
                next_poll = now + POLL_S
            if self.sock is None:
                return 3
            try:
                ready, _, _ = select.select([self.sock], [], [],
                                            max(0.0, next_poll - now))
            except InterruptedError:
                continue
            if not ready:
                continue
            try:
                conn, _ = self.sock.accept()
            except OSError as exc:
                if exc.errno in (errno.EINTR, errno.EAGAIN, errno.EWOULDBLOCK,
                                 errno.ECONNABORTED):
                    continue
                # An exhausted descriptor table or broken listener cannot
                # accept, even if the poll keeps writing a fresh heartbeat.
                # Stop advertising this endpoint; cmd() closes it and the
                # supervisor can restart us. Clients also bound connect time.
                self.accept_failed = True
                _say("stopped: cannot accept hook calls (%s)" % exc)
                return 3
            if len(self.children) >= MAX_CHILDREN:
                _send(conn, "refuse is at its limit of %d calls in flight\n"
                      % MAX_CHILDREN)
                conn.close()
                continue
            for stream in (sys.stdout, sys.stderr):
                stream.flush()
            try:
                pid = os.fork()
            except OSError as exc:
                _send(conn, "refuse could not fork (%s)\n" % exc.strerror)
                conn.close()
                continue
            if pid == 0:
                self._child(conn)
            self.children.add(pid)
            conn.close()

    def _reap(self):
        """Reap every finished child, this image's and the image's before a
        re-exec (the same pid, so they are ours to wait for). The only other
        child this process ever has is the re-exec's import check, which
        `subprocess.run` waits for on this same thread."""
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                self.children.clear()
                return
            if not pid:
                return
            self.children.discard(pid)

    def _poll(self):
        """Follow the code: stale while the tree differs from what this
        process loaded; re-exec once it has settled; while it has not moved,
        take the checkout's HEAD witness afresh (a packed-refs rewrite moves
        the witness and not the code).

        THE WITNESS IS READ BEFORE THE WALK. A land writes its files, then
        moves the ref; one that does both while the digest walk runs, to a
        file the walk has already passed, leaves the digest unmoved. A
        witness read after the walk would be the new HEAD, and calls would
        be served from the old code until the next poll. Read before, it is
        at worst the old HEAD, and the next call sees HEAD differ.

        The heartbeat is written first, so the walk's own time never counts
        against it."""
        self._beat()
        witness = stopfacts.head_witness(self.root)[1]
        moved = self.follower.code_moved()
        self.stale = self.follower.stale()
        if moved:
            self.follower.reexec(moved)
            return
        if not self.stale:
            self.witness = witness

    # -- one call, in the child ---------------------------------------------

    def _child(self, conn):
        """Never returns."""
        # BEFORE THE TOKEN MATCHES, NO ANSWER CARRIES THE PROOF: a caller
        # without the token may be anyone, and the proof is what lets the
        # client tell this resident from a listener on a port it left.
        try:
            for fd in (self.sock.fileno(), self.lock_fd):
                os.close(fd)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            fields = _request(conn, (self.token or "").encode(),
                              time.monotonic() + REQUEST_S)
        except BaseException as exc:         # noqa: BLE001 — never the cold path's
            _send(conn, "refuse %s\n" % _why(exc))
            os._exit(0)
        try:
            call = self._admit(conn, fields)
        except BaseException as exc:         # noqa: BLE001 — never the cold path's
            self._refuse(conn, exc)
        # THE HANDSHAKE: `ready`, then the caller's stdio is read or written
        # only once the client has said `go`. A client that gave up first
        # (it was slow to hear `ready`) has already gone cold and closed the
        # connection; this call ends here. NO DEADLINE ON `go`: a client
        # either says it or closes (cold, or killed by its budget), and a
        # child that gave up on a live connection could leave a client that
        # had just said `go` with nobody running its call.
        #
        # FROM `ready` ON, SIGTERM DOES NOT END THIS CALL. The child lives in
        # the resident's cgroup (helm-web.service), and a web restart sends
        # SIGTERM to every process in it: a child killed after `go` is an
        # UNKNOWN outcome, which ALLOWS the call unchecked. So it finishes
        # the call (milliseconds). A Python handler, not SIG_IGN: an ignored
        # signal stays ignored across exec, and the hook's own subprocesses
        # must keep the default. The client's own end still ends it (_watch),
        # and SIGKILL still does.
        signal.signal(signal.SIGTERM, _finish_the_call)
        _send(conn, "ready %s\n" % self.proof)
        try:
            conn.settimeout(None)
            if _line(conn) != b"go":
                os._exit(0)
        except BaseException:                # noqa: BLE001 — the client went
            os._exit(0)
        # AFTER `go`, BEFORE THE HOOK: nothing here reads stdin, so a failure
        # is a refusal and the client runs the call cold. Exactly once.
        try:
            self._become(conn, call)
        except BaseException as exc:         # noqa: BLE001 — cold, named
            self._refuse(conn, exc)
        try:
            rc = self._run(call)
        except BaseException:                # noqa: BLE001 — the client names it
            os._exit(1)
        _send(conn, "rc %d\n" % rc)
        os._exit(0)

    def _refuse(self, conn, exc):
        """Hand the call back to the cold path, proven: the caller sent the
        token, so it may learn the proof it could read in the endpoint.
        Never returns."""
        _send(conn, "refuse %s %s\n" % (self.proof, _why(exc)))
        os._exit(0)

    def _admit(self, conn, fields):
        """Everything that may refuse the call, before its stdin is touched."""
        _proto, _token, pid, bin_dir, py, _argc, *argv = fields
        try:
            pid = int(pid)
        except ValueError:
            raise Refused("sent a malformed request")
        # ONE PIDFD FOR THE WHOLE CALL, opened first: the pid is the sender's
        # (it holds this connection on fd 3), and every /proc read by number
        # below is checked against it, as the fd 2 take after `go` is.
        try:
            pidfd = os.pidfd_open(pid)
        except (AttributeError, OSError) as exc:
            raise Refused("could not hold its caller, pid %d (%s)"
                          % (pid, getattr(exc, "strerror", None) or exc))
        if not _sent_by(pid, conn):
            raise Refused("refused a call naming pid %d, which does not hold "
                          "this connection on its fd 3" % pid)
        bin_dir = os.fsdecode(bin_dir)
        argv = [os.fsdecode(a) for a in argv]
        if bin_dir != self.bin_dir:
            raise Refused("serves %s, not %s" % (self.bin_dir, bin_dir))
        if argv[0] != os.path.join(bin_dir, "helm") \
                or tuple(argv[1:]) not in SERVED:
            raise Refused("does not serve `%s`" % " ".join(argv[1:]))
        py = os.fsdecode(py)
        if os.path.realpath(py) != self.py:
            raise Refused("runs %s, and this hook's interpreter is %s"
                          % (self.py, py))
        if self.stale or stopfacts.head_witness(self.root)[1] != self.witness:
            raise Refused("is running code older than the tree; it re-execs "
                          "onto the new tree within seconds")
        try:
            pairs, env = _environ(pid)
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except OSError as exc:
            raise Refused("could not read its caller's process (%s)"
                          % (exc.strerror or exc))
        what = differs(env, self.env0, self.keys)
        if what:
            raise Refused("was started with a different %s than this caller"
                          % what)
        if CWD in self.keys and cwd != self.cwd0:
            raise Refused("read its cwd while importing, and this caller's "
                          "cwd differs")
        try:
            os.chdir(cwd)
        except OSError as exc:
            raise Refused("cannot enter its caller's cwd (%s)"
                          % (exc.strerror or exc))
        umask = _umask(pid)
        # THE CALLER'S STDIN AND STDOUT ARE TAKEN NOW, while a refusal can
        # still run cold, and KEPT: after `go` the client's own `printf go
        # >&3` points its fd 1 at this socket for as long as the builtin
        # runs, and a client stalled there must not turn a taken call into
        # no call. Its fd 2 is only checked now (the request's write points
        # it at /dev/null for a moment) and taken after `go`, which moves
        # nothing but fd 1.
        peer = conn.getpeername()
        fds = _stdio(pid, pidfd, peer, (0, 1))
        for fd in _stdio(pid, pidfd, peer, (2,)):
            os.close(fd)
        return {"py": py, "argv": argv, "pairs": pairs, "umask": umask,
                "pid": pid, "pidfd": pidfd, "fds": fds}

    def _become(self, conn, call):
        """Become the caller's hook process: its fd 2, then its stdio, umask,
        environment and argv, and the watch on the client. Reads nothing."""
        fds = call["fds"] + _stdio(call["pid"], call["pidfd"],
                                   conn.getpeername(), (2,))
        os.close(call["pidfd"])
        for n, fd in enumerate(fds):
            os.dup2(fd, n)
            os.close(fd)
        os.umask(call["umask"])
        os.environ.clear()
        for name, value in call["pairs"]:
            os.environb[name] = value
        sys.argv = list(call["argv"])
        sys.orig_argv = [call["py"], "-S", "--"] + list(call["argv"])
        global CALLER
        CALLER = call["pid"]
        threading.Thread(target=_watch, args=(conn,), daemon=True).start()

    def _run(self, call):
        """Run the hook as the caller's hook process. -> exit status."""
        if self.trace:
            try:
                with open(self.trace, "a") as f:
                    f.write("served %s\n" % " ".join(call["argv"][1:]))
            except OSError:
                pass
        try:
            from .cli import main
            code = main()
        except SystemExit as exc:
            code = exc.code
        except BaseException:                # noqa: BLE001 — as the interpreter
            sys.excepthook(*sys.exc_info())
            code = 1
        rc = _exit_status(code)
        try:
            atexit._run_exitfuncs()
        except BaseException:                # noqa: BLE001 — as the interpreter
            pass
        try:
            sys.stdout.flush()
        except Exception:                    # noqa: BLE001 — CPython exits 120
            rc = 120
        try:
            sys.stderr.flush()
        except Exception:                    # noqa: BLE001
            pass
        for n in (0, 1, 2):
            try:
                os.close(n)
            except OSError:
                pass
        return rc


class Follower(stopfacts_resident.Follower):
    """The web resident's re-exec (settle, import check, exec), with the
    import check over the modules this process serves from, and a release
    that stops serving BEFORE the import check, so no call waits on it: the
    endpoint is gone, and the client runs cold without a word."""

    PREFLIGHT = PREFLIGHT

    def __init__(self, resident):
        stopfacts_resident.Follower.__init__(self)
        self.resident = resident

    def release(self):
        self.resident.close()

    def stale(self):
        """The tree's digest differs from the code this process loaded."""
        return self._moved is not None

    def reexec(self, digest, execv=None):
        self.resident.close()
        return stopfacts_resident.Follower.reexec(self, digest, execv=execv)


def _say(line):
    try:
        print("helm hooks resident: %s" % line, file=sys.stderr, flush=True)
    except Exception:                        # noqa: BLE001 — a log line only
        pass


def cmd(args):
    """hooks resident [--status] — serve the per-tool-call hooks in
    `SERVED` from a warm process (the console `helm web` keeps one running);
    --status says whether one is serving this checkout."""
    if args == ["--status"]:
        state, line = status()
        print(line)
        return 0 if state == "serving" else 1
    if args:
        print("usage: helm hooks resident [--status]", file=sys.stderr)
        return 2
    refused = _host_refusal()
    if refused:
        print("helm hooks resident: not serving on this host: %s; argv-guard "
              "runs cold on every call, as with no resident" % refused,
              file=sys.stderr)
        return HOST_REFUSED
    r = Resident()
    why = r.start()
    if why:
        print("helm hooks resident: not serving: %s" % why, file=sys.stderr)
        return 0 if why.startswith("already served") else 1
    try:
        rc = r.serve()
    finally:
        r.close()
    if rc and not r.accept_failed:
        _say("stopped: the tree changed and the resident could not re-exec "
             "onto it; hooks run cold until it is started again")
    return rc


# ---------------------------------------------------------------------------
# the supervisor, in the console `helm web`
# ---------------------------------------------------------------------------

def enabled(port=None):
    """Does this `helm web` keep a resident alive? The console port does;
    `HELM_HOOK_RESIDENT` off stops it, on starts it on any port."""
    flag = str(home.env("HOOK_RESIDENT") or "").strip().lower()
    if flag in ("0", "off", "false", "no"):
        return False
    if flag in ("1", "on", "true", "yes"):
        return True
    from .web_common import DEFAULT_PORT
    return port == DEFAULT_PORT


def interpreter():
    """The interpreter the hooks run (bin/helm-hook's `hook-interp` record:
    the entry for this PATH's python3, else the newest), else this one."""
    import shutil
    rec = os.path.join(home.helm_home(), "_global", ".state", "hook-interp")
    try:
        with open(rec) as f:
            rows = [ln.rstrip("\n").split("\t") for ln in f
                    if ln.count("\t") == 1]
    except OSError:
        rows = []
    mine = shutil.which("python3")
    pick = [exe for k, exe in rows if k == mine] or [exe for _, exe in rows]
    for exe in reversed(pick):
        if os.path.isabs(exe) and os.access(exe, os.X_OK):
            return exe
    return sys.executable


def _held():
    """Is the resident's lock held by someone? Taking it answers; it is let
    go at once."""
    p = endpoint() + ".lock"
    try:
        os.makedirs(directory(), mode=0o700, exist_ok=True)
        fd = os.open(p, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def _supervise_loop(poll_s, stop=None):
    """Keep one resident alive: start it whenever nobody holds its lock, and
    restart it after a backoff when it exits. A resident that exits
    HOST_REFUSED is never restarted (the host refuses it every time): this
    says so once and returns. `stop` (an Event) ends the loop."""
    import subprocess
    stop = stop or threading.Event()
    delay = RESTART_S[0]
    while not stop.is_set():
        if _held():
            stop.wait(poll_s)
            continue
        argv = [interpreter(), "-S", os.path.join(own_bin(), "helm"),
                "hooks", "resident"]
        began = time.monotonic()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    cwd=os.path.dirname(own_bin()),
                                    env=dict(os.environ,
                                             HELM_NO_TREE_WARNING="1"))
            proc.wait()
        except OSError as exc:
            print("helm web: the hook resident could not start (%s)"
                  % (exc.strerror or exc), file=sys.stderr, flush=True)
        else:
            if proc.returncode == HOST_REFUSED:
                print("helm web: the hook resident does not run on this host "
                      "(`helm hooks resident --status` says why); argv-guard "
                      "runs cold, as with no resident", file=sys.stderr,
                      flush=True)
                return
        if time.monotonic() - began > RESTART_S[1]:
            delay = RESTART_S[0]
        stop.wait(delay)
        delay = min(RESTART_S[1], delay * 2)


def supervise(port=None, poll_s=5.0):
    """Keep one hook resident alive beside this `helm web` (console port by
    default). -> the supervising thread, or None. Never raises."""
    try:
        if not enabled(port):
            return None
        t = threading.Thread(target=_supervise_loop, args=(poll_s,),
                             name="helm-hook-resident", daemon=True)
        t.start()
        return t
    except Exception:                        # noqa: BLE001 — never the server
        return None

