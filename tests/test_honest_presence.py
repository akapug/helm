#!/usr/bin/env python3
"""helm — HONEST PRESENCE: a roster dot may only ever report evidence about
the seat it names.

The incident (owner, 2026-07-24). `console-design`'s roster row remembered
THREE session ids: two of its own and `0fa7c4ed…`, which belonged to a
DIFFERENT, very busy, very much alive agent. Delivery resolved its seat from
the SESSION first (`seat_for_session` before the process's own
HELM_CHAT_NAME), so every tool boundary of that stranger:
  * stamped console-design's presence beat -> the roster showed 🟢 fresh,
    refreshed every ~2s, for a seat that answered nobody, and
  * CONSUMED console-design's addressed rows -> `pending 0`, so the owner's
    @mention read as delivered while landing in a stranger's context.
Both halves are one bug: presence and delivery are PER-PROCESS facts, and the
code let one process speak for another.

These are the pins. Each fails against the pre-fix code for a different
reason, and together they say: own identity wins, a cross-seat write is
refused loudly, a session id belongs to exactly one seat, an unattributable
row SAYS so on every surface, and the surfaces all read the one same source.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat, pk, seats, web  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CHAT_OWNER_NAMES",
            "HELM_CHAT_DELIVER", "HELM_BEACON_ORPHAN_EXIT",
            "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR", "HELM_SCRATCH_GC",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_MODEL")

# SYNTHETIC, and they must stay synthetic. These were real session ids of two
# live seats until 2026-07-25 — a fixture only ever planted into a tmpdir roster,
# so nothing was coupled to machine state, but a real runtime id has no business
# being a literal in portable logic, and an as-public archive should carry none.
LIVE = "11111111-aaaa-4bbb-8ccc-111111111111"      # the stranger's live sid
DEAD = "22222222-aaaa-4bbb-8ccc-222222222222"      # the seat's own prior sid


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-honest-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        # The fixture homes its posts EXPLICITLY through the same env seam
        # launched seats use — the old accidental "main" default, now stated.
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        # IdentityDisagreementTest drives stop_guard: the scratch reaper must
        # never run against the HOST's /tmp/claude-* from inside a test
        os.environ["HELM_SCRATCH_GC"] = "0"
        chat._ensure_dir()

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def plant(self, rows):
        pk.write_json(seats.roster_path(), rows)

    def beat(self, seat, age=0.0):
        """Force a presence beat of a given age WITHOUT going through
        touch_seen (these tests are about who is ALLOWED to beat)."""
        p = seats.seen_path(seat)
        with open(p, "w"):
            pass
        t = time.time() - age
        os.utime(p, (t, t))

    def cmd(self, verb, args=(), room="main"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seats.cmd(verb, list(args), room)
        return rc, out.getvalue(), err.getvalue()

    def stderr_of(self, fn, *a, **kw):
        """Capture FD 2 — the refusal lines are one unbuffered os.write."""
        r, w = os.pipe()
        sys.stderr.flush()
        saved = os.dup(2)
        os.dup2(w, 2)
        os.close(w)
        try:
            got = fn(*a, **kw)
            sys.stderr.flush()
        finally:
            os.dup2(saved, 2)
            os.close(saved)
        chunks = []
        while True:
            b = os.read(r, 65536)
            if not b:
                break
            chunks.append(b)
        os.close(r)
        return got, b"".join(chunks).decode("utf-8")


# ---------------------------------------------------------------------------
# A. THE REPRODUCING PIN — a foreign session's activity must not stamp a seat
# ---------------------------------------------------------------------------

class ForeignBeatTest(Base):
    def test_foreign_session_activity_never_refreshes_another_seats_presence(self):
        """THE incident, minimised. `console-design`'s row remembers a session
        id that a DIFFERENT live agent is using. That agent's tool boundary
        must not refresh console-design's beat.

        Pre-fix: deliver_any resolved the seat as seat_for_session(LIVE) ==
        'console-design' (own HELM_CHAT_NAME came LAST), so this stamped the
        wrong row every ~2s — the eternal 🟢 on a silent seat.

        SUPERSESSION (owner-declared P0, 2026-08-02): this test used to also
        assert the acting process stamps ITS OWN beat. But this exact fixture
        — env says opus-integrator, sid rostered to console-design — is
        byte-identical to incident 1, where the ENV was the liar (an
        inherited HELM_CHAT_NAME on a restarted pane) and 'its own beat' was
        the impostor's. No resolver can tell the two stories apart, which is
        why a dispute now beats NOTHING under either name, loudly, until the
        sources agree (the agreement case keeps beating — next test)."""
        self.plant({"console-design": {"session": LIVE,
                                       "sessions": [DEAD, LIVE],
                                       "home_room": "helm-dogfood"},
                    "opus-integrator": {"session": "own-sid",
                                        "sessions": ["own-sid"],
                                        "home_room": "helm-dogfood"}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()
        _got, err = self.stderr_of(seats.deliver_any, session=LIVE)
        self.assertFalse(os.path.exists(seats.seen_path("console-design")),
                         "a stranger's boundary stamped console-design's beat")
        self.assertFalse(os.path.exists(seats.seen_path("opus-integrator")),
                         "a DISPUTED identity stamped a beat anyway — under "
                         "incident 1's reading this is the impostor beating "
                         "as the seat it stole")
        self.assertIn("IDENTITY DISPUTE", err)
        # positive control on the SAME observable: under agreement the very
        # same boundary DOES beat — the refusal above was the dispute, not a
        # broken beat path
        os.environ["HELM_CHAT_NAME"] = "console-design"
        seats.deliver_any(session=LIVE)
        self.assertTrue(os.path.exists(seats.seen_path("console-design")))

    def test_a_seats_own_turn_loop_does_refresh_its_own_presence(self):
        """No regression: the ordinary case — the seat's own boundary — still
        beats, and still reads fresh."""
        self.plant({"kimi": {"session": "k1", "sessions": ["k1"]}})
        os.environ["HELM_CHAT_NAME"] = "kimi"
        seats.deliver_any(session="k1")
        self.assertEqual(
            seats.presence_of(seats.last_seen("kimi", {})), "fresh")

    def test_touch_seen_refuses_a_foreign_beat_loudly(self):
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()
        ok, err = self.stderr_of(seats.touch_seen, "console-design")
        self.assertFalse(ok, "touch_seen accepted a cross-seat beat")
        self.assertIn("REFUSED a cross-seat", err)
        self.assertIn("console-design", err)
        self.assertFalse(os.path.exists(seats.seen_path("console-design")))

    def test_an_unnamed_process_still_beats_fail_open(self):
        """A process that declares NO identity (the owner CLI, an operator
        script, a pre-env-var launch seam) is never 'foreign' — the guard must
        not break every un-named caller."""
        self.assertTrue(seats.touch_seen("some-seat"))
        self.assertTrue(os.path.exists(seats.seen_path("some-seat")))

    def test_a_cross_seat_probe_consumes_nothing(self):
        """The silent-MISdelivery half: `deliver --seat <someone-else>` used to
        drain that seat's addressed rows into the prober's terminal, marking
        the owner's @mention delivered. A refused beat must deliver nothing."""
        os.environ["HELM_CHAT_NAME"] = "console-design"
        seats.deliver(seat="console-design", room="main")   # baseline its cursor
        chat.post("@console-design are you there?", who="owner")
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()
        self.assertIsNone(seats.deliver(seat="console-design", room="main"))
        self.assertIsNone(seats.deliver_any(seat="console-design"))
        # nothing consumed: the row is still pending FOR console-design
        os.environ["HELM_CHAT_NAME"] = "console-design"
        line = seats.deliver(seat="console-design", room="main")
        self.assertIn("are you there?", line or "")


# ---------------------------------------------------------------------------
# B. ONE SEAT PER SESSION ID — the write-side ownership rule
# ---------------------------------------------------------------------------

class SessionOwnershipTest(Base):
    def test_cross_seat_session_binding_is_refused_and_loud(self):
        self.plant({"console-design": {"sessions": [DEAD]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()
        _row, err = self.stderr_of(
            seats.write_roster, "console-design", session=LIVE)
        self.assertIn("REFUSED a cross-seat session binding", err)
        row = seats.roster()["console-design"]
        self.assertNotIn(LIVE, [row.get("session")] + (row.get("sessions") or []),
                         "a process bound its own session id into another row")

    def test_binding_anothers_current_session_is_refused_not_stolen(self):
        """SUPERSESSION (owner-declared P0, 2026-08-02). This test used to be
        `test_binding_a_session_evicts_it_from_every_other_row`: the incoming
        bind WON and _evict_session 'healed' the loser at the next
        SessionStart join. That silent steal IS the corruption when the
        incoming name is the wrong one — incident 2 measured seat rows
        silently ACCUMULATING foreign sids (helm-claude-2 held both 56a628d4
        and f0ad7476 at once). The new law: a sid that is another row's
        CURRENT session refuses the whole bind, loudly, naming both seats and
        the explicit repair (`helm chat seat disown --to`)."""
        self.plant({"console-design": {"session": LIVE,
                                       "sessions": [DEAD, LIVE]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()
        _row, err = self.stderr_of(
            seats.write_roster, "opus-integrator", session=LIVE)
        self.assertIn("REFUSED to bind", err)
        self.assertIn("opus-integrator", err)
        self.assertIn("console-design", err)
        self.assertIn(LIVE[:12], err)
        self.assertIn("disown", err)
        r = seats.roster()
        self.assertEqual(seats.seat_for_session(LIVE), "console-design",
                         "the current owner must keep its binding")
        oi = r["opus-integrator"]
        self.assertNotIn(LIVE, [oi.get("session")] + (oi.get("sessions") or []),
                         "the refused sid leaked into the new row anyway")

    def test_the_refusal_covers_the_addressing_only_append_too(self):
        """identity=False writes append to sessions[] without touching the
        current binding — a foreign sid accumulating as 'addressing' is the
        same accumulation, so the refusal must cover that append as well."""
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        seats._FOREIGN_WARNED.clear()
        _row, err = self.stderr_of(
            seats.write_roster, "opus-integrator", session=LIVE,
            identity=False)
        self.assertIn("REFUSED to bind", err)
        oi = seats.roster()["opus-integrator"]
        self.assertNotIn(LIVE, oi.get("sessions") or [],
                         "a foreign CURRENT sid still accumulated as "
                         "addressing history")
        # positive control on the SAME observable: a NON-conflicting
        # addressing-only write does append — sessions[] is writable, the
        # empty read above was the refusal
        seats.write_roster("opus-integrator", session=DEAD, identity=False)
        self.assertIn(DEAD,
                      seats.roster()["opus-integrator"].get("sessions") or [])

    def test_rebinding_my_own_current_session_is_not_refused(self):  # noqa: VACUOUS_ASSERTION — the positive control IS inline (the foreign-current plant above the clause asserts the refusal fires through the identical stderr_of capture), and mutation M1 (drop `k != seat`) reddens exactly this test
        """The mutation pin for the `k != seat` clause: a seat re-joining with
        the sid its OWN row already holds is the ordinary resume shape and
        must never read as a steal."""
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        # positive control on the SAME observable first: the identical
        # capture idiom DOES see the refusal when the sid is foreign-current
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        seats._FOREIGN_WARNED.clear()
        _row, err = self.stderr_of(
            seats.write_roster, "opus-integrator", session=LIVE)
        self.assertIn("REFUSED to bind", err)
        # the clause under test: my OWN current sid is no steal
        self.plant({"opus-integrator": {"session": LIVE, "sessions": [LIVE]}})
        seats._FOREIGN_WARNED.clear()
        _row, err = self.stderr_of(
            seats.write_roster, "opus-integrator", session=LIVE)
        self.assertNotIn("REFUSED to bind", err)
        self.assertEqual(seats.roster()["opus-integrator"]["session"], LIVE)

    def test_a_history_only_mention_is_still_evicted_by_the_true_owner(self):
        """Cleanup, not accumulation: a sid sitting in another row's HISTORY
        (sessions[]) while that row's CURRENT session is elsewhere is a stale
        claim, and the true owner's bind still evicts it (_evict_session keeps
        its role — tests/test_nonpane_session.py SessionEvictionTest pins the
        rest)."""
        self.plant({"console-design": {"session": DEAD,
                                       "sessions": [LIVE, DEAD]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats.write_roster("opus-integrator", session=LIVE)
        r = seats.roster()
        self.assertEqual(seats.seat_for_session(LIVE), "opus-integrator")
        cd = r["console-design"]
        self.assertNotIn(LIVE, cd.get("sessions") or [])
        self.assertEqual(cd.get("session"), DEAD)

    def test_two_seats_in_one_worktree_keep_separate_rows(self):
        """cwd/project/home_room collisions are NORMAL on this fleet (every
        helm seat shares one checkout) and must never merge identities."""
        cwd = "/home/tester/dev/example/helm"
        os.environ["HELM_CHAT_NAME"] = "codex-3"
        seats.write_roster("codex-3", session="c3", cwd=cwd,
                           home_room="helm-dogfood",
                           home_room_source="explicit")
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats.write_roster("opus-integrator", session="oi", cwd=cwd,
                           home_room="helm-dogfood",
                           home_room_source="explicit")
        r = seats.roster()
        self.assertEqual(r["codex-3"]["session"], "c3")
        self.assertEqual(r["opus-integrator"]["session"], "oi")
        self.assertEqual(seats.seat_for_session("c3"), "codex-3")
        self.assertEqual(seats.seat_for_session("oi"), "opus-integrator")
        self.assertEqual(seats.unverified_seats(), {})

    def test_disown_prunes_a_stale_session_id(self):
        self.plant({"console-design": {"session": LIVE,
                                       "sessions": [DEAD, LIVE]}})
        ok, msg = seats.disown_session("console-design", LIVE[:8])
        self.assertTrue(ok, msg)
        row = seats.roster()["console-design"]
        self.assertEqual(row["sessions"], [DEAD])
        self.assertEqual(row["session"], DEAD)
        self.assertIsNone(seats.seat_for_session(LIVE))

    def test_disown_refuses_a_too_short_or_unknown_id(self):
        self.plant({"console-design": {"sessions": [DEAD]}})
        ok, msg = seats.disown_session("console-design", "f0ad")
        self.assertFalse(ok)
        self.assertIn("8 characters", msg)
        ok, msg = seats.disown_session("console-design", "deadbeef")
        self.assertFalse(ok)
        self.assertIn("remembers no session", msg)


# ---------------------------------------------------------------------------
# C. THE OWNER SURFACE — an unattributable row SAYS so
# ---------------------------------------------------------------------------

class UnverifiedSurfaceTest(Base):
    def contaminated(self):
        self.plant({"console-design": {"session": LIVE,
                                       "sessions": [DEAD, LIVE],
                                       "home_room": "helm-dogfood",
                                       "project": "helm"},
                    "opus-integrator": {"session": LIVE,
                                        "sessions": [LIVE],
                                        "home_room": "helm-dogfood",
                                        "project": "helm"}})
        self.beat("console-design", 5)
        self.beat("opus-integrator", 5)

    def test_a_shared_session_id_makes_both_rows_unverified(self):
        self.contaminated()
        unver = seats.unverified_seats()
        self.assertEqual(sorted(unver), ["console-design", "opus-integrator"])
        self.assertEqual(unver["console-design"], (LIVE, ["opus-integrator"]))

    def test_roster_report_says_unverified_not_fresh(self):
        self.contaminated()
        rows = {s["seat"]: s for s in seats.roster_report()["seats"]}
        cd = rows["console-design"]
        self.assertEqual(cd["presence"], "unverified")
        self.assertEqual(cd["dot"], seats.PRESENCE_DOTS[seats.UNVERIFIED])
        self.assertIn("presence UNVERIFIED", cd["warn"])
        self.assertIn("opus-integrator", cd["warn"])
        self.assertEqual(cd["unverified_session"], LIVE)

    def test_presence_report_the_web_bar_says_it_too(self):
        self.contaminated()
        rows = {s["seat"]: s for s in seats.presence_report()}
        self.assertEqual(rows["console-design"]["presence"], "unverified")
        self.assertIn("presence UNVERIFIED", rows["console-design"]["warn"])

    def test_the_cli_table_shows_it_and_names_the_heal(self):
        """GUI/CLI-first: a state only agents can compute is not done."""
        self.contaminated()
        rc, out, _err = self.cmd("seats")
        self.assertEqual(rc, 0)
        line = [ln for ln in out.splitlines() if "console-design" in ln][0]
        self.assertIn("unverified", line)
        self.assertIn(seats.PRESENCE_DOTS[seats.UNVERIFIED], line)
        self.assertIn("presence UNVERIFIED", line)
        self.assertIn("helm chat seat disown console-design %s" % LIVE[:8],
                      line)

    def test_an_unverified_row_is_not_live_for_work_routing(self):
        """Claims/offer routing must skip it: routing work to a seat nobody
        can prove is listening is how a dispatch strands."""
        self.contaminated()
        self.assertNotIn("console-design", seats._live_seats())
        self.assertNotIn("opus-integrator", seats._live_seats())

    def test_an_absent_row_stays_absent_when_unverified(self):
        """Never unhide the graveyard: no beat at all is not an identity
        question, and `helm chat seats` hides absent rows for a reason."""
        self.contaminated()
        for s in ("console-design", "opus-integrator"):
            os.remove(seats.seen_path(s))
        rows = {s["seat"]: s for s in seats.roster_report()["seats"]}
        self.assertEqual(rows["console-design"]["presence"], "absent")

    def test_disown_restores_an_honest_green(self):
        self.contaminated()
        ok, _msg = seats.disown_session("console-design", LIVE[:8])
        self.assertTrue(ok)
        rows = {s["seat"]: s for s in seats.roster_report()["seats"]}
        self.assertEqual(rows["console-design"]["presence"], "fresh")
        self.assertIsNone(rows["console-design"]["warn"])
        self.assertEqual(rows["opus-integrator"]["presence"], "fresh")


# ---------------------------------------------------------------------------
# D. ONE PROJECTION — the GUI can never read fresher than the beat
# ---------------------------------------------------------------------------

class OneProjectionTest(Base):
    """The claim under test: 'the web console fabricates freshness'. It does
    not — and this pins WHY, so the question never has to be re-litigated.

    seats.last_seen() (helm/seats.py) reads the `.seen` file MTIME first and
    falls back to the row's `last_seen` FIELD only when that file is gone. The
    field is written at join/status time and is EXPECTED to be hours stale on a
    live seat; it is not the presence source and never was. So `row.last_seen
    == 3200s` beside a GUI reading '5s ago' is not a contradiction — it is the
    fallback being older than the beat, exactly as designed. The invariant that
    matters is the one below: every surface reads THE SAME source, so the GUI
    and `helm chat seats` cannot disagree."""

    def test_last_seen_prefers_the_beat_file_and_only_falls_back(self):
        row = {"last_seen": time.time()}                # fresh FIELD …
        self.plant({"seafan-claude": row})
        self.beat("seafan-claude", 3200)               # … stale BEAT
        self.assertGreater(time.time() - seats.last_seen("seafan-claude", row),
                           3000, "the row FIELD outranked the beat file")
        self.assertEqual(seats.presence_of(
            seats.last_seen("seafan-claude", row)), "absent")
        os.remove(seats.seen_path("seafan-claude"))    # no beat -> fallback
        self.assertLess(time.time() - seats.last_seen("seafan-claude", row), 5)

    def test_every_surface_reports_exactly_the_seats_own_beat(self):
        now = time.time()
        rows = {"seafan-claude": {"session": "sf", "sessions": ["sf"],
                                 "last_seen": now - 3200, "project": "seafan"},
                "kimi": {"session": "k", "sessions": ["k"],
                         "last_seen": now - 3200, "project": "helm"}}
        self.plant(rows)
        self.beat("seafan-claude", 4)                  # live beacon/boundary
        self.beat("kimi", 4000)                        # genuinely cold
        want = {s: seats.last_seen(s, rows[s]) for s in rows}
        for surface, got in (
                ("roster_report", seats.roster_report()["seats"]),
                ("presence_report", seats.presence_report())):
            by = {s["seat"]: s for s in got}
            for s in rows:
                self.assertAlmostEqual(
                    by[s]["last_seen"], want[s], places=3,
                    msg="%s reported a last_seen that is not the seat's own "
                        "beat" % surface)
                self.assertEqual(by[s]["presence"], seats.presence_of(want[s]),
                                 "%s disagreed with presence_of" % surface)
        by = {s["seat"]: s for s in seats.roster_report()["seats"]}
        self.assertEqual(by["seafan-claude"]["presence"], "fresh")
        self.assertEqual(by["kimi"]["presence"], "absent")

    def test_no_surface_can_report_fresher_than_the_beat(self):
        """Property pin over a spread of ages: projected age >= beat age for
        every surface, always."""
        rows, want = {}, {}
        for i, age in enumerate((1, 60, 121, 899, 901, 5000)):
            s = "seat-%d" % i
            rows[s] = {"session": s, "sessions": [s], "last_seen": time.time()}
            self.plant(rows)
            self.beat(s, age)
            want[s] = age
        now = time.time()
        for got in (seats.roster_report()["seats"], seats.presence_report()):
            for row in got:
                self.assertGreaterEqual(
                    now - row["last_seen"] + 0.5, want[row["seat"]],
                    "a surface reported %s FRESHER than its own beat"
                    % row["seat"])

    def test_the_web_sidebar_shares_the_one_projection(self):
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE],
                                       "home_room": "main"},
                    "opus-integrator": {"session": LIVE, "sessions": [LIVE],
                                        "home_room": "main"}})
        self.beat("console-design", 3)
        self.beat("opus-integrator", 3)
        rows = [{"from": "console-design", "ts": "2026-07-24T18:00:00Z",
                 "text": "hi"}]
        got = {r["seat"]: r for r in
               web._room_seats("main", rows, seats.roster())}
        self.assertEqual(got["console-design"]["presence"], "unverified",
                         "the sidebar painted a dot the CLI would not")


# ---------------------------------------------------------------------------
# E. THE SELECTOR THAT LIED, and the orphaned beacon
# ---------------------------------------------------------------------------

class SelectorTest(Base):
    def test_pending_fails_loud_on_an_unsupported_selector(self):
        """`helm chat pending pane:console-design` used to DROP the selector
        and print the CALLER's rows — silently answering a different question
        than the one asked, which is how a live investigation was misled."""
        self.plant({"opus-integrator": {"session": "oi", "sessions": ["oi"]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        chat.post("@console-design ping", who="opus-integrator")
        rc, out, err = self.cmd("pending", ["pane:console-design"])
        self.assertEqual(rc, 2)
        self.assertIn("unsupported selector", err)
        self.assertIn("pane:console-design", err)
        self.assertEqual(out, "", "it answered a question nobody asked")

    def test_pending_without_a_selector_still_works(self):
        self.plant({"opus-integrator": {"session": "oi", "sessions": ["oi"]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        rc, _out, _err = self.cmd("pending")
        self.assertEqual(rc, 0)

    def test_wait_refuses_a_foreign_seat_flag(self):
        self.plant({"console-design": {"session": DEAD, "sessions": [DEAD]},
                    "opus-integrator": {"session": "oi", "sessions": ["oi"]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        rc, _out, err = self.cmd("wait", ["--seat", "console-design",
                                          "--timeout", "0.01"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot receive for another seat", err)
        self.assertFalse(os.path.exists(seats.seen_path("console-design")))

    def test_wait_accepts_a_seat_flag_naming_my_own_seat(self):
        """The armed-beacon shape (`wait --seat <me> --follow`) must not
        regress — and an UN-named process may still name any seat."""
        self.plant({"kimi": {"session": "k", "sessions": ["k"]}})
        os.environ["HELM_CHAT_NAME"] = "kimi"
        rc, _out, err = self.cmd("wait", ["--seat", "kimi", "--timeout", "0.01"])
        self.assertIn(rc, (0, 1))
        self.assertNotIn("cannot receive", err)
        os.environ.pop("HELM_CHAT_NAME")
        rc, _out, err = self.cmd("wait", ["--seat", "kimi", "--timeout", "0.01"])
        self.assertNotIn("cannot receive", err)


class OrphanBeaconTest(Base):
    def test_an_orphaned_beacon_exits_instead_of_beating(self):
        """An orphaned `helm chat wait --follow` cannot wake anybody
        (native-wake-only-agent-armed), so every further beat is pure
        misinformation and every row it consumes is lost into a dead pipe."""
        self.plant({"kimi": {"session": "k", "sessions": ["k"]}})
        os.environ["HELM_CHAT_NAME"] = "kimi"
        seats.deliver(seat="kimi", room="main")        # baseline its cursor
        os.remove(seats.seen_path("kimi"))            # …and forget that beat
        chat.post("@kimi are you there?", who="owner")
        with mock.patch.object(os, "getppid", return_value=1):
            got, _err = self.stderr_of(
                seats.wait, seat="kimi", follow=True, poll=0.01)
        self.assertIsNone(got)
        self.assertFalse(os.path.exists(seats.seen_path("kimi")),
                         "an orphaned beacon still stamped a presence beat")
        # and the row is still there for whoever starts next
        line = seats.deliver(seat="kimi", room="main")
        self.assertIn("are you there?", line or "")

    def test_a_parented_beacon_is_not_orphaned(self):
        self.assertFalse(seats._beacon_orphaned())

    def test_the_orphan_check_has_a_kill_switch(self):
        os.environ["HELM_BEACON_ORPHAN_EXIT"] = "0"
        with mock.patch.object(os, "getppid", return_value=1):
            self.assertFalse(seats._beacon_orphaned())


class RenameGranularityTest(Base):
    """BUG A — the identity destroyer. `seat rename <sid> <name>` is
    ROW-granular while a session-id argument reads pane-granular, and on
    2026-07-22T18:05:21Z a resumed console-design pane used it believing the
    narrow meaning: the sid resolved inside the opus-integrator ROW and the
    whole row moved under the console-design key ('seat opus-integrator ->
    console-design'). Everything else in this file is downstream of that."""

    def incident(self):
        """The exact pre-incident roster: the sid CD's pane would target lives
        in opus-integrator's row, beside opus's other sessions."""
        self.plant({"opus-integrator": {"session": "96416633-1111",
                                        "sessions": ["96416633-1111", DEAD,
                                                     LIVE],
                                        "home_room": "helm-dogfood"}})

    def test_a_sid_target_that_would_move_another_rows_identity_is_refused(self):
        self.incident()
        os.environ.pop("HELM_CHAT_NAME", None)        # a pane launched in orca
        ok, msg = seats.rename_seat(DEAD, "console-design")
        self.assertFalse(ok, "the row moved silently — this IS the incident")
        self.assertIn("ROW-granular", msg)
        self.assertIn("opus-integrator", msg)
        self.assertIn("--row", msg)
        self.assertIn("seat disown", msg)
        self.assertIn("opus-integrator", seats.roster(),
                      "opus-integrator's row was re-keyed anyway")
        self.assertNotIn("console-design", seats.roster())

    def test_the_explicit_whole_row_form_still_works_and_says_what_it_did(self):
        self.incident()
        ok, msg = seats.rename_seat(DEAD, "console-design", whole_row=True)
        self.assertTrue(ok, msg)
        self.assertIn("moved the WHOLE row", msg)
        self.assertIn("carrying 3 sessions", msg)
        self.assertIn("console-design", seats.roster())

    def test_renaming_by_seat_name_is_unambiguous_and_unblocked(self):
        self.incident()
        ok, msg = seats.rename_seat("opus-integrator", "opus")
        self.assertTrue(ok, msg)
        self.assertIn("moved the WHOLE row", msg)
        self.assertIn("opus", seats.roster())

    def test_a_single_session_row_renames_by_sid_without_ceremony(self):
        """The legitimate use — 'the owner names a live agent they only know by
        sid' — keeps working when there is no OTHER identity in the row."""
        self.plant({"agent-abc12345": {"session": DEAD, "sessions": [DEAD]}})
        ok, msg = seats.rename_seat(DEAD[:12], "console-design")
        self.assertTrue(ok, msg)
        self.assertIn("console-design", seats.roster())

    def test_a_seat_cannot_rename_another_seats_row(self):
        self.plant({"opus-integrator": {"session": "oi", "sessions": ["oi"]}})
        os.environ["HELM_CHAT_NAME"] = "console-design"
        ok, msg = seats.rename_seat("opus-integrator", "mine")
        self.assertFalse(ok)
        self.assertIn("this process is 'console-design'", msg)
        self.assertIn("opus-integrator", seats.roster())

    def test_the_cli_carries_the_row_flag(self):
        self.incident()
        rc, out, err = self.cmd("seat", ["rename", DEAD, "console-design"])
        self.assertEqual(rc, 1)
        self.assertIn("ROW-granular", err)
        rc, out, _err = self.cmd("seat", ["rename", DEAD, "console-design",
                                         "--row"])
        self.assertEqual(rc, 0)
        self.assertIn("moved the WHOLE row", out)


class AmbiguousBindingTest(Base):
    """BUG B — resolution must not be insertion-order arbitrary, and a stale
    historical binding must never outrank a live validated identity."""

    def test_a_current_binding_outranks_a_historical_one(self):
        self.plant({"console-design": {"sessions": [LIVE]},        # history
                    "opus-integrator": {"session": LIVE,           # current
                                        "sessions": [LIVE]}})
        seats._FOREIGN_WARNED.clear()
        got, err = self.stderr_of(seats.seat_for_session, LIVE)
        self.assertEqual(got, "opus-integrator")
        self.assertIn("remembered by 2 seats", err)
        self.assertIn("seat disown", err)

    def test_resolution_is_deterministic_not_dict_order(self):
        a = {"zzz-seat": {"sessions": [LIVE]}, "aaa-seat": {"sessions": [LIVE]}}
        self.plant(a)
        first = seats.seat_for_session(LIVE)
        self.plant({"aaa-seat": a["aaa-seat"], "zzz-seat": a["zzz-seat"]})
        seats._FOREIGN_WARNED.clear()
        self.assertEqual(seats.seat_for_session(LIVE), first)
        self.assertEqual(first, "aaa-seat")

    def test_a_join_under_a_disputed_identity_refuses_instead_of_healing(self):
        """SUPERSESSION (owner-declared P0, 2026-08-02). This was
        `test_a_validated_identity_beats_a_stale_binding_at_join`: the
        declared name silently WON and the re-join re-owned the sid — the
        2026-07-24 heal. But 'validated' proves only that the STRING is
        well-formed; an INHERITED HELM_CHAT_NAME is free to any child of the
        exporting shell, and that silent win is exactly how a restarted pane
        became opus-integrator and drained ~60 of OI's DMs. Both resolution
        orders have now failed live, so a disagreement refuses — printing
        BOTH answers — and the heal is the EXPLICIT repair verb (the test
        below), never a rebind race."""
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seat, line = seats.join(session=LIVE,
                                cwd="/home/tester/dev/example/helm")
        self.assertEqual(seat, "opus-integrator")
        self.assertIn("JOIN REFUSED", line)
        self.assertIn("opus-integrator", line)
        self.assertIn("console-design", line)
        self.assertIn(LIVE[:8], line)
        self.assertIn("disown", line)
        r = seats.roster()
        self.assertNotIn("opus-integrator", r, "a refused join minted a row")
        self.assertEqual(r["console-design"]["session"], LIVE,
                         "a refused join must not touch the standing binding")

    def test_the_repair_verb_hands_a_session_back_to_its_owner(self):
        """The live-estate repair path: one command moves a mis-owned session
        to the seat that actually runs it, no restart needed."""
        self.plant({"console-design": {"session": LIVE,
                                       "sessions": [DEAD, LIVE]},
                    "opus-integrator": {"session": "oi", "sessions": ["oi"]}})
        rc, out, _err = self.cmd(
            "seat", ["disown", "console-design", LIVE[:8], "--to",
                     "opus-integrator"])
        self.assertEqual(rc, 0)
        self.assertIn("handed it to opus-integrator", out)
        self.assertEqual(seats.seat_for_session(LIVE), "opus-integrator")
        self.assertEqual(seats.roster()["console-design"]["session"], DEAD)
        self.assertEqual(seats.unverified_seats(), {})


class ActingSeatTest(Base):
    def test_own_name_wins_over_a_foreign_row_holding_my_session(self):
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        self.assertEqual(seats.acting_seat(LIVE), "opus-integrator")

    def test_without_a_name_the_session_row_still_resolves(self):
        """An un-named process keeps the old, useful behaviour: its session's
        row names it (that is how a hook-joined pane with no env var works)."""
        self.plant({"console-design": {"session": DEAD, "sessions": [DEAD]}})
        self.assertEqual(seats.acting_seat(DEAD), "console-design")

    def test_with_neither_it_falls_to_the_auto_name(self):
        self.assertEqual(seats.acting_seat("no-such-sid", "/tmp/proj"),
                         seats.auto_name("no-such-sid", "/tmp/proj"))


# ---------------------------------------------------------------------------
# F. DECLARED-NAME vs ROSTER DISAGREEMENT IS A REFUSAL, NOT A TIEBREAK
#    (owner-declared P0, 2026-08-02 — the inherited-HELM_CHAT_NAME incident)
# ---------------------------------------------------------------------------

class IdentityDisagreementTest(Base):
    """Each arm's fixture is the SAME disputed state — env declares
    opus-integrator, session LIVE is rostered to console-design — and each
    fails exactly one refusal clause. The repair (agreement) is then shown to
    work, proving the refusal was the disagreement and nothing else."""

    def dispute(self):
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()

    def test_the_helper_answers_both_names_or_nothing(self):
        self.dispute()
        self.assertEqual(seats.identity_disagreement(LIVE),
                         ("opus-integrator", "console-design"))
        # Still None HERE and correctly so: this fixture's impostor declares
        # opus-integrator, a name NO row occupies, so there is nobody to
        # displace. The r1 gap was not this assertion — it was that no fixture
        # staged the impostor declaring the OCCUPIED name (see the claim-jump
        # arm below, which is the adversary's repro).
        self.assertIsNone(seats.identity_disagreement(DEAD),
                          "unrostered sid + unoccupied name is claimable")
        self.assertIsNone(seats.identity_disagreement(None))
        os.environ["HELM_CHAT_NAME"] = "console-design"
        self.assertIsNone(seats.identity_disagreement(LIVE),
                          "agreement is the clean state, not a dispute")
        os.environ.pop("HELM_CHAT_NAME")
        self.assertIsNone(seats.identity_disagreement(LIVE),
                          "an undeclared process has nothing to dispute")

    def test_a_fresh_sid_claiming_an_OCCUPIED_name_is_the_dispute(self):
        """THE ADVERSARY'S REPRO, verbatim: inherited name + fresh sid.

        r1 shipped a predicate that required MY sid to be bound somewhere, so
        this exact shape — the one the incident actually had — returned None
        and every refusal rung opened. The arm exists to fail if anyone
        narrows the predicate back to my-session-only."""
        # The impostor declares the name that IS occupied — that is the whole
        # shape, and the one r1's fixtures never staged.
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        os.environ["HELM_CHAT_NAME"] = "console-design"
        seats._FOREIGN_WARNED.clear()
        dis = seats.identity_disagreement(DEAD)   # DEAD is rostered NOWHERE
        self.assertIsNotNone(dis, "claim-jump must refuse, not open")
        self.assertEqual(dis[0], "console-design")
        self.assertEqual(dis[1], dis[0],
                         "claim-jump is marked by bound==own — that equality "
                         "is the shape discriminator, not a parsed string")
        # the SENTENCE is where the live session must be named, and it must
        # not claim my sid is 'rostered to' anything — it is rostered nowhere
        say = seats._dispute_sentence(dis[0], dis[1], DEAD)
        self.assertIn("rostered NOWHERE", say)
        self.assertIn(LIVE[:8], say, "name the live session it protected")
        self.assertNotIn("is rostered to", say)

    def test_an_unoccupied_name_is_claimable_not_a_dispute(self):  # noqa: VACUOUS_ASSERTION — the two assertIsNones are the CONTRACT (claimable, not taken) and they sit behind an unconditional assertIsNotNone control on the same observable, three lines up: occupy the row and the predicate must speak. A predicate returning None unconditionally reddens that control, which is precisely the r1 bug this arm exists to pin.
        """THE ARM THAT KEEPS THIS FROM BEING 'name is taken'.

        A roster row with NO live session — a spawn mirror, a seat between
        panes — is a name waiting to be claimed. Refusing here would break
        every legitimate first join, including the `helm chat join` an
        integrator ran tonight to register after the reboot. Occupied is the
        predicate; existing is not."""
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()

        # POSITIVE CONTROL FIRST, on the SAME observable: occupy the row and
        # the predicate must SPEAK. Without this the two assertIsNones below
        # would pass against a predicate that returns None unconditionally —
        # which is exactly the bug r1 shipped, so the control is not ceremony.
        self.plant({"opus-integrator": {"session": LIVE, "sessions": [LIVE]}})
        self.assertIsNotNone(seats.identity_disagreement(DEAD),
                             "control: an OCCUPIED row must dispute")

        self.plant({"opus-integrator": {"cwd": "/x"}})     # row, NO session
        self.assertIsNone(seats.identity_disagreement(DEAD),
                          "an unoccupied row is claimable")
        self.plant({"opus-integrator": {"session": DEAD, "sessions": [DEAD]}})
        self.assertIsNone(seats.identity_disagreement(DEAD),
                          "my own row is never a dispute with myself")

    def test_a_SUBAGENT_of_the_seat_is_not_a_claim_jump(self):
        """THE LEGITIMATE fresh-sid-claiming-an-occupied-name, and the r2
        predicate broke it before the whole suite caught it.

        A hook/subagent process runs under its parent seat's HELM_CHAT_NAME
        and supplies its OWN different sid — byte-identical to claim-jump
        from the predicate's seat, and completely legitimate: the child IS
        that seat. nonpane_session is the discriminator (a supplied sid that
        differs from the pane's declared env sid), so the exemption is not a
        heuristic. Without it, every subagent join of a live seat refuses and
        two nonpane tests fail — which is exactly how this was found."""
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        os.environ["HELM_CHAT_NAME"] = "console-design"
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE      # the PANE's own sid
        seats._FOREIGN_WARNED.clear()

        # POSITIVE CONTROL on the same observable: with NO pane sid declared,
        # the identical fixture IS a claim-jump. So this arm cannot pass by
        # the predicate having gone inert.
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        self.assertIsNotNone(seats.identity_disagreement(DEAD),
                             "control: without a pane sid this is claim-jump")

        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.assertTrue(seats.nonpane_session(DEAD),
                        "fixture must actually BE a nonpane child")
        self.assertIsNone(seats.identity_disagreement(DEAD),
                          "a subagent of the seat is not an impostor")

    def test_the_occupancy_read_is_PINNED_to_the_current_session_field(self):
        """THE COUPLING TRIPWIRE, added because a reviewer named the risk and
        the mutation proved the existing arms did NOT cover it.

        _live_session_of and seat_for_session must read the SAME field
        (row['session'], the CURRENT binding) — the deleted "...and not mine"
        clause was justified by exactly that. Every other arm plants session
        and sessions[] with the SAME value, so a reader that forked to the
        HISTORY list still answered correctly and the fork mutation survived.
        This fixture makes them DISAGREE, so reading the wrong field names the
        wrong session and the predicate goes blind exactly as warned."""
        self.plant({"console-design": {"session": LIVE,          # CURRENT
                                       "sessions": [DEAD]}})     # HISTORY only
        os.environ["HELM_CHAT_NAME"] = "console-design"
        seats._FOREIGN_WARNED.clear()
        self.assertEqual(seats._live_session_of("console-design"), LIVE,
                         "occupancy is the CURRENT binding, never history")
        # and the refusal must name THAT session, not the historical one
        say = seats._dispute_sentence("console-design", "console-design",
                                      "33333333-cccc-4ddd-8eee-333333333333")
        self.assertIn(LIVE[:8], say)
        self.assertNotIn(DEAD[:8], say,
                         "naming the historical sid means the reader forked")

    def test_deliver_consumes_and_beats_nothing_under_a_dispute(self):
        # baseline console-design's cursor first (agreement), then post
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        seats.deliver(session=LIVE, room="main")
        chat.post("@console-design are you there?", who="owner")
        for s in ("console-design", "opus-integrator"):
            try:
                os.remove(seats.seen_path(s))
            except OSError:
                pass
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()
        got, err = self.stderr_of(seats.deliver, session=LIVE, room="main")
        self.assertIsNone(got)
        self.assertIn("IDENTITY DISPUTE", err)
        self.assertIn("opus-integrator", err)
        self.assertIn("console-design", err)
        self.assertIn(LIVE[:8], err)
        self.assertFalse(os.path.exists(seats.seen_path("opus-integrator")),
                         "a disputed process still beat presence")
        self.assertFalse(os.path.exists(seats.seen_path("console-design")),
                         "a disputed process beat the OTHER seat's presence")
        # agreement restored -> the row was never consumed and still delivers
        os.environ["HELM_CHAT_NAME"] = "console-design"
        line = seats.deliver(session=LIVE, room="main")
        self.assertIn("are you there?", line or "")

    def test_deliver_any_stops_at_the_same_gate(self):
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        seats.deliver(session=LIVE, room="main")
        for s in ("console-design", "opus-integrator"):
            try:
                os.remove(seats.seen_path(s))
            except OSError:
                pass
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        seats._FOREIGN_WARNED.clear()
        got, err = self.stderr_of(seats.deliver_any, session=LIVE)
        self.assertIsNone(got)
        self.assertIn("IDENTITY DISPUTE", err)
        self.assertFalse(os.path.exists(seats.seen_path("opus-integrator")))
        self.assertFalse(os.path.exists(seats.seen_path("console-design")))
        # positive control on the SAME observable: agreement beats again
        os.environ["HELM_CHAT_NAME"] = "console-design"
        seats.deliver_any(session=LIVE)
        self.assertTrue(os.path.exists(seats.seen_path("console-design")))

    def test_wait_refuses_to_arm_and_says_so_on_stdout(self):
        """The exact arm that drained ~60 of OI's DMs. The refusal must ride
        STDOUT (the Monitor pipe's channel), not just stderr."""
        self.dispute()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got = seats.wait(session=LIVE, timeout=5, poll=0.01)
        self.assertIsNone(got)
        text = out.getvalue()
        self.assertIn("REFUSING to arm", text)
        self.assertIn("opus-integrator", text)
        self.assertIn("console-design", text)
        self.assertIn(LIVE[:8], text)
        self.assertFalse(os.path.exists(seats.seen_path("opus-integrator")))
        self.assertFalse(os.path.exists(seats.seen_path("console-design")))
        # positive control on the SAME observable: under agreement the same
        # wait shape beats presence while it polls (returns None on timeout)
        os.environ["HELM_CHAT_NAME"] = "console-design"
        seats.wait(session=LIVE, timeout=0.05, poll=0.01)
        self.assertTrue(os.path.exists(seats.seen_path("console-design")))

    def test_the_signing_verbs_refuse_through_seat_actor(self):
        self.dispute()
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        actor, err = chat._seat_actor([])
        self.assertIsNone(actor)
        self.assertIn("opus-integrator", err or "")
        self.assertIn("console-design", err or "")
        self.assertIn("disown", err or "")
        # agreement passes: the verb works the moment the sources agree
        os.environ["HELM_CHAT_NAME"] = "console-design"
        actor, err = chat._seat_actor([])
        self.assertIsNone(err)
        self.assertEqual(actor, "console-design")

    def test_stop_guard_warns_about_the_dispute_but_never_blocks_on_it(self):
        self.dispute()
        blocks, warns = seats.stop_guard(session=LIVE, room="main")
        self.assertTrue(any("IDENTITY DISPUTE" in w for w in warns),
                        "the stop-guard stayed silent on a disputed pane: %r"
                        % warns)
        self.assertFalse(any("IDENTITY DISPUTE" in b for b in blocks),
                         "a dispute must WARN at Stop, never wedge the turn")
        # and under stop_hook_active the warn still surfaces
        _blocks, surfaced = seats.stop_guard(session=LIVE, room="main",
                                             stop_active=True)
        self.assertTrue(any("IDENTITY DISPUTE" in w for w in surfaced))


# ---------------------------------------------------------------------------
# G. HELM-OWNED LAUNCH PATHS SET THE NAME, NEVER INHERIT (fix set D)
# ---------------------------------------------------------------------------

class LaunchIdentityEnvTest(Base):
    def test_resume_exports_the_one_rostered_seat(self):
        from helm import sessions
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        self.assertEqual(sessions.resume_identity_env(LIVE),
                         {"HELM_CHAT_NAME": "console-design"})

    def test_an_unknown_or_ambiguous_sid_stays_nameless(self):  # noqa: VACUOUS_ASSERTION — the positive control IS inline (the single-owner plant asserts the helper exports before the Nones), and mutation M13 (`if hits` for `len(hits) == 1`) reddens exactly this test
        """Nameless is honest; a guessed name is incident 1."""
        from helm import sessions
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        # positive control on the SAME observable: the single-owner sid DOES
        # export — the Nones below are the refusals, not a dead helper
        self.assertEqual(sessions.resume_identity_env(LIVE),
                         {"HELM_CHAT_NAME": "console-design"})
        self.assertIsNone(sessions.resume_identity_env(DEAD))
        self.plant({"a-seat": {"session": LIVE, "sessions": [LIVE]},
                    "b-seat": {"sessions": [LIVE]}})
        seats._FOREIGN_WARNED.clear()
        got, _err = self.stderr_of(sessions.resume_identity_env, LIVE)
        self.assertIsNone(got)

    def test_the_resume_go_leg_actually_passes_it(self):
        """Assert the EFFECT at the call site, not just the helper: the --go
        spawn must thread resume_identity_env through, or the resumed pane
        inherits the metaharness ancestor's export again."""
        import inspect
        from helm import sessions
        src = inspect.getsource(sessions)
        self.assertIn("env=resume_identity_env(row[\"i\"])", src)

    def test_chat_name_docstring_records_the_contagion_hazard(self):
        from helm import home
        self.assertIn("OUTLIVES", home.chat_name.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
