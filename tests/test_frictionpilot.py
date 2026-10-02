#!/usr/bin/env python3
"""The friction autopilot (task/3899): a guard that keeps refusing gets ONE
task row, filed, counted, raised and re-checked by helm itself.

Every arm runs under a temp HELM_HOME and a temp chat directory. The
refusals are written by the REAL producer (`friction.record`, one seat at a
time through HELM_CHAT_NAME), the rows by the REAL task door, and the #seats
rows by the REAL chat post, read back from the temp room. The owner's phone
is a mock that fails the arm if anything calls it. No arm reads the fleet.

The fixture causes are real ones from the burn-down friction ranking:
cause 19 (guards refuse read-only commands: the shared-checkout guard and
the argv-guard on PreToolUse:Bash, and the work guard refusing a lane push,
which is a land-path refusal) and the split-budget rung (task/3524).
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-frictionpilot-", var="HELM_HOME")

from helm import (actors, chat, cli, friction, frictionpilot,  # noqa: E402
                  home, idle_dispatch, localnames, pk, seatevents, seats,
                  taskhygiene, tasks)

NOW = 1790000000.0
HOUR = 3600.0
STEWARD = "floor-steward"
CHECKOUT = ("shared-checkout-guard", "PreToolUse:Bash")
ARGV = ("argv-guard", "PreToolUse:Bash")
PUSH = ("hostpath", "pre-push")
SPLIT = ("split-budget", "pre-commit")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-frictionpilot-case-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_NTFY_TOPIC": "", "MELD_NTFY_TOPIC": ""})
        env.start()
        self.addCleanup(env.stop)
        self.plant({frictionpilot.STEWARD_KEY: STEWARD})
        phone = mock.patch("helm.notify.owner_push",
                           side_effect=AssertionError("the owner was paged"))
        self.phone = phone.start()
        self.addCleanup(phone.stop)

    # ------------------------------------------------------------ fixtures
    def plant(self, table):
        os.makedirs(home.global_dir(), exist_ok=True)
        with open(os.path.join(home.global_dir(), localnames.CONFIG), "w",
                  encoding="utf-8") as f:
            json.dump(table, f)
        localnames._cache["stat"] = None

    def hit(self, cause, seat, at, n=1, step=60.0):
        """`n` refusals of `cause` met by `seat`, through the real producer."""
        guard, reason = cause
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": seat}):
            for i in range(n):
                self.assertTrue(friction.record(guard, reason=reason,
                                                now=at + i * step))

    def run_pass(self, now, **kw):
        kw.setdefault("stop_text", "")
        return frictionpilot.run(now=now, **kw)

    def mine(self):
        return sorted((r for r in tasks.rows().values()
                       if r.get("source") == frictionpilot.POSTER),
                      key=tasks.sort_key)

    def seats_rows(self):
        path = chat.room_path(seatevents.ROOM)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line)["text"] for line in f if line.strip()]

    def state(self):
        with open(frictionpilot.state_path(), encoding="utf-8") as f:
            return json.load(f)

    def key(self, cause):
        return frictionpilot.cause_key(*cause)

    def admit_seat(self, seat):
        """Make the REAL admission pass admit `seat`, as tests/test_tasks'
        fixture does: a declared name and a session the roster maps to it.
        The resolve is asserted, so an arm never measures a refusal while
        claiming a seat ran the pass."""
        session = "sess-for-%s" % seat
        for key in home._SESSION_ENV:
            os.environ[key] = session
        os.environ["HELM_CHAT_NAME"] = seat
        rpath = seats.roster_path()
        rows = pk.read_json(rpath, {}) or {}
        rows[seat] = dict(rows.get(seat) or {}, session=session)
        os.makedirs(os.path.dirname(rpath), exist_ok=True)
        pk.atomic_write(rpath, json.dumps(rows))
        actor, err = tasks._admit("raise in a test")
        self.assertIsNone(err, "the fixture did not admit %s: %s" % (seat, err))
        self.assertEqual(actor.canonical_name, seat)


class CauseKeyTest(Base):
    def test_numbered_guards_do_not_merge_distinct_causes(self):  # noqa: VACUOUS_ASSERTION — after 5+5 file nothing, five more for guard1 file exactly one
        self.assertNotEqual(self.key(("guard1", "blocked")),
                            self.key(("guard2", "blocked")))
        self.hit(("guard1", "blocked"), "seat-a", NOW - HOUR, n=5)
        self.hit(("guard2", "blocked"), "seat-a", NOW - HOUR, n=5)
        rep = self.run_pass(NOW)
        self.assertEqual(rep["filed"], [])
        self.assertEqual(self.mine(), [])
        self.hit(("guard1", "blocked"), "seat-a", NOW - 30, n=5, step=1)
        rep = self.run_pass(NOW)
        self.assertEqual([f["key"] for f in rep["filed"]],
                         [self.key(("guard1", "blocked"))])
        self.assertEqual(len(self.mine()), 1)

    def test_seats_shas_numbers_times_and_paths_fold_into_one_key(self):
        k = frictionpilot.cause_key
        self.assertEqual(k("split-budget", "rung-3b87a2a75f1", "seat-a"),
                         k("split-budget", "rung-c162d84e4cc", "seat-b"))
        self.assertEqual(k("argv-guard", "retry-17-seat-a", "seat-a"),
                         k("argv-guard", "retry-3-seat-b", "seat-b"))
        self.assertEqual(k("docref", "at-2026-09-30T17:40:00Z"),
                         k("docref", "at-2026-10-01T02:05:59Z"))
        self.assertEqual(k("docref", "helm/tasks.py:2196"),
                         k("docref", "helm/chat.py:12"))
        self.assertEqual(k(*CHECKOUT), k("Shared-Checkout-Guard",
                                         "pretooluse:bash"))
        # the negative controls on the same function: another guard, or the
        # same guard on another event, is another cause
        self.assertNotEqual(k(*CHECKOUT), k(*ARGV))
        self.assertNotEqual(k(*SPLIT), k("split-budget", "pre-push"))
        self.assertNotEqual(k("argv-guard", "retry-17"), k("argv-guard", "x"))

    def test_the_ledger_rows_of_two_seats_make_one_cause(self):
        self.hit(("split-budget", "rung-3b87a2a75f1"), "seat-a", NOW - HOUR)
        self.hit(("split-budget", "rung-c162d84e4cc"), "seat-b", NOW - 60)
        rows, unreadable = friction.read()
        self.assertIsNone(unreadable)
        causes = frictionpilot.census(rows, NOW)
        self.assertEqual(len(causes), 1)
        (cause,) = causes.values()
        self.assertEqual(frictionpilot.judge(cause, NOW)["seats"],
                         ["seat-a", "seat-b"])


class BarTest(Base):
    def test_under_the_bar_files_nothing_and_posts_nothing(self):
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR - 1)
        self.hit(ARGV, "seat-a", NOW - HOUR)
        self.hit(ARGV, "seat-b", NOW - HOUR)
        rep = self.run_pass(NOW)
        self.assertEqual(rep["filed"], [])
        self.assertEqual(self.mine(), [])
        self.assertEqual(self.seats_rows(), [])
        # the positive control on the same cause: one more refusal crosses
        self.hit(CHECKOUT, "seat-a", NOW - 30)
        rep = self.run_pass(NOW)
        self.assertEqual([f["key"] for f in rep["filed"]],
                         [self.key(CHECKOUT)])

    def test_old_refusals_fall_out_of_the_window(self):
        self.hit(CHECKOUT, "seat-a", NOW - frictionpilot.WINDOW_S - HOUR,
                 n=frictionpilot.HITS_BAR)
        self.assertEqual(self.run_pass(NOW)["filed"], [])
        self.assertEqual(self.mine(), [])
        # the positive control: the same refusals a day later are in it
        rep = self.run_pass(NOW - frictionpilot.WINDOW_S)
        self.assertEqual([f["key"] for f in rep["filed"]],
                         [self.key(CHECKOUT)])


class FileTest(Base):
    def test_a_repeat_files_one_unowned_P1_row_and_one_seats_post(self):
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        rep = self.run_pass(NOW)
        (row,) = self.mine()
        self.assertEqual(rep["filed"][0]["row"], row["id"])
        self.assertEqual(row["priority"], "P1")
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["owner"])
        self.assertEqual(row["project"], tasks.OWN_PROJECT)
        self.assertIn(frictionpilot.REF + self.key(CHECKOUT), row["refs"])
        self.assertIn("shared-checkout-guard", row["title"])
        self.assertIn("cannot tell", row["note"])
        self.assertIn("guard-correct", row["note"])
        (text,) = self.seats_rows()
        self.assertTrue(text.startswith("@" + STEWARD + " "), text)
        self.assertIn(row["id"], text)
        self.phone.assert_not_called()
        # the state maps the cause to the row
        got = self.state()["causes"][self.key(CHECKOUT)]
        self.assertEqual((got["row"], got["priority"]), (row["id"], "P1"))

    def test_a_failed_event_claim_keeps_the_steward_post_owed(self):  # noqa: VACUOUS_ASSERTION — the same event is positively observed in #seats once its ledger recovers
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        blocked = os.path.join(self.tmp, "not-a-directory")
        with open(blocked, "w", encoding="utf-8") as f:
            f.write("blocked")
        with mock.patch.object(seatevents, "ledger_path", return_value=
                               os.path.join(blocked, "seat-events.json")):
            self.run_pass(NOW)
        self.assertEqual(len(self.mine()), 1)
        self.assertEqual(len(self.state()["owed_events"]), 1)
        self.assertEqual(self.seats_rows(), [])
        self.run_pass(NOW + 60)
        self.assertEqual(len(self.seats_rows()), 1)
        self.assertEqual(self.state()["owed_events"], [])
        self.run_pass(NOW + 120)
        self.assertEqual(len(self.seats_rows()), 1)
        self.phone.assert_not_called()

    def test_a_claimed_event_retries_its_failed_chat_post_without_new_events(self):  # noqa: VACUOUS_ASSERTION — the identical claimed event is observed in #seats exactly once on recovery
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)

        def down(_text, _rid):
            raise OSError("room down")

        self.run_pass(NOW, chat_post=down)
        self.assertEqual(len(self.mine()), 1)
        self.assertEqual(self.state()["owed_events"], [])
        self.assertEqual(self.seats_rows(), [])
        self.run_pass(NOW + 60)
        self.assertEqual(len(self.seats_rows()), 1)
        self.run_pass(NOW + 120)
        self.assertEqual(len(self.seats_rows()), 1)
        self.phone.assert_not_called()

    def test_three_seats_file_P0(self):
        for seat in ("seat-a", "seat-b", "seat-c"):
            self.hit(ARGV, seat, NOW - HOUR)
        self.run_pass(NOW)
        (row,) = self.mine()
        self.assertEqual(row["priority"], "P0")
        self.assertIn("3 seats", row["note"])

    def test_a_land_path_refusal_files_P0(self):
        self.hit(PUSH, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW)
        (row,) = self.mine()
        self.assertEqual(row["priority"], "P0")
        self.assertIn("land", row["note"])

    def test_a_stopped_train_that_names_the_guard_files_P0(self):
        self.hit(SPLIT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.hit(CHECKOUT, "seat-b", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW, stop_text="car lane-x: [helm split-budget] "
                                     "REFUSED: helm/seats.py grew")
        got = {r["title"]: r["priority"] for r in self.mine()}
        self.assertEqual(sorted(got.values()), ["P0", "P1"])
        p0 = [t for t, p in got.items() if p == "P0"]
        self.assertIn("split-budget", p0[0])

    def test_an_unset_steward_posts_with_no_mention_and_says_where(self):
        self.plant({})
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW)
        (text,) = self.seats_rows()
        self.assertFalse(text.startswith("@"), text)
        self.assertIn("local-names 'friction-steward-seat' is unset", text)


class OneRowPerCauseTest(Base):
    def test_more_hits_bump_the_same_row_with_a_counted_comment(self):  # noqa: VACUOUS_ASSERTION — the counted comment and one #seats filing are the positive controls on the same rows
        key = self.key(CHECKOUT)
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW)
        (row,) = self.mine()
        later = NOW + frictionpilot.BUMP_EVERY_S + 60
        self.hit(CHECKOUT, "seat-b", later - HOUR, n=4)
        rep = self.run_pass(later)
        self.assertEqual(rep["filed"], [])
        (again,) = self.mine()
        self.assertEqual(again["id"], row["id"])
        comments = tasks.comments_of(again)
        notes = [c["text"] for c in comments]
        self.assertEqual(len(notes), 1)
        self.assertIsNone(comments[0]["by"], "a machine note is not a seat's act")
        self.assertTrue(notes[0].startswith(frictionpilot.MARK), notes[0])
        self.assertIn("4 more refusals", notes[0])
        self.assertEqual(rep["bumped"][0]["key"], key)
        # inside BUMP_EVERY_S of that bump, new hits wait for the next bump
        self.hit(CHECKOUT, "seat-b", later + 60)
        self.assertEqual(self.run_pass(later + 120)["bumped"], [])
        self.assertEqual(len(tasks.comments_of(self.mine()[0])), 1)
        # only one #seats row was ever posted, for the filing
        self.assertEqual(len(self.seats_rows()), 1)

    def test_a_lost_state_file_finds_the_open_row_by_its_ref(self):
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW)
        (row,) = self.mine()
        os.remove(frictionpilot.state_path())
        rep = self.run_pass(NOW + 60)
        self.assertEqual(rep["filed"], [])
        self.assertEqual([a["row"] for a in rep["adopted"]], [row["id"]])
        self.assertEqual(len(self.mine()), 1)
        self.assertEqual(self.state()["causes"][self.key(CHECKOUT)]["row"],
                         row["id"])

    def test_a_pass_writes_at_most_MAX_WRITES_and_the_next_takes_the_rest(self):
        n = frictionpilot.MAX_WRITES + 2
        for i in range(n):
            self.hit(("guard-%s" % "abcdefghij"[i], "pre-commit"), "seat-a",
                     NOW - HOUR, n=frictionpilot.HITS_BAR)
        rep = self.run_pass(NOW)
        self.assertEqual(len(rep["filed"]), frictionpilot.MAX_WRITES)
        self.assertEqual(rep["owed"], 2)
        rep = self.run_pass(NOW + 60)
        self.assertEqual(len(rep["filed"]), 2)
        self.assertEqual(len(self.mine()), n)


class RaiseTest(Base):
    def _p1_then_spread(self):
        self.hit(ARGV, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW)
        (row,) = self.mine()
        self.assertEqual(row["priority"], "P1")
        self.hit(ARGV, "seat-b", NOW + 60)
        self.hit(ARGV, "seat-c", NOW + 120)
        return row

    def test_the_timer_pass_raises_the_row_as_helm_itself(self):  # noqa: VACUOUS_ASSERTION — the raise is asserted unconditionally first (the RAISED line, P0, ranked_by, the comment and the post); the empty lists after it are the next pass doing nothing more
        row = self._p1_then_spread()
        with mock.patch.object(frictionpilot, "land_stop_text",
                               return_value=""):
            got = frictionpilot.ride(now=NOW + frictionpilot.PASS_EVERY_S + 1)
        self.assertIn("friction autopilot: RAISED %s to P0 as "
                      "system:frictionpilot: 3 seats met it" % row["id"], got)
        (again,) = self.mine()
        self.assertEqual(again["priority"], "P0")
        self.assertEqual(again["ranked_by"], "system:frictionpilot")
        self.assertEqual(again["rank_action"], "system")
        self.assertEqual(again["ranked_from"], "P1")
        self.assertEqual(again["source"], frictionpilot.POSTER)
        (note,) = [c["text"] for c in tasks.comments_of(again)]
        self.assertTrue(note.startswith(frictionpilot.MARK + " raised]"), note)
        self.assertIn("from P1 to P0 by system:frictionpilot", note)
        texts = self.seats_rows()
        self.assertEqual(len(texts), 2)
        self.assertIn("%s raised to P0 by system:frictionpilot" % row["id"],
                      texts[1])
        self.phone.assert_not_called()
        # once: the next pass neither ranks, comments nor posts again
        rep = self.run_pass(NOW + 3 * frictionpilot.PASS_EVERY_S)
        self.assertEqual((rep["raised"], rep["raise_owed"]), ([], []))
        self.assertEqual(len(tasks.comments_of(self.mine()[0])), 1)
        self.assertEqual(len(self.seats_rows()), 2)
        self.assertEqual([c["row"] for c in frictionpilot.p0_causes()],
                         [row["id"]])

    def test_a_dry_timer_pass_says_it_would_raise_and_writes_nothing(self):
        row = self._p1_then_spread()
        rep = self.run_pass(NOW + 300, apply=False)
        self.assertEqual([(r["row"], r["by"]) for r in rep["raised"]],
                         [(row["id"], "system:frictionpilot")])
        self.assertIn("WOULD RAISE %s to P0 as system:frictionpilot"
                      % row["id"], "\n".join(frictionpilot.lines(rep)))
        (again,) = self.mine()
        self.assertEqual(again["priority"], "P1")
        self.assertEqual(tasks.comments_of(again), [])
        self.assertEqual(len(self.seats_rows()), 1)

    def test_a_seat_run_pass_still_records_the_seat(self):
        row = self._p1_then_spread()
        self.admit_seat("seat-r")
        rep = self.run_pass(NOW + 300,
                            admit=lambda: tasks._admit("raise in a test"))
        self.assertEqual([(r["row"], r["by"]) for r in rep["raised"]],
                         [(row["id"], "seat-r")])
        (again,) = self.mine()
        self.assertEqual(again["priority"], "P0")
        self.assertEqual(again["ranked_by"], "seat-r")
        self.assertEqual(again["rank_action"], "manual")
        (note,) = [c["text"] for c in tasks.comments_of(again)]
        self.assertIn("from P1 to P0 by seat-r", note)
        self.assertIn("raised to P0 by seat-r", self.seats_rows()[-1])

    def test_a_row_the_autopilot_did_not_file_is_owed_not_raised(self):  # noqa: VACUOUS_ASSERTION — the adoption, the owed row, its comment text and the door's refusal text are each asserted unconditionally beside the empty raised list
        key = self.key(ARGV)
        theirs, err = tasks.add(
            "argv-guard refuses read-only commands", None,
            refs=[frictionpilot.REF + key], source="seat-x", origin="agent",
            priority="P1", project=tasks.OWN_PROJECT, force_new=True,
            posture_na=frictionpilot.POSTURE_NA)
        self.assertIsNone(err, err)
        self.hit(ARGV, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        rep = self.run_pass(NOW)
        self.assertEqual([a["row"] for a in rep["adopted"]], [theirs["id"]])
        self.hit(ARGV, "seat-b", NOW + 60)
        self.hit(ARGV, "seat-c", NOW + 120)
        rep = self.run_pass(NOW + 300)
        self.assertEqual(rep["raised"], [])
        self.assertEqual([r["row"] for r in rep["raise_owed"]],
                         [theirs["id"]])
        got = tasks.rows()[theirs["id"]]
        self.assertEqual(got["priority"], "P1")
        self.assertIsNone(got.get("ranked_by"))
        (note,) = [c["text"] for c in tasks.comments_of(got)]
        self.assertIn("filed by seat-x, not by system:frictionpilot", note)
        self.assertIn("helm task update %s --priority P0" % theirs["id"], note)
        self.assertIn("P0", self.seats_rows()[-1])
        # the task door refuses the capability on that row by itself too
        cap, err = actors.grant_system_rank(frictionpilot.SYSTEM,
                                            [theirs["id"]])
        self.assertIsNone(err, err)
        _row, err = tasks.update(theirs["id"], rank_actor=cap, priority="P0")
        self.assertIn("filed by seat-x", err)
        self.assertEqual(tasks.rows()[theirs["id"]]["priority"], "P1")

    def test_a_seat_named_like_the_old_poster_cannot_gain_system_rank(self):
        self.admit_seat("friction-autopilot")
        actor, err = tasks._admit("file an ordinary seat row")
        self.assertIsNone(err, err)
        key = self.key(ARGV)
        theirs, err = tasks.add(
            "seat-filed guard report", None, source=actor.canonical_name,
            refs=[frictionpilot.REF + key], origin="agent", priority="P1",
            project=tasks.OWN_PROJECT, force_new=True,
            posture_na=frictionpilot.POSTURE_NA)
        self.assertIsNone(err, err)
        self.hit(ARGV, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW)
        self.hit(ARGV, "seat-b", NOW + 60)
        self.hit(ARGV, "seat-c", NOW + 120)
        rep = self.run_pass(NOW + 300)
        self.assertEqual(rep["raised"], [])
        self.assertEqual([r["row"] for r in rep["raise_owed"]], [theirs["id"]])
        self.assertEqual(tasks.rows()[theirs["id"]]["priority"], "P1")

    def test_another_subsystem_name_mints_nothing_and_the_raise_is_owed(self):
        row = self._p1_then_spread()
        with mock.patch.object(frictionpilot, "SYSTEM", "gate"):
            rep = self.run_pass(NOW + 300)
        self.assertEqual(rep["raised"], [])
        self.assertEqual([r["row"] for r in rep["raise_owed"]], [row["id"]])
        (again,) = self.mine()
        self.assertEqual(again["priority"], "P1")
        (note,) = [c["text"] for c in tasks.comments_of(again)]
        self.assertIn("no system rank was minted", note)


class SystemRankDoorTest(Base):
    """The capability through the REAL task door, against the rows the
    autopilot itself filed."""

    def _two_rows(self):
        self.hit(ARGV, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.hit(SPLIT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW)
        mine = self.mine()
        self.assertEqual([r["priority"] for r in mine], ["P1", "P1"])
        self.assertEqual({e["row"] for e in self.state()["causes"].values()},
                         {r["id"] for r in mine})
        return mine

    def test_only_its_one_move_on_a_mapped_row_lands(self):  # noqa: VACUOUS_ASSERTION — the positive control is the admitted update in the middle of the arm, asserted as P0 and system:frictionpilot on the same door
        first, second = self._two_rows()
        cap, err = actors.grant_system_rank(frictionpilot.SYSTEM,
                                            [first["id"]])
        self.assertIsNone(err, err)
        # an autopilot row this capability's map does not hold
        _row, err = tasks.update(second["id"], rank_actor=cap, priority="P0")
        self.assertIn("is not a row frictionpilot filed", err)
        # no other field rides a system rank
        _row, err = tasks.update(first["id"], rank_actor=cap, priority="P0",
                                 title="retitled")
        self.assertIn("changes only a row's priority", err)
        # the positive control: its one move lands, as the system, and is
        # not a seat working the row
        row, err = tasks.update(first["id"], rank_actor=cap, priority="P0")
        self.assertIsNone(err, err)
        self.assertEqual((row["priority"], row["ranked_by"],
                          row["rank_action"], row["ranked_from"]),
                         ("P0", "system:frictionpilot", "system", "P1"))
        self.assertEqual(row["last_updated"], first["last_updated"])
        self.assertTrue(row["ranked_at"])
        self.assertEqual(row["title"], first["title"])
        # a lowering is refused, and the rows stay as they were
        _row, err = tasks.update(first["id"], rank_actor=cap, priority="P1")
        self.assertIn("a lowering", err)
        self.assertEqual(tasks.rows()[first["id"]]["priority"], "P0")
        self.assertEqual(tasks.rows()[second["id"]]["priority"], "P1")

    def test_a_rank_rule_never_runs_as_the_system(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted, and the same rule as an admitted seat ranks the same row P2 unconditionally at the end
        unranked, err = tasks.add(
            "an unranked row the autopilot filed", None,
            source=frictionpilot.POSTER, origin="agent",
            project=tasks.OWN_PROJECT, force_new=True,
            posture_na=frictionpilot.POSTURE_NA)
        self.assertIsNone(err, err)
        self.assertIsNone(unranked["priority"])
        cap, err = actors.grant_system_rank(frictionpilot.SYSTEM,
                                            [unranked["id"]])
        self.assertIsNone(err, err)
        rule = tasks.RankRule(tasks.OWN_PROJECT)
        got, err = tasks.update(unranked["id"], rank_rule=rule,
                                rank_actor=cap)
        self.assertIsNone(got)
        self.assertIn("a rank rule decides for a seat, never for "
                      "system:frictionpilot", err)
        self.assertIsNone(tasks.rows()[unranked["id"]]["priority"])
        # the positive control: the same rule as an admitted seat ranks it
        self.admit_seat("seat-r")
        row, err = tasks.update(unranked["id"], rank_rule=rule,
                                rank_actor=tasks._admit("rank in a test")[0])
        self.assertIsNone(err, err)
        self.assertEqual((row["priority"], row["ranked_by"]), ("P2", "seat-r"))


class CureTest(Base):
    def _filed_then_closed(self, reason="fixed: the guard reads git verbs"):
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=12)
        self.run_pass(NOW)
        (row,) = self.mine()
        _r, err = tasks.close(row["id"], reason)
        self.assertIsNone(err)
        rep = self.run_pass(NOW + 600)
        return row, rep

    def test_a_close_records_the_rate_before(self):
        row, rep = self._filed_then_closed()
        self.assertEqual([c["row"] for c in rep["closed"]], [row["id"]])
        got = self.state()["causes"][self.key(CHECKOUT)]
        self.assertEqual(got["status"], "closed")
        self.assertEqual(got["rate_before"], 6.0)    # 12 hits over 2 days

    def test_a_rate_that_did_not_fall_gives_the_cause_a_new_row(self):
        row, _rep = self._filed_then_closed()
        closed_at = NOW + 600
        self.hit(CHECKOUT, "seat-b", closed_at + HOUR, n=12, step=HOUR)
        rep = self.run_pass(closed_at + frictionpilot.SETTLE_S + 60)
        (got,) = rep["reopened"]
        self.assertEqual(got["was"], row["id"])
        new = tasks.rows()[got["row"]]
        self.assertEqual(new["status"], "open")
        self.assertIn(row["id"], new["refs"])
        self.assertIn(frictionpilot.REF + self.key(CHECKOUT), new["refs"])
        self.assertIn("6.0", new["note"])
        old = tasks.rows()[row["id"]]
        self.assertEqual(old["status"], "closed")
        (note,) = [c["text"] for c in tasks.comments_of(old)]
        self.assertIn(new["id"], note)
        self.assertIn("6.0", note)
        self.assertEqual(self.state()["causes"][self.key(CHECKOUT)]["row"],
                         new["id"])
        self.assertIn(new["id"], self.seats_rows()[-1])

    def test_a_rate_that_fell_is_cured_and_files_nothing(self):
        row, _rep = self._filed_then_closed()
        closed_at = NOW + 600
        self.hit(CHECKOUT, "seat-b", closed_at + HOUR)
        rep = self.run_pass(closed_at + frictionpilot.SETTLE_S + 60)
        self.assertEqual(rep["reopened"], [])
        self.assertEqual([c["row"] for c in rep["cured"]], [row["id"]])
        self.assertEqual(len(self.mine()), 1)
        self.assertEqual(self.state()["causes"][self.key(CHECKOUT)]["status"],
                         "cured")

    def test_a_guard_correct_close_retires_the_cause(self):
        row, rep = self._filed_then_closed(
            reason="guard-correct: every one of these was a force-push")
        self.assertEqual([c["row"] for c in rep["retired"]], [row["id"]])
        later = NOW + 3 * frictionpilot.SETTLE_S
        self.hit(CHECKOUT, "seat-b", later - HOUR, n=20)
        rep = self.run_pass(later)
        self.assertEqual((rep["filed"], rep["reopened"]), ([], []))
        self.assertEqual(len(self.mine()), 1)


class DryRunTest(Base):
    def test_dry_run_reports_and_writes_nothing(self):
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        rep = self.run_pass(NOW, apply=False)
        self.assertTrue(rep["dry_run"])
        self.assertEqual([f["key"] for f in rep["filed"]],
                         [self.key(CHECKOUT)])
        self.assertEqual(self.mine(), [])
        self.assertEqual(self.seats_rows(), [])
        self.assertFalse(os.path.exists(frictionpilot.state_path()))
        self.assertIn("WOULD FILE", "\n".join(frictionpilot.lines(rep)))

    def test_the_cli_dry_run_prints_json(self):
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        out = io.StringIO()
        with mock.patch("time.time", return_value=NOW), \
                mock.patch.object(frictionpilot, "land_stop_text",
                                  return_value=""), \
                contextlib.redirect_stdout(out):
            rc = cli.VERBS["friction"](["autopilot", "--dry-run", "--json"])
        self.assertEqual(rc, 0)
        got = json.loads(out.getvalue())
        self.assertTrue(got["dry_run"])
        self.assertEqual(got["filed"][0]["key"], self.key(CHECKOUT))
        self.assertEqual(self.mine(), [])

    def test_the_cli_refuses_an_unknown_flag(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.VERBS["friction"](["autopilot", "--aply"])
        self.assertEqual(rc, 2)
        self.assertIn("--aply", err.getvalue())


class SafetyTest(Base):
    def test_an_unreadable_ledger_writes_nothing_and_says_so(self):
        os.makedirs(friction.path())        # a directory cannot be read
        rep = self.run_pass(NOW)
        self.assertTrue(rep["unreadable"])
        self.assertEqual(self.mine(), [])
        self.assertIn("UNREADABLE", "\n".join(frictionpilot.lines(rep)))

    def test_ride_never_raises_and_runs_once_per_window(self):
        with mock.patch.object(frictionpilot, "run",
                               side_effect=RuntimeError("boom")):
            got = frictionpilot.ride(now=NOW)
        self.assertTrue(any("boom" in line for line in got), got)
        self.hit(CHECKOUT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        with mock.patch.object(frictionpilot, "land_stop_text",
                               return_value=""):
            first = frictionpilot.ride(now=NOW)
            self.assertTrue(any("FILED" in line for line in first), first)
            with mock.patch.object(frictionpilot, "run") as again:
                self.assertEqual(frictionpilot.ride(now=NOW + 60), [])
                again.assert_not_called()
                frictionpilot.ride(now=NOW + frictionpilot.PASS_EVERY_S + 1)
                again.assert_called_once()

    def test_p0_causes_lists_open_P0_causes_only(self):
        self.hit(PUSH, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.hit(SPLIT, "seat-a", NOW - HOUR, n=frictionpilot.HITS_BAR)
        self.run_pass(NOW)
        got = frictionpilot.p0_causes()
        self.assertEqual([c["key"] for c in got], [self.key(PUSH)])
        self.assertEqual(got[0]["count"], frictionpilot.HITS_BAR)
        self.assertTrue(got[0]["row"].startswith("task/"))

    def test_its_comments_are_never_motion_to_the_task_sweep(self):
        self.assertTrue(taskhygiene._machine_note(
            {"text": frictionpilot.MARK + " count] 4 more refusals"}))
        self.assertFalse(taskhygiene._machine_note(
            {"text": "a seat's own note"}))


class TickTest(Base):
    def _tick(self, *args):
        got = {"findings": [], "redeliverable": []}
        out = io.StringIO()
        with mock.patch.object(idle_dispatch, "check", return_value=got), \
                mock.patch("helm.darkmove.run", return_value=([], [])), \
                mock.patch.object(frictionpilot, "ride",
                                  return_value=["friction autopilot: FILED x"]
                                  ) as ride, \
                contextlib.redirect_stdout(out):
            idle_dispatch.cmd_idle_dispatch(list(args))
        return ride, out.getvalue()

    def test_the_idle_dispatch_tick_rides_the_autopilot(self):
        ride, out = self._tick()
        ride.assert_called_once_with(apply=True)
        self.assertIn("friction autopilot: FILED x", out)
        for flag in ("--dry-run", "--quiet"):
            ride, _out = self._tick(flag)
            ride.assert_called_once_with(apply=False)
        _ride, out = self._tick("--json")
        self.assertEqual(json.loads(out)["friction_autopilot"],
                         ["friction autopilot: FILED x"])


if __name__ == "__main__":
    unittest.main()
