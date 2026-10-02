#!/usr/bin/env python3
"""The #seats room (task/3876): one seat event, two sinks, one steward woken.

Every arm runs under a temp HELM_HOME and a temp chat directory, and reads the
rows the real `chat.post` wrote there. The phone is an injected callable or a
mock of `notify.owner_push`; nothing reaches a network or the live fleet.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-seatevents-", var="HELM_HOME")

from helm import (burnflags, chat, home, localnames, proxywatch,  # noqa: E402
                  seatevents, seats_identity)

NOW = 1790000000.0
STEWARD = "creds-steward"
OPERATOR = "gpu-steward"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seatevents-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HELM_HOME": os.path.join(self.tmp, "home"),
            "HELM_CHAT_DIR": os.path.join(self.tmp, "chat"),
            "HELM_NTFY_TOPIC": "", "MELD_NTFY_TOPIC": ""})
        env.start()
        self.addCleanup(env.stop)
        self.plant({"cred-steward-seat": STEWARD,
                    "local-operator-seat": OPERATOR})
        self.ledger = os.path.join(self.tmp, "seat-events.json")
        self.pushes = []

    def plant(self, table):
        os.makedirs(home.global_dir(), exist_ok=True)
        with open(os.path.join(home.global_dir(), localnames.CONFIG), "w",
                  encoding="utf-8") as f:
            json.dump(table, f)
        localnames._cache["stat"] = None

    def push(self, body, title):
        self.pushes.append((body, title))
        return True

    def rows(self, room=seatevents.ROOM):
        path = chat.room_path(room)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def texts(self):
        return [r["text"] for r in self.rows()]

    def announce(self, *events, now=NOW, post=None):
        return seatevents.announce(list(events), now=now, post=post,
                                   push=self.push, path=self.ledger)

    @staticmethod
    def wall(kind="wall", ident="AUTH-401|t0", body="gemini dark AUTH-401",
             component="credentials", key="family:gemini"):
        return seatevents.event(component, key, kind, ident, body,
                                more="since t0", push=True,
                                title="helm upstream transition",
                                head="helm proxywatch: ")


class EpisodeTest(Base):
    def test_a_wall_posts_one_row_mentioning_its_steward_and_pushes_once(self):
        got = self.announce(self.wall())
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"],
                         "@creds-steward gemini dark AUTH-401 — since t0")
        self.assertEqual(rows[0]["from"], seatevents.WHO)
        self.assertEqual(self.pushes, [("helm proxywatch: gemini dark AUTH-401",
                                        "helm upstream transition")])
        self.assertEqual(got, {"posted": [rows[0]["text"]], "pushed": True,
                               "claimed": True})
        # THE MENTION IS THE WAKE: the steward's idle beacon takes this row in
        # a room it is not homed in; a bystander's does not.
        self.assertTrue(seats_identity.deliverable(
            rows[0], STEWARD, room=seatevents.ROOM, ambient=False,
            beacon=True))
        self.assertFalse(seats_identity.deliverable(
            rows[0], "bystander", room=seatevents.ROOM, ambient=False,
            beacon=True))

    def test_a_repeat_inside_the_episode_posts_nothing(self):  # noqa: VACUOUS_ASSERTION — the first call's one row and one push are the positive control on the same counts
        self.announce(self.wall())
        self.assertEqual((len(self.rows()), len(self.pushes)), (1, 1))
        self.announce(self.wall())                        # re-offered
        self.announce(self.wall(ident="QUOTA-402|t5"))    # still walled
        self.assertEqual((len(self.rows()), len(self.pushes)), (1, 1))

    def test_the_unblock_posts_once_and_closes_the_episode(self):
        self.announce(self.wall())
        self.announce(self.wall(kind="unblock", ident="HEALTHY|t1",
                                body="gemini recovered HEALTHY"),
                      now=NOW + 60)
        # A recovery is FYI to its steward (task/4019): FYI_LEAD follows
        # the mention, and the phone push carries the bare body.
        self.assertEqual(self.texts()[1],
                         "@creds-steward FYI, nothing to do: gemini recovered "
                         "HEALTHY — since t0")
        self.assertEqual(self.pushes[1][0],
                         "helm proxywatch: gemini recovered HEALTHY")
        self.announce(self.wall(kind="unblock", ident="HEALTHY|t1",
                                body="gemini recovered HEALTHY"),
                      now=NOW + 120)
        self.assertEqual((len(self.rows()), len(self.pushes)), (2, 2))
        # CLOSED: the next wall on the same family is a new episode
        self.announce(self.wall(ident="AUTH-401|t9"), now=NOW + 180)
        self.assertEqual((len(self.rows()), len(self.pushes)), (3, 3))

    def test_an_unknown_component_posts_without_a_mention(self):
        self.announce(self.wall(component="gpu-fans", key="fans"))
        text = self.texts()[0]
        self.assertNotIn("@", text)
        self.assertIn("(no steward woken: 'gpu-fans' is not a component in "
                      "seatevents.STEWARDS; the owner names its steward "
                      "there)", text)
        self.assertEqual(len(self.pushes), 1, "the phone still hears it")

    def test_an_undeclared_steward_posts_without_a_mention_and_says_where(self):
        self.plant({})
        self.assertEqual(seatevents.steward("credentials")[0], None)
        self.announce(self.wall())
        text = self.texts()[0]
        self.assertNotIn("@", text)
        self.assertIn("local-names 'cred-steward-seat' is unset", text)
        # THE CONTROL: declared, the same event mentions its seat
        self.plant({"cred-steward-seat": STEWARD})
        self.assertEqual(seatevents.steward("credentials"), (STEWARD, None))

    def test_a_failed_room_post_is_retried_alone(self):  # noqa: VACUOUS_ASSERTION — counts are pinned after each call; the retry posts exactly once
        def down(_text, _event_id=None):
            raise OSError("chat node down")
        got = self.announce(self.wall(), post=down)
        self.assertEqual((len(self.rows()), len(self.pushes)), (0, 1))
        self.assertTrue(got["pushed"])
        self.announce(now=NOW + 60)
        self.assertEqual((len(self.rows()), len(self.pushes)), (1, 1),
                         "the row is retried and the push is not")

    def test_a_wall_and_its_unblock_in_one_call_post_in_claim_order(self):
        """task/3876 cure R2: the rows post in the order they were claimed,
        never sorted by kind ("unblock" sorts before "wall")."""
        self.announce(self.wall(),
                      self.wall(kind="unblock", ident="HEALTHY|t1",
                                body="gemini recovered HEALTHY"))
        self.assertEqual(self.texts(), [
            "@creds-steward gemini dark AUTH-401 — since t0",
            "@creds-steward FYI, nothing to do: gemini recovered HEALTHY — "
            "since t0"])
        self.assertEqual(self.pushes[0][0], "helm proxywatch: gemini dark "
                         "AUTH-401; gemini recovered HEALTHY")

    def test_a_crash_after_the_post_does_not_post_the_row_twice(self):
        """task/3876 cure R1: the ledger write after delivery fails (the
        process dies there), so the next call still owes the row. The chat
        door's event id makes that retry return the first row."""
        def die(_body, _title):
            raise SystemExit("killed after the post, before the ledger write")
        with self.assertRaises(SystemExit):
            seatevents.announce([self.wall()], now=NOW, push=die,
                                path=self.ledger)
        self.assertEqual(len(self.rows()), 1)       # it did post
        self.announce(now=NOW + 60)                 # the retry
        self.assertEqual(len(self.rows()), 1, self.texts())

    def test_a_failed_ledger_claim_reports_that_the_event_is_not_durable(self):
        blocked = os.path.join(self.tmp, "not-a-directory")
        with open(blocked, "w", encoding="utf-8") as f:
            f.write("blocked")
        got = seatevents.announce([self.wall()], now=NOW, push=self.push,
                                  path=os.path.join(blocked, "ledger.json"))
        self.assertEqual(got, {"posted": [], "pushed": False,
                               "claimed": False})
        self.assertEqual((self.rows(), self.pushes), ([], []))

    def test_a_failed_push_is_owed_and_retried_without_a_second_row(self):
        calls = []

        def phone(body, title):
            calls.append(body)
            return len(calls) > 1
        got = seatevents.announce([self.wall()], now=NOW, push=phone,
                                  path=self.ledger)
        self.assertFalse(got["pushed"])
        got = seatevents.announce([], now=NOW + 60, push=phone,
                                  path=self.ledger)
        self.assertTrue(got["pushed"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.rows()), 1)


class StewardTableTest(Base):
    def test_home_admitted_punctuation_steward_can_be_mentioned(self):  # noqa: VACUOUS_ASSERTION — each finite case asserts its exact positive @mention
        for name in (".steward", "_steward", "-steward"):
            self.assertEqual(home.validate_seat_arg(name), name)
            self.plant({"cred-steward-seat": name})
            self.assertEqual(seatevents.address([("credentials", None)]),
                             ("@%s " % name, ""))

    def test_a_project_seat_row_mentions_that_projects_lead(self):
        team = {"members": [{"seat": "acme-claude", "role": "lead"},
                            {"seat": "acme-codex", "role": "builder"}]}
        with mock.patch("helm.teams.read", return_value=team):
            self.assertEqual(seatevents.address([("project-seats", "acme"),
                                                 ("project-seats", "acme")]),
                             ("@acme-claude ", ""))
        with mock.patch("helm.teams.read", return_value={"members": []}):
            lead, tail = seatevents.address([("project-seats", "acme")])
        self.assertEqual(lead, "")
        self.assertIn("project acme's team names no lead", tail)

    def test_the_integrator_owns_build_lanes_and_the_operator_local_serving(self):
        with mock.patch("helm.seats_integrator.integrator_seat",
                        return_value=("lane-boss", None)):
            self.assertEqual(seatevents.steward("build-lanes"),
                             ("lane-boss", None))
        self.assertEqual(seatevents.steward("local-serving"),
                         (OPERATOR, None))


class DetectorTest(Base):
    """The detectors' own edges, end to end through the real chat door."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(proxywatch, "_state_path",
                              return_value=os.path.join(self.tmp, "pw.json"))
        p.start()
        self.addCleanup(p.stop)

    def edges(self, kind, state, local=None):
        out = [{"kind": kind, "family": "gemini", "state": state,
                "since": "2026-09-30T20:25:00Z", "detail": "HTTP 401"}]
        if local:
            out.append(dict(out[0], family=local))
        return out

    def test_every_family_edge_is_one_row_and_the_phone_push_is_their_twin(self):
        local = (burnflags.local_families() or (None,))[0]
        with mock.patch("helm.notify.owner_push",
                        return_value=True) as phone:
            self.assertTrue(proxywatch._owner_push(
                self.edges("family-dark", "AUTH-401", local)))
            self.assertTrue(proxywatch._owner_push(
                self.edges("family-dark", "AUTH-401", local)))  # re-offered
        texts = self.texts()
        self.assertEqual(len(texts), 2 if local else 1)
        self.assertEqual(phone.call_count, 1)
        body = phone.call_args.args[0]
        self.assertTrue(body.startswith("helm proxywatch: "), body)
        self.assertIn("gemini dark AUTH-401", body)
        self.assertIn("@creds-steward gemini dark AUTH-401 — since "
                      "2026-09-30T20:25:00Z; HTTP 401", texts)
        if local:
            self.assertIn("@gpu-steward %s dark AUTH-401 — since "
                          "2026-09-30T20:25:00Z; HTTP 401" % local, texts)
        # ONE FORMATTER, TWO SINKS: every clause of the push is a row
        for clause in body[len("helm proxywatch: "):].split("; "):
            self.assertTrue(any(clause in t for t in texts), clause)
        with mock.patch("helm.notify.owner_push",
                        return_value=True) as phone:
            proxywatch._owner_push(self.edges("family-recovered", "HEALTHY",
                                              local))
            proxywatch._owner_push([])
        texts = self.texts()
        self.assertEqual(len(texts), 4 if local else 2)
        self.assertTrue(any(t.startswith("@creds-steward FYI, nothing to do: "
                                         "gemini recovered HEALTHY")
                            for t in texts), texts)
        if local:
            self.assertTrue(any(t.startswith("@gpu-steward FYI, nothing to "
                                             "do: %s recovered HEALTHY" % local)
                                for t in texts), texts)
        self.assertEqual(phone.call_count, 1)

    def test_a_budget_crossing_is_one_row_for_the_credentials_steward(self):  # noqa: VACUOUS_ASSERTION — the exact one-row #seats list is the positive control; the empty #helm and the unpushed phone are the claims
        """task/3876 cure F1: the FAMILY-BUDGET-LOW line goes to #seats and
        @mentions the credentials steward, once, with no push."""
        line = "FAMILY-BUDGET-LOW codex: RED (7d 99% of a week)"
        with mock.patch("helm.notify.owner_push") as phone:
            proxywatch._seat_events_pass([], line, NOW)
            proxywatch._seat_events_pass([], None, NOW + 900)
        self.assertEqual(self.texts(), ["@creds-steward " + line])
        self.assertEqual(self.rows("helm"), [])
        phone.assert_not_called()

    def test_the_default_home_switch_is_one_row_and_no_push(self):  # noqa: VACUOUS_ASSERTION — the exact one-row list is the positive control; no push is the claim, and the family-edge arm above proves the same mock is reached when a push is owed
        reading = {"switch": {"at": NOW - 60, "from": "a", "to": "b"}}
        with mock.patch("helm.notify.owner_push") as phone:
            proxywatch._seat_switch(reading, NOW)
            proxywatch._seat_switch(reading, NOW + 900)
            proxywatch._seat_switch(
                {"switch": {"at": NOW - 3 * 3600}}, NOW)   # old news
        self.assertEqual(self.texts(), [
            "@creds-steward claudepace: the Claude home switched accounts "
            "at 14:12Z. Claude pool pace UNKNOWN: not measured."])
        phone.assert_not_called()


if __name__ == "__main__":
    unittest.main()
