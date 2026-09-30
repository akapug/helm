#!/usr/bin/env python3
"""The seats lock fails CLOSED (task/2520).

`seats_common._flocked` caught an OSError on open or on flock, set `.f` to
None and ran the body anyway, so eleven roster writers and every cursor lock
wrote with no lock held whenever the lock could not be taken. It now raises a
named `LockUnavailable` (an OSError) naming the lock and why, and the writer
does not run. Three forms do not raise, each explicit at its call site:
`blocking=False` (contention is a value the caller reads off `.f`),
`check=True` (the caller reads `.f` and refuses, never writing unlocked) and
`unlocked_ok=True` (the few sites that must proceed unlocked, each saying
why). A census pins every call site to one of them."""
import errno
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402

_tmp_home()

from helm import hooklatency, pk, seats_common  # noqa: E402

_HELM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "helm")
# THE SITES THAT MUST PROCEED UNLOCKED, by module, and nothing else.
UNLOCKED_OK = {"beacon_doorbell.py"}


class LockBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-lockclosed-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = mock.patch.dict(os.environ, {"HELM_CHAT_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        self.path = os.path.join(self.tmp, "x.lock")

    def failing(self):
        """Every flock this process takes raises, as an I/O error would."""
        return mock.patch.object(hooklatency, "flock",
                                 side_effect=OSError(errno.EIO, "I/O error"))


class TheDoorFailsClosedTest(LockBase):

    def test_a_flock_that_raises_names_the_lock_and_the_body_never_runs(self):
        ran = []
        with self.failing():
            with self.assertRaises(seats_common.LockUnavailable) as got:
                with seats_common._flocked(self.path):
                    ran.append(1)
        self.assertEqual(ran, [])
        self.assertIsInstance(got.exception, OSError)
        self.assertEqual(got.exception.path, self.path)
        self.assertIn(self.path, str(got.exception))
        self.assertIn("I/O error", str(got.exception))

    def test_a_lock_that_cannot_open_is_refused_by_name(self):
        path = os.path.join(self.tmp, "x.lock")
        os.makedirs(path)                       # a directory where the lock is
        with self.assertRaises(seats_common.LockUnavailable) as got:
            with seats_common._flocked(path):
                self.fail("the body ran with no lock")
        self.assertIn(path, str(got.exception))

    def test_a_roster_writer_does_not_write_when_its_lock_raises(self):
        from helm import seats_mute
        pk.write_json(seats_common.roster_path(), {"seat-a": {"seat": "seat-a"}})
        before = seats_common.roster()
        with self.failing(), self.assertRaises(seats_common.LockUnavailable) as got:
            seats_mute.set_mute("seat-a", "main")
        self.assertIn(".roster.json.lock", str(got.exception))
        self.assertEqual(seats_common.roster(), before)

    def test_the_cursor_locks_fail_closed(self):
        from helm import seats_cursor
        path = seats_cursor.cursor_path("main", "seat-a")
        with self.failing(), self.assertRaises(seats_common.LockUnavailable):
            with seats_cursor._cursor_locks([path]):
                self.fail("a cursor transaction ran with no lock")

    def test_check_and_non_blocking_read_f_instead_of_raising(self):
        with self.failing():
            with seats_common._flocked(self.path, check=True) as lock:
                self.assertIsNone(lock.f)
            with seats_common._flocked(self.path, blocking=False) as lock:
                self.assertIsNone(lock.f)

    def test_the_opt_in_still_proceeds(self):
        ran = []
        with self.failing():
            with seats_common._flocked(self.path, unlocked_ok=True) as lock:
                ran.append(lock.f)
        self.assertEqual(ran, [None])

    def test_a_missing_parent_is_made_not_refused(self):
        """A fresh host has no chat directory yet; the lock makes its parent
        rather than refusing every verb."""
        path = os.path.join(self.tmp, "fresh", "x.lock")
        with seats_common._flocked(path) as lock:
            self.assertIsNotNone(lock.f)
        self.assertTrue(os.path.isfile(path))

    def test_a_non_blocking_take_makes_no_missing_parent(self):
        """The claims poll's caller owns the missing-directory verdict
        (`_claim_flocked(create_dir=...)`, task/3001): a non-blocking take
        reads `.f` None there and makes nothing, so a HELM_CHAT_DIR at a
        deleted tree is refused by name and never comes back empty."""
        gone = os.path.join(self.tmp, "gone")
        with seats_common._flocked(os.path.join(gone, "x.lock"),
                                   blocking=False) as lock:
            self.assertIsNone(lock.f)
        self.assertFalse(os.path.exists(gone))
        # POSITIVE CONTROL: the same take on a parent that exists holds.
        os.makedirs(gone)
        with seats_common._flocked(os.path.join(gone, "x.lock"),
                                   blocking=False) as lock:
            self.assertIsNotNone(lock.f)

    def test_a_held_lock_is_unchanged(self):
        """CONTROL: an ordinary take still holds and releases the lock."""
        with seats_common._flocked(self.path) as lock:
            self.assertIsNotNone(lock.f)
        with seats_common._flocked(self.path, blocking=False) as lock:
            self.assertIsNotNone(lock.f)


class AReadSurfaceRefusesByNameTest(LockBase):
    """A read surface whose lock now raises answers with a named refusal,
    never a traceback. `helm chat receipts` locates its row under the
    rename-journal lock, OUTSIDE the try that turns a fault into a sentence,
    so an unopenable lock reached the terminal as a LockUnavailable trace."""

    def receipts(self, rid):
        return subprocess.run(
            [sys.executable, "-m", "helm", "chat", "receipts", rid],
            cwd=os.path.dirname(_HELM), capture_output=True, text=True,
            timeout=60)

    def test_an_unopenable_rename_journal_lock_is_a_named_refusal(self):
        from helm import chat
        from helm.seats_rename import rename_journal_path
        env = mock.patch.dict(os.environ, {"HELM_CHAT_NODE_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        row = chat.post("@all receipts probe", who="owner")
        # POSITIVE CONTROL, the same command: with the lock openable it
        # answers, so the refusal below is about the lock and nothing else.
        ok = self.receipts(row["id"])
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("helm chat receipts", ok.stdout)
        lock = rename_journal_path() + ".lock"
        os.remove(lock)                     # the control's take made it
        os.makedirs(lock)                   # a directory where the lock is
        got = self.receipts(row["id"])
        self.assertNotEqual(got.returncode, 0, got.stdout)
        self.assertNotIn("Traceback", got.stderr)
        self.assertIn("helm chat receipts: ", got.stderr)
        self.assertIn(lock, got.stderr)
        self.assertIn("rename journal", got.stderr)


class EveryCallSiteIsClassifiedTest(unittest.TestCase):
    """Each `_flocked(` call in helm/ is one of: the default (raises), a
    `blocking=False` or `check=True` take whose next lines read `.f`, or an
    `unlocked_ok=True` take in a module on UNLOCKED_OK whose comment says why
    it proceeds UNLOCKED."""

    CALL = re.compile(r"(?<![\w.])(?:[\w.]+\.)?_flocked\(")

    def sites(self):
        for name in sorted(os.listdir(_HELM)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(_HELM, name), encoding="utf-8") as f:
                lines = f.read().splitlines()
            for i, ln in enumerate(lines):
                code = ln.split("#", 1)[0]
                if "class _flocked" in code or not self.CALL.search(code):
                    continue
                call = " ".join(lines[i:i + 3])
                yield name, i, lines, call

    def test_every_site_is_one_of_the_named_forms(self):
        unlocked, count = set(), 0
        for name, i, lines, call in self.sites():
            count += 1
            after = "\n".join(lines[i:i + 48])
            before = "\n".join(lines[max(0, i - 8):i + 1])
            with self.subTest(site="%s:%d" % (name, i + 1)):
                if "unlocked_ok=True" in call:
                    unlocked.add(name)
                    self.assertIn("UNLOCKED", before,
                                  "an unlocked_ok site says why it proceeds")
                elif "check=True" in call or "blocking=False" in call:
                    self.assertRegex(after, r"\.f\b",
                                     "a checked take reads .f and refuses")
        self.assertGreater(count, 30)
        self.assertEqual(unlocked, UNLOCKED_OK)


if __name__ == "__main__":
    unittest.main()
