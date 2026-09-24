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

# EVERY transport key this module can read, scrubbed in setUp. Not tidiness:
# the owner's real Telegram token is exported into any seat's shell, and an
# unscrubbed key would make these arms POST to his actual phone during a gate
# while still passing — a test reading the live system and calling it a fixture.
TRANSPORT_KEYS = TOPIC_KEYS + (
    "HELM_TELEGRAM_TOKEN", "MELD_TELEGRAM_TOKEN",
    "HELM_TELEGRAM_CHAT_ID", "MELD_TELEGRAM_CHAT_ID",
)


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class NotifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-notify-")
        self.prior = {k: os.environ.get(k)
                      for k in TRANSPORT_KEYS + ("HELM_HOME", "MELD_HOME")}
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


class TwoTransportsOneDoorTest(NotifyBase):
    """One fan-out point, two transports. The votes are the subtle part: an
    opt-out and a delivery both look like True at a leg, so summing them
    naively reports success for a push that reached nobody."""

    def tg(self, delivered=True, configured=True):
        return mock.patch.multiple(
            "helm.telegram",
            configured=mock.Mock(return_value=configured),
            owner_send=mock.Mock(return_value=delivered))

    def test_a_configured_push_reaches_BOTH_transports(self):
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=Response()) as urlopen, self.tg():
            from helm import telegram
            self.assertTrue(notify.owner_push("body", title="t"))
            self.assertEqual(telegram.owner_send.call_count, 1)
        self.assertEqual(urlopen.call_count, 1,
                         "adding a transport must not cost the original one")

    def test_an_UNCONFIGURED_leg_does_NOT_vote_a_failed_push_to_success(self):
        # The whole reason the votes list holds only CONFIGURED legs. With a
        # naive any() over [False, True-because-absent] this returns True and
        # the caller's outbox drops an edge that never reached him.
        #
        # The control is the SAME failing ntfy with the telegram leg PRESENT
        # and delivering, so True-vs-False here turns on the absent leg alone
        # and not on the failure being detected at all.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("no route")), self.tg():
            self.assertTrue(notify.owner_push("body"),
                            "control: a present, delivering leg must carry it")
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("no route")), \
                self.tg(configured=False):
            self.assertFalse(notify.owner_push("body"))

    def test_EITHER_transport_delivering_is_delivery(self):
        # He was reached. Which wire carried it is not the caller's question,
        # and re-arming the outbox would buzz him twice for one event.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           side_effect=OSError("no route")), self.tg():
            self.assertTrue(notify.owner_push("body"))

    def test_telegram_ALONE_makes_the_channel_configured(self):
        # A caller stamping a card NOT PUSHED because the ntfy topic is unset,
        # while Telegram put it in his hand, would be wrong in the direction
        # nobody re-checks.
        self.assertFalse(notify.configured(), "control: nothing configured yet")
        with self.tg():
            self.assertTrue(notify.configured())
            self.assertTrue(notify.owner_push("body"))

    def test_the_reply_key_rides_the_transport_that_HAS_an_inbound_leg(self):
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=Response()) as urlopen, self.tg():
            from helm import telegram
            notify.owner_push("q", reply_key="seat-under-test")
            self.assertEqual(telegram.owner_send.call_args[1]["reply_key"],
                             "seat-under-test")
        # ntfy has no inbound leg; the key must not leak into its payload,
        # where it would be a routing promise the channel cannot keep. The
        # equality is the positive control on the same observable — it proves
        # the payload was read at all, so the absence below is about a body
        # that exists rather than about a None nobody looked at.
        self.assertEqual(urlopen.call_args[0][0].data, b"q")
        self.assertNotIn(b"seat-under-test", urlopen.call_args[0][0].data)

    def test_a_BROKEN_telegram_module_never_breaks_a_push(self):
        # Fail-open is the founding law of this module: a notifier must never
        # break the verb it reports on, and that includes breaking on its own
        # import or on a transport raising instead of returning.
        with mock.patch.dict(os.environ, {"HELM_NTFY_TOPIC": "helm-fixture"}), \
                mock.patch("urllib.request.urlopen",
                           return_value=Response()), \
                mock.patch("helm.telegram.configured",
                           side_effect=RuntimeError("boom")):
            self.assertTrue(notify.owner_push("body"))


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
        rep = {"seats": [], "covered": [], "deaf": [], "deaf_in_effect": [],
               "vacant": [],
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

    # The two spellings of "reach for the Telegram transport". A module that
    # imports it is a module that can push to the owner behind notify's back.
    TRANSPORT_MARKS = ("from . import telegram", "from helm import telegram")

    def _files_containing(self, marks):
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
                if any(m in src for m in marks):
                    hits.append(os.path.relpath(path, root))
        return sorted(hits)

    def test_there_is_exactly_ONE_phone_path_in_the_tree(self):
        """The guard, and its own MUST-HIT: `notify.py` MUST appear, so an
        empty result fails here instead of reading as a clean bill."""
        hits = self._files_containing(self.ENDPOINT_MARKS)
        self.assertIn("notify.py", hits,
                      "the scan found no endpoint resolution at all — it is "
                      "broken, and its empty result would read as a clean bill")
        self.assertEqual(hits, ["notify.py"],
                         "a SECOND owner-push path exists")

    def test_only_notify_may_reach_the_telegram_transport(self):
        """The canon is COMPOSE, DON'T PARALLEL, and adding a transport is
        exactly when that gets violated — the second wire is useful, so a
        caller wanting the phone reaches it directly and quietly acquires a
        push path that bypasses notify's fail-open law, its receipts, and its
        opt-out. TRANSPORTS may multiply behind the door; PATHS to the door
        may not. Same MUST-HIT discipline: notify.py has to be in this set or
        the scan is measuring nothing."""
        hits = self._files_containing(self.TRANSPORT_MARKS)
        self.assertIn("notify.py", hits,
                      "the scan found no importer at all — it is broken, and "
                      "an empty result here would read as a clean bill")
        self.assertEqual(hits, ["notify.py"],
                         "a module other than notify reaches the Telegram "
                         "transport directly, bypassing the one door")


if __name__ == "__main__":
    unittest.main()
