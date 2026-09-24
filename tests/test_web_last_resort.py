#!/usr/bin/env python3
"""The last-resort guard: no exception from handler code reaches the socket.

Every named guard in the handler wraps the code it knows about. The code
BETWEEN them -- the origin check, a route lookup, an exception type a narrow
`except` does not name -- is wrapped by nothing, and a raise there closes the
connection with no response at all. A caller sees
`RemoteDisconnected: Remote end closed connection without response`: a network
error, not a value, so a page cannot render it and a poll loop reads it as the
server being gone.

These arms drive the real Handler over a real socket on an ephemeral port. The
raising code is injected synthetically, because the point is the SHAPE of the
failure (a raise before anything is written, and a raise after a status line is
already out), not any particular bug that produces it.

Each arm that changes the handler carries its own must-hit control: a healthy
request answering 200 in the same server. Without it, a 500 could as easily be
a broken fixture as a working guard.
"""
import io
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import web, web_server  # noqa: E402,F401  (web populates the module globals web_server reads)

HEALTHY = "/api/ready"


class LastResortGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path=HEALTHY, data=None):
        """(status, body) with 4xx/5xx returned rather than raised; a dropped
        connection is returned as its exception NAME, which is the negative
        this file exists to make impossible."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as exc:            # noqa: BLE001 — the measured negative
            return type(exc).__name__, b""

    def _quietly(self, fn):
        """The guard prints the traceback, which belongs on a server's stderr
        and not in a test run's output."""
        prior, sys.stderr = sys.stderr, io.StringIO()
        try:
            return fn()
        finally:
            sys.stderr = prior

    def test_a_raise_before_any_write_answers_500_with_a_json_body(self):
        self.assertEqual(self._get()[0], 200, "must-hit control")

        def boom(_self):
            raise RuntimeError("a raise from handler code no named guard wraps")

        real = web_server.Handler._same_origin
        web_server.Handler._same_origin = boom
        try:
            status, body = self._quietly(self._get)
        finally:
            web_server.Handler._same_origin = real

        self.assertEqual(status, 500)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self._get()[0], 200,
                         "the server still answers after the failure")

    def test_the_guard_covers_POST_as_well_as_GET(self):
        def boom(_self):
            raise RuntimeError("a raise from handler code no named guard wraps")

        real = web_server.Handler._same_origin
        web_server.Handler._same_origin = boom
        try:
            status, body = self._quietly(
                lambda: self._get("/api/nothing-here", data=b"{}"))
        finally:
            web_server.Handler._same_origin = real

        self.assertEqual(status, 500)
        self.assertIn("error", json.loads(body))

    def test_a_raise_after_the_status_line_leaves_the_stream_alone(self):
        """The stream shape: a handler that has already sent its own status
        line and started writing a body, then fails. A second status line
        written into that stream is CORRUPTION -- worse than the dropped
        connection the guard replaces -- so the guard must stay silent here.

        Read with a raw socket: a client that parses one response cannot see a
        second status line further down the same stream, which is exactly the
        damage being measured.
        """
        def half_written(self_):
            self_.send_response(200)
            self_.send_header("Content-Type", "text/plain")
            self_.end_headers()
            self_.wfile.write(b"first bytes\n")
            self_.wfile.flush()
            raise RuntimeError("a raise after the status line is already out")

        real = web_server.Handler._do_get
        web_server.Handler._do_get = half_written
        try:
            raw = self._quietly(lambda: self._raw(b"GET / HTTP/1.0\r\n\r\n"))
        finally:
            web_server.Handler._do_get = real

        self.assertIn(b"first bytes", raw,
                      "the arm did not reach the write it is about")
        self.assertEqual(raw.count(b"HTTP/1."), 1,
                         "a second status line was written into a live stream")

    def test_the_flag_is_per_request_not_per_connection(self):
        """One handler instance can serve MORE THAN ONE request, and the flag
        that says "nothing has been written yet" is about the request, not the
        connection. If it survived from one request to the next, a handler that
        answered the first request would tell the guard the headers were
        already out on the second -- and the guard would stay silent, dropping
        exactly the connection it exists to answer.

        The handler is HTTP/1.0 today, so every request gets its own instance
        and the reset has nothing to undo. This arm raises the protocol to the
        version where it does, and sends both requests down one socket.
        """
        state = {"n": 0}

        def answer_then_raise(self_):
            state["n"] += 1
            if state["n"] == 1:
                return self_._json({"ok": True})
            raise RuntimeError("a raise on the SECOND request of a connection")

        real_do = web_server.Handler._do_get
        real_proto = web_server.Handler.protocol_version
        web_server.Handler._do_get = answer_then_raise
        web_server.Handler.protocol_version = "HTTP/1.1"
        try:
            raw = self._quietly(lambda: self._raw(
                b"GET /api/ready HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
                b"GET /api/ready HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Connection: close\r\n\r\n"))
        finally:
            web_server.Handler._do_get = real_do
            web_server.Handler.protocol_version = real_proto

        self.assertEqual(state["n"], 2,
                         "both requests must reach the handler, or this arm "
                         "measured one request and a closed socket")
        self.assertEqual(raw.count(b"HTTP/1."), 2,
                         "the second request went unanswered")
        self.assertIn(b" 500 ", raw.split(b"HTTP/1.")[-1],
                      "the second request was not answered by the guard")

    def _raw(self, request):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(request)
            out = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return out
                out += chunk
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
