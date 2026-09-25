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
from helm import session as helm_session  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_ROOM", "MELD_CHAT_ROOM", "HELM_CHAT_ROOM_SOURCE",
            "MELD_CHAT_ROOM_SOURCE", "HELM_CHAT_OWNER_NAMES",
            "HELM_CHAT_DELIVER", "HELM_BEACON_ORPHAN_EXIT",
            "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR", "HELM_SCRATCH_GC",
            # HELM_SEAT_NAMES IS DELIBERATELY ABSENT FROM THIS LIST, and an
            # earlier version of this file had it here, which was backwards.
            # Everything else in ENV_KEYS is isolated by being POPPED, because
            # unset means "no opinion". For the seat-name authority unset is
            # not neutral: authority_path falls back to the operator's real
            # ~/.helm/_global/seat-names.txt, so popping it is what EXPOSES
            # production. It is planted once for every module at the shared
            # boundary in tests/__init__.py, and this file must leave it alone.
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
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        # REGISTERED BEFORE THE FIRST MUTATION, NOT IN tearDown: unittest SKIPS
        # tearDown when setUp RAISES, and this setUp's last line calls out to
        # _ensure_dir — so a failure there would leave every ENV_KEY mutated for
        # every test loaded afterwards, surfacing under names that look like
        # their own bugs. addCleanup runs whether setUp completes or not.
        self.addCleanup(self._restore_env)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        # The fixture homes its posts EXPLICITLY through the same env seam
        # launched seats use — the old accidental "main" default, now stated.
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        # IdentityDisagreementTest drives stop_guard: the scratch reaper must
        # never run against the HOST's /tmp/claude-* from inside a test
        os.environ["HELM_SCRATCH_GC"] = "0"
        chat._ensure_dir()

    def census(self, *rows, **patch):
        """Plant the claude process census the claim-jump asks (session.
        _proc_claude_census) for the rest of this test: `rows` are its live
        claude rows, every completeness flag clear. `patch` replaces the
        planted census whole (return_value=...) or makes it raise
        (side_effect=...). A test may not read the host's process table,
        and a synthetic session id is held by no real process."""
        planted = {"rows": list(rows), "listing_failed": False,
                   "who_failed": False, "census_partial": False}
        p = mock.patch.object(helm_session, "_proc_claude_census",
                              **(patch or {"return_value": planted}))
        p.start()
        self.addCleanup(p.stop)

    def _restore_env(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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

class SubsystemLabelIsNotAForeignSeatTest(Base):
    """task/2195 — a name that is not a seat gets no beat and no complaint.

    Three kinds of name reach `_touch_poster_presence`: a seat, an owner or
    broadcast name, and a SUBSYSTEM LABEL. helm posts under the third
    deliberately — `post(..., who="dispatches")`, "proxywatch", "resume-turn",
    "autocompact" — so the room can see which subsystem spoke.

    THE SUBSYSTEM CASE FELL THROUGH TO A GUARD ASKING A DIFFERENT QUESTION.
    `touch_seen` asks whether the name is THIS PROCESS'S seat; a subsystem
    label never is, so a seat posting under one printed "REFUSED a cross-seat
    presence beat: this process is 'seat-under-test', not 'dispatches'". No
    cross-seat write was attempted and none was refused — the write was
    already impossible.

    AND THE COST WAS THE CHANNEL, NOT A BAD BEAT. `_warn_foreign` exists to be
    loud about a REAL identity violation; its own docstring says silence is
    what let that class run for days. Firing it on an ordinary dispatch
    verdict teaches every reader to skip the one line that must not be
    skipped. So these arms pin both directions: the subsystem label is silent,
    and a genuine foreign SEAT is still loud.
    """

    def _post_as(self, poster, seat="seat-under-test"):
        os.environ["HELM_CHAT_NAME"] = seat
        seats._FOREIGN_WARNED.clear()
        return self.stderr_of(chat._touch_poster_presence, poster)

    def test_a_subsystem_label_is_silent_and_a_foreign_seat_is_not(self):
        # THE MINIMAL PAIR, IN ONE ARM. The two names differ only in whether
        # the roster lists them, and the assertion that matters is that the
        # channel still fires for the one it was built for. An arm proving
        # only the silence would pass just as well against a deleted warning.
        self.plant({"seat-under-test": {"session": "own-sid",
                                      "sessions": ["own-sid"]},
                    "seat-b": {"session": "hc2-sid",
                                      "sessions": ["hc2-sid"]}})
        _got, err = self._post_as("seat-b")
        self.assertIn("REFUSED a cross-seat", err,
                      "a real foreign SEAT did not trip the warning, so the "
                      "silence asserted below proves nothing about labels")
        self.assertIn("seat-b", err)
        _got, err = self._post_as("dispatches")
        self.assertEqual("", err,  # noqa: VACUOUS_ASSERTION — control is the SAME door on a rostered seat, necessarily a different call
                         "a subsystem label tripped the cross-seat identity "
                         "warning: %r" % err)

    def test_no_presence_file_is_minted_for_a_label_or_for_a_stranger(self):
        # Neither name may leave a beat behind. The label because it is not a
        # seat; the stranger because presence is a per-process fact. The
        # behaviour that changed is the NOISE, and this pins that the write
        # side did not change with it.
        self.plant({"seat-under-test": {"session": "own-sid",
                                      "sessions": ["own-sid"]},
                    "seat-b": {"session": "hc2-sid",
                                      "sessions": ["hc2-sid"]}})
        for poster in ("dispatches", "seat-b"):
            self._post_as(poster)
            self.assertFalse(
                os.path.exists(seats.seen_path(poster)),
                "%r was given a presence file" % poster)
        # THE CONTROL, on the same observable: the seat's OWN post does beat,
        # or the two absences above are a broken beat path rather than two
        # correct refusals.
        self._post_as("seat-under-test")
        self.assertTrue(os.path.exists(seats.seen_path("seat-under-test")),
                        "the poster's own presence stopped being recorded")

    def test_the_processs_own_name_never_consults_the_roster(self):
        # THE REGRESSION THIS CURE ALMOST SHIPPED. Gating the beat on the
        # roster would stop presence-on-post for a seat that SPEAKS BEFORE IT
        # IS ROSTERED — a live seat with a cold dot, which is worse than the
        # noisy diagnostic being removed. The own case must short-circuit
        # ahead of the roster read, so the roster's answer cannot reach it.
        self.plant({})                          # readable, and proven empty
        os.environ["HELM_CHAT_NAME"] = "seat-under-test"
        seats._FOREIGN_WARNED.clear()
        # THE BASELINE IS MEASURED, NEVER ASSUMED ZERO. `touch_seen` reads the
        # roster itself through roster_checked, so the own-name path CANNOT
        # show zero reads — it reaches touch_seen by design, which is the
        # whole point of the short circuit. What this arm settles is whether
        # THIS DOOR adds a read of its own, so the baseline is what the same
        # name costs through touch_seen alone and the assertion is EQUALITY.
        # Pinning zero here fails against correct code; that is how this arm
        # first went red.
        with mock.patch.object(seats, "roster_acquired",
                               return_value=({}, False)) as spy:
            seats.touch_seen("seat-under-test")
        beneath = spy.call_count
        self.assertTrue(beneath,
                        "touch_seen stopped reading the roster, so an equal "
                        "count below would prove nothing")
        with mock.patch.object(seats, "roster_acquired",
                               return_value=({}, False)) as spy:
            chat._touch_poster_presence("seat-under-test")
        self.assertEqual(beneath, spy.call_count,
                         "the own-seat path read the roster %d time(s) where "
                         "touch_seen alone reads it %d — this door consulted "
                         "the roster before short-circuiting"
                         % (spy.call_count, beneath))
        self.assertTrue(
            os.path.exists(seats.seen_path("seat-under-test")),
            "a seat that speaks before it is rostered lost its beat")
        # THE CONTROL: a name that is NOT this process's is decided by THIS
        # DOOR and returns BEFORE touch_seen, so it reads the roster exactly
        # once — FEWER than the baseline. That asymmetry is what proves the
        # own path went through to touch_seen and the label path did not.
        with mock.patch.object(seats, "roster_acquired",
                               return_value=({}, False)) as spy:
            chat._touch_poster_presence("seat-b")
        self.assertEqual(1, spy.call_count)

    def test_an_unreadable_roster_proves_nothing_and_stays_loud(self):
        # THE TRI-STATE, AND THE DIRECTION THAT MATTERS. A roster that cannot
        # be read has proved nothing about who exists, so a name it cannot
        # find must NOT be treated as a subsystem label — collapsing
        # unreadable into not-a-seat would silence real cross-seat violations
        # on a transient read failure.
        self.plant({"seat-under-test": {"session": "own-sid",
                                      "sessions": ["own-sid"]}})
        with mock.patch.object(seats, "roster_acquired",
                               return_value=({}, True)):
            _got, err = self._post_as("seat-b")
        self.assertIn("REFUSED a cross-seat", err,
                      "an unreadable roster silenced a foreign beat, which is "
                      "the fail-open direction this tri-state exists to "
                      "refuse")
        # AND ITS PAIR: the same empty rows with failed=False IS proof, and
        # the same name then reads as a label. One flag, opposite dispositions.
        with mock.patch.object(seats, "roster_acquired",
                               return_value=({}, False)):
            _got, err = self._post_as("seat-b")
        self.assertEqual("", err,  # noqa: VACUOUS_ASSERTION — control is the same door with failed=True, necessarily a different call
                         "a PROVEN-empty roster still warned, so the arm "
                         "above is not reading the failed flag at all")

    def test_an_owner_name_is_still_skipped_before_the_roster_is_read(self):
        # UNCHANGED BEHAVIOUR, pinned because the new roster read sits right
        # beside it: an owner or broadcast name is not a seat either, and it
        # must not start depending on whether the roster can be read.
        self.plant({"seat-under-test": {"session": "own-sid",
                                      "sessions": ["own-sid"]}})
        # ASSERT THE CALL, NOT AN EXCEPTION. `_touch_poster_presence` wraps
        # its whole body in `except Exception: pass`, so a side_effect raise
        # here would be SWALLOWED and this arm would pass against code that
        # does read the roster first. The mock's call count is the observable
        # that survives that.
        with mock.patch.object(seats, "roster_acquired",
                               return_value=({}, False)) as spy:
            _got, err = self._post_as("daria")
        self.assertEqual(0, spy.call_count,  # noqa: VACUOUS_ASSERTION — control is the same door with a non-owner name, necessarily a different call
                         "the roster was read for an owner name")
        self.assertEqual("", err)  # noqa: VACUOUS_ASSERTION — covered by the call_count pair above, whose control is a separate call
        self.assertFalse(os.path.exists(seats.seen_path("daria")))
        # THE CONTROL ON THE SPY, so a call_count of zero cannot be the mock
        # simply never being reachable: a non-owner name DOES read the roster.
        with mock.patch.object(seats, "roster_acquired",
                               return_value=({}, False)) as spy:
            self._post_as("dispatches")
        self.assertEqual(1, spy.call_count)


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
        chat.post("@console-design are you there?", who="daria")
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
        os.environ["HELM_CHAT_NAME"] = "opus-integrator"
        # READ THE NAME THE ARM ALREADY ESTABLISHED rather than restating it:
        # every line below must speak about the SAME identity the cross-seat
        # guard reads out of the environment above, and a second literal is a
        # second thing to keep in sync.
        seat = os.environ["HELM_CHAT_NAME"]
        # THE WRITING SEAT ALREADY EXISTS, WITH A CWD AND A SIDECAR OF ITS OWN.
        # A fixture that lets write_roster CREATE this row tests the wrong
        # population: production reaches this guard when a live seat rebinds,
        # so every field below would then be asserted as an APPEARANCE, and a
        # regression that stops UPDATING an existing row stays green. Planting
        # the prior state is what turns each assertion into a change.
        self.plant({"console-design": {"session": LIVE,
                                       "sessions": [DEAD, LIVE]},
                    seat: {"session": DEAD, "sessions": [DEAD],
                           "cwd": "/helm-fixture/lane-before",
                           "project": "lane-before"}})
        seats._FOREIGN_WARNED.clear()
        # BIND TO THE SIDECAR FILE, NOT TO last_seen()'s ANSWER. last_seen falls
        # back to a row's own `last_seen` field when the sidecar is unreadable,
        # so an assertion phrased through it is only as strong as which fallback
        # happens to fire. The presence beat's observable is the file, and the
        # planted age is what makes "advanced" different from "exists".
        self.beat(seat, age=60.0)
        sidecar = seats.seen_path(seat)
        before_seen = os.stat(sidecar).st_mtime
        # ONE write carrying a half that must be REFUSED and a half that
        # must LAND — the two halves have to travel together or the arm is
        # only asserting the refusal again. The cwd is synthetic and never
        # touched: it is not under TEMP_ROOTS, so the arm keeps discriminating
        # if this fixture ever plants a prior cwd and _is_temp_cwd starts
        # deciding whether the update is allowed to overwrite it.
        _row, err = self.stderr_of(
            seats.write_roster, seat, session=LIVE,
            cwd="/helm-fixture/lane-after")
        # ONE PHRASE THAT BINDS THE ROLE, not four substrings that each appear
        # somewhere. Independent checks for CURRENT, the sid and the owner name
        # all pass against a message that has the seats BACKWARDS — the owner
        # name also occurs inside the suggested disown command, and "CURRENT"
        # would still be present while naming the thief. Only the composed
        # phrase can tell a correct diagnostic from a transposed one.
        self.assertIn("it is the CURRENT session of 'console-design'", err,
                      "the diagnostic did not name the CURRENT owner in the "
                      "owner role — the seats may be transposed")
        self.assertIn("REFUSED to bind", err)
        self.assertIn(LIVE[:12], err)
        self.assertIn("disown", err)
        r = seats.roster()
        self.assertEqual(seats.seat_for_session(LIVE), "console-design",
                         "the current owner must keep its binding")
        oi = r["opus-integrator"]
        self.assertNotIn(LIVE, [oi.get("session")] + (oi.get("sessions") or []),
                         "the refused sid leaked into the new row anyway")
        # NOT-STOLEN IS ONLY HALF THE PROPERTY. Asserting the foreign sid did
        # not arrive says nothing about what happened to the sid that was
        # ALREADY there: clearing this row's own binding outright satisfies
        # every assertion above, because "LIVE is absent" is equally true of a
        # row that kept DEAD and a row that was emptied. The refusal must cost
        # the writer NOTHING it already held.
        self.assertEqual(oi.get("session"), DEAD,
                         "the refusal cleared the writer's own prior binding — "
                         "a refused bind may not unbind what it did not touch")

        # AND THE LEGAL HALF OF THE SAME WRITE MUST LAND. The guard sets
        # `session = None` and CONTINUES; a `return` there would make the whole
        # write collateral, so a caller that legitimately moved cwd in the same
        # call would lose it while the refusal looked identical from outside.
        # THREE FIELDS, because cwd alone cannot separate a recorded value from
        # a propagated one: `project` is DERIVED from cwd, and the presence beat
        # is a further write at the tail of the same call.
        self.assertEqual(oi.get("cwd"), "/helm-fixture/lane-after",
                         "the refused bind ate the cwd update and left "
                         "the row on its previous lane")
        self.assertEqual(oi.get("project"), "lane-after",
                         "cwd landed but its derived project did not, so the "
                         "value was recorded without being propagated")
        self.assertTrue(os.path.exists(sidecar),
                        "the presence beat destroyed its own .seen sidecar")
        self.assertGreater(os.stat(sidecar).st_mtime, before_seen,
                           "the sidecar was not ADVANCED by this write — a beat "
                           "that only creates a missing file leaves every "
                           "already-present seat unbeaten and looks identical")

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

    def test_wait_accepts_a_seat_flag_naming_my_own_seat(self):  # noqa: VACUOUS_ASSERTION — assertIn(rc, (0, 1)) is a presence assertion about the verb running; the sibling arm below proves the same shape can refuse
        """The armed-beacon shape (`wait --seat <me> --follow`) must not
        regress: a process that DECLARES the seat it names is the ordinary
        case and stays untouched."""
        self.plant({"kimi": {"session": "k", "sessions": ["k"]}})
        os.environ["HELM_CHAT_NAME"] = "kimi"
        rc, _out, err = self.cmd("wait", ["--seat", "kimi", "--timeout", "0.01"])
        self.assertIn(rc, (0, 1))
        self.assertNotIn("cannot receive", err)

    def test_an_UNNAMED_process_may_no_longer_name_any_seat(self):  # noqa: VACUOUS_ASSERTION — its positive control is test_the_stated_on_behalf_form_is_ACCEPTED, the same verb in the same fixture with the flag that makes it legal
        """THE CONTRACT THAT USED TO BE PINNED HERE IS RETIRED, deliberately,
        by cross-family ruling on task/994. It read "an UN-named process may
        still name any seat", and it was the guard aimed at the wrong half:
        `_assert_own_seat` REFUSED `--seat <other>` from a process that
        declared a name and WAVED THROUGH the process that declared none —
        the one that has proven nothing. `wait --seat <victim>` from an
        unnamed shell armed the victim's beacon and drained its cursor,
        silently and by contract.

        Acting for another seat is now TYPED and STATED: `--on-behalf` mints
        an `actors.OnBehalfCapability`, which is not an identity and does not
        claim to be — a supervisor arming a beacon for a seat it is standing
        up is exactly on-behalf-of. The default is refusal."""
        self.plant({"kimi": {"session": "k", "sessions": ["k"]}})
        os.environ.pop("HELM_CHAT_NAME", None)
        rc, _out, err = self.cmd("wait", ["--seat", "kimi", "--timeout", "0.01"])
        self.assertEqual(rc, 2, "an unnamed process still drained a seat")
        self.assertIn("--on-behalf", err, "the refusal must name the way in")
        self.assertFalse(os.path.exists(seats.seen_path("kimi")),
                         "the refused wait still beat kimi's presence")

    def test_the_stated_on_behalf_form_is_ACCEPTED(self):
        """The positive half, on the same observable: the capability exists to
        be usable, and an arm that only proved the refusal would leave
        `--on-behalf` unexercised and quietly broken."""
        self.plant({"kimi": {"session": "k", "sessions": ["k"]}})
        os.environ.pop("HELM_CHAT_NAME", None)
        rc, _out, err = self.cmd("wait", ["--seat", "kimi", "--on-behalf",
                                          "--timeout", "0.01"])
        self.assertIn(rc, (0, 1), err)
        self.assertNotIn("--on-behalf", err.replace("ON BEHALF OF", ""),
                         "it refused the very form it prescribed")
        self.assertIn("ON BEHALF OF", err,
                      "acting for another seat must be LOUD — silence is what "
                      "made the fail-open dangerous")


class OrphanBeaconTest(Base):
    def test_an_orphaned_beacon_exits_instead_of_beating(self):
        """An orphaned `helm chat wait --follow` cannot wake anybody
        (native-wake-only-agent-armed), so every further beat is pure
        misinformation and every row it consumes is lost into a dead pipe."""
        self.plant({"kimi": {"session": "k", "sessions": ["k"]}})
        os.environ["HELM_CHAT_NAME"] = "kimi"
        seats.deliver(seat="kimi", room="main")        # baseline its cursor
        os.remove(seats.seen_path("kimi"))            # …and forget that beat
        chat.post("@kimi are you there?", who="daria")
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

    # ---- --seat vs the identity LAW ------------------------------------
    # Two kinds of arm, and each says which it is:
    #   CLI-BOUNDARY arms go through `seats.cmd("join", ...)` and assert rc,
    #   the roster after, and the banner — what a caller at the shell sees.
    #   The raw returned key is not observable there.
    #   DIRECT-RETURN arms call `seats.join(...)` so the returned key IS
    #   observable, and can spy on downstream consumers of it.
    # Every arm plants its own roster (empty or one row). Controls are local,
    # or explicitly shared when they exercise the identical CLI and observable;
    # an arm that cannot go red under sabotage is a decoration, and one arm
    # proves the CLI boundary surfaces a refusal at all.

    def _join_cli(self, seat_flag=None):
        args = ["--seat", seat_flag] if seat_flag else []
        return self.cmd("join", args)

    def test_a_seat_flag_against_a_ROSTERED_identity_refuses_nonzero(self):
        """No declared name; the session is ROSTERED to seat-a; --seat seat-b.
        A guard that compares the flag against the DECLARED source only is
        inert here and the split walks through: join returns seat-b, mints a
        second row, and post/react still stamp seat-a. ASSERTS: rc 2, the
        refusal names the source, no seat-b row, acting_seat still seat-a."""
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({"seat-a": {"session": LIVE, "sessions": [LIVE]}})
        rc, out, err = self._join_cli("seat-b")
        self.assertEqual(rc, 2, "a refusal that exits 0 is not a refusal to "
                                "anything scripted around it: %s%s" % (out, err))
        self.assertIn("JOIN REFUSED", err)
        self.assertIn("rostered", err, "the refusal must name WHICH source")
        r = seats.roster()
        self.assertNotIn("seat-b", r, "the refused join minted the row anyway")
        self.assertEqual(seats.acting_seat(LIVE, "/tmp/x"), "seat-a")

    def test_a_seat_flag_against_a_DECLARED_identity_refuses_nonzero(self):
        """Declared seat-a, --seat seat-b: the DECLARED source disagrees with
        the flag. Carries its OWN positive control on the same roster
        observable, because a sibling arm's write does not make this arm's
        absence a measurement. ASSERTS: control join writes seat-a; then rc 2,
        the refusal names the source, no seat-b row."""
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({"seat-a": {"session": LIVE, "sessions": [LIVE]}})
        # CONTROL FIRST: the identical CLI with an AGREEING flag writes.
        rc0, _o, e0 = self._join_cli("seat-a")
        self.assertEqual(rc0, 0, e0)
        self.assertIn("seat-a", seats.roster(), "control join did not write")
        rc, _out, err = self._join_cli("seat-b")
        self.assertEqual(rc, 2, err)
        self.assertIn("JOIN REFUSED", err)
        self.assertIn("declared", err)
        self.assertNotIn("seat-b", seats.roster())

    def test_a_seat_flag_over_a_DERIVED_floor_is_a_first_bind_and_JOINS(self):
        """The legitimate first bind. No declared name, session rostered
        nowhere — the law resolves DERIVED, which is a minted name and not an
        identity, so an explicit --seat over it is exactly how a seat gets its
        first name. Refusing here would break every explicit spawn. This is
        also the POSITIVE CONTROL for the two refusals above: the identical
        CLI, the identical flag, and it WRITES — so an empty roster in those
        arms means refused, not broken."""
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "never-rostered-session"
        self.plant({})
        rc, out, err = self._join_cli("brand-new-seat")
        self.assertEqual(rc, 0, err)
        self.assertIn("brand-new-seat", seats.roster(),
                      "the first bind did not write")
        # The absence assertion is anchored on a line proven PRESENT: the join
        # line names the seat it bound, so "not refused" is a claim about a
        # real message and not about empty output.
        self.assertIn("brand-new-seat", out + err)
        self.assertNotIn("JOIN REFUSED", out + err)

    def test_a_seat_flag_that_AGREES_with_the_law_joins(self):
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({"seat-a": {"session": LIVE, "sessions": [LIVE]}})
        rc, out, err = self._join_cli("seat-a")
        self.assertEqual(rc, 0, err)
        self.assertIn("seat-a", out + err, "the join line names the seat")
        self.assertNotIn("JOIN REFUSED", out + err)
        self.assertIn("seat-a", seats.roster(), "the agreeing join wrote")

    def test_a_casefold_agreeing_flag_adopts_the_CANONICAL_spelling(self):
        """Declared seat-a, --seat SEAT-A: casefold agreement must not let
        argv's spelling become the roster KEY — a second spelling of one seat
        is a split by another route (exact-match family/runtime lookups on the
        wrong case miss). ASSERTS: rc 0, not refused, roster key is seat-a
        and SEAT-A is not a key."""
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({"seat-a": {"session": LIVE, "sessions": [LIVE]}})
        rc, out, err = self._join_cli("SEAT-A")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("JOIN REFUSED", out + err)
        r = seats.roster()
        self.assertIn("seat-a", r, "canonical spelling must be the key")
        self.assertNotIn("SEAT-A", r,
                         "argv's spelling became a SECOND roster key")

    def test_the_prelaunch_child_prejoin_is_NOT_refused(self):
        """launch pre-joins the CHILD's seat from the PARENT's process before
        the child environ exists, so the parent's declared name is a different
        agent's and disagreement is expected; that call carries session=None
        because the child does not exist yet — the discriminator. A guard that
        refuses it loses pre-boundary addressability, and launch would discard
        the refusal silently. ASSERTS: the returned seat is the child, no
        refusal in the line, the child row is registered."""
        os.environ["HELM_CHAT_NAME"] = "the-parent"
        self.plant({"the-parent": {"session": LIVE, "sessions": [LIVE]}})
        seat, line = seats.join(session=None, cwd="/tmp/x", seat="the-child")
        self.assertEqual(seat, "the-child")
        self.assertNotIn("JOIN REFUSED", line)
        self.assertIn("the-child", seats.roster(),
                      "the pre-join did not register the child")

    def test_three_casefold_spellings_yield_ONE_durable_key_and_return_it(self):
        """Declared Seat-A, roster key seat-a, flag SEAT-A: three spellings of
        one seat. The RETURN must carry the durable key, not a source's
        spelling — exact-match lookups on the return value would otherwise
        miss. ASSERTS: join returns seat-a, no refusal, exactly one roster
        key. Family/spawn-bind consumers of that return are NOT asserted here;
        the mutation arm below covers the return/durable/banner agreement."""
        os.environ["HELM_CHAT_NAME"] = "Seat-A"
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({"seat-a": {"session": LIVE, "sessions": [LIVE]}})
        seat, line = seats.join(session=LIVE, cwd="/tmp/x", seat="SEAT-A")
        self.assertEqual(seat, "seat-a", "the RETURN carried a source spelling")
        self.assertNotIn("JOIN REFUSED", line)
        self.assertEqual(sorted(seats.roster()), ["seat-a"],
                         "a second casefold-variant key was minted")

    def test_first_admission_on_an_EMPTY_roster_uses_the_laws_spelling(self):
        """EMPTY roster, declared seat-a, --seat SEAT-A. Relying on the locked
        writer alone to canonicalize fails here: there is no existing key to
        preserve, so argv's spelling would become the first key while
        acting_seat (declared wins) stays seat-a — the split, on first
        admission. Agreement must seed the law's spelling BEFORE the write.
        This is the pole the between-view-and-write mutation arm cannot see
        (that one plants a row; this one plants none). ASSERTS, through the
        real CLI: rc 0, roster key seat-a, banner names seat-a, acting_seat
        seat-a. The raw returned key is not observable through the CLI and is
        not asserted here."""
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({})                                  # EMPTY: nothing to keep
        rc, out, err = self._join_cli("SEAT-A")
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(seats.roster()), ["seat-a"],
                         "first admission keyed on argv's spelling, not the "
                         "declared authority's")
        self.assertIn("seat 'seat-a'", out + err)
        self.assertEqual(seats.acting_seat(LIVE, "/tmp/x"), "seat-a")

    def test_a_case_change_between_view_and_write_cannot_split_the_key(self):
        """A pre-write read of the canonical key and the locked write are two
        roster snapshots; a case-only rename between them can leave the
        durable key `seat-a` while join returns and banners `Seat-A`. The key
        must come FROM the locked write. This arm renames the durable key
        inside the writer's own locked read (roster_for_write), after any
        pre-write view could have run. ASSERTS: durable key, returned seat and
        banner all name seat-a. It calls seats.join directly (not the CLI) so
        the raw return is observable; family/spawn-bind consumers are NOT
        asserted here."""
        from helm import seats_roster
        os.environ["HELM_CHAT_NAME"] = "Seat-A"
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({"Seat-A": {"session": LIVE, "sessions": [LIVE]}})
        real = seats_roster.roster_for_write

        def renamed_under_lock():
            r = real()
            # UNCONDITIONAL, so the mutation FAILS LOUD (KeyError) if the
            # planted key is not there to rename — a conditional here would
            # let fixture normalization turn the defining mutation into a
            # no-op while the arm still passed.
            r["seat-a"] = r.pop("Seat-A")   # case-only rename, view -> write
            return r
        with mock.patch.object(seats_roster, "roster_for_write",
                               renamed_under_lock):
            seat, line = seats.join(session=LIVE, cwd="/tmp/x", seat="SEAT-A")
        durable = sorted(seats.roster())
        self.assertEqual(durable, ["seat-a"], "the write did not land on the "
                                              "renamed key: %s" % durable)
        self.assertEqual(seat, "seat-a",
                         "join RETURNED a spelling the write did not use")
        self.assertIn("seat 'seat-a'", line,
                      "the banner names a spelling the write did not use")
        self.assertNotIn("Seat-A", line)

    def test_the_effective_key_reaches_every_downstream_consumer(self):
        """DIRECT-RETURN arm. Under the between-view-and-write mutation, the
        key the locked write chose must be the ONE spelling handed to the
        case-SENSITIVE consumers after the write. The fixture is a KNOWN
        family (codex): _seat_family is case-sensitive, and production reaches
        _bind_spawn_session only when the family resolves, so an unknown-
        family fixture would never reach the bind and an assertion on it
        would be vacuous. ASSERTS: _seat_family saw exactly ['codex'];
        _bind_spawn_session was reached and saw exactly ['codex'] —
        unconditionally, never gated on the call having happened. NOT
        ASSERTED, deliberately: DM lane and cursor path, pinned below AS
        case-blind (both casefold their seat), so no spelling can split them
        and an equality on them proves nothing about this defect."""
        from helm import seats_roster, seat as seat_mod, seats_common
        from helm import seats_delivery
        os.environ["HELM_CHAT_NAME"] = "Codex"
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({"Codex": {"session": LIVE, "sessions": [LIVE]}})
        real = seats_roster.roster_for_write

        def renamed_under_lock():
            r = real()
            r["codex"] = r.pop("Codex")     # unconditional: KeyError if absent
            return r
        fam_calls, bind_calls = [], []
        real_family = seat_mod._seat_family

        def spy_family(name):
            fam_calls.append(name)
            return real_family(name)

        def spy_bind(name, session, source=None):
            bind_calls.append(name)
            return None
        with mock.patch.object(seats_roster, "roster_for_write",
                               renamed_under_lock), \
                mock.patch.object(seat_mod, "_seat_family", spy_family), \
                mock.patch.object(seat_mod, "_bind_spawn_session", spy_bind):
            joined, _line = seats.join(session=LIVE, cwd="/tmp/x",
                                       seat="CODEX")
        self.assertEqual(joined, "codex")
        self.assertEqual(fam_calls, ["codex"],
                         "family lookup saw a spelling the write did not use")
        self.assertEqual(bind_calls, ["codex"],
                         "spawn-session binding was not reached with the "
                         "effective key — a known family MUST reach it")
        # The two case-blind consumers, pinned AS case-blind so a future
        # change that makes either case-sensitive turns this into a claim
        # that needs a real assertion above.
        self.assertEqual(seats_common.dm_lane("codex"),
                         seats_common.dm_lane("Codex"))
        self.assertEqual(seats_delivery.cursor_path("main", "codex", LIVE),
                         seats_delivery.cursor_path("main", "Codex", LIVE))

    def test_a_typoed_flag_REFUSES_instead_of_minting_a_stranger(self):
        """`join --seet wanted` accepted with rc 0 would mint an auto-derived
        seat with its own presence and cursors — a verb answering a different
        question than the one asked. The hand-typed leg refuses; the hook leg
        stays fail-open. ASSERTS: rc 2, 'unknown arg', no stranger row; and
        the control that the correctly-spelled flag is accepted (rc 0)."""
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = LIVE
        self.plant({"seat-a": {"session": LIVE, "sessions": [LIVE]}})
        rc, _out, err = self.cmd("join", ["--seet", "wanted"])
        self.assertEqual(rc, 2, err)
        self.assertIn("unknown arg", err)
        self.assertEqual(sorted(seats.roster()), ["seat-a"],
                         "the typo minted a stranger seat")
        # CONTROL: the correctly-spelled flag on the same fixture is accepted
        # (it agrees with the rostered identity), so rc 2 above is about the
        # typo and not about the fixture.
        rc2, _o2, e2 = self.cmd("join", ["--seat", "seat-a"])
        self.assertEqual(rc2, 0, e2)

    def test_the_refusal_arms_go_RED_under_sabotage(self):
        """Proves the harness can see a refusal at all: forces one on the
        CONTROL path (a plain first bind) and asserts the CLI reports it. Arms
        that pre-plant success and discard the second result stay green when
        the join is sabotaged; if THIS arm passes while the refusal arms above
        also pass, the harness is blind and every one of them is decorative.
        ASSERTS: the sabotage marker came back (the double is in effect) and
        rc 2."""
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "never-rostered-session"
        self.plant({})
        # PATCH THE NAME THE CLI LEG ACTUALLY READS. join_cli lives in
        # seats_join and calls `join` by its module-global name, so the patch
        # target is seats_join.join — not the `seats` facade and not seats_cli
        # (both bind their own copy at import; patching either leaves the CLI
        # calling the real join and the arm reads '' back). Prove the double
        # bound: the sabotage marker must be what comes back, not a real
        # refusal.
        from helm import seats_join
        with mock.patch.object(seats_join, "join",
                               return_value=("x", "[helm chat] JOIN REFUSED: "
                                                  "sabotage-marker-7f3a")):
            rc, _out, err = self._join_cli("brand-new-seat")
        self.assertIn("sabotage-marker-7f3a", err,
                      "the double never bound — the CLI ran the real join")
        self.assertEqual(rc, 2, "the CLI did not surface a refusal the join "
                                "returned — the arms above cannot go red")

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
        # shape, and the one r1's fixtures never staged. OCCUPIED means a
        # live process holds the session, so the census plants that holder.
        self.census({"pid": 4242, "session": LIVE})
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
        self.census({"pid": 4242, "session": LIVE})    # LIVE has a process

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
        self.census({"pid": 4242, "session": LIVE})    # the seat's live pane
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
        chat.post("@console-design are you there?", who="daria")
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
        # agreement passes: the verb works the moment the sources agree.
        # `_seat_actor` hands back an ADMITTED ACTOR now, not a name (task/994):
        # answering with a string is what let a MINTED one flow into every
        # signing verb as `who=`. The label comes out of the capability
        # explicitly, and `actor == "console-design"` is deliberately FALSE —
        # a capability that compared equal to its own name would be silently
        # substitutable for the string it replaced.
        os.environ["HELM_CHAT_NAME"] = "console-design"
        actor, err = chat._seat_actor([])
        self.assertIsNone(err)
        self.assertEqual(actor.canonical_name, "console-design")

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
# F2. A RELAUNCH ONTO A FRESH SESSION IS NOT A CLAIM-JUMP (task/2594)
# ---------------------------------------------------------------------------

PRED = "44444444-aaaa-4bbb-8ccc-444444444444"   # the seat's gone predecessor
PANE = "55555555-aaaa-4bbb-8ccc-555555555555"   # its relaunched pane's session


class RelaunchIsNotAClaimJumpTest(Base):
    """A seat relaunched onto a FRESH session still names its predecessor on
    its roster row, because nothing retires a binding when its pane dies. The
    claim-jump must ask the process census whether that session is still
    HELD. Reading the record as a live occupant refused the new pane's own
    join AND every beacon it armed: `helm chat wait --follow` printed its
    refusal and exited before `beacons.arm` ran, so the beacon census read a
    live pane DEAF, `helm seat list` said UNUSABLE "no live beacon", and
    dispatch would not book work to it.

    Every arm plants the census it reads. The identical fixture with the
    predecessor HELD is the positive control, so the absent refusal in the
    first arm is a measurement and not an inert predicate."""

    SEAT = "relaunched-seat"

    def setUp(self):
        super().setUp()
        self.plant({self.SEAT: {"session": PRED, "sessions": [PRED]}})
        os.environ["HELM_CHAT_NAME"] = self.SEAT
        os.environ["CLAUDE_CODE_SESSION_ID"] = PANE    # the pane's own sid
        seats._FOREIGN_WARNED.clear()

    def arm_beacon(self):
        """The Monitor's command, through the CLI door, with the registry
        write stubbed: -> (rc, beacons.arm mock, stdout, stderr)."""
        report = {"seat": self.SEAT, "pid": 5252, "stopped": [], "kept": [],
                  "pruned": [], "registered": False, "already_live": None,
                  "conflict": None}
        with mock.patch("helm.beacons.arm", return_value=report) as arm, \
                mock.patch("helm.seats_cli.wait", return_value=None):
            rc, out, err = self.cmd("wait", ["--seat", self.SEAT, "--follow",
                                             "--timeout", "0.01"])
        return rc, arm, out, err

    def test_a_predecessor_no_process_holds_does_not_lock_out_the_pane(self):
        self.census({"pid": 5151, "session": PANE})    # only the new pane runs
        self.assertIsNone(seats.identity_disagreement(PANE),
                          "a session no process holds is a seat between "
                          "panes, not a live occupant")
        rc, arm, out, err = self.arm_beacon()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("REFUSING", out)
        arm.assert_called_once()
        self.assertEqual(arm.call_args[0][0], self.SEAT)
        self.assertEqual(arm.call_args[1]["session"], PANE,
                         "the beacon registers under the pane's own session")
        _seat, line = seats.join(session=PANE, cwd="/tmp/x")
        self.assertNotIn("JOIN REFUSED", line)
        row = seats.roster()[self.SEAT]
        self.assertEqual(row["session"], PANE,
                         "SessionStart binds the relaunched pane's session")
        self.assertIn(PRED, row["sessions"], "history stays addressable")

    def test_a_predecessor_a_process_still_holds_is_refused(self):  # noqa: VACUOUS_ASSERTION — each absence has an unconditional positive on the same observable in this arm: `REFUSING to arm` is asserted present on the stdout the arm never ran past, `rostered NOWHERE` on the line whose takeover wording is absent, and the sibling arm calls the identical stub once
        """POSITIVE CONTROL: the same fixture with the predecessor held."""
        self.census({"pid": 4242, "session": PRED},
                    {"pid": 5151, "session": PANE})
        self.assertEqual(seats.identity_disagreement(PANE),
                         (self.SEAT, self.SEAT))
        _rc, arm, out, _err = self.arm_beacon()
        self.assertIn("REFUSING to arm", out)
        arm.assert_not_called()
        _seat, line = seats.join(session=PANE, cwd="/tmp/x")
        self.assertIn("JOIN REFUSED", line)
        # THE JOIN SAYS THE SHAPE IT REFUSED: this pane's session is rostered
        # nowhere, and the takeover wording would say it was rostered to a
        # seat.
        self.assertIn("rostered NOWHERE", line)
        self.assertNotIn("is rostered to", line)
        self.assertIn(PRED[:8], line)
        self.assertEqual(seats.roster()[self.SEAT]["session"], PRED,
                         "a refused join must not touch the standing binding")

    def test_a_census_that_cannot_prove_the_predecessor_gone_refuses(self):  # noqa: VACUOUS_ASSERTION — the one None is the control, and the unconditional assertEqual over all eight refusing censuses is the positive on the same identity_disagreement call
        """FAILS CLOSED: only a census read whole, with no holder and no
        candidate, opens the seat. The None is the control on the same
        observable, so the refusals below cannot come from a predicate that
        refuses everything, and they cannot pass on one that refuses
        nothing."""
        free = {"rows": [{"pid": 5151, "session": PANE}],
                "listing_failed": False, "who_failed": False,
                "census_partial": False}
        with mock.patch.object(helm_session, "_proc_claude_census",
                               return_value=free):
            self.assertIsNone(seats.identity_disagreement(PANE),
                              "control: read whole, nothing holds it")
        refusing = (
            ("the census raised", {"side_effect": OSError("proc")}),
            ("the table could not be listed",
             {"return_value": dict(free, listing_failed=True)}),
            ("a pid stopped before comm proved it",
             {"return_value": dict(free, census_partial=True)}),
            ("the who rung was lost",
             {"return_value": dict(free, who_failed=True)}),
            ("a contract key is missing",
             {"return_value": {"rows": free["rows"]}}),
            ("rows is not a list", {"return_value": dict(free, rows=None)}),
            ("a row could not be probed",
             {"return_value": dict(free, rows=[
                 {"pid": 5151, "session": PANE},
                 {"pid": 6161, "probe_failed": True}])}),
            ("an unattributed row could hold it",
             {"return_value": dict(free, rows=[
                 {"pid": 5151, "session": PANE},
                 {"pid": 6161, "possible_sessions": [PRED]}])}),
        )
        verdicts = {}
        for label, patch in refusing:
            with mock.patch.object(helm_session, "_proc_claude_census",
                                   **patch):
                verdicts[label] = seats.identity_disagreement(PANE)
        self.assertEqual(verdicts, {label: (self.SEAT, self.SEAT)
                                    for label, _patch in refusing})


# ---------------------------------------------------------------------------
# G. HELM-OWNED LAUNCH PATHS SET THE NAME, NEVER INHERIT (fix set D)
# ---------------------------------------------------------------------------

class LaunchIdentityEnvTest(Base):
    def test_resume_exports_the_one_rostered_seat(self):
        from helm import sessions
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        self.assertEqual(sessions.resume_identity_env(LIVE),
                         {"HELM_CHAT_NAME": "console-design",
                          "HELM_CELL_PROFILE": "console-design",
                          "DREGG_PROFILE": "console-design"})

    def test_an_unknown_or_ambiguous_sid_stays_nameless(self):  # noqa: VACUOUS_ASSERTION — the positive control IS inline (the single-owner plant asserts the helper exports before the Nones), and mutation M13 (`if hits` for `len(hits) == 1`) reddens exactly this test
        """Nameless is honest; a guessed name is incident 1."""
        from helm import sessions
        self.plant({"console-design": {"session": LIVE, "sessions": [LIVE]}})
        # positive control on the SAME observable: the single-owner sid DOES
        # export — the Nones below are the refusals, not a dead helper
        self.assertEqual(sessions.resume_identity_env(LIVE),
                         {"HELM_CHAT_NAME": "console-design",
                          "HELM_CELL_PROFILE": "console-design",
                          "DREGG_PROFILE": "console-design"})
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
