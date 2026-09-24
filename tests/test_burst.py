#!/usr/bin/env python3
"""Arms for `helm/burst.py` — the measured burst exemption.

THE FIXTURE IS A MEASURED READING, not an invented world. Every window number
below is the reading taken from a real homed credential while the project was
ORANGE: session 0% resetting in 3h49m, account week 85% resetting in 13h59m, a
model-scoped week at 100%. An arm that grants on numbers nobody observed proves
nothing about the case the rule exists for.
"""
import time
import unittest

from helm import burnflags
from helm import burst
from helm import registry


SESSION_LEFT_S = 3 * 3600 + 49 * 60        # the episode's own session reset
WEEK_LEFT_S = 13 * 3600 + 59 * 60          # the episode's own weekly reset
NOW = 1_800_000_000.0


def gauge(label, used_pct, left_s, now=NOW):
    return {"label": label, "kind": "period", "limit": None, "remaining": None,
            "utilization": used_pct / 100.0, "reset": now + left_s}


def row(account="a@example.com", status="allowed", gauges=None, age_s=10.0,
        now=NOW):
    return {"provider": "anthropic", "account": account,
            "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(now - age_s)),
            "status": status, "primary": "5h",
            "gauges": [] if gauges is None else gauges}


def episode(**kw):
    """The measured reading, as one history row."""
    return row(gauges=[gauge("5h", 0.0, SESSION_LEFT_S),
                       gauge("7d", 85.0, WEEK_LEFT_S),
                       gauge("7d-fable", 100.0, WEEK_LEFT_S)], **kw)


def ask(history, now=NOW, config_dir=None, account="a@example.com"):
    """`exemption` with the home identity stubbed — the arms are about the
    ARITHMETIC, and a real home would make every one of them machine-specific."""
    saved = burst.homed_account
    burst.homed_account = lambda config_dir=None: (account, "a…@example.com",
                                                   None)
    try:
        return burst.exemption(now=now, config_dir=config_dir,
                               history=history)
    finally:
        burst.homed_account = saved


class MeasuredEpisode(unittest.TestCase):
    def test_the_owners_own_reading_grants(self):
        """RED-PROOF: with the surplus term deleted (grant on room alone) this
        arm still passes, which is why `test_the_same_room_six_days_out_refuses`
        below is its mandatory sibling — one arm cannot show the rule is about
        perishability."""
        rec = ask([episode()])
        self.assertTrue(rec["granted"], rec["why"])
        self.assertEqual(rec["state"], burst.GRANTED)
        # 15 points of quota left, 8.3% of the week still to run.
        self.assertAlmostEqual(rec["surplus_pct"], 6.7, delta=0.3)
        self.assertEqual(rec["binding"], "7d")

    def test_the_grant_names_the_credential_and_never_its_address(self):
        rec = ask([episode()])
        self.assertEqual(rec["credential"], "a…@example.com")
        self.assertIn("a…@example.com", rec["why"])
        self.assertNotIn("a@example.com", rec["why"])
        self.assertNotIn("a@example.com", "\n".join(burst.render(rec, now=NOW)))

    def test_the_grant_carries_the_behaviour_tables_own_capacity(self):
        """RED-PROOF: hard-code 4 here and the arm survives a change to the
        owner's table, which is the second-policy defect. It asserts the
        IMPORT — and that the lift is a lift, since raising the cap to one
        delegate would have lifted nothing at all."""
        rec = ask([episode()])
        self.assertEqual(rec["capacity"],
                         burnflags.BEHAVIOUR[burnflags.GREEN]["capacity"])
        self.assertGreater(rec["capacity"], 1)

    def test_the_fable_wall_is_excluded_from_the_surplus_and_named(self):
        """The episode's own hazard: a burst of Fable subagents would have hit
        a wall the account-wide windows cannot see."""
        rec = ask([episode()])
        self.assertTrue(rec["granted"])
        self.assertEqual([w["label"] for w in rec["windows"]], ["5h", "7d"])
        self.assertEqual([w["label"] for w in rec["scoped"]], ["7d-fable"])
        self.assertIn("NOT covered", rec["why"])
        self.assertIn("7d-fable", rec["why"])

    def test_red_proof_removing_the_scoped_exclusion_changes_the_answer(self):
        """The exclusion is load-bearing, shown on the episode's own numbers:
        `7d-fable` at 100% with 8.3% of its week to run carries a surplus of
        -8.3, which would have been the minimum and refused the grant the owner
        asked for. Counting it is a DIFFERENT answer, so the arm above is not
        vacuous."""
        rec = ask([episode()])
        self.assertTrue(rec["granted"])
        scoped = rec["scoped"][0]
        left_pct = 100.0 * WEEK_LEFT_S / (7 * 86400)
        would_be = (100.0 - scoped["used_percent"]) - left_pct
        self.assertLess(would_be, burst.floor_pct())
        self.assertLess(would_be, rec["surplus_pct"])

    def test_a_scoped_window_is_recognised_by_shape_not_by_name(self):
        """RED-PROOF for the hazard this arm was written after hitting: match
        the literal `7d-fable` only, and a second scoped window the vendor adds
        tomorrow cannot be sized, lands in the unsized arm, and silently blocks
        EVERY grant on the host."""
        rec = ask([row(gauges=[gauge("5h", 0.0, SESSION_LEFT_S),
                               gauge("7d", 85.0, WEEK_LEFT_S),
                               gauge("7d-opus", 100.0, WEEK_LEFT_S)])])
        self.assertTrue(rec["granted"], rec["why"])
        self.assertEqual([w["label"] for w in rec["scoped"]], ["7d-opus"])
        self.assertIn("7d-opus", rec["why"])


class PerishabilityIsTheRule(unittest.TestCase):
    def test_the_same_room_six_days_out_refuses(self):
        """The SIBLING of the grant arm. Identical room, a distant reset: the
        pacing policy is right and there is no exemption."""
        rec = ask([row(gauges=[gauge("5h", 0.0, SESSION_LEFT_S),
                               gauge("7d", 85.0, 6 * 86400)])])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["state"], burst.NO_SURPLUS)
        self.assertIn("nothing here is perishable", rec["why"])
        self.assertLess(rec["surplus_pct"], 0)

    def test_a_fresh_week_is_not_perishable(self):
        """95% of the quota left with 93% of the week to run is +2 — real, and
        below the floor. Nothing here needs bursting."""
        rec = ask([row(gauges=[gauge("5h", 0.0, SESSION_LEFT_S),
                               gauge("7d", 5.0, int(6.5 * 86400))])])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["state"], burst.NO_SURPLUS)
        self.assertIn("under the 5.0 floor", rec["why"])
        self.assertGreater(rec["surplus_pct"], 0)
        self.assertLess(rec["surplus_pct"], burst.floor_pct())

    def test_the_tightest_window_decides_not_the_perishable_one(self):
        """The episode's weekly surplus, behind a session window at its wall.
        RED-PROOF: take the MAX instead of the MIN and this arm goes green
        while the burst runs into the session wall four hours from now."""
        rec = ask([row(gauges=[gauge("5h", 95.0, SESSION_LEFT_S),
                               gauge("7d", 85.0, WEEK_LEFT_S)])])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["binding"], "5h")
        self.assertIn("tightest window (5h)", rec["why"])
        self.assertLess(rec["surplus_pct"], 0)

    def test_the_floor_moves_with_the_one_ceiling_knob(self):
        tight, loose = burst.floor_pct(ceiling=90.0), burst.floor_pct(
            ceiling=80.0)
        self.assertAlmostEqual(tight, 5.0)
        self.assertAlmostEqual(loose, 10.0)
        self.assertGreater(loose, tight)
        live = burst.floor_pct()
        self.assertAlmostEqual(live, (100.0 - burnflags.ceiling_pct()) / 2.0)
        self.assertGreater(live, 0.0)


class TheExemptionExpires(unittest.TestCase):
    def test_the_measured_episode_expires_on_its_reading_not_its_window(self):
        """RED-PROOF: drop the `stale_at` bound and this arm reports an expiry
        HOURS past the moment the evidence stops being evidence. The freshness
        bound is minutes and a window reset is hours, so in practice this is
        the bound that binds — a grant is a short-lived answer, re-measured."""
        rec = ask([episode()])
        self.assertTrue(rec["granted"], rec["why"])
        self.assertEqual(rec["expires_kind"], "reading-stale")
        self.assertAlmostEqual(rec["expires_at"],
                               NOW - 10.0 + burnflags.max_age_s(), delta=2.0)
        self.assertLess(rec["expires_at"], rec["window_reset_at"])

    def test_it_expires_at_the_window_reset_when_that_is_sooner(self):
        """A reading taken moments before its own window turns over: the
        arithmetic dies with the window, not with the reading."""
        rec = ask([row(gauges=[gauge("5h", 0.0, 120),
                               gauge("7d", 85.0, WEEK_LEFT_S)])])
        self.assertTrue(rec["granted"], rec["why"])
        self.assertEqual(rec["expires_kind"], "window-reset")
        self.assertAlmostEqual(rec["expires_at"], NOW + 120, delta=1.0)

    def test_both_instants_are_carried_so_a_reader_can_tell_them_apart(self):
        rec = ask([episode()])
        self.assertEqual(rec["state"], "granted")
        self.assertAlmostEqual(rec["window_reset_at"], NOW + SESSION_LEFT_S,
                               delta=1.0)
        self.assertGreater(rec["window_reset_at"], NOW)
        self.assertGreater(rec["expires_at"], NOW)

    def test_an_expiry_is_never_absent_on_a_grant(self):
        """A grant with no instant would be exactly the standing licence the
        module refuses to mint."""
        rec = ask([episode()])
        self.assertEqual(rec["state"], "granted")
        self.assertGreater(rec["expires_at"], NOW)
        self.assertIn(rec["expires_kind"], ("window-reset", "reading-stale"))

    def test_the_same_reading_refuses_once_its_window_has_reset(self):
        """Time alone revokes it: nothing has to remember to."""
        after = NOW + WEEK_LEFT_S + 60
        rec = ask([episode()], now=after)
        self.assertFalse(rec["granted"])
        self.assertIn("stale reading is never headroom", rec["why"])


class NothingUnmeasuredIsHeadroom(unittest.TestCase):
    def test_a_stale_reading_is_never_headroom(self):
        rec = ask([episode(age_s=burnflags.max_age_s() + 60)])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["state"], burst.STALE)
        self.assertIn("stale reading is never headroom", rec["why"])

    def test_an_absent_row_is_not_a_grant(self):
        rec = ask([episode(account="somebody-else@example.com")])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["state"], burst.UNREAD)
        self.assertIn("an absent reading is not headroom", rec["why"])

    def test_a_status_carrying_no_gauges_is_unreadable_not_open(self):
        """And it carries the probe's WHOLE sentence: the parenthesis is the
        difference between a spent account and a dead copy of a live token,
        which are different repairs by different people."""
        rec = ask([row(status="reauth-needed (helm's token expired 1d ago; "
                              "orca refreshes its own store, not this one)",
                       gauges=[])])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["state"], burst.UNREADABLE)
        self.assertIn("orca refreshes its own store", rec["why"])

    def test_an_unreadable_row_accuses_the_READING_and_never_the_credential(self):
        """THE REFUSAL MUST NOT SEND A READER TO A DESTRUCTIVE REPAIR.

        This branch is reachable while the credential is SERVING THE SEAT THAT
        READS THE LINE — a live account whose newest probe answered
        reauth-needed, because helm's on-disk copy of the token went stale
        while orca refreshed its own. Wording it as "helm cannot see this
        credential" is false there, and it points at re-authenticating the
        home: a mutation the whole fleet shares. The true statement — helm
        holds no MEASUREMENT — points at the cheap refresh the status text
        already names. A seat that reads the credential-accusing wording off
        this line goes on to tell the owner a working credential needs a
        re-login, which is the measured cost of getting this sentence wrong.
        """
        rec = ask([row(status="reauth-needed (helm's token expired 1d ago; "
                              "orca refreshes its own store, not this one)",
                       gauges=[])])
        self.assertEqual(rec["state"], burst.UNREADABLE,
                         "control: this is the branch under test")
        why = rec["why"]
        # WHAT IT MUST SAY: the subject is the reading.
        self.assertIn("MEASUREMENT", why)
        self.assertIn("not about the credential", why)
        # WHAT IT MUST NOT SAY, and the control that this detector can fire:
        # the same predicate over the old sentence must come back True.
        def accuses_the_credential(text):
            low = text.lower()
            return "cannot see this credential" in low or "cannot spend it" in low
        self.assertTrue(
            accuses_the_credential("helm cannot SEE this credential, so it "
                                   "cannot spend it"),
            "control: the detector does recognise the wording it forbids")
        self.assertFalse(accuses_the_credential(why),
                         "the refusal accuses the credential: %r" % why)

    def test_a_blocked_account_is_read_and_refused_on_its_numbers(self):
        """`blocked` carries the vendor's own gauges — it is a READ account at
        its ceiling, and it must refuse on arithmetic rather than on status."""
        rec = ask([row(status="blocked",
                       gauges=[gauge("5h", 100.0, SESSION_LEFT_S),
                               gauge("7d", 100.0, WEEK_LEFT_S)])])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["state"], burst.NO_SURPLUS)
        self.assertEqual([w["label"] for w in rec["windows"]], ["5h", "7d"])

    def test_a_window_with_a_deadline_and_no_length_blocks_the_grant(self):
        """RED-PROOF: drop the unsized window from the minimum instead of
        blocking, and this arm grants while an unread wall is invisible."""
        rec = ask([row(gauges=[gauge("weekly", 0.0, WEEK_LEFT_S),
                               gauge("7d", 85.0, WEEK_LEFT_S)])])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["state"], burst.UNSIZED)
        self.assertIn("not a measured one", rec["why"])

    def test_an_overage_gauge_is_not_a_window_and_does_not_block(self):
        over = {"label": "overage", "kind": "overage", "utilization": 0.4,
                "reset": None}
        rec = ask([row(gauges=[gauge("5h", 0.0, SESSION_LEFT_S),
                               gauge("7d", 85.0, WEEK_LEFT_S), over])])
        self.assertTrue(rec["granted"], rec["why"])
        self.assertNotIn("overage", [w["label"] for w in rec["windows"]])

    def test_an_unhomed_seat_gets_no_exemption(self):
        rec = burst.exemption(now=NOW, config_dir="/nonexistent/home",
                              history=[episode()])
        self.assertFalse(rec["granted"])
        self.assertEqual(rec["state"], burst.NOT_HOMED)
        self.assertIn("the pool flag governs", rec["why"])

    def test_the_reader_never_raises_across_its_edge(self):
        """A door that starts work must not stop because a log is malformed."""
        rec = ask([{"account": "a@example.com", "gauges": "not-a-list"}])
        self.assertFalse(rec["granted"])
        self.assertIn(rec["state"], burst.STATES)
        self.assertTrue(len(rec["why"]) > 10, rec)

    def test_every_refusal_says_which_one_it_was(self):
        control = ask([episode()])
        self.assertEqual(control["state"], "granted")
        whys = [ask([episode(age_s=burnflags.max_age_s() + 60)]),
                ask([episode(account="other@example.com")]),
                ask([row(status="network-error", gauges=[])])]
        self.assertEqual([r["granted"] for r in whys], [False, False, False])
        self.assertEqual(len({r["state"] for r in whys}), 3)
        self.assertTrue(all(len(r["why"]) > 10 for r in whys), whys)


class ItLiftsTheCountAndNothingElse(unittest.TestCase):
    def test_the_note_is_only_minted_on_a_grant(self):
        absent = burst.note(now=NOW, record=ask([episode()],
                                                now=NOW + WEEK_LEFT_S + 60))
        text = burst.note(now=NOW, record=ask([episode()]))
        self.assertIn("BURST EXEMPTION", text)
        self.assertIsNone(absent)

    def test_the_note_keeps_the_reach_rule_and_the_light_intact(self):
        text = burst.note(now=NOW, record=ask([episode()]))
        self.assertIn("lifts the COUNT and nothing else", text)
        self.assertIn("dark is still dark", text)
        self.assertIn("never speculative", text)
        self.assertIn("still decides whether work may start", text)

    def test_the_note_names_the_count_the_table_gives(self):
        text = burst.note(now=NOW, record=ask([episode()]))
        self.assertIn(str(burnflags.BEHAVIOUR[burnflags.GREEN]["capacity"]),
                      text)

    def test_the_clause_is_orange_only(self):
        """Yellow, green and grey say nothing about a delegate count, and red
        does not admit new work at all — appending arithmetic under any of them
        answers a question that colour never asked. The CONTROL is the same
        stub under orange: it proves the silence above is the colour test and
        not a reading that happened to be absent."""
        saved = burst.note
        burst.note = lambda *a, **k: "GRANTED-TEXT"
        try:
            orange = registry._burst_clause("orange")
            others = [registry._burst_clause(c)
                      for c in ("green", "yellow", "red")]
        finally:
            burst.note = saved
        self.assertEqual(orange, "\nGRANTED-TEXT")
        self.assertEqual(others, ["", "", ""])

    def test_the_clause_fails_quiet_when_the_reading_fails(self):
        saved = burst.note
        burst.note = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            broken = registry._burst_clause("orange")
        finally:
            burst.note = saved
        burst.note = lambda *a, **k: "GRANTED-TEXT"
        try:
            working = registry._burst_clause("orange")
        finally:
            burst.note = saved
        self.assertEqual(working, "\nGRANTED-TEXT")
        self.assertEqual(broken, "")

    def test_the_clause_never_changes_the_verdict(self):
        """RED-PROOF for the whole integration: `_burst_clause` is only ever
        added to the NOTE half of `admits`'s return, so no arrangement of
        credential readings can flip an admitted/refused decision."""
        import inspect
        src = inspect.getsource(registry.admits)
        self.assertIn("return True, None, why + _burst_clause(colour)", src)
        # and the refusal branch is untouched by it
        refusal = src.split("return True, None, why + _burst_clause")[1]
        self.assertNotIn("_burst_clause", refusal)

    def test_the_module_answers_for_the_native_family_only(self):
        """The pool/per-credential conflation must not be rebuilt one layer
        down: a codex seat's window is not this seat's home."""
        self.assertEqual(burst.FAMILY, burnflags.NATIVE_FAMILY)
        rec = ask([episode()])
        self.assertEqual(rec["family"], burnflags.NATIVE_FAMILY)


class TheVerb(unittest.TestCase):
    def test_a_refusal_exits_one_not_three(self):
        """Exit 3 in this tree means "no snapshot". A measured refusal is an
        ANSWER and must not be read as a missing reading."""
        saved = burst.exemption
        burst.exemption = lambda *a, **k: burst._record(burst.NO_SURPLUS,
                                                        why="none")
        try:
            refused = burnflags._cmd_burst(["--json"])
        finally:
            burst.exemption = saved
        burst.exemption = lambda *a, **k: burst._record(
            burst.GRANTED, why="yes", capacity=4, expires_at=NOW + 60,
            expires_kind="window-reset", window_reset_at=NOW + 60)
        try:
            allowed = burnflags._cmd_burst(["--json"])
        finally:
            burst.exemption = saved
        self.assertEqual(allowed, 0)
        self.assertEqual(refused, 1)

    def test_an_unknown_argument_is_usage(self):
        self.assertEqual(burnflags._cmd_burst(["--wat"]), 2)

    def test_burst_is_a_known_subcommand(self):
        self.assertIn("burn burst", burnflags._USAGE)

    def test_render_prints_every_window_and_the_floor(self):
        lines = "\n".join(burst.render(ask([episode()]), now=NOW))
        self.assertIn("GRANTED", lines)
        self.assertIn("5h", lines)
        self.assertIn("7d-fable", lines)
        self.assertIn("floor", lines)


if __name__ == "__main__":
    unittest.main()
