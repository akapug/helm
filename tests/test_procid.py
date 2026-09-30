"""procid — WHAT a pid is, and the false DEAD that made it necessary.

THE INCIDENT THESE PIN, 2026-08-06: a seat that was mid-turn landing commits was
reported "orca-adopted — DEAD" and the fleet was told to route around it. Its
pane had exec'd `.../claude/versions/<semver>` directly, so its comm was the
SEMVER, and every identity gate in helm demanded the literal string "claude".
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import procid, sessions


def _stat(pid, comm, starttime):
    """A /proc/<pid>/stat line. `_pid_is_claude` reads field 22 as index 19 of
    the split AFTER the comm parenthesis, so the shape matters more than values."""
    fields = ["S"] + ["0"] * 30
    fields[19] = str(starttime)
    return "%d (%s) %s" % (pid, comm, " ".join(fields))


class ProcFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-procid-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"HELM_PROC": self.tmp}, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def plant(self, pid, exe=None, comm=None, starttime=100, env=()):
        d = os.path.join(self.tmp, str(pid))
        os.makedirs(d, exist_ok=True)
        # environ must exist: orcaadopt reads it inside the bracket and ENOENT
        # PROVES EXIT, so a fixture without one reads as a dead process and
        # every verdict past that point is ABSENT for the wrong reason. Found
        # by the positive control in test_a_versioned_pane_we_CAN_confirm_is_a_row.
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join(e.encode() for e in env) + b"\0")
        if exe is not None:
            # the TARGET need not exist: /proc/<pid>/exe is a link whose text is
            # the answer, which is exactly why a deleted binary still reads back.
            os.symlink(exe, os.path.join(d, "exe"))
        if comm is not None:
            with open(os.path.join(d, "comm"), "w") as f:
                f.write(comm + "\n")
            with open(os.path.join(d, "stat"), "w") as f:
                f.write(_stat(pid, comm, starttime))
        return d


class ExeRungTest(ProcFixture):

    def test_a_versions_dir_with_a_nonsemver_basename_is_not_claude(self):
        """The versioned rung pins the SEMVER, not the directory: [^/]+ let
        /evil/claude/versions/malware claim the versioned-launch shape."""
        self.plant(4005, exe="/evil/claude/versions/malware", comm="malware")
        self.assertIs(procid.is_claude(4005), False)

    def test_a_versioned_launch_is_claude_though_its_comm_is_the_semver(self):
        """THE REGRESSION. comm is the exec'd binary's BASENAME, so this pane
        is named "2.1.223" and every string comparison against "claude" says no."""
        self.plant(4001, exe="/home/x/.local/share/claude/versions/2.1.223",
                   comm="2.1.223")
        self.assertFalse(procid.comm_is_claude(b"2.1.223"),
                         "control: the old rung really does reject this pane")
        self.assertTrue(procid.exe_is_claude(4001))
        self.assertTrue(procid.is_claude(4001, b"2.1.223"))

    def test_a_wrapper_launch_is_still_claude_by_either_rung(self):
        """The common shape must not regress: comm agrees AND exe agrees."""
        self.plant(4002, exe="/home/x/.local/share/claude/versions/2.1.220",
                   comm="claude")
        self.assertTrue(procid.comm_is_claude(b"claude"))
        self.assertTrue(procid.exe_is_claude(4002))
        self.assertTrue(procid.is_claude(4002, b"claude"))

    def test_an_upgraded_out_from_under_pane_survives_the_deleted_suffix(self):
        """auto-update UNLINKS the running version, and the longest-lived panes
        are both likeliest to still hold a session and likeliest to have been
        upgraded out from under — so this is the common case, not an edge."""
        self.plant(4003,
                   exe="/home/x/.local/share/claude/versions/2.1.220 (deleted)",
                   comm="2.1.220")
        self.assertEqual(procid.exe_of(4003),
                         "/home/x/.local/share/claude/versions/2.1.220")
        self.assertTrue(procid.is_claude(4003, b"2.1.220"))

    def test_a_binary_literally_named_claude_anywhere_counts(self):
        self.plant(4004, exe="/usr/local/bin/claude", comm="claude")
        self.assertTrue(procid.exe_is_claude(4004))

    def test_a_non_claude_process_is_not_claude_by_either_rung(self):
        self.plant(4005, exe="/usr/bin/sleep", comm="sleep")
        # POSITIVE CONTROL on the same observable: the link really is readable,
        # so the two refusals below are a REJECTION and not an absent fixture.
        self.assertEqual(procid.exe_of(4005), "/usr/bin/sleep")
        self.assertFalse(procid.exe_is_claude(4005))
        self.assertFalse(procid.is_claude(4005, b"sleep"))

    def test_an_unreadable_exe_is_not_a_rejection_it_is_one_rung_declining(self):
        """No exe link planted, but comm still says claude, so the COMM rung
        rescues it. Keep reading: the pane this lane exists for is the one where
        comm does NOT say claude, and that pair is the next test."""
        self.plant(4006, comm="claude")
        self.assertIsNone(procid.exe_of(4006))
        self.assertIsNone(procid.exe_is_claude(4006),
                          "unreadable must be None — a fact about permission")
        self.assertTrue(procid.is_claude(4006, b"claude"))

    def test_a_semver_comm_whose_exe_we_may_not_read_is_UNKNOWN_not_absent(self):  # noqa: VACUOUS_ASSERTION — control is pid 4108: same fixture, same call, exe READABLE -> definite True
        """THE PAIR WITH NO COVERAGE (a FIX on 998520163c82).

        readlink /proc/<pid>/exe needs PTRACE_MODE_READ and comm does not, so
        another user's pane shows a readable comm and refuses the exe. When
        that comm is a SEMVER, this is precisely the pane the lane exists to
        rescue and we were merely not permitted to confirm it. Answering False
        would hand `_read_candidate` an ABSENT — the one verdict that never
        reaches `unreadable`, and so never reaches `cannot_look()`."""
        self.plant(4008, comm="2.1.223")            # NO exe link planted
        # POSITIVE CONTROL: the identical plant WITH a readable exe answers a
        # definite True, so the None below is the unreadable link talking and
        # not a fixture that was never built.
        self.plant(4108, exe="/x/claude/versions/2.1.223", comm="2.1.223")
        self.assertIs(procid.is_claude(4108, b"2.1.223"), True)

        self.assertIsNone(procid.exe_of(4008))
        self.assertIsNone(procid.is_claude(4008, b"2.1.223"),
                          "cannot-tell must be UNKNOWN, never 'not a claude'")

    def test_the_UNKNOWN_stays_narrow_or_it_floods_the_blind_bucket(self):  # noqa: VACUOUS_ASSERTION — control is pid 4109: same fixture, same missing exe, semver comm -> None
        """THE GUARD ON THE CURE, and it is the reason UNKNOWN is gated on the
        comm SHAPE rather than on the unreadable exe alone.

        MEASURED 2026-08-06: 870 pids on this box had a readable comm and 450
        of them refused the exe read. Widening UNKNOWN to every unreadable exe
        would put all 450 into `unreadable`, and `cannot_look()` would then make
        EVERY census consumer answer UNKNOWN — a census that cannot answer about
        anything, traded for one that was confidently wrong about two panes.
        `systemd` is not an unidentifiable claude."""
        self.plant(4009, comm="systemd")            # NO exe link planted
        self.plant(4109, comm="2.1.223")            # NO exe link either
        self.assertIsNone(procid.exe_of(4009))
        # POSITIVE CONTROL AND THE DISCRIMINATION IN ONE: same fixture, same
        # missing exe, and the SEMVER comm answers UNKNOWN. So the False below
        # is the comm SHAPE being read, not a rule that refuses everything.
        self.assertIsNone(procid.is_claude(4109, b"2.1.223"))
        self.assertIs(procid.is_claude(4009, b"systemd"), False,
                      "a readable NON-version comm is a genuine refutation; "
                      "widening this to None floods the blind bucket")

    def test_the_comm_ACCEPT_PATH_IS_UNCHANGED_and_so_is_its_spoof_surface(self):
        """HONEST BOUNDARY OF THIS FIX, pinned so nobody reads the exe rung as
        having closed something it did not.

        `is_claude` ORs the rungs, so a process that sets its own comm to
        "claude" (prctl/PR_SET_NAME, which node's process.title calls) is
        accepted exactly as it was before this change, whatever its exe says.
        The exe rung ADDS an accept path for panes helm was wrongly rejecting;
        it REMOVES none. Requiring exe instead would close this, and would also
        reject every install layout not yet enumerated — a separate change with
        a separate risk, not a silent rider on a false-DEAD fix."""
        self.plant(4007, exe="/usr/bin/sleep", comm="claude")
        self.assertFalse(procid.exe_is_claude(4007))
        self.assertTrue(procid.is_claude(4007, b"claude"),
                        "the comm rung is unchanged — this is the PRE-EXISTING "
                        "surface, neither widened nor narrowed here")

    def test_a_claude_binary_execd_as_ugrep_is_not_a_seat_process(self):
        """exec -a ugrep keeps the Claude exe. is_claude still says yes;
        the seat gate must say no, or the orphan is the seat."""
        self.plant(4010, exe="/home/x/.local/share/claude/versions/2.1.283",
                   comm="ugrep")
        self.assertEqual(procid.exe_of(4010),
                         "/home/x/.local/share/claude/versions/2.1.283")
        self.assertTrue(procid.is_claude(4010, b"ugrep"))
        self.assertIs(procid.is_seat_process(4010, b"ugrep"), False)

    def test_a_node_exe_is_not_a_seat_process(self):  # noqa: VACUOUS_ASSERTION — control is pid 4111: a versioned claude exe is a seat before this node exe is refused
        self.plant(4111, exe="/home/x/.local/share/claude/versions/2.1.283",
                   comm="2.1.283")
        self.assertIs(procid.is_seat_process(4111, b"2.1.283"), True)
        self.plant(4011, exe="/home/x/.nvm/versions/node/v22.22.0/bin/node",
                   comm="node")
        self.assertIs(procid.exe_is_node(4011), True)
        self.assertIs(procid.is_seat_process(4011, b"node"), False)
        self.assertIs(procid.is_claude(4011, b"node"), False)

    def test_an_npm_claude_whose_exe_is_node_is_a_seat_process(self):  # noqa: VACUOUS_ASSERTION — control is pid 4115: a versioned claude exe is a seat, and pid 4016 comm node stays refused
        """An npm install is a node binary whose process.title is claude.
        Trunk admitted that comm. Refusing every node exe first drops
        the seat, which is a false DEAD."""
        self.plant(4115, exe="/home/x/.local/share/claude/versions/2.1.283",
                   comm="2.1.283")
        self.assertIs(procid.is_seat_process(4115, b"2.1.283"), True)
        node = "/home/x/.nvm/versions/node/v22.22.0/bin/node"
        self.plant(4015, exe=node, comm="claude")
        self.assertIs(procid.exe_is_node(4015), True)
        self.assertIs(procid.exe_is_claude(4015), False)
        self.assertIs(procid.is_seat_process(4015, b"claude"), True)
        self.assertIs(procid.is_claude(4015, b"claude"), True)
        self.plant(4016, exe=node, comm="node")
        self.assertIs(procid.is_seat_process(4016, b"node"), False)
        self.plant(4017, exe="/usr/bin/sleep", comm="claude")
        self.assertIs(procid.is_seat_process(4017, b"claude"), False)

    def test_an_unreadable_exe_with_a_tool_comm_is_not_a_seat(self):  # noqa: VACUOUS_ASSERTION — control is pid 4112: a readable versioned claude exe is a seat before the tool comm is refused
        self.plant(4112, exe="/home/x/.local/share/claude/versions/2.1.283",
                   comm="2.1.283")
        self.assertIs(procid.is_seat_process(4112, b"2.1.283"), True)
        self.plant(4012, comm="timeout")
        self.assertIsNone(procid.exe_of(4012))
        self.assertIs(procid.is_seat_process(4012, b"timeout"), False)

    def test_a_versioned_comm_with_an_unreadable_exe_stays_unknown(self):  # noqa: VACUOUS_ASSERTION — control is pid 4113: same call, exe readable, is_seat_process True
        """The false-DEAD third state belongs to the seat gate too."""
        self.plant(4013, comm="2.1.283")
        self.plant(4113, exe="/x/claude/versions/2.1.283", comm="2.1.283")
        self.assertIs(procid.is_seat_process(4113, b"2.1.283"), True)
        self.assertIsNone(procid.is_seat_process(4013, b"2.1.283"))

    def test_comm_claude_with_an_unreadable_exe_stays_a_seat(self):  # noqa: VACUOUS_ASSERTION — the positive is is_seat_process True on this same pid; exe_of None is the permission fact, not the verdict
        """A later unreadable cmdline must still surface as UNKNOWN, which
        requires this pid to have passed the seat gate on comm alone."""
        self.plant(4014, comm="claude")
        self.assertIsNone(procid.exe_of(4014))
        self.assertIs(procid.is_seat_process(4014, b"claude"), True)


class PidIsClaudeTest(ProcFixture):
    """The gate that actually returned the false DEAD."""

    def test_a_semver_comm_pane_is_now_a_live_claude(self):
        self.plant(4101, exe="/home/x/.local/share/claude/versions/2.1.223",
                   comm="2.1.223", starttime=4242)
        self.assertTrue(sessions._pid_is_claude(4101))
        self.assertTrue(sessions._pid_is_claude(4101, 4242),
                        "the procStart incarnation key must still be honoured")

    def test_the_incarnation_key_still_refuses_a_recycled_pid(self):
        """The widening must not cost the pid-reuse guard: same pid, wrong birth
        stamp, still refused."""
        self.plant(4102, exe="/home/x/.local/share/claude/versions/2.1.223",
                   comm="2.1.223", starttime=4242)
        # POSITIVE CONTROL: the RIGHT stamp is accepted on this very pid, so
        # the refusal below is the incarnation key doing its job rather than the
        # identity rung quietly rejecting the whole fixture.
        self.assertTrue(sessions._pid_is_claude(4102, 4242))
        self.assertFalse(sessions._pid_is_claude(4102, 999))

    def test_a_non_claude_pid_is_still_refused(self):
        self.plant(4103, exe="/usr/bin/sleep", comm="sleep", starttime=7)
        self.plant(4104, exe="/home/x/.local/share/claude/versions/2.1.223",
                   comm="2.1.223", starttime=7)
        # POSITIVE CONTROL: an identically-planted CLAUDE pane in the same
        # fixture IS accepted, so the refusal is about what the pid is.
        self.assertTrue(sessions._pid_is_claude(4104))
        self.assertFalse(sessions._pid_is_claude(4103))


class LiveSidsTest(ProcFixture):
    """live_sids' RECORD rung is what should have saved the seat: the record was
    valid and named a live pid, and the identity gate reading it said no."""

    def test_a_valid_record_on_a_versioned_pane_is_now_held(self):
        home = os.path.join(self.tmp, "claude-home")
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        self.plant(4201, exe="/home/x/.local/share/claude/versions/2.1.223",
                   comm="2.1.223", starttime=555)
        import json
        with open(os.path.join(home, "sessions", "4201.json"), "w") as f:
            json.dump({"pid": 4201, "sessionId": "sid-versioned",
                       "procStart": 555}, f)
        with mock.patch.object(sessions, "cred_homes", return_value=[home]):
            held = sessions.live_sids()
        self.assertEqual(held.get("sid-versioned"), 4201,
                         "a VALID record naming a LIVE pid was read as holding "
                         "nothing, which is how a live seat was called DEAD")


    def test_the_argv_rung_holds_a_versioned_pane_with_an_unreadable_exe(self):
        """The fold this class exists for, on the ARGV rung: versioned comm +
        refused exe read is UNKNOWN, and unknown must stay HELD — dropping it
        licensed the false DEAD and the duplicate resume. Control: a readable
        non-claude comm on the same path stays out of the map."""
        d = self.plant(4205, comm="2.1.223")            # NO exe: unreadable
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join([b"/x/claude/versions/2.1.223",
                                b"--resume", b"sid-unreadable"]) + b"\0")
        c = self.plant(4206, comm="systemd")            # control: genuine no
        with open(os.path.join(c, "cmdline"), "wb") as f:
            f.write(b"\0".join([b"/usr/lib/systemd/systemd",
                                b"--resume", b"sid-systemd"]) + b"\0")
        with mock.patch.object(sessions, "cred_homes", return_value=[]):
            held = sessions.live_sids()
        self.assertEqual(held.get("sid-unreadable"), 4205)
        self.assertNotIn("sid-systemd", held)


class CensusVerdictTest(ProcFixture):
    """The tri-state only matters if it survives the trip to the census, and
    the whole original defect was a verdict that never reached `unreadable`."""

    def _cmdline(self, d, *argv):
        entry = os.path.join(d, "cmdline")
        with open(entry, "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        return entry

    def test_a_pane_we_may_not_confirm_lands_in_BLIND_not_ABSENT(self):  # noqa: VACUOUS_ASSERTION — control is pid 4311: same path with a readable exe -> real ROW
        from helm import orcaadopt
        d = self.plant(4301, comm="2.1.223")        # readable comm, NO exe
        entry = self._cmdline(d, "/x/claude/versions/2.1.223", "--resume")
        # POSITIVE CONTROL on the same path: the identical candidate WITH a
        # readable exe produces a real ROW, so the BLIND below is the missing
        # link and not a fixture `_read_candidate` could never parse at all.
        ok = self.plant(4311, exe="/x/claude/versions/2.1.223", comm="2.1.223")
        ok_row, ok_verdict = orcaadopt._read_candidate(
            4311, self._cmdline(ok, "/x/claude/versions/2.1.223", "--resume"))
        self.assertEqual(ok_verdict, orcaadopt.ROW)
        self.assertIsNotNone(ok_row)

        row, verdict = orcaadopt._read_candidate(4301, entry)
        self.assertIsNone(row)
        self.assertEqual(verdict, orcaadopt.BLIND,
                         "ABSENT here is the original bug: it is the one "
                         "verdict that never reaches cannot_look()")

    def test_the_walk_finds_a_pid_under_a_nested_proc_root(self):
        """The walk's pid parse must come from the path's own shape: the old
        fixed-index split only meant "pid" when the root was literally /proc,
        so under this fixture's nested root it walked NOTHING — and every
        test here dodged it by calling _read_candidate directly."""
        from helm import orcaadopt
        d = self.plant(4321, exe="/x/claude/versions/2.1.223", comm="2.1.223",
                       env=("HELM_CHAT_NAME=walker",))
        self._cmdline(d, "/x/claude/versions/2.1.223", "--resume")
        # the walk's own must-hit control refuses a scan that failed to
        # enumerate the CALLER's pid — plant ourselves so the scan counts
        me = self.plant(os.getpid(), comm="python3")
        self._cmdline(me, "python3")
        procs, unreadable = orcaadopt.claude_processes()[:2]
        self.assertIn(4321, [r.get("pid") for r in procs],
                      "the census walk never reached a plantable pid — the "
                      "root-depth parse is broken again")

    def test_an_ordinary_process_still_lands_in_ABSENT(self):  # noqa: VACUOUS_ASSERTION — control is pid 4312: same path, semver comm -> BLIND
        """CONTROL, and the guard on the cure: if this ever becomes BLIND, the
        census stops being able to answer about anything (450 such pids
        measured on one box)."""
        from helm import orcaadopt
        d = self.plant(4302, comm="systemd")        # readable comm, NO exe
        entry = self._cmdline(d, "/usr/lib/systemd/systemd")
        # POSITIVE CONTROL: same fixture, same missing exe, semver comm — this
        # one IS blind. So ABSENT below is a decision about the comm, not a
        # path that returns ABSENT for everything it cannot read.
        blind = self.plant(4312, comm="2.1.223")
        _, blind_verdict = orcaadopt._read_candidate(
            4312, self._cmdline(blind, "/x/claude/versions/2.1.223"))
        self.assertEqual(blind_verdict, orcaadopt.BLIND)

        row, verdict = orcaadopt._read_candidate(4302, entry)
        self.assertIsNone(row)
        self.assertEqual(verdict, orcaadopt.ABSENT)

    def test_a_versioned_pane_we_CAN_confirm_is_a_row(self):
        """POSITIVE CONTROL on the same path: with the exe readable, the same
        semver comm produces an actual census ROW."""
        from helm import orcaadopt
        d = self.plant(4303, exe="/x/claude/versions/2.1.223", comm="2.1.223")
        entry = self._cmdline(d, "/x/claude/versions/2.1.223", "--resume")
        row, verdict = orcaadopt._read_candidate(4303, entry)
        self.assertEqual(verdict, orcaadopt.ROW)
        self.assertIsNotNone(row)
        # the row is REAL, not an empty shell that happens to be non-None
        self.assertEqual(row["pid"], 4303)
        self.assertIn("start", row)


class LocksTableTest(ProcFixture):
    """procid.locks — /proc/locks parsed as (kind, pid, device, inode).

    The kernel writes the locked file's device as hex major:minor before
    the decimal inode, and a blocked waiter's line carries an extra '->'
    token that shifts every column after it right by one — so the device
    field is found by its SHAPE, not its position. The line shapes below
    are the kernel's own, byte for byte."""

    def _read(self, text, name="locks"):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return procid.locks(p)

    def test_the_device_is_makedev_hex_and_the_inode_decimal(self):
        """Measured lines: /tmp (major 259, minor 5) prints "103:05:<ino>"
        and /dev/shm (major 0, minor 27) prints "00:1b:<ino>". Both must
        come back as their st_dev integer."""
        rows = self._read(
            "1: FLOCK  ADVISORY  WRITE 7072 103:05:53625209 0 EOF\n"
            "2: POSIX  ADVISORY  WRITE 82026 00:1b:11 0 EOF\n")
        self.assertEqual(rows[0]["kind"], "FLOCK")
        self.assertEqual(rows[0]["pid"], 7072)
        self.assertEqual(rows[0]["dev"], os.makedev(0x103, 0x05))
        self.assertEqual(rows[0]["ino"], 53625209)
        self.assertFalse(rows[0]["waiter"])
        self.assertEqual(rows[1]["kind"], "POSIX")
        self.assertEqual(rows[1]["dev"], os.makedev(0, 0x1b))
        self.assertEqual(rows[1]["ino"], 11)

    def test_a_waiters_line_is_parsed_not_skipped_and_marked(self):
        """The '->' shifts the pid and device field right by one: col 6 is
        now the pid, col 7 the device. A parser that indexed the old
        columns would take the pid from the wrong slot — or, worse, drop
        the line — and a waiter's lock is a real lock a holder test must
        see. The waiter flag is what lets the reader say 'queued behind',
        not 'holding'."""
        rows = self._read(
            "1: -> FLOCK  ADVISORY  WRITE 7080 00:1b:11 0 EOF\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pid"], 7080)
        self.assertEqual(rows[0]["dev"], os.makedev(0, 0x1b))
        self.assertEqual(rows[0]["ino"], 11)
        self.assertTrue(rows[0]["waiter"])

    def test_an_unreadable_table_is_none_not_empty(self):  # noqa: VACUOUS_ASSERTION — the two positive arms above parse real lines; this one pins that the absence is None, not an empty table
        """A vanished /proc/locks must read as declining to answer, never
        as 'nobody holds anything': a caller that turned the empty list
        into a verdict would name no holder for a genuinely wedged lock,
        and the diagnostic that raises or refuses is the one that lies."""
        self.assertIsNone(procid.locks(os.path.join(self.tmp, "no-such")))


if __name__ == "__main__":
    unittest.main()
