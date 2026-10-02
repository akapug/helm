#!/usr/bin/env python3
"""helm.pileflow tests — the FLOW section of `helm pile` (task/4184).

HERMETIC BY CONSTRUCTION. Every FLOW line is a pure function of the rows its
reader hands it, so each test builds those rows here: no real ~/.helm, no
real ledger, no fab or systemctl call. The mirror line reads git, so it runs
against throwaway repositories with pinned commit dates. Who acts is passed
in as a stub, so no roster is read.

The contract under test: a line flags only a MEASURED 2x deviation from its
own 7-day median (or a state that is never normal: a stopped train, a STALE
job, a failed unit, started work on a dark seat, a mirror a week behind), and
a reader that cannot read its source prints UNKNOWN, never a traceback.
"""
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-pileflow-", var="HELM_HOME")

from helm import pile, pileflow  # noqa: E402

NOW = 1790000000.0
HOUR = 3600.0
DAY = 86400.0


def _who(component):
    return "steward-of-%s" % component


def _stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _gate(t, wall, sliced=True, status="OK"):
    row = {"event": "gate", "suite": True, "ts": _stamp(t), "wall": wall,
           "status": status, "id": "g%d" % int(t)}
    if sliced:
        row["slice_authority"] = {"kind": "gateslice"}
    return row


def _history(walls_min):
    """One whole-suite gate every six hours over the last week."""
    return [_gate(NOW - DAY * 7 + i * 6 * HOUR, w * 60)
            for i, w in enumerate(walls_min)]


class GateLineTest(unittest.TestCase):
    def test_only_the_gates_a_train_names_are_land_gates(self):
        # a canary or a lane gate in the same ledger is not a land gate
        rows = _history([6] * 20) + [_gate(NOW - 60, 40 * 60)]
        ids = {r["id"] for r in rows[:-1]}
        line, = pileflow.gate_line(rows, NOW, "", who=_who, ids=ids)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("newest land gate", line["detail"])
        trains = [{"history": [{"note": "gate:%s GREEN" % ("ab" * 8)}]},
                  {"receipt": "cd" * 8}]
        self.assertEqual(pileflow.land_gate_ids(trains),
                         {"ab" * 8, "cd" * 8})

    def test_five_times_its_median_flags(self):
        rows = _history([6] * 20) + [_gate(NOW - 60, 30 * 60, sliced=False)]
        line, = pileflow.gate_line(rows, NOW, "the canary stands", who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("5.0x", line["detail"])
        self.assertIn("serial", line["detail"])
        self.assertIn("steward-of-build-lanes", line["act"])

    def test_a_normal_gate_does_not_flag(self):
        rows = _history([6] * 20) + [_gate(NOW - 60, 7 * 60)]
        line, = pileflow.gate_line(rows, NOW, "the canary stands", who=_who)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("sliced", line["detail"])

    def test_a_gate_older_than_the_week_is_no_baseline(self):
        rows = [_gate(NOW - 9 * DAY, 60)] + [_gate(NOW - 60, 40 * 60)]
        line, = pileflow.gate_line(rows, NOW, "", who=_who)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("no 7-day median", line["detail"])

    def test_no_receipt_reads_unknown(self):
        line, = pileflow.gate_line([], NOW, "", who=_who)
        self.assertEqual(line["state"], pileflow.UNKNOWN)
        self.assertIn("no whole-suite gate receipt", line["detail"])

    def test_an_unreadable_receipts_ledger_reads_unknown(self):
        # The ledger path is a directory: the reader cannot open it, and the
        # line says UNKNOWN with why; nothing raises out of `lines`.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(pileflow, "_receipts_path",
                                   return_value=tmp):
                out = pileflow.lines(readers=[("land gate",
                                               pileflow._gate)])
        self.assertEqual([l["state"] for l in out], [pileflow.UNKNOWN])
        self.assertIn("land gate", out[0]["name"])


class RunnerTest(unittest.TestCase):
    def test_a_reader_that_raises_reads_unknown(self):
        def boom(ctx):
            raise OSError("gone")
        out = pileflow.lines(readers=[("thing", boom)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["state"], pileflow.UNKNOWN)
        self.assertIn("OSError", out[0]["detail"])

    def test_every_unknown_line_names_who_acts(self):
        def boom(ctx):
            raise OSError("gone")

        def quiet(ctx):
            return [pileflow._line("x", pileflow.UNKNOWN, "unread")]

        def fine(ctx):
            return [pileflow._line("y", pileflow.OK, "measured")]
        with mock.patch.object(pileflow, "_who", return_value="steward-seat"):
            out = pileflow.lines(readers=[("thing", boom), ("x", quiet),
                                          ("y", fine)])
        self.assertEqual([l["act"] for l in out],
                         ["steward-seat", "steward-seat", ""],
                         "an ok line stays without an actor (control)")

    def test_a_reader_cut_by_its_deadline_reads_unknown(self):
        def slow(ctx):
            time.sleep(2)
            return []
        out = pileflow.lines(readers=[("slow", slow)], deadline=0.2)
        self.assertEqual(out[0]["state"], pileflow.UNKNOWN)
        self.assertIn("cut", out[0]["detail"])

    def test_one_dead_reader_leaves_the_others_measured(self):
        def boom(ctx):
            raise ValueError("x")

        def fine(ctx):
            return [pileflow._line("fine", pileflow.OK, "measured", "")]
        out = pileflow.lines(readers=[("a", boom), ("b", fine)])
        self.assertEqual([l["state"] for l in out],
                         [pileflow.UNKNOWN, pileflow.OK])


class LandsLineTest(unittest.TestCase):
    def test_a_stalled_six_hours_flags(self):
        # one land an hour all week, none in the last six hours
        stamps = [NOW - 7 * DAY + i * HOUR for i in range(7 * 24 - 6)]
        line, = pileflow.lands_line(stamps, NOW, who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("0.0/h", line["detail"])

    def test_the_usual_rate_does_not_flag(self):
        stamps = [NOW - 7 * DAY + i * HOUR for i in range(7 * 24)]
        line, = pileflow.lands_line(stamps, NOW, who=_who)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("1.0/h", line["detail"])


def _task(tid, ts, status="open", priority="P0", origin="owner", owner=None,
          created=None):
    return {"id": tid, "ts": created if created is not None else ts,
            "last_updated": ts, "status": status, "priority": priority,
            "origin": origin, "owner": owner}


class OwnerP0Test(unittest.TestCase):
    def test_a_doubled_backlog_flags(self):
        week = NOW - 7 * DAY - HOUR
        events = [_task("task/1", week), _task("task/2", week)]
        events += [_task("task/%d" % n, NOW - 600) for n in range(3, 8)]
        line, = pileflow.owner_p0_line(events, NOW, who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("7 open", line["detail"])
        self.assertIn("task/1", line["detail"])        # the oldest

    def test_the_usual_backlog_does_not_flag(self):
        events = [_task("task/%d" % n, NOW - 7 * DAY - HOUR - n)
                  for n in range(1, 4)]
        # one closed and one opened inside the week: the count holds at 3
        events.append(_task("task/1", NOW - 3 * DAY, status="closed"))
        events.append(_task("task/9", NOW - 3 * DAY))
        line, = pileflow.owner_p0_line(events, NOW, who=_who)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("3 open", line["detail"])

    def test_an_agent_p0_is_not_an_owner_p0(self):
        events = [_task("task/1", NOW - 100, origin="agent")]
        line, = pileflow.owner_p0_line(events, NOW, who=_who)
        self.assertIn("0 open", line["detail"])


class LeadLinesTest(unittest.TestCase):
    def test_a_lead_at_twice_its_unrouted_median_flags(self):
        rows = [_task("task/%d" % n, NOW - 100, priority="P1",
                      owner="lead-a") for n in (10, 12, 13)]
        rows += [_task("task/11", NOW - 100, priority="P0", owner="lead-b"),
                 _task("task/14", NOW - 100, priority="P0", owner="lead-b")]
        dispatch = [{"sender": "lead-b", "recipient": "w", "task": None,
                     "lane": "other-99"}]
        # the `_fence` integer the live lease ledger carries is not a lease
        claims = {"_fence": 7,
                  "worktree:helm:fix-11": {"holder": "lead-b"}}
        out = pileflow.lead_lines(rows, dispatch, claims,
                                  {"lead-a": 1, "lead-b": 1}, who=_who)
        flagged = [l for l in out if l["state"] == pileflow.FLAG]
        self.assertEqual([l["name"] for l in flagged], ["lead lead-a"])
        self.assertIn("3.0x", flagged[0]["detail"])
        self.assertIn("lead-a", flagged[0]["act"])
        self.assertTrue(any("idle-capacity-signal-3821" in l["detail"]
                            for l in out))

    def test_a_lead_at_its_usual_count_does_not_flag(self):
        # nothing in flight is not a flag on its own: harness subagents are
        # not counted, so zero in flight is not a measurement
        rows = [_task("task/%d" % n, NOW - 100, priority="P1",
                      owner="lead-a") for n in (10, 12, 13)]
        out = pileflow.lead_lines(rows, [], {}, {"lead-a": 3}, who=_who)
        self.assertEqual([l["state"] for l in out], [pileflow.OK])
        self.assertIn("3 unrouted P0/P1 owned across 1 seat,", out[0]["detail"])

    def test_lead_baselines_count_zero_where_a_seat_held_none(self):
        samples = [(1, [_task("task/1", 0, priority="P1", owner="a")]),
                   (2, []), (3, [])]
        self.assertEqual(pileflow.lead_baselines(samples), {"a": 0})

    def test_a_row_carried_by_a_dispatch_is_routed(self):
        rows = [_task("task/10", NOW - 100, priority="P1", owner="lead-a")]
        dispatch = [{"sender": "lead-a", "recipient": "w",
                     "task": "task/10", "lane": "x"}]
        out = pileflow.lead_lines(rows, dispatch, {}, {}, who=_who)
        self.assertEqual([l["state"] for l in out], [pileflow.OK])
        self.assertIn("no lead holds an unrouted P0/P1", out[0]["detail"])


class TrainLineTest(unittest.TestCase):
    def test_a_stopped_train_needs_a_person(self):
        st = {"name": "train9", "state": "STOPPED", "created_ts": NOW - 600,
              "stopped": {"why": "audits failed", "state": "COMPOSING"}}
        line, = pileflow.train_line(st, {"paused": None}, [], NOW, who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("needs a person", line["detail"])
        self.assertIn("audits failed", line["detail"])

    def test_a_train_twice_its_median_flags(self):
        done = [{"state": "DONE", "created_ts": NOW - DAY,
                 "archived_ts": NOW - DAY + 1800}] * 5
        st = {"name": "train9", "state": "GATING",
              "created_ts": NOW - 2 * HOUR}
        line, = pileflow.train_line(st, {"paused": None}, done, NOW,
                                    who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("4.0x", line["detail"])

    def test_a_paused_auto_land_flags(self):
        line, = pileflow.train_line(None, {"paused": {"by": "x", "ts": NOW}},
                                    [], NOW, who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("PAUSED", line["detail"])

    def test_no_train_and_no_pause_is_quiet(self):
        line, = pileflow.train_line(None, {"paused": None}, [], NOW,
                                    who=_who)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("no train in flight", line["detail"])


_FAB_OUT = """== node-a ==

IN FLIGHT (priority / phase / reason):
RUNNING      priority=normal  quiet-abc-1789990000-dead-11 (pgid 42) — STALE: no log output for 30+ min; look with fab tail <host> quiet-abc-1789990000-dead-11 -n 20, and kill it if nobody owns it · 50 min old, launched by pug@hub/seat-quiet
RUNNING      priority=normal  fresh-abc-1789999000-cafe-12 (pgid 43) — admitted; never preempted or accelerated by queued P0 work · 10 min old, launched by pug@hub/seat-fresh
== node-b ==

IN FLIGHT (priority / phase / reason):
"""


class FabLinesTest(unittest.TestCase):
    def test_a_stale_job_flags_and_names_its_launcher(self):
        out = pileflow.fab_lines(0, _FAB_OUT, "", who=_who)
        flagged = [l for l in out if l["state"] == pileflow.FLAG]
        self.assertEqual(len(flagged), 1)
        self.assertIn("quiet-abc-1789990000-dead-11", flagged[0]["detail"])
        self.assertIn("seat-quiet", flagged[0]["act"])

    def test_no_stale_job_is_quiet(self):
        text = _FAB_OUT.replace("STALE: no log output", "admitted; quiet")
        out = pileflow.fab_lines(0, text, "", who=_who)
        self.assertEqual([l["state"] for l in out], [pileflow.OK])

    def test_a_fab_without_live_reads_unknown(self):
        out = pileflow.fab_lines(
            0, "", "fab: --live unreachable (status not read)\n", who=_who)
        self.assertEqual([l["state"] for l in out], [pileflow.UNKNOWN])

    def test_an_unreachable_host_is_said(self):
        out = pileflow.fab_lines(
            0, _FAB_OUT, "fab: node-c unreachable (status not read)\n",
            who=_who)
        self.assertTrue(any("node-c" in l["detail"] for l in out))


class UnitsLineTest(unittest.TestCase):
    def test_a_failed_unit_flags(self):
        text = "helm-gc.service loaded failed failed helm gc\n"
        line, = pileflow.units_line(3, text, who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("helm-gc.service", line["detail"])

    def test_none_failed_is_quiet(self):
        line, = pileflow.units_line(0, "", who=_who)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("no helm user unit failed", line["detail"])

    def test_systemctl_unreachable_reads_unknown(self):
        line, = pileflow.units_line(None, "", who=_who)
        self.assertEqual(line["state"], pileflow.UNKNOWN)
        self.assertIn("systemctl", line["detail"])


class DarkLineTest(unittest.TestCase):
    def test_started_work_on_a_dark_seat_flags(self):
        tasks = [_task("task/5", NOW, status="in_progress", owner="kimi"),
                 _task("task/6", NOW, status="in_progress", owner="codex")]
        family = {"kimi": "kimi", "codex": "codex"}.get
        line, = pileflow.dark_line({"kimi": "quota wall"}, family, tasks,
                                   [], NOW - 60, True, NOW, who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("task/5", line["detail"])
        self.assertNotIn("task/6", line["detail"])

    def test_a_mover_that_stopped_writing_flags(self):
        line, = pileflow.dark_line({}, lambda s: None, [], [],
                                   NOW - 3 * DAY, True, NOW, who=_who)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("mover", line["detail"])

    def test_no_dark_seat_and_a_live_mover_is_quiet(self):
        line, = pileflow.dark_line({}, lambda s: None, [], [], NOW - 60,
                                   True, NOW, who=_who)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("no dark seat holds started work", line["detail"])


def _git(cwd, *args, when=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "@%d +0000" % when
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, env=env,
                   capture_output=True)


class MirrorLineTest(unittest.TestCase):
    def _mirror(self, tmp, behind_at):
        """A clone whose upstream gained one commit at `behind_at` that the
        clone's HEAD lacks; FETCH_HEAD stamped now."""
        up = os.path.join(tmp, "up")
        os.mkdir(up)
        _git(up, "init", "-q", "-b", "main")
        _git(up, "commit", "-q", "--allow-empty", "-m", "a",
             when=int(NOW - 30 * DAY))
        clone = os.path.join(tmp, "clone")
        _git(tmp, "clone", "-q", up, clone)
        _git(up, "commit", "-q", "--allow-empty", "-m", "b", when=int(behind_at))
        _git(clone, "fetch", "-q", "origin")
        os.utime(os.path.join(clone, ".git", "FETCH_HEAD"), (NOW, NOW))
        return clone

    def test_a_mirror_ten_days_behind_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = self._mirror(tmp, NOW - 10 * DAY)
            line = pileflow.mirror_line(clone, NOW)
        self.assertEqual(line["state"], pileflow.FLAG)
        self.assertIn("1 commit", line["detail"])

    def test_a_mirror_one_day_behind_does_not_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = self._mirror(tmp, NOW - DAY)
            line = pileflow.mirror_line(clone, NOW)
        self.assertEqual(line["state"], pileflow.OK)
        self.assertIn("1 commit", line["detail"])

    def test_named_paths_with_no_checkout_are_unknown_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(pileflow, "mirror_paths", return_value=[tmp]):
                out = pileflow._mirrors(pileflow._Ctx(NOW),
                                        who=lambda c: "steward-seat")
            self.assertEqual(out[0]["state"], pileflow.UNKNOWN)
            self.assertIn("none of the 1 named paths", out[0]["detail"])
            clone = self._mirror(tmp, NOW - DAY)
            with mock.patch.object(pileflow, "mirror_paths", return_value=[clone]):
                ok = pileflow._mirrors(pileflow._Ctx(NOW), who=lambda c: "s")
            self.assertEqual(ok[0]["state"], pileflow.OK, "control: one real checkout")

    def test_a_directory_that_is_no_checkout_reads_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            line = pileflow.mirror_line(tmp, NOW)
        self.assertEqual(line["state"], pileflow.UNKNOWN)
        self.assertIn("not a git checkout", line["detail"])


class PileRendersFlowTest(unittest.TestCase):
    def test_flow_header_counts_flagged_lines_and_prints_states(self):
        def flow():
            return ([pileflow._line("land gate", pileflow.FLAG, "slow", "x"),
                     pileflow._line("units", pileflow.OK, "none failed", ""),
                     pileflow._line("fab", pileflow.UNKNOWN, "unread", "")],
                    None)
        buf = io.StringIO()
        with mock.patch.object(pile, "_SECTIONS", [(pile.FLOW_TITLE, flow)]):
            with mock.patch("builtins.print",
                            side_effect=lambda *a: buf.write(" ".join(
                                map(str, a)) + "\n")):
                pile.cmd_pile([])
        out = buf.getvalue()
        self.assertIn("== %s (1 flagged, 1 unknown) ==" % pile.FLOW_TITLE, out)
        self.assertIn("FLAG", out)
        self.assertIn("UNKNOWN", out)
        self.assertIn("x acts", out)

    def test_flow_is_a_pile_section(self):
        self.assertIn(pile.FLOW_TITLE, [t for t, _fn in pile._SECTIONS])


if __name__ == "__main__":
    unittest.main()
