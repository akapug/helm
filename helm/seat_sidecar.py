"""The second process a family needs to serve one seat (task/1056).

A family whose catalog entry declares a `sidecar` (today: cursor, whose proxy
routes to a local bridge that speaks Cursor's agent protocol) is TWO processes,
and the proxy half says nothing about the other. A dead bridge behind a live
proxy answers the proxy's port and serves nothing; measured on the owner's box,
the bridge died twice in an hour while every helm surface read the seat as
healthy. So this module does for the sidecar what `seat doctor --ensure` does
for a proxy: it measures it, starts it when it is down, restarts it when it is
wedged, and says in one row what it could not fix.

ONE PROBE DECIDES "SERVING": GET <base_url>/models, the family's own route
and so the port its proxy dials (`seat_catalog.sidecar_port`). An open port is not enough: anything can hold a port. A 2xx is UP.
A refused connection is DOWN. An HTTP answer that is not 2xx is UNSERVING: the
process runs and cannot serve (for cursor, a dead login answers 500 or 502),
and a restart does not refill a credential, so it is reported, never
restarted. No answer at all from a process helm owns is WEDGED once it is past
its start grace, and is restarted. A process helm does not own is never
signalled.

THE BUILD IS CHECKED BEFORE THE PROBE. The catalog pins the one commit it
vetted and the sha256 of each file that carries helm's patches; a checkout
whose HEAD or bytes differ is UNVETTED (`vetting_gaps`), which is never
started and refuses its seat. A running bridge that drifts under helm is
reported and left running: its loaded code is not the file on disk, and the
drift is what the owner must see.

OWNERSHIP IS READ OFF /proc, not a record: the pid in the pidfile is this
seat's sidecar only while its working directory is the vendored artifact and
its argv carries the serve command. That also recognises a bridge the owner
started by hand with `bridge.sh up` (same pidfile, same log, same directory),
and it refuses a recycled pid, whose cwd is somewhere else.

Reads the catalog and the seat paths through the `helm.seat` facade, the door
every non-impl module uses (tests/test_seat_facade_injection.py).
"""
import fcntl
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

UP = "up"
DOWN = "down"
WEDGED = "wedged"
STARTING = "starting"
UNSERVING = "unserving"
FOREIGN = "foreign"
ABSENT = "absent"
UNVETTED = "unvetted"
STALE = "stale"

#: The roster badge per not-UP state; a state missing here renders its token.
BADGE = {DOWN: "BRIDGE DOWN", WEDGED: "BRIDGE WEDGED",
         STARTING: "bridge STARTING", UNSERVING: "BRIDGE NOT SERVING",
         FOREIGN: "BRIDGE PORT HELD", ABSENT: "NO BRIDGE",
         UNVETTED: "BRIDGE UNVETTED", STALE: "BRIDGE STALE"}

#: A just-started sidecar that is not answering yet is starting, not wedged.
STARTUP_GRACE_S = 20.0
#: How long a start waits for the first 2xx before it calls the start failed.
START_WAIT_S = 20.0
#: The spacing between a start's probes while it waits for that 2xx.
START_POLL_S = 1.0
#: How long a stop waits for the pid to go after each signal, and how often
#: it looks: SIGTERM gets STOP_GRACE_S before SIGKILL, and SIGKILL as long.
STOP_GRACE_S = 5.0
STOP_POLL_S = 0.2
#: One probe. The bridge's /models asks Cursor for the model list, which takes
#: a second or two; a probe that waits longer than this is not answering.
PROBE_TIMEOUT_S = 10.0
#: A WEDGED reading is confirmed before any signal, by a second probe this
#: long after the first gave up. The bridge's /models runs a synchronous curl
#: (up to 7 s) on its only event loop, so a probe queued behind another
#: surface's can outwait PROBE_TIMEOUT_S while Cursor is slow; the spacing
#: lets that queue drain, and a process that is really stuck stays silent.
WEDGE_CONFIRM_S = 10.0
#: A listener is STALE when it started before the checkout's newest runtime
#: file changed. /proc gives the start to 10 ms against a boot time kept in
#: whole seconds, so two seconds of slack keep a start in the same second as
#: the checkout from reading stale.
STALE_SLACK_S = 2.0

#: Where a tool the sidecar needs lives when the calling process's PATH does
#: not name it. `seat doctor --ensure` runs from cron with PATH=/usr/bin:/bin,
#: and the cursor bridge runs `node` for every request, so a bridge started
#: there with the inherited PATH answered its probe and failed every turn.
#: `~/.<tool>/bin` is bun's and deno's own install location.
_TOOL_DIRS = ("~/.{tool}/bin", "~/.local/bin", "~/bin", "/usr/local/bin",
              "/home/linuxbrew/.linuxbrew/bin", "/opt/homebrew/bin")


def _seat():
    from . import seat
    return seat


def spec(family):
    """The family's sidecar declaration, or None when it needs none."""
    return (_seat().FAMILIES.get(family) or {}).get("sidecar")


def port(family):
    """The sidecar's port, read off the family's route (one source)."""
    from . import seat                   # the facade first (seat_facade_injection)
    from .seat_catalog import sidecar_port
    return sidecar_port(seat.FAMILIES.get(family))


def label(family):
    """The row name: never a seat name (a slash cannot be one)."""
    return "%s/bridge" % family


def paths(family):
    """Artifact, pidfile, log and lock, all under the family's seat dir."""
    s = spec(family)
    d = _seat().seat_dir(family)
    return {"home": d,
            "artifact": os.path.join(d, s["artifact"]),
            "pidfile": os.path.join(d, s["pidfile"]),
            "log": os.path.join(d, s["log"]),
            "lock": os.path.join(d, ".%s.lock" % s["pidfile"])}


def credentials_path(family):
    return os.path.expanduser(spec(family)["credentials"])


def resolve_tool(name):
    """Absolute path of `name`: PATH first, then `_TOOL_DIRS`; None if none."""
    found = shutil.which(name)
    if found:
        return found
    for pattern in _TOOL_DIRS:
        cand = os.path.join(os.path.expanduser(pattern.format(tool=name)), name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _read_pid(pidfile):
    try:
        with open(pidfile) as f:
            word = f.read().split()
        return int(word[0]) if word else None
    except (OSError, ValueError):
        return None


def _git_head(artifact):
    """The checkout's HEAD commit, read off its .git files (loose ref, then
    packed-refs, or a detached HEAD), or None. No git process: this runs on
    every probe."""
    git = os.path.join(artifact, ".git")
    try:
        with open(os.path.join(git, "HEAD")) as f:
            head = f.read().strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head or None
    ref = head[5:].strip()
    try:
        with open(os.path.join(git, ref)) as f:
            return f.read().strip() or None
    except OSError:
        pass
    try:
        with open(os.path.join(git, "packed-refs")) as f:
            for line in f:
                sha, _sp, name = line.strip().partition(" ")
                if name == ref:
                    return sha
    except OSError:
        pass
    return None


def _sha256(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def runtime_digest(artifact, exclude):
    """(digest, [paths], error): the checkout's runtime build as one sha256,
    over "<path>\\0<sha256 of its bytes>\\n" for every file git lists
    (`ls-files --cached --others --exclude-standard`: tracked, and untracked
    but not ignored) outside the `exclude` prefixes, in path order. Untracked
    files count because a new one can run code (a bunfig.toml can preload a
    script); ignored ones do not (node_modules, the server's own writes).
    The one git question goes through the helm/vcs.py seam."""
    from . import vcs
    rc, out, err = vcs.backend(artifact).run(
        artifact, "ls-files", "-z", "--cached", "--others",
        "--exclude-standard", timeout=10)
    if rc != 0:
        return None, [], "git ls-files exited %s: %s" % (
            rc, os.fsdecode(err or b"").strip()[:200])
    paths = sorted(rel for rel in (os.fsdecode(b) for b in out.split(b"\0"))
                   if rel and not rel.startswith(tuple(exclude)))
    h = hashlib.sha256()
    for rel in paths:
        h.update(("%s\0%s\n" % (rel, _sha256(os.path.join(artifact, rel))
                                or "unreadable")).encode())
    return h.hexdigest(), paths, None


def vetting_gaps(family):
    """The gaps `_vetting` finds (its docstring)."""
    return _vetting(family)[0]


def _vetting(family):
    """([why], [runtime paths]): every way the checkout differs from the build
    the catalog vetted:
    HEAD off the `pin`, a patched file whose bytes are not the vetted ones, or
    any runtime file changed, added or removed (`runtime_digest`). Empty means
    helm may start it and launch a seat on it (task/1124's markers, bound to
    the table rather than to a marker file the checkout could carry itself)."""
    s, p = spec(family), paths(family)
    gaps = []
    head = _git_head(p["artifact"])
    if head != s["pin"]:
        gaps.append("HEAD is %s, the catalog vetted %s"
                    % (head[:12] if head else "unreadable", s["pin"][:12]))
    for rel, want in sorted(s["required_patches"].items()):
        got = _sha256(os.path.join(p["artifact"], rel))
        if got != want["sha256"]:
            gaps.append("%s %s (it carries %s)"
                        % (rel, "is unreadable" if got is None else
                           "is not the vetted bytes", ", ".join(want["patches"])))
    got, files, err = runtime_digest(p["artifact"], s["runtime_exclude"])
    if got != s["runtime_sha256"]:
        gaps.append("the runtime files git lists (%s, tests and docs "
                    "excluded) hash to %s, not the vetted %s; `git -C %s "
                    "status --short` and `git -C %s diff %s` name them"
                    % (err or "%d files" % len(files),
                       got[:12] if got else "nothing", s["runtime_sha256"][:12],
                       p["artifact"], p["artifact"], s["pin"][:12]))
    return gaps, files


def _started_at(pid):
    """Epoch seconds `pid` started, from /proc, or None."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            boot = next(int(ln.split()[1]) for ln in f
                        if ln.startswith("btime "))
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return boot + ticks / float(os.sysconf("SC_CLK_TCK"))


def _serving_refusal(family, files):
    """(state, detail) when the process LISTENING on the bridge port is not
    the vetted checkout at work, else None. The vetting reads bytes on disk;
    the port is served by a process, which may be running other bytes: one
    run from another directory is FOREIGN, and one that started before the
    checkout's newest runtime file changed is STALE (it loaded older bytes).
    Neither is probed, since a probe runs its own /models code, and neither
    is ever signalled."""
    s, p = spec(family), paths(family)
    where = "127.0.0.1:%d" % port(family)
    artifact = os.path.realpath(p["artifact"])
    newest = []
    for rel in files:
        try:
            newest.append(os.path.getmtime(os.path.join(p["artifact"], rel)))
        except OSError:
            pass
    for row in _seat()._port_listeners(port(family)):
        pid = row["pid"]
        try:
            cwd = os.path.realpath(os.readlink("/proc/%d/cwd" % pid))
        except OSError:
            continue                    # it went away mid-read: no claim
        if cwd != artifact:
            return (FOREIGN, "%s is held by pid %d, run from %s, not from the "
                    "checkout %s; not signalled" % (where, pid, cwd, artifact))
        started = _started_at(pid)
        if started is not None and newest \
                and started < max(newest) - STALE_SLACK_S:
            return (STALE, "%s pid %d serving %s started %s, before the "
                    "checkout's runtime files last changed (%s): it runs "
                    "bytes loaded before them. helm never signals it: stop it "
                    "(`kill %d`), then `helm seat up %s` starts the vetted "
                    "build" % (s["name"], pid, where, _iso(started),
                               _iso(max(newest)), pid, family))
    return None


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def unprobeable(family):
    """(state, detail) when the family's bridge must not be probed, else
    None: ABSENT (no checkout), UNVETTED (not the build the catalog vetted),
    FOREIGN or STALE (`_serving_refusal`). A probe runs the serving process's
    own /models code, so nothing unvetted is asked, and none of these is ever
    signalled. The watch's canary asks this before its probe too."""
    s, p = spec(family), paths(family)
    if not os.path.isdir(p["artifact"]):
        return (ABSENT, "%s is not provisioned: no checkout at %s; the "
                "catalog vets commit %s, helm's patches on %s, which the "
                "upstream does not carry" % (s["name"], p["artifact"],
                                             s["pin"][:12], s["origin"]))
    gaps, files = _vetting(family)
    if gaps:
        # THE REMEDY NAMES BOTH SIDES. A checkout BEHIND the pin (the vetted
        # commit is helm's own, on top of the one running) is fixed in the
        # checkout, not in the catalog; and a bridge already running keeps the
        # code it loaded, so it must be restarted once the bytes are vetted.
        return (UNVETTED, "%s at %s is not the build the catalog vetted: %s; "
                "helm neither starts it nor launches a seat on it until the "
                "checkout is the vetted commit (`git -C %s merge --ff-only "
                "%s`, then restart a running bridge so it loads those bytes) "
                "or the catalog's pin and digests are re-vetted"
                % (s["name"], p["artifact"], "; ".join(gaps), p["artifact"],
                   s["pin"]))
    return _serving_refusal(family, files)


def owned_pid(family):
    """The pidfile's pid when it is alive AND is this seat's sidecar, else
    None. A zombie has no cwd and reads as not alive."""
    s, p = spec(family), paths(family)
    pid = _read_pid(p["pidfile"])
    if not pid:
        return None
    try:
        cwd = os.readlink("/proc/%d/cwd" % pid)
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = f.read().split(b"\0")
    except OSError:
        return None
    if os.path.realpath(cwd) != os.path.realpath(p["artifact"]):
        return None
    want = [a.encode() for a in s["argv"]]
    return pid if all(a in argv for a in want) else None


def models_url(family):
    """The probe: the listing under the family's own route."""
    return str(_seat().FAMILIES[family]["base_url"]).rstrip("/") + "/models"


def probe(family, timeout=None):
    """("answered", code) | ("refused", why) | ("silent", why). The timeout
    is read at call time: a default bound at import kept every patched
    PROBE_TIMEOUT_S out of the probe, so the arms' 2 s was a silent 10 s."""
    timeout = PROBE_TIMEOUT_S if timeout is None else timeout
    url = models_url(family)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            return "answered", reply.getcode()
    except urllib.error.HTTPError as exc:
        exc.close()
        return "answered", exc.code
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError):
            return "refused", "connection refused"
        return "silent", "%s" % reason.__class__.__name__
    except (socket.timeout, TimeoutError):
        return "silent", "no answer in %.0fs" % timeout
    except OSError as exc:
        return "silent", exc.__class__.__name__


def _age_s(pidfile):
    try:
        return time.time() - os.path.getmtime(pidfile)
    except OSError:
        return None


def verdict(family):
    """(state, detail, pid) for the family's sidecar; (None, "", None) when
    it declares none."""
    s = spec(family)
    if not s:
        return None, "", None
    p, where = paths(family), "127.0.0.1:%d" % port(family)
    held = unprobeable(family)
    if held:
        return held[0], held[1], None if held[0] == ABSENT \
            else owned_pid(family)
    pid = owned_pid(family)
    kind, what = probe(family)
    if kind == "answered" and 200 <= what < 300:
        who = "pid %d" % pid if pid else "a process started outside helm"
        return UP, "%s serving %s (%s, /models %d)" % (
            s["name"], where, who, what), pid
    if kind == "answered":
        return (UNSERVING, "%s answers %s with HTTP %d: it runs and cannot "
                "serve, and a restart does not fix a refused login or a "
                "refusing upstream; check its credentials at %s and read %s"
                % (s["name"], where, what, credentials_path(family),
                   p["log"]), pid)
    if pid is None:
        if kind == "refused":
            return DOWN, "%s is not listening on %s" % (s["name"], where), None
        return (FOREIGN, "%s holds no pid helm owns, yet %s does not refuse "
                "(%s); not signalled" % (s["name"], where, what), None)
    age = _age_s(p["pidfile"])
    if age is not None and age < STARTUP_GRACE_S:
        return (STARTING, "%s pid %d started %.0fs ago, %s not serving yet "
                "(grace %.0fs)" % (s["name"], pid, age, where,
                                   STARTUP_GRACE_S), pid)
    return (WEDGED, "%s pid %d is alive and %s gives no answer (%s)"
            % (s["name"], pid, where, what), pid)


def _gone(pid):
    """True once `pid` has exited: a zombie, like a reaped pid, has no cwd."""
    try:
        os.readlink("/proc/%d/cwd" % pid)
        return False
    except OSError:
        return True


def _stop(pid):
    """SIGTERM, then SIGKILL (a stopped process takes only the second);
    True once the pid is gone. Called only on a pid `owned_pid` proved."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        for _ in range(max(1, round(STOP_GRACE_S / STOP_POLL_S))):
            if _gone(pid):
                return True
            time.sleep(STOP_POLL_S)
    return False


def _start(family):
    """(ok, detail): start the sidecar and wait for its first 2xx."""
    s, p = spec(family), paths(family)
    runtime = resolve_tool(s["runtime"])
    if runtime is None:
        return False, ("the %s runtime is not on PATH or in %s"
                       % (s["runtime"], ", ".join(_TOOL_DIRS)))
    dirs = [os.path.dirname(runtime)]
    for tool in s["needs"]:
        found = resolve_tool(tool)
        if found is None:
            return False, ("%s needs `%s`, which is not on PATH or in %s; "
                           "it would answer its probe and fail every turn"
                           % (s["name"], tool, ", ".join(_TOOL_DIRS)))
        dirs.append(os.path.dirname(found))
    # AN ALLOWLISTED ENVIRONMENT. The bridge is vendored third-party code, and
    # a start from a pane's shell handed it that pane's whole environment,
    # the seat's own proxy bearer (ANTHROPIC_AUTH_TOKEN) included. It gets
    # PATH, HOME, PORT and the variables its declaration names (`env_keep`).
    env = {name: os.environ[name] for name in s["env_keep"]
           if name in os.environ}
    env["HOME"] = os.environ.get("HOME") or os.path.expanduser("~")
    env["PATH"] = os.pathsep.join(
        list(dict.fromkeys(dirs)) + [os.environ.get("PATH") or os.defpath])
    env["PORT"] = str(port(family))
    argv = [runtime] + list(s["argv"]) + [env["PORT"]]
    try:
        log = open(p["log"], "ab")
    except OSError as exc:
        return False, "cannot open %s: %s" % (p["log"], exc)
    try:
        proc = subprocess.Popen(argv, cwd=p["artifact"], env=env,
                                stdin=subprocess.DEVNULL, stdout=log,
                                stderr=log, start_new_session=True)
    except OSError as exc:
        return False, "%s failed to launch: %s" % (s["name"], exc)
    finally:
        log.close()
    _seat()._write_private(p["pidfile"], "%d\n" % proc.pid, mode=0o644)
    # WAIT FOR A 2xx, NOT FOR THE FIRST ANSWER. Measured on the real bridge:
    # a fresh start's first /models can answer 502 (its first model discovery
    # failing) and the next, seconds later, 200. Taking the first answer
    # called a healthy restart "still unserving" and paged for it.
    deadline = time.monotonic() + START_WAIT_S
    kind, what = "silent", "not probed"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _unlink_if(p["pidfile"], proc.pid)
            return False, ("%s exited rc %s at start; tail %s"
                           % (s["name"], proc.returncode, p["log"]))
        kind, what = probe(family, timeout=5.0)
        if kind == "answered" and 200 <= what < 300:
            break
        time.sleep(START_POLL_S)
    if kind == "answered":
        # a non-2xx at the deadline is a RUNNING bridge that cannot serve:
        # left up, and the verdict after this says UNSERVING with the code
        return True, "pid %d" % proc.pid
    _stop(proc.pid)
    proc.poll()
    _unlink_if(p["pidfile"], proc.pid)
    return False, ("%s pid %d never answered on 127.0.0.1:%d in %.0fs (%s); "
                   "killed it; tail %s" % (s["name"], proc.pid, port(family),
                                            START_WAIT_S, what, p["log"]))


def _unlink_if(pidfile, pid):
    if _read_pid(pidfile) == pid:
        try:
            os.remove(pidfile)
        except OSError:
            pass


class _Lock:
    """One starter at a time per seat: cron and a hand `seat up` can race."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def ensure(family):
    """(state, detail) after making the sidecar serve where helm may.

    "healthy" when it was serving, "respawned" when this call started or
    restarted it and it now serves, "unknown" for every state a restart
    cannot fix or this call could not prove fixed. The same three words
    `_ensure_row` gives a proxy."""
    p = paths(family)
    with _Lock(p["lock"]):
        state, detail, pid = verdict(family)
        if state == WEDGED:
            time.sleep(WEDGE_CONFIRM_S)
            first = detail
            state, detail, pid = verdict(family)
            if state == UP:
                return "healthy", ("%s; a first probe went unanswered (%s) and "
                                   "a second probe %.0fs later was served, so "
                                   "nothing was signalled"
                                   % (detail, first, WEDGE_CONFIRM_S))
        if state == UP:
            return "healthy", detail
        if state not in (DOWN, WEDGED):
            return "unknown", detail
        if state == WEDGED and not _stop(pid):
            return "unknown", detail + "; it survived SIGKILL"
        ok, why = _start(family)
        after, now, _pid = verdict(family)
        if after == UP:
            return "respawned", "%s (it was %s)" % (now, state)
        if not ok:
            return "unknown", "%s; the restart failed: %s" % (detail, why)
        return "unknown", "restarted (%s), and still %s: %s" % (why, after, now)


def ensure_row(family):
    """(label, state, detail) for `seat doctor --ensure`."""
    state, detail = ensure(family)
    return label(family), state, detail


def up(family, quiet=False):
    """0 when the sidecar serves (started here if it was down), else 1 with
    the reason on stderr. `seat up` asks this before it starts the proxy."""
    state, detail = ensure(family)
    if state in ("healthy", "respawned"):
        if state == "respawned" and not quiet:
            print("helm seat: %s up — %s" % (label(family), detail))
        return 0
    print("helm seat: %s is not serving — %s" % (label(family), detail),
          file=sys.stderr)
    return 1


def launch_refusal(family, seat_name, verb):
    """The refusal when the family declares a sidecar that cannot serve after
    one start, else None: a pane launched onto it answers nothing. `seat
    spawn` and `seat resume` ask it before the endpoint, the reap and the
    re-mint, so a refusal spends nothing and leaves a live pane alone."""
    if not spec(family) or up(family, quiet=True) == 0:
        return None
    return ("refusing to %s %s — its %s cannot serve, so the pane would "
            "answer nothing; `helm seat up %s` starts it or says why it "
            "cannot" % (verb, seat_name, label(family), family))


def badge(family, reading=None):
    """(" ⚠ <BADGE>", detail) for a sidecar that is not UP, ("", None) when
    it serves or the family declares none. The roster calls it once a row.
    `reading` is a (state, detail) the caller already measured (the usability
    join's), so one render probes the bridge once; "unreadable" is a probe
    that raised."""
    try:
        state, detail = reading if reading else verdict(family)[:2]
    except Exception as exc:                # noqa: BLE001 — a render never raises
        return (" ⚠ bridge UNKNOWN", "the %s probe raised %s: %s"
                % (label(family), exc.__class__.__name__, exc))
    if state == "unreadable":
        return " ⚠ bridge UNKNOWN", detail
    if state in (None, UP):
        return "", None
    return " ⚠ " + BADGE.get(state, "bridge %s" % state), detail
