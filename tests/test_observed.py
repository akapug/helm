#!/usr/bin/env python3
"""A land is done when someone named has seen it working (helm/observed.py).

THE CONTRACT THESE ARMS PIN:
  O1 a whole land no longer closes its task: the task reads LANDED and
     names ONE check owner, the seat that filed it, and the land step's
     line @mentions that seat with the exact command;
  O2 `helm task observed <id> --evidence TEXT` closes it, recording the
     evidence and the seat that recorded it; no evidence, nothing closes;
  O3 a dark, longtail or unknown filer falls back to the task's lead, then
     to the integrator;
  O4 a check older than 24h moves to the fallback owner ONCE, with one
     @mention, and stays there;
  O5 the seat that owes checks hears so at its stop, once per owed set
     (the stop whisper), and a seat that owes none hears nothing.
The train-never-blocked arm and the lead-posture restart rules are in
tests/test_autoland.py.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, landtask, observed, tasks  # noqa: E402
from tests.test_tasks import CliBase  # noqa: E402

SHA = "5e" * 20
TIP = "7a" * 20
DAY = 24 * 3600


class ObservedBase(CliBase):
    """A scratch task ledger and a seat world each arm states: who is on the
    roster, who is dark, who is a longtail seat, the project's lead and the
    integrator."""

    def setUp(self):
        super().setUp()
        self.world(roster=("asker-seat", "lead-seat", "integrator-seat",
                           "worker-seat", "checker-seat"))
        self.posts = []

    def world(self, roster=(), dark=(), longtail=(), lead="lead-seat",
              integrator="integrator-seat"):
        for name, fn in (
                ("_roster", lambda: set(roster)),
                ("_dark", lambda seat: "UNAVAILABLE claude AUTH-401"
                 if seat in dark else None),
                ("_role", lambda seat: "worker" if seat in longtail
                 else None),
                ("_lead", lambda project: (lead, None) if lead
                 else (None, "no lead")),
                ("_integrator", lambda: integrator)):
            patch = mock.patch.object(observed, name, fn)
            patch.start()
            self.addCleanup(patch.stop)

    def file(self, title="the whole ask", source="asker-seat"):
        row, why = tasks.add(title, "builder-seat", project="helm",
                             source=source, force_new=True)
        self.assertIsNone(why, why)
        return row["id"]

    def land(self, tid, by="auto-land", restart=None):
        return landtask.run({
            "task": tid, "why": None, "whole": True, "lane": "lane-a",
            "tip": TIP, "row": "d1" * 8, "author": "builder-seat",
            "label": "LAND 9" if by == "auto-land" else None, "sha": SHA,
            "scope": "helm", "chain": None, "chain_rows": [],
            "lane_rows": [], "by": by, "live": not restart,
            "restart": restart}, post=self.room_post)

    def room_post(self, room, text, key):
        """The task room as `chat.post` keeps it: one row per event key."""
        if key not in {k for _r, _t, k in self.posts}:
            self.posts.append((room, text, key))

    def post(self, text):
        self.posts.append(text)
        return None


class AWholeLandOwesACheckTest(ObservedBase):
    """O1."""

    def test_a_whole_land_no_longer_closes_the_task_and_names_the_owner(self):
        tid = self.file()
        report = self.land(tid)
        row = tasks.get(tid)
        self.assertEqual(row["status"], "open")
        self.assertEqual(report["action"], landtask.LANDED)
        rec = row["landed"]
        self.assertEqual((rec["owner"], rec["role"], rec["land"], rec["sha"]),
                         ("asker-seat", "requester", "LAND 9", SHA))
        line = "\n".join(landtask.lines(report))
        self.assertIn("%s LANDED" % tid, line)
        self.assertIn("@asker-seat", line)
        self.assertIn('helm task observed %s --evidence "' % tid, line)
        # the task says it too, and the land step reads its own note back
        (said,) = [c["text"] for c in tasks.comments_of(row)]
        self.assertIn("owes a seen-working check by @asker-seat", said)
        self.assertTrue(landtask.parse_comment(said)["whole"])
        rc, out, err = self.cli("show", tid)
        self.assertEqual(rc, 0, err)
        self.assertIn("landed — owes a seen-working check by @asker-seat",
                      out)

    def test_a_landed_task_owing_its_check_is_offered_to_nobody(self):
        tid = self.file()
        offered = lambda **kw: [o[5]["id"] for o in tasks.offer_rows(**kw)]
        # CONTROL: before the land the open task is in the offer pool.
        self.assertIn(tid, offered(seat="idle-seat", live=()))
        self.land(tid)
        self.assertNotIn(tid, offered(seat="idle-seat", live=()))
        self.assertNotIn(tid, offered(seat="builder-seat",
                                      live=("builder-seat",)))

    def test_the_land_step_again_stamps_nothing_twice(self):  # noqa: VACUOUS_ASSERTION — the LANDED action and exactly one comment are asserted after two runs
        tid = self.file()
        self.land(tid)
        again = self.land(tid)
        self.assertEqual(again["action"], landtask.LANDED)
        self.assertEqual(len(tasks.comments_of(tasks.get(tid))), 1)

    def test_a_hand_land_tells_the_owner_in_the_task_room_once(self):
        tid = self.file()
        self.land(tid, by="land-step")
        self.land(tid, by="land-step")
        (room, text, _key), = self.posts
        self.assertTrue(text.startswith("@asker-seat %s " % tid), text)
        self.assertIn("helm task observed %s" % tid, text)

    def test_a_land_owing_a_relaunch_says_it_is_not_live(self):
        tid = self.file()
        report = self.land(tid, restart="the lead settings: relaunch the "
                           "leads; the land is not LIVE until they do")
        self.assertIn("not LIVE until", "\n".join(landtask.lines(report)))
        self.assertIn("relaunch the leads", tasks.get(tid)["landed"]
                      ["restart"])


class TheObservedVerbTest(ObservedBase):
    """O2."""

    def test_the_observed_verb_closes_it_and_records_evidence_and_recorder(
            self):
        tid = self.file()
        self.land(tid)
        with mock.patch.object(dispatches, "acting_author",
                               return_value=("checker-seat", None)):
            rc, out, err = self.cli("observed", tid, "--evidence",
                                    "ran helm brief on the live box")
        self.assertEqual(rc, 0, err)
        row = tasks.get(tid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual((row["observed"]["evidence"], row["observed"]["by"]),
                         ("ran helm brief on the live box", "checker-seat"))
        self.assertIn("seen working: ran helm brief on the live box",
                      row["closed_reason"])
        self.assertIn("@checker-seat", row["closed_reason"])
        self.assertIn("%s closed" % tid, out)

    def test_no_evidence_closes_nothing(self):
        tid = self.file()
        self.land(tid)
        with mock.patch.object(dispatches, "acting_author",
                               return_value=("checker-seat", None)):
            for argv in (("observed", tid), ("observed", tid, "--evidence"),
                         ("observed", tid, "--evidence", "   ")):
                with self.subTest(argv=argv):
                    rc, _out, err = self.cli(*argv)
                    self.assertEqual(rc, 2)
                    self.assertIn("--evidence", err)
        self.assertEqual(tasks.get(tid)["status"], "open")

    def test_a_task_no_land_stamped_is_refused(self):
        tid = self.file()
        row, err = observed.observe(tid, "saw it", "checker-seat")
        self.assertIsNone(row)
        self.assertIn("owes no seen-working check", err)
        self.assertEqual(tasks.get(tid)["status"], "open")

    def test_confirm_close_points_at_the_observed_verb(self):
        from helm import taskhygiene
        tid = self.file()
        self.land(tid)
        row, err = taskhygiene.confirm_close(tid, "asker-seat")
        self.assertIsNone(row)
        self.assertIn("helm task observed %s" % tid, err)
        self.assertEqual(tasks.get(tid)["status"], "open")


class TheOwnerFallsBackTest(ObservedBase):
    """O3."""

    def owner(self, **world):
        self.world(**world)
        tid = self.file()
        self.land(tid)
        rec = tasks.get(tid)["landed"]
        return rec["owner"], rec["role"]

    def test_a_dark_asker_falls_back_to_the_lead(self):
        self.assertEqual(self.owner(roster=("asker-seat", "lead-seat"),
                                    dark=("asker-seat",)),
                         ("lead-seat", "lead"))

    def test_a_longtail_asker_falls_back_to_the_lead(self):
        self.assertEqual(self.owner(roster=("asker-seat", "lead-seat"),
                                    longtail=("asker-seat",)),
                         ("lead-seat", "lead"))

    def test_an_unknown_asker_and_a_dark_lead_fall_back_to_the_integrator(
            self):
        self.assertEqual(self.owner(roster=("lead-seat",),
                                    dark=("lead-seat",)),
                         ("integrator-seat", "integrator"))

    def test_a_fit_asker_keeps_it(self):
        self.assertEqual(self.owner(roster=("asker-seat", "lead-seat")),
                         ("asker-seat", "requester"))


class TheCheckMovesOnceTest(ObservedBase):
    """O4."""

    def test_the_24h_reassignment_happens_once(self):
        tid = self.file()
        self.land(tid)
        t0 = tasks.get(tid)["landed"]["ts"]
        self.assertEqual(observed.sweep(post=self.post, now=t0 + DAY - 60),
                         [])
        self.assertEqual(self.posts, [])
        (moved,) = observed.sweep(post=self.post, now=t0 + DAY + 60)
        self.assertEqual((moved["task"], moved["from"], moved["to"]),
                         (tid, "asker-seat", "lead-seat"))
        (text,) = self.posts
        self.assertTrue(text.startswith("@lead-seat "), text)
        self.assertIn("helm task observed %s" % tid, text)
        rec = tasks.get(tid)["landed"]
        self.assertEqual((rec["owner"], rec["moved"]["from"]),
                         ("lead-seat", "asker-seat"))
        # it stays there: a later sweep moves nothing and says nothing
        self.assertEqual(observed.sweep(post=self.post,
                                        now=t0 + 3 * DAY), [])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(tasks.get(tid)["landed"]["owner"], "lead-seat")

    def test_an_integrator_owned_check_has_nowhere_to_move(self):  # noqa: VACUOUS_ASSERTION — the integrator owner is asserted exactly; no move and no post are the point
        self.world(roster=())
        tid = self.file()
        self.land(tid)
        self.assertEqual(tasks.get(tid)["landed"]["owner"], "integrator-seat")
        t0 = tasks.get(tid)["landed"]["ts"]
        self.assertEqual(observed.sweep(post=self.post, now=t0 + 2 * DAY),
                         [])
        self.assertEqual(self.posts, [])

    def test_the_pile_counts_landed_not_observed_and_its_age(self):
        first, second = self.file("one"), self.file("two")
        self.land(first)
        self.land(second)
        now = tasks.get(first)["landed"]["ts"] + 2 * DAY
        got = observed.pile(now=now)
        self.assertEqual((got["n"], got["over"]), (2, 2))
        self.assertGreaterEqual(got["oldest_s"], 2 * DAY - 5)
        observed.observe(first, "saw it", "checker-seat")
        self.assertEqual(observed.pile(now=now)["n"], 1)


class TheDoctorCountsThePileTest(ObservedBase):
    """O4: `helm doctor` counts the landed-not-observed tasks and their age,
    and names the seat processes that run a replaced binary (task/3717)."""

    def proc(self, procs=()):
        """A scratch /proc: (pid, ppid, exe, seat or None) per process."""
        root = os.path.join(self.tmp, "proc")
        os.makedirs(root, exist_ok=True)
        for pid, ppid, exe, seat in procs:
            d = os.path.join(root, str(pid))
            os.makedirs(d)
            os.symlink(exe, os.path.join(d, "exe"))
            with open(os.path.join(d, "stat"), "w") as fh:
                fh.write("%d (x y) S %d 1 1\n" % (pid, ppid))
            with open(os.path.join(d, "environ"), "wb") as fh:
                fh.write(b"OTHER=1\0" + (("HELM_CHAT_NAME=%s\0" % seat)
                                          .encode() if seat else b""))
        return root

    def test_doctor_counts_the_pile_and_warns_past_a_day(self):
        from helm import doctor
        quiet = self.proc()
        (level, text), = doctor.check_seen_working(proc=quiet)
        self.assertEqual(level, doctor.OK)
        self.assertIn("no landed task owes one", text)
        self.land(self.file())
        (level, text), = doctor.check_seen_working(proc=quiet)
        self.assertEqual(level, doctor.OK)
        self.assertIn("1 landed task(s) owe one", text)
        later = time.time() + 2 * DAY
        with mock.patch.object(observed.time, "time", return_value=later):
            (level, text), = doctor.check_seen_working(proc=quiet)
        self.assertEqual(level, doctor.WARN)
        self.assertIn("1 past 24h", text)

    def test_a_seat_child_on_a_replaced_binary_is_named(self):
        from helm import doctor
        root = self.proc((
            (100, 1, "/usr/bin/claude", "seat-x"),
            (101, 100, "/home/u/.local/bin/cv-mcp (deleted)", None),
            (102, 100, "/home/u/.local/bin/fresh", None),
            (200, 1, "/usr/bin/zsh", None),
            (201, 200, "/home/u/.local/bin/cv-mcp (deleted)", None)))
        self.assertEqual(observed.replaced(root),
                         [("/home/u/.local/bin/cv-mcp", "seat-x", 101)])
        _ok, (level, text) = doctor.check_seen_working(proc=root)
        self.assertEqual(level, doctor.WARN)
        self.assertIn("1 seat process runs a replaced binary: "
                      "/home/u/.local/bin/cv-mcp in seat-x, live on their "
                      "next relaunch", text)
        self.assertNotIn("OTHER", text)


class TheOwedWhisperTest(ObservedBase):
    """O5: the owing seat hears its own line through the stop whisper."""

    def stop(self, seat="asker-seat"):
        """One real stop whisper for `seat`, its text or ''."""
        from helm import seats_work_offer
        return seats_work_offer._stop_whisper(
            "s-%s" % seat, "main", seat, [], False) or ""

    def test_a_seat_owing_two_checks_hears_it_once(self):
        first, second = self.file("one"), self.file("two")
        self.land(first)
        self.land(second)
        self.assertIn("owes 2 seen-working checks: %s, %s — see each "
                      "working, then `helm task observed <id> --evidence "
                      '"..."`' % (first, second), self.stop())

    def test_a_seat_owing_none_hears_nothing(self):  # noqa: VACUOUS_ASSERTION — the owing seat's stop is asserted to carry the line first, on the same stop path
        self.land(self.file())
        self.assertIn("owes 1 seen-working check:", self.stop())
        self.assertIsNone(observed.stop_candidate("lead-seat"))
        self.assertNotIn("seen-working", self.stop("lead-seat"))

    def test_the_same_state_does_not_repeat(self):
        self.land(self.file())
        self.assertIn("owes 1 seen-working check:", self.stop())
        self.assertNotIn("seen-working", self.stop())

    def test_a_new_owed_check_fires_again(self):
        first = self.file("one")
        self.land(first)
        self.assertIn("owes 1 seen-working check: %s" % first, self.stop())
        second = self.file("two")
        self.land(second)
        self.assertIn("owes 2 seen-working checks: %s, %s"
                      % (first, second), self.stop())
        observed.observe(first, "saw it", "checker-seat")
        self.assertIn("owes 1 seen-working check: %s" % second, self.stop())

    def test_more_than_three_name_three(self):
        ids = [self.file("n%d" % i) for i in range(4)]
        for tid in ids:
            self.land(tid)
        self.assertIn("owes 4 seen-working checks: %s +1 more"
                      % ", ".join(ids[:3]), self.stop())

    def test_an_unchanged_ledger_is_read_once(self):
        self.land(self.file())
        self.assertIsNone(observed.stop_candidate("lead-seat"))
        with mock.patch.object(tasks, "snapshot",
                               side_effect=AssertionError("re-read")):
            self.assertIsNone(observed.stop_candidate("lead-seat"))
            self.assertIn("owes 1 seen-working check:",
                          observed.stop_candidate("asker-seat")[1])
        self.land(self.file("two"))
        with mock.patch.object(tasks, "snapshot",
                               wraps=tasks.snapshot) as read:
            self.assertIn("owes 2 seen-working checks:",
                          observed.stop_candidate("asker-seat")[1])
        self.assertEqual(read.call_count, 1)


class TheOrderLineTest(ObservedBase):
    """The fleet-wide ORDER lines in the brief."""

    def test_the_order_lines_name_each_owing_seat(self):
        from helm import brief
        tid = self.file()
        self.assertEqual(brief._check_lines(tasks.rows().values()), [])
        self.land(tid)
        self.assertEqual(brief._check_lines(tasks.rows().values()),
                         ["ORDER @asker-seat owes 1 seen-working check: %s"
                          % tid])


if __name__ == "__main__":
    unittest.main()
