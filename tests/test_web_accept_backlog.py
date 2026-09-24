#!/usr/bin/env python3
"""The console must make a busy moment a WAIT, never a REFUSAL.

A refused connection and a slow one are different findings everywhere except in
a browser, which renders both as a page that will not load. The owner reads the
console on a phone, cannot see a status line, and has no way to tell "this
server is working hard" from "this server is gone". So the accept backlog is
pinned here: not as a performance claim — a deeper queue makes no handler
faster — but as the claim that the failure mode under load is the honest one.

WHAT THIS ARM CANNOT DO. It does not prove kernel behaviour under real load;
that needs a live server, many simultaneous clients and a handler held open,
which is not a thing to run on a shared hub. It pins the SETTING, and it pins it
against the library default rather than against a number typed here, so that it
fails the day the default moves and the gap this exists to close stops existing.
"""
import socketserver
import unittest

from helm import web_server


class TheAcceptBacklogIsDeeperThanTheDefaultTest(unittest.TestCase):

    def test_the_library_default_is_the_shallow_one(self):
        """POSITIVE CONTROL, and the reason the number below is not arbitrary.

        The whole finding is that the inherited default is small enough for a
        polling console to overrun. If the library ever raises it, this arm
        fails and the next reader gets to decide whether helm still needs its
        own value at all — which is the question, not the constant."""
        self.assertEqual(
            5, socketserver.TCPServer.request_queue_size,
            "socketserver's default backlog is no longer 5, so the gap this "
            "setting closes has changed and the override should be re-argued")

    def test_the_console_sets_its_own_and_it_is_deeper(self):
        got = web_server._Server.request_queue_size
        self.assertGreater(
            got, socketserver.TCPServer.request_queue_size,
            "the console inherits the library's shallow accept backlog, so a "
            "sixth simultaneous request arriving while its slower handlers hold "
            "the GIL is REFUSED rather than queued — which a browser renders as "
            "a dead page, not a busy one")
        # Not merely deeper: deep enough for a console that polls many routes
        # at once from a phone opening its own connection per request.
        self.assertGreaterEqual(got, 64, "a backlog of %d is deeper than the "
                                         "default but still shallow for a "
                                         "multi-route poller" % got)
