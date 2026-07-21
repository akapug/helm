#!/usr/bin/env python3
"""helm.web — server contract tests. Runs against an ephemeral-port server in a
thread over a tmp HELM_HOME with a synthetic registry; the real ~/.helm is
never touched (HELM_HOME wins in home.env before any fallback)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import home, pk, web  # noqa: E402

PROJECTS = {
    "alpha": {
        "name": "alpha", "path": "/fake/dev/alpha", "kind": "git", "status": "active",
        "last_seen": 1900000000.0, "active_days": 4,
        "sessions": {"claude": 5, "codex": 2}, "harness_refs": {"claude": ["-fake-dev-alpha"]},
        "cwds": ["/fake/dev/alpha", "/fake/dev/alpha/worktrees/x"],
        "edges": [{"rel": "forked-from", "to": "mission-control", "note": "", "confirmed": True}],
    },
    "beta": {
        "name": "beta", "path": "/fake/dev/beta", "kind": "dir", "status": "dormant",
        "last_seen": 1700000000.0, "active_days": 1, "sessions": {"opencode": 1},
        "harness_refs": {}, "cwds": ["/fake/dev/beta"], "edges": [],
    },
}


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-web-")
        cls.env_prior = {k: os.environ.get(k) for k in ("HELM_HOME", "MELD_HOME")}
        os.environ["HELM_HOME"] = cls.tmp
        os.environ.pop("MELD_HOME", None)
        assert home.helm_home() == cls.tmp, "HELM_HOME override must win"
        pk.write_json(home.registry_path(), {"version": 1, "projects": PROJECTS,
                                             "generated_ts": pk.now_ts()})
        cls.srv = web.make_server(0)  # ephemeral port
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path):
        """(status, content_type, body_bytes) — 4xx/5xx returned, not raised."""
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type", ""), e.read()

    def _raw_get(self, path, headers):
        """A GET with fully custom headers (Host/Origin) via http.client, so we
        can forge the values the loopback origin-guard checks."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers=headers)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def test_same_origin_guard_rejects_foreign_host(self):
        # DNS-rebinding defense: a request whose Host is not our loopback bind
        # is refused before any data or the templated token can leak.
        status, body = self._raw_get("/api/registry", {"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertNotIn(b"alpha", body)

    def test_same_origin_guard_rejects_cross_origin(self):
        status, body = self._raw_get("/api/registry", {
            "Host": "127.0.0.1:%d" % self.port,
            "Origin": "http://attacker.example"})
        self.assertEqual(status, 403)

    def test_same_origin_guard_allows_loopback(self):
        status, body = self._raw_get("/api/registry", {"Host": "127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertIn(b"alpha", body)

    def test_root_serves_ui(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"helm", body)

    def test_seat_picker_popup_is_anchored_to_its_control(self):
        _, _, body = self.get("/")
        ui = body.decode()
        self.assertIn('#seatform .seatpick{position:relative', ui)
        self.assertIn('#seatlist{display:none;position:absolute;top:calc(100% + 4px);left:0;right:0', ui)
        self.assertIn('role="combobox" aria-autocomplete="list" aria-controls="seatlist"', ui)
        self.assertNotIn('list="seatlist"', ui)
        self.assertNotIn('<datalist id="seatlist"', ui)

    def test_registry_returns_synthetic_projects(self):
        status, ctype, body = self.get("/api/registry")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        reg = json.loads(body)
        self.assertEqual(set(reg["projects"]), {"alpha", "beta"})
        alpha = reg["projects"]["alpha"]
        self.assertEqual(alpha["sessions"], {"claude": 5, "codex": 2})
        self.assertEqual(alpha["edges"][0]["to"], "mission-control")

    def test_store_degrades_or_summarizes(self):
        status, _, body = self.get("/api/store")
        self.assertEqual(status, 200, "store endpoint must never 500")
        d = json.loads(body)
        self.assertTrue(d.get("unavailable") is True or "counts" in d,
                        "expected unavailable-or-counts, got %r" % d)

    def test_whoami_degrades_or_summarizes(self):
        status, _, body = self.get("/api/whoami")
        self.assertEqual(status, 200, "whoami endpoint must never 500")
        d = json.loads(body)
        self.assertIsInstance(d, dict)

    def test_unknown_path_404s_as_json(self):
        for path in ("/api/nope", "/etc/passwd", "/favicon.ico"):
            status, ctype, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertIn("application/json", ctype)
            self.assertIn("error", json.loads(body))

    def test_binds_localhost_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_port_flag_parsing(self):
        import contextlib
        import io
        self.assertEqual(web._port("7433"), 7433)
        self.assertIsNone(web._port("nope"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(web.cmd_web(["--port"]), 2)   # missing value
            self.assertEqual(web.cmd_web(["--bogus"]), 2)  # unknown flag


if __name__ == "__main__":
    unittest.main()
