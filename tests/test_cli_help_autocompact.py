"""The autocompact help text stays pinned to the watchdog's own default.

`helm seat --help` renders the autocompact clause out of
cli_help._VERB_HELP['seat'] — "autocompact = proxy-seat context watchdog
(inject /compact at ~NN% before the 100% hang)" — and that NN is the number
an operator reads for when the watchdog fires. helm/autocompact.py owns the
trigger (DEFAULT_THRESHOLD, read by threshold_pct()). The two must name the
same number; this is the arm that refuses to let them drift apart again
(it was written while the help said ~90% and the watchdog fired at 80).
"""
import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

import re
import unittest

from helm import cli_help  # noqa: E402
from helm import autocompact  # noqa: E402


class AutocompactHelpThresholdTest(unittest.TestCase):
    def test_the_seat_help_names_the_watchdogs_default_threshold(self):
        """The seat help's autocompact clause carries the watchdog's own
        default, so the help cannot read one trigger while the watchdog
        fires at another."""
        m = re.search(r"inject /compact at ~(\d+)%", cli_help._VERB_HELP["seat"])
        self.assertIsNotNone(
            m, "the seat help's autocompact clause no longer reads "
               "'inject /compact at ~NN%'")
        self.assertEqual(int(m.group(1)), autocompact.DEFAULT_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
