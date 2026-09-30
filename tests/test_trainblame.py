#!/usr/bin/env python3
"""`helm train blame` — DOES A RED TRAIN NAME ITS OWN CULPRIT, EJECT IT, AND
NEVER DO EITHER SILENTLY?

Every arm drives the shipped verb against a REAL temp repository, a REAL train
composed by the real `helm train --apply` over REAL dispatch rows with REAL
approves, a REAL red receipt in the temp home's gate ledger, and the REAL
landing-window door for the train it composes again. The substitutes are the
seams this box must never actually spend:

  * THE FAB SEAM FOR PREFIX RUNS (`fab=`) answers each `fab test` the verb
    hands it by READING THE WORKTREE IT WAS POINTED AT: a prefix is red when
    the files the arm names are present there, so a clash (two cars together)
    and a red trunk are facts of the tree, not of a script. Its output is
    unittest's own, printed by a real TextTestRunner, between the markers the
    verb's command prints, after the line Fab prints about its placement.
  * THE DOOR'S OWN SPIES (`door=`), shared by reference from
    tests/test_landwindow.py, for the gate the ejection launches.
  * THE TELLING SEAMS (`tell=`): the DM, the task comment and the room line,
    recorded, and able to fail.

EACH ARM CARRIES ITS CONTROL ON THE SAME OBSERVABLE: an arm that proves
"nothing was ejected" is paired with the changed fact that ejects.
"""
import contextlib
import io
import json
import os
import re
import unittest
from unittest import mock

from helm import eventledger, gate, gatewindow, landwindow
from helm.work import _lanes
from tests import test_landwindow as _lw
from tests._tmphome import own_env

HOSTS_ENV = "HELM_GATE_HOSTS"
EXCLUDE_ENV = "FAB_EXCLUDE_HOSTS"
KEY_ENV = "HELM_GATE_WINDOW_KEY"

TEST_FILE = "tests/test_widget.py"
TEST_ID = "tests.test_widget.WidgetTest.test_it"
_NONCE = re.compile(r"helm-train-blame-([0-9a-f]+) BEGIN")


def _blame():
    """The module under test, imported where each arm runs so a tree without
    it fails every arm by name instead of failing this module's import."""
    from helm import trainblame
    return trainblame


def unittest_text(red):
    """What unittest prints for the one widget test, red or green: a real
    TextTestRunner over a case whose id is TEST_ID."""
    def test_it(_self):
        if red:
            raise AssertionError("boom")
    case = type("WidgetTest", (unittest.TestCase,),
                {"test_it": test_it, "__module__": "tests.test_widget"})
    stream = io.StringIO()
    unittest.TextTestRunner(stream=stream, verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(case))
    return stream.getvalue()


class BlameBase(_lw.TrainBase):
    FAST, SLOW = "fastbox", "slowbox"

    def setUp(self):
        super().setUp()
        # THE ROUTING CONFIG IS THE ARM'S, never the box's.
        own_env(self, HOSTS_ENV, "%s %s" % (self.FAST, self.SLOW))
        own_env(self, EXCLUDE_ENV, "")
        own_env(self, KEY_ENV, "")
        # THE FAILING TEST LIVES ON TRUNK, so every prefix can run it.
        os.makedirs(os.path.join(self.repo, "tests"), exist_ok=True)
        self.trunk = self.commit("widget test", path=TEST_FILE)
        self.runs, self.dms, self.comments, self.posts = [], [], [], []

    # -- the train ---------------------------------------------------------

    def car(self, name, path):
        return self.ready(name, path, base=self.trunk)

    def compose(self, *cars):
        """A real train of `cars` ((name, path) pairs), in that order.

        `helm train` merges in the order the rows became READY and breaks a
        tie by row id, and rows made in one second tie. So each approve is
        backdated one step further than the next, through the fixture's own
        clock (`age`), and the merge order is asserted before any arm reads
        a prefix."""
        rows = [self.car(name, path) for name, path in cars]
        for step, (row, _tip) in enumerate(rows):
            self.age(row["id"], 100 * (len(rows) - step))
        rc, text = self.train()
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.cars_of(self.room()), [n for n, _p in cars])
        return rows

    def red(self, traceback="AssertionError: boom", room=None, status="FAILED",
            test=TEST_ID):
        """A red receipt for the room's head, in the minting grammar, plus the
        window's job log that names it -> (receipt id, log path)."""
        room = room or self.room()
        head = self.git("rev-parse", "HEAD", cwd=room)
        tree = self.git("rev-parse", "HEAD^{tree}", cwd=room)
        failures = [] if status == "OK" else [
            {"kind": "ERROR", "test": test, "traceback": traceback}]
        row = {"v": 4, "event": "gate", "ts": "2026-09-25T20:00:00Z",
               "repo_id": room, "head": head, "tree": tree, "dirty": False,
               "head_after": head, "tree_after": tree, "dirty_after": False,
               "interpreter": {"name": "cpython", "version": "3.13.7",
                               "language": "3.13.7",
                               "executable": "/usr/bin/python3"},
               "host": {"node": "fixture-node", "system": "Linux",
                        "release": "6.8.0", "id": "ab" * 8},
               "argv": ["/usr/bin/python3", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
               "suite": True, "label": "train1",
               "rc": 0 if status == "OK" else 1, "wall": 12.5,
               "status": status, "ran": 100, "skipped": 0,
               "detail": "" if status == "OK" else "errors=1",
               "elapsed": 12.0, "failures": failures,
               "failures_unreadable": False, "base_check": None}
        row["id"] = gate._receipt_id(row)
        path = gate.receipts_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        rows, unavailable, skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(skipped, 0, "the fixture receipt failed its own "
                                     "integrity recompute")
        log = gatewindow.log_path(self.store, "gate-" + "e" * 64)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"exit": row["rc"], "receipt": row["id"],
                                 "state": "COMPLETED",
                                 "verdict": "imported"}) + "\n")
        return row["id"], log

    # -- the seams ---------------------------------------------------------

    def prefix_fab(self, red_when):
        """The prefix-run seam: red exactly when `red_when(worktree)` says
        so, placed on the first configured host the exclusion leaves.

        Both unittest reports are printed HERE, in the arm's thread: the verb
        calls `_fab` from its thread pool, and a TextTestRunner opened there
        is a warnings.catch_warnings() window two threads can close out of
        order (task/3398, tests/_warnstate)."""
        report = {red: unittest_text(red) for red in (False, True)}

        def _fab(argv, timeout=None, env=None):
            given = None if env is None else env.get(EXCLUDE_ENV)
            shut = set((given or "").split())
            host = next((h for h in (self.FAST, self.SLOW) if h not in shut),
                        "anywhere")
            where = argv[argv.index("--repo") + 1]
            head = self.git("rev-parse", "HEAD", cwd=where)
            self.runs.append({"argv": list(argv), "exclude": given,
                              "host": host, "head": head})
            nonce = _NONCE.search(argv[-1]).group(1)
            red = red_when(where)
            out = ("fab: role=test-primary -> %s (priority=normal)\n"
                   "helm-train-blame-%s BEGIN\n%s"
                   "helm-train-blame-%s END %d\nfab: exit=%d\n"
                   % (host, nonce, report[red], nonce, int(red), int(red)))
            return int(red), out, ""
        return _fab

    def tell(self, dm_error=None, comment_error=None):
        def dm(to, text, who=None):
            self.dms.append((to, text))
            return (None, dm_error) if dm_error else ({"id": "d1"}, None)

        def comment(task, text, by=None):
            self.comments.append((task, text))
            return (None, comment_error) if comment_error \
                else ({"id": task}, None)

        def post(text, room=None, who=None):
            self.posts.append((room, text))
            return {"id": "p1"}
        return {"message": dm, "comment": comment, "post": post}

    def has(self, *paths):
        return lambda where: all(os.path.exists(os.path.join(where, p))
                                 for p in paths)

    def blame(self, apply=True, gate_token=None, red_when=None, tell=None,
              as_json=False, room=None, audits=None):
        out = io.StringIO()
        rc = _blame().blame(
            room or self.room(), gate=gate_token, apply=apply,
            as_json=as_json,
            fab=self.prefix_fab(red_when or (lambda _w: False)),
            door=self.door("r2", live=()), tell=tell or self.tell(), out=out,
            **({"audits": audits} if audits else {}))
        return rc, out.getvalue()

    def cars_of(self, room):
        return [s.split("merge lane ")[1] for s in self.git(
            "log", "--reverse", "--first-parent", "--format=%s",
            self.trunk + "..HEAD", cwd=room).splitlines()]

    def ejections(self):
        """Every record in the project's ejection store, oldest first."""
        rows, unavailable = eventledger.checked_events(
            landwindow.ejections_path(_lanes.find_root(self.repo)),
            strict=True)
        self.assertIsNone(unavailable, unavailable)
        return rows

    def blame_rooms(self):
        box = os.path.join(os.path.realpath(self.repo) + "-wt", "blame")
        return os.listdir(box) if os.path.isdir(box) else []


class OneNamedCarIsEjectedWithoutBisect(BlameBase):

    def test_the_one_car_whose_diff_touches_the_failing_test_is_ejected(self):  # noqa: VACUOUS_ASSERTION — no bisect run is the contract; the new room's cars, the launch and the DM are positive
        self.compose(("one", "g"), ("two", TEST_FILE), ("three", "i"))
        gid, log = self.red()
        spawned = len(self.spawns)
        rc, text = self.blame()
        self.assertEqual(rc, 0, text)
        self.assertIn("EJECT", text)
        self.assertIn("lane two", text)
        # NO BISECT: blame by diff named exactly one car (R2).
        self.assertEqual(self.runs, [])
        # THE TRAIN GOES ON WITHOUT IT, composed the same way, through the
        # door: a new room, the other cars in their order, one more launch.
        again = self.room("train1b")
        self.assertEqual(self.cars_of(again), ["one", "three"])
        self.assertEqual(len(self.spawns), spawned + 1, self.spawns)
        self.assertEqual(self.spawns[-1][:5],
                         ["fab", "gate", "submit", "--repo", again])
        # NEVER SILENT: the lane's author is told the test, the log and the
        # gate, and the helm room hears one line.
        self.assertEqual([to for to, _t in self.dms], ["integrator"])
        said = self.dms[0][1]
        for fact in (TEST_ID, log, gid, "train1b"):
            self.assertIn(fact, said)
        self.assertEqual([room for room, _t in self.posts], ["helm"])
        self.assertIn("two", self.posts[0][1])
        self.assertEqual(len(self.posts[0][1].splitlines()), 1)
        # AND IT IS RECORDED, bound to the car's exact tip, so the next plan
        # leaves it out (EjectedTipsStayOut).
        tip = self.git("rev-parse", "two")
        record = [r for r in self.ejections() if r["event"] == "eject"]
        self.assertEqual([(r["tip"], r["train"], r["gate"], r["verdict"])
                          for r in record], [(tip, "train1", gid, "EJECT")])
        self.assertIn("blame by diff", record[0]["evidence"])
        self.assertEqual(record[0]["tests"], [TEST_ID])


class TwoNamedCarsBisect(BlameBase):

    def test_the_first_red_prefix_names_the_car_that_is_ejected(self):  # noqa: VACUOUS_ASSERTION — the verdict, the prefix heads and the new room's cars are unconditional; the loops only add per-run shape
        # one touches a helm file the traceback names, two the failing test's
        # own file: TWO named cars, so the verb bisects. Only two breaks it.
        self.compose(("one", "helm/alpha.py"), ("two", TEST_FILE),
                     ("three", "i"))
        self.red('File "/node/wt/helm/alpha.py", line 2, in f | '
                 'AssertionError: boom')
        marker = os.path.join("tests", "test_widget.py")

        def broken(where):
            with open(os.path.join(where, marker), encoding="utf-8") as fh:
                return "two" in fh.read()
        rc, text = self.blame(red_when=broken, as_json=True)
        self.assertEqual(rc, 0, text)
        got = json.loads(text)
        self.assertEqual(got["verdict"]["kind"], "EJECT")
        self.assertEqual(got["verdict"]["by"], "bisect")
        self.assertEqual(got["verdict"]["car"]["lane"], "two")
        # EVERY PREFIX RAN IN ITS OWN WORKTREE, AT ITS OWN COMMIT, and the
        # runs of one round never shared a host.
        heads = {run["head"] for run in self.runs}
        prefixes = got["room"]["prefixes"]
        self.assertTrue({prefixes[0], prefixes[-1]} <= heads, heads)
        for wave in got["rounds"]:
            hosts = [run["host"] for run in wave]
            self.assertEqual(len(hosts), len(set(hosts)), wave)
            self.assertTrue(set(hosts) <= {self.FAST, self.SLOW}, wave)
        # ONLY THE FAILING MODULE, through the gate's own runner.
        for run in self.runs:
            self.assertIn("tests.test_widget", run["argv"][-1])
            self.assertNotIn("discover", run["argv"][-1])
        # The rooms are cleaned up after the runs.
        self.assertEqual(self.blame_rooms(), [])
        self.assertEqual(self.cars_of(self.room("train1b")), ["one", "three"])


class ZeroNamedAndTrunkRed(BlameBase):

    def test_trunk_red_ejects_nothing_and_says_a_composition_cure_is_owed(self):  # noqa: VACUOUS_ASSERTION — nothing ejected is the contract; the control on the same train ejects and composes
        self.compose(("one", "g"), ("two", "h"))
        self.red()
        spawned = len(self.spawns)
        # Every tree fails the module, trunk included.
        rc, text = self.blame(red_when=lambda _w: True)
        self.assertEqual(rc, 0, text)
        self.assertIn("TRUNK-RED", text)
        self.assertIn("composition cure", text)
        self.assertEqual(len(self.spawns), spawned)
        self.assertFalse(os.path.exists(self.room("train1b")))
        self.assertEqual((self.dms, self.comments, self.posts), ([], [], []))
        # CONTROL on the same train: trunk green, the second car red.
        rc2, text2 = self.blame(red_when=self.has("h"))
        self.assertEqual(rc2, 0, text2)
        self.assertIn("EJECT", text2)
        self.assertIn("lane two", text2)
        self.assertTrue(os.path.isdir(self.room("train1b")))


class TheFullTrainGreenIsAFlake(BlameBase):

    def test_a_green_rerun_of_the_full_train_ejects_nothing(self):  # noqa: VACUOUS_ASSERTION — nothing ejected is the contract; the FLAKE verdict and its re-gate line are positive
        self.compose(("one", "g"), ("two", "h"))
        self.red()
        rc, text = self.blame(red_when=lambda _w: False)
        self.assertEqual(rc, 0, text)
        self.assertIn("FLAKE", text)
        self.assertIn("helm gate window launch --repo %s --label train1"
                      % self.room(), text)
        self.assertFalse(os.path.exists(self.room("train1b")))
        self.assertEqual((self.dms, self.posts), ([], []))

    def test_a_flake_records_the_tree_and_the_failing_tests(self):  # noqa: VACUOUS_ASSERTION — an empty ejection store is the contract; the FLAKED row naming this tree and TEST_ID is the positive control
        """A FLAKE is written to its own store, naming the red receipt's
        tree and the tests that flaked. The ejection store stays empty: a
        flake never keeps a car out of the next train."""
        self.compose(("one", "g"), ("two", "h"))
        gid, _log = self.red()
        # The receipt is minted for the composed train room, not the trunk
        # checkout this fixture's git() defaults to.
        tree = self.git("rev-parse", "HEAD^{tree}", cwd=self.room())
        rc, text = self.blame(red_when=lambda _w: False)
        self.assertEqual(rc, 0, text)
        self.assertIn("FLAKE", text)
        self.assertEqual(self.ejections(), [])
        root = _lanes.find_root(self.repo)
        by_tree, unavailable = landwindow.read_flakes(root)
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(by_tree, {tree: {TEST_ID}})
        rows, unavailable = eventledger.checked_events(
            landwindow.flakes_path(root), strict=True)
        self.assertIsNone(unavailable, unavailable)
        self.assertEqual(rows[-1]["event"], landwindow.FLAKED)
        self.assertEqual(rows[-1]["tree"], tree)
        self.assertEqual(rows[-1]["gate"], gid)
        self.assertEqual(rows[-1]["train"], "train1")
        self.assertEqual(rows[-1]["tests"], [TEST_ID])
        self.assertTrue(rows[-1]["why"])


class AClashNamesTheEarlierCar(BlameBase):

    def test_a_car_green_alone_on_trunk_names_the_car_it_clashed_with(self):
        # alpha alone is fine, beta alone is fine, both together are red.
        # The traceback names alpha, and three touches the failing test's
        # file, so two cars are named and the verb bisects.
        self.compose(("one", "helm/alpha.py"), ("two", "helm/beta.py"),
                     ("three", TEST_FILE))
        self.red('File "/node/wt/helm/alpha.py", line 2, in f | '
                 'AssertionError: boom')
        rc, text = self.blame(
            red_when=self.has("helm/alpha.py", "helm/beta.py"), as_json=True)
        self.assertEqual(rc, 0, text)
        got = json.loads(text)
        self.assertEqual(got["verdict"]["car"]["lane"], "two")
        self.assertEqual(got["verdict"]["alone"], "GREEN")
        self.assertEqual([c["lane"] for c in got["verdict"]["clash"]],
                         ["one"])
        # THE DM SAYS WHICH CAR IT CLASHED WITH.
        self.assertIn("lane one", self.dms[0][1])
        # CONTROL: a car red alone on trunk clashes with nobody.
        self.assertEqual(self.cars_of(self.room("train1b")), ["one", "three"])


class EjectionIsNeverSilent(BlameBase):

    def compose_task_lane(self):
        self.compose(("one", "g"), ("fix-task-7", TEST_FILE))
        return self.red()

    def test_the_dm_and_the_task_comment_carry_the_evidence(self):
        gid, log = self.compose_task_lane()
        rc, text = self.blame()
        self.assertEqual(rc, 0, text)
        self.assertEqual([task for task, _t in self.comments], ["task/7"])
        for said in (self.dms[0][1], self.comments[0][1]):
            for fact in (TEST_ID, log, gid):
                self.assertIn(fact, said)
        self.assertTrue(os.path.isdir(self.room("train1b")))

    def test_a_failing_dm_refuses_loudly_and_composes_nothing(self):  # noqa: VACUOUS_ASSERTION — the refusal is the contract; the refusal text and the printed undelivered text are positive, and the sibling arm composes
        self.compose_task_lane()
        spawned = len(self.spawns)
        rc, text = self.blame(tell=self.tell(dm_error="no such seat"))
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", text)
        self.assertIn("no such seat", text)
        # THE UNDELIVERED TEXT IS PRINTED, so a person can deliver it.
        self.assertIn(TEST_ID, text)
        self.assertFalse(os.path.exists(self.room("train1b")))
        self.assertEqual(len(self.spawns), spawned)
        self.assertEqual(self.comments, [])
        # NOTHING RECORDED: an ejection nobody was told of must not keep the
        # car out of the next train either.
        self.assertEqual(self.ejections(), [])

    def test_a_record_that_cannot_be_written_refuses_loudly(self):  # noqa: VACUOUS_ASSERTION — the refusal is the contract; the sibling arm writes the record and composes
        self.compose_task_lane()
        spawned = len(self.spawns)
        with mock.patch.object(landwindow, "record_ejection",
                               lambda *a, **k: (None, "disk full")):
            rc, text = self.blame()
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", text)
        self.assertIn("disk full", text)
        self.assertFalse(os.path.exists(self.room("train1b")))
        self.assertEqual(len(self.spawns), spawned)

    def test_a_failing_task_comment_refuses_loudly_and_composes_nothing(self):  # noqa: VACUOUS_ASSERTION — the refusal is the contract; the control on the same train composes
        self.compose_task_lane()
        spawned = len(self.spawns)
        rc, text = self.blame(tell=self.tell(comment_error="ledger locked"))
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", text)
        self.assertIn("ledger locked", text)
        self.assertFalse(os.path.exists(self.room("train1b")))
        self.assertEqual(len(self.spawns), spawned)
        # CONTROL: the same train, the comment written.
        rc2, text2 = self.blame()
        self.assertEqual(rc2, 0, text2)
        self.assertTrue(os.path.isdir(self.room("train1b")))


class AHandComposedTrainIsRead(BlameBase):
    """THE TRAINS ON TRUNK WERE COMPOSED BY HAND: every `trainN: merge lane`
    merge on helm's trunk carries a description after the lane (`train283:
    merge lane cursor-bridge-resume-refusal: the vetted ...`, `train281:
    merge lane sliced-land-shadow-w4 (W4, ...)`) and none names its land
    request in its body. Blame reads that room too, and finds each car's land
    request by its reviewed tip."""

    def test_a_hand_composed_train_is_blamed_and_its_car_ejected(self):
        one = self.car("one", "g")[1]
        two = self.car("two", TEST_FILE)[1]
        room = self.room()
        self.git("worktree", "add", "-q", "--detach", room, self.trunk)
        for tip, subject in ((one, "train1: merge lane one (the first car)"),
                             (two, "train1: merge lane two: the second car")):
            self.git("-c", "rerere.enabled=false", "merge", "-q", "--no-ff",
                     "-m", subject, tip, cwd=room)
        gid, _log = self.red()
        rc, text = self.blame()
        self.assertEqual(rc, 0, text)
        self.assertIn("EJECT", text)
        self.assertIn("lane two", text)
        # THE LAND REQUEST WAS FOUND BY THE TIP: its author was told, and the
        # record names the row.
        self.assertEqual([to for to, _t in self.dms], ["integrator"])
        record = self.ejections()[-1]
        self.assertEqual((record["tip"], record["gate"]), (two, gid))
        self.assertTrue(record["lr"] and record["lr"] != "?", record)
        self.assertEqual(self.cars_of(self.room("train1b")), ["one"])


class EjectedTipsStayOut(BlameBase):
    """THE EJECTION BINDS THE TIP: the next `helm train` leaves an ejected
    tip out, a new tip on the lane rides again, a readmit by hand lets the
    same tip ride and records who and why, and a store nobody can read makes
    `--apply` refuse rather than guess either way."""

    def eject_two(self):
        rows = self.compose(("one", "g"), ("two", TEST_FILE), ("three", "i"))
        # CONTROL FIRST: before any ejection the next plan takes all three.
        self.assertEqual(self.lanes(self.plan()), ["one", "two", "three"])
        gid, _log = self.red()
        rc, text = self.blame()
        self.assertEqual(rc, 0, text)
        return rows[1][1], gid

    def plan(self):
        got, why = landwindow.plan(self.repo, trunk=self.main, name="train9")
        self.assertIsNone(why, why)
        return got

    def lanes(self, got):
        return [car["lane"] for car in got["cars"]]

    def verb(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = landwindow.cmd_train(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_an_ejected_tip_is_excluded_from_the_next_plan(self):
        tip, gid = self.eject_two()
        got = self.plan()
        self.assertEqual(self.lanes(got), ["one", "three"])
        why = [car["why"] for car in got["excluded"] if car["tip"] == tip]
        self.assertEqual(len(why), 1, got["excluded"])
        for fact in ("ejected from train1", tip[:12], TEST_ID, "gate:" + gid,
                     "helm train readmit"):
            self.assertIn(fact, why[0])
        rc, text = self.train(apply=False, name="train9")
        self.assertEqual(rc, 0, text)
        self.assertIn("ejected from train1", text)

    def test_a_new_tip_on_the_lane_rides_again(self):  # noqa: VACUOUS_ASSERTION — the new tip is asserted present unconditionally; the old tip's absence is the contract beside it
        old, _gid = self.eject_two()
        self.git("checkout", "-q", "two")
        new = self.commit("two, cured", path=TEST_FILE)
        self.git("checkout", "-q", self.main)
        row = self.dispatch(ref=new, lane="two")
        _row, err = self.mark_verdict(row["id"], new, "ok", polarity="approve")
        self.assertIsNone(err, err)
        tips = [car["tip"] for car in self.plan()["cars"]]
        self.assertIn(new, tips)
        self.assertNotIn(old, tips)

    def test_a_readmit_by_hand_lets_the_tip_ride_and_records_who_and_why(self):
        tip, _gid = self.eject_two()
        # REFUSALS FIRST: a readmit needs a reason, and a tip nobody ejected
        # has nothing to readmit.
        self.assertEqual(self.verb("readmit", tip[:12], "--repo",
                                   self.repo)[0], 2)
        never = self.git("rev-parse", "one")
        rc, text = self.verb("readmit", never[:12], "--reason", "no",
                             "--repo", self.repo)
        self.assertEqual(rc, 1, text)
        self.assertNotIn(tip, [car["tip"] for car in self.plan()["cars"]])
        rc, text = self.verb("readmit", tip[:12], "--reason",
                             "the blame was wrong", "--repo", self.repo)
        self.assertEqual(rc, 0, text)
        self.assertIn(tip, [car["tip"] for car in self.plan()["cars"]])
        last = self.ejections()[-1]
        self.assertEqual((last["event"], last["tip"], last["by"],
                          last["reason"]),
                         ("readmit", tip, "integrator", "the blame was wrong"))

    def test_an_unreadable_store_refuses_apply_and_guesses_neither_way(self):  # noqa: VACUOUS_ASSERTION — the refusal is the contract; every car listed with UNKNOWN and the readable-store control that composes are positive
        tip, _gid = self.eject_two()
        path = landwindow.ejections_path(_lanes.find_root(self.repo))
        with open(path, encoding="utf-8") as fh:
            good = fh.read()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        got = self.plan()
        self.assertTrue(got["ejections_unknown"])
        # NEITHER WAY SILENTLY: the ejected tip is not dropped as excluded
        # nor passed as clean; every car says its ejection is UNKNOWN.
        self.assertEqual(self.lanes(got), ["one", "two", "three"])
        self.assertEqual({car["ejection"] for car in got["cars"]}, {"UNKNOWN"})
        rc, text = self.train(apply=False, name="train9")
        self.assertEqual(rc, 0, text)
        self.assertIn("ejection UNKNOWN", text)
        spawned = len(self.spawns)
        rc, text = self.train(name="train9", door=self.door("r3", live=()))
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", text)
        self.assertIn("ejection store", text)
        self.assertFalse(os.path.exists(self.room("train9")))
        self.assertEqual(len(self.spawns), spawned)
        # CONTROL: the same store, readable again, composes without the car.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(good)
        rc, text = self.train(name="train9", door=self.door("r3", live=()))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.cars_of(self.room("train9")), ["one", "three"])
        self.assertNotIn(tip, self.git("log", "--format=%P", "-n", "5",
                                       cwd=self.room("train9")))


class TheCallersExclusionSurvives(BlameBase):

    def test_every_prefix_run_keeps_the_callers_excluded_hosts(self):  # noqa: VACUOUS_ASSERTION — self.runs is asserted non-empty before the loop, and the ejection's rc is unconditional
        own_env(self, HOSTS_ENV, "%s %s keepout" % (self.FAST, self.SLOW))
        own_env(self, EXCLUDE_ENV, "keepout")
        self.compose(("one", "g"), ("two", "h"), ("three", "i"))
        self.red()
        rc, text = self.blame(red_when=self.has("h"))
        self.assertEqual(rc, 0, text)
        self.assertTrue(self.runs)
        for run in self.runs:
            shut = run["exclude"].split()
            # EXTENDED, NEVER REPLACED: the caller's host stays out, and so
            # does every known host but the one this run is pinned to.
            self.assertIn("keepout", shut)
            self.assertNotIn(run["host"], shut)
            self.assertEqual(sorted(shut), sorted(
                {"keepout", self.FAST, self.SLOW} - {run["host"]}))


class NothingRunsWithoutApply(BlameBase):

    def test_a_dry_run_mints_nothing_runs_nothing_and_tells_nobody(self):  # noqa: VACUOUS_ASSERTION — nothing minted is the contract; the control with --apply runs and composes
        self.compose(("one", "g"), ("two", "h"), ("three", "i"))
        self.red()
        spawned = len(self.spawns)
        rc, text = self.blame(apply=False, red_when=self.has("h"))
        self.assertEqual(rc, 0, text)
        self.assertIn("dry run", text)
        self.assertIn("bisect", text)
        self.assertIn(self.FAST, text)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.blame_rooms(), [])
        self.assertFalse(os.path.exists(self.room("train1b")))
        self.assertEqual(len(self.spawns), spawned)
        self.assertEqual((self.dms, self.comments, self.posts), ([], [], []))
        # CONTROL: the same verb with --apply runs and ejects.
        rc2, text2 = self.blame(red_when=self.has("h"))
        self.assertEqual(rc2, 0, text2)
        self.assertTrue(self.runs)
        self.assertTrue(os.path.isdir(self.room("train1b")))


class TheGateMustBeRead(BlameBase):

    def test_an_unreadable_or_green_gate_refuses_by_name(self):
        self.compose(("one", "g"), ("two", TEST_FILE))
        rc, text = self.blame(gate_token="gate:" + "f" * 16)
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", text)
        self.assertIn("f" * 16, text)
        rc2, text2 = self.blame()
        self.assertEqual(rc2, 1, text2)
        self.assertIn("no receipt", text2)
        gid, _log = self.red(status="OK")
        rc3, text3 = self.blame(gate_token="gate:" + gid)
        self.assertEqual(rc3, 1, text3)
        self.assertIn("GREEN", text3)
        self.assertEqual((self.runs, self.dms), ([], []))
        # CONTROL: a red receipt for the same head is read and blamed.
        gid2, _log2 = self.red()
        rc4, text4 = self.blame(gate_token="gate:" + gid2)
        self.assertEqual(rc4, 0, text4)
        self.assertIn("EJECT", text4)


class ARedPreGateAuditIsBlamedByDiffAlone(BlameBase):
    """task/3674. Auto-land's pre-gate audits ran on the composed room and
    failed before any gate: blame reads their LOG in place of a receipt.
    When blame by diff names exactly one car, that car is told, recorded and
    ejected, and the train composed again without it, as a red gate's EJECT
    is. No car named, two named, or a log with no readable failure list
    ejects nothing, and a red audit is never bisected."""

    def audit_log(self, frames=(), report=None, name="train1-audits.log"):
        """The log `autoland.Ops.audits` writes for a red run of the widget
        test: the census head, the fab line, Fab's own words around the run,
        and unittest's real report (`report`, default the red widget run)
        with `frames` added to its traceback. -> its path."""
        report = unittest_text(True) if report is None else report
        for frame in frames:
            report = report.replace(
                "Traceback (most recent call last):\n",
                "Traceback (most recent call last):\n  File \"%s\", line 2, "
                "in f\n    f()\n" % frame, 1)
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("census: 1 extra from 1 touched paths (merge-base "
                     "abc, tip def, 0.1 s)\n"
                     "tests.test_widget: touched tests/test_widget.py\n"
                     "$ fab test --repo %s -- python3 -m unittest "
                     "tests.test_widget\n"
                     "fab: role=test-primary -> %s (priority=normal)\n\n"
                     "%sfab: exit=1\n" % (self.room(), self.FAST, report))
        return path

    def assert_nothing_ejected(self, rc, text, spawned):
        self.assertEqual(rc, 1, text)
        self.assertIn("REFUSED", text)
        self.assertNotIn("EJECT car", text)
        # NEVER BISECTED: no prefix ran, and no blame room was minted.
        self.assertEqual(self.runs, [])
        self.assertEqual(self.blame_rooms(), [])
        self.assertFalse(os.path.exists(self.room("train1b")))
        self.assertEqual(len(self.spawns), spawned)
        self.assertEqual((self.dms, self.comments, self.posts), ([], [], []))
        self.assertEqual(self.ejections(), [])

    def test_the_one_car_the_audits_failure_names_is_ejected(self):  # noqa: VACUOUS_ASSERTION — the loop is over a fixed four-fact tuple; the DM, the record, the readmit and the new room's cars are unconditional
        self.compose(("one", "g"), ("two", TEST_FILE), ("three", "i"))
        log = self.audit_log()
        spawned = len(self.spawns)
        rc, text = self.blame(audits=log)
        self.assertEqual(rc, 0, text)
        self.assertIn("EJECT car 2 (lane two", text)
        self.assertIn("pre-gate audits", text)
        # BY DIFF, NEVER A BISECT: no prefix ran.
        self.assertEqual(self.runs, [])
        # THE TRAIN GOES ON WITHOUT IT, composed and gated by the same door.
        again = self.room("train1b")
        self.assertEqual(self.cars_of(again), ["one", "three"])
        self.assertEqual(len(self.spawns), spawned + 1, self.spawns)
        self.assertEqual(self.spawns[-1][:5],
                         ["fab", "gate", "submit", "--repo", again])
        # TOLD FIRST: the failing test, the audits' log, and what went red.
        self.assertEqual([to for to, _t in self.dms], ["integrator"])
        said = self.dms[0][1]
        for fact in (TEST_ID, log, "pre-gate audits", "train1b"):
            self.assertIn(fact, said)
        self.assertNotIn("gate:None", said)
        self.assertEqual([room for room, _t in self.posts], ["helm"])
        self.assertIn("pre-gate audits", self.posts[0][1])
        self.assertNotIn("gate:None", self.posts[0][1])
        # RECORDED against the exact tip, so `helm train readmit` clears it,
        # and the plan's EXCLUDED line names the audits, not a gate.
        tip = self.git("rev-parse", "two")
        record = [r for r in self.ejections() if r["event"] == "eject"]
        self.assertEqual([(r["tip"], r["train"], r["gate"], r["audits"],
                           r["tests"]) for r in record],
                         [(tip, "train1", None, log, [TEST_ID])])
        reason = landwindow.ejection_reason(record[0])
        self.assertIn("(its pre-gate audits)", reason)
        self.assertNotIn("gate:", reason)
        _row, err = landwindow.readmit(_lanes.find_root(self.repo), tip,
                                       "the audit blame was wrong",
                                       "integrator")
        self.assertIsNone(err, err)

    def test_two_named_cars_eject_nothing_and_are_never_bisected(self):
        # one touches the helm file the traceback names, two the failing
        # test's own file: two named cars.
        self.compose(("one", "helm/alpha.py"), ("two", TEST_FILE),
                     ("three", "i"))
        spawned = len(self.spawns)
        rc, text = self.blame(
            audits=self.audit_log(frames=("/node/room/helm/alpha.py",)),
            red_when=self.has(TEST_FILE))
        self.assert_nothing_ejected(rc, text, spawned)
        self.assertIn("names 2 car(s)", text)
        self.assertIn("never bisected", text)
        # CONTROL: the same train, the traceback naming no car's helm file,
        # names car two alone and ejects it.
        rc2, text2 = self.blame(audits=self.audit_log(name="control.log"))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(self.cars_of(self.room("train1b")),
                         ["one", "three"])

    def test_no_named_car_ejects_nothing_and_is_never_bisected(self):
        # the failing test lives on trunk and no car touches it.
        self.compose(("one", "helm/alpha.py"), ("three", "i"))
        spawned = len(self.spawns)
        rc, text = self.blame(audits=self.audit_log(),
                              red_when=self.has(TEST_FILE))
        self.assert_nothing_ejected(rc, text, spawned)
        self.assertIn("names 0 car(s)", text)
        # CONTROL: the same train, the traceback naming car one's helm file,
        # names car one alone and ejects it.
        rc2, text2 = self.blame(audits=self.audit_log(
            frames=("/node/room/helm/alpha.py",), name="control.log"))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(self.cars_of(self.room("train1b")), ["three"])

    def test_words_after_the_summary_place_no_failure(self):
        # The job's stdout, flushed as the runner exits, and Fab's closing
        # words follow unittest's summary in the log. A gate's receipt holds
        # neither, so neither names a path of the failure: here the failing
        # test lives on trunk, no car touches it, and only the words after
        # the summary name car one's helm file.
        self.compose(("one", "helm/alpha.py"), ("three", "i"))
        spawned = len(self.spawns)
        rc, text = self.blame(
            audits=self.audit_log(report=unittest_text(True) + (
                "flushed at exit: /node/room/helm/alpha.py\n")),
            red_when=self.has(TEST_FILE))
        self.assert_nothing_ejected(rc, text, spawned)
        self.assertIn("names 0 car(s)", text)
        self.assertNotIn("names helm/alpha.py", text)
        # CONTROL: the same path inside the failure's own traceback names
        # car one alone, and it is ejected.
        rc2, text2 = self.blame(audits=self.audit_log(
            frames=("/node/room/helm/alpha.py",), name="control.log"))
        self.assertEqual(rc2, 0, text2)
        self.assertIn("names helm/alpha.py", text2)
        self.assertEqual(self.cars_of(self.room("train1b")), ["three"])

    def test_the_audits_red_names_the_host_the_audits_ran_on(self):
        """Auto-land re-runs the failing modules alone
        on the audits' own host before blame, and that host is the log's
        `fab: role=... -> HOST` placement line."""
        trainblame = _blame()
        self.compose(("one", "g"), ("two", TEST_FILE))
        train, why = trainblame.read_room(self.room("train1"))
        self.assertIsNone(why, why)
        log = self.audit_log()
        red, why = trainblame.audit_red(train, log)
        self.assertIsNone(why, why)
        self.assertEqual(red["host"], self.FAST)
        self.assertEqual([t["id"] for t in red["tests"]], [TEST_ID])
        # CONTROL: the same red log without its placement line names none.
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        bare = os.path.join(self.tmp, "unplaced-audits.log")
        with open(bare, "w", encoding="utf-8") as fh:
            fh.write(text.replace("fab: role=test-primary -> %s "
                                  "(priority=normal)\n" % self.FAST, ""))
        red, why = trainblame.audit_red(train, bare)
        self.assertIsNone(why, why)
        self.assertIsNone(red["host"])

    def test_the_audits_red_re_runs_in_the_audits_own_runner(self):
        """The re-run before blame is the audits' own line: audits that ran
        as slices (the slice runner, with its fail-mode leak audit) re-run
        as slices, so a failure only slices show, a unit's LeakAudit, is
        reproduced and never reads as a pass alone."""
        trainblame = _blame()
        from helm import gateaudits, gatehost
        self.compose(("one", "g"), ("two", TEST_FILE))
        train, why = trainblame.read_room(self.room("train1"))
        self.assertIsNone(why, why)
        serial = self.audit_log()
        with open(serial, encoding="utf-8") as fh:
            text, n = re.subn(r"(?m)^\$ .*$", lambda _m: "$ " + (
                gateaudits.command(self.room(), ["test_widget"],
                                   gatehost.SLICED)), fh.read())
        self.assertEqual(n, 1)
        sliced = os.path.join(self.tmp, "sliced-audits.log")
        with open(sliced, "w", encoding="utf-8") as fh:
            fh.write(text)
        red, why = trainblame.audit_red(train, sliced)
        self.assertIsNone(why, why)
        self.assertEqual(red["mode"], gatehost.SLICED)
        script = trainblame.run_argv(self.room(), red["modules"],
                                     red["mode"], "n")[-1]
        self.assertIn("HELM_GATESLICE_LEAKS=fail", script)
        self.assertIn(gate.SLICE_RUNNER, script)
        # CONTROL: audits that ran as one unittest process re-run as one.
        red, why = trainblame.audit_red(train, serial)
        self.assertIsNone(why, why)
        self.assertEqual(red["mode"], gatehost.SERIAL)
        script = trainblame.run_argv(self.room(), red["modules"],
                                     red["mode"], "n")[-1]
        self.assertIn("python3 -m unittest -v tests.test_widget", script)
        self.assertNotIn(gate.SLICE_RUNNER, script)

    def test_an_audit_log_with_no_readable_failure_list_ejects_nothing(self):
        self.compose(("one", "g"), ("two", TEST_FILE))
        spawned = len(self.spawns)
        gone = os.path.join(self.tmp, "no-such-audits.log")
        rc, text = self.blame(audits=gone)
        self.assert_nothing_ejected(rc, text, spawned)
        self.assertIn(gone, text)
        # a run Fab never finished: no unittest summary at all
        rc, text = self.blame(audits=self.audit_log(
            report="fab: no host could place the run\n", name="fab.log"))
        self.assert_nothing_ejected(rc, text, spawned)
        self.assertIn("no readable failure list", text)
        # a run whose summary is not FAILED names no failure to blame
        rc, text = self.blame(audits=self.audit_log(
            report=unittest_text(False), name="green.log"))
        self.assert_nothing_ejected(rc, text, spawned)
        self.assertIn("no readable failure list", text)
        # a summary counting more failures than the blocks it printed
        rc, text = self.blame(audits=self.audit_log(
            report=unittest_text(True).replace("FAILED (failures=1)",
                                               "FAILED (failures=2)"),
            name="short.log"))
        self.assert_nothing_ejected(rc, text, spawned)
        self.assertIn("no readable failure list", text)
        # CONTROL: the same train and a readable red log ejects car two.
        rc, text = self.blame(audits=self.audit_log())
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.cars_of(self.room("train1b")), ["one"])

    def test_a_one_car_train_ejects_its_car_and_composes_nothing(self):  # noqa: VACUOUS_ASSERTION — no b-room and no launch are the contract; the DM, the ejection record and the room line are positive
        self.compose(("two", TEST_FILE))
        spawned = len(self.spawns)
        rc, text = self.blame(audits=self.audit_log())
        self.assertEqual(rc, 0, text)
        self.assertIn("EJECT car 1 (lane two", text)
        self.assertIn("nothing is left to compose", text)
        self.assertFalse(os.path.exists(self.room("train1b")))
        self.assertEqual(len(self.spawns), spawned)
        self.assertEqual([to for to, _t in self.dms], ["integrator"])
        tip = self.git("rev-parse", "two")
        self.assertEqual([r["tip"] for r in self.ejections()
                          if r["event"] == "eject"], [tip])
        self.assertEqual(len(self.posts), 1, self.posts)


class TheSeamPrintsItsReportsInTheArmsThread(BlameBase):
    """task/3398. The verb calls the fab seam from its thread pool, and a
    TextTestRunner opens a warnings.catch_warnings() window. Two pool threads
    that closed theirs out of order left warnings.filters bound to one
    thread's copy, and the gate's leak audit failed this module
    (tests/_warnstate). So the seam prints both reports when the arm builds
    it, in the arm's own thread, and a call from the pool only reads them."""

    def test_the_seam_prints_its_reports_when_built_never_when_called(self):
        fab = self.prefix_fab(lambda _w: True)
        argv = ["fab", "test", "--repo", self.repo, "--",
                "helm-train-blame-5eed BEGIN"]
        with mock.patch(__name__ + ".unittest_text",
                        return_value="REPORT\n") as printed:
            self.prefix_fab(lambda _w: True)
            built = printed.call_count
            rc, out, _err = fab(argv)
        self.assertEqual(built, 2)
        self.assertEqual(printed.call_count, 2)
        self.assertEqual(rc, 1)
        self.assertIn("helm-train-blame-5eed END 1", out)
        self.assertIn("FAILED (failures=1)", out)


class TheRedIsRead(unittest.TestCase):
    """What a failure NAMES, read off the receipt's diagnosis and its output
    tail, on train283's own shape: the last block of a sliced run carries the
    runner's trailer (`gateslice: ...`, one `gateslice leak:` line per leaking
    module), and those lines belong to no one failure."""

    UNIT = "tests.test_stopfacts.LeakAudit.test_leaves_no_process_state"
    FILES = {"helm/gateslice.py", "helm/seats_stop_seam.py", "helm/offpeak.py"}
    OWN = ("RuntimeError: unit tests.test_stopfacts left process state other "
           "units can see: module data mutated: "
           "helm.seats_stop_seam._PENDING_DISCLOSURES (content)")
    TRAILER = ("gateslice: 16 workers, 499 units, 24540 planned tests\n"
               "gateslice leak: tests.test_seat_cred_state: module data "
               "mutated: helm.offpeak._CLOCK_CACHE (content)\n")

    def names(self, failure, row):
        blame = _blame()
        return blame.helm_paths(blame._failure_text(failure, row), self.FILES)

    def test_the_runner_trailer_names_nobody_and_the_units_own_leak_does(self):
        condensed = ('File "helm/gateslice.py", line 1, in '
                     'test_leaves_no_process_state | unit tests.test_stopfacts'
                     ' | ... traceback truncated ... | %s | %s'
                     % (self.OWN, self.TRAILER.replace("\n", " | ")))
        self.assertEqual(self.names({"test": self.UNIT,
                                     "traceback": condensed}, {}),
                         {"helm/seats_stop_seam.py"})
        tail = ("=" * 70 + "\nERROR: test_leaves_no_process_state (%s)\n"
                % self.UNIT + "-" * 70 + "\nTraceback (most recent call "
                "last):\n  File \"helm/gateslice.py\", line 1, in "
                "test_leaves_no_process_state\n    unit tests.test_stopfacts\n"
                "%s\n\n%s" % (self.OWN, self.TRAILER))
        self.assertEqual(self.names({"test": self.UNIT, "traceback": ""},
                                    {"stderr_tail": tail}),
                         {"helm/seats_stop_seam.py"})


class TheSearch(unittest.TestCase):
    """The bisect as a function of the prefixes' colours, one host or many."""

    def search(self, colours, width):
        probed = []

        def probe(points):
            probed.append(list(points))
            return {p: {"status": colours[p]} for p in points}
        kind, culprit, _note = _blame().search(len(colours) - 1, probe, width)
        return kind, culprit, probed

    def test_one_host_is_a_binary_search(self):
        colours = ["GREEN"] * 5 + ["RED"] * 4          # car 5 broke it
        kind, culprit, probed = self.search(colours, 1)
        self.assertEqual((kind, culprit), ("EJECT", 5))
        self.assertEqual(probed[0], [0, 8])
        self.assertTrue(all(len(p) == 1 for p in probed[1:]), probed)
        self.assertLessEqual(len(probed), 1 + 3)

    def test_enough_hosts_answer_in_one_round_after_the_ends(self):
        colours = ["GREEN"] * 3 + ["RED"] * 3          # car 3
        kind, culprit, probed = self.search(colours, 8)
        self.assertEqual((kind, culprit), ("EJECT", 3))
        self.assertEqual(probed, [[0, 5], [1, 2, 3, 4]])

    def test_the_ends_decide_trunk_red_and_flake(self):
        self.assertEqual(self.search(["RED", "RED", "RED"], 2)[:2],
                         ("TRUNK-RED", None))
        self.assertEqual(self.search(["GREEN", "GREEN", "GREEN"], 2)[:2],
                         ("FLAKE", None))
        self.assertEqual(self.search(["GREEN", "UNKNOWN", "RED", "RED"],
                                     1)[:1], ("UNKNOWN",))


if __name__ == "__main__":
    unittest.main()
