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
        # shutdown() waits one serve_forever poll (stdlib 0.5s)
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.01}, daemon=True)
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
        # …and the build this page IS, so an hours-old tab can notice that the
        # server has moved on and offer a reload.
        expected = expected.replace(b"__HELM_BUILD__",
                                    web_ui_loader.build_id().encode())
        self.assertEqual(body, expected)
        # EVERY MARKER IS SUBSTITUTED, not just the ones named above: a marker
        # the server forgot to fill would ship the literal to the browser, and
        # the page would compare its build against the string "__HELM_BUILD__"
        # forever without ever saying so.
        self.assertNotIn(b"__HELM_", body)

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
        # THE LINEAGE ROWS MOVED TO THEIR OWN DOOR, and the count stayed. The
        # chip asks "is there any" and is answered from the list; the rows are
        # what an OPEN card fetches.
        self.assertEqual(1, alpha["edges_n"])
        self.assertNotIn("edges", alpha)
        self.assertEqual("/api/project/detail", reg["detail_route"])

    def test_the_detail_door_serves_over_http_what_the_list_stopped_carrying(self):
        """THE SPLIT, END TO END, over the same synthetic registry the arm above
        reads — the list and the route are one contract and a unit test on
        either half alone cannot see them disagree."""
        status, ctype, body = self.get("/api/project/detail?name=alpha")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        got = json.loads(body)
        self.assertEqual("alpha", got["name"])
        self.assertEqual("upstream-project", got["detail"]["edges"][0]["to"])
        self.assertEqual(["/fake/dev/alpha", "/fake/dev/alpha/worktrees/x"],
                         got["detail"]["cwds"])
        # THE CARD'S OWN KEYS DO NOT RIDE THIS DOOR TOO — a field on both wires
        # is a field paid for twice.
        for key in ("name", "path", "status", "last_seen", "sessions", "light"):
            self.assertNotIn(key, got["detail"])

    def test_the_detail_door_404s_a_project_that_is_not_there(self):
        """THE CONTROL for the arm above, over the same live server: a row that
        exists answers 200 with a detail, so the 404 is this name being absent
        rather than the route being unreachable."""
        status, _, body = self.get("/api/project/detail?name=not-a-project")
        self.assertEqual(404, status)
        self.assertNotIn("detail", json.loads(body))
        ok_status, _, ok_body = self.get("/api/project/detail?name=beta")
        self.assertEqual(200, ok_status)
        self.assertIn("detail", json.loads(ok_body))

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
        with mock.patch.object(
                seats, "derive_home_room_typed",
                return_value=(seats.DERIVE_NONE, None)):
            self.assertEqual(web.default_room(), "main")

    def test_an_unknown_cwd_does_not_license_the_package_fallback(self):
        from helm import seats
        web._DEFAULT_ROOM[:] = []
        self.addCleanup(lambda: web._DEFAULT_ROOM.clear())
        calls = []

        def unknown(path):
            calls.append(path)
            return seats.DERIVE_UNKNOWN, None

        with mock.patch.object(seats, "safe_cwd", return_value="/gone"), \
                mock.patch.object(seats, "derive_home_room_typed",
                                  side_effect=unknown):
            self.assertEqual(web.default_room(), "main")
        self.assertEqual(calls, ["/gone"],
                         "UNKNOWN was spent as NONE to ask the package fallback")

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


class AServedListSaysWhatItLeftOutTest(unittest.TestCase):
    """A cut array on the wire reads to its consumer as the whole store, and
    the counts beside it cannot stand in: that number comes from a different
    producer and the two can disagree."""

    def setUp(self):
        from helm import web_core
        self.wc = web_core
        self.cap = web_core.ENTRY_CAP

    def faked(self, n, statement=""):
        from helm import store
        rows = [{"id": "e%d" % i, "type": "premise", "confidence": 1,
                 "load_class": "core", "scope": "global"} for i in range(n)]
        review = [{"id": "r1", "type": "premise", "status": "candidate",
                   "statement": statement}]
        return mock.patch.multiple(
            store, counts=lambda: {"global": {"premise": n}},
            entries=lambda: list(rows), reviewable=lambda: list(review))

    def test_a_cut_entry_list_carries_total_shown_and_truncated(self):
        with self.faked(self.cap + 25):
            out = self.wc._api_store()
        self.assertEqual(len(out["entries"]), self.cap)
        self.assertEqual(out["entries_window"],
                         {"total": self.cap + 25, "shown": self.cap,
                          "truncated": 25, "cap": self.cap})

    def test_a_whole_entry_list_says_nothing_was_left_out(self):
        """The must-hit control: under the cap the window reports zero
        truncated, so a non-zero truncated is a real loss."""
        with self.faked(3):
            out = self.wc._api_store()
        self.assertEqual(len(out["entries"]), 3)
        self.assertEqual(out["entries_window"]["total"], 3)
        self.assertEqual(out["entries_window"]["truncated"], 0)

    def test_a_cut_review_statement_says_so(self):
        long_one = "the measured claim and its number " * 30
        with self.faked(1, statement=long_one):
            cut = self.wc._api_store_review()["entries"][0]["statement"]
        self.assertIn("[cut: %d of %d chars]"
                      % (self.wc.REVIEW_STMT_CAP, len(long_one)), cut)
        self.assertEqual(cut, pk.cut_marked(long_one,
                                            self.wc.REVIEW_STMT_CAP))

    def test_a_whole_review_statement_is_byte_identical(self):
        with self.faked(1, statement="short and whole"):
            whole = self.wc._api_store_review()["entries"][0]["statement"]
        self.assertEqual(whole, "short and whole")


# ---------------------------------------------------------------------------
# what the project grid is allowed to cost a phone
# ---------------------------------------------------------------------------

_FULL_PROJECT = {
    "name": "alpha", "path": "/fake/dev/alpha", "kind": "git",
    "status": "active", "last_seen": 1900000000.0, "active_days": 4,
    "first_seen": 1700000000.0,
    # THREE KNOWN HARNESSES, none of them a seat name — the chip renderer takes
    # its known-harness branch for each, so the card under test draws the real
    # arm rather than the "other" fallback.
    "sessions": {"codex": 5, "opencode": 2, "pi": 1},
    "harness_refs": {"codex": ["-fake-dev-alpha"], "pi": ["pi-session"]},
    "cwds": ["/fake/dev/alpha", "/fake/dev/alpha/worktrees/x"],
    "memory_dir": "/fake/memory", "home": "/fake/home",
    "cv_scope": {"cwd_prefix": "/fake/dev/alpha",
                 "cwd_prefixes": ["/fake/dev/alpha", "/fake/dev/alpha/worktrees/x"]},
    "notes": "a note the detail pane draws",
    "edges": [{"rel": "forked-from", "to": "upstream", "note": "n",
               "confirmed": True}],
    "state": {"colour": "orange", "reason": "critical path only",
              "by": "an-agent", "ts": 1900000000},
}

_FULL_LIGHT = {"colour": "orange", "authored": True,
               "reason": "critical path only", "by": "an-agent",
               "ts": 1900000000, "key": "alpha"}


class RegistryWireBoundsTest(unittest.TestCase):
    """WHAT /api/registry MAY WEIGH, and what it may never stop carrying.

    THE DEFECT: measured on the owner's own registry, this door shipped 85,266
    bytes over 234 projects on every load of the home tab. Two whole fields in
    it had no reader at all, and one — `state` — is a field the shipped page
    REFUSES to read on principle: `light()` says so in as many words, because
    reading the merged record's own block would let a projection byte pass for
    the owner's word. Serving it anyway put that byte one property away from
    the renderer that must not take it.

    The cure is a DROP at emit. These arms hold both halves: the unread go, and
    every field a renderer touches is carried WHOLE — this door bounds no
    field's content, so nothing here can be a partial mistaken for complete.
    """

    def test_the_two_fields_with_no_reader_left_the_wire(self):
        row = web._project_on_the_wire(dict(_FULL_PROJECT), dict(_FULL_LIGHT))
        # THE UNCONDITIONAL CONTROL, before any loop below can decide not to
        # run: this row came back at all, and it is the row that went in.
        self.assertEqual("alpha", row["name"])
        self.assertEqual("/fake/dev/alpha", row["path"])
        for key in ("state", "kind"):
            self.assertNotIn(key, row,
                             "%r rode a project row nothing reads it from" % key)
        # THE CONTROL, on the same row in the same call: every field the CARD
        # draws is still here, so the two absences above are a trim and not a
        # projection that collapsed. The nine an OPEN card draws left this row
        # too, but they left it for a ROUTE, not for nowhere — which is the
        # next class's subject, not this one's.
        for key in ("name", "path", "status", "last_seen", "sessions"):
            self.assertIn(key, row, "the projection lost %r, so the absences "
                                    "above are not a deliberate trim" % key)

    def test_the_resolved_light_still_carries_everything_the_card_draws(self):
        """`state` leaving the wire may not take the owner's word with it. The
        RESOLVED light is what the card and the light-setter draw, and every
        field of it rides."""
        row = web._project_on_the_wire(dict(_FULL_PROJECT), dict(_FULL_LIGHT))
        for key in ("colour", "authored", "reason", "by", "ts"):
            self.assertIn(key, row["light"],
                          "the light lost %r, which the card draws" % key)
        self.assertEqual("critical path only", row["light"]["reason"])
        self.assertIs(True, row["light"]["authored"])

    def test_the_light_key_stays_because_it_has_a_reader(self):
        """THE CANDIDATE THIS LANE REFUSED, pinned so the next byte census does
        not file it again. `light.key` repeats the row's own name on every
        project — 5,944 of this door's bytes — and the shipped page would
        compute it back through the `|| p.name` fallback it already has. It is
        still sent, because the projects-verb suite reads it off this door: a
        field with a reader is not a field with no reader, however derivable it
        looks. It is also the key the light-setter POSTs to, so a wrong guess
        writes the owner's colour onto a project he was not looking at."""
        row = web._project_on_the_wire(dict(_FULL_PROJECT), dict(_FULL_LIGHT))
        self.assertEqual("alpha", row["light"]["key"])
        row = web._project_on_the_wire(
            dict(_FULL_PROJECT), dict(_FULL_LIGHT, key="alpha-under-another-name"))
        self.assertEqual("alpha-under-another-name", row["light"]["key"])

    def test_a_project_the_resolver_did_not_answer_for_gets_no_invented_light(self):
        """UNLIT IS NOT ORANGE. The page's own fallback — the scan's colour,
        `authored: false` — is written for a row with no `light`, and a light
        stamped here out of nothing would draw a colour with no reading behind
        it as though someone had decided it."""
        row = web._project_on_the_wire(dict(_FULL_PROJECT, light={"stale": 1}),
                                       None)
        self.assertNotIn("light", row)
        # THE CONTROL on the same helper: it DOES stamp one when the resolver
        # answered, so the absence above is the None arm and not a helper that
        # never carries a light.
        self.assertIn("light", web._project_on_the_wire(dict(_FULL_PROJECT),
                                                        dict(_FULL_LIGHT)))

    def test_the_handler_never_narrows_what_the_loaded_record_holds(self):
        """The trim is a NEW dict. `registry.load` feeds chat, the ledger
        injector, seat and sessions in the same process, and every one of them
        reads fields this door drops — a handler that trimmed in place would
        take `cv_scope` and `kind` off readers that never asked a browser for
        anything."""
        rec = dict(_FULL_PROJECT)
        web._project_on_the_wire(rec, dict(_FULL_LIGHT))
        web._project_detail_on_the_wire(rec)
        self.assertIn("state", rec, "the handler mutated the loaded record")
        self.assertIn("kind", rec)
        # AND THE SAME FOR THE FIELDS THE CARD STOPPED CARRYING: they are off
        # one wire, not off the record every in-process reader loads.
        for key in ("cv_scope", "harness_refs", "cwds", "notes", "edges"):
            self.assertIn(key, rec, "a wire projection took %r off the loaded "
                                    "record itself" % key)


class RegistryDetailSplitTest(unittest.TestCase):
    """WHAT A ROW DRAWS, AND WHAT ONLY AN OPEN ROW DRAWS.

    THE DEFECT the drops above stopped one field short of: after them the list
    still shipped 80,149 bytes over 234 projects, and the rest of it is DRAWN —
    but only once a project is opened. The retired card grid drew the pane for
    every project at paint time, so nine more fields rode every load of the
    home tab to render panes for the 233 the owner did not open: `first_seen`,
    `cv_scope`, `edges`, `cwds`, `memory_dir`, `harness_refs`, `home`,
    `active_days`, `notes`. The burn board's rows (task/2975) inherited the
    split: an open row fetches its pane.

    The cure is the shape `/api/task/notes` already set one surface over: a
    cheap list, and one row's own weight on its own route when a reader asks.
    These arms hold the seam — nothing a row draws may leave the list, nothing
    an open row draws may go missing from the route, and the two halves must
    together be the whole record.
    """

    def test_the_nine_fields_only_an_open_card_draws_left_the_list(self):
        row = web._project_on_the_wire(dict(_FULL_PROJECT), dict(_FULL_LIGHT))
        # THE UNCONDITIONAL CONTROL, before the loop: a row came back, and it
        # is the row that went in.
        self.assertEqual("alpha", row["name"])
        for key in ("first_seen", "cv_scope", "edges", "cwds", "memory_dir",
                    "harness_refs", "home", "active_days", "notes"):
            self.assertNotIn(key, row, "%r rode all 234 card rows to draw a "
                                       "pane only an open card shows" % key)

    def test_everything_that_left_the_list_is_on_the_detail_route(self):
        """A DROP MOVES A FIELD; IT MAY NOT LOSE ONE. Every key an open card
        draws is on the other door, carried whole."""
        det = web._project_detail_on_the_wire(dict(_FULL_PROJECT))
        # THE UNCONDITIONAL CONTROL on the same observable: this projection
        # returned content at all, so the assertions below are about which
        # keys it has rather than about a helper that answers `{}`.
        self.assertTrue(det, "the detail projection came back empty")
        for key in ("first_seen", "cv_scope", "edges", "cwds", "memory_dir",
                    "harness_refs", "home", "active_days", "notes"):
            self.assertIn(key, det, "%r left the card list and did not arrive "
                                    "on the detail route" % key)
        self.assertEqual("a note the detail pane draws", det["notes"],
                         "the detail route bounded a field the pane draws")
        self.assertEqual(_FULL_PROJECT["edges"], det["edges"])

    def test_the_two_halves_are_the_whole_record_and_never_overlap(self):
        """THE SEAM, STATED AS A PARTITION. A field on both wires is paid for
        twice; a field on neither is one a renderer will ask for and not get —
        and a new registry key must land on one side by DEFAULT rather than
        fall down the gap between two hand-kept allowlists."""
        rec = dict(_FULL_PROJECT, some_future_field="added next week")
        row = web._project_on_the_wire(dict(rec), dict(_FULL_LIGHT))
        det = web._project_detail_on_the_wire(dict(rec))
        # THE UNCONDITIONAL POSITIVE CONTROL on the SAME observable, in the
        # same call: this very intersection is NON-EMPTY when an overlap
        # exists. `name` is a card key by construction, so the expression
        # below is simultaneously "row ∩ det is empty" and "the operator finds
        # an overlap when there is one" — without it, `set() == set(row) &
        # set(det)` would also pass for two projections that came back empty.
        self.assertEqual({"name"}, set(row) & (set(det) | {"name"}),
                         "these keys ride both wires: %s"
                         % (set(row) & set(det),))
        # `light` and `edges_n` are MINTED by the row (a resolved light, a
        # count); `state`/`kind` are the no-reader drops. Everything else in
        # the record must be on exactly one of the two.
        covered = (set(row) | set(det) | {"state", "kind"}) - {"light",
                                                              "edges_n"}
        # AND THE SAME CONTROL FOR THE DIFFERENCE: a name no projection can
        # ever carry IS reported missing by this exact expression, so the
        # empty result for the real record means the record was covered
        # rather than that `-` found nothing to say.
        self.assertEqual({"never-classified"},
                         (set(rec) | {"never-classified"}) - covered,
                         "these record keys reach no wire at all: %s"
                         % (set(rec) - covered,))
        self.assertIn("some_future_field", det,
                      "a field nobody has classified fell off both wires "
                      "instead of defaulting to the detail route")

    def test_the_lineage_count_is_a_count_and_is_absent_at_zero(self):
        """THE CHIP ASKS A YES/NO AND WAS SENT THE ROWS. `edges_n` answers it
        under a DIFFERENT NAME, so no reader can iterate a shortened `edges`
        and believe it. And it is absent at zero because it was MEASURED: 10 of
        the owner's 234 projects have any lineage, so `"edges_n": 0` on the
        other 224 cost more than the arrays it replaced."""
        row = web._project_on_the_wire(dict(_FULL_PROJECT), dict(_FULL_LIGHT))
        self.assertEqual(1, row["edges_n"])
        self.assertNotIn("edges", row, "the count rode beside the rows it "
                                       "exists to replace")
        bare = dict(_FULL_PROJECT)
        bare.pop("edges")
        self.assertNotIn("edges_n", web._project_on_the_wire(
            bare, dict(_FULL_LIGHT)))
        self.assertNotIn("edges_n", web._project_on_the_wire(
            dict(_FULL_PROJECT, edges=[]), dict(_FULL_LIGHT)))

    def test_the_payload_names_the_door_once_not_on_every_row(self):
        """A FACT ABOUT THE DOOR IS NOT A FACT ABOUT A ROW. The first cut
        stamped the route onto all 234 rows and measured the cost: 32 bytes ×
        234 = 7.5 KB, which ate the whole trim it was announcing. It rides
        beside `generated_ts`, which the footer already reads off `reg`."""
        self.assertEqual("/api/project/detail", web.REGISTRY_DETAIL_ROUTE)
        row = web._project_on_the_wire(dict(_FULL_PROJECT), dict(_FULL_LIGHT))
        self.assertNotIn("detail", row)
        self.assertNotIn("detail_route", row)


class RegistryDetailRouteTest(unittest.TestCase):
    """THE DOOR ONE OPEN CARD KNOCKS ON, and the four things it can say.

    PARTIAL MUST NEVER READ AS COMPLETE. The pane has states no other section
    of this page has, and two of them are the pair that matters: "this project
    has nothing else on file" and "I could not find out" must never arrive as
    the same shape, because only one of them is worth clicking again.
    """

    def _reg(self, projects):
        return {"version": 1, "projects": projects}

    def test_a_project_with_nothing_else_answers_an_empty_detail_not_a_404(self):
        """AN EMPTY DICT IS AN ANSWER. Most of the owner's projects carry none
        of these keys, and `{}` from a door that FOUND the row is a different
        fact from a row that does not exist."""
        reg = self._reg({"bare": {"name": "bare", "path": "/fake/bare",
                                  "status": "active", "sessions": {}}})
        with mock.patch.object(web.registry, "load", lambda **k: reg):
            got, status = web._api_project_detail({"name": ["bare"]})
        self.assertEqual(200, status)
        self.assertEqual("bare", got["name"])
        self.assertIn("detail", got, "the reply had no `detail` key at all, so "
                                     "a reader cannot tell empty from missing")
        self.assertEqual({}, got["detail"])

    def test_a_project_that_does_not_exist_is_a_404_not_an_empty_detail(self):
        """THE CONTROL FOR THE ARM ABOVE, on the same observable: the same door,
        the same call shape, a different question, a different answer."""
        reg = self._reg({"bare": {"name": "bare"}})
        with mock.patch.object(web.registry, "load", lambda **k: reg):
            got, status = web._api_project_detail({"name": ["ghost"]})
            # UNCONDITIONAL POSITIVE CONTROL in the same call: the door DOES
            # answer 200 with a detail for a row it can find, so the 404 is
            # this row being absent rather than the door being broken.
            ok, ok_status = web._api_project_detail({"name": ["bare"]})
        self.assertEqual(404, status)
        self.assertNotIn("detail", got)
        self.assertIn("ghost", got["error"])
        self.assertEqual(200, ok_status)
        self.assertIn("detail", ok)

    def test_an_unreadable_registry_is_unavailable_and_never_an_empty_pane(self):
        """CANNOT SEE is not "nothing on file" — the state triad this file
        already enforces for the backlog holds one row down too. And the scope
        is the point: only the STORE READ is wrapped, so a bug of mine past
        that line raises and the server answers 500, which is loud and TRUE."""
        def boom(**k):
            raise OSError("registry unreadable")
        with mock.patch.object(web.registry, "load", boom):
            got, status = web._api_project_detail({"name": ["alpha"]})
        self.assertEqual(200, status)
        self.assertIs(True, got["unavailable"])
        self.assertNotIn("detail", got, "an unreadable registry answered with "
                                        "a detail key, which reads as empty")
        self.assertIn("registry unreadable", got["why"])

    def test_the_door_demands_a_name(self):
        got, status = web._api_project_detail({})
        self.assertEqual(400, status)
        self.assertIn("name", got["error"])

    def test_the_route_is_keyed_by_the_registry_key_the_light_setter_posts(self):
        """THE TRAP, PINNED. `light.key` is the key the owner's colour is POSTed
        to, and it is the key this door is read by. A card that resolved the two
        differently would open one project's pane while writing another's
        colour — so the registry key, not the display name, addresses both."""
        reg = self._reg({"alpha-under-another-name":
                         dict(_FULL_PROJECT, name="alpha")})
        with mock.patch.object(web.registry, "load", lambda **k: reg):
            by_key, key_status = web._api_project_detail(
                {"name": ["alpha-under-another-name"]})
            by_name, name_status = web._api_project_detail({"name": ["alpha"]})
        self.assertEqual(200, key_status)
        self.assertEqual("a note the detail pane draws",
                         by_key["detail"]["notes"])
        self.assertEqual(404, name_status,
                         "the door answered to the display name, so a card "
                         "could read one project and write another")
        self.assertNotIn("detail", by_name)

    def test_the_registry_payload_names_the_detail_door(self):
        """THE VERSION HANDSHAKE. A page served by a door that still sends
        whole records sees no `detail_route` and draws the pane from the row —
        so "detail is elsewhere" is something the browser is TOLD, never
        something it assumes."""
        reg = self._reg({"alpha": dict(_FULL_PROJECT)})
        with mock.patch.object(web.registry, "load", lambda **k: reg), \
                mock.patch.object(web.registry, "lights", lambda r: {}):
            got = web._api_registry()
        self.assertEqual("/api/project/detail", got["detail_route"])
        # THE UNCONDITIONAL CONTROL on the same payload: it carried the
        # projects too, so the route above rides a real answer.
        self.assertIn("alpha", got["projects"])


# THE BURN BOARD'S OPEN ROW AND EVERYTHING IT CALLS, lifted by name out of the
# shipped page. One list for both classes below, so the two cannot drift into
# driving different renderers.
BOARD_ROW_FNS = ("age", "ago", "light", "pkey", "lineageN", "lightset", "dmsg",
                 "detailBody", "detailPane", "lrDur", "lrAgo", "flagWhen",
                 "boardSec", "boardSecState", "boardSecWord", "boardChip",
                 "boardChips", "boardCount", "boardLanes", "boardLaneWord",
                 "boardProgress",
                 "boardRepoBadge", "boardRepos", "boardKanban",
                 "boardKanbanHTML", "boardKanbanCount", "boardFoldLine",
                 "boardWide", "boardWaits", "boardDetail",
                 "boardRowHTML")


class RegistryWireReadersTest(unittest.TestCase):
    """THE BROWSER HALF, RUN RATHER THAN GREPPED.

    A no-reader claim proved by searching for a field name is only as good as
    the spellings the searcher thought of. This lifts the burn board's project
    row and everything it composes — `boardRowHTML`, `boardDetail`,
    `lightset`, `light` and the pane — out of the SHIPPED page, runs them under
    node over the row the trimmed door emits and over the untrimmed record,
    OPEN, and compares the HTML. Identical HTML means no renderer lost a byte.
    (The row replaced the project card, task/2975; the question is unchanged.)

    Node is optional on non-web hosts: absent, this SKIPS.
    """

    EXTRACT = BOARD_ROW_FNS
    CONSTS = ("LIGHTS", "FLAGCOL", "KANBAN_OF")
    DECLS = ("DETAIL_HAVE", "DETAIL_ROUTE", "BOARD_DIRTY")

    SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
"""
    # THE WHOLE-RECORD DOOR, DELIBERATELY. This class asks ONE question — do
    # `state` and `kind` have a reader — and the answer must not depend on
    # where the pane is fetched from. `DETAIL_ROUTE = null` draws the pane
    # inline from the row, which is the door that existed when these two
    # fields were dropped; the lazy-detail split gets its own class below.
    DRIVER = r"""
DETAIL_ROUTE = null;
const out = {full: boardRowHTML(FULL, null, true), trim: boardRowHTML(TRIM, null, true),
             lossy: boardRowHTML(LOSSY, null, true)};
out.key_full = lightset(FULL).match(/data-key='([^']*)'/)[1];
out.key_trim = lightset(TRIM).match(/data-key='([^']*)'/)[1];
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        import shutil as _sh
        import subprocess
        from tests.test_web_chat_client_runtime import _extract_fn
        from tests.test_web_accounts import _extract_const
        cls.node = _sh.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        full = dict(_FULL_PROJECT, light=dict(_FULL_LIGHT))
        # THE TWO DROPS AND NOTHING ELSE. `_project_on_the_wire` also moves the
        # pane's nine fields onto their own route now, and folding that into
        # this comparison would mean this class could no longer say which of
        # the two changes an inequality came from. `REGISTRY_UNREAD_KEYS` is
        # lifted from the module rather than typed, so a field added to the
        # drop list arrives here instead of being silently untested.
        trim = {k: v for k, v in full.items()
                if k not in web.REGISTRY_UNREAD_KEYS}
        # LOSSY drops `notes` — a field the detail pane DOES draw. It is the
        # unconditional positive control for the comparison itself: without it
        # "trim renders as full" would also pass for a row that returned a
        # constant, or for two inputs neither renderer can see.
        lossy = dict(full)
        lossy.pop("notes")
        src = web_ui_loader.read_text()
        import re as _re
        fns = "\n\n".join(_extract_fn(src, n) for n in cls.EXTRACT)
        consts = "\n".join(_extract_const(src, n) for n in cls.CONSTS)

        def decl(name):
            m = _re.search(r"^(?:const|let) %s = .*;$" % _re.escape(name),
                           src, _re.M)
            assert m, "no declaration of %s in the assembled page" % name
            return m.group(0)

        decls = "\n".join(decl(n) for n in cls.DECLS)
        payload = ("const FULL = %s;\nconst TRIM = %s;\nconst LOSSY = %s;\n"
                   % (json.dumps(full), json.dumps(trim), json.dumps(lossy)))
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-registry-readers-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(cls.SUPPORT + consts + "\n" + decls + "\n" + payload
                    + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: "
                        + (self.proc.stderr or "")[:400])

    def test_the_shipped_row_cannot_tell_the_trimmed_row_from_the_full_one(self):
        """THE NO-READER PROOF, EXECUTED. The open board row, its pane and its
        light-setter draw byte-identical HTML from the trimmed row and the
        untrimmed record."""
        # THE UNCONDITIONAL CONTROL on the same observable: both sides are a
        # rendered row. Two empty strings are also equal.
        self.assertIn("<article class=\"brow", self.out["full"])
        self.assertIn("<article class=\"brow", self.out["trim"])
        self.assertEqual(self.out["full"], self.out["trim"],
                         "a shipped renderer saw a difference the trim made")

    def test_the_comparison_can_see_a_lost_field_so_the_equality_means_something(self):
        """THE UNCONDITIONAL POSITIVE CONTROL, in the same node run. `notes` is
        a field the detail pane DOES draw, and a row missing it renders
        DIFFERENTLY — so the equality above is the trim being invisible rather
        than `boardRowHTML()` being blind."""
        # THE UNCONDITIONAL CONTROL on the same observable: the lossy side is a
        # rendered row too, so the inequality is one field's worth of
        # difference rather than a renderer that returned nothing.
        self.assertIn("<article class=\"brow", self.out["lossy"])
        self.assertNotEqual(self.out["full"], self.out["lossy"],
                            "dropping a field the page draws changed nothing, "
                            "so this comparison proves nothing about the trim")

    def test_the_trimmed_row_addresses_the_same_project_it_always_did(self):
        """`lightset` is the element that POSTs the owner's colour, so the key
        it carries is the project that gets written. A trim that moved it would
        write his colour onto a project he was not looking at."""
        self.assertEqual("alpha", self.out["key_trim"])
        self.assertEqual(self.out["key_full"], self.out["key_trim"],
                         "the trimmed row addressed a different project than "
                         "the full one")

    def test_the_row_actually_drew_the_fields_this_fixture_carries(self):
        """MUST-HIT. Two rows that both render nothing are also equal."""
        html = self.out["trim"]
        self.assertIn("a note the detail pane draws", html)
        self.assertIn("critical path only", html)
        self.assertIn("/fake/dev/alpha", html)
        self.assertIn("lineage", html)


class RegistryLazyDetailRuntimeTest(unittest.TestCase):
    """THE BROWSER HALF OF THE SPLIT, RUN RATHER THAN GREPPED.

    The class above proves the two wires partition the record. It cannot prove
    the thing that actually matters to the owner: that the pane he ends up
    LOOKING AT is the pane he looked at before, and that the three states it
    can be in on the way there are three different sentences rather than one
    blank box.

    So the real renderers — the burn board's open row (`boardRowHTML`,
    `boardDetail`), `detailPane`, `detailBody`, `lightset`, `light`, `pkey`,
    `lineageN`, `dmsg` — are lifted verbatim out of the SHIPPED page and driven
    under node across all four states of one row.

    Node is optional on non-web hosts: absent, this SKIPS.
    """

    EXTRACT = BOARD_ROW_FNS
    CONSTS = ("LIGHTS", "FLAGCOL", "KANBAN_OF")
    DECLS = ("DETAIL_HAVE", "DETAIL_ROUTE", "BOARD_DIRTY")

    SUPPORT = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
"""
    # ONE ROW, FIVE DRAWS. The first is the OLD door — whole records, no
    # `detail_route` — which is the pane the owner has today and therefore the
    # thing every later draw is measured against.
    DRIVER = r"""
const out = {};
const row = p => boardRowHTML(p, null, true);
DETAIL_ROUTE = null;
out.before = row(FULL);
out.before_lossy = row(LOSSY);

DETAIL_ROUTE = "/api/project/detail";
out.pending = row(TRIM);
DETAIL_HAVE.set("alpha", DETAIL);
out.fetched = row(TRIM);
DETAIL_HAVE.set("alpha", {});
out.alpha_empty = row(TRIM);
DETAIL_HAVE.set("alpha", false);
out.failed = row(TRIM);

/* A PROJECT WITH NOTHING ELSE ON FILE AT ALL — no detail AND no sessions, so
   the pane really would be blank. Most of the owner's registry looks like
   this, and it is the only row for which "nothing else on file" is true. */
out.bare_pending = row(BARE);
DETAIL_HAVE.set("bare", {});
out.none = row(BARE);
DETAIL_HAVE.set("bare", false);
out.bare_failed = row(BARE);
out.keys = {pending: pkey(TRIM), before: pkey(FULL)};
console.log(JSON.stringify(out));
"""

    @classmethod
    def setUpClass(cls):
        import re as _re
        import shutil as _sh
        import subprocess
        from tests.test_web_chat_client_runtime import _extract_fn
        from tests.test_web_accounts import _extract_const
        cls.node = _sh.which("node")
        if not cls.node:
            raise unittest.SkipTest("node not available")
        src = web_ui_loader.read_text()

        def decl(name):
            """The SHIPPED one-line declaration, verbatim — a mirror of
            `new Map()` written here would be a mirror all the same."""
            m = _re.search(r"^(?:const|let) %s = .*;$" % _re.escape(name),
                           src, _re.M)
            assert m, "no declaration of %s in the assembled page" % name
            return m.group(0)

        full = dict(_FULL_PROJECT, light=dict(_FULL_LIGHT))
        trim = web._project_on_the_wire(dict(_FULL_PROJECT), dict(_FULL_LIGHT))
        detail = web._project_detail_on_the_wire(dict(_FULL_PROJECT))
        # LOSSY drops `notes`, a field the pane DOES draw, and is drawn through
        # the OLD door beside `before`. It is the unconditional positive
        # control for the comparison itself: without it "fetched equals before"
        # would also pass for a row renderer that returned a constant.
        lossy = dict(full)
        lossy.pop("notes")
        bare = web._project_on_the_wire(
            {"name": "bare", "path": "/fake/dev/bare", "status": "dormant",
             "last_seen": 1900000000.0, "sessions": {}},
            dict(_FULL_LIGHT, key="bare"))
        fns = "\n\n".join(_extract_fn(src, n) for n in cls.EXTRACT)
        consts = "\n".join(_extract_const(src, n) for n in cls.CONSTS)
        decls = "\n".join(decl(n) for n in cls.DECLS)
        payload = ("const FULL = %s;\nconst TRIM = %s;\nconst DETAIL = %s;\n"
                   "const LOSSY = %s;\nconst BARE = %s;\n"
                   % (json.dumps(full), json.dumps(trim), json.dumps(detail),
                      json.dumps(lossy), json.dumps(bare)))
        cls.tmp = tempfile.mkdtemp(prefix="helm-web-lazy-detail-")
        cls.path = os.path.join(cls.tmp, "run.js")
        with open(cls.path, "w", encoding="utf-8") as f:
            f.write(cls.SUPPORT + consts + "\n" + decls + "\n" + payload
                    + fns + cls.DRIVER)
        chk = subprocess.run([cls.node, "--check", cls.path],
                             capture_output=True, text=True)
        assert chk.returncode == 0, "node --check failed:\n" + chk.stderr
        cls.proc = subprocess.run([cls.node, cls.path], capture_output=True,
                                  text=True, timeout=60)
        try:
            cls.out = json.loads(cls.proc.stdout or "{}")
        except json.JSONDecodeError:
            cls.out = {}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def setUp(self):
        self.assertTrue(self.out, "node produced no output: "
                        + (self.proc.stderr or "")[:400])

    def test_the_fetched_pane_is_byte_identical_to_the_one_he_has_today(self):
        """THE WHOLE POINT, EXECUTED. Once the detail arrives, the split row
        draws exactly the HTML the whole-record row drew — lineage, notes,
        harnesses, working dirs, pointers, activity and the light-setter."""
        # THE UNCONDITIONAL CONTROL on the same observable: both sides are a
        # rendered row. Two empty strings are also equal.
        self.assertIn('<article class="brow', self.out["before"])
        self.assertIn('<article class="brow', self.out["fetched"])
        self.assertEqual(self.out["before"], self.out["fetched"],
                         "moving the pane onto its own route changed what the "
                         "owner sees once it lands")

    def test_the_comparison_can_see_a_lost_field_so_that_equality_means_something(self):
        """THE UNCONDITIONAL POSITIVE CONTROL, in the same node run. `notes` is
        a field the pane DOES draw, and a record missing it renders DIFFERENTLY
        through the very same door — so the equality above is the split being
        invisible rather than the row renderer being blind."""
        self.assertIn('<article class="brow', self.out["before_lossy"])
        self.assertNotEqual(self.out["before"], self.out["before_lossy"],
                            "dropping a field the pane draws changed nothing, "
                            "so the equality above proves nothing")

    def test_not_fetched_yet_never_draws_as_an_answer(self):
        """PARTIAL MUST NOT READ AS COMPLETE. Before the fetch lands the pane
        says it is waiting; it does not show an empty box, which reads as
        "there is nothing here" — an answer this card has not got yet."""
        pending = self.out["pending"]
        self.assertIn("data-state='pending'", pending)
        self.assertIn("loading", pending)
        self.assertNotIn("a note the detail pane draws", pending,
                         "an unfetched pane drew detail it cannot have")
        self.assertNotEqual(pending, self.out["fetched"])

    def test_nothing_on_file_and_could_not_read_are_different_sentences(self):
        """THE PAIR THAT MATTERS, and the one a merged shape could not tell
        apart. Only one of them is worth clicking again."""
        none, failed = self.out["none"], self.out["bare_failed"]
        self.assertIn("data-state='none'", none)
        self.assertIn("nothing else on file", none)
        self.assertIn("data-state='failed'", failed)
        self.assertIn("could not read", failed)
        self.assertNotEqual(none, failed,
                            "an empty project and an unreadable one drew the "
                            "same pane")
        # AND NEITHER MAY BE MISTAKEN FOR NOT-ASKED-YET.
        self.assertNotEqual(none, self.out["bare_pending"])
        self.assertNotEqual(failed, self.out["bare_pending"])

    def test_an_empty_detail_never_blanks_a_pane_the_card_can_still_draw(self):
        """THE ARM THAT CAUGHT ITSELF. `{}` from the detail door does NOT mean
        the pane is empty: the harnesses section is built from `sessions`,
        which is a CARD field and was never on the detail wire at all. So a
        project whose detail is genuinely empty still draws its harnesses, and
        saying "nothing else on file" over the top of a section the owner can
        see would be the same lie one layer up. "Nothing else on file" is
        reserved for a pane that would otherwise be blank."""
        empty = self.out["alpha_empty"]
        self.assertIn("harnesses", empty)
        self.assertIn("<b>codex</b>", empty)
        self.assertNotIn("nothing else on file", empty)
        # THE CONTROL on the same observable, in the same node run: the phrase
        # IS reachable — a row with no sessions and no detail draws it — so the
        # absence above is this pane having content, not a dead branch.
        self.assertIn("nothing else on file", self.out["none"])
        # and the detail-only sections are still gone, so `{}` was honoured
        self.assertNotIn("a note the detail pane draws", empty)

    def test_the_light_is_drawn_in_every_state_because_it_is_never_fetched(self):
        """THE ONE CONTROL ON THIS PANE THE OWNER ACTS WITH. It is already on
        the card row, and a colour he could not set because a second request
        failed would be a worse surface than the bytes ever cost."""
        # THE UNCONDITIONAL POSITIVE CONTROL on the same observable, before any
        # loop can decide not to run: a rendered card IS in hand, and this
        # marker IS detectable in it — so a per-state absence below is the
        # setter having vanished rather than the assertion never firing.
        self.assertIn("data-key='alpha'", self.out["fetched"])
        self.assertNotIn("data-key='bare'", self.out["fetched"])
        for state, key in (("pending", "alpha"), ("fetched", "alpha"),
                           ("alpha_empty", "alpha"), ("failed", "alpha"),
                           ("bare_pending", "bare"), ("none", "bare"),
                           ("bare_failed", "bare")):
            html = self.out[state]
            # AND IT ADDRESSES ITS OWN PROJECT, in every state. The key is what
            # the colour is POSTed to, so a pane that kept the setter but lost
            # the key would write the owner's colour onto whatever the page
            # fell back to.
            self.assertIn("data-key='%s'" % key, html,
                          "the light-setter vanished or moved in %r" % state)
            self.assertIn("critical path only", html,
                          "the light's own reason vanished in %r" % state)

    def test_the_lineage_chip_is_drawn_before_any_detail_is_fetched(self):
        """`edges_n` EARNS ITS KEY. The chip is a yes/no the card answers from
        the list, so it is right on first paint — while the lineage ROWS wait
        for the pane they belong to."""
        self.assertIn("lineage", self.out["pending"])
        # THE CONTROL on the same observable: the pane's lineage ROWS are what
        # is absent, so the chip above is the count working rather than the
        # edges having quietly ridden along.
        self.assertNotIn("forked-from", self.out["pending"])
        self.assertIn("forked-from", self.out["fetched"])

    def test_the_split_card_addresses_the_same_project_it_always_did(self):
        self.assertEqual("alpha", self.out["keys"]["pending"])
        self.assertEqual(self.out["keys"]["before"],
                         self.out["keys"]["pending"])


if __name__ == "__main__":
    unittest.main()
