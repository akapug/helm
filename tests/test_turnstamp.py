#!/usr/bin/env python3
"""Helm's Stop-dispatch record: what it writes, and everything it refuses.

WHAT THIS SOURCE IS, because the name changed for a reason. It records that
helm's own Stop dispatch ALLOWED — every helm Stop handler answered, none
vetoed. It is NOT harness acceptance (a foreign plugin Stop hook outside
helm's dispatcher can veto afterwards) and NOT a terminal response, so it is
deliberately absent from `ownership.TERMINAL_RESPONSE_SOURCES` and can never
carry a row to DARK. An arm below pins that absence, because putting it back
would restore the exact defect task/2293 cured.

THE DISPATCH ARMS DRIVE THE REAL `hookrun.run_event`, because placement is
the property: the hook fires on refusals, and one guard's allow is not the
event's. An arm that reconstructed the dispatch loop would assert against a
model of it rather than against the loop that ships.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from tests._tmphome import pin_suite_guard        # noqa: F401
from helm import hookrun, seats, turnstamp

SEAT = "seat-a"
SESSION = "sess-abc"


def _roster(*names, **kw):
    session = kw.get("session", SESSION)
    return {n: {"session": session} for n in names}


def _spec(name, rc, gate=False, status=hookrun.ANSWERED):
    return {"name": name, "args": "x", "event": "Stop", "gate": gate,
            "_rc": rc, "_status": status}


def _fake_run_one(spec, payload, argv=None, outcome=None):
    if outcome is not None:
        outcome["status"] = spec.get("_status")
    return spec["_rc"]


class _Base(unittest.TestCase):

    def setUp(self):
        d = tempfile.mkdtemp(prefix="helm-test-turnstamp-")
        self.addCleanup(shutil.rmtree, d, True)
        self._env = {}
        for k, v in (("HELM_TURNSTAMP_DIR", d), ("HELM_CHAT_NAME", SEAT),
                     # the stop-guard's scratch reaper DELETES real
                     # /tmp/claude-* scratch; off here as SeatsBase does
                     ("HELM_SCRATCH_GC", "0")):
            self._env[k] = os.environ.get(k)
            os.environ[k] = v
        self.addCleanup(self._restore)
        # A ROSTER ON DISK, because `record` reconciles the seat/session
        # binding against it and refuses when it cannot. Arms that drive
        # `record` directly pass `roster_rows=`; the dispatch arms below go
        # through the hook and need the real read to succeed.
        self.roster_path = os.path.join(d, "roster.json")
        with open(self.roster_path, "w", encoding="utf-8") as fh:
            json.dump(_roster(SEAT), fh)
        patch = mock.patch.object(seats, "roster_path",
                                  return_value=self.roster_path)
        patch.start()
        self.addCleanup(patch.stop)

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class StopDispatchStampTest(_Base):

    def setUp(self):
        _Base.setUp(self)
        self._real = hookrun.run_one
        hookrun.run_one = _fake_run_one
        self.addCleanup(setattr, hookrun, "run_one", self._real)

    def _payload(self, reentry=False):
        return json.dumps({"session_id": SESSION,
                           "stop_hook_active": bool(reentry)})

    def _stamped(self):
        row, err = turnstamp.last_allowed(SEAT, session=SESSION)
        self.assertIsNone(err, "the stamp store was unreadable: %s" % err)
        return row is not None

    def _run(self, specs, reentry=False):
        # MUST-HIT CONTROL: these specs really reach the dispatch loop.
        # `event_specs` filters any spec whose `event` key does not match, so
        # a harness that omits it drives an EMPTY loop and every arm below
        # passes while measuring nothing.
        reaching = [s["name"] for s in hookrun.event_specs("Stop", specs=specs)]
        self.assertEqual([s["name"] for s in specs], reaching,
                         "the specs never reached the loop; this arm would be "
                         "vacuous")
        return hookrun.run_event("Stop", payload=self._payload(reentry),
                                 specs=specs)

    def _control_store_records(self):
        """UNCONDITIONAL POSITIVE CONTROL ON THE SAME OBSERVABLE, and every
        absence arm opens with it. `self._stamped()` being False is satisfied
        just as well by a store that cannot record at all — a bad temp dir, a
        seat name yielding no path, a roster the reconciler refuses. This
        proves the store DOES record here, then clears it.
        """
        path, err = turnstamp.record(SEAT, SESSION, roster_rows=_roster(SEAT))
        self.assertTrue(path, "the stamp store cannot record at all: %s" % err)
        self.assertTrue(self._stamped(), "the reader cannot see the writer")
        os.unlink(path)
        self.assertFalse(self._stamped(), "clearing the control left a stamp")

    def test_an_rc2_refusal_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — _control_store_records proves the store records and the reader sees it, immediately before this; the rung cannot credit a control reached through a helper call
        """A refused stop is a turn that is STILL GOING."""
        self._control_store_records()
        rc = self._run([_spec("guard", 2, gate=True)])
        self.assertEqual(2, rc)
        self.assertFalse(self._stamped())

    def test_a_LATER_handler_veto_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — opens with _control_store_records; absence of a record IS the product law when any handler vetoes
        """`hookrun` keeps running handlers after one returns 2, so the first
        guard's allow is not the event's. A record placed inside a single
        guard would fire here and claim a dispatch that was vetoed."""
        self._control_store_records()
        rc = self._run([_spec("guard", 0), _spec("later", 2, gate=True)])
        self.assertEqual(2, rc)
        self.assertFalse(self._stamped())

    def test_an_UNCHECKED_handler_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — opens with _control_store_records; the rc is 0 either way, so the absent record is the only observable that distinguishes fail-open from allow
        """A timed-out or crashed handler returns 0, which is an ABSENCE OF AN
        ANSWER wearing an allow's exit code. Nothing measured the stop, so
        nothing may be claimed about it."""
        self._control_store_records()
        rc = self._run([_spec("guard", 0, status=hookrun.UNCHECKED)])
        self.assertEqual(0, rc, "the fail-open rc is still 0; only the record "
                                "distinguishes it")
        self.assertFalse(self._stamped())

    def test_a_SKIPPED_handler_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — opens with _control_store_records; a dispatch nobody ran is not a dispatch everybody passed
        """SKIPPED is not UNCHECKED and neither is an allow. A handler that
        never ran leaves the same rc 0 as one that passed, and the typed
        outcome is the only thing that tells them apart."""
        self._control_store_records()
        rc = self._run([_spec("guard", 0, status=hookrun.SKIPPED)])
        self.assertEqual(0, rc)
        self.assertFalse(self._stamped())

    def test_a_REENTRANT_stop_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — opens with _control_store_records; a reentrant stop is not a fresh dispatch, so writing nothing is the product law
        """`stop_hook_active` means the harness is re-entering a stop already
        handled — not a fresh dispatch."""
        self._control_store_records()
        rc = self._run([_spec("guard", 0)], reentry=True)
        self.assertEqual(0, rc)
        self.assertFalse(self._stamped())

    def test_a_REGISTRY_that_will_not_read_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — opens with _control_store_records; an event that consulted nobody must not look like an event everybody passed
        """Acquiring the handler table is part of the dispatch. If that read
        raises, NO handler ran while the rc stays 0 — which is a dispatch that
        consulted nobody wearing the exit code of one everybody passed."""
        self._control_store_records()

        def boom(*a, **k):
            raise OSError("settings unreadable")
        with mock.patch.object(hookrun, "event_specs", boom):
            rc = hookrun.run_event("Stop", payload=self._payload())
        self.assertEqual(0, rc, "a registry failure must not become a veto")
        self.assertFalse(self._stamped())

    def test_the_AGGREGATED_allow_writes_exactly_one_session_bound_record(self):
        """Every handler answered and none vetoed: as far as helm can see."""
        rc = self._run([_spec("a", 0), _spec("b", 0)])
        self.assertEqual(0, rc)
        self.assertTrue(self._stamped(), "the aggregated allow wrote nothing")
        row, err = turnstamp.last_allowed(SEAT, session=SESSION)
        self.assertIsNone(err)
        self.assertEqual(SESSION, row["session"])
        self.assertEqual(turnstamp.SOURCE, row["source"])
        # SESSION-BOUND: a seat name outlives the process that answered to it,
        # so an unbound record would let a dead incarnation's last dispatch
        # read as the live one's.
        other, err = turnstamp.last_allowed(SEAT, session="a-different-one")
        self.assertIsNone(err)
        self.assertIsNone(other)


class BindingReconciledAtTheWriterTest(_Base):
    """Item C: the seat and the session arrive from two places that can
    disagree, and a record filed under the wrong seat is another seat's
    liveness wearing this one's name."""

    def test_a_verified_binding_records(self):
        """UNCONDITIONAL POSITIVE CONTROL for the whole class: the refusals
        below are about the binding, not about a writer that never works."""
        path, err = turnstamp.record(SEAT, SESSION, roster_rows=_roster(SEAT))
        self.assertIsNone(err)
        self.assertTrue(path)

    def test_a_session_the_roster_does_not_call_current_is_REFUSED(self):
        """A reused pane still carries the old seat name in its environment.
        Recording then stamps the LIVE seat's file with a dead incarnation's
        dispatch — the reader cannot tell afterwards."""
        rows = _roster(SEAT, session="the-live-one")
        path, err = turnstamp.record(SEAT, "a-stale-session", roster_rows=rows)
        self.assertIsNone(path)
        self.assertIn("different one", err)

    def test_a_name_with_no_roster_row_is_REFUSED(self):
        path, err = turnstamp.record("ghost", SESSION, roster_rows=_roster(SEAT))
        self.assertIsNone(path)
        self.assertIn("no roster row", err)

    def test_a_row_carrying_no_session_is_REFUSED(self):
        path, err = turnstamp.record(SEAT, SESSION, roster_rows={SEAT: {}})
        self.assertIsNone(path)
        self.assertIn("no current session", err)

    def test_an_UNREADABLE_roster_refuses_rather_than_writing_unbound(self):
        """Fail CLOSED. An unverifiable binding is not a binding, and a record
        written on one cannot be distinguished later from a verified one."""
        with mock.patch.object(seats, "roster_path",
                               return_value="/nonexistent/roster.json"):
            path, err = turnstamp.record(SEAT, SESSION)
        self.assertIsNone(path)
        self.assertTrue(err)

    def test_reconcile_returns_the_CANONICAL_key_not_the_spelling_given(self):  # noqa: VACUOUS_ASSERTION — asserts a positive key equality; the assertIsNone is on the error channel of that same reconciled call
        """The file is named from the canonical key, so a case variant must
        not open a second store for one seat."""
        key, session, err = turnstamp.reconcile("Seat-A", SESSION,
                                                roster_rows=_roster(SEAT))
        self.assertIsNone(err)
        self.assertEqual(SEAT, key)
        self.assertEqual(SESSION, session)


class StoreAnswersTest(_Base):

    def test_absence_and_unreadability_do_not_share_a_value(self):
        """"This source has no record" and "this source failed" send a caller
        to different places: the first is UNKNOWN-because-uncovered, the
        second is UNKNOWN-because-broken, and only the second is a fault."""
        row, err = turnstamp.last_allowed("never-stamped-seat")
        self.assertIsNone(row)
        self.assertIsNone(err, "absence was reported as an error")
        path = turnstamp.stamp_path("broken-seat")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        row, err = turnstamp.last_allowed("broken-seat")
        self.assertIsNone(row)
        self.assertTrue(err, "an unreadable stamp was reported as absence")

    def test_a_record_needs_both_a_seat_and_a_session(self):
        rows = _roster(SEAT)
        ok, err = turnstamp.record(SEAT, SESSION, roster_rows=rows)
        self.assertTrue(ok, "the control write failed: %s" % err)
        for seat, session in ((SEAT, None), ("", SESSION), (None, SESSION)):
            path, err = turnstamp.record(seat, session, roster_rows=rows)
            self.assertIsNone(path, "recorded with seat=%r session=%r"
                              % (seat, session))
            self.assertTrue(err)

    def test_case_variants_share_one_store_but_distinct_names_do_not(self):
        rows = _roster("seat-a", "api.a", "api-a")
        path, err = turnstamp.record("seat-a", SESSION, now=100.0,
                                     roster_rows=rows)
        self.assertIsNone(err)
        row, err = turnstamp.last_allowed("seat-a", session=SESSION)
        self.assertIsNone(err)
        self.assertEqual(100.0, row["allowed_at"])
        self.assertEqual(path, turnstamp.stamp_path("Seat-A"))
        row, err = turnstamp.last_allowed("Seat-A", session=SESSION)
        self.assertIsNone(err)
        self.assertEqual(100.0, row["allowed_at"])
        updated, err = turnstamp.record("SEAT-A", SESSION, now=200.0,
                                        roster_rows=rows)
        self.assertIsNone(err)
        self.assertEqual(path, updated)
        row, err = turnstamp.last_allowed("seat-a", session=SESSION)
        self.assertIsNone(err)
        self.assertEqual(200.0, row["allowed_at"])
        # Both names slug to api-a; the shared key's hash must keep them apart.
        dot, err = turnstamp.record("api.a", SESSION, now=300.0,
                                    roster_rows=rows)
        self.assertIsNone(err)
        dash, err = turnstamp.record("api-a", SESSION, now=400.0,
                                     roster_rows=rows)
        self.assertIsNone(err)
        self.assertNotEqual(dot, dash)
        for name, expected in (("API.A", 300.0), ("API-A", 400.0)):
            row, err = turnstamp.last_allowed(name, session=SESSION)
            self.assertIsNone(err)
            self.assertEqual(expected, row["allowed_at"])

    def test_legacy_case_split_files_do_not_override_the_canonical_store(self):
        path, err = turnstamp.record(SEAT, SESSION, now=100.0,
                                     roster_rows=_roster(SEAT))
        self.assertIsNone(err)
        row, err = turnstamp.last_allowed(SEAT, session=SESSION)
        self.assertIsNone(err)
        self.assertEqual(100.0, row["allowed_at"])
        for name in ("seat-a", "Seat-A"):
            legacy = os.path.join(turnstamp.stamp_dir(), name + ".json")
            self.assertNotEqual(path, legacy)
            with open(legacy, "w", encoding="utf-8") as fh:
                json.dump(dict(row, allowed_at=999.0), fh)
        row, err = turnstamp.last_allowed("Seat-A", session=SESSION)
        self.assertIsNone(err)
        self.assertEqual(100.0, row["allowed_at"])
        os.unlink(path)
        row, err = turnstamp.last_allowed("Seat-A", session=SESSION)
        self.assertIsNone(err)
        self.assertIsNone(row, "a legacy spelling became authoritative again")


class SemanticOutcomeReachesTheAggregateTest(_Base):
    """The rc is 0 on three different meanings, and only one is an answer.

    EVERY ARM DRIVES THE REAL PUBLISHER OR THE REAL `run_one`. Setting a
    status on a fake handler would prove the aggregate reads a field, which is
    not the question — the question is whether the places that KNOW still know
    by the time the aggregate asks, across call sites that return a bare int.
    """

    def test_the_real_CRASH_publisher_allows_and_declares_UNCHECKED(self):  # noqa: VACUOUS_ASSERTION — the load-bearing assertions are POSITIVE — rc 0 AND a declared UNCHECKED naming the crash; there is no absence claim
        """A guard that crashed allows — that law does not move — but nothing
        was examined, so it is not a clean bill."""
        from helm import hookoutcome, seats_stop_response
        hookoutcome.begin()
        rc = seats_stop_response.publish_failure(RuntimeError("boom"))
        self.assertEqual(0, rc, "the fail-open rc must not change")
        status, why = hookoutcome.taken()
        self.assertEqual(hookoutcome.UNCHECKED, status)
        self.assertIn("crashed", why)

    def test_the_real_EXPIRY_publisher_allows_and_declares_UNCHECKED(self):  # noqa: VACUOUS_ASSERTION — the load-bearing assertions are POSITIVE — rc 0 AND a declared UNCHECKED from the real publisher
        """An expired ladder ran out of coverage partway: some rungs never
        looked, and the rc cannot say so."""
        from helm import hookoutcome, seats_stop_response
        hookoutcome.begin()
        rc = seats_stop_response.publish_expired([], [], stop_active=True)
        self.assertEqual(0, rc)
        status, _why = hookoutcome.taken()
        self.assertEqual(hookoutcome.UNCHECKED, status)

    def test_the_real_SCOPE_SKIP_declares_SKIPPED_through_cli_main(self):  # noqa: VACUOUS_ASSERTION — asserts a POSITIVE declared SKIPPED through the real cli.main; the rc equality is the fail-open law, not an absence
        """A fleet-scoped hook outside helm's project exits at the scope door
        with 0. The FIXTURE supplies only the scope DECISION; the outcome
        travels through the real cli.main and the real channel."""
        from helm import cli, hookoutcome, hooks
        hookoutcome.begin()
        with mock.patch.object(hooks, "hook_skips_here", return_value=True):
            rc = cli.main(["chat", "seats"])
        self.assertEqual(0, rc)
        status, why = hookoutcome.taken()
        self.assertEqual(hookoutcome.SKIPPED, status)
        self.assertIn("scoped outside", why)

    def test_run_one_carries_a_declared_outcome_over_a_bare_rc0(self):  # noqa: VACUOUS_ASSERTION — opens with an unconditional positive control on the SAME path — a plain 0 reads ANSWERED — so the UNCHECKED below is the declaration and not a reader that never says ANSWERED
        """THE SEAM ITSELF, through the REAL run_one: a handler that returns 0
        while declaring UNCHECKED must not read as ANSWERED."""
        from helm import cli, seats_stop_response

        def crashing_publisher(args):
            return seats_stop_response.publish_failure(ValueError("x"))

        spec = {"name": "guard", "args": "x", "event": "Stop", "gate": False}
        # POSITIVE CONTROL, unconditional and first: a handler that simply
        # returns 0 reads ANSWERED through this same path, so the UNCHECKED
        # below is the declaration and not a reader that never says ANSWERED.
        out = {}
        with mock.patch.object(cli, "main", return_value=0):
            self._real(spec, self._payload(), outcome=out)
        self.assertEqual(hookrun.ANSWERED, out.get("status"))
        out = {}
        with mock.patch.object(cli, "main", crashing_publisher):
            rc = self._real(spec, self._payload(), outcome=out)
        self.assertEqual(0, rc, "fail-open rc preserved")
        self.assertEqual(hookrun.UNCHECKED, out.get("status"))

    def test_a_STALE_declaration_cannot_become_the_next_handlers_outcome(self):  # noqa: VACUOUS_ASSERTION — asserts a POSITIVE (ANSWERED) after seeding a stale declaration; the product law is that the stale value does NOT survive, which is an equality not an absence
        """`run_one` clears the slot BEFORE the handler runs. A leak here
        would be the same failure-that-looks-like-success the channel
        prevents, with the blame on an innocent handler."""
        from helm import cli
        from helm import hookoutcome
        hookoutcome.declare(hookoutcome.UNCHECKED, "left over")
        spec = {"name": "guard", "args": "x", "event": "Stop", "gate": False}
        out = {}
        with mock.patch.object(cli, "main", return_value=0):
            self._real(spec, self._payload(), outcome=out)
        self.assertEqual(hookrun.ANSWERED, out.get("status"))

    def test_a_REGISTRY_family_loss_blocks_the_aggregate_record(self):  # noqa: VACUOUS_ASSERTION — opens with an unconditional control that the aggregate DOES record and asserts it stamped; the rung cannot credit a control reached through the _stamped helper
        """The loader CATCHES a family's import failure and returns the
        survivors, so the dispatch proceeds with handlers that never ran and
        no exception for anyone to catch. An aggregate ignoring that reports
        'every handler answered' about a set that was silently shortened."""
        hookrun.run_one = _fake_run_one
        try:
            specs = [_spec("a", 0)]
            self.assertEqual(0, hookrun.run_event(
                "Stop", payload=self._payload(), specs=specs))
            self.assertTrue(self._stamped(), "the control did not record")
            os.unlink(turnstamp.stamp_path(SEAT))
            hookrun._AUTH_MISSING.append(("record", "HOOK_SPECS",
                                          ImportError("x")))
            try:
                rc = hookrun.run_event("Stop", payload=self._payload(),
                                       specs=specs)
            finally:
                del hookrun._AUTH_MISSING[:]
            self.assertEqual(0, rc, "a registry loss must not become a veto")
            self.assertFalse(self._stamped())
        finally:
            hookrun.run_one = self._real_run_one

    def _isolated_home(self):
        """AN EMPTY HELM HOME FOR THE DURATION OF ONE ARM.

        `_Base` pins the turnstamp dir and the roster because the arms above
        drive FAKE specs and never reach further. These two arms drive the
        REAL Stop dispatch, which walks the whole guard ladder — and the
        whisper rung reads the live task backlog. Measured before this pin:
        rc 2, blocked by "top of backlog is 1: task/1", i.e. the arm was
        reading the operator's real fleet state and would pass or fail on
        what the fleet happened to be doing.
        """
        d = tempfile.mkdtemp(prefix="helm-test-turnstamp-home-")
        self.addCleanup(shutil.rmtree, d, True)
        return mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(d, "helm"),
            "HELM_CHAT_DIR": os.path.join(d, "chat"),
        })

    def test_real_nested_failures_prevent_aggregate_allow_stamp(self):  # noqa: VACUOUS_ASSERTION — unconditional positive controls run FIRST and LAST (the real dispatch DOES stamp), and each fault subcase asserts a POSITIVE declared status reached the aggregate; the rung cannot credit a control reached through the _stamped helper
        """THE WHOLE PATH, NOT THE PUBLISHER: real event_specs, real
        aggregate, real run_one, real cli, real nested catcher.

        The arms above prove each PUBLISHER declares correctly when called.
        That is a different claim from this one, which is that the
        declaration still reaches the aggregate through every real frame
        between them — and the aggregate is the only place the stamp is
        knowable, so a break anywhere in that chain writes an allow stamp for
        a turn nobody examined.

        The fault goes in at the registered handler's IMMEDIATE LEAF
        DEPENDENCY, inside the catcher that already exists to handle it. No
        fake cli, no fake publisher, no fake run_one, no permanently
        registered faulting handler.
        """
        from helm import hooks, projscope, seats_cli, seats_stop_response
        home = self._isolated_home()
        home.start()
        self.addCleanup(home.stop)

        # THE REGISTRY IS SCOPED TO THE FAMILY UNDER TEST, and this is a
        # SUBSET OF THE REAL SPECS rather than a fabricated one — every frame
        # below is still real event_specs, run_one, cli and catcher.
        #
        # The scoping is an ISOLATION SEAM: the full Stop event also runs two
        # cred families whose behaviour is not this arm's subject, and keeping
        # the arm to the family the fault is injected into is what makes its
        # result attributable to that fault.
        #
        # NO CLAIM IS MADE HERE ABOUT HOW ANOTHER FAMILY'S rc WOULD FOLD INTO
        # THE EVENT, and none is needed: `_stronger` swallows every rc but a
        # GATE's 2, so a sibling family returning non-zero does not by itself
        # move `worst`. The scoping earns its place as isolation, not as a
        # workaround for somebody else's exit code.
        specs = [sp for sp in hookrun.event_specs("Stop")
                 if sp.get("name") == "stop-guard"]
        self.assertEqual(1, len(specs),
                         "the real registry no longer declares exactly one "
                         "stop-guard family; this arm's scoping is stale")

        def _dispatch():
            return hookrun.run_event("Stop", payload=self._payload(),
                                     specs=specs)

        # POSITIVE CONTROL, UNCONDITIONAL AND FIRST. Every assertion below is
        # an ABSENCE — no stamp — which a dispatch that never ran would
        # satisfy for the wrong reason. This proves the real path DOES stamp.
        self.assertEqual(0, _dispatch())
        self.assertTrue(self._stamped(),
                        "the real Stop dispatch did not record an allow "
                        "stamp, so every absence below is uninformative")
        os.unlink(turnstamp.stamp_path(SEAT))

        def _leaf_raises(exc):
            def boom(**kw):
                raise exc
            return mock.patch.object(seats_cli, "stop_guard", boom)

        reached = []
        declared = []
        handed = {}

        # A DELEGATING run_one SPY. "no stamp" alone would hold even if the
        # UNCHECKED declaration were LOST in transit — the very thing this arm
        # exists to watch — because an absence proves nothing. This records
        # the status the aggregate actually received, from the ORIGINAL
        # outcome dict run_event passed in. Nothing is fabricated and no gate
        # bit is touched.
        _real_run_one = hookrun.run_one

        def _tap_run_one(spec, payload, argv=None, outcome=None):
            rc_ = _real_run_one(spec, payload, argv=argv, outcome=outcome)
            declared.append((spec.get("name"), (outcome or {}).get("status")))
            return rc_

        tap = mock.patch.object(hookrun, "run_one", _tap_run_one)
        tap.start()
        self.addCleanup(tap.stop)

        def _spy(name):
            """A DELEGATING spy: the real publisher runs and its rc is
            returned. This records that the frame was entered and what the
            real caller handed it; it does not stand in for it."""
            real = getattr(seats_stop_response, name)

            def wrapper(*a, **k):
                reached.append(name)
                handed[name] = (a, k)
                return real(*a, **k)
            return mock.patch.object(seats_stop_response, name, wrapper)

        # (1) CRASH at the leaf, caught by the catcher that wraps it.
        # `declared` is cleared before EVERY fault dispatch and compared
        # WHOLE. Comparing a tail would let a subcase pass on the PREVIOUS
        # one's identical pair — crash and expiry both declare
        # ("stop-guard", UNCHECKED), so a dispatch that appended nothing at
        # all would still match.
        del declared[:]
        with _leaf_raises(RuntimeError("boom")), _spy("publish_failure"):
            rc = _dispatch()
        self.assertEqual(0, rc, "a crashed guard must still fail OPEN")
        self.assertIn("publish_failure", reached,
                      "the fault never reached the real publisher, so this "
                      "arm measured something else")
        self.assertFalse(self._stamped(),
                         "a crashed guard examined nothing and must not "
                         "leave an allow stamp behind")
        self.assertEqual([("stop-guard", hookrun.UNCHECKED)], declared,
                         "the crash did not reach the aggregate as UNCHECKED")

        # (2) EXPIRY at the same leaf, through the real Expired path.
        del reached[:]
        del declared[:]
        with _leaf_raises(projscope.Expired("ladder incomplete")), \
                _spy("publish_expired"):
            rc = _dispatch()
        # rc 0, AND THE REASON IS MEASURED AT THE PRODUCER, not assumed. The
        # fault raises at the initial stop_guard leaf, before any rung could
        # measure a block, so the budget carries NO finding; the one line
        # `State.expire` adds is the COVERAGE UNKNOWN, and since ruling
        # task/2300 (an unexamined rung is UNKNOWN, and UNKNOWN never refuses
        # a stop) that line is a WARN. An earlier revision asserted rc 2 here
        # because the same line was then filed as a block. Nothing is lost:
        # `publish_expired` still returns 2 when the budget carries a MEASURED
        # block and stop_active is false (a reentrant stop returns 0 either
        # way), and the two assertions on `handed` below prove this
        # dispatch had none to lose and reported the absence out loud.
        #
        # THE LOAD-BEARING HALF IS UNCHANGED: whatever the rc, the aggregate
        # must not stamp, because rungs went unexamined.
        self.assertEqual(0, rc,
                         "an expired ladder with no measured block fails "
                         "OPEN; rc 2 here means an absence refused a stop, "
                         "which ruling task/2300 forbids")
        self.assertIn("publish_expired", reached)
        blocks_handed, warns_handed = handed["publish_expired"][0][:2]
        self.assertEqual([], list(blocks_handed),
                         "the leaf raised before any rung measured, so a "
                         "block here was invented, not lost")
        self.assertTrue(any("COVERAGE UNKNOWN" in w for w in warns_handed),
                        "the expiry reached the publisher without its "
                        "coverage line; the absence went quiet")
        self.assertFalse(self._stamped(),
                         "an expired ladder left rungs unexamined")
        # THE LOAD-BEARING ASSERTION FOR THIS SUBCASE. The no-stamp absence
        # above would hold even if the declaration were lost; only this
        # positive proves the aggregate heard UNCHECKED.
        self.assertEqual([("stop-guard", hookrun.UNCHECKED)], declared,
                         "expiry did not reach the aggregate as UNCHECKED")

        # (3) SCOPE-SKIP: the hook is scoped outside this project, so cli
        # exits at the scope door declaring SKIPPED. Only the scope DECISION
        # is supplied; the outcome travels the real cli.main.
        del declared[:]
        with mock.patch.object(hooks, "hook_skips_here", return_value=True):
            rc = _dispatch()
        self.assertEqual(0, rc)
        self.assertFalse(self._stamped(),
                         "a hook that skipped this project examined nothing")
        self.assertEqual([("stop-guard", hookrun.SKIPPED)], declared,
                         "the scope skip did not reach the aggregate as "
                         "SKIPPED")

        # AND THE CONTROL AGAIN, LAST: the three absences above must not be a
        # fixture that stopped stamping partway through the arm.
        self.assertEqual(0, _dispatch())
        self.assertTrue(self._stamped(),
                        "the path stopped stamping for an unrelated reason")

    def test_caught_registry_import_loss_prevents_aggregate_allow_stamp(self):  # noqa: VACUOUS_ASSERTION — opens with an unconditional full-registry positive control that DOES stamp, and asserts _AUTH_MISSING is non-empty before any absence; the rung cannot credit a control reached through the _stamped helper
        """THE LOSS IS PRODUCED, NOT ANNOUNCED BY THE ARM.

        The loader CATCHES a family's import failure and returns the
        survivors, so there is no exception for anyone to catch and the
        dispatch proceeds with handlers that never ran. An arm that appends
        to `_AUTH_MISSING` by hand proves the aggregate reads a list; this one
        makes the REAL loader populate it from a REAL failed import and lets
        the REAL aggregate decide.
        """
        import builtins
        home = self._isolated_home()
        home.start()
        self.addCleanup(home.stop)

        # POSITIVE CONTROL FIRST, whole registry intact.
        self.assertEqual(0, hookrun.run_event("Stop", payload=self._payload()))
        self.assertTrue(self._stamped(), "the full-registry control did not "
                                         "record")
        os.unlink(turnstamp.stamp_path(SEAT))
        self.assertEqual([], list(hookrun._AUTH_MISSING),
                         "the registry was already short before the fault")

        real_import = builtins.__import__
        # A REAL IMPORT AUTHORITY, even with its per-turn handlers retired.
        # The loader still imports cred._common.GUARD_SPECS: an empty tuple is
        # a known absence, a failed import is not. Poisoning a module the
        # loader never imports would leave _AUTH_MISSING empty and prove nothing.
        target = "helm.cred._common"

        def poisoned(name, *a, **k):
            # ONE NAMED FAMILY FAILS; every other import is delegated
            # normally, so the loader really does return survivors rather
            # than collapsing.
            if name == target:
                raise ImportError("forced: simulating a broken module")
            return real_import(name, *a, **k)

        err = io.StringIO()
        real_err = sys.stderr
        builtins.__import__ = poisoned
        sys.stderr = err
        try:
            rc = hookrun.run_event("Stop", payload=self._payload())
            missing = list(hookrun._AUTH_MISSING)
        finally:
            builtins.__import__ = real_import
            sys.stderr = real_err
            del hookrun._AUTH_MISSING[:]

        self.assertTrue(missing,
                        "the real loader did not record the import loss, so "
                        "the aggregate was never asked the question")
        self.assertEqual(0, rc, "a registry loss must not become a veto")
        self.assertFalse(self._stamped(),
                         "the aggregate stamped 'every handler answered' "
                         "about a set the loader had silently shortened")

    def _payload(self):
        return json.dumps({"session_id": SESSION, "stop_hook_active": False})

    def _stamped(self):
        row, err = turnstamp.last_allowed(SEAT, session=SESSION)
        self.assertIsNone(err)
        return row is not None

    def setUp(self):
        _Base.setUp(self)
        self._real = hookrun.run_one
        self._real_run_one = hookrun.run_one


class ThisSourceCannotRevertARowTest(_Base):
    """The arm that guards the ruling itself."""

    def test_the_stop_record_is_NOT_an_allowlisted_terminal_response(self):
        """THE ALLOWLIST IS THE MECHANISM AND THIS NAME MUST STAY OUT OF IT.
        helm's aggregated allow is not harness acceptance, because a foreign
        plugin Stop hook outside helm's dispatcher can veto after every helm
        handler allows. If this name appears in the allowlist, a seat whose
        harness refused every stop reads as responsive and its rows become
        revertible."""
        from helm import ownership
        # POSITIVE CONTROL on the same set, unconditional and first: the
        # allowlist is populated, so the absence below is this source being
        # excluded rather than an empty frozenset passing everything.
        self.assertTrue(ownership.TERMINAL_RESPONSE_SOURCES)
        self.assertNotIn(turnstamp.SOURCE,
                         ownership.TERMINAL_RESPONSE_SOURCES)

    def test_evidence_from_this_source_can_HOLD_but_never_reverts(self):
        """Drive the predicate, not the constant: the allowlist is the
        mechanism, and what matters is the verdict it produces."""
        from helm import ownership
        rows = {SEAT: {}}
        fresh = ownership.TurnEvidence(ts=1000.0, source=turnstamp.SOURCE,
                                       proves_terminal_response=True)
        self.assertFalse(fresh.proves_terminal_response,
                         "the claim was taken on trust from the caller")
        verdict, _why = ownership.owner_state(
            SEAT, rows, lambda s, r: fresh, lambda s: False, now=1060.0)
        self.assertEqual(ownership.HOLDING, verdict)
        stale = ownership.TurnEvidence(ts=1000.0, source=turnstamp.SOURCE)
        verdict, why = ownership.owner_state(
            SEAT, rows, lambda s, r: stale, lambda s: False,
            now=1000.0 + 99 * 3600)
        self.assertEqual(ownership.UNKNOWN, verdict,
                         "a Stop-dispatch record reverted a row")
        self.assertIn("not a TERMINAL RESPONSE", why)


if __name__ == "__main__":
    unittest.main()
