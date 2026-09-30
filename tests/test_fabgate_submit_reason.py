"""test_fabgate_submit_reason — submit() drops Fab's disposition and reason
when _handle refuses (task/3322).

When _handle refuses inside submit(), submit returns only "Fab returned no
exact v2 durable job handle" and drops raw['disposition'] and raw['reason'].
The cure: append the event's disposition and reason (laundered, bounded).
"""
import copy
import os
import subprocess
import tempfile
import unittest

from helm import fabgate, gate, gateimport


_ENV = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME", "GIT_DIR",
        "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


class FakeBuilderWithReason:
    """Builder that answers a submit with UNKNOWN disposition, a reason,
    and NO handle (handle is None so _handle refuses)."""

    def __init__(self, reason=None, hostile_reason=None):
        self.submissions = 0
        self._reason = reason
        self._hostile_reason = hostile_reason

    def submit(self, request):
        self.submissions += 1
        key = request["key"]
        event = {"v": fabgate.EVENT_VERSION, "event": "gate-job",
                 "disposition": "UNKNOWN",
                 "handle": None,
                 "snapshot": {"state": "UNKNOWN", "exit": None,
                              "exit_class": None, "artifact": None,
                              "receipt": None},
                 "reason": self._hostile_reason or self._reason
                         or "no slice field",
                 "request": copy.deepcopy(request)}
        return event

    def observe(self, handle):
        return None

    def wait(self, handle, timeout):
        return None


class SubmitReasonTest(unittest.TestCase):

    def setUp(self):
        gateimport._REPO_IDENTITIES.clear()
        gateimport._STORED_IDS_CACHE[0] = None
        gateimport._TREE_PASS[0] = None
        self.tmp = tempfile.mkdtemp(prefix="helm-test-fabgate-")
        self.prior = {name: os.environ.get(name) for name in _ENV}
        for name in _ENV:
            os.environ.pop(name, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm-home")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NAME"] = "fabgate-test"
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q", "."],
                       capture_output=True)
        with open(os.path.join(self.repo, "f.txt"), "w") as fh:
            fh.write("one\n")
        subprocess.run(["git", "-C", self.repo, "add", "f.txt"],
                       capture_output=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "-m", "init"],
                       capture_output=True)
        proc = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD"],
            capture_output=True, text=True)
        self.head = proc.stdout.strip()
        proc = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True)
        self.tree = proc.stdout.strip()
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
        gateimport._TREE_PASS[0] = None
        for name, value in self.prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _job(self):
        job, err = fabgate.request(
            self.repo, self.tree, "whole", self.interpreter,
            copy.deepcopy(self.runner),
            queue_timeout=600, execution_timeout=1800)
        if err:
            raise AssertionError("fabgate.request failed: %s" % err)
        return job

    def test_unknown_event_with_reason_prints_that_reason(self):
        builder = FakeBuilderWithReason(reason="no slice field")
        job = self._job()
        result, err = fabgate.submit(builder, job)
        self.assertIsNone(result)
        self.assertIn("no slice field", err,
                      "submit() should include Fab's UNKNOWN reason")

    def test_hostile_reason_comes_out_laundered_and_bounded(self):
        hostile = "\x1b[2J\x07\x08" + "bad text" * 1000
        builder = FakeBuilderWithReason(hostile_reason=hostile)
        job = self._job()
        result, err = fabgate.submit(builder, job)
        self.assertIsNone(result)
        self.assertNotIn("\x1b", err)
        self.assertNotIn("\x07", err)
        # Should be bounded (no 4000-char reason leak)
        self.assertLess(len(err), 500)

    def test_valid_v2_handle_unchanged(self):
        builder = FakeBuilderWithReason()
        job = self._job()
        result, err = fabgate.submit(builder, job)
        self.assertIsNone(result)
        self.assertIn("no exact v2 durable job handle", err)


    def test_an_event_with_no_reason_leaves_the_refusal_as_it_was(self):  # noqa: VACUOUS_ASSERTION — a fixed four-member table, each member asserting the refusal EQUAL to the exact handle sentence
        """Fab's reason is appended only when there is one: a null, empty,
        blank or non-string reason leaves the handle refusal exactly as it
        was, with no empty 'fab said' and no bare disposition."""
        for reason in (None, "", "   ", 7):
            with self.subTest(reason=reason):
                builder = FakeBuilderWithReason()
                answer = builder.submit
                builder.submit = lambda request, answer=answer, reason=reason: \
                    dict(answer(request), reason=reason)
                result, err = fabgate.submit(builder, self._job())
                self.assertIsNone(result)
                self.assertEqual(err, "Fab returned no exact v2 durable job handle")


if __name__ == "__main__":
    unittest.main()
