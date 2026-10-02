"""`helm dispatch send` refuses two bodies (task/3510).

A message on the command line AND a piped or heredoc body means the caller
sent two and only one can travel; keeping either would report "sent" while
the other never left their shell. `chat.resolve_one_body` holds the same law
for `helm chat`. The loss is silent: a hand-back whose file carried the proof
was recorded with only its command-line summary.

The arms drive the verb in process, the way `StdinBodyDoorTest` does: a
socket pair stands in for stdin, so the fd answers the readiness select the
verb asks before it reads.
"""
import socket
import sys
import unittest
from unittest import mock

from helm import dispatches
from tests.test_dispatches import DispatchBase, run


class SendRefusesTwoBodiesTest(DispatchBase):

    def _stdin(self, body):
        """A stdin whose fd carries `body` and then EOF; b"" is a writer that
        closes without writing anything."""
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        if body:
            right.sendall(body)
        right.shutdown(socket.SHUT_WR)
        handle = left.makefile("r")
        self.addCleanup(handle.close)

        class Fd:
            def isatty(self):
                return False

            def fileno(self):
                return left.fileno()

            def read(self, *a):
                return handle.read(*a)
        return Fd()

    def _send(self, stdin, lane, *message):
        with mock.patch.object(sys, "stdin", stdin):
            return run(dispatches.cmd_dispatch, [
                "send", "reviewer", lane, *message,
                "--ref", self.a, "--repo", self.repo, "--kind", "review",
                "--new-work", "--task", self.review_task["id"], "--part"])

    def _lanes(self):
        current, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        return sorted(row.get("lane") for row in (current or {}).values())

    def test_a_message_and_a_piped_body_refuse_and_write_no_row(self):
        rc, _out, err = self._send(self._stdin(b"the file body\n"),
                                   "two-lane", "the message")
        self.assertEqual(rc, 2, err)
        self.assertIn("REFUSING", err)
        self.assertIn("both a positional message and piped/heredoc stdin", err)
        self.assertEqual(self._lanes(), [])
        # THE CONTROL on the same observable: one body on the same door is
        # sent, so the empty list above is the refusal and not a blind read.
        rc, out, err = self._send(self._stdin(b"the file body\n"), "two-lane")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._lanes(), ["two-lane"])

    def test_b_a_message_with_a_dev_null_stdin_sends(self):
        with open("/dev/null") as null:
            with mock.patch.object(sys, "stdin", null):
                rc, out, err = run(dispatches.cmd_dispatch, [
                    "send", "reviewer", "null-lane", "the message",
                    "--ref", self.a, "--repo", self.repo, "--kind", "review",
                    "--new-work", "--task", self.review_task["id"], "--part"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._lanes(), ["null-lane"])

    def test_c_a_piped_body_and_no_message_sends(self):
        rc, out, err = self._send(self._stdin(b"the file body\n"), "pipe-lane")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._lanes(), ["pipe-lane"])

    def test_d_a_message_with_an_empty_pipe_sends(self):
        rc, out, err = self._send(self._stdin(b""), "empty-lane", "the message")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._lanes(), ["empty-lane"])

    def test_e_a_message_of_several_words_and_a_piped_body_refuse(self):
        rc, _out, err = self._send(self._stdin(b"the file body\n"),
                                   "words-lane", "the", "message")
        self.assertEqual(rc, 2, err)
        self.assertIn("REFUSING", err)
        self.assertEqual(self._lanes(), [])
        rc, out, err = self._send(self._stdin(b""), "words-lane", "the", "message")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self._lanes(), ["words-lane"])


if __name__ == "__main__":
    unittest.main()
