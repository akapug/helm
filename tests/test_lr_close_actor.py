"""Every close records the hand that closed it, and that hand is in NO schema.

THE DERIVE LAYER THESE ARMS WERE WRITTEN BESIDE IS GONE — it did not converge
under review and was cut to its own row. What survives here is the part the
reviewer accepted: the actor the writer stamps under its own lock, the
structural proof that the field belongs to no schema set, and the pass over
every close ladder proving a dry answer appends nothing to the ledger.
"""
import unittest
from unittest import mock

from helm import dispatches, eventledger, landreq
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_lr_close as _close


def rec_mismatch(rec, reason, admitted, armed):
    """Is this reason on exactly one side of the admitted/armed disagreement?"""
    return (reason in admitted) != (reason in armed)


class EveryLadderAnswersWithoutWriting(_close.CloseBase):
    """The premise the one-command build depends on, measured not read."""

    def history(self):
        return len(list(eventledger.events(dispatches.ledger_path())))

    def test_a_dry_pass_over_EVERY_reason_appends_nothing(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control is the real close above (assertGreater on the same counter) plus the at-least-one-admitted check below; the rung credits a control only from the SAME CALL's other channel, and this one is a second call on the same observable
        row = self.verdict_row(polarity="concur", lane="lane/derive-premise")
        # THE MUST-HIT ON THE LEDGER COUNTER ITSELF: a real close grows it, so
        # the unchanged count below is the dry runs staying silent rather than
        # a counter that never moves in this fixture.
        other = self.verdict_row(polarity="concur", lane="lane/derive-control")
        before = self.history()
        out, err = landreq.close(other["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertGreater(self.history(), before,
                           "the ledger does not grow in this fixture, so the "
                           "unchanged count below proves nothing")

        held = self.history()
        answers = {}
        for reason in landreq.CLOSE_CLI_REASONS:
            out, err = landreq.close(row["id"], reason, repo=self.repo,
                                     trunk=self.main, dry_run=True)
            answers[reason] = (out is not None, err)
        self.assertEqual(
            self.history(), held,
            "a DRY pass over every close reason APPENDED to the ledger — the "
            "one-command design cannot ask the ladders anything: %s" % answers)
        # AND THE PASS MUST DISCRIMINATE. If every reason refused, the arm
        # above is satisfied by a pass that did nothing at all; this row is
        # expirable, so at least one reason has to answer yes.
        admitted = [r for r, (ok, _e) in answers.items() if ok]
        self.assertTrue(
            admitted,
            "no reason admitted an expirable row, so the dry pass proves only "
            "that refusals do not write: %s" % answers)

    def test_the_dry_answers_name_the_reasons_that_refused(self):  # noqa: VACUOUS_ASSERTION — assertTrue(spoke) is the unconditional positive control and runs before the absence assertion; the rung credits a control only from the SAME CALL's other channel, and a second local counting the refusals that DID speak is not that shape
        """The drop-plus-note half: a refusal carries its own sentence, so the
        command can report the whole classification and not just the winner —
        the shape `retire --sweep` already uses for its four measurements."""
        row = self.verdict_row(polarity="concur", lane="lane/derive-notes")
        silent, spoke = [], []
        for reason in landreq.CLOSE_CLI_REASONS:
            out, err = landreq.close(row["id"], reason, repo=self.repo,
                                     trunk=self.main, dry_run=True)
            if out is not None:
                continue
            (spoke if str(err or "").strip() else silent).append(reason)
        # THE POSITIVE CONTROL, UNCONDITIONAL AND ON THE SAME OBSERVABLE. An
        # empty `silent` is satisfied by a loop that never ran, by a reason
        # list that is empty, and by a fixture where every door admits — so
        # the count that DID carry a sentence is asserted first, and the two
        # together account for every reason the CLI takes.
        self.assertTrue(spoke, "no reason refused with a sentence at all, so "
                               "an empty silent list proves nothing")
        self.assertEqual(silent, [],
                         "these reasons refused with NO sentence, so a caller "
                         "could not say why they were dropped")
        self.assertEqual(len(spoke) + len(silent),
                         sum(1 for r in landreq.CLOSE_CLI_REASONS
                             if landreq.close(row["id"], r, repo=self.repo,
                                              trunk=self.main,
                                              dry_run=True)[0] is None),
                         "the loop did not visit every refusing reason")


class EveryCloseRecordsTheHandThatClosedIt(_close.CloseBase):
    """The census's sharpest finding, cured at the one door that can.

    Only the `dispatch` event has ever recorded who acted; every terminal
    recorded nobody, so "which seat closed this row" was unanswerable from the
    ledger. The writer stamps it now, from this process's DECLARED identity
    rather than from anything a caller passes.
    """

    def test_the_writer_stamps_the_acting_seat_on_a_real_close(self):  # noqa: VACUOUS_ASSERTION — the assertEqual binding the persisted field to the resolved seat is unconditional and positive; the assertIsNone on err is a precondition, not the claim
        row = self.verdict_row(polarity="concur", lane="lane/actor-stamp")
        with mock.patch.object(landreq, "_acting_seat",
                               return_value="seat-c"):
            out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.close_event(row["id"]).get("close_actor"),
                         "seat-c")

    def test_an_undeclared_process_records_no_actor_rather_than_a_guess(self):  # noqa: VACUOUS_ASSERTION — the sibling arm above writes the field from the SAME writer under the SAME fixture, so the absence here is the guard firing; the assertEqual on close_reason is the unconditional positive proving the close still happened
        """AN ABSENT ACTOR IS HONEST; AN INVENTED ONE IS WORSE THAN NONE.
        The positive above is the control: the field IS written when there is
        an identity, so its absence here is the guard and not a writer that
        never stamps."""
        row = self.verdict_row(polarity="concur", lane="lane/actor-absent")
        with mock.patch.object(landreq, "_acting_seat", return_value=None):
            out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        event = self.close_event(row["id"])
        self.assertIsNone(event.get("close_actor"), event)
        # AND THE CLOSE STILL HAPPENED — an unidentified process may close.
        self.assertEqual(event.get("close_reason"), "expired")

    def test_the_writer_takes_no_actor_from_its_caller_at_all(self):  # noqa: VACUOUS_ASSERTION — the assertEqual on the stamped field at the end is the unconditional positive on the same writer, and it is what makes the signature absence a guarantee rather than an unimplemented feature
        """IDENTITY IS RESOLVED, NEVER TYPED, and here that is STRUCTURAL
        rather than a check: the writer has no actor parameter, so there is
        no path by which a caller's string could reach the record. Asserting
        "a passed actor is ignored" would be the weaker claim and would also
        be false — passing one raises.

        The arm reads the SIGNATURE rather than trying the call, because a
        TypeError is what a missing parameter produces and asserting on an
        exception type would pass equally for a typo in the function name.
        """
        import inspect
        params = inspect.signature(
            dispatches._record_close_proven).parameters
        for name in params:
            self.assertNotIn(
                "actor", name,
                "the writer grew an actor parameter (%s); the record's "
                "accountable half must stay the one part a caller cannot "
                "supply" % name)
        # AND THE FIELD IS STILL WRITTEN, which is what makes the absence
        # above a guarantee rather than a feature nobody implemented.
        row = self.verdict_row(polarity="concur", lane="lane/actor-structural")
        with mock.patch.object(landreq, "_acting_seat",
                               return_value="seat-d"):
            out, err = landreq.close(row["id"], "expired", repo=self.repo)
        self.assertIsNone(err, err)
        self.assertEqual(self.close_event(row["id"]).get("close_actor"),
                         "seat-d")


class TheActorIsInNoSchema(_close.CloseBase):
    """SIX TABLES JUDGE A CLOSE EVENT'S KEY SET, and they do not announce
    themselves. Adding one optional field to the candidate broke them in two
    waves and in opposite directions — first refused as an EXTRA by three,
    then demanded as MISSING by three more once it was put in a base set.

    So the property is pinned structurally rather than by fixing the sixth
    table and hoping: the actor is a member of NO schema set, and a seated
    writer's close binds for every reason a fixture can drive.
    """

    def test_no_schema_set_in_dispatches_contains_the_actor(self):  # noqa: VACUOUS_ASSERTION — the assertTrue on the scanned sets is the unconditional positive control on the same observable: an empty scan reddens there before the emptiness of `offenders` is believed
        sets = {name: value for name, value in vars(dispatches).items()
                if name.endswith("_FIELDS")
                and isinstance(value, (set, frozenset, tuple, list))}
        self.assertTrue(sets, "no *_FIELDS sets found — the scan is broken, "
                              "so its emptiness says nothing")
        offenders = sorted(n for n, v in sets.items()
                           if dispatches.CLOSE_ACTOR_FIELD in set(v))
        self.assertEqual(
            offenders, [],
            "the actor joined a schema set (%s). A field that is present or "
            "absent BY ENVIRONMENT cannot sit inside an equality about proof "
            "shape: an unseated writer's event then refuses as MISSING it."
            % ", ".join(offenders))

    def arm(self, reason, tag):
        """A row and the arguments `reason` needs, for the reasons this
        fixture can actually arm -> (row, kwargs) or None.

        THE TWO ARE NOT A PREFERENCE, they are what the base fixture's own
        producers can supply WITHOUT PERTURBING EACH OTHER: a concurred row
        expires, and an approved row withdraws on its author's attestation.
        The other ten each need an argument this fixture has no honest source
        for — a confirmation row id, a 40-char superseding tip, a carriage
        proof — and inventing one would be a fixture authoring the input it
        then measures.

        LANDED IS DELIBERATELY ABSENT AND THAT IS THE INTERESTING ONE.
        Arming it means cherry-picking the side commit onto trunk, which is a
        mutation of the WHOLE fixture rather than of one row: measured, it
        made `withdrawn` refuse with "the reviewed change IS on trunk
        (patch-equivalent)" and `expired` refuse as unbillable. A control that
        perturbs its subject is not a control, so landed stays in the
        complement, where its own refusal is checked like the rest.

        THE COMPLEMENT IS ASSERTED RATHER THAN ASSUMED, in the arm below: every
        reason absent from here must refuse while NAMING what it lacks. That is
        what keeps this table from going stale silently — a reason that later
        stops needing its argument reddens instead of quietly dropping out of
        the sweep.
        """
        lane = "lane/%s-%s" % (tag, reason)
        if reason == "expired":
            return self.verdict_row(polarity="concur", lane=lane), {
                "repo": self.repo, "trunk": self.main}
        if reason == "withdrawn":
            return self.verdict_row(polarity="approve", lane=lane), {
                "evidence": "attested"}
        return None

    def drive_every_reason(self, seat, tag):
        """Attempt a REAL close for every CLI reason, one fresh row each.

        -> {reason: (admitted, event_or_None, err)}. `landreq.close` is called
        without dry_run, so an admitted reason APPENDS and the event read back
        is what the writer actually wrote rather than a shape this file
        invented.
        """
        seen = {}
        with mock.patch.object(landreq, "_acting_seat", return_value=seat):
            for reason in landreq.CLOSE_CLI_REASONS:
                armed = self.arm(reason, tag)
                if armed is None:
                    row = self.verdict_row(polarity="approve",
                                           lane="lane/%s-%s" % (tag, reason))
                    kwargs = {"repo": self.repo}
                    if reason in landreq.REPO_TRUNK_REASONS:
                        kwargs["trunk"] = self.main
                else:
                    row, kwargs = armed
                out, err = landreq.close(row["id"], reason, **kwargs)
                admitted = err is None
                seen[reason] = (admitted,
                                self.close_event(row["id"]) if admitted
                                else None,
                                err,
                                armed is not None)
        return seen

    def test_a_seated_writer_stamps_EVERY_reason_that_really_admits(self):  # noqa: VACUOUS_ASSERTION — the assertGreaterEqual on the admitted COUNT runs first and unconditionally on the same walk, so a sweep where nothing admitted reddens there rather than passing on an empty per-event check
        """THE REGRESSION NET FOR A SEVENTH TABLE, driving real writes.

        EVERY CLOSE HERE APPENDS AND THE ASSERTION IS ON THE RECORDED FIELD.
        A dry pass cannot carry this claim: it writes no actor-bearing event,
        so an iteration count plus the ABSENCE of schema wording is all it can
        assert, and neither distinguishes a writer that stamps the acting seat
        from one that does not. The count of admitted reasons is checked before
        the field is, so an empty sweep reddens on the count.
        """
        seen = self.drive_every_reason("seat-e", "net")
        admitted = sorted(r for r, rec in seen.items() if rec[0])
        # MUST-HIT: a walk where nothing admitted would satisfy an assertion
        # about every admitted event vacuously.
        self.assertGreaterEqual(
            len(admitted), 2,
            "fewer than two reasons admitted in this fixture (%s), so the "
            "per-event assertion below covers almost nothing: %s"
            % (admitted, {r: str(v[2])[:60] for r, v in seen.items()}))
        unstamped = sorted(r for r in admitted
                           if seen[r][1].get("close_actor") != "seat-e")
        self.assertEqual(
            unstamped, [],
            "a real close admitted without recording the acting seat (%s) — "
            "the ledger cannot answer which hand closed those rows"
            % ", ".join(unstamped))
        # AND NO REASON REFUSED ON A SCHEMA, which is the seventh-table
        # regression this arm is the net for.
        schema = sorted(r for r, rec in seen.items()
                        if not rec[0] and "do not match" in str(rec[2] or ""))
        self.assertEqual(schema, [],
                         "a schema refused the actor-bearing event: %s"
                         % ", ".join(schema))
        # THE ARMING TABLE IS THE PREDICTION AND THIS IS WHERE IT IS TESTED.
        # THE COMPLEMENT ASKS `arm`, NEVER THE OUTCOME, and that distinction
        # is the whole assertion. Filtering on the set this walk OBSERVED
        # cannot catch a reason that starts admitting without being armed —
        # such a reason lands IN the observed admitted set, so it is excluded
        # from the complement by construction and the check passes on exactly
        # the case a staleness guard exists for. Every other assertion here
        # passes alongside it too: the count floor reads one higher, the actor
        # checks pass on the extra row, and seated/unseated parity is
        # unaffected because both walks grow together.
        armed = sorted(r for r, rec in seen.items() if rec[3])
        self.assertEqual(
            admitted, armed,
            "the reasons that ADMIT and the reasons this fixture ARMS have "
            "diverged (admitted %s, armed %s) — either the arming table is "
            "stale, or a reason changed what it requires. Both directions "
            "matter: an unarmed reason that admits is a sweep silently "
            "growing, and an armed one that refuses is a fixture that no "
            "longer arms what it claims: %s"
            % (admitted, armed,
               {r: str(seen[r][2])[:70] for r in seen if rec_mismatch(
                   seen[r], r, admitted, armed)}))
        # AND EVERY UNARMED REASON REFUSES WHILE SAYING WHAT IT WANTS, which
        # is what makes the equality above a statement about arguments rather
        # than about luck.
        mute = sorted(
            r for r, rec in seen.items()
            if not rec[3]
            and not any(w in str(rec[2] or "") for w in ("needs", "requires",
                                                         "carries no",
                                                         "neither")))
        self.assertEqual(
            mute, [],
            "an unarmed reason refused without naming what it lacks (%s), so "
            "the complement cannot tell a missing argument from a new refusal "
            "this arm has never seen: %s"
            % (", ".join(mute), {r: str(seen[r][2])[:70] for r in mute}))

    def test_an_unseated_writer_admits_THE_SAME_reasons_and_stamps_none(self):  # noqa: VACUOUS_ASSERTION — the final assertEqual is an unconditional POSITIVE on the same observable, binding the seated walk's stamped set to the admitted set; the unseated absence above is believed only because that line proves the writer stamps at all
        """THE OTHER HALF OF THE CLAIM: comprehensive
        seated-AND-unseated admission across the schema sets.

        A field written only sometimes is the dangerous kind, because every
        equality about proof shape that contains it refuses the other case.
        The sibling above proves the seated event is accepted everywhere it
        admits; this proves the UNSEATED event is accepted in exactly the same
        places, so the admission decision does not depend on the actor. Run
        them apart and each is half an answer: identical admitted sets is the
        claim, and it needs both walks.
        """
        seated = self.drive_every_reason("seat-f", "seated")
        unseated = self.drive_every_reason(None, "unseated")
        self.assertEqual(
            sorted(r for r, v in seated.items() if v[0]),
            sorted(r for r, v in unseated.items() if v[0]),
            "the acting seat changed WHICH reasons admit — %s vs %s"
            % (sorted(r for r, v in seated.items() if v[0]),
               sorted(r for r, v in unseated.items() if v[0])))
        admitted = sorted(r for r, v in unseated.items() if v[0])
        self.assertGreaterEqual(len(admitted), 2,
                                "nothing admitted in either walk, so equal "
                                "sets is two empties agreeing")
        stamped = sorted(r for r in admitted
                         if unseated[r][1].get("close_actor") is not None)
        self.assertEqual(
            stamped, [],
            "an undeclared process invented an actor on %s — an absent actor "
            "is honest, a guessed one is worse than none" % ", ".join(stamped))
        # The positive on the SAME observable, so the absence above is the
        # guard firing rather than a writer that never stamps.
        self.assertEqual(
            sorted(r for r in admitted
                   if seated[r][1].get("close_actor") == "seat-f"),
            admitted,
            "the seated walk did not stamp every admitted reason, so the "
            "unstamped unseated walk proves nothing")


if __name__ == "__main__":
    unittest.main()
