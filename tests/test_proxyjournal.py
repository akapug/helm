"""A wall that clears leaves a record.

THE DEFECT THIS PINS: proxywatch's store is a SNAPSHOT, and the falsification
fields are carried only while a family is in cooldown -- so the instant a wall
CLEARS, when the belief has by definition become interesting, every field it
was built on is dropped and the record that replaces it has no trace they
existed. Task/2125.

Every arm here opens with an unconditional positive control on the same
observable before the absence it is about, because "it wrote nothing" and "it
was never called" are otherwise the same green.
"""
import os
import tempfile
import unittest

from helm import proxyjournal, proxywatch


BELIEVING = {"state": "PROXY-COOLDOWN",
             "falsification_observed_at": "2026-09-10T22:00:00Z",
             "falsification_proxy_identity": "birth-abc",
             "falsification_identity_state": "VERIFIED",
             "falsification_age_s": 900,
             "falsification_due": True}


class JournalBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="proxyjournal-")
        self.log = os.path.join(self.dir, "nested", "proxywatch.log")

    def rows(self):
        return proxyjournal.read(self.log)


class RecordTest(JournalBase):
    def test_an_ending_episode_writes_what_it_believed(self):
        self.assertTrue(proxyjournal.record_episode_end(
            "ds4pro-x", BELIEVING, "OK", log=self.log))  # noqa: SEAT_NAME — a fixture family label, never a seat
        rows, err = self.rows()
        self.assertIsNone(err)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["event"], proxyjournal.END)
        self.assertEqual(row["was"], "PROXY-COOLDOWN")
        self.assertEqual(row["now"], "OK")
        self.assertEqual(row["observed_at"], "2026-09-10T22:00:00Z")
        self.assertEqual(row["proxy_identity"], "birth-abc")
        self.assertEqual(row["age_s"], "900")
        self.assertEqual(row["due"], "True")

    def test_a_healthy_watch_writes_NOTHING(self):  # noqa: VACUOUS_ASSERTION — a real record is written FIRST and its count asserted, so the unchanged count below is silence and not an uncalled writer
        """A record here means an episode ENDED. A transition that never held a
        belief has nothing to lose and must not fill the file with silence."""
        self.assertTrue(proxyjournal.record_episode_end(
            "fam-a", BELIEVING, "OK", log=self.log))
        before = len(self.rows()[0])
        self.assertFalse(proxyjournal.record_episode_end(
            "fam-a", {"state": "OK"}, "OK", log=self.log))
        self.assertFalse(proxyjournal.record_episode_end(
            "fam-a", None, "OK", log=self.log))
        self.assertEqual(len(self.rows()[0]), before)

    def test_a_record_that_cannot_name_its_family_is_refused(self):  # noqa: VACUOUS_ASSERTION — the control writes the identical belief WITH a family and is asserted present; only the name is removed
        self.assertTrue(proxyjournal.record_episode_end(
            "fam-a", BELIEVING, "OK", log=self.log))
        self.assertEqual(len(self.rows()[0]), 1)
        self.assertFalse(proxyjournal.record_episode_end(
            "", BELIEVING, "OK", log=self.log))
        self.assertEqual(len(self.rows()[0]), 1)

    def test_a_partial_belief_still_records_what_it_had(self):
        """An episode with an observation and no identity is still an episode;
        the absent field is simply absent rather than a fabricated empty."""
        self.assertTrue(proxyjournal.record_episode_end(
            "fam-a", {"state": "PROXY-COOLDOWN",
                      "falsification_observed_at": "2026-09-10T21:00:00Z"},
            "OK", log=self.log))
        row = self.rows()[0][0]
        self.assertEqual(row["observed_at"], "2026-09-10T21:00:00Z")
        self.assertNotIn("proxy_identity", row)


class ReadTest(JournalBase):
    def test_a_missing_journal_is_UNMEASURED_never_a_clean_bill(self):  # noqa: VACUOUS_ASSERTION — the control reads the same path after a real write and asserts rows plus no error
        proxyjournal.record_episode_end("fam-a", BELIEVING, "OK", log=self.log)
        rows, err = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(err)
        os.remove(self.log)
        rows, err = self.rows()
        self.assertEqual(rows, [])
        self.assertIn("UNMEASURED", err)

    def test_episodes_group_by_family_and_drop_nothing_torn(self):
        proxyjournal.record_episode_end("fam-a", BELIEVING, "OK", log=self.log)
        proxyjournal.record_episode_end("fam-b", BELIEVING, "OK", log=self.log)
        proxyjournal.record_episode_end("fam-a", BELIEVING, "AUTH-UNAVAILABLE",
                                        log=self.log)
        rows, _ = self.rows()
        grouped = proxyjournal.episodes(rows)
        self.assertEqual(sorted(grouped), ["fam-a", "fam-b"])
        self.assertEqual(len(grouped["fam-a"]), 2)
        self.assertEqual(grouped["fam-a"][-1]["now"], "AUTH-UNAVAILABLE")

    def test_our_own_torn_line_is_kept_and_a_foreign_one_is_not(self):  # noqa: VACUOUS_ASSERTION — the control asserts the intact record is read as END on the same call
        proxyjournal.record_episode_end("fam-a", BELIEVING, "OK", log=self.log)
        with open(self.log, "a") as fh:
            fh.write("2026-09-10T22:10:00 PROXY-FALSIFY-END pid=1 | was=X\n")
            fh.write("2026-09-10T22:11:00 STOP-RUNG pid=1 | run=r | rung=x\n")
        rows, _ = self.rows()
        self.assertEqual([r["event"] for r in rows],
                         [proxyjournal.END, proxyjournal.MALFORMED])
        self.assertEqual(len(proxyjournal.malformed(rows)), 1)


class WiringTest(JournalBase):
    """THE WRITE HAPPENS ON THE REAL TRANSITION, not only when called directly."""

    def setUp(self):
        super().setUp()
        os.environ["HELM_PROXYJOURNAL_LOG"] = self.log
        self.addCleanup(os.environ.pop, "HELM_PROXYJOURNAL_LOG", None)

    def test_a_clearing_wall_leaves_a_record_and_a_healthy_pass_does_not(self):  # noqa: VACUOUS_ASSERTION — the same _compose_upstream_seat call writes a real row later in this arm, so the empty read is the TRANSITION and not a dead path
        now = 1789080000.0
        # POSITIVE CONTROL FIRST: a pass with no prior belief writes nothing,
        # so the record below is the TRANSITION and not merely "it was called".
        proxywatch._compose_upstream_seat(
            "seat-a", ("OK", "fine", 5), {"state": "OK"}, now)
        rows, err = proxyjournal.read(self.log)
        self.assertEqual(rows, [])
        self.assertIn("UNMEASURED", err)
        after = proxywatch._compose_upstream_seat(
            "seat-a", ("OK", "recovered", 5), dict(BELIEVING), now)
        rows, err = proxyjournal.read(self.log)
        self.assertIsNone(err)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["was"], "PROXY-COOLDOWN")
        self.assertEqual(rows[0]["now"], "OK")
        self.assertEqual(rows[0]["proxy_identity"], "birth-abc")
        # AND THE HEALTHY RECORD STAYS HONEST ABOUT THE PRESENT: retaining the
        # fields would render a stale observation clock beside a recovered
        # family, which is a surface that lies.
        self.assertNotIn("falsification_observed_at", after)


if __name__ == "__main__":
    unittest.main()
