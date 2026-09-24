#!/usr/bin/env python3
"""A correction to LIVE canon records who read it, or records that nobody did.

THE DEFECT. `revise` stages a correction to an entry that is ALREADY FIRING
fleet-wide, and `confirm` lands it. Between those two verbs there was no door
asking whether anyone but the author had looked, and no field distinguishing a
reviewed correction from an unreviewed one. The graduation ladder guards the
CAPTURE lane — a candidate fires nothing until `xrev-clear` clears it — so the
SAFER operation had an attestation door and the riskier one had none.

WHY THIS DOES NOT REFUSE. `revise` keeps the entry serving its old statement
on purpose: demoting it to stage a correction takes canon dark exactly while
it is being corrected, which is worst for the safety entries most worth
correcting. A hard refusal at confirm is that same failure with the opposite
sign — it wedges the one actor awake to fix something dangerous. So a
SELF-CONFIRMED revision and an ATTESTED one become two different durable
observables instead, and the caller is told which it just made.

WHERE THE RECEIPT LIVES: the events journal summary, for every type. NOT on
the entry — each `_WRITERS` writer serializes an explicit field list, so a key
set at confirm time would be computed and dropped on write.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import pk, store  # noqa: E402
from helm.store import cli as store_cli  # noqa: E402
from helm.store import write as store_write  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "MELD_ADOPTED_DIR",
            "HELM_CACHE_DIR", "MELD_CACHE_DIR", "HELM_CHAT_DIR",
            "MELD_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "HELM_ACTOR")

TS = "2026-09-11T12:00:00Z"
LIVE_ID = "a-synthetic-live-entry-for-revision-arms"
CAND_ID = "a-synthetic-candidate-for-revision-arms"
AUTHOR = "seat-a"
READER = "seat-b"
OWNER = "seat-c"
OLD = "A LIVE BELIEF, SAID IMPRECISELY"
NEW = "A LIVE BELIEF, SAID CORRECTLY"


def run_store(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = store.cmd_store(list(args))
    return rc, out.getvalue(), err.getvalue()


class RevisionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-revision-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        for var, leaf in (("HELM_HOME", "helm"),
                          ("HELM_ADOPTED_DIR", "adopted"),
                          ("HELM_CACHE_DIR", "cache"),
                          ("HELM_CHAT_DIR", "chat")):
            os.environ[var] = os.path.join(self.tmp, leaf)
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        store.write_prior({"id": LIVE_ID, "statement": OLD,
                           "confidence": "0.90", "keywords": "alpha,beta",
                           "stated_ts": TS, "source": "explicit",
                           "status": "live"})
        store.write_prior({"id": CAND_ID, "statement": "A CANDIDATE BELIEF",
                           "confidence": "0.60", "keywords": "gamma,delta",
                           "stated_ts": TS, "source": "inferred",
                           "status": "candidate"})

    def tearDown(self):
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self, eid=LIVE_ID):
        hits = [e for e in store.load_all() if e["id"] == eid]
        self.assertEqual(len(hits), 1, eid)
        return hits[0]

    def stage(self, by=AUTHOR, stmt=NEW):
        with mock.patch.object(store_cli, "_acting_actor", return_value=by):
            rc, out, err = run_store(["revise", LIVE_ID, stmt, "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.entry().get("pending_revision") or {}),
                        "fixture: nothing was staged, so no arm below is "
                        "about a staged revision")
        return out

    def attest(self, by=READER):
        return run_store(["xrev-clear", LIVE_ID, "--by", by, "--project", "p"])

    def confirm(self, by=OWNER):
        with mock.patch.object(store_cli, "_acting_actor", return_value=by):
            return run_store(["confirm", LIVE_ID, "--project", "p"])

    def confirm_summaries(self):
        rows = []
        if os.path.exists(pk.events_path()):
            with open(pk.events_path(), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("verb") == "store.confirm":
                        rows.append(str(r.get("summary") or ""))
        return rows


class AnUnreadRevisionSaysSoTest(RevisionBase):

    def test_a_SELF_CONFIRMED_revision_LANDS_and_the_journal_records_that(self):
        # ONE ACTOR ON BOTH ENDS, WHICH IS WHAT THIS ARM'S NAME CLAIMS. A
        # fixture that stages as one seat and confirms as another is a
        # TWO-PARTY confirm, and pinning the lone-seat sentence against it
        # makes the arm read as coverage of the case it is hiding. That case
        # has its own arm below, with this one as its control.
        self.stage(by=OWNER)
        # UNCONDITIONAL POSITIVE ON THE SAME OBSERVABLE, inline rather than
        # inside the helper: the field this arm later asserts is EMPTY is
        # asserted PRESENT here first, so an arm that never staged anything
        # cannot pass by finding nothing.
        self.assertTrue(self.entry().get("pending_revision"))
        rc, out, err = self.confirm()
        self.assertEqual(rc, 0, err)
        # IT LANDED. The refusal-shaped alternative is the one this design
        # rejects, so the arm asserts the write happened before anything else.
        self.assertEqual(str(self.entry().get("statement") or "").strip(), NEW)
        # THE VALUE IS WHAT IS CONSUMED, NOT THE KEY. Each writer serializes
        # an explicit field list, so a popped key comes back as an explicit
        # null on the next read — asserting the key's ABSENCE would be an
        # assertion about the serializer rather than about the revision.
        self.assertFalse(self.entry().get("pending_revision"),
                         "the staged revision survived its own confirm and "
                         "would re-apply on the next one")
        said = out + err
        self.assertIn("SELF-CONFIRMED", said)
        self.assertIn("nobody else read it", said)
        self.assertIn(OWNER, said)
        summaries = [s for s in self.confirm_summaries() if "revised" in s]
        self.assertEqual(len(summaries), 1, self.confirm_summaries())
        self.assertIn("SELF-CONFIRMED", summaries[0])

    def test_a_confirm_BY_ANOTHER_HAND_is_not_reported_as_self_confirmation(self):
        """THE STAGING SLOT HOLDS ONE REVISION, so a second `revise` between
        another actor's revise and their confirm REPLACES what that confirm
        will land — and the confirmer ratifies canon they never read.

        MEASURED on the live store: two seats staged four seconds apart and
        the confirm landed the second text under the first confirmer's name.
        The clause reported that as SELF-CONFIRMED while NAMING BOTH SEATS, so
        the sentence that should have raised an alarm read as the ordinary
        lone-seat note and the data contradicting it was already inside it.

        THIS IS THE MORE DANGEROUS POLE, NOT THE SAFER ONE. A seat alone at
        night correcting a dangerous entry SHOULD confirm its own work; what
        must never happen is that a two-party confirm — where the confirmer
        may not have seen the bytes — is described with the one-party
        sentence.
        """
        self.stage(by=AUTHOR)
        rc, out, err = self.confirm(by=OWNER)
        self.assertEqual(rc, 0, err)
        said = out + err
        # IT STILL LANDS. A refusal here would take canon dark exactly while
        # it is being corrected, which is the trade this design already made.
        landed = str(self.entry().get("statement") or "").strip()
        self.assertTrue(landed,
                        "the entry carries no statement at all, so the "
                        "equality below would be about an empty record "
                        "rather than about what this verb wrote")
        self.assertEqual(landed, NEW,
                         "the confirm did not land the staged text")
        self.assertIn("CONFIRMED BY ANOTHER HAND", said,
                      "a two-party confirm is described with the lone-seat "
                      "sentence: %r" % said)
        self.assertIn(AUTHOR, said, "the clause does not name who STAGED it")
        self.assertIn(OWNER, said, "the clause does not name who CONFIRMED it")
        self.assertNotIn("nobody else read it", said,
                         "the clause claims nobody else was involved while "
                         "naming two actors")
        # UNCONDITIONAL CONTROL ON THE SAME OBSERVABLE: the identical door
        # with ONE actor on both ends still says SELF-CONFIRMED, so the
        # assertion above tracks the two-party case and is not a rename of
        # the clause for everyone.
        store.write_prior({"id": LIVE_ID, "statement": OLD,
                           "confidence": "0.90", "keywords": "alpha,beta",
                           "stated_ts": TS, "source": "explicit",
                           "status": "live"})
        self.stage(by=OWNER)
        _rc, out2, err2 = self.confirm(by=OWNER)
        alone = out2 + err2
        self.assertIn("SELF-CONFIRMED", alone,
                      "the lone-seat case lost its own sentence, so the "
                      "assertion above is about a clause nobody ever gets")
        self.assertNotIn("CONFIRMED BY ANOTHER HAND", alone)

    def test_an_ATTESTED_revision_names_its_reader_everywhere(self):
        self.stage()
        rc, out, err = self.attest()
        self.assertEqual(rc, 0, err)
        self.assertIn("ATTESTED", out)
        # THE LIVE ENTRY IS UNTOUCHED BY AN ATTESTATION. Saying so is the
        # difference between this verb and confirm, and the graduation
        # sentence would have claimed the opposite.
        held = str(self.entry().get("statement") or "").strip()
        self.assertTrue(held,
                        "the entry carries no statement at all, so the "
                        "equality below would hold over an empty record "
                        "and prove nothing about the attestation")
        self.assertEqual(held, OLD,
                         "an attestation moved the live entry")
        self.assertNotIn("XREV-CLEARED", out)
        rc, out2, err2 = self.confirm()
        self.assertEqual(rc, 0, err2)
        said = out2 + err2
        self.assertIn("READ BY " + READER, said)
        self.assertNotIn("SELF-CONFIRMED", said)
        summaries = [s for s in self.confirm_summaries() if "revised" in s]
        self.assertEqual(len(summaries), 1)
        self.assertIn("READ BY " + READER, summaries[0])

    def test_the_AUTHOR_cannot_attest_their_own_revision(self):
        self.stage(by=AUTHOR)
        # POSITIVE FIRST, SAME FIELD: the revision is on file and its
        # attestation slot is the thing under test, so a fixture that staged
        # nothing cannot satisfy the emptiness assertion below.
        self.assertTrue(self.entry().get("pending_revision"))
        rc, _out, err = self.attest(by=AUTHOR)
        self.assertEqual(rc, 1, "a self-attestation was accepted")
        self.assertIn("reading their own work", err)
        # NOTHING MUTATED. A refused attestation that still wrote a field
        # would make the next confirm read as reviewed.
        self.assertIsNone((self.entry().get("pending_revision")
                           or {}).get("xrev_by"))

    def test_case_and_space_are_not_two_readers(self):
        """THE CHEAPEST WAY PAST THIS DOOR IS A SPELLING, NOT AN ARGUMENT."""
        self.stage(by="Seat-A")
        # POSITIVE CONTROL FIRST, same staged revision: a genuinely different
        # reader is accepted, so the refusal below is about identity and not
        # about the door being shut.
        rc, _o, err = self.attest(by=READER)
        self.assertEqual(rc, 0, err)
        self.stage(by="Seat-A", stmt=NEW + " AGAIN")
        rc, _out, err = self.attest(by="  seat-a  ")
        self.assertEqual(rc, 1, "a case and whitespace variant passed as a "
                                "second reader")
        self.assertIn("reading their own work", err)

    def test_an_attestation_by_the_CONFIRMER_is_not_a_second_reader(self):
        """ONE ACTOR NAMED TWICE IS STILL ONE ACTOR, and the attestation door
        cannot see the confirmer because the confirm has not happened yet.
        So the discrimination has to live at confirm time as well."""
        self.stage(by=AUTHOR)
        rc, _o, err = self.attest(by=OWNER)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.confirm(by=OWNER)
        self.assertEqual(rc, 0, err)
        said = out + err
        self.assertIn("SELF-CONFIRMED", said)
        self.assertIn("already on this revision", said)
        self.assertNotIn("READ BY", said)

    def test_an_UNIDENTIFIED_stager_makes_the_attestation_UNVERIFIABLE(self):
        """A SECOND READER CANNOT BE PROVEN WHEN THE FIRST ONE HAS NO NAME.

        The refusal at xrev-clear compares the attester to the STAGER, so an
        empty stager can never match and the door cannot refuse. Reading that
        non-match as evidence of a second reader is `_same_actor`'s own rule
        broken one layer up: an empty side is not a match, and unknown must
        not mean somebody else.

        AND IT IS REACHABLE FROM THE CLI, not only the library: the acting
        actor resolves to None for a derived identity or an identity
        disagreement, and the revise path passes that through as an empty
        string. So the seat that cannot name itself is exactly the one whose
        self-attestation this gate cannot refuse.

        THE DOOR STILL ACCEPTS IT. The lone actor is who this design refuses
        to wedge; the record says what it can and cannot show.
        """
        self.stage(by="")
        staged = (self.entry().get("pending_revision") or {})
        self.assertTrue(staged, "fixture: nothing staged")
        self.assertFalse(staged.get("by"),
                         "fixture: the stager is named, so this arm is not "
                         "about an unidentified one")
        # CONTROL, SAME DOOR, SAME READER: with a NAMED stager this exact
        # attestation is the attested case. So the difference below is the
        # stager's anonymity and nothing else.
        rc, out, err = self.attest(by=READER)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.confirm(by=OWNER)
        self.assertEqual(rc, 0, err)
        said = out + err
        self.assertIn("UNVERIFIABLE", said)
        self.assertIn("could not be named", said)
        self.assertNotIn("READ BY " + READER + ", confirmed", said,
                         "the attested sentence rendered over a stager that "
                         "cannot be distinguished from the reader")
        summaries = [x for x in self.confirm_summaries() if "revised" in x]
        self.assertEqual(len(summaries), 1)
        self.assertIn("UNVERIFIABLE", summaries[0])

    def test_a_CANDIDATE_graduation_still_says_GRADUATION(self):
        """THE VERB NOW HAS TWO SUBJECTS AND MUST NOT DESCRIBE THE WRONG ONE."""
        rc, out, err = run_store(["xrev-clear", CAND_ID, "--by", READER,
                                  "--project", "p"])
        self.assertEqual(rc, 0, err)
        self.assertIn("XREV-CLEARED", out)
        self.assertIn("provisional", out)
        self.assertNotIn("ATTESTED the staged revision", out)
        self.assertEqual(self.entry(CAND_ID).get("status"), "provisional")

    def test_a_LIVE_entry_with_NO_staged_revision_still_REFUSES(self):
        rc, _out, err = self.attest(by=READER)
        self.assertEqual(rc, 1)
        self.assertIn("carries no staged revision", err)


if __name__ == "__main__":
    unittest.main()


class AnUnattributableConfirmIsNotASelfConfirmTest(unittest.TestCase):
    """task/2228. Two actors nobody could name compared EQUAL, so the clause
    claimed one actor while knowing neither.

    THE LAUNDERING IS THE MECHANISM AND IT IS THE IRONIC PART. `_same_actor`
    is correct on its own terms, and its docstring states the very rule that
    catches this — an empty side is not a match, unknown must never mean
    someone else. It never sees the empties: the DISPLAY placeholder is
    substituted into the same variables the identity comparison reads, two
    lines above it, and the placeholder is a single literal, so two unnamed
    actors are the same string.

    AND THE HALF-FIX IS WORSE THAN IT LOOKS, which is what the third arm
    pins. Passing the RAW values to `_same_actor` and stopping there turns a
    named stager beside an unnamed confirmer into CONFIRMED BY ANOTHER HAND —
    swapping one unsupported identity claim for the opposite one, and this
    one raises an alarm about a second reader that nothing on file evidences.
    Unknown is a THIRD answer; neither existing sentence can carry it.

    THESE ARMS CALL THE RENDERER DIRECTLY because the subject is which branch
    a receipt shape selects, and driving it through the CLI would add a state
    machine between the input and the sentence under test.
    """

    def _said(self, **receipt):
        receipt.setdefault("state", "staged")
        return store_write._revision_attestation(receipt)

    def test_two_unnamed_actors_are_not_reported_as_one(self):
        """THE DEFECT AND ITS CONTROL. The must-hit is the same renderer
        producing SELF-CONFIRMED for a receipt that genuinely names ONE actor
        twice — without it, every absence below would be satisfied by a
        renderer that had stopped emitting that sentence at all."""
        said = self._said()
        self.assertIn("UNATTRIBUTABLE", said,
                      "a revision naming NEITHER actor is not described as "
                      "unattributable: %r" % said)
        self.assertNotIn("nobody else read it", said,
                         "the clause claims nobody else read a revision "
                         "whose actors it could not name — a positive "
                         "identity claim about two unidentified parties")
        self.assertNotIn("ANOTHER HAND", said,
                         "the clause claims a SECOND reader it cannot name, "
                         "which is the same error with the opposite alarm")
        control = self._said(staged_by="seat-a", confirmed_by="seat-a")
        self.assertIn("nobody else read it", control,
                      "the renderer no longer produces the lone-seat "
                      "sentence for a genuine lone seat, so the absences "
                      "above are about a sentence that is simply gone")

    def test_empty_strings_are_unnamed_and_not_a_matching_pair(self):
        """The stored shape is not always a MISSING key: a writer that records
        an actor it could not resolve leaves the empty string, and `or` folds
        both into the same placeholder."""
        said = self._said(staged_by="", confirmed_by="")
        self.assertIn("UNATTRIBUTABLE", said,
                      "two EMPTY actor strings compare equal and report "
                      "self-confirmation: %r" % said)

    def test_one_named_side_is_unknown_and_NOT_another_hand(self):
        """THE ARM THAT SEPARATES THIS CURE FROM THE TEMPTING ONE. Handing the
        raw values to `_same_actor` alone makes these two rows CONFIRMED BY
        ANOTHER HAND, because a name and an empty string are not the same
        actor — trading a false lone-seat claim for a false second-reader
        alarm. Asserted in BOTH directions, since the missing side can be
        either one and a guard written for the case in mind covers one."""
        staged_only = self._said(staged_by="seat-a", confirmed_by="")
        self.assertIn("UNATTRIBUTABLE", staged_only,
                      "a named stager beside an unnamed confirmer is "
                      "resolved rather than reported unknown: %r"
                      % staged_only)
        self.assertNotIn("ANOTHER HAND", staged_only,
                         "an unnamed confirmer is reported as a SECOND "
                         "reader, which nothing on file evidences")
        confirmed_only = self._said(staged_by="", confirmed_by="seat-b")
        self.assertIn("UNATTRIBUTABLE", confirmed_only,
                      "an unnamed stager beside a named confirmer is "
                      "resolved rather than reported unknown: %r"
                      % confirmed_only)
        self.assertNotIn("ANOTHER HAND", confirmed_only,
                         "an unnamed stager is reported as though a second "
                         "reader had been established")

    def test_the_two_decided_cases_are_unchanged(self):
        """THE REGRESSION PAIR. A cure that made everything unattributable
        would satisfy every arm above, so both decided sentences are pinned
        here, including the case/space-insensitive match that `_same_actor`
        exists to provide."""
        same = self._said(staged_by=" Seat-A ", confirmed_by="seat-a")
        self.assertIn("nobody else read it", same,
                      "one actor recorded with different case and spacing is "
                      "no longer recognised as the same actor: %r" % same)
        two = self._said(staged_by="seat-a", confirmed_by="seat-b")
        self.assertIn("ANOTHER HAND", two,
                      "two DIFFERENT named actors are no longer reported as "
                      "a two-party confirm: %r" % two)
        self.assertNotIn("UNATTRIBUTABLE", two,
                         "a fully attributed two-party confirm is reported "
                         "as unknown")
