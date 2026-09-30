#!/usr/bin/env python3
"""A copy of the live checkout survives the files other workers write into it.

THE FLAKE. A sliced whole suite runs many workers against one checkout, and
every import writes bytecode into it: importlib writes
`__pycache__/<mod>.cpython-313.pyc.<id>` and renames it over
`<mod>.cpython-313.pyc`. A fixture that copied `helm/` with a bare
`shutil.copytree` listed the directory, another worker's rename landed, and
the copy of the listed name raised. The whole-suite gate 72deeacb13c8264a
(16 workers) errored
SameCommitIsSilentTest.test_an_unreadable_head_says_it_could_not_compare in
setUp that way, on
`helm/configs/__pycache__/_cli.cpython-313.pyc.125941355178352`. The arm
passes alone, because alone nothing else is importing.

THE CURE is `tests._tmphome.copy_live_tree`, which every site that copies the
live tree now calls: it does not copy bytecode, and it skips a file that is
gone by the time its turn comes.

THE ARM PINS THE RACE instead of hoping for it. A real thread does what the
other workers do, in a tree this test owns (never the live checkout): it
writes temps and renames them over their targets, bytecode in `__pycache__`
and an atomic save of a source file beside it. `os.scandir` is wrapped so
that whenever a copy LISTS one of those temps, the thread's rename lands
before the copy reaches the name: the worst interleaving, every time a
listing holds a temp. The two call shapes the live-tree sites had then fail,
and the cure passes under the same thread and the same interleaving.
"""
import contextlib
import os
import re
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
from tests._tmphome import copy_live_tree  # noqa: E402
_tmp_home(prefix="helm-test-livetree-", var="HELM_HOME")

PYC = "_cli.cpython-313.pyc"
SOURCE = "_cli.py"
#: A temp the churn writes: its target's name, a dot, and importlib's <id>.
TEMP = re.compile(r"(%s|%s)\.\d+" % (re.escape(PYC), re.escape(SOURCE)))
#: The package files a clean copy holds, relative to the package root.
PACKAGE = {"__init__.py": b"", "cli.py": b"VERBS = {}\n",
           os.path.join("configs", "__init__.py"): b"",
           os.path.join("configs", SOURCE): b"SOURCE = 1\n"}
#: How many copies a half may try before it has seen what it needs.
ATTEMPTS = 200


class Churn(threading.Thread):
    """The other workers, in the package directory `pkg`.

    Each cycle writes a fresh temp beside each target and renames it over
    the target. importlib writes bytecode that way, and an atomic save
    replaces a source file that way. The temps are held until a copy lists
    one (`listed`), or 20 ms, so most listings hold one; `moved` is notified
    after every cycle's renames."""

    def __init__(self, pkg):
        super().__init__(name="live-tree-churn", daemon=True)
        self.targets = {os.path.join(pkg, "__pycache__", PYC): b"bytecode",
                        os.path.join(pkg, SOURCE): PACKAGE[
                            os.path.join("configs", SOURCE)]}
        self.stop = threading.Event()
        self.listed = threading.Event()
        self.moved = threading.Condition()

    def run(self):
        n = 125941355178352          # importlib's <id> is id(path)
        while not self.stop.is_set():
            n += 1
            moves = []
            for target, body in self.targets.items():
                temp = "%s.%d" % (target, n)
                with open(temp, "wb") as f:
                    f.write(body)
                moves.append((temp, target))
            self.listed.wait(0.02)
            self.listed.clear()
            for temp, target in moves:
                os.replace(temp, target)
            with self.moved:
                self.moved.notify_all()

    def halt(self):
        self.stop.set()
        self.listed.set()
        self.join(10)


class LiveTreeCopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-livetree-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, "checkout", "helm")
        self.pkg = os.path.join(self.src, "configs")
        os.makedirs(os.path.join(self.pkg, "__pycache__"))
        for rel, body in PACKAGE.items():
            with open(os.path.join(self.src, rel), "wb") as f:
                f.write(body)
        with open(os.path.join(self.pkg, "__pycache__", PYC), "wb") as f:
            f.write(b"bytecode")

    def _pinned_scandir(self, churn, caught):
        """`os.scandir`, except that when it lists a churn temp in this
        test's tree, the churn's rename of it lands before the listing is
        returned: the name copytree is about to copy is already gone. Every
        temp so listed is appended to `caught`."""
        real = os.scandir

        def scandir(path="."):
            with real(path) as it:
                entries = list(it)
            here = os.fspath(path)
            temps = [os.path.join(here, e.name) for e in entries
                     if TEMP.fullmatch(e.name)]
            if temps and here.startswith(self.tmp + os.sep):
                caught.extend(temps)
                churn.listed.set()
                with churn.moved:
                    churn.moved.wait_for(
                        lambda: not any(map(os.path.lexists, temps)),
                        timeout=30)
            return contextlib.nullcontext(entries)
        return scandir

    def _failure_naming(self, copy, fragment):
        """The errors of the first `copy` into a fresh directory that raises
        shutil.Error naming a source path containing `fragment`, or []."""
        for i in range(ATTEMPTS):
            dest = os.path.join(self.tmp, "before-%s-%d" % (copy.__name__, i))
            try:
                copy(self.src, dest)
            except shutil.Error as e:
                errors = e.args[0]
                if any(fragment in src for src, _dest, _why in errors):
                    return errors
        return []

    def _files(self, root):
        return {os.path.relpath(os.path.join(base, name), root)
                for base, _dirs, names in os.walk(root) for name in names}

    def test_a_copy_of_the_live_tree_survives_bytecode_written_during_it(self):  # noqa: VACUOUS_ASSERTION — the empty list of bytecode temps the cure met is controlled by the same wrapper listing bytecode temps under the same thread for the old copies, asserted first and unconditionally
        churn = Churn(self.pkg)
        churn.start()
        self.addCleanup(churn.halt)
        caught = []

        def bare(src, dest):
            """The three test_cli_tree_warning sites, before the cure."""
            shutil.copytree(src, dest)

        def pycache_only(src, dest):
            """The test_trunkroute site, before the cure."""
            shutil.copytree(src, dest,
                            ignore=shutil.ignore_patterns("__pycache__"))

        with mock.patch("os.scandir", self._pinned_scandir(churn, caught)):
            # BEFORE, RED: the flake's own shape. A bytecode temp listed in
            # __pycache__ and renamed away fails the bare copy.
            pyc_temp = os.path.join("configs", "__pycache__", PYC + ".")
            failed = self._failure_naming(bare, pyc_temp)
            self.assertTrue(failed, "the bare copy never failed on a "
                                    "bytecode temp that was renamed away")
            self.assertTrue(all("No such file" in why or "Errno 2" in why
                                for _src, _dest, why in failed), failed)
            # Skipping __pycache__ alone was not enough: an atomic save's
            # temp beside a source file, listed and then gone, fails too.
            source_temp = os.path.join("configs", SOURCE + ".")
            self.assertTrue(self._failure_naming(pycache_only, source_temp),
                            "the __pycache__-only copy never failed on a "
                            "source temp that was renamed away")

            # AFTER, GREEN: the cure, under the same thread and the same
            # interleaving, until a copy has met a listed temp that was gone.
            before = caught[:]
            del caught[:]
            copies = []
            for i in range(ATTEMPTS):
                dest = os.path.join(self.tmp, "after-%d" % i)
                copy_live_tree(self.src, dest)
                copies.append(dest)
                if caught and i >= 9:
                    break

        # The skip path ran: a listed source temp was gone when its turn came.
        self.assertTrue(any(source_temp in t for t in caught), caught)
        # __pycache__ is never listed, so no bytecode temp can be met at all.
        # CONTROL ON THE SAME OBSERVABLE: the same wrapper, under the same
        # thread, did list bytecode temps while the old copies descended
        # into __pycache__, so an empty list here is the ignore.
        self.assertTrue([t for t in before if "__pycache__" in t], before)
        self.assertEqual([t for t in caught if "__pycache__" in t], [])
        # CONTROL: the source really holds bytecode, so its absence from
        # every copy is the ignore and not an empty source.
        self.assertTrue(os.path.isfile(os.path.join(self.pkg, "__pycache__",
                                                    PYC)))
        for dest in copies:
            self.assertEqual(self._files(dest), set(PACKAGE), dest)
            for rel, body in PACKAGE.items():
                with open(os.path.join(dest, rel), "rb") as f:
                    self.assertEqual(f.read(), body, rel)


if __name__ == "__main__":
    unittest.main()
