#!/usr/bin/env python3
"""helm slice-limits and its doctor rung (task/3847).

agents.slice's limits were literals typed into drop-ins by hand, and when
three of them disagreed, systemd's merge order let a stopgap
whose task had closed win at every reload. These arms build a whole estate
in a temp dir each time: a /sys CPU list, a /proc/meminfo, a user manager's
cgroup tree with agents.slice's live limit files, and the three unit
directories a user's drop-ins live in. Nothing here reads the host's /sys,
/proc, cgroup tree or unit directories, and no host systemctl runs: every
systemctl is a fake under the suite root.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._tmphome import fake_user_systemd
from tests._tmphome import home as _tmp_home

_tmp_home(prefix="helm-test-slicelimits-", var="HELM_HOME")

from helm import doctor, seatceiling, seatlimits, slicelimits  # noqa: E402

GIB = 1024 ** 3
MANAGER = "user.slice/user-1000.slice/user@1000.service"
#: A measured fleet box's MemTotal, in kB: 87.7 GiB of a 96 GB laptop.
BOX_KB = 92007992
OWNER_HEADROOM = (
    "# 800% = fleet shares 8 cores, owner keeps 16 guaranteed. Stays until\n"
    "# task/1574 (hook-storm cure) lands and is measured.\n"
    "[Slice]\nCPUQuota=800%\n")


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Estate(object):
    """A box in a temp dir: `cpus` online CPUs, `kb` of MemTotal, a live
    agents.slice, and the three unit directories, empty until a test adds a
    unit file."""

    def __init__(self, case, cpus="0-23", kb=96 * 1024 * 1024,
                 live=("800000 100000", "25", "%d" % (48 * GIB),
                       "%d" % (56 * GIB), "0")):
        self.case = case
        self.base = tempfile.mkdtemp(prefix="helm-test-slicelimits-")
        case.addCleanup(shutil.rmtree, self.base, True)
        j = os.path.join
        _w(j(self.base, "sys", "devices", "system", "cpu", "online"),
           cpus + "\n")
        _w(j(self.base, "proc", "meminfo"),
           "MemTotal:       %d kB\nMemFree: 1 kB\n" % kb)
        _w(j(self.base, "proc", "self", "cgroup"),
           "0::/%s/app.slice/helm-x.service\n" % MANAGER)
        self.fleet = j(self.base, "cg", MANAGER, "agents.slice")
        for name, text in zip(("cpu.max", "cpu.weight", "memory.high",
                               "memory.max", "memory.swap.max"), live):
            _w(j(self.fleet, name), text + "\n")
        self.units = j(self.base, "home", ".config", "systemd", "user")
        self.control = j(self.base, "home", ".config", "systemd",
                         "user.control")
        self.runtime = j(self.base, "run", "systemd", "user.control")
        self.box = slicelimits.box(
            sys_root=j(self.base, "sys"), proc=j(self.base, "proc"),
            cgroup=j(self.base, "cg"), units=self.units, runtime=self.runtime)

    def unit(self, where, name, text):
        path = os.path.join(where, name)
        _w(path, text)
        return path

    def dropin(self, where, name, text):
        return self.unit(os.path.join(where, "agents.slice.d"), name, text)

    def this_box(self):
        """The measured box: three disagreeing drop-ins and a set-property
        file, under the base unit of task/325."""
        self.unit(self.units, "agents.slice",
                  "# task/325: THE CEILING.\n[Unit]\nDescription=fleet\n"
                  "[Slice]\nCPUQuota=1400%\nCPUWeight=20\nMemoryHigh=32G\n"
                  "MemoryMax=44G\nMemorySwapMax=8G\nTasksMax=8192\n")
        self.dropin(self.units, "sacred-sizing.conf",
                    "# online=24: fleet keeps 18 threads.\n[Slice]\n"
                    "CPUQuota=1800%\nCPUWeight=25\nMemoryHigh=40G\n"
                    "MemorySwapMax=0\n")
        self.dropin(self.units, "zz-oomd-headroom.conf",
                    "[Slice]\nMemoryHigh=48G\nMemoryMax=56G\n")
        self.dropin(self.units, "zz-owner-headroom.conf", OWNER_HEADROOM)
        self.dropin(self.control, "50-CPUQuota.conf",
                    "# created via systemctl set-property\n[Slice]\n"
                    "CPUQuota=1600.00%\n")

    def tree(self):
        """{path relative to the home and run dirs: bytes} of every file."""
        out = {}
        for top in ("home", "run"):
            for d, _dirs, files in os.walk(os.path.join(self.base, top)):
                for f in files:
                    p = os.path.join(d, f)
                    with open(p, "rb") as fh:
                        out[os.path.relpath(p, self.base)] = fh.read()
        return out

    def fake_systemctl(self, live):
        """A systemctl under the suite root, first on PATH for the rest of
        the case, whose daemon-reload writes `live` ({cgroup file: text})
        into the fleet slice, the way the user manager applies what the
        files say. -> its log path."""
        d = tempfile.mkdtemp(prefix="helm-test-slicelimits-bin-")
        self.case.addCleanup(shutil.rmtree, d, True)
        self_log = os.path.join(d, "calls")
        path = os.path.join(d, "systemctl")
        lines = ["#!/bin/sh", "echo \"$*\" >> '%s'" % self_log]
        lines += ["printf '%%s\\n' '%s' > '%s'" % (text, os.path.join(
            self.fleet, name)) for name, text in live.items()]
        _w(path, "\n".join(lines) + "\n")
        os.chmod(path, 0o755)
        env = mock.patch.dict(os.environ, {
            "PATH": d + os.pathsep + os.environ.get("PATH", os.defpath)})
        env.start()
        self.case.addCleanup(env.stop)
        return self_log


class Case(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(seatceiling.FLEET_ENV, None)
        self.status = mock.Mock(side_effect=lambda ns: {n: "closed"
                                                        for n in ns})

    def run_verb(self, estate, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = slicelimits.cmd_slice_limits(list(args), b=estate.box,
                                              status=self.status)
        return rc, out.getvalue()


class DerivationTest(Case):

    def test_24_cpus_and_96g_derive_1600_percent_and_the_memory_lines(self):
        e = Estate(self)
        facts, why = slicelimits.read_box(e.box)
        self.assertEqual(why, "")
        self.assertEqual((facts["cpus"], facts["mem"]), (24, 96 * GIB))
        got = {k: v for k, (v, _why) in
               slicelimits.derive(24, 96 * GIB).items()}
        # 96 GiB: the kill line keeps 36% (34.56G), the throttle line 45%
        # (43.2G); each fleet limit rounds DOWN to whole GiB.
        self.assertEqual(got, {"CPUQuota": 1600, "MemoryHigh": 52 * GIB,
                               "MemoryMax": 61 * GIB, "CPUWeight": 25,
                               "MemorySwapMax": 0})
        why = slicelimits.derive(24, 96 * GIB)["CPUQuota"][1]
        self.assertIn("(24 online CPUs - 8 owner reserve) x 100%", why)

    def test_the_real_box_reproduces_the_limits_measured_right_there(self):
        """87.7 GiB of MemTotal: 1600% (task/3847's measured value), and the
        48G/56G of the oomd sizing, byte for byte."""
        got = slicelimits.derive(24, BOX_KB * 1024)
        self.assertEqual([got[k][0] for k in ("CPUQuota", "MemoryHigh",
                                              "MemoryMax")],
                         [1600, 48 * GIB, 56 * GIB])

    def test_online_cpus_are_the_cpulist_never_the_affinity(self):
        """nproc read 8 on a 24-CPU box. The count comes from the kernel's
        online list, whatever this process's affinity is."""
        e = Estate(self, cpus="0-3,8-11,16")
        with mock.patch.object(os, "sched_getaffinity", return_value={0}):
            facts, _ = slicelimits.read_box(e.box)
        self.assertEqual(facts["cpus"], 9)
        self.assertIn("reads 0-3,8-11,16", facts["cpus_from"])
        for bad in ("", "x", "0-", "3,a", "-2"):
            self.assertIsNone(slicelimits.cpulist(bad), bad)

    def test_the_reserve_floor_holds_on_a_small_box(self):
        """8 CPUs: a third is 2.67, under the 4-CPU floor, so the owner keeps
        4 and the fleet gets 400%. 2 CPUs: the fleet never gets less than one
        CPU. 16 GiB: both memory floors bind."""
        self.assertEqual(slicelimits.derive(8, 64 * GIB)["CPUQuota"][0], 400)
        self.assertIn("8 online CPUs - 4 owner reserve",
                      slicelimits.derive(8, 64 * GIB)["CPUQuota"][1])
        self.assertEqual(slicelimits.derive(2, 64 * GIB)["CPUQuota"][0], 100)
        # the positive control: above the floor the fraction decides
        self.assertEqual(slicelimits.derive(15, 64 * GIB)["CPUQuota"][0],
                         1000)
        small = slicelimits.derive(4, 16 * GIB)
        self.assertEqual((small["MemoryMax"][0], small["MemoryHigh"][0]),
                         (8 * GIB, 4 * GIB))

    def test_the_switch_keeps_a_reading_of_this_host_off(self):
        """The suite's HELM_SEAT_PRESSURE=off is the box's own switch: a
        reading of the real /sys and /proc answers 'not read' and touches
        neither."""
        units = tempfile.mkdtemp(prefix="helm-test-slicelimits-units-")
        self.addCleanup(shutil.rmtree, units, True)
        b = slicelimits.box(units=units)
        self.assertEqual((b.sys, b.proc, b.cgroup),
                         ("/sys", "/proc", "/sys/fs/cgroup"))
        with mock.patch.dict(os.environ, {"HELM_SEAT_PRESSURE": "off"}), \
                mock.patch.object(slicelimits, "_text") as text:
            self.assertIn("not read", slicelimits.read_box(b)[1])
            self.assertEqual(slicelimits.read_live(b)[0], {})
        text.assert_not_called()

    def test_unit_values_parse_the_way_systemd_writes_them(self):
        p = slicelimits.parse_value
        self.assertEqual(p("CPUQuota", "1600.00%"), 1600.0)
        self.assertEqual(p("CPUQuota", ""), slicelimits.INF)
        self.assertEqual(p("MemoryHigh", "48G"), 48 * GIB)
        self.assertEqual(p("MemoryHigh", "512M"), 512 * 1024 ** 2)
        self.assertEqual(p("MemoryMax", "infinity"), slicelimits.INF)
        self.assertEqual(p("MemoryMax", "50%", mem=10 * GIB), 5 * GIB)
        self.assertEqual(p("MemoryHigh", "64 G"), 64 * GIB)
        self.assertEqual(p("MemoryMax", "1P"), 1024 ** 5)
        self.assertEqual(p("CPUWeight", "25"), 25)
        self.assertIsNone(p("CPUQuota", "lots"))
        self.assertEqual(slicelimits.render("MemoryHigh", 48 * GIB), "48G")
        self.assertEqual(slicelimits.render("CPUQuota", 1600), "1600%")


class DropinTest(Case):

    def test_the_merge_follows_file_names_across_directories(self):
        """The measured box: 50-CPUQuota.conf in user.control sorts FIRST,
        so zz-owner-headroom.conf's 800% is what every reload loads."""
        e = Estate(self)
        e.this_box()
        frag, dropins = slicelimits.unit_files(e.box)
        eff = slicelimits.effective(frag, dropins)
        self.assertEqual(eff["CPUQuota"][0], 800.0)
        self.assertTrue(eff["CPUQuota"][2].endswith("zz-owner-headroom.conf"))
        self.assertEqual(eff["MemoryMax"][1], "56G")
        self.assertEqual(eff["MemorySwapMax"][1], "0")

    def test_apply_neutralizes_every_other_source_with_backups(self):
        e = Estate(self)
        e.this_box()
        mixed = e.dropin(e.units, "io.conf",
                         "[Slice]\nIOWeight=50\nCPUQuota=900%\n")
        # a same-named file in a higher-priority directory HIDES the one in
        # the user directory; moving only the visible one would expose it
        hider = e.dropin(e.runtime, "sacred-sizing.conf",
                         "[Slice]\nCPUWeight=30\n")
        before = e.tree()
        log = e.fake_systemctl({
            "cpu.max": "1600000 100000", "memory.high": "%d" % (52 * GIB),
            "memory.max": "%d" % (61 * GIB)})
        r = slicelimits.reading(e.box, status=self.status)
        self.assertEqual(len(slicelimits.others(r)), 6)
        rc, lines, back = slicelimits.apply(r, stamp="T1")
        self.assertEqual(rc, 0, lines)
        with open(log, encoding="utf-8") as fh:
            self.assertEqual(fh.read().split("\n")[0], "--user daemon-reload")
        # every other source is gone as a *.conf, and each backup carries its
        # original bytes
        moved = [p for p in before if p.endswith(".conf")]
        self.assertEqual(len(moved), 6)
        after = e.tree()
        for rel in moved:
            self.assertEqual(after[rel + slicelimits.MOVED + "T1"],
                             before[rel], rel)
            self.assertEqual(rel in after, rel.endswith("io.conf"), rel)
        with open(mixed, encoding="utf-8") as fh:
            kept = fh.read()
        self.assertIn("IOWeight=50", kept)
        self.assertIn("# moved to %s by helm slice-limits: CPUQuota=900%%"
                      % slicelimits.DROPIN, kept)
        self.assertFalse(os.path.exists(hider))
        # the files now load every limit from helm's one drop-in
        frag, dropins = slicelimits.unit_files(e.box)
        eff = slicelimits.effective(frag, dropins)
        ours = slicelimits.ours(e.box)
        self.assertEqual({k: (v[1], v[2]) for k, v in eff.items()},
                         {"CPUQuota": ("1600%", ours),
                          "MemoryHigh": ("52G", ours),
                          "MemoryMax": ("61G", ours),
                          "CPUWeight": ("25", ours),
                          "MemorySwapMax": ("0", ours)})
        self.assertIn("daemon-reloaded; live agents.slice reads CPUQuota "
                      "1600%", "\n".join(lines))
        # the printed rollback puts the estate back byte for byte
        self.assertEqual(back[-1], "systemctl --user daemon-reload")
        for line in back[:-1]:
            subprocess.run(["sh", "-c", line], check=True, timeout=10)
        self.assertEqual(e.tree(), before)
        # and the doctor, run on the applied estate, is quiet
        slicelimits.apply(slicelimits.reading(e.box, status=self.status),
                          stamp="T2")
        rows = slicelimits.doctor_rows(e.box, status=self.status)
        self.assertEqual([lvl for lvl, _ in rows], ["ok"], rows)
        self.assertIn("from helm's one drop-in", rows[0][1])

    def test_an_unchanged_apply_writes_nothing_and_does_not_reload(self):
        e = Estate(self, live=("1600000 100000", "25", "%d" % (52 * GIB),
                               "%d" % (61 * GIB), "0"))
        e.unit(e.units, "agents.slice", "[Slice]\nTasksMax=8192\n")
        fake = fake_user_systemd(self, home=False)
        r = slicelimits.reading(e.box, status=self.status)
        rc, _lines, first = slicelimits.apply(r, stamp="T1")
        # the positive control: the first apply wrote, reloaded and has a
        # rollback, on the same observables the second one leaves empty
        self.assertEqual((rc, first[0]), (0, "rm -f %s" % slicelimits.ours(
            e.box)))
        self.assertEqual(fake.calls(), [["--user", "daemon-reload"]])
        before = e.tree()
        rc, lines, back = slicelimits.apply(
            slicelimits.reading(e.box, status=self.status), stamp="T2")
        self.assertEqual((rc, back), (0, []))
        self.assertIn("nothing written", lines[0])
        self.assertEqual(e.tree(), before)
        self.assertEqual(fake.calls(), [["--user", "daemon-reload"]])

    def test_retry_after_failed_reload_actually_reloads_unchanged_files(self):
        e = Estate(self)
        e.unit(e.units, "agents.slice", "[Slice]\nTasksMax=8192\n")
        fake = e.fake_systemctl({
            "cpu.max": "1600000 100000", "cpu.weight": "25",
            "memory.high": "%d" % (52 * GIB),
            "memory.max": "%d" % (61 * GIB), "memory.swap.max": "0"})
        with mock.patch.object(slicelimits.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 1, stderr="gone")):
            rc, _lines, _back = slicelimits.apply(
                slicelimits.reading(e.box, status=self.status), stamp="T1")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(fake))
        rc, _lines, _back = slicelimits.apply(
            slicelimits.reading(e.box, status=self.status), stamp="T2")
        self.assertEqual(rc, 0)
        with open(fake, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(), ["--user daemon-reload"])
        rc, lines, _back = slicelimits.apply(
            slicelimits.reading(e.box, status=self.status), stamp="T3")
        self.assertEqual(rc, 0, lines)
        self.assertIn("nothing written", lines[0])
        with open(fake, encoding="utf-8") as fh:
            self.assertEqual(fh.read().splitlines(), ["--user daemon-reload"])

    def test_apply_sets_nothing_lower_than_live_unless_told(self):
        """Live CPUQuota 2000% and MemoryMax unlimited are above the
        derivation: kept, and named in the drop-in; --lower writes it."""
        e = Estate(self, live=("2000000 100000", "25", "%d" % (48 * GIB),
                               "max", "0"))
        e.unit(e.units, "agents.slice", "[Slice]\nCPUQuota=2000%\n")
        fake_user_systemd(self, home=False)
        r = slicelimits.reading(e.box, status=self.status)
        p = slicelimits.plan(r)
        self.assertEqual((p["CPUQuota"][0], p["MemoryMax"][0]),
                         (2000.0, slicelimits.INF))
        self.assertEqual(p["MemoryHigh"], (52 * GIB, "raise"))
        slicelimits.apply(r, stamp="T1")
        with open(slicelimits.ours(e.box), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("\nCPUQuota=2000%\n", text)
        self.assertIn("\nMemoryMax=infinity\n", text)
        self.assertIn("# CPUQuota: kept: CPUQuota is 2000% now, above the "
                      "derived 1600%", text)
        lowered = slicelimits.plan(r, lower=True)
        self.assertEqual((lowered["CPUQuota"][0], lowered["MemoryMax"][0]),
                         (1600, 61 * GIB))
        rc, out = self.run_verb(e, "--lower")
        self.assertEqual(rc, 2)

    def test_a_stop_mid_apply_leaves_every_limit_loading_from_helm(self):
        """helm's drop-in is written BEFORE any other one moves. A move that
        fails (the same state a kill between two moves leaves) must not leave
        the files loading the base unit's older literals, whose MemoryHigh
        32G is the throttle line systemd-oomd killed seats at."""
        e = Estate(self)
        e.this_box()
        fake = fake_user_systemd(self, home=False)
        before = e.tree()
        r = slicelimits.reading(e.box, status=self.status)
        real, seen = os.rename, []

        def rename(a, b, *rest, **kw):
            seen.append(a)
            if len(seen) == 2:
                raise OSError(28, "No space left on device")
            return real(a, b, *rest, **kw)
        with mock.patch.object(slicelimits.os, "rename", rename):
            rc, lines, back = slicelimits.apply(r, stamp="T1")
        self.assertEqual((rc, len(seen)), (2, 2), lines)
        self.assertEqual(fake.calls(), [])
        frag, dropins = slicelimits.unit_files(e.box)
        ours = slicelimits.ours(e.box)
        self.assertEqual({k: v[2] for k, v in slicelimits.effective(
            frag, dropins).items()}, {k: ours for k in slicelimits.MANAGED})
        # and the printed rollback still puts the estate back byte for byte
        for line in back[:-1]:
            subprocess.run(["sh", "-c", line], check=True, timeout=10)
        self.assertEqual(e.tree(), before)

    def test_rollback_after_failed_write_restores_same_inode_backup(self):
        e = Estate(self)
        mixed = e.dropin(e.units, "io.conf",
                         "[Slice]\nIOWeight=50\nCPUQuota=900%\n")
        before = e.tree()
        rel = os.path.relpath(mixed, e.base)
        self.assertIn(b"CPUQuota=900%", before[rel])
        e.fake_systemctl({})
        r = slicelimits.reading(e.box, status=self.status)
        real = slicelimits.pk.atomic_write

        def write(path, text, mode=None):
            if path == mixed:
                raise OSError(28, "No space left on device")
            return real(path, text, mode)
        with mock.patch.object(slicelimits.pk, "atomic_write", write):
            rc, _lines, back = slicelimits.apply(r, stamp="T1")
        self.assertEqual(rc, 2)
        for line in back[:-1]:
            subprocess.run(["sh", "-c", line], check=True, timeout=10)
        self.assertEqual(e.tree(), before)

    def test_a_drop_in_with_other_settings_is_never_absent(self):
        """Its backup is a hard link and its copy without the limit lines
        replaces it in one rename: a write that fails leaves the file whole,
        where a rename first left IOWeight out of the next reload."""
        e = Estate(self)
        mixed = e.dropin(e.units, "io.conf",
                         "[Slice]\nIOWeight=50\nCPUQuota=900%\n")
        with open(mixed, "rb") as fh:
            original = fh.read()
        fake_user_systemd(self, home=False)
        r = slicelimits.reading(e.box, status=self.status)
        real = slicelimits.pk.atomic_write

        def write(path, text, mode=None):
            if path == mixed:
                raise OSError(28, "No space left on device")
            return real(path, text, mode)
        with mock.patch.object(slicelimits.pk, "atomic_write", write):
            rc, lines, _back = slicelimits.apply(r, stamp="T1")
        self.assertEqual(rc, 2, lines)
        with open(mixed, "rb") as fh:
            self.assertEqual(fh.read(), original)
        # the positive control: unhindered, the same file keeps IOWeight and
        # loses only the limit line
        slicelimits.apply(slicelimits.reading(e.box, status=self.status),
                          stamp="T2")
        with open(mixed, encoding="utf-8") as fh:
            kept = fh.read()
        self.assertIn("\nIOWeight=50\n", kept)
        self.assertNotIn("\nCPUQuota=900%", kept)

    def test_unknown_existing_limit_refuses_without_lower(self):
        e = Estate(self)
        shutil.rmtree(e.fleet)
        unit = e.unit(e.units, "agents.slice", "[Slice]\nMemoryHigh=invalid\n")
        before = e.tree()
        self.assertIn(b"MemoryHigh=invalid", before[os.path.relpath(unit, e.base)])
        r = slicelimits.reading(e.box, status=self.status)
        self.assertIsNone(slicelimits.now_of(r, "MemoryHigh")[0])
        rc, lines, _back = slicelimits.apply(r, stamp="T1")
        self.assertEqual(rc, 2)
        self.assertIn("UNKNOWN (MemoryHigh)", "; ".join(lines))
        self.assertEqual(e.tree(), before)
        self.assertEqual(slicelimits.plan(r, lower=True)["MemoryHigh"][0],
                         52 * GIB)

    def test_a_later_named_limit_refuses_before_mutating_any_file(self):
        # noqa: VACUOUS_ASSERTION — each fixed subtest asserts populated file bytes
        for masked in (False, True):
            with self.subTest(masked=masked):
                e = Estate(self)
                e.unit(e.units, "agents.slice", "[Slice]\nTasksMax=8192\n")
                e.dropin(e.units, "zzzz-owner-limit.conf",
                         "[Slice]\nCPUQuota=800%\n")
                if masked:
                    e.dropin(e.control, "zzzz-owner-limit.conf",
                             "[Slice]\nCPUQuota=800%\n")
                before = e.tree()
                self.assertIn(b"CPUQuota=800%", before[os.path.relpath(
                    os.path.join(e.units, "agents.slice.d",
                                 "zzzz-owner-limit.conf"), e.base)])
                rc, lines, _back = slicelimits.apply(
                    slicelimits.reading(e.box, status=self.status), stamp="T1")
                self.assertEqual(rc, 2)
                self.assertIn("sorts before", "; ".join(lines))
                self.assertEqual(e.tree(), before)

    def test_masked_later_named_limit_can_be_neutralized(self):
        e = Estate(self)
        e.unit(e.units, "agents.slice", "[Slice]\nTasksMax=8192\n")
        hidden = e.dropin(e.units, "zzzz-owner-limit.conf",
                          "[Slice]\nCPUQuota=800%\n")
        hider = e.dropin(e.control, "zzzz-owner-limit.conf",
                           "[Slice]\nIOWeight=50\n")
        e.fake_systemctl({
            "cpu.max": "1600000 100000", "cpu.weight": "25",
            "memory.high": "%d" % (52 * GIB),
            "memory.max": "%d" % (61 * GIB), "memory.swap.max": "0"})
        before = e.tree()
        self.assertIn(b"CPUQuota=800%", before[os.path.relpath(hidden, e.base)])
        rc, lines, _back = slicelimits.apply(
            slicelimits.reading(e.box, status=self.status), stamp="T1")
        self.assertEqual(rc, 0, lines)
        with open(hider, encoding="utf-8") as fh:
            self.assertIn("IOWeight=50", fh.read())
        self.assertFalse(os.path.exists(hidden))
        self.assertEqual(slicelimits.effective(*slicelimits.unit_files(e.box))[
            "CPUQuota"][2], slicelimits.ours(e.box))

    def test_higher_priority_helm_name_refuses_before_writing(self):
        e = Estate(self)
        e.unit(e.units, "agents.slice", "[Slice]\nMemoryHigh=32G\n")
        e.dropin(e.units, "zz-oomd-headroom.conf",
                 "[Slice]\nMemoryHigh=48G\n")
        hider = e.dropin(e.control, slicelimits.DROPIN,
                           "[Slice]\nIOWeight=50\n")
        before = e.tree()
        self.assertIn(b"IOWeight=50", before[os.path.relpath(hider, e.base)])
        rc, lines, _back = slicelimits.apply(
            slicelimits.reading(e.box, status=self.status), stamp="T1")
        self.assertEqual(rc, 2)
        self.assertIn("cannot take effect first", "; ".join(lines))
        self.assertEqual(e.tree(), before)

    def test_unreadable_dropin_directory_is_not_treated_as_empty(self):
        e = Estate(self)
        e.unit(e.units, "agents.slice", "[Slice]\nTasksMax=8192\n")
        directory = os.path.join(e.control, "agents.slice.d")
        real = os.listdir

        def unreadable(path):
            if path == directory:
                raise PermissionError(13, "Permission denied", path)
            return real(path)
        before = e.tree()
        self.assertIn(b"TasksMax=8192", before[os.path.relpath(
            os.path.join(e.units, "agents.slice"), e.base)])
        with mock.patch.object(slicelimits.os, "listdir", unreadable):
            r = slicelimits.reading(e.box, status=self.status)
            rc, lines, _back = slicelimits.apply(r, stamp="T1")
            rows = slicelimits.doctor_rows(e.box, status=self.status)
        self.assertEqual(rc, 2)
        self.assertIn(directory, "; ".join(lines))
        self.assertIn(directory, "\n".join(t for _, t in rows))
        self.assertIn("would not read", "\n".join(t.lower() for _, t in rows))
        self.assertEqual(e.tree(), before)

    def test_a_limit_no_file_sets_is_the_default_not_unknown(self):
        """No seat running, so no live cgroup to read, and a base unit that
        sets no limit: each limit is systemd's default (no quota, infinity,
        weight 100), which the plan keeps without --lower, exactly as it
        does when the live slice reads those defaults."""
        e = Estate(self)
        shutil.rmtree(e.fleet)
        e.unit(e.units, "agents.slice", "[Slice]\nTasksMax=8192\n")
        r = slicelimits.reading(e.box, status=self.status)
        self.assertIn("there is no agents.slice", r["live_why"])
        inf = slicelimits.INF
        self.assertEqual({k: v[0] for k, v in slicelimits.plan(r).items()},
                         {"CPUQuota": inf, "MemoryHigh": inf,
                          "MemoryMax": inf, "CPUWeight": 100,
                          "MemorySwapMax": inf})
        # the positive control: --lower writes the derivation over them
        self.assertEqual(slicelimits.plan(r, lower=True)["CPUQuota"][0], 1600)

    def test_a_dry_run_writes_nothing_and_runs_no_systemctl(self):
        e = Estate(self)
        e.this_box()
        fake = fake_user_systemd(self, home=False)
        before = e.tree()
        rc, out = self.run_verb(e)
        self.assertEqual(rc, 1)                  # 800% is not the derivation
        self.assertEqual(e.tree(), before)
        self.assertEqual(fake.calls(), [])
        self.assertIn("CPUQuota      1600%", out)
        self.assertIn("now 800% (live), files load 800% from "
                      "zz-owner-headroom.conf; --apply writes 1600% (raise)",
                      out)
        self.assertIn("50-CPUQuota.conf: CPUQuota=1600.00%", out)
        self.assertIn("dry run: nothing written", out)
        rc, out = self.run_verb(e, "--json")
        data = json.loads(out)
        self.assertEqual(data["derived"]["CPUQuota"]["value"], 1600)
        self.assertEqual(data["live"]["MemoryMax"], 56 * GIB)
        self.assertEqual(len(data["others"]), 4)
        self.assertEqual(e.tree(), before)
        self.assertEqual(fake.calls(), [])
        # the positive control on the same two observables: --apply moves
        # four drop-ins, writes helm's, and reloads once
        rc, out = self.run_verb(e, "--apply")
        self.assertIn("rollback:", out)
        self.assertEqual(fake.calls(), [["--user", "daemon-reload"]])
        self.assertEqual(len(set(e.tree()) - set(before)), 5)


class DoctorRungTest(Case):

    def test_the_rung_names_a_closed_until_stopgap_and_the_throttle(self):
        e = Estate(self)
        e.this_box()
        rows = slicelimits.doctor_rows(e.box, status=self.status)
        text = "\n".join(t for _, t in rows)
        self.assertEqual({lvl for lvl, _ in rows}, {"warn"})
        self.assertIn("zz-owner-headroom.conf is a stopgap 'until "
                      "task/1574', and task/1574 is closed", text)
        self.assertIn("CPUQuota 800% (live, from zz-owner-headroom.conf; "
                      "derived 1600%)", text)
        self.assertIn("4 drop-in(s) besides helm's", text)
        self.status.assert_called_once_with([1574])

    def test_an_open_until_task_is_not_named_and_an_unknown_one_is(self):
        e = Estate(self)
        e.dropin(e.units, "zz-owner-headroom.conf", OWNER_HEADROOM)
        rows = slicelimits.doctor_rows(e.box, status=lambda ns: {
            n: "open" for n in ns})
        self.assertNotIn("stopgap", "\n".join(t for _, t in rows))
        rows = slicelimits.doctor_rows(e.box, status=lambda ns: {
            n: None for n in ns})
        self.assertIn("task/1574 is UNKNOWN", "\n".join(t for _, t in rows))

    def test_a_live_value_the_files_would_undo_is_named(self):
        """meta's hand-set 1600% was live while the files loaded 800%: the
        next reload took it back."""
        e = Estate(self, live=("1600000 100000", "25", "%d" % (48 * GIB),
                               "%d" % (56 * GIB), "0"))
        e.this_box()
        text = "\n".join(t for _, t in slicelimits.doctor_rows(
            e.box, status=self.status))
        self.assertIn("live CPUQuota differs from what the unit files load "
                      "(CPUQuota live 1600%, files 800% from "
                      "zz-owner-headroom.conf)", text)

    def test_a_live_slice_without_a_local_unit_is_not_a_false_ok(self):
        e = Estate(self)
        rows = slicelimits.doctor_rows(e.box)
        self.assertEqual([lvl for lvl, _ in rows], ["warn"])
        self.assertIn("its limits are UNKNOWN", rows[0][1])

    def test_a_box_with_no_fleet_slice_is_one_ok_and_reads_nothing(self):
        e = Estate(self)
        shutil.rmtree(e.fleet)
        with mock.patch.object(slicelimits, "read_box") as read:
            rows = slicelimits.doctor_rows(e.box)
        read.assert_not_called()
        self.assertEqual([lvl for lvl, _ in rows], ["ok"])
        self.assertIn("no agents.slice unit", rows[0][1])

    def test_doctor_runs_the_rung_and_never_fails_on_it(self):
        self.assertIn("check_slice_limits", doctor.CHECKS)
        rows = doctor.check_slice_limits(read=lambda: [("warn", "a"),
                                                       ("ok", "b")])
        self.assertEqual(rows, [(doctor.WARN, "a"), (doctor.OK, "b")])

        def boom():
            raise OSError("gone")
        self.assertEqual(doctor.check_slice_limits(read=boom)[0][0],
                         doctor.WARN)


# ------------------------------------------------- the seat slices (task/4062)

#: Measured on the 87.7 GiB box: the integrator's unnamed pane held
#: 4.1G anon + 3.8G shmem; the largest worker 2.1G anon; the runaway python
#: in a local worker seat grew past 9 GB twice.
RUNAWAY = int(9.5 * GIB)
PER_SEAT_MEMORY = ("# PER-SEAT MEMORY, hand-written.\n[Slice]\nMemoryHigh=12G\n"
                   "MemoryMax=16G\n")


def seat(e, name, pid, high, top, anon, shmem=0, swap="0", lead=False):
    """A running seat slice under the estate's agents.slice: its cgroup
    files, and one process in /proc whose cgroup line names it."""
    j = os.path.join
    path = j(e.fleet, name)
    for f, text in (("memory.high", str(high)), ("memory.max", str(top)),
                    ("memory.swap.max", swap),
                    ("memory.current", str(anon + shmem + GIB)),
                    ("memory.peak", str(anon + shmem + 2 * GIB)),
                    ("memory.stat", "anon %d\nfile %d\nshmem %d\n"
                     % (anon, shmem + GIB, shmem)),
                    ("memory.events", "low 0\nhigh 0\nmax 0\noom 0\n"
                     "oom_kill 0\n")):
        _w(j(path, f), text + "\n")
    _w(j(e.base, "proc", str(pid), "cgroup"),
       "0::/%s/agents.slice/%s/run-p%d.scope\n" % (MANAGER, name, pid))
    _w(j(e.base, "proc", str(pid), "environ"),
       "HOME=/h\0" + ("HELM_SEAT_ROLE=lead\0" if lead else "")
       + "HELM_CHAT_NAME=x\0")
    return path


def reload_from_files(e):
    """A user manager's daemon-reload: every seat cgroup takes what its unit
    files load (seatlimits.files_for), the way systemd applies them."""
    def run(argv, **_kw):
        assert argv == ["systemctl", "--user", "daemon-reload"], argv
        for name in os.listdir(e.fleet):
            if not name.startswith("agents-"):
                continue
            got = seatlimits.files_for(e.box, name)
            for k, f in (("MemoryHigh", "memory.high"),
                         ("MemoryMax", "memory.max"),
                         ("MemorySwapMax", "memory.swap.max")):
                if k in got:
                    v = got[k][0]
                    _w(os.path.join(e.fleet, name, f), "%s\n" % (
                        "max" if v == slicelimits.INF else v))
        return subprocess.CompletedProcess(argv, 0, "", "")
    return mock.patch.object(seatlimits.subprocess, "run", side_effect=run)


def tonight(e):
    """The measured estate: the hand-written prefix drop-in, the shim's
    set-property files (a local seat's 10G/14G stopgap), an unnamed integrator
    pane, a lead and two workers."""
    e.unit(e.units, "agents.slice", "[Slice]\nTasksMax=8192\n")
    e.unit(os.path.join(e.units, "agents-.slice.d"), "per-seat-memory.conf",
           PER_SEAT_MEMORY)
    e.unit(os.path.join(e.units, "agents-.slice.d"), "per-seat-cpu.conf",
           "[Slice]\nCPUWeight=100\n")
    for name, hi, mx in (("seat_a", "10737418240", "15032385536"),
                         ("proj_claude", "12884901888", "17179869184")):
        d = os.path.join(e.control, "agents-%s.slice.d" % name)
        e.unit(d, "50-MemoryHigh.conf", "[Slice]\nMemoryHigh=%s\n" % hi)
        e.unit(d, "50-MemoryMax.conf", "[Slice]\nMemoryMax=%s\n" % mx)
    seat(e, "agents-seat_a.slice", 101, 10 * GIB, 14 * GIB,
         int(0.8 * GIB), int(0.3 * GIB))
    seat(e, "agents-seat_b_2.slice", 102, 12 * GIB, 16 * GIB,
         int(2.1 * GIB))
    seat(e, "agents-proj_claude.slice", 103, 12 * GIB, 16 * GIB,
         int(1.0 * GIB), int(0.5 * GIB))
    seat(e, "agents-pid4242.slice", 104, 12 * GIB, 16 * GIB,
         int(4.1 * GIB), int(3.8 * GIB))
    seat(e, "agents-seat_c.slice", 105, 12 * GIB, 16 * GIB,
         int(3.1 * GIB), lead=True)


class SeatDerivationTest(Case):

    def test_the_real_box_gives_leads_todays_pair_and_workers_half(self):
        fleet = slicelimits.derive(24, BOX_KB * 1024)["MemoryHigh"][0]
        got = {role: {k: v for k, (v, _w) in seatlimits.derive(
            fleet, role).items()} for role in seatlimits.ROLES}
        self.assertEqual(got["lead"], {"MemoryHigh": 12 * GIB,
                                       "MemoryMax": 16 * GIB,
                                       "MemorySwapMax": 0})
        self.assertEqual(got["worker"], {"MemoryHigh": 6 * GIB,
                                         "MemoryMax": 8 * GIB,
                                         "MemorySwapMax": 0})

    def test_every_box_derives_no_literal_and_keeps_the_order(self):  # noqa: VACUOUS_ASSERTION — both branches are counted after the loop: 3 classes with no High, 9 with one
        """On every box size: worker max < lead max <= the fleet's throttle
        line < MemTotal minus the fleet's kill reserve, a band below each
        max, and values that MOVE with the box (no number is typed in)."""
        seen, kinds = set(), []
        for gib in (16, 32, 64, 87.75, 128, 256):
            mem = int(gib * GIB)
            fleet = slicelimits.derive(24, mem)
            line = fleet["MemoryHigh"][0]
            w = seatlimits.derive(line, "worker")
            ld = seatlimits.derive(line, "lead")
            self.assertLess(w["MemoryMax"][0], ld["MemoryMax"][0], gib)
            self.assertLessEqual(ld["MemoryMax"][0], line, gib)
            self.assertLess(line, mem - slicelimits.reserve(
                "memory_max", mem), gib)
            for got in (w, ld):
                high, cap = got["MemoryHigh"][0], got["MemoryMax"][0]
                # a per-seat High is at least the floor, under the kill
                # line, or absent (infinity): never between
                kinds.append(high == slicelimits.INF)
                if high == slicelimits.INF:
                    self.assertLess(cap, seatlimits.HIGH_CAP_MIN, gib)
                    self.assertIn(seatlimits.NO_HIGH, got["MemoryHigh"][1])
                else:
                    self.assertGreaterEqual(high, seatlimits.HIGH_FLOOR, gib)
                    self.assertLess(high, cap, gib)
                self.assertEqual(got["MemorySwapMax"][0], 0)
            seen.add((w["MemoryMax"][0], ld["MemoryMax"][0]))
        self.assertEqual(len(seen), 6)
        # both arms ran: 3 of 12 classes (16G both, 32G worker) get no High
        self.assertEqual(kinds.count(True), 3)
        self.assertEqual(kinds.count(False), 9)

    def test_the_floor_is_one_gib_over_the_measured_freeze(self):  # noqa: VACUOUS_ASSERTION — three equalities on constants, no absence
        self.assertEqual(seatlimits.HIGH_FLOOR, 4 * GIB)
        self.assertEqual(seatlimits.HIGH_FLOOR, seatlimits.SEAT_FREEZE + GIB)
        self.assertEqual(seatlimits.HIGH_CAP_MIN, 5 * GIB)

    def classes_on(self, gib):
        line = slicelimits.derive(24, int(gib * GIB))["MemoryHigh"][0]
        return {role: seatlimits.derive(line, role)
                for role in seatlimits.ROLES}

    def test_a_16g_box_derives_no_per_seat_high(self):
        """Both caps are under 5G: no per-seat High, with the reason, and
        each MemoryMax stays at its cap."""
        got = self.classes_on(16)
        self.assertEqual(sorted(got), ["lead", "worker"])
        for role, cap in (("worker", 2 * GIB), ("lead", 4 * GIB)):
            self.assertEqual(got[role]["MemoryHigh"][0], slicelimits.INF, role)
            self.assertIn("throttle left to the fleet line: a per-seat High "
                          "under 4G freezes seats", got[role]["MemoryHigh"][1])
            self.assertEqual(got[role]["MemoryMax"][0], cap, role)

    def test_a_32g_box_derives_no_worker_high_and_the_floor_for_a_lead(self):
        """A worker's 2G cap holds no 4G High: none, with the reason. A
        lead's 5G cap holds one: its 3/4 band (3G) is raised to the floor."""
        got = self.classes_on(32)
        self.assertEqual(got["worker"]["MemoryHigh"][0], slicelimits.INF)
        self.assertIn(seatlimits.NO_HIGH, got["worker"]["MemoryHigh"][1])
        self.assertEqual(got["worker"]["MemoryMax"][0], 2 * GIB)
        self.assertEqual(got["lead"]["MemoryMax"][0], 5 * GIB)
        self.assertEqual(got["lead"]["MemoryHigh"][0], 4 * GIB)

    def test_the_role_rule(self):
        role = seatlimits.role_of
        self.assertEqual(role("agents-seat_a.slice"), "worker")
        self.assertEqual(role("agents-seat_b_2.slice"), "worker")
        self.assertEqual(role("agents-proj_claude.slice"), "lead")
        self.assertEqual(role("agents-proj_integrator.slice"), "lead")
        self.assertEqual(role("agents-pid4242.slice"), "lead")
        self.assertEqual(role("agents-seat_c.slice"), "worker")
        self.assertEqual(role("agents-seat_c.slice", marked=True), "lead")


def declaration(case, seat_name, role, home=None):
    """A native role.json declaration for REAL seat name `seat_name` (which
    may fold differently from its slice name) in a temp HELM_HOME, the way
    seat_role.declare_role writes it. When `home` is None the case gets its
    own throwaway home; pass a home to plant several declarations in one."""
    if home is None:
        home = tempfile.mkdtemp(prefix="helm-test-slicelimits-home-")
        case.addCleanup(shutil.rmtree, home, True)
        env = mock.patch.dict(os.environ, {"HELM_HOME": home})
        env.start()
        case.addCleanup(env.stop)
    _w(os.path.join(home, "_global", "seats", "claude", "instances",
                    seat_name, "role.json"),
       json.dumps({"v": 1, "seat": seat_name, "role": role}))


class RecordedRoleReadingTest(Case):
    """task/4112: a native lead helm never spawned carries its role only in a
    role.json declaration keyed on its REAL name. The slice carries the FOLDED
    name, so a numbered lead's slice never matches the name rule. The seat
    reading must consult the folded-key posture (the declaration, folded to
    the slice's seat name), so a running declared lead reads lead. A
    declaration that would not read reads worker, and a fold collision between
    two differently-declared seats reads UNKNOWN and is named, not guessed."""

    def _reading(self, e):
        return seatlimits.reading(e.box, status=self.status)

    def _seat(self, r, folded):
        return [s for s in r["seats"]
                if seatlimits.seat_of(s.name) == folded]

    def test_a_running_numbered_native_lead_reads_lead(self):
        """THE MEASURED CASE: a native lead whose name ends in a number and
        has no env marker is classed lead from its role.json declaration."""
        e = Estate(self, kb=BOX_KB)
        seat(e, "agents-proj_claude_2.slice", 201, 12 * GIB, 16 * GIB,
             int(1.0 * GIB))
        declaration(self, "proj-claude-2", "lead")
        got = self._seat(self._reading(e), "proj_claude_2")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].role, "lead")
        self.assertFalse(got[0].marked)

    def test_a_numbered_worker_with_a_worker_declaration_stays_worker(self):
        """CONTROL: a numbered seat whose declaration says worker is not
        promoted; a seat with no declaration and no marker stays a worker."""
        e = Estate(self, kb=BOX_KB)
        seat(e, "agents-proj_worker_2.slice", 202, 6 * GIB, 8 * GIB,
             int(1.0 * GIB))
        declaration(self, "proj-worker-2", "worker")
        got = self._seat(self._reading(e), "proj_worker_2")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].role, "worker")

    def test_a_name_suffix_lead_reads_lead_without_a_declaration(self):
        """The name rule stays: a `<project>-claude` lead needs no declaration,
        so the declaration read must not demote a name lead."""
        e = Estate(self, kb=BOX_KB)
        seat(e, "agents-proj_claude.slice", 203, 12 * GIB, 16 * GIB,
             int(1.0 * GIB))
        got = self._seat(self._reading(e), "proj_claude")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].role, "lead")

    def test_a_fold_collision_between_declared_roles_reads_unknown(self):
        """Two DISTINCT real seats fold to one name and declare different
        roles: the map reads UNKNOWN and names both, never guessing one."""
        home = tempfile.mkdtemp(prefix="helm-test-slicelimits-home-")
        self.addCleanup(shutil.rmtree, home, True)
        env = mock.patch.dict(os.environ, {"HELM_HOME": home})
        env.start()
        self.addCleanup(env.stop)
        base = os.path.join(home, "_global", "seats", "claude", "instances")
        _w(os.path.join(base, "proj-claude-2", "role.json"),
           json.dumps({"v": 1, "seat": "proj-claude-2", "role": "lead"}))
        _w(os.path.join(base, "proj_claude_2", "role.json"),
           json.dumps({"v": 1, "seat": "proj_claude_2", "role": "worker"}))
        got, why = seatlimits.postures()
        self.assertEqual(got.get("proj_claude_2"), "unknown")
        self.assertIn("proj-claude-2", why)
        self.assertIn("proj_claude_2", why)

    def test_a_declaration_beside_a_register_that_will_not_read_is_no_role(self):
        """A spawn register that exists and would not read is BROKEN, never
        absent (seat_role._declared_role): the role.json beside it does not
        stand in for it. The running seat reads worker, as recorded_role
        reads it, and the unread register is named."""
        e = Estate(self, kb=BOX_KB)
        seat(e, "agents-proj_claude_2.slice", 204, 12 * GIB, 16 * GIB,
             int(1.0 * GIB))
        declaration(self, "proj-claude-2", "lead")
        _w(os.path.join(os.environ["HELM_HOME"], "_global", "seats", "claude",
                        "instances", "proj-claude-2", "spawn.json"),
           "{not json")
        from helm.seat_role import recorded_role
        self.assertEqual(recorded_role("proj-claude-2"), "worker")
        got, why = seatlimits.postures()
        self.assertNotIn("proj_claude_2", got)
        self.assertIn("spawn.json", why)
        s = self._seat(self._reading(e), "proj_claude_2")
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0].role, "worker")


class SeatRunawayTest(Case):

    def test_the_runaway_lived_before_and_dies_in_its_own_slice_after(self):
        """FAIL-BEFORE / PASS-AFTER on the measured estate: before, the
        files the local seat's slice loads at a reload kill at 16G and the live
        stopgap at 14G, so the 9.5 GB python lives; after the apply both
        kill at 8G, while the unnamed integrator pane and the lead keep
        16G and fit with headroom."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        before = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertEqual(before["MemoryMax"][0], 16 * GIB)
        self.assertGreater(before["MemoryMax"][0], RUNAWAY)
        r = seatlimits.reading(e.box, status=self.status)
        live = {s.name: s for s in r["seats"]}
        self.assertGreater(live["agents-seat_a.slice"]
                           .live["MemoryMax"], RUNAWAY)
        with reload_from_files(e):
            rc, lines, back = seatlimits.apply(r, lower=True, stamp="T1")
        self.assertEqual(rc, 0, lines)
        after = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertLess(after["MemoryMax"][0], RUNAWAY)
        r2 = seatlimits.reading(e.box, status=self.status)
        got = {s.name: s.live for s in r2["seats"]}
        self.assertLess(got["agents-seat_a.slice"]["MemoryMax"], RUNAWAY)
        self.assertLess(got["agents-seat_b_2.slice"]["MemoryMax"],
                        got["agents-proj_claude.slice"]["MemoryMax"])
        for name in ("agents-pid4242.slice", "agents-proj_claude.slice",
                     "agents-seat_c.slice"):
            self.assertEqual(got[name]["MemoryMax"], 16 * GIB, name)
        for s in r2["seats"]:
            self.assertLessEqual(s.held * seatlimits.HEADROOM,
                                 s.live["MemoryHigh"], s.name)
        self.assertEqual(seatlimits.doctor_rows(r2)[0][0], "ok")


class SeatApplyTest(Case):

    def test_apply_moves_the_hand_file_aside_and_rolls_back_exactly(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        before = e.tree()
        r = seatlimits.reading(e.box, status=self.status)
        with reload_from_files(e):
            rc, lines, back = seatlimits.apply(r, lower=True, stamp="T1")
        self.assertEqual(rc, 0, lines)
        text = "\n".join(lines)
        self.assertIn("moved aside: %s" % os.path.join(
            e.units, "agents-.slice.d", "per-seat-memory.conf"), text)
        after = e.tree()
        cpu = os.path.join("home", ".config", "systemd", "user",
                           "agents-.slice.d", "per-seat-cpu.conf")
        self.assertEqual(after[cpu], before[cpu])
        role = os.path.relpath(seatlimits.role_path(e.box, "seat_a"),
                               e.base)
        self.assertIn(b"MemoryMax=8G", after[role])
        self.assertNotIn(os.path.relpath(seatlimits.role_path(
            e.box, "proj_claude"), e.base), after)
        self.assertNotIn(os.path.relpath(seatlimits.role_path(
            e.box, "seat_c"), e.base), after)
        self.assertEqual(back[-1], "systemctl --user daemon-reload")
        for line in back[:-1]:
            subprocess.run(["sh", "-c", line], check=True, timeout=10)
        self.assertEqual(e.tree(), before)
        self.assertFalse(os.path.exists(os.path.dirname(
            seatlimits.role_path(e.box, "seat_a"))))

    def test_a_second_apply_writes_nothing(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        with reload_from_files(e):
            seatlimits.apply(seatlimits.reading(e.box), lower=True,
                             stamp="T1")
            snap = e.tree()
            rc, lines, back = seatlimits.apply(seatlimits.reading(e.box),
                                               lower=True, stamp="T2")
        self.assertEqual((rc, back), (0, []), lines)
        self.assertIn("nothing written", lines[0])
        self.assertEqual(e.tree(), snap)

    def test_without_lower_nothing_running_is_lowered(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        before = e.tree()
        rc, lines, back = seatlimits.apply(seatlimits.reading(e.box))
        self.assertEqual((rc, back), (2, []))
        self.assertIn("agents-seat_a.slice: MemoryHigh 10G -> 6G", lines[0])
        self.assertEqual(e.tree(), before)

    def test_a_worker_that_would_not_fit_refuses_the_whole_apply(self):
        """PLANT: a worker holding 4.5G of anon cannot keep x1.5 headroom
        under a 6G throttle line, so nothing is written; the legitimate
        estate above applies."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        seat(e, "agents-seat_d.slice", 106, 12 * GIB, 16 * GIB,
             int(4.5 * GIB))
        before = e.tree()
        with reload_from_files(e) as run:
            rc, lines, back = seatlimits.apply(seatlimits.reading(e.box),
                                               lower=True)
        run.assert_not_called()
        self.assertEqual((rc, back), (2, []))
        self.assertIn("agents-seat_d.slice holds 4.50G of anon + shmem", lines[0])
        self.assertEqual(e.tree(), before)


class SeatRefusalTest(Case):
    """Every refusal `apply` makes, each planted alone on the measured estate:
    nothing is written, nothing is reloaded, and no rollback is owed."""

    def refused(self, e):
        """-> the refusal line, once nothing moved."""
        before = e.tree()
        with reload_from_files(e) as run:
            rc, lines, back = seatlimits.apply(
                seatlimits.reading(e.box, status=self.status), lower=True,
                stamp="T1")
        run.assert_not_called()
        self.assertEqual((rc, back), (2, []), lines)
        self.assertEqual(e.tree(), before)
        return lines[0]

    def test_a_lead_with_no_kill_line_that_would_not_fit_is_never_lowered(self):
        """A running lead whose slice carries no memory.max at all, holding
        more anon + shmem than the new throttle line leaves x1.5 room for:
        even --lower must not give it a kill line it is already near."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        seat(e, "agents-proj2_claude.slice", 107, 12 * GIB, "max",
             int(4.9 * GIB), int(3.8 * GIB))
        self.assertIn("agents-proj2_claude.slice holds 8.70G of anon + shmem",
                      self.refused(e))

    def test_a_running_seat_whose_limit_will_not_read_refuses(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        _w(os.path.join(e.fleet, "agents-seat_a.slice", "memory.max"),
           "garbage\n")
        self.assertIn("agents-seat_a.slice: live MemoryMax UNKNOWN",
                      self.refused(e))

    def test_an_unreadable_prefix_dropin_refuses(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        path = os.path.join(e.units, "agents-.slice.d", "zz-odd.conf")
        with open(path, "wb") as fh:
            fh.write(b"[Slice]\nMemoryMax=\xff\xfe\n")
        self.assertIn("unreadable prefix drop-in(s): %s" % path,
                      self.refused(e))

    def test_running_seats_that_will_not_read_refuse(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        with mock.patch.object(seatceiling, "slice_members",
                               return_value=({}, "the /proc walk failed")):
            got = self.refused(e)
        self.assertIn("the running seats would not read (the /proc walk "
                      "failed)", got)


class SeatEffectiveTest(Case):
    """What a reload LOADS, not what helm writes: a seat's own drop-in
    directory can carry a file that sorts after helm's, or a same-named file
    in a higher-priority unit directory can hide helm's. A green apply must
    never promise a value the files will not deliver."""

    def refused(self, e):
        before = e.tree()
        with reload_from_files(e) as run:
            rc, lines, back = seatlimits.apply(
                seatlimits.reading(e.box, status=self.status), lower=True,
                stamp="T1")
        run.assert_not_called()
        self.assertEqual((rc, back), (2, []), lines)
        self.assertEqual(e.tree(), before)
        return lines[0]

    def test_a_seat_file_that_sorts_after_helms_refuses_and_names_it(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        hand = e.unit(os.path.join(e.units, "agents-seat_a.slice.d"),
                      "zzzz-hand.conf", "[Slice]\nMemoryMax=16G\n")
        got = self.refused(e)
        self.assertIn("agents-seat_a.slice: MemoryMax would load 16G from "
                      "%s" % hand, got)

    def test_a_same_named_file_that_hides_helms_refuses_and_names_it(self):
        """A file named like helm's role drop-in, in user.control, hides the
        one helm writes in the user directory: the worker would keep the
        lead's 16G from the prefix default."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        mask = e.unit(os.path.join(e.control, "agents-seat_a.slice.d"),
                      seatlimits.ROLE_DROPIN, "[Slice]\nCPUWeight=100\n")
        got = self.refused(e)
        self.assertIn("agents-seat_a.slice: MemoryMax would load 16G from "
                      "%s" % seatlimits.prefix_path(e.box), got)
        self.assertIn("%s hides %s" % (mask, seatlimits.role_path(
            e.box, "seat_a")), got)

    def test_the_effective_check_is_the_reload_the_apply_then_runs(self):
        """Control: on the measured estate nothing overrides helm, so the
        apply goes green, and every seat then reads what plan promised."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        r = seatlimits.reading(e.box, status=self.status)
        self.assertEqual(seatlimits.plan(r, lower=True)[2], [])
        with reload_from_files(e):
            rc, lines, _back = seatlimits.apply(r, lower=True, stamp="T1")
        self.assertEqual(rc, 0, lines)
        got = {s.name: s.live for s in seatlimits.reading(
            e.box, status=self.status)["seats"]}
        self.assertEqual(got, {s.name: seatlimits.target(r, s)
                               for s in r["seats"]})
        # the positive control on the same observable: one later seat file
        # and the plan names it
        e.unit(os.path.join(e.units, "agents-seat_b_2.slice.d"),
               "zzzz-hand.conf", "[Slice]\nMemoryHigh=12G\n")
        self.assertEqual(len(seatlimits.plan(seatlimits.reading(
            e.box, status=self.status), lower=True)[2]), 1)


class SeatStoppedTest(Case):
    """A seat that is not running has no process to read its role from: its
    name alone would make a lead named outside the lead suffixes a worker,
    and the next launch would load the worker's kill line."""

    def stopped(self, e, name="seat_lead"):
        e.unit(os.path.join(e.control, "agents-%s.slice.d" % name),
               "50-MemoryMax.conf", "[Slice]\nMemoryMax=17179869184\n")
        return seatlimits.role_path(e.box, name)

    def test_a_stopped_seat_gets_no_role_file(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        path = self.stopped(e)
        before = e.tree()
        r = seatlimits.reading(e.box, status=self.status)
        self.assertNotIn(path, seatlimits.plan(r, lower=True)[0])
        with reload_from_files(e):
            rc, lines, back = seatlimits.apply(r, lower=True, stamp="T1")
        self.assertEqual(rc, 0, lines)
        self.assertFalse(os.path.exists(path))
        # controls: a running worker still gets its file, a second apply
        # writes nothing, and the rollback restores every byte
        self.assertTrue(os.path.exists(seatlimits.role_path(e.box, "seat_a")))
        with reload_from_files(e):
            again = seatlimits.apply(seatlimits.reading(
                e.box, status=self.status), lower=True, stamp="T2")
        self.assertEqual(again[0], 0, again[1])
        self.assertIn("nothing written", again[1][0])
        for line in back[:-1]:
            subprocess.run(["sh", "-c", line], check=True, timeout=10)
        self.assertEqual(e.tree(), before)

    def test_role_for_names_a_stopped_seat_and_its_file_is_written(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        path = self.stopped(e)
        with reload_from_files(e):
            rc, out = self.run_verb(e, "--seats", "--apply", "--lower",
                                    "--role-for", "seat-lead=worker")
        self.assertEqual(rc, 0, out)
        with open(path, encoding="utf-8") as fh:
            self.assertIn("MemoryMax=8G", fh.read())
        for bad in ("seat_lead", "seat_lead=boss", "=worker"):
            self.assertEqual(self.run_verb(e, "--seats", "--role-for",
                                           bad)[0], 2, bad)

    def test_role_for_lead_moves_a_stale_worker_file_aside(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        path = self.stopped(e)
        e.unit(os.path.dirname(path), seatlimits.ROLE_DROPIN,
               "[Slice]\nMemoryMax=8G\n")
        r = seatlimits.reading(e.box, status=self.status,
                               role_for={"seat_lead": "lead"})
        self.assertIn(path, seatlimits.plan(r, lower=True)[1])
        r = seatlimits.reading(e.box, status=self.status)
        self.assertNotIn(path, seatlimits.plan(r, lower=True)[1])



#: A role file as an earlier apply wrote it: the worker class, no
#: --role-for line.
OLD_ROLE = ("# helm slice-limits --seats (task/4062): the worker class, for "
            "agents-x.slice.\n[Slice]\nMemoryHigh=6G\nMemoryMax=8G\n"
            "MemorySwapMax=0\n")


def register(case, seat_name, role):
    """A spawn register for `seat_name` in a temp HELM_HOME of the case's
    own, nested the way a native instance's lives."""
    d = tempfile.mkdtemp(prefix="helm-test-slicelimits-home-")
    case.addCleanup(shutil.rmtree, d, True)
    env = mock.patch.dict(os.environ, {"HELM_HOME": d})
    env.start()
    case.addCleanup(env.stop)
    _w(os.path.join(d, "_global", "seats", "claude", "instances", seat_name,
                    "spawn.json"),
       json.dumps({"v": 1, "seat": seat_name, "role": role}))


class SeatStaleRoleTest(Case):
    """A stopped seat's old worker role file loads at its next reload: a
    lead relaunched at 12G/16G drops to the worker's 6G/8G. The plan names
    each such file, and the apply moves it aside, unless --role-for wrote
    it."""

    def setUp(self):
        super().setUp()
        register(self, "nobody-here", "worker")

    def old_file(self, e, seat):
        return e.unit(os.path.dirname(seatlimits.role_path(e.box, seat)),
                      seatlimits.ROLE_DROPIN, OLD_ROLE)

    def removed(self, e, path, seat, why):
        """The dry plan names the file and why; the apply moves it aside in
        one line naming the seat, the file and why; the rollback restores
        it."""
        before = e.tree()
        r = seatlimits.reading(e.box, status=self.status)
        self.assertIn(path, seatlimits.plan(r, lower=True)[1])
        rc, out = self.run_verb(e, "--seats", "--lower")
        self.assertIn("--apply moves aside %s: agents-%s.slice is stopped"
                      % (path, seat), out)
        self.assertIn(why, out)
        self.assertEqual(e.tree(), before)
        doc = "\n".join(t for _, t in slicelimits.doctor_rows(
            e.box, status=self.status))
        self.assertIn("stale role file %s: agents-%s.slice is stopped"
                      % (path, seat), doc)
        with reload_from_files(e):
            rc, lines, back = seatlimits.apply(r, lower=True, stamp="T1")
        self.assertEqual(rc, 0, lines)
        self.assertFalse(os.path.exists(path))
        said = [ln for ln in lines if path in ln]
        self.assertEqual(len(said), 1, lines)
        self.assertIn("agents-%s.slice is stopped" % seat, said[0])
        self.assertIn(why, said[0])
        self.assertEqual(seatlimits.files_for(
            e.box, "agents-%s.slice" % seat)["MemoryMax"][2],
            seatlimits.prefix_path(e.box))
        for line in back[:-1]:
            subprocess.run(["sh", "-c", line], check=True, timeout=10)
        self.assertEqual(e.tree(), before)

    def test_a_stopped_lead_by_name_loses_its_stale_worker_file(self):  # noqa: VACUOUS_ASSERTION — removed() asserts the plan, the line, the reload and the rollback unconditionally
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        path = self.old_file(e, "other_claude")
        self.removed(e, path, "other_claude", "its name is a lead's")

    def test_a_stopped_lead_by_its_register_loses_its_stale_worker_file(self):  # noqa: VACUOUS_ASSERTION — removed() asserts the plan, the line, the reload and the rollback unconditionally
        """The measured case: a lead whose name reads as a worker's, and
        whose register records the lead posture."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        register(self, "seat-under-test", "lead")
        path = self.old_file(e, "seat_under_test")
        self.removed(e, path, "seat_under_test",
                     "its spawn register records the lead posture")

    def test_a_stopped_worker_keeps_a_file_that_agrees(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        register(self, "seat-w", "worker")
        path = self.old_file(e, "seat_w")
        lead = self.old_file(e, "other_claude")
        r = seatlimits.reading(e.box, status=self.status)
        files, removes, refuse = seatlimits.plan(r, lower=True)
        self.assertNotIn(path, removes)
        self.assertEqual(refuse, [])
        out = self.run_verb(e, "--seats", "--lower")[1]
        self.assertNotIn("moves aside %s" % path, out)
        # positive control on the same observables: the stopped lead's file
        # beside it is planned and printed
        self.assertIn(lead, removes)
        self.assertIn("moves aside %s: agents-other_claude.slice is stopped"
                      % lead, out)

    def test_a_role_for_worker_file_on_a_stopped_lead_is_kept(self):  # noqa: VACUOUS_ASSERTION — the loop runs its two fixed cases every time
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        register(self, "seat-under-test", "lead")
        for seat, flag in (("other_claude", "other-claude=worker"),
                           ("seat_under_test", "seat-under-test=worker")):
            path = seatlimits.role_path(e.box, seat)
            with reload_from_files(e):
                rc, out = self.run_verb(e, "--seats", "--apply", "--lower",
                                        "--role-for", flag)
            self.assertEqual(rc, 0, out)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("MemoryMax=8G", text)
            self.assertIn("# --role-for %s=worker" % seat, text)
            r = seatlimits.reading(e.box, status=self.status)
            self.assertNotIn(path, seatlimits.plan(r, lower=True)[1])
            with reload_from_files(e):
                rc, lines, back = seatlimits.apply(r, lower=True, stamp="T9")
            self.assertEqual((rc, back), (0, []), lines)
            self.assertIn("nothing written", lines[0])
            self.assertTrue(os.path.exists(path))

    def test_a_role_for_line_naming_another_seat_does_not_keep_it(self):
        """The --role-for line keeps only the file it was written for: one
        copied into another stopped lead's directory is still stale."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        path = e.unit(os.path.dirname(seatlimits.role_path(
            e.box, "other_claude")), seatlimits.ROLE_DROPIN,
            OLD_ROLE + "# --role-for seat_w=worker\n")
        self.assertIn(path, seatlimits.plan(seatlimits.reading(
            e.box, status=self.status), lower=True)[1])
        # control: the line naming this seat keeps it
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(OLD_ROLE + "# --role-for other_claude=worker\n")
        self.assertNotIn(path, seatlimits.plan(seatlimits.reading(
            e.box, status=self.status), lower=True)[1])

    def test_a_running_seats_files_behave_as_before(self):
        """A running worker's file is rewritten, a running lead's moved aside
        as a lead now, and neither is called stale."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        worker = self.old_file(e, "seat_a")
        lead = self.old_file(e, "seat_c")
        r = seatlimits.reading(e.box, status=self.status)
        files, removes, refuse = seatlimits.plan(r, lower=True)
        self.assertIn(worker, files)
        self.assertEqual(removes, [lead])
        self.assertEqual(refuse, [])
        out = self.run_verb(e, "--seats", "--lower")[1]
        self.assertIn("--apply moves aside %s (that seat is a lead now)"
                      % lead, out)
        self.assertNotIn("stopped", out)

    def test_an_unreadable_register_refuses_a_file_it_would_decide(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        register(self, "seat-x", "lead")
        reg = os.path.join(os.environ["HELM_HOME"], "_global", "seats",
                           "claude", "instances", "seat-x", "spawn.json")
        _w(reg, "{not json")
        path = self.old_file(e, "seat_x")
        r = seatlimits.reading(e.box, status=self.status)
        refuse = seatlimits.plan(r, lower=True)[2]
        self.assertEqual(len(refuse), 1, refuse)
        self.assertIn("agents-seat_x.slice is stopped", refuse[0])
        self.assertIn(path, refuse[0])
        self.assertIn("UNKNOWN", refuse[0])
        # control: a lead by name needs no register
        os.remove(path)
        self.old_file(e, "other_claude")
        self.assertEqual(seatlimits.plan(seatlimits.reading(
            e.box, status=self.status), lower=True)[2], [])


class SeatDropinReadTest(Case):
    """Every drop-in a reload reads for a seat is read here, the way systemd
    reads it, and one that will not read is named, never skipped."""

    def refused(self, e):
        before = e.tree()
        with reload_from_files(e) as run:
            rc, lines, back = seatlimits.apply(
                seatlimits.reading(e.box, status=self.status), lower=True,
                stamp="T1")
        run.assert_not_called()
        self.assertEqual((rc, back), (2, []), lines)
        self.assertEqual(e.tree(), before)
        return lines[0]

    def test_a_non_utf8_seat_dropin_is_named(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        path = os.path.join(e.units, "agents-seat_a.slice.d", "zz-odd.conf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"[Slice]\nMemoryMax=\xff\xfe\n")
        got = self.refused(e)
        self.assertIn("agents-seat_a.slice: %s would not read" % path, got)
        rc, out = self.run_verb(e, "--seats", "--lower")
        self.assertIn("--apply refuses: agents-seat_a.slice: %s would not "
                      "read" % path, out)

    def test_the_hyphen_prefix_dropin_dir_is_read_as_systemd_does(self):
        """agents-ops-a.slice reads agents-ops-a.slice.d, agents-ops-.slice.d
        and agents-.slice.d; a same-named file in the more specific prefix
        directory hides the less specific one (systemd.unit(5))."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        seat(e, "agents-ops.slice/agents-ops-a.slice", 108, 12 * GIB,
             16 * GIB, int(0.5 * GIB), lead=True)
        self.assertEqual(seatlimits.dropin_dirs("agents-ops-a.slice"),
                         ["agents-ops-a.slice.d", "agents-ops-.slice.d",
                          "agents-.slice.d"])
        mid = e.unit(os.path.join(e.units, "agents-ops-.slice.d"),
                     seatlimits.SEAT_DROPIN, "[Slice]\nMemoryMax=3G\n")
        e.unit(os.path.join(e.units, "agents-.slice.d"),
               seatlimits.SEAT_DROPIN, "[Slice]\nMemoryMax=16G\n")
        got = seatlimits.files_for(e.box, "agents-ops-a.slice")
        self.assertEqual(got["MemoryMax"][2], mid)
        self.assertEqual(seatlimits.files_for(
            e.box, "agents-seat_a.slice")["MemoryMax"][2],
            seatlimits.prefix_path(e.box))
        self.assertIn("agents-ops-a.slice: MemoryMax would load 3G from %s"
                      % mid, self.refused(e))

    def test_unit_directory_priority_is_asked_before_the_name(self):
        """systemd walks the unit directories by priority and, inside each,
        the seat's own directory before the prefix one; the first file of a
        name wins. So user.control's prefix file hides a same-named file in
        the user unit directory's own one, while inside one unit directory
        the own file hides the prefix one."""
        e = Estate(self, kb=BOX_KB)
        own = e.unit(os.path.join(e.units, "agents-seat_a.slice.d"),
                     "zz-x.conf", "[Slice]\nMemoryMax=8G\n")
        high = e.unit(os.path.join(e.control, "agents-.slice.d"),
                      "zz-x.conf", "[Slice]\nMemoryMax=16G\n")
        got = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertEqual(got["MemoryMax"][2], high)
        hidden = [c for c in seatlimits.stack(e.box, "agents-seat_a.slice")
                  if c.name == "zz-x.conf"]
        self.assertEqual([(c.path, c.masked) for c in hidden],
                         [(high, False), (own, True)])
        # control: inside one unit directory the seat's own file wins
        os.remove(high)
        low = e.unit(os.path.join(e.units, "agents-.slice.d"), "zz-x.conf",
                     "[Slice]\nMemoryMax=16G\n")
        hidden = [c for c in seatlimits.stack(e.box, "agents-seat_a.slice")
                  if c.name == "zz-x.conf"]
        self.assertEqual([(c.path, c.masked) for c in hidden],
                         [(own, False), (low, True)])

    def test_a_higher_priority_prefix_file_hiding_helms_role_file_refuses(self):
        """The guard the order feeds: a same-named file in user.control's
        prefix directory that sets no memory limit (so it is not moved
        aside) hides helm's role file for a running worker, so the apply
        refuses, naming it, and writes nothing."""
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        hider = e.unit(os.path.join(e.control, "agents-.slice.d"),
                       seatlimits.ROLE_DROPIN, "[Slice]\nCPUWeight=50\n")
        got = self.refused(e)
        self.assertIn("agents-seat_a.slice: %s hides %s" % (
            hider, seatlimits.role_path(e.box, "seat_a")), got)


#: A 32 GiB box: the worker's 2G cap holds no per-seat High.
SMALL_KB = 32 * 1024 * 1024
OLD_LEAD = ("# helm slice-limits --seats (task/4062): the lead class.\n"
            "[Slice]\nMemoryHigh=3G\nMemoryMax=5G\nMemorySwapMax=0\n")
OLD_WORKER = ("# helm slice-limits --seats (task/4062): the worker class.\n"
              "[Slice]\nMemoryHigh=1G\nMemoryMax=2G\nMemorySwapMax=0\n")


class SeatNoHighTest(Case):
    """A per-seat MemoryHigh under 4G freezes seats: where a seat's cap is
    too small to hold one, helm writes none, and agents.slice's own High
    throttles."""

    def small(self):
        """A 32G box whose helm files carry the old derivation: the lead
        class 3G/5G and a worker's 1G/2G."""
        e = Estate(self, kb=SMALL_KB)
        e.unit(e.units, "agents.slice", "[Slice]\nTasksMax=8192\n")
        e.unit(os.path.join(e.units, "agents-.slice.d"),
               seatlimits.SEAT_DROPIN, OLD_LEAD)
        e.unit(os.path.join(e.units, "agents-seat_a.slice.d"),
               seatlimits.ROLE_DROPIN, OLD_WORKER)
        seat(e, "agents-seat_a.slice", 101, GIB, 2 * GIB, int(0.3 * GIB))
        seat(e, "agents-proj_claude.slice", 103, 3 * GIB, 5 * GIB,
             int(0.5 * GIB))
        return e

    def test_apply_removes_the_stale_high_idempotently_and_rolls_back(self):
        e = self.small()
        before = e.tree()
        r = seatlimits.reading(e.box, status=self.status)
        with reload_from_files(e):
            rc, lines, back = seatlimits.apply(r, stamp="T1")
        self.assertEqual(rc, 0, lines)
        text = "\n".join(lines)
        self.assertIn("worker: no per-seat MemoryHigh (MemoryMax 2G): "
                      "throttle left to the fleet line: a per-seat High "
                      "under 4G freezes seats", text)
        with open(seatlimits.role_path(e.box, "seat_a"),
                  encoding="utf-8") as fh:
            worker = fh.read()
        self.assertNotIn("MemoryHigh=1G", worker)
        self.assertIn("MemoryHigh=infinity", worker)
        loads = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertEqual(loads["MemoryHigh"][0], slicelimits.INF)
        self.assertEqual(loads["MemoryHigh"][2],
                         seatlimits.role_path(e.box, "seat_a"))
        self.assertEqual(loads["MemoryMax"][0], 2 * GIB)
        live = {s.name: s.live for s in seatlimits.reading(
            e.box, status=self.status)["seats"]}
        self.assertEqual(live["agents-seat_a.slice"]["MemoryHigh"],
                         slicelimits.INF)
        self.assertEqual(live["agents-proj_claude.slice"]["MemoryHigh"],
                         4 * GIB)
        with reload_from_files(e):
            again = seatlimits.apply(seatlimits.reading(
                e.box, status=self.status), stamp="T2")
        self.assertEqual((again[0], again[2]), (0, []), again[1])
        self.assertIn("nothing written", again[1][0])
        for line in back[:-1]:
            subprocess.run(["sh", "-c", line], check=True, timeout=10)
        self.assertEqual(e.tree(), before)

    def test_the_plan_and_the_doctor_say_why_there_is_no_high(self):
        e = self.small()
        rc, out = self.run_verb(e, "--seats")
        self.assertIn("worker: no per-seat MemoryHigh (MemoryMax 2G): "
                      "throttle left to the fleet line", out)
        self.assertNotIn("lead: no per-seat MemoryHigh", out)
        text = "\n".join(t for _, t in slicelimits.doctor_rows(
            e.box, status=self.status))
        self.assertIn("seat limits: worker: no per-seat MemoryHigh "
                      "(MemoryMax 2G): throttle left to the fleet line: a "
                      "per-seat High under 4G freezes seats", text)
        # control: the measured box omits no High, and says nothing of it
        big = Estate(self, kb=BOX_KB)
        tonight(big)
        said = self.run_verb(big, "--seats")[1]
        self.assertIn("worker MemoryHigh 6G", said)
        self.assertNotIn("no per-seat MemoryHigh", said)

    def test_a_seat_with_no_high_is_lowered_only_with_room_under_its_kill(self):  # noqa: VACUOUS_ASSERTION — the refusal line is asserted unconditionally
        """With no per-seat throttle line, the headroom check binds at the
        new MemoryMax: a worker holding 1.6G is not lowered to a 2G kill."""
        e = self.small()
        seat(e, "agents-seat_d.slice", 106, 12 * GIB, 16 * GIB,
             int(1.6 * GIB))
        before = e.tree()
        with reload_from_files(e) as run:
            rc, lines, back = seatlimits.apply(seatlimits.reading(
                e.box, status=self.status), lower=True, stamp="T1")
        run.assert_not_called()
        self.assertEqual((rc, back), (2, []), lines)
        self.assertIn("agents-seat_d.slice holds 1.60G of anon + shmem; x1.5 "
                      "does not fit under the new MemoryMax 2G", lines[0])
        self.assertEqual(e.tree(), before)

    def test_seat_props_stamps_no_high_on_a_small_box(self):
        e = Estate(self, kb=16 * 1024 * 1024)
        os.environ.pop(seatlimits.ROLE_ENV, None)
        self.assertEqual(self.run_verb(e, "--seat-props", "seat-a")[1],
                         "MemoryHigh=infinity MemoryMax=2G MemorySwapMax=0\n")


class SeatVerbTest(Case):

    def test_seat_props_is_what_a_launch_stamps(self):
        e = Estate(self, kb=BOX_KB)
        os.environ.pop(seatlimits.ROLE_ENV, None)
        self.assertEqual(self.run_verb(e, "--seat-props", "seat-a"),
                         (0, "MemoryHigh=6G MemoryMax=8G MemorySwapMax=0\n"))
        self.assertEqual(self.run_verb(e, "--seat-props", "proj-claude")[1],
                         "MemoryHigh=12G MemoryMax=16G MemorySwapMax=0\n")
        os.environ[seatlimits.ROLE_ENV] = "lead"
        self.assertEqual(self.run_verb(e, "--seat-props", "seat-a")[1],
                         "MemoryHigh=12G MemoryMax=16G MemorySwapMax=0\n")
        self.assertEqual(self.run_verb(e, "--seat-props", "a", "--seats")[0],
                         2)

    def test_seat_props_honours_a_recorded_lead_role(self):
        e = Estate(self, kb=BOX_KB)
        os.environ.pop(seatlimits.ROLE_ENV, None)
        with mock.patch.object(seatlimits, "postures",
                               return_value=({"seat_a_2": "lead"}, "")):
            self.assertEqual(self.run_verb(e, "--seat-props", "seat-a-2")[1],
                             "MemoryHigh=12G MemoryMax=16G MemorySwapMax=0\n")
            self.assertEqual(self.run_verb(e, "--seat-props", "seat-b")[1],
                             "MemoryHigh=6G MemoryMax=8G MemorySwapMax=0\n",
                             "control: an unrecorded seat stays a worker")

    def test_the_dry_run_prints_the_table_and_writes_nothing(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        before = e.tree()
        rc, out = self.run_verb(e, "--seats", "--lower")
        self.assertEqual(rc, 1)
        self.assertIn("worker MemoryHigh 6G", out)
        self.assertIn("agents-pid4242.slice", out)
        self.assertIn("other source: %s" % os.path.join(
            e.units, "agents-.slice.d", "per-seat-memory.conf"), out)
        self.assertNotIn("--apply refuses", out)
        self.assertEqual(e.tree(), before)
        rc, out = self.run_verb(e, "--seats", "--json")
        self.assertEqual(json.loads(out)["classes"]["worker"]["MemoryMax"]
                         ["value"], 8 * GIB)

    def test_the_doctor_names_running_seats_off_the_derivation(self):
        e = Estate(self, kb=BOX_KB)
        tonight(e)
        text = "\n".join(t for _, t in slicelimits.doctor_rows(
            e.box, status=self.status))
        self.assertIn("running seat slice(s) differ from the derivation", text)
        self.assertIn("agents-seat_a.slice (worker: MemoryHigh 10.0G, "
                      "derived 6.0G, MemoryMax 14.0G, derived 8.0G)", text)
        self.assertIn("per-seat-memory.conf (MemoryHigh=12G MemoryMax=16G)",
                      text)

class FullUserPathTest(Case):
    """The box must cover the full systemd.user(5) user-unit load path and the
    type-level slice.d dir, not just the three dirs helm has owned: a limit
    planted in /etc, /usr/lib or slice.d is visible and ordered as systemd
    orders it. All dirs live under a tmp root (no host touch)."""

    def estate(self):
        """An estate whose box names the full user load path, all under a tmp
        base; the write target is home/.config/systemd/user."""
        e = Estate(self, kb=96 * GIB)
        root = os.path.join(e.base, "root")
        for rel in ("etc/xdg/systemd/user", "etc/systemd/user",
                    "run/systemd/user", "usr/lib/systemd/user",
                    "usr/local/lib/systemd/user",
                    "home/.config/systemd/user",
                    "home/.config/systemd/user.control",
                    "home/.local/share/systemd/user",
                    "runtime/systemd/user.control",
                    "runtime/systemd/transient",
                    "runtime/systemd/generator.early",
                    "runtime/systemd/user",
                    "runtime/systemd/generator",
                    "runtime/systemd/generator.late"):
            os.makedirs(os.path.join(root, rel), exist_ok=True)
        e.root = root
        e.home = os.path.join(root, "home", ".config", "systemd", "user")
        e.etc = os.path.join(root, "etc", "systemd", "user")
        e.usrlib = os.path.join(root, "usr", "lib", "systemd", "user")
        e.ctrl = os.path.join(root, "home", ".config", "systemd",
                              "user.control")
        e.box = slicelimits.box(sys_root=e.box.sys, proc=e.box.proc,
                                cgroup=e.box.cgroup, root=root)
        return e

    def test_memory_max_in_etc_is_seen(self):
        """A MemoryMax in <root>/etc/systemd/user/agents-<seat>.slice.d/ is
        reported, exactly as one planted in the home user dir is."""
        e = self.estate()
        path = e.unit(os.path.join(e.etc, "agents-seat_a.slice.d"),
                      "zz-etc.conf", "[Slice]\nMemoryMax=9G\n")
        got = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertEqual(got["MemoryMax"][2], path)
        self.assertIn(path, [c.path for c in
                             seatlimits.stack(e.box, "agents-seat_a.slice")])

    def test_memory_max_in_usr_lib_is_seen(self):
        """A MemoryMax in <root>/usr/lib/systemd/user/agents-<seat>.slice.d/
        is reported, exactly as one planted in the home user dir is."""
        e = self.estate()
        path = e.unit(os.path.join(e.usrlib, "agents-seat_a.slice.d"),
                      "zz-usrlib.conf", "[Slice]\nMemoryMax=8G\n")
        got = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertEqual(got["MemoryMax"][2], path)

    def test_type_level_slice_d_is_seen_and_lowest(self):
        """The type-level slice.d is the lowest-precedence dir: a same-named
        file in a higher name-specific dir masks it; with nothing above it,
        slice.d alone is loaded."""
        e = self.estate()
        name_dir = os.path.join(e.etc, "agents-seat_a.slice.d")
        type_dir = os.path.join(e.etc, "slice.d")
        os.makedirs(type_dir, exist_ok=True)
        high = e.unit(name_dir, "zz-hi.conf", "[Slice]\nMemoryMax=5G\n")
        type_d = e.unit(type_dir, "zz-hi.conf", "[Slice]\nMemoryMax=20G\n")
        got = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertEqual(got["MemoryMax"][2], high,
                         "a name-specific file must mask the type-level one")
        self.assertIn(type_d, [c.path for c in
                               seatlimits.stack(e.box, "agents-seat_a.slice")])
        os.remove(high)
        got = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertEqual(got["MemoryMax"][2], type_d)

    def test_type_level_loses_to_any_name_specific_dir(self):
        """The type-level slice.d loses to a same-named file in ANY
        name-specific dir: systemd applies type-level drop-ins after every
        name-specific dir's, and masking is name-based across the whole
        ordered dir list. Here the name-specific file is in a LOWER-priority
        dir (usr/lib) than the type-level one (etc), and still wins."""
        e = self.estate()
        type_d = e.unit(os.path.join(e.etc, "slice.d"), "zz-t.conf",
                        "[Slice]\nMemoryMax=20G\n")
        name_d = e.unit(os.path.join(e.usrlib, "agents-seat_a.slice.d"),
                        "zz-t.conf", "[Slice]\nMemoryMax=7G\n")
        got = seatlimits.files_for(e.box, "agents-seat_a.slice")
        self.assertEqual(got["MemoryMax"][2], name_d,
                         "type-level is lowest; a name-specific wins")

    def test_overrides_names_a_value_from_a_higher_dir(self):
        """A limit set in a dir that wins over helm's home role file is named
        by overrides with the same 'would load ... from <path>' and 'hides'
        lines as the existing user-dir guard (systemd.user(5) full path)."""
        e = self.estate()
        seat(e, "agents-seat_a.slice", 101, 8 * GIB, 12 * GIB,
             int(0.8 * GIB))
        # a same-named file in the HIGHEST load-path dir (user.control) sets a
        # value that wins over helm's home role file; overrides names it with
        # the same 'would load ... from <path>' and 'hides <role_path>' lines
        # the existing guard produces, now under the full user path.
        hider = e.unit(os.path.join(e.ctrl, "agents-seat_a.slice.d"),
                       seatlimits.ROLE_DROPIN,
                       "[Slice]\nMemoryMax=100G\nMemoryHigh=90G\n")
        refuse = seatlimits.plan(seatlimits.reading(
            e.box, status=self.status), lower=True)[2]
        refuse_text = "; ".join(refuse)
        self.assertIn(
            "agents-seat_a.slice: MemoryMax would load 100G from %s"
            % hider, refuse_text)
        self.assertIn(
            "agents-seat_a.slice: %s hides %s" % (
                hider, seatlimits.role_path(e.box, "seat_a")), refuse_text)


if __name__ == "__main__":
    unittest.main()
