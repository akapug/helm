#!/usr/bin/env python3
"""`helm store confirm` names who ratified, instead of claiming the owner did.

THE DEFECT. helm/store/cli.py printed "(owner-ratified)" as an UNCONDITIONAL
string literal and write.confirm() recorded `"by": "human"` in a prior's
evidence_log the same way, while neither has an actor check of ANY kind.
confirm()'s own docstring calls it "the owner/confirm gate that makes inferred
capture safe to leave on", so the safety property that justifies leaving
inferred capture switched on rests on a gate that checks nobody: any caller
running that verb writes the owner's authority into the ledger for an edit he
has never seen. The artifact asserts an authority nobody supplied.

WHAT THIS DOES NOT DO. Whether an agent-run confirm should be a WEAKER GRADE
than an owner-run one is the owner's call and stays open; `source` still flips
to "explicit" either way. This change only stops the false attribution — you
cannot decide who may ratify while the record cannot say who did.

WHERE THE ACTOR IS AND IS NOT KEPT: a prior's evidence_log and the events
journal, both asserted below. NOT on the entry itself — every `_WRITERS`
writer serializes an explicit field list, so a `confirmed_by` key would be
computed and dropped on write. That was measured by dogfooding this change and
the dead field was removed rather than shipped.
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import pk, seats, store  # noqa: E402
from helm.store import cli as store_cli  # noqa: E402

# EVERY VAR THESE ARMS ASSIGN IS ALSO RESTORED HERE, and the session ids are
# in the list because the arms SET them. A test file
# that assigns an env var and omits it from its own cleanup leaks that value
# to every test loaded afterwards, and the damage never appears in the file
# that caused it — it surfaces somewhere unrelated, under a name that looks
# like that file's own bug. `home.session_id()` reads all three spellings, so
# leaking any one of them would hand a later suite a session identity it never
# set, which for THIS subject — who is recorded as having acted — is the most
# expensive value in the process to get wrong.
ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR",
            "MELD_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            # `pk.event` falls back to HELM_ACTOR when no actor is passed, so
            # an arm below SETS it to prove the fallback is not what lands.
            "HELM_ACTOR")

TS = "2026-09-09T12:00:00Z"
EID = "a-synthetic-candidate-for-actor-arms"
SEAT = "seat-under-test"


def run_store(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = store.cmd_store(list(args))
    return rc, out.getvalue(), err.getvalue()


class ActorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-actor-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        for var, leaf in (("HELM_HOME", "helm"), ("HELM_ADOPTED_DIR", "adopted"),
                          ("HELM_CACHE_DIR", "cache"), ("HELM_CHAT_DIR", "chat")):
            os.environ[var] = os.path.join(self.tmp, leaf)
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        store.write_prior({"id": EID, "statement": "A CANDIDATE BELIEF",
                           "confidence": "0.60", "keywords": "alpha,beta",
                           "stated_ts": TS, "source": "inferred",
                           "status": "candidate"})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self):
        hits = [e for e in store.load_all() if e["id"] == EID]
        self.assertEqual(len(hits), 1, EID)
        return hits[0]

    def confirmed_receipt(self):
        log = self.entry().get("evidence_log") or []
        rows = [r for r in log if r.get("type") == "confirmed"]
        self.assertEqual(len(rows), 1, "expected exactly one confirm receipt")
        return rows[0]

    def _anon(self):
        """No declared name and no session: the state whose ONLY honest actor
        is none at all. Lives on the base because two classes must drive the
        SAME anonymity — a second copy is a second definition of the case
        under test."""
        for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                  "CODEX_SESSION_ID", "HELM_CHAT_NAME"):
            os.environ.pop(k, None)


class TheActorIsRecordedTest(ActorBase):
    def test_the_receipt_names_the_resolved_seat(self):  # noqa: VACUOUS_ASSERTION — the recorded actor is asserted EQUAL to the resolved seat name, which is an unconditional positive on the same field
        with mock.patch.object(store_cli, "_acting_actor", return_value=SEAT):
            rc, out, err = run_store(["confirm", EID, "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.confirmed_receipt()["by"], SEAT)
        self.assertIn(SEAT, out)

    def test_the_output_NEVER_claims_the_owner_ratified_it(self):
        # The literal this whole change exists to delete. Asserting its
        # ABSENCE is the point, so the arm above supplies the unconditional
        # positive control that the line is rendered at all.
        with mock.patch.object(store_cli, "_acting_actor", return_value=SEAT):
            _rc, out, _err = run_store(["confirm", EID, "--project", "p"])
        self.assertIn("CONFIRMED", out)
        self.assertNotIn("owner-ratified", out)

    def test_an_UNRESOLVABLE_actor_is_named_as_such_not_as_a_human(self):  # noqa: VACUOUS_ASSERTION — UNIDENTIFIED is asserted as a positive VALUE on the field, and the sibling arm asserts a real name lands in the same field through the same call
        with mock.patch.object(store_cli, "_acting_actor", return_value=None):
            rc, out, err = run_store(["confirm", EID, "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.confirmed_receipt()["by"], "UNIDENTIFIED")
        self.assertIn("UNIDENTIFIED", out)
        self.assertNotIn("owner-ratified", out)
        # "human" was the old hardcoded receipt value; it must not come back
        # by way of the unidentified path either.
        self.assertNotEqual(self.confirmed_receipt()["by"], "human")

    def test_the_actor_is_NOT_written_as_an_entry_field(self):  # noqa: VACUOUS_ASSERTION — the two surfaces that DO persist the actor are asserted populated in the sibling arms, so the entry-field absence is the documented boundary and not a dead writer
        # Every _WRITERS writer serializes an explicit field list, so setting
        # confirmed_by would be computed-then-dropped. This pins the decision
        # so a future change does not reintroduce a field the writer eats.
        with mock.patch.object(store_cli, "_acting_actor", return_value=SEAT):
            run_store(["confirm", EID, "--project", "p"])
        self.assertNotIn("confirmed_by", self.entry())
        # POSITIVE CONTROL, unconditional: the actor IS durable, elsewhere.
        self.assertEqual(self.confirmed_receipt()["by"], SEAT)


class TheRealResolverDecidesTest(ActorBase):
    """NO MOCKS. Every arm drives the real resolver against a real roster.

    The first cut of this file mocked `_acting_actor` itself, so every arm
    exercised my mapping and NONE exercised resolution — the arms could not
    have caught the fallthrough a real-store probe finds immediately. A stub cannot fail the way the thing
    it replaces fails.
    """

    SID = "a-synthetic-session-id"

    def plant(self, rows):
        pk.write_json(seats.roster_path(), rows)

    def test_ANONYMOUS_names_nobody(self):
        # No declared name, no session at all: nothing can be resolved and
        # nothing is invented.
        for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                  "CODEX_SESSION_ID", "HELM_CHAT_NAME"):
            os.environ.pop(k, None)
        self.assertIsNone(store_cli._acting_actor())
        # POSITIVE CONTROL on the same resolver, unconditional: it CAN name a
        # seat, so the None above is a decision and not a dead function.
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        self.assertEqual(store_cli._acting_actor(), "seat-a")

    def test_UNROSTERED_names_nobody_rather_than_a_minted_stranger(self):
        # A session that no roster row remembers. `acting_seat` would MINT a
        # name here — DERIVED, "a STRANGER IDENTITY THAT PASSES EVERY CHECK".
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        self.plant({"seat-a": {"session": "some-other-sid",
                               "sessions": ["some-other-sid"]}})
        self.assertIsNone(store_cli._acting_actor())
        # POSITIVE CONTROL: bind THIS session and the same call answers.
        self.plant({"seat-a": {"session": self.SID, "sessions": [self.SID]}})
        self.assertEqual(store_cli._acting_actor(), "seat-a")

    def test_a_CONFLICT_names_nobody(self):
        # Declared name and roster binding both answer and DISAGREE. There is
        # no fact here to record, so neither is picked.
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        self.plant({"seat-b": {"session": self.SID, "sessions": [self.SID]}})
        self.assertIsNone(store_cli._acting_actor())
        # POSITIVE CONTROL: make the two AGREE and the same call answers, so
        # the None above is the disagreement and not the fixture.
        self.plant({"seat-a": {"session": self.SID, "sessions": [self.SID]}})
        self.assertEqual(store_cli._acting_actor(), "seat-a")

    def test_DECLARED_is_an_actor(self):
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.plant({"seat-a": {"session": "sid-a", "sessions": ["sid-a"]}})
        self.assertEqual(store_cli._acting_actor(), "seat-a")

    def test_ROSTERED_is_an_actor(self):
        os.environ.pop("HELM_CHAT_NAME", None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.SID
        self.plant({"seat-a": {"session": self.SID, "sessions": [self.SID]}})
        self.assertEqual(store_cli._acting_actor(), "seat-a")


class TheAnonymousPathsKeepTheirMainBehaviourTest(ActorBase):
    """THE REGRESSION THIS RULE EXISTS FOR: an anonymous caller must not gain a
    minted name in a provenance field."""

    def test_an_anonymous_REVISE_still_records_a_null_author(self):
        # Main records `"by": null` here. The first cut stamped an agent name
        # minted from session and cwd, which is a FALSE IDENTITY — worse than
        # a false refusal, because nothing about it looks wrong.
        self._anon()
        store.write_prior({"id": "x-live", "statement": "LIVE ONE",
                           "confidence": "0.60", "keywords": "alpha",
                           "stated_ts": TS, "source": "human"})
        rc, _out, err = run_store(["revise", "x-live", "A CORRECTION",
                                   "--project", "p"])
        self.assertEqual(rc, 0, err)
        hits = [e for e in store.load_all() if e["id"] == "x-live"]
        staged = (hits[0].get("pending_revision") or {})
        self.assertEqual(staged.get("statement"), "A CORRECTION")
        self.assertFalse(staged.get("by"), "an anonymous revise gained a name")

    def test_an_anonymous_CONFIRM_records_UNIDENTIFIED(self):
        self._anon()
        rc, out, err = run_store(["confirm", EID, "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.confirmed_receipt()["by"], "UNIDENTIFIED")
        self.assertIn("UNIDENTIFIED", out)


class TheJournalCarriesTheActorTest(ActorBase):
    """The prose claimed journal coverage the bodies did not assert. This asserts the PERSISTED structured field, not the summary."""

    def _events(self):
        rows = []
        try:
            with open(pk.events_path(), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except OSError:
            pass
        return [r for r in rows if r.get("verb") == "store.confirm"]

    def test_the_persisted_event_actor_is_the_resolved_seat(self):
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        rc, _out, err = run_store(["confirm", EID, "--project", "p"])
        self.assertEqual(rc, 0, err)
        rows = self._events()
        self.assertEqual(len(rows), 1, "no store.confirm event was persisted")
        # THE STRUCTURED FIELD, not the summary string: recording the actor in
        # prose while leaving `actor=` on its old resolver is a value wired
        # partway, and the two halves then disagree.
        self.assertEqual(rows[0].get("actor"), "seat-a")
        self.assertIn("seat-a", rows[0].get("summary") or "")

    def test_an_ANONYMOUS_persisted_event_cannot_borrow_the_AMBIENT_actor(self):
        """THE PAIRED HALF, and without it the named arm above is passable by a
        regression. `pk.event` resolves `actor or os.environ["HELM_ACTOR"]`, so
        a change that stopped passing `actor=` would still put a plausible name
        on the named row — and would let an ANONYMOUS confirm publish whatever
        the environment happened to carry.

        The fallback is therefore SET, to a value nothing else could produce,
        and both the structured field and the summary must read UNIDENTIFIED
        anyway."""
        self._anon()
        os.environ["HELM_ACTOR"] = "ambient-fallback-must-not-land"
        rc, _out, err = run_store(["confirm", EID, "--project", "p"])
        self.assertEqual(rc, 0, err)
        rows = self._events()
        self.assertEqual(len(rows), 1, "no store.confirm event was persisted")
        self.assertEqual(rows[0].get("actor"), "UNIDENTIFIED")
        summary = rows[0].get("summary") or ""
        self.assertIn("UNIDENTIFIED", summary)
        # THE FALLBACK WAS LIVE AND STILL DID NOT LAND, which is what makes
        # this arm discriminating rather than a second spelling of the first.
        self.assertEqual(os.environ.get("HELM_ACTOR"),
                         "ambient-fallback-must-not-land")
        self.assertNotIn("ambient-fallback-must-not-land",
                         summary + str(rows[0].get("actor")))


class TheFixtureLeaksNothingTest(unittest.TestCase):
    """Every env var these arms ASSIGN must also be RESTORED by ENV_KEYS.

    Derived from this file's own source rather than listed by hand, so adding
    an assignment without extending the cleanup lands red HERE instead of in
    somebody else's suite."""

    def test_every_assigned_env_var_is_in_the_cleanup_tuple(self):
        with open(os.path.abspath(__file__), encoding="utf-8") as fh:
            src = fh.read()
        assigned = set(re.findall(r'os\.environ\["([A-Z_]+)"\]\s*=', src))
        assigned |= set(re.findall(r'os\.environ\.pop\("([A-Z_]+)"', src))
        self.assertIn("CLAUDE_CODE_SESSION_ID", assigned)   # scan works
        self.assertIn("HELM_CHAT_NAME", assigned)
        missing = assigned - set(ENV_KEYS)
        self.assertFalse(missing,
                         "assigned but never restored, so it LEAKS to every "
                         "later test: %s" % sorted(missing))


if __name__ == "__main__":
    unittest.main()
