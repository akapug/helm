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
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests._tmphome import own_env
from helm import fabgate, gatewindow, vcs


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
    return json.dumps({"v": 2, "event": "gate-measure", "host": host,
                       "interpreter": INTERPRETER, "runner": RUNNER,
                       "route_preimage": "f" * 64}) + "\n"


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


def unknown_event():
    return json.dumps({
        "v": 2, "event": "gate-job", "disposition": "UNKNOWN",
        "handle": None, "snapshot": snapshot(state="UNKNOWN"),
        "reason": "AUTHORITY_ABSENT", "request": None}) + "\n"

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
        self.kills = []
        self.spawns = []
        self.measures = []
        self.observes = []
        self.gens = {}

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

    def fab(self, run_id="r1", host="snoozy", measure=None, unknown=False):
        """The fab seam: `gate measure` then `gate submit`, answering fab's own
        bytes. Only the SUBMIT lands in self.spawns, because submit is the
        dispatch and a count of dispatches is what every refusal arm asserts."""
        def _fab(argv, timeout=None):
            if argv[:3] == [gatewindow.FAB_BINARY, "gate", "measure"]:
                self.measures.append(list(argv))
                return (0, measured(host), "") if measure is None else measure
            if argv[:3] == [gatewindow.FAB_BINARY, "gate", "submit"]:
                self.spawns.append(list(argv))
                body = json.loads(argv[argv.index("--request-json") + 1])
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
            if self.gens.get(gen) in live:
                return 0, job_event(key, gen, host, disposition="JOINED",
                                    live=True), ""
            return 0, job_event(key, gen, host, disposition="RECEIPT",
                                state=state or "COMPLETED",
                                exit_code=exit_code, receipt=receipt,
                                live=False), ""
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
            "runner": RUNNER, "route_preimage": "f" * 64}) + "\n"
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


if __name__ == "__main__":
    unittest.main()
