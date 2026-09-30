"""Pin helm.offpeak's vendor clock and peak window for a seat-path test module
(task/3238, task/3504).

Seat paths cross the off-peak door, whose clock_error() asks the vendor's
HTTPS Date (_vendor_date, a live HEAD request) and records the proof in
offpeak._CLOCK_CACHE; the sliced gate's leak audit then refuses the module.
A seat-path test is not testing the clock (tests/test_offpeak.py does), so
pin() answers every clock check "proven" and makes the real proof fail the
test that reaches it, by any path.

THE WINDOW IS PINNED OPEN TOO. The door also asks whether every route the seat
can take is inside its vendor's peak window, against the real time of day, so
a seat-path test on a gated family (ds4pro rides deepseek-direct, peak 01:00-
04:00 and 06:00-10:00 UTC on weekdays) was held PEAK-WINDOW and failed only
inside those hours (task/3504: test_pending_inbox_suppresses_offer passed at
00:33Z and failed from 01:00Z, blocking every land). A seat-path test is not
testing the window either (tests/test_offpeak.py does), so pin() reports no
closed route; a module that tests a hold does not use this pin.
"""
from unittest import mock

from helm import offpeak


def _real_proof(window):
    raise AssertionError("a seat-path test reached offpeak's real vendor "
                         "clock (%s); pin it with tests._offpeak_clock.pin"
                         % window.get("vendor"))


def pin():
    """Start the pin; setUpModule calls it and tearDownModule calls the
    returned stop(). -> stop"""
    patches = [mock.patch.object(offpeak, "clock_error", return_value=None),
               mock.patch.object(offpeak, "_vendor_date",
                                 side_effect=_real_proof),
               mock.patch.object(offpeak, "_all_closed", return_value=[])]
    for p in patches:
        p.start()
    return lambda: [p.stop() for p in reversed(patches)]
