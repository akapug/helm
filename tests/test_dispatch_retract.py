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
import hashlib
import json
import os
import pathlib
import threading
import unittest
from unittest import mock

from helm import chat, dispatches, landreq, landreq_close, meld, rowstate, seats
from helm import burnflags, review_door, seats_integrator
from tests import test_dispatches as td
from tests._verdict import native_author
from tests._tmphome import pin_live_seats


def setUpModule():
    pin_live_seats()


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
        notify = args.pop("notify", False)
        with _as(seat or self.REVIEWER):
            return dispatches.retract(rid, args.pop("reason"),
                                      args.pop("reads"), args.pop("basis"),
                                      notify=notify, **args)


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


class ModeMetricsRetractionTest(RetractBase):
    """The trial reads the real ledger fold, not projected fixture slices."""

    def test_retracted_fix_loses_its_cure_in_one_accepted_snapshot(self):
        row = self.add(recipient=self.REVIEWER, ref=self.side, kind="review")
        self.forge_open(row["id"], review_mode="MELD-DIFF")
        before, _events, accepted, _verdicts, unavailable = \
            dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        self.assertEqual(before[row["id"]]["review_mode"], "MELD-DIFF")
        self.assertEqual(accepted[row["id"]][0]["event"], "dispatch")
        out, why = dispatches.mark_verdict(
            row["id"], row["tip"], "needs a cure", "fix", basis="measured",
            finding_count=1, prior_relation="new",
            findings=["Review reports missing cure in helm/dispatches.py"],
            no_patch_because="MELD-DIFF: author applies the cure")
        self.assertIsNone(why, why)
        before, _events, accepted, _verdicts, unavailable = \
            dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        chain = before[row["id"]]["chain_root"]
        measured = {r["chain"]: r for r in review_door.mode_metrics(
            before, accepted)["chains"]}[chain]
        self.assertEqual((measured["mode"], measured["active_review_rounds"],
                          measured["cure_cycles"]), ("MELD-DIFF", 1, 1))
        self.assertEqual(accepted[row["id"]][-1]["event"], "verdict")

        self.retract(row["id"], reads="fix")
        after, _events, accepted, _verdicts, unavailable = \
            dispatches.snapshot_and_events()
        self.assertIsNone(unavailable)
        self.assertIs(after[row["id"]]["verdict_retracted"], True)
        self.assertEqual([e["event"] for e in accepted[row["id"]][-2:]],
                         ["verdict", "verdict-retract"])
        measured = {r["chain"]: r for r in review_door.mode_metrics(
            after, accepted)["chains"]}[chain]
        self.assertEqual((measured["active_review_rounds"],
                          measured["cure_cycles"]), (0, 0))


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

    def test_reissued_review_inherits_whole_brief_and_guidance_only_once(self):
        original = "reader needs the whole brief: " + ("facts " * 1100).strip()
        # A GREEN flag runs the A/B alternation (task/4005): with no fresh
        # flag the mode is MELD-DIFF, and this arm reads the PATCH arm.
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}):
            row, why, _sent = dispatches.send(
                self.REVIEWER, "reissue-guidance", original, self.side,
                repo=self.repo, kind="review", new_work=True,
                task=self.review_task["id"])
        self.assertIsNotNone(row, why)
        self.assertEqual(row["review_mode"], "PATCH")
        pre_guidance = dispatches.brief_of(row)[0].removesuffix(
            "\n\n" + dispatches.REVIEW_MODE_LINES["PATCH"])
        self.assertEqual(row["message_hash"], hashlib.blake2b(
            pre_guidance.encode("utf-8"), digest_size=16).hexdigest())
        self.assertIn(original, dispatches.brief_of(row)[0])
        verdict, why = dispatches.mark_verdict(
            row["id"], row["tip"], "needs work", "fix")
        self.assertIsNone(why, why)
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}):
            out, why = self.retract(verdict["id"], reissue=True)
        self.assertIsNone(why, why)
        child = self.state(out["retract_successor"])
        full = dispatches.brief_of(child)[0]
        self.assertTrue(full.startswith(original + "\n\n"))
        self.assertEqual(full.count("REVIEW FIX MODE:"), 1)
        self.assertEqual(child["review_mode"], "PATCH")
        self.assertEqual(child["message_hash"], row["message_hash"])
        with mock.patch.object(dispatches, "_verified_family",
                               return_value="codex"), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value={"colour": "GREEN"}):
            again, why = self.retract(verdict["id"], reissue=True)
        self.assertIsNone(why, why)
        self.assertEqual(again["reissued"]["id"], child["id"])
        self.assertEqual(dispatches.brief_of(self.state(child["id"]))[0], full)

    def test_reissue_mints_the_successor_for_the_same_reviewer_and_tip(self):  # noqa: VACUOUS_ASSERTION — every assertion is a positive equality on the minted successor
        row = self.verdicted("fix")
        opened = review_door.open_pair_round(row)
        self.assertEqual(opened["round"], 1)
        room = opened["room"]
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
        seeds = meld.seeds(chat.read(room)[0])
        self.assertEqual(len(seeds), 2)
        self.assertIn("row %s at %s" % (kid["id"][:12], kid["tip"][:12]),
                      seeds[-1][2])
        self.assertIn(" | round 2 | ", seeds[-1][2])

    def test_an_identical_retry_repairs_a_crash_after_durable_retraction(self):  # noqa: VACUOUS_ASSERTION — the durable parent and child are asserted before the retry proves each missing side effect once
        row = self.verdicted("fix")
        record = dispatches._record_retract

        def crash_after_record(*args, **kwargs):
            out, why = record(*args, **kwargs)
            self.assertIsNone(why, why)
            self.assertTrue(out["verdict_retracted"])
            raise RuntimeError("fixture crash after durable retraction")

        with mock.patch.object(dispatches, "_record_retract",
                               side_effect=crash_after_record):
            with self.assertRaisesRegex(RuntimeError, "after durable"):
                self.retract(row["id"], reissue=True, notify=True)
        parent = self.state(row["id"])
        kid_id = parent["retract_successor"]
        kid = self.state(kid_id)
        room = review_door.pair_room(kid, dispatches.snapshot()[0])[0]
        self.assertEqual(meld.seeds(chat.read(room)[0]), [])
        prefix = "@%s %s:" % (kid["recipient"], kid_id[:12])
        self.assertEqual([m for m in chat.read("main")[0]
                          if str(m.get("text") or "").startswith(prefix)], [])

        gate = threading.Barrier(3)
        replies, errors = [], []
        # BOTH RETRIES READ MAIN BEFORE EITHER POSTS. Left to the scheduler,
        # the first thread posted before the second read, and a keyless post
        # passed this arm (measured). Each read now waits for the other, so
        # only the chat key can keep the second post from a second mention.
        real_read = dispatches._existing_public_notification
        read_gate = threading.Barrier(2, timeout=10)
        reads = []

        def read_then_wait(new):
            found = real_read(new)
            reads.append(found)
            read_gate.wait()
            return found

        def retry():
            gate.wait()
            try:
                replies.append(dispatches.retract(
                    row["id"], READ["reason"], READ["reads"], READ["basis"],
                    reissue=True, notify=True))
            except Exception as exc:                         # noqa: BLE001
                errors.append(exc)

        with _as(self.REVIEWER), mock.patch.object(
                dispatches, "_existing_public_notification",
                side_effect=read_then_wait):
            threads = [threading.Thread(target=retry) for _i in range(2)]
            for thread in threads:
                thread.start()
            gate.wait()
            for thread in threads:
                thread.join(15)
        self.assertFalse(errors)
        self.assertFalse([t for t in threads if t.is_alive()])
        self.assertEqual(reads, [(None, None), (None, None)])
        for out, why in replies:
            self.assertIsNone(why, why)
            self.assertEqual(out["retract_successor"], kid_id)
            self.assertEqual(out["reissued"]["id"], kid_id)
        self.assertEqual(len(meld.seeds(chat.read(room)[0])), 1)
        mentions = [m for m in chat.read("main")[0]
                    if str(m.get("text") or "").startswith(prefix)]
        self.assertEqual(len(mentions), 1)
        live = self.state(kid_id)
        self.assertEqual(live["delivery"], "observed")
        self.assertEqual(live["delivery_ref"], mentions[0]["id"])

        again, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        self.assertEqual(again["reissued"]["id"], kid_id)
        self.assertEqual(len(meld.seeds(chat.read(room)[0])), 1)
        self.assertEqual(len([m for m in chat.read("main")[0]
                              if str(m.get("text") or "").startswith(prefix)]), 1)

    def test_retry_reconciles_a_posted_notice_before_delivery_was_stamped(self):  # noqa: VACUOUS_ASSERTION — the real notice and seed are positive controls before their nonduplication is asserted
        row = self.verdicted("fix")
        real_mark = dispatches._mark_delivered
        calls = {"n": 0}

        def crash_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("fixture crash before delivery stamp")
            return real_mark(*args, **kwargs)

        with mock.patch.object(dispatches, "_mark_delivered",
                               side_effect=crash_once):
            with self.assertRaisesRegex(RuntimeError, "before delivery"):
                self.retract(row["id"], reissue=True, notify=True)
            parent = self.state(row["id"])
            kid = self.state(parent["retract_successor"])
            room = review_door.pair_room(kid, dispatches.snapshot()[0])[0]
            prefix = "@%s %s:" % (kid["recipient"], kid["id"][:12])
            self.assertEqual(len(meld.seeds(chat.read(room)[0])), 1)
            self.assertEqual(len([m for m in chat.read("main")[0]
                                  if str(m.get("text") or "").startswith(prefix)]), 1)
            self.assertEqual(kid["delivery"], "needs-confirmation")
            out, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        self.assertEqual(out["reissued"]["delivery"], "observed")
        self.assertEqual(len(meld.seeds(chat.read(room)[0])), 1)
        self.assertEqual(len([m for m in chat.read("main")[0]
                              if str(m.get("text") or "").startswith(prefix)]), 1)

    def test_retry_reconciles_a_notice_that_posted_before_notify_raised(self):  # noqa: VACUOUS_ASSERTION — the real mention and failure receipt are asserted before retry converts that exact row id into delivery evidence
        row = self.verdicted("fix")
        real_post = chat.post

        def post_then_raise(text, *args, **kwargs):
            out = real_post(text, *args, **kwargs)
            if kwargs.get("room") == "main" and str(text).startswith("@"):
                raise RuntimeError("fixture transport raised after append")
            return out

        with mock.patch.object(chat, "post", side_effect=post_then_raise):
            out, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        kid = self.state(out["retract_successor"])
        prefix = "@%s %s:" % (kid["recipient"], kid["id"][:12])
        mentions = [m for m in chat.read("main")[0]
                    if str(m.get("text") or "").startswith(prefix)]
        self.assertEqual(len(mentions), 1)
        self.assertIsNotNone(dispatches._notify_failed_for(kid["id"]))
        self.assertEqual(kid["delivery"], "needs-confirmation")

        again, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        self.assertEqual(again["reissued"]["delivery"], "observed")
        self.assertEqual(again["reissued"]["delivery_ref"], mentions[0]["id"])
        self.assertEqual(len([m for m in chat.read("main")[0]
                              if str(m.get("text") or "").startswith(prefix)]), 1)

    def test_a_keyed_failure_before_append_is_retried_safely(self):  # noqa: VACUOUS_ASSERTION — the first pass's keyed receipt and empty main are positive controls before the retry must append and stamp exactly one mention
        row = self.verdicted("fix")
        real_post = chat.post
        failed = {"once": False}

        def fail_once(text, *args, **kwargs):
            if kwargs.get("room") == "main" and str(text).startswith("@") \
                    and not failed["once"]:
                failed["once"] = True
                raise OSError("fixture room lock timeout before append")
            return real_post(text, *args, **kwargs)

        with mock.patch.object(chat, "post", side_effect=fail_once):
            first, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        kid_id = first["retract_successor"]
        receipt = dispatches._notify_failed_for(kid_id)
        self.assertEqual(receipt.get("chat_event_id"),
                         "dispatch-handoff:" + kid_id)
        prefix = "@%s %s:" % (first["reissued"]["recipient"], kid_id[:12])
        self.assertEqual([m for m in chat.read("main")[0]
                          if str(m.get("text") or "").startswith(prefix)], [])

        again, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        mentions = [m for m in chat.read("main")[0]
                    if str(m.get("text") or "").startswith(prefix)]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(again["reissued"]["delivery"], "observed")
        self.assertEqual(again["reissued"]["delivery_ref"], mentions[0]["id"])

    def test_a_snapshot_fault_receipt_allows_a_later_keyed_handoff(self):  # noqa: VACUOUS_ASSERTION — the typed failure receipt and empty first pass are positive controls before the retry must append and stamp one mention
        row = self.verdicted("fix")
        first, why = self.retract(row["id"], reissue=True, notify=False)
        self.assertIsNone(why, why)
        kid_id = first["retract_successor"]
        kid = self.state(kid_id)
        with mock.patch.object(dispatches, "snapshot",
                               return_value=({}, "fixture snapshot unreadable")):
            dispatches._finish_reissue(kid, self.REVIEWER, True)
        receipt = dispatches._notify_failed_for(kid_id)
        self.assertEqual(receipt.get("chat_event_id"),
                         "dispatch-handoff:" + kid_id)
        prefix = "@%s %s:" % (kid["recipient"], kid_id[:12])
        self.assertEqual([m for m in chat.read("main")[0]
                          if str(m.get("text") or "").startswith(prefix)], [])

        again, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        mentions = [m for m in chat.read("main")[0]
                    if str(m.get("text") or "").startswith(prefix)]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(again["reissued"]["delivery"], "observed")

    def test_a_main_read_fault_receipt_allows_a_later_keyed_handoff(self):  # noqa: VACUOUS_ASSERTION — the typed read-fault receipt and empty first pass are positive controls before the retry must append and stamp one mention
        row = self.verdicted("fix")
        first, why = self.retract(row["id"], reissue=True, notify=False)
        self.assertIsNone(why, why)
        kid_id = first["retract_successor"]
        kid = self.state(kid_id)
        with mock.patch.object(dispatches, "_existing_public_notification",
                               return_value=(None, "fixture main unreadable")):
            dispatches._finish_reissue(kid, self.REVIEWER, True)
        receipt = dispatches._notify_failed_for(kid_id)
        self.assertEqual(receipt.get("chat_event_id"),
                         "dispatch-handoff:" + kid_id)
        prefix = "@%s %s:" % (kid["recipient"], kid_id[:12])
        self.assertEqual([m for m in chat.read("main")[0]
                          if str(m.get("text") or "").startswith(prefix)], [])

        again, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        mentions = [m for m in chat.read("main")[0]
                    if str(m.get("text") or "").startswith(prefix)]
        self.assertEqual(len(mentions), 1)
        self.assertEqual(again["reissued"]["delivery"], "observed")

    def test_an_unkeyed_failure_receipt_never_reposts(self):  # noqa: VACUOUS_ASSERTION — the successor and legacy receipt are positive controls; the empty main proves the retry stayed fail-closed rather than guessing whether an old unkeyed mention rotated away
        row = self.verdicted("fix")
        first, why = self.retract(row["id"], reissue=True, notify=False)
        self.assertIsNone(why, why)
        kid_id = first["retract_successor"]
        dispatches._record_notify_failed(kid_id, "legacy unkeyed failure")
        self.assertNotIn("chat_event_id",
                         dispatches._notify_failed_for(kid_id))

        again, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        prefix = "@%s %s:" % (again["reissued"]["recipient"], kid_id[:12])
        self.assertEqual([m for m in chat.read("main")[0]
                          if str(m.get("text") or "").startswith(prefix)], [])
        self.assertEqual(again["reissued"]["delivery"], "needs-confirmation")

    def test_two_hand_offs_that_both_read_main_first_post_one_mention(self):
        # Both calls see no mention on main and no delivery stamp, which is
        # what two racing retries see; the chat key is all that stays.
        row = self.verdicted("fix")
        with mock.patch.object(dispatches, "_existing_public_notification",
                               return_value=(None, None)), \
                mock.patch.object(dispatches, "_mark_delivered",
                                  return_value=(None, "fixture: stamp lost")):
            first, why = self.retract(row["id"], reissue=True, notify=True)
            self.assertIsNone(why, why)
            self.assertTrue(first["verdict_retracted"])
            again, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(why, why)
        kid_id = first["retract_successor"]
        self.assertEqual(again["reissued"]["id"], kid_id)
        prefix = "@%s %s:" % (first["reissued"]["recipient"], kid_id[:12])
        self.assertEqual(len([m for m in chat.read("main")[0]
                              if str(m.get("text") or "").startswith(prefix)]), 1)

    def test_a_retry_over_a_retired_successor_finishes_nothing(self):  # noqa: VACUOUS_ASSERTION — the refusal naming retirement is asserted first; the empty round and main are the contract, and the crash-repair test above is their positive control
        row = self.verdicted("fix")
        record = dispatches._record_retract

        def crash_after_record(*args, **kwargs):
            out, why = record(*args, **kwargs)
            self.assertIsNone(why, why)
            raise RuntimeError("fixture crash after durable retraction")

        with mock.patch.object(dispatches, "_record_retract",
                               side_effect=crash_after_record):
            with self.assertRaisesRegex(RuntimeError, "after durable"):
                self.retract(row["id"], reissue=True, notify=True)
        kid_id = self.state(row["id"])["retract_successor"]
        kid = self.state(kid_id)
        room = review_door.pair_room(kid, dispatches.snapshot()[0])[0]
        prefix = "@%s %s:" % (kid["recipient"], kid_id[:12])
        real = dispatches.snapshot

        def retired():
            current, unavailable = real()
            current = dict(current)
            current[kid_id] = dict(current[kid_id], retired_admin=True,
                                   retire_reason="repo-unreadable",
                                   retire_ts="2026-09-25T00:00:00Z")
            return current, unavailable

        with mock.patch.object(dispatches, "snapshot", side_effect=retired):
            out, why = self.retract(row["id"], reissue=True, notify=True)
        self.assertIsNone(out)
        self.assertIn("administratively retired", why)
        self.assertEqual(meld.seeds(chat.read(room)[0]), [])
        self.assertEqual([m for m in chat.read("main")[0]
                          if str(m.get("text") or "").startswith(prefix)], [])

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

    def test_a_retraction_that_fails_after_the_mint_disowns_the_successor(self):  # noqa: VACUOUS_ASSERTION — minted child count/status control the absent seed; # noqa: ORPHANED_MOCK — helper reaches dispatches.retract beyond the local graph
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
        kid = self.state(minted.pop())
        self.assertEqual(kid["status"], "cancelled")
        room = review_door.pair_room(kid, dispatches.snapshot()[0])[0]
        self.assertEqual(meld.seeds(chat.read(room)[0]), [])
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
