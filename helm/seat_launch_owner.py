"""One attached-process owner for every Helm launch surface.

Headless launches retain direct ``exec`` and PID continuity. A launch whose
stdout is a TTY gets this small supervisor: it preserves the child's real wait
status, disarms terminal modes after the child is gone, and reproduces signal
termination rather than flattening it to an ordinary ``128 + signal`` exit.
"""
import ctypes
import os
import signal
import sys
import time


TERMINAL_DISARM = (b"\033[?1000l\033[?1002l\033[?1003l\033[?1006l"
                   b"\033[?1015l\033[?2004l\033[?1049l")
_FORWARD = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)
_SIGNAL_GRACE = 0.5
_WAIT_POLL = 0.01
# How many exited children one reaping pass will collect. The wait loop
# re-enters every _WAIT_POLL, so a backlog larger than this drains across
# the next few passes rather than blocking this one.
_REAP_PER_PASS = 64
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def _disarm(done):
    """Write the reset once, only to the TTY that could have been armed."""
    if done[0]:
        return
    done[0] = True
    if not os.isatty(1):
        return
    view = memoryview(TERMINAL_DISARM)
    try:
        while view:
            view = view[os.write(1, view):]
    except OSError:
        pass


def _tty_foreground(fd, pgid):
    """Set one terminal's foreground group without a SIGTTOU stop."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGTTOU,))
    try:
        os.tcsetpgrp(fd, pgid)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def _subreaper(enable):
    """Set Linux child-subreaper state and return the previous value."""
    libc = ctypes.CDLL(None, use_errno=True)
    prior = ctypes.c_int()
    if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(prior), 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, int(bool(enable)), 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return bool(prior.value)


def _child_map(proc_root="/proc"):
    children = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "stat"), "rb") as f:
                raw = f.read()
            fields = raw[raw.rfind(b")") + 2:].split()
            ppid = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(name))
    return children


def _owned_descendants(owner=None):
    owner = os.getpid() if owner is None else owner
    children = _child_map()
    if children is None:
        return None
    out, todo = [], list(children.get(owner, ()))
    while todo:
        pid = todo.pop()
        out.append(pid)
        todo.extend(children.get(pid, ()))
    return out


def _reap_adopted(owned=None):
    """Reap every exited child. -> `owned`'s status if it was one, else None.

    THE OWNED PID HAS TO COME BACK OUT OR THIS CANNOT RUN IN THE WAIT LOOP.
    waitpid(-1) takes ANY child, and the direct harness is a child — so a
    reaper called beside the loop that waits on it can consume the one status
    the wrapper exists to reproduce, and the next waitpid(pid) then raises
    ChildProcessError with the exit code already gone. Returning it instead
    makes the race a normal outcome rather than a hazard to sequence around:
    whoever reaps the harness reports it, and the loop is finished either way.
    Reaping does not stop at that pid — the remaining orphans are drained in
    the same pass, because leaving them is the defect this call is for.
    """
    status = None
    # BOUNDED, BECAUSE THE ONLY OTHER EXIT IS THE KERNEL SAYING "NOTHING LEFT".
    # An unbounded drain is fine on the paths that run once at shutdown, and it
    # is a hazard in a loop that re-enters every few milliseconds: any waitpid
    # that keeps answering with a pid — a fixture returning a constant, a
    # pathological fork storm — becomes an infinite loop inside the wrapper's
    # hot path, with the harness's own exit never reported. Measured: it hung
    # a whole-suite gate for 37 minutes until SIGTERM, against a mock that was
    # correct for every caller this function had before it was called here.
    # A cap costs nothing real — the caller re-enters in _WAIT_POLL and drains
    # the rest — and it converts an unbounded wait into a bounded one.
    for _ in range(_REAP_PER_PASS):
        try:
            pid, exited = os.waitpid(-1, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            return status
        if pid <= 0:
            return status
        if owned is not None and pid == owned:
            status = exited
    return status


def _signal_descendants(sig):
    pids = _owned_descendants()
    if pids is None:
        return None
    for pid in reversed(pids):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
    return pids


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _drain_group(pgid, sig=signal.SIGTERM):
    """Terminate every descendant before terminal cleanup is allowed."""
    if not _group_alive(pgid):
        return True
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return True
    for _ in range(50):
        _reap_adopted()
        if not _group_alive(pgid):
            return True
        time.sleep(0.01)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    for _ in range(100):
        _reap_adopted()
        if not _group_alive(pgid):
            return True
        time.sleep(0.01)
    return False


def _drain_descendants(sig=signal.SIGTERM):
    """Drain adopted children, including descendants that escaped the group."""
    pids = _signal_descendants(sig)
    if pids is None:
        return False
    for _ in range(50):
        _reap_adopted()
        pids = _owned_descendants()
        if pids == []:
            return True
        if pids is None:
            return False
        time.sleep(0.01)
    pids = _signal_descendants(signal.SIGKILL)
    if pids is None:
        return False
    for _ in range(100):
        _reap_adopted()
        pids = _owned_descendants()
        if pids == []:
            return True
        if pids is None:
            return False
        time.sleep(0.01)
    return False


def _wait_owned_child(pid, pgid, terminating):
    """Reap the direct harness, escalating when owned termination stalls."""
    deadline = None
    while True:
        # THE STEADY STATE IS WHERE THE ORPHANS ACCUMULATE, and it was the one
        # place that never reaped. This loop waits on the DIRECT harness only,
        # while _reap_adopted ran exclusively on the drain paths — so for the
        # whole lifetime of a healthy wrapper, every process the harness
        # orphaned stayed a zombie in the wrapper's own table. Measured live:
        # hundreds per wrapper on the longest-running seats. A pass here costs
        # one non-blocking syscall against the sleep already below it, and it
        # may report the harness's own status, which is a completed wait.
        reaped = _reap_adopted(pid)
        if reaped is not None:
            return reaped
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            # NOT REACHABLE THROUGH THE REAPER ABOVE, which returns the status
            # rather than dropping it — but a wrapper that has lost its child
            # must not raise out of a wait loop, so the unknown-status answer
            # is the same one the caller gets for a signal it cannot decode.
            return 0
        if waited == pid:
            return status
        if terminating():
            now = time.monotonic()
            if deadline is None:
                deadline = now + _SIGNAL_GRACE
            elif now >= deadline:
                print("helm launch owner: process group %d ignored termination; "
                      "escalating to SIGKILL" % pgid, file=sys.stderr)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                while True:
                    try:
                        return os.waitpid(pid, 0)[1]
                    except InterruptedError:
                        continue
        time.sleep(_WAIT_POLL)


def run(argv, env=None, cwd=None):
    """Run argv as the foreground process-group and reproduce its outcome."""
    if not argv:
        print("helm launch owner: missing command", file=sys.stderr)
        return 2

    tty_fd = next((fd for fd in (0, 1, 2) if os.isatty(fd)), None)
    foreground_error = None
    try:
        old_pgrp = os.tcgetpgrp(tty_fd) if tty_fd is not None else None
    except OSError as e:
        old_pgrp = None
        foreground_error = "terminal foreground query failed: %s" % e
    if tty_fd is not None and (old_pgrp is None or old_pgrp <= 0):
        foreground_error = foreground_error or (
            "terminal foreground query returned no valid process group")
        print("helm launch owner: " + foreground_error, file=sys.stderr)
        return 125
    try:
        prior_subreaper = _subreaper(True)
    except OSError as e:
        print("helm launch owner: descendant authority unavailable: %s" % e,
              file=sys.stderr)
        return 125
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _FORWARD)
    run_mask = set(old_mask) - set(_FORWARD)
    old_handlers = {sig: signal.getsignal(sig) for sig in _FORWARD}
    pid = None
    pgid = None
    status = None
    forwarded = [None]
    done = [False]
    group_drained = True
    descendants_drained = True
    subreaper_restored = True
    owner_error = None
    handoff_failed = foreground_error is not None
    foreground_changed = False
    foreground_restored = True
    try:
        pid = os.fork()
        if pid == 0:
            os.setpgid(0, 0)
            for sig in _FORWARD:
                signal.signal(sig, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_SETMASK, run_mask)
            try:
                if cwd is not None:
                    os.chdir(cwd)
                if env is None:
                    os.execvp(argv[0], argv)
                else:
                    os.execvpe(argv[0], argv, env)
            except (OSError, TypeError, ValueError) as e:
                print("helm launch owner: %s: %s" % (argv[0], e),
                      file=sys.stderr)
                os._exit(127)

        pgid = pid
        try:
            os.setpgid(pid, pgid)
        except OSError:
            pass
        if handoff_failed:
            owner_error = foreground_error
            print("helm launch owner: " + owner_error, file=sys.stderr)
        elif old_pgrp is not None:
            try:
                _tty_foreground(tty_fd, pgid)
                foreground_changed = True
            except OSError as e:
                handoff_failed = True
                owner_error = "terminal foreground handoff failed: %s" % e
                print("helm launch owner: " + owner_error, file=sys.stderr)

        def forward(sig, _frame):
            if forwarded[0] is None:
                forwarded[0] = sig
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass

        for sig in _FORWARD:
            signal.signal(sig, forward)
        if handoff_failed:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        signal.pthread_sigmask(signal.SIG_SETMASK, run_mask)
        status = _wait_owned_child(
            pid, pgid, lambda: handoff_failed or forwarded[0] is not None)
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, _FORWARD)
        if pgid is not None:
            group_drained = _drain_group(
                pgid, forwarded[0] or signal.SIGTERM)
            if not group_drained:
                print("helm launch owner: process group %d survived SIGKILL"
                      % pgid, file=sys.stderr)
            descendants_drained = _drain_descendants(
                forwarded[0] or signal.SIGTERM)
            if not descendants_drained:
                print("helm launch owner: escaped descendants survived SIGKILL "
                      "or their census was unreadable", file=sys.stderr)
        if foreground_changed:
            try:
                _tty_foreground(tty_fd, old_pgrp)
            except OSError as e:
                foreground_restored = False
                owner_error = "terminal foreground restore failed: %s" % e
                print("helm launch owner: " + owner_error, file=sys.stderr)
        try:
            _subreaper(prior_subreaper)
        except OSError as e:
            subreaper_restored = False
            owner_error = "descendant authority restore failed: %s" % e
            print("helm launch owner: " + owner_error, file=sys.stderr)
        ownership_complete = status is not None and group_drained \
            and descendants_drained and foreground_restored \
            and subreaper_restored and not handoff_failed
        if pid is not None and ownership_complete:
            _disarm(done)
        reproduce_signal = ownership_complete and (
            forwarded[0] is not None or os.WIFSIGNALED(status))
        if not reproduce_signal:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

    if not ownership_complete:
        return 125
    if forwarded[0] is not None:
        sig = forwarded[0]
    elif os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
    elif owner_error is not None:
        return 125
    else:
        return os.WEXITSTATUS(status)
    if sig not in (signal.SIGKILL, signal.SIGSTOP):
        signal.signal(sig, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, (sig,))
    os.kill(os.getpid(), sig)
    return 128 + sig


def exec_attached(argv, env=None, cwd=None):
    """Direct-exec headless argv; own an attached process group and terminal."""
    if not argv:
        print("helm launch owner: missing command", file=sys.stderr)
        return 2
    if not os.isatty(1):
        prior = None
        try:
            if cwd is not None:
                prior = os.open(".", os.O_RDONLY)
                os.chdir(cwd)
            if env is None:
                os.execvp(argv[0], argv)
            else:
                os.execvpe(argv[0], argv, env)
            return None
        except (OSError, TypeError, ValueError) as e:
            if prior is not None:
                try:
                    os.fchdir(prior)
                except OSError:
                    pass
            print("helm launch owner: %s: %s" % (argv[0], e), file=sys.stderr)
            return 127
        finally:
            if prior is not None:
                os.close(prior)
    return run(argv, env, cwd)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--"]:
        argv = argv[1:]
    return exec_attached(argv)


if __name__ == "__main__":
    raise SystemExit(main())
