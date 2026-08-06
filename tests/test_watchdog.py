"""Hermetic tests for helm.watchdog — the context-window brick backstop.
HELM_HOME points at a tmp dir; synthetic proxy error logs drive detection.
No chat node, no real logs, no a2a posts (check(post=False))."""
import os
import shutil
import tempfile
import time
import unittest

from helm import watchdog, seat


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-wd-")
        self._home = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        self.logs = os.path.join(seat.seat_dir("codex"), "auth", "logs")
        os.makedirs(self.logs, exist_ok=True)

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_failure(self, name, request="a normal prompt", response="upstream ok",
                       age=0):
        """ONE failed request, in the REAL CLIProxyAPI log shape: the full
        request, the `=== RESPONSE ===` marker, then the response.

        The old fixtures wrote bare lines with no marker at all, which is a shape
        real proxy logs never have — and that is precisely why the request-side
        counting bug survived: no test could distinguish the two halves because
        the fixtures had no halves.
        """
        p = os.path.join(self.logs, name)
        with open(p, "w") as f:
            f.write("POST /v1/messages\n%s\n\n=== RESPONSE ===\nStatus: 400\n%s\n"
                    % (request, response))
        if age:
            past = time.time() - age
            os.utime(p, (past, past))
        else:
            time.sleep(0.01)          # keep mtime ordering deterministic
        return p

    CTX = '{"error":{"message":"input exceeds the context window of this model"}}'

    def test_wedge_detected_and_deduped(self):
        # a real wedge = SEVERAL failed requests, each its own log, each whose
        # RESPONSE carries the signature (compaction itself 400s too)
        for i in (1, 2, 3):
            self._write_failure("error-%d.log" % i, response=self.CTX)
        wedged = watchdog.scan(("codex",))
        self.assertEqual(len(wedged), 1)
        self.assertEqual(wedged[0]["family"], "codex")
        self.assertEqual(wedged[0]["count"], 3)    # 3 failures, not 3 lines
        # first check alerts (fresh); second dedups (count unchanged)
        r1 = watchdog.check(("codex",), post=False)
        self.assertEqual([w["family"] for w in r1["fresh"]], ["codex"])
        r2 = watchdog.check(("codex",), post=False)
        self.assertEqual(r2["fresh"], [])
        self.assertEqual(len(r2["wedged"]), 1)  # still wedged, just not re-alerted

    def test_single_400_is_not_a_wedge(self):
        # one over-large turn CC could still compact past is NOT a wedge.
        # This is the assertion WEDGE_THRESHOLD exists for, and before the
        # response-side fix it could not hold: a lone failure whose request text
        # mentioned the phrase twice cleared a threshold of 2 by itself.
        self._write_failure("error-1.log", response=self.CTX)
        self.assertEqual(watchdog.scan(("codex",)), [])

    def test_the_agents_own_words_are_not_evidence(self):
        """THE FIX. A request body discussing context windows is not a wedge.

        Measured on a live wedge log: it carried the phrase 5x in the
        REQUEST and 1x in the response. Any agent reasoning about this very
        watchdog writes the phrase into its own transcript, so request-side
        counting turns a conversation into fabricated evidence of a fleet fault.
        """
        chatty = ("i am debugging why the context window keeps overflowing; "
                  "the context window is 200k and the context window gauge "
                  "under-reads, so context window handling matters")
        for i in (1, 2, 3):
            self._write_failure("error-%d.log" % i, request=chatty,
                                response='{"error":{"message":"rate limited"}}')
        self.assertEqual(watchdog.scan(("codex",)), [],
                         "request-body mentions must never count as wedge hits")

    def test_a_markerless_log_is_unreadable_never_a_hit(self):
        """No marker means we did not read a RESPONSE. That is UNKNOWN, and it
        must not fall back to scanning the request — the fallback IS the bug."""
        p = os.path.join(self.logs, "error-1.log")
        with open(p, "w") as f:
            f.write("exceeds the context window\nexceeds the context window\n")
        hits, _log, unreadable = watchdog._count_ctx_400s(self.logs)
        self.assertEqual(hits, 0)
        self.assertEqual(unreadable, 1)

    def test_one_log_is_one_hit_however_many_lines_match(self):
        """One failed request is one data point. Counting lines conflated 'N
        failures' with 'one failure that said it N times'."""
        self._write_failure("error-1.log",
                            response="\n".join([self.CTX] * 9))
        hits, _log, _bad = watchdog._count_ctx_400s(self.logs)
        self.assertEqual(hits, 1)

    def test_clear_seat_and_recovery_rearm(self):
        # wedge -> alert; seat recovers -> state re-armed; a new wedge alerts AGAIN
        for i in (1, 2):
            self._write_failure("error-%d.log" % i, response=self.CTX)
        self.assertEqual([w["family"] for w in
                          watchdog.check(("codex",), post=False)["fresh"]], ["codex"])
        # recovery: age the wedge logs out of the window. Without the time
        # window this test cannot pass — old failures stay on disk forever, so a
        # /cleared, healthy seat would read WEDGED for the rest of its life.
        for i in (1, 2):
            past = time.time() - (watchdog.RECENT_SECONDS + 60)
            os.utime(os.path.join(self.logs, "error-%d.log" % i), (past, past))
        rec = watchdog.check(("codex",), post=False)
        self.assertEqual(rec["wedged"], [])
        for i in (3, 4):
            self._write_failure("error-%d.log" % i, response=self.CTX)
        self.assertEqual([w["family"] for w in
                          watchdog.check(("codex",), post=False)["fresh"]], ["codex"])

    def test_aged_out_failures_do_not_wedge(self):
        for i in (1, 2, 3):
            self._write_failure("error-%d.log" % i, response=self.CTX,
                                age=watchdog.RECENT_SECONDS + 300)
        self.assertEqual(watchdog.scan(("codex",)), [])

    def test_every_proxy_family_is_watched(self):
        """A hardcoded pair silently stopped covering ds4pro; an unwatched proxy
        seat wedges invisibly, which is the failure this module exists to stop."""
        from helm.seat import FAMILIES
        for fam in FAMILIES:
            self.assertIn(fam, watchdog.PROXY_FAMILIES, fam)

    def test_the_blind_spot_is_stated_not_implied_covered(self):
        self.assertIn("200", watchdog.BLIND_SPOT)
        self.assertIn("proxy-side", watchdog.BLIND_SPOT)

    def test_no_logs_is_clear(self):
        self.assertEqual(watchdog.scan(("codex",)), [])

    def test_alert_text_is_actionable(self):
        txt = watchdog._alert_text({"family": "codex", "count": 4,
                                    "log": "/x/error-9.log"})
        self.assertIn("codex", txt)
        self.assertIn("/clear", txt)          # names the recovery
        self.assertIn("context window", txt.lower())

    def test_a_log_vanishing_between_glob_and_sort_does_not_crash(self):
        """THE REAL RACE, and the mutation-killing version. My first attempt at
        this test called _mtime() directly and then scanned files that all
        existed — so reverting to a bare os.path.getmtime sort key left it GREEN.
        A test that cannot fail when the fix is removed is theatre.

        The actual failure needs a path that is present at GLOB time and gone at
        STAT time. glob is patched to inject exactly that, which is what log
        rotation does for real."""
        self._write_failure("error-1.log", response=self.CTX)
        self._write_failure("error-2.log", response=self.CTX)
        ghost = os.path.join(self.logs, "error-rotated-away.log")
        real_glob = watchdog.glob.glob
        watchdog.glob.glob = lambda pat: real_glob(pat) + [ghost]
        try:
            hits, _log, _bad = watchdog._count_ctx_400s(self.logs)
        finally:
            watchdog.glob.glob = real_glob
        self.assertEqual(hits, 2, "the two real failures must still be counted")

    def test_a_log_vanishing_mid_scan_does_not_crash_the_pass(self):
        """RESCUED FIX, pinned. Log rotation races the scan: os.path.getmtime as
        a bare sort key raises FileNotFoundError from INSIDE sorted(), which is
        outside every try block in this module, so the whole watchdog pass dies
        instead of degrading. A vanished log must sort OLDEST so it can never be
        picked as newest."""
        real = watchdog._mtime
        self._write_failure("error-1.log", response=self.CTX)
        self._write_failure("error-2.log", response=self.CTX)
        gone = os.path.join(self.logs, "error-gone.log")
        self.assertEqual(watchdog._mtime(gone), -1.0)   # never raises
        # and a real scan still returns a verdict with a missing file in the glob
        hits, _log, _bad = watchdog._count_ctx_400s(self.logs)
        self.assertEqual(hits, 2)

    def test_mtime_sorts_a_vanished_file_oldest_never_newest(self):
        p = self._write_failure("error-1.log", response=self.CTX)
        self.assertGreater(watchdog._mtime(p), watchdog._mtime(p + ".missing"))


if __name__ == "__main__":
    unittest.main()
