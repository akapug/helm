"""Port ownership and proxy-token helpers for :mod:`helm.seat`."""
import os
import shlex
import sys

from . import home

# ---------------------------------------------------------------------------
# WHO holds the port
# ---------------------------------------------------------------------------
# `_port_open` establishes THAT something is listening. It establishes NOTHING
# about WHOSE it is. A refusal that names an owner off that bare bool states a
# conclusion its evidence cannot support — and it did: measured 2026-07-29 on
# seat ds4pro, whose OWN healthy proxy held port 8360 while its proxy.pid
# recorded a dead pid. `_running_pid` failed closed (correct), and the port
# branch then labelled the seat's own proxy "someone else's socket". Worse, the
# refusal was a CLOSED LOOP: `seat spawn` told the operator to run `seat up`,
# and `seat up` hit the same refusal, so a perfectly healthy proxy was declared
# unrecoverable by a remedy that could never succeed.
#
# These readers resolve the owner from /proc alone — no subprocess, no
# third-party module. THE INVARIANT THEY EXIST FOR: every unreadable, racing or
# absent step answers None, and None means UNIDENTIFIED — never "foreign". A
# PermissionError on another user's fd table is not evidence about who holds a
# socket, and rendering it as one would recreate this same bug one layer down.

_TCP_LISTEN = "0A"          # the st column's LISTEN code in /proc/net/tcp{,6}


def _proc_root(proc_root=None):
    return proc_root or home.env("PROC") or "/proc"


def _listen_inodes(port, proc_root=None):
    """Socket inodes LISTENing on `port`, from /proc/net/tcp AND /proc/net/tcp6.

    BOTH tables, because a dual-stack bind appears only in tcp6 and a v4-only
    bind only in tcp — reading one is blind to half the binds on this host (the
    live case binds 127.0.0.1, but no caller may assume that of a sibling).
    State is filtered to LISTEN: an ESTABLISHED row can carry the same local
    port, that row is an accepted CONNECTION, and following its inode would
    name a client as the owner of the bind.

    (proxywatch.inflight reads the same two tables for the opposite rows — the
    01/ESTABLISHED census. They stay separate because they answer different
    questions; neither is a duplicate of the other.)
    """
    root = _proc_root(proc_root)
    inodes = []
    for table in ("net/tcp", "net/tcp6"):
        try:
            with open(os.path.join(root, table), encoding="ascii",
                      errors="replace") as f:
                rows = f.read().splitlines()[1:]     # drop the header line
        except OSError:
            continue                                 # one table down != blind
        for ln in rows:
            cols = ln.split()
            if len(cols) < 10 or cols[3] != _TCP_LISTEN:
                continue
            try:
                if int(cols[1].rsplit(":", 1)[1], 16) != port:
                    continue
                inodes.append(int(cols[9]))
            except (ValueError, IndexError):
                continue
    return inodes


_CENSUS_TABLES = ("net/tcp", "net/tcp6")


def listen_census(port, proc_root=None):
    """(socket inodes LISTENing on `port`, None) when this host's TCP tables can
    ANSWER, else (None, why) naming the table.

    AN UNREADABLE CENSUS IS UNKNOWN, NEVER EMPTY, and that one difference is the
    whole reason this reader stands beside `_listen_inodes` instead of replacing
    it. That helper is a best-effort OWNERSHIP reader: it answers a question
    whose every negative already means UNIDENTIFIED, so skipping a table it
    cannot open costs nothing. An ALLOCATOR asks the opposite question — is this
    endpoint free? — and there an empty list from a table helm could not read is
    a positive claim that nothing is bound, on no evidence. The project-instance
    block is 100 wide and nothing reclaims an entry, so that reading writes a
    durable allocation onto a socket another process is already serving, and the
    new seat's launch line then carries its own bearer token to that proxy.

    A table that is ABSENT is not a table that is unreadable. /proc/net/tcp6
    does not exist on a host with no IPv6, and there are then no IPv6 binds to
    miss — so ENOENT on one table is a known-empty half of the census while any
    OTHER error on it (a permission wall, an I/O failure) hides binds and is
    UNKNOWN. Only a root where NEITHER table exists cannot answer at all.

    A row helm cannot parse is UNKNOWN too, and for the same reason: the
    ownership reader may skip a malformed line because a line it cannot read
    names no owner, but a line it cannot read may name THIS port.
    """
    root = _proc_root(proc_root)
    inodes, readable, absent = [], 0, []
    for table in _CENSUS_TABLES:
        path = os.path.join(root, table)
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                rows = f.read().splitlines()[1:]     # drop the header line
        except FileNotFoundError:
            absent.append(path)
            continue
        except OSError as e:
            return None, ("the LISTEN census cannot read %s (%s) — a TCP table "
                          "helm cannot read hides every bind in it" % (path, e))
        readable += 1
        for offset, ln in enumerate(rows):
            cols = ln.split()
            if not cols:
                continue
            try:
                local = int(cols[1].rsplit(":", 1)[1], 16)
                inode = int(cols[9])
                state = cols[3]
            except (ValueError, IndexError):
                return None, ("the LISTEN census cannot parse %s line %d (%r) "
                              "— a row helm cannot read may be the bind on the "
                              "port it is asking about"
                              % (path, offset + 2, ln[:80]))
            if state == _TCP_LISTEN and local == port:
                inodes.append(inode)
    if not readable:
        return None, ("the LISTEN census found no TCP table under %s (%s) — "
                      "this /proc cannot say what is listening"
                      % (root, ", ".join(absent) or "nothing to read"))
    return inodes, None


def _socket_holders(inodes, proc_root=None):
    """Map each requested socket inode to every readable holder pid."""
    wanted = {int(i): set() for i in inodes}
    if not wanted:
        return wanted
    targets = {"socket:[%d]" % i: i for i in wanted}
    root = _proc_root(proc_root)
    try:
        entries = os.listdir(root)
    except OSError:
        return wanted
    for name in entries:
        if not name.isdigit():
            continue
        fdd = os.path.join(root, name, "fd")
        try:
            fds = os.listdir(fdd)
        except OSError:
            continue        # not ours to read, or already gone — NOT evidence
        for fd in fds:
            try:
                inode = targets.get(os.readlink(os.path.join(fdd, fd)))
                if inode is not None:
                    wanted[inode].add(int(name))
            except OSError:
                continue    # the fd closed between listdir and readlink
    return wanted


def _pids_holding_socket(inodes, proc_root=None):
    """Every readable pid whose fd table holds one of `inodes`.

    The ordinary ownership surface needs at most one candidate and keeps its
    historical first-hit wrapper below. Runtime proof uses `_socket_holders`
    directly so an inode with no readable holder remains UNKNOWN rather than
    disappearing beside one convenient readable sibling.
    """
    holders = _socket_holders(inodes, proc_root)
    return sorted({pid for pids in holders.values() for pid in pids})


def _pid_holding_socket(inodes, proc_root=None):
    """The first readable holder, or None — historical owner-display seam."""
    found = _pids_holding_socket(inodes, proc_root)
    return found[0] if found else None


def _proc_argv(pid, proc_root=None):
    """A process's argv as a list, or None when it cannot be read."""
    try:
        with open(os.path.join(_proc_root(proc_root), str(pid), "cmdline"),
                  "rb") as f:
            raw = f.read()
    except OSError:
        return None
    argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
    return argv or None


def _argv_config(argv):
    """The -config/--config path an argv names, or None. cli-proxy-api takes
    `-config <path>`; the `=` forms are accepted because a hand-written unit
    file may use them and a missed match would misread OUR proxy as foreign."""
    for i, a in enumerate(argv):
        if a in ("-config", "--config"):
            return argv[i + 1] if i + 1 < len(argv) else None
        for prefix in ("-config=", "--config="):
            if a.startswith(prefix):
                return a[len(prefix):] or None
    return None


def _port_listeners(port, proc_root=None, exact=False):
    """Every readable LISTEN owner as {pid, identity, argv, config}.

    `config` is absolute or None: a relative -config is anchored against the
    process's own cwd (never the caller's). With exact=True an inode with no
    readable holder and an owner whose argv vanished each remain as incomplete
    rows, so runtime proof sees UNKNOWN or ambiguity rather than selecting the
    one convenient readable sibling.
    """
    root = _proc_root(proc_root)
    holders = _socket_holders(_listen_inodes(port, root), root)
    out = []
    if exact:
        out.extend({"pid": None, "identity": None, "argv": None,
                    "config": None} for pids in holders.values() if not pids)
    for pid in sorted({pid for pids in holders.values() for pid in pids}):
        argv = _proc_argv(pid, root)
        if argv is None:
            if exact:
                out.append({"pid": pid, "identity": None, "argv": None,
                            "config": None})
            continue             # it went away, or its /proc entry is closed
        config = _argv_config(argv)
        if config and not os.path.isabs(config):
            try:
                config = os.path.join(
                    os.readlink(os.path.join(root, str(pid), "cwd")), config)
            except OSError:
                config = None   # a relative path we cannot anchor is no claim
        out.append({"pid": pid, "identity": _pid_identity(pid), "argv": argv,
                    "config": config})
    return out


def _port_listener(port, proc_root=None):
    """One display/adoption owner, or None when helm could not identify one."""
    found = _port_listeners(port, proc_root)
    return found[0] if found else None


def _same_config(a, b):
    """Do two config paths name the same file? Symlink-resolved on both sides;
    a missing/None left side is never a match."""
    if not a:
        return False
    return os.path.realpath(a) == os.path.realpath(b)


def _adopt_or_refuse_port(seat, port, cfgd):
    """The pre-bound-port decision, once the port is known to be OPEN.

    0 = the listener IS this seat's own proxy and helm adopted it (proxy.pid
    rewritten); 1 = helm refuses. Called under the caller's proxy lock, so the
    adopting write cannot race a concurrent `_up`.
    """
    who = _port_listener(port)
    ours = os.path.join(cfgd, "config.yaml")
    if who is None:
        print("helm seat: port %d has a listener helm COULD NOT IDENTIFY — no "
              "readable owner in /proc (another user's process, or it exited "
              "mid-scan). Refusing to spawn against it because helm does not "
              "know whose it is; this is NOT a claim that it is foreign. "
              "Identify it (`ss -ltnp | grep :%d`) — if it is %s's own proxy, "
              "`helm seat up %s` adopts it once /proc is readable."
              % (port, port, seat, seat), file=sys.stderr)
        return 1
    if not _same_config(who["config"], ours):
        print("helm seat: port %d is held by pid %d, which is NOT %s's proxy — "
              "refusing to spawn against another process's socket. Owner: %s "
              "(%s). Stop it or free port %d, then `helm seat up %s`."
              % (port, who["pid"], seat, " ".join(who["argv"]),
                 "config %s" % who["config"] if who["config"]
                 else "names no config helm can resolve", port, seat),
              file=sys.stderr)
        return 1
    if not who["identity"]:
        print("helm seat: port %d is served by %s's OWN proxy (pid %d, config "
              "%s), but helm could not capture its birth identity — refusing "
              "to record a pidfile it could never authenticate (the process "
              "may have just exited). Retry `helm seat up %s`."
              % (port, seat, who["pid"], who["config"], seat), file=sys.stderr)
        return 1
    # ADOPT. Birth identity captured NOW still defeats a future pid reuse —
    # that is the whole job of the identity field, and it does that job for a
    # pid we adopt exactly as well as for one we spawned. Launch inputs are
    # DELIBERATELY not written: helm did not launch this process, so it cannot
    # know which config bytes it loaded, and half a launch record would let
    # `proxy_drift` certify a stale proxy as CURRENT — the same lie, relocated.
    # A two-field record is the format's own legacy shape, and `proxy_drift`
    # already answers UNKNOWN + "restart once to capture them" for it.
    _write_private(os.path.join(cfgd, "proxy.pid"),
                   "%d %s\n" % (who["pid"], who["identity"]))
    print("helm seat: port %d is served by %s's OWN proxy (pid %d, config %s) "
          "that helm had no valid record of — ADOPTED it and rewrote "
          "proxy.pid. Launch inputs are not recorded for an adopted process, "
          "so drift reads UNKNOWN until the next restart."
          % (port, seat, who["pid"], who["config"]), file=sys.stderr)
    return 0


def _read_token(family, seat=None):
    """The seat's proxy token. Instances mint their own; an instance minted
    before per-instance proxies (or mid-migration) falls back to the family
    token so its launch line stays valid."""
    for d in (_proxy_home(family, seat), seat_dir(family)):
        try:
            with open(os.path.join(d, "token")) as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return None


def _token_file(family, seat=None):
    """The 0600 token file a launch line/script should READ AT EXEC TIME.
    Resolves the bearer in the child shell, so the live token never transits
    the script text, the printed stdout line, or any process argv (the
    no-keys-in-argv gate). For an INSTANCE seat this is ALWAYS the instance's
    own path (instances/<seat>/token) — never an existence-based fallback —
    because launch.sh is written BEFORE `_mint_instance_proxy` runs; pointing
    at the instance path means the script picks up the token the mint writes
    a moment later, and stays correct across every later re-mint (the
    first-mint stale-token finding). Instance 1 (seat == family) uses the
    family file. An UNMINTED-instance launch line (printed for an operator
    before `up`) resolves empty until the mint lands — a clean empty var, not
    the wrong account."""
    return os.path.join(_proxy_home(family, seat), "token")


def _token_export(family, seat=None):
    """The shell statement that puts the seat's bearer into the environ WITHOUT
    it ever touching a process argv: read the 0600 token file into a var and
    `export` it (both shell builtins — no external process, no argv). `env`'s
    NAME=value form is deliberately NOT used: the external env binary would
    carry the resolved secret in its own argv (/proc/pid/cmdline). The launch
    line/script prepend this, then exec claude (which inherits the export).
    2>/dev/null keeps an unminted seat's read a clean empty var."""
    return ("ANTHROPIC_AUTH_TOKEN=$(cat %s 2>/dev/null); export ANTHROPIC_AUTH_TOKEN; "
            % shlex.quote(_token_file(family, seat)))
