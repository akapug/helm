#!/usr/bin/env python3
"""A check that cannot see a case must return UNKNOWN, never a verdict.

Two surfaces broke this law about the SAME subjects on 2026-07-29, in opposite
directions, which is why they are one lane:

  helm seat status  said "proxy down" for three proxies that were LISTENING
  helm seat doctor  said "no valid codex cred" while two pooled creds had 233h

Both are fail-closed signals laundered into confident verdicts. In each case a
SIBLING surface read the same input and stayed honest — `doctor --ensure` says
"alive but unverifiable, refusing to signal", and `status` reports the pooled
cred's real expiry — so neither module is reliably the careful one. The law is
what was missing.

The cost was not cosmetic: the cred line escalated a nonexistent credential
problem to the owner, and "proxy down" routes a maintainer to respawn a process
whose PIDFILE, not whose liveness, is the problem.
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from helm import seat


class ProxyPidVerdictTest(unittest.TestCase):
    """The ladder's four conditions must be DISTINGUISHABLE by a reporter."""

    def test_no_record_is_named(self):
        with mock.patch.object(seat, "_proxy_pid_record", return_value=None):
            rec, why, raw = seat._proxy_pid_verdict("kimi")
        self.assertIsNone(rec)
        self.assertEqual(why, seat.PROXY_NO_RECORD)

    def test_dead_pid_is_named_and_keeps_the_raw_record(self):
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 4242, "identity": "proc:7"}), \
             mock.patch.object(seat, "_pid_alive", return_value=False):
            rec, why, raw = seat._proxy_pid_verdict("kimi")
        self.assertEqual(why, seat.PROXY_PID_DEAD)
        self.assertEqual(raw["pid"], 4242)

    def test_bare_identity_is_UNVERIFIABLE_not_absent(self):
        """The codex case: a legacy pidfile with no captured birth identity."""
        for ident in (None, "", "?"):
            with self.subTest(identity=ident):
                with mock.patch.object(seat, "_proxy_pid_record",
                                       return_value={"pid": 815805,
                                                     "identity": ident}), \
                     mock.patch.object(seat, "_pid_alive", return_value=True):
                    rec, why, raw = seat._proxy_pid_verdict("codex")
                self.assertIsNone(rec)
                self.assertEqual(why, seat.PROXY_UNVERIFIABLE)
                self.assertEqual(raw["pid"], 815805)

    def test_reused_pid_is_its_own_reason(self):
        with mock.patch.object(seat, "_proxy_pid_record",
                               return_value={"pid": 99, "identity": "proc:7"}), \
             mock.patch.object(seat, "_pid_alive", return_value=True), \
             mock.patch.object(seat, "_pid_identity", return_value="proc:8"):
            rec, why, _ = seat._proxy_pid_verdict("kimi")
        self.assertEqual(why, seat.PROXY_PID_REUSED)

    def test_running_pid_rec_is_UNCHANGED_for_every_condition(self):
        """SIGNALLING BEHAVIOUR MUST NOT MOVE. The kill path reads this, and it
        must still collapse all four refusals to None — the guard is the point."""
        cases = [
            (None, True, "x", None),                       # no record
            ({"pid": 1, "identity": "p"}, False, "p", None),   # dead
            ({"pid": 1, "identity": "?"}, True, "p", None),    # unverifiable
            ({"pid": 1, "identity": "p"}, True, "q", None),    # reused
        ]
        for record, alive, live_ident, want in cases:
            with self.subTest(record=record):
                with mock.patch.object(seat, "_proxy_pid_record",
                                       return_value=record), \
                     mock.patch.object(seat, "_pid_alive", return_value=alive), \
                     mock.patch.object(seat, "_pid_identity",
                                       return_value=live_ident):
                    self.assertIs(seat._running_pid_rec("kimi"), want)


class ProxyLiveTextTest(unittest.TestCase):

    def _text(self, record, alive, live_ident, port_open):
        with mock.patch.object(seat, "_proxy_pid_record", return_value=record), \
             mock.patch.object(seat, "_pid_alive", return_value=alive), \
             mock.patch.object(seat, "_pid_identity", return_value=live_ident), \
             mock.patch.object(seat, "_instance_port", return_value=8319), \
             mock.patch.object(seat, "_port_open", return_value=port_open), \
             mock.patch.object(seat, "proxy_drift",
                               return_value=(seat.PROXY_CURRENT, "")):
            return seat._proxy_live_text("codex")[0]

    def test_listening_but_unverifiable_is_NOT_down(self):
        """THE DEFECT. pid 815805 held :8319 and answered in 0.9ms while three
        surfaces printed "proxy down"."""
        t = self._text({"pid": 815805, "identity": "?"}, True, "p", True)
        self.assertNotIn("down", t)
        self.assertIn("UNVERIFIABLE", t)
        self.assertIn("815805", t)

    def test_the_unverifiable_text_routes_AWAY_from_respawn(self):
        """"down" sends a maintainer to rung 1. The pidfile is the problem, so
        the text must say so — an honest state that routes to the wrong verb is
        only half a fix."""
        t = self._text({"pid": 815805, "identity": "?"}, True, "p", True)
        self.assertIn("do NOT respawn", t)
        self.assertIn("bare-pid", t)

    def test_unverifiable_AND_silent_port_is_UNKNOWN_not_up_and_not_down(self):
        t = self._text({"pid": 815805, "identity": "?"}, True, "p", False)
        self.assertIn("UNKNOWN", t)
        self.assertNotIn("proxy down", t)

    def test_a_genuinely_absent_proxy_still_reads_down(self):
        """The honest half must not regress into UNKNOWN-for-everything, which
        would make the new state meaningless."""
        self.assertIn("proxy down", self._text(None, True, "p", False))

    def test_a_dead_pid_still_reads_down(self):
        self.assertIn("proxy down",
                      self._text({"pid": 1, "identity": "p"}, False, "p", False))

    def test_an_authenticated_proxy_still_reads_UP_unchanged(self):
        t = self._text({"pid": 7, "identity": "p"}, True, "p", True)
        self.assertIn("proxy UP pid 7", t)
        self.assertNotIn("UNVERIFIABLE", t)


class CredExpShapeTest(unittest.TestCase):
    """One reader, two real file shapes. Applying one to the other returns None,
    and None reads as expired at every call site."""

    def _write(self, obj):
        fd, p = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        self.addCleanup(os.unlink, p)
        return p

    FUTURE_JWT = None   # built in setUp from a real exp claim

    def setUp(self):
        import base64
        exp = int(time.time()) + 86400
        body = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        self.jwt = "h.%s.s" % body
        self.exp = exp

    def test_reads_the_NESTED_codex_home_shape(self):
        p = self._write({"tokens": {"access_token": self.jwt}})
        self.assertEqual(seat._cred_exp(p), self.exp)

    def test_reads_the_FLAT_pool_shape(self):
        """The pool file the proxy hot-reloads. Both live creds on this host had
        this shape and read as None before the fix."""
        p = self._write({"access_token": self.jwt, "disabled": False})
        self.assertEqual(seat._cred_exp(p), self.exp)

    def test_unparseable_is_still_None_not_a_number(self):
        self.assertIsNone(seat._cred_exp(self._write({"nope": 1})))


class PooledCredIsNotAbsenceTest(unittest.TestCase):
    """codex-homes is the POOLING SOURCE. Its emptiness is architectural."""

    def setUp(self):
        import base64
        exp = int(time.time()) + 86400
        body = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        self.jwt = "h.%s.s" % body
        self.homes = tempfile.mkdtemp()
        self.pool = tempfile.mkdtemp()

    def _pool(self, name, tok):
        with open(os.path.join(self.pool, name), "w") as f:
            json.dump({"access_token": tok, "disabled": False}, f)

    def _run(self):
        from helm import codexhomes
        with mock.patch.object(seat, "CODEX_HOMES", self.homes), \
             mock.patch.object(codexhomes, "pool_dir", return_value=self.pool):
            return seat.newest_valid_codex_auth()

    def test_valid_pool_plus_empty_homes_is_NOT_APPLICABLE(self):
        """THE LINE THAT ESCALATED A NONEXISTENT PROBLEM TO THE OWNER."""
        self._pool("codex-a.json", self.jwt)
        path, reason = self._run()
        self.assertIsNone(path)
        self.assertIn("not applicable", reason)
        self.assertNotIn("no valid codex cred", reason)

    def test_expired_pool_says_EXPIRED_not_missing(self):
        """Distinct from unparseable and distinct from absent — three states."""
        import base64
        past = base64.urlsafe_b64encode(
            json.dumps({"exp": int(time.time()) - 10}).encode()).decode().rstrip("=")
        self._pool("codex-old.json", "h.%s.s" % past)
        _, reason = self._run()
        self.assertIn("expired", reason)
        self.assertIn("1 pooled file", reason)

    def test_nothing_anywhere_still_reports_absence(self):
        _, reason = self._run()
        self.assertIn("no valid codex cred", reason)
        self.assertIn(self.pool, reason)


if __name__ == "__main__":
    unittest.main()
