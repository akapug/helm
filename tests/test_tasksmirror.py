#!/usr/bin/env python3
"""The harness-task mirror: one-way, idempotent, and never silently lossy.

The loop's whole purpose is that work stops disappearing into personal
scratchpads, so every arm here is about what it REFUSES to lose — an
unparseable file, an unrouted session, a row it has already seen — rather than
about the happy path, which is the easy half.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import tasks, tasksmirror  # noqa: E402

ENV_KEYS = ("HELM_HOME", "CLAUDE_CONFIG_DIR")


class MirrorBase(unittest.TestCase):
    def setUp(self):
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = tempfile.mkdtemp(prefix="helm-mirror-")
        self.home = os.path.join(self.tmp, "claude")
        os.environ["CLAUDE_CONFIG_DIR"] = self.home
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        self.ledger = os.path.join(self.tmp, "tasks.jsonl")

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plant(self, sid, tid, subject, status="pending", project=None,
              raw=None):
        d = os.path.join(self.home, "tasks", sid)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%s.json" % tid)
        if raw is not None:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(raw)
        else:
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"id": tid, "subject": subject, "status": status,
                           "description": "d"}, fh)
        if project:
            pd = os.path.join(self.home, "projects", project)
            os.makedirs(pd, exist_ok=True)
            open(os.path.join(pd, "%s.jsonl" % sid), "w").close()
        return p


class NormalizeProjectTest(unittest.TestCase):
    """A CWD-SLUG IS NOT A PROJECT, and treating it as one mis-tags the
    majority case: a seat that claims a lane MOVES INTO the worktree, so lane
    work — most of what the fleet does — routes to a project that does not
    exist. Measured on the live tree: four of the top seven tags were helm
    worktrees, 266 rows between them."""

    def test_a_lane_worktree_routes_to_its_PARENT_project(self):
        self.assertEqual(
            tasksmirror.normalize_project("-home-user-dev-akapug-helm-wt-seats-codex-2"),
            "-home-user-dev-akapug-helm")

    def test_a_subagent_worktree_routes_to_its_parent_too(self):
        self.assertEqual(
            tasksmirror.normalize_project(
                "-home-user-dev-akapug-helm--claude-worktrees-agent-7"),
            "-home-user-dev-akapug-helm")

    def test_a_plain_project_is_UNCHANGED(self):  # noqa: VACUOUS_ASSERTION — this arm IS the positive control for the two normalize arms above it; requiring it to carry its own inverts what it exists for, and its assertion is an EQUALITY against three distinct real slugs, not an absence
        """UNCONDITIONAL POSITIVE CONTROL: without this, a normalize that
        returned a constant would satisfy both arms above."""
        for slug in ("-home-user-dev-akapug-helm", "-home-user-dev",
                     "-home-user-dev-akapug-helix"):
            self.assertEqual(tasksmirror.normalize_project(slug), slug)

    def test_NONE_SURVIVES_because_unrouted_is_a_real_state(self):  # noqa: VACUOUS_ASSERTION — assertIsNone here is the CLAIM, not a weak absence: the defect was normalize returning "" instead, which this catches exactly. The unconditional positive control on the same function is the arm above, which requires three real slugs to come back unchanged
        """The defect this arm exists for: coercing None to "" destroyed the
        caller's UNROUTED signal, and nine live rows reported an empty project
        tag instead of being named as routing nowhere."""
        self.assertIsNone(tasksmirror.normalize_project(None))


class ScopeTest(MirrorBase):
    """The first pass carries LIVE work and COUNTS the finished, per the
    integrator's scope ruling. 446 of 628 harness tasks were tombstones, and
    importing them would have buried a 403-row board under history."""

    def test_live_rows_import_and_finished_rows_are_COUNTED_not_dropped(self):
        self.plant("s1", "1", "still open", "pending", project="-proj")
        self.plant("s1", "2", "being done", "in_progress", project="-proj")
        self.plant("s1", "3", "already done", "completed", project="-proj")
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(len(rep["imported"]), 2, rep)
        self.assertEqual(rep["closed_unmirrored"], 1)
        titles = sorted(r["title"] for r in rep["imported"])
        self.assertEqual(titles, ["being done", "still open"])
        # BOTH FILE AS `open`, because the ledger REFUSES an unowned
        # in_progress row and is right to — the mirror cannot name an owner
        # without asserting an accountability that does not exist. The true
        # upstream status is kept in refs so nothing is lost.
        self.assertEqual({r["status"] for r in rep["imported"]}, {"open"})
        being = [r for r in rep["imported"] if r["title"] == "being done"][0]
        self.assertEqual(being["harness_status"], "in_progress")
        # THE ACCEPTANCE BAR IS THE LEDGER, not the report.
        rows = tasks.rows(self.ledger)
        self.assertEqual(len(rows), 2)
        for r in rows.values():
            self.assertTrue(r["source"].startswith(tasksmirror.SOURCE_KEYED))
            # UNOWNED, in the ledger's OWN representation of absent — which
            # is None, not "". Asserting "" here failed and the failure was
            # right: the word a store PRINTS for absent is not the value it
            # STORES, the same trap that made 14 board rows unclaimable today.
            self.assertIsNone(r["owner"])
            self.assertTrue(any(x.startswith("project:") for x in r["refs"]))

    def test_the_counted_rows_are_RECOVERABLE_not_forgotten(self):
        """Counted-not-imported must leave a NAMED POINTER, or the scope
        ruling becomes silent truncation. Asking for them returns them."""
        self.plant("s1", "3", "already done", "completed", project="-proj")
        counted = tasksmirror.sweep(apply=False, path=self.ledger)
        self.assertEqual(counted["closed_unmirrored"], 1)
        self.assertEqual(counted["imported"], [])
        on_demand = tasksmirror.sweep(apply=False, path=self.ledger,
                                      live_only=False)
        self.assertEqual([r["title"] for r in on_demand["imported"]],
                         ["already done"])


class NeverLosesAnythingTest(MirrorBase):
    def test_running_TWICE_imports_once(self):
        """Idempotence is what makes this safe to run on a timer. The dedup
        key lives in the row's refs, so it survives anything that rewrites the
        ledger."""
        self.plant("s1", "1", "only once", "pending", project="-proj")
        first = tasksmirror.sweep(apply=True, path=self.ledger)
        second = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(len(first["imported"]), 1)
        self.assertEqual(second["imported"], [])
        self.assertEqual(second["already"], 1)
        self.assertEqual(len(tasks.rows(self.ledger)), 1,
                         "the loop filed a duplicate on its second pass")

    def test_an_UNPARSEABLE_task_file_is_REPORTED_not_skipped(self):
        """A sweep whose job is to stop work disappearing must not disappear
        what it could not read. The live tree has exactly one of these."""
        self.plant("s1", "1", "fine", "pending", project="-proj")
        bad = self.plant("s1", "broken", "", raw="{not json")
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(rep["unreadable"], [bad])
        # UNCONDITIONAL POSITIVE CONTROL: the readable sibling still imported,
        # so the arm is not passing on a sweep that gave up entirely.
        self.assertEqual([r["title"] for r in rep["imported"]], ["fine"])

    def test_a_session_that_routes_NOWHERE_is_named_and_still_imported(self):
        """probe-session is real: tasks with no transcript anywhere. Its work
        is not less real for being unroutable, so it imports with an honest
        project tag and the session is named in the report."""
        self.plant("s-orphan", "1", "homeless work", "pending")  # no project
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(len(rep["imported"]), 1)
        self.assertEqual(rep["imported"][0]["project"], None)
        self.assertTrue(any("s-orphan" in u for u in rep["unrouted"]), rep)
        row = list(tasks.rows(self.ledger).values())[0]
        self.assertIn("project:none", row["refs"])

    def test_a_DRY_RUN_writes_nothing(self):
        self.plant("s1", "1", "untouched", "pending", project="-proj")
        rep = tasksmirror.sweep(apply=False, path=self.ledger)
        self.assertEqual(len(rep["imported"]), 1)
        self.assertFalse(rep["imported"][0]["applied"])
        self.assertEqual(tasks.rows(self.ledger), {},
                         "a DRY RUN wrote to the ledger")
        # UNCONDITIONAL POSITIVE CONTROL: the same sweep applied DOES write.
        tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(len(tasks.rows(self.ledger)), 1)


if __name__ == "__main__":
    unittest.main()


class CadenceUnitTest(unittest.TestCase):
    """The unit that makes the loop STANDING, and the one law it must not
    break: a persistent unit may never capture a DISPOSABLE WORKTREE.

    This test exists because the failure is silent and durable. A unit written
    from a lane worktree keeps running after that lane is released and its
    directory is gone, and an operator path baked into a tracked template is a
    never-track needle in history — the same shape that blocked a lane's commit
    to proxywatch.py."""

    def test_the_unit_folds_a_lane_worktree_back_to_the_shared_checkout(self):
        spath, service, tpath, timer = tasksmirror._timer_units()
        # THE ARM IS RUN FROM A LANE WORKTREE — this file lives in one — so if
        # the derivation were a literal or a bare cwd, the worktree path would
        # be right here in the text.
        self.assertNotIn("-wt/", service)
        self.assertNotIn("-wt/", timer)
        # UNCONDITIONAL POSITIVE CONTROL: the unit is not empty and really does
        # name a WorkingDirectory and this seat's own verb, so the absence
        # above is a derivation working rather than a template that says
        # nothing.
        self.assertIn("WorkingDirectory=/", service)
        self.assertIn("task mirror --apply", service)
        self.assertIn(".local/bin/helm", service,
                      "the unit must use the STABLE install, never a "
                      "worktree's PATH entry")

    def test_the_interval_is_refused_rather_than_silently_floored(self):
        ok, detail = tasksmirror.ensure_timer(0)
        self.assertFalse(ok)
        self.assertIn("at least 1 second", detail)


class BuiltOwesWiredTest(unittest.TestCase):
    """A CADENCE NOBODY CALLS IS NOT A CADENCE.

    `ensure_timer` was written, tested by hand, and invoked by NOTHING — so
    the standing loop would never have stood, and its absence is silent by
    nature: the sweep simply never runs and the ledger simply stays behind.
    That is the built-not-wired half-arc, and it is invisible to every test
    that exercises the function directly."""

    def test_the_verb_actually_reaches_the_installer(self):
        from unittest import mock
        from helm import tasks
        with mock.patch.object(tasksmirror, "ensure_timer",
                               return_value=(True, "installed")) as p:
            rc = tasks.cmd_task(["mirror", "--ensure-timer"])
        self.assertEqual(rc, 0)
        self.assertTrue(p.called, "the flag exists and reaches nothing")

    def test_a_junk_tail_still_refuses_before_installing_anything(self):  # noqa: VACUOUS_ASSERTION — assertFalse(p.called) IS the claim: the installer MUTATES the operator's systemd units, so the whole point is that it is NOT reached. The arm also requires rc == 2, which a no-op dispatcher cannot produce, and the unconditional positive control on the same observable is the sibling arm above, which requires the SAME patched installer to BE called on a clean tail
        """UNCONDITIONAL POSITIVE CONTROL on the guard, on the path that
        MUTATES the operator's systemd units: the installer must not run."""
        from unittest import mock
        from helm import tasks
        with mock.patch.object(tasksmirror, "ensure_timer") as p:
            rc = tasks.cmd_task(["mirror", "--bogus", "--ensure-timer"])
        self.assertEqual(rc, 2)
        self.assertFalse(p.called, "junk reached the unit installer")


class DedupSurvivesARefsReplacementTest(MirrorBase):
    """Measured hermetically — the reproduction is this arm verbatim.

    The dedup key rode ONLY in `refs`, and the update door does
    row.update(fields), so `helm task update N --ref anything` REPLACES the
    list wholesale and deletes the key. The next sweep then files a SECOND
    ledger row for one scratchpad item. Idempotence is a load-bearing claim of
    this module's docstring, and it sat one ORDINARY seat action away from a
    silent duplicate — not an exotic one.

    The key now also rides in `source`, which the update door exposes no flag
    for, so either home alone still dedups."""

    def test_replacing_refs_does_NOT_produce_a_duplicate(self):
        self.plant("s1", "1", "one scratchpad item", "pending", project="-proj")
        first = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(len(first["imported"]), 1)
        rid = first["imported"][0]["row"]

        # THE EXACT SEAT ACTION: a --ref that replaces the whole list.
        row, err = tasks.update(rid, path=self.ledger,
                                refs=["context:something-else"])
        self.assertIsNone(err, err)
        self.assertNotIn("harness:", " ".join(row["refs"]),
                         "fixture: the key must really be GONE from refs, or "
                         "this arm is not testing the reported break")

        second = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(second["imported"], [], second)
        self.assertEqual(second["already"], 1)
        self.assertEqual(len(tasks.rows(self.ledger)), 1,
                         "one scratchpad item became two ledger rows")

    def test_losing_the_SOURCE_key_still_dedups_via_refs(self):  # noqa: VACUOUS_ASSERTION — the empty-imported assertion IS the claim, and it cannot pass on a sweep that stopped importing: the arm first requires a REAL import (first['imported'][0]['row']), then asserts its own fixture still holds the key in refs, then requires the ledger to hold exactly ONE row. The unconditional positive control on the same observable is the sibling arm above, which destroys the OTHER home and still requires already == 1
        """THE OTHER DIRECTION, so the cure is two homes rather than a moved
        one. If only `source` carried the key, a rewrite of it would put us
        back where we started."""
        self.plant("s1", "1", "one scratchpad item", "pending", project="-proj")
        first = tasksmirror.sweep(apply=True, path=self.ledger)
        rid = first["imported"][0]["row"]
        row, err = tasks.update(rid, path=self.ledger, source="something-else")
        self.assertIsNone(err, err)
        self.assertTrue(any(r.startswith("harness:") for r in row["refs"]),
                        "fixture: refs must still hold the key here")
        second = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(second["imported"], [], second)
        self.assertEqual(len(tasks.rows(self.ledger)), 1)


class TheDedupSourceIsAPreconditionTest(MirrorBase):
    """An empty dedup set and an UNKNOWN one are the same value and opposite
    instructions, and this loop WRITES on the difference.

    ISOLATED, ON MirrorBase. The first cut of this class was a bare TestCase
    and called `sweep()` with no `path`, so it walked the REAL harness homes
    and read the REAL ledger — a test about a mocked dedup source whose other
    half was ambient live state (task/2378). Every arm here now
    takes `path=self.ledger` under MirrorBase's temp HELM_HOME and
    CLAUDE_CONFIG_DIR.
    """

    def test_an_unreadable_ledger_REFUSES_the_sweep_instead_of_duplicating(self):
        from helm import tasks, tasksmirror
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "ledger unreadable: boom")):
            rep = tasksmirror.sweep(apply=False, path=self.ledger)
        self.assertTrue(rep.get("dedup_unreadable"),
                        "an unreadable ledger left the sweep looking like a "
                        "clean run: %r" % rep)
        self.assertEqual(rep["imported"], [],
                         "the sweep decided to import with no dedup source, "
                         "which re-files every row it has ever filed")
        self.assertEqual(rep["seen"], 0,
                         "the sweep walked the harness homes anyway — it must "
                         "refuse BEFORE deciding anything, not after")

    def test_a_READABLE_ledger_REACHES_THE_WALK_not_just_a_None(self):
        """THE WALK RAN, and neither cheaper observable can say so.

        `dedup_unreadable is None` cannot tell a sweep that RAN from one that
        returned early for another reason — the field is None in both. Nor can
        `homes` merely EXIST: `sweep` initialises `rep = {"homes": 0, ...}`
        above the refusal, so that field is present on both branches and a
        not-None assertion is satisfied by zero. And `claude_homes` admits a
        directory only when `<home>/tasks` exists, so a fixture that builds
        anything else leaves the loop unentered no matter what the ledger says
        (task/2461).

        SO THE FIXTURE SEEDS A HOME THE WALK CAN ADMIT, through the base's own
        producer, and the observables are the two counters incremented INSIDE
        the loops the early return skips.
        """
        from helm import tasks, tasksmirror
        self.plant("s-control-0001", "t-1", "a seeded row")
        self.assertTrue(
            os.path.isdir(os.path.join(self.home, "tasks")),
            "fixture: no tasks dir, so `claude_homes` admits no home and the "
            "walk below cannot run whatever the ledger says")

        with mock.patch.object(tasks, "snapshot", return_value=({}, None)):
            rep = tasksmirror.sweep(apply=False, path=self.ledger)
        self.assertIsNone(rep.get("dedup_unreadable"),
                          "a ledger that WAS read refused the sweep")
        self.assertGreaterEqual(rep["homes"], 1,
                                "the walk was never ENTERED: homes is counted "
                                "inside the loop the refusal skips, and it is "
                                "still at its initial value")
        self.assertGreaterEqual(rep["seen"], 1,
                                "no row was READ: the home was admitted but "
                                "the inner loop produced nothing, so this arm "
                                "still cannot tell a walk from a stub")

        # THE INDEPENDENT REFUSAL CONTROL, on the SAME SEEDED FIXTURE. The
        # counters above prove the walk ran only if they can also report that
        # it did NOT — and that is a fact about the ledger, not about the home.
        # Same planted rows, same sweep, one unreadable dedup source.
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "ledger unreadable: boom")):
            refused = tasksmirror.sweep(apply=False, path=self.ledger)
        self.assertTrue(refused.get("dedup_unreadable"))
        self.assertEqual((refused["homes"], refused["seen"]), (0, 0),
                         "the refusal walked a home this fixture proves is "
                         "walkable, so the counters above measure the fixture "
                         "rather than the decision")

    def test_already_mirrored_reports_its_own_unavailability(self):
        from helm import tasks, tasksmirror
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "torn")):
            keys, why = tasksmirror.already_mirrored(self.ledger)
        self.assertEqual(keys, set())
        self.assertEqual(why, "torn",
                         "the reader returned an empty set with no reason, "
                         "which is indistinguishable from nothing mirrored")


# ── task/1738: the source item's status flows too ──────────────────────────
#
# The mirror imported each harness item ONCE and never read it again, so a
# session that finished a step left its ledger row open forever: about 930 of
# ~1900 open rows were mirror rows when measured. Every arm below
# imports a row the way the loop does, moves its SOURCE item, and sweeps.


class FinishedSourceBase(MirrorBase):
    def imported(self, sid, tid="1"):
        """Import one pending harness item through the real sweep -> row id."""
        self.plant(sid, tid, "work in " + sid, "pending", project="-proj")
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        mine = [r for r in rep["imported"]
                if r["ref"].endswith("/%s/%s" % (sid, tid))]
        self.assertEqual(len(mine), 1, rep)
        return mine[0]["row"]

    def finish(self, sid, tid="1", status="completed"):
        """The harness moves its OWN item; the ledger is not touched."""
        self.plant(sid, tid, "work in " + sid, status)

    def row(self, rid):
        return tasks.rows(self.ledger)[rid]

    def events(self, *rids):
        """LEDGER LINES, not the projection: a write that changes no value
        still appends an event, and only the line list can show it."""
        try:
            with open(self.ledger, encoding="utf-8") as fh:
                evs = [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []
        return [e for e in evs if not rids or e.get("id") in rids]

    def mirror_notes(self, rid):
        return [c for c in self.row(rid).get("comments") or ()
                if c.get("by") == tasksmirror.MIRROR_ACTOR]


class ACompletedSourceClosesItsRowTest(FinishedSourceBase):
    def test_a_completed_source_closes_its_UNTOUCHED_row_and_names_the_source(self):
        rid = self.imported("s-done")
        self.assertEqual(self.row(rid)["status"], "open")   # fixture
        self.finish("s-done")
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        row = self.row(rid)
        self.assertEqual(row["status"], "closed", rep)
        # THE REASON NAMES THE SOURCE, so a reader of the closed row can go
        # and look at the item that finished it.
        self.assertIn("completed", row["closed_reason"])
        self.assertIn("s-done/1", row["closed_reason"])
        self.assertIn("task mirror", row["closed_reason"])
        # THE NORMAL CLOSE PATH: the import event survives and ONE close event
        # follows it — nothing was rewritten.
        self.assertEqual([e["status"] for e in self.events(rid)],
                         ["open", "closed"])
        self.assertEqual([(c["row"], c["applied"])
                          for c in rep["finished"]["closed"]], [(rid, True)])

    def test_an_EXPLICIT_cancel_or_delete_closes_like_completed(self):
        """todos.GONE is the harness vocabulary helm already parses: a
        `deleted` or `cancelled` item says the work will not happen, which
        is as final as `completed`."""
        from helm import todos
        self.assertEqual(set(todos.GONE), {"deleted", "cancelled"})
        deleted = self.imported("s-deleted")
        cancelled = self.imported("s-cancelled")
        self.finish("s-deleted", status="deleted")
        self.finish("s-cancelled", status="cancelled")
        tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual((self.row(deleted)["status"],
                          self.row(cancelled)["status"]), ("closed", "closed"))
        self.assertIn("source item deleted", self.row(deleted)["closed_reason"])
        self.assertIn("source item cancelled",
                      self.row(cancelled)["closed_reason"])

    def test_a_cancelled_item_is_not_IMPORTED_as_open_work(self):
        """The import leg mapped every status it did not know to `open`, so
        a cancelled scratchpad item arrived on the board as live work."""
        self.plant("s-fresh", "1", "never started", "cancelled",
                   project="-proj")
        self.plant("s-fresh", "2", "real work", "pending", project="-proj")
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual([r["title"] for r in rep["imported"]], ["real work"])
        self.assertEqual(rep["closed_unmirrored"], 1)


class ATouchedRowIsNotedOnceTest(FinishedSourceBase):
    TOUCHES = {
        "claimed": lambda rid, p: tasks.update(rid, path=p, owner="seat-a",
                                               status="in_progress"),
        "commented": lambda rid, p: tasks.comment(rid, "on it", by="seat-a",
                                                  path=p),
        "updated": lambda rid, p: tasks.update(rid, path=p,
                                               note="rescoped by a seat"),
    }

    def test_a_touched_row_stays_open_with_EXACTLY_one_note_across_two_sweeps(self):
        rids = {kind: self.imported("s-" + kind) for kind in self.TOUCHES}
        control = self.imported("s-control")
        errs = [touch(rids[k], self.ledger)[1]
                for k, touch in sorted(self.TOUCHES.items())]
        self.assertEqual(errs, [None, None, None])
        for sid in ["s-" + k for k in rids] + ["s-control"]:
            self.finish(sid)
        first = tasksmirror.sweep(apply=True, path=self.ledger)
        second = tasksmirror.sweep(apply=True, path=self.ledger)
        # NOT CLOSED: each row keeps the status its toucher left.
        self.assertEqual({k: self.row(r)["status"] for k, r in rids.items()},
                         {"claimed": "in_progress", "commented": "open",
                          "updated": "open"})
        # ONE NOTE EACH, after two sweeps, naming the source that finished.
        notes = {k: [n["text"] for n in self.mirror_notes(r)]
                 for k, r in rids.items()}
        self.assertEqual({k: len(v) for k, v in notes.items()},
                         {"claimed": 1, "commented": 1, "updated": 1})
        self.assertEqual(
            {k: ("s-%s/1" % k in v[0], "completed" in v[0])
             for k, v in notes.items()},
            {"claimed": (True, True), "commented": (True, True),
             "updated": (True, True)})
        # EACH TOUCH IS NAMED, so the dry run can say what holds rows open.
        self.assertEqual({n["row"]: n["touched"]
                          for n in first["finished"]["noted"]},
                         {rids["claimed"]: ["claimed"],
                          rids["commented"]: ["commented"],
                          rids["updated"]: ["updated"]})
        # UNCONDITIONAL POSITIVE CONTROL, in the SAME two sweeps: the
        # untouched sibling closed, so the rows above stayed open by decision.
        self.assertEqual(self.row(control)["status"], "closed")
        self.assertEqual(second["finished"]["already_noted"], 3)
        self.assertEqual(second["finished"]["noted"], [])

    def test_a_triage_rank_counts_as_a_touch_and_is_named(self):
        """A rank is an update by an actor other than the mirror. The reason
        is named so the dry run can say how many rows a rank alone holds."""
        rid = self.imported("s-ranked")
        row = self.row(rid)
        row.update(priority="P2", ranked_by="seat-a",
                   last_updated=row["last_updated"] + 1)
        from helm import eventledger
        self.assertTrue(eventledger.append(self.ledger, row))
        self.finish("s-ranked")
        rep = tasksmirror.sweep(apply=False, path=self.ledger)
        self.assertEqual(rep["finished"]["closed"], [])
        self.assertEqual([n["touched"] for n in rep["finished"]["noted"]],
                         [["ranked"]])


class NothingMovesWithoutAFinishedSourceTest(FinishedSourceBase):
    def test_a_VANISHED_source_changes_nothing(self):
        """Unknown is not done: a file or session that is gone says nothing
        about whether the work finished."""
        item_gone = self.imported("s-item-gone")
        session_gone = self.imported("s-session-gone")
        control = self.imported("s-control")
        os.remove(os.path.join(self.home, "tasks", "s-item-gone", "1.json"))
        shutil.rmtree(os.path.join(self.home, "tasks", "s-session-gone"))
        self.finish("s-control")
        before = self.events(item_gone, session_gone)
        self.assertEqual(len(before), 2)    # one import event each
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(self.events(item_gone, session_gone), before)
        self.assertEqual(rep["finished"]["vanished"], 2)
        self.assertEqual(self.row(control)["status"], "closed")

    def test_a_LIVE_source_changes_nothing(self):
        pending = self.imported("s-pending")
        running = self.imported("s-running")
        control = self.imported("s-control")
        self.finish("s-running", status="in_progress")
        self.finish("s-control")
        before = self.events(pending, running)
        self.assertEqual(len(before), 2)    # one import event each
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(self.events(pending, running), before)
        self.assertEqual(rep["finished"]["live"], 2)
        self.assertEqual(self.row(control)["status"], "closed")

    def test_an_UNPARSEABLE_source_is_REPORTED_and_its_row_left_open(self):
        rid = self.imported("s-torn")
        control = self.imported("s-control")
        bad = self.plant("s-torn", "1", "", raw="{torn")
        self.finish("s-control")
        before = self.events(rid)
        self.assertEqual(len(before), 1)    # the import event
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(self.events(rid), before)
        self.assertIn(bad, rep["unreadable"])
        self.assertEqual(rep["finished"]["unreadable"], [rid])
        self.assertEqual(self.row(control)["status"], "closed")


class BackfillTest(FinishedSourceBase):
    def test_rows_filed_BEFORE_this_change_close_on_the_first_sweep(self):
        """Both key shapes the old code wrote: the first cut put the key in
        `refs` only under source `harness-mirror`; the later cut also put it
        in `source`. Neither row is imported by the sweep that settles it."""
        root = os.path.realpath(self.home)
        refs = {sid: [tasksmirror.harness_ref(root, sid, "1"),
                      "project:-proj", "harness-status:open"]
                for sid in ("s-oldest", "s-older")}
        oldest, err_oldest = tasks.add(
            "legacy s-oldest", "", refs=refs["s-oldest"],
            source=tasksmirror.SOURCE, path=self.ledger, force_new=True)
        older, err_older = tasks.add(
            "legacy s-older", "", refs=refs["s-older"],
            source=tasksmirror.SOURCE_KEYED + refs["s-older"][0],
            path=self.ledger, force_new=True)
        self.assertEqual((err_oldest, err_older), (None, None))
        self.finish("s-oldest")
        self.finish("s-older")
        rep = tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual((self.row(oldest["id"])["status"],
                          self.row(older["id"])["status"]),
                         ("closed", "closed"))
        self.assertEqual(rep["already"], 2)
        self.assertEqual(rep["imported"], [])

    def test_the_write_cap_DEFERS_the_rest_and_says_so(self):
        rids = [self.imported("s-cap-%d" % i) for i in range(3)]
        for i in range(3):
            self.finish("s-cap-%d" % i)
        rep = tasksmirror.sweep(apply=True, path=self.ledger, write_cap=2)
        closed = [r for r in rids if self.row(r)["status"] == "closed"]
        self.assertEqual(len(closed), 2)
        self.assertEqual(rep["finished"]["deferred"], 1)
        tasksmirror.sweep(apply=True, path=self.ledger, write_cap=2)
        self.assertEqual({self.row(r)["status"] for r in rids}, {"closed"})


class DryRunAndRaceTest(FinishedSourceBase):
    def test_a_DRY_RUN_writes_nothing_and_reports_both_counts(self):
        close_me = self.imported("s-close")
        note_me = self.imported("s-note")
        seat_note, err = tasks.comment(note_me, "mine", by="seat-a",
                                       path=self.ledger)
        self.assertEqual([c["by"] for c in seat_note["comments"]], ["seat-a"],
                         err)
        self.finish("s-close")
        self.finish("s-note")
        before = self.events()
        self.assertEqual(len(before), 3)    # two imports and one comment
        rep = tasksmirror.sweep(apply=False, path=self.ledger)
        self.assertEqual(self.events(), before, "a DRY RUN wrote the ledger")
        fin = rep["finished"]
        self.assertEqual([(c["row"], c["applied"]) for c in fin["closed"]],
                         [(close_me, False)])
        self.assertEqual([(c["row"], c["applied"]) for c in fin["noted"]],
                         [(note_me, False)])
        # UNCONDITIONAL POSITIVE CONTROL: the same sweep applied does both.
        tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(self.row(close_me)["status"], "closed")
        self.assertEqual(len(self.mirror_notes(note_me)), 1)

    def test_the_verb_DRY_RUN_prints_the_counts_and_writes_nothing(self):
        import contextlib
        import io
        self.ledger = tasks.ledger_path()   # the verb reads the default
        rid = self.imported("s-verb")
        self.finish("s-verb")
        before = self.events()
        self.assertEqual(len(before), 1)    # the import event
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tasks.cmd_task(["mirror"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.events(), before)
        self.assertIn("would close 1", out.getvalue())
        self.assertEqual(self.row(rid)["status"], "open")
        with contextlib.redirect_stdout(io.StringIO()):
            rc_apply = tasks.cmd_task(["mirror", "--apply"])
        self.assertEqual(rc_apply, 0)
        self.assertEqual(self.row(rid)["status"], "closed")

    def test_a_claim_landing_between_the_read_and_the_close_WINS(self):
        """The sweep judges a row from its snapshot; a seat can claim it
        before the close lands. The close must see that and write nothing."""
        rid = self.imported("s-race")
        self.finish("s-race")
        real_close = tasks.close

        def claim_first(token, reason, path=None, expect=None):
            _row, err = tasks.update(rid, path=self.ledger,
                                     status="in_progress", owner="seat-a")
            self.assertIsNone(err, err)
            return real_close(token, reason, path=path, expect=expect)

        with mock.patch.object(tasks, "close", side_effect=claim_first):
            rep = tasksmirror.sweep(apply=True, path=self.ledger)
        row = self.row(rid)
        self.assertEqual((row["status"], row["owner"]), ("in_progress", "seat-a"))
        self.assertEqual([s["row"] for s in rep["finished"]["skipped"]], [rid])
        # THE NEXT SWEEP judges it touched and notes it once.
        tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(len(self.mirror_notes(rid)), 1)
        self.assertEqual(self.row(rid)["status"], "in_progress")


class AMirrorCloseIsReversedTheLedgersWayTest(FinishedSourceBase):
    """CONTROL. A mirror close goes through the ledger's normal close path, so
    it is reversed the way any close is. The ledger REFUSES to reopen a closed
    row (task/345: a reopen by update leaves a live row carrying a tombstone
    reason); the remedy it names is new work that cites the row. This arm pins
    that the mirror's close is an ordinary close, not a special one."""

    def test_the_close_is_an_ordinary_close_with_the_ledgers_remedy(self):
        import contextlib
        import io
        self.ledger = tasks.ledger_path()   # the verb reads the default
        rid = self.imported("s-undo")
        self.finish("s-undo")
        tasksmirror.sweep(apply=True, path=self.ledger)
        self.assertEqual(self.row(rid)["status"], "closed")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = tasks.cmd_task(["update", rid, "--status", "open"])
        self.assertEqual(rc, 2)
        self.assertIn("CLOSED", err.getvalue())
        self.assertIn("helm task add", err.getvalue())
        # The event log still holds the imported snapshot before the close.
        self.assertEqual([e["status"] for e in self.events(rid)],
                         ["open", "closed"])
        # The named remedy works: new open work that cites the closed row.
        again, aerr = tasks.add("work in s-undo, again", "", refs=[rid],
                                path=self.ledger)
        self.assertIsNone(aerr, aerr)
        self.assertEqual(again["status"], "open")
