#!/usr/bin/env python3
"""A beacon must never spend an addressed row into a filter that holds it.

task/2920, MEASURED. opus-integrator armed its beacon as

    helm chat wait --seat opus-integrator --follow --replace 2>&1 \
        | grep --line-buffered -v -E '<housekeeping filter>'

and inside Claude Code's Bash tool `grep` is a SHELL FUNCTION that execs the
harness-embedded ugrep (argv0 "ugrep"). That ugrep holds every line until the
NEXT line arrives, whatever its flags, --line-buffered included: fed "L1",
three seconds, "L2", it printed L1 at 3.0s and L2 at EOF, where GNU grep
printed them at 0.0s and 3.0s. The waiter commits its cursor BEFORE it emits
(at-most-once), so the last wake line of every 30-minute Monitor window sat in
ugrep's buffer until the Monitor deadline killed the pipeline, and the row was
gone for every reader: rows 3243, 3398 and 3413, and 17 of the day's ~50
addressed #helm rows. Each earlier row arrived only when the next one pushed
it out (row 3394 reached the seat 17 minutes late, 7 seconds after row 3398).

The waiter cannot see into another process's buffer, but it CAN see who reads
its stdout pipe. A reader known to hold a line is refused at arm time (the
refusal reaches the Monitor because the waiter then exits, and EOF flushes the
filter) and fenced at every delivery (a consumer whose output is proven to be
held must not commit)."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from helm import beacons, chat, seats  # noqa: E402

ENV = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME",
       "HELM_ADOPTED_DIR", "HELM_CHAT_EVENT_DIR", "CLAUDE_CODE_SESSION_ID",
       "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "HELM_BEACON_ORPHAN_EXIT")

PROBE = ("import os, sys; sys.path.insert(0, %r); "
         "from helm import beacons; "
         "print(beacons.line_holder(os.getpid()), flush=True)" % REPO)

PASS = ("%s -S -c 'import sys, shutil; "
        "shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)'"
        % sys.executable)


def _pipeline(reader):
    """The probe's stdout piped into `reader`, as the Monitor shell builds
    it: both stages are children of one bash, the reader a sibling."""
    cmd = "%s -c %s | %s" % (sys.executable, _q(PROBE), reader)
    out = subprocess.run(["bash", "-c", cmd], capture_output=True,
                         text=True, timeout=60)
    return out.stdout.strip(), out.stderr


def _q(text):
    return "'" + text.replace("'", "'\\''") + "'"


class LineHolderDiscoveryTest(unittest.TestCase):
    """Measured on REAL processes and a REAL pipe: a planted proc root would
    agree with any implementation."""

    def test_a_sibling_reading_the_pipe_as_ugrep_is_named(self):
        # `exec -a ugrep` is the harness shim's own spelling
        # (`(exec -a ugrep "$_cc_bin" ...)`), around a reader that passes
        # lines. Python, not cat: a multicall coreutils refuses a foreign
        # argv0 ("Requested utility `ugrep` does not match").
        got, err = _pipeline("(exec -a ugrep %s)" % PASS)
        self.assertEqual(got, "ugrep", err)

    def test_a_reader_measured_to_pass_lines_is_not_a_line_holder(self):  # noqa: VACUOUS_ASSERTION — the sibling arm above is the positive control on the same pipeline shape
        got, err = _pipeline("(exec -a cat %s)" % PASS)
        self.assertEqual(got, "None", err)

    def test_a_reader_nobody_measured_holds_until_proven(self):
        # The same copier under its own name: it may pass lines, but nothing
        # PROVES it does, and a wake it holds is a row lost.
        got, err = _pipeline(PASS)
        self.assertEqual(got, os.path.basename(sys.executable), err)

    def test_a_non_pipe_stdout_has_no_holder(self):  # noqa: VACUOUS_ASSERTION — positive control is the ugrep arm; this pins the fifo precondition
        out = subprocess.run([sys.executable, "-c", PROBE], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=60)
        # the reader of THIS pipe is the test process, which is not a sibling
        self.assertEqual(out.stdout.strip(), "None", out.stderr)


class HolderFenceTest(unittest.TestCase):
    SEAT, SID = "holdsink", "s-holdsink"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-holder-")
        self.prior = {k: os.environ.get(k) for k in ENV}
        for k in ENV:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_EVENT_DIR"] = os.path.join(self.tmp, "ev")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_BEACON_ORPHAN_EXIT"] = "0"
        self.cwd_prior = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd_prior)
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def addressed(self, text):
        seats.join(session=self.SID, seat=self.SEAT, cwd=self.tmp)
        return chat.post("@%s %s" % (self.SEAT, text), who="alice")

    def _survives(self, text):
        """THE ROW'S OWN BAR: a usable consumer still receives it."""
        landed = []
        got = seats.deliver_any(session=self.SID, seat=self.SEAT,
                                emit=landed.append, sink_usable=True)
        self.assertIsNotNone(got, "the addressed row was spent into the "
                                  "line-holding filter and is gone")
        self.assertIn(text, got)

    def test_a_pipe_read_by_a_line_holder_is_PROVEN_unusable(self):  # noqa: VACUOUS_ASSERTION — assertIs(False) is a non-empty verdict and the unpatched assertIsNone is its must-hit control on the same pipe
        from helm import seats_join
        rfd, wfd = os.pipe()
        saved = os.dup(1)
        try:
            os.dup2(wfd, 1)
            with open(1, "w", closefd=False) as fd1, \
                    mock.patch.object(sys, "stdout", fd1):
                # MUST-HIT: without a holder this pipe is plain UNKNOWN
                self.assertIsNone(seats_join.destination_usable(print))
                with mock.patch.object(beacons, "line_holder",
                                       return_value="ugrep"):
                    held = seats_join.destination_usable(print)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(rfd)
            os.close(wfd)
        self.assertIs(held, False, "a stdout pipe whose reader holds each "
                                   "line was classified usable")

    def test_the_follower_refuses_to_arm_into_a_line_holder(self):  # noqa: VACUOUS_ASSERTION — assertIn(REFUSING) and _survives (a real delivery) are the positive observables; assertNotIn only pins that the row was not spent
        from helm import seats_join
        self.addressed("this must not die in a filter buffer")
        out = io.StringIO()
        with mock.patch.object(beacons, "stdout_line_holder",
                               return_value="ugrep"), \
                contextlib.redirect_stdout(out):
            got = seats_join.wait(seat=self.SEAT, session=self.SID,
                                  follow=True, timeout=0.3, poll=0.05)
        self.assertIsNone(got)
        self.assertIn("REFUSING this beacon", out.getvalue())
        self.assertIn("ugrep", out.getvalue())
        self.assertNotIn("this must not die", out.getvalue())
        self._survives("this must not die in a filter buffer")

    def test_a_follower_without_a_holder_still_streams(self):  # noqa: VACUOUS_ASSERTION — positive control for the refusal arm on the same fixture
        from helm import seats_join
        self.addressed("plain sink delivers")
        out = io.StringIO()
        with mock.patch.object(beacons, "stdout_line_holder",
                               return_value=None), \
                contextlib.redirect_stdout(out):
            seats_join.wait(seat=self.SEAT, session=self.SID, follow=True,
                            timeout=0.3, poll=0.05)
        self.assertIn("plain sink delivers", out.getvalue())


if __name__ == "__main__":
    unittest.main()
