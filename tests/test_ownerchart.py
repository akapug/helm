#!/usr/bin/env python3
"""The operator's progress chart — a rendering surface, so every arm here asks
what a READER would see, not whether a function returned.

The chart exists to keep fleet dialect away from someone who does not speak it,
which makes a passing render() a weak claim: the interesting failures are a
chart that fires while the operator is PRESENT, a chart that leaks a sha, and a
bar that reads finished when nothing verified it. Each arm below is paired with
the input it must REJECT.
"""
import calendar
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from helm import ownerchart, seats_stop_seam


class AwayIsReadNeverInferred(unittest.TestCase):
    def test_the_marker_decides_in_both_directions(self):
        """away() is a file test and nothing else — no clock, no activity."""
        d = tempfile.mkdtemp()
        marker = os.path.join(d, "owner-away")
        # MUST-MISS: absent marker is PRESENT-not-away. Getting this wrong
        # fires the chart at a man who is typing, which is the whole failure
        # the only-when-AFK condition exists to prevent.
        self.assertFalse(ownerchart.away(marker))
        with open(marker, "w") as fh:
            fh.write("1")
        self.assertTrue(ownerchart.away(marker))
        os.remove(marker)
        self.assertFalse(ownerchart.away(marker))

    def test_the_module_writes_no_marker_and_reads_no_clock(self):
        """One sentinel, one reader. The /afk skill bans a second away-state
        mechanism by name, and an inferred posture IS one."""
        with io.open(ownerchart.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("def away", src)                       # control
        for banned in ("open(away_marker", "os.mknod", "time.time()",
                       "datetime.now", "last_post", "idle_for"):
            self.assertNotIn(banned, src,
                             "away must be READ from the marker, not written "
                             "or inferred: found %r" % banned)


class WhatHasBeenWaitingAndHowLong(unittest.TestCase):
    """The dogfood run found a row that had been blocked on the owner for 22
    days rendering identically to one filed that minute. These arms exist so
    that cannot come back."""

    # A fixed clock, because a test that reads the real one measures the day
    # it runs on. 2026-08-26T09:00:00Z.
    NOW = calendar.timegm(time.strptime("2026-08-26T09:00:00", "%Y-%m-%dT%H:%M:%S"))

    def test_both_stamp_shapes_the_board_actually_uses(self):
        """MEASURED on the live board: one row stamps seconds and another
        omits them. A parser that knows one shape drops the age off the OLDER
        rows — precisely the ones worth showing."""
        with_secs = ownerchart._age("2026-08-04T19:15:00Z", self.NOW)
        without = ownerchart._age("2026-08-04T19:15Z", self.NOW)
        self.assertEqual(with_secs, without)
        self.assertIn("21 days", with_secs)
        # MUST-MISS: an unparseable or absent stamp yields NO age. An invented
        # age on a broken row is worse than a blank, because it reads as fact.
        for bad in ("", None, "sometime last week", "not-a-date", 17):
            self.assertEqual(ownerchart._age(bad, self.NOW), "",
                             "invented an age from %r" % (bad,))
        # MUST-MISS: no clock means no age claim, never a guessed one.
        self.assertEqual(ownerchart._age("2026-08-04T19:15Z", None), "")
        # MUST-MISS: a future stamp is a broken row, not a negative age.
        self.assertEqual(ownerchart._age("2027-01-01T00:00Z", self.NOW), "")

    def test_the_oldest_block_is_listed_first_and_carries_its_age(self):
        board = {"tasks": [{"t": "x", "stage": "📡 LIVE"}],
                 "owner_gated_queue": [
                     {"ask": "filed just now", "state": "waiting-owner",
                      "since": "2026-08-26T08:55:00Z"},
                     {"ask": "blocked for three weeks", "state": "waiting-owner",
                      "since": "2026-08-04T19:15Z"}]}
        out = ownerchart.render(board, now=self.NOW)
        self.assertIn("21 days", out)
        self.assertIn("5 min", out)
        self.assertLess(out.index("blocked for three weeks"),
                        out.index("filed just now"),
                        "the oldest block must be read first, not last")

    def test_a_decide_row_shows_the_choices_not_the_preamble(self):
        """A decide card's why-line is a context paragraph followed by an
        options tail. Clipping from the front keeps the prose and discards the
        choices, which is backwards — the choices are what he can act on."""
        board = {"tasks": [{"t": "x", "stage": "📡 LIVE"}],
                 "owner_gated_queue": [{
                     "ask": "decide: something",
                     "state": "waiting-owner-decision",
                     "since": "2026-08-26T08:00:00Z",
                     "why": "A long preamble that would eat the whole line and "
                            "tell him nothing he can answer — options: "
                            "leave it | wire the bot* | overrule the skill"}]}
        out = ownerchart.render(board, now=self.NOW)
        self.assertIn("wire the bot", out)
        # MUST-MISS: the preamble must NOT be what survives the clip.
        self.assertNotIn("A long preamble", out)

    def test_freshness_is_shown_only_when_the_caller_supplies_it(self):
        board = {"tasks": [{"t": "x", "stage": "📡 LIVE"}]}
        dated = ownerchart.render(board, now=self.NOW,
                                  as_of=self.NOW - 3 * 86400)
        self.assertIn("3 days ago", dated)
        # MUST-MISS: no mtime means NO freshness claim. Twelve full bars over
        # a silently stale board is the failure this line exists to prevent,
        # and a fabricated date would cause it rather than cure it.
        blind = ownerchart.render(board, now=self.NOW)
        self.assertTrue(blind)                       # control: it still renders
        self.assertNotIn("ago", blind)


class ItNeverSpendsMoreAttentionThanItIsWorth(unittest.TestCase):
    """An uncapped push surface spends the attention it exists to serve."""

    def _many(self, n, stage="\U0001f4e1 LIVE"):
        return {"tasks": [{"t": "piece %d" % i, "stage": stage}
                          for i in range(n)],
                "owner_gated_queue": [
                    {"ask": "block %02d" % i, "state": "waiting-owner",
                     "since": "2026-08-%02dT00:00Z" % (i + 1)}
                    for i in range(n)]}

    def test_a_huge_board_does_not_become_a_huge_chart(self):
        out = ownerchart.render(self._many(60))
        # control: it still renders, and renders real rows
        self.assertIn("piece 0", out)
        self.assertLess(len(out.splitlines()), 45,
                        "the chart grew without bound; brief caps its own "
                        "render at ~40 lines for the same reason")

    def test_every_dropped_row_is_counted_out_loud(self):
        """Silent truncation reads as 'that is everything' — the same lie the
        missing age told, and this chart exists because of that lie."""
        out = ownerchart.render(self._many(60))
        self.assertIn("more pieces not shown", out)
        self.assertIn("more things waiting on you not shown", out)
        # MUST-MISS: a board that FITS must carry no fold line at all. A
        # remainder notice on a complete list is its own small lie.
        small = ownerchart.render(self._many(2))
        self.assertIn("piece 0", small)                  # control
        self.assertNotIn("not shown", small)

    def test_unfinished_work_wins_the_space_over_finished_work(self):
        """When the cap bites, a screen of full bars is the least useful thing
        it could keep."""
        board = {"tasks": [{"t": "done piece %d" % i, "stage": "\U0001f4e1 LIVE"}
                           for i in range(30)]
                 + [{"t": "THE UNFINISHED ONE", "stage": "\U0001f528 building"}]}
        out = ownerchart.render(board)
        self.assertIn("THE UNFINISHED ONE", out)


class ItRidesTheCleanStopAndOnlyTheCleanStop(unittest.TestCase):
    """The seam arm the review required, and the reason it is required: this
    was the one property I could only assert by READING _emit. A code read
    cannot tell you that stop three still speaks."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.marker = os.path.join(self.dir, "owner-away")
        # PATCH WHAT IS ACTUALLY READ. This used to patch
        # ownerchart.away_marker, which is an ALIAS nobody consults: ownerchart.away() calls away.is_away, which resolves
        # through away.marker_path. So the double was never in effect and the
        # arm was measuring the real resolver while claiming a fixture — the
        # same shape as a fake in sys.modules that `from . import X` bypasses.
        from helm import away as _away
        real = _away.marker_path
        marker = self.marker
        _away.marker_path = lambda: marker
        # RESTORED EVEN IF THIS TEST DIES. A module attribute left patched
        # leaks into every later test in the process, and the damage is
        # invisible from inside the method that caused it.
        self.addCleanup(setattr, _away, "marker_path", real)

        # AND THE BOARD MUST BE OURS, WHICH THE GATE TAUGHT ME AND MY OWN
        # PROBE COULD NOT. These arms passed on my box and failed on the fab,
        # because _owner_chart reads board.path() and fab steers HELM_HOME
        # into a per-run tree where the board is EMPTY — so render() returned
        # None, correctly, and the chart never appeared. The arm was silently
        # measuring my machine's live board. A fixture board makes it test the
        # SEAM (does a clean stop carry the chart) instead of testing whether
        # the host happens to have work on it.
        from helm import board as _board
        self.board = os.path.join(self.dir, "board.json")
        with io.open(self.board, "w", encoding="utf-8") as fh:
            json.dump({"tasks": [{"t": "a piece of work",
                                  "stage": "\U0001f4e1 LIVE"}]}, fh)
        real_path = _board.path
        board_file = self.board
        _board.path = lambda *a, **k: board_file
        self.addCleanup(setattr, _board, "path", real_path)

    def _stop(self):
        buf = io.StringIO()
        seats_stop_seam.emit_warns(["[helm stop-guard] an unrelated advisory"],
                                   stream=buf)
        return buf.getvalue()

    def test_it_speaks_on_every_clean_stop_not_just_the_first(self):
        """A once-per-arrangement latch would make stop two silent. The chart
        is a plain appended string, never an armed disclosure, so
        commit_disclosures has nothing to consume — this arm is what keeps
        that true if someone later arms it."""
        open(self.marker, "w").close()
        first, second, third = self._stop(), self._stop(), self._stop()
        for n, out in (("first", first), ("second", second), ("third", third)):
            self.assertIn("WHERE THE WORK IS", out, "stop %s went silent" % n)
        # CONTROLS ON THE SAME VARIABLES THE EQUALITY USES. Without these,
        # second == third == "" satisfies the comparison perfectly — the
        # assertions above are about `out`, a different name, so they cannot
        # rescue this one. Two silent stops are equal to each other.
        self.assertTrue(second.strip(), "stop two emitted nothing at all")
        self.assertTrue(third.strip(), "stop three emitted nothing at all")
        self.assertEqual(second, third, "the chart drifted between stops")

    def test_not_away_means_no_chart_and_the_advisory_still_prints(self):
        out = self._stop()
        # CONTROL: an empty stream would satisfy the assertion below for the
        # wrong reason, so prove the seam spoke at all.
        self.assertIn("an unrelated advisory", out)
        self.assertNotIn("WHERE THE WORK IS", out)

    def test_the_refusal_exit_never_carries_it(self):
        """Every exit-2 emission renders as a red "Stop hook error:", so a
        dashboard stapled to a blocker is the loudest possible mistake. This
        holds because emit_blocks does not call _owner_chart — a property of
        which function you are in, not a rule at a branch."""
        open(self.marker, "w").close()
        buf = io.StringIO()
        seats_stop_seam.emit_blocks(["[helm stop-guard] a blocker"], stream=buf)
        out = buf.getvalue()
        self.assertIn("a blocker", out)          # control: it did emit
        self.assertNotIn("WHERE THE WORK IS", out)

    def test_removing_the_flag_stops_it_the_very_next_stop(self):
        open(self.marker, "w").close()
        while_away = self._stop()
        self.assertIn("WHERE THE WORK IS", while_away)
        os.remove(self.marker)
        after = self._stop()
        # THE CONTROL RIDES THE SAME VARIABLE AS THE ABSENCE. `after` must be
        # a real emission that simply lacks the chart — not a dead stream,
        # which would satisfy assertNotIn for entirely the wrong reason.
        self.assertIn("an unrelated advisory", after)
        self.assertNotIn("WHERE THE WORK IS", after)


class ItCannotReachTheLiveFlagFromAnIsolatedEnv(unittest.TestCase):
    def test_helm_home_redirects_the_marker_completely(self):
        """chat.py:222 states the law this inherits — "isolation that skips the
        identity surface is not isolation". Because away_marker() derives from
        chat.chat_dir(), a redirected HELM_HOME moves the flag with it. The
        literal path this module started with had none of that: it would have
        punched straight through any isolation to the live /dev/shm flag."""
        d = tempfile.mkdtemp()
        code = (
            "import sys, os; sys.path.insert(0, %r)\n"
            "from helm import ownerchart\n"
            "p = ownerchart.away_marker()\n"
            "print(p)\n"
            "print(ownerchart.away())\n"
            "print(os.path.isdir(os.path.dirname(p)))\n"
        ) % os.path.dirname(os.path.dirname(os.path.abspath(ownerchart.__file__)))
        env = dict(os.environ, HELM_HOME=os.path.join(d, "home"))
        env.pop("HELM_CHAT_DIR", None)
        r = subprocess.run([sys.executable, "-c", code], env=env, cwd=d,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        path, away, made_dir = r.stdout.split("\n")[:3]
        self.assertTrue(path.startswith(os.path.join(d, "home")),
                        "the marker escaped the redirected HELM_HOME: %s" % path)
        # MUST-MISS: the literal fallback must NOT win when HELM_HOME is set,
        # or every isolated test silently reads the live fleet's away flag.
        self.assertNotEqual(path, "/dev/shm/helm-chat/owner-away")
        self.assertEqual(away, "False")
        # READS, NEVER WRITES — the seam calls this on every clean stop of
        # every seat, so a resolver that mkdir-ed would write to shared tmpfs
        # thousands of times a day for a feature meant to be inert.
        self.assertEqual(made_dir, "False", "resolving the marker created a dir")


class TheChartSpeaksPlainly(unittest.TestCase):
    def _board(self, **over):
        b = {"tasks": [{"t": "a class of bug -> 0ff1ce5 / badca11",
                        "stage": "\U0001f4e1 LIVE"}],
             "building": [{"lane": "lr-delivery-leg", "seat": "seat-a"}],
             "owner_gated_queue": [
                 {"ask": "a provider login the fleet cannot do",
                  "state": "waiting-owner",
                  "why": "grant dead at 19:15Z"}]}
        b.update(over)
        return b

    def test_shas_and_arrow_tails_never_reach_him(self):
        """MEASURED on the live board: a majority of task titles end in an arrow
        followed by one to three abbreviated commit hashes. The hex below is
        SYNTHETIC and deliberately resolves to no object (tests/ is public and
        the docref rung refuses real citations) — the stripper cares about the
        SHAPE, and a renderer that passes that shape through defeats the one
        thing the chart is for."""
        out = ownerchart.render(self._board())
        self.assertIn("a class of bug", out)          # control: title survives
        for leak in ("0ff1ce5", "badca11", "->"):
            self.assertNotIn(leak, out, "sha/arrow leaked to the owner: %r" % out)

    def test_an_unknown_stage_reads_NOT_STARTED_not_finished(self):
        """A stage word this code does not recognise is UNKNOWN progress, and
        the honest rendering of unknown is empty. An unrecognised word must
        never inherit the confident end of the scale."""
        out = ownerchart.render(self._board(
            tasks=[{"t": "a piece", "stage": "✨ marvellous"}]))
        self.assertIn("not started", out)
        self.assertNotIn("done", out)

    def test_live_fills_the_bar_and_building_only_half(self):
        """The standing rule: a full bar means VERIFIED LIVE, not built."""
        live = ownerchart.render(self._board(
            tasks=[{"t": "x", "stage": "\U0001f4e1 LIVE"}]))
        mid = ownerchart.render(self._board(
            tasks=[{"t": "x", "stage": "\U0001f528 building"}]))
        self.assertIn(ownerchart._FULL * ownerchart._WIDTH, live)
        self.assertNotIn(ownerchart._FULL * ownerchart._WIDTH, mid)
        self.assertIn(ownerchart._HALF, mid)

    def test_an_empty_board_wakes_him_for_nothing(self):
        # noqa: VACUOUS_ASSERTION — returning None IS the contract here (say
        # nothing rather than render an empty frame), and the unconditional
        # positive control on the same observable is the populated render on
        # the very next line: it proves render() can speak at all.
        populated = ownerchart.render(self._board())
        self.assertTrue(populated)        # control: this board DOES render
        self.assertIsNone(ownerchart.render({}))
        self.assertIsNone(ownerchart.render({"tasks": [], "owner_gated_queue": []}))

    def test_a_board_of_ONLY_IN_FLIGHT_work_still_renders(self):
        """THE POSITIVE POLE FOR THE GUARD, and the one an enumerating guard
        forgets: in-flight work is the state where an operator most wants to
        see that something is moving, and a guard that lists sections has to
        list all of them."""
        out = ownerchart.render({"building": [{"lane": "a-lane",
                                               "seat": "a-seat"}]})
        self.assertTrue(out, "a board with only in-flight work rendered nothing")
        self.assertIn("a-seat", out)

    def test_a_board_of_ONLY_MALFORMED_in_flight_rows_wakes_him_for_NOTHING(self):
        """The negative pole of the same guard, and the reason it is derived
        from what RENDERS rather than from what was supplied. The section
        below only draws rows carrying BOTH a lane and a seat, so a board of
        rows missing either passes a guard that counts raw dicts and produces
        a body with no sections in it — a chart that wakes him to say nothing.
        Paired with the arm above so neither is satisfied by a renderer that
        always answers None."""
        self.assertTrue(ownerchart.render(
            {"building": [{"lane": "a-lane", "seat": "a-seat"}]}))   # control
        for malformed in ([{"lane": "a-lane"}],
                          [{"seat": "a-seat"}],
                          [{"lane": "", "seat": ""}],
                          ["not-a-dict"]):
            self.assertIsNone(ownerchart.render({"building": malformed}),
                              "rendered a body for %r" % (malformed,))

    def test_he_is_told_the_VERB_that_lifts_it_not_the_file(self):
        """THIS ARM USED TO ASSERT THE OPPOSITE AND WAS RIGHT TO, WHICH IS THE
        interesting part. When nothing wrote the flag, the only honest
        off-switch was `rm <path>`, and the arm banned naming a verb helm did
        not have. The owner then overruled the /afk skill and `helm back`
        exists — so the old assertion became a pin holding a stale world in
        place, and the fix is to invert it rather than delete it.

        AND THE VERB IS NOT MERELY NICER: `rm` clears the flag SILENTLY, while
        `helm back` answers what it did and says when his fleet notice is
        still standing (no chat row is posted by either; the flag is the only
        away state, and each seat's next turn reads it). Printing the raw
        path would hand him the one route that says nothing."""
        out = ownerchart.render(self._board())
        self.assertIn("marked away", out)
        self.assertIn("helm back", out)
        # MUST-MISS: the raw path must NOT be what he is told to reach for.
        self.assertNotIn("rm ", out)
        self.assertNotIn(ownerchart.away_marker(), out)

    def test_what_waits_on_him_is_a_named_section(self):
        """The chart doubles as the scheduling instrument — what blocks on the
        owner has to be visible AS his, not mixed into the work list."""
        out = ownerchart.render(self._board())
        self.assertIn("WAITING ON YOU", out)
        self.assertIn("a provider login the fleet cannot do", out)
        # MUST-MISS: an informational row is not something waiting on him.
        quiet = ownerchart.render(self._board(
            owner_gated_queue=[{"ask": "fyi thing", "state": "fyi"}]))
        self.assertNotIn("fyi thing", quiet)


def tearDownModule():
    """A stop that armed a surviving disclosure and never emitted it leaves
    the text queued for whatever refuses next in this process, which is
    another module's stop; drain it the way an interrupted response does
    (task/3039: the slice runner's data audit named it)."""
    from helm import seats_stop_seam
    seats_stop_seam.fallback_lines(())


if __name__ == "__main__":
    unittest.main()
