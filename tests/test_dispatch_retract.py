#!/usr/bin/env python3
"""`helm dispatch retract` — the corrective for a WRONG verdict (task/3060).

THE INCIDENT. A delegated reader running inside a seat inherited that seat's
identity and wrote an APPROVE with zero findings that its brief never
authorized. The verdict is immutable by design: the door refuses a second
verdict, `cancel` refuses a row whose verdict declared a polarity, and nothing
could take the approve's authority back. It kept reading READY for its tip.

WHAT THESE ARMS PIN, one class per build item:
  C1 the reducer folds a `verdict-retract` event AFTER the verdict and refuses
     every other shape by identity;
  C2 the writer admits the author seat, the integrator and the owner's
     capability, refuses everyone else (the sender included), and reconciles
     an identical retry;
  C3 the CLI refuses a missing reading or basis and prints the retraction;
  C4 `--reissue` mints the successor first and leaves nothing behind when the
     retraction does not happen;
  C5 the land-request projection reads RETRACTED as a terminal and the land
     nudge never says READY over it;
  C6 the list and triage lines and the refusals of every later door name it;
  C7 the docs and both help surfaces carry the verb.
Every class leads with a positive control on the same observable its
absences are read against.
"""
import json
import os
import pathlib
import unittest
from unittest import mock

from helm import dispatches, landreq, landreq_close, rowstate, seats
from helm import seats_integrator
from tests import test_dispatches as td
from tests._verdict import native_author

ROOT = pathlib.Path(__file__).resolve().parent.parent
READ = {"reason": "the verdict was minted by a delegate outside its brief",
        "reads": "source-clean", "basis": "measured"}


def _as(seat):
    """Run as `seat`: the acting identity every writer resolves."""
    return mock.patch.dict(os.environ, {"HELM_CHAT_NAME": seat})


def _integrator(seat="seat-c"):
    return mock.patch.object(seats_integrator, "integrator_seat",
                             return_value=(seat, None))


class RetractBase(td.DispatchBase):
    """A scratch ledger with FIX and APPROVE verdicts to retract.

    THE APPROVE IS A REAL ONE: the gate binding is stubbed to a complete,
    recomputable receipt (the land nudge fixture's shape), the author proof is
    the exact-session fixture, and the tip is OFF trunk, so the projection
    reads plain READY before the retraction and the arms below measure a
    change of state rather than a row that was never landable."""

    REVIEWER = "seat-b"

    def setUp(self):
        super().setUp()
        receipt = {
            "v": 1, "event": "gate", "ts": dispatches.pk.now_ts(),
            "repo_id": self.repo, "head": "0" * 40, "tree": "1" * 40,
            "dirty": False,
            "interpreter": {"name": "cpython", "version": "3.14.6",
                            "language": "3.14.6",
                            "executable": "/usr/bin/python3"},
            "argv": ["/usr/bin/python3", "-m", "unittest"],
            "status": "OK", "ran": 1, "skipped": 0, "rc": 0}
        receipt["id"] = dispatches.gate._receipt_id(receipt)
        self.receipt = receipt
        for patch in (
                mock.patch.object(dispatches.gate, "bind", return_value=(
                    "VERIFIED", receipt["id"], "test receipt")),
                mock.patch.object(seats, "dm", return_value=({}, None))):
            patch.start()
            self.addCleanup(patch.stop)

    def verdicted(self, polarity="fix", recipient=None):
        """One verdicted review row addressed to `recipient`, off trunk."""
        row = self.add(recipient=recipient or self.REVIEWER, ref=self.side)
        if polarity == "approve":
            path = dispatches.gate.receipts_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(self.receipt) + "\n")
            with native_author(self):
                out, why = dispatches.mark_verdict(
                    row["id"], row["tip"], "APPROVE",
                    "approve", basis="measured", bind_author=True)
        else:
            out, why = dispatches.mark_verdict(
                row["id"], row["tip"], "needs work", polarity)
        self.assertIsNone(why, why)
        return out

    def events(self, rid):
        return [e for e in dispatches.history(rid)]

    def state(self, rid):
        return dispatches.snapshot()[0][rid]

    def retract(self, rid, seat=None, **kw):
        args = dict(READ, **kw)
        with _as(seat or self.REVIEWER):
            return dispatches.retract(rid, args.pop("reason"),
                                      args.pop("reads"), args.pop("basis"),
                                      notify=False, **args)


def _event(state, **over):
    """A well-formed retraction of `state`, spelled with literals so the arm
    runs (and fails) on a helm that has no retract constants at all."""
    event = {"v": 3, "event": "verdict-retract", "seq": state["seq"] + 1,
             "id": state["id"], "ts": dispatches.pk.now_ts(),
             "retract_reason": "wrong", "retract_reads": "unknown",
             "retract_basis": "measured", "retract_role": "author",
             "retract_seat": "seat-b",
             "retracted_polarity": state.get("polarity"),
             "retracted_tip": state.get("reviewed_tip"),
             "retract_proof_version": 1}
    event.update(over)
    return {k: v for k, v in event.items() if v is not None}


class ReducerFoldsARetractionTest(RetractBase):
    """C1. The event follows the verdict and rewrites none of it."""

    def test_a_retraction_after_the_verdict_projects_RETRACTED(self):
        state = self.verdicted("fix")
        out = dispatches._apply(state, _event(state))
        self.assertIsNot(out, state, "the reducer did not fold the event")
        self.assertEqual(out["polarity"], "retracted")
        self.assertEqual(out["retracted_polarity"], "fix")
        self.assertIs(out["verdict_retracted"], True)
        self.assertEqual(out["seq"], state["seq"] + 1)
        # NOTHING THE VERDICT RECORDED IS REWRITTEN.
        for key in ("reviewed_tip", "verdict_ref", "verdict_ts", "status"):
            self.assertEqual(out[key], state[key], key)

    def test_every_other_shape_returns_the_state_by_identity(self):  # noqa: VACUOUS_ASSERTION — the well-formed event is asserted to FOLD first, on the same call the identity arms read
        verdicted = self.verdicted("fix")
        # POSITIVE CONTROL on the same call: the well-formed event folds.
        self.assertIsNot(dispatches._apply(verdicted, _event(verdicted)),
                         verdicted)
        opened = self.state(self.add(ref=self.side)["id"])
        retracted = dispatches._apply(verdicted, _event(verdicted))
        retired = dict(verdicted, retired_admin=True,
                       retire_reason="author-unresolvable")
        undeclared = dict(verdicted, polarity=None)
        cases = {
            "open row": (opened, _event(opened)),
            "already retracted": (retracted, _event(retracted)),
            "administratively retired": (retired, _event(retired)),
            "undeclared verdict": (undeclared, _event(undeclared)),
            "no seat": (verdicted, _event(verdicted, retract_seat=None)),
            "unknown reading": (verdicted,
                                _event(verdicted, retract_reads="clean")),
            "unverified basis": (verdicted,
                                 _event(verdicted, retract_basis="unverified")),
            "a door nobody has": (verdicted,
                                  _event(verdicted, retract_role="sender")),
            "another verdict's polarity": (
                verdicted, _event(verdicted, retracted_polarity="approve")),
            "a future proof version": (
                verdicted, _event(verdicted, retract_proof_version=2)),
            "a bool for the proof version": (
                verdicted, _event(verdicted, retract_proof_version=True)),
            "itself as successor": (
                verdicted, _event(verdicted, retract_successor=verdicted["id"])),
        }
        for name, (state, event) in cases.items():
            with self.subTest(case=name):
                self.assertIs(dispatches._apply(state, event), state)


class WhoMayRetractTest(RetractBase):
    """C2. The author seat, the integrator, the owner; nobody else."""

    def test_the_AUTHOR_seat_retracts_from_any_session(self):
        row = self.verdicted("fix")
        before = len(self.events(row["id"]))
        with mock.patch.dict(os.environ,
                             {"CLAUDE_CODE_SESSION_ID": "a-later-session"}):
            out, why = self.retract(row["id"])
        self.assertIsNone(why, why)
        self.assertEqual(out["retract_role"], "author")
        self.assertEqual(out["retract_seat"], self.REVIEWER)
        self.assertEqual(len(self.events(row["id"])), before + 1)
        self.assertEqual(self.state(row["id"])["polarity"], "retracted")

    def test_the_INTEGRATOR_retracts(self):
        row = self.verdicted("fix")
        with _integrator():
            out, why = self.retract(row["id"], seat="seat-c")
        self.assertIsNone(why, why)
        self.assertEqual(out["retract_role"], "integrator")

    def test_the_OWNER_retracts_only_through_his_capability(self):  # noqa: VACUOUS_ASSERTION — the capability path is asserted to WRITE (role owner) after the string is refused
        from helm import ownerasks
        row = self.verdicted("fix")
        _out, why = self.retract(row["id"], seat="outsider", owner="owner")
        self.assertIn("capability", why)
        self.assertEqual(self.state(row["id"])["polarity"], "fix")
        out, why = self.retract(row["id"], seat="outsider",
                                owner=ownerasks.owner_door("web"))
        self.assertIsNone(why, why)
        self.assertEqual((out["retract_role"], out["retract_seat"]),
                         ("owner", ownerasks.OWNER))

    def test_the_SENDER_and_a_stranger_are_refused_and_nothing_is_written(self):  # noqa: VACUOUS_ASSERTION — the author's retraction of the same row is asserted to add one event after the refusals
        row = self.verdicted("fix")
        before = len(self.events(row["id"]))
        for seat in ("integrator", "someone-else"):   # the fixture's sender
            with self.subTest(seat=seat), _integrator():
                out, why = self.retract(row["id"], seat=seat)
                self.assertIsNone(out)
                self.assertIn("AUTHOR (@%s" % self.REVIEWER, why)
                self.assertIn("INTEGRATOR (@seat-c)", why)
                self.assertIn("OWNER", why)
                self.assertIn("--supersedes %s" % row["id"][:12], why)
        self.assertEqual(len(self.events(row["id"])), before)
        self.assertEqual(self.state(row["id"])["polarity"], "fix")
        # POSITIVE CONTROL on the same observable: the author's retraction of
        # this row IS counted, so the unchanged count above is a refusal.
        _out, why = self.retract(row["id"])
        self.assertIsNone(why, why)
        self.assertEqual(len(self.events(row["id"])), before + 1)

    def test_an_identical_retry_reconciles_and_a_different_one_refuses(self):  # noqa: VACUOUS_ASSERTION — the first retraction is asserted to succeed and its stamp is the one the retry returns
        row = self.verdicted("fix")
        first, why = self.retract(row["id"])
        self.assertIsNone(why, why)
        count = len(self.events(row["id"]))
        again, why = self.retract(row["id"])
        self.assertIsNone(why, why)
        self.assertEqual(again["retract_ts"], first["retract_ts"])
        self.assertEqual(len(self.events(row["id"])), count)
        _out, why = self.retract(row["id"], reads="fix")
        self.assertIn("RETRACTED", why)
        self.assertIn("not retracted again", why)
        self.assertEqual(len(self.events(row["id"])), count)

    def test_an_undeclared_verdict_names_the_advisory_door(self):
        state = self.verdicted("fix")
        # CONTROL on the same predicate: the declared verdict is admitted.
        self.assertIsNone(dispatches._retract_admission_error(state))
        why = dispatches._retract_admission_error(dict(state, polarity=None))
        self.assertIn("declared no polarity", why)
        self.assertIn("helm dispatch cancel", why)


class TheCliTest(RetractBase):
    """C3. `helm dispatch retract` parses, refuses, and says what carries it."""

    def cli(self, *argv, seat=None):
        with _as(seat or self.REVIEWER):
            return td.run(dispatches.cmd_dispatch, ["retract"] + list(argv))

    def test_a_retraction_prints_the_row_and_the_json_carries_every_field(self):
        row = self.verdicted("fix")
        rc, out, err = self.cli(row["id"][:12], "--reason", "wrong review",
                                "--reads", "unknown", "--inferred", "--json")
        self.assertEqual((rc, err), (0, ""))
        got = json.loads(out)
        self.assertEqual(
            {k: got.get(k) for k in ("retract_reason", "retract_reads",
                                     "retract_basis", "retracted_polarity",
                                     "retract_role", "retract_successor")},
            {"retract_reason": "wrong review", "retract_reads": "unknown",
             "retract_basis": "inferred", "retracted_polarity": "fix",
             "retract_role": "author", "retract_successor": None})

    def test_the_plain_output_names_the_reissue_door_when_nothing_carries_it(self):
        row = self.verdicted("fix")
        rc, out, err = self.cli(row["id"], "--reason", "wrong", "--reads",
                                "fix", "--measured")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("VERDICT FIX RETRACTED by @%s (author)" % self.REVIEWER,
                      out)
        self.assertIn("--supersedes %s" % row["id"][:12], out)

    def test_every_missing_or_conflicting_argument_exits_2_and_writes_nothing(self):  # noqa: VACUOUS_ASSERTION — the complete invocation is asserted to add one event after the refusals
        row = self.verdicted("fix")
        before = len(self.events(row["id"]))
        cases = {
            "no reads": ([row["id"], "--reason", "r", "--measured"], "--reads"),
            "no reason": ([row["id"], "--reads", "fix", "--measured"],
                          "--reason"),
            "no basis": ([row["id"], "--reason", "r", "--reads", "fix"],
                         "basis"),
            "two bases": ([row["id"], "--reason", "r", "--reads", "fix",
                           "--measured", "--inferred"], "basis"),
            "reissue and successor": (
                [row["id"], "--reason", "r", "--reads", "fix", "--measured",
                 "--reissue", "--successor", "abcdef12"], "pass one"),
            "unknown flag": ([row["id"], "--reason", "r", "--reads", "fix",
                              "--measured", "--bogus"], "unknown option"),
        }
        for name, (argv, word) in cases.items():
            with self.subTest(case=name):
                rc, _out, err = self.cli(*argv)
                self.assertEqual(rc, 2, err)
                self.assertIn(word, err)
        self.assertEqual(len(self.events(row["id"])), before)
        # POSITIVE CONTROL: the complete invocation writes one event.
        rc, _out, err = self.cli(row["id"], "--reason", "r", "--reads", "fix",
                                 "--measured")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(self.events(row["id"])), before + 1)


class ReissueTest(RetractBase):
    """C4. The successor is written first and never outlives a failed call."""

    def test_reissue_mints_the_successor_for_the_same_reviewer_and_tip(self):  # noqa: VACUOUS_ASSERTION — every assertion is a positive equality on the minted successor
        row = self.verdicted("fix")
        out, why = self.retract(row["id"], reissue=True)
        self.assertIsNone(why, why)
        kid = self.state(out["retract_successor"])
        self.assertEqual(kid["status"], "open")
        self.assertEqual(kid["supersedes"], row["id"])
        self.assertEqual(kid["chain_root"], row["chain_root"])
        self.assertEqual((kid["recipient"], kid["tip"], kid["kind"]),
                         (row["recipient"], row["tip"], row["kind"]))
        # THE LANE AUTHOR STAYS THE SENDER; the retracting hand is recorded
        # as the mover, so the successor does not read as a self-review.
        self.assertEqual(kid["sender"], row["sender"])
        self.assertEqual(kid.get("acted_by"), self.REVIEWER)
        self.assertEqual(self.state(row["id"])["retract_successor"], kid["id"])

    def test_a_refused_successor_leaves_no_retraction(self):
        row = self.verdicted("fix")
        before = len(self.events(row["id"]))
        with mock.patch.object(dispatches, "add",
                               return_value=(None, "fixture refusal")):
            out, why = self.retract(row["id"], reissue=True)
        self.assertIsNone(out)
        self.assertIn("fixture refusal", why)
        self.assertEqual(len(self.events(row["id"])), before)
        self.assertEqual(self.state(row["id"])["polarity"], "fix")

    def test_a_retraction_that_fails_after_the_mint_disowns_the_successor(self):
        row = self.verdicted("fix")
        before = set(dispatches.snapshot()[0])
        with mock.patch.object(dispatches, "_record_retract",
                               return_value=(None, "fixture failure")):
            out, why = self.retract(row["id"], reissue=True)
        self.assertIsNone(out)
        self.assertIn("fixture failure", why)
        minted = set(dispatches.snapshot()[0]) - before
        self.assertEqual(len(minted), 1, "the successor was never minted, so "
                         "this arm proves nothing about its cleanup")
        self.assertEqual(self.state(minted.pop())["status"], "cancelled")
        self.assertEqual(self.state(row["id"])["polarity"], "fix")

    def test_successor_links_only_a_row_that_supersedes_this_one(self):  # noqa: VACUOUS_ASSERTION — the linking retraction is asserted to succeed and to record the successor
        row = self.verdicted("fix")
        stranger = self.add(ref=self.side)
        _out, why = self.retract(row["id"], successor=stranger["id"])
        self.assertIn("does not supersede", why)
        kid = self.add(ref=self.side, supersedes=row["id"])
        out, why = self.retract(row["id"], successor=kid["id"][:12])
        self.assertIsNone(why, why)
        self.assertEqual(out["retract_successor"], kid["id"])


class LandProjectionTest(RetractBase):
    """C5. RETRACTED is a terminal on every land-request surface."""

    def test_a_retracted_APPROVE_is_never_READY_again(self):  # noqa: VACUOUS_ASSERTION — the same row is asserted plain READY before the retraction
        row = self.verdicted("approve")
        # POSITIVE CONTROL: the same row reads plain READY before.
        self.assertEqual(landreq.land_instruction(row["id"]), ("READY", None))
        out, why = self.retract(row["id"])
        self.assertIsNone(why, why)
        lr, err = landreq.get(row["id"])
        self.assertIsNone(err, err)
        self.assertEqual(lr["state"], "RETRACTED")
        self.assertIs(lr["terminal"], True)
        self.assertEqual(lr["owed_by"], "nobody")
        self.assertIs(lr["stalled"], False)
        self.assertEqual(landreq.terminal_annotation(lr),
                         ("RETRACTED", out["retract_ts"]))
        self.assertEqual(landreq.land_instruction(row["id"])[0], "RETRACTED")
        self.assertEqual(lr["retracted_polarity"], "approve")
        self.assertEqual(lr["retract_reads"], "source-clean")

    def test_every_other_terminal_door_refuses_a_retracted_row(self):
        row = self.verdicted("fix")
        self.retract(row["id"])
        count = len(self.events(row["id"]))
        _out, why = landreq_close.close(row["id"], "withdrawn",
                                        evidence="fixture", dry_run=True)
        self.assertIn("retract", why)
        _out, why = landreq.retire(row["id"], "author-unresolvable",
                                   seat="seat-a")
        self.assertIn("retract", why)
        self.assertEqual(len(self.events(row["id"])), count)
        # THE DERIVED STATE AGREES: nothing is coming from this row.
        self.assertEqual(rowstate._lifecycle(self.state(row["id"]))[0],
                         rowstate.CANCELLED)

    def test_a_retracted_FIX_stops_owing_and_carries_nothing(self):
        row = self.verdicted("fix")
        snap = dispatches.snapshot()[0]
        # POSITIVE CONTROL: the FIX is cure debt and its branch is live.
        self.assertEqual([r["id"] for r in dispatches.cure_eligible(
            snap, ids=[row["id"]])], [row["id"]])
        self.assertTrue(dispatches._duplicate_branch_live(snap[row["id"]]))
        self.assertFalse(dispatches.moved_nothing(snap[row["id"]]))
        self.retract(row["id"])
        snap = dispatches.snapshot()[0]
        self.assertEqual(dispatches.cure_eligible(snap, ids=[row["id"]]), [])
        self.assertFalse(dispatches._duplicate_branch_live(snap[row["id"]]))
        self.assertTrue(dispatches.moved_nothing(snap[row["id"]]))


class ReadersAndRefusalsTest(RetractBase):
    """C6. The list and triage lines say it; every later door names it."""

    def test_the_list_label_keeps_the_decision_and_names_the_retraction(self):
        row = self.verdicted("fix")
        self.assertEqual(dispatches._base_label(self.state(row["id"])),
                         "VERDICT fix")
        self.retract(row["id"])
        self.assertEqual(
            dispatches._base_label(self.state(row["id"])),
            "VERDICT fix / RETRACTED (reads source-clean) by %s (no successor)"
            % self.REVIEWER)

    def test_triage_says_RETRACTED_and_the_successor(self):
        row = self.verdicted("fix")
        out, _why = self.retract(row["id"], reissue=True)
        snap = dispatches.snapshot()[0]
        self.assertEqual(
            dispatches.untriaged(snap[row["id"]], snap),
            ("RETRACTED", "not triaged: verdict RETRACTED (was FIX) — "
                          "successor %s" % out["retract_successor"][:12]))
        rc, printed, err = td.run(dispatches.cmd_dispatch,
                                  ["triage", row["id"]])
        self.assertEqual(rc, 0, err)
        self.assertIn("verdict RETRACTED (was FIX)", printed + err)

    def test_the_second_verdict_refusal_names_the_retract_door(self):
        row = self.verdicted("fix")
        _out, why = dispatches.mark_verdict(row["id"], row["tip"], "other",
                                            "supersede")
        self.assertIn("already has a verdict", why)
        self.assertIn("helm dispatch retract %s" % row["id"][:12], why)

    def test_verdict_and_cancel_on_a_retracted_row_name_the_retraction(self):  # noqa: VACUOUS_ASSERTION — the retraction itself is asserted to add one event before the refused doors are counted
        row = self.verdicted("fix")
        before = len(self.events(row["id"]))
        _out, why = self.retract(row["id"])
        self.assertIsNone(why, why)
        count = len(self.events(row["id"]))
        self.assertEqual(count, before + 1, "the counter did not see the "
                         "retraction, so the unchanged count below is vacuous")
        _out, why = dispatches.mark_verdict(row["id"], row["tip"], "again",
                                            "fix")
        self.assertIn("RETRACTED", why)
        self.assertIn("takes no new verdict", why)
        _out, why = dispatches.mark_cancel(row["id"], "cancel it")
        self.assertIn("RETRACTED", why)
        self.assertIn("--supersedes %s" % row["id"][:12], why)
        self.assertEqual(len(self.events(row["id"])), count)


class IncidentReplayTest(RetractBase):
    """The incident's own shape, end to end through the CLI: the approve a
    delegate minted is retracted by its author seat, reads source-clean, and
    the review is re-requested in the same motion."""

    def test_the_delegate_approve_is_retracted_and_reissued(self):
        row = self.verdicted("approve")
        self.assertEqual(landreq.land_instruction(row["id"]), ("READY", None))
        with _as(self.REVIEWER):
            rc, out, err = td.run(dispatches.cmd_dispatch, [
                "retract", row["id"][:12], "--reason",
                "approve minted by a delegated reader without authority",
                "--reads", "source-clean", "--measured", "--reissue"])
        self.assertEqual(rc, 0, err)
        self.assertIn("VERDICT APPROVE RETRACTED", out)
        self.assertIn("--source-clean %s" % row["reviewed_tip"], out)
        parent = self.state(row["id"])
        kid = self.state(parent["retract_successor"])
        self.assertEqual(kid["status"], "open")
        self.assertEqual(landreq.land_instruction(row["id"])[0], "RETRACTED")


class DocsTest(unittest.TestCase):
    """C7. The verb is on every surface a reader asks."""

    def test_the_synopsis_is_on_every_surface(self):
        from helm import cli
        clause = ("retract <id-or-unique-prefix> --reason R --reads "
                  "source-clean|fix|supersede|unknown --measured|--inferred "
                  "[--reissue|--successor ID] [--json]")
        verbs = (ROOT / "docs" / "VERBS.md").read_text(encoding="utf-8")
        for name, text in (("VERBS.md", verbs),
                           ("helm dispatch --help", cli._VERB_HELP["dispatch"]),
                           ("dispatches.USAGE", dispatches.USAGE)):
            with self.subTest(surface=name):
                self.assertIn(clause, " ".join(text.split()))
        self.assertIn("RETRACTED is a terminal too", verbs)


if __name__ == "__main__":
    unittest.main()
