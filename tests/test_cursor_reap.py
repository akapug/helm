#!/usr/bin/env python3
"""Per-session read cursors must be reaped when their SESSION dies.

THE LIFETIME MISMATCH: a cursor is created per (room, seat, SESSION) but the
only reaper was keyed on the SEAT, so a seat surviving many sessions leaked one
cursor per session forever. Measured on the live fleet 2026-07-25: 24,349
cursors across 520 sessions, NINE alive — 99% garbage. It costs LATENCY, not
just disk, because list_rooms scans the directory to find 26 rooms among 25,094
entries and seats._scan_rooms calls it on every tool boundary.

The dangerous direction is over-reaping: dropping a LIVE session's cursor makes
that seat re-read its whole room and re-deliver everything it already saw. So
these pin liveness-before-age, and that an unprovable liveness keeps EVERYTHING.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import chat                                            # noqa: E402


class CursorReapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-reap-")
        self.prior = os.environ.get("HELM_CHAT_DIR")
        os.environ["HELM_CHAT_DIR"] = self.tmp

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_CHAT_DIR", None)
        else:
            os.environ["HELM_CHAT_DIR"] = self.prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cursor(self, room, seat, sid):
        p = os.path.join(self.tmp, "%s.cursor.%s.%s" % (room, seat, sid))
        open(p, "w").write("42\n")
        open(p + ".lock", "w").close()
        return p

    # --- the dangerous direction, first -------------------------------------

    def test_a_live_sessions_cursor_is_never_dropped(self):
        keep = self._cursor("main", "codex-aaaa", "1111beef")
        drop = self._cursor("main", "codex-aaaa", "dead0001")
        chat._reap_for_test(live={"1111beef-full-uuid-rest"})
        self.assertTrue(os.path.exists(keep), "dropped a LIVE session's cursor")
        self.assertFalse(os.path.exists(drop))

    def test_a_short_sid_matches_its_full_session_id_by_prefix(self):
        """A filename carries the SHORT sid; live_sids returns FULL uuids. An
        equality test would call every live session dead and reap everything."""
        keep = self._cursor("helm", "kimi-bbbb", "0fa7c4ed")
        chat._reap_for_test(live={"0fa7c4ed-9a5e-46a0-b2da-f862eb8afad6"})
        self.assertTrue(os.path.exists(keep))

    def test_unprovable_liveness_keeps_everything(self):
        """An unknown session is not a dead one. If liveness cannot be
        established the reaper must touch NOTHING and say why."""
        c = self._cursor("main", "codex-aaaa", "dead0001")
        victims, kept, err = chat._reap_for_test(live=set())
        # empty live set is still a PROVEN answer; the unprovable case is an
        # exception inside live_sids, exercised below
        self.assertTrue(len(victims) >= 1 or kept >= 1)
        import helm.sessions as S
        real = S.live_sids
        S.live_sids = lambda: (_ for _ in ()).throw(RuntimeError("no homes"))
        try:
            c2 = self._cursor("main", "codex-aaaa", "dead0002")
            victims, kept, err = chat._reap_for_test()
            self.assertEqual((victims, kept), ([], 0))
            self.assertIsNotNone(err)
            self.assertTrue(os.path.exists(c2), "reaped without proving death")
        finally:
            S.live_sids = real

    def test_a_live_sessions_LONG_FORM_cursor_is_kept(self):
        """The case the first implementation got wrong. Filenames do not agree
        on sid length — measured live: 7,480 cursors with 8 chars, 3,613 with
        23, a tail to 34. Truncating only the LIVE side and testing
        `live.startswith(sid)` can never match a 23-char sid, so a live session
        holding a long-form cursor read as DEAD. It was safe on the day only
        because no live session happened to have one. Normalise BOTH sides."""
        full = "0fa7c4ed-9a5e-46a0-b2da-f862eb8afad6"
        long_form = self._cursor("main", "codex-aaaa", full[:23])
        short = self._cursor("main", "codex-aaaa", full[:8])
        chat._reap_for_test(live={full})
        self.assertTrue(os.path.exists(long_form),
                        "reaped a LIVE session's long-form cursor")
        self.assertTrue(os.path.exists(short))

    # --- the ordinary direction ---------------------------------------------

    def test_dead_cursors_and_their_lock_siblings_both_go(self):
        c = self._cursor("main", "codex-aaaa", "dead0001")
        chat._reap_for_test(live={"9999ffff-x"})
        self.assertFalse(os.path.exists(c))
        self.assertFalse(os.path.exists(c + ".lock"),
                         "the .lock sibling is half the directory entries")

    def test_identifying_never_deletes(self):
        """dead_cursors() only NAMES victims — `helm gc` does the deleting, via
        the same _reap every other retention stream uses. Two actuators for one
        kind of file is how you get two policies and a divergence nobody sees."""
        c = self._cursor("main", "codex-aaaa", "dead0001")
        victims, _kept, _e = chat.dead_cursors(live={"9999ffff-x"})
        self.assertEqual(len(victims), 2)   # the cursor AND its lock sibling
        self.assertTrue(os.path.exists(c), "identifying must not delete")

    def test_non_cursor_files_are_never_touched(self):
        room = os.path.join(self.tmp, "main.jsonl")
        open(room, "w").write("{}\n")
        seen = os.path.join(self.tmp, ".seen.codex-aaaa")
        open(seen, "w").close()
        chat._reap_for_test(live=set())
        self.assertTrue(os.path.exists(room), "reaped a ROOM")
        self.assertTrue(os.path.exists(seen))

    def test_reaping_shrinks_what_list_rooms_must_scan(self):
        """The point of the whole exercise: list_rooms finds rooms by scanning
        the directory, so dead cursors are a tax on every tool boundary."""
        open(os.path.join(self.tmp, "main.jsonl"), "w").write("{}\n")
        for i in range(200):
            self._cursor("main", "codex-aaaa", "dead%04d" % i)
        before = len(os.listdir(self.tmp))
        chat._reap_for_test(live=set())
        after = len(os.listdir(self.tmp))
        self.assertLess(after, before / 10)
        self.assertEqual(chat.list_rooms(), ["main"])


if __name__ == "__main__":
    unittest.main()
