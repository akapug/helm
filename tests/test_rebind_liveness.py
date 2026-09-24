"""The rebind door's live-reader rung, asked directly.

`tests/test_dispatches.py` proves the rung through the verb. These arms hold
the two halves apart, because the whole claim of the rung is a CONJUNCTION:
a live recipient alone and a delivered row alone must each pass, and only
their coincidence may refuse. An end-to-end arm cannot show which half did
the work.

EVERY ABSENCE HERE CARRIES ITS OWN POSITIVE CONTROL in the same method. An
arm that only asserts "" would also pass against a function that returned ""
for everything, which is the failure mode this whole rung is built to avoid:
a rung that refuses nothing and a rung that is never reached read alike.
"""
import os
import shutil
import tempfile
import unittest

from . import _tmphome

from helm import rebind_liveness, seats


def _row(**kw):
    """A dispatch row shaped the way the FOLD hands one to `rebind`."""
    row = {"id": "a" * 32, "recipient": "seat-a", "status": "open",
           "delivery": "needs-confirmation", "delivery_ref": None,
           "lane": "a-lane", "kind": "review"}
    row.update(kw)
    return row


_DELIVERED = {"delivery": "observed", "delivery_ref": "dm-ref"}
_READING = "PENDING VERDICT with delivery observed"


class ReadingStateTest(unittest.TestCase):
    def test_an_open_row_with_delivery_observed_is_a_read_in_progress(self):
        self.assertEqual(rebind_liveness.reading_state(_row(**_DELIVERED)),
                         _READING)

    def test_an_undelivered_open_row_is_not(self):
        """The brief is not proven to have reached anybody, so there is no
        read to interrupt — this is the ordinary rebind the verb exists for."""
        self.assertEqual(rebind_liveness.reading_state(_row(**_DELIVERED)),
                         _READING)          # control: the door does open
        self.assertEqual(rebind_liveness.reading_state(_row()), "")

    def test_a_closed_row_is_not_being_read_however_it_was_delivered(self):
        """Status outranks delivery: a verdicted or cancelled row was answered
        or retired, and its delivery evidence is history rather than a read."""
        self.assertEqual(rebind_liveness.reading_state(_row(**_DELIVERED)),
                         _READING)          # control: same row, open
        for status in ("verdict", "cancelled", "held"):
            with self.subTest(status=status):
                self.assertEqual(
                    rebind_liveness.reading_state(
                        _row(status=status, **_DELIVERED)), "")

    def test_a_non_row_is_not_a_read(self):
        self.assertEqual(rebind_liveness.reading_state(_row(**_DELIVERED)),
                         _READING)          # control
        for value in (None, "a-row-id", 7, []):
            with self.subTest(value=value):
                self.assertEqual(rebind_liveness.reading_state(value), "")


class RecipientActivityTest(unittest.TestCase):
    """The activity half, over a REAL helm home and a REAL empty process
    table. Nothing here mocks the readers: a seat whose presence file exists
    is live, a seat whose presence file does not is absent, and that is the
    same pair of facts the stranded sweep reads."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-rebindlive-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _tmphome.own_env(self, "HELM_HOME", os.path.join(self.tmp, "helm"))
        _tmphome.own_env(self, "HELM_ADOPTED_DIR",
                         os.path.join(self.tmp, "adopted"))
        proc = os.path.join(self.tmp, "proc")
        os.makedirs(proc)
        _tmphome.own_env(self, "HELM_PROC", proc)

    def _live(self, seat_name):
        """A tool boundary for this seat, written AS this seat.

        `touch_seen` refuses a FOREIGN seat, so the declared name is swapped
        for the duration of the beat. A process that inherits somebody else's
        HELM_CHAT_NAME would otherwise get a silently refused beat and an arm
        that passes for the wrong reason."""
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = seat_name
        try:
            seats.write_roster(seat_name, presence_beat=True)
        finally:
            if prior is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = prior
        self.assertEqual(seats.presence_of(seats.last_seen(seat_name)),
                         "fresh", "the fixture did not write a tool boundary")

    def _stranded(self, seat_name):
        seats.write_roster(seat_name, presence_beat=False)
        self.assertEqual(seats.presence_of(seats.last_seen(seat_name)),
                         "absent", "the control recipient is not stranded")

    def test_a_seat_with_a_recent_tool_boundary_reads_live(self):
        self._live("seat-a")
        self.assertIn("presence fresh",
                      rebind_liveness.recipient_activity("seat-a"))

    def test_a_seat_with_no_tool_boundary_reads_absent(self):
        """THE CONTROL for the arm above, and the whole reason the rung can be
        composed with a routing decision: a stranded recipient produces no
        fact, so the capacity gate keeps its answer."""
        self._live("seat-a")
        self.assertNotEqual(rebind_liveness.recipient_activity("seat-a"), "")
        self._stranded("seat-b")
        self.assertEqual(rebind_liveness.recipient_activity("seat-b"), "")

    def test_an_unnamed_recipient_is_never_live(self):
        self._live("seat-a")
        self.assertNotEqual(rebind_liveness.recipient_activity("seat-a"), "")
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(rebind_liveness.recipient_activity(value), "")

    def test_live_reader_needs_BOTH_halves(self):
        self._live("seat-a")
        live_and_read = rebind_liveness.live_reader(_row(**_DELIVERED))
        self.assertIsNotNone(live_and_read,
                             "the conjunction itself did not fire, so the two "
                             "negatives below prove nothing")
        self.assertEqual(live_and_read["who"], "seat-a")
        # live, but nobody has been handed the brief
        self.assertIsNone(rebind_liveness.live_reader(_row()))
        # delivered, but to a seat with no measured activity
        self._stranded("seat-b")
        self.assertIsNone(rebind_liveness.live_reader(
            _row(recipient="seat-b", **_DELIVERED)))

    def test_the_activity_fact_stays_short_enough_to_be_recorded(self):
        """The fact rides a cancel reason the ledger cuts at 256 bytes, with
        the caller's own words after it. A long evidence string would push the
        reason a reader actually needs off the end of the record."""
        self._live("seat-a")
        fact = rebind_liveness.live_reader(_row(**_DELIVERED))
        recorded = rebind_liveness.override_reason(fact, "why it had to move")
        self.assertLessEqual(len(recorded), 200, recorded)
        self.assertIn("@seat-a", recorded)
        self.assertIn("why it had to move", recorded)


class RefusalTextTest(unittest.TestCase):
    def test_the_refusal_names_the_reader_the_evidence_and_both_doors(self):
        text = rebind_liveness.refusal(
            {"who": "seat-a", "activity": "presence fresh",
             "state": _READING})
        self.assertIn("is READING this row", text)      # unconditional control
        for needle in ("@seat-a", "presence fresh", _READING,
                       "WAIT for the verdict", "--force --reason"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
