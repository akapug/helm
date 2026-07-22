"""Hermetic tests for helm.modelrouter — the transparent multi-model router.

Every upstream is a FAKE local HTTP server minted in-test: the real
api.anthropic.com and the real CLIProxyAPI are never contacted, no claude is
ever launched, HELM_HOME points at a tmp dir. The two laws under test:
  1. claude-* requests relay VERBATIM (the client's own OAuth Authorization,
     body, and headers — no substitution, no re-auth, no API key ever).
  2. non-claude requests land on the owning seat's proxy with the seat token
     swapped in and the client's credential stripped.
"""
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helm import modelrouter


class _Fake(BaseHTTPRequestHandler):
    """A capturing upstream: records (method, path, headers, body) on the
    server, replies with the server's canned status/headers/body — optionally
    as an unlengthed stream (SSE shape) to exercise the chunked relay."""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.seen.append({
            "method": self.command, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        status, hdrs, payload, stream = self.server.reply
        self.send_response(status)
        for k, v in hdrs.items():
            self.send_header(k, v)
        if stream:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in payload:
                self.wfile.write(b"%X\r\n" % len(piece) + piece + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    do_GET = do_POST = _serve


def _fake_upstream(status=200, hdrs=None, payload=b'{"ok":true}', stream=False):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    srv.daemon_threads = True
    srv.seen = []
    srv.reply = (status, hdrs or {"Content-Type": "application/json"},
                 payload, stream)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return srv


class RouterTest(unittest.TestCase):
    OAUTH = "Bearer sk-ant-oat-FAKE-oauth-token-for-tests"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-router-")
        self._env = {k: os.environ.get(k) for k in
                     ("HELM_HOME", "MELD_HOME", "ANTHROPIC_API_KEY",
                      "HELM_ROUTER_ANTHROPIC_UPSTREAM")}
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ.pop("MELD_HOME", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("HELM_ROUTER_ANTHROPIC_UPSTREAM", None)
        self._cleanup = []

    def tearDown(self):
        for srv in self._cleanup:
            srv.shutdown()
            srv.server_close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- plumbing ------------------------------------------------------------
    def _upstream(self, **kw):
        srv = _fake_upstream(**kw)
        self._cleanup.append(srv)
        return srv

    def _router(self, anthropic, table=None, default=None, log=None):
        srv = modelrouter.start_inprocess(
            table=table or {}, default_family=default, log_path=log,
            anthropic="http://127.0.0.1:%d" % anthropic.server_address[1]
            if anthropic else "http://127.0.0.1:9")
        self._cleanup.append(srv)
        return srv

    def _post(self, router, body, headers=None, path="/v1/messages?beta=true"):
        conn = http.client.HTTPConnection("127.0.0.1",
                                          router.server_address[1], timeout=10)
        try:
            payload = json.dumps(body).encode() if isinstance(body, dict) else body
            conn.request("POST", path, body=payload,
                         headers={"Content-Type": "application/json",
                                  **(headers or {})})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    # -- law 1: claude-* passthrough is VERBATIM ------------------------------
    def test_claude_request_forwards_verbatim_oauth_untouched(self):
        up = self._upstream(payload=b'{"id":"msg_fake"}')
        router = self._router(up)
        body = {"model": "claude-fable-5", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}]}
        status, _, out = self._post(router, body, headers={
            "Authorization": self.OAUTH,
            "anthropic-beta": "oauth-2025-04-20",
            "x-helm-test-header": "rides-verbatim"})
        self.assertEqual(status, 200)
        self.assertEqual(out, b'{"id":"msg_fake"}')
        seen = up.seen[0]
        self.assertEqual(seen["path"], "/v1/messages?beta=true")  # query intact
        self.assertEqual(seen["body"], json.dumps(body).encode())  # byte-same
        self.assertEqual(seen["headers"]["authorization"], self.OAUTH)  # untouched
        self.assertEqual(seen["headers"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(seen["headers"]["x-helm-test-header"], "rides-verbatim")
        self.assertNotIn("x-api-key", seen["headers"])  # no re-auth, ever

    def test_ambient_api_key_never_injected(self):
        """HARD LAW: Claude is OAuth-only. Even with ANTHROPIC_API_KEY sitting
        in the router's own environment, the passthrough must not grow an
        x-api-key header or touch Authorization."""
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api-FORBIDDEN"
        up = self._upstream()
        router = self._router(up)
        self._post(router, {"model": "claude-fable-5"},
                   headers={"Authorization": self.OAUTH})
        seen = up.seen[0]
        self.assertEqual(seen["headers"]["authorization"], self.OAUTH)
        self.assertNotIn("x-api-key", seen["headers"])
        self.assertNotIn("FORBIDDEN", json.dumps(seen["headers"]))

    def test_modelless_get_passes_through_to_anthropic(self):
        up = self._upstream(payload=b'{"data":[]}')
        router = self._router(up)
        conn = http.client.HTTPConnection("127.0.0.1",
                                          router.server_address[1], timeout=10)
        try:
            conn.request("GET", "/v1/models?limit=5",
                         headers={"Authorization": self.OAUTH})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b'{"data":[]}')
        finally:
            conn.close()
        self.assertEqual(up.seen[0]["path"], "/v1/models?limit=5")
        self.assertEqual(up.seen[0]["headers"]["authorization"], self.OAUTH)

    def test_streaming_relay_arrives_whole(self):
        """SSE shape: upstream streams chunked with no Content-Length — the
        relay re-chunks live and the client reads the full event stream."""
        events = [b"event: message_start\ndata: {}\n\n",
                  b"event: content_block_delta\ndata: {\"d\":1}\n\n",
                  b"event: message_stop\ndata: {}\n\n"]
        up = self._upstream(hdrs={"Content-Type": "text/event-stream"},
                            payload=events, stream=True)
        router = self._router(up)
        status, hdrs, out = self._post(
            router, {"model": "claude-fable-5", "stream": True},
            headers={"Authorization": self.OAUTH})
        self.assertEqual(status, 200)
        self.assertEqual(out, b"".join(events))
        self.assertEqual(hdrs.get("Content-Type"), "text/event-stream")

    # -- law 2: non-claude -> the seat proxy, token swapped --------------------
    def _table(self, codex_srv=None, kimi_srv=None):
        t = {}
        if codex_srv:
            t["codex"] = {"port": codex_srv.server_address[1],
                          "token": "tok-codex-fake",
                          "models": {"gpt-5.6-sol", "gpt-5.6-terra"}}
        if kimi_srv:
            t["kimi"] = {"port": kimi_srv.server_address[1],
                         "token": "tok-kimi-fake", "models": {"kimi-k3"}}
        return t

    def test_non_claude_routes_to_proxy_with_seat_token(self):
        anth, proxy = self._upstream(), self._upstream()
        router = self._router(anth, table=self._table(codex_srv=proxy),
                              default="codex")
        body = {"model": "gpt-5.6-sol", "messages": []}
        status, _, _ = self._post(router, body, headers={
            "Authorization": self.OAUTH, "x-api-key": "stray-key"})
        self.assertEqual(status, 200)
        self.assertEqual(anth.seen, [])            # anthropic never touched
        seen = proxy.seen[0]
        self.assertEqual(seen["headers"]["authorization"],
                         "Bearer tok-codex-fake")  # seat token swapped in
        self.assertEqual(seen["body"], json.dumps(body).encode())
        self.assertNotIn("x-api-key", seen["headers"])   # client cred stripped
        self.assertNotIn("sk-ant-oat", json.dumps(seen["headers"]))  # no OAuth leak

    def test_exact_model_match_routes_cross_family(self):
        anth = self._upstream()
        codex, kimi = self._upstream(), self._upstream()
        router = self._router(anth, table=self._table(codex, kimi),
                              default="codex")
        self._post(router, {"model": "kimi-k3"})
        self._post(router, {"model": "gpt-5.6-terra"})
        self._post(router, {"model": "gpt-9-unknown"})   # falls to default seat
        self.assertEqual(len(kimi.seen), 1)
        self.assertEqual(kimi.seen[0]["headers"]["authorization"],
                         "Bearer tok-kimi-fake")
        self.assertEqual(len(codex.seen), 2)             # terra + the unknown
        self.assertEqual(anth.seen, [])

    def test_mixed_fanout_one_router_both_worlds(self):
        """The 0.2 shape end-to-end: one router, a claude-* parent request
        passing through verbatim AND two differently-pinned non-claude
        subagent requests conducted to the proxy — the proven run-1 pattern
        with the claude parent seam closed."""
        anth, proxy = self._upstream(), self._upstream()
        log = os.path.join(self.tmp, "router.log")
        router = self._router(anth, table=self._table(codex_srv=proxy),
                              default="codex", log=log)
        self._post(router, {"model": "claude-fable-5"},
                   headers={"Authorization": self.OAUTH})
        self._post(router, {"model": "gpt-5.6-sol"})
        self._post(router, {"model": "gpt-5.6-terra"})
        self.assertEqual(len(anth.seen), 1)
        self.assertEqual(anth.seen[0]["headers"]["authorization"], self.OAUTH)
        self.assertEqual(len(proxy.seen), 2)
        self.assertEqual(modelrouter.logged_models(log),
                         {"claude-fable-5", "gpt-5.6-sol", "gpt-5.6-terra"})

    # -- the conductor log ----------------------------------------------------
    def test_conductor_log_shape_and_secrecy(self):
        anth, proxy = self._upstream(), self._upstream()
        log = os.path.join(self.tmp, "router.log")
        router = self._router(anth, table=self._table(codex_srv=proxy),
                              default="codex", log=log)
        self._post(router, {"model": "claude-fable-5"},
                   headers={"Authorization": self.OAUTH})
        self._post(router, {"model": "gpt-5.6-sol"},
                   headers={"Authorization": self.OAUTH})
        with open(log) as f:
            lines = [json.loads(l) for l in f.read().splitlines()]
        self.assertEqual(len(lines), 2)
        by_model = {l["model"]: l for l in lines}
        self.assertEqual(by_model["claude-fable-5"]["route"], "anthropic")
        self.assertEqual(by_model["gpt-5.6-sol"]["route"], "proxy:codex")
        for l in lines:
            self.assertEqual(l["status"], 200)
            self.assertEqual(l["method"], "POST")
        with open(log) as f:
            raw = f.read()
        self.assertNotIn("sk-ant-oat", raw)      # header values never logged
        self.assertNotIn("tok-codex-fake", raw)  # seat token never logged

    def test_dead_upstream_is_502_and_logged(self):
        anth = self._upstream()
        table = {"codex": {"port": 9, "token": "tok-x",
                           "models": {"gpt-5.6-sol"}}}   # port 9: nothing there
        log = os.path.join(self.tmp, "router.log")
        router = self._router(anth, table=table, default="codex", log=log)
        status, _, out = self._post(router, {"model": "gpt-5.6-sol"})
        self.assertEqual(status, 502)
        self.assertIn(b"helm_router_upstream_error", out)
        with open(log) as f:
            entry = json.loads(f.read().splitlines()[0])
        self.assertEqual(entry["status"], 502)
        self.assertEqual(entry["route"], "proxy:codex")

    # -- routing decision + table -----------------------------------------
    def test_decide_matrix(self):
        r = modelrouter.Router(
            {"codex": {"port": 1, "token": "t", "models": {"gpt-5.6-sol"}},
             "kimi": {"port": 2, "token": "t", "models": {"kimi-k3"}}},
            default_family="codex", anthropic="http://x", log_path=None)
        self.assertEqual(r.decide("claude-fable-5"), ("anthropic", None))
        self.assertEqual(r.decide("claude-3-5-haiku-20241022"), ("anthropic", None))
        self.assertEqual(r.decide(None), ("anthropic", None))
        self.assertEqual(r.decide("gpt-5.6-sol"), ("proxy", "codex"))
        self.assertEqual(r.decide("kimi-k3"), ("proxy", "kimi"))
        self.assertEqual(r.decide("gpt-9"), ("proxy", "codex"))  # default seat
        # no default -> unknown non-claude passes through (transparent bias)
        r2 = modelrouter.Router({}, None, "http://x", None)
        self.assertEqual(r2.decide("gpt-9"), ("anthropic", None))

    def test_build_table_reads_minted_seats_only(self):
        from helm import seat
        d = seat.seat_dir("codex")
        os.makedirs(d, mode=0o700)
        with open(os.path.join(d, "token"), "w") as f:
            f.write("tok-from-disk\n")
        table = modelrouter.build_table()
        self.assertEqual(set(table), {"codex"})     # kimi unminted -> absent
        self.assertEqual(table["codex"]["port"], seat.FAMILIES["codex"]["port"])
        self.assertEqual(table["codex"]["token"], "tok-from-disk")
        self.assertIn("gpt-5.6-sol", table["codex"]["models"])
        self.assertIn("gpt-5.6-terra", table["codex"]["models"])

    # -- CLI surface --------------------------------------------------------
    def test_parent_line_oauth_only_no_pin(self):
        line = modelrouter.parent_line()
        self.assertIn("-u ANTHROPIC_API_KEY", line)        # hard law, actively unset
        self.assertIn("-u ANTHROPIC_AUTH_TOKEN", line)     # OAuth must win
        self.assertIn("-u CLAUDE_CODE_SUBAGENT_MODEL", line)  # frontmatter routes
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:%d"
                      % modelrouter.ROUTER_PORT_DEFAULT, line)
        self.assertNotIn("ANTHROPIC_API_KEY=", line)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN=", line)
        self.assertIn(" claude", line)
        self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:9999",
                      modelrouter.parent_line(9999))

    def test_cmd_router_status_and_line(self):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = modelrouter.cmd_router(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("down", out.getvalue())
        self.assertIn("VERBATIM passthrough", out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = modelrouter.cmd_router(["line"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), modelrouter.parent_line())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = modelrouter.cmd_router([])
        self.assertEqual(rc, 2)

    def test_cmd_router_probes_mints_per_family_agents(self):
        import contextlib
        from helm import seat
        d = seat.seat_dir("codex")
        os.makedirs(d, mode=0o700)
        with open(os.path.join(d, "token"), "w") as f:
            f.write("tok\n")
        base = os.path.join(self.tmp, "proj")
        os.makedirs(base)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = modelrouter.cmd_router(["probes", "--dir", base])
        self.assertEqual(rc, 0)
        ad = os.path.join(base, ".claude", "agents")
        names = sorted(os.listdir(ad))
        self.assertEqual(names, ["helm-probe-gpt-5-6-sol.md",
                                 "helm-probe-gpt-5-6-terra.md"])
        with open(os.path.join(ad, "helm-probe-gpt-5-6-terra.md")) as f:
            body = f.read()
        self.assertIn("model: gpt-5.6-terra", body)
        self.assertIn("name: helm-probe-gpt-5-6-terra", body)
        # no seats minted -> loud unblock, rc 1
        shutil.rmtree(os.path.dirname(d))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = modelrouter.cmd_router(["probes", "--dir", base])
        self.assertEqual(rc, 1)
        self.assertIn("helm seat add codex", err.getvalue())


if __name__ == "__main__":
    unittest.main()
