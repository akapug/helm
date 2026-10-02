#!/usr/bin/env python3
"""inject by ARRIVAL (trigger design lanes 1 and 2, task/2980 items 2 and 3).

The per-turn budget keys on who began the turn, because the hook cannot see
the phase. Each arm is an owner-visible misfire or a measured class:

  * the pinned contract on a machine wake (a 12-line block on the chief's
    seat) — deferred to the context's next working turn;
  * empty, duplicate and replayed notices, and a dark seat's catch-up — zero
    bytes, a fast path that still writes its 0-byte ledger row;
  * WHO on a machine wake, and cut mid-word ("runs a mul…") — typed turns
    only, once per context, whole clauses;
  * correction-language, owner feedback and coinage — typed turns only;
  * a wake's block over its cap — every long-tail line kept and shortened,
    never a rank cut (keyword rank carries no relevance, E2);
  * the Monitor-expiry re-arm line (the one perfect hit seen live) — kept,
    by the generalized `route:<id>` cell and the legacy `notice:` spelling;
  * timed_out / fast_path / arrival on every ledger row, a soft deadline
    that leaves a timed_out row, and the payload probe's `source`.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_inject import InjectBase  # noqa: E402
from tests import _notices as N  # noqa: E402
from helm import inject, reflex, store  # noqa: E402


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class ArrivalBase(InjectBase):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(inject, "_greeted_today", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def rows(self):
        return list(inject._ledger_rows())

    def jit_ids(self, sections):
        return [l.split(":", 1)[0].split(" ")[-1] for l in sections["jit"]
                if not l.startswith("[helm")]


class ContractByArrivalTest(ArrivalBase):

    def test_a_machine_wake_defers_the_contract_to_the_next_working_turn(self):
        self.plant_pinned("pin-a", "Always state CL% on a load-bearing claim.")
        machine = N.wake("proxy health CHANGED codex probe=healthy",
                         author="proxywatch")
        first = inject.gather(machine, session="s-contract")
        self.assertEqual(first["pinned"], [])
        peer = inject.gather(N.wake("ready for your verdict"),
                             session="s-contract")
        self.assertTrue(any("pin-a" in l for l in peer["pinned"]))
        again = inject.gather(N.wake("one more thing"), session="s-contract")
        self.assertEqual(again["pinned"], [])      # once per context, as ever

    def test_a_monitor_expiry_carries_its_route_and_not_the_contract(self):
        self.plant_pinned("pin-a", "Always state CL% on a load-bearing claim.")
        self.plant_jit("beacon-rule", "Re-arm the beacon on each expiry.",
                       "beacon, route:arrival.monitor-expired")
        got = inject.gather(N.monitor(N.EXPIRED), session="s-exp")
        self.assertEqual(got["pinned"], [])
        self.assertEqual(self.jit_ids(got), ["beacon-rule"])

    def test_the_legacy_notice_cell_still_routes(self):
        self.plant_jit("beacon-rule", "Re-arm the beacon on each expiry.",
                       "beacon, notice:monitor-expired")
        got = inject.gather(N.monitor(N.EXPIRED))
        self.assertEqual(self.jit_ids(got), ["beacon-rule"])

    def test_a_route_cell_is_never_a_word_probe(self):
        self.plant_jit("beacon-rule", "Re-arm the beacon on each expiry.",
                       "beacon, route:arrival.monitor-expired")
        self.assertEqual(self.jit_ids(inject.gather("is the beacon up?")),
                         ["beacon-rule"])                         # control
        self.assertEqual(self.jit_ids(inject.gather(
            "see route:arrival.monitor-expired please")), [])


class ZeroByteArrivalTest(ArrivalBase):

    def setUp(self):
        super().setUp()
        self.plant_pinned("pin-a", "Always state CL% on a load-bearing claim.")
        self.plant_jit("jit-flux", "a fact about the fluxcap", "fluxcap")

    def assert_silent(self, sections):
        self.assertEqual(inject.render(sections), "")

    def test_an_empty_notice_is_zero_bytes_even_first_in_a_context(self):
        self.assert_silent(inject.gather(N.command_done("wait for it"),
                                         session="s-empty"))
        row = self.rows()[-1]
        self.assertIs(row["fast_path"], True)
        self.assertIs(row["silent"], True)
        self.assertEqual(row["arrival"], "empty")
        # the contract was NOT spent: the next working turn carries it
        nxt = inject.gather("typed: look at the fluxcap", session="s-empty")
        self.assertTrue(any("pin-a" in l for l in nxt["pinned"]))

    def test_a_duplicate_handback_is_zero_bytes(self):
        text = N.handback("the fluxcap moved to six")
        first = inject.gather(text, session="s-dup")
        self.assertTrue(any(" jit-flux:" in l for l in first["jit"]))
        self.assert_silent(inject.gather(text, session="s-dup"))
        self.assertEqual(self.rows()[-1]["arrival"], "duplicate")

    def test_a_replayed_wake_is_zero_bytes(self):
        old = N.wake("the fluxcap rewrite is ready",
                     ts=iso(time.time() - 7200))
        self.assert_silent(inject.gather(old, session="s-replay"))
        self.assertEqual(self.rows()[-1]["arrival"], "replay")
        fresh = inject.gather(N.wake("the fluxcap rewrite is ready"),
                              session="s-replay-2")
        self.assertTrue(any(" jit-flux:" in l for l in fresh["jit"]))   # control

    def test_a_row_queued_behind_a_long_turn_still_injects(self):
        """MUST-MISS: stamped before the seat's last turn began, and news."""
        queued = iso(time.time() - 600)
        inject.gather("typed first", session="s-queue")
        got = inject.gather(N.wake("the fluxcap rewrite is ready", ts=queued),
                            session="s-queue")
        self.assertTrue(any(" jit-flux:" in l for l in got["jit"]))
        self.assertEqual(self.rows()[-1]["arrival"], "peer-wake")


class LongTailByArrivalTest(ArrivalBase):

    def test_a_bg_result_carries_no_long_tail(self):
        """task notices: 0 of 55 relevant in the E2 gold."""
        self.plant_jit("jit-flux", "a fact about the fluxcap", "fluxcap")
        self.assertTrue(any(" jit-flux:" in l for l in
                            inject.gather("the fluxcap?")["jit"]))    # control
        self.assertEqual(inject.gather(N.agent_done("tuned the fluxcap"))["jit"],
                         [])
        self.assertEqual(inject.gather(N.monitor("gate: fluxcap 12 OK",
                                                 desc="gate watcher"))["jit"],
                         [])

    def test_a_wake_over_its_cap_keeps_every_line_and_shortens_them(self):  # noqa: VACUOUS_ASSERTION — the four ids are asserted EQUAL to a non-empty list and the row's fired count to 4, which a dropped or empty lane fails
        long = ("This statement is deliberately long so that four of them "
                "cannot fit a peer wake's five hundred byte ceiling whole; "
                "every one must still arrive, shortened, and none dropped "
                "for its rank. ")
        for i in range(4):
            self.plant_jit("wide-rule-%d" % i, long + "Variant %d." % i,
                           "fluxcap")
        got = inject.gather(N.wake("the fluxcap is ready"), session="s-cap")
        self.assertEqual(sorted(self.jit_ids(got)),
                         ["wide-rule-%d" % i for i in range(4)])
        lane = len("\n".join(got["jit"]).encode("utf-8"))
        self.assertLessEqual(lane, 500)
        row = self.rows()[-1]
        self.assertEqual(row["cap"], 500)
        self.assertEqual(len(row["fired"]["jit"]), 4)


class TypedOnlyTest(ArrivalBase):

    def test_correction_language_fires_on_typed_turns_only(self):
        reflex.seed_defaults()
        line = next(e for e in reflex.load_all()
                    if e["id"] == "correction-language")["steer"]
        typed = inject.gather("like i said, use the fab")["reflex"]
        self.assertTrue(any(line[:40] in l for l in typed))         # control
        wake = inject.gather(N.wake("like i said, use the fab"))["reflex"]
        self.assertFalse(any(line[:40] in l for l in wake))

    def test_seeding_gates_an_existing_owner_feedback_reflex(self):
        reflex.write({"id": "owner-feedback-is-triage-not-interrupt",
                      "steer": "fixture triage steer", "signal": "prompt",
                      "pattern": reflex.OWNER_FEEDBACK_PATTERN})
        typed = inject.gather("one more thing: the cap")["reflex"]
        self.assertIn("REFLEX: fixture triage steer", typed)          # control
        reflex.seed_defaults()
        e = next(e for e in reflex.load_all()
                 if e["id"] == "owner-feedback-is-triage-not-interrupt")
        self.assertEqual(e["arrival"], "typed")
        self.assertIn("REFLEX: fixture triage steer",
                      inject.gather("one more thing: the cap")["reflex"])
        self.assertNotIn("REFLEX: fixture triage steer", inject.gather(
            N.wake("one more thing: the cap"))["reflex"])

    def test_an_operator_trigger_is_never_gated(self):  # noqa: VACUOUS_ASSERTION — the file is asserted EQUAL to its non-empty prior bytes and the operator's steer asserted PRESENT on a wake
        """MUST-MISS: seeding touches only a trigger helm knows."""
        reflex.write({"id": "owner-feedback-is-triage-not-interrupt",
                      "steer": "fixture triage steer", "signal": "prompt",
                      "pattern": r"\bmy own words\b"})
        path = reflex.reflex_path("owner-feedback-is-triage-not-interrupt")
        with open(path, encoding="utf-8") as f:
            before = f.read()
        reflex.seed_defaults()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        self.assertIn("REFLEX: fixture triage steer", inject.gather(
            N.wake("in my own words"))["reflex"])

    def test_coinage_counts_typed_turns_only(self):  # noqa: VACUOUS_ASSERTION — the typed call is asserted EQUAL to the non-empty raw text first, on the same mock
        text = 'the "fluxcap drift" came back'
        with mock.patch.object(inject, "_coinage", return_value=None) as c:
            inject.gather(text)
        self.assertEqual(c.call_args[0][0], text)            # control, raw text
        with mock.patch.object(inject, "_coinage", return_value=None) as c:
            inject.gather(N.handback(text))
            inject.gather(N.wake(text))
        self.assertIsNone(c.call_args)


class WhoByArrivalTest(ArrivalBase):

    def plant_profile(self, level, guidance=()):
        from helm import whoami
        p = mock.patch.object(whoami, "load_profile", return_value={
            "technical_level": level, "guidance": list(guidance)})
        p.start()
        self.addCleanup(p.stop)

    def test_who_waits_for_the_first_typed_turn_then_holds(self):
        self.plant_profile("expert operator", ["headline first"])
        wake = inject.gather(N.wake("ready for your verdict"), session="s-who")
        self.assertFalse(any(l.startswith("WHO") for l in wake["pinned"]))
        typed = inject.gather("please rescue him", session="s-who")
        self.assertTrue(any(l.startswith("WHO operator") for l in typed["pinned"]))
        again = inject.gather("and the next thing", session="s-who")
        self.assertFalse(any(l.startswith("WHO") for l in again["pinned"]))

    def test_who_is_whole_clauses_never_a_cut_word(self):
        level = "; ".join(["clause number %d about how the operator works "
                           "with the fleet" % i for i in range(12)])
        self.plant_profile(level, ["headline first: lead with the outcome"])
        lines = [l for l in inject.gather("hello")["pinned"]
                 if l.startswith("WHO")]
        self.assertTrue(lines)
        for l in lines:
            with self.subTest(line=l[:40]):
                self.assertFalse(l.endswith("…"), l)
                self.assertTrue(l.endswith("fleet") or l.endswith("outcome"), l)
        self.assertTrue(any("headline first" in l for l in lines))
        self.assertLessEqual(sum(len(l) for l in lines), inject.WHO_CAP)


    # WHO HAS TWO AUDIENCES (task/4071): the owner's own
    # guidance (decision cards, human-only gates) reaches the seats he talks
    # to; a worker that hears only its lead gets the operator line and the
    # profile's longtail_guidance, never the lead-only rules a brief typed
    # in by its lead would otherwise carry on every typed turn.
    LEAD_ONLY = "a decision for him is a work-queue card plus a push"
    LONGTAIL = "if he talks to you directly, answer short; else agent-to-agent"

    def plant_audiences(self, seat, leads=frozenset({"seat-a"})):
        from helm import teams, whoami
        p = mock.patch.object(whoami, "load_profile", return_value={
            "technical_level": "non-technical operator",
            "guidance": ["headline first", self.LEAD_ONLY],
            "longtail_guidance": [self.LONGTAIL]})
        p.start()
        self.addCleanup(p.stop)
        if seat:
            # InjectBase.tearDown restores the key (it is in ENV_KEYS); a
            # cleanup here would run after it and drop the caller's value
            os.environ["HELM_CHAT_NAME"] = seat
        got = mock.patch.object(
            teams, "settled_role",
            side_effect=lambda name: "lead" if name in leads else "reviewer")
        self.leads = got.start()
        self.addCleanup(got.stop)

    def who(self, sections):
        return [l for l in sections["pinned"] if l.startswith("WHO")]

    def test_a_worker_seat_typed_turn_gets_the_longtail_line_not_the_owner_rules(self):
        self.plant_audiences("seat-b")
        lines = self.who(inject.gather("brief from your lead", session="s-w"))
        self.assertTrue(lines)                                # control
        self.assertTrue(any("non-technical operator" in l for l in lines))
        self.assertTrue(any(self.LONGTAIL in l for l in lines))
        self.assertFalse(any(self.LEAD_ONLY in l for l in lines), lines)
        self.assertFalse(any("headline first" in l for l in lines), lines)

    def test_a_lead_seat_typed_turn_gets_the_full_owner_guidance(self):
        self.plant_audiences("seat-a")
        lines = self.who(inject.gather("what is the fleet doing", session="s-l"))
        self.assertTrue(any(self.LEAD_ONLY in l for l in lines), lines)
        self.assertFalse(any(self.LONGTAIL in l for l in lines), lines)

    def test_an_unnamed_pane_is_the_owners_own_and_reads_no_roster(self):
        self.plant_audiences(None)
        lines = self.who(inject.gather("hello", session="s-u"))
        self.assertTrue(any(self.LEAD_ONLY in l for l in lines), lines)
        self.leads.assert_not_called()
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-b"}):
            # control: a named seat reads the roster
            self.assertTrue(self.who(inject.gather("hello", session="s-u2")))
        self.assertTrue(self.leads.called)

    def test_a_worker_wake_still_carries_no_who(self):
        self.plant_audiences("seat-b")
        wake = inject.gather(N.wake("ready for your verdict"), session="s-ww")
        self.assertEqual(self.who(wake), [])
        typed = inject.gather("now the brief", session="s-ww")
        self.assertTrue(self.who(typed))                      # control

    def test_a_worker_holding_its_line_is_not_re_read_or_re_sent(self):
        self.plant_audiences("seat-b")
        self.assertTrue(self.who(inject.gather("brief one", session="s-h")))
        self.assertTrue(self.leads.called)                    # control
        self.leads.reset_mock()
        again = inject.gather("brief two", session="s-h")
        self.assertEqual(self.who(again), [])
        self.leads.assert_not_called()

    def test_an_unreadable_team_reads_owner_never_a_withheld_digest(self):
        self.plant_audiences("seat-b")
        self.leads.side_effect = RuntimeError("roster unreadable")
        lines = self.who(inject.gather("brief", session="s-x"))
        self.assertTrue(any(self.LEAD_ONLY in l for l in lines), lines)

    def test_an_empty_profile_never_pays_the_role_read(self):  # noqa: VACUOUS_ASSERTION — the same role-read mock is asserted CALLED on the profile-bearing turn first, and the digest non-empty there
        """Door follow-on: a profile-less install never sets seen['who'], so
        a role read there would be paid on every typed turn and thrown away."""
        from helm import whoami
        self.plant_audiences("seat-b")
        self.assertTrue(self.who(inject.gather("brief", session="s-e0")))
        self.assertTrue(self.leads.called)                    # control
        self.leads.reset_mock()
        with mock.patch.object(whoami, "load_profile", return_value={
                "technical_level": "", "guidance": [],
                "longtail_guidance": []}), \
                mock.patch.object(sys.modules["helm.inject._whisper"],
                                  "_who_audience") as aud:
            self.assertEqual(self.who(inject.gather("brief", session="s-e")), [])
        aud.assert_not_called()
        self.leads.assert_not_called()

    def test_both_variants_fit_who_cap(self):
        self.plant_audiences("seat-b")
        self.assertEqual(inject.WHO_AUDIENCES,
                         (inject.WHO_OWNER, inject.WHO_LONGTAIL))
        owner = inject._who_lines(inject.WHO_OWNER)
        longtail = inject._who_lines(inject.WHO_LONGTAIL)
        self.assertNotEqual(owner, longtail)
        self.assertLessEqual(sum(len(l) for l in owner), inject.WHO_CAP)
        self.assertLessEqual(sum(len(l) for l in longtail), inject.WHO_CAP)


class LedgerFieldsTest(ArrivalBase):

    def test_every_row_carries_arrival_timed_out_and_fast_path(self):
        inject.gather("typed words")
        inject.gather(N.wake("ready"))
        inject.gather(N.command_done("wait"))
        for r in self.rows():
            with self.subTest(arrival=r.get("arrival")):
                self.assertIs(r["timed_out"], False)
                self.assertIn(r["fast_path"], (True, False))
                self.assertIn("wall_ms", r)
                self.assertIsInstance(r["moments"], list)
        self.assertEqual([r["arrival"] for r in self.rows()],
                         ["typed", "peer-wake", "empty"])

    def test_the_soft_deadline_leaves_a_timed_out_row(self):  # noqa: VACUOUS_ASSERTION — the row's timed_out IS True, arrival and stage asserted EQUAL, which an absent row fails
        import importlib
        from helm import moments
        _whisper = importlib.import_module("helm.inject._whisper")
        with mock.patch.object(_whisper, "_lanes",
                               side_effect=moments.Deadline()):
            got = inject.gather("typed words", session="s-slow")
        self.assertEqual(inject.render(got), "")
        row = self.rows()[-1]
        self.assertIs(row["timed_out"], True)
        self.assertEqual(row["arrival"], "typed")
        self.assertEqual(row["stage"], "store")

    def test_the_hook_probe_records_source_and_no_text(self):
        payload = {"prompt": "hello there", "session_id": "s-probe",
                   "cwd": self.tmp, "hook_event_name": "UserPromptSubmit",
                   "source": "user", "brand_new_key": 1}
        rc, _out, _err = self.run_inject(["--hook-json"],
                                         stdin_text=json.dumps(payload))
        self.assertEqual(rc, 0)
        row = self.rows()[-1]
        self.assertEqual(row["hook"], {"source": "user",
                                       "extra": ["brand_new_key"]})
        self.assertNotIn("hello there", json.dumps(row))

    def test_the_moment_ledger_names_what_reached_the_seat(self):
        from helm import moments
        self.plant_jit("beacon-rule", "Re-arm the beacon on each expiry.",
                       "route:arrival.monitor-expired")
        inject.gather(N.monitor(N.EXPIRED), session="s-mom")
        inject.gather(N.monitor(N.EXPIRED), session="s-mom")
        rows = [r for r in moments._read(moments.ledger_path())
                if r["route"] == "arrival.monitor-expired"]
        self.assertEqual([r["outcome"] for r in rows],
                         [moments.DELIVERED, moments.IN_CONTEXT])
        self.assertEqual(rows[0]["ids"], ["beacon-rule"])


class NoticeKindRouteTest(ArrivalBase):

    def test_an_undeclared_notice_kind_is_a_moment_not_a_crash(self):
        """promptshape.notice_kinds is the extension point: a new fixed kind
        is the route arrival.<kind> before any ROUTES row names it, and a
        turn carrying it with no declaring entry still injects normally."""
        from helm import promptshape
        self.plant_jit("jit-flux", "a fact about the fluxcap", "fluxcap")
        with mock.patch.object(promptshape, "notice_kinds",
                               lambda _p: frozenset({"brand-new-kind"})):
            got = inject.gather("the fluxcap?", session="s-kind")
        self.assertTrue(any(" jit-flux:" in l for l in got["jit"]))
        self.assertIn("arrival.brand-new-kind", self.rows()[-1]["moments"])


class StoreRouteCellTest(ArrivalBase):

    def test_doctor_names_a_legacy_notice_cell_with_its_rekey(self):
        self.plant_jit("beacon-rule", "Re-arm the beacon on each expiry.",
                       "beacon, notice:monitor-expired")
        rc, out, _err = self.run_store(["doctor"])
        self.assertIn("LEGACY ROUTE CELLS (1)", out)
        self.assertIn('--remove "notice:monitor-expired" --add '
                      '"route:arrival.monitor-expired"', out)

    def run_store(self, args):
        import contextlib
        import io
        from helm.store import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.cmd_store(list(args))
        return rc, out.getvalue(), err.getvalue()


if __name__ == "__main__":
    unittest.main()
