#!/usr/bin/env python3
"""The dark-seat mover under canon (task/3881, prior
dark-confirmed-seat-reassigns-everything): a CONFIRMED dark seat's work moves
on the idle-dispatch tick, with no one typing --apply.

The fixture is test_turnwall's MoverBase: real dispatch and task ledgers under
a temp home, a fleet of grok and kimi answered by a patched usability join,
and a burn snapshot that reads grok RED. Nothing here reads the live fleet.
"""
import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import burnflags, dispatches, home, localnames, tasks
from tests import test_seat_hold as tsh
from tests import test_turnwall as ttw


def plant_names(table):
    os.makedirs(home.global_dir(), exist_ok=True)
    with open(os.path.join(home.global_dir(), localnames.CONFIG), "w",
              encoding="utf-8") as f:
        json.dump(table, f)
    localnames._cache["stat"] = None


class Base(ttw.MoverBase):
    def setUp(self):
        super().setUp()
        self.seat_rows = []
        from helm import chat
        # BOTH ROOMS' LINES: the mover's own (#helm) and its #seats row.
        patch = mock.patch.object(chat, "post", lambda text, **kw: (
            self.posts.append(text) if kw.get("who") == "idle-dispatch"
            else self.seat_rows.append(text)
            if kw.get("who") == "seat-events" else None) or {"id": "x"})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(lambda: localnames._cache.update(stat=None))

    def unreachable(self, pane):
        """grok as the usability join measures a seat with no armed beacon."""
        from helm import seat_usability
        self.fleet["grok"] = dict(
            self.fleet["grok"], verdict=seat_usability.UNUSABLE,
            can_take_work=False, pane=pane, reachable=False,
            reachable_why="no live beacon",
            refusals=(("pane",) if pane is False else ()) + (
                seat_usability.REFUSE_REACHABLE,))

    def flags(self, **grok):
        out = json.loads(json.dumps(ttw.RED_GROK))
        out["grok"].update(grok)
        return out

    def run_mover(self, flags=None, apply=True, now=None):
        from helm import darkmove
        with self.dark(flags or ttw.RED_GROK):
            return darkmove.run(apply=apply, now=now)


class ConfirmedDarkTest(Base):
    """(1) What counts as CONFIRMED dark: hysteresis, never a hair trigger."""

    def test_a_wall_whose_reset_is_near_stays_put(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        soon = self.flags(expires_at=time.time() + 20 * 60)
        lines, moves = self.run_mover(soon)
        self.assertEqual(moves, [], lines)
        self.assertIn("waits for its reset", "\n".join(lines))
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")
        # THE CONTROL: a far reset, and no reset, each move it
        far = self.flags(expires_at=time.time() + 5 * 3600)
        lines, moves = self.run_mover(far, apply=False)
        self.assertEqual([m["id"] for m in moves], [row["id"]], lines)

    def test_a_gone_pane_moves_only_past_its_bound(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        self.unreachable(pane=False)
        lines, moves = self.run_mover(ttw.GREEN_ALL)
        self.assertEqual(moves, [], lines)
        self.assertIn("pane bound", "\n".join(lines))
        from helm import darkmove
        lines, moves = self.run_mover(
            ttw.GREEN_ALL, now=time.time() + darkmove.pane_gone_s() + 1)
        self.assertEqual([(m["id"], m["moved"]) for m in moves],
                         [(row["id"], True)], lines)

    def test_the_seat_event_starts_the_clock(self):
        """The family's #seats wall episode (task/3876) dates the darkness,
        so a wall an hour old confirms on the mover's first sighting."""
        from helm import seatevents
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        seatevents.announce([seatevents.event(
            "credentials", "family:grok", "wall", "MONEY|t0",
            "grok dark QUOTA")], now=time.time() - 3600,
            post=lambda _t, _r: None, push=lambda _b, _t: True)
        with mock.patch.dict(os.environ, {"HELM_DARK_MOVE_GRACE_S": "600"}):
            lines, moves = self.run_mover(apply=False)
        self.assertEqual([m["id"] for m in moves], [row["id"]], lines)

    def test_an_open_seat_event_alone_moves_nothing(self):  # noqa: VACUOUS_ASSERTION — the same fixture moves the row in test_the_seat_event_starts_the_clock; here the burn read is GREEN
        """A lost closing edge must not move live work: the episode dates a
        darkness the burn read confirms, and never stands in for it."""
        from helm import seatevents
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        seatevents.announce([seatevents.event(
            "credentials", "family:grok", "wall", "MONEY|t0",
            "grok dark QUOTA")], now=time.time() - 3600,
            post=lambda _t, _r: None, push=lambda _b, _t: True)
        lines, moves = self.run_mover(ttw.GREEN_ALL)
        self.assertEqual(moves, [], lines)
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")


class KillSwitchTest(Base):
    """The automatic tick moves; one local-names key makes it report only."""

    def tick(self):
        from helm import idle_dispatch
        res = {"findings": [], "alerted": [], "woke": [], "undelivered": [],
               "redeliverable": []}
        buf = io.StringIO()
        with self.dark(), \
                mock.patch.object(idle_dispatch, "check", return_value=res), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(idle_dispatch.cmd_idle_dispatch([]), 0)
        return buf.getvalue()

    def test_the_tick_moves_and_the_kill_switch_makes_it_report(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        plant_names({"dark-seat-mover": "off"})
        out = self.tick()
        self.assertIn("would move dispatch %s" % row["id"][:12], out)
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "open")
        plant_names({})
        out = self.tick()
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "cancelled",
                         out)

    def test_doctor_shows_the_switch(self):
        from helm import doctor
        self.assertIn("check_dark_mover", doctor.CHECKS)
        rows = doctor.check_dark_mover()
        self.assertEqual([r[0] for r in rows], [doctor.OK], rows)
        self.assertIn("automatic", rows[0][1])
        plant_names({"dark-seat-mover": "off"})
        rows = doctor.check_dark_mover()
        self.assertEqual([r[0] for r in rows], [doctor.WARN], rows)
        self.assertIn("OFF", rows[0][1])


class CarriedAndHeldTest(Base):
    def test_a_carried_predecessor_is_cancelled_naming_its_carrier(self):
        parent = self.add(recipient="grok")
        child = self.add(recipient="kimi", supersedes=parent["id"])
        lines, moves = self.run_mover()
        after = dispatches.rows()[parent["id"]]
        self.assertEqual(after["status"], "cancelled", lines)
        self.assertIn(child["id"][:12], json.dumps(after))
        # NEVER MOVED: no second successor was minted for it
        kids = [r for r in dispatches.rows().values()
                if r.get("supersedes") == parent["id"]]
        self.assertEqual([k["id"] for k in kids], [child["id"]])
        self.assertEqual([m for m in moves if m["id"] == parent["id"]
                          and m["what"] == "dispatch"], [])

    def test_a_held_row_is_released_and_moved(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        held, err = dispatches.mark_hold(row["id"], "waiting on the fab")
        self.assertIsNone(err, err)
        lines, moves = self.run_mover()
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "cancelled",
                         lines)
        kids = [r for r in dispatches.rows().values()
                if r.get("supersedes") == row["id"]]
        self.assertEqual([k["recipient"] for k in kids], ["kimi"])

    def test_a_held_row_released_by_the_mover_records_the_mover(self):
        """The mover's release names the MOVER (task/4149): a deliberate
        release must never read as an unattributed one, and the mover is a
        fixed hand, not a seat, so it records its own name."""
        from helm import darkmove
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        held, err = dispatches.mark_hold(row["id"], "waiting on the fab")
        self.assertIsNone(err, err)
        lines, moves = self.run_mover()
        replayed = dispatches.rows()[row["id"]]
        self.assertEqual(replayed["status"], "cancelled", lines)
        self.assertEqual(replayed.get("release_actor"),
                         darkmove.SWITCH_KEY, replayed)

    def test_an_owner_gated_hold_stays_with_its_gate(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        _held, err = dispatches.mark_hold(row["id"], "his word on the release",
                                          owner_gated=True)
        self.assertIsNone(err, err)
        lines, moves = self.run_mover()
        self.assertEqual(moves, [], lines)
        self.assertIn("owner-gated", "\n".join(lines))
        self.assertEqual(dispatches.rows()[row["id"]]["status"], "held")


class RoleTableTest(Base):
    """(2) The target is a live seat of the dark seat's ROLE, from a declared
    table; with none live, the component steward, and the line says so."""

    def setUp(self):
        super().setUp()
        self.fleet["seat-c"] = tsh._row("seat-c", "codex", age=30)
        self.flagged = json.loads(json.dumps(ttw.RED_GROK))
        self.flagged["codex"] = dict(ttw.RED_GROK["kimi"], family="codex")

    def test_a_move_goes_to_a_live_seat_of_the_same_role(self):
        self.add(recipient="seat-c")              # seat-c owes one row
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        plant_names({"builder-seats": ["grok", "seat-c"]})
        lines, moves = self.run_mover(self.flagged, apply=False)
        self.assertEqual([(m["id"], m["to"]) for m in moves],
                         [(row["id"], "seat-c")], lines)

    def test_no_live_seat_of_the_role_goes_to_the_steward_and_says_so(self):
        row, why, _ = self.send()
        self.assertIsNotNone(row, why)
        plant_names({"builder-seats": ["grok", "seat-d"],
                     "cred-steward-seat": "kimi"})
        lines, moves = self.run_mover(self.flagged, apply=False)
        self.assertEqual([(m["id"], m["to"]) for m in moves],
                         [(row["id"], "kimi")], lines)
        self.assertIn("no live builder seat: to the credentials steward",
                      "\n".join(lines))


class SeatsRowTest(Base):
    """(5) Each pass posts ONE #seats row per dark seat it moved work off,
    mentioning the steward, with a count by kind and the receiving seat."""

    def test_one_row_per_dark_seat_with_counts_by_kind_and_receiver(self):  # noqa: VACUOUS_ASSERTION — three moves and one exact #seats row are pinned before the empty #helm list
        from helm import seat_reassign
        plant_names({"cred-steward-seat": "meta"})
        project = os.path.basename(self.repo)
        for _ in range(2):
            row, why, _ = self.send()
            self.assertIsNotNone(row, why)
        task, err = tasks.add("the balance top-up", "grok", project=project)
        self.assertIsNone(err, err)
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "pane gone")):
            lines, moves = self.run_mover()
        self.assertEqual(sum(1 for m in moves if m["moved"]), 3, lines)
        self.assertEqual(len(self.seat_rows), 1, self.seat_rows)
        text = self.seat_rows[0]
        self.assertTrue(text.startswith("@meta "), text)
        self.assertIn("@grok", text)
        self.assertIn("2 build rows and 1 task to @kimi", text)
        self.assertEqual(self.posts, [], "no per-move #helm line")
        # THE NEXT PASS moves nothing and posts nothing
        self.run_mover()
        self.assertEqual(len(self.seat_rows), 1)


class OneLinePerSeatPerDecisionTest(Base):
    """The flood (LAND 546): 137 "@qwenlocal keeps its rows" lines in six
    minutes, one per held row. A seat's decision posts ONCE, to #seats, with
    its steward mentioned; the same decision on later passes posts nothing,
    and a changed one posts once. The main room gets nothing."""

    ROWS, PASSES = 4, 3

    def deaf_rows(self):
        out = []
        for _ in range(self.ROWS):
            row, why, _ = self.send()
            self.assertIsNotNone(row, why)
            _held, err = dispatches.mark_hold(row["id"], "waiting on the fab")
            self.assertIsNone(err, err)
            out.append(row)
        self.unreachable(pane=True)          # a live pane, no armed beacon
        return out

    def lead(self):
        from helm import seatevents, teams
        return mock.patch.multiple(
            seatevents, project_of=lambda seats: {s: "acme" for s in seats}), \
            mock.patch.object(teams, "read", return_value={"members": [
                {"seat": "acme-lead", "role": "lead"}]})

    def test_held_rows_on_a_deaf_seat_across_passes_post_once(self):  # noqa: VACUOUS_ASSERTION — exactly one #seats row, its steward and its text are pinned before the empty main-room list
        self.deaf_rows()
        project, team = self.lead()
        with project, team:
            for at in range(self.PASSES):
                lines, moves = self.run_mover(
                    ttw.GREEN_ALL, now=time.time() + at * 300)
                self.assertEqual(moves, [], lines)
        self.assertEqual(len(self.seat_rows), 1, self.seat_rows)
        self.assertTrue(self.seat_rows[0].startswith("@acme-lead "),
                        self.seat_rows[0])
        self.assertIn("@grok keeps its rows", self.seat_rows[0])
        self.assertEqual(self.posts, [], "nothing to the main room")

    def test_a_changed_decision_posts_once_more(self):  # noqa: VACUOUS_ASSERTION — the two #seats rows and the second one's text are pinned before the empty main-room list
        self.deaf_rows()
        project, team = self.lead()
        with project, team:
            self.run_mover(ttw.GREEN_ALL)
            self.assertEqual(len(self.seat_rows), 1, self.seat_rows)
            # THE DECISION CHANGES: the pane is gone and kimi is walled, so
            # the rows are dark with nowhere to go
            self.unreachable(pane=False)
            walled = json.loads(json.dumps(ttw.GREEN_ALL))
            walled["kimi"]["axes"] = {"money": "RED", "reach": None}
            with mock.patch.dict(os.environ, {"HELM_DARK_MOVE_PANE_S": "0"}):
                for at in (1, 2):
                    self.run_mover(walled, now=time.time() + at * 300)
        self.assertEqual(len(self.seat_rows), 2, self.seat_rows)
        self.assertIn("stays on @grok", self.seat_rows[1])
        self.assertIn("4 dispatch rows", self.seat_rows[1])
        self.assertEqual(self.posts, [])


class StartedWorkTest(Base):
    """task/3881's remainder: a confirmed-dark seat's STARTED work (an
    in_progress task, a lane lease, lane commits off trunk) moves to a live
    builder when its room holds no uncommitted change. A dirty room moves
    nothing and asks the integrator once. Measured instance: task/4019 sat
    19h as 4 unreviewed commits on a walled kimi's lane."""

    def setUp(self):
        super().setUp()
        from helm import seats
        # A ROSTER THAT LISTS THE TARGET: a lease moved to a seat on no
        # roster row is orphaned, and `rebind_claim_holder` refuses that.
        # seat-a stands in for the integrator (HELM_INTEGRATOR_SEAT below).
        for name in ("grok", "kimi", "seat-a"):
            seats.write_roster(name, presence_beat=False)

    def dead(self):
        from helm import seat_reassign
        return mock.patch.object(seat_reassign, "source_disposition",
                                 return_value=(seat_reassign.SOURCE_DEAD,
                                               "pane gone"))

    def started(self, title, priority="P0", slug="wake-slicec"):
        """An in_progress task of grok's with a lane carrying one commit off
        trunk, checked out in its room, and grok's live lease on it."""
        from helm import pk, seats
        from helm.work import _lanes
        task, err = tasks.add(title, "grok", project=os.path.basename(
            self.repo), priority=priority, status="in_progress")
        self.assertIsNone(err, err)
        lane = "%s-%s" % (slug, task["id"].split("/")[-1])
        self.git("checkout", "-q", "-b", "lane/" + lane, self.a)
        tip = self.commit("finished work on " + lane)
        self.git("checkout", "-q", self.main)
        room = _lanes.lane_path(self.repo, lane)
        self.git("worktree", "add", "-q", room, "lane/" + lane)
        # A ROW IN THE LEDGER'S OWN SHAPE (`seats_claims` refuses a ledger
        # with no `_fence` counter or a row with no `ts`, and a refused
        # ledger is a lease that does not move).
        ledger = pk.read_json(seats.claims_path(), {}) or {}
        fence = int(ledger.get("_fence") or 0) + 1
        ledger[_lanes.resource(self.repo, lane)] = {
            "holder": "grok", "lease": "L-" + lane, "fence": fence,
            "session": None, "ts": pk.epoch_ts(time.time()),
            "exp_wall": time.time() + 3600,
            "exp_mono": time.monotonic() + 3600}
        ledger["_fence"] = fence
        self.claims(ledger)
        return task, lane, room, tip

    def holder(self, lane):
        from helm import pk, seats
        from helm.work import _lanes
        return (pk.read_json(seats.claims_path(), {}).get(
            _lanes.resource(self.repo, lane)) or {}).get("holder")

    def test_a_clean_started_P0_moves_to_a_live_builder_with_its_lease(self):
        plant_names({"builder-seats": ["grok", "kimi"]})
        task, lane, room, tip = self.started("the wake slice")
        with self.dead():
            lines, moves = self.run_mover()
        self.assertEqual([(m["id"], m["to"], m["moved"]) for m in moves],
                         [(task["id"], "kimi", True)], lines)
        after = tasks.rows()[task["id"]]
        self.assertEqual((after["owner"], after["status"]),
                         ("kimi", "in_progress"))
        self.assertEqual(self.holder(lane), "kimi")
        said = [p for p in self.posts if task["id"] in p]
        self.assertEqual(len(said), 1, self.posts)
        for part in ("lane/" + lane, room, tip, "continue from this tip",
                     "review and land them, then finish the task"):
            self.assertIn(part, said[0])

    def test_a_parked_dispatch_is_carried_once_with_its_started_task(self):  # noqa: VACUOUS_ASSERTION — the successful task and mate rebinds are positively asserted on ledger rows; no stays line may contradict the carried row
        task, lane, _room, _tip = self.started("the wake slice")
        mate = self.add(recipient="grok", lane=lane, kind="build",
                        ref=self.a, task=task["id"])
        from helm import darkmove
        self.assertTrue(darkmove._row_started(mate, "grok")[0],
                        "control: this row has started on the task's lane")
        with self.dead():
            preview, projected = self.run_mover(apply=False)
        self.assertEqual([(m["id"], m["accounted_rows"])
                          for m in projected if m.get("started")],
                         [(task["id"], [mate["id"]])], preview)
        self.assertIn("would move started task", "\n".join(preview))
        self.assertEqual([s for s in preview if mate["id"][:12] in s
                          and "stays on @grok" in s], [], preview)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(dispatches.rows()[mate["id"]]["status"], "open")
        with self.dead():
            lines, moves = self.run_mover()
        self.assertEqual([(m["id"], m["moved"], m["rows"])
                          for m in moves if m.get("started")],
                         [(task["id"], True, [mate["id"]])], lines)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "kimi")
        self.assertEqual(self.holder(lane), "kimi")
        self.assertEqual(dispatches.rows()[mate["id"]]["status"],
                         "cancelled")
        successors = [r for r in dispatches.rows().values()
                      if r.get("supersedes") == mate["id"]]
        self.assertEqual([r["recipient"] for r in successors], ["kimi"])
        self.assertEqual([s for s in lines if mate["id"][:12] in s
                          and "stays on @grok" in s], [], lines)
        self.assertIn("dispatch " + mate["id"][:12], "\n".join(lines))

    def test_a_refused_mate_rebind_reports_one_stay_after_task_moves(self):  # noqa: VACUOUS_ASSERTION, ORPHANED_MOCK — the task owner and lease move, the mate remains held, and exactly one refusal line is required; mark_release is reached via the cross-module run_mover -> darkmove._move_started -> dispatches path
        task, lane, _room, _tip = self.started("the wake slice")
        mate = self.add(recipient="grok", lane=lane, kind="build",
                        ref=self.a, task=task["id"])
        held, err = dispatches.mark_hold(mate["id"], "waiting on fab")
        self.assertIsNone(err, err)
        self.assertIsNotNone(held)
        from helm import darkmove
        self.assertTrue(darkmove._row_started(held, "grok")[0],
                        "control: this held mate has started on the task's lane")
        with self.dead(), mock.patch.object(
                dispatches, "mark_release",
                return_value=(None, "release denied")) as release:
            lines, moves = self.run_mover()
        self.assertEqual(release.call_count, 1, lines)
        self.assertEqual([(m["id"], m["moved"]) for m in moves
                          if m.get("started")], [(task["id"], True)], lines)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "kimi")
        self.assertEqual(self.holder(lane), "kimi")
        self.assertEqual(dispatches.rows()[mate["id"]]["status"], "held")
        reported = [s for s in lines if mate["id"][:12] in s
                    and "stays on @grok" in s]
        self.assertEqual(len(reported), 1, lines)
        self.assertIn("release denied", reported[0])

    def test_a_dirty_room_moves_nothing_and_asks_the_integrator_once(self):  # noqa: VACUOUS_ASSERTION — the empty move list is beside exactly one #seats row, its @mention and its text
        task, lane, room, _tip = self.started("the wake slice")
        with open(os.path.join(room, "half-done.py"), "w") as f:
            f.write("x = 1\n")
        with self.dead(), mock.patch.dict(
                os.environ, {"HELM_INTEGRATOR_SEAT": "seat-a"}):
            lines, moves = self.run_mover()
            self.assertEqual(moves, [], lines)
            self.run_mover(now=time.time() + 300)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "grok")
        rows = [r for r in self.seat_rows if "half-done.py" in r]
        self.assertEqual(len(rows), 1, self.seat_rows)
        self.assertTrue(rows[0].startswith("@seat-a "), rows[0])
        for part in ("@grok", task["id"], room):
            self.assertIn(part, rows[0])

    def test_a_wall_that_resets_within_the_hour_moves_nothing(self):
        task, lane, _room, _tip = self.started("the wake slice")
        soon = self.flags(expires_at=time.time() + 20 * 60)
        with self.dead():
            lines, moves = self.run_mover(soon)
        self.assertEqual(moves, [], lines)
        self.assertIn("waits for its reset", "\n".join(lines))
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "grok")

    def test_a_room_mid_rebase_is_dirty_though_git_names_no_branch(self):
        """A REBASE DETACHES HEAD, so git's worktree registry lists the room
        with no branch. Reading only the branch skipped it, and the task
        moved out from under a conflicted rebase."""
        import subprocess
        from helm.work import _lanes
        task, lane, room, _tip = self.started("the wake slice")
        # Place the real registered room OUTSIDE the lane container. Only
        # rebase-merge/head-name can identify its lane once HEAD detaches.
        outside = os.path.join(self.tmp, "external-rebase-room")
        self.git("worktree", "move", room, outside)
        room = outside
        self.assertFalse(room.startswith(os.path.dirname(
            _lanes.lane_path(self.repo, lane)) + os.sep))
        # `commit` appends to one shared file, so this rebase conflicts.
        got = subprocess.run(["git", "-C", room, "rebase", self.main],
                             capture_output=True, text=True)
        self.assertNotEqual(got.returncode, 0, got.stdout)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=room), "HEAD")
        self.assertEqual(self.git("worktree", "list", "--porcelain").count(
            "worktree " + room + "\n"), 1)
        with open(os.path.join(self.git("rev-parse", "--absolute-git-dir",
                                         cwd=room), "rebase-merge", "head-name")) as f:
            self.assertEqual(f.read().strip(), "refs/heads/lane/" + lane)
        from helm import darkmove
        self.assertEqual(darkmove._room_lane(self.repo, room, ""), lane)
        with self.dead():
            lines, moves = self.run_mover()
        self.assertEqual(moves, [], lines)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "grok")
        self.assertIn("(a rebase in progress)", "\n".join(lines))

    def test_a_detached_room_in_the_lane_container_is_still_read(self):
        task, lane, room, _tip = self.started("the wake slice")
        self.git("checkout", "-q", "--detach", cwd=room)
        with open(os.path.join(room, "half-done.py"), "w") as f:
            f.write("x = 1\n")
        with self.dead():
            lines, moves = self.run_mover()
        self.assertIn("half-done.py", "\n".join(lines))
        self.assertEqual(moves, [], lines)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "grok")

    def test_detached_clean_room_with_unbranched_commits_stays_put(self):
        """Real unreachable detached work is dirty despite clean porcelain."""
        task, lane, room, _tip = self.started("the wake slice")
        self.git("checkout", "-q", "--detach", cwd=room)
        with open(os.path.join(room, "detached-work"), "w") as f:
            f.write("committed but not on any branch\n")
        self.git("add", "detached-work", cwd=room)
        self.git("commit", "-q", "-m", "detached work", cwd=room)
        lost = self.git("rev-list", "HEAD", "--not", "--branches", cwd=room)
        self.assertEqual(lost, self.git("rev-parse", "HEAD", cwd=room))
        self.assertEqual(self.git("status", "--porcelain", cwd=room), "")
        with self.dead():
            lines, moves = self.run_mover()
        self.assertEqual(moves, [], lines)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "grok")
        self.assertIn("detached HEAD", "\n".join(lines))

    def test_detached_clean_room_without_unbranched_commits_can_move(self):
        task, lane, room, _tip = self.started("the wake slice")
        self.git("checkout", "-q", "--detach", cwd=room)
        self.assertEqual(self.git("rev-list", "HEAD", "--not", "--branches",
                                  cwd=room), "")
        with self.dead():
            lines, moves = self.run_mover()
        self.assertEqual([(m["id"], m["moved"]) for m in moves],
                         [(task["id"], True)], lines)
        self.assertEqual(self.holder(lane), "kimi")

    def test_detached_room_with_unreadable_reach_is_not_moved(self):
        from helm.work import _lanes
        task, lane, room, _tip = self.started("the wake slice")
        self.git("checkout", "-q", "--detach", cwd=room)
        real = _lanes._git

        def fail_walk(path, *args, **kw):
            if path == room and args[:1] == ("rev-list",):
                return 1, "", "object could not be read"
            return real(path, *args, **kw)

        with self.dead(), mock.patch.object(_lanes, "_git", side_effect=fail_walk):
            lines, moves = self.run_mover()
        self.assertEqual(moves, [], lines)
        self.assertIn("did not read", "\n".join(lines))
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "grok")

    def test_P0_moves_before_P1(self):  # noqa: VACUOUS_ASSERTION — the one move is pinned to the P0 task's id and the P1 is pinned waiting
        low, _l, _r, _t = self.started("the polish", priority="P1",
                                       slug="polish")
        high, _l, _r, _t = self.started("the outage", priority="P0",
                                        slug="outage")
        with self.dead(), mock.patch.dict(os.environ,
                                          {"HELM_DARK_MOVE_MAX": "1"}):
            lines, moves = self.run_mover(apply=False)
        self.assertEqual([m["id"] for m in moves], [high["id"]], lines)
        self.assertIn("1 more wait", "\n".join(lines))
        self.assertNotEqual(low["id"], high["id"])

    def running(self, state, blocked_on=None):
        """grok's pane process runs (the reboot classifier reads it LIVE),
        and its liveness reading, the one the cubicle mover asks, is
        `state`. The reassign capability decides from those two readings."""
        from helm import harness, poolwall, seat as seat_mod, seat_resume_all
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(harness, "detect",
                                              return_value=object()))
        stack.enter_context(mock.patch.object(
            seat_resume_all, "prove_reboot_dead",
            return_value=(seat_resume_all.LIVE, None, "its pane runs")))
        stack.enter_context(mock.patch.object(
            seat_mod, "seat_liveness", return_value={
                "seat": "grok", "state": state, "blocked_on": blocked_on}))
        stack.enter_context(mock.patch.object(
            poolwall, "seat_wall", return_value=(None, "no pool refusal")))
        return stack

    def test_a_walled_seat_with_a_live_pane_moves_and_records_the_wall(self):
        """The kimi/4019 shape: a family wall five hours from its reset,
        the pane process still running, an in_progress P0 in a clean room.
        Its liveness reads WALLED, so it is not live for work: the task
        moves with no force, and its record names the classification and
        the reset."""
        task, lane, _room, _tip = self.started("the wake slice")
        reset = time.time() + 5 * 3600
        with self.running("WALLED", "upstream QUOTA since 03:00Z"):
            lines, moves = self.run_mover(self.flags(expires_at=reset))
        self.assertEqual([(m["id"], m["to"], m["moved"]) for m in moves],
                         [(task["id"], "kimi", True)], lines)
        self.assertEqual(self.holder(lane), "kimi")
        record = tasks.rows()[task["id"]]["takeover"]
        self.assertEqual(record["source"]["state"], "walled")
        for part in ("WALLED (upstream QUOTA since 03:00Z)", time.strftime(
                "reset %Y-%m-%dT%H:%M:%SZ", time.gmtime(reset))):
            self.assertIn(part, record["source"]["why"])

    def test_a_live_pane_reading_healthy_keeps_the_refusal(self):
        """The CONTROL: the same running pane whose liveness reads IDLE is
        LIVE, so the move is refused and the lease already moved is rolled
        back."""
        task, lane, _room, _tip = self.started("the wake slice")
        with self.running("IDLE"):
            lines, moves = self.run_mover()
        self.assertEqual([(m["id"], m["moved"]) for m in moves],
                         [(task["id"], False)], lines)
        self.assertIn("LIVE", "\n".join(lines))
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "grok")

    def test_a_failed_rollback_after_the_live_veto_is_reported(self):
        """The same LIVE veto, but the lease rollback is refused: the task
        stays on grok while its lease sits on kimi. That split is said on
        the pass's line and kept on the move's record, never dropped."""
        from helm import seats_claims
        task, lane, _room, _tip = self.started("the wake slice")
        refused = mock.patch.object(
            seats_claims, "rollback_claim_holder",
            return_value=(False, "ledger refused the rollback", []))
        with self.running("IDLE"), refused as rollback:
            lines, moves = self.run_mover()
        self.assertEqual(rollback.call_count, 1, lines)
        self.assertEqual([(m["id"], m["moved"]) for m in moves],
                         [(task["id"], False)], lines)
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "kimi")
        said = [ln for ln in lines if "ROLLBACK FAILED" in ln]
        self.assertEqual(len(said), 1, lines)
        for part in (task["id"][:12], "ledger refused the rollback",
                     "stays on @kimi", "not back on @grok"):
            self.assertIn(part, said[0])
        self.assertEqual(len(moves[0].get("rollback") or []), 1, moves)
        self.assertIn("ledger refused the rollback", moves[0]["rollback"][0])

    def test_a_partial_rollback_reports_exact_missing_lease(self):
        """A successful API call is not proof that every lease came back."""
        from helm import seats_claims
        from helm.work import _lanes
        task, lane, _room, _tip = self.started("the wake slice")
        resource = _lanes.resource(self.repo, lane)
        partial = mock.patch.object(
            seats_claims, "rollback_claim_holder",
            return_value=(True, "no matching live lease", []))
        with self.running("IDLE"), partial as rollback:
            lines, moves = self.run_mover()
        self.assertEqual(rollback.call_count, 1, lines)
        self.assertEqual([(m["id"], m["moved"]) for m in moves],
                         [(task["id"], False)], lines)
        self.assertEqual(self.holder(lane), "kimi")
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        said = [ln for ln in lines if "ROLLBACK FAILED" in ln]
        self.assertEqual(len(said), 1, lines)
        self.assertIn(resource, said[0])
        self.assertIn("stays on @kimi", said[0])
        self.assertEqual(len(moves[0].get("rollback") or []), 1, moves)
        self.assertIn(resource, moves[0]["rollback"][0])

    def test_partial_rollback_names_only_the_lease_that_stayed(self):
        from helm import pk, seats, seats_claims
        from helm.work import _lanes
        task, lane, _room, _tip = self.started("the wake slice")
        first = _lanes.resource(self.repo, lane)
        second = first + "-extra"
        ledger = pk.read_json(seats.claims_path(), {})
        ledger[second] = dict(ledger[first], lease="L-extra", fence=2)
        ledger["_fence"] = 2
        self.claims(ledger)
        real = seats_claims.rollback_claim_holder

        def partial(source, target, manifest):
            self.assertEqual({m["resource"] for m in manifest},
                             {first, second})
            return real(source, target, manifest[:1])

        with self.running("IDLE"), mock.patch.object(
                seats_claims, "rollback_claim_holder", side_effect=partial):
            lines, moves = self.run_mover()
        self.assertEqual([(m["id"], m["moved"]) for m in moves],
                         [(task["id"], False)], lines)
        self.assertEqual(self.holder(lane), "grok")
        self.assertEqual(pk.read_json(seats.claims_path(), {})[second]["holder"],
                         "kimi")
        self.assertEqual(len(moves[0].get("rollback") or []), 1, moves)
        report = moves[0]["rollback"][0]
        self.assertIn(second, report)
        self.assertNotIn("lease " + first + " stays", report)
        self.assertIn("ROLLBACK FAILED", "\n".join(lines))

    def test_a_room_whose_status_does_not_read_moves_nothing(self):
        from helm.work import _lanes
        task, lane, _room, _tip = self.started("the wake slice")
        with self.dead(), mock.patch.object(
                _lanes, "_worktree_records", return_value=(None, "boom")):
            lines, moves = self.run_mover()
        self.assertEqual(moves, [], lines)
        self.assertIn("did not read", "\n".join(lines))
        self.assertEqual(tasks.rows()[task["id"]]["owner"], "grok")
        self.assertEqual(self.holder(lane), "grok")


class ListAgreesWithHoldingsTest(tsh.BrokenSeatDoorBase):
    """(6) `dispatch list --open --to SEAT` names what it sets aside, by
    count, so its answer and seat reassign's holdings add up."""

    def test_the_listing_names_held_and_carried_rows_by_count(self):  # noqa: VACUOUS_ASSERTION — the owed row id and both counts are asserted in the same output
        from helm import seat_reassign
        from tests import test_dispatches as td
        owed = self.add(recipient="grok")
        held = self.add(recipient="grok")
        _row, err = dispatches.mark_hold(held["id"], "waiting on the fab")
        self.assertIsNone(err, err)
        parent = self.add(recipient="grok")
        self.add(recipient="kimi", supersedes=parent["id"])
        rc, out, err = td.run(dispatches.cmd_dispatch,
                              ["list", "--open", "--to", "grok",
                               "--all-projects"])
        self.assertEqual(rc, 0, err)
        self.assertIn(owed["id"][:12], out)
        self.assertIn("1 held", out)
        self.assertIn("1 carried by a successor", out)
        man, _unread = seat_reassign.holdings("grok")
        self.assertEqual(len(man["dispatch_in"]), 3)


if __name__ == "__main__":
    unittest.main()
