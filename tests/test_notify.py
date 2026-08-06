#!/usr/bin/env python3
"""helm notify — THE one path off this box to the owner's phone.

Hermetic: `urllib.request.urlopen` is mocked in every test and no test ever
touches the network or names a real endpoint. The topics here are synthetic
fixtures; the real one is a capability that lives in an env var and appears
nowhere in this repo.

The load-bearing test in this file is the LAST one. helm now has three callers
that must reach the owner when the fleet cannot be reached, and the owner's
canon is COMPOSE, DON'T PARALLEL — a second notification path is the bug, not
the fix. So the tree is scanned for a second endpoint resolution and the scan
is the guard.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import notify, pk  # noqa: E402

TOPIC_KEYS = ("HELM_NTFY_TOPIC", "MELD_NTFY_TOPIC")


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class NotifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-notify-")
        self.prior = {k: os.environ.get(k)
                      for k in TOPIC_KEYS + ("HELM_HOME", "MELD_HOME")}
        for k in self.prior:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "home")
        os.makedirs(os.environ["HELM_HOME"], exist_ok=True)

    def tearDown(self):
        for k, v in self.prior.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class OwnerPushTest(NotifyBase):
    def test_a_bare_topic_resolves_and_POSTs_the_body(self):
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=Response()) as urlopen:
            self.assertTrue(notify.owner_push("seven seats unreachable",
                                              title="helm fleet"))
        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/helm-fixture")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, b"seven seats unreachable")
        self.assertEqual(req.get_header("Title"), "helm fleet")
        self.assertEqual(urlopen.call_args[1]["timeout"], notify.TIMEOUT_S)

    def test_a_full_url_is_used_as_is(self):
        with mock.patch.dict(os.environ,
                             {"HELM_NTFY_TOPIC": "https://push.example/x"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=Response()) as urlopen:
            self.assertTrue(notify.owner_push("body"))
        self.assertEqual(urlopen.call_args[0][0].full_url,
                         "https://push.example/x")

    def test_an_UNSET_topic_is_an_opt_out_and_touches_no_network(self):  # noqa: VACUOUS_ASSERTION — assertTrue(owner_push(...)) is the unconditional positive control (opt-out ACKNOWLEDGES the batch), and test_a_bare_topic_resolves_and_POSTs_the_body proves the same call DOES post when a topic exists
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("opted out")) as urlopen:
            self.assertTrue(notify.owner_push("body"))   # acknowledged
        urlopen.assert_not_called()
        self.assertFalse(notify.configured())

    def test_a_down_notifier_returns_False_and_journals_one_line(self):  # noqa: VACUOUS_ASSERTION — the assertTrue over the journal receipt is the unconditional positive control on the same failed push; False IS the outbox signal under test
        """False is the OUTBOX SIGNAL — the caller keeps the edge armed for
        at-least-once retry — and the miss is never silent."""
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("no route")):
            self.assertFalse(notify.owner_push(
                "body", receipt=("beacons.notify_failed", "fleet")))
        self.assertTrue(any(r.get("verb") == "beacons.notify_failed"
                            and r.get("target") == "fleet"
                            for r in pk.read_events(20)))

    def test_configured_answers_the_question_without_handing_over_the_value(self):  # noqa: VACUOUS_ASSERTION — assertTrue(notify.configured()) is the unconditional positive control on the same module surface; the assertNotIns are the capability-containment claim
        """The topic is a push capability for the owner's phone. `configured`
        is the whole public surface: nothing returns it to a caller that could
        print it into a log, an error string, or a chat room."""
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}):
            self.assertTrue(notify.configured())
        self.assertNotIn("topic", dir(notify))
        self.assertNotIn("endpoint", dir(notify))
        for name in dir(notify):
            if name.startswith("_"):
                continue
            self.assertNotIn("topic", name.lower())


class OnePhonePathTest(NotifyBase):
    """COMPOSE, DON'T PARALLEL (owner canon). A second notification path adds
    failure modes without the benefit, and the owner's channel reference is
    explicit: reuse it, never mint a parallel topic."""

    def test_proxywatchs_family_edges_go_through_the_shared_push(self):
        from helm import proxywatch
        transitions = [{"kind": "family-dark", "family": "codex",
                        "state": "AUTH-401"}]
        with mock.patch("helm.notify.owner_push", return_value=True) as push:
            self.assertTrue(proxywatch._owner_push(transitions))
        push.assert_called_once()
        self.assertIn("codex dark", push.call_args[0][0])

    def test_the_stores_graduation_push_goes_through_the_shared_push(self):
        from helm.store import write
        with mock.patch("helm.notify.owner_push", return_value=True) as push:
            write._notify_graduation("prior", "x-law")
        push.assert_called_once()
        self.assertIn("x-law", push.call_args[0][0])

    def test_beacons_reachability_alarm_goes_through_the_shared_push(self):
        from helm import beacons
        att = {"state": beacons.DEAF, "alarm": True, "alarmed": False,
               "pushed": False, "covered": None, "seen": None, "why": "x"}
        rep = {"seats": [], "covered": [], "deaf": [], "vacant": [],
               "unproven": [], "ghosts": [], "beacons": 0, "surplus": 0}
        with mock.patch("helm.notify.owner_push", return_value=True) as push, \
                mock.patch("helm.chat.post"), \
                mock.patch.object(beacons, "_ack_alerts"):
            out = beacons.escalate([("alpha", att, {})], rep)
        self.assertTrue(out["push"])
        push.assert_called_once()
        self.assertIn("alpha", push.call_args[0][0])

    # The two spellings of "resolve the owner's push endpoint". Prose naming
    # the env var does not match either — only code that actually resolves it.
    ENDPOINT_MARKS = ('home.env("NTFY_TOPIC")', "https://ntfy.sh/")

    def test_there_is_exactly_ONE_phone_path_in_the_tree(self):
        """The guard, and its own MUST-HIT: `notify.py` MUST appear, so an
        empty result fails here instead of reading as a clean bill."""
        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helm")
        hits = []
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as f:
                    src = f.read()
                if any(m in src for m in self.ENDPOINT_MARKS):
                    hits.append(os.path.relpath(path, root))
        self.assertEqual(sorted(hits), ["notify.py"],
                         "a SECOND owner-push path exists: %s" % sorted(hits))


if __name__ == "__main__":
    unittest.main()
