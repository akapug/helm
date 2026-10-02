#!/usr/bin/env python3
"""A voice with a budget (task/3901): `helm seat shout`.

Three volumes. TALK is the seat's own room and this verb adds nothing there.
SPEAK UP posts one #seats row that @mentions the seat's steward. SHOUT pushes
the owner's phone, carries exactly one "what I need" line and the seat's name,
names the seat's Orca tab, and costs one of N shouts per seat per 24 hours
(default 2, the owner's dial).

Every arm runs under a temp HELM_HOME and a temp chat directory. The phone is
a fake `notify.owner_push`/`notify.configured`; identity is the module's one
admission seam, patched, except in the arm that proves the seam refuses an
unadmitted actor. Nothing reaches a network, an Orca pane or the live fleet.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-seatshout-", var="HELM_HOME")

from helm import (actors, chat, eventledger, home, localnames,  # noqa: E402
                  notify, pk, seatevents, seats_integrator, seats_roster,
                  seatshout)

NOW = 1790000000.0
DAY = 86400
SEAT = "seat-a"
WORKTREE = "repo-1::/work/helm"
#: The module's real admission seam, kept before Base patches it.
REAL_ADMIT = seatshout._admit


def clock(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seatshout-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_NTFY_TOPIC": "", "MELD_NTFY_TOPIC": "",
            "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_ID": "",
            "CODEX_SESSION_ID": "", "ORCA_WORKTREE_ID": WORKTREE})
        env.start()
        self.addCleanup(env.stop)
        for key in ("HELM_CHAT_NAME", "MELD_CHAT_NAME"):
            os.environ.pop(key, None)
        self.pushes = []
        self.deliver = True
        self.phone = True
        for name, fake in (("owner_push", self.push),
                           ("configured", lambda: self.phone)):
            patcher = mock.patch.object(notify, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.who = SEAT
        admit = mock.patch.object(seatshout, "_admit",
                                  lambda: (self.who, None))
        admit.start()
        self.addCleanup(admit.stop)

    def push(self, body, title=None, receipt=None, reply_key=None):
        self.pushes.append({"body": body, "title": title,
                            "receipt": receipt, "reply_key": reply_key})
        return self.deliver

    def run_cli(self, *argv, now=NOW):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = seatshout.cmd_shout(list(argv), now=now)
        return rc, out.getvalue(), err.getvalue()

    def shout(self, need="a ruling on the gate", now=NOW, seat=None):
        self.who = seat or SEAT
        return self.run_cli("--need", need, now=now)

    def ok(self, need="a ruling on the gate", now=NOW, seat=None):
        rc, out, err = self.shout(need, now=now, seat=seat)
        self.assertEqual(rc, 0, err)
        return out

    def control(self, now=NOW + 7):
        """The positive control every refusal arm runs: a good shout from a
        fresh seat does ring the fake phone and does write the ledger, so a
        refusal's empty push list is the refusal, not a dead fake."""
        before = len(self.pushes)
        rc, _out, err = self.shout(now=now, seat="control-seat")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.pushes), before + 1)
        self.assertTrue(os.path.exists(seatshout.path()))

    def plant(self, table):
        os.makedirs(home.global_dir(), exist_ok=True)
        with open(os.path.join(home.global_dir(), localnames.CONFIG), "w",
                  encoding="utf-8") as f:
            json.dump(table, f)
        localnames._cache["stat"] = None

    def seats_rows(self):
        path = chat.room_path(seatevents.ROOM)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class NeedLineTest(Base):
    """Every shout carries exactly one line of what the seat needs."""

    def assert_refused(self, *argv):
        rc, _out, err = self.run_cli(*argv)
        self.assertEqual(rc, 2, err)
        self.assertIn("--need", err)
        self.assertEqual(self.pushes, [])
        self.assertFalse(os.path.exists(seatshout.path()))
        self.control()
        return err

    def test_no_need_is_refused_and_nothing_is_pushed(self):
        err = self.assert_refused()
        self.assertIn("exactly one", err)

    def test_two_need_lines_are_refused(self):
        err = self.assert_refused("--need", "one thing", "--need", "another")
        self.assertIn("exactly one", err)

    def test_a_need_with_a_newline_is_refused(self):
        err = self.assert_refused("--need", "first line\nsecond line")
        self.assertIn("one line", err)

    def test_an_empty_need_is_refused(self):
        self.assert_refused("--need", "   ")

    def test_a_need_over_the_limit_is_refused(self):
        err = self.assert_refused("--need", "x" * (seatshout.NEED_MAX + 1))
        self.assertIn(str(seatshout.NEED_MAX), err)

    def test_a_need_with_a_control_character_is_refused(self):
        self.assert_refused("--need", "look \x1b[2J here")

    def test_need_line_answers_the_line_or_why(self):
        self.assertEqual(seatshout.need_line(["  merge it?  "]),
                         ("merge it?", None))
        line, why = seatshout.need_line([])
        self.assertIsNone(line)
        self.assertIn("exactly one", why)


class ShoutTest(Base):
    def test_a_shout_pushes_once_naming_the_seat_its_need_and_its_tab(self):
        rc, out, err = self.shout("a ruling on the gate")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.pushes), 1)
        got = self.pushes[0]
        self.assertIn(SEAT, got["title"])
        self.assertIn("%s needs you: a ruling on the gate" % SEAT, got["body"])
        self.assertIn('tab "%s"' % SEAT, got["body"])
        self.assertIn("/work/helm", got["body"])
        self.assertEqual(got["reply_key"], SEAT)
        self.assertIn("1 of 2", out)
        self.assertIn("a ruling on the gate", out)

    def test_the_push_says_when_no_orca_pane_is_recorded(self):
        os.environ.pop("ORCA_WORKTREE_ID", None)
        rc, _out, err = self.shout()
        self.assertEqual(rc, 0, err)
        self.assertIn("no Orca pane", self.pushes[0]["body"])
        self.assertIn('tab "%s"' % SEAT, self.pushes[0]["body"])

    def test_a_third_shout_in_a_day_is_refused_and_says_when_it_refills(self):
        self.ok(now=NOW)
        self.ok(now=NOW + 60)
        rc, _out, err = self.shout(now=NOW + 120)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.pushes), 2)
        self.assertIn("2 of 2", err)
        self.assertIn(clock(NOW + DAY), err)
        self.assertIn("refill", err)

    def test_a_shout_refills_when_the_oldest_ages_out(self):
        self.ok(now=NOW)
        self.ok(now=NOW + 60)
        rc, _out, err = self.shout(now=NOW + DAY - 1)
        self.assertEqual(rc, 1, err)
        rc, out, err = self.shout(now=NOW + DAY + 1)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.pushes), 3)
        self.assertIn("2 of 2", out)

    def test_budgets_are_per_seat(self):
        self.ok(now=NOW)
        self.ok(now=NOW + 1)
        rc, _out, err = self.shout(now=NOW + 2, seat="seat-b")
        self.assertEqual(rc, 0, err)
        self.assertIn("seat-b", self.pushes[-1]["title"])

    def test_no_phone_configured_refuses_and_spends_nothing(self):
        self.phone = False
        rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("no phone", err)
        self.assertEqual(self.pushes, [])
        self.phone = True
        self.ok(now=NOW + 1)
        self.ok(now=NOW + 2)
        self.assertEqual(len(self.pushes), 2)

    def test_a_failed_push_spends_nothing(self):
        self.deliver = False
        rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("not delivered", err)
        self.deliver = True
        self.ok(now=NOW + 1)
        self.ok(now=NOW + 2)
        rc, _out, err = self.shout(now=NOW + 3)
        self.assertEqual(rc, 1, err)
        self.assertEqual(len(self.pushes), 3)

    def test_an_unreadable_ledger_refuses_and_is_never_read_as_zero(self):
        os.makedirs(seatshout.path())
        rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)
        self.assertEqual(self.pushes, [])
        os.rmdir(seatshout.path())
        self.control()
        self.assertEqual(len(self.pushes), 1)

    def test_a_held_ledger_lock_refuses_rather_than_spending_twice(self):
        target = seatshout.path()
        with eventledger.locked(target, timeout=1) as held:
            self.assertTrue(held)
            got = seatshout.shout(SEAT, "need", "pane", now=NOW,
                                  lock_wait=0.05)
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "busy")
        self.assertIn("another shout holds it", got["message"])
        self.assertEqual(self.pushes, [])
        self.control()
        self.assertEqual(len(self.pushes), 1)

    def test_unopenable_lock_is_unreadable_not_busy(self):  # noqa: VACUOUS_ASSERTION — control() proves push on a repaired lock
        target = seatshout.path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.mkdir(target + ".lock")
        rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)
        self.assertNotIn("another shout holds it", err)
        self.assertEqual(self.pushes, [])
        os.rmdir(target + ".lock")
        self.control()

    def test_an_unadmitted_identity_is_refused_before_any_push(self):
        refusal = "refusing to shout as 'x': this identity is DERIVED"
        with mock.patch.object(seatshout, "_admit", REAL_ADMIT), \
                mock.patch.object(actors, "resolve_actor",
                                  return_value=(None, refusal)) as resolve:
            rc, _out, err = self.run_cli("--need", "something")
        self.assertEqual(rc, 1)
        self.assertIn(refusal, err)
        self.assertEqual(self.pushes, [])
        self.assertEqual(resolve.call_args.kwargs.get("act"), "shout")
        self.control()
        self.assertEqual(len(self.pushes), 1)

    def test_the_ledger_reserves_before_push_and_confirms_after(self):
        def inspect(body, **kwargs):
            rows, unreadable = seatshout.read()
            self.assertIsNone(unreadable)
            self.assertEqual([r["kind"] for r in rows], ["reserved"])
            self.assertEqual(rows[0]["need"], "merge 3876?")
            return True
        with mock.patch.object(notify, "owner_push", inspect):
            self.ok("merge 3876?")
        rows, unreadable = seatshout.read()
        self.assertIsNone(unreadable)
        self.assertEqual([r["kind"] for r in rows], ["reserved", "delivered"])
        self.assertEqual(rows[1]["reservation"], rows[0]["id"])
        self.assertEqual(seatshout.standing(rows, SEAT, 2, NOW)["spent"], 1)

    def test_reservation_write_failure_prevents_a_push(self):  # noqa: VACUOUS_ASSERTION — control() pushes when write works
        with mock.patch.object(seatshout, "_append", return_value=None):
            rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)
        self.assertEqual(self.pushes, [])
        self.control()

    def test_delivery_confirmation_failure_still_spends_and_prevents_retry(self):  # noqa: VACUOUS_ASSERTION — successful delivery is observed in pushes
        append = seatshout._append
        def fail_confirmation(dest, row, now):
            return None if row["kind"] == "delivered" else append(dest, row, now)
        with mock.patch.object(seatshout, "_append", side_effect=fail_confirmation):
            rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("reservation still counts", err)
        self.assertEqual(len(self.pushes), 1)
        seatshout.set_dial(1, by="owner")
        rc, _out, err = self.shout(now=NOW + 1)
        self.assertEqual(rc, 1)
        self.assertIn("1 of 1", err)
        self.assertEqual(len(self.pushes), 1)
        self.assertIsNone(seatshout.standing(seatshout.read()[0], SEAT, 1,
                                             NOW)["open"])

    def test_failed_push_refunds_only_when_cancellation_is_durable(self):
        self.deliver = False
        append = seatshout._append
        def fail_cancellation(dest, row, now):
            return None if row["kind"] == "failed" else append(dest, row, now)
        with mock.patch.object(seatshout, "_append", side_effect=fail_cancellation):
            rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("reservation still counts", err)
        self.deliver = True
        seatshout.set_dial(1, by="owner")
        rc, _out, _err = self.shout(now=NOW + 1)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.pushes), 1)

    def test_exception_after_push_keeps_uncertain_delivery_reserved(self):
        def uncertain(*args, **kwargs):
            self.pushes.append({"body": args[0]})
            raise OSError("transport outcome unknown")
        with mock.patch.object(notify, "owner_push", uncertain):
            rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("outcome is unknown", err)
        self.assertEqual(len(self.pushes), 1)
        seatshout.set_dial(1, by="owner")
        rc, _out, err = self.shout(now=NOW + 1)
        self.assertEqual(rc, 1)
        self.assertIn("1 of 1", err)
        self.assertEqual(len(self.pushes), 1)

    def test_home_admitted_punctuation_names_count_toward_budget(self):  # noqa: VACUOUS_ASSERTION — each loop iteration pushes twice before refusing
        for seat in (".seat", "_seat", "-seat"):
            self.assertEqual(home.validate_seat_arg(seat), seat)
            self.ok(now=NOW, seat=seat)
            self.ok(now=NOW + 1, seat=seat)
            rc, _out, err = self.shout(now=NOW + 2, seat=seat)
            self.assertEqual(rc, 1, err)
            self.assertIn("2 of 2", err)
            self.assertEqual(seatshout.floor(NOW + 2)["seats"][seat]["spent"], 2)
        self.assertEqual(len(self.pushes), 6)

    def test_invalid_complete_budget_row_refuses_instead_of_dropping_it(self):
        self.ok()
        with open(seatshout.path(), "ab") as f:
            f.write(b'{"id":"broken","kind":"shout","seat":"seat-a"}\n')
        rc, _out, err = self.shout(now=NOW + 1)
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)
        self.assertEqual(len(self.pushes), 1)

    def test_unknown_or_incomplete_shout_row_cannot_hide_a_spend(self):  # noqa: VACUOUS_ASSERTION — control() pushes after removing corrupt rows
        for row in ({"id": "bad", "kind": "shou", "seat": SEAT,
                     "at": NOW, "need": "hidden", "pane": "tab"},
                    {"id": "bad", "kind": "shout", "seat": SEAT,
                     "at": NOW, "need": "hidden", "pane": "tab"},
                    {"id": "bad", "kind": "reserved", "seat": SEAT,
                     "at": float("nan"), "need": "hidden", "pane": "tab"}):
            with self.subTest(row=row):
                os.makedirs(os.path.dirname(seatshout.path()), exist_ok=True)
                with open(seatshout.path(), "w", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                rc, _out, err = self.shout()
                self.assertEqual(rc, 1)
                self.assertIn("UNREADABLE", err)
                self.assertEqual(self.pushes, [])
        os.unlink(seatshout.path())
        self.control()

    def test_corrupt_rotated_generation_refuses_before_phone(self):  # noqa: VACUOUS_ASSERTION — corrupt generation planted before refusal
        os.makedirs(os.path.dirname(seatshout.path()), exist_ok=True)
        with open(seatshout.path() + ".1", "wb") as f:
            f.write(b'{not json}\n')
        rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)
        self.assertEqual(self.pushes, [])

    def test_corrupt_complete_budget_row_refuses_shout_and_status(self):
        self.ok()
        with open(seatshout.path(), "ab") as f:
            f.write(b'{not json}\n')
        rc, _out, err = self.shout(now=NOW + 1)
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)
        self.assertEqual(len(self.pushes), 1)
        rc, _out, err = self.run_cli("--status")
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)


class DialTest(Base):
    """The budget is the owner's number, kept the way friction keeps his."""

    def test_the_default_budget_is_two(self):
        got = seatshout.dial()
        self.assertEqual(got["value"], 2)
        self.assertEqual(got["default"], seatshout.DIAL_DEFAULT)
        self.assertEqual(got["max"], seatshout.DIAL_MAX)
        self.assertFalse(got["authored"])

    def test_the_owners_dial_sets_the_budget(self):
        row, problem, code = seatshout.set_dial(1, by="owner")
        self.assertEqual((problem, code), (None, None))
        self.assertEqual(row["dial"]["value"], 1)
        self.assertEqual(row["dial"]["by"], "owner")
        self.ok(now=NOW)
        rc, _out, err = self.shout(now=NOW + 1)
        self.assertEqual(rc, 1)
        self.assertIn("1 of 1", err)

    def test_the_dial_refuses_a_value_it_does_not_admit(self):
        codes = [seatshout.set_dial(bad)[2] for bad in
                 (seatshout.DIAL_MAX + 1, -1, True, "3", None)]
        self.assertEqual(codes, ["refused"] * 5)
        rc, _out, err = self.run_cli("--dial", "lots")
        self.assertEqual(rc, 2, err)
        before = seatshout.dial()
        self.assertFalse(before["authored"])
        self.assertEqual(before["value"], seatshout.DIAL_DEFAULT)
        row, why, code = seatshout.set_dial(seatshout.DIAL_MAX)
        self.assertEqual((why, code), (None, None))
        self.assertEqual(row["dial"]["value"], seatshout.DIAL_MAX)

    def test_a_seat_cannot_set_the_dial(self):
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        rc, _out, err = self.run_cli("--dial", "5")
        self.assertEqual(rc, 1)
        self.assertIn("owner", err)
        self.assertFalse(seatshout.dial()["authored"])
        os.environ.pop("HELM_CHAT_NAME")
        rc, _out, err = self.run_cli("--dial", "5")
        self.assertEqual(rc, 0, err)
        self.assertTrue(seatshout.dial()["authored"])

    def test_clearing_the_name_does_not_turn_a_seat_session_into_owner(self):  # noqa: VACUOUS_ASSERTION — control() proves a separate seat can act
        os.environ["CLAUDE_CODE_SESSION_ID"] = "seat-session"
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        with mock.patch.object(seats_roster, "seat_for_session",
                               return_value=SEAT):
            rc, _out, err = self.run_cli("--dial", "0")
            self.assertEqual(rc, 1, err)
            os.environ.pop("HELM_CHAT_NAME")
            rc, _out, err = self.run_cli("--dial", "0")
        self.assertEqual(rc, 1)
        self.assertIn("only the owner terminal", err)
        self.assertFalse(seatshout.dial()["authored"])
        self.control()

    def test_unrostered_session_cannot_claim_to_be_owner(self):  # noqa: VACUOUS_ASSERTION — owner positive control in test_the_owners_terminal_sets_the_dial_through_the_verb
        os.environ["CLAUDE_CODE_SESSION_ID"] = "unverified-session"
        with mock.patch.object(seats_roster, "seat_for_session",
                               return_value=None):
            rc, _out, err = self.run_cli("--dial", "0")
        self.assertEqual(rc, 1)
        self.assertIn("unverified", err)
        self.assertFalse(seatshout.dial()["authored"])

    def test_the_owners_terminal_sets_the_dial_through_the_verb(self):
        rc, out, err = self.run_cli("--dial", "3")
        self.assertEqual(rc, 0, err)
        self.assertIn("3", out)
        self.assertEqual(seatshout.dial()["value"], 3)

    def test_a_dial_of_zero_silences_every_shout_and_says_the_owner_set_it(self):
        seatshout.set_dial(0, by="owner")
        rc, _out, err = self.shout()
        self.assertEqual(rc, 1)
        self.assertIn("budget to 0", err)
        self.assertIn("owner", err)
        self.assertEqual(self.pushes, [])
        seatshout.set_dial(1, by="owner")
        self.control()
        self.assertEqual(len(self.pushes), 1)

    def test_an_unusable_stored_dial_is_the_default_and_says_so(self):
        os.makedirs(os.path.dirname(home.authored_path()), exist_ok=True)
        pk.write_json(home.authored_path(), {"host": {
            seatshout.DIAL_KEY: {"value": 99, "by": "x", "ts": 1}}})
        got = seatshout.dial()
        self.assertEqual(got["value"], seatshout.DIAL_DEFAULT)
        self.assertTrue(got["problem"])


class SpeakUpTest(Base):
    """Volume 2: one #seats row that wakes the seat's steward, no phone."""

    def setUp(self):
        super().setUp()
        self.integrator = mock.patch.object(
            seats_integrator, "integrator_seat",
            return_value=("integ-seat", None))
        self.integrator.start()
        self.addCleanup(self.integrator.stop)
        self.project = mock.patch.object(
            seatevents, "project_of", lambda seats: {s: None for s in seats})
        self.project.start()
        self.addCleanup(self.project.stop)

    def speak(self, *more):
        return self.run_cli("--need", "a second pair of eyes on 3876",
                            "--speak-up", *more)

    def test_speak_up_posts_one_seats_row_mentioning_the_integrator(self):
        rc, out, err = self.speak()
        self.assertEqual(rc, 0, err)
        rows = self.seats_rows()
        self.assertEqual(len(rows), 1)
        text = rows[0]["text"]
        self.assertTrue(text.startswith("@integ-seat "), text)
        self.assertIn("%s needs: a second pair of eyes on 3876" % SEAT, text)
        self.assertEqual(rows[0]["from"], SEAT)
        self.assertEqual(self.pushes, [])
        self.assertIn("integ-seat", out)
        self.control()
        self.assertEqual(len(self.pushes), 1)

    def test_speak_up_about_credentials_mentions_the_cred_steward(self):
        self.plant({"cred-steward-seat": "creds-steward"})
        rc, _out, err = self.speak("--about", "credentials")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.seats_rows()[0]["text"].startswith(
            "@creds-steward "))

    def test_speak_up_reaches_the_project_lead_when_the_seat_serves_one(self):
        from helm import teams
        with mock.patch.object(seatevents, "project_of",
                               lambda seats: {s: "proj" for s in seats}), \
                mock.patch.object(teams, "read", return_value={"members": [
                    {"seat": "proj-lead", "role": "lead"}]}):
            rc, _out, err = self.speak()
        self.assertEqual(rc, 0, err)
        text = self.seats_rows()[0]["text"]
        self.assertTrue(text.startswith("@proj-lead "), text)
        self.assertNotIn("@integ-seat", text)

    def test_a_project_with_no_lead_falls_to_the_integrator_and_says_why(self):
        from helm import teams
        with mock.patch.object(seatevents, "project_of",
                               lambda seats: {s: "proj" for s in seats}), \
                mock.patch.object(teams, "read", return_value={"members": []}):
            rc, _out, err = self.speak()
        self.assertEqual(rc, 0, err)
        text = self.seats_rows()[0]["text"]
        self.assertTrue(text.startswith("@integ-seat "), text)
        self.assertIn("no steward woken", text)
        self.assertIn("proj", text)

    def test_explicit_project_seats_without_lead_wakes_integrator(self):  # noqa: VACUOUS_ASSERTION — positive @integrator mention comes from posted row
        from helm import teams
        with mock.patch.object(seatevents, "project_of",
                               lambda seats: {s: "proj" for s in seats}), \
                mock.patch.object(teams, "read", return_value={"members": []}):
            rc, _out, err = self.speak("--about", "project-seats")
        self.assertEqual(rc, 0, err)
        text = self.seats_rows()[0]["text"]
        self.assertTrue(text.startswith("@integ-seat "), text)
        self.assertIn("no steward woken", text)
        self.assertEqual(self.pushes, [])

    def test_a_speak_up_that_wakes_nobody_exits_1_and_says_why(self):
        with mock.patch.object(seats_integrator, "integrator_seat",
                               return_value=(None, "no integrator here")):
            rc, _out, err = self.speak()
        self.assertEqual(rc, 1)
        self.assertIn("no integrator here", err)
        self.assertEqual(len(self.seats_rows()), 1)

    def test_speak_up_spends_no_budget(self):
        codes = [self.speak()[0] for _ in range(3)]
        self.assertEqual(codes, [0, 0, 0])
        self.assertEqual(len(self.seats_rows()), 3)
        self.ok(now=NOW)
        self.ok(now=NOW + 1)
        self.assertEqual(len(self.pushes), 2)

    def test_an_unknown_about_is_refused(self):
        rc, _out, err = self.speak("--about", "plumbing")
        self.assertEqual(rc, 2)
        self.assertIn("credentials", err)
        self.assertEqual(self.seats_rows(), [])
        rc, _out, err = self.speak("--about", "build-lanes")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.seats_rows()), 1)


class FloorTest(Base):
    """The budget is information: what each seat spent, and what it asked."""

    def test_status_shows_spent_budget_refill_and_the_open_question(self):
        self.shout("merge 3876?", now=NOW)
        self.shout("pick the dial default", now=NOW + 60)
        self.shout("a cred for gemini", now=NOW + 120, seat="seat-b")
        rc, out, err = self.run_cli("--status", now=NOW + 180)
        self.assertEqual(rc, 0, err)
        self.assertIn("2 shouts per seat per 24 h", out)
        line_a = [ln for ln in out.splitlines() if SEAT in ln][0]
        self.assertIn("2/2", line_a)
        self.assertIn(clock(NOW + DAY), line_a)
        self.assertIn("pick the dial default", line_a)
        line_b = [ln for ln in out.splitlines() if "seat-b" in ln][0]
        self.assertIn("1/2", line_b)
        self.assertIn("a cred for gemini", line_b)

    def test_status_json_carries_the_floor(self):
        self.shout("merge 3876?", now=NOW)
        rc, out, err = self.run_cli("--status", "--json", now=NOW + 10)
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["seats"][SEAT]["spent"], 1)
        self.assertEqual(got["seats"][SEAT]["budget"], 2)
        self.assertIsNone(got["seats"][SEAT]["refills_at"])
        self.assertEqual(got["seats"][SEAT]["open"]["need"], "merge 3876?")
        self.assertEqual(got["budget"]["value"], 2)
        self.assertIsNone(got["unreadable"])
        self.ok("again", now=NOW + 20)
        rc, out, err = self.run_cli("--status", "--json", now=NOW + 30)
        spent = json.loads(out)
        self.assertEqual(spent["seats"][SEAT]["refills_at"], NOW + DAY)

    def test_answered_closes_the_open_question_and_keeps_the_spend(self):
        self.shout("merge 3876?", now=NOW)
        rc, out, err = self.run_cli("--answered", now=NOW + 30)
        self.assertEqual(rc, 0, err)
        self.assertIn("merge 3876?", out)
        after = seatshout.floor(now=NOW + 40)
        self.assertIsNone(after["seats"][SEAT]["open"])
        self.assertEqual(after["seats"][SEAT]["spent"], 1)
        self.assertEqual(len(self.pushes), 1)
        self.ok("and the dial?", now=NOW + 50)
        reopened = seatshout.floor(now=NOW + 60)["seats"][SEAT]["open"]
        self.assertEqual(reopened["need"], "and the dial?")

    def test_answered_with_nothing_open_says_so(self):
        rc, _out, err = self.run_cli("--answered")
        self.assertEqual(rc, 1)
        self.assertIn("no open question", err)

    def test_a_floor_with_no_shouts_says_so(self):
        rc, out, err = self.run_cli("--status")
        self.assertEqual(rc, 0, err)
        self.assertIn("no seat has shouted", out)

    def test_an_unreadable_ledger_is_never_an_empty_floor(self):
        os.makedirs(seatshout.path())
        rc, _out, err = self.run_cli("--status")
        self.assertEqual(rc, 1)
        self.assertIn("UNREADABLE", err)


class WiringTest(Base):
    def test_the_seat_verb_routes_shout(self):
        from helm import seat
        err = StringIO()
        with redirect_stderr(err), redirect_stdout(StringIO()):
            rc = seat.cmd_seat(["shout"])
        self.assertEqual(rc, 2)
        self.assertIn("--need", err.getvalue())
        self.assertEqual(self.pushes, [])
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            rc = seat.cmd_seat(["shout", "--need", "routed?"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.pushes), 1)

    def test_the_help_names_the_three_volumes(self):
        rc, out, _err = self.run_cli("--help")
        self.assertEqual(rc, 0)
        for word in ("TALK", "helm chat post", "SPEAK UP", "SHOUT",
                     "--need", "--status", "--dial"):
            self.assertIn(word, out)

    def test_the_seat_synopsis_names_shout(self):
        from helm import cli_help
        self.assertIn("shout --need", cli_help._VERB_HELP["seat"])

    def test_an_unknown_flag_is_refused_before_anything_runs(self):
        rc, _out, err = self.run_cli("--need", "x", "--loud")
        self.assertEqual(rc, 2)
        self.assertIn("--loud", err)
        self.assertEqual(self.pushes, [])
        self.control()
        self.assertEqual(len(self.pushes), 1)

    def test_a_flag_its_mode_would_ignore_is_refused(self):
        for argv in (("--need", "x", "--json"), ("--status", "--speak-up"),
                     ("--need", "x", "--about", "credentials"),
                     ("--answered", "--need", "x")):
            rc, _out, err = self.run_cli(*argv)
            self.assertEqual(rc, 2, argv)
        self.assertEqual(self.pushes, [])
        self.control()
        self.assertEqual(len(self.pushes), 1)


if __name__ == "__main__":
    unittest.main()
