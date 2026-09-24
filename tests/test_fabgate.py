"""Durable Fab gate submit/follower/reconciliation contract."""
import copy
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import eventledger, fabgate, gate, gateimport, pk


_ENV = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME", "GIT_DIR",
        "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")
_MISSING = object()


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", repo] + list(args),
                          capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise AssertionError("git %r: %s" % (args, proc.stderr))
    return proc.stdout.strip()


class FakeBuilder:
    """The exact task/1527 v2 boundary, including immutable completion identity."""

    def __init__(self):
        self.jobs = {}
        self.launches = 0
        self.submissions = 0
        self.dispositions = []
        self.kills = 0
        self.wait_error = None
        self.observe_error = None
        self.fetch_error = None
        self.wait_returns = False
        self.wait_event = None
        self.drop_submit_once = False
        self.mutate_submit_once = False
        self.event_extra = {}

    def _sha(self, request):
        return _git(request["identity"]["repository"]["common_dir"],
                    "rev-parse", "HEAD")

    def _event(self, job, disposition=None):
        return {"v": fabgate.EVENT_VERSION, "event": "gate-job",
                "key": job["request"]["key"],
                "job_id": job["handle"]["job_id"],
                "tree": job["request"]["identity"]["tree"],
                "sha": job["sha"],
                "disposition": disposition or (
                    "RECEIPT" if job["artifact"] else "JOINED"),
                "node": job["handle"]["host"],
                "generation": job["handle"]["generation"],
                "identity": copy.deepcopy(job["request"]["identity"]),
                "budgets": copy.deepcopy(job["request"]["budgets"]),
                "snapshot": copy.deepcopy(job["snapshot"]), "reason": None,
                **copy.deepcopy(self.event_extra)}

    def submit(self, request):
        self.submissions += 1
        key = request["key"]
        job = self.jobs.get(key)
        if job is None:
            self.launches += 1
            handle = {"v": fabgate.HANDLE_VERSION, "key": key,
                      "job_id": "gate-" + key, "host": "snoozy",
                      "generation": "run-%s-%d-%s" % (
                          "a" * 32, self.launches, "b" * 16)}
            job = {"handle": handle, "request": copy.deepcopy(request),
                   "sha": self._sha(request),
                   "snapshot": {
                       "state": "QUEUED-KEY", "exit": None,
                       "exit_class": None, "artifact": None,
                       "artifact_sha256": None, "receipt": None,
                       "queue_elapsed_s": 0.0,
                       "execution_elapsed_s": None, "live": True,
                       "reason": None},
                   "artifact": None}
            self.jobs[key] = job
            disposition = "LAUNCHED"
        else:
            disposition = "RECEIPT" if job["artifact"] else "JOINED"
        self.dispositions.append(disposition)
        response = {"v": fabgate.EVENT_VERSION, "event": "gate-job",
                    "disposition": disposition,
                    "handle": copy.deepcopy(job["handle"]),
                    "snapshot": copy.deepcopy(job["snapshot"]),
                    "reason": None,
                    "request": copy.deepcopy(job["request"])}
        if self.mutate_submit_once:
            self.mutate_submit_once = False
            request["identity"]["runner"]["mutated"] = True
            raise OSError("builder mutated request before reply loss")
        if self.drop_submit_once:
            self.drop_submit_once = False
            raise OSError("reply lost after authoritative submit")
        return response

    def observe(self, handle):
        if self.observe_error:
            raise self.observe_error
        return self._event(self.jobs[handle["key"]])

    def wait(self, _handle, _timeout):
        if self.wait_error:
            raise self.wait_error
        if self.wait_event is not None:
            event, self.wait_event = self.wait_event, None
            return copy.deepcopy(event)
        if self.wait_returns:
            return
        raise AssertionError("test follower waited without a terminal event")

    def fetch_receipt(self, handle):
        if self.fetch_error:
            raise self.fetch_error
        job = self.jobs[handle["key"]]
        fetched = {"v": fabgate.EVENT_VERSION, "event": "gate-fetch",
                   "key": job["request"]["key"],
                   "job_id": job["handle"]["job_id"],
                   "tree": job["request"]["identity"]["tree"],
                   "sha": job["sha"],
                   "generation": job["handle"]["generation"],
                   "identity": copy.deepcopy(job["request"]["identity"]),
                   "source_artifact": job.get(
                       "source_artifact", job["snapshot"].get("artifact")),
                   "artifact": job["artifact"],
                   "artifact_sha256": job.get(
                       "artifact_sha256", job["snapshot"].get("artifact_sha256")),
                   "exit": job.get("fetch_exit", job["snapshot"].get("exit")),
                   "receipt": job.get("receipt"), "error": job.get("error")}
        return fetched

    def complete(self, request, artifact, receipt=_MISSING, exit_code=0):
        job = self.jobs[request["key"]]
        job["artifact"] = artifact
        job["source_artifact"] = artifact
        digest = None
        if artifact is not None:
            with open(artifact, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        job["artifact_sha256"] = digest
        if receipt is not _MISSING:
            job["receipt"] = receipt
        job["snapshot"] = {
            "state": "CANCELED" if exit_code == 143 else "COMPLETED",
            "exit": exit_code,
            "exit_class": "OK" if exit_code == 0 else "FAILED",
            "artifact": artifact, "artifact_sha256": digest,
            "receipt": None if receipt is _MISSING else receipt,
            "queue_elapsed_s": 1.0, "execution_elapsed_s": 2.0,
            "live": False, "reason": None}

    def state(self, request, state):
        self.jobs[request["key"]]["snapshot"] = {
            "state": state, "exit": None, "exit_class": None,
            "artifact": None, "artifact_sha256": None, "receipt": None,
            "queue_elapsed_s": 1.0, "execution_elapsed_s": None,
            "live": True, "reason": None}

    def kill(self, _handle):
        self.kills += 1


class FabGateBase(unittest.TestCase):
    def setUp(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        self.tmp = tempfile.mkdtemp(prefix="helm-test-fabgate-")
        self.prior = {name: os.environ.get(name) for name in _ENV}
        for name in _ENV:
            os.environ.pop(name, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NAME"] = "fabgate-test"
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", ".")
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("one\n")
        _git(self.repo, "add", "f.txt")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "one")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        self.tree = _git(self.repo, "rev-parse", "HEAD^{tree}")
        self.interpreter = {"name": "cpython", "version": "3.13.7",
                            "language": "3.13.7",
                            "executable": "/usr/bin/python3.13"}
        self.runner = {
            "format": "fab-gate-runner-v1",
            "argv": ["-m", "helm", "gate", "run", "--repo", "."],
            "wrapper_version": "c" * 64,
            "cgroup": "systemd-user-scope-or-loud-direct"}

    def tearDown(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        for name, value in self.prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def request(self, tree=None, scope="whole", interpreter=None, runner=None,
                queue_timeout=600, execution_timeout=1800):
        if runner is None:
            runner = copy.deepcopy(self.runner)
            if isinstance(scope, dict) and scope.get("kind") == "focus":
                runner["argv"].append("--focus")
        job, err = fabgate.request(
            self.repo, tree or self.tree, scope,
            interpreter or self.interpreter, runner,
            queue_timeout=queue_timeout, execution_timeout=execution_timeout)
        self.assertIsNone(err)
        return job

    def focus_scope(self):
        return {"kind": "focus", "plan": {
            "policy": gate.FOCUS_POLICY, "trunk": self.head, "base": self.head,
            "changed": ["helm/fabgate.py"],
            "selected": ["tests.test_fabgate"], "universe": 2}}

    def receipt(self, *prior, **over):
        row = {"v": 4, "event": "gate", "ts": pk.now_ts(),
               "repo_id": "/remote/fab/wt/helm", "head": self.head,
               "tree": self.tree, "dirty": False, "head_after": self.head,
               "tree_after": self.tree, "dirty_after": False,
               "interpreter": dict(self.interpreter),
               "argv": ["/usr/bin/python3.13", "-m", "unittest", "discover",
                        "-s", "tests", "-t", "."],
               "suite": True, "label": None, "rc": 0, "wall": 2.0,
               "status": "OK", "ran": 10, "skipped": 0, "detail": "",
               "elapsed": 1.5, "failures": [],
               "failures_unreadable": False, "base_check": None,
               "host": {"node": "snoozy", "system": "Linux",
                        "release": "1", "id": "machine"}}
        row.update(over)
        row["id"] = gate._receipt_id(row)
        path = os.path.join(self.tmp, "gate-receipts.jsonl")
        with open(path, "w") as fh:
            for stored in prior + (row,):
                fh.write(json.dumps(stored) + "\n")
        return row, path

    def authority(self, job, handle, source_artifact, exit_code=0):
        return {"format": "helm-fab-completion-v2", "key": job["key"],
                "generation": handle["generation"],
                "job_id": handle["job_id"], "host": handle["host"],
                "sha": self.head, "identity": copy.deepcopy(job["identity"]),
                "budgets": copy.deepcopy(job["budgets"]),
                "source_artifact": source_artifact,
                "artifact_sha256": "d" * 64, "exit": exit_code}


class RequestIdentityTest(FabGateBase):
    def test_same_tree_scope_interpreter_and_runner_is_a_must_hit(self):
        first = self.request(queue_timeout=60, execution_timeout=600)
        second = self.request(queue_timeout=900, execution_timeout=1200)
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(first["budgets"],
                         {"queue_s": 60.0, "execution_s": 600.0})
        self.assertEqual(second["budgets"],
                         {"queue_s": 900.0, "execution_s": 1200.0})

        builder = FakeBuilder()
        launched, err = fabgate.submit(builder, first)
        joined, join_err = fabgate.submit(builder, second)
        self.assertEqual((err, join_err), (None, None))
        self.assertEqual((launched["disposition"], joined["disposition"]),
                         ("LAUNCHED", "JOINED"))
        self.assertEqual(launched["handle"], joined["handle"])
        self.assertEqual(builder.launches, 1, "same key burned the suite twice")

    def test_join_uses_the_generation_launch_budgets(self):
        builder = FakeBuilder()
        launched, err = fabgate.submit(
            builder, self.request(queue_timeout=60, execution_timeout=600))
        self.assertIsNone(err)
        joined, err = fabgate.submit(
            builder, self.request(queue_timeout=900, execution_timeout=1200))
        self.assertIsNone(err)
        self.assertEqual(joined["disposition"], "JOINED")
        self.assertEqual(joined["request"]["budgets"],
                         {"queue_s": 60.0, "execution_s": 600.0})
        observed, err = fabgate._observed_event(
            builder.observe(joined["handle"]), joined["request"],
            joined["handle"])
        self.assertIsNone(err)
        self.assertEqual(observed["event"]["budgets"],
                         joined["request"]["budgets"])
        self.assertEqual(launched["handle"], joined["handle"])

    def test_launched_response_cannot_rewrite_submitted_budgets(self):
        builder, job = FakeBuilder(), self.request(
            queue_timeout=60, execution_timeout=600)
        submit = builder.submit

        def rewritten(request):
            response = submit(request)
            response["request"]["budgets"]["queue_s"] = 900.0
            return response

        with mock.patch.object(builder, "submit", side_effect=rewritten):
            admitted, err = fabgate.submit(builder, job)

        self.assertIsNone(admitted)
        self.assertEqual(err, "Fab launch did not preserve the submitted budgets")
        self.assertEqual(builder.launches, 1,
                         "control: the authority really launched this request")

    def test_observation_budgets_must_match_the_generation_launch(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        observed = builder.observe(launched["handle"])
        observed["budgets"]["execution_s"] = 1.0

        checked, err = fabgate._observed_event(
            observed, launched["request"], launched["handle"])

        self.assertIsNone(checked)
        self.assertEqual(
            err, "Fab observation budgets differ from the admitted launch")

    def test_identity_b_under_key_a_refuses_before_builder_submit(self):  # noqa: VACUOUS_ASSERTION — valid A reaches builder; stale B under A is the paired refusal
        first = self.request()
        second = self.request(runner=dict(self.runner, wrapper_version="d" * 64))
        stale = copy.deepcopy(first)
        stale["identity"] = second["identity"]
        self.assertNotEqual(first["key"], second["key"],
                            "control: identities A and B hash differently")
        builder = FakeBuilder()
        control, control_err = fabgate.submit(builder, first)
        self.assertIsNone(control_err)
        self.assertEqual(control["disposition"], "LAUNCHED")

        admitted, err = fabgate.submit(builder, stale)

        self.assertIsNone(admitted)
        self.assertEqual(err,
                         "gate-job request key does not match its identity")
        self.assertEqual(builder.submissions, 1)
        self.assertEqual(builder.launches, 1)

    def test_malformed_exact_envelope_and_budgets_refuse_before_submit(self):  # noqa: VACUOUS_ASSERTION — valid exact envelope reaches builder before each malformed envelope is refused
        control_builder = FakeBuilder()
        control, control_err = fabgate.submit(control_builder, self.request())
        self.assertIsNone(control_err)
        self.assertEqual(control["disposition"], "LAUNCHED")
        self.assertEqual(control_builder.submissions, 1)
        malformed = []
        extra = copy.deepcopy(self.request())
        extra["extra"] = True
        malformed.append(extra)
        budgets = copy.deepcopy(self.request())
        budgets["budgets"]["queue_s"] = float("inf")
        malformed.append(budgets)
        for job in malformed:
            with self.subTest(job=job):
                builder = FakeBuilder()
                admitted, err = fabgate.submit(builder, job)
                self.assertIsNone(admitted)
                self.assertIn("malformed", err)
                self.assertEqual(builder.submissions, 0)

    def test_submit_validates_without_reopening_the_repository(self):
        builder, job = FakeBuilder(), self.request()
        with mock.patch.object(gateimport, "_repo_identity",
                               side_effect=AssertionError("repository reopened")), \
                mock.patch.object(gateimport, "_tree_of",
                                  side_effect=AssertionError("tree reopened")):
            admitted, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual(admitted["disposition"], "LAUNCHED")
        self.assertEqual(builder.submissions, 1)

    def test_lost_submit_reply_retries_same_key_and_recovers_one_handle(self):
        builder, job = FakeBuilder(), self.request()
        builder.drop_submit_once = True
        admitted, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual(admitted["disposition"], "JOINED")
        self.assertEqual(admitted["handle"]["job_id"],
                         "gate-" + job["key"])
        self.assertEqual(builder.launches, 1)

    def test_mutating_failed_submit_retries_one_frozen_request(self):
        builder, job = FakeBuilder(), self.request()
        original = copy.deepcopy(job)
        builder.mutate_submit_once = True

        admitted, err = fabgate.submit(builder, job)

        self.assertIsNone(err)
        self.assertEqual(admitted["disposition"], "JOINED")
        self.assertTrue(job["key"])
        self.assertTrue(original["key"])
        self.assertEqual(job, original)
        stored = builder.jobs[job["key"]]["request"]
        self.assertTrue(stored["key"])
        self.assertEqual(stored, original)
        self.assertEqual(builder.launches, 1)
        self.assertEqual(builder.submissions, 2)

    def test_huge_durations_refuse_without_float_overflow(self):
        self.assertEqual(self.request(
            queue_timeout=60, execution_timeout=600)["budgets"],
                         {"queue_s": 60.0, "execution_s": 600.0})
        job, err = fabgate.request(
            self.repo, self.tree, "whole", self.interpreter, self.runner,
            queue_timeout=10 ** 10000, execution_timeout=600)
        self.assertIsNone(job)
        self.assertIn("positive finite number", err)
        job, err = fabgate.request(
            self.repo, self.tree, "whole", self.interpreter, self.runner,
            queue_timeout=60, execution_timeout=10 ** 10000)
        self.assertIsNone(job)
        self.assertIn("positive finite number", err)
        snap = fabgate.snapshot({
            "state": "RUNNING", "queue_elapsed_s": 10 ** 10000})
        self.assertEqual(snap["state"], "RUNNING")
        self.assertIsNone(snap["queue_elapsed_s"])

    def test_whole_scope_and_v2_key_ignore_mutable_gate_suite(self):
        before = self.request()
        with mock.patch.object(gate, "SUITE", ("tests.not_the_suite",)):
            after = self.request()
        self.assertEqual(after["identity"]["scope"], {
            "kind": "whole",
            "argv": ["-m", "unittest", "discover", "-s", "tests", "-t", "."]})
        self.assertEqual(after["key"], before["key"])

    def test_linked_worktrees_share_the_canonical_repository_key(self):
        first = self.request()
        room = os.path.join(self.tmp, "linked-room")
        _git(self.repo, "worktree", "add", "-q", room, "HEAD")
        second, err = fabgate.request(
            room, self.tree, "whole", self.interpreter, self.runner,
            queue_timeout=60, execution_timeout=600)
        self.assertIsNone(err)
        self.assertEqual(first["identity"]["repository"],
                         second["identity"]["repository"])
        self.assertEqual(first["key"], second["key"])

    def test_both_cgroup_regimes_fab_admits_build_a_request(self):
        """FAB'S PRODUCERS EMIT THE GENERATION CGROUP, and its node authority
        ranks that one first and the direct-scope regime as historical. A reader
        here that took only the historical one refused every measurement Fab
        currently makes, so `helm gate fab contract` could not accept
        `fab gate measure`'s own runner identity and the typed boundary had no
        composable path at all."""
        keys = set()
        for cgroup in gateimport.FAB_RUNNER_CGROUPS:
            job, err = fabgate.request(
                self.repo, self.tree, "whole", self.interpreter,
                dict(self.runner, cgroup=cgroup))
            self.assertIsNone(err, cgroup)
            keys.add(job["key"])
        # DIFFERENT REGIMES ARE DIFFERENT SUITES: both are admitted and neither
        # is silently folded into the other.
        self.assertEqual(len(keys), len(gateimport.FAB_RUNNER_CGROUPS))
        # CONTROL on the same field: a regime Fab does not admit is refused, so
        # the two above are a named set and not an unchecked field.
        _job, err = fabgate.request(
            self.repo, self.tree, "whole", self.interpreter,
            dict(self.runner, cgroup="systemd-whatever-v9"))
        self.assertIsNotNone(err)

    def test_tree_scope_interpreter_and_runner_changes_are_must_misses(self):
        base = self.request()
        with open(os.path.join(self.repo, "other.txt"), "w") as fh:
            fh.write("other\n")
        _git(self.repo, "add", "other.txt")
        other_tree = _git(self.repo, "write-tree")
        _git(self.repo, "reset", "--hard", "-q", "HEAD")
        other_interpreter = dict(self.interpreter, executable="/opt/python")
        other_runner = dict(self.runner, wrapper_version="d" * 64)
        focus = self.request(scope=self.focus_scope())
        runnable = (
            self.request(tree=other_tree),
            self.request(interpreter=other_interpreter),
            self.request(runner=other_runner),
        )
        self.assertEqual(
            len({base["key"], focus["key"]} | {job["key"] for job in runnable}),
            5)

        builder = FakeBuilder()
        admitted, err = fabgate.submit(builder, base)
        self.assertIsNone(err)
        self.assertEqual(admitted["disposition"], "LAUNCHED")
        for job in runnable:
            admitted, err = fabgate.submit(builder, job)
            self.assertIsNone(err)
            self.assertEqual(admitted["disposition"], "LAUNCHED")
        self.assertEqual(builder.launches, 4)

    def test_focused_identity_is_representable_but_submit_refuses(self):
        builder = FakeBuilder()
        whole, focus = self.request(), self.request(scope=self.focus_scope())
        launched, err = fabgate.submit(builder, whole)
        self.assertIsNone(err)
        self.assertEqual(launched["disposition"], "LAUNCHED")

        admitted, err = fabgate.submit(builder, focus)

        self.assertIsNone(admitted)
        self.assertIn("no live challenge-framed custody", err)
        self.assertIn("helm gate run --focus --box HOST", err)
        self.assertEqual(builder.submissions, 1)
        self.assertEqual(builder.launches, 1)

    def test_gate_cli_exposes_the_machine_contract(self):
        out = io.StringIO()
        args = ["fab", "contract", "--repo", self.repo,
                "--tree", self.tree,
                "--interpreter", json.dumps(self.interpreter),
                "--runner", json.dumps(self.runner),
                "--queue-timeout", "60",
                "--execution-timeout", "600"]
        with mock.patch.object(fabgate.sys, "stdout", out):
            rc = gate._gate_dispatch(args)
        self.assertEqual(rc, 0)
        body = json.loads(out.getvalue())
        self.assertEqual(body["key"], self.request()["key"])
        self.assertEqual(body["budgets"],
                         {"queue_s": 60.0, "execution_s": 600.0})

    def test_gate_cli_emits_no_executable_focused_contract(self):
        out, err = io.StringIO(), io.StringIO()
        runner = copy.deepcopy(self.runner)
        runner["argv"].append("--focus")
        args = ["fab", "contract", "--repo", self.repo,
                "--tree", self.tree,
                "--interpreter", json.dumps(self.interpreter),
                "--runner", json.dumps(runner),
                "--scope", json.dumps(self.focus_scope())]
        with mock.patch.object(fabgate.sys, "stdout", out), \
                mock.patch.object(fabgate.sys, "stderr", err):
            rc = gate._gate_dispatch(args)
        self.assertEqual(rc, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("no live challenge-framed custody", err.getvalue())
        self.assertIn("helm gate run --focus --box HOST", err.getvalue())

    def test_queued_and_running_same_key_both_join(self):
        builder, job = FakeBuilder(), self.request()
        first, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual(first["snapshot"]["state"], "QUEUED-KEY")
        queued, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual((queued["disposition"], queued["snapshot"]["state"]),
                         ("JOINED", "QUEUED-KEY"))
        builder.state(job, "RUNNING")
        running, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual((running["disposition"], running["snapshot"]["state"]),
                         ("JOINED", "RUNNING"))
        self.assertEqual(builder.launches, 1)


class StateAndFollowerTest(FabGateBase):
    def test_exact_builder_states_and_exits_are_not_flattened(self):
        for state in ("QUEUED-KEY", "QUEUED-P0", "QUEUED-SLOT", "RUNNING",
                      "SUPERSEDED"):
            self.assertEqual(fabgate.snapshot({"state": state})["state"], state)
        for code in (94, 95, 96, 97, 98, 99, 143):
            got = fabgate.snapshot({"state": "RUNNING", "exit": str(code)})
            self.assertEqual((got["exit"], got["exit_state"]), (code, "EXACT"))
        malformed = fabgate.snapshot({"state": "running", "exit": "09x"})
        self.assertEqual((malformed["state"], malformed["exit"],
                          malformed["exit_state"]),
                         ("UNKNOWN", None, "UNKNOWN"))
        unreadable = fabgate.snapshot(None)
        self.assertEqual((unreadable["state"], unreadable["exit_state"]),
                         ("UNKNOWN", "UNKNOWN"))
        absent = fabgate.snapshot({"state": "RUNNING"})
        null = fabgate.snapshot({"state": "RUNNING", "receipt": None})
        empty = fabgate.snapshot({"state": "RUNNING", "receipt": ""})
        exact = fabgate.snapshot({"state": "RUNNING", "receipt": "abcd"})
        unknown = fabgate.snapshot({"state": "RUNNING", "receipt": []})
        self.assertEqual((absent["receipt_state"], null["receipt_state"],
                          empty["receipt_state"], exact["receipt_state"],
                          unknown["receipt_state"]),
                         ("ABSENT", "ABSENT", "ABSENT", "EXACT", "UNKNOWN"))
        self.assertEqual(fabgate.snapshot(unknown)["receipt_state"], "UNKNOWN")

    def test_receipt_marker_contradictions_never_abstain_as_absent(self):
        self.assertEqual(fabgate.snapshot({
            "state": "RUNNING", "receipt_state": "EXACT",
            "receipt": "abcd"})["receipt_state"], "EXACT")
        cases = (
            ({"receipt_state": "EXACT"}, "UNKNOWN"),
            ({"receipt_state": "EXACT", "receipt": None}, "UNKNOWN"),
            ({"receipt_state": "ABSENT", "receipt": "abcd"}, "UNKNOWN"),
            ({"receipt_state": " UNKNOWN ", "receipt": None}, "UNKNOWN"),
            ({"receipt_state": "unknown", "receipt": None}, "UNKNOWN"),
            ({"receipt_state": 17, "receipt": None}, "UNKNOWN"),
            ({"receipt_state": "ABSENT", "receipt": None}, "ABSENT"),
            ({"receipt_state": "EXACT", "receipt": "abcd"}, "EXACT"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                raw = dict(raw, state="RUNNING")
                self.assertEqual(
                    fabgate.snapshot(raw)["receipt_state"], expected)

    def test_handle_requires_exact_generation_grammar(self):
        job = self.request()
        good = {"v": fabgate.HANDLE_VERSION, "key": job["key"],
                "job_id": "gate-" + job["key"], "host": "snoozy",
                "generation": "run-%s-123-%s" % ("a" * 32, "b" * 16)}
        exact, err = fabgate._handle(good, job["key"])
        self.assertIsNone(err)
        self.assertTrue(exact["generation"])
        self.assertEqual(exact, good)
        refused = []
        for generation in ("run-1", "run-%s-0-%s" % (
                "a" * 32, "b" * 16), "RUN-%s-1-%s" % (
                    "a" * 32, "b" * 16)):
            bad = dict(good, generation=generation)
            refused.append(fabgate._handle(bad, job["key"]))
        self.assertEqual(len(refused), 3)
        self.assertTrue(all(exact is None for exact, _err in refused))
        self.assertTrue(all("exact v2" in err for _exact, err in refused))

    def test_wire_snapshot_requires_the_exact_v2_shape(self):
        raw = {"state": "RUNNING", "exit": None, "exit_class": None,
               "artifact": None, "artifact_sha256": None, "receipt": None,
               "queue_elapsed_s": 1.0, "execution_elapsed_s": 2.0,
               "live": True, "reason": None}
        state, err = fabgate._wire_snapshot(raw)
        self.assertIsNone(err)
        self.assertEqual(state["state"], "RUNNING")
        _state, err = fabgate._wire_snapshot(dict(raw, extra=True))
        self.assertIn("exact v2 ten-field shape", err)

    def test_producer_v2_snapshot_samples_are_accepted(self):
        running = {
            "state": "RUNNING", "exit": None, "exit_class": None,
            "artifact": None, "artifact_sha256": None, "receipt": None,
            "queue_elapsed_s": 1.25, "execution_elapsed_s": 0.5,
            "live": True, "reason": None}
        completed = {
            "state": "COMPLETED", "exit": 0, "exit_class": "OK",
            "artifact": "/remote/gate-receipts.jsonl",
            "artifact_sha256": "d" * 64, "receipt": "abcd",
            "queue_elapsed_s": 1.25, "execution_elapsed_s": 2.5,
            "live": False, "reason": None}

        checked_running, running_err = fabgate._wire_snapshot(running)
        checked_completed, completed_err = fabgate._wire_snapshot(completed)

        self.assertEqual((running_err, completed_err), (None, None))  # noqa: VACUOUS_ASSERTION — exact accepted state/digest assertions below prove both producer samples were parsed
        self.assertEqual((checked_running["state"], checked_completed["state"]),
                         ("RUNNING", "COMPLETED"))
        self.assertEqual(checked_completed["artifact_sha256"], "d" * 64)

    def test_exact_unknown_authority_is_typed_not_malformed(self):
        builder, job = FakeBuilder(), self.request()
        admitted, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        event = builder.observe(admitted["handle"])
        event.update({
            "tree": None, "sha": None, "disposition": "UNKNOWN",
            "generation": None, "identity": None,
            "budgets": {"queue_s": None, "execution_s": None},
            "snapshot": {"state": "UNKNOWN", "exit": None,
                         "exit_class": None, "artifact": None,
                         "artifact_sha256": None, "receipt": None,
                         "queue_elapsed_s": None,
                         "execution_elapsed_s": None, "live": False,
                         "reason": "authority unreadable"},
            "reason": "authority unreadable"})

        result, err = fabgate.reconcile_events(
            job, admitted["handle"], event, None, self.repo)

        self.assertEqual(result, {"state": "UNKNOWN", "exit": None,
                                  "receipt": None, "verdict": None})
        self.assertEqual(err, "authority unreadable")

    def test_unknown_disposition_cannot_carry_normal_authority(self):
        builder, job = FakeBuilder(), self.request()
        admitted, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"])
        event = builder.observe(admitted["handle"])
        event["disposition"] = "UNKNOWN"

        result, err = fabgate.reconcile_events(
            job, admitted["handle"], event,
            builder.fetch_receipt(admitted["handle"]), self.repo)

        self.assertIsNone(result)
        self.assertEqual(
            err, "Fab observation UNKNOWN disposition/snapshot is contradictory")
        receipts, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(receipts, [])  # noqa: VACUOUS_ASSERTION — exact contradiction error proves the normal completion was reached; zero receipts is the authority boundary under test

    def test_submit_unknown_disposition_cannot_carry_normal_snapshot(self):
        builder, job = FakeBuilder(), self.request()
        submit = builder.submit
        def contradictory(request):
            response = submit(request)
            response["disposition"] = "UNKNOWN"
            return response
        with mock.patch.object(builder, "submit", side_effect=contradictory):
            admitted, err = fabgate.submit(builder, job)

        self.assertIsNone(admitted)
        self.assertEqual(
            err, "Fab submit UNKNOWN disposition/snapshot is contradictory")

    def test_successor_generation_is_terminal_superseded(self):
        builder, job = FakeBuilder(), self.request()
        admitted, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        event = builder.observe(admitted["handle"])
        event["generation"] = "run-%s-2-%s" % ("d" * 32, "e" * 16)
        event["disposition"] = "JOINED"
        event["snapshot"].update({
            "state": "SUPERSEDED", "exit": 93,
            "exit_class": "SUPERSEDED", "artifact": None,
            "artifact_sha256": None, "receipt": None, "live": False,
            "reason": None})
        event["reason"] = (
            "requested generation is superseded by canonical authority")

        result, err = fabgate.reconcile_events(
            job, admitted["handle"], event, None, self.repo)

        self.assertEqual(result, {"state": "SUPERSEDED", "exit": 93,
                                  "receipt": None, "verdict": None})
        self.assertIn("superseded", err)

    def test_local_interrupt_detaches_loudly_without_killing_remote_job(self):
        builder, job = FakeBuilder(), self.request()
        admitted, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        builder.state(job, "RUNNING")
        builder.wait_error = KeyboardInterrupt()
        out = io.StringIO()
        result, err = fabgate.follow(builder, job, admitted["handle"], self.repo,
                                     out=out)
        self.assertIsNone(err)
        self.assertTrue(result["detached"])
        self.assertEqual(builder.kills, 0)
        text = out.getvalue()
        job_id = admitted["handle"]["job_id"]
        generation = admitted["handle"]["generation"]
        self.assertIn("CLIENT DETACHED", text)
        self.assertIn("remote job: %s" % job_id, text)
        self.assertIn("remote state: RUNNING  exit=pending", text)
        self.assertIn(
            "join: fab gate --join snoozy %s --generation %s --repo"
            % (job_id, generation), text)
        self.assertIn(
            "status: fab gate observe --host snoozy --job %s --generation %s"
            % (job_id, generation), text)
        self.assertIn(
            "kill: fab gate kill --host snoozy --job %s --generation %s"
            % (job_id, generation), text)
        self.assertIn(
            "import: fab gate --import snoozy %s --generation %s --repo"
            % (job_id, generation), text)
        self.assertEqual(
            builder.observe(admitted["handle"])["snapshot"]["state"],
            "RUNNING")

    def test_sigterm_becomes_client_detach_and_restores_the_prior_handler(self):
        prior = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(fabgate.ClientDetached) as raised:
            with fabgate._detach_signals():
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertEqual(raised.exception.reason,
                         "local signal %d" % signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), prior)

    def test_receipt_recovery_failure_detaches_with_generation_commands(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"])
        builder.fetch_error = OSError("remote copy failed")
        out = io.StringIO()

        result, err = fabgate.submit_and_follow(
            builder, job, self.repo, out=out)

        self.assertIsNone(err)
        self.assertTrue(result["detached"])
        text = out.getvalue()
        self.assertIn("CLIENT DETACHED — remote receipt recovery failure", text)
        self.assertIn("--generation %s" % launched["handle"]["generation"], text)
        self.assertIn("status: fab gate observe", text)
        self.assertIn("import: fab gate --import", text)

    def test_malformed_observation_detaches_with_recovery_surface(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        out = io.StringIO()
        with mock.patch.object(builder, "observe", return_value={"bad": True}):
            result, err = fabgate.follow(
                builder, job, launched["handle"], self.repo, out=out)

        self.assertIsNone(err)
        self.assertTrue(result["detached"])
        self.assertIn("CLIENT DETACHED — remote observation unreadable",
                      out.getvalue())
        self.assertIn(launched["handle"]["generation"], out.getvalue())

    def test_pre_admission_client_timeout_stays_queued_not_suite_timed_out(self):
        builder, job = FakeBuilder(), self.request(
            queue_timeout=600, execution_timeout=1800)
        admitted, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        builder.state(job, "QUEUED-SLOT")
        builder.wait_returns = True
        out = io.StringIO()
        with mock.patch.object(fabgate.time, "monotonic",
                               side_effect=(0.0, 1.0)):
            result, err = fabgate.follow(builder, job, admitted["handle"], self.repo,
                                         client_timeout=0.5, out=out)
        self.assertIsNone(err)
        self.assertEqual(builder.submissions, 1)
        self.assertTrue(result["detached"])
        self.assertEqual(result["snapshot"]["state"], "QUEUED-SLOT")
        self.assertIsNone(result["snapshot"]["execution_elapsed_s"])
        self.assertIn("remote state: QUEUED-SLOT  exit=pending", out.getvalue())
        self.assertNotIn("suite timed out", out.getvalue().lower())
        self.assertEqual(builder.kills, 0)

    def test_non_live_pending_job_refuses_instead_of_waiting_forever(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        builder.jobs[job["key"]]["snapshot"]["live"] = False

        result, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)

        self.assertIsNone(result)
        self.assertEqual(err, "Fab non-live job has no terminal authority")

    def test_wait_returned_event_is_consumed_without_stale_reobserve(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"])
        builder.wait_event = builder.observe(launched["handle"])
        builder.jobs[job["key"]]["source_artifact"] = artifact
        builder.state(job, "QUEUED-SLOT")
        observed = mock.Mock(side_effect=(
            builder.observe(launched["handle"]),
            AssertionError("stale state was re-observed")))

        with mock.patch.object(builder, "observe", observed):
            result, err = fabgate.follow(
                builder, job, launched["handle"], self.repo)

        self.assertIsNone(err)  # noqa: VACUOUS_ASSERTION — exact imported receipt and one observe call prove the returned wait event drove completion
        self.assertEqual(result["receipt"], row["id"])
        self.assertEqual(observed.call_count, 1)

    def test_typed_unknown_detach_renders_unknown_exit(self):
        key = "a" * 64
        handle = {"v": fabgate.HANDLE_VERSION, "key": key,
                  "job_id": "gate-" + key, "host": "snoozy",
                  "generation": "run-%s-1-%s" % ("b" * 32, "c" * 16)}
        text = fabgate.detach_text(
            handle, self.repo,
            {"state": "UNKNOWN", "exit": None,
             "artifact": None, "receipt": None}, "authority unreadable")
        self.assertIn("remote state: UNKNOWN  exit=UNKNOWN", text)
        self.assertNotIn("exit=pending", text)

    def test_detach_preserves_unknown_measurement(self):
        key = "a" * 64
        handle = {"v": fabgate.HANDLE_VERSION, "key": key,
                  "job_id": "gate-" + key, "host": "snoozy",
                  "generation": "run-%s-1-%s" % ("b" * 32, "c" * 16)}
        text = fabgate.detach_text(handle, self.repo,
                                   {"state": "garbled", "exit": "garbled"},
                                   "wrapper lost")
        self.assertIn("remote state: UNKNOWN  exit=UNKNOWN", text)


class ReconciliationTest(FabGateBase):
    def _completion(self, **complete):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        values = {"receipt": row["id"]}
        values.update(complete)
        builder.complete(job, artifact, **values)
        return (builder, job, launched["handle"], row,
                builder.observe(launched["handle"]),
                builder.fetch_receipt(launched["handle"]))

    def _assert_no_authority(self):
        receipts, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(receipts, [])
        bindings, unavailable = eventledger.checked_events(
            gateimport.bindings_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual(bindings, [])
        completions, unavailable = eventledger.checked_events(
            gateimport.fab_completions_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual(completions, [])

    def test_terminal_null_artifact_recovers_from_newer_fetch_event(self):
        builder, job, handle, row, observed, fetched = self._completion()
        observed["snapshot"]["artifact"] = None
        observed["snapshot"]["artifact_sha256"] = None

        result, err = fabgate.reconcile_events(
            job, handle, observed, fetched, self.repo)

        self.assertIsNone(err)
        self.assertEqual(result["receipt"], row["id"])
        self.assertEqual(result["state"], "COMPLETED")

    def test_fetch_artifact_and_error_are_contradictory(self):
        _builder, job, handle, _row, observed, fetched = self._completion()
        fetched["error"] = "copy failed after stale output was found"

        result, err = fabgate.reconcile_events(
            job, handle, observed, fetched, self.repo)

        self.assertEqual(result, {"state": "COMPLETED", "exit": None,
                                  "receipt": None, "verdict": None})
        self.assertEqual(err, "Fab fetch cannot carry both an artifact and an error")
        self._assert_no_authority()

    def test_reconciliation_loads_artifact_once_inside_importer(self):
        builder, job, handle, row, observed, fetched = self._completion()
        load = gateimport._load_rows
        with mock.patch.object(gateimport, "_load_rows", wraps=load) as opened:
            result, err = fabgate.reconcile_events(
                job, handle, observed, fetched, self.repo)

        self.assertIsNone(err)  # noqa: VACUOUS_ASSERTION — exact receipt import is the positive control for the one-open ownership assertion
        self.assertEqual(result["receipt"], row["id"])
        self.assertEqual(opened.call_count, 1)

    def test_completion_identity_mismatches_refuse_before_authority(self):
        mutations = (
            ("observed-key", "observed", "key", "f" * 64),
            ("observed-job", "observed", "job_id", "gate-" + "f" * 64),
            ("observed-tree", "observed", "tree", "f" * 40),
            ("observed-sha", "observed", "sha", "f" * 40),
            ("observed-generation", "observed", "generation",
             "run-%s-9-%s" % ("f" * 32, "e" * 16)),
            ("fetched-key", "fetched", "key", "f" * 64),
            ("fetched-job", "fetched", "job_id", "gate-" + "f" * 64),
            ("fetched-tree", "fetched", "tree", "f" * 40),
            ("fetched-sha", "fetched", "sha", "f" * 40),
            ("fetched-generation", "fetched", "generation",
             "run-%s-9-%s" % ("f" * 32, "e" * 16)),
            ("source-artifact", "fetched", "source_artifact", "/other"),
        )
        outcomes = []
        for label, side, field, value in mutations:
            _builder, job, handle, _row, observed, fetched = self._completion()
            target = observed if side == "observed" else fetched
            target[field] = value
            result, err = fabgate.reconcile_events(
                job, handle, observed, fetched, self.repo)
            outcomes.append((label, result, err))
        self.assertEqual(len(outcomes), len(mutations))
        self.assertTrue(all(err for _label, _result, err in outcomes))
        self.assertTrue(all(
            result is None or result["receipt"] is None
            for _label, result, _err in outcomes))
        self._assert_no_authority()

    def test_observed_and_fetched_identity_echoes_are_exact(self):
        outcomes = []
        for side in ("observed", "fetched"):
            _builder, job, handle, _row, observed, fetched = self._completion()
            target = observed if side == "observed" else fetched
            target["identity"] = copy.deepcopy(job["identity"])
            target["identity"]["runner"]["wrapper_version"] = "d" * 64
            outcomes.append(fabgate.reconcile_events(
                job, handle, observed, fetched, self.repo))
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(
            result is None or result["receipt"] is None
            for result, _err in outcomes))
        self.assertTrue(all(
            "identity differs" in err for _result, err in outcomes))
        self._assert_no_authority()

    def test_coherent_job_b_identity_cannot_complete_handle_a(self):
        builder_a, job_a = FakeBuilder(), self.request()
        launched_a, err = fabgate.submit(builder_a, job_a)
        self.assertIsNone(err)
        job_b = self.request(runner=dict(
            self.runner, wrapper_version="d" * 64))
        builder_b = FakeBuilder()
        launched_b, err = fabgate.submit(builder_b, job_b)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder_b.complete(job_b, artifact, receipt=row["id"])

        result, err = fabgate.reconcile_events(
            job_a, launched_a["handle"],
            builder_b.observe(launched_b["handle"]),
            builder_b.fetch_receipt(launched_b["handle"]), self.repo)

        self.assertIsNone(result)
        self.assertIn("key differs", err)
        self._assert_no_authority()

    def test_malformed_observed_exit_refuses_valid_fetch(self):
        _builder, job, handle, _row, observed, fetched = self._completion()
        observed["snapshot"]["exit"] = "not-an-exit"

        result, err = fabgate.reconcile_events(
            job, handle, observed, fetched, self.repo)

        self.assertIsNone(result)
        self.assertEqual(err, "Fab observed exit is UNKNOWN")
        self._assert_no_authority()

    def test_reconcile_stdin_is_the_production_import_seam(self):
        _builder, job, handle, row, observed, fetched = self._completion()
        envelope = {"request": job, "handle": handle,
                    "observed": observed, "fetched": fetched}
        stdin = io.StringIO(json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        stdout, stderr = io.StringIO(), io.StringIO()

        with mock.patch.object(fabgate.sys, "stdin", stdin), \
                mock.patch.object(fabgate.sys, "stdout", stdout), \
                mock.patch.object(fabgate.sys, "stderr", stderr):
            rc = gate._gate_dispatch(
                ["fab", "reconcile", "--repo", self.repo, "--stdin"])

        self.assertEqual(rc, 0)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["receipt"])
        self.assertEqual(result["receipt"], row["id"])
        self.assertEqual(stderr.getvalue(), "")  # noqa: VACUOUS_ASSERTION — rc 0 and the exact imported receipt prove this is the success-path silence contract

    def test_reconcile_stdin_emits_typed_unknown_before_warning(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        observed = builder.observe(launched["handle"])
        observed.update({
            "tree": None, "sha": None, "disposition": "UNKNOWN",
            "generation": None, "identity": None,
            "budgets": {"queue_s": None, "execution_s": None},
            "snapshot": {"state": "UNKNOWN", "exit": None,
                         "exit_class": None, "artifact": None,
                         "artifact_sha256": None, "receipt": None,
                         "queue_elapsed_s": None,
                         "execution_elapsed_s": None, "live": False,
                         "reason": "authority unreadable"},
            "reason": "authority unreadable"})
        envelope = {"request": job, "handle": launched["handle"],
                    "observed": observed, "fetched": None}
        stdin = io.StringIO(json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        stdout, stderr = io.StringIO(), io.StringIO()

        with mock.patch.object(fabgate.sys, "stdin", stdin), \
                mock.patch.object(fabgate.sys, "stdout", stdout), \
                mock.patch.object(fabgate.sys, "stderr", stderr):
            rc = gate._gate_dispatch(
                ["fab", "reconcile", "--repo", self.repo, "--stdin"])

        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {
            "state": "UNKNOWN", "exit": None,
            "receipt": None, "verdict": None})
        self.assertIn("WARNING — authority unreadable", stderr.getvalue())
        self.assertNotIn("REFUSED", stderr.getvalue())  # noqa: VACUOUS_ASSERTION — exact typed UNKNOWN output and warning are the positive controls; refusal text must remain absent

    def test_reconcile_stdin_emits_typed_superseded_before_warning(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        observed = builder.observe(launched["handle"])
        observed["generation"] = "run-%s-9-%s" % ("f" * 32, "e" * 16)
        observed["snapshot"] = {
            "state": "SUPERSEDED", "exit": 93,
            "exit_class": "SUPERSEDED", "artifact": None,
            "artifact_sha256": None, "receipt": None,
            "queue_elapsed_s": 1.0, "execution_elapsed_s": None,
            "live": False, "reason": None}
        envelope = {"request": job, "handle": launched["handle"],
                    "observed": observed, "fetched": None}
        stdin = io.StringIO(json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        stdout, stderr = io.StringIO(), io.StringIO()

        with mock.patch.object(fabgate.sys, "stdin", stdin), \
                mock.patch.object(fabgate.sys, "stdout", stdout), \
                mock.patch.object(fabgate.sys, "stderr", stderr):
            rc = gate._gate_dispatch(
                ["fab", "reconcile", "--repo", self.repo, "--stdin"])

        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {
            "state": "SUPERSEDED", "exit": 93,
            "receipt": None, "verdict": None})
        self.assertIn("WARNING — remote gate job generation was superseded",
                      stderr.getvalue())
        self.assertNotIn("REFUSED", stderr.getvalue())  # noqa: VACUOUS_ASSERTION — exact typed SUPERSEDED output and warning are the positive controls; refusal text must remain absent

    def test_reconcile_stdin_byte_cap_counts_utf8_not_codepoints(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(fabgate.sys, "stdin", io.StringIO("é" * 600000)), \
                mock.patch.object(fabgate.sys, "stdout", stdout), \
                mock.patch.object(fabgate.sys, "stderr", stderr):
            rc = gate._gate_dispatch(
                ["fab", "reconcile", "--repo", self.repo, "--stdin"])

        self.assertEqual(rc, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("1048576 UTF-8 bytes", stderr.getvalue())

    def test_reconcile_stdin_mismatch_writes_no_authority(self):
        _builder, job, handle, _row, observed, fetched = self._completion()
        fetched["source_artifact"] = "/wrong"
        envelope = {"request": job, "handle": handle,
                    "observed": observed, "fetched": fetched}
        stdin = io.StringIO(json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        stdout, stderr = io.StringIO(), io.StringIO()

        with mock.patch.object(fabgate.sys, "stdin", stdin), \
                mock.patch.object(fabgate.sys, "stdout", stdout), \
                mock.patch.object(fabgate.sys, "stderr", stderr):
            rc = gate._gate_dispatch(
                ["fab", "reconcile", "--repo", self.repo, "--stdin"])

        self.assertEqual(rc, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("REFUSED", stderr.getvalue())
        self._assert_no_authority()

    def test_reconcile_stdin_rejects_noncanonical_or_argv_json(self):
        def run(args, body):
            stderr = io.StringIO()
            with mock.patch.object(fabgate.sys, "stdin", io.StringIO(body)), \
                    mock.patch.object(fabgate.sys, "stderr", stderr):
                return gate._gate_dispatch(args), stderr.getvalue()

        rc, stderr = run(["fab", "reconcile", "--repo", self.repo], "{}")
        self.assertEqual(rc, 2)
        self.assertTrue(stderr)
        rc, stderr = run(
            ["fab", "reconcile", "--repo", self.repo, "--stdin"],
            "{ \"request\": null }")
        self.assertEqual(rc, 2)
        self.assertTrue(stderr)
        self._assert_no_authority()

    def test_pre_guard_focused_job_still_refuses_plain_artifact_import(self):
        builder, job = FakeBuilder(), self.request(scope=self.focus_scope())
        raw = builder.submit(job)
        plan = copy.deepcopy(job["identity"]["scope"]["plan"])
        plan["executed"] = list(plan["selected"])
        plan["executed_ids"] = 1
        row, artifact = self.receipt(
            v=gate.FOCUSED_VERSION, suite=False, focus=plan,
            argv=[self.interpreter["executable"], "-m", "unittest", "-v"]
                 + plan["selected"])
        builder.complete(job, artifact, receipt=row["id"])

        recovered, err = fabgate.reconcile(
            builder, job, raw["handle"], self.repo)

        self.assertEqual(recovered,
                         {"state": "COMPLETED", "exit": 0,
                          "receipt": None, "verdict": None})
        self.assertIn("focused Fab completion", err)
        self.assertIn("plain artifact", err)
        self._assert_no_authority()

    def test_multi_row_artifact_reconciles_the_exact_builder_receipt(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual(launched["disposition"], "LAUNCHED")
        unrelated, _path = self.receipt(ts="2026-08-28T00:00:00Z")
        target, artifact = self.receipt(
            unrelated, ts="2026-08-28T00:00:01Z")
        builder.complete(job, artifact, receipt=target["id"])

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)
        self.assertIsNone(err)
        self.assertEqual(recovered["verdict"], "imported")
        self.assertEqual(recovered["receipt"], target["id"])
        receipts, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual([row["id"] for row in receipts], [target["id"]])
        self.assertTrue(target["id"])
        self.assertTrue(unrelated["id"])
        self.assertNotEqual(target["id"], unrelated["id"],
                            "control: the artifact carries two distinct gate rows")

    def test_wrong_builder_receipt_id_refuses_multi_row_artifact(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        unrelated, _path = self.receipt(ts="2026-08-28T00:00:00Z")
        _target, artifact = self.receipt(
            unrelated, ts="2026-08-28T00:00:01Z")
        builder.complete(job, artifact, receipt="deadbeef")

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)
        self.assertIsNone(recovered["receipt"])
        self.assertIn("no row with id deadbeef", err)
        self._assert_no_authority()

    def test_missing_builder_receipt_id_refuses_before_import(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        _row, artifact = self.receipt()
        builder.complete(job, artifact)

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)
        self.assertIsNone(recovered["receipt"])
        self.assertEqual(err, "Fab completion names no exact receipt id")
        self._assert_no_authority()

    def test_snapshot_and_fetch_receipt_ids_must_agree(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        target, artifact = self.receipt(ts="2026-08-28T00:00:00Z")
        other, artifact = self.receipt(
            target, ts="2026-08-28T00:00:01Z")
        builder.complete(job, artifact, receipt=target["id"])
        builder.jobs[job["key"]]["receipt"] = other["id"]

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)
        self.assertIsNone(recovered["receipt"])
        self.assertEqual(err, "Fab snapshot/fetch receipt ids disagree")
        self._assert_no_authority()

    def test_malformed_builder_receipt_id_refuses_before_import(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        _row, artifact = self.receipt()
        builder.complete(job, artifact, receipt="NOT-A-RECEIPT")

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)
        self.assertIsNone(recovered)
        self.assertEqual(err, "Fab snapshot receipt id is malformed")
        self._assert_no_authority()

    def test_present_non_string_snapshot_receipt_refuses_valid_fetch(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"])
        builder.jobs[job["key"]]["snapshot"]["receipt"] = {
            "id": row["id"]}

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)

        self.assertIsNone(recovered)
        self.assertEqual(err, "Fab snapshot receipt id is malformed")
        self._assert_no_authority()

    def test_receipt_disposition_preserves_invalid_snapshot_marker(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"])
        builder.jobs[job["key"]]["snapshot"]["receipt"] = "not/valid"

        recovered, err = fabgate.submit_and_follow(builder, job, self.repo)

        self.assertIsNone(recovered)
        self.assertEqual(err, "Fab snapshot receipt id is malformed")
        self.assertEqual(builder.launches, 1)
        self.assertEqual(builder.dispositions, ["LAUNCHED", "RECEIPT"],
                         "control: the refusing path is RECEIPT recovery")
        self._assert_no_authority()

    def test_completion_exit_must_match_the_receipt_rc_before_import(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"], exit_code=1)

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)

        self.assertIsNone(recovered["receipt"])
        self.assertEqual(err, "Fab receipt rc differs from the completion authority")
        self._assert_no_authority()

    def test_terminal_snapshot_and_fetch_exits_must_agree(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"], exit_code=0)
        builder.jobs[job["key"]]["fetch_exit"] = 143

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)
        self.assertEqual(recovered,
                         {"state": "COMPLETED", "exit": None,
                          "receipt": None, "verdict": None})
        self.assertEqual(err, "Fab snapshot/fetch exits disagree (0 != 143)")
        self._assert_no_authority()

    def test_absent_fetch_exit_defers_to_exact_terminal_snapshot(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"], exit_code=0)
        builder.jobs[job["key"]]["fetch_exit"] = None

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)
        self.assertIsNone(err)
        self.assertEqual(recovered,
                         {"state": "COMPLETED", "exit": 0,
                          "receipt": row["id"], "verdict": "imported"})

    def test_completion_append_failure_never_claims_fab_authority(self):
        builder, job, handle, row, _observed, _fetched = self._completion()
        with mock.patch.object(
                gateimport, "_record_fab_completion",
                return_value=(None, None, "completion append failed")):
            recovered, err = fabgate.reconcile(
                builder, job, handle, self.repo)

        self.assertEqual(recovered, {"state": "COMPLETED", "exit": 0,
                                     "receipt": None,
                                     "verdict": "completion-pending"})
        self.assertEqual(err, "completion append failed")
        receipts, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(receipts, [])  # noqa: VACUOUS_ASSERTION — the exact completion-pending verdict is the positive control; no generic receipt may become spendable
        bindings, unavailable = eventledger.checked_events(
            gateimport.bindings_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual(bindings, [])  # noqa: VACUOUS_ASSERTION — the exact completion append failure is the positive control; no repository binding may follow it
        completions, unavailable = eventledger.checked_events(
            gateimport.fab_completions_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual(completions, [])  # noqa: VACUOUS_ASSERTION — the planted append failure is the positive control; no completion row may exist
        authorized, why = gateimport.repository_authorization(row, self.repo)
        self.assertIsNone(authorized)
        self.assertIn("receipt ledger does not hold this exact receipt", why)

    def test_direct_fab_import_normalizes_integer_budgets_for_replay(self):
        builder, job = FakeBuilder(), self.request(
            queue_timeout=60, execution_timeout=600)
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        with open(artifact, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        authority = self.authority(
            job, launched["handle"], "/remote/gate-receipts.jsonl")
        authority["artifact_sha256"] = digest
        authority["budgets"] = {"queue_s": 60, "execution_s": 600}

        imported, verdict, err = gateimport.import_fab_receipt(
            artifact, self.repo, authority, want_id=row["id"])
        authority["budgets"] = {"queue_s": 60.0, "execution_s": 600.0}
        again, replay, replay_err = gateimport.import_fab_receipt(
            artifact, self.repo, authority, want_id=row["id"])

        self.assertIsNone(err)  # noqa: VACUOUS_ASSERTION — imported/duplicate verdicts and exact receipt ids prove both direct calls succeeded
        self.assertIsNone(replay_err)  # noqa: VACUOUS_ASSERTION — canonical float completion evidence below proves integer replay normalized
        self.assertEqual((verdict, replay), ("imported", "duplicate"))
        self.assertEqual((imported["id"], again["id"]), (row["id"], row["id"]))
        completions, unavailable = eventledger.checked_events(
            gateimport.fab_completions_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual(completions[0]["authority"]["budgets"],
                         {"queue_s": 60.0, "execution_s": 600.0})

    def test_direct_fab_import_rejects_receipt_authority_mismatches(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        mutations = (
            {"host": {"node": "other", "system": "Linux",
                      "release": "1", "id": "machine"}},
            {"suite": False}, {"dirty": True}, {"dirty_after": True},
            {"status": "FAILED"},
        )
        errors = []
        for mutation in mutations:
            row, artifact = self.receipt(**mutation)
            with open(artifact, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            authority = self.authority(
                job, launched["handle"], "/remote/gate-receipts.jsonl")
            authority["artifact_sha256"] = digest
            imported, verdict, import_err = gateimport.import_fab_receipt(
                artifact, self.repo, authority, want_id=row["id"])
            errors.append((imported, verdict, import_err))

        self.assertEqual([error for _row, _verdict, error in errors], [
            "Fab receipt host differs from the completion authority",
            "Fab receipt suite differs from the completion authority",
            "Fab receipt dirty differs from the completion authority",
            "Fab receipt dirty_after differs from the completion authority",
            "Fab receipt status differs from the completion authority",
        ])
        self.assertTrue(  # noqa: VACUOUS_ASSERTION — five exact authority errors are the positive controls; none may mint a receipt
            all(imported is None and verdict is None
                for imported, verdict, _err in errors))
        self._assert_no_authority()

    def test_direct_fab_import_rejects_an_artifact_the_authority_did_not_name(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the first import in this arm, the SAME artifact under an authority naming its real digest, asserted "imported" with the exact receipt id; proving a digest check is live necessarily takes a second call, because one call cannot both match and mismatch
        """The one check that binds the completion authority to actual BYTES.

        Every other authority comparison in this file is field-against-field:
        host, suite, dirty, status, key, tree, sha, generation, identity. This
        one is different in kind — gateimport compares the sha256 of the file
        it is about to import against the digest the authority carries — and it
        is the whole reason a fetched artifact cannot be swapped between the
        moment the remote authority is read and the moment bytes are imported.

        THE POSITIVE RUNS FIRST AND ON THE SAME ARTIFACT, so the refusal below
        is attributable to the digest and not to anything else about the
        fixture: the identical receipt imports cleanly when the authority names
        its real digest.
        """
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        with open(artifact, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()

        honest = self.authority(
            job, launched["handle"], "/remote/gate-receipts.jsonl")
        honest["artifact_sha256"] = digest
        imported, verdict, import_err = gateimport.import_fab_receipt(
            artifact, self.repo, honest, want_id=row["id"])
        self.assertIsNone(import_err)
        self.assertEqual((verdict, imported["id"]), ("imported", row["id"]))

        # AND THE SAME BYTES UNDER AN AUTHORITY THAT NAMES A DIFFERENT DIGEST.
        # A valid-looking 64-hex value, because a malformed one is refused by
        # an EARLIER shape check and would prove nothing about this comparison.
        forged = self.authority(
            job, launched["handle"], "/remote/gate-receipts.jsonl")
        forged["artifact_sha256"] = "a" * 64
        self.assertNotEqual(forged["artifact_sha256"], digest)
        again, again_verdict, again_err = gateimport.import_fab_receipt(
            artifact, self.repo, forged, want_id=row["id"])
        self.assertEqual(
            again_err,
            "Fab completion artifact digest disagrees with the authority")
        self.assertEqual((again, again_verdict), (None, None))

    def test_direct_fab_import_accepts_valid_cached_receipt_version(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt(v=gate.CACHED_VERSION, executed=True)
        with open(artifact, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        authority = self.authority(
            job, launched["handle"], "/remote/gate-receipts.jsonl")
        authority["artifact_sha256"] = digest

        imported, verdict, import_err = gateimport.import_fab_receipt(
            artifact, self.repo, authority, want_id=row["id"])

        self.assertIsNone(import_err)  # noqa: VACUOUS_ASSERTION — exact imported receipt and verdict prove the valid cached kind crossed the Fab authority seam
        self.assertEqual((imported["id"], verdict), (row["id"], "imported"))

    def test_fab_completion_id_hashes_surrogates_deterministically(self):
        row = {"v": 1, "event": "control", "value": "\udcff"}
        self.assertEqual(gateimport._fab_completion_id(row),
                         gateimport._fab_completion_id(copy.deepcopy(row)))
        self.assertEqual(len(gateimport._fab_completion_id(row)), 64)

    def test_direct_fab_import_rejects_self_hashed_non_v2_identity(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        row, artifact = self.receipt()
        errors = []
        for mutate in (
                lambda identity: identity["runner"].update(
                    {"format": "other-runner"}),
                lambda identity: identity.update(
                    {"repository": {"common_dir": "relative/repo"}}),
                lambda identity: identity.update(
                    {"scope": {"kind": "whole", "argv": ["wrong"]}})):
            authority = self.authority(
                job, launched["handle"], "/remote/gate-receipts.jsonl")
            mutate(authority["identity"])
            body = json.dumps(authority["identity"], ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"),
                              allow_nan=False)
            authority["key"] = hashlib.sha256(
                body.encode("utf-8", "surrogatepass")).hexdigest()
            authority["job_id"] = "gate-" + authority["key"]
            imported, verdict, err = gateimport.import_fab_receipt(
                artifact, self.repo, authority, want_id=row["id"])
            errors.append((imported, verdict, err))

        self.assertEqual([error for _row, _verdict, error in errors], [
            "Fab completion runner identity is malformed",
            "Fab completion repository identity is malformed",
            "Fab completion scope identity is malformed"])
        self.assertTrue(all(imported is None and verdict is None
                            for imported, verdict, _err in errors))
        receipts, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual(receipts, [])  # noqa: VACUOUS_ASSERTION — three exact malformed-identity errors are the positive controls; direct import must mint no generic receipt

    def test_existing_generic_binding_repairs_with_separate_fab_completion(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual(launched["disposition"], "LAUNCHED")
        row, artifact = self.receipt()
        imported, verdict, err = gateimport.import_receipt(
            artifact, self.repo, want_id=row["id"])
        self.assertIsNone(err)
        self.assertTrue(imported["id"])
        self.assertEqual(imported["id"], row["id"])
        self.assertEqual(verdict, "imported")
        builder.complete(job, artifact, receipt=row["id"])

        recovered, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)

        self.assertIsNone(err)
        self.assertEqual(recovered["verdict"], "repaired")
        bindings, unavailable = eventledger.checked_events(
            gateimport.bindings_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual([binding["v"] for binding in bindings], [1])
        completions, unavailable = eventledger.checked_events(
            gateimport.fab_completions_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual([event["receipt"] for event in completions], [row["id"]])

    def test_remote_completion_without_follower_reconciles_later_once(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual(launched["disposition"], "LAUNCHED")
        row, artifact = self.receipt()
        builder.complete(job, artifact, receipt=row["id"])

        recovered, err = fabgate.submit_and_follow(builder, job, self.repo)
        self.assertIsNone(err)
        self.assertEqual(recovered,
                         {"state": "COMPLETED", "exit": 0, "receipt": row["id"],
                          "verdict": "imported"})
        again, err = fabgate.submit_and_follow(builder, job, self.repo)
        self.assertIsNone(err)
        self.assertEqual(again,
                         {"state": "COMPLETED", "exit": 0, "receipt": row["id"],
                          "verdict": "duplicate"})
        receipts, unavailable, _skipped = gate.receipts()
        self.assertIsNone(unavailable)
        self.assertEqual([receipt["id"] for receipt in receipts], [row["id"]])
        bindings, unavailable = eventledger.checked_events(
            gateimport.bindings_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["v"], gateimport.BINDING_VERSION)
        completions, unavailable = eventledger.checked_events(
            gateimport.fab_completions_path(), strict=True)
        self.assertIsNone(unavailable)
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]["authority"]["key"], job["key"])
        self.assertEqual(completions[0]["authority"]["generation"],
                         launched["handle"]["generation"])
        self.assertEqual(completions[0]["authority"]["identity"]["runner"],
                         job["identity"]["runner"])
        self.assertEqual(completions[0]["authority"]["budgets"], job["budgets"])
        self.assertEqual(builder.launches, 1)

    def test_pre_admission_refusal_keeps_queue_state_and_exact_exit(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        builder.jobs[job["key"]]["snapshot"] = {
            "state": "COMPLETED", "exit": 94,
            "exit_class": "SCHEDULER_REFUSED", "artifact": None,
            "artifact_sha256": None, "receipt": None,
            "queue_elapsed_s": 1.0, "execution_elapsed_s": None,
            "live": False, "reason": "scheduler refused admission"}
        result, err = fabgate.reconcile(builder, job, launched["handle"], self.repo)
        self.assertEqual(result,
                         {"state": "COMPLETED", "exit": 94,
                          "receipt": None, "verdict": None})
        self.assertIn("unretrievable", err)
        self.assertNotIn("suite", err.lower())

    def test_unretrievable_artifact_keeps_exact_builder_exit(self):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        builder.complete(job, None, exit_code=99)
        result, err = fabgate.reconcile(builder, job, launched["handle"], self.repo)
        self.assertEqual(result,
                         {"state": "COMPLETED", "exit": 99,
                          "receipt": None, "verdict": None})
        self.assertIn("unretrievable", err)
        self.assertEqual(builder.launches, 1)


    def test_exit_3_that_names_a_receipt_is_not_NOT_RUN(self):
        """task/1740 P3: NOT RUN needs the receipt ABSENT, not merely no
        artifact. A completion that names a receipt id ran a suite whatever its
        exit, so its missing artifact is the ordinary unretrievable case, exit
        1 through the seam — never "no receipt exists". The arm below is the
        control: the same exit with the receipt ABSENT is NOT RUN."""
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        builder.complete(job, None, receipt="0123456789abcdef",
                         exit_code=gate.EXIT_NOT_RUN_CAPACITY)
        result, err = fabgate.reconcile(builder, job, launched["handle"],
                                        self.repo)
        self.assertEqual(result, {"state": "COMPLETED", "exit": 3,
                                  "receipt": None, "verdict": None})
        self.assertNotIsInstance(err, gate.CapacityRefusal)
        self.assertIn("unretrievable", err)
        self.assertNotIn("NOT RUN", err)

    def test_a_node_capacity_refusal_is_NOT_RUN_and_exits_3(self):
        """task/1740 through the durable job: the spoke passes helm's own exit,
        and exit 3 with no artifact is a node that refused to run the suite —
        NOT RUN, no receipt owed — where every other artifactless exit stays
        "unretrievable" (the exit-99 arm above is that control). The
        production seam `gate fab reconcile --stdin` exits 3 on it."""
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        builder.complete(job, None, exit_code=gate.EXIT_NOT_RUN_CAPACITY)
        result, err = fabgate.reconcile(builder, job, launched["handle"],
                                        self.repo)
        self.assertEqual(result, {"state": "COMPLETED", "exit": 3,
                                  "receipt": None, "verdict": None})
        self.assertIsInstance(err, gate.CapacityRefusal)
        self.assertIn("NOT RUN", err)
        self.assertIn("no receipt exists", err)
        envelope = {"request": job, "handle": launched["handle"],
                    "observed": builder.observe(launched["handle"]),
                    "fetched": builder.fetch_receipt(launched["handle"])}
        stdin = io.StringIO(json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(fabgate.sys, "stdin", stdin), \
                mock.patch.object(fabgate.sys, "stdout", stdout), \
                mock.patch.object(fabgate.sys, "stderr", stderr):
            rc = gate._gate_dispatch(
                ["fab", "reconcile", "--repo", self.repo, "--stdin"])
        self.assertEqual(rc, gate.EXIT_NOT_RUN_CAPACITY)
        self.assertIn("NOT RUN", stderr.getvalue())
        self.assertNotIn("REFUSED", stderr.getvalue())
        self.assertEqual(builder.launches, 1)

class NodeMachineIdTest(FabGateBase):
    """One box, two names: a Fab alias on the handle, its own uname on the
    receipt. The gate-job event may carry the node's machine-id hash, and
    once it does, the receipt's `host.id` is compared with it and the hostname
    never decides. Without it, the hostname rung is exactly what it was."""

    ID_A = "0123456789abcdef"
    ID_B = "fedcba9876543210"

    def host(self, node, ident):
        return {"node": node, "system": "Linux", "release": "1", "id": ident}

    def reconcile(self, host, event_extra=None):
        builder, job = FakeBuilder(), self.request()
        launched, err = fabgate.submit(builder, job)
        self.assertIsNone(err)
        self.assertEqual(launched["handle"]["host"], "snoozy")
        row, artifact = self.receipt(host=host)
        builder.complete(job, artifact, receipt=row["id"])
        builder.event_extra.update(event_extra or {})
        result, err = fabgate.reconcile(
            builder, job, launched["handle"], self.repo)
        return builder, job, launched["handle"], row, result, err

    def completions(self):
        rows, unavailable = eventledger.checked_events(
            gateimport.fab_completions_path(), strict=True)
        self.assertIsNone(unavailable)
        return rows

    def test_an_event_without_node_id_imports_exactly_as_before(self):  # noqa: VACUOUS_ASSERTION — the imported verdict and exact receipt id are asserted unconditionally; the absent node_id key is the contract under test
        _b, _j, _h, row, result, err = self.reconcile(
            self.host("snoozy", self.ID_B))

        self.assertIsNone(err)  # noqa: VACUOUS_ASSERTION — the imported verdict and exact receipt id below are the positive evidence
        self.assertEqual((result["verdict"], result["receipt"]),
                         ("imported", row["id"]))
        self.assertNotIn("node_id", self.completions()[0]["authority"])

    def test_an_event_without_node_id_keeps_the_hostname_rung(self):
        _b, _j, _h, _row, result, err = self.reconcile(
            self.host("snoozy-am5", self.ID_A))

        self.assertIsNone(result["receipt"])
        self.assertEqual(
            err, "Fab receipt host differs from the completion authority")
        ReconciliationTest._assert_no_authority(self)

    def test_a_matching_machine_id_imports_under_a_second_hostname(self):
        builder, job, handle, row, result, err = self.reconcile(
            self.host("snoozy-am5", self.ID_A), {"node_id": self.ID_A})

        self.assertIsNone(err)  # noqa: VACUOUS_ASSERTION — the imported verdict and exact receipt id below are the positive evidence
        self.assertEqual((result["verdict"], result["receipt"]),
                         ("imported", row["id"]))
        self.assertEqual(self.completions()[0]["authority"]["node_id"],
                         self.ID_A)
        again, err = fabgate.reconcile(builder, job, handle, self.repo)
        self.assertIsNone(err)  # noqa: VACUOUS_ASSERTION — the duplicate verdict below proves the stored node_id completion re-validates
        self.assertEqual((again["verdict"], again["receipt"]),
                         ("duplicate", row["id"]))
        self.assertEqual(len(self.completions()), 1)

    def test_a_different_machine_id_refuses_and_names_both_hashes(self):  # noqa: VACUOUS_ASSERTION — test_a_matching_machine_id_imports_under_a_second_hostname is the positive control on the same fixture and reconcile path
        _b, _j, _h, _row, result, err = self.reconcile(
            self.host("snoozy-am5", self.ID_B), {"node_id": self.ID_A})

        self.assertIsNone(result["receipt"])
        self.assertEqual(err, (
            "Fab receipt machine id %s differs from the dispatched node's "
            "machine id %s" % (self.ID_B, self.ID_A)))
        ReconciliationTest._assert_no_authority(self)

    def test_a_matching_hostname_never_decides_once_the_id_is_present(self):
        _b, _j, _h, _row, result, err = self.reconcile(
            self.host("snoozy", self.ID_B), {"node_id": self.ID_A})

        self.assertIsNone(result["receipt"])
        self.assertIn("machine id %s differs" % self.ID_B, err)
        ReconciliationTest._assert_no_authority(self)

    def test_a_receipt_without_a_machine_id_cannot_match_a_present_one(self):
        _b, _j, _h, _row, result, err = self.reconcile(
            self.host("snoozy", ""), {"node_id": self.ID_A})

        self.assertIsNone(result["receipt"])
        self.assertIn("machine id absent differs", err)
        ReconciliationTest._assert_no_authority(self)

    def test_a_malformed_node_id_refuses_and_is_never_ignored(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts one exact refusal string; test_a_matching_machine_id_imports_under_a_second_hostname is the positive control
        for bad in ("0123456789ABCDEF", "0123", self.ID_A + "0", "",
                    None, 1234567890123456, ["x"]):
            with self.subTest(bad=bad):
                self.tearDown()
                self.setUp()
                _b, _j, _h, _row, result, err = self.reconcile(
                    self.host("snoozy", self.ID_A), {"node_id": bad})
                self.assertIsNone(result)
                self.assertEqual(err, "Fab observation node_id is not a "
                                      "16-hex machine-id hash")
                ReconciliationTest._assert_no_authority(self)

    def test_an_unknown_field_is_still_refused_with_or_without_node_id(self):  # noqa: VACUOUS_ASSERTION — every subTest asserts one exact refusal string; test_a_matching_machine_id_imports_under_a_second_hostname is the positive control
        for extra in ({"node_name": "snoozy-am5"},
                      {"node_id": self.ID_A, "node_name": "snoozy-am5"}):
            with self.subTest(extra=sorted(extra)):
                self.tearDown()
                self.setUp()
                _b, _j, _h, _row, result, err = self.reconcile(
                    self.host("snoozy", self.ID_A), extra)
                self.assertIsNone(result)
                self.assertEqual(err, "Fab observation is not the exact v2 "
                                      "gate-job event")
                ReconciliationTest._assert_no_authority(self)

    def test_the_direct_authority_admits_node_id_and_nothing_wider(self):  # noqa: VACUOUS_ASSERTION — the final unconditional import of the same artifact with a valid node_id is the positive control for the three refusals
        job = self.request()
        launched, err = fabgate.submit(FakeBuilder(), job)
        self.assertIsNone(err)
        row, artifact = self.receipt(host=self.host("snoozy-am5", self.ID_A))
        with open(artifact, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        cases = (({"node_id": "not-hex"},
                  "Fab completion node_id is not a 16-hex machine-id hash"),
                 ({"node_id": self.ID_A, "node_name": "snoozy-am5"},
                  "Fab completion authority is not the exact v2 shape"),
                 ({"node_name": "snoozy-am5"},
                  "Fab completion authority is not the exact v2 shape"))
        for extra, want in cases:
            authority = self.authority(
                job, launched["handle"], "/remote/gate-receipts.jsonl")
            authority["artifact_sha256"] = digest
            authority.update(extra)
            imported, verdict, err = gateimport.import_fab_receipt(
                artifact, self.repo, authority, want_id=row["id"])
            self.assertEqual((imported, verdict, err), (None, None, want))
        ReconciliationTest._assert_no_authority(self)
        authority = self.authority(
            job, launched["handle"], "/remote/gate-receipts.jsonl")
        authority["artifact_sha256"] = digest
        authority["node_id"] = self.ID_A
        imported, verdict, err = gateimport.import_fab_receipt(
            artifact, self.repo, authority, want_id=row["id"])
        self.assertIsNone(err)  # noqa: VACUOUS_ASSERTION — the imported verdict and id below are the positive control for the three refusals
        self.assertEqual((verdict, imported["id"]), ("imported", row["id"]))


if __name__ == "__main__":
    unittest.main()
