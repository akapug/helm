#!/usr/bin/env python3
"""Every task row lives in one project (task/3745).

One store, global ids, every row in exactly one project, and each project's
lead reads its own burn-down and health line. These arms carry that contract
at every surface it touches:

  task add          refuses a row with no project: the cwd's project (the
                    resolution `task list` uses) or --project NAME, and
                    `--project none` is not a way out
  task rehome       --plan FILE is a dry run: one line per plan row (id,
                    title, None -> project, the plan's reason) and a count;
                    unknown projects, closed rows and missing rows are
                    refused by name
  task rehome       --apply writes each row as one project update through
                    the task writer, attributed to the caller, and a re-run
                    writes nothing
  doctor            one line: the count of open rows with no project, OK at 0
  task list         a scoped listing says how many homeless rows it withholds
  task health       one line per project: open stories, open rows, opened
                    today, closed today, 7-day net; --json has the same keys

THE DAY IS THE HOST'S LOCAL DAY, the one `helm goal cycle` counts from, and
the arms pin it: TZ is set to a fixed zone seven hours west of UTC, so an
event after UTC midnight and before local midnight is yesterday.

Project names are placeholders and every arm runs against a temp HELM_HOME.
"""
import calendar
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import doctor, tasks  # noqa: E402
from tests.test_tasks import CliBase  # noqa: E402

ALPHA, BETA, EMPTY = "alpha-proj", "beta-proj", "empty-proj"

# A FIXED ZONE SEVEN HOURS WEST OF UTC, spelled in POSIX form so no tz
# database is needed on the runner. "LCL" is what %Z prints for it.
ZONE = "LCL+07"


def utc(stamp):
    """'2026-09-30T07:30:00' (UTC) -> epoch seconds."""
    return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")))


# 05:00 local on the pinned day (the 30th): local midnight is 07:00Z on the
# 30th, and the 7-day window opens at local midnight on the 24th, 07:00Z.
NOW = utc("2026-09-30T12:00:00")


class HomesBase(CliBase):
    """Three registered projects, each at its own temp directory, and a
    fourth directory no project claims. `raw` appends ledger events with the
    stamps an arm names, the event-sourced shape the writer produces."""

    HOME_ADDS = False          # these arms own their cwd

    def setUp(self):
        super().setUp()
        self.prior_cwd_homes = os.getcwd()
        self.dirs = {}
        projects = {}
        for name in (ALPHA, BETA, EMPTY):
            path = os.path.realpath(os.path.join(self.tmp, name + "-repo"))
            os.makedirs(path)
            self.dirs[name] = path
            projects[name] = {"name": name, "path": path}
        self.nowhere = os.path.realpath(os.path.join(self.tmp, "nowhere"))
        os.makedirs(self.nowhere)
        gdir = os.path.dirname(tasks.ledger_path())
        os.makedirs(gdir, exist_ok=True)
        with open(os.path.join(gdir, "registry.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": 1, "projects": projects}, fh)

    def tearDown(self):
        os.chdir(self.prior_cwd_homes)
        super().tearDown()

    def stand_in(self, name):
        """cwd inside a registered project, asserted through the real lens."""
        os.chdir(self.dirs[name])
        self.assertEqual(tasks.current_project(), name)

    def stand_nowhere(self):
        """cwd inside no registered project, asserted."""
        os.chdir(self.nowhere)
        self.assertEqual(tasks.current_project(), None)

    def raw(self, num, title, project=None, filed=None, closed=None,
            parent=None, born_closed=False):
        """Append a row's birth event and, when `closed` is given, its
        closing event, with those exact stamps."""
        filed = filed if filed is not None else utc("2026-09-01T00:00:00")
        row = {"id": "task/%d" % num, "ts": filed, "last_updated": filed,
               "continues": parent, "priority": None, "title": title,
               "status": "closed" if born_closed else "open",
               "owner": None, "note": None, "refs": [], "source": "seat-a",
               "origin": "agent",
               "closed_reason": "placeholder" if born_closed else None,
               "project": project, "comments": []}
        events = [dict(row)]
        if closed is not None:
            events.append(dict(row, status="closed", last_updated=closed,
                               closed_reason="placeholder finished"))
        with open(tasks.ledger_path(), "a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev) + "\n")
        return row["id"]

    def plan(self, doc, name="plan.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def line_for(self, out, tid):
        got = [l for l in out.splitlines() if l.split()[:1] == [tid]]
        self.assertEqual(len(got), 1, "%s: %r" % (tid, out))
        return got[0]


class AddNeedsAProjectTest(HomesBase):
    """`helm task add` files a row only when it has a project."""

    def test_cwd_inside_a_project_files_there(self):  # noqa: VACUOUS_ASSERTION — the stamped project is compared to the registered name the lens control just resolved, and rc 0 is asserted
        self.stand_in(ALPHA)
        rc, _out, err = self.cli("add", "placeholder row filed in alpha")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.filed("placeholder row filed in alpha")
                         .get("project"), ALPHA)

    def test_cwd_outside_any_project_without_the_flag_refuses(self):  # noqa: VACUOUS_ASSERTION — the refusal text is asserted PRESENT against literals, and the ledger file is asserted to hold exactly the one row the positive control filed
        self.stand_in(ALPHA)
        rc, _out, err = self.cli("add", "placeholder control row")
        self.assertEqual(rc, 0, err)
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        self.stand_nowhere()
        rc, out, err = self.cli("add", "placeholder homeless row")
        self.assertEqual(rc, 2, err)
        self.assertIn("a task must name its project", err)
        self.assertIn("Nothing was filed", err)
        # BOTH WAYS ARE NAMED: the cwd, and the flag
        self.assertIn("from inside a project's checkout", err)
        self.assertIn("--project NAME", err)
        # and the names the flag would take
        self.assertIn(ALPHA, err)
        self.assertIn(BETA, err)
        self.assertEqual(out, "")
        self.assertEqual(self.ledger_lines(), before)

    def test_cwd_outside_any_project_with_the_flag_files_there(self):  # noqa: VACUOUS_ASSERTION — the scope sentence is asserted PRESENT in err and the stamped project equals the flag's registered name
        self.stand_nowhere()
        rc, _out, err = self.cli("add", "--project", BETA,
                                 "placeholder row filed by flag")
        self.assertEqual(rc, 0, err)
        self.assertIn("scoping to project '%s'" % BETA, err)
        self.assertEqual(self.filed("placeholder row filed by flag")
                         .get("project"), BETA)

    def test_project_none_is_not_a_way_out(self):  # noqa: VACUOUS_ASSERTION — the control add files at rc 0 first, so the unchanged line count is measured on a ledger this door can write; each refusal sentence is asserted PRESENT
        self.stand_in(ALPHA)
        rc, _out, err = self.cli("add", "placeholder control row")
        self.assertEqual(rc, 0, err)
        before = self.ledger_lines()
        for spelling in (("--project", "none"), ("--project=None",),
                         ("--project", "NULL")):
            rc, _out, err = self.cli("add", *spelling + (
                "placeholder row with no home",))
            self.assertEqual(rc, 2, (spelling, err))
            self.assertIn("is not a way out", err)
            self.assertIn("a task must name its project", err)
            self.assertIn("--project NAME", err)
        self.assertEqual(self.ledger_lines(), before)

    def test_an_unreadable_registry_is_named_not_called_empty(self):  # noqa: VACUOUS_ASSERTION — the UNREADABLE sentence and the registry path are asserted PRESENT in the same err the absences are read from
        self.stand_in(ALPHA)
        rc, _out, err = self.cli("add", "placeholder control row")
        self.assertEqual(rc, 0, err)
        before = self.ledger_lines()
        reg = os.path.join(os.path.dirname(tasks.ledger_path()),
                           "registry.json")
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write("{ this is not registry json\n")
        rc, _out, err = self.cli("add", "placeholder row filed blind")
        self.assertEqual(rc, 2, err)
        self.assertIn("UNREADABLE", err)
        self.assertIn(reg, err)
        self.assertIn("a task must name its project", err)
        self.assertNotIn("inside no registered project", err)
        self.assertNotIn("none is registered", err)
        self.assertEqual(self.ledger_lines(), before)

    def test_an_unknown_project_name_refuses(self):  # noqa: VACUOUS_ASSERTION — the control add files at rc 0 first, and the refusal sentence is asserted PRESENT
        self.stand_in(ALPHA)
        rc, _out, err = self.cli("add", "placeholder control row")
        self.assertEqual(rc, 0, err)
        before = self.ledger_lines()
        rc, _out, err = self.cli("add", "--project", "no-such-proj",
                                 "placeholder row for nobody")
        self.assertEqual(rc, 2, err)
        self.assertIn("not a registered project", err)
        self.assertEqual(self.ledger_lines(), before)


class RehomeDryRunTest(HomesBase):
    """`helm task rehome --plan FILE` shows the plan and writes nothing."""

    def seed(self):
        return {
            "home": self.raw(11, "placeholder homeless one"),
            "home2": self.raw(12, "placeholder homeless two"),
            "same": self.raw(13, "placeholder already alpha", project=ALPHA),
            "other": self.raw(14, "placeholder already beta", project=BETA),
            "closed": self.raw(15, "placeholder closed row",
                               closed=utc("2026-09-02T00:00:00")),
            "odd": self.raw(16, "placeholder row for an odd home"),
        }

    def test_the_nesting_shape_reads_directly_and_nothing_is_written(self):  # noqa: VACUOUS_ASSERTION — each planned row's line is asserted PRESENT with its id, its move and its reason before the unchanged line count is read
        ids = self.seed()
        path = self.plan({
            "homes": {"11": ALPHA, "12": BETA},
            "home_notes": {ALPHA: "placeholder reason for alpha"},
            "stories": [], "reparent": [], "notes": {}})
        before = self.ledger_lines()
        self.stand_nowhere()                  # rehome reads no scope
        rc, out, err = self.cli("rehome", "--plan", path)
        self.assertEqual(rc, 0, err)
        one = self.line_for(out, ids["home"])
        self.assertIn("None -> %s" % ALPHA, one)
        self.assertIn("placeholder homeless one", one)
        self.assertIn("placeholder reason for alpha", one)
        two = self.line_for(out, ids["home2"])
        self.assertIn("None -> %s" % BETA, two)
        self.assertIn("no reason in the plan", two)
        # the keys it did not read are named, so nobody believes a
        # reparent or a story was applied
        self.assertIn("not read: notes, reparent, stories", out)
        self.assertIn("DRY RUN", out)
        self.assertIn("2 row(s) would be homed", out)
        self.assertEqual(self.ledger_lines(), before)

    def test_every_refusal_names_its_row_and_its_reason(self):  # noqa: VACUOUS_ASSERTION — every refusal line is asserted PRESENT with its reason, and the one homed row's move line is the positive control
        ids = self.seed()
        path = self.plan({"homes": {
            "13": ALPHA,                        # already homed there
            "14": ALPHA,                        # homed elsewhere, no force
            "15": ALPHA,                        # closed
            "99": ALPHA,                        # missing
            "16": "helm?",                      # unknown project
            "11": ALPHA}})
        before = self.ledger_lines()
        rc, out, err = self.cli("rehome", "--plan", path)
        self.assertEqual(rc, 1, err)
        self.assertIn("already in %s" % ALPHA, self.line_for(out, ids["same"]))
        moved = self.line_for(out, ids["other"])
        self.assertIn("REFUSED", moved)
        self.assertIn("already in project '%s'" % BETA, moved)
        self.assertIn('"force": true, "from": "%s"' % BETA, moved)
        closed = self.line_for(out, ids["closed"])
        self.assertIn("REFUSED", closed)
        self.assertIn("closed", closed)
        missing = self.line_for(out, "task/99")
        self.assertIn("REFUSED", missing)
        self.assertIn("not in the ledger", missing)
        odd = self.line_for(out, ids["odd"])
        self.assertIn("REFUSED", odd)
        self.assertIn("'helm?' is not a registered project", odd)
        self.assertIn("None -> %s" % ALPHA, self.line_for(out, ids["home"]))
        self.assertIn("1 row(s) would be homed, 1 already homed, 4 refused",
                      out)
        self.assertEqual(self.ledger_lines(), before)

    def test_a_bare_mapping_and_a_forced_row_read_too(self):
        ids = self.seed()
        path = self.plan({
            "task/14": {"project": ALPHA, "force": True, "from": BETA,
                        "reason": "placeholder move reason"},
            "#11": {"project": BETA}})
        rc, out, err = self.cli("rehome", "--plan", path)
        self.assertEqual(rc, 0, err)
        self.assertIn("bare {task id: project} mapping", out)
        moved = self.line_for(out, ids["other"])
        self.assertIn("%s -> %s" % (BETA, ALPHA), moved)
        self.assertIn("FORCED", moved)
        self.assertIn("placeholder move reason", moved)
        self.assertIn("None -> %s" % BETA, self.line_for(out, ids["home"]))
        self.assertIn("2 row(s) would be homed", out)

    def test_a_forced_row_moves_only_from_the_project_it_names(self):  # noqa: VACUOUS_ASSERTION — the row whose "from" matches is asserted to read its move line, the positive control for the two refusals beside it
        """A forced row carries the project the plan's author saw it in, so
        an apply after somebody else moved the row refuses instead of moving
        it from a project nobody read."""
        ids = self.seed()
        self.raw(17, "placeholder second beta row", project=BETA)
        path = self.plan({
            "14": {"project": ALPHA, "force": True},
            "13": {"project": BETA, "force": True, "from": EMPTY},
            "17": {"project": ALPHA, "force": True, "from": BETA}})
        rc, out, err = self.cli("rehome", "--plan", path)
        self.assertEqual(rc, 1, err)
        bare = self.line_for(out, ids["other"])
        self.assertIn("REFUSED", bare)
        self.assertIn('names no "from"', bare)
        stale = self.line_for(out, ids["same"])
        self.assertIn("REFUSED", stale)
        self.assertIn("is in '%s', not '%s'" % (ALPHA, EMPTY), stale)
        self.assertIn("%s -> %s" % (BETA, ALPHA), self.line_for(out, "task/17"))

    def test_an_unreadable_plan_refuses_whole(self):
        self.seed()
        path = os.path.join(self.tmp, "broken.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        rc, out, err = self.cli("rehome", "--plan", path)
        self.assertEqual(rc, 2, err)
        self.assertIn("helm task rehome", err)
        self.assertIn(path, err)
        self.assertEqual(out, "")
        rc, _out, err = self.cli("rehome")
        self.assertEqual(rc, 2, err)
        self.assertIn("--plan", err)


class RehomeApplyTest(HomesBase):
    """`--apply` writes one project update per row, through the writer."""

    def test_apply_homes_each_row_once_and_a_rerun_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the first apply is asserted to grow the ledger by exactly two events and to stamp both projects before the re-run's unchanged count is read
        self.admit("seat-a")
        a = self.raw(21, "placeholder homeless alpha")
        b = self.raw(22, "placeholder homeless beta")
        filed = self.lines(tasks.ledger_path())[0]["last_updated"]
        path = self.plan({"homes": {"21": ALPHA, "22": BETA},
                          "home_notes": {BETA: "placeholder beta reason"}})
        before = self.ledger_lines()
        self.stand_nowhere()
        rc, out, err = self.cli("rehome", "--plan", path, "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("homed 2 row(s)", out)
        # ONE EVENT PER ROW
        self.assertEqual(self.ledger_lines(), before + 2)
        rows = tasks.rows(path=tasks.ledger_path())
        self.assertEqual(rows[a]["project"], ALPHA)
        self.assertEqual(rows[b]["project"], BETA)
        # ATTRIBUTED TO THE CALLER, with where it came from
        self.assertEqual(rows[a]["homed_by"], "seat-a")
        self.assertEqual(rows[a]["homed_from"], None)
        self.assertTrue(rows[a]["homed_at"] > 0)
        # A HOME IS NOT WORK ON THE ROW: the staleness clock does not move
        self.assertEqual(rows[a]["last_updated"], filed)
        # THE RE-RUN IS IDEMPOTENT
        rc, out, err = self.cli("rehome", "--plan", path, "--apply")
        self.assertEqual(rc, 0, err)
        self.assertIn("homed 0 row(s)", out)
        self.assertIn("2 already homed", out)
        self.assertEqual(self.ledger_lines(), before + 2)

    def test_a_homed_row_moves_only_when_the_plan_forces_it(self):  # noqa: VACUOUS_ASSERTION — the forced apply is the positive control: it grows the ledger by one event and stamps the new project and homed_from
        self.admit("seat-a")
        tid = self.raw(23, "placeholder row in beta", project=BETA)
        path = self.plan({"23": ALPHA})
        before = self.ledger_lines()
        rc, out, err = self.cli("rehome", "--plan", path, "--apply")
        self.assertEqual(rc, 1, err)
        self.assertIn("REFUSED", self.line_for(out, tid))
        self.assertEqual(self.ledger_lines(), before)
        path = self.plan({"23": {"project": ALPHA, "force": True,
                                 "from": BETA}}, name="forced.json")
        rc, out, err = self.cli("rehome", "--plan", path, "--apply")
        self.assertEqual(rc, 0, err)
        row = tasks.rows(path=tasks.ledger_path())[tid]
        self.assertEqual(row["project"], ALPHA)
        self.assertEqual(row["homed_from"], BETA)
        self.assertEqual(self.ledger_lines(), before + 1)

    def test_apply_writes_the_good_rows_and_refuses_the_rest_by_name(self):  # noqa: VACUOUS_ASSERTION — the good row's project is asserted equal to the registered name and the ledger to grow by exactly one event before the refused row's absent project is read
        self.admit("seat-a")
        good = self.raw(25, "placeholder homeless row")
        closed = self.raw(26, "placeholder closed row",
                          closed=utc("2026-09-02T00:00:00"))
        odd = self.raw(27, "placeholder row for an odd home")
        path = self.plan({"25": ALPHA, "26": ALPHA, "98": ALPHA,
                          "27": "not-registered"})
        before = self.ledger_lines()
        rc, out, err = self.cli("rehome", "--plan", path, "--apply")
        self.assertEqual(rc, 1, err)
        self.assertIn("homed 1 row(s)", out)
        self.assertIn("3 refused", out)
        self.assertIn("closed", self.line_for(out, closed))
        self.assertIn("not in the ledger", self.line_for(out, "task/98"))
        self.assertIn("'not-registered' is not a registered project",
                      self.line_for(out, odd))
        self.assertEqual(self.ledger_lines(), before + 1)
        rows = tasks.rows(path=tasks.ledger_path())
        self.assertEqual(rows[good]["project"], ALPHA)
        self.assertEqual(rows[odd]["project"], None)

    def test_apply_without_an_admitted_actor_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the refusal sentence is asserted PRESENT in err; its sibling arms prove the same plan writes when an actor is admitted
        self.raw(24, "placeholder homeless row")
        self.unidentify()
        path = self.plan({"24": ALPHA})
        before = self.ledger_lines()
        rc, out, err = self.cli("rehome", "--plan", path, "--apply")
        self.assertEqual(rc, 2, err)
        self.assertIn("helm task rehome --apply", err)
        self.assertIn("nothing was written", err)
        self.assertEqual(out, "")
        self.assertEqual(self.ledger_lines(), before)


class WriterHomesARowTest(HomesBase):
    """The project write lives in `tasks.update`, the one task writer."""

    def test_a_project_change_needs_an_admitted_actor(self):
        tid = self.raw(31, "placeholder homeless row")
        row, err = tasks.update(tid, project=ALPHA)
        self.assertEqual(row, None)
        self.assertIn("ADMITTED ACTOR", err)
        actor = self.admit("seat-a")
        row, err = tasks.update(tid, project=ALPHA, home_actor=actor)
        self.assertEqual(err, None)
        self.assertEqual(row["project"], ALPHA)
        self.assertEqual(row["homed_by"], "seat-a")

    def test_a_project_cannot_be_cleared_and_a_homed_row_needs_force(self):
        actor = self.admit("seat-a")
        tid = self.raw(32, "placeholder row in beta", project=BETA)
        for blank in (None, "", "  "):
            row, err = tasks.update(tid, project=blank, home_actor=actor)
            self.assertEqual(row, None)
            self.assertIn("cannot be cleared", err)
        row, err = tasks.update(tid, project=ALPHA, home_actor=actor)
        self.assertEqual(row, None)
        self.assertIn("already has project '%s'" % BETA, err)
        row, err = tasks.update(tid, project=ALPHA, home_actor=actor,
                                home_force=True)
        self.assertEqual(err, None)
        self.assertEqual(row["homed_from"], BETA)


class StoryHomeHintTest(HomesBase):
    """`helm task add` without --continues names up to three open rows that
    share RARE title words with the new row, as places it may belong. One
    line, and it never refuses."""

    def seed(self):
        # "placeholder" and "common" are in every row, so they are not rare
        for n in range(90, 96):
            self.raw(n, "placeholder common filler row %s" % "abcdef"[n - 90],
                     project=ALPHA)
        self.raw(71, "placeholder common zanzibar story", project=ALPHA)
        self.raw(72, "placeholder common quokka story", project=ALPHA)
        self.raw(73, "placeholder common narwhal story", project=ALPHA)
        self.raw(74, "placeholder common okapi story", project=ALPHA)
        # a child of 71: a match on it names its story root instead
        self.raw(75, "placeholder common axolotl child", project=ALPHA,
                 parent="task/71")
        # 72-74 are stories because something in this project continues
        # them. A row nothing continues is not a story (task/3994).
        self.raw(82, "placeholder common quokka child", project=ALPHA,
                 parent="task/72")
        self.raw(83, "placeholder common narwhal child", project=ALPHA,
                 parent="task/73")
        self.raw(84, "placeholder common okapi child", project=ALPHA,
                 parent="task/74")
        # the same rare word in ANOTHER project is never a home here
        self.raw(76, "placeholder common wombat elsewhere", project=BETA)

    def hint(self, out):
        got = [l for l in out.splitlines() if "story home?" in l]
        self.assertLessEqual(len(got), 1, out)
        return got[0] if got else None

    def test_a_row_sharing_rare_words_names_its_story_homes(self):
        self.seed()
        self.stand_in(ALPHA)
        rc, out, err = self.cli("add", "placeholder",
                                "zanzibar axolotl wiring")
        self.assertEqual(rc, 0, err)
        line = self.hint(out)
        self.assertIn("task/71", line)
        self.assertNotIn("task/75", line)       # its root is named instead
        self.assertIn("--continues", line)
        self.assertEqual(self.filed("placeholder zanzibar axolotl wiring")
                         .get("continues"), None)

    def test_at_most_three_homes_and_never_another_project(self):
        self.seed()
        self.stand_in(ALPHA)
        rc, out, err = self.cli("add", "placeholder", "zanzibar", "quokka",
                                "narwhal", "okapi", "wombat", "sweep")
        self.assertEqual(rc, 0, err)
        line = self.hint(out)
        named = [t for t in ("task/71", "task/72", "task/73", "task/74")
                 if t in line]
        self.assertEqual(len(named), 3, line)
        self.assertNotIn("task/76", line)

    def test_no_line_with_a_parent_or_without_a_rare_word(self):
        self.seed()
        self.stand_in(ALPHA)
        rc, out, err = self.cli("add", "--continues", "task/72",
                                "placeholder zanzibar under quokka")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.hint(out), None)
        self.assertIn("filed", out)
        rc, out, err = self.cli("add", "placeholder common unrelated words")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.hint(out), None)
        self.assertIn("filed", out)

    def test_a_standalone_task_is_not_named_as_a_story(self):  # noqa: VACUOUS_ASSERTION — the story root filed beside it is named, so the missing standalone id is a refusal to mislabel and not a hint that names nothing
        self.seed()
        self.raw(77, "placeholder common pangolin alone", project=ALPHA)
        self.stand_in(ALPHA)
        rc, out, err = self.cli("add", "placeholder pangolin zanzibar wiring")
        self.assertEqual(rc, 0, err)
        line = self.hint(out)
        self.assertIn("task/71", line)
        self.assertNotIn("task/77", line)

    def test_a_root_in_another_project_is_not_this_rows_story(self):  # noqa: VACUOUS_ASSERTION — the in-project story filed in the same call is named, so the absent foreign id is the boundary and not an empty hint
        self.seed()
        self.raw(79, "placeholder common cassowary root", project=BETA)
        self.raw(78, "placeholder common cassowary child", project=ALPHA,
                 parent="task/79")
        self.stand_in(ALPHA)
        rc, out, err = self.cli("add", "placeholder cassowary zanzibar wiring")
        self.assertEqual(rc, 0, err)
        line = self.hint(out)
        self.assertIn("task/71", line)
        self.assertNotIn("task/79", line)
        self.assertNotIn("task/78", line)

    def test_a_closed_child_still_names_its_open_root(self):  # noqa: VACUOUS_ASSERTION — the closed-child root and the other-project-child root are named, so the missing standalone id and the missing foreign root are refusals and not an empty hint
        """An open root is a story when any row continues it, including a
        closed child and a child in another project. A standalone root is
        not, and a root that lives in another project is not."""
        self.seed()
        self.raw(85, "placeholder common ibis story", project=ALPHA)
        self.raw(86, "placeholder common ibis child", project=ALPHA,
                 parent="task/85", closed=utc("2026-09-02T00:00:00"))
        self.raw(96, "placeholder common serval story", project=ALPHA)
        self.raw(97, "placeholder common serval child", project=BETA,
                 parent="task/96")
        self.raw(87, "placeholder common tapir alone", project=ALPHA)
        self.raw(88, "placeholder common numbat root", project=BETA)
        self.raw(89, "placeholder common numbat child", project=ALPHA,
                 parent="task/88")
        self.stand_in(ALPHA)
        rc, out, err = self.cli("add", "placeholder", "ibis", "serval",
                                "tapir", "numbat", "wiring")
        self.assertEqual(rc, 0, err)
        line = self.hint(out)
        self.assertIsNotNone(line)
        self.assertIn("task/85", line)
        self.assertIn("task/96", line)
        self.assertNotIn("task/87", line)
        self.assertNotIn("task/88", line)
        self.assertNotIn("task/89", line)


class DoctorCountsHomelessRowsTest(HomesBase):

    def test_zero_homeless_open_rows_is_ok(self):
        self.raw(41, "placeholder homed row", project=ALPHA)
        # a CLOSED homeless row is history, never counted
        self.raw(42, "placeholder closed homeless",
                 closed=utc("2026-09-02T00:00:00"))
        self.assertIn("check_task_homes", doctor.CHECKS)
        got = doctor.check_task_homes()
        self.assertEqual(got, [("OK", "task homes: every open task row "
                                      "names its project")])

    def test_homeless_open_rows_warn_with_their_count(self):
        self.raw(43, "placeholder homed row", project=ALPHA)
        self.raw(44, "placeholder homeless one")
        self.raw(45, "placeholder homeless two")
        got = doctor.check_task_homes()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], "WARN")
        self.assertIn("2 open task row(s) have no project", got[0][1])
        self.assertIn("helm task rehome", got[0][1])


class OwnProjectIsRegisteredTest(HomesBase):
    """The fallback project helm's own recovery row uses is registered, or
    the doctor says it is not (task/3994). Filing still happens; the rung
    is how a lead learns the row has no list."""

    def test_an_unregistered_fallback_warns(self):  # noqa: VACUOUS_ASSERTION — registering the same name then reads OK, so the warning is the unregistered name and not a rung that always warns
        self.assertIn("check_own_project_registered", doctor.CHECKS)
        got = doctor.check_own_project_registered()
        self.assertEqual(got[0][0], "WARN")
        self.assertIn(tasks.OWN_PROJECT, got[0][1])
        self.assertIn("not a registered project", got[0][1])
        reg = os.path.join(os.path.dirname(tasks.ledger_path()),
                           "registry.json")
        with open(reg, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["projects"][tasks.OWN_PROJECT] = {
            "name": tasks.OWN_PROJECT,
            "path": self.dirs[ALPHA]}
        with open(reg, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        got = doctor.check_own_project_registered()
        self.assertEqual(got[0][0], "OK")
        self.assertIn(tasks.OWN_PROJECT, got[0][1])


    def test_an_unreadable_registry_is_unknown_not_ok(self):  # noqa: VACUOUS_ASSERTION — the unregistered arm above is the positive warning, so UNKNOWN here is the distinct refusal and not a rung that always warns
        reg = os.path.join(os.path.dirname(tasks.ledger_path()),
                           "registry.json")
        with open(reg, "w", encoding="utf-8") as fh:
            fh.write("{ this is not registry json\n")
        got = doctor.check_own_project_registered()
        self.assertEqual(got[0][0], "WARN")
        self.assertIn("UNKNOWN", got[0][1])
        self.assertIn(tasks.OWN_PROJECT, got[0][1])
        self.assertNotIn("not a registered project", got[0][1])

class ListSaysWhatItWithholdsTest(HomesBase):

    def test_a_scoped_list_counts_the_homeless_rows_it_does_not_show(self):
        self.raw(51, "placeholder alpha row", project=ALPHA)
        self.raw(52, "placeholder homeless one")
        self.raw(53, "placeholder homeless two")
        self.raw(54, "placeholder homeless closed",
                 closed=utc("2026-09-02T00:00:00"))
        self.raw(55, "placeholder beta row", project=BETA)
        self.stand_in(ALPHA)
        rc, out, err = self.cli("list")
        self.assertEqual(rc, 0, err)
        self.assertIn("placeholder alpha row", out)
        self.assertIn("2 homeless row(s) with no project (`helm task rehome` "
                      "gives them one), 1 row(s) from other projects — "
                      "--all-projects shows them", out)
        rc, out, err = self.cli("list", "--all")
        self.assertEqual(rc, 0, err)
        self.assertIn("3 homeless row(s) with no project, 2 of them open "
                      "(`helm task rehome` gives those one)", out)

    def test_closed_homeless_rows_are_not_offered_to_rehome(self):
        self.raw(56, "placeholder alpha row", project=ALPHA)
        self.raw(57, "placeholder homeless closed",
                 closed=utc("2026-09-02T00:00:00"))
        self.stand_in(ALPHA)
        rc, out, err = self.cli("list", "--all")
        self.assertEqual(rc, 0, err)
        self.assertIn("1 homeless row(s) with no project, none of them open "
                      "(a closed row is not rehomed)", out)
        self.assertNotIn("gives those one", out)


class HealthBase(HomesBase):

    def setUp(self):
        super().setUp()
        self.zone = mock.patch.dict(os.environ, {"TZ": ZONE})
        self.zone.start()
        time.tzset()
        self.clock = mock.patch("helm.taskhomes._now", return_value=NOW)
        self.clock.start()

    def tearDown(self):
        # BEFORE the base restores the environment: patch.dict puts back the
        # whole environ it saw at start, which holds the fixture's HELM_HOME,
        # so stopping it after the base would plant those keys again.
        self.clock.stop()
        self.zone.stop()
        time.tzset()
        super().tearDown()

    def seed(self):
        """alpha: a story of two open rows (a parent and its child) and one
        more open row; filings and closes straddling both midnights."""
        # opened TODAY (00:30 local) and still open
        self.raw(61, "placeholder alpha story", project=ALPHA,
                 filed=utc("2026-09-30T07:30:00"))
        # its child, filed at 23:30 local YESTERDAY: after UTC midnight,
        # before local midnight, so NOT today
        self.raw(62, "placeholder alpha child", project=ALPHA, parent="task/61",
                 filed=utc("2026-09-30T06:30:00"))
        # a third open row, filed long ago
        self.raw(63, "placeholder alpha old row", project=ALPHA)
        # closed TODAY (00:01 local), filed inside the 7-day window
        self.raw(64, "placeholder alpha closed today", project=ALPHA,
                 filed=utc("2026-09-24T08:00:00"),
                 closed=utc("2026-09-30T07:01:00"))
        # closed YESTERDAY (23:59 local), filed BEFORE the window opened
        self.raw(65, "placeholder alpha closed yesterday", project=ALPHA,
                 filed=utc("2026-09-24T06:00:00"),
                 closed=utc("2026-09-30T06:59:00"))
        # a tombstone born closed today is history, never opened or closed
        self.raw(66, "placeholder alpha tombstone", project=ALPHA,
                 filed=utc("2026-09-30T08:00:00"), born_closed=True)
        # beta: one row filed today
        self.raw(67, "placeholder beta row", project=BETA,
                 filed=utc("2026-09-30T08:00:00"))
        # a homeless open row
        self.raw(68, "placeholder homeless row")

    # alpha: opened today 61; closed today 64. 7-day window: opened 61, 62,
    # 64 (65 filed before it opened); closed 64, 65. Net 3 - 2 = +1.
    ALPHA_LINE = {"project": ALPHA, "open_stories": 2, "open_rows": 3,
                  "opened_today": 1, "closed_today": 1, "net_7d": 1}
    BETA_LINE = {"project": BETA, "open_stories": 1, "open_rows": 1,
                 "opened_today": 1, "closed_today": 0, "net_7d": 1}
    EMPTY_LINE = {"project": EMPTY, "open_stories": 0, "open_rows": 0,
                  "opened_today": 0, "closed_today": 0, "net_7d": 0}
    NONE_LINE = {"project": None, "open_stories": 1, "open_rows": 1,
                 "opened_today": 0, "closed_today": 0, "net_7d": 0}


class HealthLineTest(HealthBase):

    def test_the_text_line_per_project(self):
        self.seed()
        rc, out, err = self.cli("health", "--project", ALPHA)
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertIn("since 2026-09-30 00:00 LCL", lines[0])
        self.assertIn("since 2026-09-24 00:00 LCL", lines[0])
        self.assertIn("local time", lines[0])
        self.assertEqual(len(lines), 2, out)
        self.assertEqual(lines[1].split(), [
            ALPHA, "2", "open", "stories,", "3", "open", "rows;", "today",
            "1", "opened,", "1", "closed;", "7-day", "net", "+1"])

    def test_all_projects_lists_every_registered_project_even_an_empty_one(self):
        self.seed()
        rc, out, err = self.cli("health", "--all-projects")
        self.assertEqual(rc, 0, err)
        names = [l.split()[0] for l in out.splitlines()[1:]]
        self.assertEqual(names, [ALPHA, BETA, EMPTY, "(no"])
        empty = [l for l in out.splitlines() if l.startswith(EMPTY)][0]
        self.assertEqual(empty.split()[1:], [
            "0", "open", "stories,", "0", "open", "rows;", "today", "0",
            "opened,", "0", "closed;", "7-day", "net", "0"])

    def test_the_cwd_decides_when_no_flag_is_given(self):
        self.seed()
        self.stand_in(BETA)
        rc, out, err = self.cli("health")
        self.assertEqual(rc, 0, err)
        self.assertEqual([l.split()[0] for l in out.splitlines()[1:]], [BETA])
        self.assertIn("scoping to project '%s'" % BETA, err)

    def test_a_cwd_in_no_project_shows_every_project(self):
        self.seed()
        self.stand_nowhere()
        rc, out, err = self.cli("health")
        self.assertEqual(rc, 0, err)
        self.assertEqual([l.split()[0] for l in out.splitlines()[1:]],
                         [ALPHA, BETA, EMPTY, "(no"])
        self.assertNotIn("scoping to project", err)

    def test_a_line_shows_when_rows_moved_even_if_they_cancel(self):
        """A homeless row filed in the window and another closed in it: the
        net is 0 and no homeless row is open, and the line still shows,
        because rows moved."""
        self.raw(81, "placeholder alpha row", project=ALPHA)
        self.raw(82, "placeholder homeless filed in the window",
                 filed=utc("2026-09-27T12:00:00"),
                 closed=utc("2026-09-28T12:00:00"))
        rc, out, err = self.cli("health", "--all-projects", "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)["projects"]
        self.assertEqual([l["project"] for l in got],
                         [ALPHA, BETA, EMPTY, None])
        self.assertEqual(got[-1], {"project": None, "open_stories": 0,
                                   "open_rows": 0, "opened_today": 0,
                                   "closed_today": 0, "net_7d": 0})

    def test_an_unknown_project_and_two_scopes_refuse(self):
        rc, out, err = self.cli("health", "--project", "no-such-proj")
        self.assertEqual(rc, 2, err)
        self.assertIn("not a registered project", err)
        self.assertEqual(out, "")
        rc, out, err = self.cli("health", "--project", ALPHA,
                                "--all-projects")
        self.assertEqual(rc, 2, err)
        self.assertIn("--all-projects", err)
        self.assertEqual(out, "")

    def test_the_json_carries_the_same_keys_and_the_window(self):
        self.seed()
        rc, out, err = self.cli("health", "--all-projects", "--json")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual(got["projects"], [self.ALPHA_LINE, self.BETA_LINE,
                                           self.EMPTY_LINE, self.NONE_LINE])
        self.assertEqual(got["today_since"], utc("2026-09-30T07:00:00"))
        self.assertEqual(got["week_since"], utc("2026-09-24T07:00:00"))
        self.assertEqual(got["tz"], "LCL")
        # THE SAME KEYS AS THE TEXT LINE, and nothing else per project
        from helm import taskhomes
        self.assertEqual(list(got["projects"][0]), list(taskhomes.HEALTH_KEYS))

    def test_the_day_is_local_not_utc(self):
        """Under UTC the child filed at 06:30Z would be 'today' and the row
        closed at 06:59Z would be 'closed today'; under the host's local
        day (UTC-7 here) both are yesterday."""
        self.seed()
        rc, out, err = self.cli("health", "--project", ALPHA, "--json")
        self.assertEqual(rc, 0, err)
        line = json.loads(out)["projects"][0]
        self.assertEqual(line["opened_today"], 1)
        self.assertEqual(line["closed_today"], 1)
        # the same ledger read under UTC counts both of those as today
        with mock.patch.dict(os.environ, {"TZ": "UTC+00"}):
            time.tzset()
            rc, out, err = self.cli("health", "--project", ALPHA, "--json")
        time.tzset()
        self.assertEqual(rc, 0, err)
        line = json.loads(out)["projects"][0]
        self.assertEqual(line["opened_today"], 2)
        self.assertEqual(line["closed_today"], 2)


if __name__ == "__main__":
    unittest.main()
