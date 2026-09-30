#!/usr/bin/env python3
"""The SSE doorbell lists the chat directory once per NEW LOG, not per tick
(task/3519).

MEASURED with py-spy on the owner's laptop: `helm web` burned about 35% of a
core while a browser had the console open. `_chat_fingerprint` cached the
room-log names keyed on the chat directory's mtime, but the per-session state
beside the logs (cursors, locks, stop-whisper files) is created and replaced
several times a second, so the directory moved on nearly every 250ms tick and
every tick listed it. The live directory held 34,058 entries of which 305
were room logs.

These arms plant thousands of state files beside a few logs and keep the
state churning: the listing must not follow it, while a room or a DM lane
the chat writer creates still reaches the doorbell on the very next tick.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-sse-listing-", var="HELM_HOME")

from helm import chat, web  # noqa: E402

ENV_KEYS = ("HELM_CHAT_DIR", "HELM_CHAT_NODE_URL")
STATE_FILES = 3000


def _row(text="r", dm=False):
    row = {"from": "alice", "text": text, "ts": "2026-09-28T00:00:00Z"}
    if dm:
        row["dm"] = 1
    return row


class LogListingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-sse-listing-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore)
        os.environ["HELM_CHAT_DIR"] = self.d = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        chat._ensure_dir()
        self.dm = os.path.join(self.d, "dm")
        for room in ("main", "helm", "meld-1-topic"):
            chat._append_once(_row(), room)
        chat._append_once(_row(dm=True), "dm-bob-1234")
        # THE DEBRIS, in the shapes the live directory holds: per-session
        # cursors and their beacon twins, locks, stop-whisper state
        for i in range(STATE_FILES // 3):
            for name in ("main.cursor.seat-%d.%08x" % (i, i),
                         ".beacon-main.cursor.seat-%d.%08x" % (i, i),
                         "main.stopwhisper.seat-%d" % i):
                with open(os.path.join(self.d, name), "w") as f:
                    f.write('{"off": 0}\n')
        web._CHAT_NAMES.clear()
        self.addCleanup(web._CHAT_NAMES.clear)
        # COUNTED ONLY INSIDE A TICK: the chat writer driven between ticks
        # may list the directory for its own reasons, and those are not the
        # doorbell's cost
        self.calls, self.ticking = [], False
        real = os.listdir

        def counting(path="."):
            if self.ticking:
                self.calls.append(os.path.abspath(path))
            return real(path)
        patcher = mock.patch("os.listdir", side_effect=counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fp(self):
        self.ticking = True
        try:
            return web._chat_fingerprint()
        finally:
            self.ticking = False

    def listings(self, base):
        return self.calls.count(os.path.abspath(base))

    def churn(self, i):
        """What the live directory does between two ticks: a new session
        cursor appears and an existing one is replaced by rename."""
        new = os.path.join(self.d, "helm.cursor.seat-x.%08x" % (0x100000 + i))
        with open(new, "w") as f:
            f.write('{"off": 1}\n')
        old = os.path.join(self.d, "main.cursor.seat-%d.%08x" % (i, i))
        with open(old + ".tmp", "w") as f:
            f.write('{"off": 2}\n')
        os.replace(old + ".tmp", old)

    def test_state_churn_never_relists_the_directory(self):
        fp0 = self.fp()
        self.assertIsNotNone(fp0)
        # MUST-HIT CONTROL: the first tick DOES list, once per base dir
        self.assertEqual(self.listings(self.d), 1, self.calls)
        self.assertEqual(self.listings(self.dm), 1, self.calls)
        for i in range(40):
            before = os.stat(self.d).st_mtime_ns
            self.churn(i)
            # the premise of the whole bug: the state churn MOVES the dir
            if os.stat(self.d).st_mtime_ns == before:
                os.utime(self.d, ns=(before + 1, before + 1))
            self.assertEqual(self.fp(), fp0,
                             "state churn rang the doorbell")
        self.assertEqual(self.listings(self.d), 1,
                         "40 ticks of state churn re-listed the directory")
        self.assertEqual(self.listings(self.dm), 1)
        # AND AN APPEND STILL RINGS with the names cached
        chat._append_once(_row("more"), "main")
        self.assertNotEqual(self.fp(), fp0)
        self.assertEqual(self.listings(self.d), 1, "an append re-listed")

    def test_a_new_room_is_seen_on_the_next_tick_for_one_listing(self):
        fp0 = self.fp()
        self.churn(0)
        self.assertEqual(self.fp(), fp0)
        chat._append_once(_row("hello"), "brand-new-room")
        fp1 = self.fp()
        self.assertNotEqual(fp1, fp0, "a new room did not ring")
        self.assertEqual(self.listings(self.d), 2,
                         "a new room must cost exactly one listing")
        # the new room is IN the cached names: its next append rings with no
        # further listing, however much state churns around it
        for i in range(1, 20):
            self.churn(i)
            self.assertEqual(self.fp(), fp1)
        chat._append_once(_row("again"), "brand-new-room")
        self.assertNotEqual(self.fp(), fp1)
        self.assertEqual(self.listings(self.d), 2)

    def test_a_new_dm_lane_is_seen_on_the_next_tick(self):
        fp0 = self.fp()
        dm_before = self.listings(self.dm)
        chat._append_once(_row("psst", dm=True), "dm-carol-5678")
        fp1 = self.fp()
        self.assertNotEqual(fp1, fp0, "a new DM lane did not ring")
        self.assertEqual(self.listings(self.dm), dm_before + 1)
        chat._append_once(_row("psst again", dm=True), "dm-carol-5678")
        self.assertNotEqual(self.fp(), fp1)
        self.assertEqual(self.listings(self.dm), dm_before + 1)

    def test_an_append_to_an_existing_room_does_not_move_the_generation(self):
        """Only a room log's CREATE moves the signal. A writer that bumped it
        on every post would put the listing back on the hot path."""
        gen0 = chat.rooms_generation()
        chat._append_once(_row("one more"), "main")
        chat._append_once(_row("dm more", dm=True), "dm-bob-1234")
        self.assertEqual(chat.rooms_generation(), gen0)
        chat._append_once(_row("new"), "another-new-room")
        self.assertNotEqual(chat.rooms_generation(), gen0)

    def test_a_bump_never_shows_a_reader_an_empty_token(self):
        """A bump that truncates the token file and then writes shows a
        reader b"" in between, and leaves it there if the write fails. The
        token is replaced whole: a reader sees the old one or the new one."""
        gen0 = chat.rooms_generation()
        self.assertTrue(gen0)
        seen, real = [], os.urandom

        def mid(n):
            seen.append(chat.rooms_generation())
            return real(n)
        with mock.patch("helm.chat.os.urandom", side_effect=mid):
            chat.bump_rooms_generation()
        self.assertEqual(seen, [gen0], "a reader saw the token mid-bump")
        gen1 = chat.rooms_generation()
        self.assertNotIn(gen1, (b"", gen0))
        with mock.patch("helm.chat.os.urandom", side_effect=OSError("x")):
            chat.bump_rooms_generation()
        self.assertEqual(chat.rooms_generation(), gen1,
                         "a failed bump emptied the token")
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".tmp")],
                         [], "a bump left its temporary behind")

    def test_an_unannounced_log_is_seen_within_the_age_backstop(self):
        """A writer running code from before the generation file (a beacon
        runs the code it armed with) creates a log without bumping it. The
        30s age bound is what catches that one."""
        fp0 = self.fp()
        with open(os.path.join(self.d, "old-writer.jsonl"), "a") as f:
            f.write('{"from": "x", "text": "r"}\n')
        self.assertEqual(self.fp(), fp0,
                         "must-hit: the raw create was expected to be unseen")
        stamp, at, names = web._CHAT_NAMES[self.d]
        web._CHAT_NAMES[self.d] = (stamp, at - web._NAMES_MAX_AGE_S - 1,
                                   names)
        self.assertNotEqual(self.fp(), fp0)
        self.assertEqual(self.listings(self.d), 2)


if __name__ == "__main__":
    unittest.main()
