#!/usr/bin/env python3
"""The handler waits on a client for a bounded time, and on itself forever.

do_POST reads the request body with `self.rfile.read(n)` where n is the
CLIENT's own Content-Length. With no timeout on the connection that read blocks
with no bound, so a client that announces more than it sends and holds the
socket open pins a handler thread permanently. A client that CLOSES is not the
case: the read sees EOF, returns short, and the JSON parse answers 400.

THE ARMS DRIVE A REAL SERVER OVER A RAW SOCKET, because urllib will not send a
Content-Length it does not intend to honour.

THREE GATES STAND BEFORE THAT READ -- the origin check, the route lookup, and
the bearer -- and each answers instantly, so an arm that trips one of them
measures the gate rather than the read and its early 403 looks exactly like a
bounded read. Every arm below therefore carries a must-hit control: the SAME
route with a COMPLETE body, whose status must be neither 403 nor 404.
"""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import web, web_server  # noqa: E402


class ClientWaitBoundTest(unittest.TestCase):
    def setUp(self):
        self.route = sorted(web_server.POST_API)[0]
        self.prior = web_server.Handler.timeout
        # the shipped bound is deliberately loose; an arm that waited it out
        # would spend half a minute proving a property one second proves
        web_server.Handler.timeout = 1
        self.srv = web.make_server(0)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        web_server.Handler.timeout = self.prior
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)

    def _post(self, body, declared, hold=0.0, window=5.0):
        """The status line, or None if the server never answered."""
        head = (
            "POST %s HTTP/1.0\r\n"
            "Host: 127.0.0.1:%d\r\n"
            "Authorization: Bearer %s\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: %d\r\n\r\n"
            % (self.route, self.port, web.MUTATION_TOKEN, declared)).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(head + body)
            if hold:
                time.sleep(hold)
            s.settimeout(window)
            try:
                got = s.recv(200)
            except socket.timeout:
                return None
        finally:
            s.close()
        if not got:
            return None
        return got.split(b"\r\n", 1)[0].decode("utf-8", "replace")

    def _control(self):
        """A complete body on the same route. Its answer proves a request of
        this shape reaches the body read at all."""
        line = self._post(b'{"a": 1}', 8)
        self.assertIsNotNone(line, "the control never answered")
        self.assertNotIn(" 403", line, "the control stopped at a gate before "
                                       "the read: %s" % line)
        self.assertNotIn(" 404", line, "the control stopped at a gate before "
                                       "the read: %s" % line)
        return line

    def test_the_shipped_handler_carries_a_bound_at_all(self):
        """THE ARMS BELOW SET THEIR OWN BOUND, SO NONE OF THEM CAN SEE THE
        SHIPPED ONE. setUp replaces Handler.timeout with a second so an arm
        need not wait out the real value -- which means every mechanism arm
        stays green with the class attribute deleted entirely. A mutation
        proved that: `timeout = None` survived the whole file.

        This reads the value setUp saved before overwriting it, which is the
        one the server actually runs with.
        """
        self.assertIsNotNone(
            self.prior, "the shipped handler has NO bound; a stalled client "
                        "pins its thread forever and no other arm here can "
                        "tell, because they all set their own")
        self.assertGreater(self.prior, 0)

    def test_a_client_that_under_delivers_and_waits_gets_an_answer(self):
        self._control()
        line = self._post(b"{", 4096, hold=0.0, window=5.0)
        self.assertIsNotNone(
            line, "the handler is still waiting on a client that stopped "
                  "sending; without a bound this never returns")
        self.assertIn(" 408", line,
                      "a stalled client is not a server failure, and 500 "
                      "would bury it with the real ones: %s" % line)

    def test_a_client_that_closes_early_is_not_the_same_case(self):
        """EOF is not a stall: the read returns short and the JSON parse
        answers. This arm exists so the bound is not credited with a case that
        already worked."""
        self._control()
        head = (
            "POST %s HTTP/1.0\r\n"
            "Host: 127.0.0.1:%d\r\n"
            "Authorization: Bearer %s\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 4096\r\n\r\n"
            % (self.route, self.port, web.MUTATION_TOKEN)).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(head + b"{")
        s.shutdown(socket.SHUT_WR)
        s.settimeout(5.0)
        got = s.recv(200)
        s.close()
        self.assertIn(b" 400 ", got.split(b"\r\n", 1)[0],
                      "a closed connection should parse short and answer 400")

    def test_the_bound_is_on_the_client_never_on_the_servers_own_work(self):
        """A handler that takes LONGER than the bound to compute still answers.
        settimeout bounds socket operations, and a handler that is thinking
        performs none -- so a slow answer stays an answer.
        """
        slept = {}

        def slow(self_):
            slept["for"] = web_server.Handler.timeout * 2.0
            time.sleep(slept["for"])
            return self_._json({"slow": True})

        real = web_server.Handler._do_get
        web_server.Handler._do_get = slow
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
            s.sendall(("GET /api/ready HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n"
                       % self.port).encode())
            s.settimeout(30)
            got = s.recv(200)
            s.close()
        finally:
            web_server.Handler._do_get = real

        self.assertGreater(slept.get("for", 0), web_server.Handler.timeout,
                           "the arm did not actually outlast the bound")
        self.assertIn(b" 200 ", got.split(b"\r\n", 1)[0],
                      "a slow-but-working handler was cut off by the bound")


if __name__ == "__main__":
    unittest.main()
