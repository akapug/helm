#!/usr/bin/env python3
"""A ring says each sentence of instruction once per waiter (task/3382).

THE SYMPTOM. Measured over 24 h of a local seat's transcript: its 222 beacon
rings repeated one trailer, how rows clear (the bulk ack, or a read) and when
the doorbell rings again, for 361k characters. A ring is a new turn, and a
seat with a small window paid for those sentences on every one of them.

THE CONTRACT these arms pin, through the real waiter entry point
(DoorbellBase: `helm chat wait --seat gemini --follow` on a scratch home):

  * the FIRST ring of a waiter process carries every sentence, byte for byte
    what every ring carried before;
  * a LATER ring of the same waiter carries the facts and nothing it already
    said: the lead row, the waiting count, the exact pull, the unread split
    and the count new since the last ring;
  * a sentence the waiter has not said yet (an @all-only ring after an
    addressed one) is said once, on the first ring it applies to;
  * a new waiter (a restart, `--replace`) says every sentence again;
  * the same waiter says every sentence again on each RESAY_EVERY-th ring
    after its first, and on the first ring after its seat compacts its own
    thread or joins under a new session, because the waiter outlives the
    context it told.

Each arm reads a brief ring and a full ring from the same run, so a waiter
that says nothing at all, or says everything every time, fails it.
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import beacon_doorbell, chat, seats, seats_join  # noqa: E402
from tests._tmphome import declaring as _tmp_declaring  # noqa: E402
from tests.test_beacon_doorbell import SEAT, SID, DoorbellBase  # noqa: E402

#: The two sentences of instruction, as the first ring of a waiter says them.
ACK = ("rows you already handled clear in ONE call, helm chat ack <id> "
       "<id> …, one row for them all; the rest: pull, and read what is "
       "addressed first")
READS = ("a row addressed to no one, an @all row, a reaction or room "
         "chatter, clears by reading, not by ack, so pull, and read what is "
         "addressed first")
AGAIN = ("it rings again when a new row lands, or in 12 min while a row it "
         "only counted stays unread")
#: A brief ring's tail: the facts, then the closing parenthesis.
BRIEF = re.compile(r" \(\+(\d+) waiting — (helm chat read [^·()]*) · "
                   r"doorbell: (\d+) unread = ([^·()]*) · (\d+) new since "
                   r"the last ring\)$")


class _RingBase(DoorbellBase):

    def facts(self, line):
        """(waiting, pull, unread, split, new) of a BRIEF ring, or a failure
        naming the line."""
        m = BRIEF.search(line)
        self.assertIsNotNone(m, "not a brief ring:\n" + line)
        return (int(m.group(1)), m.group(2), int(m.group(3)), m.group(4),
                int(m.group(5)))

    def assertFull(self, line, *sentences):
        """`line` ends with every sentence a waiter's first ring says, in the
        order and the joining a ring has always used."""
        self.assertTrue(line.endswith(" · %s)" % "; ".join(sentences)),
                        "not a full ring:\n" + line)
        self.assertTrue(beacon_doorbell._TAIL.search(line), line)

    def assertSaysNone(self, line):
        for sentence in (ACK, READS, AGAIN):
            self.assertNotIn(sentence, line)
        self.assertTrue(beacon_doorbell._TAIL.search(line),
                        "the brief tail no longer strips as fixed text: "
                        + line)


class LaterRingTest(_RingBase):

    def test_n_waiting_a_later_ring_keeps_every_fact_and_drops_the_sentences(self):
        """RED before the cure: the later ring repeated both sentences.
        MEASURED: the characters a later ring saves are printed on stderr."""
        pa = chat.post("@gemini pending a", who="bob")["id"]
        pb = chat.post("@gemini pending b", who="bob")["id"]
        late = []

        def script(n, _now):
            if n == 2:
                late.append(chat.post("@gemini late 1", who="carol")["id"])
                late.append(chat.post("@gemini late 2", who="carol")["id"])
        lines, _seen = self.follow(passes=40, script=script, clock=True)
        self.assertEqual(len(lines), 2, lines)
        first, later = lines
        self.assertFull(first, ACK, AGAIN)
        pull = "helm chat read --id %s,%s" % (pb, pa)
        self.assertIn("(+1 waiting — %s · doorbell: 2 unread = 2 addressed, "
                      "0 DM, 0 @all · 2 new since the last ring · " % pull,
                      first)
        self.assertSaysNone(later)
        self.assertIn("] carol: @gemini late 2 (+3 waiting", later)
        again = "helm chat read --id %s,%s,%s,%s" % (late[1], late[0], pb, pa)
        self.assertEqual(self.facts(later), (
            3, again, 4, "4 addressed, 0 DM, 0 @all", 2))
        said = len(" · %s; %s" % (ACK, AGAIN))
        self.assertEqual(len(later), len(first) - said
                         + len(" late 2") - len(" pending b")
                         + len("carol") - len("bob")
                         + len(again) - len(pull),
                         "the later ring dropped more than the sentences")
        sys.stderr.write("\nMEASURED ring chars: first %d, later %d; the "
                         "sentences a later ring no longer says: %d\n"
                         % (len(first), len(later), said))

    def test_zero_waiting_a_later_ring_is_the_row_and_its_facts(self):
        chat.post("@gemini only one", who="bob")
        nxt = []

        def script(n, _now):
            if n == 2:
                self.hook_pass()
            elif n == 3:
                nxt.append(chat.post("@gemini the next one", who="carol")["id"])
        lines, _seen = self.follow(passes=40, script=script, clock=True)
        self.assertEqual(len(lines), 2, lines)
        self.assertFull(lines[0], ACK, AGAIN)
        self.assertIn("] bob: @gemini only one (+0 waiting", lines[0])
        self.assertSaysNone(lines[1])
        self.assertEqual(self.facts(lines[1]), (
            0, "helm chat read --id %s" % nxt[0], 1,
            "1 addressed, 0 DM, 0 @all", 1))

    def test_a_dm_later_ring_names_the_dm_pull_and_no_sentence(self):  # noqa: VACUOUS_ASSERTION — assertFull on the first ring and facts() on the later ring are unconditional positive controls on the same waiter's lines; the absence sits in the assertSaysNone helper
        dms = [seats.dm(SEAT, "direct ask 1", who="bob")[0]["id"]]

        def script(n, _now):
            if n == 3:
                dms.append(seats.dm(SEAT, "direct ask 2", who="carol")[0]["id"])
        lines, _seen = self.follow(passes=6, script=script, clock=True)
        self.assertEqual(len(lines), 2, lines)
        self.assertFull(lines[0], ACK, AGAIN)
        self.assertSaysNone(lines[1])
        waiting, pull, unread, split, new = self.facts(lines[1])
        self.assertEqual((waiting, unread, split, new),
                         (1, 2, "0 addressed, 2 DM, 0 @all", 1))
        self.assertEqual(pull, "helm chat read --id %s,%s" % (dms[1], dms[0]))
        self.assertIn("helm chat read --id %s · " % dms[0], lines[0],
                      "the first ring named its one DM")

    def test_an_all_ring_says_its_own_sentence_once_and_never_again(self):
        """The first ring was an addressed one, so the @all ring that follows
        says the sentence an addressed ring does not ("clears by reading"),
        once, and not the one already said; a third ring says neither."""
        chat.post("@gemini addressed first", who="bob")

        def script(n, _now):
            if n == 2:
                self.hook_pass()
            elif n == 3:
                chat.post("@all standup one", who="carol")
            elif n == 40:
                chat.post("@all standup two", who="carol")
        lines, _seen = self.follow(passes=80, script=script, clock=True)
        self.assertEqual(len(lines), 3, lines)
        self.assertFull(lines[0], ACK, AGAIN)
        self.assertTrue(lines[1].endswith(" · %s)" % READS), lines[1])
        self.assertNotIn(AGAIN, lines[1])
        self.assertIn("1 @all", lines[1])
        self.assertSaysNone(lines[2])
        self.assertEqual(self.facts(lines[2])[2:4],
                         (2, "0 addressed, 0 DM, 2 @all"))


class NewWaiterTest(_RingBase):

    def test_a_restarted_waiter_says_every_sentence_again(self):
        """Two waiter processes in turn: each one's first ring is full, and
        the first waiter's later ring is the brief control."""
        chat.post("@gemini before", who="bob")

        def script(n, _now):
            if n == 3:
                chat.post("@gemini during", who="carol")
        first, _seen = self.follow(passes=40, script=script, clock=True)
        self.assertEqual(len(first), 2, first)
        self.assertFull(first[0], ACK, AGAIN)
        self.assertSaysNone(first[1])
        chat.post("@gemini after the restart", who="daria")
        again, _seen = self.follow(passes=2, clock=True)
        self.assertEqual(len(again), 1, again)
        self.assertFull(again[0], ACK, AGAIN)
        self.assertIn("doorbell: 3 unread", again[0])

    def test_a_replace_rotation_says_every_sentence_again(self):
        chat.post("@gemini before", who="bob")

        def script(n, _now):
            if n == 3:
                chat.post("@gemini during", who="carol")
        first, _seen = self.follow(passes=40, script=script, clock=True)
        self.assertEqual(len(first), 2, first)
        self.assertSaysNone(first[1])
        chat.post("@all after the rotation", who="daria")
        again, _seen = self.follow(passes=2, flags=("--replace",),
                                   clock=True)
        self.assertEqual(len(again), 1, (again, self.stderr))
        self.assertFull(again[0], ACK, AGAIN)


class AWaiterOutlivesTheContextTest(_RingBase):
    """What a waiter told lives in the waiter process, and the process
    outlives the seat's context: a compaction or a new session empties the
    context of every sentence, and a long day of rings drifts past them. So
    a waiter says every sentence again on each RESAY_EVERY-th ring after its
    first (rings 1, 11, 21, ...), and on the first ring after its seat
    reports a compaction of its own thread (the PreCompact record
    `resumeturn.note_precompact` writes) or a new session (the roster's
    `session`, which the SessionStart join moves). A DM rings at once, so
    each arm posts one DM per pass and reads one ring per DM."""

    def dm_each_pass(self, upto, also=None):
        """A DM before the waiter arms and one after each of its first
        `upto` passes; `also(n)` runs first after pass n -> the script."""
        seats.dm(SEAT, "direct ask 0", who="bob")

        def script(n, _now):
            if also:
                also(n)
            if n <= upto:
                seats.dm(SEAT, "direct ask %d" % n, who="bob")
        return script

    def full(self, lines):
        """The 1-based numbers of the rings that said every sentence, each
        one checked whole, and every other ring checked brief."""
        out = []
        for k, line in enumerate(lines, 1):
            if AGAIN in line:
                self.assertFull(line, ACK, AGAIN)
                out.append(k)
            else:
                self.assertSaysNone(line)
        return out

    def test_every_tenth_ring_after_the_first_says_every_sentence(self):
        """RED at 3b9a8f48cfd: ring 11 was as brief as ring 2, so a seat
        whose context had moved on never read the sentences again."""
        lines, _seen = self.follow(passes=14, script=self.dm_each_pass(11),
                                   clock=True)
        self.assertEqual(len(lines), 12, lines)
        self.assertEqual(self.full(lines), [1, 11])
        self.assertEqual(beacon_doorbell.RESAY_EVERY, 10)

    def test_a_compaction_of_the_seats_thread_says_every_sentence(self):
        """RED at 3b9a8f48cfd: the ring after the seat compacted was brief.
        A subagent's compaction (its agent id on the record) leaves the
        seat's context as it was, the control: that ring stays brief."""
        from helm import resumeturn

        def compact(n):
            if n == 2:
                resumeturn.note_precompact(SID, "auto", agent="agent-a1")
            elif n == 3:
                resumeturn.note_precompact(SID, "manual")
        lines, _seen = self.follow(passes=6,
                                   script=self.dm_each_pass(4, compact),
                                   clock=True)
        self.assertEqual(len(lines), 5, (lines, self.stderr))
        self.assertEqual(self.full(lines), [1, 4])

    def test_a_seat_named_in_another_case_reads_its_own_compaction(self):
        """The PreCompact record is keyed by the seat's declared name
        (`resumeturn.compaction_key(own_name(), ...)`), so a waiter armed
        with --seat in another case reads it under the declared spelling.
        RED at 41f8d8ad857 (R2 of the approval-tier read there): it looked
        under its own spelling, found nothing, and never heard its seat
        compact."""
        from helm import resumeturn
        with _tmp_declaring(["--seat", SEAT]):
            resumeturn.note_precompact(SID, "manual")
            own = beacon_doorbell.context_epoch(SEAT, SID)
            other = beacon_doorbell.context_epoch(SEAT.upper(), SID)
        self.assertIsNotNone(own and own[1],
                             "control: the record under the declared name")
        self.assertEqual(other, own)

    def test_a_new_session_of_the_seat_says_every_sentence(self):
        """RED at 3b9a8f48cfd: the ring after the seat's pane joined under a
        new session was brief."""
        new = "sess-new-%s" % SEAT

        def rejoin(n):
            if n == 2:
                with mock.patch.dict(os.environ,
                                     {"CLAUDE_CODE_SESSION_ID": new}):
                    seats.join(session=new, seat=SEAT, cwd="/tmp/p",
                               room="main")
        lines, _seen = self.follow(passes=5,
                                   script=self.dm_each_pass(3, rejoin),
                                   clock=True)
        self.assertEqual(len(lines), 4, (lines, self.stderr))
        self.assertEqual(self.full(lines), [1, 3])


class AFailedStreamTest(_RingBase):
    """A sentence is told once the ring carrying it reached the stream. A
    ring whose write raised told the seat nothing, so the retry says every
    sentence the failed ring would have said."""

    def test_a_ring_the_stream_refused_is_said_whole_on_the_retry(self):
        """RED before the cure: the failed ring had already added its
        sentences to `told`, so the retry, the first ring the seat saw, was
        brief."""
        chat.post("@gemini pending a", who="bob")
        real, calls = seats_join._emit_line, []

        def flaky(line):
            calls.append(line)
            if len(calls) == 1:
                raise OSError(5, "the pane went away")
            real(line)

        def script(n, _now):
            if n == 4:
                chat.post("@gemini late", who="carol")
        with mock.patch.object(seats_join, "_emit_line", flaky):
            lines, _seen = self.follow(passes=50, script=script, clock=True)
        self.assertIn("beacon doorbell fault (OSError", self.stderr)
        self.assertEqual(len(calls), 3, calls)
        self.assertFull(calls[0], ACK, AGAIN)
        self.assertEqual(lines, calls[1:])
        self.assertFull(lines[0], ACK, AGAIN)
        self.assertIn("] bob: @gemini pending a (+0 waiting", lines[0])
        self.assertSaysNone(lines[1])
        self.assertIn("] carol: @gemini late (+1 waiting", lines[1])


if __name__ == "__main__":
    unittest.main()
