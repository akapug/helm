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
_CGROUP_PREFIX = "helm-gate-"
_CGROUP_SETTLE_PASSES = 100
_TREE_SETTLE_PASSES = 64
_TREE_SETTLE_S = 0.002
_WATCH_POLL_S = 0.05


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


def _set_subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.prctl(_PR_SET_CHILD_SUBREAPER, 1) == 0


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


def _cgroup_path(position):
    root = _cgroup_root()
    token = position.lower()
    if not root or not token or len(token) > 64 \
            or any(ch not in "0123456789abcdef" for ch in token):
        return None
    return os.path.join(root, _CGROUP_PREFIX + token)


def _create_cgroup(position):
    path = _cgroup_path(position)
    if not path:
        return None
    try:
        os.mkdir(path, 0o700)
        for name in ("cgroup.procs", "cgroup.events", "cgroup.kill"):
            if not os.path.exists(os.path.join(path, name)):
                os.rmdir(path)
                return None
        return path
    except OSError:
        return None


def _join_cgroup(path):
    if not _valid_cgroup(path):
        return False
    try:
        with open(os.path.join(path, "cgroup.procs"), "w") as f:
            f.write(str(os.getpid()))
        return str(os.getpid()) in _cgroup_members(path)
    except OSError:
        return False


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
    if not position or not cmd or not _arm_parent_death(parent, parent_start):
        return 125

    guard = os.getpid()
    row = _proc_rows().get(guard)
    if not row:
        return 125
    cgroup = _create_cgroup(position)
    if not cgroup:
        return 125
    if not _join_cgroup(cgroup):
        _remove_cgroup_tree(cgroup)
        return 125
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
    except OSError:
        os.close(ready_fd)
        os.close(report_fd)
        return 125
    os.close(ready_fd)
    child_row = _proc_rows().get(child.pid)
    if not child_row:
        os.close(report_fd)
        _kill_descendants(guard)
        return 125
    try:
        os.write(report_fd, ("%d %d\n" % (
            child.pid, child_row[1])).encode("ascii"))
    except OSError:
        _kill_descendants(guard)
        return 125
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
    if not position or not cmd or not _arm_parent_death(
            guard, guard_start, handler=_exit_process):
        return 125

    try:
        ready = os.read(ready_fd, 1)
    finally:
        os.close(ready_fd)
    # No suite process exists before the launcher durably binds this supervisor
    # into the FIFO row and releases the one-byte barrier.
    if ready != b"\0" or _self_parent() != guard \
            or not _same_process(launcher, launcher_start):
        return 125

    try:
        child = subprocess.Popen(cmd)
        return child.wait()
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
