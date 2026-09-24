#!/usr/bin/env python3
"""A stand-down is a STATE the offer rung can consult (task/406).

THE DEFECT THIS MEASURES. A stand-down existed only as chat prose and as a
sentence inside a row's `note`, so no guard could consult it. The work-offer
rung asks "is this seat idle with no claim" — correct, and silent on whether
the ROW should be handed to anybody. Measured on task/213: an owner work order
paused it, five seats read it and wrote a decline rationale, and the rung
offered it a sixth time — including once to the seat whose own pass comment
said it was being recorded "so the next whisper does not re-route it blind".

Each arm names the wrong implementation it kills, so a green run reads as a
set of refuted edits rather than a set of executed lines. The two that matter
most are the fail-CLOSED arm (an unreadable stand-down must block, because
reading it as absent resumes the leak invisibly) and the NOT-A-STATUS arm (a
stood-down row must stay in open_rows, or the cure hides the backlog it was
protecting).
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import eventledger  # noqa: E402
from helm import tasks  # noqa: E402


def _row(tid="task/900", owner="", **extra):
    row = {"id": tid, "title": "a paused thing", "status": "open",
           "owner": owner}
    row.update(extra)
    return row


def _live(reason="owner work order", **extra):
    sd = {"reason": reason, "ts": time.time(), "by": "seat-a"}
    sd.update(extra)
    return sd


def _offer_ids(rows):
    """offer_rows' ids for a hand-built population, with the ledger read
    stubbed. The subject is the FILTER; routing through a real ledger file
    would measure the writer as well and blur which one failed."""
    real = tasks.open_rows
    tasks.open_rows = lambda path=None: list(rows)
    try:
        return {r[0] for r in tasks.offer_rows(seat="seat-a",
                                               claimed=(), live=set())}
    finally:
        tasks.open_rows = real


class StandDownState(unittest.TestCase):

    def test_absent_is_none_and_does_not_block(self):
        """Kills: treating any missing value as a stand-down, which would
        silence the whole backlog at once.

        ABSENCE IS EXACTLY TWO SHAPES: a missing key, and the explicit None
        that --clear writes. Every other falsy shape is asserted UNREADABLE in
        test_empty_shapes_are_unreadable_not_absent, because the writer
        refuses them -- a reader that accepted them as absence would be more
        permissive than its own write door, and a value that cannot be written
        legitimately would silently re-open the offer."""
        # POSITIVE CONTROL, unconditional and on the SAME observable: if this
        # predicate answered False for everything, every assertion below would
        # hold and the arm would prove nothing.
        self.assertTrue(tasks.standdown_blocks_offer(_row(standdown=_live())))
        for row in (_row(), _row(standdown=None)):
            self.assertIsNone(tasks.standdown_of(row)[0], repr(row))
            self.assertFalse(tasks.standdown_blocks_offer(row), repr(row))

    def test_live_blocks(self):
        """Kills: the no-op filter — a guard that reads the field and offers
        the row anyway."""
        state, detail = tasks.standdown_of(_row(standdown=_live()))
        self.assertEqual(state, tasks.STANDDOWN_LIVE)
        self.assertEqual(detail["reason"], "owner work order")
        self.assertTrue(tasks.standdown_blocks_offer(_row(standdown=_live())))

    def test_expired_lifts_itself(self):
        """Kills: a stand-down that only ever clears by hand. Without self-
        lifting, every time-bounded pause becomes permanent by neglect."""
        past = _live(until=time.time() - 60)
        self.assertEqual(tasks.standdown_of(_row(standdown=past))[0],
                         tasks.STANDDOWN_EXPIRED)
        self.assertFalse(tasks.standdown_blocks_offer(_row(standdown=past)))

    def test_future_until_still_blocks(self):
        """Kills: an expiry comparison with the inequality backwards — which
        would pass the arm above while lifting every live stand-down."""
        soon = _live(until=time.time() + 3600)
        self.assertEqual(tasks.standdown_of(_row(standdown=soon))[0],
                         tasks.STANDDOWN_LIVE)
        self.assertTrue(tasks.standdown_blocks_offer(_row(standdown=soon)))

    def test_unreadable_fails_closed(self):
        """THE SAFETY ARM. Kills: swallowing a malformed value and returning
        None, which reads a broken stand-down as no stand-down and resumes the
        leak silently. A reason-less mapping and an unparseable `until` are
        both somebody's stand-down that did not survive."""
        # POSITIVE CONTROL: a WELL-FORMED stand-down must NOT read as
        # unreadable, or "everything is unreadable" would satisfy the loop.
        self.assertEqual(tasks.standdown_of(_row(standdown=_live()))[0],
                         tasks.STANDDOWN_LIVE)
        for bad in ("a bare string", ["not", "a", "mapping"], {"lift": "x"},
                    {"reason": "   "}, {"reason": "ok", "until": "soon"}):
            row = _row(standdown=bad)
            self.assertEqual(tasks.standdown_of(row)[0],
                             tasks.STANDDOWN_UNREADABLE, repr(bad))
            self.assertTrue(tasks.standdown_blocks_offer(row), repr(bad))

    def test_detail_is_returned_for_unreadable(self):
        """Kills: discarding the raw value on the unreadable path — a surface
        that refuses to interpret something must still be able to SHOW it."""
        state, detail = tasks.standdown_of(_row(standdown="garbage"))
        self.assertEqual(state, tasks.STANDDOWN_UNREADABLE)
        self.assertEqual(detail, "garbage")

    def test_now_is_injectable_so_the_arms_do_not_race_the_clock(self):
        """Kills: reading the clock inside the predicate with no seam — the
        expiry arms would then be timing-dependent rather than decided."""
        sd = _live(until=1000.0)
        self.assertEqual(
            tasks.standdown_of(_row(standdown=sd), now=999.0)[0],
            tasks.STANDDOWN_LIVE)
        self.assertEqual(
            tasks.standdown_of(_row(standdown=sd), now=1001.0)[0],
            tasks.STANDDOWN_EXPIRED)


class ParseUntil(unittest.TestCase):

    def test_durations_resolve_to_an_absolute_instant(self):  # noqa: VACUOUS_ASSERTION — the unconditional 1h parse two lines below IS the positive control
        """Kills: storing the duration text. A relative expiry re-anchors on
        every read, so the stand-down never actually expires."""
        # POSITIVE CONTROL: one concrete parse, unconditional, so an empty
        # loop body could never be mistaken for a passing arm.
        one_hour, err = tasks.parse_standdown_until("1h")
        self.assertIsNone(err)
        self.assertGreater(one_hour, time.time() + 3500)
        now = time.time()
        for txt, secs in (("90m", 5400), ("4h", 14400), ("2d", 172800),
                          ("1w", 604800)):
            got, err = tasks.parse_standdown_until(txt)
            self.assertIsNone(err, txt)
            self.assertAlmostEqual(got - now, secs, delta=5, msg=txt)

    def test_date_means_start_of_that_utc_day(self):
        """Kills: end-of-day rounding, which silently buys a stand-down an
        extra 24 hours nobody typed."""
        got, err = tasks.parse_standdown_until("2026-09-11")
        self.assertIsNone(err)
        self.assertEqual(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(got)),
                         "2026-09-11T00:00:00Z")

    def test_garbage_and_empty_are_errors_not_silent_nones(self):  # noqa: VACUOUS_ASSERTION — the unconditional 2d parse below IS the positive control
        """Kills: returning (None, None) for junk, which would store a
        stand-down with no expiry while the operator believes it has one."""
        # POSITIVE CONTROL: a spelling that MUST parse, so "everything errors"
        # cannot satisfy the refusals below.
        good, gerr = tasks.parse_standdown_until("2d")
        self.assertIsNone(gerr)
        self.assertIsNotNone(good)
        for txt in ("", "   ", "tomorrow", "4x", "99", "2026-13-40"):
            got, err = tasks.parse_standdown_until(txt)
            self.assertIsNone(got, repr(txt))
            self.assertTrue(err, repr(txt))


class OfferAndListing(unittest.TestCase):
    """The two doors, and the fact that they must DISAGREE."""

    def test_offer_rows_skips_a_stood_down_row_and_offers_its_control(self):
        """THE MUST-HIT / MUST-MISS PAIR. The control is the same row shape
        with the field removed: if it were not offered either, the assertion
        above it would pass for reasons unrelated to stand-downs."""
        offered = _offer_ids([_row("task/901", standdown=_live()),
                              _row("task/902")])
        self.assertNotIn("901", offered)
        self.assertIn("902", offered,
                      "the control row must be offerable, or this test proves "
                      "nothing about stand-downs")

    def test_expired_row_is_offered_again(self):
        offered = _offer_ids([_row("task/903",
                                   standdown=_live(until=time.time() - 1))])
        self.assertIn("903", offered)

    def test_unreadable_row_is_not_offered(self):
        # The control rides in the SAME population, so an offer list that came
        # back empty for an unrelated reason fails this arm instead of
        # passing it.
        offered = _offer_ids([_row("task/904", standdown={"no": "reason"}),
                              _row("task/9041")])
        self.assertIn("9041", offered)
        self.assertNotIn("904", offered)

    def test_a_stood_down_row_keeps_an_open_status(self):
        """THE NOT-A-STATUS ARM. Kills the tempting cure: a fourth status. A
        stood-down row is WORK OWED — it must stay in the backlog listing, or
        a row nobody is offered AND nobody can see is indistinguishable from a
        closed one."""
        row = _row("task/905", standdown=_live())
        self.assertTrue(tasks.standdown_blocks_offer(row))
        self.assertIn(row["status"], tasks.OPEN_STATUSES,
                      "the field must not change the row's status")

    def test_fmt_marks_a_blocked_row_and_leaves_an_expired_one_alone(self):
        """Kills: enforcing the field invisibly. If the listing renders a
        stood-down row as ordinary, the offer quietly skips it and nobody can
        see the backlog shrinking."""
        blocked = tasks._fmt(_row("task/906", standdown=_live()))
        expired = tasks._fmt(_row("task/907",
                                  standdown=_live(until=time.time() - 1)))
        plain = tasks._fmt(_row("task/908"))
        self.assertTrue(blocked.lstrip().startswith("~"), blocked)
        self.assertFalse(expired.lstrip().startswith("~"), expired)
        self.assertFalse(plain.lstrip().startswith("~"), plain)


class RealRowLifecycle(unittest.TestCase):
    """THE ARMS THAT REACH THE REAL WRITER.

    AN ARM AIMED AT A ROW THAT DOES NOT EXIST CANNOT PROVE THE FIELD IS
    WRITABLE. update() checks a VALUE before it checks EXISTENCE, so bad-value
    arms against a missing id do see value errors -- but the positive control
    beside them gets back "does not exist", which satisfies any assertion that
    merely looks for the absence of a value complaint. Such a control passes
    whether or not `standdown` is an allowed field at all, which is the exact
    vacuity the rest of this file exists to catch.

    So these seed a REAL row in an isolated ledger and assert the round trip:
    the write lands, the REPLAY reads back what was written, a refused write
    appends NOTHING, and the CLI drives writer -> replay -> offer/list/show
    end to end."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-standdown-")
        self.env = {}
        for k, v in (("HELM_HOME", self.tmp),
                     ("HELM_ADOPTED_DIR", os.path.join(self.tmp, "adopted")),
                     ("HELM_CHAT_DIR", os.path.join(self.tmp, "chat")),
                     ("HELM_CHAT_NAME", "seat-a")):
            self.env[k] = os.environ.get(k)
            os.environ[k] = v
        row, err = tasks.add("a row that must not be offered", owner="",
                             tid="task/8801")
        self.assertIsNone(err, err)
        self.tid = row["id"]

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ledger_lines(self):
        try:
            with open(tasks.ledger_path()) as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tasks.cmd_task(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_a_valid_standdown_writes_and_replays(self):
        """THE POSITIVE CONTROL THAT THE OLD CLASS ONLY PRETENDED TO HAVE.
        Kills: the field being silently unwritable. It reads the value back
        out of the STORE rather than trusting update()'s return."""
        sd = _live(until=time.time() + 3600)
        row, err = tasks.update(self.tid, standdown=sd)
        self.assertIsNone(err, err)
        replayed = tasks.get(self.tid)
        state, detail = tasks.standdown_of(replayed)
        self.assertEqual(state, tasks.STANDDOWN_LIVE)
        self.assertEqual(detail["reason"], "owner work order")
        self.assertTrue(tasks.standdown_blocks_offer(replayed))

    def test_a_refused_write_appends_nothing(self):  # noqa: VACUOUS_ASSERTION — the ledger-line count is compared against a real seeded row whose valid write is proven in test_a_valid_standdown_writes_and_replays
        """Kills: validating loudly and appending anyway. The ledger is
        event-sourced, so a bad row that reaches it is permanent -- an error
        return is not evidence the write did not happen."""
        before = self.ledger_lines()
        for bad in ({}, {"reason": "  "}, "prose", ["nope"],
                    {"reason": "ok", "until": "soon"},
                    {"reason": "ok", "until": True},
                    {"reason": "ok", "until": float("nan")},
                    {"reason": "ok", "until": float("inf")},
                    {"reason": "ok", "until": float("-inf")}):
            row, err = tasks.update(self.tid, standdown=bad)
            self.assertIsNotNone(err, repr(bad))
            self.assertNotIn("does not exist", err, repr(bad))
            self.assertIsNone(row, repr(bad))
        self.assertEqual(self.ledger_lines(), before,
                         "a refused stand-down appended to the ledger")
        self.assertIsNone(tasks.standdown_of(tasks.get(self.tid))[0],
                          "a refused write must leave the row untouched")

    def test_bool_and_infinity_never_read_as_expired(self):  # noqa: VACUOUS_ASSERTION — the LIVE/EXPIRED discriminator on the same predicate is proven unconditionally in test_future_until_still_blocks and test_expired_lifts_itself
        """Kills the three that all look numeric: bool is an int so False read
        as epoch 0 (permanently EXPIRED, re-offering the row); -inf compares
        below every clock; NaN compares false against everything AND makes
        gmtime raise on the one surface a human uses to see why."""
        for bad in (False, True, float("-inf"), float("nan")):
            row = _row("task/8802", standdown={"reason": "r", "until": bad})
            self.assertEqual(tasks.standdown_of(row)[0],
                             tasks.STANDDOWN_UNREADABLE, repr(bad))
            self.assertTrue(tasks.standdown_blocks_offer(row), repr(bad))

    def test_empty_shapes_are_unreadable_not_absent(self):  # noqa: VACUOUS_ASSERTION — the two unconditional absence assertions at the end of this method are the positive control on the same predicate
        """THE WRITER/REPLAY SYMMETRY ARM. Kills: a reader more permissive
        than its own write door. {} and "" are refused by the writer, so
        reading them back as ABSENT would mean a value that can never be
        written legitimately silently re-opens the offer."""
        for shape in ({}, "", [], 0):
            row = _row("task/8803", standdown=shape)
            self.assertEqual(tasks.standdown_of(row)[0],
                             tasks.STANDDOWN_UNREADABLE, repr(shape))
            self.assertTrue(tasks.standdown_blocks_offer(row), repr(shape))
        # POSITIVE CONTROL on the same observable: a MISSING key and an
        # explicit None are the only absences.
        self.assertIsNone(tasks.standdown_of(_row("task/8804"))[0])
        self.assertIsNone(
            tasks.standdown_of(_row("task/8805", standdown=None))[0])

    def test_show_renders_every_state_without_raising(self):  # noqa: VACUOUS_ASSERTION — the rendering is asserted positively in test_fmt_marks_a_blocked_row_and_leaves_an_expired_one_alone; this arm asserts only that no state raises
        """Kills: gmtime raising on a persisted value the writer accepted.
        `show` is the surface a human reaches for to find out WHY a row is
        unoffered, so it must survive the states it is meant to explain."""
        for sd in (_live(), _live(until=time.time() + 60),
                   _live(until=time.time() - 60), {"broken": True}, "junk"):
            tasks.update(self.tid, standdown=None)
            row = _row(self.tid, standdown=sd)
            tasks._fmt(row)
            state, _ = tasks.standdown_of(row)
            self.assertIn(state, (None, tasks.STANDDOWN_LIVE,
                                  tasks.STANDDOWN_EXPIRED,
                                  tasks.STANDDOWN_UNREADABLE), repr(sd))

    def test_the_cli_drives_writer_replay_offer_list_and_clear(self):
        """THE END-TO-END ARM. Kills: a library that works and a verb that
        does not reach it. Nothing here is mocked -- the CLI
        writes, the store replays, and the offer and listing are asked about
        the row that actually landed."""
        rc, out, err = self.cli("standdown", "8801", "owner", "work", "order",
                                "--lift", "cleanup done", "--until", "2d")
        self.assertEqual(rc, 0, err)
        self.assertIn("STOOD DOWN", out)

        replayed = tasks.get(self.tid)
        state, detail = tasks.standdown_of(replayed)
        self.assertEqual(state, tasks.STANDDOWN_LIVE)
        self.assertEqual(detail["reason"], "owner work order")
        self.assertEqual(detail["lift"], "cleanup done")

        offered = {r[0] for r in tasks.offer_rows(seat="seat-b", claimed=(),
                                                  live=set())}
        self.assertNotIn("8801", offered, "a stood-down row was offered")
        self.assertTrue(tasks._fmt(replayed).lstrip().startswith("~"))

        rc, out, err = self.cli("show", "8801")
        self.assertEqual(rc, 0, err)
        self.assertIn("standdown", out)
        self.assertIn("owner work order", out)
        self.assertIn("cleanup done", out)

        rc, out, err = self.cli("standdown", "8801", "--clear")
        self.assertEqual(rc, 0, err)
        cleared = tasks.get(self.tid)
        self.assertIsNone(tasks.standdown_of(cleared)[0])
        offered = {r[0] for r in tasks.offer_rows(seat="seat-b", claimed=(),
                                                  live=set())}
        self.assertIn("8801", offered,
                      "a cleared row must be offerable again, or the control "
                      "above proves nothing about the stand-down")

    def test_the_cli_refuses_a_malformed_option_instead_of_filing_it(self):  # noqa: VACUOUS_ASSERTION — the unconditional well-formed CLI call at the end of this method is the positive control on the same verb
        """Kills the silent shapes: a bare --until VANISHED through _take and
        wrote a PERMANENT stand-down while reporting success, and any token
        the parser did not recognise fell into the joined reason tail."""
        before = self.ledger_lines()
        for args in (("standdown", "8801", "paused", "--until"),
                     ("standdown", "8801", "paused", "--lift"),
                     ("standdown", "8801", "paused", "--until", "2d",
                      "--until", "4h"),
                     ("standdown", "8801", "paused", "--lft", "typo"),
                     ("standdown", "8801", "--until", "2026-09-31", "bad day"),
                     ("standdown", "8801", "--until", "2026-09-00", "bad day"),
                     ("standdown", "8801", "--clear", "with a reason")):
            rc, out, err = self.cli(*args)
            self.assertEqual(rc, 2, "%r -> rc %s\n%s" % (args, rc, out))
        self.assertEqual(self.ledger_lines(), before,
                         "a refused CLI stand-down appended to the ledger")
        self.assertIsNone(tasks.standdown_of(tasks.get(self.tid))[0])
        # POSITIVE CONTROL, unconditional: the SAME verb with well-formed
        # options succeeds, so these refusals are about the option shapes and
        # not about the verb being broken.
        rc, out, err = self.cli("standdown", "8801", "paused", "--until", "4h")
        self.assertEqual(rc, 0, err)

    def test_an_unbounded_int_refuses_instead_of_raising(self):
        """A Python int is unbounded and JSON carries integers verbatim, so
        10**400 is a value the predicate must be able to REFUSE. With the
        float() conversion outside the validation boundary the WRITER raised
        OverflowError instead of refusing and the REPLAY raised instead of
        reading UNREADABLE -- a hostile stored value became a traceback on
        whichever surface touched the row first."""
        before = self.ledger_lines()
        # PAST THE RENDERING BOUNDARY, not merely past the float one. The
        # interpreter's decimal conversion limit is 4300 digits: 10**4299 has
        # exactly 4300 and renders, 10**4300 has 4301 and does NOT. An arm
        # that stops at 10**400 tests the float boundary and never reaches the
        # one where the refusal itself can raise.
        for exp in (400, 4299, 4300, 5000):
            row, err = tasks.update(self.tid,
                                    standdown={"reason": "r",
                                               "until": 10 ** exp})
            self.assertIsNotNone(err, exp)
            self.assertIn("representable", err, exp)
            # the diagnostic must DESCRIBE, never render: no digit run of the
            # value itself can appear in it.
            self.assertNotIn("0" * 50, err, exp)
        self.assertEqual(self.ledger_lines(), before)
        # REPLAY of a value that reached the ledger by some other path must
        # classify, not explode.
        self.assertEqual(
            tasks.standdown_of(_row("task/8806",
                                    standdown={"reason": "r",
                                               "until": 10 ** 400}))[0],
            tasks.STANDDOWN_UNREADABLE)
        # POSITIVE CONTROL, unconditional: an ordinary instant still writes.
        row, err = tasks.update(self.tid, standdown=_live(until=time.time() + 60))
        self.assertIsNone(err, err)

    def test_the_rejection_describes_the_value_and_never_renders_it(self):
        """THE CONTRACT'S CENTRAL ARM. A refusal that renders the rejected
        value inherits that value's pathologies: an int past the interpreter's
        decimal limit raises on str/repr, so a guard that formats what it
        refuses throws on exactly the input it exists to reject.

        Kills the tempting near-cure as well as the bug: a CAPPED EXCERPT
        still renders the whole object before slicing it, so ("%r" % v)[:40]
        raises identically. Every branch must derive from structural
        properties instead."""
        huge = 10 ** 5000
        got = tasks._describe_rejected(huge)
        self.assertIn("int", got)
        self.assertIn("5001", got, "an approximate digit count, computed "
                                   "from bit_length rather than rendered")
        self.assertLess(len(got), 200, "the description must be bounded")
        # POSITIVE CONTROL, unconditional: ordinary values describe too, so
        # this is not a function that only handles the pathological case.
        for value, want in ((3.5, "float"), ({"a": 1}, "dict"),
                            ([1, 2], "list"), (True, "bool")):
            self.assertIn(want, tasks._describe_rejected(value))
        # A long string is bounded by SLICING SOMETHING ALREADY A STRING.
        long_s = tasks._describe_rejected("x" * 100000)
        self.assertIn("100000", long_s)
        self.assertLess(len(long_s), 200)

    def test_show_renders_a_hostile_stored_standdown_within_a_bound(self):  # noqa: VACUOUS_ASSERTION — the unconditional control after the loop asserts show PRINTS reason and lift and does NOT say UNREADABLE, so a renderer that refused everything fails here
        """THE ARM THAT REACHES THE RENDERER, WHICH ITS PREDECESSOR DID NOT.
        Kills: restoring a raw %r (or a bare str()) in show's UNREADABLE
        branch. The old arm called standdown_of and _describe_rejected in a
        loop and ran the CLI only on a VALID row, so the hostile values never
        touched the renderer and that regression escaped it -- the same
        can-this-arm-fail defect this class was built to end, one layer in.

        THE VALUE IS PLANTED IN THE LEDGER, NOT WRITTEN THROUGH update, and
        that is the point: the contract now REFUSES it, so the only way it can
        reach a reader is the path the fail-closed design exists for -- a row
        that arrived by some other hand. show must explain that row, not die
        on it.

        REACH IS A PROPERTY OF THIS ARM AND IT IS ASSERTED, NOT ASSUMED. TWO
        CEILINGS stand between a hostile value and this renderer and neither
        is in tasks.py, so a value chosen for how badly it renders can fail to
        ARRIVE and leave the arm passing on a row that holds nothing. A
        5001-digit integer is not ledger-reachable in either direction, since
        json.dumps refuses to write it and int() refuses to parse it at the
        same conversion limit. An event line is capped at MAX_EVENT_BYTES, so
        a 100000-character lift is written and then SKIPPED by the reader,
        folding the row back to no stand-down at all -- ABSENT, which show
        explains without ever reaching the branch under test. That is why the
        state assertion below runs BEFORE the renderer and demands UNREADABLE:
        it is the arm's own proof that its input arrived. The huge-integer
        face is therefore exercised at the API door
        (test_the_contract_refuses_what_show_cannot_render) and these values
        are sized to fit under the cap while still rendering far past what a
        surface should print. MEASURED at da48b9038 with the first of them
        stored: state LIVE, `show` rc 0, 40087 characters printed."""
        for planted in ({"reason": "r", "lift": "x" * 40000},
                        {"reason": "r", "by": {"deep": ["x" * 20000]}},
                        {"reason": "r", "note": ["x" * 3000] * 10},
                        "prose", ["nope"], 7):
            row = dict(tasks.get(self.tid))
            row["standdown"] = planted
            eventledger.append(tasks.ledger_path(), row)
            state, _ = tasks.standdown_of(tasks.get(self.tid))
            self.assertEqual(state, tasks.STANDDOWN_UNREADABLE,
                             tasks._describe_rejected(planted))
            rc, out, err = self.cli("show", "8801")
            self.assertEqual(rc, 0, err)
            # SCOPED TO THE STAND-DOWN LINE, NEVER THE WHOLE SCREEN. show
            # prints many fields and a state vocabulary of its own, so
            # assertIn on the full output is a claim about the surface rather
            # than about the row.
            line = [ln for ln in out.splitlines()
                    if ln.split()[:1] == ["standdown"]]
            self.assertEqual(len(line), 1, out[:400])
            self.assertIn("UNREADABLE", line[0])
            self.assertLess(len(out), 2000,
                            "show must stay bounded on a stored value whose "
                            "rendering is the thing that would run away")
        # POSITIVE CONTROL ON THE SAME RENDERER: a VALID stand-down still
        # prints its substance, so a show that simply refused everything --
        # or one whose standdown line vanished -- fails here.
        rc, out, err = self.cli("standdown", "8801", "real reason",
                                "--lift", "when done")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("show", "8801")
        self.assertEqual(rc, 0, err)
        line = [ln for ln in out.splitlines()
                if ln.split()[:1] == ["standdown"]]
        self.assertEqual(len(line), 1, out[:400])
        self.assertIn("real reason", line[0])
        self.assertIn("when done", line[0])
        self.assertNotIn("UNREADABLE", line[0])

    def test_the_contract_refuses_what_show_cannot_render(self):  # noqa: VACUOUS_ASSERTION — the unconditional control after the loop asserts an ordinary stand-down is ACCEPTED with its lift intact, so a contract that refuses everything fails here
        """THE WHOLE-OBJECT ARM. Kills: validating the fields one happens to
        name. Every value below is admitted by a reason/until allow-list and
        is then something show cannot put on a surface -- an integer whose
        str() raises, a string that renders a screenful, a container that
        renders many screenfuls, a value of a type nothing can render.

        It asserts the DIAGNOSTIC as well as the refusal, because the error
        this replaces was 'task ledger refused the write' -- true about where
        the write stopped and silent about why, since json.dumps hit the same
        conversion limit further down. And it asserts the ledger did not grow:
        an error return is not evidence the append did not happen."""
        before = self.ledger_lines()
        for bad, wanted in (
                ({"reason": "r", "lift": 10 ** 5000}, "lift"),
                ({"reason": "r", "by": 10 ** 5000}, "by"),
                ({"reason": "r", "lift": {"a": 10 ** 5000}}, "lift"),
                ({"reason": "r", "by": "x" * 100000}, "by"),
                ({"reason": "x" * 100000}, "reason"),
                ({"reason": "r", "seen": [["x" * 2000] * 30] * 30}, "seen"),
                ({"reason": "r", "who": object()}, "who"),
                # THE CASE A SUM-OF-PARTS ESTIMATE ADMITS. Nested, these NULs
                # cost 3902 by their own lengths and render as about 15600,
                # because a list reprs its elements. Refused by KIND now, so
                # no estimate has to be right about it.
                ({"reason": "r", "lift": ["\x00" * 3900]}, "lift"),
                ({"reason": "r", "lift": {1, 2}}, "lift"),
                ({"reason": "r", "lift": ("a", "b")}, "lift")):
            payload, err = tasks.standdown_contract(bad)
            # THE ARM OBEYS THE CONTRACT IT TESTS. A bare assertIsNone
            # formats the payload into its own failure message, so the arm
            # that forbids rendering a hostile value renders it -- into a
            # receipt log, where it is somebody else's problem to read.
            self.assertIsNone(payload, tasks._describe_rejected(bad))
            self.assertIn(wanted, err)
            self.assertLess(len(err), 300, "a refusal stays bounded too")
            row, err = tasks.update(self.tid, standdown=bad)
            self.assertIsNotNone(err)
            self.assertIn(wanted, err)
        # A REFUSAL MAY NOT INTERPOLATE WHAT IT IS REFUSING, AND THE FIELD
        # NAME IS PART OF WHAT IT IS REFUSING. A 40000-character key is
        # untrusted text exactly as a value is, so a message that names it
        # reproduces the defect it reports -- and it does so INSIDE the
        # diagnostic, where nothing else is looking.
        huge_key = "k" * 40000
        for bad in ({"reason": "r", huge_key: None},
                    {"reason": "r", huge_key: object()},
                    {"reason": "r", ("a", "b"): "x"},
                    {"reason": "r", 10 ** 5000: "x"}):
            payload, err = tasks.standdown_contract(bad)
            self.assertIsNone(payload, tasks._describe_rejected(bad))
            self.assertLess(len(err), 300, "the refusal is bounded too")
            self.assertNotIn(huge_key, err)
        self.assertEqual(self.ledger_lines(), before)
        # POSITIVE CONTROL: the bound admits an ordinary stand-down, so this
        # arm fails against a contract that refuses everything.
        payload, err = tasks.standdown_contract(
            {"reason": "owner work order", "lift": "when 2118 lands",
             "by": "seat-a"})
        self.assertIsNone(err, err)
        self.assertEqual(payload["lift"], "when 2118 lands")

    def test_the_bound_is_the_renderers_own_width_not_an_estimate(self):
        """THE ESTIMATOR AND THE RENDERER MUST BE ONE GRAMMAR, and the case
        that separates them is a string whose repr is not its str.

        Kills: predicting a container's rendered size by summing its parts.
        NUL characters are the discriminator -- str() of a string is the
        string, so 3000 of them occupy 3000 columns, while the same bytes
        inside a list are repr'd to four characters each and occupy about
        12000. A contract that accepts the container on the strength of the
        sum has bounded nothing, and the arm below would have to have been
        written in the estimator's language to miss it.

        So the scalar is ACCEPTED and its rendered width is asserted to be
        exactly what the contract counted, and the container form of the SAME
        BYTES is refused by kind rather than measured."""
        body = "\x00" * 3000
        row, err = tasks.update(self.tid, standdown={"reason": "r",
                                                     "lift": body})
        self.assertIsNone(err, err)
        rc, out, err = self.cli("show", "8801")
        self.assertEqual(rc, 0, err)
        line = [ln for ln in out.splitlines()
                if ln.split()[:1] == ["standdown"]]
        self.assertEqual(len(line), 1, repr(out[:200]))
        self.assertEqual(line[0].count("\x00"), 3000,
                         "show renders the scalar at the width the contract "
                         "counted, with no escaping in between")
        payload, err = tasks.standdown_contract({"reason": "r",
                                                 "lift": [body]})
        self.assertIsNone(payload, "the container form must be refused")
        self.assertIn("lift", err)
        self.assertLess(len(err), 300)

    def _strict_utf8_cli(self, *args):
        """Exercise actual encoding, not StringIO's acceptance of surrogates."""
        out_bytes, err_bytes = io.BytesIO(), io.BytesIO()
        with io.TextIOWrapper(out_bytes, encoding="utf-8", errors="strict") as out, \
                io.TextIOWrapper(err_bytes, encoding="utf-8", errors="strict") as err:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = tasks.cmd_task(list(args))
            out.flush()
            err.flush()
            return (rc, out_bytes.getvalue().decode("utf-8"),
                    err_bytes.getvalue().decode("utf-8"))

    def _append_ascii_standdown(self, payload):
        """Model an external JSON writer in this test's existing private ledger.

        ensure_ascii=True permits escaped lone surrogates without invalid UTF-8
        bytes. The normal writer refuses these bytes before append, so using it
        here would never exercise replay. Assert exact arrival before classifying.
        """
        row = dict(tasks.get(self.tid))
        self.assertEqual(row["id"], self.tid)
        row["standdown"] = payload
        encoded = (json.dumps(row, ensure_ascii=True, separators=(",", ":"))
                   + "\n").encode("ascii")
        self.assertLessEqual(len(encoded), eventledger.MAX_EVENT_BYTES)
        with open(tasks.ledger_path(), "ab") as ledger:
            ledger.write(encoded)
        replayed = tasks.get(self.tid)
        self.assertEqual(replayed["standdown"], payload)
        return replayed

    def test_utf8_api_refuses_surrogate_scalars_before_append(self):
        """Kills scalar acceptance that only fails in the ledger encoder.

        A real accepted Unicode write is the control; each rejection must be
        a stand-down diagnostic, not the generic downstream ledger refusal.
        """
        valid = {"reason": "révision " + "漢" * 1400, "lift": "après \U0001d11e",
                 "by": "siège"}
        # The signed budget counts characters, not UTF-8 bytes. Encodability
        # must not silently exclude valid multibyte text within that budget.
        self.assertLessEqual(sum(len(key) + len(value) for key, value in valid.items()), 4000)
        self.assertGreater(len("".join(valid.values()).encode("utf-8")), 4000)
        before_lines = self.ledger_lines()
        row, err = tasks.update(self.tid, standdown=valid)
        self.assertIsNone(err, err)
        self.assertEqual(row["standdown"], valid)
        self.assertEqual(self.ledger_lines(), before_lines + 1)
        self.assertEqual(tasks.get(self.tid)["standdown"], valid)
        self.assertEqual(tasks.standdown_of(tasks.get(self.tid))[0], tasks.STANDDOWN_LIVE)
        rc, out, err = self._strict_utf8_cli("show", "8801")
        self.assertEqual(rc, 0, err)
        self.assertIn(valid["reason"], out)
        self.assertIn(valid["lift"], out)
        with open(tasks.ledger_path(), "rb") as ledger:
            before = ledger.read()
        for field in ("reason", "lift", "by", "extra"):
            for surrogate in ("\ud800", "\udfff"):
                with self.subTest(field=field, codepoint=ord(surrogate)):
                    bad = dict(valid, **{field: surrogate})
                    row, err = tasks.update(self.tid, standdown=bad)
                    self.assertIsNone(row)
                    self.assertIsInstance(err, str)
                    with open(tasks.ledger_path(), "rb") as ledger:
                        self.assertEqual(ledger.read(), before)
                    self.assertEqual(tasks.get(self.tid)["standdown"], valid)
                    self.assertNotIn("task ledger refused the write", err)
                    self.assertIn("stand-down", err)
                    self.assertLess(len(err), 300)
                    err.encode("utf-8", errors="strict")
                    payload, why = tasks.standdown_contract(bad)
                    self.assertIsNone(payload)
                    self.assertIsInstance(why, str)
                    why.encode("utf-8", errors="strict")

    def test_utf8_escaped_replay_is_unreadable_and_show_encodes(self):
        """Kills LIVE classification and strict-output crashes after JSON replay.

        Separate subtests keep the strict renderer reachable even when the old
        implementation first fails the UNREADABLE assertion. No surface is
        mocked; only the ledger's external-input bytes are planted.
        """
        valid = {"reason": "révision " + "漢" * 1400, "lift": "après \U0001d11e"}
        # The signed budget counts characters, not UTF-8 bytes. Encodability
        # must not silently exclude valid multibyte text within that budget.
        self.assertLessEqual(sum(len(key) + len(value) for key, value in valid.items()), 4000)
        self.assertGreater(len("".join(valid.values()).encode("utf-8")), 4000)
        replayed = self._append_ascii_standdown(valid)
        self.assertEqual(tasks.standdown_of(replayed)[0], tasks.STANDDOWN_LIVE)
        rc, out, err = self._strict_utf8_cli("show", "8801")
        self.assertEqual(rc, 0, err)
        lines = [line for line in out.splitlines() if line.split()[:1] == ["standdown"]]
        self.assertEqual(len(lines), 1)
        self.assertIn(valid["reason"], lines[0])
        self.assertIn(valid["lift"], lines[0])
        self.assertNotIn("UNREADABLE", lines[0])
        cases = ({"reason": "\ud800"}, {"reason": "r", "lift": "\ud800"},
                 {"reason": "r", "by": "\udfff"},
                 {"reason": "r", "extra": "\udfff"},
                 {"reason": "r", "\ud800": []})
        for index, bad in enumerate(cases):
            replayed = self._append_ascii_standdown(bad)
            with self.subTest(case=index, surface="classification"):
                self.assertEqual(tasks.standdown_of(replayed)[0], tasks.STANDDOWN_UNREADABLE)
                self.assertTrue(tasks.standdown_blocks_offer(replayed))
            with self.subTest(case=index, surface="strict UTF-8 show"):
                rc, out, err = self._strict_utf8_cli("show", "8801")
                self.assertEqual(rc, 0, err)
                self.assertEqual(err, "")
                lines = [line for line in out.splitlines()
                         if line.split()[:1] == ["standdown"]]
                self.assertEqual(len(lines), 1)
                self.assertIn("UNREADABLE", lines[0])
                self.assertLess(len(out), 2000)

    def test_utf8_surrogate_key_refusal_is_itself_encodable(self):
        """Kills a length-checked field name interpolated raw into a refusal."""
        valid = {"reason": "révision", "étiquette": "漢字"}
        payload, err = tasks.standdown_contract(valid)
        self.assertIsNone(err, err)
        self.assertEqual(payload, valid)
        row, err = tasks.update(self.tid, standdown=valid)
        self.assertIsNone(err, err)
        self.assertEqual(row["standdown"], valid)
        self.assertEqual(tasks.get(self.tid)["standdown"], valid)
        with open(tasks.ledger_path(), "rb") as ledger:
            before = ledger.read()
        for value in ("ok", [], "x" * 4001):
            with self.subTest(value_kind=type(value).__name__, size=len(value)):
                bad = {"reason": "r", "\ud800": value}
                payload, err = tasks.standdown_contract(bad)
                self.assertIsInstance(err, str)
                self.assertLess(len(err), 300)
                # This assertion reaches the real encoding operation even if a
                # nonempty error merely echoed the hostile key into its text.
                err.encode("utf-8", errors="strict")
                self.assertIsNone(payload)
                row, err = tasks.update(self.tid, standdown=bad)
                self.assertIsNone(row)
                self.assertIsInstance(err, str)
                self.assertNotIn("task ledger refused the write", err)
                err.encode("utf-8", errors="strict")
                with open(tasks.ledger_path(), "rb") as ledger:
                    self.assertEqual(ledger.read(), before)
                self.assertEqual(tasks.get(self.tid)["standdown"], valid)

    def test_a_duration_too_far_away_refuses_instead_of_raising(self):
        """The digits in a duration are unbounded too, so the multiply and the
        add can produce a value float cannot hold or gmtime cannot render. A
        deadline nobody can express is a refusal, never a traceback out of a
        CLI verb."""
        got, err = tasks.parse_standdown_until("99999999999999999999w")
        self.assertIsNone(got)
        self.assertTrue(err)
        rc, out, err_s = self.cli("standdown", "8801", "paused",
                                  "--until", "99999999999999999999w")
        self.assertEqual(rc, 2, out)
        # POSITIVE CONTROL, unconditional: an ordinary duration is accepted.
        ok, oerr = tasks.parse_standdown_until("2d")
        self.assertIsNone(oerr)
        self.assertIsNotNone(ok)

    def test_the_literal_escape_protects_the_reserved_words_too(self):
        """THE ESCAPE MUST ESCAPE THE WORDS IT EXISTS FOR. Resolving the
        option/literal boundary AFTER scanning for --clear meant `standdown ID
        -- --clear` was read as a clear-carrying-a-reason and refused: the
        escape protected every word except the reserved ones, which are
        exactly the words a reason needs protecting from."""
        rc, out, err = self.cli("standdown", "8801", "--", "--clear")
        self.assertEqual(rc, 0, err)
        detail = tasks.standdown_of(tasks.get(self.tid))[1]
        self.assertEqual(detail["reason"], "--clear")
        self.assertTrue(tasks.standdown_blocks_offer(tasks.get(self.tid)))
        # And the real --clear, BEFORE any escape, still clears.
        rc, out, err = self.cli("standdown", "8801", "--clear")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(tasks.standdown_of(tasks.get(self.tid))[0])

    def test_a_reason_may_start_with_a_dash_after_the_literal_escape(self):
        """The escape the refusal above owes: refusing unknown options must
        not make a legitimate dash-leading reason unsayable."""
        rc, out, err = self.cli("standdown", "8801", "--", "--not-a-flag",
                                "but a reason")
        self.assertEqual(rc, 0, err)
        detail = tasks.standdown_of(tasks.get(self.tid))[1]
        self.assertEqual(detail["reason"], "--not-a-flag but a reason")


class Synopsis(unittest.TestCase):

    def test_usage_advertises_the_verb(self):
        """Kills: a verb the parser dispatches and --help never mentions. A
        synopsis omission reads as absence to every seat that checks."""
        self.assertIn("standdown", tasks.USAGE)
        self.assertIn("--clear", tasks.USAGE)


if __name__ == "__main__":
    unittest.main()
