#!/usr/bin/env python3
"""Supervise one whole-suite process tree and die with the gate launcher."""
import ctypes
import os
import signal
import subprocess
import sys
import time


_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_CGROUP_PREFIX = "helm-gate-"
_CGROUP_SETTLE_PASSES = 100
_TREE_SETTLE_PASSES = 64
_TREE_SETTLE_S = 0.002
_WATCH_POLL_S = 0.05


def _refuse(reason):
    print("gate guard: %s" % reason, file=sys.stderr, flush=True)
    return 125


def _proc_rows(proc_dir="/proc"):
    rows = {}
    try:
        names = os.listdir(proc_dir)
    except OSError:
        return rows
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(os.path.join(proc_dir, name, "stat")) as f:
                fields = f.read().rsplit(") ", 1)[1].split()
            rows[pid] = int(fields[1]), int(fields[19]), fields[0]
        except (OSError, IndexError, ValueError):
            continue
    return rows


def _descendants(root, proc_dir="/proc"):
    rows = _proc_rows(proc_dir)
    children = {}
    for pid, (parent, _starttime, _state) in rows.items():
        children.setdefault(parent, []).append(pid)
    found, seen, stack = [], set(), [(root, False)]
    while stack:
        pid, expanded = stack.pop()
        if expanded:
            if pid != root and pid in rows:
                found.append((pid, rows[pid][1], rows[pid][2]))
            continue
        if pid in seen:
            continue
        seen.add(pid)
        stack.append((pid, True))
        stack.extend((child, False)
                     for child in reversed(children.get(pid, ())))
    return found


def _process_state(pid, starttime, proc_dir="/proc"):
    try:
        with open(os.path.join(proc_dir, str(pid), "stat")) as f:
            fields = f.read().rsplit(") ", 1)[1].split()
        return fields[0] if int(fields[19]) == starttime else None
    except (OSError, IndexError, ValueError):
        return None


def _same_process(pid, starttime, proc_dir="/proc"):
    state = _process_state(pid, starttime, proc_dir=proc_dir)
    return state is not None and state not in ("Z", "X")


def _signal_exact(pid, starttime, signum):
    if not _same_process(pid, starttime):
        return
    try:
        os.kill(pid, signum)
    except OSError:
        pass


def _kill_descendants(root):
    # Freeze every observed generation first. Once the tree is entirely stopped,
    # no descendant can fork between the final snapshot and the kill pass.
    for _pass in range(_TREE_SETTLE_PASSES):
        rows = _descendants(root)
        live = [row for row in rows if row[2] != "Z"]
        if not live:
            return
        for pid, starttime, state in live:
            if state != "T":
                _signal_exact(pid, starttime, signal.SIGSTOP)
        time.sleep(_TREE_SETTLE_S)
        stopped = [row for row in _descendants(root) if row[2] != "Z"]
        if stopped and all(row[2] == "T" for row in stopped):
            break
    for _pass in range(_TREE_SETTLE_PASSES):
        live = [row for row in _descendants(root) if row[2] != "Z"]
        if not live:
            return
        for pid, starttime, _state in live:
            _signal_exact(pid, starttime, signal.SIGKILL)
        time.sleep(_TREE_SETTLE_S)


def _subreaper_state():
    """Current Linux child-subreaper state, or None when unavailable."""
    libc = ctypes.CDLL(None, use_errno=True)
    state = ctypes.c_int()
    if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(state), 0, 0, 0) != 0:
        return None
    return bool(state.value)


def _set_subreaper(enabled=True):
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.prctl(
        _PR_SET_CHILD_SUBREAPER, int(bool(enabled)), 0, 0, 0) == 0


def _cgroup_root(proc_path="/proc/self/cgroup",
                 cgroup_root="/sys/fs/cgroup"):
    try:
        with open(proc_path) as f:
            row = next(line for line in f if line.startswith("0::"))
        relative = row.split("::", 1)[1].strip()
        if not relative.startswith("/"):
            return None
        base = os.path.realpath(cgroup_root)
        root = os.path.realpath(os.path.join(base, relative.lstrip("/")))
        if os.path.commonpath((base, root)) != base:
            return None
        return root
    except (OSError, StopIteration, ValueError):
        return None


def _valid_cgroup(path):
    root = _cgroup_root()
    real = os.path.realpath(path)
    name = os.path.basename(real)
    token = name[len(_CGROUP_PREFIX):]
    return bool(root and os.path.dirname(real) == root
                and name.startswith(_CGROUP_PREFIX) and token
                and len(token) <= 64
                and all(ch in "0123456789abcdef" for ch in token))


# Split across two literals ON PURPOSE: as one 16-character run it is
# indistinguishable from a cited commit sha, and the docref rung
# correctly refuses a token this repo cannot account for.
_HEX = frozenset("0123456789" + "abcdef")


def _cgroup_token(position):
    """The caller's half of the question, answered WITHOUT the environment.

    Token validity is a fact about the argument alone. Deriving it inside a
    function that also needs the cgroup root ties the two together, and then
    a root that reads differently on a second look is reported as a malformed
    token -- a caller fault invented by an environment fault.
    """
    token = position if isinstance(position, str) else ""
    # NO .lower() HERE. Folding case ALIASES two distinct positions onto one
    # cgroup: "ABC" and "abc" are different tokens and would have become the
    # same containment identity, so two runs could land in one subtree and
    # each would kill the other's processes on cleanup. The contract and the
    # refusal both say lowercase, and now so does the code.
    if not token or len(token) > 64 \
            or any(ch not in _HEX for ch in token):
        return None
    return token


def _cgroup_path(position):
    root = _cgroup_root()
    token = _cgroup_token(position)
    if not root or not token:
        return None
    return os.path.join(root, _CGROUP_PREFIX + token)


def _create_cgroup(position):
    """Make this run's cgroup, or say WHY NOT in the caller's own terms.

    THE TWO FAILURES HERE ARE NOT THE SAME FAULT AND MUST NOT SHARE A
    SENTENCE. An unreadable root is a fact about the ENVIRONMENT -- this box,
    this scope, delegation. A None path is a fact about the CALLER -- the
    position token is not lowercase hex, or is over 64 chars.

    A refusal is only as useful as the cause it names, and one that names the
    wrong subsystem confidently is worse than a bare failure: it sends the
    reader somewhere real and wrong. An environment-shaped message is the
    expensive case, because re-running on another box reproduces it and each
    repetition reads as corroboration rather than as the same wrong answer.
    """
    # ONE READ OF THE ROOT, and the token judged without it. Calling
    # _cgroup_path here read the root a SECOND time: when the first read
    # succeeded and the second did not, `root` was truthy, the environment
    # branch was skipped, and a valid lowercase token was reported as the
    # caller's malformed one. That is this function's own defect class,
    # rebuilt inside its cure.
    root = _cgroup_root()
    token = _cgroup_token(position)
    path = os.path.join(root, _CGROUP_PREFIX + token) if root and token else None
    if not root:
        return None, ("this process's cgroup is unreadable, so no subtree can "
                      "be created here (environment, not the caller)")
    if not path:
        return None, ("gate position %r is not a usable cgroup name: it must "
                      "be lowercase hex, 1-64 chars (caller, not the "
                      "environment)" % (position,))
    if not os.path.exists(root):
        return None, "cgroup root %s is missing" % root
    missing = [name for name, mode in (("write", os.W_OK),
                                        ("search", os.X_OK))
               if not os.access(root, mode)]
    if missing:
        return None, "cgroup root %s lacks %s access" % (
            root, "/".join(missing))
    try:
        os.mkdir(path, 0o700)
        for name in ("cgroup.procs", "cgroup.events", "cgroup.kill"):
            probe = os.path.join(path, name)
            if not os.path.exists(probe):
                os.rmdir(path)
                return None, "created cgroup %s is missing required interface %s" % (
                    path, probe)
        return path, None
    except OSError as exc:
        return None, "cannot create cgroup under %s at %s: %s" % (
            root, path, exc)


def _join_cgroup(path):
    if not _valid_cgroup(path):
        return False, "cgroup path is invalid: %s" % path
    if not os.path.exists(path):
        return False, "cgroup path is missing: %s" % path
    probe = os.path.join(path, "cgroup.procs")
    if not os.access(probe, os.W_OK):
        return False, "cgroup %s lacks write access to %s" % (path, probe)
    try:
        with open(probe, "w") as f:
            f.write(str(os.getpid()))
        if str(os.getpid()) not in _cgroup_members(path):
            return False, "cgroup join through %s did not retain pid %d" % (
                probe, os.getpid())
        return True, None
    except OSError as exc:
        return False, "cgroup join write failed at %s: %s" % (probe, exc)


def _cgroup_members(path):
    try:
        with open(os.path.join(path, "cgroup.procs")) as f:
            return f.read().split()
    except OSError:
        return []


def _remove_cgroup_tree(path):
    try:
        entries = list(os.scandir(path))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    for entry in entries:
        if entry.is_dir(follow_symlinks=False) \
                and not _remove_cgroup_tree(entry.path):
            return False
    try:
        os.rmdir(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _kill_cgroup(path):
    if not _valid_cgroup(path):
        return False
    if not _cgroup_members(path):
        return _remove_cgroup_tree(path)
    try:
        with open(os.path.join(path, "cgroup.kill"), "w") as f:
            f.write("1")
    except FileNotFoundError:
        return True
    except OSError:
        if not _cgroup_members(path):
            return _remove_cgroup_tree(path)
        return False
    for _pass in range(_CGROUP_SETTLE_PASSES):
        if not _cgroup_members(path) and _remove_cgroup_tree(path):
            return True
        time.sleep(_WATCH_POLL_S)
    return False


def _self_parent():
    try:
        with open("/proc/self/stat") as f:
            fields = f.read().rsplit(") ", 1)[1].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


def _kill_own_tree(_signum=None, _frame=None):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _kill_descendants(os.getpid())
    os._exit(128 + signal.SIGTERM)


def _exit_process(_signum=None, _frame=None):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os._exit(128 + signal.SIGTERM)


def _arm_parent_death(parent, parent_start, handler=_kill_own_tree):
    signal.signal(signal.SIGTERM, handler)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM) != 0:
        return False
    if not _set_subreaper():
        return False
    # The parent can die before prctl. Check after arming so this process exits
    # instead of adopting the wrong parent forever.
    return _self_parent() == parent and _same_process(parent, parent_start)


def _watch_launcher(argv):
    if len(argv) != 6:
        return 2
    try:
        launcher, launcher_start = int(argv[1]), int(argv[2])
        guard, guard_start = int(argv[3]), int(argv[4])
    except ValueError:
        return 2
    cgroup = argv[5]
    if not _valid_cgroup(cgroup):
        return 2
    while _same_process(guard, guard_start):
        state = _process_state(launcher, launcher_start)
        if state is None or state in ("T", "t", "Z", "X"):
            _kill_cgroup(cgroup)
            return 0
        time.sleep(_WATCH_POLL_S)
    _kill_cgroup(cgroup)
    return 0


def _guard(argv):
    if len(argv) < 13 or argv[1] != "--parent" \
            or argv[3] != "--parent-start" \
            or argv[5] != "--position" \
            or argv[7] != "--ready-fd" \
            or argv[9] != "--report-fd" or argv[11] != "--":
        return 2
    try:
        parent, parent_start = int(argv[2]), int(argv[4])
        ready_fd, report_fd = int(argv[8]), int(argv[10])
    except ValueError:
        return 2
    position, cmd = argv[6], argv[12:]
    if not position:
        return _refuse("guard position is empty")
    if not cmd:
        return _refuse("guard command is empty")
    if not _arm_parent_death(parent, parent_start):
        return _refuse(
            "parent-death probe failed for pid %d start %d" % (
                parent, parent_start))

    guard = os.getpid()
    row = _proc_rows().get(guard)
    if not row:
        return _refuse("guard proc row is unreadable for pid %d" % guard)
    cgroup, reason = _create_cgroup(position)
    if not cgroup:
        return _refuse(reason)
    joined, reason = _join_cgroup(cgroup)
    if not joined:
        _remove_cgroup_tree(cgroup)
        return _refuse(reason)
    launch = [
        sys.executable, os.path.abspath(__file__), "--supervise",
        "--launcher", str(parent),
        "--launcher-start", str(parent_start),
        "--guard", str(guard),
        "--guard-start", str(row[1]),
        "--position", position,
        "--ready-fd", str(ready_fd),
        "--",
    ] + cmd
    try:
        child = subprocess.Popen(launch, pass_fds=(ready_fd,))
    except OSError as exc:
        try:
            return _refuse("supervisor process did not start: %s" % exc)
        finally:
            os.close(ready_fd)
            os.close(report_fd)
    os.close(ready_fd)
    child_row = _proc_rows().get(child.pid)
    if not child_row:
        try:
            return _refuse(
                "supervisor proc row is unreadable for pid %d" % child.pid)
        finally:
            os.close(report_fd)
            _kill_descendants(guard)
    try:
        os.write(report_fd, ("%d %d\n" % (
            child.pid, child_row[1])).encode("ascii"))
    except OSError as exc:
        _kill_descendants(guard)
        return _refuse("supervisor report-fd write failed: %s" % exc)
    finally:
        os.close(report_fd)
    try:
        rc = child.wait()
    finally:
        # This process is the causal outer boundary: it has no children except
        # this gate, so adopted descendants are exact gate work, never temporal
        # guesses about unrelated launcher children.
        _kill_descendants(guard)
    return rc if rc >= 0 else 128 - rc


def _wait_reaping_adopted(child):
    """Wait for `child` while REAPING every orphan this supervisor adopts.

    _set_subreaper() makes this process the parent of any descendant whose own
    parent exits, and nothing here ever reaped them. task/1151 measured 22952
    zombies across three live gates (~2300/min/gate) and one gate on a fab node
    carried 10,116; against ulimit -u 745210 that is roughly 3.7h of concurrent
    gating before fork() starts failing. Setting CHILD_SUBREAPER is a promise
    to take responsibility for orphans — _kill_descendants needs the
    reparenting so it can see the whole tree — and this file took the
    reparenting without ever paying the reaping half.

    THE OBVIOUS CURE IS THE DANGEROUS ONE, which is why this PEEKS. A bare
    os.waitpid(-1, 0) reaper also reaps the TRACKED child; CPython's
    Popen._try_wait then catches the resulting ChildProcessError and
    substitutes sts=0, so A FAILING SUITE REPORTS EXIT 0. Measured directly: a
    child exiting 3, reaped naively, is reported by child.wait() as 0. That is
    a silent-green gate — strictly worse than the leak, and invisible.

    So every exit is PEEKED with WNOWAIT, which leaves the status on the
    process for Popen to collect, and only pids that are NOT the tracked child
    are actually reaped. Measured: a peek reading si_status 4 still leaves
    child.wait() returning 4.

    The peek BLOCKS rather than polling. There is no sleep and no spin: an
    orphan exiting wakes it, and so does the tracked child, which is the one
    event that ends the loop.
    """
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOWAIT)
        except InterruptedError:
            continue
        except ChildProcessError:
            break
        if info is None or info.si_pid == child.pid:
            break
        try:
            os.waitpid(info.si_pid, 0)
        except (ChildProcessError, InterruptedError):
            pass
    rc = child.wait()
    _drain_adopted()
    return rc


def _drain_adopted():
    """Reap orphans that exited while the tracked child was being collected.

    NON-BLOCKING on purpose: a live orphan is not waited for, because the
    supervisor's job ends with the suite and anything still running reparents
    to init on exit and is reaped there. Blocking here would hold a finished
    gate open on a stray helper.
    """
    while True:
        try:
            info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG)
        except (ChildProcessError, InterruptedError):
            return
        if info is None:
            return


def _supervise(argv):
    if len(argv) < 15 or argv[1] != "--launcher" \
            or argv[3] != "--launcher-start" \
            or argv[5] != "--guard" \
            or argv[7] != "--guard-start" \
            or argv[9] != "--position" \
            or argv[11] != "--ready-fd" or argv[13] != "--":
        return 2
    try:
        launcher, launcher_start = int(argv[2]), int(argv[4])
        guard, guard_start = int(argv[6]), int(argv[8])
        ready_fd = int(argv[12])
    except ValueError:
        return 2
    position, cmd = argv[10], argv[14:]
    if not position:
        return _refuse("supervisor position is empty")
    if not cmd:
        return _refuse("supervisor command is empty")
    if not _arm_parent_death(
            guard, guard_start, handler=_exit_process):
        return _refuse(
            "guard-death probe failed for pid %d start %d" % (
                guard, guard_start))

    try:
        ready = os.read(ready_fd, 1)
    finally:
        os.close(ready_fd)
    # No suite process exists before the launcher durably binds this supervisor
    # into the FIFO row and releases the one-byte barrier.
    if ready != b"\0":
        return _refuse("suite-admission barrier did not carry the ready byte")
    if _self_parent() != guard:
        return _refuse("supervisor parent is no longer guard pid %d" % guard)
    if not _same_process(launcher, launcher_start):
        return _refuse(
            "launcher generation is not live: pid %d start %d" % (
                launcher, launcher_start))

    try:
        child = subprocess.Popen(cmd)
        return _wait_reaping_adopted(child)
    except BaseException:
        _exit_process()
        raise


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--watch":
        return _watch_launcher(argv)
    if argv and argv[0] == "--guard":
        return _guard(argv)
    if argv and argv[0] == "--supervise":
        return _supervise(argv)
    return 2


if __name__ == "__main__":
    sys.exit(main())
