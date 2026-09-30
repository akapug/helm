"""No two threads of a test are inside os.get_exec_path at once (task/3398).

tests/_warnstate describes the race, the gate it failed and the census of the
suite. The control shows the race on the real function; the arm shows that
the function every test calls cannot be entered by two threads at once.
"""
import os
import threading
import unittest
import warnings

from tests import _warnstate


class ExecPathWindowsTest(unittest.TestCase):

    def test_control_the_real_function_leaves_the_list_on_a_threads_copy(self):
        with warnings.catch_warnings():
            held = warnings.filters
            rebound = _warnstate.windows_out_of_order()
        self.assertIsNot(rebound, held)
        self.assertEqual(rebound[0], ("ignore", None, BytesWarning, None, 0))

    def test_no_second_thread_enters_while_one_is_inside(self):
        entered, release = threading.Event(), threading.Event()
        inside = threading.Thread(target=os.get_exec_path, args=(
            _warnstate.BlockingEnv(entered, release),))
        taken = []

        def take():
            lock = _warnstate.exec_path_lock()
            got = lock.acquire(blocking=False)
            if got:
                lock.release()
            taken.append("taken" if got else "held by the thread inside")

        with warnings.catch_warnings():
            inside.start()
            try:
                opened = entered.wait(_warnstate.HANG_S)
                other = threading.Thread(target=take)
                other.start()
                other.join(_warnstate.HANG_S)
            finally:
                release.set()
                inside.join(_warnstate.HANG_S)
        self.assertTrue(opened, "the first thread never entered the window")
        self.assertEqual(taken, ["held by the thread inside"],
                         "a second thread could enter: the suite did not "
                         "serialize os.get_exec_path")


if __name__ == "__main__":
    unittest.main()
