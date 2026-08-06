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

RUNTIME_ENV_KEYS = ("HELM_AGENT_HARNESS", "HELM_MODEL_FAMILY",
                    "HELM_MODEL_BACKEND", "PI_CODING_AGENT",
                    "HELM_PI_PROXY_KEY")
ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_PROC",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL",
            "HELM_CELL_BIN", "MELD_CELL_BIN",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME") + RUNTIME_ENV_KEYS


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
        # DETERMINISTIC LIVENESS. A claim minted here records no session,
        # so the classifier falls to a /proc scan for the HOLDER NAME —
        # and against the real /proc that answers "live" or "unknown"
        # depending on whether some unrelated process happens to carry
        # the string. "porter" did on this box and "coder" did not, so
        # the same fixture classified two ways and the suite disagreed
        # with the fab node. An empty root makes the answer a DECISION.
        os.makedirs(os.path.join(cls.tmp, "proc"), exist_ok=True)
        os.environ["HELM_PROC"] = os.path.join(cls.tmp, "proc")
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
        for key in RUNTIME_ENV_KEYS:
            os.environ.pop(key, None)


class RuntimeMetadataTest(PresenceBase):
    def test_runtime_metadata_persists_and_reaches_every_roster_projection(self):
        runtime = {"agent_harness": "pi", "family": "codex",
                   "backend": "proxy"}
        seats.write_roster("pi-codex", session="p" * 32, cwd=self.tmp,
                           runtime=runtime)
        self.assertEqual(seats.roster()["pi-codex"]["runtime"], runtime)
        full = seats.roster_report()["seats"][0]
        light = seats.presence_report()[0]
        self.assertEqual(full["runtime"], runtime)
        self.assertEqual(light["runtime"], runtime)
        self.assertEqual(seats.runtime_label(full), "codex · pi/proxy")
        seats.write_roster("z-legacy", session="l" * 32, cwd=self.tmp)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seats.cmd("seats", []), 0)
        text = out.getvalue()
        self.assertIn("[codex · pi/proxy]", text)
        lines = [line for line in text.splitlines() if " pending " in line]
        self.assertEqual(len(lines), 2)
        self.assertEqual(len({line.index("pending") for line in lines}), 1)

    def test_join_reads_pi_harness_and_canonical_backend_without_the_key(self):
        seats.write_roster(
            "pi-worker", session="q" * 32, cwd=self.tmp,
            runtime={"agent_harness": "claude", "family": "old",
                     "backend": "native"})
        os.environ["PI_CODING_AGENT"] = "true"
        os.environ["HELM_MODEL_BACKEND"] = "proxy"
        os.environ["HELM_PI_PROXY_KEY"] = "secret-never-store"
        with contextlib.redirect_stdout(io.StringIO()):
            seats.join("q" * 32, cwd=self.tmp, seat="pi-worker")
        text = json.dumps(seats.roster()["pi-worker"])
        self.assertEqual(seats.roster()["pi-worker"]["runtime"],
                         {"agent_harness": "pi", "backend": "proxy"})
        self.assertNotIn("secret-never-store", text)

    def test_explicit_runtime_never_composes_with_ambient_launch_labels(self):
        os.environ["HELM_MODEL_FAMILY"] = "stale-family"
        os.environ["HELM_MODEL_BACKEND"] = "native"
        seats.write_roster(
            "pi-worker", session="q" * 32, cwd=self.tmp,
            runtime={"agent_harness": "pi"})
        self.assertEqual(seats.roster()["pi-worker"]["runtime"],
                         {"agent_harness": "pi"})

    def test_legacy_join_stays_unlabelled_instead_of_guessing_from_name(self):
        seats.write_roster("pi-looking-name", session="r" * 32, cwd=self.tmp)
        self.assertNotIn("runtime", seats.roster()["pi-looking-name"])
        self.assertEqual(seats.runtime_label(seats.roster_report()["seats"][0]), "")


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
        # THE VERB IS "UNVERIFIED", NOT "working", AND THAT IS THE POINT OF
        # THE CLAIM-TIER FIX: this fixture mints a claim with NO session, so
        # its holder cannot be proven live OR dead. Rendering it "working"
        # is the collapse the liveness meld removed — an unprovable hold
        # read exactly like a healthy one. The LADDER assertion is what this
        # test is for and it is unchanged; only the honest word moved.
        # (Measured 2026-08-04: 4 of 4 live production claims DO carry a
        # session, so UNVERIFIED is an edge case, not the common render.)
        self.assertIn("UNVERIFIED hold on lane/web-presence (helm)", line)
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
        # Same claim-tier fix on the NON-worktree branch: a sessionless
        # hold is unprovable, so it says so rather than reading as settled
        # work. The tier assertion above is what this test is for.
        self.assertIn("UNVERIFIED hold on port:8317", line)

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


class ClaimTierScrubTest(PresenceBase):
    """Reviewer pin: the claim tier is roster-borne content like any other —
    a hostile claim resource must leave status_line scrubbed + clipped, and
    `helm chat seats` must never print raw ESC/bidi to the operator's
    terminal (the planted \\x1b[2J screen-clear probe)."""

    def _seats_out(self, args=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = seats.cmd("seats", list(args))
        return rc, out.getvalue()

    def test_hostile_claim_resource_is_scrubbed(self):
        seats.write_roster("victim", session="h" * 32, cwd=self.tmp)
        ok, _, _ = seats.claim("bad\x1b[2J\x1b[31m‮resource", "victim",
                               ttl=600)
        self.assertTrue(ok)
        line, source = seats.status_line(
            seats.roster()["victim"], seats._claims_by_holder()["victim"])
        self.assertEqual(source, "claim")
        self.assertNotIn("\x1b", line)      # no screen-clear
        self.assertNotIn("‮", line)    # no bidi reorder (Cf)
        self.assertIn("resource", line)
        rc, out = self._seats_out()         # the CLI glance stays inert too
        self.assertEqual(rc, 0)
        # the WHOLE output — per-seat rows AND the claims footer. (An earlier
        # pin carved the footer out with out.split("claim:")[0]; the footer
        # printed the raw resource and cleared the operator's terminal.)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        self.assertIn("claim:", out)        # the footer still shows, scrubbed
        self.assertIn("resource", out)

    def test_planted_home_room_and_todo_are_scrubbed(self):
        """The other roster-borne columns of the same rows: a planted
        home_room must not reshape the `home #…` column, and a todo whose
        active text carries ESC (capture's whitespace-collapse keeps \\x1b)
        must not reshape the task cell."""
        from helm import pk, todos
        sid = "s" * 32
        seats.write_roster("victim", session=sid, cwd=self.tmp)
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            r["victim"]["home_room"] = "room\x1b[2J‮pwn"
            r["victim"]["home_room_source"] = "exp\x1b]0;t\x07licit"
            pk.write_json(seats.roster_path(), r)
        os.makedirs(os.path.dirname(todos.state_path(sid)), exist_ok=True)
        pk.write_json(todos.state_path(sid), {
            "v": 1, "ts": time.time(),
            "items": [{"id": "1", "text": "evil\x1b[31m‮task",
                       "status": "in_progress"}]})
        rc, out = self._seats_out(["--all"])
        self.assertEqual(rc, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        self.assertIn("#room", out)          # the column survives, laundered
        self.assertIn("task", out)
        rep = {s["seat"]: s for s in seats.roster_report()["seats"]}
        self.assertNotIn("\x1b", rep["victim"]["home_room"])   # web copy too
        self.assertNotIn("\x1b", rep["victim"]["home_room_source"])
        self.assertNotIn("\x1b", rep["victim"]["todo"]["active"])

    def test_claims_verb_is_scrubbed(self):
        """`helm chat claims` reads the same public table — the standalone
        listing must be as inert as the seats footer."""
        seats.write_roster("victim", session="c" * 32, cwd=self.tmp)
        seats.claim("bad\x1b[2J‮res", "victim", ttl=600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seats.cmd("claims", []), 0)
        got = out.getvalue()
        self.assertNotIn("\x1b", got)
        self.assertNotIn("‮", got)
        self.assertIn("res -> victim", got)

    def test_giant_claim_resource_is_clipped(self):
        seats.write_roster("victim", session="i" * 32, cwd=self.tmp)
        seats.claim("r" * 1000, "victim", ttl=600)
        line, _ = seats.status_line(
            seats.roster()["victim"], seats._claims_by_holder()["victim"])
        self.assertLessEqual(len(line.encode("utf-8")),
                             seats.STATUS_BYTES + len("…".encode("utf-8")))


# the two payload markers every roster surface must strip: a screen-clear
# CSI (Cc) and a right-to-left override (Cf, reorders the whole line).
ESC, BIDI = "\x1b", "‮"


def _walk_strings(v):
    """Every string reachable in a report value — dict values, list items,
    nested. The completeness guard walks the OUTPUT schema, so a NEW string
    field added to a roster row (without laundering) is caught with no test
    edit: it simply shows up here carrying the planted payload."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _walk_strings(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _walk_strings(x)


class RosterLaunderCompletenessTest(PresenceBase):
    """r4 pin — CLOSE THE CLASS, don't patch a 5th site. The seat KEY was the
    one roster-borne string that still reached the operator terminal raw
    (status tier → footer → todos → seat key was the 4th iteration of the
    SAME display-laundering bug). This enumerates EVERY roster-borne string
    field, plants an ESC+bidi payload in each, and asserts it is absent from
    the FULL output of every roster-printing verb AND from every string in
    the roster_report JSON — source-driven over the output schema so a 5th
    surface (or a new field) cannot be born unlaundered."""

    # a hostile seat KEY (the unvalidated HELM_CHAT_NAME join seam) — the
    # most prominent, first-printed column on every glance surface.
    SEAT = "lane" + ESC + "[2J" + BIDI + "pwn"

    def _plant(self):
        """One victim carrying the payload in every roster-borne field: the
        seat KEY, the display columns (project/cwd/home_room/source), the
        explicit status + its cross-seat writer, a live claim (resource +
        holder), and the todo cell. A legit session keeps the cursor plumbing
        alive so no field falls to the fail-open '?' row."""
        from helm import pk, todos
        sid = "z" * 32
        seats.write_roster(self.SEAT, session=sid, cwd=self.tmp)
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            row = r[self.SEAT]
            row["project"] = "proj" + ESC + "[31m" + BIDI + "X"
            row["cwd"] = "/tmp/" + ESC + "]0;t\x07" + BIDI + "cwd"
            row["home_room"] = "room" + ESC + "[2J" + BIDI + "pwn"
            row["home_room_source"] = "exp" + ESC + "]0;t\x07" + BIDI + "licit"
            row["status"] = "busy" + ESC + "[2J" + BIDI + "wiping"
            row["status_ts"] = time.time()        # FRESH: the status tier wins
            row["status_by"] = "boss" + ESC + "[31m" + BIDI + "man"
            row["runtime"] = {
                "agent_harness": "pi" + ESC + "[2J" + BIDI + "h",
                "family": "codex" + ESC + "[31m" + BIDI + "f",
                "backend": "proxy" + ESC + "]0;t\x07" + BIDI + "b"}
            pk.write_json(seats.roster_path(), r)
        seats.claim("res" + ESC + "[2J" + BIDI + "ource",
                    self.SEAT, ttl=600)
        os.makedirs(os.path.dirname(todos.state_path(sid)), exist_ok=True)
        pk.write_json(todos.state_path(sid), {
            "v": 1, "ts": time.time(),
            "items": [{"id": "1", "text": "evil" + ESC + "[31m" + BIDI + "task",
                       "status": "in_progress"}]})
        return sid

    def _verb(self, verb, args=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd(verb, list(args))
        return rc, out.getvalue() + err.getvalue()

    def test_no_roster_string_reaches_any_verb_raw(self):
        self._plant()
        # every roster-printing verb — the seat KEY rides the FIRST column of
        # each, and the claim footer / todo cell / status line ride the rest.
        surfaces = [("seats", []), ("seats", ["--all"]), ("claims", []),
                    ("seat", ["gc"]), ("status", ["--seat", self.SEAT])]
        for verb, args in surfaces:
            rc, out = self._verb(verb, args)
            self.assertNotIn(ESC, out, "%s %s leaked ESC" % (verb, args))
            self.assertNotIn(BIDI, out, "%s %s leaked bidi" % (verb, args))
        # the seat KEY specifically must have PRINTED (laundered), not vanished
        _, seats_out = self._verb("seats", ["--all"])
        self.assertIn("lane", seats_out)          # the label survives, inert
        self.assertIn("pwn", seats_out)

    # ---- the guard enumerates SURFACES, not just fields of ONE report ----
    # r4 lesson: the fix that was supposed to CLOSE this class shipped the 5TH
    # surface (presence_report) in the same branch — because the guard walked
    # ONLY roster_report, blind to every SIBLING surface that rebuilds the
    # roster data elsewhere. So the guard now enumerates every roster-consuming
    # REPORT surface below and walks EACH one's full output. A new surface that
    # rebuilds roster rows (a 6th report, a new JSON endpoint) fails this test
    # BY CONSTRUCTION the moment it is added here — and the point is that ANY
    # roster-consuming surface MUST be added here (that is the enforced law),
    # so a surface that routes through _pub_row passes and one that ships raw
    # rows fails. Each entry: (name, zero-arg callable returning the surface's
    # published structure). The mutation-helper return messages — a DIFFERENT
    # class of surface (echoed to the CLI, not a report dict) — are guarded
    # separately in test_mutation_helpers_launder_echoed_seat below.
    def _report_surfaces(self):
        return [
            ("roster_report.seats", lambda: seats.roster_report()["seats"]),
            ("roster_report.claims", lambda: seats.roster_report()["claims"]),
            # the fleet presence bar — the sibling surface that ESCAPED r4's
            # roster_report-only guard; it rebuilds per-seat rows OUTSIDE the
            # choke point and shipped the RAW seat KEY + status until r4-fix.
            ("presence_report", seats.presence_report),
        ]

    def test_every_report_surface_launders_every_string(self):
        """The source-driven, SURFACE-COMPLETE completeness guard: for EVERY
        enumerated roster-consuming report surface, walk EVERY string in its
        output and assert none carries the ESC/bidi payload. Enumerates the
        output schema per surface — a new roster-borne string field added
        without routing through _pub_row fails here, AND a whole new sibling
        surface that rebuilds roster rows raw fails the moment it is enumerated
        (the law: every roster-consuming surface belongs in _report_surfaces)."""
        self._plant()
        for name, fn in self._report_surfaces():
            strings = list(_walk_strings(fn()))
            self.assertTrue(strings, "%s walked no content" % name)
            for s in strings:
                self.assertNotIn(ESC, s, "%s leaked ESC in %r" % (name, s))
                self.assertNotIn(BIDI, s, "%s leaked bidi in %r" % (name, s))

    def test_report_surfaces_keep_their_fields(self):
        """The launder must not be a field-drop: assert each report surface
        still PRESENTS its roster-borne fields (a scrub that silently deleted
        the seat column would 'pass' the leak walk while blanking the bar)."""
        self._plant()
        rep = seats.roster_report()
        for field in ("seat", "runtime", "project", "cwd", "home_room",
                      "home_room_source", "status", "status_by", "line",
                      "source", "todo"):
            self.assertIn(field, rep["seats"][0],
                          "roster_report dropped field %r" % field)
        pres = seats.presence_report()
        for field in ("seat", "runtime", "presence", "dot", "status",
                      "status_by", "line", "source"):
            self.assertIn(field, pres[0],
                          "presence_report dropped field %r" % field)
        # the seat label must SURVIVE (laundered, inert), not vanish
        self.assertTrue(any("lane" in r["seat"] and "pwn" in r["seat"]
                            for r in pres), "presence dropped the seat label")

    def test_mutation_helpers_launder_echoed_seat(self):
        """The OTHER surface class: the mutation helpers echo a message the CLI
        prints verbatim. Under a hostile seat KEY (+ a hostile home_room) each
        of set_mute / set_status / rename_seat / rehome_seat must return an
        ESC/bidi-free message — the raw key still drives the dict write, only
        the echoed label is laundered (_seat_label)."""
        self._plant()
        cases = [
            ("set_mute", lambda: seats.set_mute(self.SEAT, "main", True)),
            ("set_unmute", lambda: seats.set_mute(self.SEAT, "main", False)),
            ("set_status", lambda: seats.set_status(self.SEAT, "on it")),
            ("status_clear", lambda: seats.set_status(self.SEAT, "")),
            ("rehome", lambda: seats.rehome_seat(self.SEAT, "otherroom")),
            ("rehome_clear", lambda: seats.rehome_seat(self.SEAT, "main")),
            ("rename", lambda: seats.rename_seat(self.SEAT, "safename")),
        ]
        for name, fn in cases:
            _, msg = fn()
            self.assertNotIn(ESC, msg, "%s echoed raw ESC: %r" % (name, msg))
            self.assertNotIn(BIDI, msg, "%s echoed raw bidi: %r" % (name, msg))

    def test_pub_row_is_field_agnostic(self):
        """The choke point itself: _pub_row scrubs EVERY string value, even a
        field name it has never seen, and RECURSES into nested dicts/lists —
        the mechanism is enumeration over the row, not a fixed per-field list
        or a hand-added nested special case. This is what stops the next
        surface: a future `seats.append({... "newthing": row.get("newthing")})`
        (or a new nested cell) is laundered the moment it joins the dict."""
        row = seats._pub_row({
            "seat": self.SEAT, "session": "sid" + ESC + BIDI,
            "future_field": "surprise" + ESC + "[2J" + BIDI + "!",
            "nested_todo": {"active": "x" + ESC + BIDI + "y"},
            "nested_list": ["a" + ESC + BIDI + "b", {"deep": "c" + ESC + "d"}],
            "count": 7, "flag": True, "empty": None})
        self.assertNotIn(ESC, row["future_field"])
        self.assertNotIn(BIDI, row["future_field"])
        self.assertNotIn(ESC, row["seat"])
        self.assertNotIn(ESC, row["session"])
        # the nested dict is laundered by recursion, not a special case
        self.assertNotIn(ESC, row["nested_todo"]["active"])
        self.assertNotIn(BIDI, row["nested_todo"]["active"])
        # nested list items — string AND dict — are laundered too
        self.assertNotIn(ESC, row["nested_list"][0])
        self.assertNotIn(BIDI, row["nested_list"][0])
        self.assertNotIn(ESC, row["nested_list"][1]["deep"])
        self.assertEqual(row["count"], 7)         # non-strings pass through
        self.assertIs(row["flag"], True)
        self.assertIsNone(row["empty"])


class StatusDecayTest(PresenceBase):
    """Reviewer pin (P9): a 3-day-old explicit status must not mask a LIVE
    worktree lease — fresh claim beats stale status, fresh status still
    beats the claim, and the status age shows on every surface."""

    def _backdate_status(self, seat, sec):
        from helm import pk
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            r[seat]["status_ts"] = time.time() - sec
            pk.write_json(seats.roster_path(), r)

    def test_stale_status_yields_to_live_claim(self):
        seats.write_roster("victim", session="p" * 32, cwd=self.tmp)
        seats.set_status("victim", "old away message")
        self._backdate_status("victim", 3 * 86400)
        seats.claim("worktree:helm:web-presence", "victim", ttl=600)
        row = {x["seat"]: x for x in seats.presence_report()}["victim"]
        self.assertEqual(row["source"], "claim")      # live truth wins
        self.assertIn("UNVERIFIED hold on lane/web-presence", row["line"])
        self.assertGreaterEqual(row["status_age"], 3 * 86400 - 60)
        # a FRESH status still outranks the claim
        seats.set_status("victim", "actually on triage")
        row = {x["seat"]: x for x in seats.presence_report()}["victim"]
        self.assertEqual((row["line"], row["source"]),
                         ("actually on triage", "status"))

    def test_stale_status_with_no_claim_still_shows_aged(self):
        seats.write_roster("away", session="q" * 32, cwd=self.tmp)
        seats.set_status("away", "gone fishing")
        self._backdate_status("away", 2 * 86400)
        row = {x["seat"]: x for x in seats.presence_report()}["away"]
        self.assertEqual((row["line"], row["source"]),
                         ("gone fishing", "status"))
        self.assertGreaterEqual(row["status_age"], 2 * 86400 - 60)
        out = io.StringIO()                 # CLI renders '▸ line (2d)'
        with contextlib.redirect_stdout(out):
            self.assertEqual(seats.cmd("seats", []), 0)
        self.assertIn("gone fishing (2d)", out.getvalue())

    def test_missing_status_ts_counts_as_stale(self):
        """A planted status without status_ts (unknown age) must not outrank
        a live lease."""
        from helm import pk
        seats.write_roster("planted", session="r" * 32, cwd=self.tmp)
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            r["planted"]["status"] = "no timestamp"
            pk.write_json(seats.roster_path(), r)
        seats.claim("worktree:helm:x", "planted", ttl=600)
        row = {x["seat"]: x for x in seats.presence_report()}["planted"]
        self.assertEqual(row["source"], "claim")
        self.assertIsNone(row["status_age"])

    def test_future_status_ts_counts_as_junk(self):
        """The inversion of the missing-ts law: a status_ts planted in the
        FUTURE must not read age-0-forever (perpetually fresh, masking every
        live lease until the heat death of the fleet). Beyond the skew
        allowance it is junk — the live claim wins."""
        from helm import pk
        seats.write_roster("fut", session="f" * 32, cwd=self.tmp)
        seats.set_status("fut", "frozen in amber")
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            r["fut"]["status_ts"] = time.time() + 10 * 365 * 86400
            pk.write_json(seats.roster_path(), r)
        seats.claim("worktree:helm:real-work", "fut", ttl=600)
        row = {x["seat"]: x for x in seats.presence_report()}["fut"]
        self.assertEqual(row["source"], "claim")      # the lease surfaces
        self.assertIn("UNVERIFIED hold on lane/real-work", row["line"])
        self.assertIsNone(row["status_age"])          # junk ts = unknown age

    def test_small_clock_skew_still_reads_fresh(self):
        """NTP drift between writers is not an attack: a ts a few seconds
        ahead reads age 0 and the explicit status still wins."""
        from helm import pk
        seats.write_roster("skew", session="e" * 32, cwd=self.tmp)
        seats.set_status("skew", "on triage")
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            r["skew"]["status_ts"] = time.time() + 60   # < STATUS_SKEW_S
            pk.write_json(seats.roster_path(), r)
        seats.claim("worktree:helm:x", "skew", ttl=600)
        row = {x["seat"]: x for x in seats.presence_report()}["skew"]
        self.assertEqual((row["line"], row["source"]), ("on triage", "status"))
        self.assertEqual(row["status_age"], 0)


class JunkRowTest(PresenceBase):
    """Reviewer pin: a planted/corrupt roster row (int status, non-dict row)
    fails open PER ROW — `helm chat seats` exits 0 and the remaining rows
    render; one junk row never blanks the whole bar."""

    def _plant(self):
        from helm import pk
        seats.write_roster("good", session="g" * 32, cwd=self.tmp)
        seats.write_roster("victim", session="j" * 32, cwd=self.tmp)
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            r["victim"]["status"] = 42       # the int-status plant
            r["junk"] = "not a dict"         # a wholly corrupt row
            pk.write_json(seats.roster_path(), r)

    def test_junk_rows_fail_open_everywhere(self):
        self._plant()
        rows = {x["seat"]: x for x in seats.presence_report()}
        self.assertEqual(rows["good"]["source"], "home")   # survivors render
        self.assertEqual(rows["victim"]["line"], "42")     # coerced, no raise
        self.assertEqual(rows["junk"]["line"], "?")        # fail-open marker
        rep = {x["seat"]: x for x in seats.roster_report()["seats"]}
        self.assertEqual(rep["junk"]["line"], "?")
        self.assertEqual(rep["junk"]["pending"], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seats.cmd("seats", ["--all"]), 0)  # never rc 1
        self.assertIn("good", out.getvalue())


class StatusByTest(PresenceBase):
    """Cross-seat writes stay allowed (the room's open-post model) but carry
    provenance: writer != target records status_by, rendered '(by X)'; the
    presence beat lands on the WRITER, never the annotated target."""

    def test_cross_seat_write_records_the_writer(self):
        seats.write_roster("victim", session="k" * 32, cwd=self.tmp)
        old = time.time() - 3 * 3600
        os.utime(seats.seen_path("victim"), (old, old))    # target is quiet
        ok, _ = seats.set_status("victim", "wedged on a rebase",
                                 by="coordinator")
        self.assertTrue(ok)
        row = seats.roster()["victim"]
        self.assertEqual(row["status_by"], "coordinator")
        # the annotation did NOT freshen the wedged target's beat
        self.assertLess(seats.last_seen("victim"), time.time() - 3600)
        rows = {x["seat"]: x for x in seats.presence_report()}
        self.assertEqual(rows["victim"]["status_by"], "coordinator")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(seats.cmd("seats", ["--all"]), 0)
        self.assertIn("(by coordinator)", out.getvalue())

    def test_self_set_carries_no_by(self):
        seats.write_roster("selfie", session="m" * 32, cwd=self.tmp)
        ok, _ = seats.set_status("selfie", "on my own lane", by="selfie")
        self.assertTrue(ok)
        self.assertNotIn("status_by", seats.roster()["selfie"])

    def test_cli_stamps_the_ambient_writer(self):
        seats.write_roster("victim", session="n" * 32, cwd=self.tmp)
        os.environ["HELM_CHAT_NAME"] = "coordinator"
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = seats.cmd("status", ["annotated", "--seat", "victim"])
            self.assertEqual(rc, 0)
            self.assertEqual(seats.roster()["victim"]["status_by"],
                             "coordinator")
        finally:
            os.environ.pop("HELM_CHAT_NAME", None)
        seats.set_status("victim", None)      # clear pops provenance too
        self.assertNotIn("status_by", seats.roster().get("victim", {}))


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
        seats.write_roster(
            "coder", session="z" * 32, cwd=self.tmp,
            runtime={"agent_harness": "pi", "family": "codex",
                     "backend": "proxy"})
        seats.set_status("coder", "wiring the presence bar")
        st, body = self.get("/api/chat?since=0")
        self.assertEqual(st, 200)
        d = json.loads(body)
        rows = {r["seat"]: r for r in d.get("presence") or []}
        self.assertIn("coder", rows)
        self.assertEqual(rows["coder"]["line"], "wiring the presence bar")
        self.assertEqual(rows["coder"]["source"], "status")
        self.assertEqual(rows["coder"]["dot"], "\U0001f7e2")
        self.assertEqual(rows["coder"]["runtime"],
                         {"agent_harness": "pi", "family": "codex",
                          "backend": "proxy"})

    def test_roster_endpoint_carries_status_and_line(self):
        seats.write_roster(
            "coder", session="z" * 32, cwd=self.tmp,
            home_room="helm", home_room_source="explicit",
            runtime={"agent_harness": "pi", "backend": "proxy"})
        st, body = self.get("/api/chat/roster")
        self.assertEqual(st, 200)
        d = json.loads(body)
        row = next(s for s in d["seats"] if s["seat"] == "coder")
        self.assertEqual(row["line"], "in #helm")
        self.assertEqual(row["source"], "home")
        self.assertEqual(row["runtime"],
                         {"agent_harness": "pi", "backend": "proxy"})
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

    def test_junk_row_never_blanks_the_web_bar(self):
        """Reviewer pin: one corrupt roster row must not turn `presence`
        into [] (the whole fleet bar hiding on a single plant)."""
        from helm import pk
        seats.write_roster("good", session="w" * 32, cwd=self.tmp)
        with seats._flocked(seats.roster_path() + ".lock"):
            r = seats.roster()
            r["junk"] = "not a dict"
            pk.write_json(seats.roster_path(), r)
        st, body = self.get("/api/chat?since=0")
        self.assertEqual(st, 200)
        rows = {x["seat"]: x for x in json.loads(body)["presence"]}
        self.assertIn("good", rows)
        self.assertEqual(rows["junk"]["line"], "?")

    def test_ui_ships_the_bar(self):
        st, body = self.get("/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8")
        self.assertIn('id="chatpresence"', html)
        self.assertIn("function chatPresence", html)

    def test_ledger_seats_panel_annotates_age_and_provenance(self):
        """VERBS: the web bar, `helm chat seats`, and the roster tab's DOING
        cell glance identically — rosterDoing must render status_age + '(by X)'
        (roster_report ships both), not just the bare line. (The standalone
        rosterSeats panel was retired into the roster tab in the consolidation.)"""
        st, body = self.get("/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8")
        panel = html.split("function rosterDoing")[1].split(
            "function rosterTask")[0]
        self.assertIn("status_age", panel)
        self.assertIn("status_by", panel)


if __name__ == "__main__":
    unittest.main()
