#!/usr/bin/env python3
"""Fleet presence — the ICQ-style glance. `helm chat status` round-trips
through THE roster writer (same flock — never a second writer path), the one
status line composes with the fixed precedence (explicit > claim > home), the
web poll carries the presence bar's data, and a stale seat renders ⚫.
Hermetic: tmp HELM_HOME + HELM_CHAT_DIR; the real ~/.helm and
/dev/shm/helm-chat are never touched."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME")


class PresenceBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="helm-test-presence-")
        cls.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(cls.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(cls.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""  # transport off — hermetic
        cls.cwd_prior = os.getcwd()
        os.chdir(cls.tmp)  # no git cwd — homing defaults stay 'main'

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd_prior)
        for k, v in cls.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        shutil.rmtree(os.environ["HELM_CHAT_DIR"], ignore_errors=True)


class StatusVerbTest(PresenceBase):
    """`helm chat status` — the roster round-trip, through the real writer."""

    def _cmd(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd("status", list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_set_show_clear_roundtrip(self):
        seats.write_roster("lane-a", session="s" * 32, cwd=self.tmp)
        rc, out, _ = self._cmd(["shipping", "the", "presence", "bar",
                                "--seat", "lane-a"])
        self.assertEqual(rc, 0)
        self.assertIn("shipping the presence bar", out)
        row = seats.roster()["lane-a"]          # the REAL roster, on disk
        self.assertEqual(row["status"], "shipping the presence bar")
        self.assertIsInstance(row["status_ts"], float)
        rc, out, _ = self._cmd(["--seat", "lane-a"])   # bare: show
        self.assertEqual(rc, 0)
        self.assertIn("shipping the presence bar", out)
        self.assertIn("(status)", out)
        rc, out, _ = self._cmd(["--clear", "--seat", "lane-a"])
        self.assertEqual(rc, 0)
        row = seats.roster()["lane-a"]
        self.assertNotIn("status", row)
        self.assertNotIn("status_ts", row)

    def test_status_needs_a_row(self):
        rc, _, err = self._cmd(["doing things", "--seat", "never-joined"])
        self.assertEqual(rc, 1)
        self.assertIn("no roster row", err)

    def test_status_is_scrubbed_and_clipped(self):
        seats.write_roster("lane-b", session="t" * 32, cwd=self.tmp)
        ok, _ = seats.set_status("lane-b", "one\nline\x07 only  " + "x" * 400)
        self.assertTrue(ok)
        got = seats.roster()["lane-b"]["status"]
        self.assertNotIn("\n", got)
        self.assertNotIn("\x07", got)
        self.assertLessEqual(len(got.encode("utf-8")),
                             seats.STATUS_BYTES + len("…".encode("utf-8")))

    def test_concurrent_writers_hold_the_flock(self):
        """Two seats setting statuses in parallel: the RMW rides the roster
        flock, so neither row (nor either final status) is ever lost."""
        for s in ("race-a", "race-b"):
            seats.write_roster(s, session=s * 8, cwd=self.tmp)

        def spin(seat):
            for i in range(25):
                ok, _ = seats.set_status(seat, "%s pass %d" % (seat, i))
                assert ok

        ts = [threading.Thread(target=spin, args=(s,))
              for s in ("race-a", "race-b")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        r = seats.roster()
        self.assertEqual(r["race-a"]["status"], "race-a pass 24")
        self.assertEqual(r["race-b"]["status"], "race-b pass 24")


class PrecedenceTest(PresenceBase):
    """explicit status > live claim > home room — the ONE line, one law."""

    def test_precedence_ladder(self):
        seats.write_roster("coder", session="u" * 32, cwd=self.tmp,
                           home_room="helm", home_room_source="explicit")
        row = seats.roster()["coder"]
        # floor: no status, no claim -> the home room
        self.assertEqual(seats.status_line(row), ("in #helm", "home"))
        # a live worktree lease lifts it to the claim tier
        ok, _, lease = seats.claim("worktree:helm:web-presence", "coder",
                                   ttl=3600)
        self.assertTrue(ok)
        claim = seats._claims_by_holder().get("coder")
        line, source = seats.status_line(row, claim)
        self.assertEqual(source, "claim")
        self.assertIn("working lane/web-presence (helm)", line)
        self.assertIn("left", line)
        # an explicit status beats the claim
        seats.set_status("coder", "reviewing codex's diff")
        row = seats.roster()["coder"]
        self.assertEqual(seats.status_line(row, claim),
                         ("reviewing codex's diff", "status"))
        # cleared -> falls back to the claim, then the home floor
        seats.set_status("coder", None)
        row = seats.roster()["coder"]
        self.assertEqual(seats.status_line(row, claim)[1], "claim")
        seats.release("worktree:helm:web-presence", "coder", lease=lease)
        self.assertEqual(seats.status_line(row,
                                           seats._claims_by_holder().get("coder")),
                         ("in #helm", "home"))

    def test_non_worktree_claim_reads_as_holds(self):
        seats.write_roster("porter", session="v" * 32, cwd=self.tmp)
        seats.claim("port:8317", "porter", ttl=120)
        line, source = seats.status_line({}, seats._claims_by_holder()["porter"])
        self.assertEqual(source, "claim")
        self.assertIn("holds port:8317", line)

    def test_presence_report_composes_the_same_line(self):
        seats.write_roster("coder", session="w" * 32, cwd=self.tmp,
                           home_room="helm", home_room_source="explicit")
        seats.claim("worktree:helm:web-presence", "coder", ttl=3600)
        seats.set_status("coder", "wiring the bar")
        rows = {r["seat"]: r for r in seats.presence_report()}
        self.assertEqual(rows["coder"]["line"], "wiring the bar")
        self.assertEqual(rows["coder"]["source"], "status")
        self.assertEqual(rows["coder"]["presence"], "fresh")
        self.assertEqual(rows["coder"]["dot"], "\U0001f7e2")


class StalePresenceTest(PresenceBase):
    def test_stale_seat_renders_black(self):
        seats.write_roster("ghost", session="x" * 32, cwd=self.tmp)
        old = time.time() - 3 * 3600
        os.utime(seats.seen_path("ghost"), (old, old))  # backdate the beat
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            r["ghost"]["last_seen"] = old
            from helm import pk
            pk.write_json(seats.roster_path(), r)
        rows = {x["seat"]: x for x in seats.presence_report()}
        self.assertEqual(rows["ghost"]["presence"], "absent")
        self.assertEqual(rows["ghost"]["dot"], "⚫")
        # absent sorts last so the live fleet leads the bar
        seats.write_roster("live", session="y" * 32, cwd=self.tmp)
        order = [x["seat"] for x in seats.presence_report()]
        self.assertEqual(order, ["live", "ghost"])


class WebPresenceTest(PresenceBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = web.make_server(0)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(timeout=5)
        super().tearDownClass()

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read()

    def test_poll_carries_the_presence_bar(self):
        """GET /api/chat — the existing ~2s poll — now carries `presence`:
        one light row per seat with the dot + the composed status line."""
        seats.write_roster("coder", session="z" * 32, cwd=self.tmp)
        seats.set_status("coder", "wiring the presence bar")
        st, body = self.get("/api/chat?since=0")
        self.assertEqual(st, 200)
        d = json.loads(body)
        rows = {r["seat"]: r for r in d.get("presence") or []}
        self.assertIn("coder", rows)
        self.assertEqual(rows["coder"]["line"], "wiring the presence bar")
        self.assertEqual(rows["coder"]["source"], "status")
        self.assertEqual(rows["coder"]["dot"], "\U0001f7e2")

    def test_roster_endpoint_carries_status_and_line(self):
        seats.write_roster("coder", session="z" * 32, cwd=self.tmp,
                           home_room="helm", home_room_source="explicit")
        st, body = self.get("/api/chat/roster")
        self.assertEqual(st, 200)
        d = json.loads(body)
        row = next(s for s in d["seats"] if s["seat"] == "coder")
        self.assertEqual(row["line"], "in #helm")
        self.assertEqual(row["source"], "home")
        self.assertIn("dot", row)

    def test_stale_seat_serves_black_dot(self):
        seats.write_roster("ghost", session="q" * 32, cwd=self.tmp)
        old = time.time() - 3 * 3600
        os.utime(seats.seen_path("ghost"), (old, old))
        with seats._flocked(seats.roster_path() + ".lock"):
            from helm import pk
            r = seats.roster()
            r["ghost"]["last_seen"] = old
            pk.write_json(seats.roster_path(), r)
        st, body = self.get("/api/chat?since=0")
        d = json.loads(body)
        row = next(r for r in d["presence"] if r["seat"] == "ghost")
        self.assertEqual(row["presence"], "absent")
        self.assertEqual(row["dot"], "⚫")

    def test_ui_ships_the_bar(self):
        st, body = self.get("/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8")
        self.assertIn('id="chatpresence"', html)
        self.assertIn("function chatPresence", html)


if __name__ == "__main__":
    unittest.main()
