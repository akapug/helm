#!/usr/bin/env python3
"""A READER WHO CLOSES THE TAB IS NOT A SERVER FAULT.

The console's error handler was already cured of printing a full stack for a
disconnect — and the cure was applied to `handle_error` only, leaving the same
defect standing in its sibling, the request path. A hang-up reaches that path by
two routes: during a normal response write, and during the 500 a failed handler
tries to send afterwards. Both printed a traceback.

MEASURED on the live console before this cure: 86 tracebacks in one hour, every
one of them the same four frames (`_do_get` -> `_json` -> `_send` -> `write`),
inside 1,670 journal lines. The cost is not the bytes. A log where the ordinary
act of closing a tab prints a stack is a log where a REAL fault is one stack
among ninety, and nobody reads the ninetieth.

THE PAIR BELOW IS THE WHOLE TEST. Asserting "no traceback" alone would pass just
as well against a handler that swallows everything, so the second arm asserts a
genuine fault STILL prints one. Neither arm means anything without the other.
"""
import io
import unittest
from unittest import mock

from helm import web_server


class _Probe(web_server.Handler):
    """A handler with no socket. Only `_guarded` is exercised, and the two
    methods it can reach are recorded rather than performed."""

    def __init__(self, path="/api/whoami"):
        self.path = path
        self.client_address = ("127.0.0.1", 5555)
        self.sent = []
        self._helm_wrote = False

    def _json(self, obj, status=200):
        self.sent.append((status, obj))


class AHangUpIsOneLineTest(unittest.TestCase):

    def guarded(self, boom):
        """Run `_guarded` over a dispatch that raises `boom`; return
        (stderr text, what the handler tried to send)."""
        probe = _Probe()
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            def dispatch():
                raise boom
            probe._guarded(dispatch)
        return err.getvalue(), probe.sent

    def test_a_disconnect_prints_no_stack(self):
        text, sent = self.guarded(BrokenPipeError(32, "Broken pipe"))
        self.assertNotIn(
            "Traceback", text,
            "a client hanging up still prints a full stack — this is the "
            "defect: %r" % text[:300])
        self.assertIn("hung up", text,
                      "the disconnect was swallowed entirely; it should be one "
                      "line naming the request, not silence: %r" % text[:200])
        self.assertIn("/api/whoami", text,
                      "the line does not say WHICH request, so a disconnect "
                      "that only happens on one route cannot be noticed")
        self.assertEqual(
            [], sent,
            "the server tried to write a response to a socket whose reader had "
            "already gone — that write is what raised inside the error handler "
            "and produced the stack in the first place")

    def test_a_real_fault_still_prints_its_stack(self):
        """POSITIVE CONTROL, and the arm above is worthless without it.

        A handler that quietly swallowed every exception would satisfy the
        no-traceback assertion perfectly while hiding every genuine failure."""
        text, sent = self.guarded(ValueError("a real fault"))
        self.assertIn(
            "Traceback", text,
            "a genuine handler fault no longer prints a stack, so the quiet "
            "branch has swallowed real failures along with the disconnects")
        self.assertIn("a real fault", text)
        self.assertEqual(1, len(sent), "a real fault owes the caller a 500")
        self.assertEqual(500, sent[0][0])
