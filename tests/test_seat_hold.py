#!/usr/bin/env python3
"""A seat helm KNOWS is broken takes no new work (task/3546).

The owner's ruling: "if something is broken we should canonically not keep
using it until it's fixed". The dispatch door refuses a seat in a drop storm,
a seat an operator HELD, and a family walled on money, and each refusal names
the fact, since when, and a seat to use instead. `--force --reason R` admits
the repair work itself and is recorded. An unreadable fact warns and admits.

Real rows through the real door (`DispatchBase`'s ledger, temp home and
fixture repo). The only doubles are the usability join, which would otherwise
walk the host's processes, and, where an arm says so, the transcript count.

THE MODULE IS IMPORTED, NEVER ITS NAMES, so discovery does not run the
dispatch suite twice (see tests/test_dispatch_project_light.py)."""
import json
import os
import time
import unittest
from unittest import mock

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import dispatches, home, pk, seat_usability, silent_drop
from tests import test_dispatches as td


def holds_path():
    """Where helm/seat_hold.py documents the hold log: spelled here so an arm
    run against a tree without the module fails on behaviour, not import."""
    return os.path.join(home.global_dir(), "seat-holds.jsonl")


def hold_verb(argv):
    from helm import seat_hold
    return td.run(seat_hold.cmd_hold, argv)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def _turn(at, text="done"):
    """One transcript line: an assistant turn at `at`, with text or (empty
    text) a drop."""
    return json.dumps({"type": "assistant", "timestamp": _iso(at),
                       "message": {"content": [{"type": "text",
                                                "text": text}]}})


def tail(turns, first=0.0):
    """The transcript read, answered: text turns at `turns`, the tail
    reaching back to epoch `first`."""
    return mock.patch.object(silent_drop, "_text_turns",
                             return_value=(turns, first, None))


def _row(seat, family, age=None):
    """A usability row the join would answer for a measured USABLE seat."""
    now = time.time()
    return {"seat": seat, "family": family, "verdict": seat_usability.USABLE,
            "reason": "", "can_take_work": True, "pane": "LIVE",
            "refusals": (), "refusal": None, "semantic_age_s": age,
            "measured_at": now, "unknown": {}}


class BrokenSeatDoorBase(td.DispatchBase):
    RECIPIENT = "grok"

    def setUp(self):
        super().setUp()
        self.fleet = {"grok": _row("grok", "grok", age=30),
                      "kimi": _row("kimi", "kimi", age=30)}
        # THE JOIN, ANSWERED FROM THIS FLEET: `seat_verdict` asks it for one
        # seat and the alternative asks it for all of them.
        def join(seats=None, **_kw):
            names = seats or sorted(self.fleet)
            return {n: self.fleet[n] for n in names if n in self.fleet}
        patch = mock.patch.object(seat_usability, "join", join)
        patch.start()
        self.addCleanup(patch.stop)
        self._lane = 0

    def send(self, recipient=None, **kwargs):
        self._lane += 1
        return dispatches.send(
            recipient or self.RECIPIENT, "lane-broken-%d" % self._lane,
            "work", self.a, repo=self.repo, kind="build", new_work=True,
            sign=False, **kwargs)

    def cli(self, *extra):
        self._lane += 1
        return td.run(dispatches.cmd_dispatch, [
            "send", self.RECIPIENT, "lane-cli-%d" % self._lane, "the repair",
            "--ref", self.a, "--repo", self.repo, "--kind", "build",
            "--new-work"] + list(extra))

    def storm(self, gap_s=120, entry=None, ago=600, transcript=True):
        """Plant the silent-drop latch: two drops `gap_s` apart, the first
        `ago` seconds back; and, unless `transcript` is False, the seat's
        transcript, reaching back before them and showing them unanswered."""
        now = time.time()
        first, last = now - ago, now - ago + gap_s
        path = silent_drop._state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pk.write_json(path, {self.RECIPIENT: entry or {
            "alerted_at": first, "ts": _iso(first), "session": "s",
            "suppressed": 1, "drops": [_iso(first), _iso(last)]}})
        if transcript:
            self.transcript([_turn(first - 60), _turn(first, text=""),
                             _turn(last, text="")])
        return first, last

    def transcript(self, lines, sid="0" * 36, mtime=None):
        """Plant one session transcript of the seat, where the watchdog
        reads it; `mtime` orders sessions (the newest is read)."""
        proj = os.path.join(home.global_dir(), "seats", self.RECIPIENT,
                            "claude", "projects", "-tmp-proj")
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, sid + ".jsonl")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def plant_hold(self, until=None, at=None, deadline=None):
        """A hold in the documented event shape, written without the verb."""
        path = holds_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({"id": "h%d" % time.time_ns(), "ev": "hold",
                                "seat": self.RECIPIENT,
                                "reason": "its turns drop the tool result",
                                "until": until, "deadline": deadline,
                                "by": "integrator",
                                "at": pk.epoch_ts(at or time.time() - 600)})
                    + "\n")

    def task(self, reason=None):
        from helm import tasks
        row, err = tasks.add("the cursor fix", "integrator", tid=3533,
                             project="helm")
        self.assertIsNone(err, err)
        if reason:
            row, err = tasks.close("task/3533", reason)
            self.assertIsNone(err, err)


class ASeatInADropStormIsRefusedTest(BrokenSeatDoorBase):

    def test_a_storm_refuses_naming_the_fact_since_when_and_a_seat_instead(self):
        first, _last = self.storm()
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("DROP-STORM since %s" % pk.epoch_ts(first), why)
        self.assertIn("2 silent drops", why)
        self.assertIn("Use @kimi instead", why)
        self.assertIn("--force --reason R", why)

    def test_no_transcript_is_UNKNOWN_and_measured_clean_turns_are(self):
        # NO TRANSCRIPT AT ALL: nothing shows whether the seat answered its
        # drops, so the storm is UNKNOWN, and UNKNOWN warns, never refuses.
        self.storm(transcript=False)
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        notes = " ".join(row.get(dispatches._ADMISSION_NOTES, ()))
        self.assertIn("drop-storm reading UNKNOWN", notes)
        self.assertIn("no transcript to read", notes)
        # A TRANSCRIPT SHOWING THEM UNANSWERED is a storm until 3 clean
        # turns follow the last drop.
        first, last = self.storm()
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("0 of 3 clean turns", why)
        after = [last + 5, last + 6, last + 7]
        with tail(after[:2], first=first - 60):
            self.assertIsNone(self.send()[0])
        with tail(after, first=first - 60):
            row, why, _ = self.send()
        self.assertIsNotNone(row, why)

    def test_a_relaunched_seat_whose_drops_were_answered_is_admitted(self):
        """A seat that dropped and answered each drop, then was relaunched:
        its newest transcript is the new session, which begins after the
        drops and cannot show the answers. That is UNKNOWN, not a storm."""
        now = time.time()
        drops = [now - 900, now - 600]
        self.storm(entry={"drops": [_iso(t) for t in drops]},
                   transcript=False)
        self.transcript([_turn(drops[0] - 60), _turn(drops[0], text=""),
                         _turn(drops[0] + 30), _turn(drops[1], text=""),
                         _turn(drops[1] + 30)], sid="1" * 36,
                        mtime=now - 400)
        self.transcript([_turn(now - 300)], sid="2" * 36, mtime=now - 290)
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        notes = " ".join(row.get(dispatches._ADMISSION_NOTES, ()))
        self.assertIn("drop-storm reading UNKNOWN", notes)
        self.assertIn("begins at %s" % pk.epoch_ts(now - 300), notes)
        # CONTROL: two new drops the new session shows unanswered are a
        # storm, whatever the old session held.
        self.storm(entry={"drops": [_iso(t) for t in
                                    drops + [now - 200, now - 100]]},
                   transcript=False)
        self.transcript([_turn(now - 300), _turn(now - 200, text=""),
                         _turn(now - 100, text="")], sid="2" * 36,
                        mtime=now - 90)
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("DROP-STORM since %s" % pk.epoch_ts(now - 200), why)
        self.assertIn("2 silent drops", why)

    def test_an_empty_tail_is_UNKNOWN_and_a_covering_one_is_read(self):
        now = time.time()
        entry = {"drops": [_iso(now - 400), _iso(now - 300)]}
        rd, err = silent_drop.storm("grok", latch={"grok": entry}, lines=[])
        self.assertIsNone(rd)
        self.assertIn("cannot be read", err)
        rd, err = silent_drop.storm("grok", latch={"grok": entry},
                                    lines=[_turn(now - 350, text="")])
        self.assertIsNone(rd)
        self.assertIn("begins at", err)
        rd, err = silent_drop.storm("grok", latch={"grok": entry},
                                    lines=[_turn(now - 500, text="")])
        self.assertIsNone(err)
        self.assertEqual(rd["drops"], 2)

    def test_a_self_recovering_drop_class_never_trips_a_storm(self):
        """The watchdog's known reasoning-only class: the seat answers each
        drop with text before its next one. Seen at about half the turns of a
        working codex seat, it is not a broken seat."""
        now = time.time()
        drops = [now - 900, now - 600, now - 300]
        entry = {"drops": [_iso(t) for t in drops]}
        answered = [t + 30 for t in drops]
        self.storm(entry=entry)
        with tail(answered):
            row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        rd, err = silent_drop.storm("grok", latch={"grok": entry},
                                    lines=[_turn(now - 1000, text="")])
        # CONTROL: the same drops with no answer between them are a storm.
        self.assertIsNone(err)
        self.assertEqual(rd["drops"], 3)
        with tail(answered):
            self.assertEqual(silent_drop.storm("grok", latch={"grok": entry}),
                             (None, None))
        # ONE unanswered drop before an answered run: still no storm.
        with tail(answered[1:]):
            rd, err = silent_drop.storm("grok", latch={"grok": entry})
        self.assertEqual((rd, err), (None, None))

    def test_an_old_storm_ages_out_and_its_window_is_a_variable(self):
        self.storm(ago=7 * 3600)
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        with mock.patch.dict(os.environ,
                             {silent_drop.STORM_AGE_ENV: str(8 * 3600)}):
            row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("DROP-STORM", why)
        self.assertIn("ages out 8h after its last drop", why)
        # 0 is no window at all, so it reads as the default, not as a door
        # that every storm ages out of at once.
        self.storm(ago=3600)
        with mock.patch.dict(os.environ, {silent_drop.STORM_AGE_ENV: "0"}):
            self.assertEqual(silent_drop.storm_age_s(),
                             silent_drop.STORM_AGE_S)
            row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("ages out 6h after its last drop", why)

    def test_clear_lifts_a_storm_and_a_new_storm_after_it_refuses(self):
        self.storm()
        rc, out, _ = hold_verb([])
        self.assertIn("grok DROP-STORM since", out)
        rc, out, err = hold_verb(["grok", "--clear", "--reason",
                                  "the proxy was fixed"])
        self.assertEqual(rc, 0, err)
        self.assertIn("DROP-STORM", out)
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        rc, out, _ = hold_verb([])
        self.assertNotIn("DROP-STORM", out)
        # the drops the clear acknowledged stay acknowledged; two NEW ones
        # after it are a new storm.
        now = time.time() + 5
        entry = {"drops": [_iso(now - 1200), _iso(now - 1100), _iso(now),
                           _iso(now + 60)]}
        self.storm(entry=entry)
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("2 silent drops", why)

    def test_clear_on_a_seat_neither_held_nor_storming_is_refused(self):
        rc, _out, err = hold_verb(["grok", "--clear", "--reason", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("neither held nor in a drop storm", err)

    def test_a_force_past_a_storm_is_listed(self):
        self.storm()
        rc, _out, err = self.cli("--force", "--reason", "repair the proxy")
        self.assertEqual(rc, 0, err)
        rc, out, _ = hold_verb([])
        self.assertIn("forced past a broken fact with no hold in force", out)
        self.assertIn("past DROP-STORM", out)

    def test_drops_far_apart_are_not_a_storm(self):
        self.storm(gap_s=silent_drop.STORM_GAP_S + 60)
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)

    def test_the_clean_turn_count_reads_only_text_turns_after_the_last_drop(self):
        now = time.time()

        def turn(at, text="done", sidechain=False):
            return json.dumps({"type": "assistant", "isSidechain": sidechain,
                               "timestamp": _iso(at), "message": {
                                   "content": [{"type": "text",
                                                "text": text}]}})
        lines = [turn(now - 500), turn(now - 100), turn(now - 90, text=""),
                 turn(now - 80, sidechain=True), turn(now - 70), turn(now - 60)]
        entry = {"drops": [_iso(now - 400), _iso(now - 300)]}
        rd, err = silent_drop.storm("grok", latch={"grok": entry},
                                    lines=lines)
        self.assertIsNone(err)
        self.assertEqual(rd["clean_turns"], 3)
        self.assertTrue(rd["healthy"])
        rd, _ = silent_drop.storm("grok", latch={"grok": entry},
                                  lines=lines[:2])
        self.assertFalse(rd["healthy"])

    def test_the_watchdog_latch_keeps_every_drop_of_a_run(self):
        """The storm is read from the latch `check` writes, and a suppressed
        drop is a drop: before this, the latch kept only the alerted one."""
        now = time.time()
        finds = [[{"seat": "grok", "ts": _iso(now - 200), "session": "s"}],
                 [{"seat": "grok", "ts": _iso(now - 100), "session": "s"}]]
        with mock.patch.object(silent_drop, "scan",
                               side_effect=lambda seats=None: finds.pop(0)):
            silent_drop.check(post=False)
            silent_drop.check(post=False)
        rd, err = silent_drop.storm("grok",
                                    lines=[_turn(now - 300, text="")])
        self.assertIsNone(err)
        self.assertEqual(rd["drops"], 2)


class AWalledFamilyIsRefusedTest(BrokenSeatDoorBase):
    RECIPIENT = "codex"

    def test_a_walled_family_names_its_reading_and_a_seat_of_another_family(self):
        from helm import burnflags, codexbudget
        at = time.time()
        rows = [{"email": "a%d@x.example" % i, "account_id": "acct-%d" % i,
                 "plan": "team", "state": "ok", "status": "allowed",
                 "longest_pct": p, "windows": [{
                     "label": "7d", "used_percent": p,
                     "reset_after_seconds": 3600}]}
                for i, p in enumerate((100.0, 99.0))]
        codexbudget.write_snapshot(rows, ceiling=90.0, now=at)
        burnflags.write_snapshot({"ceiling": 90.0, "money": {"codex": rows},
                                  "money_measured_at": {"codex": at},
                                  "upstream": {}, "anthropic_history": None,
                                  "declarations": None}, now=at)
        self.fleet.update({"codex": _row("codex", "codex", age=30),
                           "codex-2": _row("codex-2", "codex", age=30)})
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("walled on MONEY", why)
        self.assertIn("(measured %s)" % pk.epoch_ts(at), why)
        # codex-2 is USABLE but walled with its family: the seat named is
        # one of another family.
        self.assertRegex(why, r"Use @(grok|kimi) instead")
        self.assertNotIn("@codex-2", why)


class AnOperatorHoldTest(BrokenSeatDoorBase):

    def test_a_hold_refuses_naming_who_since_why_and_its_fix(self):
        self.plant_hold(until="task/3533")
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("HOLD since", why)
        self.assertIn("set by integrator", why)
        self.assertIn("its turns drop the tool result", why)
        self.assertIn("lifts when task/3533 lands", why)
        self.assertIn("Use @kimi instead", why)

    def test_the_hold_lifts_when_its_task_lands_and_not_when_it_is_dropped(self):
        self.plant_hold(until="task/3533")
        self.task()
        self.assertIsNone(self.send()[0])                   # open: it stands
        from helm import tasks
        tasks.close("task/3533", "duplicate of another task")
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("closed WITHOUT landing", why)
        tasks.update("task/3533", status="open")
        tasks.close("task/3533", "landed train9 abcdef12345 (gate:x, Ran 1)")
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.assertTrue(any("task/3533 landed" in n for n in
                            row.get(dispatches._ADMISSION_NOTES, ())),
                        row.get(dispatches._ADMISSION_NOTES))

    def test_a_held_seat_that_keeps_working_stays_held(self):
        """An operator holds a seat for a defect helm does not measure, so a
        turn the seat completes after the hold is not evidence of the fix."""
        self.plant_hold(at=time.time() - 600)
        for age in (6000, None, 60, 1):
            self.fleet["grok"] = _row("grok", "grok", age=age)
            row, why, _ = self.send()
            self.assertIsNone(row, "age %r lifted the hold" % age)
            self.assertIn("lifts only by `helm seat hold grok --clear", why)
        rc, _out, err = hold_verb(["grok", "--clear", "--reason", "fixed"])
        self.assertEqual(rc, 0, err)
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)

    def test_an_until_deadline_lifts_the_hold_and_nothing_before_it(self):
        self.plant_hold(deadline=pk.epoch_ts(time.time() + 3600))
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("lifts at", why)
        self.plant_hold(deadline=pk.epoch_ts(time.time() - 60))
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.assertTrue(any("--until" in n and "passed" in n for n in
                            row.get(dispatches._ADMISSION_NOTES, ())))

    def test_the_verb_takes_a_time_until_and_refuses_a_past_or_bad_one(self):
        rc, out, err = hold_verb(["grok", "--reason", "r", "--until", "2h"])
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, r"until \d{4}-\d\d-\d\dT")
        for bad, said in (("2020-01-01T00:00Z", "already past"),
                          ("tomorrow", "neither a task id")):
            rc, _out, err = hold_verb(["grok", "--reason", "r",
                                       "--until", bad])
            self.assertEqual(rc, 2, bad)
            self.assertIn(said, err)

    def test_until_a_task_the_ledger_does_not_hold_is_refused(self):
        rc, _out, err = hold_verb(["grok", "--reason", "r",
                                   "--until", "task/999999"])
        self.assertEqual(rc, 2)
        self.assertIn("names no task in the ledger", err)
        self.task(reason="landed train1 abcdef12345 (gate:x, Ran 1)")
        rc, _out, err = hold_verb(["grok", "--reason", "r",
                                   "--until", "task/3533"])
        self.assertEqual(rc, 2)
        self.assertIn("already closed", err)
        # a hold already naming an absent task says so, and stands
        self.plant_hold(until="task/999999")
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("not in the task ledger", why)

    def test_the_alternative_names_a_seat_whose_hold_lifted(self):
        from helm import seat_hold, tasks
        self.plant_hold(until="task/3533")
        self.task()
        self.assertIn("No other seat measures USABLE",
                      seat_hold.instead("kimi"))
        tasks.close("task/3533", "landed train9 abcdef12345 (gate:x, Ran 1)")
        self.assertIn("Use @grok instead", seat_hold.instead("kimi"))

    def test_setting_a_hold_names_the_rows_the_seat_still_owes(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        rc, out, err = hold_verb(["grok", "--reason", "drops tool results"])
        self.assertEqual(rc, 0, err)
        self.assertIn(row["id"], out)
        self.assertIn("helm seat reassign grok --to", out)

    def test_the_router_reads_the_same_broken_seats(self):
        from helm import seat_hold
        self.plant_hold()
        self.assertEqual(seat_hold.broken_seats(["grok", "kimi"]),
                         {"grok": ["HOLD"]})

    def test_a_task_named_hold_does_not_lift_on_a_healthy_reading(self):
        self.plant_hold(until="task/3533", at=time.time() - 600)
        self.task()
        self.fleet["grok"] = _row("grok", "grok", age=60)
        row, why, _ = self.send()
        self.assertIsNone(row)
        self.assertIn("lifts when task/3533 lands", why)

    def test_the_verb_sets_lists_and_clears_and_the_log_keeps_the_hold(self):
        self.task()
        rc, out, err = hold_verb([
            "grok", "--reason", "drops tool results", "--until", "3533"])
        self.assertEqual(rc, 0, err)
        self.assertIn("until task/3533 lands", out)
        self.assertIsNone(self.send()[0])
        rc, out, _ = hold_verb([])
        self.assertIn("grok HOLD since", out)
        rc, out, err = hold_verb(["grok", "--clear",
                                                   "--reason", "fixed"])
        self.assertEqual(rc, 0, err)
        self.assertIsNotNone(self.send()[0])
        with open(holds_path()) as f:
            self.assertEqual([json.loads(ln)["ev"] for ln in f],
                             ["hold", "clear"])


class AHealthySeatIsAdmittedTest(BrokenSeatDoorBase):

    def test_a_healthy_seat_is_admitted_with_no_broken_seat_note(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.assertFalse([n for n in row.get(dispatches._ADMISSION_NOTES, ())
                          if "hold" in n or "storm" in n])


class ForceAdmitsTheRepairAndIsRecordedTest(BrokenSeatDoorBase):

    def test_force_needs_a_reason_and_the_admission_is_recorded(self):
        self.plant_hold(until="task/3533")
        rc, _out, err = self.cli()
        self.assertNotEqual(rc, 0)
        self.assertIn("BROKEN", err)
        rc, _out, err = self.cli("--force")
        self.assertEqual(rc, 2)
        self.assertIn("--force past HOLD on grok needs --reason", err)
        rc, _out, err = self.cli("--reason", "repair it")
        self.assertEqual(rc, 2)
        self.assertIn("--reason rides --force", err)
        rc, _out, err = self.cli("--force", "--reason", "repair the proxy")
        self.assertEqual(rc, 0, err)
        self.assertIn("FORCED past HOLD on @grok", err)
        with open(holds_path()) as f:
            forced = [json.loads(ln) for ln in f
                      if json.loads(ln)["ev"] == "forced"]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["facts"], ["HOLD"])
        self.assertEqual(forced[0]["reason"], "repair the proxy")
        self.assertIn(forced[0]["row"], dispatches.rows())
        rc, out, _ = hold_verb([])
        self.assertIn("forced %s past HOLD" % forced[0]["row"], out)

    def test_a_forced_rebind_to_a_held_seat_is_refused_and_records_nothing(self):  # noqa: VACUOUS_ASSERTION — the absent forced event IS the contract; the send arm above writes one through the same log
        """Every manual rebind carries --force, but it attests the SOURCE is
        starved, and rebind passes no force to the add that writes the
        successor: a held target is refused whatever the flags, and nothing
        is recorded as forced. Handing a held seat its repair work is
        `dispatch send --force --reason R`."""
        from helm import seats
        for name in ("grok", "kimi"):          # rebind's target must be joined
            seats.write_roster(name, presence_beat=False)
        row, why, _ = self.send(recipient="kimi")
        self.assertIsNotNone(row, why)
        self.plant_hold(until="task/3533")
        rc, _out, err = td.run(dispatches.cmd_dispatch, [
            "rebind", row["id"], "--to", "grok", "--force",
            "--reason", "grok takes its own repair", "--repo", self.repo])
        self.assertEqual(rc, 1)
        self.assertIn("is BROKEN and takes no new work", err)
        self.assertEqual(dispatches.snapshot()[0][row["id"]]["status"], "open")
        with open(holds_path()) as f:
            self.assertEqual([json.loads(ln)["ev"] for ln in f], ["hold"])

    def test_force_to_a_healthy_seat_needs_no_reason_and_records_nothing(self):  # noqa: VACUOUS_ASSERTION — the absent hold log IS the contract; the arm above writes that same path through the same door
        rc, _out, err = self.cli("--force")
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(holds_path()))


class AnUnreadableFactWarnsTest(BrokenSeatDoorBase):

    def test_an_unreadable_hold_log_or_latch_admits_and_says_UNKNOWN(self):
        path = holds_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{ not json\n")
        os.makedirs(os.path.dirname(silent_drop._state_path()), exist_ok=True)
        with open(silent_drop._state_path(), "w") as f:
            f.write("{ not json")
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        notes = " ".join(row.get(dispatches._ADMISSION_NOTES, ()))
        self.assertIn("seat hold UNKNOWN", notes)
        self.assertIn("drop-storm reading UNKNOWN", notes)

    def test_a_corrupt_hold_event_or_latch_degrades_the_verb(self):
        """Well-formed JSON of the wrong shape: a hold whose seat is not a
        name, and a latch that is a list. The door admits on them; the verb
        lists and clears past them, and says the latch is UNKNOWN."""
        path = holds_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"id": "h1", "ev": "hold", "seat": ["grok"],
                                "reason": "r", "at": pk.now_ts()}) + "\n")
        self.plant_hold(until=["task/3533"])
        latch = silent_drop._state_path()
        os.makedirs(os.path.dirname(latch), exist_ok=True)
        pk.write_json(latch, [{"grok": {"drops": []}}])
        rc, out, err = hold_verb([])
        self.assertEqual(rc, 1, err)
        self.assertIn("grok HOLD since", out)
        self.assertIn("drop storms UNKNOWN", err)
        row, why, _ = self.send(recipient="kimi")
        self.assertIsNotNone(row, why)
        self.assertIn("drop-storm reading UNKNOWN", " ".join(
            row.get(dispatches._ADMISSION_NOTES, ())))
        rc, out, err = hold_verb(["grok", "--clear", "--reason", "fixed"])
        self.assertEqual(rc, 0, err)
        self.assertIn("grok cleared (HOLD", out)
        pk.write_json(latch, {"grok": {"drops": 5}})
        rc, out, err = hold_verb([])
        self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()
