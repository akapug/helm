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
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import home, pk, web, web_ui_loader  # noqa: E402

PROJECTS = {
    "alpha": {
        "name": "alpha", "path": "/fake/dev/alpha", "kind": "git", "status": "active",
        "last_seen": 1900000000.0, "active_days": 4,
        "sessions": {"claude": 5, "codex": 2, "pi": 1},
        "harness_refs": {"claude": ["-fake-dev-alpha"], "pi": ["pi-session"]},
        "cwds": ["/fake/dev/alpha", "/fake/dev/alpha/worktrees/x"],
        "edges": [{"rel": "forked-from", "to": "upstream-project", "note": "", "confirmed": True}],
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

    def test_root_serves_the_exact_assembled_ui(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        expected = web_ui_loader.read_bytes()
        expected = expected.replace(b"__HELM_TOKEN__",
                                    web.MUTATION_TOKEN.encode())
        expected = expected.replace(b"__HELM_ROOM__",
                                    web.default_room().encode())
        self.assertEqual(body, expected)

    def test_ui_assembly_failures_keep_the_error_shape_and_name_the_cause(self):
        failures = (
            ValueError("web UI manifest repeats a.part"),
            FileNotFoundError("missing.part"),
        )
        for failure in failures:
            with self.subTest(failure=failure), mock.patch.object(
                    web.web_ui_loader, "read_bytes", side_effect=failure):
                status, ctype, body = self.get("/")
                self.assertEqual(status, 500)
                self.assertIn("application/json", ctype)
                self.assertEqual(json.loads(body), {
                    "error": "web UI assembly failed: %s" % failure,
                })

    def test_ui_labels_pi_harness_and_runtime_route(self):
        _, _, body = self.get("/")
        ui = body.decode()
        self.assertIn('const KNOWN_H = ["claude", "codex", "opencode", "pi"]', ui)
        self.assertIn(".h-pi{", ui)
        self.assertIn("const seatRuntime = s =>", ui)
        self.assertIn('fact("runtime", seatRuntime(s))', ui)
        self.assertIn('class="rruntime"', ui)

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
        self.assertEqual(alpha["sessions"],
                         {"claude": 5, "codex": 2, "pi": 1})
        self.assertEqual(alpha["edges"][0]["to"], "upstream-project")

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

    def test_the_console_opens_on_a_DERIVED_room_never_the_main_literal(self):
        """The owner's console opened on #main while the whole fleet talked in
        #helm — he had to be TOLD where a council was. The default is now the
        same derivation every seat uses, and it must survive a cwd that
        derives nothing (a systemd unit with no WorkingDirectory starts in
        $HOME), because that is the shape that put it on #main."""
        from helm import seats
        web._DEFAULT_ROOM[:] = []                     # drop the lazy cache
        self.addCleanup(lambda: web._DEFAULT_ROOM.clear())
        with mock.patch.object(seats, "safe_cwd", return_value="/"):
            room = web.default_room()
        # the PROPERTY is "derived from the code's own location", not the
        # literal "helm" — pinning the string would test the harness rather
        # than the package location that owns the assembled page.
        self.assertEqual(room,
                         seats.derive_home_room(web_ui_loader.PACKAGE_DIR),
                         "no-project cwd must fall back to the CODE's own "
                         "project")
        self.assertNotEqual(room, "main", "fell back to the #main literal")

    def test_a_project_less_helm_still_gets_an_honest_main(self):
        """The fallback chain must END somewhere true: a helm that genuinely
        has no project has #main, and saying so is not the bug — silently
        preferring it over a real project was."""
        from helm import seats
        web._DEFAULT_ROOM[:] = []
        self.addCleanup(lambda: web._DEFAULT_ROOM.clear())
        with mock.patch.object(seats, "derive_home_room", return_value=None):
            self.assertEqual(web.default_room(), "main")

    def test_the_served_page_carries_the_room_not_the_placeholder(self):
        """The wiring leg: a correct default_room() behind a page that never
        receives it is the built-not-wired shape this fleet keeps finding."""
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"__HELM_ROOM__", body, "placeholder reached the browser")
        self.assertIn(b'HELM_DEFAULT_ROOM = chatSlug("', body)


class RoomTypeTest(unittest.TestCase):
    """The sidebar's room TYPING is read from the name, so it is testable
    without a browser — the classifier is the load-bearing half of 'melds
    should just be separate still' (owner 2026-07-29)."""

    def _classify(self, name):
        """Mirror of the assembled web UI's roomType() — kept honest by the
        source check below, which fails if the JS regex changes without this test."""
        import re as _re
        if _re.match(r"^(meld|council)-", name):
            return "meld"
        if _re.match(r"^dm-", name):
            return "dm"
        return "project"

    def test_the_live_room_inventory_types_correctly(self):
        for name, want in (
                ("meld-1785274962-mute-backlog-asymmetry", "meld"),
                ("council-forge-model", "meld"),   # a council IS a meld
                ("dm-codex", "dm"),
                ("helm", "project"), ("main", "project"),
                # a HYPHENATED project name must not read as a meld. Synthetic
                # on purpose: tests/ is tracked and helm is meant to go public,
                # so a real private project name here is owner data in a public
                # artifact — caught by test_never_track's fixture-label guard.
                ("example-platform", "project"),
                ("meldrooms", "project"),          # prefix, not substring
        ):
            self.assertEqual(self._classify(name), want, name)

    def test_the_JS_classifier_matches_this_test(self):
        """A python mirror of JS logic rots silently. This pins the actual
        regex text in the assembled web UI, so a change there fails HERE."""
        src = web_ui_loader.read_text()
        self.assertIn('/^(meld|council)-/.test(name)', src)
        self.assertIn('/^dm-/.test(name)', src)
        # melds still render as their own labeled, collapsible section, with
        # the quiet-fold group rows beneath each section (owner 2026-08-01)
        self.assertIn('id="crmeldhead"', src)
        self.assertIn('data-qgroup', src)


if __name__ == "__main__":
    unittest.main()
