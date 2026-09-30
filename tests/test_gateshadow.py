"""THE SLICED SHADOW BESIDE EVERY TRAIN GATE: scheduled by the window door,
run after it, compared test by test, and unable to land anything.

The launch arms drive `gatewindow.launch`, the shipped door, over the window
suite's real temp repository and its spied fab seams. The runner arms drive
`gateshadow._Pass` over receipts MINTED through the real paths (the canary
fixture's serial mint and sliced door), with the one thing this box must not
do — talk to Fab — replaced by a spy that answers Fab's own event shapes, and
whose import copies a staged ledger into whatever helm home the call carried,
the way `fab gate --import` binds into the importing process's home.

  (a) ScheduleTest       one launch, one shadow of the same tree; the serial
                         job is the request the door always built and declares
                         land authority before Fab chooses its placement
  (b) LandUntouchedTest  a whole shadow pass leaves the land ledger, the
                         tree's last receipt and the land bind unchanged
  (c) CompareTest        AGREE / DISAGREE on a sliced-only failure /
                         DISAGREE on a differing Ran count
  (d) StreakTest         the clock walks the land ledger's trains: it resets
                         on a disagreement, an UNKNOWN, or a train with no
                         readable record, names what stopped it, and needs a
                         RED both runs caught; trains from before the first
                         shadowed train do not stop it
      LandSequenceTest   those trains read from the real land and Fab
                         completion ledgers, joined by generation
  (4) MarkerTest         a DISAGREE writes the canary's DISABLE marker; an
                         AGREE does not
  (f) FlakeTest          a sliced-only miss recorded FLAKE for this tree is
                         UNKNOWN and writes no marker; another tree, or a
                         store that cannot be read, still DISAGREES
  (e) UnknownTest        a crash, a timeout and a yield are UNKNOWN, never
                         AGREE, and none touches the land ledger
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helm import (eventledger, fabgate, gate, gatecanary, gateshadow,
                   gatewindow, home, landwindow)
from tests import test_fabgate as fabfix
from tests import test_land_provenance as prov
from tests.test_gatecanary import AUDIT, B_FAILS, CanaryFixture
from tests.test_gatewindow import (
    INTERPRETER, WindowBase, job_event, measured, observed_event)

STUB_GATE = 'SLICE_VERSION = %d\n"--sliced"\n' % gate.SLICE_VERSION


def generation(name):
    digest = hashlib.sha256(name.encode()).hexdigest()
    return "run-%s-4114-%s" % (digest[:32], digest[32:48])


def handle(name, host="snoozy"):
    key = hashlib.sha256(("key-" + name).encode()).hexdigest()
    return {"key": key, "job_id": "gate-" + key, "host": host,
            "generation": generation(name)}


class ShadowWindowBase(WindowBase):
    """The window suite's repository, plus a commit that ships the slice
    runner, so a room at `self.sliceable` can run slices and one at `self.c`
    cannot."""

    def setUp(self):
        super().setUp()
        for rel in gate.SLICE_RUNNER_FILES + ("helm/gate.py",):
            path = os.path.join(self.repo, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(STUB_GATE if rel == "helm/gate.py" else "")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "ship the slice runner")
        self.sliceable = self.git("rev-parse", "HEAD")
        self.global_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            self.path)))
        self.detached = []

    def detach(self, pid=4242, order=None):
        def _detach(argv, log):
            self.detached.append((list(argv), log))
            if order is not None:
                order.append(("spawn", list(argv[:3])))
            return type("P", (), {"pid": pid})()
        return _detach


class ScheduleTest(ShadowWindowBase):

    def test_a_train_launch_schedules_exactly_one_shadow_of_the_same_tree(self):  # noqa: VACUOUS_ASSERTION — the record count is asserted EQUAL to one with its fields, and the spawns EQUAL to the exact serial submit
        room = self.room("train01", self.sliceable)
        rc, text, _req = self.door(room, label="train01",
                                   detach=self.detach())
        self.assertEqual(rc, 0, text)
        row = gatewindow.read_runs(self.path)[0]
        recs = gateshadow.records(self.global_dir)
        self.assertEqual(len(recs), 1, recs)
        rec = recs[0]
        self.assertEqual(
            (rec["state"], rec["mode"], rec["tree"], rec["head"], rec["room"],
             rec["label"]),
            (gateshadow.SCHEDULED, "sliced-shadow", row["tree"], row["head"],
             row["room"], "train01"))
        self.assertEqual(rec["serial_job"], {k: row[k] for k in (
            "key", "job_id", "host", "generation", "tree")})
        self.assertIn("shadow:", text)
        # THE SERIAL JOB IS THE ONE THE DOOR ALWAYS BUILT: one measure of the
        # serial whole scope, one submit carrying exactly the request helm
        # builds for that scope, and nothing sliced anywhere in the launch.
        self.assertEqual(len(self.measures), 1)
        self.assertEqual(self.measures[0], gatewindow.measure_argv(
            os.path.realpath(room), row["tree"], land_authority=True))
        want, err = fabgate.request(os.path.realpath(room), row["tree"],
                                    fabgate.whole_scope(), INTERPRETER,
                                    json.loads(measured("snoozy"))["runner"])
        self.assertIsNone(err, err)
        self.assertEqual(self.spawns, [gatewindow.submit_argv(
            os.path.realpath(room), want, "train01", land_authority=True)])
        # and the launch spawned the serial client and nothing else: the
        # shadow is a record, never a process of the door's.
        self.assertEqual([argv[0] for argv, _log in self.detached], ["sh"])

        # A SECOND LAUNCH THE WINDOW REFUSES schedules nothing: still one.
        second = self.room("train02", self.sliceable)
        rc2, text2, _r2 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(("r1",)),
                                    detach=self.detach())
        self.assertEqual(rc2, 3, text2)
        self.assertEqual(len(gateshadow.records(self.global_dir)), 1)

    def test_a_room_that_cannot_run_slices_gets_no_shadow(self):  # noqa: VACUOUS_ASSERTION — the absence is the contract; the arm above schedules exactly one record through the same door
        # CONTROL for the arm above, on the same observable: the same door,
        # a tree without the runner, and no record at all.
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01",
                                   detach=self.detach())
        self.assertEqual(rc, 0, text)
        self.assertEqual(gateshadow.records(self.global_dir), [])
        self.assertNotIn("shadow:", text)

    def test_the_serial_gate_declares_land_authority_to_fab(self):
        room = self.room("train01", self.sliceable)
        rc, text, _req = self.door(room, label="train01",
                                   detach=self.detach())
        self.assertEqual(rc, 0, text)
        self.assertIn(gatewindow.LAND_AUTHORITY_OPTION, self.measures[0])
        self.assertIn(gatewindow.LAND_AUTHORITY_OPTION, self.spawns[0])
        self.assertNotIn(gatewindow.SHADOW_OPTION, self.spawns[0])

    def test_a_fab_without_land_authority_admission_starts_nothing(self):  # noqa: VACUOUS_ASSERTION — rc and the named missing option are asserted before both dispatch and shadow records are asserted absent
        event = json.loads(measured("snoozy"))
        event["submit_options"].remove(gatewindow.LAND_AUTHORITY_OPTION)
        room = self.room("train01", self.sliceable)
        rc, text, _req = self.door(
            room, label="train01",
            fab=self.fab(measure=(0, json.dumps(event) + "\n", "")),
            detach=self.detach())
        self.assertEqual(rc, 2, text)
        self.assertIn(gatewindow.LAND_AUTHORITY_OPTION, text)
        self.assertEqual(self.spawns, [])
        self.assertEqual(gateshadow.records(self.global_dir), [])

    def test_a_shadow_fault_never_fails_the_launch(self):
        room = self.room("train01", self.sliceable)
        with mock.patch.object(gateshadow, "schedule",
                               side_effect=OSError("disk full")):
            rc, text, _req = self.door(room, label="train01",
                                       detach=self.detach())
        self.assertEqual(rc, 0, text)
        self.assertIn("shadow schedule skipped", text)
        self.assertEqual(len(self.spawns), 1)


class RunnerBase(CanaryFixture):
    """A SCHEDULED shadow of the fixture repository's tree, and the seams a
    pass runs through."""

    def setUp(self):
        super().setUp()
        self.gd = home.global_dir()
        self.calls, self.kills = [], []
        self.now = [1000.0]
        self.shadow_state = "COMPLETED"
        self.cancel_results = [0]
        self.live = []
        self.staged = ""

    def plant(self, serial, label="train01"):
        tree = self._git("rev-parse", "HEAD^{tree}")
        row = dict(handle("serial-" + label), room=self.repo, ts=self.now[0],
                   label=label, project="p", head=self.head, tree=tree,
                   trunk=self.head)
        self.serial_id = serial["id"] if serial else "0123456789abcdef"
        with mock.patch.object(gateshadow, "sliceable", return_value=None):
            return gateshadow.schedule(row, self.gd)

    def stage(self, blocks=()):
        """Mint the sliced receipt a node would bring home, in a staging
        home, and keep its ledger bytes for the import to place."""
        staging = os.path.join(self.tmp, "staging")
        with mock.patch.dict(os.environ, {"HELM_HOME": staging}):
            row = self.sliced_row(blocks)
            with open(gate.receipts_path()) as fh:
                self.staged = fh.read()
        self.sliced_id = row["id"]
        return row

    def fab(self, argv, timeout=None, env=None):
        self.calls.append((list(argv), dict(env or {})))
        head = argv[:3]
        if head == ["fab", "gate", "measure"]:
            scope = json.loads(argv[argv.index("--scope-json") + 1])
            answer = json.loads(measured("snoozy"))
            answer["runner"]["argv"] += fabgate.gateimport.fab_scope_gate_args(
                scope)
            return 0, json.dumps(answer) + "\n", ""
        if head == ["fab", "gate", "submit"]:
            key = json.loads(argv[argv.index("--request-json") + 1])["key"]
            self.shadow = {"key": key, "job_id": "gate-" + key,
                           "host": "snoozy", "generation": generation(key)}
            return 0, job_event(key, generation(key), "snoozy",
                                live=True), ""
        if head == ["fab", "gate", "--import"]:
            target = os.path.join((env or os.environ)["HELM_HOME"],
                                  home.GLOBAL, gate.RECEIPTS)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "a") as fh:
                fh.write(self.staged)
            return 0, "", ""
        if head == ["fab", "gate", "kill"]:
            self.kills.append(list(argv))
            return self.cancel_results.pop(0), "", ""
        raise AssertionError("the shadow ran an unexpected fab command: %r"
                             % (argv,))

    def observe(self, argv, timeout=None):
        job = argv[argv.index("--job") + 1]
        gen = argv[argv.index("--generation") + 1]
        host = argv[argv.index("--host") + 1]
        key = job[len("gate-"):]
        serial = job == handle("serial-train01")["job_id"]
        state = "COMPLETED" if serial else self.shadow_state
        tree = self._git("rev-parse", "HEAD^{tree}")
        if state == "RUNNING":
            return 0, observed_event(key, gen, host, tree, live=True), ""
        return 0, observed_event(
            key, gen, host, tree, disposition="RECEIPT", state=state,
            live=False, exit_code=143 if state == "CANCELED" else 0,
            receipt=self.serial_id if serial else self.sliced_id), ""

    def sleep(self, seconds):
        self.now[0] += seconds

    def run_pass(self, **over):
        seams = dict(fab=self.fab, observe=self.observe,
                     peek=lambda root, sha: (0, {"path": self.repo,
                                                 "sha": sha}),
                     drop=lambda root, path: (0, []),
                     live_land=lambda: list(self.live),
                     sleep=self.sleep, clock=lambda: self.now[0],
                     out=open(os.devnull, "w"))
        seams.update(over)
        self.addCleanup(seams["out"].close)
        return gateshadow._Pass(self.gd, **seams).run()

    def land_bytes(self):
        try:
            with open(gateshadow.land_ledger(self.gd), "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return b""


class LandUntouchedTest(RunnerBase):

    def test_a_whole_shadow_pass_leaves_every_land_answer_unchanged(self):  # noqa: VACUOUS_ASSERTION — the verdict is asserted EQUAL to AGREE and the sliced id PRESENT in the shadow ledger before the ledger is asserted unchanged
        serial = self.serial_row()
        self.plant(serial)
        self.stage()
        before = self.land_bytes()
        line = gate.evidence_line(serial)
        bound = gate.bind(line, self.head, need=gate.NEED_LAND)
        last = gate.tree_suite_receipt(self.repo)
        done = self.run_pass()
        self.assertEqual([r["verdict"] for r in done], [gateshadow.AGREE],
                         done)
        # THE LAND LEDGER IS BYTE FOR BYTE WHAT IT WAS, the tree's last
        # whole-suite receipt is still the serial one, and the land bind
        # answers exactly as before the shadow ran.
        self.assertEqual(self.land_bytes(), before)
        self.assertEqual(gate.tree_suite_receipt(self.repo), last)
        self.assertEqual(last[0]["id"], serial["id"])
        self.assertEqual(gate.bind(line, self.head, need=gate.NEED_LAND),
                         bound)
        self.assertEqual(gatewindow.ledger_heads()[0], {self.head})
        # CONTROL on the same pass: the sliced receipt DID come home — into
        # the shadow's own ledger, which no land door reads.
        rows, _unavailable = eventledger.checked_events(
            gateshadow.shadow_ledger(self.gd), strict=True)
        self.assertIn(self.sliced_id, [r.get("id") for r in rows])
        submit, imported = [env for argv, env in self.calls
                            if argv[2] in ("submit", "--import")]
        self.assertEqual({submit["HELM_HOME"], imported["HELM_HOME"]},
                         {gateshadow.shadow_home(self.gd)})
        self.assertEqual(
            {submit["HELM_ADOPTED_DIR"], imported["HELM_ADOPTED_DIR"]},
            {gateshadow.shadow_adopted_dir(self.gd)})
        submitted = next(argv for argv, _env in self.calls
                         if argv[2] == "submit")
        self.assertIn(gatewindow.SHADOW_OPTION, submitted)
        self.assertNotIn(gatewindow.LAND_AUTHORITY_OPTION, submitted)


class CompareTest(RunnerBase):

    def judge(self, serial, sliced):
        rows, _unavailable = eventledger.checked_events(gate.receipts_path(),
                                                        strict=True)
        return gateshadow.compare_rows(serial, rows, sliced, rows, self.repo)

    def test_identical_outcomes_AGREE(self):
        result = self.judge(self.serial_row([B_FAILS]),
                            self.sliced_row([B_FAILS]))
        self.assertEqual(result["verdict"], gateshadow.AGREE, result)
        self.assertEqual(result["differing"], [])
        self.assertTrue(result["red"])
        self.assertEqual(result["outcomes"]["serial"],
                         {B_FAILS[1]: "fail"})
        evidence = result["slice_evidence"]
        self.assertEqual((evidence["mode"], evidence["each_module_once"]),
                         ("sliced-shadow", True))

    def test_a_test_failing_only_in_the_sliced_run_DISAGREES_by_name(self):
        result = self.judge(self.serial_row(), self.sliced_row([B_FAILS]))
        self.assertEqual(result["verdict"], gateshadow.DISAGREE, result)
        self.assertEqual(result["differing"], [B_FAILS[1]])
        self.assertEqual(result["outcomes"]["sliced"], {B_FAILS[1]: "fail"})
        self.assertFalse(result["red"])

    def test_a_different_Ran_count_DISAGREES(self):
        result = self.judge(self.serial_row(ran=3), self.sliced_row())
        self.assertEqual(result["verdict"], gateshadow.DISAGREE, result)
        self.assertEqual(result["differing"], [])
        self.assertEqual(result["differing_counts"], ["<ran count>"])

    def test_slice_evidence_that_does_not_re_derive_never_AGREES(self):  # noqa: VACUOUS_ASSERTION — the verdict is asserted EQUAL to UNKNOWN; test_identical_outcomes_AGREE is the control with a repository
        result = gateshadow.compare_rows(
            self.serial_row(), [], self.sliced_row(), [], repo=None)
        self.assertEqual(result["verdict"], gateshadow.UNKNOWN, result)
        self.assertFalse(result["slice_evidence"]["each_module_once"])


def rid(label):
    return hashlib.sha256(("receipt-" + label).encode()).hexdigest()[:16]


def done(verdict, red=False, label="t"):
    return {"v": gateshadow.RECORD_VERSION, "project": "p",
            "state": gateshadow.DONE, "verdict": verdict, "red": red,
            "label": label, "serial_job": handle(label),
            "serial": {"id": rid(label)}, "tree": "f" * 40,
            "reason": "planted", "differing": ["tests.x.C.t"],
            "differing_counts": []}


def train(label, project="p", attributed=True):
    """One train as `land_trains` reads it: its receipt, and the Fab
    completion naming the generation its serial job ran under."""
    return {"id": rid(label), "label": label, "tree": "f" * 40,
            "status": "OK", "completions": [(project, generation(label))]
            if attributed else []}


def agreeing(n, red_at=0, start=1):
    return [done(gateshadow.AGREE, red=i == red_at, label="train%d" % i)
            for i in range(start, start + n)]


class StreakTest(unittest.TestCase):
    """The clock walks the LAND LEDGER's trains; a record is looked up per
    train, so a train with no readable finished record stops it."""

    def setUp(self):
        self.gd = tempfile.mkdtemp(prefix="helm-test-shadow-")
        self.addCleanup(shutil.rmtree, self.gd, ignore_errors=True)

    def clock(self, recs, trains=None):
        trains = [train(r["label"]) for r in recs] if trains is None \
            else trains
        return gateshadow.streak(recs, trains, self.gd)

    def test_ten_agreeing_trains_with_a_caught_RED_meet_the_criterion(self):  # noqa: VACUOUS_ASSERTION — the clock tuple is asserted EQUAL to (10, True, [the red train], None)
        clock = self.clock(agreeing(10, red_at=3))
        self.assertEqual((clock["streak"], clock["met"], clock["red"],
                          clock["stopped_by"]),
                         (10, True, ["train3"], None))

    def test_ten_agreeing_trains_with_no_RED_do_not(self):  # noqa: VACUOUS_ASSERTION — the clock tuple is asserted EQUAL to (12, False); the arm above is the met control
        clock = self.clock(agreeing(12, red_at=None))
        self.assertEqual((clock["streak"], clock["met"]), (12, False))

    def test_a_train_with_NO_shadow_record_stops_the_clock_by_name(self):
        """The defect's own shape: ten AGREE records, one of them RED, and
        between them in the land ledger a train nobody measured."""
        recs = agreeing(11, red_at=2)
        missing = recs.pop(5)
        trains = [train("train%d" % i) for i in range(1, 12)]
        clock = self.clock(recs, trains)
        self.assertEqual((clock["streak"], clock["met"]), (5, False))
        stopped = clock["stopped_by"]
        self.assertEqual((stopped["train"], stopped["verdict"]),
                         (missing["label"], gateshadow.UNKNOWN))
        self.assertIn("no shadow record for %s (serial receipt %s)"
                      % (missing["label"], rid(missing["label"])),
                      stopped["reason"])
        # CONTROL: the same land sequence with that record present is met.
        whole = self.clock(agreeing(11, red_at=2), trains)
        self.assertEqual((whole["streak"], whole["met"], whole["stopped_by"]),
                         (11, True, None))

    def test_a_malformed_or_unreadable_record_stops_the_clock_by_name(self):  # noqa: VACUOUS_ASSERTION — the loop is over a literal three-case tuple, and each case asserts the record count, the clock tuple and the stopper EQUAL
        """The record file stands at the train's generation, but records()
        cannot read a record out of it."""
        for why, text in (
                ("unreadable", "{not json"),
                ("malformed", json.dumps({"v": 99, "state": "DONE"})),
                ("malformed", json.dumps({"v": gateshadow.RECORD_VERSION,
                                          "serial_job": {}}))):
            with self.subTest(text=text):
                shutil.rmtree(gateshadow.records_dir(self.gd),
                              ignore_errors=True)
                for rec in agreeing(11, red_at=2):
                    gateshadow._save(rec, self.gd)
                bad = gateshadow.record_path(generation("train6"), self.gd)
                with open(bad, "w") as fh:
                    fh.write(text)
                recs = gateshadow.records(self.gd)
                self.assertEqual(len(recs), 10)
                clock = self.clock(recs, [train("train%d" % i)
                                          for i in range(1, 12)])
                self.assertEqual((clock["streak"], clock["met"]), (5, False))
                stopped = clock["stopped_by"]
                self.assertEqual((stopped["train"], stopped["verdict"]),
                                 ("train6", gateshadow.UNKNOWN))
                self.assertIn("no readable shadow record for train6 (serial "
                              "receipt %s): its record file %s is %s"
                              % (rid("train6"), bad, why), stopped["reason"])

    def test_trains_before_the_first_shadowed_train_do_not_stop_the_clock(self):  # noqa: VACUOUS_ASSERTION — each clock tuple is asserted EQUAL, the met arm with its red train named
        before = [train("train%d" % i) for i in range(1, 6)] + [
            train("train-legacy", attributed=False)]
        recs = agreeing(10, red_at=17, start=10)
        clock = self.clock(recs, before + [train(r["label"]) for r in recs])
        self.assertEqual((clock["streak"], clock["met"], clock["red"],
                          clock["stopped_by"]),
                         (10, True, ["train17"], None))
        # A clock that has not run ten trains yet is not stopped either.
        short = self.clock(recs[:3], before + [train(r["label"])
                                               for r in recs[:3]])
        self.assertEqual((short["streak"], short["met"], short["stopped_by"]),
                         (3, False, None))

    def test_another_projects_trains_are_not_this_ones_and_an_unowned_one_is(self):
        recs = agreeing(10, red_at=3)
        trains = [train(r["label"]) for r in recs]
        theirs = trains[:5] + [train("train-q", project="q")] + trains[5:]
        clock = self.clock(recs, theirs)
        self.assertEqual((clock["streak"], clock["met"], clock["stopped_by"]),
                         (10, True, None))
        # A train no completion attributes to anyone may be this project's,
        # so it stays in the sequence and, unmeasured, stops the clock.
        unowned = trains[:5] + [train("train-x", attributed=False)] \
            + trains[5:]
        stopped = self.clock(recs, unowned)["stopped_by"]
        self.assertEqual((stopped["train"], stopped["verdict"]),
                         ("train-x", gateshadow.UNKNOWN))

    def test_an_AGREE_of_another_receipt_does_not_count_for_this_train(self):
        recs = agreeing(10, red_at=3)
        recs[-1] = dict(recs[-1], serial={"id": "0" * 16})
        clock = self.clock(recs)
        self.assertEqual((clock["streak"], clock["met"]), (0, False))
        self.assertIn("compared serial receipt %s" % ("0" * 16),
                      clock["stopped_by"]["reason"])

    def test_an_unreadable_land_ledger_stops_the_clock(self):  # noqa: VACUOUS_ASSERTION — the clock tuple is asserted EQUAL to (0, False) and the stopper names the ledger's reason
        clock = gateshadow.streak(agreeing(10, red_at=3), None, self.gd,
                                  unavailable="the land ledger is unreadable")
        self.assertEqual((clock["streak"], clock["met"]), (0, False))
        self.assertEqual(clock["stopped_by"]["reason"],
                         "the land ledger is unreadable")

    def test_a_disagreement_resets_the_clock_and_is_named(self):
        recs = agreeing(10, red_at=1) + [
            done(gateshadow.DISAGREE, label="train11")] + agreeing(
                3, red_at=None, start=12)
        clock = self.clock(recs)
        self.assertEqual((clock["streak"], clock["met"]), (3, False))
        self.assertEqual(clock["last_disagreement"]["train"], "train11")
        self.assertEqual(clock["last_disagreement"]["differing"],
                         ["tests.x.C.t"])
        # CONTROL: the same ten without the disagreement after them are met.
        self.assertTrue(self.clock(recs[:10])["met"])

    def test_an_UNKNOWN_between_agreeing_trains_resets_the_clock(self):  # noqa: VACUOUS_ASSERTION — the clock tuple is asserted EQUAL to (5, False) and the stopper EQUAL to the UNKNOWN train with its cause
        recs = agreeing(5, red_at=1) + [
            dict(done(gateshadow.UNKNOWN, label="train6"),
                 reason="timed out: the sliced job was not over")] + agreeing(
                     5, red_at=None, start=7)
        clock = self.clock(recs)
        self.assertEqual((clock["streak"], clock["met"]), (5, False))
        self.assertEqual(
            (clock["stopped_by"]["train"], clock["stopped_by"]["verdict"],
             clock["stopped_by"]["reason"]),
            ("train6", gateshadow.UNKNOWN,
             "timed out: the sliced job was not over"))
        # CONTROL: the same eleven with the UNKNOWN an AGREE are ten and met.
        agreed = recs[:5] + [done(gateshadow.AGREE, label="train6")] \
            + recs[6:]
        self.assertEqual((self.clock(agreed)["streak"],
                          self.clock(agreed)["met"]), (11, True))

    def test_an_unfinished_newer_train_stops_the_clock(self):
        recs = agreeing(10, red_at=1) + [{
            "v": gateshadow.RECORD_VERSION, "project": "p",
            "state": gateshadow.MARKING, "verdict": gateshadow.DISAGREE,
            "label": "train11", "serial_job": handle("train11"),
            "serial": {"id": rid("train11")},
            "tree": "e" * 40, "reason": "marker write pending"}]
        clock = self.clock(recs)
        self.assertEqual((clock["streak"], clock["met"]), (0, False))
        self.assertEqual((clock["stopped_by"]["train"],
                          clock["stopped_by"]["verdict"]),
                         ("train11", gateshadow.UNKNOWN))

    def test_the_surface_prints_the_clock_it_walked(self):
        import io
        out = io.StringIO()
        recs = [dict(done(gateshadow.DISAGREE, label="train7")),
                dict(done(gateshadow.AGREE, label="train8")),
                dict(done(gateshadow.UNKNOWN, label="train9"),
                     reason="yielded to the land gate train10")]
        with mock.patch.object(gateshadow, "records", return_value=recs), \
                mock.patch.object(gateshadow, "land_trains", return_value=(
                    [train(r["label"]) for r in recs], None)):
            self.assertEqual(gateshadow.status(self.gd, out=out), 0)
        text = out.getvalue()
        self.assertIn("streak: 0 consecutive train(s) AGREE", text)
        self.assertIn("NOT MET", text)
        self.assertIn("stopped by: train9 UNKNOWN tree ffffffffffff — "
                      "yielded to the land gate train10", text)
        self.assertIn("last disagreement: train7 DISAGREE", text)
        self.assertIn("tests.x.C.t", text)


class LandSequenceTest(prov.ProvenanceBase):
    """The clock's trains read from the REAL ledgers: receipts placed by the
    Fab completion door (fabgate.reconcile), each joined to its shadow record
    by the generation its completion names."""

    def setUp(self):
        super().setUp()
        self.builder = fabfix.FakeBuilder()
        self.gd = home.global_dir()
        self.project = gatewindow.project_id(self.repo)

    def gate(self, label):
        """One whole-suite Fab job of a new commit, imported home. -> (the
        receipt, the job's handle)."""
        n = self.builder.launches
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("commit %d\n" % n)
        prov._git(self.repo, "commit", "-qam", "commit %d" % n)
        self.head = prov._git(self.repo, "rev-parse", "HEAD")
        self.tree = prov._git(self.repo, "rev-parse", "HEAD^{tree}")
        job, err = fabgate.request(self.repo, self.tree, "whole",
                                   dict(prov.INTERPRETER), dict(prov.RUNNER),
                                   queue_timeout=600, execution_timeout=3600)
        self.assertIsNone(err, err)
        admitted, err = fabgate.submit(self.builder, job)
        self.assertIsNone(err, err)
        handle = admitted["handle"]
        row = self.receipt(label=label, host={
            "node": handle["host"], "system": "Linux", "release": "1",
            "id": "ab" * 8})
        self.builder.complete(admitted["request"], self.artifact(row),
                              receipt=row["id"])
        result, err = fabgate.reconcile(self.builder, admitted["request"],
                                        handle, self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(result["receipt"], row["id"])
        return row, handle

    def measured(self, row, handle, label):
        """The shadow record the window would have scheduled for this job,
        finished AGREE on exactly this receipt."""
        with mock.patch.object(gateshadow, "sliceable", return_value=None):
            rec = gateshadow.schedule(dict(
                handle, tree=row["tree"], room=self.repo, ts=0.0,
                label=label, project=self.project, head=row["head"],
                trunk=row["head"]), self.gd)
        rec.update(state=gateshadow.DONE, verdict=gateshadow.AGREE, red=False,
                   serial={"id": row["id"]}, reason="planted")
        gateshadow._save(rec, self.gd)

    def status(self):
        import io
        out = io.StringIO()
        self.assertEqual(gateshadow.status(self.gd, as_json=True, out=out), 0)
        return json.loads(out.getvalue())["projects"][self.project]

    def test_the_clock_walks_the_land_ledgers_trains_by_their_generation(self):
        self.gate(None)
        self.gate("lane-suite: not a train")
        gated = [self.gate("train0%d" % n) for n in (1, 2, 3)]
        trains, why = gateshadow.land_trains(self.gd)
        self.assertIsNone(why, why)
        # ONLY THE TRAINS, in the order they came home, each carrying the
        # project and generation its Fab completion names.
        self.assertEqual(
            [(t["label"], t["id"], t["completions"]) for t in trains],
            [("train0%d" % n, row["id"], [(self.project, h["generation"])])
             for n, (row, h) in zip((1, 2, 3), gated)])
        for n in (1, 3):
            self.measured(gated[n - 1][0], gated[n - 1][1], "train0%d" % n)
        clock = self.status()
        self.assertEqual((clock["streak"], clock["stopped_by"]["train"],
                          clock["stopped_by"]["verdict"]),
                         (1, "train02", gateshadow.UNKNOWN))
        self.assertIn("no shadow record for train02 (serial receipt %s)"
                      % gated[1][0]["id"], clock["stopped_by"]["reason"])
        # CONTROL: the middle train measured too, and nothing stops it.
        self.measured(gated[1][0], gated[1][1], "train02")
        clock = self.status()
        self.assertEqual((clock["streak"], clock["stopped_by"]), (3, None))


class MarkerTest(RunnerBase):
    """A DISAGREE writes the canary's sliced-at-land DISABLE marker, the one
    `gate.sliced_land_disabled()` reads; an AGREE writes nothing."""

    def test_a_DISAGREE_writes_the_canarys_marker(self):
        serial = self.serial_row()
        self.plant(serial)
        sliced = self.stage([B_FAILS])
        self.assertIsNone(gate.sliced_land_disabled(self.gd))
        finished = self.run_pass()
        self.assertEqual([r["verdict"] for r in finished],
                         [gateshadow.DISAGREE])
        why = gate.sliced_land_disabled(self.gd)
        self.assertIsNotNone(why)
        with open(gate.sliced_land_marker_path(self.gd)) as fh:
            marker = json.load(fh)
        self.assertEqual((marker["serial"], marker["sliced"], marker["tree"]),
                         (serial["id"], sliced["id"], serial["tree"]))
        self.assertEqual([d["test"] for d in marker["divergences"]],
                         [B_FAILS[1]])
        self.assertIn("the shadow of train01 disagreed", marker["reason"])
        self.assertEqual(finished[0]["marker"],
                         gate.sliced_land_marker_path(self.gd))

    def test_an_AGREE_writes_no_marker(self):  # noqa: VACUOUS_ASSERTION — the verdict is asserted EQUAL to AGREE; the DISAGREE arm above is the marker's positive control on the same path
        serial = self.serial_row([B_FAILS])
        self.plant(serial)
        self.stage([B_FAILS])
        finished = self.run_pass()
        self.assertEqual([r["verdict"] for r in finished], [gateshadow.AGREE])
        self.assertTrue(finished[0]["red"])
        self.assertIsNone(gate.sliced_land_disabled(self.gd))
        self.assertNotIn("marker", finished[0])

    def test_a_failed_marker_write_stays_retryable_with_the_disagreement(self):  # noqa: VACUOUS_ASSERTION — the durable MARKING/DISAGREE record and differing test are positive controls before marker absence, then the retry writes it
        serial = self.serial_row()
        self.plant(serial)
        self.stage([B_FAILS])
        with mock.patch.object(gatecanary, "write_marker",
                               side_effect=OSError("disk full")):
            self.assertEqual(self.run_pass(), [])
        pending = gateshadow.records(self.gd)[0]
        self.assertEqual((pending["state"], pending["verdict"]),
                         (gateshadow.MARKING, gateshadow.DISAGREE))
        self.assertIn(B_FAILS[1], pending["differing"])
        self.assertIsNone(gate.sliced_land_disabled(self.gd))
        calls = list(self.calls)
        finished = self.run_pass()
        self.assertEqual([r["verdict"] for r in finished],
                         [gateshadow.DISAGREE])
        self.assertEqual(self.calls, calls, "marker retry reran the shadow job")
        self.assertIsNotNone(gate.sliced_land_disabled(self.gd))


    def test_a_failed_marker_write_still_closes_sliced_land_by_the_record(self):  # noqa: VACUOUS_ASSERTION — the DIVERGED's source is asserted EQUAL to shadow and the record's verdicts EQUAL to one DIVERGED
        """The DISAGREE reaches the canary record when it is measured, so a
        marker that cannot be written leaves the land door closed by the
        record; the finished shadow does not record it a second time."""
        serial = self.serial_row()
        self.plant(serial)
        self.stage([B_FAILS])
        with mock.patch.object(gatecanary, "write_marker",
                               side_effect=OSError("disk full")):
            self.assertEqual(self.run_pass(), [])
        self.assertIsNone(gate.sliced_land_disabled(self.gd))  # no marker yet
        diverged = gatecanary.standing(self.gd)["last_diverged"]
        self.assertEqual((diverged or {}).get("source"), gatecanary.SHADOW)
        finished = self.run_pass()                  # the marker path heals
        self.assertEqual([r["verdict"] for r in finished],
                         [gateshadow.DISAGREE])
        rows = eventledger.checked_events(gatecanary.history_path(self.gd),
                                          strict=True)[0]
        self.assertEqual([r["verdict"] for r in rows
                          if r.get("event") == gatecanary.VERDICT_EVENT],
                         [gatecanary.DIVERGED])

    def test_no_durable_veto_from_a_shadow_is_said_loudly(self):  # noqa: VACUOUS_ASSERTION — the loud post count is asserted EQUAL to one
        serial = self.serial_row()
        self.plant(serial)
        self.stage([B_FAILS])
        err = io.StringIO()
        with mock.patch.object(gatecanary, "write_marker",
                               side_effect=OSError("disk full")), \
                mock.patch.object(gatecanary, "append_verdict",
                                  return_value=False), \
                mock.patch.object(gatecanary.chat, "post",
                                  return_value=True) as post, \
                contextlib.redirect_stderr(err):
            self.assertEqual(self.run_pass(), [])
        self.assertIn("NO DURABLE VETO", err.getvalue())
        self.assertEqual(len([c for c in post.call_args_list
                              if "NO DURABLE VETO" in c.args[0]]), 1,
                         post.call_args_list)


class FlakeTest(RunnerBase):
    """A sliced-only miss on a test recorded FLAKE for this tree is UNKNOWN.
    The shadow writes no DISABLE marker, and the reason names the test. The
    same test recorded on another tree, or a store that cannot be read, is
    still a DISAGREE and the marker is written."""

    def _flake(self, tree, tests):
        row, err = landwindow.record_flake(self.repo, {
            "tree": tree, "gate": "ab" * 8, "train": "train01",
            "tests": list(tests), "why": "planted flake"})
        self.assertIsNone(err, err)
        return row

    def test_a_sliced_only_flake_is_unknown_and_writes_no_marker(self):  # noqa: VACUOUS_ASSERTION — the absent marker is the contract; the different-tree arm asserts the DISABLE marker PRESENT
        serial = self.serial_row()
        self.plant(serial)
        self.stage([B_FAILS])
        self._flake(serial["tree"], [B_FAILS[1]])
        finished = self.run_pass()
        self.assertEqual([r["verdict"] for r in finished],
                         [gateshadow.UNKNOWN], finished)
        self.assertIsNone(gate.sliced_land_disabled(self.gd))
        self.assertIn(B_FAILS[1], finished[0]["reason"])
        self.assertIn("flake-explained", finished[0]["reason"])

    def test_the_same_flake_on_a_different_tree_still_disagrees(self):
        serial = self.serial_row()
        self.plant(serial)
        self.stage([B_FAILS])
        self._flake("ab" * 20, [B_FAILS[1]])
        finished = self.run_pass()
        self.assertEqual([r["verdict"] for r in finished],
                         [gateshadow.DISAGREE], finished)
        why = gate.sliced_land_disabled(self.gd)
        self.assertIsNotNone(why)
        self.assertIn(serial["tree"][:12], why)

    def test_a_recorded_audit_flake_still_disagrees(self):
        serial = self.serial_row()
        self.plant(serial)
        self.stage([AUDIT])
        self._flake(serial["tree"], [AUDIT[1]])
        finished = self.run_pass()
        self.assertEqual([r["verdict"] for r in finished],
                         [gateshadow.DISAGREE], finished)
        self.assertIsNotNone(gate.sliced_land_disabled(self.gd))

    def test_an_unreadable_flake_store_disagrees(self):
        serial = self.serial_row()
        self.plant(serial)
        self.stage([B_FAILS])
        path = landwindow.flakes_path(self.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json\n")
        finished = self.run_pass()
        self.assertEqual([r["verdict"] for r in finished],
                         [gateshadow.DISAGREE], finished)
        self.assertIn(path, finished[0]["reason"])
        self.assertIsNotNone(gate.sliced_land_disabled(self.gd))


class UnknownTest(RunnerBase):

    def test_a_fab_without_shadow_admission_runs_no_shadow(self):  # noqa: VACUOUS_ASSERTION — the exact UNKNOWN verdict and missing-option reason are positive controls before submit absence
        serial = self.serial_row()
        self.plant(serial)

        def unsupported(argv, timeout=None, env=None):
            rc, text, err = self.fab(argv, timeout, env)
            if argv[:3] == ["fab", "gate", "measure"]:
                event = json.loads(text)
                event["submit_options"].remove(gatewindow.SHADOW_OPTION)
                text = json.dumps(event) + "\n"
            return rc, text, err
        finished = self.run_pass(fab=unsupported)
        self.assertEqual(finished[0]["verdict"], gateshadow.UNKNOWN)
        self.assertIn(gatewindow.SHADOW_OPTION, finished[0]["reason"])
        self.assertNotIn("submit", [argv[2] for argv, _env in self.calls])

    def test_a_submit_with_no_handle_names_what_fab_said(self):
        """fab answers the sliced submit UNKNOWN with no handle and its own
        reason for it: the shadow's UNKNOWN reason names the missing handle
        AND what fab said, so the cause is read, not guessed."""
        self.plant(self.serial_row())
        why = "helm's plan answer carries no slice field"

        def no_handle(argv, timeout=None, env=None):
            if argv[:3] == ["fab", "gate", "submit"]:
                return 94, json.dumps({
                    "v": 2, "event": "gate-job", "disposition": "UNKNOWN",
                    "reason": why, "snapshot": {"state": "UNKNOWN"}}) + "\n", ""
            return self.fab(argv, timeout, env)
        finished = self.run_pass(fab=no_handle)
        self.assertEqual([r["verdict"] for r in finished], [gateshadow.UNKNOWN])
        self.assertIn("no exact v2 durable job handle; fab said: " + why,
                      finished[0]["reason"])

    def test_a_crash_is_UNKNOWN_never_AGREE_and_touches_no_land_receipt(self):  # noqa: VACUOUS_ASSERTION — the verdicts are asserted EQUAL to [UNKNOWN] and the reason names the crash
        serial = self.serial_row()
        self.plant(serial)
        self.stage()
        before = self.land_bytes()

        def crash(argv, timeout=None, env=None):
            if argv[2] == "submit":
                raise RuntimeError("fab vanished")
            return self.fab(argv, timeout, env)
        finished = self.run_pass(fab=crash)
        self.assertEqual([r["verdict"] for r in finished],
                         [gateshadow.UNKNOWN])
        self.assertIn("crashed: RuntimeError: fab vanished",
                      finished[0]["reason"])
        self.assertEqual(self.land_bytes(), before)
        clock = gateshadow.streak(finished, [{
            "id": serial["id"], "label": "train01", "tree": serial["tree"],
            "status": serial["status"],
            "completions": [("p", handle("serial-train01")["generation"])]}],
            self.gd)
        self.assertEqual((clock["streak"], clock["stopped_by"]["verdict"]),
                         (0, gateshadow.UNKNOWN))

    def test_a_sliced_job_that_never_finishes_times_out_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the kills are asserted EQUAL to the one cancel of the submitted job
        serial = self.serial_row()
        self.plant(serial)
        self.stage()
        self.shadow_state = "RUNNING"
        finished = self.run_pass()
        self.assertEqual(finished[0]["verdict"], gateshadow.UNKNOWN)
        self.assertIn("timed out", finished[0]["reason"])
        self.assertEqual(self.kills, [gatewindow._kill_argv(self.shadow)])
        self.assertNotIn("--import", [argv[2] for argv, _env in self.calls])

    def test_an_unknown_cancel_stays_retryable_at_the_exact_generation(self):  # noqa: VACUOUS_ASSERTION — the persisted CANCELING state and two exact kill argv calls prove the retry path positively
        serial = self.serial_row()
        self.plant(serial)
        self.stage()
        self.shadow_state = "RUNNING"
        self.cancel_results = [None, 0]
        self.assertEqual(self.run_pass(), [])
        pending = gateshadow.records(self.gd)[0]
        self.assertEqual(pending["state"], gateshadow.CANCELING)
        finished = self.run_pass()
        self.assertEqual(finished[0]["verdict"], gateshadow.UNKNOWN)
        self.assertEqual(self.kills, [gatewindow._kill_argv(self.shadow)] * 2)

    def test_a_completion_first_seen_after_the_deadline_is_UNKNOWN(self):  # noqa: VACUOUS_ASSERTION — the UNKNOWN verdict and hard-deadline reason are positive controls before import absence
        serial = self.serial_row()
        self.plant(serial)
        self.stage()
        advanced = []

        def delayed(argv, timeout=None):
            if argv[argv.index("--job") + 1] != handle(
                    "serial-train01")["job_id"] and not advanced:
                advanced.append(1)
                self.now[0] += gateshadow.RUN_TIMEOUT_S + 1
            return self.observe(argv, timeout)
        finished = self.run_pass(observe=delayed)
        self.assertEqual(finished[0]["verdict"], gateshadow.UNKNOWN)
        self.assertIn("completion was not observed within", finished[0]["reason"])
        self.assertNotIn("--import", [argv[2] for argv, _env in self.calls])

    def test_a_land_gate_in_flight_makes_the_running_shadow_yield(self):
        serial = self.serial_row()
        self.plant(serial)
        self.stage()
        self.shadow_state = "RUNNING"
        polls = []

        def land():
            polls.append(1)
            return [{"label": "train02"}] if len(polls) > 2 else []
        finished = self.run_pass(live_land=land)
        self.assertEqual(finished[0]["verdict"], gateshadow.UNKNOWN)
        self.assertIn("yielded to the land gate train02",
                      finished[0]["reason"])
        self.assertEqual(len(self.kills), 1)

    def test_a_shadow_never_starts_while_a_land_gate_is_in_flight(self):  # noqa: VACUOUS_ASSERTION — the absence of fab calls is the contract; the record is asserted still SCHEDULED, and the pass arms start it once no gate is live
        serial = self.serial_row()
        self.plant(serial)
        self.live = [{"label": "train02"}]
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(gateshadow.records(self.gd)[0]["state"],
                         gateshadow.SCHEDULED)

    def test_a_shadow_waits_for_its_serial_receipt_to_come_home(self):  # noqa: VACUOUS_ASSERTION — the wait is the contract; the control past the pending limit asserts the UNKNOWN verdict by name
        self.plant(None)
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.calls, [])
        # CONTROL: past the pending limit the wait ends UNKNOWN, by name.
        self.now[0] += gateshadow.PENDING_LIMIT_S + 1
        finished = self.run_pass()
        self.assertEqual(finished[0]["verdict"], gateshadow.UNKNOWN)
        self.assertIn("never came home", finished[0]["reason"])


if __name__ == "__main__":
    unittest.main()
