"""The suite runs os.get_exec_path in one thread at a time (task/3398).

THE RACE IS IN THE STANDARD LIBRARY. subprocess.Popen, given a bare program
name such as "git", finds it through os.get_exec_path(), and that function
reads PATH inside `warnings.catch_warnings()`. catch_warnings is not
thread-safe: entering it saves the current list and binds warnings.filters to
a copy; leaving it binds the saved list again. When two threads leave in the
wrong order -- A enters, B enters and saves A's copy, A leaves, B leaves --
warnings.filters stays bound to A's copy, which starts with
("ignore", None, BytesWarning, None, 0). The gate's leak audit reports that
as "warnings changed", on whichever unit was running.

WHERE IT HAPPENED. A 16-worker whole-suite gate on a build host failed
tests.test_dispatches that way, and the module alone passed. Its arm
test_concurrent_adds_are_all_replayable runs dispatches.add from 24 threads,
and every add runs git. With a 1 microsecond switch interval, 11 of 20 runs
of that arm left the list changed.

THE CENSUS (task/3398): the whole suite run with every catch_warnings window
recorded by the thread and the function that opened it. About 165 tests in
29 modules open a window off the main thread (the count moves a little with
timing). Every such window is os.get_exec_path's except one: test_trainblame's
fab seam ran a TextTestRunner in helm.trainblame's thread pool, and that test
now prints its reports in the arm's own thread. With a 20 microsecond switch
interval, windows overlapped in six tests (in test_dispatches, test_seats,
test_trainblame, test_wiring and test_work) and three left the list changed.
With only the lock below, four test_trainblame tests still changed it. With
both cures, the same census found no overlap and no changed list, except in
test_warnings_isolation, which makes the race on purpose.

THE CURE IS AT THE RACE. The bootstrap (tests/__init__.py) calls
`serialize_exec_path()` once, before any test module loads, for every runner.
It binds os.get_exec_path to a wrapper that runs the real function under one
process-wide lock. No two threads are then inside its window at once, so the
windows close in the order they opened. The real function still runs; only
its concurrency changes, and only in a test process. A child forked while
another thread holds the lock gets a new lock, because the holder does not
exist in the child.

WHAT IT DOES NOT COVER: any other catch_warnings window a thread opens (a
TextTestRunner, assertWarns) while another thread is inside one. The census
found one, cured in its test. A test that must do this puts the list back
itself, with warnings.catch_warnings() around its threads.
"""
import functools
import os
import threading
import warnings

HANG_S = 30.0
REAL_EXEC_PATH = getattr(os.get_exec_path, "__wrapped__", os.get_exec_path)
_lock = threading.RLock()


@functools.wraps(REAL_EXEC_PATH)
def serial_exec_path(env=None):
    with _lock:
        return REAL_EXEC_PATH(env)


def exec_path_lock():
    return _lock


def _new_lock_in_child():
    global _lock
    _lock = threading.RLock()


def serialize_exec_path():
    """Bind os.get_exec_path to `serial_exec_path`, once per process."""
    if os.get_exec_path is serial_exec_path:
        return
    os.get_exec_path = serial_exec_path
    os.register_at_fork(after_in_child=_new_lock_in_child)


class BlockingEnv(dict):
    """An environment whose PATH lookup sets `entered` and waits for
    `release`: it holds a thread inside os.get_exec_path's window."""

    def __init__(self, entered, release):
        super().__init__()
        self.entered, self.release = entered, release

    def get(self, key, default=None):
        self.entered.set()
        if not self.release.wait(HANG_S):
            raise RuntimeError("the window was never released")
        return os.defpath


def windows_out_of_order():
    """Two threads run the REAL os.get_exec_path: A enters, B enters, A
    leaves, B leaves. -> the list warnings.filters is bound to afterwards."""
    windows = []
    for _ in range(2):
        entered, release = threading.Event(), threading.Event()
        thread = threading.Thread(target=REAL_EXEC_PATH,
                                  args=(BlockingEnv(entered, release),))
        thread.start()
        if not entered.wait(HANG_S):
            raise RuntimeError("a get_exec_path window never opened")
        windows.append((thread, release))
    for thread, release in windows:
        release.set()
        thread.join(HANG_S)
        if thread.is_alive():
            raise RuntimeError("a get_exec_path window never closed")
    return warnings.filters
