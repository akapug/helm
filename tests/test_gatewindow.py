#!/usr/bin/env python3
"""THE ONE-SUITE-PER-LANDING-WINDOW DOOR REFUSES, AND SAYS WHAT IS OPEN.

Every arm here drives `gatewindow.launch` — the shipped door — against a REAL
temp git repository with real worktrees, so containment is answered by git and
not by a fixture's opinion. The three seams an arm has to observe are the three
this box must not actually perform: the `fab gate` dispatch, the `fab kill`,
and the node's in-flight read. Each is substituted with a spy and each arm
asserts on WHAT THE DOOR PASSED IT, never on a reconstruction of it.

EVERY ARM CARRIES ITS CONTROL ON THE SAME OBSERVABLE. A refusal arm that only
proves "nothing was dispatched" would pass against a door that dispatches
nothing ever; so each refusal arm is paired, in the same method, with the one
changed fact that makes the same door dispatch — a moved trunk head, a
retired run, a room that does contain the running head. The pair is the arm.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests._tmphome import own_env
from helm import fabgate, gatewindow, landwindow, vcs

# The routing knobs, spelled once: HELM_GATE_HOSTS is the ordered host list,
# FAB_EXCLUDE_HOSTS the exclusion Fab's own placement honours, and
# HELM_GATE_WINDOW_KEY what one gate at a time is keyed by.
HOSTS_ENV = "HELM_GATE_HOSTS"
EXCLUDE_ENV = "FAB_EXCLUDE_HOSTS"
KEY_ENV = "HELM_GATE_WINDOW_KEY"


# WHAT `fab gate measure` AND `fab gate submit` REALLY EMIT — one JSON event on
# stdout, built here from the same field sets fab's own readers require, so an
# arm that feeds these exercises the door's parse rather than a dict it handed
# itself. The interpreter is helm's exact four-field shape and the runner is
# fab's installed wrapper identity; either one malformed is a refusal, which is
# what the no-identity arm turns on.
INTERPRETER = {"name": "cpython", "version": "3.13.7", "language": "python",
               "executable": "/usr/bin/python3"}
# The wrapper version is a 64-hex digest of the runtime bundle and the cgroup is
# the immutable runtime's own regime — both exactly as `fab gate measure` emits
# them, because a fixture that invented either would arm a request Fab refuses.
RUNNER = {"format": "fab-gate-runner-v1",
          "argv": ["-m", "helm", "gate", "run", "--repo", "."],
          "wrapper_version": "b" * 64,
          "cgroup": "systemd-user-generation-cgroup-v1"}
RECEIPT_ID = "0123456789abcdef"


def measured(host):
    return json.dumps({
        "v": 2, "event": "gate-measure", "host": host,
        "interpreter": INTERPRETER, "runner": RUNNER,
        "route_preimage": "f" * 64,
        "submit_options": [gatewindow.LABEL_OPTION,
                           gatewindow.LAND_AUTHORITY_OPTION,
                           gatewindow.SHADOW_OPTION]}) + "\n"


def snapshot(state="RUNNING", exit_code=None, receipt=None, live=None):
    return {"state": state, "exit": exit_code, "exit_class": None,
            "artifact": None, "artifact_sha256": None, "receipt": receipt,
            "queue_elapsed_s": 1.0, "execution_elapsed_s": None,
            "live": live, "reason": None}


def job_event(key, generation, host, disposition="LAUNCHED", **snap):
    return json.dumps({
        "v": 2, "event": "gate-job", "disposition": disposition,
        "handle": {"v": 2, "key": key, "job_id": "gate-" + key,
                   "host": host, "generation": generation},
        "snapshot": snapshot(**snap), "reason": None,
        "request": {"v": 2, "key": key}}) + "\n"


def observed_event(key, generation, host, tree, disposition="JOINED", **snap):
    """The real `fab gate observe` shape, distinct from submit's handle event."""
    return json.dumps({
        "v": 2, "event": "gate-job", "key": key,
        "job_id": "gate-" + key, "tree": tree, "sha": "a" * 40,
        "disposition": disposition, "node": host, "generation": generation,
        "identity": {"tree": tree},
        "budgets": {"queue_s": None, "execution_s": None},
        "snapshot": snapshot(**snap), "reason": None}) + "\n"


def unknown_event():
    return json.dumps({
        "v": 2, "event": "gate-job", "disposition": "UNKNOWN",
        "handle": None, "snapshot": snapshot(state="UNKNOWN"),
        "reason": "AUTHORITY_ABSENT", "request": None}) + "\n"


def QUIET_CLIENT(argv, log):
    """A detached client that is never spawned, for arms that dispatch."""
    return type("P", (), {"pid": 4242})()


# WHAT `fab gate submit` ANSWERS WHEN IT LAUNCHED NOTHING (task/3114): the build
# stopped before the node registered anything, so fab exits 75 with the null
# authority, AUTHORITY_ABSENT, and the handle it would have named with a null
# generation — fab-gate-job's own emit_submit shape, field for field.
FAB_LAUNCHED_NOTHING = ("Fab launched nothing on node-a (exit 75, retry the "
                        "submit): RETRY: the build stopped before the node "
                        "registered the job")


def null_snapshot(code):
    """fab's `unknown_snapshot(code)`: the exact ten-field null authority."""
    return {"state": "UNKNOWN", "exit": None, "exit_class": None,
            "artifact": None, "artifact_sha256": None, "receipt": None,
            "queue_elapsed_s": None, "execution_elapsed_s": None,
            "live": False, "reason": code}


def absent_event(key, host="node-a", generation=None, code="AUTHORITY_ABSENT",
                 reason=FAB_LAUNCHED_NOTHING):
    return json.dumps({
        "v": 2, "event": "gate-job", "disposition": "UNKNOWN",
        "handle": {"v": 2, "key": key, "job_id": "gate-" + key,
                   "host": host, "generation": generation},
        "snapshot": null_snapshot(code), "reason": reason,
        "request": {"v": 2, "key": key}}) + "\n"


def no_record_event(key, host="node-a", code="AUTHORITY_ABSENT",
                    reason="canonical gate record is absent"):
    """What `fab gate observe` prints when the node holds NO by-key record:
    fab-gate-state's observe answer, passed through remote_observe."""
    return json.dumps({
        "v": 2, "event": "gate-job", "key": key, "job_id": "gate-" + key,
        "tree": None, "sha": None, "disposition": "UNKNOWN", "node": host,
        "generation": None, "identity": None,
        "budgets": {"queue_s": None, "execution_s": None},
        "snapshot": null_snapshot(code), "reason": reason}) + "\n"


# WHAT `fab status <host>` REALLY WRITES, copied from its own printf shapes.
# The unreachable notice is the load-bearing one: fab prints it on STDOUT and
# the function still exits 0, so SUCCESS and EMPTY leave fab in the same
# shape, and only the parse boundary can tell them apart.
FAB_UNREACHABLE = "snoozy: unreachable\n"
FAB_BANNER = "IN FLIGHT (priority / phase / reason):\n"
FAB_IDLE = "helm-2026-a exit=0\n\n" + FAB_BANNER
FAB_RUNNING = (FAB_IDLE + "RUNNING      priority=normal  %s (pgid 4114) "
               "— admitted; never preempted\n")


class WindowBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gatewindow-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        own_env(self, "HELM_HOME", os.path.join(self.tmp, "helm"))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.main = self.git("symbolic-ref", "--short", "HEAD")
        self.a = self.commit("a")
        self.git("branch", "side", self.a)
        self.b = self.commit("b")
        self.c = self.commit("c")
        self.git("checkout", "-q", "side")
        self.side = self.commit("side", path="g")
        self.git("checkout", "-q", self.main)
        self.path = gatewindow.runs_path(os.path.join(self.tmp, "helm",
                                                      "_global"))
        # THE ROUTING CONFIG IS THE ARM'S, never the box's: an operator's
        # FAB_EXCLUDE_HOSTS or HELM_GATE_HOSTS must not steer a fixture launch.
        for key in (HOSTS_ENV, EXCLUDE_ENV):
            own_env(self, key, "")
        # THESE ARMS ARE THE TRUNK-KEYED WINDOW'S: its same-window refusal is
        # what most of them measure, so they pin the key that keeps it. The
        # default, host, has its own arms (FastHostRouting, WindowKeyDefault).
        own_env(self, KEY_ENV, "trunk")
        self.kills = []
        self.spawns = []
        self.measures = []
        self.capacities = []
        self.observes = []
        self.gens = {}
        # FAB_EXCLUDE_HOSTS on each measure and submit, as the door passed it
        # (None: the door passed no environment of its own).
        self.exclusions = []

    def git(self, *args):
        out = subprocess.run(("git",) + args, cwd=self.repo, text=True,
                             capture_output=True, check=True)
        return out.stdout.strip()

    def commit(self, name, path="f"):
        with open(os.path.join(self.repo, path), "a") as fh:
            fh.write(name + "\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    def room(self, name, committish):
        """A detached compose room, exactly as the integrator stands one."""
        where = os.path.join(self.tmp, "rooms", name)
        os.makedirs(os.path.dirname(where), exist_ok=True)
        self.git("worktree", "add", "-q", "--detach", where, committish)
        return where

    # -- the substituted seams ---------------------------------------------

    def generation(self, run_id):
        """One run id's generation, in fab's exact shape. The map back is what
        lets an arm say `("r1",)` and still be about a keyed job."""
        digest = hashlib.sha256(run_id.encode()).hexdigest()
        gen = "run-%s-4114-%s" % (digest[:32], digest[32:48])
        self.gens[gen] = run_id
        return gen

    def fab(self, run_id="r1", host="snoozy", measure=None, unknown=False,
            answer=None):
        """The fab seam: `gate measure` then `gate submit`, answering fab's own
        bytes. Only the SUBMIT lands in self.spawns, because submit is the
        dispatch and a count of dispatches is what every refusal arm asserts.
        `answer(key)` is the whole (rc, stdout, stderr) of one submit."""
        def _fab(argv, timeout=None, env=None):
            if argv[:3] == [gatewindow.FAB_BINARY, "capacity", "--json"]:
                # THE ROUTE READS CAPACITY THROUGH THIS SEAM (task/3923); this
                # fake answers UNKNOWN, so routing keeps today's choice
                # setdefault: test_trainblame and test_landwindow reuse this
                # fake from classes whose setUp never makes the list
                self.__dict__.setdefault("capacities", []).append(list(argv))
                return 1, "", "no capacity in this fake"
            self.exclusions.append((argv[2], None if env is None
                                    else env.get(EXCLUDE_ENV)))
            if argv[:3] == [gatewindow.FAB_BINARY, "gate", "measure"]:
                self.measures.append(list(argv))
                return (0, measured(host), "") if measure is None else measure
            if argv[:3] == [gatewindow.FAB_BINARY, "gate", "submit"]:
                self.spawns.append(list(argv))
                body = json.loads(argv[argv.index("--request-json") + 1])
                if answer is not None:
                    return answer(body["key"])
                if unknown:
                    return 94, unknown_event(), ""
                return 0, job_event(body["key"], self.generation(run_id),
                                    host, live=True), ""
            raise AssertionError("the door ran an unexpected fab command: %r"
                                 % (argv,))
        return _fab

    def observe(self, live=("r1",), exit_code=0, state=None,
                receipt=RECEIPT_ID):
        """The AUTHORITY read for a typed row. `live=None` is a node that could
        not be read — UNKNOWN, which must keep a record rather than open the
        window. `receipt=None` is a finished job that named none."""
        def _observe(argv, timeout=None):
            self.observes.append(list(argv))
            if live is None:
                return 94, unknown_event(), ""
            gen = argv[argv.index("--generation") + 1]
            key = argv[argv.index("--job") + 1][len("gate-"):]
            host = argv[argv.index("--host") + 1]
            row = next((r for r in gatewindow.read_runs(self.path)
                        if r.get("generation") == gen), {})
            tree = row.get("tree") or self.git("rev-parse", "HEAD^{tree}")
            if self.gens.get(gen) in live:
                return 0, observed_event(key, gen, host, tree, live=True), ""
            return 0, observed_event(
                key, gen, host, tree, disposition="RECEIPT",
                state=state or "COMPLETED", exit_code=exit_code,
                receipt=receipt, live=False), ""
        return _observe

    def no_fab_status(self):
        """A POSITIVE CONTROL ON THE ROAD TAKEN. A keyed job has no id in
        `fab status`, so a door that asked that question about a typed row
        would be asking an authority that cannot answer it."""
        def _ask(host):
            raise AssertionError(
                "the door read `fab status %s` about a typed row" % host)
        return _ask

    def kill(self, rc=0, text="fab: killed"):
        def _kill(row):
            self.kills.append((row.get("host"),
                               self.gens.get(row.get("generation"))
                               or row.get("run_id")))
            return rc, text
        return _kill

    def inflight(self, live=("r1",)):
        """A HAND-STUBBED node answer, for arms whose subject is the decision
        and not the reading. Arms whose subject IS the reading use
        `through_fab`, which goes through the shipped boundary."""
        if live is None:
            return lambda host: None
        return lambda host: {rid: "RUNNING" for rid in live}

    def through_fab(self, text, rc=0):
        """The node seam as it really runs: fab's own bytes, through
        `node_inflight` and `parse_inflight`. THE ARM THAT WAS MISSING went
        red here — nothing fed the door what an OFFLINE node actually
        produces, so the door's unknown-keeps-the-record promise was never
        exercised against the shape that breaks it."""
        return lambda host: gatewindow.node_inflight(
            host, runner=lambda h: (rc, text))

    def door(self, room, **kw):
        out = io.StringIO()
        kw.setdefault("fab", self.fab())
        kw.setdefault("kill", self.kill())
        kw.setdefault("observe", self.observe())
        kw.setdefault("inflight", self.no_fab_status())
        kw.setdefault("trunk_ref", self.main)
        kw.setdefault("path", self.path)
        # THE LAUNCHER HAS RETURNED by the time any later read happens, so its
        # process is gone. The fake child reports THIS process's pid (the only
        # pid a test can be sure about), which is alive, so without this the
        # fixture would report every past launch as a running client and hide
        # every question about what happens after one exits. One arm overrides
        # it to hold the live-client case.
        kw.setdefault("pid_alive", lambda pid: False)
        rc, request = gatewindow.launch(room, out=out, **kw)
        return rc, out.getvalue(), request


class OneSuitePerWindow(WindowBase):

    def test_the_second_launch_on_one_trunk_head_is_refused_naming_the_first(self):
        first = self.room("train01", self.c)
        rc, text, _req = self.door(first, label="train01",
                                   fab=self.fab("r1", "snoozy"),
                                   observe=self.observe(()))
        # CONTROL, on the same observable as the refusal below: the door DID
        # dispatch, and it dispatched the real argv, not a reconstruction.
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.spawns[0][:5],
                         ["fab", "gate", "submit", "--repo",
                          os.path.realpath(first)])
        self.assertEqual(self.measures[0][:5],
                         ["fab", "gate", "measure", "--repo",
                          os.path.realpath(first)])
        # THE JOB IDENTITY IS PRINTED, and it is the one the submit carried.
        key = json.loads(self.spawns[0][
            self.spawns[0].index("--request-json") + 1])["key"]
        self.assertIn("DISPATCHED gate-%s (LAUNCHED) on snoozy" % key, text)

        second = self.room("train02", self.c)
        rc2, text2, _r2 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(("r1",)))
        self.assertEqual(rc2, 3, text2)
        self.assertIn("REFUSED", text2)
        # NAMED BY ITS TYPED IDENTITY. A keyed job's name is the job id and the
        # generation the authority issued it; no fab run id exists to print, and
        # the WAIT door is `fab gate --join`, which needs both.
        self.assertIn("running: gate-%s on snoozy" % key, text2)
        self.assertIn("train01", text2)
        self.assertIn("WAIT", text2)
        self.assertIn("SUPERSEDE", text2)
        self.assertIn("fab gate --join snoozy gate-%s --generation %s"
                      % (key, self.generation("r1")), text2)
        # and NOTHING was dispatched for it — one spawn in the whole arm.
        self.assertEqual(len(self.spawns), 1, self.spawns)

    def test_a_launch_on_a_moved_trunk_head_is_admitted(self):
        first = self.room("train01", self.c)
        rc, text, _req = self.door(first, label="train01",
                                   observe=self.observe(()))
        self.assertEqual(rc, 0, text)

        # TRUNK MOVES: the window closes and a new one opens. Same project,
        # same live run on the node, and the door admits — this is the control
        # that proves the refusal above is about the WINDOW and not about the
        # store merely being non-empty.
        moved = self.commit("d")
        second = self.room("train02", moved)
        rc2, text2, _r2 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(("r1",)))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(len(self.spawns), 2, self.spawns)
        self.assertEqual(self.spawns[1][:5],
                         ["fab", "gate", "submit", "--repo",
                          os.path.realpath(second)])

    def test_a_room_a_running_run_contains_is_refused_as_already_covered(self):
        first = self.room("train01", self.c)
        rc, text, _req = self.door(first, label="train01",
                                   observe=self.observe(()))
        self.assertEqual(rc, 0, text)

        # A room standing at `b`, which IS an ancestor of the running room's
        # `c`: the running receipt binds this tree, so a second suite measures
        # the same tree twice. Trunk has moved, so only containment can refuse.
        moved = self.commit("d")
        self.assertTrue(moved)
        behind = self.room("behind", self.b)
        rc2, text2, _r2 = self.door(behind, label="behind",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(("r1",)))
        self.assertEqual(rc2, 3, text2)
        self.assertIn("CONTAINS", text2)
        self.assertIn(self.c[:12], text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)

        # CONTROL on the same observable: a room on the SAME moved trunk whose
        # head the running run does NOT contain is dispatched.
        divergent = self.room("divergent", self.side)
        rc3, text3, _r3 = self.door(divergent, label="divergent",
                                    fab=self.fab("r3", "drowsy"),
                                    observe=self.observe(("r1",)))
        self.assertEqual(rc3, 0, text3)
        self.assertEqual(len(self.spawns), 2, self.spawns)


class JobIdentity(WindowBase):
    """THE DOOR BUYS AN IDENTITY BEFORE IT SPENDS A BUILD BOX.

    A whole suite dispatched without a gate-job identity leaves the node holding
    a receipt that no import door can bind — `fab gate reconcile` can only call
    it UNVERIFIED — so the identity is the precondition, not a nicety. It is
    knowable before anything runs, because the job id is `gate-<key>` and the
    key is a hash of the request helm builds.
    """

    def tree_of(self, room):
        out = subprocess.run(("git", "-C", room, "rev-parse", "HEAD^{tree}"),
                             text=True, capture_output=True, check=True)
        return out.stdout.strip()

    def request_body(self):
        argv = self.spawns[0]
        return json.loads(argv[argv.index("--request-json") + 1])

    def test_a_launch_with_no_job_identity_refuses_before_dispatching(self):
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01", fab=self.fab(
            measure=(1, "", "fab-gate-job: no eligible node answered\n")))
        self.assertEqual(rc, 2, text)
        self.assertIn("no gate-job identity", text)
        self.assertIn("NOTHING was dispatched", text)
        self.assertEqual(self.spawns, [],
                         "a whole suite was dispatched with no job identity")
        self.assertEqual(gatewindow.read_runs(self.path), [],
                         "an unidentified dispatch was recorded")

        # CONTROL on the same observable: with the bytes fab really emits, the
        # same room dispatches exactly ONCE, through submit, carrying the exact
        # measured identity — and the record names the job.
        rc2, text2, _r2 = self.door(room, label="train01")
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)
        body = self.request_body()
        self.assertEqual(body["identity"]["tree"], self.tree_of(room))
        self.assertEqual(body["identity"]["interpreter"], INTERPRETER)
        self.assertEqual(body["identity"]["runner"], RUNNER)
        row = gatewindow.read_runs(self.path)[0]
        self.assertEqual(row["job_id"], "gate-" + body["key"])
        self.assertEqual(row["generation"], self.generation("r1"))
        self.assertEqual(row["host"], "snoozy")
        self.assertEqual(row["tree"], self.tree_of(room))

    def test_a_measurement_helm_cannot_turn_into_a_request_refuses(self):
        """THE REFUSAL IS HELM'S OWN, not merely a missing fab answer: fab
        answered, and `fabgate.request` rejected an interpreter whose
        executable is not absolute."""
        relative = json.dumps({
            "v": 2, "event": "gate-measure", "host": "snoozy",
            "interpreter": dict(INTERPRETER, executable="python3"),
            "runner": RUNNER, "route_preimage": "f" * 64,
            "submit_options": [gatewindow.LAND_AUTHORITY_OPTION]}) + "\n"
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01",
                                   fab=self.fab(measure=(0, relative, "")))
        self.assertEqual(rc, 2, text)
        self.assertIn("interpreter identity", text)
        self.assertEqual(self.spawns, [])
        # CONTROL: the same measurement with an absolute executable dispatches.
        rc2, text2, _r2 = self.door(room, label="train01")
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)


class FlakeAttempt(WindowBase):
    """A recorded FLAKE earns the next gate attempt (task/3298).

    Fab keys a whole-suite job by its identity, so relaunching an unchanged
    tree returns the same red receipt. N FLAKED rows for this tree put
    attempt N+1 on that identity and change the key. No row for this tree
    leaves the field off, byte-identical to a request built without it.
    """

    def tree_of(self, room):
        out = subprocess.run(("git", "-C", room, "rev-parse", "HEAD^{tree}"),
                             text=True, capture_output=True, check=True)
        return out.stdout.strip()

    def _bytes(self, identity):
        return json.dumps(identity, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)

    def identity(self):
        request = {"project": "p", "room": self.repo, "head": self.c,
                   "trunk": self.c, "label": "train01"}
        caught = io.StringIO()
        with contextlib.redirect_stderr(caught):
            got, err = gatewindow.job_identity(
                request, fab=lambda argv, timeout=None, **_rest: (
                    0, measured("snoozy"), ""))
        return got, err, caught.getvalue()

    def bare(self):
        job, err = fabgate.request(
            self.repo, self.tree_of(self.repo), fabgate.whole_scope(),
            INTERPRETER, RUNNER)
        self.assertIsNone(err, err)
        return job

    def flake(self, tree, test="tests.test_b.Case.test_b"):
        _row, err = landwindow.record_flake(self.repo, {
            "tree": tree, "gate": "ab" * 8, "train": "train01",
            "tests": [test], "why": "flake"})
        self.assertIsNone(err, err)

    def test_no_flaked_row_leaves_the_identity_byte_identical(self):
        got, err, _said = self.identity()
        self.assertIsNone(err, err)
        self.assertEqual(got["mode"], "serial")
        bare = self.bare()
        self.assertEqual(self._bytes(got["request"]["identity"]),
                         self._bytes(bare["identity"]))
        self.assertEqual(got["key"], bare["key"])

    def test_one_flaked_row_for_this_tree_is_attempt_two(self):
        self.flake(self.tree_of(self.repo))
        got, err, _said = self.identity()
        self.assertIsNone(err, err)
        self.assertEqual(got["request"]["identity"]["attempt"], 2)
        argv = gatewindow.submit_argv(
            self.repo, got["request"], land_authority=True)
        sent = json.loads(argv[argv.index("--request-json") + 1])
        self.assertEqual(sent["identity"]["attempt"], 2)
        self.assertEqual(sent["key"], got["key"])
        self.assertNotEqual(got["key"], self.bare()["key"])

    def test_two_flaked_rows_are_attempt_three(self):
        tree = self.tree_of(self.repo)
        self.flake(tree)
        self.flake(tree, test="tests.test_c.Case.test_c")
        got, err, _said = self.identity()
        self.assertIsNone(err, err)
        self.assertEqual(got["request"]["identity"]["attempt"], 3)

    def test_a_flake_for_another_tree_leaves_the_identity_unchanged(self):
        self.flake("ab" * 20)
        got, err, _said = self.identity()
        self.assertIsNone(err, err)
        self.assertEqual(got["mode"], "serial")
        bare = self.bare()
        self.assertEqual(self._bytes(got["request"]["identity"]),
                         self._bytes(bare["identity"]))
        self.assertEqual(got["key"], bare["key"])

    def test_an_unreadable_flake_store_reuses_the_last_receipt(self):
        path = landwindow.flakes_path(self.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{not json\n")
        got, err, said = self.identity()
        self.assertIsNone(err, err)
        bare = self.bare()
        self.assertEqual(self._bytes(got["request"]["identity"]),
                         self._bytes(bare["identity"]))
        self.assertIn(
            "the flake store could not be read, so this relaunch reuses "
            "the last receipt", said)


class LaunchLabel(WindowBase):
    """THE LAUNCH LABEL RIDES THE JOB (task/3066). MEASURED: `--label
    train200` reached the window record and never receipt a8385c943e5d7a54,
    because nothing carried it past the request. It rides `gate submit
    --label` when the Fab that answered `gate measure` lists that option, and
    the dispatch says which of the two happened."""

    def measure_with(self, options):
        event = json.loads(measured("snoozy"))
        event["submit_options"] = [gatewindow.LAND_AUTHORITY_OPTION] + options
        return 0, json.dumps(event) + "\n", ""

    def test_a_fab_that_takes_the_label_carries_it_on_the_submit(self):
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01", fab=self.fab(
            measure=self.measure_with(["--label"])))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.spawns[0][-2:], ["--label", "train01"])
        self.assertIn("label:      train01 (carried to the node", text)
        row = gatewindow.read_runs(self.path)[0]
        self.assertEqual((row["label"], row["label_forwarded"]),
                         ("train01", True))

    def test_a_fab_that_does_not_take_it_is_told_nothing_and_says_so(self):  # noqa: VACUOUS_ASSERTION — rc 0 and the 'recorded here only' text are asserted positively; the sibling arm sees --label on the same submit argv
        """CONTROL on the same observable: no option advertised, so the
        submit Fab would refuse whole is sent unchanged, and the dispatch
        names the label that stays on the record only."""
        room = self.room("train01", self.c)
        rc, text, _req = self.door(
            room, label="train01",
            fab=self.fab(measure=self.measure_with([])))
        self.assertEqual(rc, 0, text)
        self.assertNotIn("--label", self.spawns[0])
        self.assertIn("recorded here only", text)
        self.assertFalse(gatewindow.read_runs(self.path)[0]["label_forwarded"])


class DetachedClient(WindowBase):
    """THE CLIENT OUTLIVES ITS CALLER, OR THE RECEIPT IS LOST.

    `fab gate`'s FETCH and IMPORT legs ran inside a child of the launching
    process. A backgrounded launch that hit a harness time limit, a TaskStop or
    earlyoom therefore took the only path to a minted receipt with it: the node
    ran green and nothing on the hub ever learned the receipt existed. So the
    door hands that work to a process in its OWN SESSION and returns as soon as
    the record and the dispatch are written.
    """

    def detach(self, pid=4242):
        def _detach(argv, log):
            self.detached.append((list(argv), log))
            return type("P", (), {"pid": pid})()
        return _detach

    def setUp(self):
        super().setUp()
        self.detached = []

    def test_a_launch_records_the_job_detaches_the_client_and_names_the_log(self):
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01",
                                   detach=self.detach())
        self.assertEqual(rc, 0, text)
        row = gatewindow.read_runs(self.path)[0]
        # THE CLIENT IS THE ONE THE DOOR WOULD REALLY RUN: both legs, built by
        # fabgate.commands, which is also what the refusal and the recovery
        # print. Spied as passed, never reconstructed here.
        argv, log = self.detached[0]
        cmds = fabgate.commands(gatewindow.handle_of(row), row["room"])
        self.assertEqual(argv, ["sh", "-c", "%s\n%s\n"
                                % (cmds["join"], cmds["import"])])
        self.assertEqual(log, os.path.join(os.path.dirname(self.path), "logs",
                                           row["job_id"] + ".log"))
        # and the launch printed the identity and the log, so a caller that
        # walks away can still find both.
        self.assertIn(row["job_id"], text)
        self.assertIn(row["generation"], text)
        self.assertIn(log, text)
        self.assertEqual(row["pid"], 4242)
        self.assertEqual(row["log"], log)

    def test_a_client_that_cannot_be_detached_is_said_out_loud(self):
        def refuse(argv, log):
            raise OSError("no fork")
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01", detach=refuse)
        # THE SUITE IS RUNNING AND RECORDED, so this is not a failed launch.
        self.assertEqual(rc, 0, text)
        self.assertIn("could NOT be detached", text)
        self.assertIn("show --recover", text)
        self.assertEqual(len(gatewindow.read_runs(self.path)), 1)
        # CONTROL on the same observable, on the next window: a client that CAN
        # be detached says nothing of the kind and records its pid.
        moved = self.commit("d")
        third = self.room("train03", moved)
        rc2, text2, _r2 = self.door(third, label="train03",
                                    fab=self.fab("r2", "drowsy"),
                                    detach=self.detach())
        self.assertEqual(rc2, 0, text2)
        self.assertNotIn("could NOT be detached", text2)
        self.assertEqual(self.detached[0][0][0], "sh")
        self.assertEqual(
            [r["pid"] for r in gatewindow.read_runs(self.path)], [None, 4242])

    def test_the_real_client_runs_in_its_own_session_and_binds_a_RED_gate(self):
        """THE KILLED-CALLER PROPERTY, MEASURED. A fake `fab` on PATH records
        what the shipped follower really runs, so this arm proves three things
        about the detached process at once: it is in a session of its own, so a
        signal aimed at this caller's process group cannot reach it; it runs the
        IMPORT leg even though the JOIN leg exited nonzero, which is every red
        suite; and its output lands in the log rather than in a terminal that
        may already be gone."""
        record = os.path.join(self.tmp, "fab-calls.txt")
        binary = os.path.join(self.tmp, "bin")
        os.makedirs(binary)
        fake = os.path.join(binary, "fab")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\n"
                     "printf '%s\\n' \"$*\" >> \"$FAB_RECORD\"\n"
                     "python3 -c 'import os; print(\"sid\", os.getsid(0))'"
                     " >> \"$FAB_RECORD\"\n"
                     "echo \"fab ran: $1 $2\"\n"
                     "[ \"$2\" = --join ] && exit 1\n"
                     "exit 0\n")
        os.chmod(fake, 0o755)
        own_env(self, "FAB_RECORD", record)
        own_env(self, "PATH", binary + os.pathsep + os.environ["PATH"])

        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01")
        self.assertEqual(rc, 0, text)
        row = gatewindow.read_runs(self.path)[0]
        os.waitpid(row["pid"], 0)
        with open(record) as fh:
            calls = [line.strip() for line in fh if line.strip()]
        legs = [c for c in calls if c.startswith("gate ")]
        self.assertEqual(len(legs), 2, calls)
        self.assertTrue(legs[0].startswith("gate --join %s %s"
                                          % (row["host"], row["job_id"])),
                        legs)
        self.assertTrue(legs[1].startswith("gate --import %s %s"
                                           % (row["host"], row["job_id"])),
                        legs)
        # A SESSION OF ITS OWN is the whole cure: same session would mean the
        # caller's death reaches it.
        sids = {c for c in calls if c.startswith("sid ")}
        self.assertEqual(len(sids), 1, calls)
        self.assertNotIn("sid %d" % os.getsid(0), sids,
                         "the client shares this caller's session")
        with open(row["log"]) as fh:
            written = fh.read()
        self.assertIn("fab ran: gate --join", written)
        self.assertIn("fab ran: gate --import", written)


class ShowRecovers(WindowBase):
    """A RETIRED RECORD WITH NO RECEIPT IS A MEASUREMENT WITH NOWHERE TO GO.

    A record retires when the node is DONE with it, and done is the moment its
    receipt either arrived or was lost. Counting those as dropped records reads
    like housekeeping and is the report of a loss, so the render joins each one
    against the local ledger and names which it is and how to bind it.
    """

    def ledger(self, *heads, unavailable=None):
        rows = [{"head": h, "id": RECEIPT_ID} for h in heads]
        return lambda: (rows, unavailable, 0)

    def stood(self, name="train01", at=None, run_id="r1"):
        """One run through the shipped door, its client spied away."""
        room = self.room(name, at or self.c)
        rc, text, _req = self.door(room, label=name,
                                   fab=self.fab(run_id, "snoozy"),
                                   inflight=lambda host: {},
                                   detach=lambda argv, log:
                                   type("P", (), {"pid": 4242})())
        self.assertEqual(rc, 0, text)
        return room, gatewindow.read_runs(self.path)[0]

    def shown(self, **kw):
        out = io.StringIO()
        kw.setdefault("path", self.path)
        kw.setdefault("out", out)
        rc = gatewindow.show(**kw)
        return rc, out.getvalue()

    def test_a_finished_run_this_hub_never_bound_renders_STRANDED(self):
        _room, row = self.stood()
        rc, text = self.shown(observe=self.observe(()), receipts=self.ledger())
        self.assertEqual(rc, 1, text)
        self.assertIn("STRANDED", text)
        self.assertIn(row["job_id"], text)
        self.assertIn("1 stranded", text)
        # THE EXACT DOOR, and it is the one fabgate mints — not a string this
        # arm invented.
        cmds = fabgate.commands(gatewindow.handle_of(row), row["room"])
        self.assertIn(cmds["import"], text)
        self.assertIn("authority: COMPLETED exit=0", text)

        # CONTROL on the same store and the same authority read: once the
        # ledger HAS a receipt for that head, the same record renders as it
        # always did and nothing is owed.
        rc2, text2 = self.shown(observe=self.observe(()),
                                receipts=self.ledger(row["head"]))
        self.assertEqual(rc2, 0, text2)
        self.assertNotIn("STRANDED", text2)
        self.assertIn("0 stranded", text2)

    def test_a_CANCELED_job_owes_no_receipt_and_is_not_accused(self):
        _room, row = self.stood()
        rc, text = self.shown(observe=self.observe((), state="CANCELED",
                                                  exit_code=143),
                              receipts=self.ledger())
        self.assertEqual(rc, 0, text)
        self.assertNotIn("STRANDED", text)
        # CONTROL, same record and same empty ledger: a COMPLETED job IS owed,
        # so the silence above is about cancellation and not about a reader
        # that never accuses anything.
        rc2, text2 = self.shown(observe=self.observe(()),
                                receipts=self.ledger())
        self.assertEqual(rc2, 1, text2)
        self.assertIn("STRANDED", text2)

    def test_a_job_its_node_refused_on_capacity_is_NOT_RUN_not_STRANDED(self):  # noqa: VACUOUS_ASSERTION — NOT RUN and its relaunch line are unconditional positive controls before the fixed two-row control table
        """task/1740: helm's NOT RUN exit (3) and no receipt — the node refused
        to start the suite, so nothing is owed and no import door can help.
        It is reported as NOT RUN with the relaunch, and `show` exits 0 on it.
        CONTROLS on the same record and empty ledger: a receiptless exit 1 is
        still STRANDED, and so is an exit 3 that names a receipt."""
        _room, row = self.stood()
        rc, text = self.shown(observe=self.observe((), exit_code=3,
                                                  receipt=None),
                              receipts=self.ledger())
        self.assertEqual(rc, 0, text)
        self.assertNotIn("STRANDED", text)
        self.assertIn("NOT RUN", text)
        self.assertIn("relaunch: helm gate window launch --repo %s"
                      % row["room"], text)
        self.assertIn("0 stranded, 1 NOT RUN on node capacity", text)
        for exit_code, receipt in ((1, None), (3, RECEIPT_ID)):
            with self.subTest(exit_code=exit_code, receipt=receipt):
                rc2, text2 = self.shown(
                    observe=self.observe((), exit_code=exit_code,
                                         receipt=receipt),
                    receipts=self.ledger())
                self.assertEqual(rc2, 1, text2)
                self.assertIn("STRANDED", text2)
                self.assertNotIn("NOT RUN", text2)

    def test_an_UNREADABLE_receipt_on_exit_3_stays_STRANDED(self):
        """task/1740 P3: NOT RUN is keyed on the receipt's STATE. An authority
        that names a receipt id it cannot read reports receipt None exactly as
        ABSENT does, but UNKNOWN is not "owes none": the job stays STRANDED and
        its render says receipt=UNKNOWN. The ABSENT arm above is the control."""
        _room, row = self.stood()
        rc, text = self.shown(observe=self.observe((), exit_code=3,
                                                  receipt="not-a-receipt-id"),
                              receipts=self.ledger())
        self.assertEqual(rc, 1, text)
        self.assertIn("STRANDED", text)
        self.assertIn("exit=3 receipt=UNKNOWN", text)
        self.assertNotIn("NOT RUN", text)

    def test_recover_re_reads_the_ledger_instead_of_believing_the_import(self):
        _room, row = self.stood()
        calls = []

        def ran(argv, timeout=None):
            calls.append(list(argv))
            return 0, "", ""

        # A ZERO FROM A COMMAND THAT WROTE NOTHING is indistinguishable from a
        # bind, so the ledger decides.
        rc, text = self.shown(observe=self.observe(()), recover_owed=True,
                              receipts=self.ledger(), runner=ran)
        self.assertEqual(rc, 1, text)
        self.assertIn("STILL OWED", text)
        self.assertEqual(calls, [gatewindow.import_argv(row)])
        self.assertEqual(calls[0][:3], ["fab", "gate", "--import"])

        # CONTROL on the same observable: the same exit 0 with the head now in
        # the ledger is BOUND, so the refusal above is the re-read and not a
        # reader that never binds.
        rc2, text2 = self.shown(observe=self.observe(()), recover_owed=True,
                                receipts=self.ledger(row["head"]),
                                runner=ran)
        self.assertEqual(rc2, 0, text2)
        self.assertNotIn("STILL OWED", text2)
        self.assertEqual(len(calls), 1, "a bound row was imported again")

    def test_a_record_with_no_job_identity_says_no_import_door_exists(self):
        """The shape the live store still holds: a run the plain-`fab gate`
        door dispatched. Its receipt is on the node and `fab gate reconcile`
        can only call it UNVERIFIED, so the render must not print a cure that
        cannot be run. The TYPED record beside it in the same store is the
        control: the render does offer an import door when one exists, so the
        refusal is about the missing identity."""
        _room2, typed = self.stood("train02", at=self.b)
        room = self.room("train01", self.c)
        legacy = {"project": gatewindow.project_id(room), "room": room,
                  "head": self.c, "trunk": self.c, "label": "train01",
                  "pid": None, "ts": 1.0, "token": "t0", "host": "snoozy",
                  "run_id": "r1", "announce": "SEEN"}
        gatewindow.write_runs(self.path, [legacy, typed])

        rc, text = self.shown(inflight=lambda host: {}, recover_owed=True,
                              observe=self.observe(()),
                              receipts=self.ledger(), runner=lambda *a, **k:
                              (0, "", ""))
        self.assertEqual(rc, 1, text)
        self.assertEqual(text.count("STRANDED"), 2, text)
        self.assertIn("NO GATE-JOB IDENTITY", text)
        self.assertIn("authority=UNVERIFIED", text)
        self.assertIn("fab gate reconcile --host snoozy", text)
        self.assertIn("NOT RECOVERABLE", text)
        # and the typed one beside it DOES name its import door
        self.assertIn("fab gate --import snoozy %s" % typed["job_id"], text)

    def test_an_unreadable_ledger_calls_nothing_stranded(self):
        _room, _row = self.stood()
        rc, text = self.shown(observe=self.observe(()),
                              receipts=self.ledger(unavailable="permission "
                                                   "denied"))
        self.assertEqual(rc, 0, text)
        self.assertIn("could not be read", text)
        self.assertIn("unreadable and empty are different facts", text)
        self.assertNotIn("STRANDED", text)
        # CONTROL: a ledger that reads and holds nothing DOES strand it.
        rc2, text2 = self.shown(observe=self.observe(()),
                                receipts=self.ledger())
        self.assertEqual(rc2, 1, text2)
        self.assertIn("STRANDED", text2)

    def test_an_unreadable_store_is_UNKNOWN_never_nothing_in_flight(self):  # noqa: VACUOUS_ASSERTION — each unreadable shape asserts rc 1, UNKNOWN and its own reason PRESENT in the same text; the absent-store control asserts "no whole-suite gate is in flight" PRESENT on the same `show` text and ([], None) by equality
        """task/3129: `read_runs` folds a store it cannot read to [], which
        is right for the door (an unreadable store must not wall a landing)
        and wrong for a reader: `show` printed "no whole-suite gate is in
        flight" over it. Every unreadable shape is UNKNOWN, exits nonzero and
        is left as it was, --recover included; an ABSENT store is the
        control, and says nothing is in flight."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for name, body in (("not json", "{not json"),
                           ("another version", '{"v": 99, "runs": []}'),
                           ("no runs list", '{"v": 1, "runs": {}}')):
            with self.subTest(store=name):
                with open(self.path, "w") as fh:
                    fh.write(body)
                rows, why = gatewindow.read_runs_checked(self.path)
                self.assertIsNone(rows)
                self.assertIn(self.path, why)
                self.assertEqual(gatewindow.read_runs(self.path), [],
                                 "the door's reading changed")
                for recover in (False, True):
                    rc, text = self.shown(observe=self.observe(()),
                                          receipts=self.ledger(),
                                          recover_owed=recover)
                    self.assertEqual(rc, 1, text)
                    self.assertIn("UNKNOWN", text)
                    self.assertIn(why, text)
                    self.assertNotIn("no whole-suite gate is in flight", text)
                    with open(self.path) as fh:
                        self.assertEqual(fh.read(), body)
        os.unlink(self.path)
        self.assertEqual(gatewindow.read_runs_checked(self.path), ([], None))
        rc, text = self.shown(observe=self.observe(()), receipts=self.ledger())
        self.assertEqual(rc, 0, text)
        self.assertIn("no whole-suite gate is in flight", text)


class LaunchedNothing(WindowBase):
    """FAB LAUNCHED NOTHING, SO NOTHING HOLDS THE WINDOW (task/3114).

    Measured at trunk 00988cc4296: fab's absent-authority answer — exit 75,
    disposition UNKNOWN, snapshot AUTHORITY_ABSENT, a handle whose generation
    is null — was rejected on the null generation, fab's reason was dropped,
    the job was recorded LOST and the window was held for an hour over a suite
    that never existed. Every arm drives the shipped door; the controls are the
    near-misses that must keep today's handling, because the cure may only
    fire on the whole shape.
    """

    def launched_nothing(self, rc=75, **kw):
        return self.fab(host="node-a",
                        answer=lambda key: (rc, absent_event(key, **kw), ""))

    def test_fab_launching_nothing_is_NOT_DISPATCHED_and_the_retry_is_admitted(self):
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01",
                                   fab=self.launched_nothing(),
                                   observe=self.observe(()))
        self.assertEqual(rc, 3, text)
        self.assertIn("NOT DISPATCHED", text)
        # FAB'S REASON, VERBATIM, where every other refusal prints its own.
        self.assertIn("fab said: " + FAB_LAUNCHED_NOTHING, text)
        self.assertIn("retry: helm gate window launch --repo %s --label "
                      "train01" % os.path.realpath(room), text)
        self.assertNotIn("HELD", text)
        self.assertNotIn("without exact v2 safe identities", text)
        # NO LOST ROW AND NO HOLD: the store names no run for this window.
        self.assertEqual(gatewindow.read_runs(self.path), [])
        self.assertEqual(len(self.spawns), 1, self.spawns)

        # THE RETRY, ON ITS NEXT PASS: the same room on the same window is
        # dispatched at once — nothing an hour-long hold would have refused.
        rc2, text2, _r2 = self.door(room, label="train01",
                                    fab=self.fab("r1", "node-a"),
                                    observe=self.observe(("r1",)),
                                    detach=QUIET_CLIENT)
        self.assertEqual(rc2, 0, text2)
        self.assertIn("DISPATCHED", text2)
        self.assertEqual(len(self.spawns), 2, self.spawns)
        self.assertEqual([r.get("announce")
                          for r in gatewindow.read_runs(self.path)], ["SEEN"])

    def test_fabs_own_reason_reaches_the_HELD_refusal(self):
        """The answer fab really gave when a helm too old to slice was asked
        for a sliced suite: exit 94, disposition UNKNOWN, NO handle and NO
        request, and fab's reason naming the cause. The HELD refusal names
        the missing handle AND says what fab said, so the operator reads the
        cause instead of a symptom."""
        reason = ("helm cannot run /var/tmp/helm-train305 as slices: helm's "
                  "plan answer carries no slice field (a helm that predates "
                  "`gate run --sliced`)")
        snapshot = {"artifact": None, "artifact_sha256": None,
                    "execution_elapsed_s": None, "exit": None,
                    "exit_class": None, "live": False, "queue_elapsed_s": None,
                    "reason": reason, "receipt": None, "state": "UNKNOWN"}
        event = json.dumps({"disposition": "UNKNOWN", "event": "gate-job",
                            "reason": reason, "snapshot": snapshot,
                            "v": 2}) + "\n"
        room = self.room("train01", self.c)
        rc, text, _req = self.door(
            room, label="train01",
            fab=self.fab(host="node-a", answer=lambda key: (94, event, "")),
            observe=self.observe(()))
        self.assertEqual(rc, 4, text)
        self.assertIn("UNKNOWN and the window is HELD", text)
        self.assertIn("no exact v2 durable job handle; fab said: helm cannot "
                      "run /var/tmp/helm-train305 as slices", text)

    def test_every_near_miss_of_the_shape_keeps_todays_HELD_rejection(self):  # noqa: VACUOUS_ASSERTION — a fixed four-row table; every row asserts rc 4, the HELD text and a LOST row positively, and the NOT DISPATCHED arm above is the control on the same door
        """A null generation WITHOUT AUTHORITY_ABSENT, or the absent shape on
        any exit but 75 (94 is fab's "the launch's answer is UNKNOWN": it may
        be running), is the old answer: LOST row, window HELD, rc 4."""
        near = (("null generation, TRANSIENT_AUTHORITY, exit 75",
                 dict(rc=75, code="TRANSIENT_AUTHORITY")),
                ("null generation, AUTHORITY_REFUSED, exit 75",
                 dict(rc=75, code="AUTHORITY_REFUSED")),
                ("the whole absent shape on exit 94", dict(rc=94)),
                ("the whole absent shape on exit 0", dict(rc=0)))
        room = self.room("train01", self.c)
        for name, kw in near:
            with self.subTest(name):
                gatewindow.write_runs(self.path, [])
                rc, text, _req = self.door(room, label="train01",
                                           fab=self.launched_nothing(**kw),
                                           observe=self.observe(()))
                self.assertEqual(rc, 4, text)
                self.assertIn("UNKNOWN and the window is HELD", text)
                self.assertIn("without exact v2 safe identities", text)
                self.assertNotIn("NOT DISPATCHED", text)
                self.assertEqual([r.get("announce") for r in
                                  gatewindow.read_runs(self.path)], ["LOST"])

    def test_exit_75_with_a_real_handle_is_handled_as_today(self):
        """The exit alone decides nothing. Exit 75 over a LAUNCHED event with a
        real generation dispatches as it always did; exit 75 over the absent
        snapshot with a real generation is the old UNKNOWN hold."""
        room = self.room("train01", self.c)
        gen = self.generation("r1")
        rc, text, _req = self.door(
            room, label="train01", observe=self.observe(("r1",)),
            detach=QUIET_CLIENT,
            fab=self.fab(host="node-a", answer=lambda key: (
                75, job_event(key, gen, "node-a", live=True), "")))
        self.assertEqual(rc, 0, text)
        self.assertIn("DISPATCHED gate-", text)
        self.assertNotIn("NOT DISPATCHED", text)
        self.assertEqual([r.get("generation") for r in
                          gatewindow.read_runs(self.path)], [gen])

        gatewindow.write_runs(self.path, [])
        rc2, text2, _r2 = self.door(room, label="train01",
                                    fab=self.launched_nothing(generation=gen),
                                    observe=self.observe(()))
        self.assertEqual(rc2, 4, text2)
        self.assertIn("UNKNOWN and the window is HELD", text2)
        self.assertNotIn("NOT DISPATCHED", text2)


class UnnamedRowRetires(WindowBase):
    """A ROW NOBODY CAN NAME RETIRES WHEN THE AUTHORITY SAYS NO SUCH JOB EXISTS
    (task/3115, the second half of task/3114's lane).

    A submit whose answer was UNKNOWN leaves a row with no generation and no
    pid, and on its own the window holds that row for up to an hour, whatever
    the node knows. `show --recover` asks the node that owns the job key, and
    retires the row only when the node affirmatively holds no record for that
    key. Every other answer — none, unreadable, a matching job — keeps the
    hold.
    """

    ledger = ShowRecovers.ledger
    shown = ShowRecovers.shown
    stood = ShowRecovers.stood

    def unnamed(self, name="train01"):
        """The phantom, through the shipped door: an UNKNOWN submit."""
        room = self.room(name, self.c)
        rc, text, _req = self.door(room, label="train01",
                                   fab=self.fab("r1", "node-a", unknown=True),
                                   observe=self.observe(()))
        self.assertEqual(rc, 4, text)
        (row,) = gatewindow.read_runs(self.path)
        self.assertIsNone(row["generation"])
        self.assertIsNone(row["pid"])
        return room, row

    def asked(self, answer):
        """The observe seam: every argv it was asked, and `answer(argv)`."""
        self.asks = []

        def _observe(argv, timeout=None):
            self.asks.append(list(argv))
            return answer(argv)
        return _observe

    @staticmethod
    def key_of(argv):
        return argv[argv.index("--job") + 1][len("gate-"):]

    def no_record(self, argv):
        return 94, no_record_event(self.key_of(argv)), ""

    def past_floor(self):
        """A clock past the retire floor and still inside the lost-announce
        grace, so the row is live and old enough to be put to its node."""
        return (time.time() + gatewindow.SUBMIT_TIMEOUT_S
                + gatewindow.FAB_ROLLOUT_DRAIN_S + 1)

    def recovered(self, observe, now=None):
        """`show --recover`, on a clock past the retire floor unless told."""
        return self.shown(recover_owed=True, receipts=self.ledger(),
                          observe=observe, now=now or self.past_floor)

    def retire_log(self):
        path = os.path.join(os.path.dirname(self.path), gatewindow.LOGS_SUBDIR,
                            "retired.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_the_node_holding_no_such_job_retires_the_row_and_frees_the_window(self):
        room, row = self.unnamed()
        # CONTROL on the store before the answer: the phantom is there.
        self.assertEqual([r["token"] for r in gatewindow.read_runs(self.path)],
                         [row["token"]])
        rc, text = self.recovered(self.asked(self.no_record))
        self.assertEqual(rc, 0, text)
        self.assertIn("RETIRED", text)
        self.assertIn(row["job_id"], text)
        # FAB'S EVIDENCE, in its own words, printed and logged.
        self.assertIn("canonical gate record is absent", text)
        self.assertEqual(gatewindow.read_runs(self.path), [])
        (logged,) = self.retire_log()
        self.assertEqual(logged["row"]["job_id"], row["job_id"])
        self.assertEqual(logged["fab"]["rc"], 94)
        self.assertEqual(logged["fab"]["event"]["reason"],
                         "canonical gate record is absent")
        # IT ASKED THE NODE THAT OWNS THE KEY, through fab's observe door.
        (argv,) = self.asks
        self.assertEqual(argv[:3], ["fab", "gate", "observe"])
        self.assertEqual(argv[argv.index("--host") + 1], "node-a")
        self.assertEqual(argv[argv.index("--job") + 1], row["job_id"])
        # It asks existence itself, never a stand-in generation: a row
        # nobody can name has no generation to offer.
        self.assertIn("--exists", argv)
        self.assertNotIn("--generation", argv)

        # THE WINDOW IS FREE ON THE NEXT PASS: the same room dispatches.
        rc2, text2, _r2 = self.door(room, label="train01",
                                    fab=self.fab("r1", "node-a"),
                                    observe=self.observe(("r1",)),
                                    detach=QUIET_CLIENT)
        self.assertEqual(rc2, 0, text2)
        self.assertEqual([r.get("announce") for r in
                          gatewindow.read_runs(self.path)], ["SEEN"])

    def test_an_answer_that_is_not_an_affirmative_no_keeps_the_hold(self):  # noqa: VACUOUS_ASSERTION — each subTest asserts the positive HELD line and fab's words; the retiring arm above is the control on the same row shape
        cases = (
            ("fab could not be run",
             lambda argv: (None, "", "FileNotFoundError: fab"),
             "FileNotFoundError: fab"),
            ("no event on stdout",
             lambda argv: (94, "ssh: connect to host node-a\n", ""),
             "ssh: connect to host node-a"),
            ("the node could not be reached",
             lambda argv: (94, no_record_event(
                 self.key_of(argv), code="TRANSIENT_AUTHORITY",
                 reason="gate authority is unreachable"), ""),
             "gate authority is unreachable"),
            ("the absent shape on exit 0",
             lambda argv: (0, no_record_event(self.key_of(argv)), ""),
             "canonical gate record is absent"),
            ("the absent shape for another key",
             lambda argv: (94, no_record_event("e" * 64), ""),
             "canonical gate record is absent"),
            # A fab that predates --exists refuses the flag at its argument
            # parser: exit 2 and no event, never the affirmative no.
            ("a fab that predates --exists",
             lambda argv: (2, "", "fab-gate-job observe: error: "
                                  "unrecognized arguments: --exists"),
             "unrecognized arguments: --exists"),
        )
        for n, (name, answer, words) in enumerate(cases):
            with self.subTest(name):
                gatewindow.write_runs(self.path, [])
                _room, row = self.unnamed("train%02d" % n)
                rc, text = self.recovered(self.asked(answer))
                self.assertIn("HELD", text)
                self.assertIn(words, text)
                self.assertNotIn("RETIRED", text)
                self.assertEqual([r["token"] for r in
                                  gatewindow.read_runs(self.path)],
                                 [row["token"]])
        self.assertEqual(self.retire_log(), [])

    def test_UNKNOWN_without_AUTHORITY_ABSENT_never_retires(self):  # noqa: VACUOUS_ASSERTION — a fixed two-code table, each asserting HELD, fab's code and one ask positively; AUTHORITY_ABSENT on the same row retires it after the loop
        """Keyed on the snapshot reason, never on UNKNOWN alone. fab answers a
        node it could not reach, or one that refused the read, UNKNOWN on exit
        94 with TRANSIENT_AUTHORITY or AUTHORITY_REFUSED — every other field
        exactly the absent shape — and neither says the job is absent. The node
        WAS asked each time, so the hold is the answer's and not the floor's;
        AUTHORITY_ABSENT on the same row is the control and retires it."""
        for n, code in enumerate(("TRANSIENT_AUTHORITY", "AUTHORITY_REFUSED")):
            with self.subTest(code):
                gatewindow.write_runs(self.path, [])
                _room, row = self.unnamed("unknown%02d" % n)
                rc, text = self.recovered(self.asked(lambda argv: (
                    94, no_record_event(self.key_of(argv), code=code,
                                        reason="fab said %s" % code), "")))
                self.assertIn("HELD", text)
                self.assertIn("[%s]" % code, text)
                self.assertNotIn("RETIRED", text)
                self.assertEqual(len(self.asks), 1)
                self.assertEqual([r["token"] for r in
                                  gatewindow.read_runs(self.path)],
                                 [row["token"]])
        self.assertEqual(self.retire_log(), [])
        rc, text = self.recovered(self.asked(self.no_record))
        self.assertIn("RETIRED", text)
        self.assertEqual(gatewindow.read_runs(self.path), [])
        self.assertEqual([e["row"]["token"] for e in self.retire_log()],
                         [row["token"]])

    def test_an_unreadable_launch_time_is_LIVE_and_HELD_and_never_raises(self):  # noqa: VACUOUS_ASSERTION — every case asserts the row LIVE by token, show names it and the refused launch is rc 3; the readable control retires the same row by token
        """An age nobody can read is never an expired one. A row whose `ts` is
        a string, missing, non-finite or a bool is LIVE — held, exactly as
        under the retire floor — and reading the store never raises over it.
        The same row with a readable launch time past the grace is the control
        and retires."""
        room = self.room("train01", self.c)
        row = {"project": gatewindow.project_id(room), "room": room,
               "head": self.c, "trunk": self.c, "label": "train01",
               "pid": None, "ts": "yesterday", "token": "t0",
               "host": "node-a", "run_id": None, "key": "e" * 64,
               "job_id": "gate-" + "e" * 64, "generation": None,
               "announce": "LOST"}
        later = time.time() + gatewindow.LOST_ANNOUNCE_GRACE_S + 1
        missing = dict(row)
        del missing["ts"]
        for case in (row, missing, dict(row, ts=float("inf")),
                     dict(row, ts=True)):
            with self.subTest(ts=case.get("ts", "<missing>")):
                live, retired, _unknown = gatewindow.live_runs(
                    [case], observe=self.observe(()),
                    pid_alive=lambda pid: False, now=lambda: later)
                self.assertEqual([r["token"] for r in live], ["t0"])
                self.assertEqual(retired, [])

        gatewindow.write_runs(self.path, [row])
        rc, text = self.shown(observe=self.observe(()),
                              receipts=self.ledger(), now=lambda: later)
        self.assertEqual(rc, 0, text)
        self.assertIn(row["job_id"], text)
        self.assertIn("launch time is unreadable", text)
        self.assertNotIn("no whole-suite gate is in flight", text)
        # AND IT HOLDS THE WINDOW: a launch on the same window is refused.
        rc2, text2, _r2 = self.door(self.room("train02", self.c),
                                    label="train02", fab=self.fab("r2"),
                                    observe=self.observe(()),
                                    now=lambda: later)
        self.assertEqual(rc2, 3, text2)
        self.assertEqual(self.spawns, [])

        # CONTROL on the same row and clock: a readable launch time past the
        # grace retires it, so the hold above is the unreadable time's.
        readable = dict(row, ts=later - gatewindow.LOST_ANNOUNCE_GRACE_S - 1)
        live, retired, _unknown = gatewindow.live_runs(
            [readable], observe=self.observe(()),
            pid_alive=lambda pid: False, now=lambda: later)
        self.assertEqual(live, [])
        self.assertEqual([r["token"] for r in retired], ["t0"])

    def test_a_job_the_node_does_hold_is_not_retired(self):
        _room, row = self.unnamed()
        gen = self.generation("r1")

        def held(argv):
            key = self.key_of(argv)
            return 0, json.dumps({
                "v": 2, "event": "gate-job", "key": key,
                "job_id": "gate-" + key, "tree": "a" * 40, "sha": self.c,
                "disposition": "JOINED", "node": "node-a",
                "generation": gen, "identity": {}, "budgets": {},
                "snapshot": snapshot(state="SUPERSEDED", exit_code=93,
                                     live=False),
                "reason": "requested generation is superseded by canonical "
                          "authority"}) + "\n", ""

        rc, text = self.recovered(self.asked(held))
        self.assertIn("HELD", text)
        self.assertIn(gen, text)
        self.assertNotIn("RETIRED", text)
        self.assertEqual([r["token"] for r in
                          gatewindow.read_runs(self.path)], [row["token"]])
        self.assertEqual(self.retire_log(), [])
        # CONTROL on the same row and the same log: the node's affirmative no
        # retires it, so the hold above is the node's yes and not a door that
        # never retires.
        rc2, text2 = self.recovered(self.asked(self.no_record))
        self.assertIn("RETIRED", text2)
        self.assertEqual(gatewindow.read_runs(self.path), [])
        self.assertEqual([e["row"]["token"] for e in self.retire_log()],
                         [row["token"]])

    def test_a_row_with_a_real_generation_is_never_asked_or_retired_here(self):
        """This door is for rows nobody can name. A named row's liveness is its
        own generation-bound observation, so an existence answer — even an
        affirmative no — never reaches it. The unnamed row beside it in the
        same store, under the same answer, is the control: it IS asked and
        retired."""
        _room, named = self.stood()
        unnamed = dict(named, key="e" * 64, job_id="gate-" + "e" * 64,
                       generation=None, pid=None, host="node-a",
                       token="t-unnamed", announce="LOST")
        gatewindow.write_runs(self.path, [named, unnamed])
        gens = set(self.gens)
        live = self.observe(("r1",))

        def either(argv):
            if "--exists" not in argv \
                    and argv[argv.index("--generation") + 1] in gens:
                return live(argv)
            return self.no_record(argv)

        rc, text = self.recovered(self.asked(either))
        self.assertEqual(text.count("RETIRED"), 1, text)
        self.assertIn(unnamed["job_id"], text)
        self.assertEqual([r["token"] for r in
                          gatewindow.read_runs(self.path)], [named["token"]])
        self.assertEqual([e["row"]["token"] for e in self.retire_log()],
                         ["t-unnamed"])
        existence = [a[a.index("--job") + 1] for a in self.asks
                     if "--exists" in a]
        self.assertEqual(existence, [unnamed["job_id"]])

    def test_a_row_younger_than_the_retire_floor_is_HELD_with_its_age(self):  # noqa: VACUOUS_ASSERTION — the HELD line, its age and its retirable time are positive; the same row past the floor retires below, on the same store and log
        """The race the floor closes: a submit helm's own timeout killed can
        leave fab's build child still registering the job, so for helm's
        submit timeout plus a grace the node's "no such job" is not yet
        evidence. A row meeting every other retire condition is HELD, is not
        even asked about, and says its age and when it becomes retirable. The
        same row past the floor, under the same answer, is the control."""
        _room, row = self.unnamed()
        rc, text = self.recovered(self.asked(self.no_record),
                                  now=lambda: row["ts"] + 60)
        self.assertNotIn("RETIRED", text)
        self.assertIn("HELD", text)
        self.assertIn("is 60s old", text)
        self.assertEqual(self.asks, [])
        self.assertEqual([r["token"] for r in
                          gatewindow.read_runs(self.path)], [row["token"]])
        self.assertEqual(self.retire_log(), [])
        # THE RULED FLOOR: helm's 900 s submit timeout plus fab's 300 s
        # rollout drain, built from the two named constants.
        floor = gatewindow.SUBMIT_TIMEOUT_S + gatewindow.FAB_ROLLOUT_DRAIN_S
        self.assertEqual((floor, gatewindow.RETIRE_FLOOR_S), (1200, 1200))
        self.assertIn("retirable at %s" % time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["ts"] + floor)), text)

        rc2, text2 = self.recovered(self.asked(self.no_record),
                                    now=lambda: row["ts"] + floor + 1)
        self.assertIn("RETIRED", text2)
        self.assertEqual(gatewindow.read_runs(self.path), [])
        self.assertEqual([e["row"]["token"] for e in self.retire_log()],
                         [row["token"]])

    def test_a_row_with_no_readable_launch_time_is_too_young_to_retire(self):  # noqa: VACUOUS_ASSERTION — a fixed four-value table, each asserting HELD and "unreadable" positively; the readable row after it retires under the same answer and clock
        """Fail closed: an age nobody can measure is never old enough. The
        readable row beside each case, under the same answer and clock, is the
        control and retires."""
        _room, row = self.unnamed()
        later = row["ts"] + gatewindow.SUBMIT_TIMEOUT_S \
            + gatewindow.FAB_ROLLOUT_DRAIN_S + 1
        for ts in (None, "yesterday", float("inf"), True):
            with self.subTest(ts=ts):
                out = io.StringIO()
                kept = gatewindow.clear_unnamed(
                    self.path, [dict(row, ts=ts)],
                    observe=self.asked(self.no_record), out=out,
                    now=lambda: later)
                self.assertEqual([r["token"] for r in kept], [row["token"]])
                self.assertIn("HELD", out.getvalue())
                self.assertIn("unreadable", out.getvalue())
                self.assertNotIn("RETIRED", out.getvalue())
                self.assertEqual(self.asks, [])
        self.assertEqual(self.retire_log(), [])
        out = io.StringIO()
        kept = gatewindow.clear_unnamed(self.path, [row],
                                        observe=self.asked(self.no_record),
                                        out=out, now=lambda: later)
        self.assertEqual(kept, [])
        self.assertIn("RETIRED", out.getvalue())

    def test_a_launch_time_no_float_or_calendar_holds_is_HELD_and_never_raises(self):  # noqa: VACUOUS_ASSERTION — each case asserts LIVE by token, HELD, no ask and the door's refusal positively; the readable row after them retires under the same answer and clock
        """Two numbers a finite-looking `ts` can still break the reader with.
        An int too large for a float (a 400-digit ts, which JSON reads back
        as an int) overflowed `_row_ts` and, through it, EVERY read of the
        store — the launch door's included. A finite float past the
        calendar's range (1e18, 1e300) overflowed the retirable-time render
        under `show --recover`, a verb that held such a row before the retire
        door existed. Each is held, never asked and never raises, on every
        road: the store read, the recover door and the launch door. The
        readable row is the control and retires."""
        _room, row = self.unnamed()
        later = row["ts"] + gatewindow.SUBMIT_TIMEOUT_S \
            + gatewindow.FAB_ROLLOUT_DRAIN_S + 1
        spawned = len(self.spawns)
        for n, ts in enumerate((10 ** 400, 1e18, 1e300)):
            with self.subTest(ts=ts):
                case = dict(row, ts=ts)
                live, retired, _unknown = gatewindow.live_runs(
                    [case], observe=self.observe(()),
                    pid_alive=lambda pid: False, now=lambda: later)
                self.assertEqual([r["token"] for r in live], [row["token"]])
                self.assertEqual(retired, [])
                out = io.StringIO()
                kept = gatewindow.clear_unnamed(
                    self.path, [case], observe=self.asked(self.no_record),
                    out=out, now=lambda: later)
                self.assertEqual([r["token"] for r in kept], [row["token"]])
                self.assertIn("HELD", out.getvalue())
                self.assertNotIn("RETIRED", out.getvalue())
                self.assertEqual(self.asks, [])
                # THE VERB ITSELF, over the store: it renders and holds.
                gatewindow.write_runs(self.path, [case])
                rc, text = self.recovered(self.asked(self.no_record),
                                          now=lambda: later)
                self.assertIn(row["job_id"], text)
                self.assertNotIn("RETIRED", text)
                self.assertEqual(self.asks, [])
                self.assertEqual([r["token"] for r in
                                  gatewindow.read_runs(self.path)],
                                 [row["token"]])
                # AND THE LAUNCH DOOR, whose read of the store must refuse
                # over the held row rather than raise.
                rc2, text2, _r2 = self.door(self.room("held%02d" % n, self.c),
                                            label="train02",
                                            fab=self.fab("r2", "node-a"),
                                            observe=self.observe(()),
                                            now=lambda: later)
                self.assertEqual(rc2, 3, text2)
                self.assertIn("REFUSED", text2)
                self.assertEqual(len(self.spawns), spawned)
        self.assertEqual(self.retire_log(), [])
        gatewindow.write_runs(self.path, [row])
        rc, text = self.recovered(self.asked(self.no_record),
                                  now=lambda: later)
        self.assertIn("RETIRED", text)
        self.assertEqual(gatewindow.read_runs(self.path), [])


class Supersede(WindowBase):

    def _running_at(self, committish, run_id="r1", host="snoozy",
                    label="train01"):
        """Stand a real live run at `committish` through the shipped door."""
        room = self.room("running-" + run_id, committish)
        rc, text, _req = self.door(room, label=label,
                                   fab=self.fab(run_id, host),
                                   observe=self.observe(()))
        self.assertEqual(rc, 0, text)
        self.spawns.clear()
        return room

    def test_supersede_kills_the_running_run_and_gates_the_superset_room(self):
        self._running_at(self.b)
        superset = self.room("train02", self.c)     # c contains b

        # CONTROL FIRST, on the same room and the same store: without the flag
        # this exact request is refused. The flag is the only changed fact.
        rc, text, _req = self.door(superset, label="train02",
                                   fab=self.fab("r2", "drowsy"))
        self.assertEqual(rc, 3, text)
        self.assertEqual(self.kills, [])
        self.assertEqual(self.spawns, [])

        rc2, text2, _r2 = self.door(superset, label="train02", supersede=True,
                                    fab=self.fab("r2", "drowsy"))
        self.assertEqual(rc2, 0, text2)
        self.assertIn("SUPERSEDED", text2)
        # the kill the door performed, spied as it was called
        self.assertEqual(self.kills, [("snoozy", "r1")])
        self.assertEqual(self.spawns[0][:5],
                         ["fab", "gate", "submit", "--repo",
                          os.path.realpath(superset)])
        self.assertEqual(len(self.spawns), 1, self.spawns)
        # and the killed run is out of the window: the store now holds one row
        live, _retired, unknown = gatewindow.live_runs(
            gatewindow.read_runs(self.path), observe=self.observe(("r2",)),
            pid_alive=lambda pid: False)
        self.assertEqual([self.gens[r["generation"]] for r in live], ["r2"])
        self.assertEqual(unknown, set())

    def test_supersede_refuses_a_non_superset_room_naming_the_dropped_car(self):
        self._running_at(self.side, label="sidecar")
        other = self.room("train02", self.c)        # c does NOT contain side

        rc, text, _req = self.door(other, label="train02", supersede=True,
                                   fab=self.fab("r2", "drowsy"))
        self.assertEqual(rc, 3, text)
        self.assertIn("DROP", text)
        self.assertIn(self.side[:12], text)         # the car by name
        self.assertEqual(self.kills, [], "a refused supersede killed a run")
        self.assertEqual(self.spawns, [])

        # CONTROL on the same observable: merge that car in and the same
        # supersede is allowed, kills, and dispatches.
        merged = self.room("train03", self.c)
        subprocess.run(("git", "merge", "--no-edit", "-q", self.side),
                       cwd=merged, check=True, capture_output=True)
        rc2, text2, _r2 = self.door(merged, label="train03", supersede=True,
                                    fab=self.fab("r3", "drowsy"))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(self.kills, [("snoozy", "r1")])
        self.assertEqual(len(self.spawns), 1, self.spawns)

    def test_a_kill_that_does_not_report_success_refuses_before_launching(self):
        self._running_at(self.b)
        superset = self.room("train02", self.c)

        rc, text, _req = self.door(superset, label="train02", supersede=True,
                                   kill=self.kill(rc=1, text="no such run"),
                                   fab=self.fab("r2", "drowsy"))
        self.assertEqual(rc, 3, text)
        self.assertIn("did NOT die", text)
        self.assertIn("no such run", text)
        self.assertEqual(self.spawns, [], "launched over a run that may live")

        # CONTROL: the same request with a kill that DOES report success runs.
        rc2, text2, _r2 = self.door(superset, label="train02", supersede=True,
                                    fab=self.fab("r2", "drowsy"))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)


class LivenessIsMeasured(WindowBase):

    def test_a_run_the_node_no_longer_reports_refuses_nothing(self):
        first = self.room("train01", self.c)
        rc, text, _req = self.door(first, label="train01",
                                   observe=self.observe(()))
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(gatewindow.read_runs(self.path)), 1)

        second = self.room("train02", self.c)
        # THE NODE SAYS IT IS GONE: same window, same project, admitted.
        rc2, text2, _r2 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(()))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(len(self.spawns), 2, self.spawns)
        # CONTROL on the same observable: the node still reporting it refuses.
        third = self.room("train03", self.c)
        rc3, text3, _r3 = self.door(third, label="train03",
                                    fab=self.fab("r3", "drowsy"),
                                    observe=self.observe(("r2",)))
        self.assertEqual(rc3, 3, text3)
        self.assertEqual(len(self.spawns), 2, self.spawns)

    def test_an_OFFLINE_node_is_UNKNOWN_and_keeps_the_record(self):
        """THE ARM H1 IS ABOUT, driven through the real authority boundary.

        An authority that cannot be read answers UNKNOWN, and UNKNOWN is not
        "over". Read as over, the record retires and the door admits a second
        whole suite at the exact moment it can verify least — failing OPEN on
        the one input that should make it most conservative.
        """
        first = self.room("train01", self.c)
        rc, text, _req = self.door(first, label="train01",
                                   observe=self.observe(()))
        self.assertEqual(rc, 0, text)

        second = self.room("train02", self.c)
        rc2, text2, _r2 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(None))
        self.assertEqual(rc2, 3, text2)
        self.assertIn("UNKNOWN, not gone", text2)
        self.assertIn("snoozy", text2)
        # and the record SURVIVED the read, which is the thing that was wrong
        kept = gatewindow.read_runs(self.path)
        self.assertEqual([self.gens[r["generation"]] for r in kept], ["r1"])

        # CONTROL on the same observable, through the SAME boundary: a node
        # that actually answers and reports nothing in flight retires the
        # record and admits. So the refusal above is about UNKNOWN, not about
        # the store merely being non-empty.
        rc3, text3, _r3 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(()))
        self.assertEqual(rc3, 0, text3)
        self.assertEqual(len(self.spawns), 2, self.spawns)

    def test_an_authority_that_reports_the_job_refuses_naming_that_job(self):
        first = self.room("train01", self.c)
        rc, text, _req = self.door(first, label="train01",
                                   observe=self.observe(()))
        self.assertEqual(rc, 0, text)
        running = gatewindow.read_runs(self.path)[0]
        second = self.room("train02", self.c)
        rc2, text2, _r2 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(("r1",)))
        self.assertEqual(rc2, 3, text2)
        self.assertIn(running["job_id"], text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)
        # CONTROL: the same bytes naming a DIFFERENT run retire ours.
        rc3, text3, _r3 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(("zz",)))
        self.assertEqual(rc3, 0, text3)
        self.assertEqual(len(self.spawns), 2, self.spawns)

    def test_a_submit_that_answers_UNKNOWN_HOLDS_the_window(self):
        """M4. fab decides same-key existence at its own sink, so a submit
        whose answer could not be read may still have dispatched. Dropping the
        record leaves a live run with nothing recording it, which is H1's
        failure again by another road."""
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01",
                                   fab=self.fab("r1", "snoozy",
                                                unknown=True),
                                   observe=self.observe(()))
        self.assertEqual(rc, 4, text)
        self.assertIn("UNKNOWN and the window is HELD", text)
        rows = gatewindow.read_runs(self.path)
        self.assertEqual([r.get("announce") for r in rows], ["LOST"])

        second = self.room("train02", self.c)
        rc2, text2, _r2 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(()))
        self.assertEqual(rc2, 3, text2)
        self.assertIn("could not be named", text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)

        # CONTROL on the same observable: the hold is BOUNDED. Past the grace
        # the unnameable record retires and the same request is admitted, so
        # a lost announcement cannot wall the project forever.
        import time as _t
        later = _t.time() + gatewindow.LOST_ANNOUNCE_GRACE_S + 1
        rc3, text3, _r3 = self.door(second, label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(()),
                                    now=lambda: later)
        self.assertEqual(rc3, 0, text3)
        self.assertEqual(len(self.spawns), 2, self.spawns)

    def test_a_LIVE_launcher_holds_a_LEGACY_record_with_no_grace(self):
        """The other pole of the lost-identity rule, on the shape the store
        still holds: a row the PLAIN-`fab gate` door wrote, whose announcement
        was lost. While its client runs the record needs neither an id nor the
        grace. The row is seeded because the shipped door no longer writes this
        shape — and the rows already in the live store are exactly why the
        legacy road is still read."""
        room = self.room("train01", self.c)
        rows = [{"project": gatewindow.project_id(room), "room": room,
                 "head": self.c, "trunk": self.c, "label": "train01",
                 "pid": 4114, "ts": 1.0, "token": "t0", "host": None,
                 "run_id": None, "announce": "LOST"}]
        gatewindow.write_runs(self.path, rows)
        import time as _t
        past = _t.time() + gatewindow.LOST_ANNOUNCE_GRACE_S + 1
        live, _retired, _unknown = gatewindow.live_runs(
            rows, observe=self.observe(()),
            pid_alive=lambda pid: True, now=lambda: past)
        self.assertEqual([r["room"] for r in live], [rows[0]["room"]])
        # CONTROL on the same observable and the same clock: once the client
        # is gone, the grace is what decides, and past it the record retires.
        gone, retired, _u = gatewindow.live_runs(
            rows, observe=self.observe(()),
            pid_alive=lambda pid: False, now=lambda: past)
        self.assertEqual(gone, [])
        self.assertEqual([r["room"] for r in retired], [rows[0]["room"]])

    def test_supersede_refuses_a_run_it_cannot_name(self):
        """A kill needs a generation. A submit whose answer was UNKNOWN left
        none, so --supersede must refuse instead of reaching a kill with
        None."""
        room = self.room("train01", self.c)
        rc, text, _req = self.door(room, label="train01",
                                   fab=self.fab("r1", "snoozy",
                                                unknown=True),
                                   observe=self.observe(()))
        self.assertEqual(rc, 4, text)
        second = self.room("train02", self.c)
        rc2, text2, _r2 = self.door(second, label="train02", supersede=True,
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(()))
        self.assertEqual(rc2, 3, text2)
        self.assertIn("could not be named", text2)
        self.assertEqual(self.kills, [], "a kill was attempted with no id")
        self.assertEqual(len(self.spawns), 1, self.spawns)
        # CONTROL: a run the submit DID name is supersedable.
        self.spawns.clear()
        third = self.room("train03", self.c)
        rc3, text3, _r3 = self.door(third, label="train03",
                                    fab=self.fab("r3", "snoozy"),
                                    observe=self.observe(()),
                                    now=lambda: __import__("time").time()
                                    + gatewindow.LOST_ANNOUNCE_GRACE_S + 1)
        self.assertEqual(rc3, 0, text3)
        fourth = self.room("train04", self.c)
        rc4, text4, _r4 = self.door(fourth, label="train04", supersede=True,
                                    fab=self.fab("r4", "drowsy"),
                                    observe=self.observe(("r3",)))
        self.assertEqual(rc4, 0, text4)
        self.assertEqual(self.kills, [("snoozy", "r3")])

    def test_a_dirty_room_is_refused_because_fab_would_gate_a_snapshot(self):
        """M3. `fab gate` on a dirty tree snapshots tracked AND untracked into
        a dangling commit, so the receipt binds a tree that is on no branch —
        and the window record would claim it covers a head that was never
        gated. Every containment answer after that is about the wrong tree."""
        room = self.room("train01", self.c)
        with open(os.path.join(room, "f"), "a") as fh:
            fh.write("uncommitted\n")
        rc, text, _req = self.door(room, label="train01",
                                   observe=self.observe(()))
        self.assertEqual(rc, 2, text)
        self.assertIn("DIRTY", text)
        self.assertEqual(self.spawns, [])

        # CONTROL on the same room and the same observable: commit it and the
        # identical request dispatches.
        subprocess.run(("git", "add", "-A"), cwd=room, check=True,
                       capture_output=True)
        subprocess.run(("git", "commit", "-q", "-m", "committed"), cwd=room,
                       check=True, capture_output=True)
        rc2, text2, _r2 = self.door(room, label="train01",
                                    observe=self.observe(()))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)

    def test_an_untracked_file_is_dirty_too_because_fab_snapshots_it(self):
        room = self.room("train01", self.c)
        with open(os.path.join(room, "scratch-note"), "w") as fh:
            fh.write("untracked\n")
        rc, text, _req = self.door(room, label="train01",
                                   observe=self.observe(()))
        self.assertEqual(rc, 2, text)
        self.assertIn("DIRTY", text)
        self.assertEqual(self.spawns, [])
        # CONTROL: remove it and the same room dispatches.
        os.unlink(os.path.join(room, "scratch-note"))
        rc2, text2, _r2 = self.door(room, label="train01",
                                    observe=self.observe(()))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)


class AuthorityObservation(unittest.TestCase):
    """A terminal snapshot is authority only for the exact requested job."""

    def setUp(self):
        self.key = "a" * 64
        self.generation = "run-%s-4114-%s" % ("b" * 32, "c" * 16)
        self.tree = "d" * 40
        self.row = {"key": self.key, "job_id": "gate-" + self.key,
                    "host": "snoozy", "generation": self.generation,
                    "tree": self.tree}

    def event(self, **change):
        event = json.loads(observed_event(
            self.key, self.generation, "snoozy", self.tree,
            disposition="RECEIPT", state="COMPLETED", exit_code=0,
            receipt=RECEIPT_ID, live=False))
        event.update(change)
        return json.dumps(event) + "\n"

    def state(self, text):
        return gatewindow.job_state(
            self.row, observe=lambda argv, timeout=None: (0, text, ""))

    def test_only_the_exact_authority_identity_is_accepted(self):  # noqa: VACUOUS_ASSERTION — an exact COMPLETED control is asserted before every mismatched identity is rejected
        valid = self.state(self.event())
        self.assertEqual(valid["state"], "COMPLETED")
        other_key = "e" * 64
        mismatches = {
            "key": self.event(key=other_key, job_id="gate-" + other_key),
            "job": self.event(job_id="gate-" + "f" * 64),
            "host": self.event(node="drowsy"),
            "generation": self.event(
                generation="run-%s-4114-%s" % ("1" * 32, "2" * 16)),
        }
        wrong_tree = json.loads(self.event())
        wrong_tree["tree"] = "3" * 40
        wrong_tree["identity"]["tree"] = "3" * 40
        mismatches["tree"] = json.dumps(wrong_tree) + "\n"
        for name, text in mismatches.items():
            with self.subTest(name=name):
                self.assertIsNone(self.state(text))

    def test_SUPERSEDED_requires_a_different_valid_generation(self):
        newer = "run-%s-4114-%s" % ("4" * 32, "5" * 16)
        event = json.loads(self.event())
        event.update(generation=newer, disposition="JOINED")
        event["snapshot"].update(state="SUPERSEDED", exit=93, live=False)
        self.assertEqual(self.state(json.dumps(event) + "\n")["state"],
                         "SUPERSEDED")
        event["generation"] = self.generation
        self.assertIsNone(self.state(json.dumps(event) + "\n"))


class NodeReading(unittest.TestCase):
    """`fab status` is an EXTERNAL producer; its spellings are its own."""

    STATUS = (
        "helm-2026-a exit=0\n"
        "helm-2026-b exit=1\n"
        "\n"
        "IN FLIGHT (priority / phase / reason):\n"
        "RUNNING      priority=normal  helm-2026-c (pgid 4114) — admitted; "
        "never preempted\n"
        "QUEUED-SLOT  priority=normal  helm-2026-d (pgid 4120) — host-global "
        "ordered slot queue\n"
        "SUPERSEDED   priority=normal  helm-2026-e (pgid 4130) — older "
        "same-key waiter exited itself\n")

    def test_only_the_lines_under_the_banner_are_read_as_live(self):
        live = gatewindow.parse_inflight(self.STATUS)
        self.assertEqual(live, {"helm-2026-c": "RUNNING",
                                "helm-2026-d": "QUEUED-SLOT"})
        # the completed runs above the banner carry ids too, and reading one as
        # live would wall a window forever
        self.assertNotIn("helm-2026-a", live)
        # SUPERSEDED is a run that exited itself; it holds nothing
        self.assertNotIn("helm-2026-e", live)

    def test_fabs_unreachable_notice_is_UNKNOWN_not_an_idle_node(self):
        """H1 AT THE PARSE BOUNDARY. fab writes this on STDOUT and exits 0."""
        self.assertIsNone(gatewindow.parse_inflight("snoozy: unreachable"))
        self.assertIsNone(gatewindow.node_inflight(
            "snoozy", runner=lambda host: (0, "snoozy: unreachable\n")))
        # CONTROL on the same boundary: a node that ANSWERS is a dict, so the
        # None above is a distinction and not an always-None parser.
        self.assertEqual(gatewindow.node_inflight(
            "snoozy", runner=lambda host: (0, self.STATUS)),
            {"helm-2026-c": "RUNNING", "helm-2026-d": "QUEUED-SLOT"})

    def test_an_answer_with_no_banner_is_UNKNOWN_never_empty(self):
        """The banner is the PROOF the node answered. Its absence is the shape
        every non-answer arrives in — an empty pipe, a truncated read, a
        notice line — and none of those is a node saying 'nothing runs here'.
        """
        self.assertIsNone(gatewindow.parse_inflight(""))
        self.assertIsNone(gatewindow.parse_inflight("helm-2026-a exit=0\n"))
        self.assertIsNone(gatewindow.parse_inflight(None))
        # CONTROL, two poles on the same observable: the banner ALONE is a
        # genuinely idle node — an empty dict, which is a measurement and not
        # an unknown — and the banner with rows under it is a populated one,
        # so the Nones above are a distinction the parser draws rather than
        # everything it can say.
        self.assertEqual(
            gatewindow.parse_inflight("IN FLIGHT (priority / phase / reason):"),
            {})
        self.assertEqual(gatewindow.parse_inflight(self.STATUS),
                         {"helm-2026-c": "RUNNING",
                          "helm-2026-d": "QUEUED-SLOT"})

    def test_a_node_that_exits_nonzero_is_UNKNOWN_not_empty(self):
        self.assertIsNone(gatewindow.node_inflight(
            "snoozy", runner=lambda host: (255, self.STATUS)))
        self.assertEqual(gatewindow.node_inflight(
            "snoozy", runner=lambda host: (0, self.STATUS)),
            {"helm-2026-c": "RUNNING", "helm-2026-d": "QUEUED-SLOT"})


class WindowLock(WindowBase):
    """H2. O_EXCL is only mutual exclusion if the content that identifies the
    holder exists BEFORE the name does."""

    def lockpath(self):
        where = os.path.dirname(self.path)
        os.makedirs(where, exist_ok=True)
        return os.path.join(where, gatewindow.LOCK_NAME)

    def test_a_lock_that_has_not_yet_named_its_holder_is_not_stolen(self):
        lock = self.lockpath()
        # EXACTLY the window between the O_EXCL open and the pid write: the
        # file exists and names nobody. Read as holder 0, it is broken, and
        # two launches then each believe they hold the window.
        open(lock, "w").close()
        with self.assertRaises(RuntimeError):
            with gatewindow._Lock(lock, pid_alive=lambda pid: False):
                pass
        self.assertTrue(os.path.exists(lock),
                        "an unnamed holder's lock was deleted")
        # CONTROL on the same observable, with the SAME dead-pid oracle: a
        # lock that NAMES a pid that is gone IS broken and taken, so the
        # refusal above is about an unreadable holder and not about a lock
        # that can never be acquired.
        with open(lock, "w") as fh:
            fh.write("4242\n")
        with gatewindow._Lock(lock, pid_alive=lambda pid: False):
            self.assertTrue(os.path.exists(lock))
        self.assertFalse(os.path.exists(lock))

    def test_release_never_deletes_a_lock_it_no_longer_holds(self):
        lock = self.lockpath()
        held = gatewindow._Lock(lock)
        held.__enter__()
        os.unlink(lock)                      # ours vanishes (reaper, or a steal)
        with open(lock, "w") as fh:          # and somebody else takes the name
            fh.write("4242\n")
        held.__exit__(None, None, None)
        self.assertTrue(os.path.exists(lock),
                        "release deleted a lock another holder had taken")
        with open(lock) as fh:
            self.assertEqual(fh.read().strip(), "4242")
        # CONTROL on the same observable: releasing a lock that IS still ours
        # does remove it.
        with gatewindow._Lock(lock, pid_alive=lambda pid: False):
            pass
        self.assertFalse(os.path.exists(lock))

    def test_the_holder_is_named_before_the_lock_is_visible(self):
        lock = self.lockpath()
        with gatewindow._Lock(lock):
            # POSITIVE, on the same observable the release arm reads: the lock
            # stands AND it names this process. There is no instant at which
            # it exists unnamed, which is the whole of the fix.
            self.assertTrue(os.path.exists(lock))
            with open(lock) as fh:
                named = fh.read().strip()
            self.assertEqual(named, str(os.getpid()))
        self.assertFalse(os.path.exists(lock))

    def test_a_live_holder_refuses_a_second_taker(self):
        lock = self.lockpath()
        with gatewindow._Lock(lock):
            with self.assertRaises(RuntimeError):
                with gatewindow._Lock(lock, pid_alive=lambda pid: True):
                    pass
        # CONTROL: once released, the same take succeeds.
        with gatewindow._Lock(lock, pid_alive=lambda pid: True):
            pass

    def asked_by_a_child(self):
        """What `dispatching` answers in a REAL child of this process, which
        is where `fab gate submit` asks helm its sizing question from."""
        src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
             "from helm import gatewindow; "
             "print(gatewindow.dispatching(path=sys.argv[2]))",
             src, self.path], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_the_windows_own_dispatch_is_known_by_its_lineage(self):
        """task/3463 item 14: a process asks whether the landing window's own
        dispatch is asking it, and the answer is the lock: held by this
        process or one of its ancestors. A child of the holder is inside the
        dispatch; a process beside the holder is not, whatever it runs."""
        lock = self.lockpath()
        with gatewindow._Lock(lock):
            self.assertEqual(self.asked_by_a_child(), "True")
        # CONTROL: the window free, the same child is outside any dispatch.
        self.assertEqual(self.asked_by_a_child(), "False")
        # A LIVE HOLDER THAT IS NOT AN ANCESTOR: a sibling of the asker.
        sibling = subprocess.Popen(["sleep", "30"])
        self.addCleanup(sibling.wait)
        self.addCleanup(sibling.kill)
        with open(lock, "w") as fh:
            fh.write("%d\n" % sibling.pid)
        self.assertEqual(self.asked_by_a_child(), "False")
        # AN UNREADABLE HOLDER names nobody's lineage.
        with open(lock, "w") as fh:
            fh.write("\n")
        self.assertEqual(self.asked_by_a_child(), "False")
        os.unlink(lock)


class Surface(WindowBase):

    def test_an_unknown_subverb_is_refused_by_name(self):
        self.assertEqual(gatewindow.cmd(["gaet"]), 2)
        self.assertEqual(gatewindow.cmd([]), 2)
        # CONTROL: a real subverb is not refused by the same dispatcher.
        self.assertEqual(gatewindow.cmd(["show"]), 0)

    def test_a_room_with_no_resolvable_trunk_refuses_naming_the_window(self):
        room = self.room("train01", self.c)
        rc, text, request = self.door(room, trunk_ref="refs/heads/nope")
        self.assertEqual(rc, 2, text)
        self.assertIsNone(request)
        self.assertIn("trunk head", text)
        self.assertEqual(self.spawns, [])
        # CONTROL: the same room with the real trunk ref dispatches.
        rc2, text2, _r2 = self.door(room, observe=self.observe(()))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)

    def test_every_worktree_of_one_repo_answers_one_project_identity(self):
        one, two = self.room("one", self.c), self.room("two", self.b)
        one_id, two_id = gatewindow.project_id(one), gatewindow.project_id(two)
        # THE POSITIVE OBSERVABLE, not merely an equality: each answer is a
        # real git directory on disk, so two empty strings could not agree
        # their way through this arm.
        self.assertEqual(os.path.basename(one_id), ".git")
        self.assertEqual(os.path.basename(two_id), ".git")
        self.assertEqual(one_id, two_id)
        self.assertEqual(one_id,
                         os.path.realpath(os.path.join(self.repo, ".git")))
        # CONTROL: a DIFFERENT repository answers ITS OWN git directory.
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        subprocess.run(("git", "init", "-q"), cwd=other, check=True)
        other_id = gatewindow.project_id(other)
        self.assertEqual(os.path.basename(other_id), ".git")
        self.assertEqual(other_id,
                         os.path.realpath(os.path.join(other, ".git")))


class Containment(WindowBase):
    """`blocker` and `supersede_plan` answer from the vcs seam's tri-state."""

    def test_an_unanswerable_containment_never_refuses_as_covered(self):
        request = {"project": "p", "room": "/room", "head": self.b,
                   "trunk": self.c}
        live = [{"project": "p", "head": self.c, "trunk": "other",
                 "run_id": "r1", "host": "snoozy"}]
        row, why = gatewindow.blocker(
            request, live, lambda tip, ref: vcs.UNKNOWN)
        self.assertIsNone(row)
        self.assertEqual(why, "")
        # CONTROL: the same request and the same live row, answered.
        row2, why2 = gatewindow.blocker(
            request, live, lambda tip, ref: vcs.ANCESTOR)
        self.assertIs(row2, live[0])
        self.assertEqual(why2, "contained")

    def test_supersede_refuses_an_unanswerable_containment(self):
        request = {"room": "/room", "head": self.c}
        row = {"head": self.side, "run_id": "r1", "host": "snoozy",
               "label": "sidecar", "ts": 0}
        plan, err = gatewindow.supersede_plan(
            request, row, lambda tip, ref: vcs.UNKNOWN)
        self.assertIsNone(plan)
        self.assertIn("could not answer", err)
        # CONTROL: the identical call with a measured ANCESTOR is allowed.
        plan2, err2 = gatewindow.supersede_plan(
            request, row, lambda tip, ref: vcs.ANCESTOR)
        self.assertIsNone(err2)
        self.assertEqual(plan2["run_id"], "r1")


def finished_event(host, seconds, scope, key, exit_code=0, state="COMPLETED"):
    """WHAT A FINISHED LAND GATE LEAVES IN ITS JOB LOG: Fab's terminal
    gate-job event as `fab gate --join` prints it — the node Fab placed the
    gate on, the scope, and the execution seconds Fab measured — the one
    record that names both the routing host and how long the gate took."""
    snap = snapshot(state=state, exit_code=exit_code, receipt=RECEIPT_ID,
                    live=False)
    snap.update(execution_elapsed_s=seconds,
                exit_class="OK" if exit_code == 0 else "FAILED")
    return {"v": 2, "event": "gate-job", "key": key, "job_id": "gate-" + key,
            "tree": "a" * 40, "sha": "a" * 40, "disposition": "RECEIPT",
            "node": host, "node_id": "0" * 16,
            "generation": "run-%s-4114-%s" % ("c" * 32, "d" * 16),
            "identity": {"scope": scope, "tree": "a" * 40},
            "budgets": {"queue_s": None, "execution_s": None},
            "snapshot": snap, "reason": None}


class FastHostRouting(WindowBase):
    """A LAND GATE GOES WHERE IT FINISHES FIRST, AND NEVER ONTO A BUSY HOST.

    The measured baseline: a land gate took a median 18.3 min on the fast
    build host and 52.7 min on the slow one, and Fab's placement fell through
    to the slow host whenever the fast one was at its cap. The door now routes
    each gate to the host with the lowest EXPECTED FINISH — its median green
    land gate from the window's own job logs, plus what is left of the gate
    running there — and pins that host on Fab by excluding every other.

    THE FAB SEAM HERE PLACES LIKE FAB: the first host of its OWN preference
    that FAB_EXCLUDE_HOSTS leaves. Its preference puts the SLOW host first
    wherever the arm is about the door's choice, so a gate that lands on the
    fast host was put there by the door's pin and not by Fab's taste. Hosts
    are fixture names; the config lists the slow host first, so config order
    alone would choose wrong.
    """

    FAST, SLOW = "fastbox", "slowbox"
    T0 = 1800000000.0

    def setUp(self):
        super().setUp()
        self.placed = []
        self.logged = 0

    def finished(self, host, seconds, count=3, mode="serial", exit_code=0,
                 state="COMPLETED"):
        """Plant `count` finished land gates of `host` in the window's logs,
        each newer than the last, exactly where the door writes them."""
        scope = fabgate.whole_scope() if mode == "serial" \
            else fabgate.slice_scope()
        for _n in range(count):
            self.logged += 1
            key = hashlib.sha256(("%s %d" % (host, self.logged))
                                 .encode()).hexdigest()
            log = gatewindow.log_path(self.path, "gate-" + key)
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(finished_event(
                    host, seconds, scope, key, exit_code, state)) + "\n")
                fh.write(json.dumps({"exit": exit_code, "receipt": RECEIPT_ID,
                                     "state": state,
                                     "verdict": "imported"}) + "\n")
            stamp = self.T0 - 1000000 + self.logged
            os.utime(log, (stamp, stamp))

    def measured_fast_and_slow(self, fast=1000, slow=3000):
        own_env(self, HOSTS_ENV, "%s %s" % (self.SLOW, self.FAST))
        self.finished(self.FAST, fast)
        self.finished(self.SLOW, slow)

    def placing(self, prefer, run_id="r1", down=(), obey=True):
        """Fab's placement: the first host of `prefer` that FAB_EXCLUDE_HOSTS
        leaves (every host when `obey` is False) and that is not `down`; a
        failed measure when none is left. The submit launches on the host
        the measure named."""
        def _fab(argv, timeout=None, env=None):
            if argv[:3] == [gatewindow.FAB_BINARY, "capacity", "--json"]:
                self.__dict__.setdefault("capacities", []).append(list(argv))
                return 1, "", "no capacity in this fake"   # UNKNOWN (task/3923)
            given = None if env is None else env.get(EXCLUDE_ENV)
            self.exclusions.append((argv[2], given))
            shut = set((given or "").split()) if obey else set()
            if argv[:3] == [gatewindow.FAB_BINARY, "gate", "measure"]:
                self.measures.append(list(argv))
                left = [h for h in prefer if h not in shut and h not in down]
                if not left:
                    return 1, "", ("fab: no build node admitted this gate "
                                   "(probed: %s)" % " ".join(prefer))
                self.placed.append(left[0])
                return 0, measured(left[0]), ""
            if argv[:3] == [gatewindow.FAB_BINARY, "gate", "submit"]:
                self.spawns.append(list(argv))
                body = json.loads(argv[argv.index("--request-json") + 1])
                return 0, job_event(body["key"], self.generation(run_id),
                                    self.placed[-1], live=True), ""
            raise AssertionError("the door ran an unexpected fab command: %r"
                                 % (argv,))
        return _fab

    def launch(self, room, label, at, run_id="r1", live=(), **kw):
        kw.setdefault("fab", self.placing((self.SLOW, self.FAST), run_id))
        return self.door(room, label=label, observe=self.observe(live),
                         now=lambda: at, **kw)

    def moved_room(self, name):
        return self.room(name, self.commit(name))

    def test_with_both_hosts_free_the_fast_host_is_chosen(self):
        self.measured_fast_and_slow()
        rc, text, _req = self.launch(self.room("train01", self.c), "train01",
                                     self.T0)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.placed, [self.FAST])
        self.assertEqual([r["host"] for r in gatewindow.read_runs(self.path)],
                         [self.FAST])
        # PINNED BY EXCLUSION, on both calls: the measure and the submit
        # exclude the slow host, and only it.
        self.assertEqual(self.exclusions,
                         [("measure", self.SLOW), ("submit", self.SLOW)])
        self.assertIn("ROUTED to %s" % self.FAST, text)
        self.assertIn("~16.7 min", text)    # the fast median, 1,000 s
        self.assertIn("~50.0 min", text)    # the slow median, 3,000 s

    def test_the_route_reads_fab_capacity_through_the_launch_fab_seam(self):
        """task/3923: with measured hosts, the land gate's host choice reads
        `fab capacity --json` through launch's own fab seam, never a real
        subprocess; an UNKNOWN answer keeps today's choice (the fast host)."""
        self.measured_fast_and_slow(fast=1000, slow=3000)
        rc, text, _req = self.launch(self.room("train01", self.c), "train01",
                                     self.T0)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.placed, [self.FAST])
        self.assertEqual(self.capacities,
                         [[gatewindow.FAB_BINARY, "capacity", "--json"]])
        self.assertIn("capacity UNKNOWN", text)

    def test_a_busy_fast_host_that_still_finishes_first_is_waited_for(self):
        self.measured_fast_and_slow(fast=1000, slow=3000)
        rc, text, _req = self.launch(self.room("train01", self.c), "train01",
                                     self.T0)
        self.assertEqual(rc, 0, text)
        # A NEW WINDOW 200 s LATER: the fast host has ~800 s left, so it
        # finishes this gate in ~1,800 s against the slow host's 3,000 s.
        second = self.moved_room("train02")
        rc2, text2, _r2 = self.launch(second, "train02", self.T0 + 200, "r2",
                                      live=("r1",))
        self.assertEqual(rc2, 3, text2)
        self.assertIn("REFUSED", text2)
        self.assertIn("WAIT", text2)
        self.assertIn(self.FAST, text2)
        self.assertIn("~30.0 min", text2)
        self.assertIn("helm gate window launch --repo %s"
                      % os.path.realpath(second), text2)
        self.assertEqual(len(self.measures), 1, "measured a gate it waits on")
        self.assertEqual(len(self.spawns), 1, self.spawns)
        # CONTROL, at the same moment: a slow host now measured to finish in
        # 1,500 s (< 1,800 s) takes the same gate, pinned the same way.
        self.finished(self.SLOW, 1500, count=4)
        rc3, text3, _r3 = self.launch(second, "train02", self.T0 + 200, "r2",
                                      live=("r1",))
        self.assertEqual(rc3, 0, text3)
        self.assertEqual(self.placed, [self.FAST, self.SLOW])
        self.assertEqual(self.exclusions[-2:],
                         [("measure", self.FAST), ("submit", self.FAST)])

    def test_a_fast_host_excluded_or_down_sends_the_gate_to_the_slow_host(self):
        self.measured_fast_and_slow()
        own_env(self, EXCLUDE_ENV, self.FAST)
        rc, text, _req = self.launch(
            self.room("train01", self.c), "train01", self.T0,
            fab=self.placing((self.FAST, self.SLOW)))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.placed, [self.SLOW])
        self.assertIn("excluded (%s)" % EXCLUDE_ENV, text)
        # DOWN: nothing excludes it, but Fab cannot place a gate there. The
        # door routes again without it rather than refusing the train.
        own_env(self, EXCLUDE_ENV, "")
        rc2, text2, _r2 = self.launch(
            self.moved_room("train02"), "train02", self.T0 + 60, "r2",
            fab=self.placing((self.FAST, self.SLOW), "r2", down=(self.FAST,)))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(self.placed, [self.SLOW, self.SLOW])
        self.assertEqual([e for e in self.exclusions if e[0] == "measure"],
                         [("measure", self.FAST), ("measure", self.SLOW),
                          ("measure", self.FAST)])
        self.assertIn("could not take", text2)
        # CONTROL: the same launch with the fast host up goes to it.
        rc3, text3, _r3 = self.launch(
            self.moved_room("train03"), "train03", self.T0 + 120, "r3",
            fab=self.placing((self.FAST, self.SLOW), "r3"))
        self.assertEqual(rc3, 0, text3)
        self.assertEqual(self.placed, [self.SLOW, self.SLOW, self.FAST])

    def test_a_second_gate_is_never_submitted_onto_a_busy_host(self):
        # 1,500 s on the slow host beats ~800 s left plus 1,000 s on the fast
        # one, so the door routes the second gate to the free slow host...
        self.measured_fast_and_slow(fast=1000, slow=1500)
        rc, text, _req = self.launch(self.room("train01", self.c), "train01",
                                     self.T0)
        self.assertEqual(rc, 0, text)
        second = self.moved_room("train02")
        # ...and a Fab that ignores the exclusion answers the BUSY fast host.
        # The door reads the host Fab answered and submits nothing onto it.
        rc2, text2, _r2 = self.launch(
            second, "train02", self.T0 + 200, "r2", live=("r1",),
            fab=self.placing((self.FAST, self.SLOW), "r2", obey=False))
        self.assertEqual(rc2, 3, text2)
        self.assertIn("REFUSED", text2)
        self.assertIn("ALREADY RUNNING on %s" % self.FAST, text2)
        self.assertEqual(len(self.spawns), 1, self.spawns)
        # CONTROL: a Fab that honours the exclusion places it on the free host.
        rc3, text3, _r3 = self.launch(second, "train02", self.T0 + 200, "r2",
                                      live=("r1",))
        self.assertEqual(rc3, 0, text3)
        self.assertEqual(self.placed, [self.FAST, self.FAST, self.SLOW])
        self.assertEqual(len(self.spawns), 2, self.spawns)

    def test_with_no_host_known_fab_places_it_excluding_every_busy_host(self):
        rc, text, _req = self.door(self.room("train01", self.c),
                                   label="train01", observe=self.observe(()))
        self.assertEqual(rc, 0, text)
        # CONTROL: nothing busy, nothing known — Fab's environment untouched.
        self.assertEqual(self.exclusions, [("measure", None),
                                           ("submit", None)])
        rc2, text2, _r2 = self.door(self.moved_room("train02"),
                                    label="train02",
                                    fab=self.fab("r2", "drowsy"),
                                    observe=self.observe(("r1",)))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(self.exclusions[2:], [("measure", "snoozy"),
                                               ("submit", "snoozy")])

    def test_with_no_history_the_config_order_decides(self):
        own_env(self, HOSTS_ENV, "zeta alpha")
        prefer = ("alpha", "zeta")
        rc, text, _req = self.launch(self.room("train01", self.c), "train01",
                                     self.T0, fab=self.placing(prefer))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.placed, ["zeta"])
        # zeta busy: the next free host in config order.
        rc2, text2, _r2 = self.launch(
            self.moved_room("train02"), "train02", self.T0 + 60, "r2",
            live=("r1",), fab=self.placing(prefer, "r2"))
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(self.placed, ["zeta", "alpha"])
        # both busy: nothing is stacked; it waits on the first in config order.
        rc3, text3, _r3 = self.launch(
            self.moved_room("train03"), "train03", self.T0 + 120, "r3",
            live=("r1", "r2"), fab=self.placing(prefer, "r3"))
        self.assertEqual(rc3, 3, text3)
        self.assertIn("WAIT", text3)
        self.assertIn("ALREADY RUNNING on zeta", text3)
        self.assertEqual(len(self.spawns), 2, self.spawns)

    def test_a_red_or_canceled_gate_is_not_read_as_a_fast_host(self):
        self.measured_fast_and_slow(fast=1000, slow=3000)
        self.finished(self.SLOW, 60, count=5, exit_code=1)
        self.finished(self.SLOW, 30, count=5, exit_code=1, state="CANCELED")
        rc, text, _req = self.launch(self.room("train01", self.c), "train01",
                                     self.T0)
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.placed, [self.FAST])
        # CONTROL: the same short runs GREEN are the slow host's real speed.
        self.finished(self.SLOW, 60, count=5)
        rc2, text2, _r2 = self.launch(self.moved_room("train02"), "train02",
                                      self.T0)
        self.assertEqual(rc2, 0, text2)
        self.assertEqual(self.placed, [self.FAST, self.SLOW])

    def test_the_default_key_runs_two_rooms_of_one_trunk_head_on_two_hosts(self):
        """THE DEFAULT IS THE HOST KEY (R1 of the landing refactor): one gate
        per host, and two rooms of one trunk head (a train and the same train
        less an ejected car) run at once on two hosts. `trunk` is the escape
        that keeps the one-gate-per-trunk-head refusal."""
        own_env(self, KEY_ENV, "")
        self.measured_fast_and_slow(fast=1000, slow=1500)
        rc, text, _req = self.launch(self.room("train01", self.c), "train01",
                                     self.T0)
        self.assertEqual(rc, 0, text)
        sibling = self.room("sibling", self.side)   # same trunk head, apart
        # CONTROL FIRST: under the trunk key, the escape, the sibling is the
        # same landing window and is refused as one.
        own_env(self, KEY_ENV, "trunk")
        rc2, text2, _r2 = self.launch(sibling, "sibling", self.T0 + 200, "r2",
                                      live=("r1",))
        self.assertEqual(rc2, 3, text2)
        self.assertIn("landing window", text2)
        # THE DEFAULT, with the key unset: admitted, on the other host.
        own_env(self, KEY_ENV, "")
        rc3, text3, _r3 = self.launch(sibling, "sibling", self.T0 + 200, "r2",
                                      live=("r1",))
        self.assertEqual(rc3, 0, text3)
        rows = gatewindow.read_runs(self.path)
        self.assertEqual(sorted(r["host"] for r in rows),
                         [self.FAST, self.SLOW])
        self.assertEqual({r["trunk"] for r in rows}, {self.c})
        # NEVER TWO ON ONE HOST: a third room of the same head, both busy.
        self.git("checkout", "-q", "-b", "third", self.a)
        apart = self.commit("third", path="h")
        self.git("checkout", "-q", self.main)
        rc4, text4, _r4 = self.launch(self.room("third", apart), "third",
                                      self.T0 + 200, "r3",
                                      live=("r1", "r2"))
        self.assertEqual(rc4, 3, text4)
        self.assertIn("ALREADY RUNNING on %s" % self.FAST, text4)
        self.assertEqual(len(self.spawns), 2, self.spawns)


class WindowKeyDefault(unittest.TestCase):
    """HELM_GATE_WINDOW_KEY reads `host` unless it says `trunk`."""

    def test_the_default_key_is_the_host_and_trunk_is_the_escape(self):  # noqa: VACUOUS_ASSERTION — every assertion is an exact key value, trunk and host both
        from helm import gatehost
        for environ in ({}, {KEY_ENV: ""}, {KEY_ENV: "host"},
                        {KEY_ENV: "bogus"}):
            self.assertEqual(gatehost.window_key(environ), gatehost.KEY_HOST,
                             environ)
        for raw in ("trunk", " Trunk "):
            self.assertEqual(gatehost.window_key({KEY_ENV: raw}),
                             gatehost.KEY_TRUNK, raw)


class RouteByMode(unittest.TestCase):
    """The median a route reads is the request's MODE's; while no host has a
    gate of that mode, gates of any mode rank the hosts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-gatehost-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.planted = 0

    def plant(self, host, seconds, scope, count=3):
        for _n in range(count):
            self.planted += 1
            key = hashlib.sha256(("%s %d" % (host, self.planted))
                                 .encode()).hexdigest()
            path = os.path.join(self.tmp, "gate-%s.log" % key)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(finished_event(host, seconds, scope, key))
                         + "\n")
            os.utime(path, (1000 + self.planted, 1000 + self.planted))

    def route(self, mode, busy, environ=None):
        from helm import gatehost
        return gatehost.plan(mode, busy, self.tmp, environ=environ or {},
                             now=100.0)

    def test_each_mode_reads_its_own_median(self):
        whole, sliced = fabgate.whole_scope(), fabgate.slice_scope()
        self.plant("quick", 1000, whole)
        self.plant("quick", 200, sliced)
        self.plant("sluggish", 1500, whole)
        self.plant("sluggish", 250, sliced)
        # quick runs a sliced gate launched 100 s ago: ~100 s left of 200.
        busy = [{"host": "quick", "ts": 0.0, "mode": "sliced"}]
        # SLICED: 100 + 200 on quick against 250 on sluggish -> go now.
        route = self.route("sliced", busy)
        self.assertEqual(route["host"], "sluggish")
        self.assertEqual(route["exclude"], ["quick"])
        # SERIAL, same moment: 100 + 1,000 on quick beats 1,500 -> wait.
        route2 = self.route("serial", busy)
        self.assertIsNone(route2["host"])
        self.assertEqual(route2["wait"]["host"], "quick")

    def test_no_gate_of_the_mode_yet_ranks_hosts_by_their_gates_of_any_mode(self):
        self.plant("quick", 1000, fabgate.whole_scope())
        self.plant("sluggish", 3000, fabgate.whole_scope())
        busy = [{"host": "quick", "ts": 0.0, "mode": "sliced"}]
        # No sliced gate anywhere: the serial medians still say quick is
        # three times faster, so a sliced gate waits for it.
        route = self.route("sliced", busy)
        self.assertIsNone(route["host"])
        self.assertEqual(route["wait"]["host"], "quick")
        self.assertIn("no sliced gate measured yet", "\n".join(route["lines"]))
        # CONTROL on the same observable: with quick excluded, the only
        # candidate left takes it.
        route2 = self.route("sliced", busy, {"FAB_EXCLUDE_HOSTS": "quick"})
        self.assertEqual(route2["host"], "sluggish")


if __name__ == "__main__":
    unittest.main()
