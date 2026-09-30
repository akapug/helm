"""A seat process is Claude Code. A node tool is not, even when it inherited the name.

An npm-installed Claude Code is the exception: its exe is node and its
comm is claude, because process.title sets the comm. That one is the seat.

task/3384: `grep` on this host is `exec -a ugrep` of the Claude binary, so an
orphaned ugrep keeps a Claude exe and the seat's HELM_CHAT_NAME. Counting
every Claude exe as the seat made fleet and orcaadopt report that tool as
the seat. These arms use a planted proc root and never read the real /proc.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import fleet, orcaadopt, session, who


def _stat(pid, comm, starttime):
    fields = ["S"] + ["0"] * 30
    fields[19] = str(starttime)
    return "%d (%s) %s\n" % (pid, comm, " ".join(fields))


class SeatProcFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-seat-proc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def plant(self, pid, exe=None, comm="sleep", argv=(), env=(),
              starttime=100):
        d = os.path.join(self.tmp, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(_stat(pid, comm, starttime))
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"\0".join(e.encode() for e in env) + b"\0")
        os.symlink(self.tmp, os.path.join(d, "cwd"))
        if exe is not None:
            os.symlink(exe, os.path.join(d, "exe"))
        return d

    def _census(self):
        with mock.patch.object(session, "PROC", self.tmp), \
                mock.patch.object(who, "scan", return_value=[]):
            return session._proc_claude_census()

    def _fleet(self):
        buf = io.StringIO()
        with mock.patch.object(session, "PROC", self.tmp), \
                mock.patch.object(who, "scan", return_value=[]), \
                mock.patch.object(fleet, "_daemon_pids",
                                  lambda: ({}, set(), False)), \
                mock.patch.object(fleet, "_daemon_for",
                                  lambda *a: ("headless", None)), \
                mock.patch.object(fleet, "_roster", lambda: ({}, False)), \
                mock.patch.object(fleet, "_throttle",
                                  lambda pid, start: ({}, 0, "", True)), \
                contextlib.redirect_stdout(buf):
            rc = fleet.cmd_fleet([])
        return buf.getvalue(), rc


class SeatProcessCensusTest(SeatProcFixture):
    CLAUDE_EXE = "/home/x/.local/share/claude/versions/2.1.283"

    def test_a_ugrep_carrying_a_seat_name_is_not_that_seat(self):
        """exec -a ugrep of the Claude binary inherits HELM_CHAT_NAME and
        must not become a kimi row. The census still counts it, out loud."""
        self.plant(7102, exe=self.CLAUDE_EXE, comm="2.1.283",
                   argv=(self.CLAUDE_EXE,),
                   env=("HELM_CHAT_NAME=kimi", "HOME=/nonexistent-home"))
        self.plant(7101, exe=self.CLAUDE_EXE, comm="ugrep",
                   argv=("ugrep", "-r", "x"),
                   env=("HELM_CHAT_NAME=kimi", "HOME=/nonexistent-home"))
        census = self._census()
        pids = [r["pid"] for r in census["rows"]]
        # The versioned Claude pane is the control: the same proc root, the
        # same seat name, and it IS the seat. The ugrep assertion below is a
        # rejection, not a fixture the census never walked.
        self.assertIn(7102, pids)
        self.assertNotIn(7101, pids)
        self.assertEqual(census.get("excluded_by_seat", {}).get("kimi"), 1)
        out, _rc = self._fleet()
        self.assertIn(
            "1 non-agent process carrying seat name kimi was not counted",
            out)

    def test_a_node_mcp_child_carrying_a_seat_name_is_not_that_seat(self):
        """A node MCP child inherits HELM_CHAT_NAME and must not become a
        seat row. The census still counts it, out loud."""
        node = "/home/x/.nvm/versions/node/v22.22.0/bin/node"
        self.plant(7106, exe=self.CLAUDE_EXE, comm="2.1.283",
                   argv=(self.CLAUDE_EXE,),
                   env=("HELM_CHAT_NAME=kimi", "HOME=/nonexistent-home"))
        self.plant(7103, exe=node, comm="node",
                   argv=(node, "mcp-child"),
                   env=("HELM_CHAT_NAME=seat-a",
                        "HOME=/nonexistent-home"))
        census = self._census()
        pids = [r["pid"] for r in census["rows"]]
        self.assertIn(7106, pids)
        self.assertNotIn(7103, pids)
        self.assertEqual(census.get("excluded_by_seat", {}).get("seat-a"), 1)
        out, _rc = self._fleet()
        self.assertIn(
            "1 non-agent process carrying seat name seat-a was not counted",
            out)

    def test_a_node_mainthread_carrying_a_seat_name_is_not_that_seat(self):
        """node-MainThread is an MCP child, not the seat. The npm install
        beside it — node exe, comm claude — is the seat, and the census
        counts only the child out."""
        node = "/home/x/.nvm/versions/node/v22.22.0/bin/node"
        self.plant(7109, exe=node, comm="claude",
                   argv=("/home/x/.npm-global/bin/claude",),
                   env=("HELM_CHAT_NAME=kimi", "HOME=/nonexistent-home"))
        self.plant(7107, exe=node, comm="node-MainThread",
                   argv=(node, "mcp-child"),
                   env=("HELM_CHAT_NAME=seat-a",
                        "HOME=/nonexistent-home"))
        census = self._census()
        pids = [r["pid"] for r in census["rows"]]
        self.assertIn(7109, pids)
        self.assertNotIn(7107, pids)
        self.assertEqual(census.get("excluded_by_seat", {}).get("seat-a"), 1)
        out, _rc = self._fleet()
        self.assertIn(
            "1 non-agent process carrying seat name seat-a was not counted",
            out)

    def test_an_unreadable_exe_carrying_a_seat_name_is_excluded_and_counted(self):
        """No exe link: the kernel declined the read. comm is a tool, the
        environ still carries the seat, and the census must say so."""
        self.plant(7104, exe=None, comm="timeout", argv=("timeout", "5"),
                   env=("HELM_CHAT_NAME=seat-b",
                        "HOME=/nonexistent-home"))
        # Control: a readable versioned exe on the same root is a seat row,
        # so the exclusion below is the missing exe and not an empty census.
        self.plant(7105, exe=self.CLAUDE_EXE, comm="2.1.283",
                   argv=(self.CLAUDE_EXE,),
                   env=("HELM_CHAT_NAME=kimi", "HOME=/nonexistent-home"))
        census = self._census()
        pids = [r["pid"] for r in census["rows"]]
        self.assertIn(7105, pids)
        self.assertNotIn(7104, pids)
        self.assertEqual(
            census.get("excluded_by_seat", {}).get("seat-b"), 1)
        out, _rc = self._fleet()
        self.assertIn(
            "1 non-agent process carrying seat name seat-b was not counted",
            out)


class OrcaAdoptSeatProcessTest(SeatProcFixture):
    def test_resolve_returns_only_the_claude_pid_when_a_ugrep_shares_the_name(self):
        claude = "/home/x/.local/share/claude/versions/2.1.283"
        self.plant(7202, exe=claude, comm="2.1.283", argv=(claude,),
                   env=("HELM_CHAT_NAME=kimi",))
        self.plant(7201, exe=claude, comm="ugrep", argv=("ugrep",),
                   env=("HELM_CHAT_NAME=kimi",))
        node = "/home/x/.nvm/versions/node/v22.22.0/bin/node"
        self.plant(7203, exe=node, comm="node", argv=(node, "mcp-child"),
                   env=("HELM_CHAT_NAME=kimi",))
        # The walk refuses a scan that did not enumerate this process.
        me = self.plant(os.getpid(), exe="/usr/bin/python3", comm="python3",
                        argv=("python3",))
        self.assertTrue(os.path.isdir(me))
        with mock.patch.dict(os.environ, {"HELM_PROC": self.tmp},
                             clear=False), \
                mock.patch.object(orcaadopt, "roster_identity",
                                  return_value=(None, [], False)):
            info = orcaadopt.resolve("kimi")
        self.assertIsNotNone(info)
        self.assertEqual([int(p) for p in info["pids"]], [7202])
