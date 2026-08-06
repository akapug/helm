#!/usr/bin/env python3
"""helm fleet — the daemon count now says what it MEANS.

An orca upgrade starts a new terminal daemon and does not reap the old one.
The current UI cannot render sessions owned by another generation, so agents
inside a stale daemon go INVISIBLE while still running, with live TTYs and
transcripts still being written.

`helm fleet` has always PRINTED the daemon count. On 2026-07-27 it printed
"4 orca daemon(s)" in the first command run during the incident, and two
agents read past it — because a number with no NORMAL beside it is a fact,
not a finding. The owner diagnosed it instead, from bare shells in his UI,
and reasonably concluded the upgrade had killed his fleet. Nothing had been
killed: zero session-killed events, one seat sitting on 6h31m of CPU.

So this is not new detection. It is the interpretation the detection was
missing. The tests that matter here are the FAIL-CLOSED ones: "I could not
look" must never render as "there is only one".
"""
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-fleetgen-", var="HELM_HOME")

from helm import fleet  # noqa: E402

# A FIXTURE, not a real path — and deliberately not the owner's home.
# The first cut hardcoded the OWNER'S HOME, which passed gemini's review (the
# LOGIC was right) and then failed the LAND, because the as-public scan
# reads literals the reviewer had no reason to look at. A machine-specific
# path in a test is the same defect class as one in shipped code: it is
# context identity baked into portable logic, and it only ever surfaces on
# somebody else's machine. The comment explaining this fix ORIGINALLY
# quoted the offending path verbatim and would have tripped the same
# scan — a literal in a comment is still a literal in the file.
SOCK = "/tmp/orca-daemon-test/daemon-%s.sock"


def argv_for(gen):
    return ["/tmp/.mount_orca-x/orca-ide", "daemon-entry.js", "--socket",
            SOCK % gen, "--token", "/x/tok"]


class GenerationScanTest(unittest.TestCase):
    def scan(self, daemons, starts, probes):
        with mock.patch.object(fleet.session, "_proc_start",
                               side_effect=lambda p: starts.get(p)), \
             mock.patch.object(fleet, "_cmdline_probe",
                               side_effect=lambda p: probes.get(p, ("failed", None))):
            return fleet._daemon_generations(daemons)

    def test_one_generation_is_the_quiet_case(self):
        gens, unknown = self.scan({10: "111"}, {10: "111"},
                                  {10: ("ok", argv_for("v28"))})
        self.assertEqual(gens, {"v28": [10]})
        self.assertEqual(unknown, 0)
        self.assertEqual(fleet._generation_verdict(gens, unknown), "")

    def test_the_measured_incident_four_generations(self):
        """v23/v24/v26/v28, the real 2026-07-27 shape."""
        d = {1: "a", 2: "b", 3: "c", 4: "d"}
        probes = {1: ("ok", argv_for("v23")), 2: ("ok", argv_for("v24")),
                  3: ("ok", argv_for("v26")), 4: ("ok", argv_for("v28"))}
        gens, unknown = self.scan(d, d, probes)
        self.assertEqual(sorted(gens), ["v23", "v24", "v26", "v28"])
        v = fleet._generation_verdict(gens, unknown)
        self.assertIn("4 PROTOCOL GENERATIONS ALIVE", v)
        self.assertIn("v23, v24, v26, v28", v)
        self.assertIn("INVISIBLE", v)          # names the consequence
        self.assertIn("not dead", v)           # and the wrong conclusion it prevents

    def test_a_REUSED_pid_is_unknown_not_counted(self):
        """The starttime no longer matches, so this is not the daemon we
        scanned. Counting its cmdline would attribute a generation to a
        process that is not the one under discussion."""
        gens, unknown = self.scan({10: "111"}, {10: "999"},
                                  {10: ("ok", argv_for("v28"))})
        self.assertEqual(gens, {})
        self.assertEqual(unknown, 1)

    def test_an_unreadable_cmdline_is_unknown_not_clean(self):
        for status in ("failed", "gone"):
            gens, unknown = self.scan({10: "111"}, {10: "111"},
                                      {10: (status, None)})
            self.assertEqual(gens, {}, status)
            self.assertEqual(unknown, 1, status)

    def test_an_UNPARSEABLE_socket_is_unknown(self):
        """A daemon whose argv carries no daemon-vNN.sock — a shape change
        upstream must read as UNKNOWN, never as agreement."""
        gens, unknown = self.scan(
            {10: "111"}, {10: "111"},
            {10: ("ok", ["orca-ide", "daemon-entry.js", "--socket", "/x/d.sock"])})
        self.assertEqual(gens, {})
        self.assertEqual(unknown, 1)


class VerdictTest(unittest.TestCase):
    """The verdict is the whole point — the count already existed."""

    def test_unknown_REFUSES_to_certify_a_single_generation(self):
        """THE fail-closed case. One readable v28 plus one unreadable daemon
        must NOT report a clean single generation: the unreadable one could be
        the stale generation stranding somebody."""
        v = fleet._generation_verdict({"v28": [1]}, 1)
        self.assertIn("UNPROVEN", v)
        self.assertIn("cannot be ruled out", v)
        self.assertNotEqual(v, "")

    def test_unknown_wins_over_a_multi_generation_message(self):
        """Precedence: if we could not look at everything, say THAT — a
        confident 'two generations' understates when a third is unreadable."""
        v = fleet._generation_verdict({"v26": [1], "v28": [2]}, 2)
        self.assertIn("UNPROVEN", v)

    def test_zero_daemons_says_nothing(self):
        self.assertEqual(fleet._generation_verdict({}, 0), "")

    def test_the_verdict_tells_you_WHAT_TO_DO(self):
        """A warning that names a condition without a next move gets read once
        and ignored after. This one names the roll-call."""
        v = fleet._generation_verdict({"v26": [1], "v28": [2]}, 0)
        self.assertIn("helm chat", v)
        self.assertIn("Roll-call", v)


if __name__ == "__main__":
    unittest.main()
