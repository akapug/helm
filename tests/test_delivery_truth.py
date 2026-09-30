#!/usr/bin/env python3
"""A row is delivered only when its text reached the addressed seat's lead.

Three paths marked a row delivered without showing it, each measured live:

  * THE BEACON'S FILTER. A seat armed its beacon as
    `helm chat wait --seat S --follow 2>&1 | cut -c1-500`. The waiter commits
    the wake cursor and prints the line; `cut` holds that line until its
    producer exits, and the Monitor ends the pipeline by killing it at its
    deadline, buffer and all. The delivery hook then skips the row as already
    woken, so three addressed rows reached nobody while `helm chat seats` read
    pending 0. The line-holder fence knew only the harness `ugrep`.

  * THE TRUNCATED PAYLOAD. The hook read at most 64 KiB of its payload. An
    Edit of a large module carries the whole original file, so the document
    was cut, the parse failed and the payload read as `{}`: no session id and
    no agent id. Delivery then ran on the SEAT-level cursor, which session-keyed
    consumers never advance, and re-delivered rows hours old -- into subagents
    too, because the subagent fence reads `agent_id` from that same payload.

  * THE DEADLINE BETWEEN COMMIT AND EMIT. The installed pair bounds the
    delivery handler with SIGALRM, whose handler raises wherever the
    interpreter is. Raised after the cursor commit and before the emit, the
    row was consumed and never shown.

Every arm runs in a temporary HELM_HOME and chat dir; no live seat is read.
"""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from helm import (chat, cli, hookoutcome, hookrun, hooks,  # noqa: E402
                  seats, seats_cli, seats_cursor, seats_delivery, seats_join,
                  sessions)
from tests import test_beacons as tb  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "HELM_CHAT_LOG", "HELM_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "HELM_CHAT_OWNER_NAMES", "HELM_ADOPTED_DIR", "HELM_SEAT_STORAGE",
            "HELM_CHAT_EVENT_DIR", "HELM_BEACON_ORPHAN_EXIT", "HELM_SCRATCH_GC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE",
            "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL",
            "ORCA_PANE_KEY", "CLAUDE_CONFIG_DIR")

SID = "0d17e2a1-2222-4333-8444-555555555555"
SEAT = "truthseat"
ROW = "@%s APPROVE: the reviewer's verdict" % SEAT

# A copier that passes lines, run under whatever argv0 an arm names.
PASS = ("%s -S -c 'import sys, shutil; "
        "shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)'"
        % sys.executable)
PROBE = ("import os, sys; sys.path.insert(0, %r); "
         "from helm import beacons; "
         "print(beacons.line_holder(os.getpid()), flush=True)" % REPO)


def _q(text):
    return "'" + text.replace("'", "'\\''") + "'"


def _seam(plant):
    """THE SEAM the three planted arms freeze (task/3411): the walk's own
    bounded wait. The planted root holds the reader's argv in its PRE-EXEC
    state on a regular file — a fork that has forked but not yet execed its
    filter, which is what that reader's cmdline reads as under gate load —
    and `plant` is applied the moment the walk waits at all (the cure's one
    settle). A walk that never waits (the base walk judges its one reading
    and is done) never applies the landing, so it stands on the fork; a
    walk that waits sees the filter as the kernel would show it: the same
    pid, a landed argv. The arms patch `beacons.time.sleep` because that is
    the seam's one tick; `line_holder` calls no other clock.
    """
    def sleep(s):
        plant()
    return sleep


# A copier that passes lines when it is told to, as a python filter would.
COPY = ("import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write(line)\n")


def _pipeline(reader, wrap="{probe}"):
    """The probe's stdout piped into `reader`, as the Monitor shell builds it.
    `wrap` puts the probe inside a wrapper that forks (`timeout`, `sh -c`,
    a brace group), which moves the filter out of the probe's siblings."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "probe.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(PROBE.replace("; ", "\n") + "\n")
        probe = "%s -S %s" % (sys.executable, path)
        cmd = "%s | %s" % (wrap.format(probe=probe), reader)
        out = subprocess.run(["bash", "-c", cmd], capture_output=True,
                             text=True, timeout=60)
    return out.stdout.strip(), out.stderr


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-deliverytruth-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        prior = {k: os.environ.get(k) for k in ENV_KEYS}

        def restore():
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_EVENT_DIR": os.path.join(self.tmp, "ev"),
            "HELM_ADOPTED_DIR": os.path.join(self.tmp, "adopted"),
            "HELM_CHAT_ROOM": "main",
            "HELM_CHAT_NODE_URL": "",
            "HELM_CHAT_OWNER_NAMES": "daria",
            "HELM_BEACON_ORPHAN_EXIT": "0",
            # The stop-guard arm must never reap real harness scratch.
            "HELM_SCRATCH_GC": "0",
            "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cfg-home")})
        os.makedirs(os.path.join(self.tmp, "cfg-home", "sessions"))
        homes = mock.patch.object(sessions, "cred_homes", return_value=[])
        homes.start()
        self.addCleanup(homes.stop)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        chat._ensure_dir()

    def join(self):
        with contextlib.redirect_stderr(io.StringIO()):
            seats_join.join(session=SID, cwd=self.tmp, seat=SEAT, room="main")

    def lead_delivers(self):
        """The lead's next ordinary boundary: what it is shown, if anything."""
        landed = []
        seats.deliver_any(session=SID, seat=SEAT, emit=landed.append,
                          sink_usable=True)
        return landed


class TheBeaconsFilterTest(Base):
    """Measured on REAL processes and a REAL pipe: a planted proc root would
    agree with any implementation."""

    def test_cut_on_the_beacons_pipe_holds_the_line(self):
        got, err = _pipeline("cut -c1-500")
        self.assertEqual(got, "cut", err)

    def test_a_holder_behind_a_pass_through_reader_still_holds(self):
        # `| cat | cut`: the first reader passes the line on, the second holds
        # it, and the Monitor reads only what leaves the last stage.
        got, err = _pipeline("(exec -a cat %s) | cut -c1-500" % PASS)
        self.assertEqual(got, "cut", err)

    def test_grep_passes_lines_only_with_line_buffered(self):  # noqa: VACUOUS_ASSERTION — the held arm below reads a non-empty name on the same reader, so the None here is not the empty answer of a probe that saw nothing
        got, err = _pipeline("(exec -a grep %s --line-buffered)" % PASS)
        self.assertEqual(got, "None", err)
        held, err = _pipeline("(exec -a grep %s)" % PASS)
        self.assertEqual(held, "grep", err)

    def test_sed_passes_lines_only_unbuffered(self):  # noqa: VACUOUS_ASSERTION — the held arm below is the positive control on the same reader
        got, err = _pipeline("(exec -a sed %s -u)" % PASS)
        self.assertEqual(got, "None", err)
        held, err = _pipeline("(exec -a sed %s)" % PASS)
        self.assertEqual(held, "sed", err)

    # M1 of the cross-family read: a wrapper that forks puts the filter
    # outside the waiter's siblings, and the sibling-only walk read None.
    def test_a_filter_behind_a_forking_wrapper_still_holds(self):
        for wrap in ("timeout 30 {probe}", "sh -c 'cd /tmp; {probe}'",
                     "{{ cd /tmp; {probe}; }}"):
            with self.subTest(wrap=wrap):
                got, err = _pipeline("cut -c1-500", wrap=wrap)
                self.assertEqual(got, "cut", err)

    def test_the_plain_and_subshell_forms_stay_caught(self):
        for wrap, reader in (("{probe}", "cut -c1-500"),
                             ("({probe})", "cut -c1-500"),
                             ("{probe}", "{ cut -c1-500; }")):
            with self.subTest(wrap=wrap, reader=reader):
                got, err = _pipeline(reader, wrap=wrap)
                self.assertEqual(got, "cut", err)

    def test_a_forking_wrapper_in_the_reader_position_is_looked_through(self):
        got, err = _pipeline(
            "timeout 60 bash -c %s" % _q("exec -a grep %s --line-buffered" % PASS))
        self.assertEqual(got, "None", err)
        held, err = _pipeline("timeout 60 cut -c1-500")
        self.assertEqual(held, "cut", err)

    def test_a_reader_execs_its_filter_after_the_walk_reads_it(self):
        # task/3411, MEASURED under the 57-module sliced gate: the stage
        # reading the probe's pipe had FORKED but not yet EXECD its
        # filter when the walk first read it, so the walk judged the
        # forking shell and answered None ('None' != 'cut'). This arm
        # freezes that window the way the kernel presents it: the
        # reader's cmdline is a regular file holding the PRE-EXEC argv,
        # and the landing — a pid's argv changes in place, exactly once,
        # at that exec — is applied the moment the walk waits at all
        # (the cure's one settle). A walk that never waits (the base)
        # stands on the fork: it follows the shell's fd 1 to the
        # downstream pipe, which only the waiter's own harness reads —
        # a chain member, never a holder here — and answers None.
        from helm import beacons
        tmp = tempfile.mkdtemp(prefix="helm-test-land-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = os.path.join(tmp, "proc")
        p1_r, p1_w = os.pipe()
        p2_r, p2_w = os.pipe()
        for fd in (p1_r, p1_w, p2_r, p2_w):
            self.addCleanup(os.close, fd)
        for pid, ppid, st, comm in ((1, 1, 1, "bash"),
                                    (100, 1, 2, "python3"),
                                    (200, 1, 3, "bash")):
            d = os.path.join(root, str(pid))
            os.makedirs(d)
            with open(os.path.join(d, "stat"), "w") as f:
                f.write(tb._stat(pid, st, comm, ppid))
        with open(os.path.join(root, "200", "cmdline"), "wb") as f:
            f.write(b"bash\0-c\0exec cut -c1-500\0")
        os.makedirs(os.path.join(root, "100", "fd"))
        os.symlink("/dev/fd/%d" % p1_w, os.path.join(root, "100", "fd", "1"))
        os.makedirs(os.path.join(root, "200", "fd"))
        os.symlink("/dev/fd/%d" % p1_r, os.path.join(root, "200", "fd", "0"))
        os.symlink("/dev/fd/%d" % p2_w, os.path.join(root, "200", "fd", "1"))
        os.makedirs(os.path.join(root, "200", "fdinfo"))
        for fd, flags in (("0", b"02"), ("1", b"01")):
            with open(os.path.join(root, "200", "fdinfo", fd), "wb") as f:
                f.write(b"pos:\t0\nflags:\t" + flags + b"\n")
        os.makedirs(os.path.join(root, "1", "task", "1"))
        with open(os.path.join(root, "1", "task", "1", "children"), "w") as f:
            f.write("100 200\n")

        def land():
            with open(os.path.join(root, "200", "cmdline"), "wb") as f:
                f.write(b"cut\0-c1-500\0")
        with mock.patch.object(beacons.time, "sleep", _seam(land)):
            got = beacons.line_holder(100, 1, proc_dir=root)
        self.assertEqual(got, "cut",
                         "the reader execs its filter after the walk's first "
                         "reading; the walk's one re-read settles it")

    def test_a_wrapper_that_forks_its_reader_after_the_walk_looks_past_it(self):
        # task/3411, MEASURED, the same landing-lag one generation down:
        # the wrapper's DESCENT ran before the wrapper had FORKED its
        # reader, so the descent found no child holding the pipe's read
        # end, the wrapper was followed blind, and the walk's answer
        # stood on the fork not having landed. The planted root freezes
        # that window the way the kernel presents it: the wrapper's
        # children list reads EMPTY on the descent's first read, and the
        # fork — the reader holding the same read end, the shape
        # `timeout 60 cut` has — appears the moment the walk waits at
        # all. A walk that never waits (the base) follows the wrapper's
        # downstream pipe to the waiter's own harness, a chain member
        # never named here, and answers None.
        from helm import beacons
        tmp = tempfile.mkdtemp(prefix="helm-test-land-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = os.path.join(tmp, "proc")
        p1_r, p1_w = os.pipe()
        p3_r, p3_w = os.pipe()
        for fd in (p1_r, p1_w, p3_r, p3_w):
            self.addCleanup(os.close, fd)
        for pid, ppid, st, comm, argv in ((1, 1, 1, "bash",
                                          (b"bash", b"-c")),
                                         (100, 1, 2, "python3", None),
                                         (200, 1, 3, "bash",
                                          (b"bash", b"-c"))):
            d = os.path.join(root, str(pid))
            os.makedirs(d)
            with open(os.path.join(d, "stat"), "w") as f:
                f.write(tb._stat(pid, st, comm, ppid))
            if argv is not None:
                with open(os.path.join(d, "cmdline"), "wb") as f:
                    f.write(b"\0".join(argv) + b"\0")
        for pid, fd, target, flags in ((1, "1", p3_r, b"02"),
                                       # the harness reads P3
                                       (200, "0", p1_r, b"02"),
                                       # the wrapper reads P1
                                       (200, "1", p3_w, b"01"),
                                       # ...and writes P3
                                       (100, "1", p1_w, b"01")):
            d = os.path.join(root, str(pid))
            os.makedirs(os.path.join(d, "fd"), exist_ok=True)
            os.symlink("/dev/fd/%d" % target, os.path.join(d, "fd", fd))
            os.makedirs(os.path.join(d, "fdinfo"), exist_ok=True)
            with open(os.path.join(d, "fdinfo", fd), "wb") as f:
                f.write(b"pos:\t0\nflags:\t" + flags + b"\n")
        os.makedirs(os.path.join(root, "1", "task", "1"))
        with open(os.path.join(root, "1", "task", "1", "children"), "w") as f:
            f.write("100 200\n")
        os.makedirs(os.path.join(root, "200", "task", "200"))
        with open(os.path.join(root, "200", "task", "200", "children"),
                  "w") as f:
            f.write("")

        def fork_child():
            with open(os.path.join(root, "200", "task", "200",
                                   "children"), "w") as f:
                f.write("300\n")
            d = os.path.join(root, "300")
            os.makedirs(d)
            with open(os.path.join(d, "stat"), "w") as f:
                f.write(tb._stat(300, 4, "cut", 200))
            with open(os.path.join(d, "cmdline"), "wb") as f:
                f.write(b"cut\0-c1-500\0")
            os.makedirs(os.path.join(d, "fd"))
            os.symlink("/dev/fd/%d" % p1_r, os.path.join(d, "fd", "0"))
            os.symlink("/dev/fd/%d" % p3_w, os.path.join(d, "fd", "1"))
            os.makedirs(os.path.join(d, "fdinfo"))
            for fd, flags in (("0", b"02"), ("1", b"01")):
                with open(os.path.join(d, "fdinfo", fd), "wb") as f:
                    f.write(b"pos:\t0\nflags:\t" + flags + b"\n")
        with mock.patch.object(beacons.time, "sleep", _seam(fork_child)):
            got = beacons.line_holder(100, 1, proc_dir=root)
        self.assertEqual(got, "cut",
                         "the wrapper's reader is forked after the "
                         "descent's first reading; the walk's one "
                         "re-descent finds it")

    def test_a_reader_still_its_fork_stays_followed(self):  # noqa: VACUOUS_ASSERTION — the held arms above are the unconditional positive control on the same line_holder observable; this pin says the settle must not invent one
        # The settle must never INVENT a holder: a reader that is still its
        # forking shell when re-read is followed as before, never named.
        from helm import beacons
        tmp = tempfile.mkdtemp(prefix="helm-test-land-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = os.path.join(tmp, "proc")
        rfd, wfd = os.pipe()
        self.addCleanup(os.close, rfd)
        self.addCleanup(os.close, wfd)
        for pid, ppid, st, comm, argv in ((1, 1, 1, "bash", None),
                                          (100, 1, 2, "python3", None),
                                          (200, 1, 3, "sh", ("sh", "-c"))):
            d = os.path.join(root, str(pid))
            os.makedirs(d)
            with open(os.path.join(d, "stat"), "w") as f:
                f.write(tb._stat(pid, st, comm, ppid))
            if argv is not None:
                with open(os.path.join(d, "cmdline"), "wb") as f:
                    f.write(b"\0".join(a.encode() for a in argv) + b"\0")
        for pid, fd in ((100, wfd), (200, rfd)):
            os.mkdir(os.path.join(root, str(pid), "fd"))
            os.symlink("/dev/fd/%d" % fd,
                       os.path.join(root, str(pid), "fd", "1"))
        os.mkdir(os.path.join(root, "200", "fdinfo"))
        with open(os.path.join(root, "200", "fdinfo", "1"), "wb") as f:
            f.write(b"pos:\t0\nflags:\t02\n")
        os.makedirs(os.path.join(root, "1", "task", "1"))
        with open(os.path.join(root, "1", "task", "1", "children"), "w") as f:
            f.write("100 200\n")
        with mock.patch.object(beacons.time, "sleep",
                               _seam(lambda: None)):
            got = beacons.line_holder(100, 1, proc_dir=root)
        self.assertIsNone(got, "a still-forking reader is followed, "
                               "never named a holder")

    def test_an_empty_walk_is_reused_briefly_and_then_expires(self):
        # optional (b): the process-table walk may not answer "nobody" for
        # ever, or a filter spliced in after the first ask is never seen.
        # Three asks with no near reader: one walk, an ask one second later
        # that reuses it, and an ask an hour later that walks again.
        from helm import beacons
        walks = []

        def holding(key, pids, root):
            if pids:
                walks.append(len(pids))
            return []
        clock = iter([100.0, 101.0, 3700.0])
        fake_time = types.SimpleNamespace(monotonic=lambda: next(clock))
        beacons._READER_MEMO.clear()
        with mock.patch.object(beacons, "_holding", holding), \
                mock.patch.object(beacons, "time", fake_time):
            got = [beacons._readers((1, 2), [os.getpid()]) for _ in range(3)]
        beacons._READER_MEMO.clear()
        self.assertEqual(got, [[], [], []])
        self.assertEqual(len(walks), 2, walks)

    def test_a_destination_moved_during_the_reader_walk_is_judged_afresh(self):  # noqa: VACUOUS_ASSERTION — the verdicts are asserted, None unmoved and False moved on the same pipe; the spy only counts that the walk ran
        # The walk sits between the reading and the spend. A harness that
        # moves fd 1 to the discard DURING the walk must get False, not the
        # verdict on the pipe the walk started from.
        from helm import beacons
        rfd, wfd = os.pipe()
        null = os.open(os.devnull, os.O_WRONLY)
        dest = os.fdopen(os.dup(wfd), "w")
        moved = []

        def walk(pid, fd=1, proc_dir=None):
            os.dup2(null, fd)
            moved.append(fd)
            return None
        try:
            # MUST-HIT: unmoved, the same pipe is not refused
            before = beacons.sink_usable_for(dest, follower=True)
            with mock.patch.object(beacons, "line_holder", walk):
                got = beacons.sink_usable_for(dest, follower=True)
        finally:
            dest.close()
            for fd in (rfd, wfd, null):
                os.close(fd)
        self.assertIsNone(before)
        self.assertEqual(len(moved), 1, "MUST-HIT: the walk ran on the pipe")
        self.assertIs(got, False)

    def test_a_shell_loop_that_prints_each_line_passes(self):  # noqa: VACUOUS_ASSERTION — the brace-group arm above reads "cut" through the same shell-reader path, so None here is the loop passing, not a walk that saw nothing
        got, err = _pipeline("while IFS= read -r l; do printf '%s\\n' \"$l\"; done")
        self.assertEqual(got, "None", err)

    # LOW 2: flags are parsed, and only a measured pass admits a reader.
    def test_flags_are_parsed_not_matched_as_words(self):
        for reader, want in (
                ("(exec -a grep %s -e --line-buffered)" % PASS, "grep"),
                ("(exec -a grep %s --line-buffered -e x)" % PASS, "None"),
                ("(exec -a sed %s -nu)" % PASS, "None"),
                ("(exec -a sed %s -n)" % PASS, "sed"),
                ("(exec -a sed %s -iu)" % PASS, "sed")):
            with self.subTest(reader=reader):
                got, err = _pipeline(reader)
                self.assertEqual(got, want, err)

    def test_stdbuf_mawk_and_python_pass_only_where_measured(self):
        py = sys.executable
        for reader, want in (
                ("stdbuf -oL sed s/x/y/", "None"),
                ("stdbuf -o0 sed s/x/y/", "None"),
                ("stdbuf -oL cut -c1-500", "cut"),
                ("stdbuf -oL mawk '{print}'", "mawk"),
                ("mawk -W interactive '{print}'", "None"),
                ("mawk '{print}'", "mawk"),
                ("%s -u -S -c %s" % (py, _q(COPY)), "None"),
                ("env PYTHONUNBUFFERED=1 %s -S -c %s" % (py, _q(COPY)), "None"),
                ("%s -S -c %s -u" % (py, _q(COPY)), os.path.basename(py))):
            with self.subTest(reader=reader):
                got, err = _pipeline(reader)
                self.assertEqual(got, want, err)

    def follower(self, reader, prelude=""):
        """Arm a real follower with its stdout piped into `reader`, wait for it
        to spend the row or exit, then end the pipeline the way a Monitor's
        deadline does: SIGTERM to the whole group, buffers included.
        `prelude` is child code run before the follower arms.
        -> what the Monitor saw."""
        child = ("import sys; sys.path.insert(0, %r); %s"
                 "from helm import seats_join; "
                 "seats_join.wait(seat=%r, session=%r, follow=True, "
                 "timeout=20, poll=0.05)" % (REPO, prelude, SEAT, SID))
        before = seats_delivery._cursor("main", SEAT, SID, beacon=True)
        proc = subprocess.Popen(
            ["bash", "-c", "%s -c %s | %s" % (sys.executable, _q(child), reader)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        end = time.time() + 20
        while time.time() < end and proc.poll() is None:
            now = seats_delivery._cursor("main", SEAT, SID, beacon=True)
            if now is not None and before is not None \
                    and now.get("off") != before.get("off"):
                time.sleep(0.3)         # the line is written after the commit
                break
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            out, _err = proc.communicate(timeout=30)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)   # leave no stage behind
        return out.decode("utf-8", "replace")

    def test_a_follower_piped_into_cut_never_spends_the_row(self):
        self.join()
        chat.post(ROW, room="main", who="reviewer")
        seen = self.follower("cut -c1-500")
        self.assertNotIn(ROW, seen, "cut cannot have passed the line on")
        shown = self.lead_delivers()
        self.assertEqual(len([x for x in shown if ROW in x]), 1,
                         "the follower consumed the row into cut's buffer and "
                         "the Monitor's deadline destroyed it: the lead never "
                         "sees it (Monitor saw: %r)" % seen)
        self.assertIn("REFUSING this beacon", seen)

    def test_a_follower_piped_into_cat_shows_the_row_once(self):
        # MUST-PASS: a reader measured to pass lines keeps the beacon armed,
        # the Monitor sees the row, and the row is consumed exactly once.
        self.join()
        chat.post(ROW, room="main", who="reviewer")
        seen = self.follower("cat")
        self.assertEqual(seen.count(ROW), 1, seen)
        self.assertEqual([x for x in self.lead_delivers() if ROW in x], [],
                         "a row the Monitor showed was delivered again")

    def test_the_wake_line_is_whole_before_its_row_is_spent(self):
        # THE HOST-DEPENDENT RED ABOVE, MADE DETERMINISTIC. The arm above
        # failed on two of three fab nodes. The per-row wake line names the
        # rows the seat owes, and that clause was computed AFTER the wake
        # cursor committed. Its first call imports helm.dispatches and about
        # 80 modules; a fab job starts with no bytecode, so the import
        # compiles them: 0.58-0.63 s on a slower node, 0.21 s on the fastest
        # (measured). The Monitor's SIGTERM, 0.3 s after the commit, landed
        # between the commit and the line on each slower node. The row was
        # consumed and never shown. This plants that slow first reading on
        # every host, so the arm cannot depend on the host's speed.
        slow = ("import time; from helm import stopfacts; "
                "_read = stopfacts.read; "
                "stopfacts.read = lambda *a, **k: "
                "(time.sleep(1.5), _read(*a, **k))[1]; ")
        self.join()
        chat.post(ROW, room="main", who="reviewer")
        seen = self.follower("cat", prelude=slow)
        self.assertEqual(seen.count(ROW), 1,
                         "the wake cursor committed before the wake line was "
                         "composed, and the Monitor's deadline killed the "
                         "follower between the two (Monitor saw: %r)" % seen)
        self.assertEqual([x for x in self.lead_delivers() if ROW in x], [],
                         "a row the Monitor showed was delivered again")


# A REAL SUBAGENT PostToolUse PAYLOAD SHAPE, captured from Claude Code (see
# tests/test_join_atomic.py): agent_id and agent_type sit beside the lead's own
# session_id. The lead's payloads carry neither key.
AGENT = {"agent_id": "a1f0afed187ccb124", "agent_type": "general-purpose"}


def _payload(big=False, agent=False, cwd=None):
    body = {"session_id": SID, "cwd": cwd, "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_response": {"originalFile": "x" * (100 * 1024 if big else 8)}}
    if agent:
        body.update(AGENT)
    return json.dumps(body)


class TheWholePayloadTest(Base):
    """The delivery hook on a stdin shaped exactly as the installed pair hands
    it: an in-memory stream carrying the harness's whole document. The seat's
    name is declared, as it is in every live pane, so an unparsed payload
    resolves the seat and reaches the SEAT-level cursor."""

    def setUp(self):
        super().setUp()
        os.environ["HELM_CHAT_NAME"] = SEAT
        self.join()

    def hook(self, text):
        out = []
        stdin = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")),
                                 encoding="utf-8")
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(seats_cli, "_hook_emit",
                                  lambda event: out.append), \
                contextlib.redirect_stderr(io.StringIO()):
            seats_cli.cmd("deliver", ["--hook-json"])
        return [line for line in out if line and ROW in line]

    def seat_level(self):
        return seats_delivery._cursor("main", SEAT)

    def test_a_large_lead_payload_delivers_once_on_the_sessions_cursor(self):
        chat.post(ROW, room="main", who="reviewer")
        seat_before = self.seat_level()
        first = self.hook(_payload(big=True, cwd=self.tmp))
        again = self.hook(_payload(cwd=self.tmp))
        self.assertEqual(len(first + again), 1,
                         "the lead saw the row %d times across two boundaries "
                         "(a large payload read as `{}` delivered it on the "
                         "seat-level cursor, and the session's cursor "
                         "delivered it again)" % len(first + again))
        self.assertEqual(len(first), 1, "the large payload's boundary missed it")
        self.assertEqual(self.seat_level(), seat_before,
                         "the lead's delivery moved the seat-level cursor")

    def test_a_large_subagent_payload_delivers_nothing(self):  # noqa: VACUOUS_ASSERTION — the lead's later boundary must carry the row exactly once, a non-empty positive on the same reader and room
        chat.post(ROW, room="main", who="reviewer")
        seat_before = self.seat_level()
        self.assertEqual(self.hook(_payload(big=True, agent=True, cwd=self.tmp)),
                         [], "the lead's row was delivered into a subagent")
        self.assertEqual(self.seat_level(), seat_before)
        self.assertEqual(len(self.hook(_payload(cwd=self.tmp))), 1,
                         "the lead's next boundary must still get the row")

    def test_a_small_subagent_payload_delivers_nothing(self):  # noqa: VACUOUS_ASSERTION — the lead's later boundary must carry the row exactly once, an unconditional positive on the same reader
        # MUST-PASS control: the fence already held for a readable payload.
        chat.post(ROW, room="main", who="reviewer")
        self.assertEqual(self.hook(_payload(agent=True, cwd=self.tmp)), [])
        self.assertEqual(len(self.hook(_payload(cwd=self.tmp))), 1)

    def test_a_payload_that_does_not_parse_spends_nothing(self):  # noqa: VACUOUS_ASSERTION — the lead's next readable boundary must carry the row exactly once, a non-empty positive on the same reader and room
        chat.post(ROW, room="main", who="reviewer")
        seat_before = self.seat_level()
        session_before = seats_delivery._cursor("main", SEAT, SID)
        self.assertEqual(self.hook('{"session_id": "%s", "tool_name": ' % SID),
                         [], "an unreadable payload cannot prove it is the lead")
        self.assertEqual(self.seat_level(), seat_before)
        self.assertEqual(seats_delivery._cursor("main", SEAT, SID),
                         session_before)
        self.assertEqual(len(self.hook(_payload(cwd=self.tmp))), 1)

    def test_an_empty_payload_spends_nothing(self):  # noqa: VACUOUS_ASSERTION — the lead's next readable boundary must carry the row exactly once, a non-empty positive on the same reader and room
        # LOW 3: empty stdin names no thread and no session either.
        chat.post(ROW, room="main", who="reviewer")
        seat_before = self.seat_level()
        self.assertEqual(self.hook(""), [])
        self.assertEqual(self.seat_level(), seat_before)
        self.assertEqual(len(self.hook(_payload(cwd=self.tmp))), 1)

    def test_a_large_payload_on_a_real_descriptor_is_read_whole(self):
        # The standalone hook reads a real fd, not the pair's in-memory stream.
        with tempfile.TemporaryFile() as f:
            f.write(_payload(big=True, agent=True, cwd=self.tmp).encode())
            f.seek(0)
            with mock.patch.object(sys, "stdin", types.SimpleNamespace(buffer=f)):
                got = seats._hook_stdin()
        self.assertEqual((got["session_id"], got["agent_id"]),
                         (SID, AGENT["agent_id"]))


class TheDeadlineTest(Base):
    """The pair's real SIGALRM handler, fired at the two places that decide
    whether a timed-out delivery loses its row."""

    def setUp(self):
        super().setUp()
        self.join()
        chat.post(ROW, room="main", who="reviewer")
        prior = signal.signal(signal.SIGALRM, hookrun._alarm)
        self.addCleanup(signal.signal, signal.SIGALRM, prior)

    def expire_after(self, module, name, when=lambda *a, **k: True):
        """Deliver the deadline right after `module.name` returns: the moment
        an uninterruptible call (an fsync) comes back past the budget."""
        real = getattr(module, name)

        def late(*args, **kwargs):
            out = real(*args, **kwargs)
            if when(*args, **kwargs):
                os.kill(os.getpid(), signal.SIGALRM)
            return out
        return mock.patch.object(module, name, late)

    def hook_pass(self):
        landed = []
        with contextlib.suppress(hookrun._Timeout):
            seats.deliver_any(session=SID, seat=SEAT, emit=landed.append,
                              channel="hook")
        return [x for x in landed if ROW in x]

    def test_a_deadline_after_the_commit_still_shows_the_row(self):
        committed = lambda _path, row: row.get("state") == "committed"  # noqa: E731
        with self.expire_after(seats_cursor, "_publish_cursor_outcome",
                               committed):
            shown = self.hook_pass()
        later = [x for x in self.lead_delivers() if ROW in x]
        self.assertEqual(len(shown) + len(later), 1,
                         "the deadline landed between the cursor commit and "
                         "the emit: the row was consumed and never shown")
        self.assertEqual(len(shown), 1)

    def test_a_deadline_before_the_commit_leaves_the_row_for_the_beacon(self):  # noqa: VACUOUS_ASSERTION — the beacon pass must carry the row exactly once, a non-empty positive on the same room
        # MUST-PASS: a pass that times out before it commits spends nothing,
        # so the beacon still has the row to wake the seat with.
        with self.expire_after(seats_delivery, "_tail"):
            self.assertEqual(self.hook_pass(), [])
        woken = []
        seats.deliver_any(session=SID, seat=SEAT, emit=woken.append,
                          ambient=False, channel="beacon", sink_usable=True)
        self.assertEqual(len([x for x in woken if ROW in x]), 1)


class TheBoundaryCarriesPeopleNotMachinesTest(Base):
    """task/2980 lane 7: the tool-boundary hook keeps the seat's home room,
    less the plain rows a SUBSYSTEM posts. The designer measured machine
    room broadcasts at 46.5% of all delivered bytes. A plain row from the
    owner or a seat still reaches every seat homed there at its next tool
    call; a proxywatch row is pulled with `helm chat read`. The stop-guard
    and the pending counts read the same rule, so a Stop never blocks on a
    row the hook skipped."""

    def setUp(self):
        super().setUp()
        os.environ["HELM_CHAT_NAME"] = SEAT
        self.join()

    def boundary(self):
        out = []
        stdin = io.TextIOWrapper(io.BytesIO(_payload(cwd=self.tmp).encode()),
                                 encoding="utf-8")
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(seats_cli, "_hook_emit",
                                  lambda event: out.append), \
                contextlib.redirect_stderr(io.StringIO()):
            seats_cli.cmd("deliver", ["--hook-json"])
        return out

    def test_the_owners_plain_row_reaches_the_home_room(self):
        # The failing input the cross-family read named: an owner ruling
        # with no @ must still reach every seat homed in the room.
        chat.post("hold all lands until I say", room="main", who="daria",
                  origin="web")
        got = self.boundary()
        self.assertEqual(len(got), 1, got)
        self.assertIn("hold all lands until I say", got[0])

    def test_a_seats_plain_row_reaches_the_home_room(self):
        chat.post("seat-a: the train is gating on top", room="main",
                  who="seat-a")
        got = self.boundary()
        self.assertEqual(len(got), 1, got)
        self.assertIn("the train is gating on top", got[0])

    def test_a_machine_broadcast_is_pulled_and_never_pushed(self):  # noqa: VACUOUS_ASSERTION — the seat's row posted after it must reach the same boundary, a non-empty positive on the same reader and room
        for who in ("proxywatch", "beacons", "autocompact", "worktree-gc", "teams"):
            chat.post("%s: state CHANGED" % who, room="main", who=who)
        chat.post("seat-a: after the machines", room="main", who="seat-a")
        got = self.boundary()
        self.assertEqual(len(got), 1, got)
        self.assertIn("after the machines", got[0])
        self.assertEqual(self.boundary(), [], "a machine row reached the lead")
        rows, _total = chat.read("main")
        self.assertEqual(sum("state CHANGED" in str(r.get("text"))
                             for r in rows), 5, "the rows stay readable")

    def test_a_machine_broadcast_is_still_addressed_by_at_all(self):
        chat.post("@all proxywatch: every seat must re-arm", room="main",
                  who="proxywatch")
        got = self.boundary()
        self.assertEqual(len(got), 1, got)
        self.assertIn("every seat must re-arm", got[0])

    def test_a_stop_does_not_block_on_a_row_the_hook_skipped(self):
        # LOW 1: the stop-guard's inbox rung reads the same rule. MUST-HIT
        # first, on the same guard: a seat's plain row still blocks.
        chat.post("seat-a: a real obligation", room="main", who="seat-a")
        blocks, _warns = seats.stop_guard(session=SID, seat=SEAT)
        self.assertTrue(blocks, "control: a person's plain row must block")
        self.assertEqual(len(self.boundary()), 1)
        chat.post("proxywatch: proxy health CHANGED", room="main",
                  who="proxywatch")
        blocks, _warns = seats.stop_guard(session=SID, seat=SEAT)
        self.assertFalse(blocks, "a Stop blocked on a machine row: %r"
                         % (blocks,))

    # Every post/dm site whose sender the walk cannot resolve to a literal,
    # by (file, expression). Each is seat- or owner-valued. A NEW site that
    # the walk cannot resolve fails the arm until it is named here or given
    # a literal subsystem label.
    UNRESOLVED_SENDERS = frozenset((
        ("helm/chat.py", "seat"),
        ("helm/dispatches.py", "row.get('acted_by') or row.get('sender')"),
        ("helm/dispatches.py", "sender"),
        ("helm/gate.py", "holder"),
        ("helm/human.py", "operator_name()"),
        ("helm/mcpd.py", "seat"),
        ("helm/meld.py", "seat"),
        ("helm/obligation.py", "who"),
        ("helm/ownerasks.py", "_verdict_author(r) or seats.owner_name()"),
        ("helm/ownerasks.py", "row['asker']"),
        ("helm/ownerasks.py", "who"),
        ("helm/remote_relay.py", "seat_name"),
        ("helm/seats_ack.py", "seat"),
        ("helm/seats_catchup.py", "seat"),
        ("helm/seats_cli.py", "actor"),
        ("helm/seats_cli.py", "sender"),
        ("helm/seats_delivery.py", "sender"),
        ("helm/seats_delivery.py", "who"),
        ("helm/takeover.py", "record['successor']"),
        ("helm/telegram.py", "who"),
        ("helm/todos.py", "seat"),
        ("helm/web_chat.py", "str(payload.get('name') or seats.owner_name())"),
    ))

    def test_every_label_helm_posts_under_is_a_known_subsystem(self):
        """The set is derived from the tree, and this keeps it so: a new
        literal sender at a post or dm call site must be named in
        machine_senders, or its plain rows would be pushed to every seat.
        The sender is read as `who=` or as the third positional argument,
        through module constants (plain or annotated); anything else, a
        `**kw` splat included, is UNRESOLVED and must be named above."""
        import ast
        import glob
        from helm import machine_senders
        found, unresolved = {}, set()
        for path in sorted(glob.glob(os.path.join(REPO, "helm", "**", "*.py"),
                                     recursive=True)):
            rel = os.path.relpath(path, REPO)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            consts = {}
            for n in tree.body:
                targets = (n.targets if isinstance(n, ast.Assign) else
                           [n.target] if isinstance(n, ast.AnnAssign) else [])
                value = getattr(n, "value", None)
                for t in targets:
                    if isinstance(t, ast.Name) and isinstance(value, ast.Constant) \
                            and isinstance(value.value, str):
                        consts[t.id] = value.value
            for node in ast.walk(tree):
                fn = getattr(node, "func", None)
                name = getattr(fn, "attr", None) or getattr(fn, "id", "")
                if not isinstance(node, ast.Call) \
                        or name not in ("post", "dm", "_dm_canonical"):
                    continue
                exprs = [kw.value for kw in node.keywords if kw.arg == "who"]
                exprs += node.args[2:3]
                for kw in node.keywords:
                    if kw.arg is None:
                        unresolved.add((rel, "**" + ast.unparse(kw.value)))
                for v in exprs:
                    label = (v.value if isinstance(v, ast.Constant) else
                             consts.get(v.id) if isinstance(v, ast.Name)
                             else None)
                    if isinstance(label, str):
                        found.setdefault(label, set()).add(rel)
                    else:
                        unresolved.add((rel, ast.unparse(v)))
        self.assertIn("proxywatch", found, "control: the walk saw a label")
        self.assertIn("worktree-gc", found, "the gc summary names itself")
        missing = {k: sorted(v) for k, v in found.items()
                   if not machine_senders.is_machine(k)}
        self.assertEqual(missing, {})
        self.assertEqual(unresolved, set(self.UNRESOLVED_SENDERS))

    # MUST-CURE 1 of the second read: the beacon wins the race at every seat.
    def beacon_pass(self):
        woken = []
        seats.deliver_any(session=SID, seat=SEAT, emit=woken.append,
                          ambient=False, channel="beacon", sink_usable=True)
        return woken

    def test_the_owners_ruling_survives_a_beacon_pass_and_reaches_once(self):
        chat.post("hold all lands until I say", room="main", who="daria",
                  origin="web")
        self.assertEqual(self.beacon_pass(), [], "a plain row woke the seat")
        got = self.boundary()
        self.assertEqual(len(got), 1, got)
        self.assertIn("hold all lands until I say", got[0])
        self.assertEqual(self.boundary(), [], "delivered twice")

    def test_the_owner_is_never_inferred_from_a_rail_or_a_name_alone(self):  # noqa: VACUOUS_ASSERTION — the owner arm above delivers through the same beacon-then-boundary path, so these empties are the door, not a boundary that sees nothing
        # MUST-MISS: a seat stamping the web rail, and a bare CLI post under
        # the owner's name, are crossed by the beacon as any plain row is.
        chat.post("forged ruling", room="main", who="seat-a", origin="web")
        chat.post("named ruling", room="main", who="daria")
        self.assertEqual(self.beacon_pass(), [])
        self.assertEqual(self.boundary(), [])

    # LOW 2 of the second read: an unknown sessionless poster reaches.
    def test_a_bare_shell_poster_named_agent_reaches(self):
        chat.post("from a bare shell", room="main", who="agent")
        got = self.boundary()
        self.assertEqual(len(got), 1, got)
        self.assertIn("from a bare shell", got[0])


FOREIGN_SEAT = "seat-under-test"


class TheOrcaDoorOutsideHelmTest(Base):
    """A pane Orca opened in ANOTHER project gets its own mail at the tool
    boundary, and only its own mail (task/3165).

    The fleet scope skipped the delivery leg for every seat whose cwd was
    another project, so those seats' rows left only by pull. The Orca door
    now admits that leg, bounded by premise
    feedback-project-seat-context-stays-in-project: a DM, an @mention and a
    reply reach the seat, and so does its own project's room; an @all in
    #helm or #main, and another project's traffic, do not.

    Every arm crosses the real door, `cli.main` with the installed argv, the
    payload on stdin and the answer read off fd 1. Only the registry is a
    double: this fixture's checkout is helm's project and the pane's cwd is
    another project's."""

    #: The budget the hook runner gives the delivery leg: the installed
    #: spec's own timeout, which bin/helm-hook and the PostToolUse pair both
    #: enforce as wall time.
    BUDGET_S = next(s["timeout"] for s in hooks.SPECS
                    if s["name"] == "deliver" and s["event"] == "PostToolUse")

    def setUp(self):
        super().setUp()
        self.helm_root = os.path.join(self.tmp, "helm-checkout")
        self.cwd = os.path.join(self.tmp, "sitka-inc", "work")
        os.makedirs(os.path.join(self.helm_root, "bin"))
        os.makedirs(self.cwd)
        os.chdir(self.cwd)
        env = mock.patch.dict(os.environ, {
            "ORCA_PANE_KEY": "pane-key-fixture",
            "HELM_CHAT_NAME": FOREIGN_SEAT,
            "HELM_CHAT_ROOM": "sitka-inc",
            "HELM_CHAT_ROOM_SOURCE": "derived",
            # bin/helm-hook exports this for every hook it runs.
            "HELM_NO_TREE_WARNING": "1"})
        env.start()
        self.addCleanup(env.stop)
        root, self.inside = self.helm_root, False

        def project(path):
            if os.path.abspath(str(path)) == root:
                return "helm"
            return "helm" if self.inside else "sitka-inc"
        for patch in (mock.patch.object(hooks, "helm_bin", return_value=os.path.join(
                          root, "bin", "helm")),
                      mock.patch("helm.inject._ledger.project_for_cwd", project)):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertIs(hooks.outside_helm(), True,
                      "control: the double places the pane outside helm")

    def join_seat(self):
        with contextlib.redirect_stderr(io.StringIO()):
            seats_join.join(session=SID, cwd=self.cwd, seat=FOREIGN_SEAT,
                            room="sitka-inc")

    def hook(self):
        """One PostToolUse boundary: (rc, contexts, outcome, seconds)."""
        stdin = io.TextIOWrapper(io.BytesIO(_payload(cwd=self.cwd).encode()),
                                 encoding="utf-8")
        hookoutcome.begin()
        with tempfile.TemporaryFile() as fd1:
            saved = os.dup(1)
            try:
                os.dup2(fd1.fileno(), 1)
                with mock.patch.object(sys, "stdin", stdin), \
                        contextlib.redirect_stdout(io.StringIO()) as text, \
                        contextlib.redirect_stderr(io.StringIO()):
                    started = time.monotonic()
                    rc = cli.main(["chat", "deliver", "--hook-json"])
                    elapsed = time.monotonic() - started
            finally:
                os.dup2(saved, 1)
                os.close(saved)
            fd1.seek(0)
            raw = fd1.read().decode("utf-8")
        self.assertEqual(text.getvalue(), "", "the hook printed past its emitter")
        contexts = [json.loads(line)["hookSpecificOutput"]["additionalContext"]
                    for line in raw.splitlines() if line.strip()]
        return rc, contexts, hookoutcome.taken()[0], elapsed

    def drain(self, limit=8):
        """Every row the seat's boundaries hand it, one per boundary, until a
        boundary hands it nothing. Returns (shown, the quiet boundary's
        outcome)."""
        shown = []
        for _ in range(limit):
            rc, got, status, _elapsed = self.hook()
            self.assertEqual(rc, 0)
            if not got:
                return shown, status
            shown.extend(got)
        self.fail("the boundary never went quiet: %r" % shown)

    def test_a_dm_reaches_the_orca_pane_outside_helm(self):  # noqa: VACUOUS_ASSERTION — the only absence is the SKIPPED outcome, and the DM's text on the same boundaries is the unconditional positive
        self.join_seat()
        _row, err = seats.dm(FOREIGN_SEAT, "please re-arm your beacon",
                             who="seat-a")
        self.assertIsNone(err)
        shown, status = self.drain()
        self.assertNotEqual(status, hookoutcome.SKIPPED,
                            "the delivery leg was skipped at the scope door")
        self.assertEqual(len(shown), 1, shown)
        self.assertIn("please re-arm your beacon", shown[0])

    def test_an_at_all_in_helm_does_not_reach_it(self):  # noqa: VACUOUS_ASSERTION — the DM on the same drained boundaries must be shown, so the @all's absence is the narrowing and not a quiet leg
        self.join_seat()
        chat.post("@all LAND pushed: trains 251-258", room="helm",
                  who="seat-a")
        _row, err = seats.dm(FOREIGN_SEAT, "your lane is ready",
                             who="seat-a")
        self.assertIsNone(err)
        shown, _status = self.drain()
        self.assertTrue(any("your lane is ready" in s for s in shown),
                        "control: the DM on the same boundaries: %r" % shown)
        self.assertFalse([s for s in shown if "LAND pushed" in s],
                         "a #helm @all reached a seat working in another project")

    def test_an_at_all_in_main_does_not_reach_it(self):  # noqa: VACUOUS_ASSERTION — the DM on the same drained boundaries must be shown, and the arm after this one delivers the same row inside helm
        self.join_seat()
        chat.post("@all SUITE START: gatelock held", room="main",
                  who="seat-a")
        _row, err = seats.dm(FOREIGN_SEAT, "your lane is ready",
                             who="seat-a")
        self.assertIsNone(err)
        shown, _status = self.drain()
        self.assertTrue(any("your lane is ready" in s for s in shown),
                        "control: the DM on the same boundaries: %r" % shown)
        self.assertFalse([s for s in shown if "SUITE START" in s],
                         "a #main @all reached a seat working in another project")

    def test_inside_helm_the_same_at_all_in_main_still_reaches(self):
        # MUST-HIT for the arm above: the row is an @all the fleet delivers,
        # so only the narrowing keeps it from the pane in another project.
        self.inside = True
        self.join_seat()
        chat.post("@all SUITE START: gatelock held", room="main",
                  who="seat-a")
        shown, _status = self.drain()
        self.assertEqual(len(shown), 1, shown)
        self.assertIn("SUITE START", shown[0])

    def test_its_own_room_and_its_own_mentions_reach_it_and_nothing_else(self):
        self.join_seat()
        chat.post("seat-b: the migration is green", room="sitka-inc",
                  who="seat-b")
        chat.post("@%s the permalink review is yours" % FOREIGN_SEAT,
                  room="helm", who="seat-a")
        chat.post("seat-c: the preview deploy is up",
                  room="bench-dev", who="seat-c")
        chat.post("seat-a: train259 is gating", room="helm",
                  who="seat-a")
        shown, _status = self.drain()
        self.assertEqual(sorted(("migration is green" in s,
                                 "permalink review is yours" in s)
                                for s in shown),
                         [(False, True), (True, False)], shown)

    def test_nothing_addressed_is_silent_and_inside_the_budget_cold(self):  # noqa: VACUOUS_ASSERTION — silence is the contract; the outcome must not be SKIPPED, so the leg ran, and the DM arm shows the same reader speaking
        """A cold chat dir: no room, no row and no cursor exist. The pane's
        first boundary must answer inside the delivery budget and say
        nothing, whether its join ran or not."""
        self.assertEqual(os.listdir(chat.chat_dir()), [chat.STATE_SUBDIR],
                         "control: the chat dir is cold, holding only the "
                         "exhaust subdir every chat dir is minted with")
        for joined in (False, True):
            with self.subTest(joined=joined):
                if joined:
                    self.join_seat()
                rc, got, status, elapsed = self.hook()
                self.assertEqual((rc, got), (0, []))
                self.assertNotEqual(status, hookoutcome.SKIPPED,
                                    "the delivery leg was skipped, so its "
                                    "silence and its time prove nothing")
                self.assertLess(elapsed, self.BUDGET_S,
                                "the delivery leg took %.3fs of a %ss budget"
                                % (elapsed, self.BUDGET_S))

    def test_non_addressed_traffic_is_silent(self):  # noqa: VACUOUS_ASSERTION — silence is the contract; the outcome must not be SKIPPED, and the main-room arm inside helm delivers one of these rows on the same reader
        self.join_seat()
        chat.post("@all LAND pushed", room="helm", who="seat-a")
        chat.post("@all SUITE FINISH", room="main", who="seat-a")
        chat.post("seat-c: preview is up", room="bench-dev",
                  who="seat-c")
        rc, got, status, _elapsed = self.hook()
        self.assertNotEqual(status, hookoutcome.SKIPPED,
                            "the delivery leg was skipped, so its silence "
                            "proves nothing")
        self.assertEqual((rc, got), (0, []))

    def test_the_leg_carries_no_whisper_and_no_delegation_evidence(self):  # noqa: VACUOUS_ASSERTION — the same leg inside helm must emit the whisper and record the evidence on the same spies
        """The standalone leg (the one bin/helm-hook installs on a seat's
        own config dir) also speaks the per-tool-call whisper and records
        delegation evidence. Both belong to hooks that stay scoped: the
        whisper steers helm work, and the evidence is cleared only by the
        SubagentStop hook, which never runs here, so it would go stale."""
        from helm import toolwhisper
        calls = {"whisper": [], "delegation": []}

        def whisper(session):
            calls["whisper"].append(session)
            return "WHISPER: a helm steer"

        def delegation(d):
            calls["delegation"].append(d.get("session_id"))
        self.join_seat()
        with mock.patch.object(toolwhisper, "for_hook", whisper), \
                mock.patch.object(seats_cli, "_record_posttool_delegation",
                                  delegation):
            rc, got, status, _elapsed = self.hook()
            self.assertNotEqual(status, hookoutcome.SKIPPED,
                                "the delivery leg was skipped, so the "
                                "absence below proves nothing")
            self.assertEqual((rc, got), (0, []))
            self.assertEqual(calls, {"whisper": [], "delegation": []})
            # MUST-HIT: the same leg inside helm speaks and records both.
            self.inside = True
            rc, got, status, _elapsed = self.hook()
            self.assertEqual((rc, got), (0, ["WHISPER: a helm steer"]))
            self.assertEqual(calls, {"whisper": [SID], "delegation": [SID]})

    def test_stop_guard_and_delegation_stop_still_skip_for_the_pane(self):
        self.assertTrue(hooks.hook_skips_here("chat", ["stop-guard", "--hook-json"]))
        self.assertTrue(hooks.hook_skips_here(
            "chat", ["delegation-stop", "--hook-json"]))
        self.assertFalse(hooks.hook_skips_here("chat", ["deliver", "--hook-json"]))


def tearDownModule():
    """A stop that armed a surviving disclosure and never emitted it leaves
    the text queued for whatever refuses next in this process, which is
    another module's stop; drain it the way an interrupted response does
    (task/3039: the slice runner's data audit named it)."""
    from helm import seats_stop_seam
    seats_stop_seam.fallback_lines(())


if __name__ == "__main__":
    unittest.main()
