#!/usr/bin/env python3
"""GUARD 2 — the mount plane (helm/scratch.py).

THE INCIDENT it exists for (ground-truthed live 2026-07-24): `No space left on
device` on /tmp while `df -h /tmp` read 36% used — a tmpfs with an explicit
nr_inodes=1048576 cap, 1,048,562 used, 14 free. Bytes green, inodes full,
invisible to every check on the estate.

Hermetic in the hard sense: every filesystem measurement runs off a SYNTHETIC
statvfs, every process probe off a synthetic /proc tree, and every reap runs
inside a tempdir. Nothing here reads or deletes a real mount — plus a tripwire
that pins the stop-guard's silent reaper OFF in every test module that drives
it, because that leg really does delete under /tmp/claude-*.
"""
import ast
import contextlib
import glob
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import projscope, scratch  # noqa: E402

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "helm")
TESTS = os.path.dirname(os.path.abspath(__file__))


class Stat(object):
    """A synthetic os.statvfs result. THE incident shape is the default:
    bytes comfortable, inodes exhausted."""

    def __init__(self, blocks=1000, bfree=640, bavail=640,
                 files=1048576, ffree=14, favail=14, frsize=4096):
        self.f_blocks, self.f_bfree, self.f_bavail = blocks, bfree, bavail
        self.f_files, self.f_ffree, self.f_favail = files, ffree, favail
        self.f_frsize = self.f_bsize = frsize


HOST_OK = {"level": "ok", "why": "", "avail_pct": 90, "swap_used_pct": 0,
           "psi": None, "trouble": None}

MOUNTS = (
    "tmpfs /dev/shm tmpfs rw,nosuid,nodev,inode64 0 0\n"
    "tmpfs /tmp tmpfs rw,nosuid,nodev,size=46004020k,nr_inodes=1048576,"
    "inode64 0 0\n"
    "/dev/nvme0n1p5 / ext4 rw,relatime 0 0\n"
)


class MountTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scratch-mt-")
        self.mounts = os.path.join(self.tmp, "mounts")
        with open(self.mounts, "w") as f:
            f.write(MOUNTS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_longest_prefix_wins(self):
        t = scratch.mount_table(self.mounts)
        self.assertEqual(scratch._mount_of("/tmp/claude-1000/x", t)[:2],
                         ("/tmp", "tmpfs"))
        self.assertEqual(scratch._mount_of("/home/tester/.helm", t)[:2],
                         ("/", "ext4"))
        self.assertEqual(scratch._mount_of("/dev/shm/helm-chat", t)[:2],
                         ("/dev/shm", "tmpfs"))

    def test_nr_inodes_cap_is_read_from_the_mount_options(self):
        """THE invisible fact: the cap lives in the mount options and nowhere
        in df -h output."""
        t = scratch.mount_table(self.mounts)
        self.assertEqual(scratch._nr_inodes(scratch._mount_of("/tmp", t)[2]),
                         1048576)
        self.assertIsNone(
            scratch._nr_inodes(scratch._mount_of("/dev/shm", t)[2]))

    def test_unreadable_mounts_file_is_not_a_crash(self):
        self.assertEqual(scratch.mount_table(self.mounts + ".nope"), [])


class UsageTest(unittest.TestCase):
    def test_inode_pressure_is_seen_when_bytes_are_low(self):
        """The whole point of the guard: 36% bytes, 100% inodes -> 100%."""
        with mock.patch.object(os, "statvfs", return_value=Stat()):
            u = scratch.usage("/tmp", [("/tmp", "tmpfs", "nr_inodes=1048576")])
        self.assertEqual(u["bytes_pct"], 36)
        self.assertEqual(u["inodes_pct"], 100)
        self.assertEqual(u["inodes_free"], 14)
        self.assertEqual(u["nr_inodes"], 1048576)
        self.assertEqual(scratch.worst_pct(u), 100)      # the WORSE axis wins
        self.assertEqual(scratch.level_of(scratch.worst_pct(u)), "critical")

    def test_a_filesystem_with_no_inode_accounting_reports_none(self):
        with mock.patch.object(os, "statvfs",
                               return_value=Stat(files=0, ffree=0, favail=0)):
            u = scratch.usage("/x", [])
        self.assertIsNone(u["inodes_pct"])               # never a div-by-zero
        self.assertEqual(scratch.worst_pct(u), 36)       # bytes axis still read

    def test_missing_mount_is_unmeasured_not_a_crash(self):
        """FAIL-OPEN: the check must never crash on a path that vanished."""
        with mock.patch.object(os, "statvfs", side_effect=OSError(2, "gone")):
            self.assertIsNone(scratch.usage("/gone"))

    def test_survey_dedupes_by_filesystem(self):
        with mock.patch.object(os, "statvfs", return_value=Stat()), \
             mock.patch.object(scratch, "_dev", return_value=7):
            rows = scratch.survey()
        self.assertEqual(len(rows), 1)                   # one fs, one row


class DoctorRowsTest(unittest.TestCase):
    def setUp(self):
        """The two NEW legs are stubbed here and driven in their own classes:
        the host axis reads /proc/meminfo, the unattributable listing walks
        real scratch roots and the TMPDIR route reads the real mount table,
        and this module's promise is that nothing here touches a real
        mount. Each is driven in its own class."""
        for target, value in (("host_memory", HOST_OK),
                              ("unattributable", []),
                              ("tmpdir_trouble", None)):
            patch = mock.patch.object(scratch, target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def fs_rows(self, out):
        return [row for row in out if row[1].startswith("fs ")]

    def rows(self, **kw):
        base = {"path": "/tmp", "mount": "/tmp", "fstype": "tmpfs",
                "volatile": True, "nr_inodes": 1048576, "label": "tmp",
                "bytes_used": 16 << 30, "bytes_avail": 28 << 30,
                "bytes_pct": 36, "inodes_total": 1048576,
                "inodes_used": 1048562, "inodes_free": 14, "inodes_pct": 100,
                "dev": 1}
        base.update(kw)
        return [base]

    def test_high_inodes_warn_even_when_bytes_are_low(self):
        with mock.patch.object(scratch, "top_offender", return_value=None), \
             mock.patch.object(scratch, "_last_gc", return_value=None):
            out = scratch.doctor_rows(self.rows())
        self.assertEqual(len(self.fs_rows(out)), 1)
        level, msg = self.fs_rows(out)[0]
        self.assertEqual(level, scratch.WARN)
        self.assertIn("INODES 100%", msg)
        self.assertIn("1048562 of 1048576", msg)
        self.assertIn("14 free", msg)
        self.assertIn("bytes are only 36%", msg)
        self.assertIn("df -h", msg)                 # names WHY it was invisible
        self.assertIn("No space left on device", msg)
        # the DURABLE pointer: both real fixes, by name
        self.assertIn("nr_inodes=1048576 cap", msg)
        self.assertIn("RAISE THE CAP", msg)
        self.assertIn("KEEP BIG TREES OFF IT", msg)
        self.assertIn("helm scratch big", msg)
        self.assertIn("helm scratch gc --apply", msg)

    def test_low_pressure_is_silent(self):
        with mock.patch.object(scratch, "_last_gc", return_value=None):
            out = scratch.doctor_rows(
                self.rows(bytes_pct=31, inodes_pct=42, inodes_free=600000))
        self.assertEqual([l for l, _ in out], [scratch.OK, scratch.OK])
        self.assertIn("bytes 31%, inodes 42%", out[0][1])
        self.assertIn("host memory", out[1][1])

    def test_bytes_pressure_warns_on_its_own(self):
        with mock.patch.object(scratch, "top_offender", return_value=None), \
             mock.patch.object(scratch, "_last_gc", return_value=None):
            out = scratch.doctor_rows(
                self.rows(bytes_pct=97, inodes_pct=3, inodes_free=900000))
        self.assertEqual(out[0][0], scratch.WARN)
        self.assertIn("BYTES 97%", out[0][1])
        self.assertNotIn("INODES", out[0][1])

    def test_top_offender_is_named_when_over(self):
        with mock.patch.object(scratch, "top_offender",
                               return_value=("/tmp/claude-1000/p/u", 88142,
                                             True)), \
             mock.patch.object(scratch, "_last_gc", return_value=None):
            out = scratch.doctor_rows(self.rows())
        self.assertIn("Top offender: /tmp/claude-1000/p/u (>=88142 entries)",
                      out[0][1])

    def test_a_crashing_offender_probe_does_not_lose_the_warning(self):
        with mock.patch.object(scratch, "top_offender",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(scratch, "_last_gc", return_value=None):
            out = scratch.doctor_rows(self.rows())
        self.assertEqual(out[0][0], scratch.WARN)

    def test_a_failed_survey_warns_loudly_instead_of_crashing(self):
        with mock.patch.object(scratch, "survey",
                               side_effect=RuntimeError("boom")):
            out = scratch.doctor_rows()
        self.assertEqual(out[0][0], scratch.WARN)
        self.assertIn("UNKNOWN", out[0][1])

    def test_the_last_applied_reap_is_surfaced(self):
        """LOUD: a reaper nobody can audit is not allowed."""
        with mock.patch.object(scratch, "_last_gc",
                               return_value="reaped 4 dirs (14000 files)"):
            out = scratch.doctor_rows(self.rows(bytes_pct=1, inodes_pct=1))
        self.assertIn("scratch gc: reaped 4 dirs", out[-1][1])

    def test_doctor_wires_the_check_in(self):
        from helm import doctor
        self.assertIn("check_filesystems", doctor.CHECKS)
        with mock.patch.object(scratch, "doctor_rows",
                               return_value=[(scratch.WARN, "inode pressure")]):
            self.assertEqual(doctor.check_filesystems(),
                             [("WARN", "inode pressure")])


class RoutingTest(unittest.TestCase):
    """The substrate picks the mount — the agent never guesses."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scratch-route-")
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_SCRATCH_DIR", "HELM_CACHE_DIR",
                                "HELM_SCRATCH_TMPDIR", "HELM_HOME")}
        for k in self.prior:
            os.environ.pop(k, None)
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.mounts = os.path.join(self.tmp, "mounts")
        with open(self.mounts, "w") as f:
            # the tempdir itself really IS under /tmp (tmpfs), so declare it a
            # DISK mount in the fixture — longest-prefix wins — or every
            # disk-destination assertion would be testing the host's /tmp
            f.write(MOUNTS + "/dev/nvme0n1p5 %s ext4 rw,relatime 0 0\n"
                    % os.path.realpath(self.tmp))
        self.mp = mock.patch.object(scratch, "MOUNTS", self.mounts)
        self.mp.start()

    def tearDown(self):
        self.mp.stop()
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def usages(self, tmp_stat, shm_stat):
        """Route with a synthetic per-path statvfs."""
        def fake(path):
            real = os.path.realpath(path)
            if real == "/dev/shm" or real.startswith("/dev/shm/"):
                return shm_stat
            if real == "/tmp" or real.startswith("/tmp/"):
                return tmp_stat
            return Stat(files=119169024, ffree=108662598, favail=108662598,
                        bfree=560, bavail=560)
        return mock.patch.object(os, "statvfs", side_effect=fake)

    ROOMY = dict(files=11501004, ffree=11455912, favail=11455912,
                 bfree=950, bavail=950)

    def test_a_big_tree_leaves_the_capped_mount_for_the_uncapped_one(self):
        """The live shape: /tmp capped and 86% inodes, /dev/shm uncapped at 1%.
        A clone tree (thousands of inodes at once) must not land on /tmp."""
        with self.usages(Stat(ffree=146975, favail=146975),
                         Stat(**self.ROOMY)):
            root, why = scratch.route("big")
        self.assertTrue(root.startswith("/dev/shm/"), root)
        self.assertIn("uncapped", why)

    def test_a_big_tree_falls_to_disk_when_no_ram_mount_has_headroom(self):
        with self.usages(Stat(ffree=14, favail=14), Stat(ffree=9, favail=9)):
            root, why = scratch.route("big")
        self.assertTrue(root.startswith(os.environ["HELM_CACHE_DIR"]), root)
        self.assertIn("disk", why)

    def test_small_scratch_stays_on_the_fast_ambient_tmp_when_healthy(self):
        with self.usages(Stat(files=11501004, ffree=11000000, favail=11000000,
                              bfree=950, bavail=950), Stat(**self.ROOMY)):
            root, why = scratch.route("small")
        self.assertTrue(root.startswith(tempfile.gettempdir()), root)
        self.assertIn("small+fast", why)

    def test_small_scratch_is_rerouted_off_a_pressured_mount(self):
        with self.usages(Stat(ffree=14, favail=14), Stat(**self.ROOMY)):
            root, why = scratch.route("small")
        self.assertTrue(root.startswith("/dev/shm/"), root)
        self.assertIn("under pressure", why)

    def test_durable_never_lands_on_volatile_tmpfs(self):
        """tmpfs dies with the boot — an artifact that must survive cannot
        live there, no matter how much headroom RAM has."""
        with self.usages(Stat(**self.ROOMY), Stat(**self.ROOMY)):
            root, why = scratch.route("durable")
        self.assertTrue(root.startswith(os.environ["HELM_CACHE_DIR"]), root)
        self.assertIn("durable never lands on volatile", why)

    def test_the_operator_override_wins_outright(self):
        os.environ["HELM_SCRATCH_DIR"] = os.path.join(self.tmp, "mine")
        with self.usages(Stat(**self.ROOMY), Stat(**self.ROOMY)):
            for cls in scratch.CLASSES:
                root, why = scratch.route(cls)
                self.assertTrue(root.startswith(os.environ["HELM_SCRATCH_DIR"]))
                self.assertIn("override", why)

    def test_an_unknown_class_refuses(self):
        self.assertRaises(ValueError, scratch.route, "enormous")

    def test_resolve_creates_a_session_keyed_dir(self):
        os.environ["HELM_SCRATCH_DIR"] = os.path.join(self.tmp, "mine")
        with mock.patch.object(scratch, "tag", return_value="pid-4242"):
            path, _why = scratch.resolve("big", name="Verify Clone")
        self.assertTrue(os.path.isdir(path))
        self.assertIn("pid-4242", path)        # attributable => reapable
        self.assertTrue(path.endswith("verify-clone"))

    def test_tag_prefers_the_ambient_session_id(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": sid}):
            self.assertEqual(scratch.tag(), sid)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "junk"}):
            self.assertTrue(scratch.tag().startswith("pid-"))

    def test_launch_env_routes_tmpdir_and_can_be_opted_out(self):
        from helm import launch
        os.environ["HELM_SCRATCH_DIR"] = os.path.join(self.tmp, "mine")
        env = launch.build_env({"PATH": "/bin"}, "alice")
        self.assertTrue(env["TMPDIR"].startswith(os.environ["HELM_SCRATCH_DIR"]))
        # an operator's own TMPDIR always wins
        env = launch.build_env({"TMPDIR": "/my/tmp"}, "alice")
        self.assertEqual(env["TMPDIR"], "/my/tmp")
        os.environ["HELM_SCRATCH_TMPDIR"] = "0"
        env = launch.build_env({"PATH": "/bin"}, "alice")
        self.assertNotIn("TMPDIR", env)

    def test_a_seats_tmpdir_never_shares_the_chat_bus_mount(self):
        """TMPDIR content is unbounded AND not session-attributable, so the
        reaper cannot reap it — it must never land on the mount carrying
        /dev/shm/helm-chat, or a seat's build cache could squeeze the fleet's
        message bus. Overflow goes to DISK, not the other RAM mount."""
        with self.usages(Stat(ffree=14, favail=14), Stat(**self.ROOMY)):
            tmpdir = scratch.launch_tmpdir()
            self.assertIsNotNone(tmpdir)
            self.assertFalse(tmpdir.startswith("/dev/shm"), tmpdir)
            self.assertTrue(tmpdir.startswith(os.environ["HELM_CACHE_DIR"]),
                            tmpdir)
            # …while `big` still gets the fast uncapped RAM mount (bounded,
            # inode-heavy-not-byte-heavy, and reapable because it IS tagged)
            self.assertTrue(scratch.route("big")[0].startswith("/dev/shm/"))

    HEALTHY_TMP = dict(files=11501004, ffree=11000000, favail=11000000,
                       bfree=950, bavail=950)

    def test_a_healthy_ambient_tmp_no_longer_keeps_TMPDIR_in_RAM(self):
        """THE STRUCTURAL CURE. A 60% headroom test on the ambient tmp is not
        enough to keep the routed TMPDIR there, because what it gates is
        UNBOUNDED and UNATTRIBUTABLE and no reaper can take it back.
        Measured: /tmp (a 24 GB tmpfs, so every byte of it is RAM) sat at 58%,
        the test passed, the routed target was `/tmp/helm-scratch/tmpdir`, and
        one seat's 9.9 GB measurement copy then took /tmp to its cap. The
        TAGGED classes are the control: they still take the fast RAM mounts in
        the same call, because they are reapable."""
        # A PERMISSIVE UMASK IS THE ONLY CONDITION UNDER WHICH THE chmod IS
        # OBSERVABLE. A node whose umask is already 077 hands makedirs a 0700
        # directory, so an arm that only reads the mode is measuring the
        # environment — a mutant deleting the chmod outright survived it.
        self.addCleanup(os.umask, os.umask(0o022))
        with self.usages(Stat(**self.HEALTHY_TMP), Stat(**self.ROOMY)):
            path = scratch.launch_tmpdir()
            self.assertTrue(path.startswith(os.environ["HELM_CACHE_DIR"]), path)
            self.assertFalse(path.startswith(
                os.path.join(tempfile.gettempdir(), scratch.SCRATCH_LEAF)),
                path)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
            sibling = os.path.join(os.path.dirname(path), "umask-control")
            os.makedirs(sibling)        # the control: what the umask alone gives
            self.assertNotEqual(stat.S_IMODE(os.stat(sibling).st_mode), 0o700)
            self.assertTrue(scratch.route("small")[0].startswith(
                tempfile.gettempdir()))
            self.assertTrue(scratch.route("big")[0].startswith("/dev/shm/"))

    def test_every_TMPDIR_root_a_running_seat_holds_stays_in_the_scan_set(self):
        """A SEAT KEEPS THE ENV IT LAUNCHED WITH. A change to this default
        reaches a running seat never — its TMPDIR, and the `claude-<uid>` root
        the harness derived from it, stay where they were until it next
        launches. So every root the routing can name has to stay in the
        reaper's scan set; dropping one makes a live seat's whole harness
        scratch estate invisible."""
        with self.usages(Stat(**self.HEALTHY_TMP), Stat(**self.ROOMY)):
            roots = scratch.tmpdir_roots()
            specs = [glob_ for glob_, _label in scratch.scratch_specs()]
            self.assertEqual(roots[0], scratch.tmpdir_root())
        was = os.path.join(tempfile.gettempdir(), scratch.SCRATCH_LEAF,
                           "tmpdir")
        self.assertIn(was, roots)
        self.assertNotEqual(roots[0], was)
        for root in roots:
            self.assertIn(os.path.join(root, "claude-*", "*", "*"), specs)

    def test_the_operator_still_wins_the_TMPDIR_route(self):  # noqa: VACUOUS_ASSERTION — the pinned-path half asserts a concrete routed path through the same tmpdir_root() immediately before the off-switch half asserts None
        pinned = os.path.join(self.tmp, "pinned")
        os.environ["HELM_SCRATCH_TMPDIR"] = pinned
        with self.usages(Stat(**self.HEALTHY_TMP), Stat(**self.ROOMY)):
            self.assertEqual(scratch.tmpdir_root(),
                             os.path.join(pinned, "tmpdir"))
        os.environ["HELM_SCRATCH_TMPDIR"] = "0"
        self.assertEqual(scratch.tmpdir_roots(), [])
        self.assertIsNone(scratch.tmpdir_root())
        self.assertIsNone(scratch.launch_tmpdir())
        os.environ.pop("HELM_SCRATCH_TMPDIR")
        os.environ["HELM_SCRATCH_DIR"] = os.path.join(self.tmp, "mine")
        with self.usages(Stat(**self.HEALTHY_TMP), Stat(**self.ROOMY)):
            self.assertEqual(
                scratch.tmpdir_root(),
                os.path.join(self.tmp, "mine", "small", "tmpdir"))

    def test_an_operator_override_ADDS_a_root_it_does_not_drop_the_others(self):
        """L. The override won the SCAN SET as well as the route, so the
        moment an operator set HELM_SCRATCH_DIR every root a running seat was
        still writing into went invisible to the reaper — and a seat keeps the
        env it launched with for as long as it lives."""
        was = os.path.join(tempfile.gettempdir(), scratch.SCRATCH_LEAF,
                           "tmpdir")
        with self.usages(Stat(**self.HEALTHY_TMP), Stat(**self.ROOMY)):
            before = scratch.tmpdir_roots()
            os.environ["HELM_SCRATCH_DIR"] = os.path.join(self.tmp, "mine")
            after = scratch.tmpdir_roots()
        self.assertIn(was, before)                       # the control
        self.assertEqual(after[0],
                         os.path.join(self.tmp, "mine", "small", "tmpdir"))
        for root in before:
            self.assertIn(root, after)

    def test_a_TMPDIR_route_that_fell_back_to_RAM_says_so(self):
        """L. `_disk_root` returns a second value saying whether it actually
        FOUND a non-volatile root, and this route ignored it — so on a host
        with no disk candidate `DISK, unconditionally` silently put an
        unbounded, unreapable estate back on RAM. Silence was the bug."""
        with self.usages(Stat(**self.HEALTHY_TMP), Stat(**self.ROOMY)):
            self.assertIsNone(scratch.tmpdir_trouble())       # the control
            with mock.patch.object(scratch, "_disk_root",
                                   return_value=("/tmp/nowhere", False)):
                trouble = scratch.tmpdir_trouble()
                rows = scratch.doctor_rows(
                    [{"mount": "/tmp", "label": "tmp", "fstype": "tmpfs",
                      "volatile": True, "dev": 1, "bytes_pct": 1,
                      "inodes_pct": 1, "inodes_total": 10, "inodes_used": 1,
                      "inodes_free": 9, "bytes_avail": 1, "nr_inodes": None}])
        self.assertIsNotNone(trouble)
        self.assertIn("NOT on a non-volatile mount", trouble)
        self.assertTrue([r for r in rows if "scratch TMPDIR" in r[1]],
                        "the doctor row never carried the fallback")


class LiveEvidenceTest(unittest.TestCase):
    """The anti-overbearing gate: LIVENESS BEFORE AGE, off a synthetic /proc."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scratch-live-")
        self.proc = os.path.join(self.tmp, "proc")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, pid, cmdline=b"python3\0x\0", environ=b"A=b\0", cwd=None,
            unreadable=None):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        for leaf, blob in (("cmdline", cmdline), ("environ", environ)):
            with open(os.path.join(d, leaf), "wb") as f:
                f.write(blob)
        if cwd:
            os.symlink(cwd, os.path.join(d, "cwd"))
        if unreadable:
            os.chmod(os.path.join(d, unreadable), 0)
        return d

    SID = "11111111-2222-3333-4444-555555555555"

    def test_a_session_id_in_a_live_cmdline_is_liveness(self):
        self.add(11, cmdline=("claude --resume " + self.SID).encode())
        live, cwds, trouble = scratch.live_evidence(self.proc)
        self.assertIsNone(trouble)
        self.assertIn(self.SID, live["ids"])
        self.assertEqual(cwds, set())

    def test_a_session_id_in_a_live_environ_is_liveness(self):
        self.add(12, environ=("CLAUDE_CODE_SESSION_ID=" + self.SID).encode())
        live, _c, _t = scratch.live_evidence(self.proc)
        self.assertIn(self.SID, live["ids"])

    def test_a_live_cwd_is_collected(self):
        target = os.path.join(self.tmp, "work")
        os.makedirs(target)
        self.add(13, cwd=target)
        _live, cwds, _t = scratch.live_evidence(self.proc)
        self.assertIn(os.path.realpath(target), cwds)

    def test_an_unlistable_process_table_is_trouble(self):
        live, cwds, trouble = scratch.live_evidence(
            os.path.join(self.tmp, "nope"))
        self.assertIsNone(live)
        self.assertIsNone(cwds)
        self.assertIn("unlistable", trouble)

    def test_a_not_dumpable_process_is_opaque_not_trouble(self):
        """THE live-run bug: `systemd --user` is uid-1000 with an unreadable
        environ. Failing closed on that one PermissionError disabled the reaper
        permanently on every real host."""
        self.add(14, cmdline=("claude " + self.SID).encode())
        self.add(15, unreadable="environ")
        live, _c, trouble = scratch.live_evidence(self.proc)
        self.assertIsNone(trouble)
        self.assertEqual(live["opaque"], 1)
        self.assertIn(self.SID, live["ids"])       # the real evidence survives

    def test_the_probe_is_bounded_AND_a_cut_list_never_answers(self):
        """THE BOUND STAYS; WHAT IT MAY CONCLUDE CHANGES. /proc readdir is
        ascending pid order, so a truncated process list drops the most
        recently started processes — exactly where a just-launched agent
        lives. A cut list therefore proves nothing about liveness, and the
        whole pass is UNKNOWN rather than one session being called dead."""
        for pid in range(100, 140):
            self.add(pid)
        with mock.patch.object(scratch, "PROC_BUDGET", 5):
            live, cwds, trouble = scratch.live_evidence(self.proc)
        self.assertIsNone(live)
        self.assertIsNone(cwds)
        self.assertIn("UNKNOWN", trouble)
        self.assertIn("process table", trouble)
        # CONTROL: the identical fixture with a budget that fits answers
        live, _c, trouble = scratch.live_evidence(self.proc)
        self.assertIsNone(trouble)
        self.assertEqual(len(live["pids"]), 40)


class GcTest(unittest.TestCase):
    """The reaper, entirely inside a tempdir: no real mount, no real /proc."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scratch-gc-")
        self.root = os.path.join(self.tmp, "scratch")
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_CACHE_DIR", "HELM_HOME",
                                "HELM_SCRATCH_GC")}
        for k in self.prior:
            os.environ.pop(k, None)
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.specs = [(os.path.join(self.root, "*"), "test scratch")]
        # THE THIRD PLANE READS THE LIVE BOX unless an arm owns it. The
        # automatic-leg arms in this class are about the mount and host
        # planes, and a real seat pressing on whatever box runs them must not
        # decide them; SeatPressurePlaneTest owns that plane in full.
        calm = mock.patch.object(scratch, "seat_plane", return_value={
            "slices": {}, "sessions": {}, "slice_seats": {}, "trouble": None})
        calm.start()
        self.addCleanup(calm.stop)

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    LIVE = "aaaaaaaa-1111-2222-3333-444444444444"
    DEAD = "bbbbbbbb-1111-2222-3333-444444444444"
    DEAD2 = "cccccccc-1111-2222-3333-444444444444"

    def tree(self, tag, files=3, age_h=48, sub=None):
        d = os.path.join(self.root, tag, sub) if sub \
            else os.path.join(self.root, tag)
        os.makedirs(d, exist_ok=True)
        for i in range(files):
            with open(os.path.join(d, "f%d" % i), "w") as f:
                f.write("x")
        old = time.time() - age_h * 3600
        for base, _dirs, fs in os.walk(os.path.join(self.root, tag)):
            for f in fs:
                os.utime(os.path.join(base, f), (old, old))
            os.utime(base, (old, old))
        return os.path.join(self.root, tag)

    def live_proc(self, sid):
        d = os.path.join(self.proc, "999")
        os.makedirs(d, exist_ok=True)
        for leaf, blob in (("cmdline", ("claude " + sid).encode()),
                           ("environ", b"A=b\0")):
            with open(os.path.join(d, leaf), "wb") as f:
                f.write(blob)

    def gc(self, apply=False, pct=100, **kw):
        rows = [{"bytes_pct": 36, "inodes_pct": pct, "mount": "/tmp",
                 "dev": 1, "inodes_total": 1048576}]
        return scratch.gc(apply=apply, specs=self.specs, proc_dir=self.proc,
                          rows=rows, **kw)

    # ── the anti-overbearing pin (the strong one) ─────────────────────────
    def test_a_live_sessions_scratch_is_never_touched(self):
        self.tree(self.LIVE)
        self.tree(self.DEAD)
        self.live_proc(self.LIVE)
        rep = self.gc(apply=True)
        self.assertTrue(os.path.isdir(os.path.join(self.root, self.LIVE)))
        self.assertFalse(os.path.isdir(os.path.join(self.root, self.DEAD)))
        self.assertEqual([w for _p, w in rep["kept"]],
                         ["session id referenced by a live process"])
        self.assertEqual(rep["reaped"], 1)

    def test_a_live_cwd_inside_the_tree_protects_it(self):
        d = self.tree(self.DEAD)
        os.makedirs(os.path.join(self.proc, "998"))
        for leaf in ("cmdline", "environ"):
            with open(os.path.join(self.proc, "998", leaf), "wb") as f:
                f.write(b"bash\0")
        os.symlink(d, os.path.join(self.proc, "998", "cwd"))
        rep = self.gc(apply=True)
        self.assertTrue(os.path.isdir(d))
        self.assertIn("cwd'd inside it", rep["kept"][0][1])

    def test_a_live_pid_tag_protects_its_tree(self):
        self.tree("pid-%d" % os.getpid())
        os.makedirs(os.path.join(self.proc, str(os.getpid())), exist_ok=True)
        for leaf in ("cmdline", "environ"):
            with open(os.path.join(self.proc, str(os.getpid()), leaf), "wb") as f:
                f.write(b"python3\0")
        rep = self.gc(apply=True)
        self.assertEqual(rep["reaped"], 0)
        self.assertIn("is alive", rep["kept"][0][1])

    def test_probe_trouble_reaps_nothing(self):
        self.tree(self.DEAD)
        rep = scratch.gc(apply=True, specs=self.specs,
                         proc_dir=os.path.join(self.tmp, "nope"),
                         rows=[{"bytes_pct": 1, "inodes_pct": 100,
                                "mount": "/x", "dev": 1,
                                "inodes_total": 10}])
        self.assertTrue(os.path.isdir(os.path.join(self.root, self.DEAD)))
        self.assertEqual(rep["reaped"], 0)
        self.assertIn("liveness UNKNOWN", rep["trouble"])

    def test_fresh_scratch_is_never_reaped(self):
        self.tree(self.DEAD, age_h=0)
        rep = self.gc(apply=True)
        self.assertEqual(rep["aged"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self.root, self.DEAD)))

    def test_an_unattributable_dir_is_never_a_candidate(self):
        """Only a session-id / pid-<n> child is ever considered — anything
        helm cannot attribute is left alone, forever."""
        self.tree("my-important-work")
        self.tree("node_modules")
        rep = self.gc(apply=True)
        self.assertEqual(rep["candidates"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self.root,
                                                   "my-important-work")))

    def test_a_transcript_subtree_is_structurally_off_limits(self):
        """corpus.py backs `**/projects/**/*.jsonl` up as TRAINING CORPUS —
        the reaper must never be able to delete the authored record."""
        self.tree(self.DEAD, sub=os.path.join("projects", "slug"))
        with mock.patch.object(scratch, "COUNT_CAP", 1):
            rep = self.gc(apply=True)
        self.assertEqual(rep["reaped"], 0)
        self.assertIn("training corpus is never reapable", rep["kept"][0][1])
        self.assertTrue(os.path.isdir(os.path.join(self.root, self.DEAD)))

    # ── pressure escalation ───────────────────────────────────────────────
    def test_pressure_escalates_the_ttl(self):
        self.assertEqual(self.gc(pct=1)["ttl"], scratch.TTL_BY_LEVEL["ok"])
        self.assertEqual(self.gc(pct=88)["ttl"], scratch.TTL_BY_LEVEL["warn"])
        self.assertEqual(self.gc(pct=99)["ttl"],
                         scratch.TTL_BY_LEVEL["critical"])

    def test_a_tree_reapable_under_pressure_survives_at_rest(self):
        self.tree(self.DEAD, age_h=12)             # older than 6h, under 7d
        self.assertEqual(self.gc(apply=True, pct=1)["reaped"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self.root, self.DEAD)))
        self.assertEqual(self.gc(apply=True, pct=99)["reaped"], 1)

    def test_biggest_trees_reap_first(self):
        self.tree(self.DEAD, files=2)
        self.tree(self.DEAD2, files=9)
        rep = self.gc()
        self.assertEqual(rep["victims"][0]["tag"], self.DEAD2)

    # ── bounds, idempotence, fail-open, loudness ──────────────────────────
    def test_candidate_scan_returns_every_attributable_tree(self):
        first = self.tree(self.DEAD)
        second = self.tree(self.DEAD2)
        with mock.patch.object(glob, "iglob",
                               return_value=iter((first, second))):
            rows = scratch.candidates(specs=[("ignored", "test")])
        self.assertEqual([row[0] for row in rows], [first, second])

    def test_the_pass_is_bounded(self):
        for i in range(12):
            self.tree("bbbbbbbb-1111-2222-3333-%012d" % i)
        self.assertEqual(self.gc()["candidates"], 12)
        with mock.patch.object(scratch, "AGED_CAP", 3):
            rep = self.gc()
        self.assertEqual(len(rep["victims"]), 3)
        self.assertTrue(rep["capped"])
        with mock.patch.object(scratch, "REAP_CAP", 2):
            self.assertEqual(len(self.gc()["victims"]), 2)

    def test_count_entries_is_capped_and_says_the_count_is_a_floor(self):  # noqa: VACUOUS_ASSERTION — the second half reads the SAME tree with the real cap and asserts the concrete (30, True), so the capped half cannot be measuring an empty walk
        self.tree(self.DEAD, files=30)
        with mock.patch.object(scratch, "COUNT_CAP", 10):
            n, _prot, complete = scratch.count_entries(
                os.path.join(self.root, self.DEAD))
        self.assertEqual((n, complete), (10, False))
        # CONTROL: the same tree read whole
        n, _prot, complete = scratch.count_entries(
            os.path.join(self.root, self.DEAD))
        self.assertEqual((n, complete), (30, True))

    def test_budget_check_precedes_directory_iterator_advance(self):  # noqa: VACUOUS_ASSERTION — each hostile cursor raises if advanced; Expired proves the spending door fired first
        class Cursor:
            advanced = False

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def __next__(self):
                self.advanced = True
                return mock.Mock()

        def expire_on(boundary):
            def spend(what):
                if what == boundary:
                    raise projscope.Expired("test expiry")
            return spend

        count_cursor = Cursor()
        with mock.patch.object(os, "scandir", return_value=count_cursor), \
             mock.patch.object(projscope, "spend_or_raise",
                               side_effect=expire_on("counting scratch tree entry")):
            with self.assertRaises(projscope.Expired):
                scratch.count_entries("/synthetic")
        self.assertFalse(count_cursor.advanced)

        mtime_cursor = Cursor()
        with mock.patch.object(os, "scandir", return_value=mtime_cursor), \
             mock.patch.object(os.path, "getmtime", return_value=1), \
             mock.patch.object(projscope, "spend_or_raise",
                               side_effect=expire_on("scanning scratch tree mtime")):
            with self.assertRaises(projscope.Expired):
                scratch._newest_mtime("/synthetic")
        self.assertFalse(mtime_cursor.advanced)

    # ── round 3: the readings tier ONE was still concluding from ─────────
    def padded_proc(self, pid, sid, pad):
        """A live process whose session id sits `pad` bytes into its environ —
        the shape a truncating blob read cannot see."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"claude\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"X" * pad + ("SID=%s\0" % sid).encode())
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%s (claude) S 1 0 0 0 0\n" % pid)

    def test_a_session_id_past_the_environ_read_keeps_the_WHOLE_tree(self):
        """ROUND 3, THE SECOND SIBLING AND THE MOST DESTRUCTIVE. A blob read
        that stops has not read the process's environment, so the session ids
        it did not see are UNKNOWN — and the id it missed belongs to a RUNNING
        session whose entire tree tier one was about to remove. There is no
        unit to attach the doubt to, so the whole pass is UNKNOWN."""
        tree = self.tree(self.LIVE)
        self.padded_proc(4242, self.LIVE, scratch.BLOB_CAP + 4096)
        rep = self.gc(apply=True)
        self.assertEqual(rep["victims"], [])
        self.assertTrue(os.path.isdir(tree))
        self.assertIn("UNKNOWN", rep["trouble"])
        self.assertIn("environ", rep["trouble"])

    def test_a_session_id_INSIDE_the_environ_read_is_liveness_as_before(self):
        """The CONTROL: the same id, the same process, no padding. It must be
        KEPT for LIVENESS and not for a bound — otherwise the arm above proves
        only that this module keeps things when it is confused."""
        tree = self.tree(self.LIVE)
        self.padded_proc(4242, self.LIVE, 0)
        rep = self.gc(apply=True)
        self.assertEqual(rep["victims"], [])
        self.assertIsNone(rep["trouble"])
        self.assertTrue(os.path.isdir(tree))
        self.assertIn("session id referenced by a live process",
                      [why for _p, why in rep["kept"]])

    def test_a_TRANSCRIPT_past_tier_ones_entry_count_keeps_the_tree(self):
        """The same fourth sibling, one tier up: tier one may age from a
        SAMPLE (its liveness gate proved the session dead, so nobody is
        writing into the part the walk missed) but it may not decide the
        transcript refusal from one — that refusal is about a directory
        EXISTING, and a count that stopped has not looked for it."""
        dead = self.tree(self.DEAD, files=8)
        home = os.path.join(dead, "home")
        os.makedirs(os.path.join(home, "projects", "p1"))
        transcript = os.path.join(home, "projects", "p1", "t.jsonl")
        with open(transcript, "w") as f:
            f.write("{}\n")
        old = time.time() - 48 * 3600
        for base, _dirs, files in os.walk(dead):
            for name in files:
                os.utime(os.path.join(base, name), (old, old))
            os.utime(base, (old, old))
        os.utime(dead, (old, old))
        with mock.patch.object(scratch, "COUNT_CAP", 3):
            rep = self.gc(apply=True)
        self.assertEqual(rep["victims"], [])
        self.assertTrue(os.path.exists(transcript))
        why = [w for p, w in rep["kept"] if p == dead][0]
        self.assertIn("UNKNOWN", why)
        self.assertIn("transcript", why)
        self.assertEqual(rep["unknown"]["transcript"], 1)
        self.assertIn("KEPT as UNKNOWN", scratch.summary(rep))
        # CONTROL: the identical tree with a count that finishes is refused
        # STRUCTURALLY, naming the corpus rather than the bound
        rep = self.gc(apply=True)
        self.assertEqual(rep["victims"], [])
        self.assertIn("training corpus",
                      [w for p, w in rep["kept"] if p == dead][0])

    def test_a_WORKTREE_past_tier_ones_scan_cap_keeps_the_tree(self):
        """And the third sibling one tier up. A dead session's tree can hold a
        checkout too — that is what `git worktree add $(mktemp -d)` makes."""
        dead = self.tree(self.DEAD)
        for i in range(6):
            os.makedirs(os.path.join(dead, "pad%d" % i))
        old = time.time() - 48 * 3600
        for base, _d, _f in os.walk(dead):
            os.utime(base, (old, old))
        with mock.patch.object(scratch, "WT_SCAN_CAP", 2):
            rep = self.gc(apply=True)
        self.assertEqual(rep["victims"], [])
        self.assertTrue(os.path.isdir(dead))
        why = [w for p, w in rep["kept"] if p == dead][0]
        self.assertIn("worktree registration scan", why)
        self.assertEqual(rep["unknown"]["worktree"], 1)
        # CONTROL: the same tree, scan complete, IS a victim
        rep = self.gc(apply=True)
        self.assertEqual([v["path"] for v in rep["victims"]], [dead])

    def test_the_SAMPLED_flag_reaches_the_audited_line_and_the_CLI(self):  # noqa: VACUOUS_ASSERTION — the assertIn halves on summary() and the rendered CLI fire before the unsampled control's assertNotIn
        """L. `The sample is RECORDED on the victim so the report never passes
        it off as a full reading` was true of the dict and false of every
        surface an owner reads."""
        dead = self.tree(self.DEAD, files=8)
        with mock.patch.object(scratch, "MTIME_STAT_CAP", 2):
            rep = self.gc(apply=True)
        self.assertEqual(rep["sampled"], 1)
        self.assertTrue(rep["victims"][0]["sampled"])
        self.assertIn("SAMPLED", scratch.summary(rep))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scratch._print_gc(rep, True)
        self.assertIn("SAMPLED", out.getvalue())
        # CONTROL: the same tree read whole is not marked
        dead = self.tree(self.DEAD2, files=8)
        rep = self.gc(apply=True)
        self.assertEqual(rep["sampled"], 0)
        self.assertNotIn("SAMPLED", scratch.summary(rep))

    def test_the_DRY_RUN_ends_with_a_total_before_the_unattributable_block(self):
        """L. The dry run is what an operator reads BEFORE authorising an
        apply, and it ended on the list of what the pass will NOT touch. The
        eighty-eight `would reap` lines now add up to something."""
        self.tree(self.DEAD)
        self.tree(self.DEAD2)
        rep = self.gc(list_unattributable=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scratch._print_gc(rep, False)
        text = out.getvalue()
        self.assertIn("WOULD REAP", text)
        self.assertIn("TOTAL", text)
        self.assertIn("tier 1 (dead sessions)", text)
        self.assertLess(text.index("TOTAL"), len(text))
        self.assertIn("2 units", text)

    def test_the_tier2_kill_switch_shows_its_OFF_row_in_the_dry_run(self):
        """L. HELM_SCRATCH_GC_TIER2=0 short-circuits before tier2_scan, so the
        row that function owns never appeared: an operator who takes the
        documented mitigation could not see it in the report."""
        self.tree(self.DEAD)
        os.environ["HELM_SCRATCH_GC_TIER2"] = "0"
        try:
            rep = self.gc()
        finally:
            os.environ.pop("HELM_SCRATCH_GC_TIER2", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scratch._print_gc(rep, False)
        self.assertIn("HELM_SCRATCH_GC_TIER2=0", out.getvalue())
        # CONTROL: with the tier on, the row is absent
        rep = self.gc()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            scratch._print_gc(rep, False)
        self.assertNotIn("HELM_SCRATCH_GC_TIER2=0", out.getvalue())

    def test_dry_run_is_the_default_and_removes_nothing(self):
        self.tree(self.DEAD)
        rep = scratch.gc(specs=self.specs, proc_dir=self.proc,
                         rows=[{"bytes_pct": 1, "inodes_pct": 100,
                                "mount": "/x", "dev": 1, "inodes_total": 10}])
        self.assertEqual(rep["reaped"], 0)
        self.assertEqual(len(rep["victims"]), 1)
        self.assertTrue(os.path.isdir(os.path.join(self.root, self.DEAD)))

    def test_a_second_pass_is_a_no_op(self):
        self.tree(self.DEAD)
        self.assertEqual(self.gc(apply=True)["reaped"], 1)
        rep = self.gc(apply=True)
        self.assertEqual((rep["reaped"], rep["candidates"]), (0, 0))

    def test_a_locked_victim_is_skipped_loudly_not_fatally(self):
        self.tree(self.DEAD)
        self.tree(self.DEAD2)
        with mock.patch.object(shutil, "rmtree",
                               side_effect=[OSError(13, "denied"), None]):
            rep = self.gc(apply=True)
        self.assertEqual(rep["reaped"], 1)
        self.assertTrue(any(v.get("error") for v in rep["victims"]))

    def test_expiry_before_deletion_starts_no_victim(self):  # noqa: VACUOUS_ASSERTION — the planted victim is the positive control; its continued existence and zero remove calls prove no deletion began
        path = self.tree(self.DEAD)

        def spend(what):
            if what == "deleting scratch victim":
                raise projscope.Expired("test expiry")

        with mock.patch.object(projscope, "spend_or_raise", side_effect=spend), \
             mock.patch.object(shutil, "rmtree") as remove:
            with self.assertRaises(projscope.Expired):
                self.gc(apply=True)
        self.assertFalse(remove.called)
        self.assertTrue(os.path.isdir(path))

    def test_budgeted_gc_uses_cooperative_deletion(self):  # noqa: VACUOUS_ASSERTION — reaped=1 and absent victim prove deletion ran while zero rmtree calls pins the cooperative path
        path = self.tree(self.DEAD)
        with projscope.scope(deadline=time.monotonic() + 60), \
             mock.patch.object(shutil, "rmtree") as remove:
            rep = self.gc(apply=True)
        self.assertFalse(remove.called)
        self.assertEqual(rep["reaped"], 1)
        self.assertFalse(os.path.exists(path))

    def test_budgeted_delete_never_follows_a_swapped_child_symlink(self):
        victim = os.path.join(self.root, self.DEAD)
        os.makedirs(victim)
        child = os.path.join(victim, "child")
        os.makedirs(child)
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        marker = os.path.join(outside, "keep")
        with open(marker, "w") as f:
            f.write("keep")
        moved = os.path.join(self.root, "moved-child")

        class Entry:
            name = "child"

            def is_dir(self, follow_symlinks=False):
                os.rename(child, moved)
                os.symlink(outside, child)
                return True

        @contextlib.contextmanager
        def scan(target):
            if isinstance(target, int):
                yield iter((Entry(),))
            else:
                with os.scandir(target) as entries:
                    yield entries

        with projscope.scope(deadline=time.monotonic() + 60), \
             mock.patch.object(os, "scandir", side_effect=scan):
            removed = [0]
            scratch._remove_tree(victim, removed)
        self.assertTrue(os.path.isfile(marker))
        self.assertTrue(os.path.isdir(moved))
        self.assertFalse(os.path.exists(victim))

    def test_expiry_during_scan_prevents_later_deletion(self):  # noqa: VACUOUS_ASSERTION — the planted victim survives and the recursive remover stays untouched after a must-hit scan expiry
        path = self.tree(self.DEAD)

        def spend(what):
            if what == "counting scratch tree entry":
                raise projscope.Expired("test expiry")

        with mock.patch.object(projscope, "spend_or_raise", side_effect=spend), \
             mock.patch.object(shutil, "rmtree") as remove:
            with self.assertRaises(projscope.Expired):
                self.gc(apply=True)
        self.assertFalse(remove.called)
        self.assertTrue(os.path.isdir(path))

    def test_admitted_delete_is_accounted_before_next_expiry(self):  # noqa: VACUOUS_ASSERTION — first victim absence is the positive mutation control; second presence and the audit event pin the expiry boundary
        first = self.tree(self.DEAD, files=5)
        second = self.tree(self.DEAD2, files=1)
        deleted = False
        real_rmtree = shutil.rmtree

        def remove(path):
            nonlocal deleted
            real_rmtree(path)
            deleted = True

        def spend(what):
            if what == "deleting scratch victim" and deleted:
                raise projscope.Expired("test expiry")

        with mock.patch.object(projscope, "spend_or_raise", side_effect=spend), \
             mock.patch.object(shutil, "rmtree", side_effect=remove), \
             mock.patch.object(scratch.pk, "event") as event:
            with self.assertRaises(projscope.Expired):
                self.gc(apply=True)
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.isdir(second))
        self.assertEqual(event.call_count, 1)
        self.assertIn("reaped 1", event.call_args[0][2])

    def test_an_applied_pass_writes_an_audit_event(self):
        from helm import pk
        self.tree(self.DEAD)
        with mock.patch.object(pk, "event") as ev:
            self.gc(apply=True)
        self.assertTrue(ev.called)
        self.assertEqual(ev.call_args[0][:2], ("scratch", "gc"))
        self.assertIn("reaped 1", ev.call_args[0][2])

    # ── the automatic leg ─────────────────────────────────────────────────
    def test_auto_gc_is_a_no_op_at_rest(self):
        """Pressure-gated: deleting files nobody needs deleted is its own
        failure mode. At rest the pass costs one statvfs per mount."""
        rows = [{"bytes_pct": 30, "inodes_pct": 40, "mount": "/x", "dev": 1,
                 "inodes_total": 10}]
        with mock.patch.object(scratch, "survey", return_value=rows), \
             mock.patch.object(scratch, "gc") as g:
            self.assertIsNone(scratch.auto_gc())
        self.assertFalse(g.called)

    def test_auto_gc_runs_under_pressure_and_then_throttles(self):
        rows = [{"bytes_pct": 36, "inodes_pct": 100, "mount": "/x", "dev": 1,
                 "inodes_total": 10}]
        rep = {"reaped": 2, "files": 9, "pressure": 100, "level": "critical",
               "ttl": 3600, "kept": [], "candidates": 4, "victims": []}
        with mock.patch.object(scratch, "survey", return_value=rows), \
             mock.patch.object(scratch, "gc", return_value=rep) as g:
            line = scratch.auto_gc()
            self.assertIn("reaped 2", line)
            self.assertTrue(g.call_args[1]["apply"])
            self.assertIsNone(scratch.auto_gc())      # throttled within the hour

    def test_auto_gc_kill_switch(self):
        os.environ["HELM_SCRATCH_GC"] = "0"
        with mock.patch.object(scratch, "survey") as s:
            self.assertIsNone(scratch.auto_gc())
        self.assertFalse(s.called)

    def test_expired_budget_never_checks_a_fresh_stamp(self):  # noqa: VACUOUS_ASSERTION — the stamp exists as the positive fixture; Expired plus zero getmtime calls proves refusal precedes reading it
        scratch._touch_stamp()
        real_getmtime = os.path.getmtime
        with projscope.scope(deadline=0), \
             mock.patch.object(os.path, "getmtime", wraps=real_getmtime) as stat:
            with self.assertRaises(projscope.Expired):
                scratch.auto_gc()
        self.assertFalse(stat.called)

    def test_auto_gc_never_raises(self):
        with mock.patch.object(scratch, "survey",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(scratch.auto_gc())

    def test_auto_gc_propagates_budget_expiry(self):  # noqa: VACUOUS_ASSERTION — the GC double raises Expired; absent stamp proves incomplete work was not throttled as complete
        rows = [{"bytes_pct": 36, "inodes_pct": 100, "mount": "/x", "dev": 1,
                 "inodes_total": 10}]
        with mock.patch.object(scratch, "survey", return_value=rows), \
             mock.patch.object(scratch, "gc",
                               side_effect=projscope.Expired("test expiry")):
            with self.assertRaises(projscope.Expired):
                scratch.auto_gc()
        self.assertFalse(os.path.exists(scratch._stamp_path()))

    def test_auto_gc_keeps_ordinary_gc_errors_fail_open(self):  # noqa: VACUOUS_ASSERTION — the GC double raises RuntimeError; None is the explicit janitor fail-open contract
        rows = [{"bytes_pct": 36, "inodes_pct": 100, "mount": "/x", "dev": 1,
                 "inodes_total": 10}]
        with mock.patch.object(scratch, "survey", return_value=rows), \
             mock.patch.object(scratch, "gc", side_effect=RuntimeError("boom")):
            self.assertIsNone(scratch.auto_gc())

    def test_auto_gc_wakes_on_host_memory_alone_on_a_ram_mount(self):
        """THE HOLE, closed at the gate that kept the reaper asleep: every mount
        axis reads `ok` and the box is 23 GB into swap."""
        rows = [{"bytes_pct": 36, "inodes_pct": 40, "mount": "/tmp", "dev": 1,
                 "inodes_total": 10, "volatile": True}]
        hot = {"level": "warn", "why": "swap 42% used (warn at 25)",
               "avail_pct": 40, "swap_used_pct": 42, "psi": None,
               "trouble": None}
        rep = {"reaped": 1, "tier2_reaped": 0, "files": 9, "pressure": 40,
               "level": "ok", "ttl": 3600, "kept": [], "candidates": 1,
               "victims": []}
        with mock.patch.object(scratch, "survey", return_value=rows), \
             mock.patch.object(scratch, "host_memory", return_value=hot), \
             mock.patch.object(scratch, "gc", return_value=rep) as g:
            self.assertIn("reaped 1", scratch.auto_gc())
        self.assertTrue(g.called)
        self.assertIs(g.call_args[1]["host"], hot)

    def test_auto_gc_ignores_host_memory_on_a_disk_only_estate(self):  # noqa: VACUOUS_ASSERTION — the volatile half asserts (True, True, True) through the same reader and the same pressured host before the disk half asserts the Nones
        """THE CONTROL PAIR, in one arm: the SAME pressured host over a RAM
        mount wakes the pass, and over a disk-only estate it does not. Nothing
        helm writes there is made of that memory, so nothing escalates."""
        hot = {"level": "warn", "why": "swap 42% used (warn at 25)",
               "avail_pct": 40, "swap_used_pct": 42, "psi": None,
               "trouble": None}
        rep = {"reaped": 1, "tier2_reaped": 0, "files": 9, "pressure": 40,
               "level": "ok", "ttl": 3600, "kept": [], "candidates": 1,
               "victims": []}

        def run(volatile):
            row = [{"bytes_pct": 36, "inodes_pct": 40, "mount": "/", "dev": 1,
                    "inodes_total": 10, "volatile": volatile}]
            with mock.patch.object(scratch, "survey", return_value=row), \
                 mock.patch.object(scratch, "host_memory",
                                   return_value=hot) as host, \
                 mock.patch.object(scratch, "gc", return_value=rep) as g:
                answer = scratch.auto_gc()
            return answer, g.called, host.called

        def unthrottle():
            try:
                os.remove(scratch._stamp_path())
            except OSError:
                pass

        unthrottle()
        woke, called, read = run(True)
        self.assertEqual((bool(woke), called, read), (True, True, True))
        unthrottle()
        slept, called, read = run(False)
        self.assertEqual((slept, called, read), (None, False, False))

    def test_the_stop_hook_runs_the_automatic_leg(self):
        """Wired to an EXISTING hook — helm adds no service."""
        from helm import seats
        with mock.patch.object(scratch, "auto_gc") as a:
            seats.stop_guard(session=None, room="main", seat="nobody")
        self.assertTrue(a.called)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scratch-cli-")
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_SCRATCH_DIR", "HELM_CACHE_DIR")}
        os.environ["HELM_SCRATCH_DIR"] = os.path.join(self.tmp, "s")
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, argv):
        import contextlib
        import io
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_the_verb_is_registered_with_help(self):
        from helm import cli
        self.assertIn("scratch", cli.VERBS)
        self.assertIn("scratch", cli._VERB_HELP)
        self.assertIn("INODES", cli._VERB_HELP["scratch"])

    def test_a_class_prints_a_usable_path(self):
        rc, out, err = self.run_cli(["scratch", "big"])
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.isdir(out.strip()), out)

    def test_status_reports_both_axes(self):
        rc, out, _e = self.run_cli(["scratch", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("bytes", out)
        self.assertIn("inodes", out)
        self.assertIn("routing", out)

    def test_an_unknown_subverb_refuses(self):
        rc, _o, err = self.run_cli(["scratch", "frobnicate"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown verb 'frobnicate'", err)

    def test_gc_rejects_a_bad_flag(self):
        rc, _o, err = self.run_cli(["scratch", "gc", "--yolo"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_the_unattributable_verb_lists_the_cost_and_the_manual_act(self):
        root = os.path.join(os.environ["HELM_SCRATCH_DIR"], "small")
        os.makedirs(os.path.join(root, "codex3-task2340-leg34"))
        with open(os.path.join(root, "codex3-task2340-leg34", "b"), "w") as f:
            f.write("x" * 4096)
        rc, out, err = self.run_cli(["scratch", "unattributable"])
        self.assertEqual(rc, 0, err)
        self.assertIn("codex3-task2340-leg34", out)
        self.assertIn("NEVER", out)
        self.assertIn("manual act", out)

    def test_the_unattributable_verb_is_silent_when_everything_is_named(self):
        root = os.path.join(os.environ["HELM_SCRATCH_DIR"], "small")
        os.makedirs(os.path.join(root, "aaaaaaaa-1111-2222-3333-444444444444"))
        rc, out, err = self.run_cli(["scratch", "unattributable"])
        self.assertEqual(rc, 0, err)
        self.assertIn("every child of every scratch root is attributable", out)

    def test_the_unattributable_verb_rejects_a_bad_flag(self):
        rc, _o, err = self.run_cli(["scratch", "unattributable", "--yolo"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err)

    def test_the_dry_run_shows_what_is_never_reapable(self):
        root = os.path.join(os.environ["HELM_SCRATCH_DIR"], "small")
        os.makedirs(os.path.join(root, "clientproj-host-test-96XAPt"))
        rc, out, err = self.run_cli(["scratch", "gc"])
        self.assertEqual(rc, 0, err)
        self.assertIn("UNATTRIBUTABLE", out)
        self.assertIn("clientproj-host-test-96XAPt", out)


class HostMemoryTest(unittest.TestCase):
    """THE SECOND INCIDENT'S DENOMINATOR. A tmpfs percentage answers "how full
    is this mount" and never "is the box out of memory" — measured on the
    owner's laptop, /tmp read 44% of its cap while the host was 23 GB into swap.
    Synthetic /proc files only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scratch-host-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def meminfo(self, total_kb=92007992, avail_kb=64000000,
                swap_total_kb=58720244, swap_free_kb=58720244):
        path = os.path.join(self.tmp, "meminfo")
        with open(path, "w") as f:
            f.write("MemTotal:       %d kB\n"
                    "MemFree:         1000000 kB\n"
                    "MemAvailable:   %d kB\n"
                    "SwapTotal:      %d kB\n"
                    "SwapFree:       %d kB\n"
                    % (total_kb, avail_kb, swap_total_kb, swap_free_kb))
        return path

    def psi(self, avg60):
        path = os.path.join(self.tmp, "pressure")
        with open(path, "w") as f:
            f.write("some avg10=0.00 avg60=%.2f avg300=0.00 total=1\n"
                    "full avg10=0.00 avg60=0.00 avg300=0.00 total=1\n" % avg60)
        return path

    def missing(self, name="nope"):
        return os.path.join(self.tmp, name)

    def test_a_healthy_host_is_ok(self):
        host = scratch.host_memory(self.meminfo(), self.missing())
        self.assertEqual((host["level"], host["trouble"]), ("ok", None))
        self.assertEqual(host["avail_pct"], 70)

    def test_low_memavailable_warns_then_goes_critical(self):
        warn = scratch.host_memory(self.meminfo(avail_kb=18000000),
                                   self.missing())
        self.assertEqual(warn["level"], "warn")
        self.assertIn("MemAvailable 20%", warn["why"])
        crit = scratch.host_memory(self.meminfo(avail_kb=7000000),
                                   self.missing())
        self.assertEqual(crit["level"], "critical")

    def test_swap_in_use_escalates_on_its_own(self):
        """THE MEASURED SHAPE: MemAvailable looks healthy precisely BECAUSE the
        box already swapped. The swap axis is the memory of that."""
        host = scratch.host_memory(
            self.meminfo(swap_free_kb=34201472), self.missing())
        self.assertEqual(host["level"], "warn")
        self.assertEqual(host["swap_used_pct"], 42)
        self.assertIn("swap 42% used", host["why"])

    def test_psi_stall_is_critical_on_its_own(self):
        host = scratch.host_memory(self.meminfo(), self.psi(30.0))
        self.assertEqual(host["level"], "critical")
        self.assertIn("PSI some avg60 30.0%", host["why"])

    def test_absent_psi_is_not_pressure(self):
        """A kernel without PSI has no file. Absence is not a reading."""
        host = scratch.host_memory(self.meminfo(), self.missing())
        self.assertIsNone(host["psi"])
        self.assertEqual(host["level"], "ok")

    def test_a_quiet_psi_file_does_not_escalate(self):  # noqa: VACUOUS_ASSERTION — the same reader on the same path asserts (30.0, "critical") unconditionally first, so a 0.0 reading is a read file
        """Paired on one reader and one file: the same path at 30% is critical,
        so a `0.0 -> ok` here is a reading and not an unread file."""
        loud = scratch.host_memory(self.meminfo(), self.psi(30.0))
        self.assertEqual((loud["psi"], loud["level"]), (30.0, "critical"))
        quiet = scratch.host_memory(self.meminfo(), self.psi(0.0))
        self.assertEqual((quiet["psi"], quiet["level"]), (0.0, "ok"))

    def test_unreadable_meminfo_fails_closed(self):
        """UNKNOWN never licenses a deletion: no level, no why, a named
        trouble."""
        host = scratch.host_memory(self.missing("meminfo"), self.missing())
        self.assertEqual(host["level"], "ok")
        self.assertIn("UNREADABLE", host["trouble"])
        self.assertIn("meminfo", host["trouble"])

    def test_a_truncated_meminfo_fails_closed(self):
        path = os.path.join(self.tmp, "partial")
        with open(path, "w") as f:
            f.write("MemTotal:       92007992 kB\n")   # no MemAvailable
        host = scratch.host_memory(path, self.missing())
        self.assertEqual(host["level"], "ok")
        self.assertIsNotNone(host["trouble"])

    def test_the_refusal_names_the_input_that_actually_failed(self):
        """A REFUSAL NAMES THE INPUT IT CHECKED. On an old kernel or inside a
        container /proc/meminfo reads perfectly and simply does not carry
        MemAvailable; calling that UNREADABLE sends the reader after a
        permission problem that does not exist. The unreadable half is the
        control: the same function, the same field, one readable file apart."""
        path = os.path.join(self.tmp, "partial")
        with open(path, "w") as f:
            f.write("MemTotal:       92007992 kB\n")
        parsed = scratch.host_memory(path, self.missing())["trouble"]
        self.assertIn("MemAvailable", parsed)
        self.assertIn("read fine", parsed)
        self.assertNotIn("UNREADABLE", parsed)
        gone = scratch.host_memory(self.missing("meminfo"),
                                   self.missing())["trouble"]
        self.assertIn("UNREADABLE", gone)
        self.assertNotIn("MemAvailable", gone)

    def test_no_swap_device_is_not_an_axis(self):
        host = scratch.host_memory(
            self.meminfo(swap_total_kb=0, swap_free_kb=0), self.missing())
        self.assertIsNone(host["swap_used_pct"])
        self.assertEqual(host["level"], "ok")

    def test_volatile_devs_reads_only_ram_mounts(self):
        rows = [{"volatile": True, "dev": 7}, {"volatile": False, "dev": 8},
                {"volatile": True, "dev": None}]
        self.assertEqual(scratch.volatile_devs(rows), {7})


class SecondTierTest(unittest.TestCase):
    """HOLE 2: liveness protects a session TREE and said nothing about its
    contents, so an integrator session that lives for days grew without bound —
    5.6 GB, 879 of 920 top-level entries untouched for six hours. Inside a live
    tree the unit is the direct child. Fixture /proc, fixture trees, fixture
    mount rows; nothing here reads or deletes a real mount."""

    LIVE = "aaaaaaaa-5555-6666-7777-888888888888"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scratch-tier2-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.realpath(os.path.join(self.tmp, "scratch"))
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        self.prior = {k: os.environ.get(k)
                      for k in ("HELM_CACHE_DIR", "HELM_HOME",
                                "HELM_SCRATCH_GC_TIER2")}
        for k in self.prior:
            os.environ.pop(k, None)
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.addCleanup(self.restore)
        self.specs = [(os.path.join(self.root, "*"), "test scratch")]
        self.tree = os.path.join(self.root, self.LIVE)
        self.pad = os.path.join(self.tree, "scratchpad")
        os.makedirs(self.pad)
        self.fresh(os.path.join(self.tree, "now.txt"))
        self.live_proc("999", self.LIVE)

    def restore(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def fresh(self, path):
        with open(path, "w") as f:
            f.write("x")
        return path

    def unit(self, name, age_h=8, files=3, where=None):
        """One direct child of the live session's scratchpad, aged whole: the
        UNIT's own mtime is the cheap gate, so ageing only the leaves would
        leave every fixture young and measure nothing."""
        d = os.path.join(where or self.pad, name)
        os.makedirs(d, exist_ok=True)
        for i in range(files):
            with open(os.path.join(d, "f%d" % i), "w") as f:
                f.write("x" * 16)
        return self.age(os.path.join(where or self.pad,
                                     name.split(os.sep)[0]), age_h) and d

    def age(self, top, age_h=8):
        old = time.time() - age_h * 3600
        for base, _dirs, fs in os.walk(top):
            for f in fs:
                os.utime(os.path.join(base, f), (old, old))
            os.utime(base, (old, old))
        os.utime(top, (old, old))
        return True

    def live_proc(self, pid, sid=None, cwd=None, fd=None):
        d = os.path.join(self.proc, pid)
        os.makedirs(d, exist_ok=True)
        for leaf, blob in (("cmdline", ("claude " + (sid or "")).encode()),
                           ("environ", b"A=b\0")):
            with open(os.path.join(d, leaf), "wb") as f:
                f.write(blob)
        # EVERY REAL /proc/<pid> CARRIES A `stat`, and it stays readable when
        # the rest of the directory does not — which is exactly why the opaque
        # ladder reads it. A fixture process without one is not a process this
        # module could ever meet, and leaving it out made a denied fd read look
        # like an unreadable state.
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%s (claude) S 1 0 0 0 0\n" % pid)
        if cwd:
            os.symlink(os.path.realpath(cwd), os.path.join(d, "cwd"))
        if fd:
            fdir = os.path.join(d, "fd")
            os.makedirs(fdir, exist_ok=True)
            os.symlink(os.path.realpath(fd), os.path.join(fdir, "3"))
        return d

    def rows(self, volatile=True):
        return [{"bytes_pct": 36, "inodes_pct": 36, "mount": "/tmp",
                 "dev": os.stat(self.root).st_dev, "inodes_total": 1048576,
                 "volatile": volatile}]

    HOST_WARN = {"level": "warn", "why": "swap 42% used (warn at 25)",
                 "avail_pct": 40, "swap_used_pct": 42, "psi": None,
                 "trouble": None}

    def gc(self, apply=False, host=None, volatile=True, **kw):
        return scratch.gc(apply=apply, specs=self.specs, proc_dir=self.proc,
                          rows=self.rows(volatile),
                          host=self.HOST_WARN if host is None else host, **kw)

    def paths(self, rep):
        return [v["path"] for v in rep["tier2"]]

    def kept(self, rep, path):
        return [why for p, why in rep["tier2_kept"] if p == path]

    # ── the cure ──────────────────────────────────────────────────────────
    def test_an_aged_unheld_child_of_a_LIVE_session_is_reaped(self):
        stale = self.unit("worktree-copy")
        rep = self.gc(apply=True)
        self.assertEqual(rep["tier2_trees"], [self.tree])
        self.assertIn(stale, self.paths(rep))
        self.assertEqual(rep["tier2_reaped"], 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.isdir(self.tree))    # the SESSION is untouched

    def test_the_live_tree_itself_is_never_a_second_tier_victim(self):
        stale = self.unit("worktree-copy")
        rep = self.gc(apply=True)
        self.assertIn(stale, self.paths(rep))       # the tier really ran
        self.assertNotIn(self.tree, self.paths(rep))
        self.assertNotIn(self.pad, self.paths(rep))
        self.assertTrue(os.path.isdir(self.pad))

    def test_a_child_written_in_place_is_refused_though_its_dir_looks_old(self):
        """The cheap gate reads the UNIT's own mtime, and appending to a file
        that already exists does not touch its directory. So the cheap gate is
        a cost filter and the deep newest-mtime walk is the decision — without
        it, a tree someone is still writing into reads as a week old."""
        busy = self.unit("appending", age_h=8)
        with open(os.path.join(busy, "f0"), "w") as f:
            f.write("still working")          # file now, directory still old
        self.assertLess(os.path.getmtime(busy), time.time() - 7 * 3600)
        stale = self.unit("finished", age_h=8)
        rep = self.gc(apply=True)
        self.assertIn(stale, self.paths(rep))
        self.assertNotIn(busy, self.paths(rep))
        self.assertTrue(os.path.isdir(busy))
        self.assertIn("written", self.kept(rep, busy)[0])

    def test_a_young_child_is_refused(self):
        young = self.unit("in-use", age_h=0)
        stale = self.unit("finished", age_h=8)
        rep = self.gc(apply=True)
        self.assertIn(stale, self.paths(rep))       # same pass, same tree
        self.assertFalse(os.path.exists(stale))
        self.assertNotIn(young, self.paths(rep))
        self.assertTrue(os.path.isdir(young))

    def test_a_child_with_an_open_descriptor_is_refused(self):
        held = self.unit("held")
        self.live_proc("998", fd=os.path.join(held, "f0"))
        rep = self.gc(apply=True)
        self.assertNotIn(held, self.paths(rep))
        self.assertTrue(os.path.isdir(held))
        self.assertIn("holds", self.kept(rep, held)[0])

    def test_a_child_with_a_live_cwd_inside_is_refused(self):
        occupied = self.unit("occupied")
        self.live_proc("997", cwd=occupied)
        rep = self.gc(apply=True)
        self.assertNotIn(occupied, self.paths(rep))
        self.assertTrue(os.path.isdir(occupied))
        self.assertIn("cwd'd inside it", self.kept(rep, occupied)[0])

    def test_a_keep_list_name_is_refused(self):
        """Discovered by reading what the harness keeps there: `tasks/` is its
        own subagent-output dir, written continuously and read back long after."""
        tasks = self.unit("tasks", where=self.tree)
        dotted = self.unit(".remember")
        rep = self.gc(apply=True)
        self.assertNotIn(tasks, self.paths(rep))
        self.assertNotIn(dotted, self.paths(rep))
        self.assertTrue(os.path.isdir(tasks))
        self.assertTrue(os.path.isdir(dotted))
        self.assertIn("harness-owned", self.kept(rep, tasks)[0])
        self.assertIn("dot-name", self.kept(rep, dotted)[0])

    def test_a_transcript_subtree_inside_a_live_session_is_refused(self):
        corpus = self.unit(os.path.join("history", "projects"))
        parent = os.path.dirname(corpus)
        rep = self.gc(apply=True)
        self.assertNotIn(parent, self.paths(rep))
        self.assertTrue(os.path.isdir(corpus))
        self.assertIn("training corpus", self.kept(rep, parent)[0])

    # ── the pressure the tier keys on ─────────────────────────────────────
    def test_host_pressure_escalates_the_ttl_on_a_volatile_mount(self):
        stale = self.unit("copy", age_h=8)
        rep = self.gc(apply=True, volatile=True)
        self.assertEqual(rep["level"], "ok")           # the MOUNT is fine
        self.assertEqual(rep["level_volatile"], "warn")
        self.assertEqual(rep["ttl_volatile"], scratch.TTL_BY_LEVEL["warn"])
        self.assertEqual(rep["ttl"], scratch.TTL_BY_LEVEL["ok"])
        self.assertIn(stale, self.paths(rep))

    def test_host_pressure_does_not_escalate_a_disk_mount(self):
        """THE CONTROL PAIR: same host, same tree, a non-volatile mount. A tree
        on disk costs the host no memory, so host scarcity is none of its
        business."""
        stale = self.unit("copy", age_h=8)
        rep = self.gc(apply=True, volatile=False)
        self.assertEqual(rep["level_volatile"], "ok")
        self.assertEqual(rep["ttl_volatile"], scratch.TTL_BY_LEVEL["ok"])
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.isdir(stale))

    def test_unreadable_meminfo_escalates_nothing_and_reaps_nothing(self):
        """FAIL-CLOSED through the REAL reader: a host whose memory helm cannot
        measure is UNKNOWN, and unknown never licenses a deletion."""
        stale = self.unit("copy", age_h=8)
        blind = scratch.host_memory(os.path.join(self.tmp, "no-meminfo"),
                                    os.path.join(self.tmp, "no-psi"))
        rep = self.gc(apply=True, host=blind)
        self.assertIsNotNone(rep["host"]["trouble"])
        self.assertEqual(rep["level_volatile"], "ok")
        self.assertEqual(rep["tier2"], [])
        self.assertEqual(rep["tier2_reaped"], 0)
        self.assertTrue(os.path.isdir(stale))

    def test_critical_host_memory_shortens_the_tier_ttl(self):
        stale = self.unit("copy", age_h=2)
        warn = self.gc(apply=False)
        self.assertNotIn(stale, self.paths(warn))      # 2h < the 4h warn ttl
        crit = dict(self.HOST_WARN, level="critical")
        self.assertIn(stale, self.paths(self.gc(apply=False, host=crit)))

    # ── bounds, switches, loudness ────────────────────────────────────────
    def test_the_second_tier_is_bounded_per_pass(self):
        for i in range(6):
            self.unit("copy%d" % i, files=i + 1)
        self.assertEqual(len(self.gc()["tier2"]), 6)   # unbounded: all six
        with mock.patch.object(scratch, "TIER2_REAP_CAP", 2):
            rep = self.gc()
        self.assertEqual(len(rep["tier2"]), 2)
        with mock.patch.object(scratch, "TIER2_PROBE_CAP", 3):
            rep = self.gc()
        self.assertEqual(len(rep["tier2"]), 3)
        self.assertTrue(rep["tier2_capped"])
        with mock.patch.object(scratch, "TIER2_TREE_CAP", 0):
            self.assertEqual(self.gc()["tier2"], [])

    def test_the_dry_run_prices_what_it_would_reap_and_the_apply_does_not(self):  # noqa: VACUOUS_ASSERTION — the priced half asserts a positive byte count through the same gc on the same victim immediately before the unpriced half
        """The operator reading a dry run is the one who needs the bytes; the
        Stop hook's applying pass must not pay for a walk it does not need."""
        self.unit("copy", files=4)
        priced = self.gc(list_unattributable=True)
        self.assertGreater(priced["tier2"][0]["bytes"], 0)
        self.assertNotIn("bytes", self.gc()["tier2"][0])

    def test_the_biggest_probed_child_is_reaped_first(self):
        small = self.unit("small", files=1, age_h=9)
        big = self.unit("big", files=9, age_h=8)
        rep = self.gc()
        self.assertEqual(self.paths(rep)[0], big)
        self.assertIn(small, self.paths(rep))

    def test_the_kill_switch_stops_the_tier_and_nothing_else(self):  # noqa: VACUOUS_ASSERTION — the switch-ON assertIn names this exact victim through the same gc before the switch is set
        stale = self.unit("copy")
        self.assertIn(stale, self.paths(self.gc()))   # switch ON: a victim
        os.environ["HELM_SCRATCH_GC_TIER2"] = "0"
        rep = self.gc(apply=True)
        self.assertEqual(rep["tier2"], [])
        self.assertEqual(rep["tier2_reaped"], 0)
        self.assertTrue(os.path.isdir(stale))
        # and the tier itself says WHY when a pass does reach it
        t2 = scratch.tier2_scan(
            [(self.tree, "test scratch", 3600)], time.time(), set())
        self.assertEqual((t2["victims"], t2["trouble"]), ([], None))
        self.assertIn("HELM_SCRATCH_GC_TIER2=0", t2["kept"][0][1])

    def test_an_applied_second_tier_pass_writes_the_audit_event(self):
        """LOUD: the tier deletes inside a LIVE session, so a pass that reaps
        only there still writes the one auditable line."""
        self.unit("copy")
        with mock.patch.object(scratch.pk, "event") as ev:
            rep = self.gc(apply=True)
        self.assertEqual(rep["reaped"], 0)             # tier ONE reaped nothing
        self.assertEqual(rep["tier2_reaped"], 1)
        self.assertTrue(ev.called)
        self.assertEqual(ev.call_args[0][:2], ("scratch", "gc"))
        self.assertIn("1 aged child of a LIVE session", ev.call_args[0][2])

    # ── the handle probe's two failure classes ────────────────────────────
    def _listdir_raising(self, exc):
        real = os.listdir

        def fake(path, *a, **kw):
            if os.path.basename(str(path)) == "fd":
                raise exc
            return real(path, *a, **kw)
        return fake

    def test_a_denied_fd_read_is_opaque_and_does_not_disable_the_tier(self):
        """MEASURED, and the reason this is not fail-closed: 11 of 625 same-uid
        processes on a real desktop refuse /proc/<pid>/fd — every one of them a
        desktop or credential daemon that cleared its dumpable flag, none of
        them capable of hosting an agent harness. Failing closed on those would
        disable the tier permanently, which is how the equivalent environ rule
        broke once. They are counted, and the count rides on every victim."""
        stale = self.unit("copy")
        with mock.patch.object(
                os, "listdir",
                side_effect=self._listdir_raising(PermissionError(13, "no"))):
            rep = self.gc()
        self.assertIsNone(rep["tier2_trouble"])
        self.assertIn(stale, self.paths(rep))
        self.assertGreaterEqual(rep["tier2"][0]["opaque"], 1)

    def test_a_broken_fd_probe_fails_the_tier_closed(self):
        stale = self.unit("copy")
        with mock.patch.object(
                os, "listdir",
                side_effect=self._listdir_raising(OSError(5, "I/O error"))):
            rep = self.gc(apply=True)
        self.assertIn("reaps nothing", rep["tier2_trouble"])
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.isdir(stale))

    def test_an_unlistable_proc_fails_the_tier_closed(self):
        stale = self.unit("copy")
        held, opaque, hazards, unread, trouble = scratch.open_handles(
            [self.tree], os.path.join(self.tmp, "no-proc"))
        self.assertEqual((held, opaque, hazards, unread),
                         (None, 0, [], None))
        self.assertIn("unlistable", trouble)
        self.assertTrue(os.path.isdir(stale))

    # ── WHAT A BOUND IS ALLOWED TO CONCLUDE ───────────────────────────────
    def deep_unit(self, name, files=3, age_h=8):
        """(unit, subdir) — a unit whose files live one level DOWN. Appending
        to one of them moves the FILE's mtime and leaves every directory above
        it untouched, so only a walk that DESCENDS can see the write."""
        d = os.path.join(self.pad, name)
        sub = os.path.join(d, "sub")
        os.makedirs(sub)
        for i in range(files):
            with open(os.path.join(sub, "f%d" % i), "w") as f:
                f.write("x" * 16)
        self.age(d, age_h)
        return d, sub

    def test_a_write_in_a_SUBDIRECTORY_is_seen_though_every_dir_looks_old(self):
        """THE SURVIVING MUTANT'S ARM. Deleting the two lines that make the
        newest-mtime walk descend left the whole suite green, because every
        arm that exercised the deep gate kept its fresh file at the unit's TOP
        level — which the walk reads before it descends anywhere. Here the
        write is one level down and every directory on the path is old."""
        busy, sub = self.deep_unit("deep-busy")
        with open(os.path.join(sub, "f0"), "a") as f:
            f.write("still working")
        now = time.time()
        self.assertLess(os.path.getmtime(busy), now - 7 * 3600)
        self.assertLess(os.path.getmtime(sub), now - 7 * 3600)
        stale, _sub = self.deep_unit("deep-finished")   # the positive control
        rep = self.gc(apply=True)
        self.assertIn(stale, self.paths(rep))      # the walk reaches this deep
        self.assertFalse(os.path.exists(stale))
        self.assertNotIn(busy, self.paths(rep))
        self.assertTrue(os.path.isdir(busy))
        self.assertIn("written", self.kept(rep, busy)[0])

    def test_a_unit_too_large_to_age_within_the_walk_budget_is_KEPT(self):
        """A WALK THAT DID NOT COMPLETE PROVES NOTHING YOUNG OR OLD. In tier
        one a sampled walk is still allowed to age, because liveness already
        proved nobody is writing there; here the session is RUNNING, so the
        sample is the only protection and a sample is not an answer."""
        big = self.unit("too-big", files=12)
        small = self.unit("small-enough", files=1)
        with mock.patch.object(scratch, "TIER2_MTIME_CAP", 4):
            rep = self.gc(apply=True)
        self.assertIn(small, self.paths(rep))      # same pass, same tier
        self.assertFalse(os.path.exists(small))
        self.assertNotIn(big, self.paths(rep))
        self.assertTrue(os.path.isdir(big))
        self.assertIn("too large to age cheaply", self.kept(rep, big)[0])
        self.assertEqual(rep["tier2_unknown"]["incomplete_walk"], 1)
        self.assertEqual([row[0] for row in rep["tier2_unknown"]["too_large"]],
                         [big])
        self.assertIn("too large to age cheaply", scratch.summary(rep))

    def test_a_walk_stopped_by_an_unreadable_subtree_is_incomplete(self):
        """A PARTIALLY DENIED READ IS A TRUNCATED READ. The walk that cannot
        open one subdirectory has measured the others, not the tree."""
        unit, sub = self.deep_unit("denied")
        newest, _seen, complete = scratch._newest_mtime(unit)
        self.assertTrue(complete)                  # the positive control
        self.assertGreater(newest, 0)
        real = os.scandir

        def deny(path, *a, **kw):
            if os.path.realpath(str(path)) == os.path.realpath(sub):
                raise PermissionError(13, "no")
            return real(path, *a, **kw)
        with mock.patch.object(os, "scandir", side_effect=deny):
            _newest, _seen, complete = scratch._newest_mtime(unit)
        self.assertFalse(complete)

    def fd_proc(self, pid, target, total=400):
        """(fd dir, the descriptor name pinning `target`) for a process holding
        `total` real descriptors."""
        d = self.live_proc(pid)
        fdir = os.path.join(d, "fd")
        os.makedirs(fdir, exist_ok=True)
        junk = os.path.join(self.tmp, "junk-target")
        for i in range(3, 3 + total):
            os.symlink(junk, os.path.join(fdir, str(i)))
        pin = str(3 + total)
        os.symlink(os.path.realpath(target), os.path.join(fdir, pin))
        return fdir, pin

    def _listdir_placing(self, fdir, pin, index):
        """The REAL entries of that fd dir, in one NAMED readdir order.

        readdir order is a filesystem hash order: creating a descriptor last
        does not put it last, and deleting one and re-creating it under the
        same name lands it at index 399 on one filesystem and index 0 on the
        next. The position the probe would have stopped before is the whole
        arm, so it is stated here rather than hoped for — every name in the
        list is a descriptor that really exists and really points where the
        fixture put it."""
        real = os.listdir

        def fake(path, *a, **kw):
            names = real(path, *a, **kw)
            if os.path.realpath(str(path)) == os.path.realpath(fdir):
                rest = [n for n in names if n != pin]
                return rest[:index] + [pin] + rest[index:]
            return names
        return fake

    def test_a_descriptor_past_the_old_cap_still_protects_its_unit(self):
        """H1. A per-process cap over an UNSORTED listdir means a process
        holding more descriptors than the cap cannot protect the directory it
        is working in. Measured on a fleet desktop with the whole table read:
        seventeen same-uid processes hold more than 256 descriptors, and the
        whole table costs 0.13s to read — the cap bought a hundredth of a
        second and sold the one thing this probe exists for."""
        held = self.unit("held-late")
        fdir, pin = self.fd_proc("990", os.path.join(held, "f0"))
        with mock.patch.object(
                os, "listdir",
                side_effect=self._listdir_placing(fdir, pin, 300)):
            rep = self.gc(apply=True)
        self.assertNotIn(held, self.paths(rep))
        self.assertTrue(os.path.isdir(held))
        self.assertIn("holds", self.kept(rep, held)[0])

    def test_a_descriptor_at_the_front_of_the_table_protects_it_too(self):
        """The CONTROL for the arm above: identical fixture, identical unit,
        only the descriptor's readdir position differs. Both must protect; a
        truncating probe passes this one and fails that one."""
        held = self.unit("held-early")
        fdir, pin = self.fd_proc("991", os.path.join(held, "f0"))
        with mock.patch.object(
                os, "listdir",
                side_effect=self._listdir_placing(fdir, pin, 0)):
            rep = self.gc(apply=True)
        self.assertNotIn(held, self.paths(rep))
        self.assertTrue(os.path.isdir(held))
        self.assertIn("holds", self.kept(rep, held)[0])

    def test_a_truncated_handle_table_keeps_the_units_it_never_cleared(self):
        """The budget stays — a GC tick must never be the expensive thing —
        but it reports instead of concluding."""
        stale = self.unit("copy")
        self.assertIn(stale, self.paths(self.gc()))   # control: normally a pick
        self.live_proc("992", fd=os.path.join(self.tmp, "junk-target"))
        with mock.patch.object(scratch, "FD_BUDGET", 0):
            rep = self.gc(apply=True)
        self.assertEqual(rep["tier2_reaped"], 0)
        self.assertNotIn(stale, self.paths(rep))
        self.assertTrue(os.path.isdir(stale))
        self.assertIn("UNKNOWN", self.kept(rep, stale)[0])
        self.assertEqual(rep["tier2_unknown"]["truncated_handles"], 1)

    # ── what a unit IS ────────────────────────────────────────────────────
    def test_an_aged_plain_FILE_is_reaped_by_unlink(self):
        """M1. 54 of 64 tier-2 picks on the real box were regular files the
        remover then refused — receipts, logs, one-off scripts, rendered pages
        — so they occupied the per-pass cap forever and their failures never
        reached the audit line. They are real clutter: reap them as FILES."""
        receipt = os.path.join(self.pad, "r7-receipts.jsonl")
        with open(receipt, "w") as f:
            f.write("{}\n" * 8)
        old = time.time() - 8 * 3600
        os.utime(receipt, (old, old))
        rep = self.gc(apply=True)
        picks = {v["path"]: v["kind"] for v in rep["tier2"]}
        self.assertEqual(picks.get(receipt), "file")
        self.assertEqual(rep["tier2_reaped"], 1)
        self.assertFalse(os.path.exists(receipt))
        self.assertEqual(rep["errors"], [])

    def test_a_symlink_is_never_a_unit_and_its_target_is_never_walked(self):
        """A symlink is one inode of clutter whose target may be precious and
        outside the estate entirely. Pricing or ageing one means walking
        through it, so it is not a unit at all — and it is SAID, not silently
        dropped."""
        precious = os.path.join(self.tmp, "precious")
        os.makedirs(precious)
        with open(os.path.join(precious, "keep"), "w") as f:
            f.write("gold")
        link = os.path.join(self.pad, "link-to-precious")
        os.symlink(precious, link)
        old = time.time() - 8 * 3600
        os.utime(link, (old, old), follow_symlinks=False)
        stale = self.unit("copy")                  # the positive control
        rep = self.gc(apply=True)
        self.assertIn(stale, self.paths(rep))
        self.assertNotIn(link, self.paths(rep))
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.exists(os.path.join(precious, "keep")))
        self.assertIn("symlink", self.kept(rep, link)[0])

    def test_a_failed_removal_reaches_the_report_and_the_audit_line(self):
        """A pass that fails two of three picks was SILENT in summary()."""
        stale = self.unit("copy")
        with mock.patch.object(scratch.shutil, "rmtree",
                               side_effect=OSError(20, "Not a directory")):
            rep = self.gc(apply=True)
        self.assertEqual(rep["tier2_reaped"], 0)
        self.assertEqual([row[0] for row in rep["errors"]], [stale])
        self.assertIn("NOT removed", scratch.summary(rep))
        self.assertTrue(os.path.isdir(stale))

    # ── an OPAQUE process: what it is allowed to prove ────────────────────
    def opaque_proc(self, pid, comm, state="S", ppid=1):
        d = self.live_proc(pid)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("%s (%s) %s %s 0 0 0 0\n" % (pid, comm, state, ppid))
        return d

    def _listdir_denying(self, pid):
        real = os.listdir

        def fake(path, *a, **kw):
            p = str(path)
            if (os.path.basename(p) == "fd"
                    and os.path.basename(os.path.dirname(p)) == pid):
                raise PermissionError(13, "no")
            return real(path, *a, **kw)
        return fake

    def test_an_opaque_live_shell_under_a_terminal_makes_the_pass_UNKNOWN(self):
        """L5. A same-uid process the kernel refuses to describe yields NO cwd
        and NO descriptor, so `no evidence` and `unheld` read identically —
        and on a fleet desktop one of them was a shell whose parent is an Orca
        terminal, exactly where an agent's `cd` lives."""
        stale = self.unit("copy")
        self.opaque_proc("2000", "orca-ide", ppid=1)
        self.opaque_proc("2001", "bash", ppid=2000)
        with mock.patch.object(os, "listdir",
                               side_effect=self._listdir_denying("2001")):
            rep = self.gc(apply=True)
        self.assertEqual(rep["tier2"], [])
        self.assertEqual(rep["tier2_reaped"], 0)
        self.assertTrue(os.path.isdir(stale))
        self.assertIn("2001", rep["tier2_trouble"])
        self.assertEqual(rep["tier2_unknown"]["opaque_hazard"], 1)

    def test_an_opaque_CORPSE_under_a_terminal_is_not_a_hazard(self):
        """The CONTROL that keeps the cure from being the blanket fail-closed
        that disabled this reaper once: identical fixture, one character of
        process state apart. A zombie has already had its descriptor table and
        its cwd torn down — that is WHY /proc/<pid>/fd starts denying reads —
        so it can hold nothing. Measured on a fleet desktop: the one
        agent-adjacent opaque process was exactly this."""
        stale = self.unit("copy")
        self.opaque_proc("2000", "orca-ide", ppid=1)
        self.opaque_proc("2001", "bash", state="Z", ppid=2000)
        with mock.patch.object(os, "listdir",
                               side_effect=self._listdir_denying("2001")):
            rep = self.gc(apply=True)
        self.assertIn(stale, self.paths(rep))
        self.assertEqual(rep["tier2_reaped"], 1)
        self.assertIsNone(rep["tier2_trouble"])

    def test_an_opaque_desktop_daemon_is_not_a_hazard(self):
        """The other control: a LIVE non-dumpable process that is neither a
        shell nor terminal-parented cannot reach the harness estate."""
        stale = self.unit("copy")
        self.opaque_proc("2002", "ssh-agent", ppid=1)
        with mock.patch.object(os, "listdir",
                               side_effect=self._listdir_denying("2002")):
            rep = self.gc(apply=True)
        self.assertIn(stale, self.paths(rep))
        self.assertEqual(rep["tier2_reaped"], 1)

    def test_an_exited_pid_is_not_a_hazard_but_an_unreadable_live_one_is(self):
        """A HAZARD MINTED BY THE PROBE'S OWN RACE IS NOT A HAZARD. The fd read
        denies, the process exits, the state read then gets ENOENT — and
        calling that `unreadable` would have shut the tier down on a box where
        nothing was wrong. Measured: it fired on the first real pass."""
        self.assertIsNone(scratch.opaque_hazard("31337", self.proc))
        alive = os.path.join(self.proc, "31338")
        os.makedirs(alive)
        with open(os.path.join(alive, "stat"), "w") as f:
            f.write("not a stat line")
        self.assertIn("unreadable", scratch.opaque_hazard("31338", self.proc))

    def test_an_opaque_process_past_the_HOP_BOUND_is_UNKNOWN(self):
        """ROUND 3, THE FIFTH SIBLING. `not agent-adjacent` is a claim about
        the WHOLE ancestry, and a walk that spent its hop bound has not read
        the whole ancestry. Under the old rule the tier ran — and a unit an
        opaque shell was sitting in was reaped."""
        stale = self.unit("copy")
        self.opaque_proc("2000", "orca-ide", ppid=1)
        prev = 2000
        for i in range(10):                       # ten hops of plain daemons
            self.opaque_proc("21%02d" % i, "python3", ppid=prev)
            prev = int("21%02d" % i)
        self.opaque_proc("2200", "python3", ppid=prev)
        with mock.patch.object(scratch, "OPAQUE_HOPS", 3), \
             mock.patch.object(os, "listdir",
                               side_effect=self._listdir_denying("2200")):
            why = scratch.opaque_hazard("2200", self.proc)
            rep = self.gc(apply=True)
        self.assertIsNotNone(why)
        self.assertIn("parent walk", why)
        self.assertIn("UNKNOWN", why)
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.isdir(stale))
        self.assertEqual(rep["tier2_unknown"]["opaque_hazard"], 1)

    def test_an_opaque_process_INSIDE_the_hop_bound_still_answers(self):  # noqa: VACUOUS_ASSERTION — the cleared-process half is proven by the unit being REAPED in the same pass (assertIn + tier2_reaped == 1)
        """The CONTROL for the arm above, same fixture and same bound, two
        hops instead of eleven: the walk finishes, and a walk that finishes is
        allowed to clear the process. Without this the cure would be `every
        opaque process is a hazard`, which is the blanket fail-closed that
        disabled this reaper once."""
        stale = self.unit("copy")
        self.opaque_proc("2000", "systemd", ppid=1)
        self.opaque_proc("2001", "python3", ppid=2000)
        self.opaque_proc("2200", "python3", ppid=2001)
        with mock.patch.object(scratch, "OPAQUE_HOPS", 3), \
             mock.patch.object(os, "listdir",
                               side_effect=self._listdir_denying("2200")):
            why = scratch.opaque_hazard("2200", self.proc)
            rep = self.gc(apply=True)
        self.assertIsNone(why)
        self.assertIn(stale, self.paths(rep))
        self.assertEqual(rep["tier2_reaped"], 1)

    def test_an_ancestor_that_cannot_be_read_is_UNKNOWN_too(self):
        """The hop bound is not the only way that walk stops. An ancestor
        whose stat will not parse leaves the ancestry above it unread, which
        is the same claim the bound could not make."""
        self.opaque_proc("2000", "python3", ppid=4242)     # parent: no such row
        why = scratch.opaque_hazard("2000", self.proc)
        self.assertIsNotNone(why)
        self.assertIn("4242", why)

    def test_a_PPID_CYCLE_is_an_UNFINISHED_ancestry_not_a_clearance(self):
        """A WALK THAT DID NOT REACH pid 1 HAS NOT CLEARED THE PROCESS — and
        the loop concluded exactly that on its second disjunct. `cur in seen`
        ends the walk where the ancestry RETURNED to a pid it had already
        read, which cannot happen among co-existing processes (fork builds a
        tree) and therefore means a pid was REUSED between two stat reads
        inside this one walk. The ancestry above that point was never read, so
        it is the same UNKNOWN the hop bound raises, and it was silent: the
        walk broke complete, no hazard was minted, and the tier reaped."""
        stale = self.unit("copy")
        self.opaque_proc("6001", "python3", ppid=6002)
        self.opaque_proc("6002", "python3", ppid=6001)   # …and back again
        self.opaque_proc("7000", "python3", ppid=6001)
        with mock.patch.object(os, "listdir",
                               side_effect=self._listdir_denying("7000")):
            why = scratch.opaque_hazard("7000", self.proc)
            rep = self.gc(apply=True)
        self.assertIsNotNone(why)
        self.assertIn("returned to pid", why)
        self.assertIn("parent walk", why)
        self.assertIn("UNKNOWN", why)
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.isdir(stale))
        self.assertEqual(rep["tier2_unknown"]["opaque_hazard"], 1)

    def test_the_SAME_walk_without_the_cycle_reads_its_ancestry_whole(self):
        """The CONTROL, one edge apart: 6002's parent is a terminal instead of
        the pid the walk started from. The walk finishes, and what it says is
        about the TERMINAL — so the arm above is measuring the cycle and not
        merely `an opaque process under a python3 chain`."""
        self.unit("copy")
        self.opaque_proc("5000", "orca-ide", ppid=1)
        self.opaque_proc("6001", "python3", ppid=6002)
        self.opaque_proc("6002", "python3", ppid=5000)   # no cycle
        self.opaque_proc("7000", "python3", ppid=6001)
        with mock.patch.object(os, "listdir",
                               side_effect=self._listdir_denying("7000")):
            why = scratch.opaque_hazard("7000", self.proc)
        self.assertIsNotNone(why)
        self.assertNotIn("returned to pid", why)
        self.assertIn("orca-ide", why)
        self.assertIn("terminal", why)

    def test_a_TRUNCATED_proc_stat_is_no_answer_not_a_shorter_one(self):
        """A SURVIVING MUTANT: `_proc_stat`'s `if not complete: return None`
        had no arm. A stat line cut mid-field still PARSES — the comm, the
        state and a ppid are all in the first twenty-one bytes — so deleting
        that guard hands the opaque ladder a ppid it never finished reading
        and the ladder walks a fabricated ancestry."""
        d = os.path.join(self.proc, "8100")
        os.makedirs(d)
        with open(os.path.join(d, "stat"), "w") as f:
            f.write("8100 (python3) S 4242 0 0 0 0\n")
        self.assertEqual(scratch._proc_stat("8100", self.proc),
                         ("python3", "S", 4242))      # CONTROL: the whole line
        with mock.patch.object(scratch, "STAT_READ_CAP", 21):
            # the SAME twenty-one bytes the mutant would have parsed
            self.assertIsNone(scratch._proc_stat("8100", self.proc))
            why = scratch.opaque_hazard("8100", self.proc)
        self.assertIsNotNone(why)
        self.assertIn("unreadable", why)

    # ── a holder the process list never reached ───────────────────────────
    def test_a_holder_past_the_PROCESS_LIST_cut_still_protects_its_unit(self):
        """ROUND 3, THE FIRST SIBLING — two lines above the descriptor budget
        round 2 cured, in the same function. /proc readdir is ascending pid
        order, so the processes a truncation drops are the most recently
        started ones: exactly where a just-launched agent lives. The fixture
        pins readdir order rather than assuming it."""
        held = self.unit("held-late")
        stale = self.unit("plain-stale")           # the tier-really-ran control
        self.live_proc("991", fd=os.path.join(held, "f0"))
        order = [n for n in os.listdir(self.proc) if n.isdigit()]
        # THE FIXTURE PINS READDIR ORDER RATHER THAN ASSUMING IT: the cut is
        # placed exactly where the holder actually landed, so this arm cannot
        # pass by accident on a node whose directory hashing differs.
        self.assertIn("991", order)
        with mock.patch.object(scratch, "PROC_BUDGET", order.index("991")):
            rep = self.gc(apply=True)
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.isdir(held))
        self.assertTrue(os.path.isdir(stale))
        self.assertIn("UNKNOWN", rep["trouble"])
        # CONTROL: the identical fixture with a budget that fits keeps the
        # HELD unit for the right reason and reaps the unheld one.
        rep = self.gc(apply=True)
        self.assertIn(stale, self.paths(rep))
        self.assertNotIn(held, self.paths(rep))
        self.assertIn("holds", self.kept(rep, held)[0])

    def test_open_handles_reports_a_cut_process_list(self):
        """The unit-level half: the same truncation, read off the probe."""
        self.live_proc("991", fd=os.path.join(self.pad, "x"))
        with mock.patch.object(scratch, "PROC_BUDGET", 1):
            _held, _op, _hz, unread, trouble = scratch.open_handles(
                [self.tree], self.proc)
        self.assertIsNone(trouble)
        self.assertIsNotNone(unread)
        self.assertIn("process", unread)
        _held, _op, _hz, unread, _t = scratch.open_handles(
            [self.tree], self.proc)                 # CONTROL: whole table
        self.assertIsNone(unread)

    # ── a registration the scan never reached ─────────────────────────────
    def test_a_WORKTREE_past_the_scan_cap_keeps_the_unit_as_UNKNOWN(self):
        """ROUND 3, THE THIRD SIBLING, and the one that destroyed data in the
        verifier's fixture: a unit whose registration sits past the readdir cut
        was treated as having NO registration at all — so the locked check, the
        dirtiness check and the prune record were all skipped and an applying
        pass deleted a checkout holding uncommitted work, with no UNKNOWN, no
        error and no line in the audit."""
        unit = self.unit("copies")
        wt = os.path.join(unit, "lane")
        os.makedirs(wt)
        for i in range(6):
            os.makedirs(os.path.join(unit, "pad%d" % i))
        self.register_worktree(wt, dirty=True)
        self.age(unit)
        with mock.patch.object(scratch, "WT_SCAN_CAP", 2):
            rep = self.gc(apply=True)
        self.assertNotIn(unit, self.paths(rep))
        self.assertTrue(os.path.exists(os.path.join(wt, "unsaved.py")))
        why = self.kept(rep, unit)[0]
        self.assertIn("UNKNOWN", why)
        self.assertIn("worktree registration scan", why)
        self.assertEqual(rep["tier2_unknown"]["worktree"], 1)

    def test_a_WORKTREE_inside_the_scan_cap_is_refused_for_its_DIRTINESS(self):
        """The CONTROL: identical fixture, identical unit, only the scan's
        bound differs. The refusal must come from `git status`, not from the
        bound — otherwise the arm above proves only that an UNKNOWN keeps
        things, which was already true."""
        unit = self.unit("copies")
        wt = os.path.join(unit, "lane")
        os.makedirs(wt)
        for i in range(6):
            os.makedirs(os.path.join(unit, "pad%d" % i))
        self.register_worktree(wt, dirty=True)
        self.age(unit)
        rep = self.gc(apply=True)
        self.assertNotIn(unit, self.paths(rep))
        self.assertIn("UNCOMMITTED WORK", self.kept(rep, unit)[0])

    # ── a transcript subtree the count never reached ──────────────────────
    def test_a_TRANSCRIPT_past_the_entry_count_keeps_the_unit(self):
        """ROUND 3, THE FOURTH SIBLING. The transcript refusal is the one this
        module calls STRUCTURAL — `seeing the directory at all is enough` —
        and a partial count had been answering `protected=False` for it. In
        the verifier's fixture a 6,001-entry unit with `projects/` at readdir
        index 4,262 was reaped, transcripts and all."""
        unit = self.unit("harness-home", files=8)
        # THE SUBTREE SITS BELOW THE CUT, which is the shape that destroyed
        # data: a walk that stops in the unit's own top level never descends,
        # so it never sees the directory the refusal is about.
        home = os.path.join(unit, "home")
        os.makedirs(os.path.join(home, "projects", "p1"))
        transcript = os.path.join(home, "projects", "p1", "t.jsonl")
        with open(transcript, "w") as f:
            f.write("{}\n")
        self.age(unit)
        with mock.patch.object(scratch, "TIER2_COUNT_CAP", 3):
            rep = self.gc(apply=True)
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.exists(transcript))
        why = self.kept(rep, unit)[0]
        self.assertIn("UNKNOWN", why)
        self.assertIn("transcript", why)
        self.assertEqual(rep["tier2_unknown"]["transcript"], 1)

    def test_a_count_that_REACHES_the_transcript_refuses_structurally(self):
        """The CONTROL: same unit, same transcript, a count that finishes. The
        refusal must name the corpus, not the bound."""
        unit = self.unit("harness-home", files=8)
        home = os.path.join(unit, "home")
        os.makedirs(os.path.join(home, "projects", "p1"))
        with open(os.path.join(home, "projects", "p1", "t.jsonl"), "w") as f:
            f.write("{}\n")
        self.age(unit)
        rep = self.gc(apply=True)
        self.assertEqual(rep["tier2"], [])
        self.assertIn("training corpus", self.kept(rep, unit)[0])

    def test_a_count_stopped_by_the_SHARED_budget_keeps_the_unit_too(self):
        """The per-unit cap is not the only bound on that reading: the probe
        shares ONE counting budget across every unit it looks at, and a unit
        the shared budget never reached is in exactly the position the capped
        one is. Curing the cap and leaving the budget is this lane's own
        bug-class."""
        first = self.unit("aaa-first", files=6)
        second = self.unit("zzz-second", files=6)
        self.age(first, 9)
        self.age(second, 8)
        with mock.patch.object(scratch, "TIER2_COUNT_BUDGET", 4):
            rep = self.gc(apply=True)
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.isdir(first))
        self.assertTrue(os.path.isdir(second))
        self.assertEqual(rep["tier2_unknown"]["transcript"], 2)
        # CONTROL: the same two units under the real budget are both reaped
        rep = self.gc(apply=True)
        self.assertEqual(rep["tier2_reaped"], 2)

    # ── the git-status allowance is a reading, deadline included ──────────
    def test_the_last_git_status_cannot_outlive_the_wall_budget(self):
        """L. The deadline was checked BEFORE a spawn and not enforced during
        it, so the worst case was the budget PLUS a whole per-call timeout —
        twenty seconds on a leg that costs a fifth of one. The timeout handed
        to the spawn is now the smaller of the two."""
        allow = scratch.dirty_allowance()
        allow.until = time.time() + 0.25
        self.assertLessEqual(allow.timeout(), 0.25)
        self.assertGreater(allow.timeout(), 0)
        # CONTROL: with the whole budget ahead of it the per-call bound is
        # what binds — the deadline is a whole budget away, so the spawn gets
        # (all but a sliver of) its own timeout rather than a truncated one.
        fresh = scratch.dirty_allowance()
        self.assertGreater(fresh.timeout(), scratch.WT_DIRTY_TIMEOUT_S - 1)
        self.assertLessEqual(fresh.timeout(), scratch.WT_DIRTY_TIMEOUT_S)

    def test_an_UNSPAWNABLE_git_status_is_UNKNOWN_not_clean(self):
        """ROUND 2's SURVIVING MUTANT. The `except` branch of worktree_dirty —
        git not on PATH, a TimeoutExpired, any spawn failure — was correct and
        UNHELD: every existing arm drove the allowance or a non-zero exit, so
        a mutant making that branch read CLEAN left the module green."""
        import subprocess as sp
        unit = self.unit("lane-nogit")
        self.register_worktree(unit)
        real = sp.run

        def boom(*_a, **_kw):
            raise sp.TimeoutExpired(cmd="git", timeout=1)

        sp.run = boom
        try:
            dirty, trouble = scratch.worktree_dirty(unit, timeout=1)
            rep = self.gc(apply=True)
        finally:
            sp.run = real
        self.assertFalse(dirty)
        # the seam folds a timeout into NOTHING RAN; the reason still says the
        # status was not taken, which is what keeps the tree
        self.assertIn("unavailable", trouble)
        self.assertNotIn(unit, self.paths(rep))
        self.assertTrue(os.path.isdir(unit))
        self.assertIn("dirtiness UNKNOWN", self.kept(rep, unit)[0])
        self.assertEqual(rep["tier2_unknown"]["worktree"], 1)
        # CONTROL: the identical fixture with a working spawn IS reaped
        rep = self.gc(apply=True)
        self.assertIn(unit, self.paths(rep))
        self.assertFalse(os.path.exists(unit))

    # ── the window between the probe and the unlink ───────────────────────
    def test_a_unit_written_during_the_pass_is_not_removed(self):
        """L6. Every gate was decided before the ranking, the pricing and the
        sort; the re-check makes the window one unit wide."""
        stale, sub = self.deep_unit("racy")
        real = scratch._fresh_handles
        touched = []

        def write_then_read(rep, proc_dir):
            if not touched:
                touched.append(True)
                with open(os.path.join(sub, "f0"), "a") as f:
                    f.write("an agent is back")
            return real(rep, proc_dir)
        with mock.patch.object(scratch, "_fresh_handles",
                               side_effect=write_then_read):
            rep = self.gc(apply=True)
        self.assertTrue(touched)                   # the race really happened
        self.assertIn(stale, self.paths(rep))      # it WAS a pick
        self.assertEqual(rep["tier2_reaped"], 0)
        self.assertTrue(os.path.isdir(stale))
        self.assertIn("between the probe and the deletion",
                      rep["errors"][0][1])

    def test_the_unraced_twin_of_that_pass_does_reap(self):  # noqa: VACUOUS_ASSERTION — reaped==1 and the victim's absence are the unconditional positive controls; the empty errors list is the same pass's other half
        """The positive control for the re-check: same fixture, same pass, no
        write in the window."""
        stale, _sub = self.deep_unit("racy")
        rep = self.gc(apply=True)
        self.assertEqual(rep["tier2_reaped"], 1)
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(rep["errors"], [])

    def test_the_handle_window_is_bounded_in_seconds_not_in_victims(self):
        """The handle table costs 0.13s against the cwd table's 0.013s, so it
        is the one gate that cannot be re-read per victim on an 88-victim
        pass. It is re-read by AGE instead, and the report carries the widest
        window any victim was actually decided on."""
        self.unit("one")
        self.unit("two")
        real = scratch.open_handles
        with mock.patch.object(scratch, "open_handles",
                               side_effect=real) as spy:
            with mock.patch.object(scratch, "TOCTOU_HANDLE_MAX_AGE_S", 0):
                rep = self.gc(apply=True)
            stale_window = spy.call_count
        self.assertEqual(rep["tier2_reaped"], 2)
        self.assertGreaterEqual(stale_window, 3)   # scan + one per victim
        self.assertLessEqual(rep["handle_window_s"], 0.0)
        with mock.patch.object(scratch, "open_handles",
                               side_effect=real) as spy:
            self.unit("three")
            self.unit("four")
            rep = self.gc(apply=True)
            fresh_window = spy.call_count
        self.assertEqual(rep["tier2_reaped"], 2)
        self.assertEqual(fresh_window, 2)          # scan + ONE deletion-time
        self.assertLessEqual(rep["handle_window_s"],
                             scratch.TOCTOU_HANDLE_MAX_AGE_S)

    # ── a registered worktree ─────────────────────────────────────────────
    def _git(self, cwd, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_SYSTEM="/dev/null", GIT_AUTHOR_NAME="t",
                   GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@e")
        return subprocess.run(["git", "-C", cwd] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=True)

    def register_worktree(self, unit, locked=False, dirty=False):
        """A REAL git worktree registered at `unit`.

        A hand-written `.git` pointer at a hand-made admin dir is not a
        fixture of the world for a gate that asks `git status` whether the
        checkout is dirty: git refuses a fabricated registration, so a
        synthetic fixture proves only that an UNKNOWN keeps the tree — never
        that a clean one is reaped
        or that a dirty one is refused."""
        repo = os.path.join(self.tmp, "repo-" + os.path.basename(unit))
        os.makedirs(repo)
        self._git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "seed"), "w") as f:
            f.write("seed\n")
        self._git(repo, "add", "seed")
        self._git(repo, "commit", "-q", "-m", "seed")
        shutil.rmtree(unit)               # `git worktree add` mints it
        self._git(repo, "worktree", "add", "-q", "-b",
                  "lane-" + os.path.basename(unit), unit)
        if locked:
            self._git(repo, "worktree", "lock", unit)
        if dirty:
            with open(os.path.join(unit, "unsaved.py"), "w") as f:
                f.write("the only copy\n")
        admin = os.path.join(repo, ".git", "worktrees", os.path.basename(unit))
        self.age(unit)
        return repo, admin

    def test_a_reaped_registered_worktree_leaves_a_prune_record(self):
        unit = self.unit("lane")
        repo, admin = self.register_worktree(unit)
        rep = self.gc(apply=True)
        self.assertEqual(rep["tier2_reaped"], 1)
        self.assertEqual([p for p, _link in rep["worktrees"]], [unit])
        self.assertEqual(rep["worktrees"][0][1]["repo"], repo)
        self.assertEqual(rep["worktrees"][0][1]["admin"], admin)
        self.assertIn("worktree prune", scratch.summary(rep))

    def test_a_pass_reaping_two_repos_worktrees_names_BOTH(self):
        """L3. The count clause covered every reaped registration and the
        instruction named only the first repo, so the other repo kept a stale
        worktree entry that nobody was told about."""
        left = self.unit("lane-left")
        repo_left, _admin = self.register_worktree(left)
        right = self.unit("lane-right")
        repo_right, _admin = self.register_worktree(right)
        rep = self.gc(apply=True)
        self.assertEqual(rep["tier2_reaped"], 2)
        line = scratch.summary(rep)
        self.assertIn(repo_left, line)
        self.assertIn(repo_right, line)

    def test_the_audited_line_gives_each_tier_its_OWN_file_count(self):
        """L. One accumulator carried BOTH tiers and was rendered inside the
        tier-one sentence, so a pass that reaped nothing in tier one and three
        units in tier two said `reaped 0 dead-session scratch dirs (7 files)`.
        The same line also said `2 reaped trees WAS a registered git
        worktree`. Both are pinned here as RENDERED TEXT, because both were
        true of a dict and false of the sentence."""
        left = self.unit("lane-left")
        repo_left, _a = self.register_worktree(left)
        right = self.unit("lane-right")
        repo_right, _b = self.register_worktree(right)
        rep = self.gc(apply=True)
        self.assertEqual((rep["reaped"], rep["tier2_reaped"]), (0, 2))
        line = scratch.summary(rep)
        self.assertIn("reaped 0 dead-session scratch dirs (0 files)", line)
        self.assertIn("2 aged children of a LIVE session (second tier, %d "
                      "files" % rep["tier2_files"], line)
        self.assertGreater(rep["tier2_files"], 0)
        self.assertIn("2 reaped trees were a registered git worktree", line)
        self.assertIn(repo_left, line)
        self.assertIn(repo_right, line)

    def test_ONE_reaped_worktree_still_reads_as_singular(self):
        """The grammar control: the fix must not swap one wrong word for
        another in the other direction."""
        only = self.unit("lane-only")
        self.register_worktree(only)
        rep = self.gc(apply=True)
        self.assertIn("1 reaped tree was a registered git worktree",
                      scratch.summary(rep))

    def test_a_LOCKED_registered_worktree_is_refused(self):
        unit = self.unit("locked-lane")
        self.register_worktree(unit, locked=True)
        rep = self.gc(apply=True)
        self.assertNotIn(unit, self.paths(rep))
        self.assertTrue(os.path.isdir(unit))
        self.assertIn("LOCKED", self.kept(rep, unit)[0])

    def test_a_DIRTY_unlocked_worktree_is_refused_and_a_CLEAN_one_is_not(self):
        """A lock is `do not disturb`; dirtiness is `this removal destroys the
        only copy`. Untracked counts — a new module nobody has added yet is
        exactly the work most easily lost. The clean half is the positive
        control: same fixture, same pass, one `git status` apart."""
        dirty = self.unit("dirty-lane")
        self.register_worktree(dirty, dirty=True)
        clean = self.unit("clean-lane")
        self.register_worktree(clean)
        rep = self.gc(apply=True)
        self.assertIn(clean, self.paths(rep))            # the gate really ran
        self.assertFalse(os.path.exists(clean))
        self.assertNotIn(dirty, self.paths(rep))
        self.assertTrue(os.path.isdir(dirty))
        self.assertIn("UNCOMMITTED WORK", self.kept(rep, dirty)[0])

    def test_a_worktree_whose_status_cannot_be_taken_is_UNKNOWN_not_clean(self):
        """The spawn allowance is a bound like any other, and a bound may stop
        a reading without answering it."""
        unit = self.unit("lane-unreadable")
        self.register_worktree(unit)
        with mock.patch.object(scratch, "WT_DIRTY_CAP", 0):
            rep = self.gc(apply=True)
        self.assertNotIn(unit, self.paths(rep))
        self.assertTrue(os.path.isdir(unit))
        self.assertIn("UNKNOWN", self.kept(rep, unit)[0])
        self.assertEqual(rep["tier2_unknown"]["worktree"], 1)
        # CONTROL: the identical fixture with an allowance IS reaped
        rep = self.gc(apply=True)
        self.assertIn(unit, self.paths(rep))
        self.assertFalse(os.path.exists(unit))

    def test_a_worktree_whose_git_status_FAILS_is_UNKNOWN_not_clean(self):  # noqa: VACUOUS_ASSERTION — the kept-reason assertIn names this exact unit through the same report, and the fixture's own worktree_dirty control asserts a non-None trouble before the pass runs
        """THE MUTATION MATRIX FOUND THIS GAP: the other two UNKNOWN arms both
        drive the pass's allowance, so `an untakeable status reads CLEAN`
        survived every one of them. A broken registration is the real case —
        the repo moved, or was pruned out from under the checkout — and git
        exits non-zero rather than answering."""
        unit = self.unit("lane-broken")
        repo, _admin = self.register_worktree(unit)
        shutil.rmtree(repo)             # the registration now points nowhere
        self.assertIsNotNone(scratch.worktree_link(unit))
        dirty, trouble = scratch.worktree_dirty(unit, timeout=5)
        self.assertFalse(dirty)         # the fixture's own control
        self.assertIsNotNone(trouble)
        rep = self.gc(apply=True)
        self.assertNotIn(unit, self.paths(rep))
        self.assertTrue(os.path.isdir(unit))
        self.assertIn("dirtiness UNKNOWN", self.kept(rep, unit)[0])
        self.assertEqual(rep["tier2_unknown"]["worktree"], 1)

    def test_the_dirtiness_budget_is_wall_time_and_not_only_a_count(self):
        """A COUNT IS NOT A BOUND ON COST. WT_DIRTY_CAP spawns each allowed
        WT_DIRTY_TIMEOUT_S is a worst case of minutes, and both tiers spend
        the same pass — so the allowance is shared and the wall clock is what
        actually holds it. Spending it is UNKNOWN, not clean."""
        unit = self.unit("lane-slow")
        self.register_worktree(unit)
        with mock.patch.object(scratch, "WT_DIRTY_BUDGET_S", -1.0):
            rep = self.gc(apply=True)
        self.assertNotIn(unit, self.paths(rep))
        self.assertTrue(os.path.isdir(unit))
        why = self.kept(rep, unit)[0]
        self.assertIn("dirtiness is UNKNOWN", why)
        self.assertIn("wall-clock deadline", why)
        self.assertEqual(rep["tier2_unknown"]["worktree"], 1)
        rep = self.gc(apply=True)               # CONTROL: with a budget
        self.assertIn(unit, self.paths(rep))
        self.assertFalse(os.path.exists(unit))

    def test_worktree_dirty_reads_a_real_checkout(self):
        """The one arm that spawns the real `git status` the gate leans on."""
        unit = self.unit("probe-lane")
        self.register_worktree(unit)
        self.assertEqual(scratch.worktree_dirty(unit, timeout=5),
                         (False, None))
        with open(os.path.join(unit, "new.py"), "w") as f:
            f.write("x")
        self.assertEqual(scratch.worktree_dirty(unit, timeout=5), (True, None))
        missing, trouble = scratch.worktree_dirty(
            os.path.join(self.tmp, "no-such-checkout"), timeout=5)
        self.assertFalse(missing)
        self.assertIsNotNone(trouble)

    def test_worktree_link_reads_the_pointer_file_not_a_git_dir(self):  # noqa: VACUOUS_ASSERTION — the registered pointer's admin dir is asserted through the same reader immediately before
        """A plain checkout has a .git DIRECTORY and is not a registration."""
        registered = self.unit("registered")
        _repo, admin = self.register_worktree(registered)
        self.assertEqual(scratch.worktree_link(registered)["admin"], admin)
        plain = self.unit("plain")
        os.makedirs(os.path.join(plain, ".git"))
        self.assertIsNone(scratch.worktree_link(plain))

    # ── the guards that were correct and had no arm ───────────────────────
    def test_a_GIT_POINTER_FILE_the_read_did_not_finish_keeps_the_unit(self):
        """THE SHARPEST SURVIVING MUTANT: `worktree_link`'s truncated-pointer
        branch. Delete it and a `.git` longer than the read reads as NO
        REGISTRATION — so the locked check, the dirtiness check and the prune
        record are all skipped and an applying pass DELETES a checkout holding
        uncommitted work, with the whole suite green.

        The refusal is also the one an owner reads, so it is pinned WHOLE: an
        inner reading's stop belongs in ONE clause, not nested inside the
        outer reading's own sentence."""
        unit = self.unit("copies")
        wt = os.path.join(unit, "lane")
        os.makedirs(wt)
        self.register_worktree(wt, dirty=True)
        self.age(unit)
        with mock.patch.object(scratch, "GITFILE_CAP", 0):
            rep = self.gc(apply=True)
        self.assertNotIn(unit, self.paths(rep))
        self.assertTrue(os.path.exists(os.path.join(wt, "unsaved.py")))
        why = self.kept(rep, unit)[0]
        self.assertIn("git pointer file reading stopped at its 0-byte read "
                      "cap", why)
        self.assertIn("whether a git worktree is registered inside it is "
                      "UNKNOWN", why)
        self.assertEqual(why.count("reading stopped at"), 1)
        self.assertEqual(rep["tier2_unknown"]["worktree"], 1)
        rep = self.gc(apply=True)      # CONTROL: one bound apart, the pointer
        self.assertNotIn(unit, self.paths(rep))   # reads whole and git answers
        self.assertIn("UNCOMMITTED WORK", self.kept(rep, unit)[0])

    def test_a_NEWEST_WRITE_walk_that_stops_at_DELETION_time_keeps_it(self):
        """A SURVIVING MUTANT: the deletion-time re-check's own bounded walk.
        The victim passed the age gate on a walk that FINISHED; if the walk
        taken again immediately before the unlink does not finish, its newest
        mtime is a floor and the age gate below it is deciding on a reading
        that measured nothing. Deleting the guard drops the victim straight
        past the age gate."""
        unit = self.unit("big", files=0)
        for i in range(30):
            with open(os.path.join(unit, "f%d" % i), "w") as f:
                f.write("x")
        self.age(unit)
        handles = {"held": [], "hazards": [], "unread": None, "trouble": None}
        v = {"path": unit, "ttl": 3600, "kind": "dir"}
        with mock.patch.object(scratch, "TIER2_MTIME_CAP", 5):
            stop = scratch._recheck_victim(v, time.time(), handles,
                                           self.proc, tier2=True)
        self.assertIsNotNone(stop)
        self.assertIn("newest-write walk", stop)
        self.assertIn("UNKNOWN", stop)
        # CONTROL: the same victim, the same second, a walk that finishes.
        self.assertIsNone(scratch._recheck_victim(
            v, time.time(), handles, self.proc, tier2=True))

    def test_a_LIVE_TREE_this_pass_never_OPENED_is_said_not_silent(self):
        """A SURVIVING MUTANT, and its row is live on the owner's box today:
        the tier opens a bounded slice of the live trees, and the trees past
        that slice are not units THIS pass. Saying nothing makes a throttle
        read as `there was nothing else`."""
        second = "cccccccc-1111-2222-3333-444444444444"
        pad = os.path.join(self.root, second, "scratchpad")
        os.makedirs(pad)
        self.fresh(os.path.join(self.root, second, "now.txt"))
        self.unit("copy-two", where=pad)
        self.live_proc("998", second)
        self.unit("copy-one")
        with mock.patch.object(scratch, "TIER2_TREE_CAP", 1):
            rep = self.gc(apply=True)
        row = self.kept(rep, "(second tier)")
        self.assertTrue(row, rep["tier2_kept"])
        self.assertIn("live-tree slice reading stopped", row[0])
        self.assertIn("drain on the next one", row[0])
        self.assertEqual(len(rep["tier2_trees"]), 1)
        rep = self.gc(apply=True)      # CONTROL: a slice that fits both trees
        self.assertEqual(len(rep["tier2_trees"]), 2)
        self.assertEqual(self.kept(rep, "(second tier)"), [])

    def test_the_DIRTINESS_SPAWN_cannot_be_called_without_its_bound(self):
        """`worktree_dirty(path, timeout=None)` defaulted to an UNBOUNDED
        spawn inside a function whose docstring promises ONE BOUNDED spawn.
        A default cannot be seen by the structural tripwire — a keyword
        default is not a bound read — so the signature itself is the arm, and
        every call site in the package is checked for the argument."""
        with self.assertRaises(TypeError):
            scratch.worktree_dirty(self.tmp)
        tree = ast.parse(io.open(
            os.path.join(PKG, "scratch.py")).read())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", None))
                 == "worktree_dirty"]
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("timeout", [k.arg for k in call.keywords],
                          "worktree_dirty at line %d spawns unbounded"
                          % call.lineno)


class UnattributableTest(unittest.TestCase):
    """HOLE 3: `never reap what you cannot attribute` is the floor and it
    stays — but it was also SILENT, and 8 GB of a 12.8 GB tmp root sat in
    entries no shape gate admits. The cure is the listing, not a new deletion:
    nothing in this class removes anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-scratch-unattr-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.realpath(os.path.join(self.tmp, "root"))
        os.makedirs(self.root)

    def entry(self, name, kb=1, age_h=1):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "blob"), "w") as f:
            f.write("x" * (kb * 1024))
        old = time.time() - age_h * 3600
        os.utime(os.path.join(d, "blob"), (old, old))
        os.utime(d, (old, old))
        return d

    def test_the_listing_is_biggest_first_with_size_and_age(self):
        small = self.entry("small", kb=4, age_h=1)
        big = self.entry("big", kb=400, age_h=9)
        rows = scratch.unattributable([self.root])
        self.assertEqual([r["path"] for r in rows], [big, small])
        self.assertGreater(rows[0]["bytes"], rows[1]["bytes"])
        self.assertGreaterEqual(rows[0]["age"], 9 * 3600)

    def test_an_attributable_child_is_not_in_the_listing(self):
        """THE CONTROL on the shape gate: a session-named tree belongs to tier
        one and must not be double-counted as unreachable."""
        self.entry("aaaaaaaa-1111-2222-3333-444444444444")
        self.entry("pid-4242")
        mine = self.entry("codex3-task2340-leg34")
        self.assertEqual([r["path"] for r in scratch.unattributable([self.root])],
                         [mine])

    def test_the_harness_per_uid_root_is_globbed_not_listed(self):
        self.entry("claude-1000")
        other = self.entry("other")
        self.assertEqual([r["path"] for r in scratch.unattributable([self.root])],
                         [other])

    def test_a_bounded_walk_marks_its_size_as_a_floor(self):
        """The budget must be SMALLER THAN THE TREE for this to measure a
        floor: a budget that happens to cover every entry produced a complete
        reading, and calling that one a floor would be the mirror image of the
        defect this module is about."""
        wide = self.entry("wide", kb=4)
        for i in range(4):
            with open(os.path.join(wide, "f%d" % i), "w") as f:
                f.write("x" * 1024)
        rows = scratch.unattributable([self.root], budget=[2])
        self.assertTrue(rows[0]["at_least"])
        full = scratch.unattributable([self.root])
        self.assertFalse(full[0]["at_least"])
        self.assertGreater(full[0]["bytes"], rows[0]["bytes"])

    def test_an_entry_on_another_filesystem_is_not_this_mounts_cost(self):
        """Measured: the two biggest rows without this rule were 529 MB
        AppImage mounts under the tmp root — a squashfs image each, not one
        byte of the tmpfs, and the last thing to tell anyone to remove."""
        self.entry("big", kb=64)
        self.assertTrue(scratch.unattributable([self.root]))   # the control
        real = os.stat

        def elsewhere(path, *a, **kw):
            st = real(path, *a, **kw)
            if os.path.realpath(str(path)) == self.root:
                return os.stat_result(tuple(st)[:2] + (st.st_dev + 1,)
                                      + tuple(st)[3:])
            return st

        with mock.patch.object(os, "stat", side_effect=elsewhere):
            self.assertEqual(scratch.unattributable([self.root]), [])

    def test_a_live_helm_surface_is_named_as_one(self):
        bus = self.entry("helm-chat")
        with mock.patch.object(scratch, "helm_paths",
                               return_value=[("chat bus", bus)]):
            rows = scratch.unattributable([self.root])
        self.assertEqual(rows[0]["role"], "chat bus")

    def test_a_root_routing_no_longer_picks_is_still_a_root(self):
        """Routing is LIVE: a tmp mount that loses its headroom stops being the
        `small` root, and the tree it already holds must not become an unnamed
        stranger's the moment that line is crossed (measured: 2.1 GB read that
        way). Its own child is what the listing names."""
        amb = os.path.join(self.tmp, "amb")
        leaf = os.path.join(amb, scratch.SCRATCH_LEAF)
        os.makedirs(leaf)
        inside = os.path.join(leaf, "tmpdir")
        os.makedirs(inside)
        with open(os.path.join(inside, "blob"), "w") as f:
            f.write("x" * 2048)
        with mock.patch.object(scratch, "_ambient_tmp", return_value=amb), \
             mock.patch.object(scratch, "route",
                               return_value=("/nonexistent-root", "x")), \
             mock.patch.object(scratch, "tmpdir_roots", return_value=[]), \
             mock.patch.object(scratch.home, "env", return_value=None):
            roots = scratch.unattr_roots()
            rows = scratch.unattributable()
        self.assertIn(os.path.realpath(leaf), roots)
        paths = [r["path"] for r in rows]
        self.assertIn(inside, paths)         # its child is the real cost
        self.assertNotIn(leaf, paths)        # the root itself is not a stranger

    def test_the_scan_is_bounded_per_root(self):
        for i in range(6):
            self.entry("e%d" % i)
        self.assertEqual(len(scratch.unattributable([self.root])), 6)
        self.assertEqual(len(scratch.unattributable([self.root], scan_cap=3)), 3)

    def test_the_doctor_row_is_silent_until_something_is_pressured(self):
        listing = [{"path": "/tmp/x", "bytes": 1 << 20, "age": 9, "entries": 1,
                    "at_least": False, "root": "/tmp", "role": ""}]
        quiet = {"volatile": True, "dev": 1, "bytes_pct": 10,
                 "inodes_pct": 10, "mount": "/tmp"}
        with mock.patch.object(scratch, "host_memory", return_value=HOST_OK):
            hot = scratch.unattributable_rows(
                [dict(quiet, bytes_pct=97)], listing)
            self.assertEqual(len(hot), 1)          # the same reader DOES speak
            with mock.patch.object(scratch, "unattributable") as walk:
                self.assertEqual(scratch.unattributable_rows([quiet]), [])
        self.assertFalse(walk.called)

    def test_the_doctor_row_names_the_cost_and_the_manual_act(self):
        rows = [{"volatile": True, "dev": 1, "bytes_pct": 97, "inodes_pct": 10,
                 "mount": "/tmp"}]
        listing = [{"path": "/tmp/codex3-task2340-leg34", "bytes": 1 << 30,
                    "age": 4 * 86400, "at_least": True, "entries": 9,
                    "root": "/tmp", "role": ""}]
        with mock.patch.object(scratch, "host_memory", return_value=HOST_OK):
            out = scratch.unattributable_rows(rows, listing)
        self.assertEqual(out[0][0], scratch.WARN)
        self.assertIn("NEVER reaps", out[0][1])
        self.assertIn("/tmp/codex3-task2340-leg34 >=1.0G (4d old)", out[0][1])
        self.assertIn("helm scratch unattributable", out[0][1])
        self.assertIn("MANUAL", out[0][1])

    def test_the_doctor_row_refuses_to_rank_what_its_budget_never_walked(self):
        """M2. The row's budget is spent in READDIR order, so the trees it
        reached last were never walked at all: their size is the top-level
        lstat and they sort last whatever they hold. Measured on the real box,
        the three genuinely biggest unattributable trees — helm's own routed
        TMPDIR root among them — were all absent from a row that opened with
        the word `Biggest`. The unstarved half is the control."""
        rows = [{"volatile": True, "dev": 1, "bytes_pct": 97, "inodes_pct": 10,
                 "mount": "/tmp"}]
        walked = [{"path": "/tmp/a", "bytes": 1 << 30, "age": 4 * 86400,
                   "at_least": False, "starved": False, "entries": 9,
                   "root": "/tmp", "role": ""}]
        with mock.patch.object(scratch, "host_memory", return_value=HOST_OK):
            ranked = scratch.unattributable_rows(rows, walked)
            starved = scratch.unattributable_rows(
                rows, walked + [{"path": "/tmp/b", "bytes": 4096, "age": 60,
                                 "at_least": True, "starved": True,
                                 "entries": 1, "root": "/tmp", "role": ""}])
        self.assertIn("Biggest", ranked[0][1])
        self.assertNotIn("SAMPLED", ranked[0][1])
        self.assertNotIn("Biggest", starved[0][1])
        self.assertIn("SAMPLED, NOT RANKED", starved[0][1])
        self.assertIn("helm scratch unattributable", starved[0][1])

    def test_a_row_the_budget_never_reached_is_marked_starved(self):
        """The flag the row above leans on, measured against real trees: a
        budget that runs out mid-listing leaves the rest unwalked, and an
        unwalked 400 KB tree outranks nothing."""
        self.entry("aaa-first", kb=4)
        big = self.entry("zzz-biggest", kb=400)
        full = scratch.unattributable([self.root])
        self.assertEqual(full[0]["path"], big)      # control: ranked correctly
        self.assertFalse(any(r["starved"] for r in full))
        thin = scratch.unattributable([self.root], budget=[1])
        self.assertTrue(any(r["starved"] for r in thin))

    def test_host_pressure_alone_opens_the_listing(self):
        """The whole point of hole one: the MOUNT is fine and the box is not."""
        rows = [{"volatile": True, "dev": 1, "bytes_pct": 10, "inodes_pct": 10,
                 "mount": "/tmp"}]
        hot = dict(HOST_OK, level="warn")
        with mock.patch.object(scratch, "host_memory", return_value=hot), \
             mock.patch.object(scratch, "unattributable", return_value=[]):
            scratch.unattributable_rows(rows)
            self.assertTrue(scratch.unattributable.called)

    def test_a_disk_only_estate_never_pays_for_the_listing(self):
        rows = [{"volatile": False, "dev": 1, "bytes_pct": 99,
                 "inodes_pct": 99, "mount": "/"}]
        ram = [dict(rows[0], volatile=True, mount="/tmp")]
        listing = [{"path": "/tmp/x", "bytes": 1 << 20, "age": 9, "entries": 1,
                    "at_least": False, "root": "/tmp", "role": ""}]
        with mock.patch.object(scratch, "host_memory", return_value=HOST_OK):
            self.assertEqual(len(scratch.unattributable_rows(ram, listing)), 1)
            self.assertEqual(len(scratch.host_rows(ram)), 1)
            with mock.patch.object(scratch, "unattributable") as walk:
                self.assertEqual(scratch.unattributable_rows(rows), [])
                self.assertEqual(scratch.host_rows(rows), [])
        self.assertFalse(walk.called)


class HostDoctorRowTest(unittest.TestCase):
    RAM = [{"volatile": True, "dev": 1, "mount": "/tmp", "bytes_pct": 10,
            "inodes_pct": 10}]

    def test_a_healthy_host_reads_ok_with_both_axes(self):
        with mock.patch.object(scratch, "host_memory", return_value=HOST_OK):
            out = scratch.host_rows(self.RAM)
        self.assertEqual(out[0][0], scratch.OK)
        self.assertIn("MemAvailable 90%", out[0][1])
        self.assertIn("swap 0% used", out[0][1])

    def test_a_pressured_host_warns_and_names_the_new_ttl(self):
        hot = dict(HOST_OK, level="warn", swap_used_pct=42,
                   why="swap 42% used (warn at 25)")
        with mock.patch.object(scratch, "host_memory", return_value=hot):
            out = scratch.host_rows(self.RAM)
        self.assertEqual(out[0][0], scratch.WARN)
        self.assertIn("EVERY BYTE of /tmp IS THIS MEMORY", out[0][1])
        self.assertIn("6h ttl", out[0][1])

    def test_the_row_names_the_ttl_the_reaper_will_actually_use(self):
        """L1. The reaper keys on worse(mount, host); the row rendered the HOST
        level alone, so it understated how aggressive the pass is about to be
        exactly on the box where both planes are bad. The warn/ok half above is
        the control: same reader, same host, a mount percentage apart."""
        hot = dict(HOST_OK, level="warn", swap_used_pct=42,
                   why="swap 42% used (warn at 25)")
        full = [dict(self.RAM[0], bytes_pct=97)]
        with mock.patch.object(scratch, "host_memory", return_value=hot):
            out = scratch.host_rows(full)
        self.assertEqual(out[0][0], scratch.WARN)
        self.assertIn("1h ttl", out[0][1])          # NOT the host's own 6h
        self.assertIn("effective level critical", out[0][1])
        self.assertIn("aged children to 1h", out[0][1])

    def test_an_unreadable_host_says_so_instead_of_escalating(self):
        blind = dict(HOST_OK, trouble="host memory UNREADABLE (x) — no "
                                      "escalation")
        with mock.patch.object(scratch, "host_memory", return_value=blind):
            out = scratch.host_rows(self.RAM)
        self.assertEqual(out[0][0], scratch.WARN)
        self.assertIn("MOUNT ttl only", out[0][1])


class HostMutationTripwireTest(unittest.TestCase):
    """The stop-guard's silent-mechanical lane now DELETES files (dead-session
    scratch under the real /tmp/claude-* harness estate). CONTRIBUTING.md's law
    is that tests never touch a real harness store — so any test module that
    drives the stop hook must neutralize the reaper. Source-driven, so a NEW
    test module that forgets trips this instead of quietly eating host scratch."""

    # an INVOCATION, not a mention: `stop_guard(...)`, or "stop-guard" passed
    # as a verb argument. A hooks-table membership check (`rows[x]["stop-guard"]`
    # or `s["name"] == "stop-guard"`) drives nothing and must not trip.
    _DRIVES = re.compile(r"stop_guard\(|[\"']stop-guard[\"']\s*,")

    def test_every_module_driving_the_stop_hook_disables_the_reaper(self):
        offenders = []
        for fn in sorted(os.listdir(TESTS)):
            if not (fn.startswith("test_") and fn.endswith(".py")):
                continue
            with open(os.path.join(TESTS, fn), encoding="utf-8") as f:
                text = f.read()
            if fn == os.path.basename(__file__):
                continue          # this module stubs scratch.auto_gc directly
            if self._DRIVES.search(text) and "HELM_SCRATCH_GC" not in text:
                offenders.append(fn)
        self.assertFalse(
            offenders,
            "test module(s) %r drive the Stop hook without setting "
            "HELM_SCRATCH_GC=0 — the scratch reaper would DELETE real "
            "dead-session scratch under /tmp/claude-* during a test run. Set "
            "it in the test's setUp (see tests/test_seats.py SeatsBase)."
            % offenders)

    def test_the_reaper_is_pointed_at_a_real_root_in_production(self):
        """The tripwire above must not be vacuous: the shipped spec list really
        does cover the harness scratchpad estate."""
        pats = [p for p, _lab in scratch.scratch_specs()]
        self.assertTrue(any("claude-*" in p for p in pats), pats)


class ReapOwnedTest(unittest.TestCase):
    """The reaper both scratch doors use (tests/__init__.py at exit, the
    gate's finally) removes what ignore_errors rmtree keeps."""

    def _release_shaped(self):
        root = tempfile.mkdtemp(prefix="helm-test-reap-")
        self.addCleanup(scratch.reap_owned, root)
        ro = os.path.join(root, "releases", "deadbeef")
        os.makedirs(os.path.join(ro, "helm"))
        with open(os.path.join(ro, "helm", "x.py"), "w") as f:
            f.write("")
        os.chmod(os.path.join(ro, "helm", "x.py"), 0o444)
        os.chmod(os.path.join(ro, "helm"), 0o555)
        os.chmod(ro, 0o555)
        return root, ro

    def test_a_plain_rmtree_keeps_the_release_tree(self):
        """THE CONTROL: the shape really defeats the old remover, so the arm
        below is measuring the hardening and not an empty tree."""
        root, ro = self._release_shaped()
        shutil.rmtree(root, ignore_errors=True)
        self.assertTrue(os.path.exists(os.path.join(ro, "helm", "x.py")))

    def test_reap_owned_removes_it(self):
        root, ro = self._release_shaped()
        # POSITIVE CONTROL on the same observable: the read-only file is
        # there to remove before the reap says it is gone.
        self.assertTrue(os.path.exists(os.path.join(ro, "helm", "x.py")))
        scratch.reap_owned(root)
        self.assertFalse(os.path.exists(root))

    def test_a_symlink_inside_is_never_followed(self):
        root, _ro = self._release_shaped()
        victim = tempfile.mkdtemp(prefix="helm-test-reap-victim-")
        self.addCleanup(shutil.rmtree, victim, ignore_errors=True)
        with open(os.path.join(victim, "keep"), "w") as f:
            f.write("")
        os.symlink(victim, os.path.join(root, "link"))
        scratch.reap_owned(root)
        self.assertFalse(os.path.exists(root))
        self.assertTrue(os.path.exists(os.path.join(victim, "keep")))

    def test_a_missing_tree_is_not_an_error(self):  # noqa: VACUOUS_ASSERTION — the positive arm above proves the same call removes a real tree; this pins that the atexit/finally callers never see a raise
        scratch.reap_owned(os.path.join(tempfile.gettempdir(), "no-such-reap"))


class OwnedBoundaryTest(unittest.TestCase):
    """THE ROOT BOUNDARY. Owned cleanup must not change permissions through a
    substituted symlink: neither a symlinked ROOT (os.walk followed it and the
    target's children went 0o755 -> 0o700 outside the named tree, through the
    registered atexit) nor a child swapped for a link between the look and
    the chmod. Every arm here FAILED on the os.walk cut and passes on the
    descriptor-based one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name)
        self.victim = self.parent / "victim"
        self.victim.mkdir()
        self.keep = self.victim / "keep"
        self.keep.mkdir()
        # EXPLICIT, NOT mkdir(mode=): the mode argument is masked by umask,
        # and the fab runs the suite under 077 — every arm below read 0o700
        # where it planted 0o755 and failed the control it never touched.
        self.keep.chmod(0o755)
        (self.keep / "data").write_text("preserved")
        self.owned = self.parent / "owned"

    def tearDown(self):
        self.tmp.cleanup()

    def assert_target_untouched(self):
        self.assertEqual(stat.S_IMODE(self.keep.stat().st_mode), 0o755)
        self.assertEqual((self.keep / "data").read_text(), "preserved")

    def test_root_symlink_does_not_mode_its_target(self):  # noqa: VACUOUS_ASSERTION — assert_target_untouched asserts the target child still reads 0o755 and its data; that is the positive control on the observable this arm protects
        self.owned.symlink_to(self.victim, target_is_directory=True)
        scratch.reap_owned(str(self.owned))
        self.assert_target_untouched()

    def test_child_replaced_at_mode_boundary_is_not_followed(self):  # noqa: VACUOUS_ASSERTION — hits == [replaced] proves the swap fired, the target mode/data equality is the positive control, and the owned root's absence is the reap's own effect
        self.owned.mkdir()
        branch = self.owned / "branch"
        branch.mkdir()
        opened, chmod = os.open, os.chmod
        hits = []

        def replace():
            if not hits:
                branch.rename(self.owned / "moved")
                branch.symlink_to(self.keep, target_is_directory=True)
                hits.append("replaced")

        def interleaved_open(path, flags, *args, **kwargs):
            if path == "branch" and kwargs.get("dir_fd") is not None:
                replace()
            return opened(path, flags, *args, **kwargs)

        def interleaved_chmod(path, mode, *args, **kwargs):
            if path == str(branch):
                replace()
            return chmod(path, mode, *args, **kwargs)

        # Exercise the same replacement at the old pathname chmod boundary
        # and the descriptor-based no-follow open boundary.
        with mock.patch.object(scratch.os, "open", side_effect=interleaved_open), \
                mock.patch.object(scratch.os, "chmod", side_effect=interleaved_chmod):
            scratch.reap_owned(str(self.owned))
        self.assertEqual(hits, ["replaced"])
        self.assert_target_untouched()
        self.assertFalse(self.owned.exists())

    def test_normal_exit_refuses_a_replaced_process_root(self):  # noqa: VACUOUS_ASSERTION — the child exits 0 and its stdout names a symlink that IS one; the target mode/data equality is the positive control through the real atexit
        """Through the ACTUAL registered atexit: a child imports the tests
        package, replaces its own _TESTROOT with a link to the victim, and
        exits naturally — the target's children keep 0o755."""
        ambient = self.parent / "ambient"
        ambient.mkdir()
        code = """import pathlib, shutil, sys
sys.path.insert(0, sys.argv[1])
import tests
owned = pathlib.Path(tests._testroot())
assert owned.is_dir() and not owned.is_symlink()
shutil.rmtree(owned)
owned.symlink_to(sys.argv[2], target_is_directory=True)
print(owned, flush=True)
"""
        source = str(Path(__file__).resolve().parents[1])
        child = subprocess.run(
            [sys.executable, "-c", code, source, str(self.victim)],
            env=dict(os.environ, TMPDIR=str(ambient)), capture_output=True,
            text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertTrue(Path(child.stdout.strip()).is_symlink())
        self.assert_target_untouched()

    def test_readonly_root_and_subtree_are_removed(self):  # noqa: VACUOUS_ASSERTION — the root and file are planted read-only above and the sibling ReapOwnedTest control proves a plain rmtree keeps that shape
        self.owned.mkdir()
        release = self.owned / "release"
        release.mkdir()
        (release / "data").write_text("remove")
        release.chmod(0o555)
        self.owned.chmod(0o555)
        try:
            scratch.reap_owned(str(self.owned))
            self.assertFalse(self.owned.exists())
        finally:
            # A failed arm must not prevent the fixture from cleaning itself.
            if self.owned.exists():
                self.owned.chmod(0o700)
                if release.exists():
                    release.chmod(0o700)
                shutil.rmtree(self.owned)
        self.assert_target_untouched()


class BoundedReadingTripwireTest(unittest.TestCase):
    """THE SHAPE, ENFORCED — the structural half of round three.

    Three rounds of one finding, and every round cured the instances the
    previous round NAMED: round one found two caps that turned a truncated
    reading into a conclusion, round two cured those two, and the verifier
    then found five more siblings in the same functions. Patching the sixth
    would have bought round four. So the module now routes every bound through
    one primitive (scratch.Reading), and this walks its AST to keep it that
    way. TWO ADMITTED HOMES, and no others: the primitive's class body, and an
    argument to `Reading(...)` / `Reading.pool(...)`.

    FIVE SHAPES, because a walker that only refuses the shapes someone
    happened to write is a walker a bound walks around. A rule keyed on the
    NAME of a constant is blind to `if n >= 500: break`, which names nothing
    at all and is a form this module has carried. So the refusal covers a
    bound constant or one of the primitive's own bound fields read outside
    it, a sized read, a slice with a constant upper bound, a range/islice
    length, and a counter-guarded break, return or loop head. The last of
    those needs a COUNT told apart from a SENTINEL (`if n >= 500: break`
    stops a reading; `if cur <= 1: break` reaches init), and both are derived
    from the module rather than listed here.

    A LINTER NOBODY HAS SEEN FAIL IS NOT EVIDENCE. Every check below is run
    twice — once over the real module, once over a copy carrying a PLANTED
    violation of exactly that check — because a walker with a typo in its
    pattern reports a clean tree forever. Ten shapes are planted as separate
    must-hits, each paired with a control that must stay silent."""

    BOUND = re.compile(r"_CAP$|_BUDGET|_HOPS$|_MAX|_TIMEOUT")
    SOURCE = os.path.join(PKG, "scratch.py")

    def setUp(self):
        with open(self.SOURCE) as f:
            self.src = f.read()

    # ── the walker ────────────────────────────────────────────────────────
    def bounds_in(self, tree):
        """Every module-level constant whose NAME says it is a bound. Derived
        from the module, never transcribed: a constant added tomorrow is
        covered the day it is written."""
        out = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and self.BOUND.search(target.id):
                    out.add(target.id)
        return out

    def module_names(self, tree):
        """EVERY module-level name, not only the ones whose spelling happens
        to match the bound pattern. A bound named `SCAN_LIMIT` bounds exactly
        as hard as one named `SCAN_CAP`, and the regex is a naming
        convention, never the enforcement."""
        out = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        out.add(t.id)
            elif (isinstance(node, ast.AnnAssign)
                  and isinstance(node.target, ast.Name)):
                out.add(node.target.id)
        return out

    def violations(self, src):
        """[(line, what)] — every place a bound escapes the primitive.

        FIVE SHAPES, because a bound has five ways to stop a reading and
        curing only the ones the previous round wrote is how a finding buys
        another round: a NAME read outside the primitive, a SIZED read, a
        SLICE, a COUNTER-GUARDED break or return, and a range()/islice()
        length. The last two are the ones that need a COUNT and a SIZE told
        apart — `if n >= 500: break` bounds a reading and `if cur <= 1: break`
        reaches a root — so both are derived from the module: a count is
        something the module itself increments, lengths, indexes or clamps,
        and a size is a literal, a module-level name, or a local bound to
        one."""
        tree = ast.parse(src)
        bounds = self.bounds_in(tree)
        consts = self.module_names(tree)
        parent = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        inside = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Reading":
                inside.update(ast.walk(node))
        fields = set()
        for node in inside:
            if isinstance(node, ast.FunctionDef) and node.name == "__init__":
                fields.update(a.arg for a in node.args.args[2:])
        mints = {"Reading"}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                for r in ast.walk(node):
                    if (isinstance(r, ast.Return)
                            and isinstance(r.value, ast.Call)
                            and isinstance(r.value.func, ast.Name)
                            and r.value.func.id == "Reading"):
                        mints.add(node.name)

        def routed(node):
            """Is this being HANDED to the primitive? That, and the class
            body itself, are the only two admitted homes for a bound:
            `Reading(cap=X)` and `Reading.pool(X)` are the routing itself."""
            while node in parent:
                node = parent[node]
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "Reading":
                        return True
                    if (isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "Reading"):
                        return True
            return False

        def enclosing(node):
            while node in parent:
                node = parent[node]
                if isinstance(node, ast.FunctionDef):
                    return node
            return tree

        scope = {}

        def scanned(fn):
            """(what each local name is bound to, which locals are COUNTS)."""
            if fn in scope:
                return scope[fn]
            binds, counts = {}, set()
            if fn is not tree:
                for node in ast.walk(fn):
                    if isinstance(node, ast.Assign):
                        for t in node.targets:
                            if isinstance(t, ast.Name):
                                binds.setdefault(t.id, []).append(node.value)
                    elif (isinstance(node, ast.AugAssign)
                          and isinstance(node.target, ast.Name)):
                        if isinstance(node.op, ast.Add):
                            counts.add(node.target.id)   # `n += 1` IS a count
                        else:
                            binds.setdefault(node.target.id,
                                             []).append(node.value)
                    elif isinstance(node, ast.For):
                        it = node.iter
                        if (isinstance(it, ast.Call)
                                and isinstance(it.func, ast.Name)
                                and it.func.id in ("range", "enumerate")):
                            tgt = node.target
                            names = ([tgt] if isinstance(tgt, ast.Name) else
                                     [e for e in getattr(tgt, "elts", ())
                                      if isinstance(e, ast.Name)])
                            if names:
                                counts.add(names[0].id)
                    elif (isinstance(node, ast.BinOp)
                          and isinstance(node.op, ast.Add)):
                        for a, b in ((node.left, node.right),
                                     (node.right, node.left)):
                            if isinstance(a, ast.Name) and literal(b):
                                counts.add(a.id)         # `depth + 1` too
            scope[fn] = (binds, counts)
            return scope[fn]

        def literal(node):
            return (isinstance(node, ast.Constant)
                    and isinstance(node.value, int)
                    and not isinstance(node.value, bool))

        def clamp(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("min", "max"))

        def size_like(node, seen=frozenset()):
            """A SIZE: a literal int, ANY module-level name, a local bound to
            one of those, arithmetic on them, or a min()/max() clamp over
            them."""
            if literal(node):
                return True
            if isinstance(node, ast.Name):
                if node.id in consts:
                    return True
                if node.id in seen:
                    return False
                binds, _counts = scanned(enclosing(node))
                return any(size_like(v, seen | {node.id})
                           for v in binds.get(node.id, ()))
            if isinstance(node, ast.BinOp):
                return size_like(node.left, seen) or size_like(node.right, seen)
            if clamp(node):
                return any(size_like(a, seen) for a in node.args)
            return False

        def count_like(node, seen=frozenset()):
            """A COUNT: len()/sum(), a name the module increments, a range or
            enumerate index, or arithmetic and clamps over them. A pid read
            out of a stat line is not one, which is why `if cur <= 1: break`
            is a root and not a bound."""
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in ("len", "sum"):
                    return True
                if isinstance(func, ast.Attribute) and func.attr == "count":
                    return True
                if clamp(node):
                    return any(count_like(a, seen) for a in node.args)
            if isinstance(node, ast.Name):
                binds, counts = scanned(enclosing(node))
                if node.id in counts:
                    return True
                if node.id in seen:
                    return False
                return any(count_like(v, seen | {node.id})
                           for v in binds.get(node.id, ()))
            if isinstance(node, ast.BinOp):
                return (count_like(node.left, seen)
                        or count_like(node.right, seen))
            return False

        def bound_in(test, stopping):
            """The SIZE a comparison in `test` measures a count against, in
            the polarity that stops a reading — `count >= size` for a guard
            that breaks or returns, `count < size` for a loop head that runs
            while under it — or None."""
            reach = ((ast.Gt, ast.GtE, ast.Eq) if stopping
                     else (ast.Lt, ast.LtE))
            back = ((ast.Lt, ast.LtE, ast.Eq) if stopping
                    else (ast.Gt, ast.GtE))
            for cmp_ in (n for n in ast.walk(test)
                         if isinstance(n, ast.Compare)):
                if len(cmp_.ops) != 1:
                    continue
                op, left = cmp_.ops[0], cmp_.left
                right = cmp_.comparators[0]
                if (isinstance(op, reach) and count_like(left)
                        and size_like(right)):
                    return right
                if (isinstance(op, back) and size_like(left)
                        and count_like(right)):
                    return left
            return None

        def reading_backed(node):
            """Is this `.read()`'s receiver the primitive itself? That is the
            one sized read this module is allowed to make."""
            if not isinstance(node, ast.Name):
                return False
            binds, _counts = scanned(enclosing(node))
            for v in binds.get(node.id, ()):
                if not isinstance(v, ast.Call):
                    continue
                func = v.func
                if isinstance(func, ast.Name) and func.id in mints:
                    return True
                if (isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "Reading"):
                    return True
            return False

        def length_of(call, name):
            """The LENGTH argument of a range()/islice(), whichever position
            this spelling puts it in."""
            if name == "range" and call.args:
                return call.args[0] if len(call.args) == 1 else call.args[1]
            if name == "islice" and len(call.args) > 1:
                return call.args[1] if len(call.args) == 2 else call.args[2]
            return None

        bad = []
        for node in ast.walk(tree):
            if node in inside or routed(node):
                continue
            if (isinstance(node, ast.Name) and node.id in bounds
                    and isinstance(node.ctx, ast.Load)):
                bad.append((node.lineno, "%s read outside Reading" % node.id))
            if (isinstance(node, ast.Attribute) and node.attr in fields
                    and isinstance(node.ctx, ast.Load)):
                bad.append((node.lineno, "Reading.%s read outside Reading"
                            % node.attr))
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("read", "readline", "readlines",
                                           "recv", "recvfrom")
                    and node.args and not reading_backed(node.func.value)):
                bad.append((node.lineno, "sized .%s()" % node.func.attr))
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Slice)
                    and node.slice.upper is not None
                    and size_like(node.slice.upper)):
                bad.append((node.lineno, "slice with a constant upper bound"))
            if isinstance(node, ast.If):
                size = bound_in(node.test, stopping=True)
                if size is not None and any(
                        isinstance(x, (ast.Break, ast.Return))
                        for x in ast.walk(node)):
                    bad.append((node.lineno, "a counted break/return bounded "
                                             "by %s" % ast.unparse(size)))
            if isinstance(node, ast.While):
                size = bound_in(node.test, stopping=False)
                if size is not None:
                    bad.append((node.lineno,
                                "a loop bounded by %s" % ast.unparse(size)))
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute)
                        else "")
                length = length_of(node, name)
                if length is not None and (
                        literal(length) or size_like(length)
                        or (isinstance(length, ast.Name)
                            and not count_like(length))):
                    bad.append((node.lineno, "%s() bounded by %s"
                                % (name, ast.unparse(length))))
        return sorted(set(bad))

    # ── the module obeys it ───────────────────────────────────────────────
    def test_every_bound_in_the_module_is_held_by_the_primitive(self):  # noqa: VACUOUS_ASSERTION — five planted-violation tests in this class prove the same walker reports a violation when one exists
        bad = self.violations(self.src)
        self.assertEqual(bad, [], "helm/scratch.py reads a bound outside "
                                  "scratch.Reading: %s" % bad)

    def test_the_inventory_is_not_empty_and_names_the_known_bounds(self):
        """THE WALKER'S OWN MUST-HIT. A pattern that matched nothing would
        make the test above pass on any file at all, including an empty one."""
        found = self.bounds_in(ast.parse(self.src))
        self.assertGreater(len(found), 20)
        for known in ("PROC_BUDGET", "BLOB_CAP", "WT_SCAN_CAP",
                      "TIER2_COUNT_CAP", "COUNT_CAP", "OPAQUE_HOPS",
                      "FD_BUDGET", "TOCTOU_HANDLE_MAX_AGE_S",
                      "WT_DIRTY_TIMEOUT_S"):
            self.assertIn(known, found)
        for not_a_bound in ("WARN_PCT", "THROTTLE_S", "BIG_INODE_FLOOR",
                            "UNATTR_AGE_FLOOR"):
            self.assertNotIn(not_a_bound, found)

    # ── and the walker can SEE a violation ────────────────────────────────
    def planted(self, snippet, module_level=""):
        """The module as it stands, plus one function carrying exactly one
        planted bound — and, where the shape needs one, a module-level
        constant to bound it with."""
        return ("%s\n\n%s\n\ndef _planted_violation(seq, fh, depth=0):\n%s"
                % (self.src, module_level, snippet))

    def caught(self, snippet, module_level=""):
        """What the walker says about a planted violation that it does not
        already say about the module underneath it."""
        base = self.violations(self.src)
        return [row for row in self.violations(
            self.planted(snippet, module_level)) if row not in base]

    def test_a_planted_SLICE_is_caught(self):
        bad = self.violations(self.planted("    return seq[:PROC_BUDGET]\n"))
        self.assertTrue(any("PROC_BUDGET" in what for _ln, what in bad), bad)

    def test_a_planted_COMPARISON_guarding_a_return_is_caught(self):
        bad = self.violations(self.planted(
            "    if len(seq) > TIER2_COUNT_CAP:\n        return None\n"
            "    return seq\n"))
        self.assertTrue(any("TIER2_COUNT_CAP" in what for _ln, what in bad),
                        bad)

    def test_a_planted_sized_READ_is_caught(self):
        bad = self.violations(self.planted("    return fh.read(BLOB_CAP)\n"))
        self.assertTrue(any("BLOB_CAP" in what or "sized" in what
                            for _ln, what in bad), bad)

    def test_a_planted_LITERAL_read_is_caught_even_without_a_constant(self):
        """The literal is the case a name-based rule cannot see, and the
        module carried two of them — a 4,096-byte read of /proc/<pid>/stat and
        another of a `.git` pointer file — for three rounds."""
        bad = self.violations(self.planted("    return fh.read(4096)\n"))
        self.assertTrue(any("sized .read()" == what for _ln, what in bad), bad)

    def test_a_planted_LITERAL_slice_is_caught(self):
        bad = self.violations(self.planted("    return seq[:3]\n"))
        self.assertTrue(any("upper bound" in what for _ln, what in bad), bad)

    # ── TEN SHAPES A BOUND CAN TAKE, each its own must-hit ────────────────
    # The previous walker caught two of these ten. Each arm below fails if the
    # walker stops seeing that shape, which is the only thing that keeps the
    # law from being prose again.
    def test_SHAPE_A_a_slice_bounded_by_a_pattern_named_constant(self):
        bad = self.caught("    return seq[:SCAN_CAP]\n", "SCAN_CAP = 500")
        self.assertTrue(any("SCAN_CAP" in what for _ln, what in bad), bad)

    def test_SHAPE_B_a_slice_bounded_by_a_bare_literal(self):
        bad = self.caught("    return seq[:500]\n")
        self.assertTrue(any("upper bound" in what for _ln, what in bad), bad)

    def test_SHAPE_C_a_counter_guarded_break_with_a_LITERAL(self):
        """The shape the module CARRIED — round two found this exact form
        living in the tier-two entry count — and the shape the old walker was
        blindest to: no constant is named, so nothing about the name tells
        you a reading just stopped."""
        bad = self.caught("    n = 0\n    for item in seq:\n        n += 1\n"
                          "        if n >= 500:\n            break\n"
                          "    return n\n")
        self.assertTrue(any("counted break" in what for _ln, what in bad), bad)

    def test_SHAPE_D_a_counter_guarded_break_through_a_LOCAL(self):
        bad = self.caught("    limit = 500\n"
                          "    for i, item in enumerate(seq):\n"
                          "        if i >= limit:\n            break\n"
                          "    return i\n")
        self.assertTrue(any("counted break" in what for _ln, what in bad), bad)

    def test_SHAPE_E_a_counter_guarded_break_through_an_OFF_PATTERN_name(self):
        """A bound named `SCAN_LIMIT` bounds exactly as hard as one named
        `SCAN_CAP`. The name pattern is a convention for reading the module,
        never the enforcement."""
        bad = self.caught("    for i, item in enumerate(seq):\n"
                          "        if i >= SCAN_LIMIT:\n            break\n"
                          "    return i\n", "SCAN_LIMIT = 500")
        self.assertTrue(any("SCAN_LIMIT" in what for _ln, what in bad), bad)

    def test_SHAPE_F_a_range_length(self):
        bad = self.caught("    out = []\n    for i in range(500):\n"
                          "        out.append(seq[i])\n    return out\n")
        self.assertTrue(any("range()" in what for _ln, what in bad), bad)

    def test_SHAPE_G_an_islice_length(self):
        bad = self.caught("    import itertools\n"
                          "    return list(itertools.islice(seq, 500))\n")
        self.assertTrue(any("islice()" in what for _ln, what in bad), bad)

    def test_SHAPE_H_a_sized_read_through_a_LOCAL(self):
        """`fh.read(n)` where n is a local is the same silent truncation as
        `fh.read(4096)`; only the spelling moved."""
        bad = self.caught("    n = 4096\n    return fh.read(n)\n")
        self.assertTrue(any("sized .read()" == what for _ln, what in bad), bad)

    def test_SHAPE_I_a_slice_bounded_by_an_OFF_PATTERN_name(self):
        bad = self.caught("    return seq[:SCAN_LIMIT2]\n",
                          "SCAN_LIMIT2 = 500")
        self.assertTrue(any("upper bound" in what for _ln, what in bad), bad)

    def test_SHAPE_J_a_depth_guarded_return_in_a_bounded_descent(self):
        """A hop bound is a bound: a descent that stops at a depth has read a
        SLICE of the tree, and returning from it hands the caller an answer
        about the whole one. This module already carries the same shape as
        `OPAQUE_HOPS`, routed."""
        bad = self.caught("    if depth > 20:\n        return []\n"
                          "    return _planted_violation(seq, fh, depth + 1)\n")
        self.assertTrue(any("counted break" in what for _ln, what in bad), bad)

    def test_SHAPE_K_a_min_CLAMP_feeding_a_loop_guard(self):
        """The clamp is the shape that looks safest: `min(len(seq), 500)`
        reads like `however many there are`, and it is a cap whenever the
        second argument is the smaller one."""
        bad = self.caught("    n = min(len(seq), 500)\n"
                          "    for i, item in enumerate(seq):\n"
                          "        if i >= n:\n            break\n"
                          "    return i\n")
        self.assertTrue(any("counted break" in what for _ln, what in bad), bad)

    def test_a_WHILE_HEAD_carrying_the_bound_is_caught_from_the_other_side(self):
        """The same bound, inverted: a loop that RUNS while the count is under
        the size stops the reading exactly where a guarded break would."""
        bad = self.caught("    n = 0\n    while n < 500:\n        n += 1\n"
                          "    return n\n")
        self.assertTrue(any("loop bounded" in what for _ln, what in bad), bad)

    def test_the_PRIMITIVES_OWN_bound_read_from_OUTSIDE_it_is_caught(self):
        """The other admitted home is the class body, and the module's zero
        violations is that home's negative control — `items[:self.cap]`,
        `blob[:self.cap]` and `fh.read(self.cap + 1)` all live there. This is
        its positive twin: the same three shapes, one indent level out of the
        class, reaching for the primitive's own bound."""
        bad = self.caught("    r = seq\n    return r.cap and fh.read(r.cap + 1)\n")
        self.assertTrue(any("Reading.cap read outside Reading" == what
                            for _ln, what in bad), bad)
        self.assertTrue(any("sized .read()" == what for _ln, what in bad), bad)

    # ── and the shapes that are NOT bounds ────────────────────────────────
    def test_a_SENTINEL_comparison_is_not_a_bound(self):  # noqa: VACUOUS_ASSERTION — the eighteen planted-violation arms in this class are this one's positive control: the same walker over the same module reports a violation when the shape really is a bound
        """THE WALKER MUST TELL A COUNT FROM A PID. `if cur <= 1: break` ends
        the parent walk at init and is not a bound at all; a rule that flagged
        every literal in a loop guard would refuse this module's real ancestry
        walk and be turned off within a round."""
        self.assertEqual(self.caught(
            "    cur = seq[0]\n    while True:\n"
            "        if cur <= 1:\n            break\n"
            "        cur = fh(cur)\n    return cur\n"), [])

    def test_a_COUNT_DERIVED_length_is_not_a_bound(self):  # noqa: VACUOUS_ASSERTION — test_SHAPE_F plants range(500) through the same walker and asserts it IS caught; only the count-derived length is exempt
        """`range(len(seq))` reads every item there is. A walker that could
        not say so would push authors to spell their bounds in ways it cannot
        see, which is worse than no walker."""
        self.assertEqual(self.caught(
            "    n = len(seq)\n    for i in range(n):\n"
            "        fh.write(seq[i])\n    return n\n"), [])

    def test_a_bound_HANDED_to_the_primitive_is_not_a_violation(self):  # noqa: VACUOUS_ASSERTION — the planted-violation tests are this one's positive control: the same walker over the same module reports violations when the bound is not routed
        """The negative control: the routing itself must not trip the walker,
        or the only way to pass would be to stop bounding anything."""
        bad = self.violations(self.planted(
            "    r = Reading('x', cap=PROC_BUDGET,\n"
            "                budget=Reading.pool(COUNT_BUDGET))\n"
            "    return r.head(seq)\n"))
        self.assertEqual(bad, [])


class ReadingTest(unittest.TestCase):
    """The primitive itself: the only thing it may hand back is a value and
    whether the reading finished."""

    def test_head_says_when_it_truncated_and_when_it_did_not(self):  # noqa: VACUOUS_ASSERTION — both halves assert a concrete list AND its completeness, and they differ in both
        short, complete = scratch.Reading("x", cap=5).head([1, 2, 3])
        self.assertEqual((short, complete), ([1, 2, 3], True))
        cut, complete = scratch.Reading("x", cap=2).head([1, 2, 3])
        self.assertEqual((cut, complete), ([1, 2], False))

    def test_read_asks_for_one_byte_more_than_it_keeps(self):  # noqa: VACUOUS_ASSERTION — the whole-file half asserts (10, True) against a file of ten real bytes before the truncating half asserts (9, False)
        """The whole difference between a bounded reading and a silent
        conclusion: `fh.read(cap)` returns exactly cap bytes whether the file
        ended there or not."""
        tmp = tempfile.mkdtemp(prefix="helm-test-reading-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "blob")
        with open(path, "wb") as f:
            f.write(b"x" * 10)
        with open(path, "rb") as f:
            blob, complete = scratch.Reading("x", cap=10).read(f)
        self.assertEqual((len(blob), complete), (10, True))
        with open(path, "rb") as f:
            blob, complete = scratch.Reading("x", cap=9).read(f)
        self.assertEqual((len(blob), complete), (9, False))

    def test_a_stop_that_came_from_an_INNER_reading_renders_ONE_clause(self):
        """A refusal an owner reads. Handing an inner reading's whole REFUSAL
        SENTENCE to an outer `stop` nested one inside the other: `the worktree
        registration scan reading stopped at the git pointer file reading
        stopped at its 0-byte read cap, so ...`. The bound travels instead of
        the prose, so the sentence names the reading that actually hit a bound
        and keeps the subject the caller asked about."""
        inner = scratch.Reading("git pointer file", cap=0)
        tmp = tempfile.mkdtemp(prefix="helm-test-reading-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "pointer")
        with open(path, "wb") as f:
            f.write(b"gitdir: somewhere")
        with open(path, "rb") as f:
            inner.read(f)
        outer = scratch.Reading("worktree registration scan", cap=9)
        outer.stop_from(inner)
        why = outer.why("whether a git worktree is registered inside it")
        self.assertEqual(why.count("reading stopped at"), 1)
        self.assertEqual(why, "the git pointer file reading stopped at its "
                              "0-byte read cap, so whether a git worktree is "
                              "registered inside it is UNKNOWN")
        self.assertFalse(outer.complete)
        # CONTROL: a reading stopped by its OWN bound still names ITSELF.
        own = scratch.Reading("worktree registration scan", cap=0)
        self.assertFalse(own.take())
        self.assertEqual(own.why("whether a git worktree is registered "
                                 "inside it"),
                         "the worktree registration scan reading stopped at "
                         "its 0-entry cap, so whether a git worktree is "
                         "registered inside it is UNKNOWN")

    def test_ask_requests_one_more_than_it_keeps_so_a_FULL_answer_shows(self):
        """`ask` is the bound for a reader whose own API takes a count, and a
        reader handed exactly `cap` cannot tell a whole answer from a slice of
        one. Returning `cap` instead survived the suite: it makes `no applied
        pass in the journal` and `no applied pass in the rows I read` the same
        sentence."""
        self.assertEqual(scratch.Reading("x", cap=7).ask(), 8)
        self.assertIsNone(scratch.Reading("x").ask())
        rows = []

        def journal(n):
            rows.append(n)                  # an honest reader: EXACTLY n rows
            return [{"verb": "chat", "target": "x"} for _ in range(n)]

        with mock.patch.object(scratch, "EVENT_SCAN_CAP", 5), \
             mock.patch.object(scratch.pk, "read_events", side_effect=journal):
            why = scratch._last_gc()
        self.assertEqual(rows, [6])
        self.assertIsNotNone(why)
        self.assertIn("journal scan reading stopped", why)
        self.assertIn("when this reaper last applied a pass is UNKNOWN", why)
        # CONTROL: a journal SHORTER than the bound answers `no pass at all`,
        # which is a different fact and must stay a different answer.
        with mock.patch.object(scratch, "EVENT_SCAN_CAP", 5), \
             mock.patch.object(scratch.pk, "read_events",
                               side_effect=lambda n: [{"verb": "chat"}] * 2):
            self.assertIsNone(scratch._last_gc())

    def test_a_shared_pool_is_spent_by_every_reading_that_holds_it(self):
        pool = scratch.Reading.pool(3)
        first = scratch.Reading("x", cap=100, budget=pool)
        second = scratch.Reading("y", cap=100, budget=pool)
        self.assertTrue(first.take(2))
        self.assertTrue(second.take(1))
        self.assertFalse(second.take(1))
        self.assertFalse(second.complete)
        self.assertIn("shared", second.why("z"))

    def test_a_deadline_narrows_the_per_read_timeout(self):
        r = scratch.Reading("x", per_call=10, until=time.time() + 0.5)
        self.assertLessEqual(r.timeout(), 0.5)
        self.assertEqual(scratch.Reading("x", per_call=10).timeout(), 10)

    def test_why_is_None_while_the_reading_is_whole(self):
        r = scratch.Reading("x", cap=2)
        self.assertIsNone(r.why("z"))
        r.head([1, 2, 3])
        self.assertIn("UNKNOWN", r.why("z"))
        self.assertIn("x", r.why("z"))


class ScratchRoutingTripwireTest(unittest.TestCase):
    """THE MEASURED DEFECT: the suite leaked its scratch onto the build
    nodes' tmpfs /tmp by the tens of thousands (one node: 46,369 owner-uid
    top-level entries older than two hours; its sibling: 100% of nr_inodes
    and three receipts poisoned with Errno 28). tests/__init__.py cures
    it at the door — every mint in a test process lands under one per-process
    root its atexit hook removes. THIS class is the arm on that door.

    EFFECT, NOT COMPLAINT. The parent mints an empty directory that stands in
    for a node's /tmp, hands it to a spawned CANONICAL runner as TMPDIR, has a
    reporter arm inside that child mint through every tempfile door and write
    where the mints landed, and then asserts the stand-in is EMPTY after the
    child exits. A control seeds one mint OUTSIDE the routed root and proves
    the same census sees it — so the empty-directory assertion is a
    measurement and not a vacuous listdir of a directory nothing touched."""

    _REPORT_VAR = "SUITE_SCRATCH_REPORT"   # NOT HELM_-prefixed: the parent
    _LEAK_VAR = "SUITE_SCRATCH_LEAK_AT"    # strips that namespace below
    _REPORTER = "test_REPORTER_mints_scratch_where_the_process_puts_it"

    def test_REPORTER_mints_scratch_where_the_process_puts_it(self):  # noqa: VACUOUS_ASSERTION — a reporter, not an assertion; it is the instrument the arms below read
        """Runs INSIDE the spawned runner. Mints through every tempfile door a
        fixture uses, seeds one leak outside the root when asked, and writes
        what it saw. Under the ordinary suite (no report var) it is a no-op."""
        path = os.environ.get(self._REPORT_VAR)
        if not path:
            return
        paths = [tempfile.mkdtemp(prefix="helm-test-routed-")]
        fd, name = tempfile.mkstemp(prefix="helm-test-routed-")
        os.close(fd)
        paths.append(name)
        with tempfile.NamedTemporaryFile(prefix="helm-test-routed-",
                                         delete=False) as fh:
            paths.append(fh.name)
        paths.append(tempfile.TemporaryDirectory(
            prefix="helm-test-routed-").name)  # collected — the leaky shape
        # THE RELEASE SHAPE: gateauthority extracts 0o555 dirs / 0o444 files,
        # and a plain ignore_errors rmtree keeps them. Planted here so the
        # empty-directory assertion covers the reap's hardest case.
        readonly = os.path.join(paths[0], "releases", "deadbeef")
        os.makedirs(os.path.join(readonly, "helm"))
        with open(os.path.join(readonly, "helm", "x.py"), "w") as f:
            f.write("")
        os.chmod(os.path.join(readonly, "helm", "x.py"), 0o444)
        os.chmod(os.path.join(readonly, "helm"), 0o555)
        os.chmod(readonly, 0o555)
        leak_at = os.environ.get(self._LEAK_VAR)
        seeded = tempfile.mkdtemp(prefix="seeded-leak-", dir=leak_at) \
            if leak_at else None
        import json
        with open(path, "w") as f:
            json.dump({"TMPDIR": os.environ.get("TMPDIR"),
                       "gettempdir": tempfile.gettempdir(),
                       "paths": paths, "seeded": seeded}, f)

    def _canonical_run(self, ambient, seed_leak=False):
        """The literal gate argv, with `ambient` standing in for the node's
        /tmp, narrowed to this module's reporter. Returns what it reported."""
        import json, subprocess
        root = os.path.dirname(TESTS)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HELM_", "MELD_"))}
        env["TMPDIR"] = ambient
        report = os.path.join(self.report_dir, "report.json")
        env[self._REPORT_VAR] = report
        if seed_leak:
            env[self._LEAK_VAR] = ambient
        argv = [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                "-t", ".", "-p", os.path.basename(__file__),
                "-k", self._REPORTER]
        out = subprocess.run(argv, capture_output=True, text=True, cwd=root,
                             env=env, timeout=300)
        self.assertEqual(out.returncode, 0,
                         "the spawned canonical runner exited %s under %r:\n%s"
                         % (out.returncode, argv, out.stderr[-800:]))
        self.assertTrue(os.path.exists(report),
                        "the reporter arm never ran under %r:\n%s"
                        % (argv, out.stderr[-800:]))
        with open(report) as f:
            return json.load(f)

    def setUp(self):
        # Two directories of our own, OUTSIDE the stand-in, so the report
        # file and the stand-in cannot be confused for each other's contents.
        self.report_dir = tempfile.mkdtemp(prefix="helm-test-scratch-report-")
        self.ambient = tempfile.mkdtemp(prefix="helm-test-scratch-ambient-")
        self.addCleanup(shutil.rmtree, self.report_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.ambient, ignore_errors=True)

    def test_every_mint_in_a_canonical_run_dies_with_the_process(self):
        got = self._canonical_run(self.ambient)
        # THE ROUTE: the child's tempfile and its TMPDIR agree, and both sit
        # in a helm-suite-env root nested under the stand-in — the operator's
        # mount, helm's reaping.
        self.assertEqual(got["gettempdir"], got["TMPDIR"])
        self.assertTrue(got["TMPDIR"].startswith(self.ambient + os.sep),
                        got["TMPDIR"])
        self.assertIn("helm-suite-env-", got["TMPDIR"])
        self.assertEqual(len(got["paths"]), 4, got)
        for path in got["paths"]:
            self.assertTrue(path.startswith(got["TMPDIR"] + os.sep), path)
        # THE EFFECT: nothing the child minted outlives it. Not "no warning
        # was printed" — the stand-in for /tmp holds zero entries.
        self.assertEqual(os.listdir(self.ambient), [],
                         "the canonical run left scratch on the ambient tmp")

    def test_the_census_sees_a_seeded_leak(self):  # noqa: VACUOUS_ASSERTION — the exact-listing equality on the seeded entry is the positive control; the routed mints' absence is the same run's other half
        """POSITIVE CONTROL. The same run, with one mint deliberately placed
        outside the routed root, leaves exactly that entry behind — so the
        empty-directory assertion above is watching a directory the child
        really can dirty."""
        got = self._canonical_run(self.ambient, seed_leak=True)
        self.assertIsNotNone(got["seeded"])
        self.assertFalse(got["seeded"].startswith(got["TMPDIR"] + os.sep),
                         got["seeded"])
        left = os.listdir(self.ambient)
        self.assertEqual(left, [os.path.basename(got["seeded"])], left)
        # …and the routed mints from the same run are still gone.
        for path in got["paths"]:
            self.assertFalse(os.path.exists(path), path)

    def test_no_test_module_unroutes_the_process(self):  # noqa: VACUOUS_ASSERTION — the assertRegex on tests/__init__.py proves the pattern matches the one writer it should
        """`tempfile.tempdir = ...` in any test module rebinds the WHOLE
        process's tempfile for every module that runs after it, which is the
        one line that silently reopens the leak for the rest of the suite.
        Source-driven, like the reaper tripwire above: the routing door
        (tests/__init__.py) is the only file allowed to write it."""
        offenders = []
        for fn in sorted(os.listdir(TESTS)):
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            with open(os.path.join(TESTS, fn), encoding="utf-8") as f:
                text = f.read()
            if re.search(r"^\s*tempfile\.tempdir\s*=", text, re.M):
                offenders.append(fn)
        self.assertFalse(
            offenders,
            "test module(s) %r assign tempfile.tempdir — that unroutes every "
            "later module's scratch off the per-process root tests/__init__.py "
            "reaps. Use a fixture directory and `dir=` instead." % offenders)
        # the pin is not vacuous: the door itself does write it
        with open(os.path.join(TESTS, "__init__.py"), encoding="utf-8") as f:
            self.assertRegex(f.read(),
                             re.compile(r"^\s*tempfile\.tempdir\s*=", re.M))



MB = 1024 * 1024
AGENTS = "user.slice/user-1000.slice/user@1000.service/agents.slice"


class SeatPressurePlaneTest(unittest.TestCase):
    """THE THIRD PLANE. A seat froze in its OWN memory cgroup — its slice over
    memory.high, a process in the kernel's over-high throttle — while the host
    had a third of its memory free, because three full clones its verifier
    subagents left in its tmpfs scratchpad were shared memory charged to it.
    The host and mount planes both read calm, and the automatic pass rides a
    Stop hook the frozen seat never reaches. So the pass reads every seat's
    own slice, from ANY seat, and a pressing seat's big unheld scratch reaps at
    ttl 0, biggest first, under every predicate the tier already has.

    Fixture /proc, fixture cgroup tree, fixture scratch trees, fixture census.
    Nothing here reads or writes a live cgroup, a live seat or live scratch."""

    LIVE = "dddddddd-5555-6666-7777-888888888888"    # the pressured seat
    OTHER = "eeeeeeee-5555-6666-7777-888888888888"   # the seat running the pass
    FLOOR = 64 * 1024
    ENV = ("HELM_CACHE_DIR", "HELM_HOME", "HELM_CHAT_DIR",
           "HELM_SCRATCH_GC_TIER2", "HELM_SCRATCH_GC_SEATS", "HELM_CHAT_NAME",
           "CLAUDE_CODE_SESSION_ID", "HELM_INTEGRATOR_SEAT")

    unit = SecondTierTest.unit
    age = SecondTierTest.age
    fresh = SecondTierTest.fresh
    paths = SecondTierTest.paths
    kept = SecondTierTest.kept
    HOST_WARN = SecondTierTest.HOST_WARN

    def setUp(self):
        self.tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="helm-test-scratch-seats-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "scratch")
        self.proc = os.path.join(self.tmp, "proc")
        self.cg = os.path.join(self.tmp, "cgroup")
        os.makedirs(self.proc)
        self.prior = {k: os.environ.get(k) for k in self.ENV}
        self.addCleanup(self.restore)
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ["HELM_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        # THE PASS RUNS AS ANOTHER SEAT: nothing in it may need the pressured
        # seat's own identity, because that seat is the one that cannot run.
        os.environ["HELM_CHAT_NAME"] = "seat-b"
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.OTHER
        floor = mock.patch.object(scratch, "PRESSURE_UNIT_FLOOR", self.FLOOR)
        floor.start()
        self.addCleanup(floor.stop)
        self.specs = [(os.path.join(self.root, "*"), "test scratch")]
        self.pad = os.path.join(self.root, self.LIVE, "scratchpad")
        self.other_pad = os.path.join(self.root, self.OTHER, "scratchpad")
        os.makedirs(self.pad)
        os.makedirs(self.other_pad)
        self.live_proc("999", self.LIVE)
        self.live_proc("777", self.OTHER)
        self.slice("seat-under-test", "999", 10 * MB, 100 * MB)
        self.slice("seat-b", "777", 10 * MB, 100 * MB)

    def restore(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    live_proc = SecondTierTest.live_proc

    def slice(self, seat, pid, current, high, count=0, stalled=False,
              shmem=None):
        """One seat slice, and the pid it holds naming it in its cgroup line.
        `shmem` defaults to ALL of `current`: the tmpfs-clone shape."""
        from helm import seatceiling
        rel = "%s/%s" % (AGENTS, seatceiling.seat_slice_name(seat))
        d = os.path.join(self.cg, rel)
        os.makedirs(os.path.join(d, "run-p%s-i1.scope" % pid), exist_ok=True)
        shared = current if shmem is None else shmem
        for name, text in (("memory.current", "%d\n" % current),
                           ("memory.high", "%d\n" % high),
                           ("memory.stat", "anon 4096\nfile %d\nshmem %d\n"
                                           % (max(0, current - shared),
                                              shared)),
                           ("memory.events", "low 0\nhigh %d\nmax 0\noom 0\n"
                                             "oom_kill 0\n" % count)):
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        pd = os.path.join(self.proc, pid)
        with open(os.path.join(pd, "cgroup"), "w") as f:
            f.write("0::/%s/run-p%s-i1.scope\n" % (rel, pid))
        with open(os.path.join(pd, "stat"), "w") as f:
            f.write("%s (claude) %s 1 0 0 0 0\n" % (pid, "D" if stalled
                                                     else "S"))
        with open(os.path.join(pd, "wchan"), "w") as f:
            f.write("__mem_cgroup_handle_over_high" if stalled else "0")
        return d

    def census(self):
        return {"listing_failed": False, "rows": [
            {"pid": 999, "session": self.LIVE,
             "environ": {"HELM_CHAT_NAME": "seat-under-test"}},
            {"pid": 777, "session": self.OTHER,
             "environ": {"HELM_CHAT_NAME": "seat-b"}}]}

    def plane(self, during=None):
        return scratch.seat_plane(proc_dir=self.proc, root=self.cg,
                                  census=self.census(),
                                  sleep=lambda _s: during and during())

    def rows(self):
        return [{"bytes_pct": 36, "inodes_pct": 36, "mount": "/tmp",
                 "dev": os.stat(self.root).st_dev, "inodes_total": 1048576,
                 "volatile": True}]

    def gc(self, apply=True, plane=None, host=HOST_OK):
        return scratch.gc(apply=apply, specs=self.specs, proc_dir=self.proc,
                          rows=self.rows(), host=host, seats=plane)

    def blob(self, name, sizes, where=None, age_h=0.05):
        """A unit of REAL bytes — the reaper ranks by allocated blocks, so a
        sparse file would be a fixture measuring nothing. Three minutes old by
        default: far inside every host ttl, so only ttl 0 can take it."""
        d = os.path.join(where or self.pad, name)
        os.makedirs(d)
        for i, n in enumerate(sizes):
            with open(os.path.join(d, "f%d" % i), "wb") as f:
                f.write(os.urandom(n))
        self.age(d, age_h)
        return d

    # ── the cure ──────────────────────────────────────────────────────────
    def test_a_seat_at_95pct_reaps_at_ttl_0_biggest_first_from_another_seats_pass(self):  # noqa: VACUOUS_ASSERTION — the empty tier2 is the BEFORE half of a before/after pair on the same trees; the after half asserts [big, mid] out of the same gc() helper, unconditionally
        big = self.blob("clone-big", [400 * 1024])       # 1 file, most bytes
        mid = self.blob("clone-mid", [16 * 1024] * 12)   # 12 files, fewer bytes
        receipt = os.path.join(self.pad, "receipt.txt")
        self.fresh(receipt)
        self.age(receipt, 0.05)
        theirs = self.blob("their-clone", [400 * 1024], where=self.other_pad)
        # CONTROL: the same trees with no seat reading. Host calm, units young:
        # the tier takes nothing, so every pick below is the plane's doing.
        self.assertEqual(self.gc(apply=False)["tier2"], [])
        self.slice("seat-under-test", "999", 95 * MB, 100 * MB)
        plane = self.plane()
        self.assertEqual(set(plane["sessions"]), {self.LIVE},
                         "only the pressing seat's session escalates")
        self.assertEqual(os.environ["HELM_CHAT_NAME"], "seat-b")
        rep = self.gc(plane=plane)
        self.assertEqual(self.paths(rep), [big, mid],
                         "not biggest-first by BYTES — an entry count ranks "
                         "the 12-file unit above the 400K one")
        for v in rep["tier2"]:
            self.assertEqual(v["ttl"], 0)
            self.assertTrue(v["pressure"].endswith("agents-seat_under_test.slice"))
        self.assertEqual(rep["tier2_reaped"], 2)
        self.assertFalse(os.path.exists(big))
        self.assertFalse(os.path.exists(mid))
        self.assertTrue(os.path.exists(receipt), "a unit under the floor "
                                                 "relieves nothing and stays")
        self.assertIn("floor", " ".join(self.kept(rep, receipt)))
        self.assertTrue(os.path.isdir(theirs), "a calm seat's young scratch "
                                               "was taken")
        self.assertIn("SEAT memory pressure", scratch.summary(rep))

    def test_biggest_first_stops_once_the_slice_is_projected_relieved(self):
        """Biggest first is half the rule; stopping is the other half. At 95%
        of a 1M ceiling the 400K unit alone projects the slice under the clear
        line, so the smaller one is the agent's to keep."""
        big = self.blob("clone-big", [400 * 1024])
        mid = self.blob("clone-mid", [16 * 1024] * 12)
        self.slice("seat-under-test", "999", int(0.95 * MB), MB)
        rep = self.gc(plane=self.plane())
        self.assertEqual(self.paths(rep), [big])
        self.assertTrue(os.path.isdir(mid))
        self.assertIn("already projected", " ".join(self.kept(rep, mid)))

    def test_a_held_or_live_cwd_unit_is_kept_under_pressure(self):
        """EVERY predicate the tier already has still holds at ttl 0."""
        held = self.blob("held", [200 * 1024])
        occupied = self.blob("occupied", [200 * 1024])
        free = self.blob("free", [200 * 1024])
        self.live_proc("998", fd=os.path.join(held, "f0"))
        self.live_proc("997", cwd=occupied)
        self.slice("seat-under-test", "999", 95 * MB, 100 * MB)
        rep = self.gc(plane=self.plane())
        self.assertEqual(self.paths(rep), [free])
        self.assertFalse(os.path.exists(free))
        self.assertTrue(os.path.isdir(held))
        self.assertTrue(os.path.isdir(occupied))
        self.assertIn("holds", self.kept(rep, held)[0])
        self.assertIn("cwd'd inside it", self.kept(rep, occupied)[0])

    def test_a_seat_at_50pct_keeps_the_host_pressure_ttl(self):
        young = self.blob("young-clone", [400 * 1024])
        aged = self.blob("aged-clone", [400 * 1024], age_h=8)
        self.slice("seat-under-test", "999", 50 * MB, 100 * MB)
        plane = self.plane()
        self.assertEqual(sorted(round(r.ratio, 2) for r in
                                plane["slices"].values()), [0.1, 0.5],
                         "control: the plane READ both slices")
        self.assertEqual(plane["sessions"], {})
        rep = self.gc(plane=plane, host=self.HOST_WARN)
        self.assertEqual(self.paths(rep), [aged],
                         "control: the tier ran, at the host's warn ttl")
        self.assertEqual(rep["tier2"][0]["ttl"],
                         scratch.TIER2_TTL_BY_LEVEL["warn"])
        self.assertNotIn("pressure", rep["tier2"][0])
        self.assertTrue(os.path.isdir(young))

    def test_a_still_counter_escalates_nothing_and_a_rising_one_does(self):
        """HISTORY IS NOT NOW, end to end: at 85% the rise is sampled, and a
        lifetime counter that does not move across the window leaves the
        seat's scratch alone. The same fixture with the counter moving INSIDE
        the window is a throttle happening now, and the unit goes."""
        clone = self.blob("clone", [400 * 1024])
        d = self.slice("seat-under-test", "999", 85 * MB, 100 * MB,
                       count=1531339)
        still = self.plane()
        self.assertEqual(sorted(r.rise for r in still["slices"].values()
                                if r.rise is not None), [0],
                         "control: the 85% slice WAS sampled, and did not move")
        self.assertEqual(still["sessions"], {})
        calm = self.gc(plane=still)
        self.assertIn(os.path.join(self.root, self.LIVE),
                      calm["tier2_trees"], "control: the tier opened the tree")
        self.assertEqual(calm["tier2"], [])
        self.assertTrue(os.path.isdir(clone))

        def tick():
            with open(os.path.join(d, "memory.events"), "w") as f:
                f.write("low 0\nhigh 1531400\nmax 0\noom 0\noom_kill 0\n")
        rising = self.plane(during=tick)
        self.assertEqual(set(rising["sessions"]), {self.LIVE})
        self.assertEqual(self.paths(self.gc(plane=rising)), [clone])

    def test_any_seats_stop_pass_reaps_it_inside_the_hourly_throttle(self):
        """THE THROTTLED SEAT NEVER REACHES ITS OWN STOP HOOK. The automatic
        leg of any other seat reads every slice, and a pressing one overrides
        the hourly throttle that would otherwise keep that pass asleep."""
        clone = self.blob("clone", [400 * 1024])
        real = scratch.seat_plane
        scratch._touch_stamp()                        # the hourly pass: NOT due

        def run():
            with mock.patch.object(scratch, "PROC", self.proc), \
                 mock.patch.object(scratch, "scratch_specs",
                                   return_value=self.specs), \
                 mock.patch.object(scratch, "survey",
                                   return_value=self.rows()), \
                 mock.patch.object(scratch, "host_memory",
                                   return_value=HOST_OK), \
                 mock.patch.object(scratch, "seat_plane", side_effect=lambda: real(
                     proc_dir=self.proc, root=self.cg, census=self.census(),
                     sleep=lambda _s: None)), \
                 mock.patch.object(scratch, "spells",
                                   return_value=[]) as woke:
                return scratch.auto_gc(), woke.called

        calm, bookkept = run()
        self.assertIsNone(calm)
        self.assertTrue(bookkept, "a calm read still keeps the spell book")
        self.assertTrue(os.path.isdir(clone))
        os.remove(scratch._seat_stamp_path())
        self.slice("seat-under-test", "999", 95 * MB, 100 * MB)
        line, _bookkept = run()
        self.assertIn("SEAT memory pressure", line or "")
        self.assertFalse(os.path.exists(clone))

    def test_a_page_cache_slice_is_neither_pressing_nor_woken(self):
        """A seat that sits at 94% of memory.high in reclaimable page cache is
        not in danger, and it sits there routinely: reaping its scratch or
        paging its lead and the integrator about it is noise. Its reading is
        HIGH — the quiet badge only. The positive control, on the same trees
        and the same wake door: its memory held as shmem pages at once."""
        clone = self.blob("clone", [400 * 1024])
        self.slice("seat-under-test", "999", 94 * MB, 100 * MB,
                   shmem=2 * MB)
        plane = self.plane()
        from helm import seatceiling
        self.assertEqual(len(plane["slices"]), 2, "control: both slices read")
        words = sorted(r.word or "-" for r in plane["slices"].values())
        self.assertEqual(words, ["-", seatceiling.HIGH],
                         "control: the plane READ the cache slice as HIGH")
        self.assertEqual(plane["sessions"], {}, "a page-cache slice escalated "
                                                "its seat's scratch")
        rep = self.gc(plane=plane)
        self.assertIn(os.path.join(self.root, self.LIVE), rep["tier2_trees"],
                      "control: the tier opened the tree")
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.isdir(clone))
        posts = []

        def post(text, room):
            posts.append(text)
            return {"id": "row-%d" % len(posts)}
        scratch.spells(plane, rep, post=post, roster=self.roster())
        self.assertEqual(posts, [], "a page-cache slice woke its lead and the "
                                    "integrator")
        self.slice("seat-under-test", "999", 94 * MB, 100 * MB,
                   shmem=90 * MB)
        scratch.spells(self.plane(), None, post=post, roster=self.roster())
        self.assertEqual(len(posts), 1, "control: the same slice held as "
                                        "shmem did not wake")

    def test_the_incident_shape_is_pressing_and_reaped(self):
        """The frozen seat, scaled from gigabytes to megabytes: 13.35 of a
        12.88 ceiling, 11.3 of it shmem — three clones in a tmpfs
        scratchpad. No stalled process and no counter rise in this fixture,
        so the shmem rule alone must make it pressing."""
        clone = self.blob("clone", [400 * 1024])
        self.assertTrue(os.path.isdir(clone), "control: the unit exists")
        self.slice("seat-under-test", "999", int(13.35 * MB),
                   int(12.88 * MB), shmem=int(11.3 * MB))
        plane = self.plane()
        from helm import seatceiling
        (p,) = [r for r in plane["slices"].values() if r.pids == (999,)]
        self.assertEqual((p.word, p.stalled, p.rise),
                         (seatceiling.NEAR, (), 0))
        self.assertEqual(p.shmem, int(11.3 * MB))
        self.assertTrue(seatceiling.pressing(p))
        self.assertEqual(set(plane["sessions"]), {self.LIVE})
        rep = self.gc(plane=plane)
        self.assertEqual(self.paths(rep), [clone])
        self.assertEqual(rep["tier2"][0]["ttl"], 0)
        self.assertFalse(os.path.exists(clone))

    def test_a_census_that_fails_escalates_nothing_and_still_wakes(self):
        """A pressing slice whose sessions cannot be named is still a known
        slice: its scratch is not escalated — no session, no tree — and its
        spell still wakes the people who can act."""
        clone = self.blob("clone", [400 * 1024])
        self.slice("seat-under-test", "999", 95 * MB, 100 * MB)
        plane = scratch.seat_plane(proc_dir=self.proc, root=self.cg,
                                   census={"listing_failed": True, "rows": []},
                                   sleep=lambda _s: None)
        self.assertIsNone(plane["trouble"])
        self.assertIn("census failed", plane["unmapped"])
        self.assertEqual(plane["sessions"], {})
        rep = self.gc(plane=plane)
        self.assertIn(os.path.join(self.root, self.LIVE), rep["tier2_trees"],
                      "control: the tier opened the tree")
        self.assertEqual(rep["tier2"], [])
        self.assertTrue(os.path.isdir(clone))
        posts = []
        scratch.spells(plane, rep, roster=self.roster(),
                       post=lambda t, r: posts.append(t) or {"id": "x"})
        self.assertEqual(len(posts), 1, "a known pressing slice woke nobody")
        self.assertIn("NEAR now", posts[0])

    # ── the wake, once per spell ─────────────────────────────────────────
    ROSTER = {"seat-under-test": {"home_room": "helm", "cwd": "/x/helm-wt/lane"},
              "seat-a": {"home_room": "helm", "cwd": "/x/helm"},
              "seat-integrator": {"home_room": "helm", "cwd": "/x/helm"}}

    def roster(self):
        now = time.time()
        return {k: dict(v, last_seen=now) for k, v in self.ROSTER.items()}

    def test_the_wake_fires_once_per_spell(self):
        posts = []

        def post(text, room):
            posts.append((room, text))
            return {"id": "row-%d" % len(posts)}

        def book(current, stalled=False):
            self.slice("seat-under-test", "999", current, 100 * MB,
                       stalled=stalled)
            plane = self.plane()
            return scratch.spells(plane, self.gc(plane=plane), post=post,
                                  roster=self.roster())

        clone = self.blob("clone-big", [400 * 1024])
        self.assertTrue(os.path.isdir(clone), "control: the unit exists")
        woke = book(104 * MB, stalled=True)
        self.assertEqual(len(posts), 1, "the spell's opening woke nobody")
        room, text = posts[0]
        self.assertEqual(room, "helm")
        self.assertEqual(woke[0][2], ["seat-a", "seat-integrator"])
        for want in ("@seat-under-test", "THROTTLED now",
                     "agents-seat_under_test.slice", "104%", "clone-big",
                     "reaped", "@seat-a", "@seat-integrator"):
            self.assertIn(want, text)
        self.assertFalse(os.path.exists(clone))
        book(104 * MB, stalled=True)
        self.assertEqual(len(posts), 1, "a second pass inside one spell woke "
                                        "again")
        book(85 * MB)
        self.assertEqual(len(posts), 1, "a dip to 85% is the same spell")
        book(50 * MB)
        self.assertEqual(len(posts), 1)
        book(95 * MB)
        self.assertEqual(len(posts), 2, "a NEW spell after a clear woke "
                                        "nobody")
        self.assertIn("NEAR now", posts[1][1])

    def test_an_undelivered_wake_does_not_latch(self):
        self.slice("seat-under-test", "999", 95 * MB, 100 * MB)
        plane = self.plane()
        self.assertEqual(scratch.spells(plane, None, post=lambda t, r: None,
                                        roster=self.roster()), [])
        posts = []
        got = scratch.spells(plane, None, roster=self.roster(),
                             post=lambda t, r: posts.append(t) or {"id": "x"})
        self.assertEqual(len(posts), 1, "the failed wake was latched anyway")
        self.assertEqual(len(got), 1)


class CloneSteerTest(unittest.TestCase):
    """PREVENT AT SOURCE. The frozen seat's scratch held full clones of a
    local repository — the whole object store copied into tmpfs, charged to
    the seat. The argv guard's pass path says what that copy costs and what
    shares the objects instead; it never blocks, and it stays silent on every
    clone that is not that shape."""

    def setUp(self):
        self.tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="helm-test-clonesteer-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        self.ram = os.path.join(self.tmp, "ram")
        self.disk = os.path.join(self.tmp, "disk")
        for d in (self.repo, self.ram, self.disk):
            os.makedirs(d)
        mounts = os.path.join(self.tmp, "mounts")
        with open(mounts, "w") as f:
            f.write("/dev/sda1 / ext4 rw,relatime 0 0\n"
                    "tmpfs %s tmpfs rw,nosuid,nodev 0 0\n" % self.ram)
        table = mock.patch.object(scratch, "MOUNTS", mounts)
        table.start()
        self.addCleanup(table.stop)

    def ids(self, command, cwd=None):
        from helm import chat
        return [sid for sid, _t in chat.argv_steers(command, cwd)]

    def test_a_local_clone_into_tmpfs_is_steered_and_nothing_else_is(self):  # noqa: VACUOUS_ASSERTION — every silence is one half of a discrimination: the five fire controls above run first, unconditionally, through the same ids() helper and the same planted mount table
        fire = ["local-clone-into-ram"]
        self.assertEqual(self.ids("git clone %s %s/copy"
                                  % (self.repo, self.ram)), fire)
        self.assertEqual(self.ids("git clone file://%s %s/copy"
                                  % (self.repo, self.ram)), fire)
        self.assertEqual(self.ids("cd %s && git clone %s copy"
                                  % (self.ram, self.repo)), fire)
        self.assertEqual(self.ids("git clone %s copy" % self.repo,
                                  cwd=self.ram), fire)
        self.assertEqual(self.ids("git -C %s clone -q %s copy"
                                  % (self.ram, self.repo)), fire)
        for quiet in ("git clone https://github.com/a/b %s/copy" % self.ram,
                      "git clone git@github.com:a/b.git %s/copy" % self.ram,
                      "git clone %s %s/copy" % (self.repo, self.disk),
                      "git clone --shared %s %s/copy" % (self.repo, self.ram),
                      "git clone -s %s %s/copy" % (self.repo, self.ram),
                      "git clone --depth 1 file://%s %s/copy"
                      % (self.repo, self.ram),
                      "git clone %s/missing %s/copy" % (self.tmp, self.ram),
                      "git clone %s copy" % self.repo):
            with self.subTest(command=quiet):
                self.assertEqual(self.ids(quiet), [])

    def test_the_steer_rides_the_pass_path_and_never_blocks(self):
        from helm import chat
        self.assertIn("local-clone-into-ram", self.ids(
            "git clone %s %s/copy" % (self.repo, self.ram)))
        self.assertFalse(chat.argv_guard(
            "git clone %s %s/copy" % (self.repo, self.ram)),
            "the guard REFUSED a clone; this rung is advice, never a gate")
        text = dict(chat.argv_steers(
            "git clone %s %s/copy" % (self.repo, self.ram)))[
            "local-clone-into-ram"]
        self.assertIn("--shared", text)
        self.assertIn("worktree add --detach", text)


if __name__ == "__main__":
    unittest.main()
