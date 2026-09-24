#!/usr/bin/env python3
"""task/2948: "no cross-family reviewer" is never a blocker, anywhere in helm.

THE OWNER RULE (store premise review-is-cross-family-or-fable-never-sonnet): a
review is CROSS-FAMILY, or FABLE when only a different model is needed, read in
a fresh context, and Sonnet and Haiku never review anything. The canonical
fallback when the reviewer you wanted cannot take a row is any other-family
seat (the openrouter seat always among them), then a Fable one-agent Workflow,
then on a Fable limit Fable through another credential or seat.

The arms below pin the six gaps the owner's directive named, each at the
surface a seat actually reads:

  1  the dispatch door's UNUSABLE refusal names the fallback and its commands
  2  a DEAF seat whose pane is LIVE says a keystroke wakes it
  3  the skills and the store name the rung after a Fable quota wall
  4  a model run's read can be recorded on the seat-bound ledger, and a seat
     cannot file its own model's read as another model's
  5  /build and reviewer-implements-own-findings name the fallback
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (dispatches, obligation, owedpush,  # noqa: E402
                  seat_usability)
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: What every fallback surface must name. The rung words and the two commands
#: a reader types, so a surface that shrank to "use the fallback" goes red.
SEAT = dispatches.REVIEW_FALLBACK_SEAT
LADDER = ("Fable", "Workflow", SEAT, "other-family",
          "another credential or seat", "Sonnet and Haiku never review",
          "helm dispatch send " + SEAT, "--reviewer-model")
#: The rung the owner's ruling removed. No surface may teach it again.
REMOVED_RUNG = "same-family different model"


class TheDispatchDoorNamesTheFallbackTest(unittest.TestCase):
    """GAP 1. The door refused with "repair it or pass force=True" and nothing
    else, so a sender whose reviewer was walled read the row as stuck."""

    def gate(self, verdict_state, reason, row):
        with mock.patch.object(seat_usability, "seat_verdict",
                               return_value=(verdict_state, reason, row)):
            return dispatches._validate_recipient_usable("seat-a", False)

    def test_an_UNUSABLE_live_recipient_is_refused_WITH_the_ladder(self):
        ok, refusal, _warning = self.gate(
            seat_usability.UNUSABLE, "upstream AUTH-UNAVAILABLE since T",
            {"can_take_work": False, "pane": True})
        self.assertFalse(ok)
        # the old repair survives: the ladder is added, never swapped in
        self.assertIn("force=True", refusal)
        for word in LADDER:
            self.assertIn(word, refusal, word)

    def test_a_MONEY_walled_family_is_refused_WITH_the_ladder(self):
        from helm import burnflags
        flag = {"axes": {"money": burnflags.RED}, "cause": "pool spent"}
        with mock.patch.object(seat_usability, "seat_verdict",
                               return_value=(seat_usability.USABLE, "",
                                             {"can_take_work": True,
                                              "pane": True,
                                              "family": "codex"})), \
                mock.patch.object(burnflags, "family_flag",
                                  return_value=flag):
            ok, refusal, _w = dispatches._validate_recipient_usable(
                "seat-a", False)
        self.assertFalse(ok)
        self.assertIn("MONEY", refusal)
        for word in LADDER:
            self.assertIn(word, refusal, word)

    def test_CONTROL_an_admitted_recipient_carries_no_ladder(self):
        """The ladder rides a refusal only: the same door admitting a usable
        seat says nothing, so the arms above measure the refusal path."""
        ok, refusal, warning = self.gate(
            seat_usability.USABLE, "", {"can_take_work": True, "pane": True})
        self.assertTrue(ok)
        self.assertIsNone(refusal)
        self.assertIsNone(warning)

    def test_the_ladder_fills_the_row_when_the_caller_has_one(self):
        text = dispatches.review_fallback_text(
            {"id": "abcdef0123456789", "lane": "lane/x"}, tip="f" * 40)
        self.assertIn("helm dispatch send %s lane/x --ref %s --kind review "
                      "--supersedes abcdef012345" % (SEAT, "f" * 40), text)
        self.assertIn("helm dispatch verdict abcdef012345 " + "f" * 40, text)
        # the Fable limit is the trigger for rung three, in the owner's words
        self.assertIn("You have reached your Fable limit", text)
        # the order is the premise's: cross-family first, Fable when only a
        # different model is needed, then Fable on another credential
        self.assertLess(text.index("(1) any other-family seat"),
                        text.index("(2) a Fable one-agent Workflow"))
        self.assertLess(text.index("(2) a Fable one-agent Workflow"),
                        text.index("(3) on"))
        self.assertLess(text.index("(3) on"),
                        text.index("another credential or seat"))
        # the removed rung is gone
        self.assertNotIn(REMOVED_RUNG, text)


class ADeafSeatWithALivePaneSaysSoTest(unittest.TestCase):
    """GAP 2. MEASURED on the live fleet: ds4pro, gemini and openrouter read
    `UNUSABLE ... no live beacon` while `helm beacons` measured them DEAF (no
    beacon process — true, not stale state) and each proxy's canary answered
    200 on /v1/messages in the same minute. The verdict is true of helm's own
    wake path; the line misled by stopping there."""

    def verdict(self, pane):
        return seat_usability._verdict_core({
            "seat": "seat-a", "pane": pane, "reachable": False,
            "reachable_why": "no live beacon: helm cannot wake it",
            "turn_state": "ok", "semantic_age_s": 60})

    def test_a_LIVE_pane_names_the_keystroke_that_wakes_it(self):
        state, why = self.verdict(True)
        self.assertEqual(state, seat_usability.UNUSABLE)
        self.assertIn("pane is LIVE", why)
        self.assertIn("helm seat resume-turn --nudge --seat seat-a", why)
        self.assertIn("orca terminal send", why)

    def test_CONTROL_an_UNKNOWN_pane_claims_nothing_about_it(self):
        """The clause is a claim about the pane, so it needs the pane MEASURED
        live: an unread pane gets the old sentence and no liveness claim."""
        state, why = self.verdict(None)
        self.assertEqual(state, seat_usability.UNUSABLE)
        self.assertIn("helm chat wait --seat seat-a --follow", why)
        self.assertNotIn("pane is LIVE", why)


class TheOwedDigestOffersTheFallbackTest(unittest.TestCase):
    """GAP 4, the DM half: cure / re-dispatch / withdraw, and now the answer
    to "and if nobody can review it"."""

    def test_the_digest_ends_on_the_ladder_once(self):
        items = [{"lane": "l%d" % k, "row": "r%d" % k, "owed_since": None,
                  "what": "w"} for k in range(3)]
        text = owedpush.digest_text("alice", items)
        for word in LADDER:
            self.assertIn(word, text, word)
        self.assertEqual(text.count("NO REVIEWER IS NEVER A BLOCKER"), 1)

    def test_every_unanswered_fix_names_the_fallback_in_its_remedy(self):
        rows = [{"id": "a" * 16, "status": "verdict", "polarity": "fix",
                 "lane": "lane/x", "sender": "alice", "recipient": "bob",
                 "reviewed_tip": "b" * 40, "ts": "2026-09-23T00:00:00Z"}]
        items, _forks, err = obligation.unanswered_fixes(rows=rows)
        self.assertIsNone(err)
        self.assertEqual(len(items), 1)
        what = items[0]["what"]
        # the three answers it always gave are still there
        self.assertIn("--supersedes " + "a" * 12, what)
        self.assertIn("--reason withdrawn", what)
        # and the fourth
        self.assertIn("--reviewer-model", what)
        self.assertIn(SEAT, what)


class AModelRunsReadIsRecordedOnTheLedgerTest(_landreq.LandReqBase):
    """GAP 4, the code gap. A Workflow run is not a seat, so the seat-bound
    ledger could not take its read. The seat that ran it now records it, and
    names the model — as an ADVISORY read that leaves the row OWED.

    THE RULING ON THE CROSS-FAMILY FINDING (task/2948): exact
    model inequality let an Opus 5.5 author file an Opus 5.4 read, or an
    invented model, as independent, and the verdict closed the obligation. So
    a model run's read counts only when its model is ANOTHER FAMILY than the
    author's, or FABLE for a Claude author; a model helm does not recognise is
    refused; and nothing it records discharges the row until task/2966 can
    verify the run on disk.

    The fixture's sender is `integrator` (HELM_CHAT_NAME) and its recipient
    `codex-3`; neither has a runtime record naming a model, so the author's
    model is declared."""

    RUN = "wf-7f3a"
    AUTHOR = "claude-opus-5-5"

    def row(self):
        return self.dispatch(ref=self.side, lane="lane/fallback", kind="review")

    def record(self, rid, polarity="concur", **kw):
        kw.setdefault("reviewer_model", "fable")
        kw.setdefault("reviewer_run", self.RUN)
        kw.setdefault("author_model", self.AUTHOR)
        return dispatches.mark_verdict(rid, self.side, "read clean",
                                       polarity=polarity, basis="measured",
                                       bind_author=True, **kw)

    def open_row(self, rid):
        current, err = dispatches.snapshot()
        self.assertFalse(err)
        return current[rid]

    def reads(self, rid):
        return list(self.open_row(rid).get("advisory_reads") or ())

    def test_a_FABLE_read_for_a_CLAUDE_author_is_recorded_ADVISORY(self):
        row = self.row()
        out, err = self.record(row["id"])
        self.assertIsNone(err, err)
        replayed = self.open_row(row["id"])
        # THE ROW STAYS OWED: an advisory read discharges nothing
        self.assertEqual(out["status"], "open")
        self.assertEqual(replayed["status"], "open")
        self.assertNotIn("polarity", replayed)
        read = self.reads(row["id"])[-1]
        self.assertEqual(
            {k: read.get(k) for k in dispatches.REVIEWER_FIELDS},
            {"reviewer_model": "fable", "reviewer_run": self.RUN,
             "author_model": self.AUTHOR, "author_model_source": "declared",
             "recorded_by": "integrator", "reviewer_family": "claude",
             "independence": "fable"})  # noqa: SEAT_NAME — the MODEL family of a Fable read, not a seat
        self.assertEqual(read["polarity"], "concur")
        self.assertEqual(read["reviewed_tip"], self.side)

    def test_an_OTHER_FAMILY_read_is_recorded_ADVISORY(self):  # noqa: VACUOUS_ASSERTION — every assertion is positive and unconditional: err is None, and the read's own fields or its count are compared to exact values off the replayed ledger
        row = self.row()
        out, err = self.record(row["id"], reviewer_model="gpt-6-astra")
        self.assertIsNone(err, err)
        self.assertEqual(self.open_row(row["id"])["status"], "open")
        read = self.reads(row["id"])[-1]
        self.assertEqual((read["reviewer_family"], read["independence"]),
                         ("codex", "cross-family"))

    def test_the_SAME_FAMILY_is_refused_even_as_another_model(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (out is None AND the error names the rule), and the absence of a read is read off the same ledger a sibling arm proves a read lands on
        """Opus 5.4 for an Opus 5.5 author, in the two spellings helm knows
        for a Claude Opus model. Both are Claude and neither is Fable."""
        row = self.row()
        for model in ("opus", "claude-opus-5"):
            with self.subTest(model=model):
                out, err = self.record(row["id"], reviewer_model=model)
                self.assertIsNone(out)
                self.assertIn("same family", err)
        self.assertEqual(self.reads(row["id"]), [])
        self.assertEqual(self.open_row(row["id"])["status"], "open")

    def test_an_UNRECOGNISED_model_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (out is None AND the error names the rule), and the absence of a read is read off the same ledger a sibling arm proves a read lands on
        row = self.row()
        for model in ("claude-opus-5-4", "made-up-model-9"):
            with self.subTest(model=model):
                out, err = self.record(row["id"], reviewer_model=model)
                self.assertIsNone(out)
                self.assertIn("does not recognise", err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_the_AUTHORS_OWN_MODEL_is_refused_as_the_reader(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (out is None AND the error names the rule), and the absence of a read is read off the same ledger a sibling arm proves a read lands on
        """Fable reading a Fable author's work is the author's own model,
        whatever the spelling."""
        row = self.row()
        out, err = self.record(row["id"], author_model="claude-fable-5-1",
                               reviewer_model="Fable")
        self.assertIsNone(out)
        self.assertIn("IS the author's model", err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_an_unknown_author_model_is_refused_not_assumed_different(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (out is None AND the error names the rule), and the absence of a read is read off the same ledger a sibling arm proves a read lands on
        row = self.row()
        out, err = self.record(row["id"], author_model=None)
        self.assertIsNone(out)
        self.assertIn("--author-model", err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_a_recorded_runtime_model_wins_and_a_contrary_claim_refuses(self):  # noqa: VACUOUS_ASSERTION — every assertion is positive and unconditional: err is None, and the read's own fields or its count are compared to exact values off the replayed ledger
        row = self.row()
        with mock.patch.object(dispatches, "_runtime_model",
                               return_value="claude-fable-5-1"):
            out, err = self.record(row["id"], author_model=self.AUTHOR)
        self.assertIsNone(out)
        self.assertIn("contradicts the author's runtime record", err)
        with mock.patch.object(dispatches, "_runtime_model",
                               return_value="gpt-6-astra"):
            out, err = self.record(row["id"], author_model=None)
        self.assertIsNone(err, err)
        read = self.reads(row["id"])[-1]
        self.assertEqual((read["author_model"], read["author_model_source"],
                          read["independence"]),
                         ("gpt-6-astra", "runtime", "cross-family"))

    def test_APPROVE_is_refused_and_names_concur(self):
        row = self.row()
        out, err = self.record(row["id"], polarity="approve")
        self.assertIsNone(out)
        self.assertIn("--concur", err)

    def test_a_SONNET_or_HAIKU_reader_is_refused(self):  # noqa: VACUOUS_ASSERTION — after the loop, the row is asserted to hold no read and a Fable read on the same row is asserted RECORDED, both unconditional
        """OWNER RULING: Sonnet and Haiku never review anything, and a Fable
        limit is never a reason to step down to one."""
        row = self.row()
        for model in ("claude-sonnet-4-5", "Claude-Haiku-4-5[1m]",
                      "sonnet", "anthropic/claude-3-5-haiku"):
            with self.subTest(model=model):
                out, err = self.record(row["id"], reviewer_model=model)
                self.assertIsNone(out)
                self.assertIn("never reviews anything", err)
        self.assertEqual(self.reads(row["id"]), [])
        out, err = self.record(row["id"], reviewer_model="fable")
        self.assertIsNone(err, err)
        self.assertEqual(len(self.reads(row["id"])), 1)
        self.assertIsNone(dispatches._NEVER_REVIEWS.search("unsonnetlike-1"))

    def test_a_model_without_a_run_is_refused(self):
        row = self.row()
        out, err = self.record(row["id"], reviewer_run=None)
        self.assertIsNone(out)
        self.assertIn("--reviewer-run", err)

    def test_a_seat_that_is_neither_sender_nor_recipient_is_refused(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (out is None AND the error names the rule), and the absence of a read is read off the same ledger a sibling arm proves a read lands on
        row = self.row()
        with mock.patch.object(dispatches, "_acting_author",
                               return_value=("stranger", None)):
            out, err = self.record(row["id"])
        self.assertIsNone(out)
        self.assertIn("@stranger is neither", err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_a_retry_of_one_run_records_it_once(self):  # noqa: VACUOUS_ASSERTION — every assertion is positive and unconditional: err is None, and the read's own fields or its count are compared to exact values off the replayed ledger
        row = self.row()
        for _ in range(2):
            out, err = self.record(row["id"])
            self.assertIsNone(err, err)
        self.assertEqual(len(self.reads(row["id"])), 1)

    def test_CONTROL_a_plain_verdict_is_untouched(self):
        """Without the fields the write is the seat's own, exactly as before:
        the author proof binds, the row closes, and no read is advisory."""
        row = self.row()
        out, err = self.mark_verdict(row["id"], self.side, "read clean",
                                     polarity="concur", basis="measured")
        self.assertIsNone(err, err)
        self.assertIn("verdict_author_session", out)
        self.assertEqual(out["status"], "verdict")
        self.assertNotIn("advisory_reads", out)

    def test_the_cli_parses_the_flags_and_says_ADVISORY(self):  # noqa: VACUOUS_ASSERTION — every assertion is positive and unconditional: rc == 0 from the real CLI, the said-back lines present in its stdout, and the run id replayed off the ledger
        import contextlib
        import io
        row = self.row()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = dispatches.cmd_dispatch(
                ["verdict", row["id"], self.side, "--concur", "--measured",
                 "--reviewer-model", "fable", "--reviewer-run", self.RUN,
                 "--author-model", self.AUTHOR, "read", "clean"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("ADVISORY read by model fable (run %s," % self.RUN,
                      out.getvalue())
        self.assertIn("stays OWED", out.getvalue())
        self.assertEqual(self.reads(row["id"])[-1]["reviewer_run"], self.RUN)
        self.assertEqual(self.open_row(row["id"])["status"], "open")


class EveryNoReviewerOutcomeCarriesTheLadderTest(unittest.TestCase):
    """GAP 3 of the cross-family read: when every candidate was mid-turn,
    `helm reviewers` printed "NOBODY IS FREE ... WAIT" and no ladder, which
    tells the reader to wait — against the owner directive."""

    def render(self, **seams):
        from helm import reviewer_eligibility as re_
        from tests import test_reviewer_eligibility as t
        report, err = re_.eligibility("row-1", seams=t._seams(**seams))
        self.assertIsNone(err)
        return report, "\n".join(re_.render(report))

    def test_ALL_BUSY_carries_the_ladder_and_never_WAIT(self):
        from tests import test_reviewer_eligibility as t
        report, text = self.render(liveness=t._liveness(
            **{"seat-a": "RUNNING", "seat-b": "RUNNING"}))
        self.assertEqual(report["eligible"], [])
        self.assertIn("mid-turn", text)
        self.assertIn("NO REVIEWER IS NEVER A BLOCKER", text)
        self.assertNotIn("WAIT", text)

    def test_CONTROL_an_eligible_seat_prints_no_ladder(self):
        report, text = self.render()
        self.assertIn("seat-a", report["eligible"])
        self.assertNotIn("NO REVIEWER IS NEVER A BLOCKER", text)


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


class TheTaughtSurfacesNameTheLadderTest(unittest.TestCase):
    """GAPS 3 AND 5. A rule only /x carried was a rule the seats that load
    /build or the review procedure never met."""

    # /x is NEVER-TRACKED (.gitignore, tests/test_never_track.py): it is
    # edited in place on the host and cannot be asserted from a tree.
    SKILLS = ("agents/claudecode/skills/build/SKILL.md",
              "agents/claudecode/skills/reviewer-implements-own-findings/"
              "SKILL.md")

    def test_each_skill_names_every_rung_and_the_fable_wall(self):  # noqa: VACUOUS_ASSERTION — the absence of the removed rung is read off the same text the loop just asserted five phrases present in, and seen == 2 is asserted unconditionally after it
        seen = 0
        for rel in self.SKILLS:
            text = read(rel)
            with self.subTest(skill=rel):
                for word in ("Fable one-agent Workflow", SEAT, "other-family",
                             "Sonnet and Haiku never review",
                             "another credential or seat",
                             "You have reached your Fable limit",
                             "--reviewer-model"):
                    self.assertIn(word, text, word)
                self.assertNotIn(REMOVED_RUNG, text)
            seen += 1
        self.assertEqual(seen, 2)


if __name__ == "__main__":
    unittest.main()
