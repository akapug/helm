#!/usr/bin/env python3
"""helm chat node move — the data dir leaves tmpfs only whole, verified and
reversibly (task/4064).

WHY THIS EXISTS: measured on the owner's laptop (87 GB, 29 GB in swap), the chat node's store at /dev/shm/helm-chat-node/dregg.redb was 4.8 GB
of shmem: RAM the kernel can swap but never drop, growing with the chain. A
disk data dir is served from the page cache, which the kernel reclaims.

What these tests pin is the move's safety, because a wrong move costs the
ledger:

  * A COPY OF A LIVE STORE IS TORN. redb rewrites pages in place between
    commits; four of five copies of the live store spanned a commit, and redb
    refused the one opened ("All roots are corrupted"). So move refuses a
    running unit AND a store whose redb lock is held.
  * THE RECORD NEVER NAMES A DIR WITHOUT THE STORE. prepare restores the
    identity into an empty dir, so a record pointing at an empty dir starts
    the same node on an empty ledger. The copy is verified and renamed into
    place before the record moves, and the source is set aside only after.
  * THE UNIT FOLLOWS THE RECORD AT ONCE. The unit is enabled; a reboot
    between `move` and `up` must not start it on the old path.
"""
import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from helm import chatnode  # noqa: E402

STOPPED = {"active": "inactive", "sub": "dead", "restarts": 0, "result":
           "success", "invocation": "", "running_s": None, "pid": None}
RUNNING = dict(STOPPED, active="active", sub="running", pid=4242)


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class MoveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-move-")
        self.prior = {k: os.environ.get(k) for k in ("HOME", "HELM_HOME")}
        os.environ["HOME"] = self.tmp
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.ram = os.path.join(self.tmp, "shm", "helm-chat-node")
        p = mock.patch.object(chatnode, "DATA_DIR", self.ram)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        p = mock.patch.object(chatnode, "_systemctl",
                              lambda *a: (self.calls.append(a) or (0, "")))
        p.start()
        self.addCleanup(p.stop)
        self.unit = os.path.join(self.tmp, "units", chatnode.UNIT)
        p = mock.patch.object(chatnode, "unit_path", lambda: self.unit)
        p.start()
        self.addCleanup(p.stop)
        self.disk = chatnode.disk_data_dir()
        self.plant_node(self.ram)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant_node(self, where):
        """A data dir shaped like the live one: a descriptor, and a store
        with real bytes, an all-zero run (a hole after a sparse copy) and a
        tail, plus a nested dir a later dregg may create."""
        os.makedirs(where, mode=0o700)
        os.chmod(where, 0o700)
        for name, blob in (("node.key", b"k" * 32), ("genesis.json", b"{}"),
                           (".devnet", b"devnet\n")):
            with open(os.path.join(where, name), "wb") as f:
                f.write(blob)
            os.chmod(os.path.join(where, name), 0o600)
        self.store = os.path.join(where, "dregg.redb")
        with open(self.store, "wb") as f:
            f.write(b"redb" + os.urandom(5000))
            f.write(bytes(3 * chatnode.COPY_CHUNK))
            f.write(os.urandom(777))
        os.chmod(self.store, 0o664)
        os.makedirs(os.path.join(where, "known_federations"))
        with open(os.path.join(where, "known_federations", "peer.json"),
                  "wb") as f:
            f.write(b'{"peer": 1}')
        self.shas = {r: _sha(os.path.join(where, r)) for r in (
            "node.key", "genesis.json", ".devnet", "dregg.redb",
            os.path.join("known_federations", "peer.json"))}

    def recorded(self):
        if not os.path.exists(chatnode.state_path()):
            return None
        with open(chatnode.state_path(), encoding="utf-8") as f:
            return json.load(f).get("data_dir")

    def aside(self, src):
        parent, base = os.path.split(src)
        return [os.path.join(parent, n) for n in os.listdir(parent)
                if n.startswith(base + chatnode.MOVED_INFIX)]


class MoveCopiesWholeTest(MoveBase):
    def test_a_stopped_node_moves_to_disk_whole_and_verified(self):
        msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)
        for rel, sha in self.shas.items():
            self.assertEqual(_sha(os.path.join(self.disk, rel)), sha, rel)
        self.assertEqual(self.recorded(), self.disk)
        self.assertEqual(chatnode.node_data_dir(), self.disk)
        self.assertIn("verified by sha256", msg)

    def test_modes_survive_the_move(self):
        chatnode.move(self.disk, unit_st=STOPPED)
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)  # noqa: E731
        self.assertEqual(mode(self.disk), 0o700)
        self.assertEqual(mode(os.path.join(self.disk, "node.key")), 0o600)
        self.assertEqual(mode(os.path.join(self.disk, "dregg.redb")), 0o664)

    def test_the_zero_run_stays_a_hole(self):
        """The live store's tmpfs file had 0.4 GB of never-written pages;
        copying them as data would cost that much disk for nothing."""
        chatnode.move(self.disk, unit_st=STOPPED)
        st = os.stat(os.path.join(self.disk, "dregg.redb"))
        if st.st_blocks * 512 >= st.st_size:
            self.skipTest("this filesystem does not keep holes")
        self.assertLess(st.st_blocks * 512, st.st_size - chatnode.COPY_CHUNK)

    def test_the_source_is_set_aside_never_deleted(self):
        msg, _err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertFalse(os.path.exists(self.ram))
        aside = self.aside(self.ram)
        self.assertEqual(len(aside), 1)
        self.assertEqual(_sha(os.path.join(aside[0], "dregg.redb")),
                         self.shas["dregg.redb"])
        self.assertIn("rm -rf " + aside[0], msg)
        self.assertEqual([p for p, _ in chatnode.moved_aside()], aside)

    def test_the_record_moves_before_the_source_does(self):
        """A source that cannot be set aside leaves the record on the
        verified copy, never on a dir that could be emptied under it."""
        real = os.rename

        def no_aside(a, b, *k, **kw):
            if chatnode.MOVED_INFIX in b:
                raise OSError("EBUSY, injected")
            return real(a, b, *k, **kw)
        with mock.patch.object(chatnode.os, "rename", no_aside):
            msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)
        self.assertEqual(self.recorded(), self.disk)
        self.assertEqual(_sha(os.path.join(self.disk, "dregg.redb")),
                         self.shas["dregg.redb"])
        self.assertIn("could NOT be set aside", msg)

    def test_moving_back_to_ram_clears_the_record_and_round_trips(self):
        chatnode.move(self.disk, unit_st=STOPPED)
        msg, err = chatnode.move(chatnode.placement_target("ram"),
                                 unit_st=STOPPED)
        self.assertIsNone(err, msg)
        self.assertIsNone(self.recorded())
        self.assertEqual(chatnode.node_data_dir(), self.ram)
        for rel, sha in self.shas.items():
            self.assertEqual(_sha(os.path.join(self.ram, rel)), sha, rel)

    def test_an_empty_target_dir_is_taken(self):
        os.makedirs(self.disk)
        _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)
        self.assertEqual(_sha(os.path.join(self.disk, "dregg.redb")),
                         self.shas["dregg.redb"])

    def test_the_same_dir_is_a_no_op(self):
        msg, err = chatnode.move(self.ram, unit_st=STOPPED)
        self.assertIsNone(err)
        self.assertIn("nothing to move", msg)
        self.assertTrue(os.path.exists(self.store))

    def test_an_absent_source_records_the_placement_and_copies_nothing(self):
        """After a reboot the tmpfs dir is gone; the move still records the
        new placement, and prepare restores the identity there."""
        shutil.rmtree(self.ram)
        msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)
        self.assertEqual(self.recorded(), self.disk)
        self.assertFalse(os.path.exists(self.disk))
        self.assertIn("held nothing to copy", msg)


class MoveRefusesTest(MoveBase):
    def assertUntouched(self):
        self.assertEqual(_sha(self.store), self.shas["dregg.redb"])
        self.assertFalse(os.path.exists(self.disk))
        self.assertFalse(os.path.exists(chatnode.state_path()))
        self.assertEqual(self.aside(self.ram), [])
        parent = os.path.dirname(self.disk)
        if os.path.isdir(parent):
            self.assertEqual([n for n in os.listdir(parent)
                              if chatnode.MOVE_INFIX in n], [], "stage left")

    def test_a_running_unit_is_refused(self):
        _msg, err = chatnode.move(self.disk, unit_st=RUNNING)
        self.assertIn("stop it first", err)
        self.assertUntouched()

    def test_a_held_store_lock_is_refused_and_a_free_one_is_not(self):
        """redb holds an exclusive flock on its file while a node has it open.
        The guard must fail with the lock held AND pass with it released."""
        fd = os.open(self.store, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
            self.assertIn("holds redb's lock", err)
            self.assertUntouched()
        finally:
            os.close(fd)
        _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)

    def test_a_non_empty_target_is_refused(self):
        os.makedirs(self.disk)
        with open(os.path.join(self.disk, "dregg.redb"), "wb") as f:
            f.write(b"another store")
        _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("not an empty directory", err)
        with open(os.path.join(self.disk, "dregg.redb"), "rb") as f:
            self.assertEqual(f.read(), b"another store")
        self.assertEqual(_sha(self.store), self.shas["dregg.redb"])

    def test_a_full_target_is_refused(self):
        usage = shutil._ntuple_diskusage(10 ** 12, 10 ** 12, 1000)
        with mock.patch.object(chatnode.shutil, "disk_usage",
                               return_value=usage):
            _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("refusing to fill it", err)
        self.assertUntouched()

    def test_a_copy_that_fails_part_way_leaves_everything_as_it_was(self):
        real = chatnode._copy_file
        seen = []

        def flaky(a, b):
            seen.append(a)
            if len(seen) == 3:
                raise OSError("ENOSPC, injected")
            return real(a, b)
        with mock.patch.object(chatnode, "_copy_file", flaky):
            _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("ENOSPC, injected", err or "")
        self.assertIn("the source %s is untouched" % self.ram, err)
        self.assertIn("nothing was copied", err)
        self.assertUntouched()

    def test_a_copy_that_does_not_verify_is_never_installed(self):
        with mock.patch.object(chatnode, "file_sha256", lambda p: "0" * 64):
            _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("does not match its source", err or "")
        self.assertUntouched()

    def test_nested_paths_are_refused(self):
        _msg, err = chatnode.move(os.path.join(self.ram, "inner"),
                                  unit_st=STOPPED)
        self.assertIn("nest", err)


class MoveRepointsTheUnitTest(MoveBase):
    def install_unit(self):
        os.makedirs(os.path.dirname(self.unit))
        with open(self.unit, "w") as f:
            f.write(chatnode.unit_text("/opt/dregg-node-rebased"))

    def test_an_installed_unit_runs_the_new_dir_before_any_up(self):
        self.install_unit()
        chatnode.move(self.disk, unit_st=STOPPED)
        with open(self.unit) as f:
            text = f.read()
        self.assertIn("prepare --data-dir %s " % self.disk, text)
        self.assertIn("run --data-dir %s " % self.disk, text)
        self.assertNotIn(self.ram + " ", text)
        self.assertIn("ExecStart=/opt/dregg-node-rebased run", text)
        self.assertIn(("daemon-reload",), self.calls)

    def test_no_unit_is_written_where_none_was_installed(self):
        chatnode.move(self.disk, unit_st=STOPPED)
        self.assertFalse(os.path.exists(self.unit))

    def test_the_unit_text_follows_the_record(self):
        self.assertIn("--data-dir %s " % self.ram, chatnode.unit_text("/b"))
        chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("--data-dir %s " % self.disk, chatnode.unit_text("/b"))

    def test_prepare_finds_the_moved_dir_live_and_leaves_it(self):
        chatnode.move(self.disk, unit_st=STOPPED)
        msg, err = chatnode.prepare(binary="/nonexistent")
        self.assertIsNone(err)
        self.assertIn("live at %s" % self.disk, msg)
        self.assertEqual(_sha(os.path.join(self.disk, "dregg.redb")),
                         self.shas["dregg.redb"])

    def test_identity_state_reads_the_moved_dir(self):
        chatnode.snapshot_identity()
        chatnode.move(self.disk, unit_st=STOPPED)
        ident = chatnode.identity_state()
        self.assertTrue(ident["matched"])
        self.assertIn("node.key", ident["live"])



class MoveOrderTest(MoveBase):
    """THE UNIT FOLLOWS THE RECORD BEFORE THE SOURCE LEAVES. Between the
    rename of the source and a unit rewrite after it, systemd's loaded unit
    still runs prepare/run on the source, the source is gone and no lock is
    held: a start there (a reboot, an auto-restart) restores the node's
    identity into an EMPTY ledger at the old path. So the unit is rewritten
    and reloaded under the prepare lock, before the source moves."""

    def install_unit(self):
        os.makedirs(os.path.dirname(self.unit), exist_ok=True)
        with open(self.unit, "w") as f:
            f.write(chatnode.unit_text("/opt/dregg-node-rebased", self.ram))
        with open(self.unit) as f:
            return f.read()

    def test_a_start_inside_the_unit_write_finds_the_lock_held(self):
        chatnode.snapshot_identity(self.ram)
        self.install_unit()
        real = chatnode.write_unit
        seen = []

        def start_meanwhile(text):
            # systemd starts the unit it has loaded: prepare on the old path.
            seen.append((chatnode.prepare(self.ram, binary="/nonexistent"),
                         chatnode.node_data_dir()))
            return real(text)
        with mock.patch.object(chatnode, "write_unit", start_meanwhile):
            _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)
        (_said, perr), recorded = seen[0]
        self.assertIn("another prepare holds", perr or "")
        self.assertEqual(recorded, self.disk)
        self.assertFalse(os.path.exists(os.path.join(self.ram, "node.key")),
                         "a start in the window restored the identity into "
                         "an empty ledger at the old path")

    def test_the_unit_names_the_new_dir_before_the_source_moves(self):
        self.install_unit()
        real = os.rename
        unit_then = []

        def watch(a, b, *k, **kw):
            if chatnode.MOVED_INFIX in b:
                with open(self.unit) as f:
                    unit_then.append(f.read())
            return real(a, b, *k, **kw)
        with mock.patch.object(chatnode.os, "rename", watch):
            _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)
        self.assertEqual(len(unit_then), 1)
        self.assertIn("run --data-dir %s " % self.disk, unit_then[0])
        self.assertIn(("daemon-reload",), self.calls)

    def assertRolledBack(self, before):
        self.assertEqual(_sha(self.store), self.shas["dregg.redb"])
        self.assertIsNone(self.recorded())
        self.assertEqual(chatnode.node_data_dir(), self.ram)
        self.assertFalse(os.path.exists(self.disk))
        self.assertEqual(self.aside(self.ram), [])
        with open(self.unit) as f:
            self.assertEqual(f.read(), before)

    def test_a_unit_write_that_fails_rolls_the_move_back(self):
        before = self.install_unit()

        def broken(_text):
            raise OSError("EROFS, injected")
        with mock.patch.object(chatnode, "write_unit", broken):
            _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("EROFS", err or "")
        self.assertIn("Rolled back", err)
        self.assertRolledBack(before)

    def test_a_daemon_reload_that_fails_rolls_the_move_back(self):
        """A rewritten file systemd never reloaded still starts the old dir."""
        before = self.install_unit()
        with mock.patch.object(chatnode, "_systemctl",
                               lambda *a: (1, "no user bus, injected")):
            _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("daemon-reload", err or "")
        self.assertRolledBack(before)

    def test_a_retry_after_a_rollback_moves(self):
        self.install_unit()
        with mock.patch.object(chatnode, "write_unit",
                               mock.Mock(side_effect=OSError("injected"))):
            chatnode.move(self.disk, unit_st=STOPPED)
        msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err, msg)
        self.assertEqual(self.recorded(), self.disk)
        with open(self.unit) as f:
            self.assertIn("run --data-dir %s " % self.disk, f.read())

    def test_a_unit_without_a_binary_is_refused_before_the_copy(self):
        os.makedirs(os.path.dirname(self.unit))
        with open(self.unit, "w") as f:
            f.write("[Service]\nType=simple\n")
        _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("names no ExecStart binary", err or "")
        self.assertIn(self.unit, err)
        self.assertFalse(os.path.exists(self.disk))
        self.assertIsNone(self.recorded())
        self.assertEqual(_sha(self.store), self.shas["dregg.redb"])

    def test_a_clean_move_names_the_new_dir_end_to_end(self):
        """CONTROL: the reorder changes nothing a clean move ends with."""
        self.install_unit()
        msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err, msg)
        self.assertEqual(self.recorded(), self.disk)
        self.assertEqual(len(self.aside(self.ram)), 1)
        self.assertFalse(os.path.exists(self.ram))
        with open(self.unit) as f:
            text = f.read()
        self.assertIn("prepare --data-dir %s " % self.disk, text)
        self.assertIn("unit refreshed to run %s" % self.disk, msg)


class MoveUnitStateTest(MoveBase):
    """move's own predicate: stopped for good, nothing pending. `_gone`
    counts a unit waiting out RestartSec as gone, which is right for a boot
    wait and wrong here: systemd starts it again mid-copy."""

    def test_a_pending_restart_is_refused(self):
        for st in (dict(STOPPED, active="activating", sub="auto-restart"),
                   dict(STOPPED, active="failed", sub="auto-restart"),
                   dict(STOPPED, active="activating",
                        sub="auto-restart-queued"),
                   dict(STOPPED, active="deactivating", sub="stop-sigterm")):
            _msg, err = chatnode.move(self.disk, unit_st=st)
            self.assertIn("helm chat node down", err or "", st)
            self.assertFalse(os.path.exists(self.disk))
            self.assertFalse(os.path.exists(chatnode.state_path()))

    def test_inactive_and_failed_are_taken(self):
        """CONTROL: a stopped or a failed unit with no restart queued."""
        _msg, err = chatnode.move(
            self.disk, unit_st=dict(STOPPED, active="failed", sub="failed",
                                    result="exit-code"))
        self.assertIsNone(err)
        _msg, err = chatnode.move(self.ram, unit_st=STOPPED)
        self.assertIsNone(err)


class MoveDurabilityTest(MoveBase):
    """A RENAME CAN OUTLIVE THE BYTES IT NAMES. Power lost after the source
    is set aside but before the record's bytes reach the disk leaves no
    record: node_data_dir() falls back to the tmpfs default, the source is
    no longer there, and prepare re-geneses the node on an empty ledger. So
    the record and the unit are fsynced (file, then directory) before the
    source moves, and the source's parent is fsynced after it moves."""

    def trace(self):
        events = []
        real_fsync, real_rename, real_replace = os.fsync, os.rename, os.replace

        def fsync(fd):
            events.append(("fsync", os.readlink("/proc/self/fd/%d" % fd)))
            return real_fsync(fd)

        def rename(a, b, *k, **kw):
            real_rename(a, b, *k, **kw)
            events.append(("rename", os.path.realpath(b)))

        def replace(a, b, *k, **kw):
            real_replace(a, b, *k, **kw)
            events.append(("replace", os.path.realpath(b)))
        for name, fn in (("fsync", fsync), ("rename", rename),
                         ("replace", replace)):
            p = mock.patch.object(chatnode.os, name, fn)
            p.start()
            self.addCleanup(p.stop)
        return events

    def at(self, events, ev, start=0):
        self.assertIn(ev, events[start:], "%r never happened in %r"
                      % (ev, events))
        return events.index(ev, start)

    def test_the_record_is_durable_before_the_source_leaves(self):
        events = self.trace()
        _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)
        rec = os.path.realpath(chatnode.state_path())
        aside = os.path.realpath(self.aside(self.ram)[0])
        tmp = self.at(events, ("fsync", rec + ".tmp"))
        rep = self.at(events, ("replace", rec), tmp)
        sync = self.at(events, ("fsync", os.path.dirname(rec)), rep)
        gone = self.at(events, ("rename", aside), sync)
        self.at(events, ("fsync", os.path.dirname(aside)), gone)

    def test_the_unit_is_durable_before_the_source_leaves(self):
        os.makedirs(os.path.dirname(self.unit))
        with open(self.unit, "w") as f:
            f.write(chatnode.unit_text("/opt/dregg-node-rebased", self.ram))
        events = self.trace()
        _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err)
        unit = os.path.realpath(self.unit)
        tmp = self.at(events, ("fsync", unit + ".tmp"))
        rep = self.at(events, ("replace", unit), tmp)
        sync = self.at(events, ("fsync", os.path.dirname(unit)), rep)
        self.at(events, ("rename", os.path.realpath(self.aside(self.ram)[0])),
                sync)

    def test_write_state_syncs_the_file_then_its_directory(self):
        events = self.trace()
        chatnode.write_state({"url": "u"})
        rec = os.path.realpath(chatnode.state_path())
        self.assertEqual(events, [("fsync", rec + ".tmp"), ("replace", rec),
                                  ("fsync", os.path.dirname(rec))])


class MoveFailureWordsTest(MoveBase):
    """The words after a failure say what the move left, never a blanket
    "(the source is untouched)": past the rename the source is not."""

    def cli(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        err = io.StringIO()
        with mock.patch.object(chatnode, "unit_state", lambda *a: STOPPED), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = chatnode.cmd_node(["move", "--to", "disk"])
        return rc, err.getvalue()

    def test_a_failure_after_the_rename_names_where_the_source_went(self):
        real = chatnode._fsync_dir
        parent = os.path.dirname(self.ram)

        def broken(path):
            if os.path.realpath(path) == os.path.realpath(parent):
                raise OSError(5, "EIO, injected")
            return real(path)
        with mock.patch.object(chatnode, "_fsync_dir", broken):
            rc, text = self.cli()
        self.assertEqual(rc, 1)
        self.assertIn("EIO, injected", text)
        self.assertNotIn("untouched", text)
        aside = self.aside(self.ram)
        self.assertEqual(len(aside), 1)
        self.assertIn("set aside at %s" % aside[0], text)
        self.assertIn("the record names %s" % self.disk, text)

    def test_a_parent_fsync_that_fails_after_the_rename_names_the_copy(self):  # noqa: VACUOUS_ASSERTION — the same text must positively name the verified copy, the rm -rf line and the record, and the retry arm must move
        """The stage is renamed onto the target, then the target's parent
        fsync raises: the target holds the whole verified store, the record
        still names the source, and a retry refuses on the occupied target.
        The words say where the store is and how to retry, never "nothing
        was copied"."""
        real = chatnode._fsync_dir
        parent = os.path.realpath(os.path.dirname(self.disk))
        hit = []

        def broken(path):
            if os.path.realpath(path) == parent and not hit:
                hit.append(path)
                raise OSError(5, "EIO, injected")
            return real(path)
        with mock.patch.object(chatnode, "_fsync_dir", broken):
            rc, text = self.cli()
        self.assertEqual(rc, 1)
        self.assertEqual(len(hit), 1, "the parent fsync was never reached")
        self.assertIn("EIO, injected", text)
        self.assertNotIn("nothing was copied", text)
        self.assertIn("the verified copy is at %s" % self.disk, text)
        self.assertIn("rm -rf %s" % self.disk, text)
        self.assertIn("the source %s is untouched" % self.ram, text)
        self.assertIn("the record names %s" % self.ram, text)
        for rel, sha in self.shas.items():
            self.assertEqual(_sha(os.path.join(self.disk, rel)), sha, rel)
        _msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIn("not an empty directory", err or "")
        shutil.rmtree(self.disk)
        msg, err = chatnode.move(self.disk, unit_st=STOPPED)
        self.assertIsNone(err, msg)
        self.assertEqual(self.recorded(), self.disk)

    def test_a_failure_with_no_source_claims_no_copy(self):  # noqa: VACUOUS_ASSERTION — the same text must positively carry the injected error and "nothing was copied to <dst>"
        """An empty source copies nothing; a failure after that point must
        say so, even with an empty target dir already in place."""
        shutil.rmtree(self.ram)
        os.makedirs(self.ram)
        os.makedirs(self.disk)
        with mock.patch.object(chatnode, "write_state",
                               mock.Mock(side_effect=OSError("EROFS, "
                                                             "injected"))):
            rc, text = self.cli()
        self.assertEqual(rc, 1)
        self.assertIn("EROFS, injected", text)
        self.assertIn("nothing was copied to %s" % self.disk, text)
        self.assertNotIn("verified copy", text)
        self.assertNotIn("rm -rf", text)

    def test_a_failure_before_the_copy_says_nothing_changed(self):
        """CONTROL: before the rename, "untouched" is true and said."""
        with mock.patch.object(chatnode, "_tree_allocated",
                               mock.Mock(side_effect=OSError("EACCES, "
                                                             "injected"))):
            rc, text = self.cli()
        self.assertEqual(rc, 1)
        self.assertIn("the source %s is untouched" % self.ram, text)
        self.assertIn("the record names %s" % self.ram, text)
        self.assertFalse(os.path.exists(self.disk))


class MoveUnansweredUnitTest(MoveBase):
    """unit_state() is None when systemctl cannot answer. With a unit
    installed, move cannot tell a running or restarting node from a stopped
    one, and the daemon-reload it needs would fail after a full copy: it
    refuses. With none installed there is no systemd node to ask about; the
    store's redb lock is the guard, and the message says so."""

    def test_an_installed_unit_systemctl_cannot_report_is_refused(self):
        os.makedirs(os.path.dirname(self.unit))
        with open(self.unit, "w") as f:
            f.write(chatnode.unit_text("/opt/dregg-node-rebased", self.ram))
        with mock.patch.object(chatnode, "unit_state", lambda *a: None):
            _msg, err = chatnode.move(self.disk)
        self.assertIn("systemctl cannot report", err or "")
        self.assertFalse(os.path.exists(self.disk))
        self.assertIsNone(self.recorded())
        self.assertEqual(_sha(self.store), self.shas["dregg.redb"])

    def test_no_unit_proceeds_and_names_the_redb_lock_as_the_guard(self):
        with mock.patch.object(chatnode, "unit_state", lambda *a: None):
            msg, err = chatnode.move(self.disk)
        self.assertIsNone(err, msg)
        self.assertIn("systemctl did not answer", msg)
        self.assertIn("redb lock", msg)
        self.assertEqual(self.recorded(), self.disk)

    def test_no_unit_and_a_held_store_is_still_refused(self):
        """The guard the message names is real: a held lock refuses."""
        fd = os.open(self.store, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(chatnode, "unit_state", lambda *a: None):
            _msg, err = chatnode.move(self.disk)
        self.assertIn("holds redb's lock", err or "")
        self.assertFalse(os.path.exists(self.disk))


class MovePathTest(MoveBase):
    """The unit writes the data dir unquoted into ExecStartPre/ExecStart:
    whitespace splits it, `%` is a systemd specifier, a newline ends the
    line."""

    def test_unit_unsafe_paths_are_refused(self):
        for name in ("a b", "a\tb", "50%", "a\nb", "a\x7fb"):
            dst = os.path.join(self.tmp, name, "data")
            _msg, err = chatnode.move(dst, unit_st=STOPPED)
            self.assertIn("cannot be the data dir", err or "", repr(name))
            self.assertFalse(os.path.exists(dst))
        self.assertEqual(_sha(self.store), self.shas["dregg.redb"])
        self.assertFalse(os.path.exists(chatnode.state_path()))

    def test_a_plain_path_is_taken(self):
        """CONTROL: dots, dashes and underscores are fine."""
        dst = os.path.join(self.tmp, "my-node_data.v2")
        _msg, err = chatnode.move(dst, unit_st=STOPPED)
        self.assertIsNone(err)
        self.assertEqual(self.recorded(), dst)


class StatusNamesLeftoversTest(MoveBase):
    def status(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from helm import chat
        out = io.StringIO()
        with mock.patch.object(chat, "node_url", lambda: "http://127.0.0.1:9"), \
                mock.patch.object(chat, "transport_status",
                                  lambda *a, **k: {"mode": "off"}), \
                mock.patch.object(chatnode.cell, "get_json",
                                  lambda *a, **k: None), \
                mock.patch.object(chatnode, "unreachable_line",
                                  lambda url: "unreachable"), \
                mock.patch.object(chatnode, "priority_line", lambda: None), \
                mock.patch.object(chatnode, "binary_report", lambda: None), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            chatnode._status([])
        return out.getvalue()

    def test_status_names_a_leftover_move_stage(self):
        parent = os.path.dirname(self.disk)
        os.makedirs(parent, exist_ok=True)
        stage = tempfile.mkdtemp(prefix=os.path.basename(self.disk)
                                 + chatnode.MOVE_INFIX, dir=parent)
        with open(os.path.join(stage, "dregg.redb"), "wb") as f:
            f.write(b"half a copy")
        text = self.status()
        self.assertIn(stage, text)
        self.assertIn("rm -rf " + stage, text)

    def test_status_is_quiet_without_one(self):
        """CONTROL: no stage, no stage line; a moved-aside source is not one."""
        chatnode.move(self.disk, unit_st=STOPPED)
        text = self.status()
        self.assertNotIn("move stage", text)
        self.assertIn("moved-aside", text)

class PlacementWordsTest(MoveBase):
    def test_a_tmpfs_store_says_it_cannot_be_dropped(self):
        with mock.patch.object(chatnode, "fs_type", lambda p: "tmpfs"):
            line = chatnode.placement_line(self.ram)
        self.assertIn("(tmpfs)", line)
        self.assertIn("never drop", line)

    def test_a_disk_store_says_the_page_cache_holds_it(self):
        with mock.patch.object(chatnode, "fs_type", lambda p: "ext4"):
            line = chatnode.placement_line(self.ram)
        self.assertIn("page cache", line)

    def test_the_store_size_is_its_files(self):
        apparent, _alloc = chatnode.store_bytes(self.ram)
        self.assertEqual(apparent, os.path.getsize(self.store))
        self.assertIsNone(chatnode.store_bytes(os.path.join(self.tmp, "no")))

    def test_the_verb_words(self):
        self.assertEqual(chatnode.placement_target("ram"), self.ram)
        self.assertEqual(chatnode.placement_target("disk"), self.disk)
        self.assertEqual(chatnode.placement_target("/x/y/"), "/x/y")
        self.assertIsNone(chatnode.placement_target("relative/dir"))

    def test_a_relative_record_is_not_taken(self):
        chatnode.write_state({"data_dir": "relative/dir"})
        self.assertEqual(chatnode.node_data_dir(), self.ram)

    def test_the_cli_refuses_without_a_target(self):
        self.assertEqual(chatnode.cmd_node(["move"]), 2)
        self.assertEqual(chatnode.cmd_node(["move", "--to", "nowhere"]), 2)


if __name__ == "__main__":
    unittest.main()
