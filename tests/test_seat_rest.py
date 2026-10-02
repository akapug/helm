#!/usr/bin/env python3
"""task/3280 — RESTING, the owner pause (`helm seat rest`).

THE OWNER'S VOCABULARY. A seat whose turn has ended is IDLE (beacon armed,
any addressed row wakes it), RESTING (beacon deliberately off because the
OWNER paused it, woken by nothing but an explicit resume) or DEAF (beacon
missing by accident, a fault to repair). While the owner had a seat paused,
five readers woke it or gave it work anyway; each arm below plants a rest on
a fixture seat and drives one of them, and each carries its own control: the
same reader on the same fixture with no rest, or with the rest ended or
expired, still acts.

    (a) the stop-guard makes no beacon demand and says RESTING;
    (b) the resume-turn nudge refuses with kind "paused" and types nothing;
    (c) the reviewer bench never names it idle and lists it RESTING;
    (d) the stop-whisper neither offers nor auto-claims for it;
    (e) the beacons census reads RESTING, never DEAF, and repairs nothing.

Hermetic: every arm runs under a temporary HELM_HOME, the pane and process
probes are fixtures or doubles, and no arm reads a live seat or pane.

THE CROSS-FAMILY REVIEW'S SIX (arms f1-f6). A rename must neither drop the
rest nor hand a seat a stale one (f1); a rest recorded while a delivery is
between its pause read and its commit waits for that commit (f2); the
dispatch append (f3) and the stop-whisper's claim (f4) re-read the rest at
their commit; an end and a new rest cannot lose the new one (f5); and a
record that is not one statement of finite times reads UNKNOWN (f6). Each
race is driven through a seam between the check and the commit, and a lock
is observed (tests/_lockwait.py), never timed.
"""
import contextlib
import io
import json
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (beacons, dispatches, idle_dispatch, pk, poolwall,  # noqa: E402
                  proxywatch, resumeturn, reviewer_eligibility as re_, route,
                  seat, seat_rest, seat_usability, seats, seats_delivery,
                  seats_identity, seats_work_offer)
from tests import _lockwait  # noqa: E402
# MODULES, not classes, so none of their arms is collected here again.
import tests.test_beacons as tb  # noqa: E402
import tests.test_beacons_deaf_rearm as tdr  # noqa: E402
import tests.test_dispatch_chain as tdc  # noqa: E402
import tests.test_local_beacon_refusal as tlb  # noqa: E402
import tests.test_reviewer_eligibility as tre  # noqa: E402
import tests.test_route as trt  # noqa: E402
import tests.test_seat_usability as tsu  # noqa: E402
import tests.test_seats as ts  # noqa: E402

def setUpModule():
    """The f3 arm writes dispatch rows, and every row asks the live-seat
    census: it is stood in with a measured empty fleet (tests/_tmphome.py)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()


SEAT = "zz-rest-seat"
SID = "abababab-1111-2222-3333-444444444444"
WORDS = "drain to pause for the time being"
BIG = "9" * 401     # a JSON integer float() cannot hold (review N2)


def rest(seat=SEAT, by=SEAT, role="self", **kw):
    rec, err = seat_rest.mark(seat, WORDS, by, role, **kw)
    assert err is None, err
    return rec


def unreadable(seat=SEAT, text="{not json"):
    """Plant a record helm cannot read, at the path the reader reads."""
    where = seat_rest.path(seat)
    os.makedirs(os.path.dirname(where), exist_ok=True)
    with open(where, "w") as f:
        f.write(text)
    return where


class TheRecordTest(tb.Base):
    """Who, the owner's words, when, and an optional until."""

    def test_a_rest_records_who_the_words_and_when_in_its_own_file(self):
        now = time.time()
        rest(by="seat-int", role="integrator", until=now + 3600, now=now)
        self.assertEqual(os.path.join(self.tmp, "home", "_global", ".state",
                                      "seat-rest", SEAT + ".json"),
                         seat_rest.path(SEAT))
        self.assertEqual(0o600, os.stat(seat_rest.path(SEAT)).st_mode & 0o777)
        rec, err = seat_rest.read(SEAT)
        self.assertIsNone(err)
        self.assertEqual(("seat-int", "integrator", WORDS, now),
                         (rec["by"], rec["role"], rec["because"], rec["at"]))
        line = seat_rest.phrase(rec)
        self.assertTrue(line.startswith("RESTING (owner pause since "), line)
        self.assertIn('"%s"' % WORDS, line)
        self.assertIn("recorded by seat-int", line)
        self.assertIn("until ", line)
        self.assertIn("RESTING", seat_rest.display(SEAT))
        self.assertIn("helm seat rest %s --end" % SEAT,
                      seat_rest.display(SEAT))

    def test_an_ended_or_expired_rest_holds_nothing(self):
        rest()
        self.assertTrue(seat_rest.holds(SEAT), "MUST-HIT: a live rest holds")
        was, err = seat_rest.end(SEAT, "seat-int", "integrator", "resumed")
        self.assertEqual((WORDS, None), (was["because"], err))
        self.assertEqual((None, None), seat_rest.read(SEAT))
        self.assertIsNone(seat_rest.pause(SEAT))
        self.assertEqual("", seat_rest.holds(SEAT))
        ended = json.load(open(seat_rest.path(SEAT)))["ended"]
        self.assertEqual(("seat-int", "resumed"),
                         (ended["by"], ended["because"]))
        self.assertEqual((None, None), seat_rest.end(SEAT, "seat-int",
                                                     "integrator"),
                         "ending an ended rest writes nothing")
        now = time.time()
        rest(until=now - 1, now=now - 100)
        self.assertEqual((None, None), seat_rest.read(SEAT))
        self.assertIsNone(seat_rest.pause(SEAT))

    def test_an_unreadable_record_holds_the_wakers_and_is_never_RESTING(self):
        unreadable()
        rec, why = seat_rest.read(SEAT)
        self.assertIsNone(rec)
        self.assertIn("unreadable", why)
        held = seat_rest.pause(SEAT)
        self.assertEqual("UNKNOWN", held["state"],
                         "an unreadable record must never be called RESTING")
        self.assertIn("UNREADABLE", seat_rest.holds(SEAT))
        self.assertIn("helm seat rest %s --end" % SEAT, seat_rest.holds(SEAT))
        # --end is the repair its sentence names: it rewrites the record.
        was, err = seat_rest.end(SEAT, SEAT, "self")
        self.assertIsNone(err)
        self.assertIsNotNone(was)
        self.assertEqual((None, None), seat_rest.read(SEAT))

    def test_until_takes_a_duration_or_an_instant_in_the_future(self):
        now = 1789000000.0
        self.assertEqual((now + 7200, None), seat_rest.parse_until("2h", now))
        self.assertEqual((now + 86400, None), seat_rest.parse_until("1d", now))
        got, why = seat_rest.parse_until("2020-01-01T00:00:00Z", now)
        self.assertIsNone(got)
        self.assertIn("not in the future", why)
        self.assertEqual((1789003600.0, None), seat_rest.parse_until(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600)),
            now))
        self.assertIsNone(seat_rest.parse_until("soon", now)[0])

    def test_f5_a_rest_recorded_while_an_end_commits_is_not_lost(self):
        """f5 (codex, P2): an end reads the rest, and a NEW rest recorded
        before its write must survive it. The seam is the end's own write,
        and the new rest runs on a second thread started there: it either
        waits for the end (serialized) or lands first and is overwritten."""
        rest(by="seat-int", role="integrator")
        real, seen = seat_rest._write, {}

        def commit(seat_name, rec):
            if rec.get("ended") is not None and "thread" not in seen:
                seen["thread"] = threading.Thread(
                    target=seat_rest.mark,
                    args=(SEAT, "a second pause", "seat-int", "integrator"))
                seen["thread"].start()
                seen["held back on"] = waits.wait_blocked(seen["thread"])
            return real(seat_name, rec)

        with _lockwait.observed() as waits, \
                mock.patch.object(seat_rest, "_write", side_effect=commit):
            was, err = seat_rest.end(SEAT, SEAT, "self", "resumed")
            seen["thread"].join(_lockwait.HANG_S)
        self.assertEqual((WORDS, None), (was["because"], err),
                         "MUST-HIT: the end ended the rest it read")
        live, why = seat_rest.read(SEAT)
        self.assertIsNone(why)
        self.assertIsNotNone(live, "the owner's new rest was lost: the end "
                             "wrote the rest it had read over it")
        self.assertEqual("a second pause", live["because"])
        self.assertIsNotNone(seen["held back on"],
                             "the new rest was not held back by the end")

    def test_f6_a_record_that_is_not_one_statement_reads_UNKNOWN(self):
        """f6 (codex, P2): a duplicate field, or a time that is not a finite
        number, is a record helm cannot read. It holds every waker and is
        never called RESTING; before, one copy of a duplicate won (and could
        end the rest), -Infinity expired it and NaN crashed the reader."""
        now = time.time()
        head = ('"v": 1, "seat": "%s", "by": "%s", "role": "self", '
                '"because": "%s"' % (SEAT, SEAT, WORDS))
        for label, text in (
                ("a duplicate 'ended' whose last copy ends it",
                 '{%s, "at": %r, "until": null, "ended": null, '
                 '"ended": {"by": "x", "at": %r}}' % (head, now, now)),
                ("an 'until' of -Infinity",
                 '{%s, "at": %r, "until": -Infinity, "ended": null}'
                 % (head, now)),
                ("an 'at' of NaN",
                 '{%s, "at": NaN, "until": null, "ended": null}' % head),
                ("an 'until' of 1e400, which parses as infinity",
                 '{%s, "at": %r, "until": 1e400, "ended": null}'
                 % (head, now)),
                ("an 'at' of a 401-digit integer, which no float holds",
                 '{%s, "at": %s, "until": null, "ended": null}'
                 % (head, BIG)),
                ("an 'until' of a 401-digit integer",
                 '{%s, "at": %r, "until": %s, "ended": null}'
                 % (head, now, BIG)),
                ("an 'ended.at' of a 401-digit integer",
                 '{%s, "at": %r, "until": null, "ended": '
                 '{"by": "x", "at": %s}}' % (head, now, BIG)),
                ("a 'v' of true, which equals 1",
                 '{%s, "at": %r, "until": null, "ended": null}'
                 % (head.replace('"v": 1', '"v": true'), now)),
                ("a 'v' of 1.0, which equals 1",
                 '{%s, "at": %r, "until": null, "ended": null}'
                 % (head.replace('"v": 1', '"v": 1.0'), now))):
            with self.subTest(label):
                unreadable(text=text)
                rec, why = seat_rest.read(SEAT)
                self.assertIsNone(rec)
                self.assertTrue(why)
                self.assertEqual("UNKNOWN",
                                 (seat_rest.pause(SEAT) or {}).get("state"))
                self.assertIn("UNREADABLE", seat_rest.holds(SEAT))
                self.assertEqual(beacons.DEAF, beacons._rested(
                    SEAT, beacons.DEAF, "no live beacon")[0])
                self.assertTrue(seat_rest.authority(SEAT)
                                .startswith("unreadable: "))
                # The repair the UNKNOWN line names must not traceback: it
                # replaces the record with an ended one.
                was, err = seat_rest.end(SEAT, SEAT, "self")
                self.assertIsNone(err)
                self.assertIsNone(seat_rest.pause(SEAT))
        # MUST-MISS: the same fields, once each and finite, read RESTING.
        unreadable(text='{%s, "at": %r, "until": null, "ended": null}'
                   % (head, now))
        self.assertEqual(seat_rest.STATE, seat_rest.pause(SEAT)["state"])


class TheVerbTest(tb.Base):
    """`helm seat rest`: the words are required; the seat itself or the
    integrator records them; anyone else is refused."""

    def run_verb(self, args, who=SEAT, err=None):
        actor = types.SimpleNamespace(canonical_name=who)
        out, errs = io.StringIO(), io.StringIO()
        with mock.patch("helm.actors.resolve_actor",
                        return_value=(None if err else actor, err)), \
                mock.patch("helm.seats_integrator.integrator_seat",
                           return_value=("seat-int", None)), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(errs):
            rc = seat.cmd_seat(["rest"] + list(args))
        return rc, out.getvalue(), errs.getvalue()

    def test_a_rest_on_a_name_no_seat_holds_says_so(self):
        """N1, re-read of CURE4, the meta-claude ruling: a rest asked for
        on a name no roster row holds (a name an --alias-hours 0 rename
        released, an actorless seat, a seat not yet joined) is recorded,
        since a rest may wait for the seat that next joins under it, but the
        line never says the seat is paused: it says no seat holds the name
        and names the way to rest a renamed seat."""
        rc, out, err = self.run_verb(["zz-nobody", "--because", WORDS],
                                     who="zz-nobody")
        self.assertEqual(0, rc, err)
        self.assertIn("no seat holds zz-nobody", out)
        self.assertIn("helm seat list", out)
        # CONTROL: a rostered seat's rest reads as before.
        seats.join(session="s-here", seat="zz-here", cwd=self.tmp)
        rc, out, err = self.run_verb(["zz-here", "--because", WORDS],
                                     who="zz-here")
        self.assertEqual(0, rc, err)
        self.assertIn("RESTING (owner pause since", out)
        self.assertNotIn("no seat holds", out)

    def test_a_rest_when_the_roster_cannot_be_read_says_unknown(self):
        """N1, re-read of CURE5: with the roster unreadable, whether any seat
        holds the name is UNKNOWN, so the line never claims a paused seat."""
        with mock.patch.object(seat_rest, "_row_identity",
                               return_value=(None, None)):
            rc, out, err = self.run_verb(["zz-unk", "--because", WORDS],
                                         who="zz-unk")
        self.assertEqual(0, rc, err)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("Nothing wakes it", out)

    def test_the_seat_itself_records_the_owners_words(self):
        rc, out, err = self.run_verb([SEAT, "--because", WORDS,
                                      "--until", "8h"])
        self.assertEqual(0, rc, err)
        self.assertIn("RESTING (owner pause since", out)
        rec, _ = seat_rest.read(SEAT)
        self.assertEqual((SEAT, "self", WORDS), (rec["by"], rec["role"],
                                                 rec["because"]))
        self.assertIsNotNone(rec["until"])

    def test_the_integrator_ends_it_and_is_recorded(self):
        rest()
        self.assertIsNotNone(seat_rest.pause(SEAT), "MUST-HIT: it rests")
        rc, out, err = self.run_verb([SEAT, "--end", "--because",
                                      "the owner resumed it"], who="seat-int")
        self.assertEqual(0, rc, err)
        self.assertIn("ENDED by seat-int (integrator)", out)
        ended = json.load(open(seat_rest.path(SEAT)))["ended"]
        self.assertEqual(("seat-int", "integrator", "the owner resumed it"),
                         (ended["by"], ended["role"], ended["because"]))
        self.assertIsNone(seat_rest.pause(SEAT))

    def test_anyone_else_and_an_unresolved_identity_are_refused(self):  # noqa: VACUOUS_ASSERTION — rc 2 and the refusal text are asserted PRESENT; test_the_seat_itself_records_the_owners_words is the positive control that the same door writes the file
        rc, _out, err = self.run_verb([SEAT, "--because", WORDS],
                                      who="seat-c")
        self.assertEqual(2, rc)
        self.assertIn("may not record", err)
        rc, _out, err = self.run_verb([SEAT, "--because", WORDS],
                                      err="no admissible identity")
        self.assertEqual(2, rc)
        self.assertIn("no admissible identity", err)
        self.assertFalse(os.path.exists(seat_rest.path(SEAT)))

    def test_the_words_are_required_and_a_bad_tail_is_refused(self):  # noqa: VACUOUS_ASSERTION — each refusal's rc 2 and its words are asserted PRESENT; test_the_seat_itself_records_the_owners_words is the positive control that the same door writes the file
        for args, said in (([SEAT], "--because"),
                           ([SEAT, "--because", "  "], "--because"),
                           ([SEAT, "--because", WORDS, "--bogus"], "--bogus"),
                           ([SEAT, "--end", "--until", "1h"], "--end ends"),
                           ([SEAT, "--because", WORDS, "--until", "soon"],
                            "--until"),
                           ([], "usage")):
            with self.subTest(args=args):
                rc, _out, err = self.run_verb(args)
                self.assertEqual(2, rc)
                self.assertIn(said, err)
        self.assertFalse(os.path.exists(seat_rest.path(SEAT)))


class TheSeamTest(tb.Base):
    """The existing delivery pause carries the rest, for a seat with no proxy
    family, and a credential wall beside it stays named."""

    def wall(self):
        """A real PROXY-COOLDOWN record, through poolwall's own composer."""
        now = time.time()
        return poolwall.pause({"family": "family-x", "seat": SEAT,
                               "observed_at": now, "expires_at": now + 600,
                               "model": "model-x", "count": 2}, now)

    def test_a_resting_seats_delivery_pause_is_RESTING_with_who_and_why(self):
        self.assertEqual((None, None), proxywatch.delivery_pause(SEAT),
                         "MUST-MISS: a seat with no rest and no family")
        rest()
        held, err = proxywatch.delivery_pause(SEAT)
        self.assertIsNone(err)
        self.assertEqual((seat_rest.STATE, SEAT, WORDS),
                         (held["state"], held["by"], held["because"]))
        self.assertTrue(held["since"])
        self.assertEqual(seat_rest.STATE,
                         seats_identity._delivery_pause(SEAT)["state"])

    def test_a_wall_and_a_rest_are_one_pause_naming_both(self):
        wall = self.wall()
        with mock.patch.object(proxywatch, "_wall_pause",
                               return_value=(wall, None)):
            rest()
            both, _err = proxywatch.delivery_pause(SEAT)
            self.assertEqual(seat_rest.STATE, both["state"])
            self.assertIn(WORDS, both["reason"])
            self.assertIn(wall["reason"], both["reason"])
            self.assertIs(both["wall"], wall)
            seat_rest.end(SEAT, SEAT, "self")
            alone, _err = proxywatch.delivery_pause(SEAT)
            self.assertIs(alone, wall, "ending the rest left the wall")
        rest()
        with mock.patch.object(proxywatch, "_wall_pause",
                               return_value=(None, None)):
            held, _err = proxywatch.delivery_pause(SEAT)
        self.assertEqual(seat_rest.STATE, held["state"])
        self.assertNotIn("wall", held, "the wall cleared and the rest stayed")

    def test_a_wall_that_raises_cannot_lift_a_rest(self):
        with mock.patch.object(proxywatch, "_wall_pause",
                               side_effect=RuntimeError("observer")):
            with self.assertRaises(RuntimeError):
                proxywatch.delivery_pause(SEAT)
            rest()
            held, _err = proxywatch.delivery_pause(SEAT)
        self.assertEqual(seat_rest.STATE, held["state"])


class TheDeliveryOrderTest(tb.Base):
    """f2 (codex, P1): a rest and a delivery are ordered. A delivery reads
    its pause and commits under one lock; a rest recorded between the two
    must wait for that commit, so no wake commits after `helm seat rest`
    has said the seat rests."""

    def test_f2_a_rest_recorded_mid_delivery_waits_for_its_commit(self):
        seen = {}

        def commit(**_kw):
            """The delivery's commit, after its pause read found none."""
            seen["thread"] = threading.Thread(target=rest)
            seen["thread"].start()
            seen["held back on"] = waits.wait_blocked(seen["thread"])
            seen["rest before the commit"] = seat_rest.read(SEAT)[0]
            return None

        with _lockwait.observed() as waits, \
                mock.patch.object(seats_delivery, "_deliver_unpaused",
                                  side_effect=commit):
            seats_delivery.deliver(seat=SEAT)
            seen["thread"].join(_lockwait.HANG_S)
        self.assertIn("held back on", seen,
                      "MUST-HIT: the unrested delivery reached its commit")
        self.assertIsNone(seen["rest before the commit"],
                          "the rest was recorded, and `helm seat rest` said "
                          "so, before a delivery that read no pause had "
                          "committed its wake")
        self.assertIsNotNone(seen["held back on"],
                             "the rest did not wait for the delivery's lock")
        self.assertEqual(seat_rest.STATE, seat_rest.pause(SEAT)["state"])
        with mock.patch.object(seats_delivery, "_deliver_unpaused") as later:
            seats_delivery.deliver(seat=SEAT)
        later.assert_not_called()


class TheNudgeTest(tb.Base):
    """(b) resume-turn: the pane nudge refuses a resting seat with kind
    "paused" and types nothing, through the real pause seam."""

    def wake(self):
        spawned = []
        with mock.patch.object(resumeturn, "_registered",
                               return_value=("", [4242])), \
                mock.patch.object(resumeturn, "_still_owed",
                                  return_value=(900.0, "")), \
                mock.patch.object(resumeturn, "spawn_child",
                                  side_effect=spawned.append):
            got = resumeturn.wake_undelivered(SEAT, SID, waited=900)
        return got, spawned

    def text_file(self):
        """Where the nudge writes the text it is about to type."""
        return os.path.join(self.tmp, "home", "_global", ".state", "resume",
                            pk.slug(resumeturn._repair_key(SEAT, SID))
                            + ".txt")

    def test_b_a_resting_seat_is_refused_as_paused_and_nothing_is_typed(self):  # noqa: VACUOUS_ASSERTION — the same method ends the rest and asserts the same door forks exactly once, the positive control on the spawn observable
        rest()
        got, spawned = self.wake()
        self.assertEqual("paused", got["action"], got)
        self.assertIn("RESTING", got["detail"])
        self.assertEqual([], spawned)
        self.assertFalse(os.path.exists(self.text_file()),
                         "a refused nudge wrote the text it would have typed")
        self.assertEqual(("state RESTING", ""),
                         resumeturn._pause_verdict(SEAT, SID, {}))
        # MUST-MISS: the rest ends, and the same door, same fixture, types.
        seat_rest.end(SEAT, SEAT, "self")
        self.assertEqual(("", ""), resumeturn._pause_verdict(SEAT, SID, {}))
        got, spawned = self.wake()
        self.assertEqual("wake", got["action"], got)
        self.assertEqual(1, len(spawned))

    def test_an_unreadable_rest_holds_the_nudge(self):
        unreadable()
        got, spawned = self.wake()
        self.assertEqual("paused", got["action"], got)
        self.assertEqual([], spawned)
        self.assertNotIn("RESTING", got["detail"])

    def test_the_act_capture_reads_a_rest_set_after_it_as_a_change(self):  # noqa: VACUOUS_ASSERTION — the unrested capture is asserted EQUAL to the wall authority first, so the inequality after the rest is measured against a value that was read
        before = resumeturn._pause_authority(SEAT, SID)
        self.assertEqual(resumeturn._wall_authority(SEAT, SID), before,
                         "a seat that never rested reads as it always did")
        rest()
        self.assertNotEqual(before, resumeturn._pause_authority(SEAT, SID))


class TheBenchTest(tb.Base):
    """(c) the usability join the router, the reviewer bench and the dispatch
    door read refuses a resting seat and names it; idle_readers never lists
    it."""

    def rows(self):
        reg = {s: tsu._att("covered") for s in ("seat-a", "seat-b")}
        return tsu._join([tsu._hrow("seat-a", family="family-x"),
                          tsu._hrow("seat-b", family="family-x")],
                         upstream={"family-x": {"state": "HEALTHY",
                                                "dark": False}},
                         reg=reg, panes=("seat-a", "seat-b"))

    def report(self, rows):
        join = lambda seats=None, **_kw: {s: rows[s] for s in seats or ()}  # noqa: E731
        report, err = re_.eligibility("row-1", seams=tre._seams(join=join))
        self.assertIsNone(err)
        return report

    def test_c_a_resting_seat_is_never_an_idle_reader_and_is_listed(self):
        rows = self.rows()
        self.assertIs(True, rows["seat-a"]["can_take_work"])
        self.assertEqual(["seat-a", "seat-b"], self.report(rows)["idle"],
                         "MUST-HIT: both seats idle before the rest")
        rest("seat-a")
        rows = self.rows()
        row = rows["seat-a"]
        self.assertEqual((False, seat_usability.UNUSABLE, "rest"),
                         (row["can_take_work"], row["verdict"],
                          row["refusal"]))
        self.assertFalse(seat_usability.deaf_only(row))
        self.assertTrue(row["reason"].startswith("RESTING (owner pause"),
                        row["reason"])
        self.assertIs(True, rows["seat-b"]["can_take_work"],
                      "an IDLE seat is untouched")
        report = self.report(rows)
        self.assertEqual(["seat-b"], report["idle"])
        listed = tre._by_seat(report, "seat-a")
        self.assertEqual((re_.EXCLUDED, re_.AWAKE),
                         (listed["state"], listed["conjunct"]))
        self.assertTrue(listed["reason"].startswith("RESTING"),
                        listed["reason"])
        self.assertIn("RESTING (owner pause", "\n".join(re_.render(report)))

    def test_c_an_expired_rest_restores_the_reader(self):
        now = time.time()
        rest("seat-a", until=now - 1, now=now - 100)
        self.assertEqual(["seat-a", "seat-b"], self.report(self.rows())["idle"])

    def test_c_an_unreadable_rest_refuses_and_says_UNKNOWN(self):
        unreadable("seat-a")
        row = self.rows()["seat-a"]
        self.assertIs(False, row["can_take_work"])
        self.assertNotIn("RESTING (owner pause", row["reason"])
        self.assertIn("UNREADABLE", row["reason"])

    def test_the_dispatch_door_refuses_a_resting_recipient_unless_forced(self):
        row = self.rows()["seat-a"]
        self.assertEqual((True, None, None), dispatches._recipient_seat_rung(
            "seat-a", False, joined=(row["verdict"], row["reason"], row,
                                     None)),
                         "MUST-HIT: the unrested seat is admitted")
        rest("seat-a")
        row = self.rows()["seat-a"]
        joined = (row["verdict"], row["reason"], row, None)
        ok, refusal, _warn = dispatches._recipient_seat_rung(
            "seat-a", False, joined=joined)
        self.assertFalse(ok)
        self.assertIn("RESTING (owner pause since", refusal)
        self.assertIn("helm seat rest seat-a --end", refusal)
        self.assertNotIn("repair it", refusal)
        self.assertEqual((True, None, None), dispatches._recipient_seat_rung(
            "seat-a", True, joined=joined))

    def test_the_router_lists_a_resting_seat_it_will_not_pick(self):
        world = trt.World().freshly_read()
        picked = world.ask("verify", frm="fable", project="helm")["answer"][0]
        name = picked["seat"]
        self.assertTrue(name, "MUST-HIT: the router picks a seat unrested")
        rest(name)
        held = seat_rest.pause(name)
        plain = world.join

        def join(seats=None, now=None, **kw):
            out = plain(seats=seats, now=now, **kw)
            if name in out:
                out[name] = dict(out[name], can_take_work=False,
                                 rest={"state": held["state"],
                                       "reason": held["reason"]})
            return out
        report = world.ask("verify", frm="fable", project="helm",
                           seams={"join": join})
        self.assertNotIn(name, [r["seat"] for r in report["answer"]])
        self.assertEqual([name], [r["seat"] for r in report["resting"]])
        self.assertIn("never picked while it rests",
                      "\n".join(route.render(report, now=world.now)))


class TheOfferTest(tb.Base):
    """(d) the stop-whisper's work offer — the only minter of an auto-claim —
    never claims or offers for a resting seat."""

    ROW = ("d1234567", "review lane-x", "helm chat claim dispatch:d1234567",
           True, "review", {"id": "d1234567abcdef00"})

    def offer(self):
        with mock.patch.object(seats_work_offer, "_session_holds_claim",
                               return_value=False), \
                mock.patch.object(seats_work_offer, "_offer_rows",
                                  return_value=[self.ROW]):
            return seats_work_offer._work_offer_candidate(
                SID, SEAT, None, None, [], False)

    def test_d_a_resting_seat_is_never_auto_claimed(self):
        self.assertEqual("autoclaim:d1234567", self.offer()[0],
                         "MUST-HIT: an idle seat auto-claims its own row")
        rest()
        self.assertIsNone(self.offer())
        seat_rest.end(SEAT, SEAT, "self")
        self.assertEqual("autoclaim:d1234567", self.offer()[0],
                         "an ended rest restores the claim")

    def test_d_an_unreadable_rest_claims_nothing(self):  # noqa: VACUOUS_ASSERTION — test_d_a_resting_seat_is_never_auto_claimed asserts the same candidate auto-claims on this fixture with no record
        unreadable()
        self.assertIsNone(self.offer())

    def finalize(self, rest_meanwhile):
        """The winning candidate's finalizer, with the landing-state probe
        (the slow read between the offer and the lease) as the seam."""
        claims = []

        def landing(_row):
            if rest_meanwhile:
                rest()
            return False, "f" * 40

        def claim(*args, **kw):
            claims.append(args)
            return True, "claimed", "lease-1"
        with mock.patch.object(seats_work_offer, "_offer_landing_state",
                               side_effect=landing), \
                mock.patch("helm.actors.AutoClaimCapability",
                           return_value=types.SimpleNamespace(ref="d1234567")), \
                mock.patch.object(seats_work_offer, "claim",
                                  side_effect=claim):
            got = seats_work_offer._finalize_work_offer(
                SID, SEAT, self.ROW, "line", actor=object())
        return got, claims

    def test_f4_a_rest_recorded_before_the_claim_claims_nothing(self):
        """f4 (codex, P1): the offer read no rest, the rest landed while the
        finalizer probed the landing state, and the lease must not be
        taken: the claim re-reads the rest at its commit."""
        self.assertIsNotNone(self.offer(), "MUST-HIT: the offer finds no rest")
        got, claims = self.finalize(rest_meanwhile=False)
        self.assertEqual(("autoclaim:d1234567", 1), (got[0], len(claims)),
                         "MUST-HIT: an unrested finalizer takes the lease")
        got, claims = self.finalize(rest_meanwhile=True)
        self.assertEqual((None, []), (got, claims),
                         "a seat rested before its claim was auto-claimed")


class TheDispatchAppendTest(tdc.ChainBase):
    """f3 (codex, P1): the dispatch door admitted the recipient, the owner's
    rest landed before the ledger append, and the append must not file the
    row: it re-reads the rest under the ledger lock."""

    def add(self, lane, rest_meanwhile, force=False):
        real = dispatches._validate_recipient_usable

        def door(recipient, force_):
            got = real(recipient, force_)
            if rest_meanwhile:
                rest(recipient, by="seat-int", role="integrator")
            return got
        with mock.patch.object(dispatches, "_validate_recipient_usable",
                               side_effect=door):
            return dispatches.add("codex-3", lane, repo=self.repo,
                                  kind="review", notify=False, new_work=True,
                                  _reason=True, ref=self.a, force=force,
                                  task=self.review_task["id"])

    def test_f3_a_rest_recorded_after_the_door_refuses_the_append(self):
        row, why = self.add("lane-open", rest_meanwhile=False)
        self.assertIsNone(why, "MUST-HIT: the unrested recipient is filed")
        seat_rest.end("codex-3", "seat-int", "integrator")
        row, why = self.add("lane-rested", rest_meanwhile=True)
        self.assertIsNone(row, "a row was filed for a recipient the owner "
                          "rested after the door read it")
        self.assertIn("RESTING (owner pause since", why)
        self.assertIn("helm seat rest codex-3 --end", why)
        self.assertEqual(["lane-open"],
                         [r.get("lane") for r in dispatches.rows().values()])
        row, why = self.add("lane-forced", rest_meanwhile=True, force=True)
        self.assertIsNone(why, "force still files it")


class TheIdleDispatchTest(tb.Base):
    """Idle-dispatch never wakes a resting recipient; its sender is told to
    route the row elsewhere, not to tend the pane."""

    def finding(self, **over):
        f = {"id": "d1234567abcdef00", "id8": "d1234567", "lane": "lane-x",
             "sender": "seat-s", "recipient": SEAT, "presence": "quiet",
             "claim": idle_dispatch.CLAIM_NONE, "live_pane": " live pane.",
             "wakeable": False, "wake_route": idle_dispatch.WAKE_NONE,
             "age_min": 30, "overdue": False,
             "rest": idle_dispatch._rest(SEAT)}
        f.update(over)
        return f

    def test_the_sender_is_told_the_recipient_rests(self):
        f = self.finding()
        self.assertEqual(idle_dispatch.ACT_UNWAKEABLE,
                         idle_dispatch._sender_act(f),
                         "MUST-HIT: a live unwakeable pane asks for tending")
        rest()
        f = self.finding()
        self.assertTrue(f["rest"].startswith("RESTING"))
        self.assertEqual(idle_dispatch.ACT_STRANDED,
                         idle_dispatch._sender_act(f))
        text = idle_dispatch._alert_text(f)
        self.assertIn("route the row to another seat", text)
        self.assertNotIn("pane-level tending", text)


class TheCensusTest(tb.Base):
    """(e) `helm beacons --post`: RESTING, never a DEAF SEAT, and nothing is
    typed into it. The fixture is the DEAF re-arm arms' own pass."""

    pass_ = tdr.DeafSeatRearmNudgeTest.pass_
    att = tdr.DeafSeatRearmNudgeTest.att
    seat_row = tdr.DeafSeatRearmNudgeTest.seat_row

    def setUp(self):
        super().setUp()
        self.spawned, self.paused = [], ""

    def test_e_must_miss_a_DEAF_seat_with_no_rest_is_DEAF_and_repaired(self):
        rep, out = self.pass_()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])
        self.assertEqual(1, len(self.spawned), out)
        self.assertEqual([("alpha", "wake")],
                         [e[:2] for e in out.get("rearmed") or ()])

    def test_e_a_resting_seat_reads_RESTING_and_is_never_typed_into(self):  # noqa: VACUOUS_ASSERTION — the RESTING verdict and bucket are asserted PRESENT; test_e_must_miss_a_DEAF_seat_with_no_rest_is_DEAF_and_repaired is the same pass typing once
        rest("alpha")
        rep, out = self.pass_()
        self.assertEqual([beacons.RESTING],
                         [r["verdict"] for r in rep["seats"]])
        self.assertIn(WORDS, rep["seats"][0]["why"])
        self.assertEqual(["alpha"], [r["seat"] for r in rep["resting"]])
        self.assertEqual(([], []), (rep["deaf"], rep["unreachable"]))
        self.assertEqual([], self.spawned)
        self.assertIsNone(out.get("rearmed"))
        att = self.att()
        self.assertEqual((beacons.RESTING, False), (att["state"],
                                                    att["alarm"]))
        self.assertIn("1 RESTING", beacons._summary(rep))
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            beacons._print_census(rep)
        self.assertIn("RESTING SEAT alpha", shown.getvalue())
        self.assertNotIn("DEAF SEAT alpha", shown.getvalue())

    def test_e_an_ended_rest_restores_the_repair(self):
        rest("alpha")
        self.pass_()
        self.assertEqual([], self.spawned)
        seat_rest.end("alpha", "alpha", "self")
        rep, out = self.pass_(fresh=False)
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])
        self.assertEqual(1, len(self.spawned), out)

    def test_e_an_IDLE_seat_is_untouched(self):
        rep, _out = self.pass_(covered=True)
        self.assertEqual([beacons.COVERED],
                         [r["verdict"] for r in rep["seats"]])
        rest("alpha")
        rep, _out = self.pass_(covered=True, fresh=False)
        self.assertEqual([beacons.RESTING],
                         [r["verdict"] for r in rep["seats"]],
                         "an armed beacon on a resting seat wakes nothing")

    def test_e_an_unreadable_rest_is_never_called_RESTING(self):
        unreadable("alpha")
        rep, _out = self.pass_()
        self.assertEqual([beacons.DEAF], [r["verdict"] for r in rep["seats"]])
        self.assertIn("UNREADABLE", rep["seats"][0]["why"])


class TheStopGuardTest(tlb.LocalSeatBase):
    """(a) the stop-guard: a resting seat hears RESTING, never a beacon
    demand. A local-family seat is the strictest case: unrested and with no
    beacon, it is REFUSED."""

    def demands(self, err):
        return [d for d in ("NO ARMED BEACON", "NO PROVEN WAKE PATH",
                            "NO BEACON —", "Monitor(command:") if d in err]

    def test_a_a_resting_seat_hears_RESTING_and_no_beacon_demand(self):
        self.seat_up()
        rc, _out, err = self.guard(obligation=True)
        self.assertEqual(2, rc, err)
        self.assertTrue(self.demands(err), "MUST-HIT: the unrested control "
                        "demands a beacon")
        rest(tlb.SEAT, by=tlb.SEAT)
        rc, _out, err = self.guard(obligation=True)
        self.assertEqual(0, rc, err)
        self.assertEqual([], self.demands(err))
        self.assertIn("RESTING (owner pause since", err)
        self.assertIn(WORDS, err)
        self.assertIn("helm seat rest %s --end" % tlb.SEAT, err)
        # MUST-MISS: an ended rest, and an expired one, restore the demand.
        seat_rest.end(tlb.SEAT, tlb.SEAT, "self")
        rc, _out, err = self.guard(obligation=True)
        self.assertEqual(2, rc, err)
        self.assertTrue(self.demands(err))
        self.assertNotIn("RESTING (owner pause", err)
        now = time.time()
        rest(tlb.SEAT, by=tlb.SEAT, until=now - 1, now=now - 100)
        rc, _out, err = self.guard()
        self.assertEqual(2, rc, err)
        self.assertTrue(self.demands(err))


class TheRenameTest(ts.SeatsBase):
    """f1 (codex, P1): the rest is recorded under the seat's name, so a
    rename must neither drop it (the seat wakes under its new name) nor hand
    the seat a rest left on the name it takes (a stale one)."""

    def test_f1_a_rename_neither_drops_nor_inherits_a_rest(self):
        seats.join(session="s-rest-old", seat="zz-old", cwd=self.tmp)
        rest("zz-old")
        for dry_run in (True, False):
            ok, msg = seats.rename_seat("zz-old", "zz-new", dry_run=dry_run)
            self.assertFalse(ok, "the rename dropped the owner's rest "
                             "(dry_run=%s): %s" % (dry_run, msg))
            self.assertIn("RESTING (owner pause since", msg)
            self.assertIn("helm seat rest zz-old --end", msg)
        self.assertIn("zz-old", seats.roster())
        self.assertEqual(seat_rest.STATE, seat_rest.pause("zz-old")["state"])
        # MUST-MISS: the rest ended, the same rename goes through, and
        # neither name reads resting after it.
        seat_rest.end("zz-old", "zz-old", "self")
        ok, msg = seats.rename_seat("zz-old", "zz-new")
        self.assertTrue(ok, msg)
        self.assertEqual((None, None), (seat_rest.pause("zz-old"),
                                        seat_rest.pause("zz-new")))
        # RE-STALE: a live rest left on the name the seat would take is
        # someone else's pause, never this seat's. (Planted as a file: mark
        # itself refuses a name the rename just moved away, review N1.)
        unreadable("zz-old", text=json.dumps({
            "v": 1, "seat": "zz-old", "by": "seat-int", "role": "integrator",
            "because": WORDS, "at": time.time(), "until": None,
            "ended": None}))
        self.assertEqual(seat_rest.STATE, seat_rest.pause("zz-old")["state"])
        ok, msg = seats.rename_seat("zz-new", "zz-old")
        self.assertFalse(ok, "the renamed seat inherited a stale rest: %s"
                         % msg)
        self.assertIn("helm seat rest zz-old --end", msg)
        self.assertIn("zz-new", seats.roster())

    def test_n1_a_rest_during_a_rename_is_neither_dropped_nor_inherited(self):
        """N1 (review of the cure, P2): the rename refuses a rested name under
        the roster lock and publishes under it, so a rest recorded between the
        two was unseen: on the old name it was dropped (the seat woke as its
        new name), on the new name it was inherited. mark takes the roster
        lock too, so a rest issued inside the window waits for the publish,
        and a rest on the name the rename took away is refused, naming the
        seat's new name, never recorded where no seat will read it."""
        from helm import seats_roster
        real = seats_roster.rename_window
        for name, old, new in (("old", "zz-win-a", "zz-win-b"),
                               ("new", "zz-win-c", "zz-win-d")):
            with self.subTest(name):
                seats.join(session="s-win-" + name, seat=old, cwd=self.tmp)
                target = old if name == "old" else new
                got = {}

                def rest_now():
                    got["result"] = seat_rest.mark(target, WORDS, target,
                                                   "self")

                def window(*a, **kw):
                    t = threading.Thread(target=rest_now)
                    t.start()
                    t.join(0.5)
                    got["waited"] = t.is_alive()
                    got["thread"] = t
                    return real(*a, **kw)

                with mock.patch.object(seats_roster, "rename_window", window):
                    ok, msg = seats.rename_seat(old, new)
                got["thread"].join(10)
                self.assertTrue(ok, msg)
                self.assertTrue(got["waited"], "the %s-name rest was written "
                                "inside the rename's window" % name)
                rec, err = got["result"]
                if name == "old":
                    self.assertIsNone(rec)
                    self.assertIn(new, err or "",
                                  "a rest on the renamed-away name was not "
                                  "refused naming the seat's new name")
                    self.assertIsNone(seat_rest.pause(old),
                                      "the rest was recorded on a name no "
                                      "seat reads (dropped)")
                else:
                    self.assertIsNone(err)
                    self.assertEqual(new, rec["seat"])
        # MUST-MISS: with no rename running, a rest on a rostered seat is
        # recorded as before.
        seats.join(session="s-win-z", seat="zz-win-z", cwd=self.tmp)
        rec, err = seat_rest.mark("zz-win-z", WORDS, "zz-win-z", "self")
        self.assertIsNone(err)
        self.assertEqual(seat_rest.STATE, seat_rest.pause("zz-win-z")["state"])

    def test_n1_a_rest_waiting_on_a_rename_with_no_alias_window_is_refused(self):
        """N1, re-read of CURE2 (P2): `--alias-hours 0` records no rename
        event on the row, so after the publish the roster no longer says the
        old name moved. A rest issued on the old name inside the rename's
        window waited for the publish, then found no row and no alias and was
        written where no seat reads it, while the renamed seat stayed awake.
        A name that was a roster row when the rest was asked for and is gone
        once it holds the lock was moved or removed meanwhile: refused."""
        from helm import seats_roster
        real = seats_roster.rename_window
        seats.join(session="s-za", seat="zz-za-old", cwd=self.tmp)
        got = {}

        def rest_now():
            got["result"] = seat_rest.mark("zz-za-old", WORDS, "zz-za-old",
                                           "self")

        def window(*a, **kw):
            t = threading.Thread(target=rest_now)
            t.start()
            t.join(0.5)
            got["waited"] = t.is_alive()
            got["thread"] = t
            return real(*a, **kw)

        with mock.patch.object(seats_roster, "rename_window", window):
            ok, msg = seats.rename_seat("zz-za-old", "zz-za-new",
                                        alias_hours=0)
        got["thread"].join(10)
        self.assertTrue(ok, msg)
        self.assertTrue(got["waited"], "the rest did not wait for the rename")
        rec, err = got["result"]
        self.assertIsNone(rec, "a rest waiting on a no-alias rename was "
                          "written on the name the rename took away")
        self.assertIn("zz-za-old", err or "")
        self.assertIsNone(seat_rest.pause("zz-za-old"))
        # MUST-MISS: the seat's new name rests as before.
        rec, err = seat_rest.mark("zz-za-new", WORDS, "zz-za-new", "self")
        self.assertIsNone(err)
        self.assertEqual("zz-za-new", rec["seat"])

    def test_n1_a_rest_whose_name_changed_hands_while_it_waited_is_refused(self):
        """N1, re-read of CURE3 (P2): presence alone is not identity. The rest
        reads its name present, a rename with no alias window moves the seat
        away, another seat joins under the old name, then the rest takes the
        lock: the name is present again, so a presence check passes and the
        owner's rest for the renamed seat lands on the newcomer. The row's
        incarnation, which a rename carries and a join mints fresh, tells
        them apart."""
        seats.join(session="s-zb", seat="zz-zb-old", cwd=self.tmp)
        real_named = seat_rest._named

        def named_after_the_name_changes_hands():
            ok, msg = seats.rename_seat("zz-zb-old", "zz-zb-new",
                                        alias_hours=0)
            self.assertTrue(ok, msg)
            seats.join(session="s-zb-2", seat="zz-zb-old", cwd=self.tmp)
            return real_named()

        with mock.patch.object(seat_rest, "_named",
                               named_after_the_name_changes_hands):
            rec, err = seat_rest.mark("zz-zb-old", WORDS, "zz-zb-old", "self")
        self.assertIsNone(rec, "the owner's rest landed on the seat that "
                          "took the old name after the rename")
        self.assertIn("zz-zb-old", err or "")
        self.assertIsNone(seat_rest.pause("zz-zb-old"))
        # MUST-MISS: with nothing moving, the newcomer rests as before.
        rec, err = seat_rest.mark("zz-zb-old", WORDS, "zz-zb-old", "self")
        self.assertIsNone(err)
        self.assertEqual("zz-zb-old", rec["seat"])

    def test_n1_a_rest_after_a_no_alias_rename_names_the_new_seat(self):
        """N1, re-read of CURE3: a rest asked for AFTER a rename with no alias
        window finds no row and no rename event; the actor store still holds
        the old name as an alias of the actor the new name holds, so the rest
        is refused naming the seat's new name."""
        from helm import actors
        actor, err = actors.bind("zz-zc-old")
        self.assertIsNone(err, err)
        seats.join(session="s-zc", seat="zz-zc-old", cwd=self.tmp)
        ok, msg = seats.rename_seat("zz-zc-old", "zz-zc-new", alias_hours=0)
        self.assertTrue(ok, msg)
        self.assertEqual("zz-zc-new",
                         (actors.lookup("zz-zc-old") or {}).get(
                             "canonical_name"),
                         "control: the actor store kept the old name")
        rec, err = seat_rest.mark("zz-zc-old", WORDS, "zz-zc-old", "self")
        self.assertIsNone(rec)
        self.assertIn("zz-zc-new", err or "")
        self.assertIsNone(seat_rest.pause("zz-zc-old"))

    def test_an_unreadable_roster_never_refuses_a_rest_as_moved(self):
        """N1 addendum of CURE3: a roster that cannot be parsed after the rest
        read its row is UNKNOWN, not absent, so it never reads as a rename."""
        from helm.seats_common import roster_path
        seats.join(session="s-zd", seat="zz-zd", cwd=self.tmp)
        real_named = seat_rest._named

        def named_after_corruption():
            with open(roster_path(), "w", encoding="utf-8") as f:
                f.write("{ not json")
            return real_named()

        with mock.patch.object(seat_rest, "_named", named_after_corruption):
            rec, err = seat_rest.mark("zz-zd", WORDS, "zz-zd", "self")
        self.assertIsNone(err, err)
        self.assertEqual("zz-zd", rec["seat"])

    def test_a_case_only_rename_keeps_its_one_record(self):
        """The control: the record's file is the casefolded name, so a
        case-only rename cannot drop it and is not refused."""
        seats.join(session="s-rest-case", seat="zz-case", cwd=self.tmp)
        rest("zz-case")
        ok, msg = seats.rename_seat("zz-case", "ZZ-case")
        self.assertTrue(ok, msg)
        self.assertEqual(seat_rest.STATE, seat_rest.pause("ZZ-case")["state"])


class TheWhereTest(tb.Base):
    """`helm seat where` prints the rest, and --json carries it."""

    INFO = {"provenance": "orca-adopted", "state": "LIVE",
            "evidence": "fixture", "sessions": []}

    def where(self, *args):
        out = io.StringIO()
        with mock.patch("helm.orcaadopt.resolve", return_value=dict(self.INFO)), \
                contextlib.redirect_stdout(out):
            rc = seat._where_adopted(SEAT, list(args), "unknown seat")
        self.assertEqual(0, rc)
        return out.getvalue()

    def test_where_names_the_rest(self):
        self.assertNotIn("rest    :", self.where())
        self.assertIsNone(json.loads(self.where("--json"))["rest"])
        rest()
        self.assertIn("rest    : RESTING (owner pause since", self.where())
        self.assertTrue(json.loads(self.where("--json"))["rest"]
                        .startswith("RESTING"))


if __name__ == "__main__":
    unittest.main()
