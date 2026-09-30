"""Hermetic tests for `helm.seat_lifecycle_runtime` — session recovery.

Tests the dead-pid skip rule: a session record whose name is `<pid>.json` and
whose pid is not a live process must not block resume.  (task/3495 part N2.)
"""
import json
import os
import subprocess
import tempfile
import unittest

from helm import seat  # noqa: F401 — seeds the impl modules


class DeadPidCorruptRecordTest(unittest.TestCase):
    """Arms a-d: dead-pid skip before record parse.

    THE RULE: before a record is parsed, if its file name is ``<digits>.json``
    and that pid is not running, skip the file: it is *not found* and *not*
    ``unavailable``.  Everything else stays as it is today.
    """

    def _make_tree(self, files):
        """Create a temp dir whose ``sessions/`` subdirectory holds ``files``.

        ``files`` is ``{filename: body}``.  Returns ``(config_home, sessions)``
        where ``config_home`` is suitable as ``record["config_home"]``.
        """
        config_home = tempfile.mkdtemp(prefix="helm-session-home-")
        sessions = os.path.join(config_home, "sessions")
        os.makedirs(sessions)
        for name, body in files.items():
            with open(os.path.join(sessions, name), "w") as fh:
                fh.write(body)
        return config_home, sessions

    def _spawn_short_child(self):
        """Spawn a short-lived child process and return its pid.

        The child exits after this function returns, so the pid is DEAD by the
        time the caller uses it.
        """
        proc = subprocess.Popen(
            [os.environ.get("PYTHON", "python3"), "-c", "pass"],
        )
        pid = proc.pid
        proc.wait(timeout=5)
        return pid

    def test_a_dead_pid_corrupt_skipped_found_and_unavailable_empty(self):
        """Arm a: a corrupt file named ``<dead_pid>.json`` gives found==[] and
        detail==None — the file is invisible, not unavailable.
        """
        from helm import seat_lifecycle_runtime as runtime

        pid = self._spawn_short_child()
        filename = "%d.json" % pid
        root, sessions = self._make_tree({filename: "{not json"})

        found, detail = runtime._live_session_records(None, record={
            "config_home": root,
        })
        self.assertEqual(found, [])
        self.assertIsNone(detail)
        # The dead-pid file must NOT appear in unavailable.
        self.assertNotIn(filename, (detail or ""))

    def test_b_corrupt_named_live_pid_still_unavailable(self):
        """Arm b: corrupt body named ``<this_pid>.json`` still gives detail
        naming that file — unchanged behaviour for a live pid.
        """
        from helm import seat_lifecycle_runtime as runtime

        my_pid = os.getpid()
        filename = "%d.json" % my_pid
        root, sessions = self._make_tree({filename: "{not json"})

        found, detail = runtime._live_session_records(None, record={
            "config_home": root,
        })
        self.assertEqual(found, [])
        self.assertIsNotNone(detail)
        self.assertIn(filename, detail)

    def test_c_non_pid_filename_corrupt_still_unavailable(self):
        """Arm c: corrupt body named ``notes.json`` (no pid in name) still
        gives detail naming that file.
        """
        from helm import seat_lifecycle_runtime as runtime

        root, sessions = self._make_tree({"notes.json": "{not json"})

        found, detail = runtime._live_session_records(None, record={
            "config_home": root,
        })
        self.assertEqual(found, [])
        self.assertIsNotNone(detail)
        self.assertIn("notes.json", detail)

    def test_d_live_seat_session_dead_pid_corrupt_returns_live_none(self):
        """Arm d: ``live_seat_session`` over case a answers LIVE_NONE, not
        LIVE_UNKNOWN.
        """
        from helm import seat_lifecycle_runtime as runtime

        pid = self._spawn_short_child()
        filename = "%d.json" % pid
        root, sessions = self._make_tree({filename: "{not json"})

        state, session, cwd, why = runtime.live_seat_session(None, record={
            "config_home": root,
        })
        self.assertEqual(state, runtime.LIVE_NONE)
        self.assertIsNone(session)


class DeadPidSkipGuardRegisterTest(unittest.TestCase):
    """Verify the guard is registered in the CHECKS-like path (not applicable
    for this runtime module — just a placeholder for module-level smoke test).
    """

    def test_module_imports_cleanly(self):
        """The module must import without side effects."""
        from helm import seat_lifecycle_runtime  # noqa: F401
        # If we got here the import succeeded.
