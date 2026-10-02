#!/usr/bin/env python3
"""task/3899: the seat's CHECK-IN, the 15five pulse shape on `helm seat mood set`.

A seat answers in one word, and may add a 1-5 rating of how its work is
going, one blocker line and one win line. A BLOCKER is posted ONCE to #seats
with the seat's steward @mentioned (its project's lead, else the build-lanes
steward), so the steward answers it; the same words never post twice. A win
is recorded only. The rating joins the honesty signal: a seat that rates its
work 4 or 5 while helm measures it stuck or grinding is DIVERGENT.

Every world is a temp HELM_HOME and a temp chat dir. The #seats leg is a
recorder: nothing reaches a real room, the chat node or a phone.
"""
import contextlib
import io
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-seatmood-pulse-", var="HELM_HOME")

from helm import pk, seatevents  # noqa: E402
# the seat facade beside any seat impl the arms reach (the co-occurrence law)
from helm import seat  # noqa: E402,F401

SEAT = "mood-seat"
LEAD = "proj-lead"
INTEGRATOR = "lanes-steward"
NOW = 1_790_000_000.0
MIN = 60


def _sm():
    from helm import seatmood
    return seatmood


def _pulse():
    from helm import seatmood_pulse
    return seatmood_pulse


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seatmood-pulse-case-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "helm"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_CHAT_NAME": SEAT,
            "HELM_NTFY_TOPIC": "", "MELD_NTFY_TOPIC": ""})
        env.start()
        self.addCleanup(env.stop)
        self.posts = []
        for p in (
                mock.patch.object(seatevents, "_chat_post",
                                  side_effect=lambda text, rid:
                                  self.posts.append(text)),
                mock.patch.object(seatevents, "_owner_push",
                                  side_effect=AssertionError(
                                      "a check-in never reaches the phone")),
                mock.patch("helm.seats_integrator.integrator_seat",
                           return_value=(INTEGRATOR, None)),
                mock.patch("helm.actors.resolve_actor",
                           return_value=(types.SimpleNamespace(
                               canonical_name=SEAT), None))):
            p.start()
            self.addCleanup(p.stop)
        roster = mock.patch.object(_sm(), "_roster_key",
                                   side_effect=lambda n: (n, None))
        roster.start()
        self.addCleanup(roster.stop)

    def project(self, name):
        """The seat serves `name` (None: no project), and `name`'s team has
        LEAD as its one lead."""
        team = {"members": [{"seat": LEAD, "role": "lead"},
                            {"seat": SEAT, "role": "worker"}]}
        return contextlib.ExitStack(), [
            mock.patch.object(seatevents, "project_of",
                              return_value={SEAT: name}),
            mock.patch("helm.teams.read", return_value=team)]

    def run_cli(self, *args, project=None):
        from helm import cli
        out, err = io.StringIO(), io.StringIO()
        stack, patches = self.project(project)
        with stack:
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                try:
                    rc = cli.main(["seat", "mood"] + list(args))
                except SystemExit as exc:
                    rc = exc.code
        return rc, out.getvalue(), err.getvalue()


class CheckInTest(Base):
    def test_set_records_the_rating_the_blocker_and_the_win(self):
        rc, out, err = self.run_cli(
            "set", "grinding", "--why", "fab queue is slow", "--rating", "2",
            "--blocker", "the fab runner refuses my lane",
            "--win", "landed the pulse arms", project="proj")
        self.assertEqual(rc, 0, err)
        said = _sm().self_report(SEAT)
        self.assertEqual(
            (said["word"], said["why"], said["rating"], said["blocker"],
             said["win"]),
            ("grinding", "fab queue is slow", 2,
             "the fab runner refuses my lane", "landed the pulse arms"))
        rows, why = _sm().history()
        self.assertIsNone(why)
        self.assertEqual([(r["rating"], r["blocker"], r["win"]) for r in rows],
                         [(2, "the fab runner refuses my lane",
                           "landed the pulse arms")])
        self.assertIn("2/5", out)
        self.assertIn("posted to #seats", out)

    def test_a_blocker_posts_once_mentioning_the_project_lead(self):
        rc, _out, err = self.run_cli("set", "stuck", "--blocker",
                                     "the brief names no lane path",
                                     project="proj")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.posts), 1, self.posts)
        text = self.posts[0]
        self.assertTrue(text.startswith("@%s " % LEAD), text)
        self.assertIn("%s is blocked: the brief names no lane path" % SEAT,
                      text)
        # the SAME words again post nothing: one blocker, one row
        rc, out, err = self.run_cli("set", "stuck", "--blocker",
                                    "the  brief names no lane PATH",
                                    project="proj")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.posts), 1, self.posts)
        self.assertIn("already posted", out)
        # a NEW blocker is a new row
        self.run_cli("set", "stuck", "--blocker", "fab is down",
                     project="proj")
        self.assertEqual(len(self.posts), 2, self.posts)

    def test_a_seat_with_no_project_wakes_the_build_lanes_steward(self):
        rc, _out, err = self.run_cli("set", "stuck", "--blocker",
                                     "no reviewer answers", project=None)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0].startswith("@%s " % INTEGRATOR),
                        self.posts[0])

    def test_the_lead_itself_wakes_the_build_lanes_steward(self):
        """A project lead's own blocker does not mention itself."""
        p = _pulse()
        with mock.patch.object(seatevents, "project_of",
                               return_value={LEAD: "proj"}), \
                mock.patch("helm.teams.read", return_value={
                    "members": [{"seat": LEAD, "role": "lead"}]}):
            self.assertEqual(p.subject(LEAD), ("build-lanes", None))
            self.assertEqual(p.subject(SEAT.upper()), ("build-lanes", None))
        with mock.patch.object(seatevents, "project_of",
                               return_value={SEAT: "proj"}), \
                mock.patch("helm.teams.read", return_value={
                    "members": [{"seat": LEAD, "role": "lead"}]}):
            self.assertEqual(p.subject(SEAT), ("project-seats", "proj"))

    def test_a_win_alone_is_recorded_and_posts_nothing(self):  # noqa: VACUOUS_ASSERTION — the blocker arms above post through the same recorder; here the empty list is the claim and the recorded win is the positive control
        rc, out, err = self.run_cli("set", "flowing", "--win",
                                    "the gate went green", project="proj")
        self.assertEqual(rc, 0, err)
        self.assertEqual(_sm().self_report(SEAT)["win"], "the gate went green")
        self.assertEqual(self.posts, [])
        self.assertNotIn("#seats", out)

    def test_a_bad_check_in_records_nothing_and_posts_nothing(self):  # noqa: VACUOUS_ASSERTION — every bad argv is asserted rc 2 first; the record and post arms above drive the same self_report and recorder to non-empty
        for args in (("--rating", "0"), ("--rating", "6"), ("--rating", "x"),
                     ("--rating", "3.5"), ("--blocker", "two\nlines"),
                     ("--blocker", "   "), ("--win", "x" * 201),
                     ("--rating",)):
            rc, _out, err = self.run_cli("set", "stuck", *args,
                                         project="proj")
            self.assertEqual(rc, 2, (args, err))
        self.assertIsNone(_sm().self_report(SEAT))
        self.assertEqual(self.posts, [])

    def test_check_names_each_refusal(self):
        p = _pulse()
        self.assertEqual(p.check("4", None, None),
                         ({"rating": 4, "blocker": None, "win": None}, None))
        self.assertEqual(p.check(5, " a  b ", "c")[0],
                         {"rating": 5, "blocker": "a b", "win": "c"})
        for bad in (True, 0, 6, "05", "", "4 "):
            fields, refusal = p.check(bad, None, None)
            self.assertIsNone(fields, bad)
            self.assertIn("1 to 5", refusal)

    def test_a_failed_room_post_says_it_is_owed(self):
        with mock.patch.object(seatevents, "_chat_post",
                               side_effect=OSError("chat node down")):
            rc, out, err = self.run_cli("set", "stuck", "--blocker",
                                        "the chat node is down",
                                        project="proj")
        self.assertEqual(rc, 0, err)
        self.assertIn("owed", out)
        self.assertEqual(_sm().self_report(SEAT)["blocker"],
                         "the chat node is down")
        # the owed row goes out on the next seat event, once
        rc, _out, _err = self.run_cli("set", "stuck", project="proj")
        self.assertEqual(rc, 0)
        seatevents.announce([], now=None)
        self.assertEqual(len(self.posts), 1, self.posts)


class RatingHonestyTest(unittest.TestCase):
    """The rating is the structured half of the honesty signal."""

    def judged(self, said, refusals=4):
        sm = _sm()
        raw = {"seat": SEAT, "refusals": [
            {"at": NOW - (4 - i) * MIN, "guard": "author-gate",
             "reason": "PreToolUse:Bash"} for i in range(refusals)],
            "counters": {}, "idle": {"state": "BUSY", "idle_s": None,
                                     "turn_opened": NOW - MIN, "why": "x"},
            "progress": None, "wall": None, "card": None,
            "context_pct": None, "self": said, "unread": []}
        return sm.judge(raw, NOW)

    def test_a_high_rating_beside_a_stuck_measure_diverges(self):
        got = self.judged({"word": "grinding", "why": None, "at": NOW - MIN,
                           "rating": 5, "blocker": None, "win": None})
        self.assertEqual(got["state"], "stuck")
        self.assertIs(got["divergent"], True)
        self.assertEqual(got["checkin"]["rating"], 5)

    def test_a_low_rating_is_honest_whatever_the_word(self):
        got = self.judged({"word": "fine", "why": None, "at": NOW - MIN,
                           "rating": 1, "blocker": "x", "win": None})
        self.assertIs(got["divergent"], False)
        self.assertEqual(got["checkin"]["blocker"], "x")

    def test_no_rating_falls_back_to_the_word(self):  # noqa: VACUOUS_ASSERTION — the first assertion is the positive True on the same judge; the False is the zero-refusal pole
        said = {"word": "fine", "why": None, "at": NOW - MIN}
        self.assertIs(self.judged(said)["divergent"], True)
        self.assertIs(self.judged(said, refusals=0)["divergent"], False)


if __name__ == "__main__":
    unittest.main()
