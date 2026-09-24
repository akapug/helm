"""The stamp that names a beacon's producer, and every way it refuses.

EVERY REFUSAL IS ITS OWN ARM, because the whole value of this module is that
it answers UNKNOWN rather than guessing, and an UNKNOWN reached for the wrong
reason is indistinguishable from one reached for the right reason unless the
reason is asserted. A single "it returns unknown" arm would pass against a
module that returned unknown unconditionally.
"""

import os
import tempfile
import unittest

from helm import beacon_origin


class StampBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-beacon-stamp-")
        self.addCleanup(self._restore)
        self._prev = {k: os.environ.get(k)
                      for k in ("HELM_HOME", beacon_origin.STAMP_ENV)}
        os.environ["HELM_HOME"] = self.tmp
        os.environ.pop(beacon_origin.STAMP_ENV, None)
        # THE FIXTURE PROVES ITS OWN ISOLATION RATHER THAN ASSUMING IT. Every
        # arm below mints and unlinks real files; if HELM_HOME ever stops
        # steering this path the arms would silently operate on the LIVE
        # beacon state of a running fleet and still pass. This assertion is
        # what makes that a loud failure instead of a quiet one.
        self.assertTrue(
            beacon_origin.stamp_dir().startswith(self.tmp),
            "stamp_dir escaped the fixture: %s" % beacon_origin.stamp_dir())

    def _restore(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mint(self, origin=beacon_origin.ORIGIN_MAIN, tool_use="toolu_01",
             session="S", now=1000.0):
        nonce = beacon_origin.mint(origin, tool_use, session=session, now=now)
        self.assertIsNotNone(nonce, "the fixture's own mint must succeed")
        return nonce


class MintAndConsumeTest(StampBase):
    def test_a_minted_stamp_reads_back_as_what_the_producer_said(self):
        nonce = self.mint(beacon_origin.ORIGIN_SUBAGENT, "toolu_abc")
        stamp, reason = beacon_origin.consume(nonce, session="S", now=1001.0)
        self.assertIsNone(reason)
        self.assertEqual(stamp["origin"], beacon_origin.ORIGIN_SUBAGENT)
        self.assertEqual(stamp["tool_use_id"], "toolu_abc")

    def test_the_NONCE_never_carries_the_answer(self):
        """A process that copies the environment variable copies a NAME. If the
        origin were in the variable, every consumer of a leaked env would mint
        its own truth."""
        nonce = self.mint(beacon_origin.ORIGIN_SUBAGENT, "toolu_abc")
        self.assertNotIn(beacon_origin.ORIGIN_SUBAGENT, nonce)
        self.assertNotIn(beacon_origin.ORIGIN_MAIN, nonce)
        self.assertTrue(beacon_origin.valid_nonce(nonce))

    def test_reading_a_stamp_CONSUMES_it(self):
        """Single-use is the property that makes a replayed nonce harmless."""
        nonce = self.mint()
        first, reason = beacon_origin.consume(nonce, session="S", now=1001.0)
        self.assertIsNone(reason)
        self.assertEqual(first["origin"], beacon_origin.ORIGIN_MAIN)
        second, reason2 = beacon_origin.consume(nonce, session="S", now=1002.0)
        self.assertIsNone(second)
        self.assertEqual(reason2, "consumed_or_absent")

    def test_a_producer_that_cannot_tell_does_not_MINT(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is the first statement in the body: mint(ORIGIN_MAIN, "toolu_ctl") must return a nonce, so a mint that could never succeed reddens this arm before any refusal is asserted
        """UNKNOWN is not a value a producer may stamp: the only way to say
        'I do not know' is to write no stamp at all."""
        # POSITIVE CONTROL, unconditional: mint CAN succeed here, so the
        # refusals below are refusals and not a broken fixture.
        self.assertIsNotNone(
            beacon_origin.mint(beacon_origin.ORIGIN_MAIN, "toolu_ctl"))
        for origin in (beacon_origin.ORIGIN_UNKNOWN, "", None, "MAIN", "other"):
            with self.subTest(origin=origin):
                self.assertIsNone(beacon_origin.mint(origin, "toolu_01"))

    def test_an_unbound_stamp_is_refused_at_MINT(self):  # noqa: VACUOUS_ASSERTION — same shape: the first assertion is an unconditional assertIsNotNone on a well-formed mint, which the walker does not read as a positive control
        """Tool_use-bound is a precondition, not a later check."""
        self.assertIsNotNone(                       # unconditional control
            beacon_origin.mint(beacon_origin.ORIGIN_MAIN, "toolu_ctl"))
        self.assertIsNone(
            beacon_origin.mint(beacon_origin.ORIGIN_MAIN, ""))
        self.assertIsNone(
            beacon_origin.mint(beacon_origin.ORIGIN_MAIN, None))


class EveryRefusalNamesItselfTest(StampBase):
    def test_no_nonce_at_all_is_absent(self):
        self.assertEqual(beacon_origin.consume(None), (None, "absent"))
        self.assertEqual(beacon_origin.consume(""), (None, "absent"))

    def test_a_nonce_that_is_not_a_nonce_never_reaches_the_filesystem(self):
        """THE FORMAT CHECK IS A PATH GUARD. The nonce arrives in an
        environment variable and is joined into a path, so a separator or a
        dot segment must be refused before `open` ever sees it."""
        # UNCONDITIONAL CONTROL, outside the loop: a well-formed nonce takes
        # a DIFFERENT branch, so "malformed_nonce" below is the guard
        # answering rather than the only answer this function has.
        self.assertEqual(beacon_origin.consume("a" * beacon_origin.NONCE_LEN)[1],
                         "consumed_or_absent")
        short = "".join("%x" % i for i in range(16))          # a real hex word, but half the required length
        for bad in ("../../etc/passwd", "a/b", "." * 32, "A" * 32,
                    short, "g" * 32, short * 3):
            with self.subTest(bad=bad):
                stamp, reason = beacon_origin.consume(bad)
                self.assertIsNone(stamp)
                self.assertEqual(reason, "malformed_nonce")

    def test_a_stamp_that_cannot_be_DESTROYED_is_not_a_stamp(self):
        """SINGLE-USE IS A PROPERTY OF THE REMOVAL, so a failed removal is a
        refusal rather than a warning.

        Swallowing the unlink error left the stamp on disk after a SUCCESSFUL
        read, so a later reader could consume it again — single-use degrading
        silently to best-effort exactly when the filesystem is behaving
        adversarially, which is the only circumstance in which it matters. A
        stamp that outlives one read is a second PROVEN waiting to happen, and
        fail-closed is this module's law.

        THE CONTROL IS THE SAME STAMP WITH THE SAME CALL and a working unlink:
        it is consumed and returns its origin, so the refusal below is the
        removal failing rather than this fixture being unreadable."""
        nonce = self.mint(now=1000.0)
        stamp, reason = beacon_origin.consume(nonce, session="S", now=1000.0)
        self.assertIsNotNone(stamp, "control: a destroyable stamp IS consumed")
        self.assertIsNone(reason)

        nonce = self.mint(now=1000.0)
        real_unlink = os.unlink

        def refusing_unlink(path):
            raise OSError(30, "Read-only file system")

        os.unlink = refusing_unlink
        try:
            stamp, reason = beacon_origin.consume(
                nonce, session="S", now=1000.0)
        finally:
            os.unlink = real_unlink

        self.assertIsNone(stamp,
                          "the bytes were readable and the origin was real — "
                          "it is refused because it could not be destroyed")
        self.assertEqual(reason, "unconsumable")

        # AND THE REFUSAL IS NOT A ONE-SHOT SIDE EFFECT: the stamp is still
        # on disk, which is exactly why crediting the first reader would have
        # been wrong. It stays readable until `reap` takes it.
        self.assertTrue(os.path.exists(
            beacon_origin._stamp_path(nonce)),
            "the stamp survived, which is the world this refusal is about")

    def test_a_STALE_stamp_is_refused_and_says_so(self):
        nonce = self.mint(now=1000.0)
        stamp, reason = beacon_origin.consume(
            nonce, session="S", now=1000.0 + beacon_origin.STAMP_TTL_S + 1)
        self.assertIsNone(stamp)
        self.assertEqual(reason, "stale")

    def test_a_stamp_from_the_FUTURE_is_refused_too(self):
        """A negative age is a clock that moved, not a fresh stamp, and
        crediting it would make a stale stamp usable forever."""
        nonce = self.mint(now=5000.0)
        stamp, reason = beacon_origin.consume(nonce, session="S", now=1000.0)
        self.assertIsNone(stamp)
        self.assertEqual(reason, "stale")

    def test_a_stamp_minted_in_ANOTHER_session_is_a_CONFLICT_not_an_absence(self):
        """Named apart on purpose: a census must be able to tell a missing
        producer from a crossed one, and only one of those is a bug."""
        nonce = self.mint(session="OTHER")
        stamp, reason = beacon_origin.consume(nonce, session="MINE", now=1001.0)
        self.assertIsNone(stamp)
        self.assertEqual(reason, "session_conflict")

    def test_a_MALFORMED_stamp_file_is_refused(self):
        nonce = "b" * 32
        os.makedirs(beacon_origin.stamp_dir(), exist_ok=True)
        with open(os.path.join(beacon_origin.stamp_dir(), "%s.json" % nonce),
                  "w", encoding="utf-8") as f:
            f.write("{not json")
        stamp, reason = beacon_origin.consume(nonce, session="S", now=1.0)
        self.assertIsNone(stamp)
        self.assertEqual(reason, "malformed")

    def test_a_stamp_claiming_an_ORIGIN_THAT_IS_NOT_ONE_is_refused(self):
        """The file is other-process-supplied data. Its `origin` is a CLAIM and
        is re-checked against the closed set on the way out, not trusted
        because mint would have refused it on the way in."""
        import json
        nonce = "c" * 32
        os.makedirs(beacon_origin.stamp_dir(), exist_ok=True)
        with open(os.path.join(beacon_origin.stamp_dir(), "%s.json" % nonce),
                  "w", encoding="utf-8") as f:
            json.dump({"origin": "unknown", "tool_use_id": "t",
                       "session": "S", "minted": 1.0}, f)
        stamp, reason = beacon_origin.consume(nonce, session="S", now=2.0)
        self.assertIsNone(stamp)
        self.assertEqual(reason, "malformed")

    def test_a_MALFORMED_stamp_is_still_CONSUMED(self):
        """The unlink precedes the parse so that two racing readers cannot both
        succeed. A malformed file that survived its own read would be retried
        forever by every census pass."""
        nonce = "d" * 32
        os.makedirs(beacon_origin.stamp_dir(), exist_ok=True)
        path = os.path.join(beacon_origin.stamp_dir(), "%s.json" % nonce)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertTrue(os.path.exists(path))       # positive control
        beacon_origin.consume(nonce, session="S", now=1.0)
        self.assertFalse(os.path.exists(path))


class TheRowAlwaysCarriesAnOriginTest(StampBase):
    def test_a_refusal_writes_UNKNOWN_and_the_reason_rather_than_omitting(self):
        """A row that omitted the field on failure would read exactly like a
        row written by a helm that predates this module. Those are different
        facts: origin asked and not established, versus origin never asked."""
        fields = beacon_origin.row_fields(None, "stale")
        self.assertEqual(fields["origin"], beacon_origin.ORIGIN_UNKNOWN)
        self.assertEqual(fields["origin_reason"], "stale")

    def test_a_success_carries_the_origin_and_the_binding(self):
        nonce = self.mint(beacon_origin.ORIGIN_SUBAGENT, "toolu_xyz")
        stamp, reason = beacon_origin.consume(nonce, session="S", now=1001.0)
        fields = beacon_origin.row_fields(stamp, reason)
        self.assertEqual(fields["origin"], beacon_origin.ORIGIN_SUBAGENT)
        self.assertIsNone(fields["origin_reason"])
        self.assertEqual(fields["origin_tool_use"], "toolu_xyz")

    def test_UNKNOWN_is_never_one_of_the_two_real_answers(self):
        """Pinned because every consumer branches on this: UNKNOWN must not be
        admissible anywhere ORIGINS is checked, or 'no stamp' would silently
        become an attribution."""
        self.assertIn(beacon_origin.ORIGIN_MAIN, beacon_origin.ORIGINS)
        self.assertIn(beacon_origin.ORIGIN_SUBAGENT, beacon_origin.ORIGINS)
        self.assertNotIn(beacon_origin.ORIGIN_UNKNOWN, beacon_origin.ORIGINS)
        self.assertEqual(beacon_origin.ORIGINS,
                         frozenset((beacon_origin.ORIGIN_MAIN,
                                    beacon_origin.ORIGIN_SUBAGENT)))


class FromEnvTest(StampBase):
    def test_the_waiters_one_call_reads_and_consumes_its_own_environment(self):
        nonce = self.mint(beacon_origin.ORIGIN_SUBAGENT, "toolu_env")
        env = {beacon_origin.STAMP_ENV: nonce}
        stamp, reason = beacon_origin.from_env(env, session="S", now=1001.0)
        self.assertIsNone(reason)
        self.assertEqual(stamp["origin"], beacon_origin.ORIGIN_SUBAGENT)
        again, reason2 = beacon_origin.from_env(env, session="S", now=1002.0)
        self.assertIsNone(again)
        self.assertEqual(reason2, "consumed_or_absent")

    def test_an_environment_with_no_stamp_is_ABSENT_not_an_error(self):
        """The overwhelmingly common case today, and it must stay quiet: no
        producer is installed, so every beacon is unattributed and nothing is
        alarming about that."""
        stamp, reason = beacon_origin.from_env({}, session="S", now=1.0)
        self.assertIsNone(stamp)
        self.assertEqual(reason, "absent")


class ReapTest(StampBase):
    def test_an_UNCONSUMED_stamp_is_the_normal_case_and_is_reaped(self):
        """The producer stamps a command that may register no waiter at all —
        the fail-closed direction — so unconsumed stamps accumulate by design
        and must be swept."""
        nonce = self.mint(now=1000.0)
        path = os.path.join(beacon_origin.stamp_dir(), "%s.json" % nonce)
        self.assertTrue(os.path.exists(path))       # positive control
        os.utime(path, (0, 0))
        self.assertEqual(beacon_origin.reap(now=10 ** 6), 1)
        self.assertFalse(os.path.exists(path))

    def test_a_FRESH_stamp_survives_the_reaper(self):
        nonce = self.mint(now=1000.0)
        path = os.path.join(beacon_origin.stamp_dir(), "%s.json" % nonce)
        self.assertEqual(beacon_origin.reap(now=os.stat(path).st_mtime), 0)
        self.assertTrue(os.path.exists(path))

    def test_the_reaper_answers_zero_when_there_is_no_directory(self):
        self.assertFalse(os.path.isdir(beacon_origin.stamp_dir()))
        self.assertEqual(beacon_origin.reap(now=10 ** 6), 0)
        # POSITIVE CONTROL ON THE SAME OBSERVABLE: this reaper returns a
        # NON-zero count when there is something to take, so the zero above
        # is about the missing directory and not about the reaper.
        nonce = self.mint(now=1000.0)
        os.utime(os.path.join(beacon_origin.stamp_dir(), "%s.json" % nonce),
                 (0, 0))
        self.assertEqual(beacon_origin.reap(now=10 ** 6), 1)


if __name__ == "__main__":
    unittest.main()
