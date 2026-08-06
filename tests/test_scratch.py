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
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import scratch  # noqa: E402

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
        self.assertEqual(len(out), 1)
        level, msg = out[0]
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
        self.assertEqual([l for l, _ in out], [scratch.OK])
        self.assertIn("bytes 31%, inodes 42%", out[0][1])

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

    def test_a_healthy_ambient_tmp_keeps_tmpdir_fast(self):
        with self.usages(Stat(files=11501004, ffree=11000000, favail=11000000,
                              bfree=950, bavail=950), Stat(**self.ROOMY)):
            self.assertTrue(scratch.launch_tmpdir().startswith(
                tempfile.gettempdir()))


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

    def test_the_probe_is_bounded(self):
        for pid in range(100, 140):
            self.add(pid)
        with mock.patch.object(scratch, "PROC_CAP", 5):
            live, _c, _t = scratch.live_evidence(self.proc)
        self.assertEqual(len(live["pids"]), 5)


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
    def test_the_pass_is_bounded(self):
        for i in range(12):
            self.tree("bbbbbbbb-1111-2222-3333-%012d" % i)
        with mock.patch.object(scratch, "CANDIDATE_CAP", 5):
            self.assertEqual(self.gc()["candidates"], 5)
        with mock.patch.object(scratch, "AGED_CAP", 3):
            rep = self.gc()
        self.assertEqual(len(rep["victims"]), 3)
        self.assertTrue(rep["capped"])
        with mock.patch.object(scratch, "REAP_CAP", 2):
            self.assertEqual(len(self.gc()["victims"]), 2)

    def test_count_entries_is_capped(self):
        self.tree(self.DEAD, files=30)
        with mock.patch.object(scratch, "COUNT_CAP", 10):
            n, capped, _prot = scratch.count_entries(
                os.path.join(self.root, self.DEAD))
        self.assertEqual((n, capped), (10, True))

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

    def test_auto_gc_never_raises(self):
        with mock.patch.object(scratch, "survey",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(scratch.auto_gc())

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


if __name__ == "__main__":
    unittest.main()
