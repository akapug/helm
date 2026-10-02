#!/usr/bin/env python3
"""task/2948: "no cross-family reviewer" is never a blocker, anywhere in helm.

THE OWNER RULE (store premise
review-routing-is-cheapest-reader-that-clears-the-bar-measured, with the
integrator's reversibility ruling): the
cheapest reader that clears the bar, in this order. A fresh-context OPUS read
is the DEFAULT (the Agent tool from an Opus seat), and a full review leg on a
reversible lane. A lane that touches prod, a migration, a deletion, money or
credentials needs ONE approval-tier read by a reader that is NOT the author,
chosen by `helm burn` and judged on the resolved model: claude on Opus 5.5 as
a fresh-context, non-author read, codex, ds4pro on V4 Pro, kimi or grok. A
different family is not required for that one read (the owner confirmed it,
room row 2217, after room row 2104's "I think opus seats should be in the upper
tier, we are probably eating lots of tokens on extra rounds"); among readers who
clear the bar, prefer another lane over an Opus agent, especially a local seat
once the owner admits it, weighed case by case and never a fixed list. Gemini,
codex-spark and local seats read as input only until the owner admits them on
their record. When no tier reader can take a
door read, it PARKS, with gemini reading meanwhile as input only. FABLE IS NO
RUNG AND NO FALLBACK (task/3202,
the owner's ruling as the integrator reconciled it in room row 1919: "no more
automatia fable slots, just for max qc for the most important stuff"): it is
for max QC on the most important work only, a Fable token costs about 3 Opus
tokens, and it is not used while `helm burn` reads anthropic ORANGE or worse.
Sonnet and Haiku never review anything, and no local seat is "always" a free
lane (qwen27 is prefill-bound).

The arms below pin the six gaps the owner's directive named, each at the
surface a seat actually reads, and the six the task/3202 rulings added:

  1  the dispatch door's UNUSABLE refusal names the fallback and its commands
  2  a DEAF seat whose pane is LIVE says a keystroke wakes it
  3  the skills and the store name the rung after a Fable quota wall
  4  a model run's read can be recorded on the seat-bound ledger, and a seat
     cannot file its own model's read as another model's
  5  /build and reviewer-implements-own-findings name the fallback
  6  the recorded read is printed where the row is read: `helm lr show` and
     `helm dispatch triage` (task/3081)
  7  a read on a row later CANCELLED or REBOUND rides the row that continues
     it, and `lr show <cancelled id>` opens that row (task/3081)
  a  a door read no tier reader can take PARKS, and never falls to Fable
  b  Fable is named only for max QC, with its cases, its price and the burn
     bar
  c  MUST-MISS: no surface calls Fable a default, a last rung or an
     automatic fallback
  d  rung 2 names the approval tier whole, the fresh-context non-author Opus
     read included, judged on the resolved model and chosen by `helm burn`
  e  gemini, codex-spark and local seats read as input only until the owner
     admits them on their record
  f  MUST-MISS: no surface says a door needs a different family, lists gemini
     or a local seat as an approval reader, or calls Opus input only
"""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (burnflags, dispatches, eventledger, landreq,  # noqa: E402
                  obligation, owedpush, route, seat_usability)
# The module, never its TestCase: tests/test_suite_collection.py says why.
from tests import test_landreq as _landreq  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: What every fallback surface must name. The rung words and the two commands
#: a reader types, so a surface that shrank to "use the fallback" goes red.
LADDER = ("fresh-context Opus", "the Agent tool", "REVERSIBLE", "codex",
          "approval-tier read", "PARKS", "Fable", "max QC",
          "another credential or seat",
          "Sonnet and Haiku never review", "helm reviewers",
          "helm dispatch send <seat>", "--reviewer-model")
#: (a) WHAT A DOOR READ DOES WHEN NO TIER READER CAN TAKE IT: it parks until
#: one can, and gemini's read meanwhile is input, not the review. Phrases,
#: read off whitespace-normalised text, so a skill's hard wrap cannot split
#: one.
PARKS_WORDS = ("PARKS until a tier reader can take it",
               "gemini reading meanwhile as input only")
#: (b) THE ONLY DOOR FABLE HAS: the max-QC cases, its price and the burn bar.
#: The rung opens with FABLE_RUNG; before it, the tier names Fable only in
#: NEVER_OVER_OPUS (the owner, room row 2217).
FABLE_RUNG = "Fable is for max QC only"
NEVER_OVER_OPUS = "never prefer Fable over Opus automatically"
#: ONLY ON THE OWNER'S ASK (task/3855, the owner: "Fable? we
#: haven't used Fable in days. why did we just start again now?"). The old
#: case list ended "or a money or creds door with no other reader", and that
#: clause is how a door read with every author Opus or codex went to Fable.
MAX_QC_CASES = "it reads only when the owner asks for it"
#: The retired case list, which no ladder surface may teach again.
RETIRED_MAX_QC_CASES = "a money or creds door with no other reader"
MAX_QC_WORDS = ("max QC", MAX_QC_CASES, "3 Opus tokens per Fable token",
                "helm burn", "anthropic ORANGE or worse")
#: (c) MUST-MISS: Fable named as a last rung, a default, or a fallback the
#: ladder falls to by itself. Each alternative is a POSITIVE claim, so a
#: surface may still say Fable is never one; the arm that uses it first
#: proves it fires on every sentence the ruling retired.
FABLE_AS_FALLBACK = re.compile(
    r"(?i)last[- ]resort|\bfable,? last\b|\(3\) fable"
    r"|\bfall(?:s|ing)? (?:back|through) to fable\b"
    r"|\bfable\b[^.;:]{0,30}\b(?:is|as) (?:the |a |an )?"
    r"(?:default|fallback|automatic)")


def flat(text):
    """One surface's words with markdown bold and hard wraps dropped."""
    return " ".join(text.replace("**", "").split())


def past_the_opus_clause(case, words):
    """`words` with rung 2's NEVER_OVER_OPUS clause blanked in place, so an
    index into it is an index into `words`: that clause names Fable only to
    say it is never preferred over Opus, and every other naming is the one
    an arm judges. The clause must be there, or blanking it proves nothing."""
    case.assertIn(NEVER_OVER_OPUS, words)
    return words.replace(NEVER_OVER_OPUS, "_" * len(NEVER_OVER_OPUS))


def assert_fable_max_qc_only(case, text, parks=True):
    """THE OWNER'S RULING ON FABLE, read off one surface (task/3202): (a)
    with `parks`, a door read no tier reader can take PARKS until one can,
    gemini reading meanwhile as input only, and the park is named before
    Fable; (b) Fable is named for max QC with its
    cases, its price and the burn bar; (c) nothing calls Fable a default, a
    last rung or an automatic fallback."""
    words = flat(text)
    for word in (PARKS_WORDS if parks else ()) + MAX_QC_WORDS:
        case.assertIn(word, words, word)
    if parks:
        case.assertLess(words.index(PARKS_WORDS[0]), words.index("max QC"),
                        words)
    hit = FABLE_AS_FALLBACK.search(words)
    case.assertIsNone(hit, hit and words[max(0, hit.start() - 80):
                                         hit.end() + 80])


#: (d) RUNG 2 (the owner's ruling as meta-claude reconciled it, room row
#: 2104, over the owner-revised approval-tier store prior named here): ONE
#: approval-tier read by a reader that is not the author, the tier named
#: whole with the fresh-context non-author Opus read in it, judged on the
#: resolved model and chosen by `helm burn`.
APPROVAL_TIER_PRIOR = "approval-tier-2026-08-11-owner-revised"
TIER_WORDS = ("ONE approval-tier read by a reader that is NOT the author",
              "claude on Opus 5.5 as a fresh-context, non-author read",
              "never the author's own context",
              "codex, ds4pro on V4 Pro, kimi or grok",
              "the cursor route included", "chosen by `helm burn`",
              "RESOLVED model", APPROVAL_TIER_PRIOR,
              "A different family is not required for that one read",
              "the owner confirmed it", "prefer another lane over an Opus agent",
              "especially a local seat once the owner admits it",
              "never prefer Fable over Opus automatically",
              "the exact order is weighed case by case, never a fixed list")
#: The families the tier names, spelled as helm's own tier check spells
#: them once its native family reads as claude.
TIER_FAMILIES = ("claude", "codex", "ds4pro", "kimi", "grok")
#: (e) WHO READS A DOOR AS INPUT ONLY until the owner admits them on their
#: record; a local seat's measured record is put to him, never an automatic
#: admission.
INPUT_ONLY_WORDS = ("Gemini, codex-spark and local seats read as INPUT only "
                    "until the owner admits them on their record",
                    "5 door reads with no miss",
                    "never an automatic admission")
#: (f) MUST-MISS, three shapes the rulings retired: a door that needs a
#: different model or family; gemini or a local seat listed beside a tier
#: family as a reader (a model the tier excludes, such as codex-spark, is
#: not that family), or a local seat admitted once its throughput is
#: measured; and Opus named as input only. Each is a POSITIVE claim, so
#: "a different family is not required for that one read" passes; the CONTROL arm proves
#: each fires on the retired sentences.
DIFFERENT_FAMILY_REQUIRED = re.compile(
    r"(?i)\b(?:owes?|needs?|requires?|required)\b[^.;:]{0,40}?"
    r"\b(?:different[- ](?:model|family)|another (?:model|family)"
    r"|other[- ]family|cross-family|family other than)"
    r"|\b(?:a|one) (?:different[- ](?:model|family)|other[- ]family)"
    r"(?:'s)? read\b"
    r"|\ba review is (?:another|a different)[- ](?:model|family)\b")
READER_AS_APPROVER = re.compile(
    r"(?i)\b(?:cursor|ds4pro|codex|kimi|grok|claude),? (?:or |and )?gemini\b"
    r"|\bgemini,? (?:or |and )?"
    r"(?:cursor|ds4pro|codex|kimi|grok|claude)\b(?!-)"
    r"|\bor a local seat\b|\bonce its outcomes per hour are measured\b"
    r"|\bcheap other family\b|\blocal seats first\b")
OPUS_AS_INPUT = re.compile(
    r"(?i)\blocal seats,? and opus\b|\bopus (?:reads?|read) as input")
RUNG_TWO_MUST_MISS = (DIFFERENT_FAMILY_REQUIRED, READER_AS_APPROVER,
                      OPUS_AS_INPUT)


def rung_two_hit(text):
    """The first retired rung-2 claim in `text`, with its context, or None."""
    words = flat(text)
    for rule in RUNG_TWO_MUST_MISS:
        hit = rule.search(words)
        if hit:
            return words[max(0, hit.start() - 80):hit.end() + 80]
    return None


def assert_approval_tier_rung(case, text):
    """RUNG 2, read off one surface (task/3202, room row 2104): (d) it names
    one approval-tier read by a reader that is not the author, the tier whole
    with the fresh-context non-author Opus read, the resolved model, the
    store prior and `helm burn` as the chooser; (e) gemini, codex-spark and
    local seats read as input only until the owner admits them; (f) none of
    the retired rung-2 claims survives."""
    words = flat(text)
    for word in TIER_WORDS + INPUT_ONLY_WORDS:
        case.assertIn(word, words, word)
    case.assertIsNone(rung_two_hit(text))


#: The rungs the owner's rulings removed. No surface may teach them again:
#: the same-family step-down, a local seat that is "always" a free lane, and
#: another family or Fable as the first rung.
REMOVED_RUNGS = ("same-family different model", "is always one",
                 "always among them", "always another family",
                 "(1) any other-family seat", "(2) a Fable one-agent Workflow")


def assert_opus_first_fable_last(case, text, once=True):
    """THE OWNER'S ORDER, read off one surface's words: the fresh-context
    Opus read is named before the approval-tier rung, which is named
    before Fable, and Fable is named nowhere earlier. Sonnet and Haiku are
    named to say they never review (and, with `once`, nowhere else), and no
    removed rung survives."""
    opus = text.index("fresh-context Opus")
    other = text.index("codex")
    fable = text.index("Fable")
    case.assertLess(opus, other, text)
    case.assertLess(other, fable, text)
    case.assertIn("Sonnet and Haiku never review", text)
    if once:
        for word in ("Sonnet", "Haiku"):
            case.assertEqual(text.count(word), 1, (word, text))
    for gone in REMOVED_RUNGS:
        case.assertNotIn(gone, text, gone)


#: Where a door refusal's remedy starts. The head of a refusal names the
#: models it refused (Fable or Sonnet among them), and the order is the
#: remedy's.
REMEDY_HEAD = "Take another approval-tier read instead"


def assert_cheap_first_fable_max_qc(case, err):
    """THE DOOR'S REMEDY IN THE OWNER'S ORDER. The door refused a model run's
    read, and its rule is not this arm's subject: what it asserts is that the
    remedy names the approval tier `helm burn` chooses from, then the PARK
    when no tier reader can take it, and Fable only after that and only for
    max QC, and names no seat as always free. Before the park, Fable is
    named only to say it is never preferred over Opus automatically."""
    remedy = err[err.index(REMEDY_HEAD):]
    case.assertIsNone(rung_two_hit(err[:err.index(REMEDY_HEAD)]))
    park = remedy.index(PARKS_WORDS[0])
    case.assertLess(remedy.index("codex"), park, remedy)
    case.assertLess(park, remedy.index(FABLE_RUNG), remedy)
    case.assertEqual(remedy[:park].count("Fable"),
                     remedy[:park].count(NEVER_OVER_OPUS), remedy)
    case.assertIn("Sonnet and Haiku never review", remedy)
    assert_approval_tier_rung(case, remedy)
    assert_fable_max_qc_only(case, remedy)
    for gone in REMOVED_RUNGS:
        case.assertNotIn(gone, err, gone)
    case.assertNotIn("qwen27", remedy)


class TheDispatchDoorNamesTheFallbackTest(unittest.TestCase):
    """GAP 1. The door refused with "repair it or pass --force" and nothing
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
        self.assertIn("--force", refusal)
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
        self.assertIn("helm reviewers abcdef012345", text)
        self.assertIn("helm dispatch send <seat> lane/x --ref %s --kind review "
                      "--supersedes abcdef012345" % ("f" * 40), text)
        self.assertIn("helm dispatch verdict abcdef012345 " + "f" * 40, text)
        # the Fable limit is still named, in the owner's words, for the max-QC
        # read that does take Fable
        self.assertIn("You have reached your Fable limit", text)
        # THE OWNER'S ORDER: Opus first, the approval tier where the lane
        # touches a door, a PARK when no tier reader can take it, and Fable
        # only after it, for max QC
        self.assertLess(text.index("(1) a fresh-context Opus read"),
                        text.index("(2) "))
        self.assertLess(text.index("(2) "), text.index("approval-tier read"))
        self.assertLess(text.index("(2) "), text.index("PARKS"))
        self.assertLess(text.index("PARKS"), text.index("max QC"))
        self.assertLess(text.index("max QC"),
                        text.index("another credential or seat"))
        assert_opus_first_fable_last(self, text)
        assert_fable_max_qc_only(self, text)
        assert_approval_tier_rung(self, text)
        # AND IT PROMISES ONLY THE RECORD THE DOOR TAKES: an Opus read is
        # recorded on a door lane as on a reversible one, and it authorizes
        # no land or close, so a door still takes a seat's tier read
        self.assertIn("recorded as `fresh-context run <id>` on a door lane "
                      "as on a REVERSIBLE one", text)
        self.assertIn("it authorizes no land or close", text)

    def test_the_ladder_names_no_seat_as_always_free(self):
        """qwen27 is prefill-bound: no local seat is "always" the free lane.
        A local seat reads as input until the owner admits it on its measured
        record, so the ladder names the evidence put to him, and the verb that
        says who can take the row NOW, and never hard-codes a seat into the
        send command."""
        text = dispatches.review_fallback_text()
        self.assertNotIn("qwen27", text)
        self.assertIn("5 door reads with no miss", text)
        self.assertIn("helm reviewers <row>", text)


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
        assert_fable_max_qc_only(self, text)
        assert_approval_tier_rung(self, text)

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
        # and the fourth, in the owner's order: THIS is the sentence owed-bot
        # DMs every hour, and it still read "another family ... (the qwen27
        # seat is always one), or Fable does through a one-agent Workflow"
        # after the owner ruled Opus first and Fable last; and it still read
        # "Fable is the LAST resort" after the owner ruled Fable max QC only
        self.assertIn("--reviewer-model", what)
        self.assertIn("the Agent tool", what)
        self.assertIn("REVERSIBLE", what)
        self.assertIn("PARKS", what)
        assert_opus_first_fable_last(self, what)
        assert_fable_max_qc_only(self, what)
        assert_approval_tier_rung(self, what)
        self.assertNotIn("qwen27", what)
        self.assertIn("`fresh-context run <id>` on a door lane as on a "
                      "REVERSIBLE one", what)
        self.assertIn("it authorizes no land or close", what)


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

    def test_an_OPUS_reader_of_a_CLAUDE_author_is_judged_by_its_run(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (out is None AND the error names the run's bound and the remedy), and the absence of a read is read off the same ledger a sibling arm proves a read lands on
        """Opus 5.4 for an Opus 5.5 author, in the two spellings helm knows
        for a Claude Opus model. Both are Claude and neither is Fable, so the
        read counts only as a fresh-context run (task/3855): with no run on
        disk the refusal names that bound, never "the same family", and the
        remedy it names is the owner's order."""
        row = self.row()
        for model in ("opus", "claude-opus-5"):
            with self.subTest(model=model):
                out, err = self.record(row["id"], reviewer_model=model)
                self.assertIsNone(out)
                self.assertIn("helm judges it as a fresh-context run", err)
                self.assertNotIn("same family", err)
                assert_cheap_first_fable_max_qc(self, err)
        self.assertEqual(self.reads(row["id"]), [])
        self.assertEqual(self.open_row(row["id"])["status"], "open")

    def test_the_AUTHORS_EXACT_MODEL_is_refused_and_names_the_owners_order(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (out is None AND the error names the run's bound and the remedy), and the absence of a read is read off the same ledger a sibling arm proves a read lands on
        """THE LIVE REFUSAL: "the reviewing model opus IS the author's model
        opus ... Get another family's read (the qwen27 seat is always one)
        or Fable's". An Opus read of Opus work is judged by its run
        (task/3855), so with no run on disk the refusal names that bound and
        never says the read is not independent; its remedy names the
        approval tier, the park when none can take it, and Fable only on the
        owner's ask."""
        row = self.row()
        out, err = self.record(row["id"], reviewer_model="opus",
                               author_model="opus")
        self.assertIsNone(out)
        self.assertIn("helm judges it as a fresh-context run", err)
        self.assertNotIn("not an independent review", err)
        assert_cheap_first_fable_max_qc(self, err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_a_same_family_codex_model_is_refused_with_the_remedy(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (out is None AND the error names the rule and the remedy), and the absence of a read is read off the same ledger a sibling arm proves a read lands on
        """The same-family refusal's other face: a codex model reading another
        codex model's work. It named "the qwen27 seat is always another
        family"."""
        row = self.row()
        out, err = self.record(row["id"], reviewer_model="gpt-5.6-sol",
                               author_model="gpt-6-astra")
        self.assertIsNone(out)
        self.assertIn("same family", err)
        assert_cheap_first_fable_max_qc(self, err)
        self.assertEqual(self.reads(row["id"]), [])

    def test_the_findings_pass_model_is_refused_with_the_remedy(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted POSITIVELY (given is None AND the error names the rule and the remedy)
        """The findings-pass refusal named "(the qwen27 seat is always one)"
        while refusing qwen27's own read."""
        given, err = dispatches._on_behalf_shape(
            dispatches.FINDINGS_READER, "wf-1", self.AUTHOR, "concur")
        self.assertIsNone(given)
        self.assertIn("findings pass's model", err)
        assert_cheap_first_fable_max_qc(self, err)

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
        assert_cheap_first_fable_max_qc(self, err)
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
                assert_cheap_first_fable_max_qc(self, err)
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

    def test_the_wire_card_carries_the_read_marked(self):  # noqa: VACUOUS_ASSERTION — the plain row's card is asserted to lack the key before the advisory row's is asserted to carry the marked lines
        """kimi's read of this lane: the kanban card is where a lane owner
        reads the row, and it dropped the reads — the same invisibility the
        CLI side cured. The card carries the server's own rendered lines."""
        from helm import landreq_cli
        # card() reads the PROJECTION, never the raw row: the fields it
        # requires are the board's shape, so the rows go through a real
        # `_lr`-shaped projection, not a hand-built one.
        def projected(r):
            return dict(r, state="AWAITING_REVIEW", branch="b",
                        review_sha=None, review_sha_full=None,
                        reviewer=None, superseded=False, landed=False,
                        merged_local=False, observable=True, terminal=False,
                        owed_by=None, author=None, dwell_s=0,
                        close_reason=None, stalled=False)
        row = self.row()
        # the plain row's card, BEFORE any read is recorded on it
        self.assertNotIn("advisory_lines",
                         landreq_cli.card(projected(self.open_row(row["id"]))))
        out, err = self.record(row["id"])
        self.assertIsNone(err, err)
        card = landreq_cli.card(projected(self.open_row(row["id"])))
        self.assertIn("advisory_lines", card)
        text = " ".join(card["advisory_lines"])
        self.assertIn("ADVISORY", text)
        self.assertIn("discharges nothing", text)

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


class AnAdvisoryReadIsPrintedWhereTheRowIsReadTest(_landreq.LandReqBase):
    """GAP 6 (task/3081). The read was recorded — rc 0, said back as ADVISORY
    — and then NOTHING that reads the row printed it: `helm lr show` read
    `verdict (none)`, and `helm dispatch triage` printed no line and no patch
    tip. On the live row (1589b672c6bb, task/3043) the lane owner concluded
    the write had failed and asked the integrator to check its rc.

    One arm per surface x state row of the brief. Every arm records through
    the real verb (`dispatch verdict ... --reviewer-model`) and reads back
    through the two real CLI entry points, `lr show` and `dispatch triage`.
    The fixture row is DELIVERED, so its lifecycle reads AWAITING_REVIEW, the
    live case's stage."""

    AUTHOR = "claude-opus-5-5"
    HEAD = "ADVISORY read by model "
    CLOSE = "ADVISORY: "

    def setUp(self):
        super().setUp()
        self.rid = self.dispatch(ref=self.side, lane="lane/advisory-visible",
                                 kind="review")["id"]
        dispatches._mark_delivered(self.rid, "post-advisory")

    def cli(self, verb, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = verb(args)
        return rc, out.getvalue(), err.getvalue()

    def advise(self, run, polarity="--concur", model="fable", *extra):
        rc, out, err = self.cli(dispatches.cmd_dispatch, [
            "verdict", self.rid, self.side, polarity, "--measured",
            "--reviewer-model", model, "--reviewer-run", run,
            "--author-model", self.AUTHOR, *extra, "evidence", "of", run])
        self.assertEqual(rc, 0, out + err)
        self.assertIn(self.HEAD + model, out)
        return out

    def show(self):
        rc, out, err = self.cli(landreq.cmd_lr, ["show", self.rid])
        self.assertEqual(rc, 0, err)
        return out

    def triage(self):
        rc, out, err = self.cli(dispatches.cmd_dispatch, ["triage", self.rid])
        self.assertEqual(rc, 0, err)
        return out

    def cure(self):
        """A model run's committed cure: one commit off the reviewed tip, on
        its own branch so the reviewed branch does not move."""
        self.git("checkout", "-q", "-b", "run-cure", self.side)
        try:
            return self.commit("the run's cure", path="cure")
        finally:
            self.git("checkout", "-q", self.main)

    def block(self, text):
        """The advisory block one surface printed, indentation dropped: from
        the first read's header through the one closing sentence. [] when
        the surface printed no header at all."""
        lines = [line.strip() for line in text.splitlines()]
        start = next((i for i, line in enumerate(lines)
                      if line.startswith(self.HEAD)), None)
        if start is None:
            return []
        end = next((i for i in range(start, len(lines))
                    if lines[i].startswith(self.CLOSE)), len(lines) - 1)
        return lines[start:end + 1]

    def both(self):
        """(lr show text, triage text, the advisory block they share). The
        two surfaces print ONE renderer's lines, so the block is asserted
        equal here and every arm's assertions on it bind both surfaces."""
        shown, triaged = self.show(), self.triage()
        block = self.block(shown)
        self.assertEqual(block, self.block(triaged),
                         "lr show and triage word the reads differently:\n"
                         "%s\n----\n%s" % (shown, triaged))
        return shown, triaged, block

    def test_an_advisory_FIX_prints_its_run_and_patch_tip_on_both_surfaces(self):
        """THE LIVE CASE, and the arm that is red on trunk: a FIX with a
        patch tip and no seat verdict. The lifecycle does not move."""
        before = self.show().splitlines()[0]
        patch = self.cure()
        self.advise("wf-fix", "--fix", "fable", "--worse-than-main", "g",
                    "--patch-tip", patch)
        shown, triaged, block = self.both()
        self.assertTrue(block, "no advisory read printed:\n%s\n----\n%s"
                        % (shown, triaged))
        self.assertTrue(block[0].startswith(
            "ADVISORY read by model fable (run wf-fix, fable: family "
            "claude) — FIX at %s" % self.side[:12]), block[0])
        self.assertIn("patch tip %s" % patch[:12], "\n".join(block))
        self.assertIn("exit: WORSE-THAN-MAIN: g", block)
        self.assertIn("evidence: evidence of wf-fix", block)
        self.assertIn("stays OWED", block[-1])
        # AND THE LIFECYCLE DID NOT MOVE: the headline is byte-identical and
        # the seat verdict line still reads none.
        self.assertEqual(shown.splitlines()[0], before)
        self.assertIn("AWAITING_REVIEW", before)
        self.assertIn("  verdict   (none)   reviewed -", shown)
        self.assertTrue(triaged.startswith(self.rid[:12]), triaged)

    def test_an_advisory_CONCUR_prints_with_no_patch_line(self):
        self.advise("wf-concur")
        _shown, _triaged, block = self.both()
        self.assertIn("— CONCUR at %s" % self.side[:12], block[0])
        self.assertIn("evidence: evidence of wf-concur", block)
        self.assertIn("stays OWED", block[-1])
        # the same block's lines, read unconditionally above, and the FIX
        # arm's patch line are this absence's positive controls
        self.assertEqual([line for line in block
                          if line.startswith("patch tip")], [])

    def test_a_seat_verdict_after_an_advisory_read_is_the_lifecycle_and_both_print(self):
        self.advise("wf-first")
        row, err = self.mark_verdict(self.rid, self.side, "seat read clean",
                                     polarity="concur", basis="measured")
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "verdict")
        shown, triaged, block = self.both()
        # THE SEAT VERDICT IS THE LIFECYCLE on both surfaces
        self.assertIn("  verdict   seat read clean   reviewed %s" % self.side,
                      shown)
        self.assertIn("REVIEWED", shown.splitlines()[0])
        self.assertIn("not triaged: closed by CONCUR verdict",
                      triaged.splitlines()[0])
        # AND THE READ STILL PRINTS, saying it moved nothing
        self.assertIn("(run wf-first,", block[0])
        self.assertIn("the seat verdict is this row's lifecycle", block[-1])
        self.assertNotIn("stays OWED", block[-1])

    def test_CONTROL_a_row_with_no_advisory_read_prints_what_it_did(self):
        """Nothing is added to a row that carries no read, and the projection
        does not grow a key: `lr show --json` is byte for byte what it was.
        The positive control is the same row, read the same two ways, once a
        read is recorded on it."""
        shown, triaged, block = self.both()
        self.assertTrue(shown.startswith("LAND REQUEST %s" % self.rid), shown)
        self.assertTrue(triaged.startswith(self.rid[:12]), triaged)
        self.assertEqual(block, [])
        self.assertNotIn("ADVISORY", shown + triaged)
        lr, err = landreq.get(self.rid)
        self.assertIsNone(err, err)
        self.assertNotIn("advisory_reads", lr)
        # POSITIVE CONTROL, same row and same observables
        self.advise("wf-control")
        _shown, _triaged, block = self.both()
        self.assertIn("(run wf-control,", block[0])
        self.assertIn("advisory_reads", landreq.get(self.rid)[0])

    def test_the_advisory_lines_are_ADDED_and_no_other_line_moves(self):
        """What makes the no-read row byte-identical is that the read only
        ever ADDS one contiguous block: the same projection rendered without
        its reads is the page with that block cut out."""
        self.advise("wf-add")
        lr, err = landreq.get(self.rid)
        self.assertIsNone(err, err)
        self.assertEqual([r["reviewer_run"] for r in lr["advisory_reads"]],
                         ["wf-add"])
        page = landreq._render_show(lr).splitlines()
        bare = dict(lr)
        del bare["advisory_reads"]
        without = landreq._render_show(bare).splitlines()
        block = self.block("\n".join(page))
        self.assertTrue(block, page)
        start = next(i for i, line in enumerate(page)
                     if line.strip().startswith(self.HEAD))
        self.assertEqual(page[:start] + page[start + len(block):], without)

    def test_two_advisory_reads_print_oldest_first_on_both_surfaces(self):
        self.advise("wf-older")
        self.advise("wf-newer", "--fix", "gpt-6-astra", "--worse-than-main",
                    "g", "--no-patch-because",
                    "a design finding bound for a meld")
        _shown, _triaged, block = self.both()
        heads = [line for line in block if line.startswith(self.HEAD)]
        self.assertEqual(len(heads), 2, block)
        self.assertIn("(run wf-older,", heads[0])
        self.assertIn("— CONCUR at", heads[0])
        self.assertIn("(run wf-newer,", heads[1])
        self.assertIn("— FIX at", heads[1])
        self.assertIn("no cure committed, because: a design finding bound "
                      "for a meld", block)
        self.assertEqual(sum(line.startswith(self.CLOSE) for line in block), 1)

    def test_a_CANCELLED_row_with_a_read_says_it_ENDED_on_the_one_surface_that_reads_it(self):
        """A row can be cancelled after a read was recorded (the reducer takes
        the read on an open row, and cancel takes an open row). A cancelled
        row is no land request — `lr` filters CANCELLED structurally, so `lr
        show` refuses it as it did before — and `dispatch triage` is the one
        surface that reads it: its closing line says the row ENDED, never
        that it stays owed."""
        self.advise("wf-cancelled")
        row, err = dispatches.mark_cancel(self.rid, "work moot")
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "cancelled")
        rc, out, err = self.cli(landreq.cmd_lr, ["show", self.rid])
        self.assertEqual(rc, 1, out + err)
        self.assertIn("no such land request", err)
        triaged = self.triage()
        block = self.block(triaged)
        self.assertTrue(block, triaged)
        self.assertIn("(run wf-cancelled,", block[0])
        self.assertIn("ended on its own lifecycle", block[-1])
        self.assertNotIn("stays OWED", block[-1])

    def test_a_FIX_verdicted_row_whose_cure_landed_still_prints_its_reads_on_triage(self):
        """The commit says triage prints the reads under a named row that is
        open, verdicted or carried. A FIX-verdicted row whose author cured on
        the lane is a verdicted row that triage lists under CURE AWAITING
        REVIEW instead of the skipped loop; the read must print there too."""
        self.advise("wf-cured", "--fix", "fable", "--worse-than-main", "g",
                    "--no-patch-because", "a design finding bound for a meld")
        row, err = self.mark_verdict(self.rid, self.side, "seat found g worse",
                                     polarity="fix", basis="measured",
                                     worse_than_main_paths=["g"],
                                     no_patch_because="design finding")
        self.assertIsNone(err, err)
        self.assertEqual(row["polarity"], "fix")
        # THE AUTHOR CURES ON THE LANE: one commit ahead of the reviewed tip on
        # the row's own branch, which the cure census reads.
        self.git("checkout", "-q", "-b", "lane/advisory-visible", self.side)
        try:
            self.commit("the author's cure", path="cured")
        finally:
            self.git("checkout", "-q", self.main)
        shown, triaged = self.show(), self.triage()
        # MUST-HIT: the row really took the CURED path, or this arm proves
        # nothing about it.
        self.assertIn("CURED", triaged, triaged)
        self.assertIn("(run wf-cured,", shown)
        block = self.block(triaged)
        self.assertTrue(block, "triage printed no advisory read for the "
                        "cured row:\n%s" % triaged)
        self.assertEqual(block, self.block(shown))


class AReadOnACancelledRowRidesItsSuccessorTest(_landreq.LandReqBase):
    """GAP 7, the 09-28 rescore's remainder (task/3081). A read recorded on a
    row that is later CANCELLED or REBOUND disappeared: `lr show` cannot open
    a cancelled id, and the row that continues it printed only its own reads.

    The planted row holds both kinds of read — a model run's ADVISORY read and
    a SOURCE-CLEAN hold by its recipient — and is cancelled and continued with
    `--supersedes`; the rebind arm moves a read row through the real verb."""

    AUTHOR = "claude-opus-5-5"
    HOLD = "read clean, awaiting the land gate; fab Ran 5 tests OK"

    def setUp(self):
        super().setUp()
        self.gone = self.dispatch(ref=self.side, lane="lane/carried-reads",
                                  kind="review")["id"]
        dispatches._mark_delivered(self.gone, "post-carried")

    def cli(self, verb, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = verb(args)
        return rc, out.getvalue(), err.getvalue()

    def advise(self, rid, run):
        rc, out, err = self.cli(dispatches.cmd_dispatch, [
            "verdict", rid, self.side, "--concur", "--measured",
            "--reviewer-model", "fable", "--reviewer-run", run,
            "--author-model", self.AUTHOR, "evidence", "of", run])
        self.assertEqual(rc, 0, out + err)

    def planted(self, read=True):
        """The successor's id: the planted row (read and held when `read`)
        is cancelled, and a new round continues it with --supersedes."""
        if read:
            self.advise(self.gone, "wf-gone")
            with mock.patch.object(dispatches, "_acting_author",
                                   return_value=("codex-3", None)):
                _row, why = dispatches.mark_hold(
                    self.gone, self.HOLD, source_clean_tip=self.side)
            self.assertIsNone(why, why)
        row, err = dispatches.mark_cancel(self.gone, "moved to a new round")
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "cancelled")
        return self.dispatch(ref=self.side, lane="lane/carried-reads",
                             kind="review", supersedes=self.gone)["id"]

    def show(self, rid, *extra):
        rc, out, err = self.cli(landreq.cmd_lr, ["show", rid, *extra])
        self.assertEqual(rc, 0, out + err)
        return out, err

    def triage(self, rid):
        rc, out, err = self.cli(dispatches.cmd_dispatch, ["triage", rid])
        self.assertEqual(rc, 0, out + err)
        return out

    def carried(self, text, why):
        """The lines under the planted row's header, which must be there."""
        lines = [line.strip() for line in text.splitlines()]
        head = "FROM %s (CANCELLED: %s)" % (self.gone[:12], why)
        start = next((i for i, line in enumerate(lines)
                      if line.startswith(head)), None)
        self.assertIsNotNone(start, "no read carried from %s:\n%s"
                             % (self.gone[:12], text))
        return lines[start + 1:]

    def test_the_successor_prints_the_cancelled_rows_read_and_hold(self):
        succ = self.planted()
        for text in (self.show(succ)[0], self.triage(succ)):
            under = self.carried(text, "moved to a new round")
            self.assertTrue(under[0].startswith(
                "ADVISORY read by model fable (run wf-gone, fable: family "
                "claude) — CONCUR at %s" % self.side[:12]), under[0])
            self.assertIn("evidence: evidence of wf-gone", under)
            self.assertIn("HOLD source-clean at %s by @codex-3" % self.side[:12],
                          "\n".join(under))
            self.assertIn(self.HOLD, "\n".join(under))

    def test_lr_show_of_the_cancelled_id_opens_its_successor(self):
        succ = self.planted()
        out, _err = self.show(self.gone)
        note, page = out.split("\n", 1)
        for word in (self.gone[:12], "CANCELLED", succ[:12]):
            self.assertIn(word, note)
        self.assertTrue(page.startswith("LAND REQUEST %s" % succ), page)
        self.assertIn("(run wf-gone,", "\n".join(self.carried(page, "moved "
                                                              "to a new round")))
        # --json STAYS ONE DOCUMENT: the note goes to stderr, the reads ride
        # the projection as `superseded_reads`.
        out, err = self.show(self.gone, "--json")
        lr = json.loads(out)
        self.assertEqual(lr["id"], succ)
        self.assertIn(succ[:12], err)
        self.assertEqual([r["id"] for r in lr["superseded_reads"]], [self.gone])
        self.assertEqual(lr["superseded_reads"][0]["source_clean_tip"],
                         self.side)

    def test_a_REBOUND_rows_read_rides_the_rebind(self):
        self.advise(self.gone, "wf-rebound")
        moved, why = dispatches.rebind(self.gone, "seat-b", force=True,
                                       reason="the reader went dark",
                                       notify=False)
        self.assertIsNone(why, why)
        new = moved["new"]["id"]
        shown = self.show(self.gone)[0]
        self.assertIn("LAND REQUEST %s" % new, shown)
        for text in (shown, self.triage(new)):
            under = self.carried(text, "rebound to seat-b: the reader went dark")
            self.assertIn("(run wf-rebound,", under[0])

    def test_CONTROL_a_successor_of_a_row_with_no_read_carries_nothing(self):
        """The successor of a cancelled row that holds no read prints what it
        did, and its projection grows no key; the planted arms above are the
        positive control on the same observables."""
        succ = self.planted(read=False)
        shown, triaged = self.show(succ)[0], self.triage(succ)
        self.assertTrue(shown.startswith("LAND REQUEST %s" % succ), shown)
        self.assertTrue(triaged.startswith(succ[:12]), triaged)
        self.assertNotIn("FROM %s" % self.gone[:12], shown + triaged)
        self.assertNotIn("superseded_reads", landreq.get(succ)[0])

    def test_an_OPEN_ancestors_read_is_not_repeated_on_its_successor(self):
        """Only a CANCELLED or REBOUND row's reads ride on: a normal lane's
        earlier round is a land request with its own page, so its read prints
        there and never again on every round after it (review of 7ca5ab1817f,
        P3-2). The last line is the must-hit: the read is on that page."""
        self.advise(self.gone, "wf-open")
        succ = self.dispatch(ref=self.side, lane="lane/carried-reads",
                             kind="review", supersedes=self.gone)["id"]
        self.assertEqual(dispatches.rows()[self.gone]["status"], "open")
        shown, triaged = self.show(succ)[0], self.triage(succ)
        self.assertTrue(shown.startswith("LAND REQUEST %s" % succ), shown)
        self.assertNotIn("FROM %s" % self.gone[:12], shown + triaged)
        self.assertNotIn("wf-open", shown + triaged)
        self.assertNotIn("superseded_reads", landreq.get(succ)[0])
        self.assertIn("(run wf-open,", self.show(self.gone)[0])

    def test_a_cancelled_ids_successor_that_is_no_land_request_is_named(self):
        """The one row that continues a cancelled id can be no land request —
        here a ref-less row, which the fold reads as needs-redispatch. `lr
        show` then refuses the id it was given and names that row, never "no
        such land request" about an id nobody typed (review of 7ca5ab1817f,
        P3-3)."""
        _row, err = dispatches.mark_cancel(self.gone, "moved to a new round")
        self.assertIsNone(err, err)
        ts = "2026-07-01T00:00:00Z"
        self.assertTrue(eventledger.append(dispatches.ledger_path(), {
            "id": "ce1e7dd0", "ts": ts, "recipient": "codex-3",
            "lane": "lane/carried-reads", "ref": self.a[:7], "note": None,
            "deadline_s": 60, "source": "old", "status": "open",
            "chain_root": dispatches.rows()[self.gone]["chain_root"],
            "supersedes": self.gone, "ack_ref": None, "verdict_ref": None,
            "last_updated": ts}))
        # MUST-HIT: the planted row is the one row that continues the
        # cancelled id, and it is no land request.
        live = dispatches.live_successors(dispatches.snapshot()[0], self.gone)
        self.assertEqual([r["id"] for r in live[1]], ["ce1e7dd0"])
        self.assertIsNone(landreq.get("ce1e7dd0")[0])
        rc, out, err = self.cli(landreq.cmd_lr, ["show", self.gone])
        self.assertEqual((rc, out), (1, ""), err)
        self.assertIn("no such land request: %s" % self.gone, err)
        for word in ("CANCELLED (moved to a new round)", "ce1e7dd0"):
            self.assertIn(word, err)
        self.assertNotIn("no such land request: ce1e7dd0", err)


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


def section(text, start, end):
    """The text from `start` up to `end`, asserted present, so an arm that
    reads a section cannot pass on an empty one."""
    i = text.index(start)
    return text[i:text.index(end, i)]


#: Every ladder surface kept in a file, as (path, start, end) of its section.
#: /x is NEVER-TRACKED (.gitignore, tests/test_never_track.py): it is edited
#: in place on the host and cannot be asserted from a tree.
SKILL_SECTIONS = (("agents/claudecode/skills/build/SKILL.md",
                   "**No reviewer is NEVER a blocker.**",
                   "- **Receipt-verify coordination.**"),
                  ("agents/claudecode/skills/reviewer-implements-own-findings/"
                   "SKILL.md", "## When no reviewer seat can take it",
                   "## Pitfalls"))
PITFALLS = ("agents/claudecode/skills/reviewer-implements-own-findings/"
            "SKILL.md", "## Pitfalls", "## Success Criteria")
VERBS_REMEDY = ("docs/VERBS.md",
                "**A subagent or a Workflow run is never a seat.**",
                "**A grant is for deliberate same-family delegation.**")
VERBS_DOOR = ("docs/VERBS.md", "- (a) the lane's changed files can be read.",
              "- (b) the run's own record is on disk")
#: fleet-maintenance's cross-family section, which teaches reviewer routing
#: to the maintenance seat without carrying the ladder itself.
FLEET_GATING = ("agents/claudecode/skills/fleet-maintenance/SKILL.md",
                "## 6. Cross-family gating", "## 7.")
#: The owner of the ladder text: its constants, `review_remedy` and
#: `review_fallback_text`, docstrings and comments included.
LADDER_SOURCE = ("helm/dispatches.py", "#: THE DEFAULT READER'S MODEL",
                 "def _recipient_join")


class TheTaughtSurfacesNameTheLadderTest(unittest.TestCase):
    """GAPS 3 AND 5. A rule only /x carried was a rule the seats that load
    /build or the review procedure never met. Each taught surface now names
    the owner's order: Opus first, the approval tier on a door, the park
    when no tier reader can take it, and Fable only for max QC."""

    WORDS = ("fresh-context Opus", "the Agent tool", "REVERSIBLE", "codex",
             "Fable", "PARKS", "max QC", "Sonnet and Haiku never review",
             "another credential or seat", "You have reached your Fable limit",
             "helm reviewers", "--reviewer-model")

    def test_each_skill_names_every_rung_in_the_owners_order(self):  # noqa: VACUOUS_ASSERTION — the absence of the removed rungs is read off the same section the loop just asserted every word present in, and seen == 2 is asserted unconditionally after it
        seen = 0
        for rel, start, end in SKILL_SECTIONS:
            text = section(read(rel), start, end)
            with self.subTest(skill=rel):
                for word in self.WORDS:
                    self.assertIn(word, flat(text), word)
                assert_opus_first_fable_last(self, text, once=False)
                assert_fable_max_qc_only(self, text)
                assert_approval_tier_rung(self, text)
            seen += 1
        self.assertEqual(seen, 2)

    def test_the_dispatch_help_names_the_owners_order(self):  # noqa: VACUOUS_ASSERTION — the absence of qwen27 is read off the same section the loop and the order helper just asserted nine phrases present in, unconditionally
        from helm import cli_help
        text = section(cli_help._VERB_HELP["dispatch"],
                       "NO REVIEWER IS NEVER A BLOCKER", "It discharges NOTHING")
        for word in ("fresh-context Opus", "REVERSIBLE", "codex", "Fable",
                     "PARKS", "Sonnet and Haiku never review",
                     "--reviewer-model",
                     "recorded as `fresh-context run <id>` on a door lane "
                     "as on a REVERSIBLE one",
                     "it authorizes no land or close"):
            self.assertIn(word, text, word)
        assert_opus_first_fable_last(self, text, once=False)
        assert_fable_max_qc_only(self, text)
        assert_approval_tier_rung(self, text)
        self.assertNotIn("qwen27", text)

    def test_the_verbs_doc_names_the_park_and_max_QC(self):  # noqa: VACUOUS_ASSERTION — the helper's one absence (no fallback wording) is read off the same section it first asserts the park and max-QC phrases present in, unconditionally
        """docs/VERBS.md says what a refusal names instead; it read "codex for
        an irreversible door, and Fable last"."""
        text = section(read(VERBS_REMEDY[0]), *VERBS_REMEDY[1:])
        assert_fable_max_qc_only(self, text)
        assert_approval_tier_rung(self, text)


def ladder_surfaces():
    """{name: text} for every surface that carries the ladder, and the
    source that owns it."""
    from helm import cli_help
    what = obligation.unanswered_fixes(rows=[{
        "id": "a" * 16, "status": "verdict", "polarity": "fix",
        "lane": "lane/x", "sender": "alice", "recipient": "bob",
        "reviewed_tip": "b" * 40, "ts": "2026-09-23T00:00:00Z"}])[0][0]["what"]
    surfaces = {
        "review_fallback_text": dispatches.review_fallback_text(
            {"id": "abcdef0123456789", "lane": "lane/x"}, tip="f" * 40),
        "review_remedy": dispatches.review_remedy("abcdef0123456789"),
        "owed digest": owedpush.digest_text("alice", [{
            "lane": "l", "row": "r", "owed_since": None, "what": "w"}]),
        "unanswered fix": what,
        "dispatch help": section(cli_help._VERB_HELP["dispatch"],
                                 "NO REVIEWER IS NEVER A BLOCKER",
                                 "It discharges NOTHING")}
    for rel, start, end in SKILL_SECTIONS + (PITFALLS, VERBS_REMEDY,
                                             LADDER_SOURCE):
        surfaces["%s from %s" % (rel, start[:30])] = section(
            read(rel), start, end)
    return surfaces


class FableIsForMaxQCOnlyTest(unittest.TestCase):
    """task/3202. THE OWNER'S RULING, as opus-integrator reconciled it into
    the rung text (room row 1919): "let's try not to use fable if we can help
    it, claude usage is creeping toward orange"; "no more automatia fable
    slots, just for max qc for the most important stuff"; "for every fable
    token we can get 3 opus tokens". Rung 2's fallback became a PARK until a
    tier reader can take the door read, gemini as input meanwhile, and Fable
    only for max QC on the most important work."""

    def text(self):
        return dispatches.review_fallback_text(
            {"id": "abcdef0123456789", "lane": "lane/x"}, tip="f" * 40)

    def test_a_door_read_whose_reader_is_out_PARKS_and_never_takes_Fable(self):  # noqa: VACUOUS_ASSERTION — the Fable-free slice is bounded by two index() calls that raise when either end is absent, and the park phrases are asserted present on the same text
        """(a) Rung 2's fallback is the park, and nothing between rung 2 and
        the park names Fable: the door read waits for a tier reader, and
        gemini's read meanwhile is input, never the review."""
        words = flat(self.text())
        two = words.index("(2) ")
        park = words.index(PARKS_WORDS[0])
        self.assertLess(two, park)
        self.assertNotIn("Fable", past_the_opus_clause(self, words)[two:park])
        for word in PARKS_WORDS:
            self.assertIn(word, words, word)
        self.assertNotIn("(3) ", words)

    def test_Fable_is_named_only_for_the_max_QC_cases(self):  # noqa: VACUOUS_ASSERTION — every assertion is positive: the loop runs over the non-empty MAX_QC_WORDS constant and the order is read off index() calls that raise on absence
        """(b) Fable is first named in the sentence that says max QC, which
        names the cases, its price and the burn bar; the Workflow mechanics
        and the limit, for when Fable IS used, come after it."""
        words = flat(self.text())
        for word in MAX_QC_WORDS:
            self.assertIn(word, words, word)
        qc = words.index("max QC")
        sentence = words.rfind(". ", 0, qc) + 2
        self.assertGreaterEqual(past_the_opus_clause(self, words)
                                .index("Fable"), sentence,
                                words[sentence:qc + 80])
        self.assertLess(qc, words.index(MAX_QC_CASES))
        self.assertLess(words.index(MAX_QC_CASES),
                        words.index("You have reached your Fable limit"))

    def test_Fable_reads_only_on_the_owners_ask_after_a_fresh_Opus_read(self):  # noqa: VACUOUS_ASSERTION — the surface count is asserted 10 unconditionally, and each surface is asserted to carry the new case and name Fable before the retired case's absence is read
        """task/3855: every ladder surface names a fresh-context Opus read
        before Fable, says Fable reads only on the owner's ask, and no
        longer teaches the "money or creds door with no other reader" case
        that sent a door read to Fable."""
        remedy = flat(dispatches.review_remedy("abcdef0123456789"))
        self.assertLess(remedy.index("fresh-context"), remedy.index("Fable"))
        surfaces = ladder_surfaces()
        self.assertEqual(len(surfaces), 10)
        for name, text in surfaces.items():
            with self.subTest(surface=name):
                words = flat(text)
                self.assertIn("Fable", words)
                self.assertIn(MAX_QC_CASES, words)
                self.assertNotIn(RETIRED_MAX_QC_CASES, words)

    #: The sentences the ruling retired, as the surfaces carried them (a
    #: placeholder filled), and the shapes a later edit could reach for.
    RETIRED = (
        "(3) Fable LAST, never required, at about 3x an Opus read: a "
        "one-agent Workflow",
        "codex for an irreversible door; and Fable only as the last resort, "
        "never required.",
        "names who can take it now; Fable is the LAST resort, never required.",
        "then Fable LAST, never required, with a Fable limit met by Fable "
        "through another credential or seat",
        "cheapest first, codex for an irreversible door and Fable last.",
        "(3) **Fable is the last resort**, never required, at about 3x an "
        "Opus read",
        "3. **Fable, the last resort** — never required, at about 3x an Opus "
        "read",
        "- Reaching for Fable before a fresh-context Opus read: Fable is the "
        "last\n  resort, at about 3x an Opus read.",
        "#: The LAST-RESORT reader's model: never required, and about 3x an "
        "Opus read.",
        "a door read with no reader falls through to Fable",
        "Fable is the default reader",
        "a one-agent Workflow with Fable as the automatic fallback")

    def test_CONTROL_the_must_miss_fires_on_every_retired_sentence(self):  # noqa: VACUOUS_ASSERTION — the loop runs over the twelve-sentence RETIRED constant and asserts a HIT on each; the one absence is the control sentence that denies both claims
        """The must-miss below is a regex, so it is proven on the words it
        exists to refuse before its silence is read as a pass; and it stays
        silent on the sentence that says Fable is neither."""
        for sentence in self.RETIRED:
            with self.subTest(sentence=sentence[:50]):
                self.assertIsNotNone(FABLE_AS_FALLBACK.search(flat(sentence)))
        self.assertIsNone(FABLE_AS_FALLBACK.search(
            "Fable is for max QC only, never a default and never an "
            "automatic fallback"))

    def test_MUST_MISS_no_surface_calls_Fable_a_default_or_a_fallback(self):  # noqa: VACUOUS_ASSERTION — the surface count is asserted 10 unconditionally, each surface is asserted to name Fable before its absence is read, and the regex is proven on the retired sentences by the CONTROL arm
        """(c) Every surface that carries the ladder, and the source that
        owns it: no rung, docstring, comment, remedy, digest, help, skill,
        pitfall or doc paragraph names Fable as a default, a last rung or an
        automatic fallback."""
        surfaces = ladder_surfaces()
        self.assertEqual(len(surfaces), 10)
        for name, text in surfaces.items():
            with self.subTest(surface=name):
                self.assertIn("Fable", text)
                words = flat(text)
                hit = FABLE_AS_FALLBACK.search(words)
                self.assertIsNone(hit, hit and words[
                    max(0, hit.start() - 80):hit.end() + 80])



class RungTwoIsOneApprovalTierReadTest(unittest.TestCase):
    """task/3202, the owner's ruling as meta-claude reconciled it (room row
    2104), superseding the two rung-2 texts before it: a door read needs ONE
    approval-tier read by a reader that is not the author, and a different
    family is not required for that one read (the owner confirmed it, room
    row 2217). The tier is claude on Opus 5.5 as a
    fresh-context, non-author read, codex, ds4pro on V4 Pro, kimi and grok,
    judged on the resolved model and chosen by `helm burn`. Gemini,
    codex-spark and local seats read as input until the owner admits them."""

    def text(self):
        return flat(dispatches.review_fallback_text(
            {"id": "abcdef0123456789", "lane": "lane/x"}, tip="f" * 40))

    def test_the_ladder_names_the_approval_tier_with_the_fresh_context_Opus_read(self):  # noqa: VACUOUS_ASSERTION — every assertion is positive: the loop runs over the non-empty TIER_WORDS constant, the order is read off index() calls that raise on absence, and the family set is compared to an exact value
        """(d) Rung 2 names the tier whole, the fresh-context non-author Opus
        read first in it, chosen by `helm burn` on the resolved model, and it
        is the same tier helm's own check enforces."""
        words = self.text()
        for word in TIER_WORDS:
            self.assertIn(word, words, word)
        two = words.index("(2) ")
        self.assertLess(two, words.index(TIER_WORDS[0]))
        self.assertLess(words.index(TIER_WORDS[1]), words.index(TIER_WORDS[3]))
        self.assertLess(words.index(TIER_WORDS[0]), words.index(PARKS_WORDS[0]))
        # THE SAME TIER THE CODE CHECKS: a family added to or dropped from
        # route.APPROVAL_TIER turns this red until the ladder names it.
        self.assertEqual(
            sorted("claude" if f == burnflags.NATIVE_FAMILY else f
                   for f in route.APPROVAL_TIER), sorted(TIER_FAMILIES))
        for family in TIER_FAMILIES:
            self.assertIn(family, words[two:words.index(PARKS_WORDS[0])],
                          family)

    def test_gemini_codex_spark_and_local_seats_read_as_input_only(self):  # noqa: VACUOUS_ASSERTION — every assertion is positive: the loop runs over the non-empty INPUT_ONLY_WORDS constant and the order is read off index() calls that raise on absence
        """(e) Gemini, codex-spark and local seats read as input until the
        owner admits them on their record, and a local seat's evidence is
        put to him, never an automatic admission."""
        words = self.text()
        for word in INPUT_ONLY_WORDS:
            self.assertIn(word, words, word)
        self.assertLess(words.index("(2) "), words.index(INPUT_ONLY_WORDS[0]))
        self.assertLess(words.index(INPUT_ONLY_WORDS[0]),
                        words.index(PARKS_WORDS[0]))

    #: The rung-2 sentences the rulings retired, as the surfaces carried them
    #: (placeholders filled), and the corrected wording in between.
    RETIRED = (
        "(2) A lane that touches prod still owes a DIFFERENT model's read: "
        "codex, a cheap other family such as cursor, ds4pro or gemini, or a "
        "local seat once its outcomes per hour are measured.",
        "Take another family's read instead, cheapest first: cursor, gemini "
        "or ds4pro, or a local seat once its outcomes per hour are measured",
        "2. a **different model's read** when the lane touches prod",
        "still owes a **different model's read**: codex, or a cheap other "
        "family (cursor, ds4pro, gemini)",
        "another family's read, cheapest first (cursor, gemini, ds4pro, or a "
        "local seat once its outcomes per hour are measured)",
        "A door lane needs another family's read or Fable's.",
        "A door needs another family's read, and when none can take it",
        "a lane that touches prod needs ONE approval-tier read by a family "
        "other than the author's",
        "Gemini, local seats and Opus read as INPUT only",
        "and so does Opus read as input on Claude-authored work",
        "Among readers who clear the bar, prefer another lane over an Opus "
        "agent, local seats first, without overusing codex",
        "and it is not Fable reading a Claude author: a review is another "
        "family's read, or Fable's.",
        "A different-model read is the default, and it stays REQUIRED on "
        "irreversible work")

    def test_CONTROL_the_rung_two_must_miss_fires_on_every_retired_sentence(self):  # noqa: VACUOUS_ASSERTION — the loop runs over the thirteen-sentence RETIRED constant and asserts a HIT on each; the one absence is the new rung's own sentence
        """The must-miss below is three regexes, so they are proven on the
        words they exist to refuse before their silence is read as a pass;
        and they stay silent on the new rung's own words."""
        for sentence in self.RETIRED:
            with self.subTest(sentence=sentence[:50]):
                self.assertIsNotNone(rung_two_hit(sentence))
        self.assertIsNone(rung_two_hit(
            "A lane that touches prod needs ONE approval-tier read by a "
            "reader that is NOT the author: claude on Opus 5.5 as a "
            "fresh-context, non-author read, codex, ds4pro on V4 Pro, kimi or "
            "grok. A different family is not required for that one read; "
            "the owner confirmed it. Among readers who clear the bar, prefer "
            "another lane over an Opus agent, especially a local seat once "
            "the owner admits it, without overusing codex, and never prefer "
            "Fable over Opus automatically; the exact order is weighed case "
            "by case, never a fixed list. Gemini, "
            "codex-spark and local seats read as INPUT only until the owner "
            "admits them on their record."))

    def test_the_fleet_maintenance_skill_teaches_rung_two_as_ruled(self):  # noqa: VACUOUS_ASSERTION — the section is asserted to carry the ruled sentences before the must-miss absence is read, and the regexes are proven on the retired sentences by the CONTROL arm
        """fleet-maintenance's cross-family section taught "A different-model
        read is the default, and it stays REQUIRED on irreversible work": the
        rung 1 and rung 2 the rulings retired, on a surface the maintenance
        seat reads. It now names the fresh-context Opus default and the one
        approval-tier read a door needs, with no different family required."""
        text = section(read(FLEET_GATING[0]), *FLEET_GATING[1:])
        words = flat(text)
        for word in ("A fresh-context Opus read is the default",
                     "needs ONE approval-tier read by a reader that is not "
                     "the author",
                     "A different family is not required for that one read",
                     "max QC"):
            self.assertIn(word.lower(), words.lower(), word)
        self.assertIsNone(rung_two_hit(text))
        self.assertIsNone(FABLE_AS_FALLBACK.search(words))

    def test_MUST_MISS_no_surface_needs_a_different_family_or_lists_gemini_as_a_reader(self):  # noqa: VACUOUS_ASSERTION — the surface count is asserted 11 unconditionally, each surface is asserted to name gemini before its absence is read, and the regexes are proven on the retired sentences by the CONTROL arm
        """(f) Every surface that carries the ladder, and the source that
        owns it: none says a door needs a different model or family, none
        lists gemini or a local seat as a door's reader, and none calls Opus
        input only."""
        surfaces = ladder_surfaces()
        surfaces["VERBS door bullet"] = section(read(VERBS_DOOR[0]),
                                                *VERBS_DOOR[1:])
        self.assertEqual(len(surfaces), 11)
        for name, text in surfaces.items():
            with self.subTest(surface=name):
                self.assertIn("gemini", text.lower())
                self.assertIsNone(rung_two_hit(text))


if __name__ == "__main__":
    unittest.main()
