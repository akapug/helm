#!/usr/bin/env python3
"""helm eval board — the model scorecard (task/3448).

The owner: "maybe helm needs our own internal version on a page of the webui
that ranks the models we use as well, with a mix of quant and qualitative
factors", and "we could even have it use outside model benchmarks to initially
organize it, and then let the rankings upgrade themselves over time with our
'direct experience' factors".

So each model's score is a PRIOR from a public benchmark, measured under its
maker's harness and labelled so, blended with our own record into a POSTERIOR
that the record takes over as lanes accrue. These arms drive the pure readers
over a planted ledger: what a lane is, who authored it, whether it landed,
how many rounds and FIX verdicts it took, whether a reviewer patched it, how
long the builder took to hand it back, whether a reader agreed with the later
approval-tier read of the same tip, and what the blend does at small and large
n. Nothing the ledger cannot say is ever a zero: it is None, and the text
reads UNKNOWN.
"""
import calendar
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from helm import scorecard

NOW = calendar.timegm(time.strptime("2026-09-27T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))


def ts(hours_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - hours_ago * 3600))


def disp(i, sender, recipient, lane, kind, at, root=None, tip=None):
    return {"event": "dispatch", "id": i, "ts": ts(at), "sender": sender,
            "recipient": recipient, "lane": lane, "kind": kind,
            "chain_root": root or i, "tip": tip or ("t" + i), "ref": tip or ("t" + i)}


def verdict(i, at, polarity, tip, family=None, model=None, patch_author=None):
    row = {"event": "verdict", "id": i, "ts": ts(at), "polarity": polarity,
           "reviewed_tip": tip}
    if family:
        backend = "native" if family == "claude" else "proxy"
        resolved = {"family": family, "backend": backend}
        resolved["upstream_model" if backend == "proxy" else "model"] = model
        row["verdict_author_runtime_evidence"] = {"resolved": resolved}
    if patch_author:
        row["patch_tip"] = "p" + i
        row["patch_author"] = patch_author
    return row


def close(i, at, reason, tip=None):
    row = {"event": "close", "id": i, "ts": ts(at), "close_reason": reason}
    if tip:
        row["reviewed_tip"] = tip
    return row


def ledger():
    """Four lanes in the window and one before it.

    A  qwen27 builds for the mentor; two rounds; the first read FIXes, the
       second approves with the mentor's patch; the ledger closes it landed.
    B  qwen27 builds; one round, approved; no close, but trunk merged it.
    C  qwenlocal; one round, FIX; withdrawn — decided, not landed.
    D  seat-c (no recorded model); one round approved by the mentor; closed landed.
    E  qwen27, forty days ago — outside a 30-day window.
    """
    return [
        disp("a1", "seat-a", "qwen27", "lane-a", "build", 30),
        disp("a2", "qwen27", "seat-b", "lane-a", "review", 28, root="a1", tip="ta1"),
        verdict("a2", 27.5, "fix", "ta1", "codex", "gpt-5.6-sol"),
        disp("a3", "qwen27", "seat-a", "lane-a", "review", 26, root="a1", tip="ta2"),
        verdict("a3", 25, "approve", "ta2", "claude", None, patch_author="seat-a"),
        close("a3", 24, "landed"),
        disp("b1", "seat-a", "qwen27", "lane/lane-b", "build", 20),
        disp("b2", "qwen27", "seat-b", "lane/lane-b", "review", 16, root="b1", tip="tb1"),
        verdict("b2", 15, "approve", "tb1", "codex", "gpt-5.6-sol"),
        disp("c1", "qwenlocal", "seat-b", "lane-c", "review", 12, tip="tc1"),
        verdict("c1", 11, "fix", "tc1", "codex", "gpt-6-astra"),
        close("c1", 10, "withdrawn"),
        disp("d1", "seat-c", "seat-a", "lane-d", "review", 9, tip="td1"),
        verdict("d1", 8, "approve", "td1", "claude"),
        close("d1", 7, "landed"),
        # a reader record: qwen27 said nothing was wrong with tc1, and the
        # approval tier read FIX on it afterwards — a miss; on tb0 it approved
        # and the tier approved after — an agreement
        {"event": "findings-note", "id": "c1", "ts": ts(11.5), "reader": "qwen27",
         "reviewed_tip": "tc1", "outcome": "complete", "kept": "0"},
        disp("r1", "seat-a", "qwen27", "lane-r", "review", 6, tip="tr1"),
        verdict("r1", 5.5, "approve", "tr1", "qwen27", "qwen27"),
        disp("r2", "seat-a", "seat-b", "lane-r", "review", 5, root="r1", tip="tr1"),
        verdict("r2", 4, "approve", "tr1", "codex", "gpt-5.6-sol"),
        disp("e1", "seat-a", "qwen27", "lane-e", "build", 40 * 24),
        close("e1", 40 * 24 - 5, "landed"),
    ]


# trunk's merges in the window: lane name -> [(the merged tip, merge time)]
TRUNK = {"lane-b": [("tb1", NOW - 14 * 3600)]}


def run_board(rows=None, trunk=TRUNK, window_s=30 * 86400):
    return scorecard.board(rows if rows is not None else ledger(), trunk,
                           now=NOW, window_s=window_s)


def grok_lanes(clean=0, withdrawn=0, at=3, seat="seat-g", family="grok",
               model="grok-build-0.1", slow=0):
    """A seat whose own verdict recorded `model` (by default one with no
    public prior, grok-build-0.1), then lanes it built: `clean` landed
    without a patch, `withdrawn` ended without landing. Each lane hands back
    30 minutes after its build, the first `slow` of them 31."""
    g = seat + "-"
    rows = [disp(g + "gv", "seat-a", seat, "lane-" + g + "gv", "review", at + 1, tip="t" + g + "gv"),
            verdict(g + "gv", at + 0.9, "approve", "t" + g + "gv", family, model)]
    for i in range(clean + withdrawn):
        b, r, tip = g + "%db" % i, g + "%dr" % i, "t" + g + "%d" % i
        back = at - 0.5 - (1 / 60.0 if i < slow else 0)
        rows += [disp(b, "seat-a", seat, "lane-" + g + "%d" % i, "build", at),
                 disp(r, seat, "seat-b", "lane-" + g + "%d" % i, "review", back, root=b, tip=tip)]
        if i < clean:
            rows += [verdict(r, at - 0.6, "approve", tip, "codex", "gpt-5.6-sol"),
                     close(r, at - 0.7, "landed")]
        else:
            rows.append(close(r, at - 0.7, "withdrawn"))
    return rows


def compose_rows():
    """One lane, two chains that each closed LANDED on the same tip (the
    shape of the ledger's compose-verb-emits-per-car-provenance): seat-lead
    asks seat-builder to review, then to build a cure, and seat-reader
    approves the cure's tip tx9; seat-lead also asks seat-second to read tx9
    in a chain of its own. One tip reached trunk, so one land, credited to
    the builder alone."""
    return [
        disp("x1", "seat-lead", "seat-builder", "lane-cv", "review", 30, tip="tx1"),
        verdict("x1", 29.8, "fix", "tx1", "codex", "gpt-5.6-sol"),
        disp("x2", "seat-lead", "seat-builder", "lane-cv", "build", 29.5, root="x1", tip="tx1"),
        disp("x3", "seat-lead", "seat-reader", "lane-cv", "review", 25, root="x1", tip="tx9"),
        verdict("x3", 24.5, "approve", "tx9", "codex", "gpt-5.6-sol"),
        close("x3", 20, "landed", tip="tx9"),
        disp("y1", "seat-lead", "seat-second", "lane-cv", "review", 24.8, tip="tx9"),
        verdict("y1", 24, "approve", "tx9", "claude"),
        close("y1", 20, "landed", tip="tx9"),
    ]


def codex3_rows():
    """seat-k builds a lane early in the window, when its verdicts record
    gpt-5.6-sol, and later signs verdicts as gpt-6-astra."""
    return [
        disp("k1", "seat-a", "seat-k", "lane-k", "build", 30),
        disp("k2", "seat-k", "seat-b", "lane-k", "review", 29, root="k1", tip="tk1"),
        verdict("k2", 28.5, "approve", "tk1", "codex", "gpt-5.6-sol"),
        close("k2", 28, "landed"),
        disp("m1", "seat-a", "seat-k", "lane-m1", "review", 20, tip="tm1"),
        verdict("m1", 19.5, "approve", "tm1", "codex", "gpt-5.6-sol"),
        disp("m2", "seat-a", "seat-k", "lane-m2", "review", 5, tip="tm2"),
        verdict("m2", 4.5, "approve", "tm2", "codex", "gpt-6-astra"),
    ]


def model(b, name):
    got = [m for m in b["models"] if m["model"] == name]
    assert got, "no model %s in %s" % (name, [m["model"] for m in b["models"]])
    return got[0]


class LaneReaderTest(unittest.TestCase):
    def setUp(self):
        self.lanes = {l["lane"]: l for l in scorecard.lanes(
            ledger(), NOW - 30 * 86400, TRUNK)}

    def test_a_lane_is_its_chain_and_its_author_is_the_builder(self):
        self.assertEqual(sorted(self.lanes), ["lane-a", "lane-b", "lane-c", "lane-d", "lane-r"])
        self.assertEqual(self.lanes["lane-a"]["author"], "qwen27")
        self.assertEqual(self.lanes["lane-c"]["author"], "qwenlocal")   # no build: the first sender

    def test_rounds_fixes_patches_and_the_land(self):
        a = self.lanes["lane-a"]
        self.assertEqual((a["rounds"], a["fixes"], a["patched"], a["landed"]), (2, 1, True, True))
        b = self.lanes["lane-b"]
        self.assertEqual((b["rounds"], b["fixes"], b["patched"], b["landed"]), (1, 0, False, True))
        c = self.lanes["lane-c"]
        self.assertEqual((c["landed"], c["decided"]), (False, True))

    def test_hand_back_is_build_to_the_builders_first_review_request(self):
        self.assertEqual(self.lanes["lane-a"]["handback_s"], 2 * 3600)
        self.assertIsNone(self.lanes["lane-c"]["handback_s"])           # never built for

    def test_reads_and_patches_are_counted_by_the_family_that_did_them(self):
        a = self.lanes["lane-a"]
        self.assertEqual(a["reads"], {"codex": 1, "anthropic": 1})
        self.assertEqual(a["patches"], {"anthropic": 1})


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.b = run_board()

    def test_each_author_seat_rolls_up_under_the_model_it_ran(self):
        q = model(self.b, "qwen27")
        m = q["measured"]
        self.assertEqual((m["lanes"], m["landed"], m["decided"], m["clean"]), (2, 2, 2, 1))
        self.assertEqual(m["rounds_median"], 1.5)
        self.assertEqual(m["handback_median_min"], 180)                # 2h and 4h
        self.assertEqual(q["seats"], ["qwen27"])
        # a reviewer's reads land under the model its verdicts recorded, and a
        # seat nothing records a model for is not guessed into one
        self.assertIn("gpt-5.6-sol", [x["model"] for x in self.b["models"]])
        self.assertEqual(self.b["unnamed"], {"lanes": 1, "reviews": 0, "seats": ["seat-c"]})

    def test_a_window_leaves_older_lanes_out(self):
        self.assertEqual(model(self.b, "qwen27")["measured"]["lanes"], 2)
        wide = run_board(window_s=60 * 86400)
        self.assertEqual(model(wide, "qwen27")["measured"]["lanes"], 3)

    def test_reader_agreement_against_the_later_approval_tier_read(self):
        r = model(self.b, "qwen27")["measured"]["reader"]
        self.assertEqual((r["pairs"], r["agree"], r["misses"], r["false_alarms"]), (2, 1, 1, 0))

    def test_what_cannot_be_measured_is_none_never_zero(self):  # noqa: VACUOUS_ASSERTION — the unknown-key loop asserts each named key present and non-empty, and the UNKNOWN text assertion is unconditional
        c = model(self.b, "qwenlocal")["measured"]
        self.assertIsNone(c["reader"])                  # it read nothing the tier read after
        self.assertIsNone(c["handback_median_min"])
        for key in ("request_speed", "deaf_time", "token_cost"):
            self.assertIn(key, self.b["unknown"])
            self.assertTrue(self.b["unknown"][key])
        text = "\n".join(scorecard.board_lines(self.b))
        self.assertIn("UNKNOWN", text)

    def test_cost_per_land_counts_reviewer_reads_and_patches_and_says_it_is_inferred(self):
        cost = model(self.b, "qwen27")["cost"]
        self.assertEqual(cost["reads_per_land"], 1.5)        # 3 reads over 2 lands
        self.assertEqual(cost["patches_per_land"], 0.5)
        self.assertEqual(cost["by_family"], {"codex": 2, "anthropic": 1})   # counts, not shares
        self.assertEqual((cost["lands"], cost["reads"], cost["patches"]), (2, 3, 1))
        self.assertIn("INFERRED", cost["basis"])

    def test_the_prior_is_labelled_the_makers_and_the_posterior_carries_n_and_a_band(self):
        s = model(self.b, "qwen27")["score"]
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["state"], "prior only")         # under the floor
        lo, hi = s["band"]
        self.assertLessEqual(lo, s["posterior"])
        self.assertLessEqual(s["posterior"], hi)
        prior = model(self.b, "qwen27")["prior"]
        self.assertIn("maker", prior["harness"])
        self.assertTrue(prior["source"].startswith("https://"))

    def test_a_model_with_no_public_number_reads_no_prior(self):
        c = model(self.b, "claude")
        self.assertIsNone(c["prior"])
        self.assertIn("no prior", "\n".join(scorecard.board_lines(self.b)))

    def test_measured_models_rank_first_then_prior_only_each_by_posterior(self):
        """A model with one lane would otherwise rank on a prior it has not
        earned above models with hundreds of lanes (seen on the live ledger:
        a one-lane model third, on the fleet's pooled rate). A model with no
        evidence at all is not scored and sorts after both, with no rank."""
        b = run_board(ledger() + grok_lanes(clean=4))
        states = [m["score"]["state"] for m in b["models"]]
        order = ("measured", "prior only", "not scored")
        self.assertEqual(states[0], "measured")
        self.assertIn("not scored", states)
        self.assertEqual(states, sorted(states, key=order.index))
        for group in ("measured", "prior only"):
            post = [m["score"]["posterior"] for m in b["models"] if m["score"]["state"] == group]
            self.assertEqual(post, sorted(post, reverse=True), group)
        scored = [m for m in b["models"] if m["score"]["state"] != "not scored"]
        self.assertEqual([m["rank"] for m in scored], list(range(1, len(scored) + 1)))
        self.assertEqual({m["rank"] for m in b["models"] if m["score"]["state"] == "not scored"},
                         {None})

    def test_an_unreadable_trunk_is_named_and_the_ledger_still_answers(self):
        b = scorecard.board(ledger(), None, now=NOW, window_s=30 * 86400,
                            trunk_why="git log failed")
        self.assertEqual(b["sources"]["trunk"]["why"], "git log failed")
        self.assertEqual(model(b, "qwen27")["measured"]["landed"], 1)   # lane-b unproven


class NoNumberWithoutEvidenceTest(unittest.TestCase):
    """THE OWNER'S FINDING (task/3448): a model with no public prior and no
    lanes of ours showed the fleet's pooled rate as its score (89, 70–100 for
    dots-3, grok-build-0.1 and "unknown" on the live ledger), and a model
    with a lane or two and no prior showed the pooled number too. A number
    with no evidence behind it is not a score: such a row reads "not scored",
    with no band and no rank, and sorts last. `helm eval board` and Fleet ›
    models read this one function, so both say so."""

    def setUp(self):
        self.b = run_board()

    def test_no_prior_and_no_lanes_is_not_scored(self):
        c = model(self.b, "claude")                 # no public number, no decided lane
        self.assertEqual(c["measured"]["decided"], 0)
        s = c["score"]
        self.assertEqual(s["state"], "not scored")
        self.assertIsNone(s["posterior"])
        self.assertIsNone(s["band"])
        self.assertIsNone(s["prior"])
        self.assertIsNone(s["ours"])
        self.assertIsNone(c["rank"])

    def test_a_few_lanes_and_no_prior_keep_their_cells_but_no_score(self):
        g = model(run_board(ledger() + grok_lanes(clean=1, withdrawn=1)), "grok-build-0.1")
        self.assertEqual((g["measured"]["decided"], g["measured"]["clean"]), (2, 1))
        s = g["score"]
        self.assertEqual(s["ours"], 50.0)           # the measured cell stays
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["state"], "not scored")  # under the floor, no pooled number
        self.assertIsNone(s["posterior"])
        self.assertIsNone(s["band"])
        self.assertIsNone(g["rank"])

    def test_at_the_floor_a_model_with_no_prior_is_scored_on_its_lanes(self):
        g = model(run_board(ledger() + grok_lanes(clean=3)), "grok-build-0.1")
        self.assertEqual(g["score"]["state"], "measured")
        self.assertIsNotNone(g["score"]["posterior"])
        self.assertIsInstance(g["rank"], int)

    def test_a_prior_alone_still_places_a_model(self):
        # the maker's number IS evidence: a benchmark with no lanes is prior only
        q = model(self.b, "gpt-6-astra")
        self.assertEqual(q["measured"]["lanes"], 0)
        self.assertEqual(q["score"]["state"], "prior only")
        self.assertIsNotNone(q["score"]["posterior"])

    def test_the_text_says_not_scored_and_never_a_pooled_number(self):
        lines = scorecard.board_lines(self.b)
        row = [ln for ln in lines if ln.split()[1:2] == ["claude"]]
        self.assertEqual(len(row), 1, lines)
        self.assertIn("not scored", row[0])
        self.assertNotIn("pooled", row[0])
        self.assertTrue(row[0].startswith("-"), row[0])     # no rank


class UnnamedIsNotAModelTest(unittest.TestCase):
    """THE OWNER'S FINDING (task/3448): "unknown" ranked as a model. Lanes
    and reviews whose model the ledger cannot name are one footnote under the
    table, never a row: nothing is known about them to rank."""

    def setUp(self):
        self.b = run_board()

    def test_the_unknown_bucket_is_never_a_row(self):
        names = [m["model"] for m in self.b["models"]]
        self.assertIn("qwen27", names)
        self.assertNotIn("unknown", names)
        self.assertEqual(self.b["unnamed"]["seats"], ["seat-c"])

    def test_its_lanes_still_count_in_the_fleets_totals(self):  # noqa: VACUOUS_ASSERTION — the exact (lanes, decided) tuple is an unconditional structural assertion on the board
        # the unnamed seat's landed lane: the fleet's lanes and its pooled
        # rate still count it
        self.assertEqual((self.b["lanes"], self.b["decided"]), (5, 4))

    def test_the_text_gives_it_one_footnote_line_under_the_table(self):
        lines = scorecard.board_lines(self.b)
        notes = [i for i, ln in enumerate(lines) if "no model on the ledger" in ln]
        self.assertEqual(len(notes), 1, lines)
        self.assertIn("1 lane carries no model on the ledger", lines[notes[0]])
        self.assertIn("seat-c", lines[notes[0]])
        last_row = max(i for i, ln in enumerate(lines) if ln.split()[1:2] == ["claude"])
        self.assertEqual(notes[0], last_row + 1)
        self.assertFalse([ln for ln in lines[:notes[0]] if ln.split()[1:2] == ["unknown"]])

    def test_a_seat_that_only_reviewed_is_counted_in_reviews(self):
        rows = ledger() + [disp("u1", "seat-a", "seat-u", "lane-u", "review", 3, tip="tu1"),
                           verdict("u1", 2.5, "approve", "tu1")]
        b = run_board(rows)
        self.assertEqual(b["unnamed"]["reviews"], 1)
        self.assertIn("seat-u", b["unnamed"]["seats"])
        text = "\n".join(scorecard.board_lines(b))
        self.assertIn("1 lane and 1 review carry no model on the ledger", text)

    def test_with_every_model_named_there_is_no_footnote(self):
        rows = [r for r in ledger() if r.get("id") != "d1"]
        b = run_board(rows)
        self.assertEqual(b["unnamed"], {"lanes": 0, "reviews": 0, "seats": []})
        self.assertNotIn("no model on the ledger", "\n".join(scorecard.board_lines(b)))


class LandCreditTest(unittest.TestCase):
    """A LAND IS CREDITED ONCE, to the chain that made it: the chain whose
    close event says it landed, or whose tip is the one trunk merged. Every
    other chain under the same lane name is not landed by the name alone, and
    a chain that closed LANDED on a tip another chain already landed is the
    same land (its reads and verdicts count toward it)."""

    SINCE = NOW - 30 * 86400

    def named(self, rows, trunk, name):
        return [l for l in scorecard.lanes(rows, self.SINCE, trunk) if l["lane"] == name]

    def test_two_chains_closed_landed_on_one_tip_are_one_land(self):  # noqa: VACUOUS_ASSERTION — the loop runs over a literal two-element tuple, and each pass asserts exactly one lane with its author, land and reads
        for trunk in ({"lane-cv": [("tx9", NOW - 19 * 3600)]}, {}):
            got = self.named(compose_rows(), trunk, "lane-cv")
            self.assertEqual(len(got), 1, (trunk, got))
            lane = got[0]
            self.assertEqual((lane["author"], lane["landed"], lane["decided"]), ("seat-builder", True, True))
            self.assertEqual(lane["reads"], {"codex": 1, "anthropic": 1})   # seat-reader and seat-second
            self.assertEqual(lane["fixes"], 1)

    def test_the_second_author_is_credited_no_land(self):
        b = run_board(ledger() + compose_rows(), trunk=dict(TRUNK, **{"lane-cv": [("tx9", NOW - 19 * 3600)]}))
        seats = {x["seat"]: x["measured"] for x in b["seats"]}
        self.assertEqual(seats["seat-builder"]["landed"], 1)
        self.assertEqual(seats.get("seat-lead", {"landed": 0, "lanes": 0})["landed"], 0)
        self.assertEqual(seats.get("seat-lead", {"lanes": 0})["lanes"], 0)

    def rival_chains(self):
        return [disp("p1", "seat-a", "seat-p", "lane-t", "build", 30, tip="tp1"),
                disp("q1", "seat-a", "seat-q", "lane-t", "build", 20, tip="tq1")]

    def test_trunk_lands_the_chain_whose_tip_it_merged_not_every_chain_of_the_name(self):  # noqa: VACUOUS_ASSERTION — the exact (True, False) tuple over both named chains is the unconditional structural assertion
        got = {l["author"]: l for l in self.named(self.rival_chains(), {"lane-t": [("tq1", NOW - 10 * 3600)]}, "lane-t")}
        self.assertEqual((got["seat-q"]["landed"], got["seat-p"]["landed"]), (True, False))
        self.assertFalse(got["seat-p"]["decided"])

    def test_a_merge_of_a_tip_no_chain_carries_lands_one_chain_the_latest_before_it(self):
        late = {l["author"]: l["landed"] for l in self.named(
            self.rival_chains(), {"lane-t": [("rebased", NOW - 10 * 3600)]}, "lane-t")}
        self.assertEqual(late, {"seat-p": False, "seat-q": True})
        early = {l["author"]: l["landed"] for l in self.named(
            self.rival_chains(), {"lane-t": [("rebased", NOW - 25 * 3600)]}, "lane-t")}
        self.assertEqual(early, {"seat-p": True, "seat-q": False})


class SeatModelTest(unittest.TestCase):
    """A SEAT'S MODEL IS ITS LATEST: seat-k's verdicts moved from gpt-5.6-sol
    to gpt-6-astra inside the window, so its entry (and a #models?seat= link)
    names gpt-6-astra, while each lane stays credited to the model that ran
    it at the time."""

    def test_a_seat_names_the_model_its_latest_verdicts_record(self):
        b = run_board(ledger() + codex3_rows())
        seat = {x["seat"]: x for x in b["seats"]}["seat-k"]
        self.assertEqual(seat["model"], "gpt-6-astra")
        self.assertEqual(model(b, "gpt-5.6-sol")["measured"]["lanes"], 1)    # lane-k, run then
        self.assertIn("seat-k", model(b, "gpt-6-astra")["seats"])


class GapTest(unittest.TestCase):
    """THE GAP IS AGAINST A MAKER'S NUMBER ONLY: the fleet's pooled rate is
    where a model with no public benchmark starts, never a benchmark, so such
    a model has no gap on the board, in the text or on the page."""

    def test_no_public_prior_means_no_gap(self):  # noqa: VACUOUS_ASSERTION — the measured state and the absent prior are asserted first, unconditionally, on the same model
        b = run_board(ledger() + grok_lanes(clean=3))
        g = model(b, "grok-build-0.1")
        self.assertEqual(g["score"]["state"], "measured")
        self.assertIsNone(g["prior"])
        self.assertIsNone(g["score"]["gap"])
        lines = scorecard.board_lines(b)
        at = next((i for i, ln in enumerate(lines) if ln.startswith("gaps between")), len(lines))
        gaps = lines[at:at + 1 + next((j for j, ln in enumerate(lines[at + 1:]) if not ln), 0)]
        self.assertFalse([ln for ln in gaps if "grok-build-0.1" in ln], gaps)

    def test_a_measured_model_with_a_prior_keeps_its_gap(self):
        b = run_board(ledger() + grok_lanes(clean=3, seat="seat-q", family="qwen27", model="qwen27"))
        q = model(b, "qwen27")
        self.assertEqual(q["score"]["state"], "measured")
        self.assertIsNotNone(q["score"]["gap"])


class NoZeroForARealCountTest(unittest.TestCase):
    """A REAL COUNT NEVER READS 0: reviews and patches by family are counts
    over the model's lands, and a per-land ratio over a real count keeps two
    significant digits where two decimals would round it to 0."""

    def setUp(self):
        rows = ledger() + grok_lanes(clean=201) + [
            disp("gx", "seat-g", "seat-a", "lane-seat-g-0", "review", 2.4, root="seat-g-0b", tip="tgx"),
            verdict("gx", 2.35, "approve", "tgx", "claude", None, patch_author="seat-a")]
        self.cost = model(run_board(rows), "grok-build-0.1")["cost"]

    def test_by_family_is_counted(self):
        self.assertEqual(self.cost["by_family"], {"anthropic": 1, "codex": 201})
        self.assertEqual(self.cost["patches_by_family"], {"anthropic": 1})
        self.assertEqual((self.cost["lands"], self.cost["reads"], self.cost["patches"]), (201, 202, 1))

    def test_a_ratio_over_a_real_count_is_never_zero(self):
        self.assertEqual(self.cost["reads_per_land"], 1.0)
        self.assertEqual(self.cost["patches_per_land"], 0.005)      # 1 of 201, not 0.0


class RoundOnceTest(unittest.TestCase):
    """EVERY NUMBER IS ROUNDED ONCE, HERE: the text and the page print what
    the board carries and round nothing again, so a half never reads one way
    in the CLI and another on the page (62.5 read 62 in one and 63 in the
    other). Half rounds up."""

    def setUp(self):
        self.b = run_board(ledger() + grok_lanes(clean=5, withdrawn=3, slow=4))
        self.g = model(self.b, "grok-build-0.1")

    def test_the_board_carries_whole_numbers(self):  # noqa: VACUOUS_ASSERTION — the fixture's models are non-empty (grok-build-0.1 is asserted by name in the sibling arms), and each key is checked on every row
        for m in self.b["models"]:
            s = m["score"]
            for key in ("posterior", "prior", "ours", "gap"):
                self.assertTrue(s[key] is None or type(s[key]) is int, (m["model"], key, s[key]))
            self.assertTrue(s["band"] is None or all(type(v) is int for v in s["band"]), m["model"])
            h = m["measured"]["handback_median_min"]
            self.assertTrue(h is None or type(h) is int, (m["model"], h))

    def test_a_half_rounds_up_once(self):
        self.assertEqual(self.g["score"]["ours"], 63)                 # 5 of 8 = 62.5
        self.assertEqual(self.g["measured"]["handback_median_min"], 31)   # 30.5 minutes

    def test_the_text_prints_the_boards_numbers(self):
        lines = scorecard.board_lines(self.b)
        head = [ln for ln in lines if ln.startswith("rank ")][0]
        row = [ln for ln in lines if ln.split()[1:2] == ["grok-build-0.1"]][0]
        col = lambda name: row[head.index(name):].split("  ")[0].strip()
        self.assertEqual(col("ours"), "63")
        self.assertEqual(col("hand-back"), "31m")
        self.assertEqual(col("score"), str(self.g["score"]["posterior"]))

    def test_the_totals_say_which_count_is_which(self):  # noqa: VACUOUS_ASSERTION — the header and totals line are read off the board's own lines, each asserted to contain its count
        lines = scorecard.board_lines(self.b)
        self.assertIn("%d lanes from the dispatch ledger" % self.b["lanes"], lines[1])
        self.assertIn("%d decided" % self.b["decided"], lines[1])
        self.assertNotEqual(self.b["lanes"], self.b["decided"])
        head = [ln for ln in lines if ln.startswith("rank ")][0]
        self.assertIn(" decided ", head)


class ReaderTipsTest(unittest.TestCase):
    """A READER IS JUDGED ONCE PER TIP: a seat that read one tip twice before
    the approval tier's later read has one pair on it, so "on N of M tips"
    counts distinct tips."""

    def test_a_second_read_of_the_same_tip_is_not_a_second_pair(self):  # noqa: VACUOUS_ASSERTION — the exact (pairs, agree, misses) tuple is the unconditional structural assertion
        rows = ledger() + [{"event": "findings-note", "id": "c1", "ts": ts(11.4), "reader": "qwen27",
                            "reviewed_tip": "tc1", "outcome": "complete", "kept": "0"}]
        r = model(run_board(rows), "qwen27")["measured"]["reader"]
        self.assertEqual((r["pairs"], r["agree"], r["misses"]), (2, 1, 1))


class FixMedianTest(unittest.TestCase):
    """FIX VERDICTS PER LANE ARE COUNTED OVER DECIDED LANES: a lane still in
    flight has not had all its reads, so its FIX count so far would pull the
    median down."""

    def test_an_in_flight_lane_is_left_out_of_the_fix_median(self):
        rows = ledger() + [disp("c2", "qwenlocal", "seat-b", "lane-c2", "review", 3, tip="tc2")]
        m = model(run_board(rows), "qwenlocal")["measured"]
        self.assertEqual(m["lanes"], 2)
        self.assertEqual(m["fix_per_lane_median"], 1)


class BlendTest(unittest.TestCase):
    def test_the_prior_dominates_a_thin_record_and_fades_as_lanes_accrue(self):
        thin = scorecard.blend(0.9, 0, 1)
        thick = scorecard.blend(0.9, 20, 100)
        self.assertGreater(thin["posterior"], 0.7)            # one failure barely moves it
        self.assertLess(abs(thick["posterior"] - 0.2), 0.05)  # 100 lanes: ours
        self.assertLess(thick["band"][1] - thick["band"][0],
                        thin["band"][1] - thin["band"][0])

    def test_the_band_stays_inside_zero_and_one(self):
        b = scorecard.blend(1.0, 3, 3)
        self.assertGreaterEqual(b["band"][0], 0.0)
        self.assertLessEqual(b["band"][1], 1.0)


class DataTest(unittest.TestCase):
    def setUp(self):
        # Built-in rungs fallback: point at an empty HELM_HOME, so the real
        # seat-rungs.json the mentor wrote is not read by these arms.
        self._tmp = tempfile.mkdtemp(prefix="helm-rungs-")
        self._env = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self._tmp

    def tearDown(self):
        if self._env is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._env

    def test_every_prior_names_its_benchmark_source_date_and_harness(self):
        self.assertTrue(scorecard.PRIORS)
        for key, p in scorecard.PRIORS.items():
            self.assertTrue(p["exact"], key)
            self.assertTrue(p["scores"], key)
            for s in p["scores"]:
                self.assertTrue(s["bench"], key)
                self.assertTrue(0 < s["value"] <= 100, key)
                self.assertTrue(s["source"].startswith("https://"), key)
                self.assertRegex(s["date"], r"^\d{4}-\d{2}(-\d{2})?$")
                self.assertTrue(s["harness"], key)

    def test_the_rungs_are_data_and_only_the_owner_admits(self):
        self.assertEqual(scorecard.RUNGS, ("input", "non-door reviewer",
                                           "door reader on stated invariants", "admitted"))
        for seat, r in scorecard.SEAT_RUNGS.items():
            self.assertIn(r["step"], scorecard.RUNGS[:-1], seat)   # nobody seeded as admitted
            self.assertTrue(r["next"], seat)
            self.assertTrue(r["source"], seat)
        self.assertEqual(sorted(scorecard.SEAT_RUNGS), ["bonsai", "qwen27", "qwenlocal"])
        rungs = run_board()["rungs"]
        self.assertEqual(rungs["ladder"], list(scorecard.RUNGS))   # the web page joins this list
        self.assertEqual(rungs["ladders"], {"reader": list(scorecard.RUNGS)})
        self.assertIn("owner", rungs["who_admits"])


class GradeCardWordsTest(unittest.TestCase):
    """THE RUNGS READ IN THE GRADE CARD'S OWN WORDS: the mentor's cards (the
    local-seat-grade-cards journal) propose bonsai as "non-door reviewer on
    briefed tables", a narrower rung than qwen27's, and ask qwenlocal for each
    claimed FAB log. The board quotes each card's rung and its "To move to"
    sentence, and places the seat on the ladder by its step, so bonsai never
    reads equal to qwen27."""

    CARD = {
        "qwen27": ("non-door reviewer", "non-door reviewer",
                   "5 door reads briefed with the stated-invariant table plus the standing rows, "
                   "each scored against the approval-tier read of the same tip, with every stated "
                   "row judged right and no stated-row miss"),
        "qwenlocal": ("input", "input",
                      "3 non-door reads against an answer key with every stated row judged right, "
                      "no false findings, no rebuilding in place of reading, and each claimed fab "
                      "log quoted"),
        "bonsai": ("non-door reviewer on briefed tables", "non-door reviewer",
                   "5 briefed reads (non-door first) with EVERY asked row answered (no skipped "
                   "row), no stated-row miss against the key, and numbers quoted from receipts, "
                   "not memory"),
    }

    def setUp(self):
        # The built-in grade cards are the fallback; point at an empty
        # HELM_HOME so the data file the mentor wrote is not read here.
        self._tmp = tempfile.mkdtemp(prefix="helm-rungs-")
        self._env = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = self._tmp

    def tearDown(self):
        if self._env is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self._env

    def test_each_seat_reads_its_cards_rung_step_and_next(self):  # noqa: VACUOUS_ASSERTION — an exact equality with the non-empty CARD table is the unconditional structural assertion
        got = {seat: (r["rung"], r["step"], r["next"]) for seat, r in scorecard.SEAT_RUNGS.items()}
        self.assertEqual(got, self.CARD)

    def test_the_board_carries_the_words_and_the_ladder_places_the_step(self):
        rows = {r["seat"]: r for r in run_board()["rungs"]["seats"]}
        self.assertEqual(rows["bonsai"]["rung"], "non-door reviewer on briefed tables")
        self.assertEqual(rows["bonsai"]["next_rung"], "door reader on stated invariants")
        self.assertNotEqual(rows["bonsai"]["rung"], rows["qwen27"]["rung"])
        self.assertIn("each claimed fab log quoted", rows["qwenlocal"]["next"])
        self.assertNotIn("test log", rows["qwenlocal"]["next"])
        text = "\n".join(scorecard.board_lines(run_board()))
        self.assertIn("bonsai [reader] — non-door reviewer on briefed tables", text)


class DataFileRungsTest(unittest.TestCase):
    """THE RUNGS CAN LIVE IN <global dir>/seat-rungs.json: the data file the
    mentor edits, the code only reads it. The board shows the source line under
    the rungs, each seat's own track and its model, and a rung taken from the
    seat's OWN track's ladder (qwenlocal's next reader rung is "non-door
    reviewer", the builder's is the builder ladder's next). A file the board
    cannot use is one honest fallback line: missing, unreadable, or a step off
    its ladder.
    """

    def _home_with(self, text):
        tmp = tempfile.mkdtemp(prefix="helm-rungs-file-")
        old = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = tmp
        self.addCleanup(lambda old=old: os.environ.__setitem__("HELM_HOME", old)
                        if old is not None else os.environ.pop("HELM_HOME", None))
        os.makedirs(os.path.join(tmp, "_global"), exist_ok=True)
        with open(os.path.join(tmp, "_global", "seat-rungs.json"), "w") as f:
            f.write(text)
        return tmp

    def test_a_valid_file_shows_its_seats_with_track_model_and_own_track_rung(self):
        self._home_with(json.dumps({
            "ladders": {"reader": ["input", "non-door reviewer", "admitted"],
                        "builder": ["supervised slices", "clean slices", "admitted"]},
            "seats": [
                {"seat": "qwenlocal", "track": "reader", "rung": "input",
                 "step": "input", "model": "Ornith", "by": "m", "date": "2026-09-30",
                 "next": "3 non-door reads", "evidence": "x", "source": "s"},
                {"seat": "qwenlocal", "track": "builder", "rung": "supervised slices",
                 "step": "supervised slices", "model": "Ornith", "by": "m",
                 "date": "2026-09-30", "next": "1 in 5", "evidence": "y", "source": "s"},
            ]}))
        b = run_board()
        src = b["rungs"]["source_line"]
        self.assertIn("seat-rungs.json", src)
        self.assertNotIn("built-in", src)
        rows = {(r["seat"], r["track"]): r for r in b["rungs"]["seats"]}
        self.assertEqual(rows[("qwenlocal", "reader")]["next_rung"], "non-door reviewer")
        self.assertEqual(rows[("qwenlocal", "builder")]["next_rung"], "clean slices")
        self.assertEqual(rows[("qwenlocal", "reader")]["model"], "Ornith")
        text = "\n".join(scorecard.board_lines(b))
        self.assertIn("Ornith", text)

    def test_a_missing_file_falls_back_to_builtin_and_says_missing(self):
        tmp = tempfile.mkdtemp(prefix="helm-rungs-miss-")
        old = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = tmp
        self.addCleanup(lambda old=old: os.environ.__setitem__("HELM_HOME", old)
                        if old is not None else os.environ.pop("HELM_HOME", None))
        lad, seats, src = scorecard.load_rungs()
        self.assertEqual(lad, {"reader": list(scorecard.RUNGS)})
        self.assertIn("missing", src)
        self.assertTrue(seats)  # built-in seats still load

    def test_a_garbage_file_falls_back_and_says_unreadable(self):
        self._home_with("{ not json")
        _, _, src = scorecard.load_rungs()
        self.assertIn("unreadable", src)
        self.assertEqual(scorecard.load_rungs()[0], {"reader": list(scorecard.RUNGS)})

    def test_a_seat_off_its_ladder_is_skipped_but_the_others_show(self):
        self._home_with(json.dumps({
            "ladders": {"reader": ["input", "admitted"], "builder": ["clean slices", "admitted"]},
            "seats": [
                {"seat": "qwenlocal", "track": "reader", "rung": "input",
                 "step": "input", "model": "Ornith", "by": "m", "date": "2026-09-30",
                 "next": "n", "evidence": "e", "source": "s"},
                # step "admitted" is on the ladder, but this one names a rung NOT on it
                {"seat": "qwen27", "track": "builder", "rung": "peer-reviewed",
                 "step": "peer-reviewed", "model": "Opus", "by": "m", "date": "2026-09-30",
                 "next": "n", "evidence": "e", "source": "s"},
            ]}))
        b = run_board()
        rows = {r["seat"]: r for r in b["rungs"]["seats"]}
        self.assertIn("qwenlocal", rows)
        self.assertNotIn("qwen27", rows)
        # the file was used, not the built-in fallback
        self.assertNotIn("built-in", b["rungs"]["source_line"])

    def _one(self, **seat):
        base = {"seat": "qwen27", "track": "reader", "rung": "input", "step": "input", "model": "M",
                "by": "m", "date": "2026-09-30", "next": "n", "evidence": "e", "source": "s"}
        return dict(base, **seat)

    def test_the_web_ladder_stays_a_list_and_the_tracks_ride_beside_it(self):
        # the console's Models page does rungs.ladder.join(" → "); a dict there breaks the page
        self._home_with(json.dumps({"ladders": {"reader": ["input", "admitted"], "builder": ["b1", "b2"]},
                                    "seats": [self._one()]}))
        b = run_board()
        self.assertEqual(b["rungs"]["ladder"], ["input", "admitted"])
        self.assertEqual(sorted(b["rungs"]["ladders"]), ["builder", "reader"])

    def test_a_seat_on_the_top_rung_has_no_next_rung_and_the_board_still_renders(self):
        self._home_with(json.dumps({"ladders": {"reader": ["input", "admitted"]},
                                    "seats": [self._one(rung="admitted", step="admitted")]}))
        b = run_board()
        self.assertIsNone(b["rungs"]["seats"][0]["next_rung"])
        self.assertIn("none (the top rung)", "\n".join(scorecard.board_lines(b)))

    def test_the_text_board_names_each_rows_track(self):
        self._home_with(json.dumps({"ladders": {"reader": ["input", "admitted"], "builder": ["b1", "b2"]},
                                    "seats": [self._one(), self._one(track="builder", rung="b1", step="b1")]}))
        text = "\n".join(scorecard.board_lines(run_board()))
        self.assertIn("qwen27 [reader]", text)
        self.assertIn("qwen27 [builder]", text)

    def test_skipped_seats_are_counted_and_a_file_with_none_left_says_so(self):
        self._home_with(json.dumps({"ladders": {"reader": ["input", "admitted"]},
                                    "seats": [self._one(), self._one(seat="bonsai", step="nowhere")]}))
        self.assertIn("1 seat entry off its ladder skipped", scorecard.load_rungs()[2])
        self._home_with(json.dumps({"ladders": {"reader": ["input", "admitted"]},
                                    "seats": [self._one(step="nowhere")]}))
        src = scorecard.load_rungs()[2]
        self.assertIn("no seat on its ladders: 1 skipped", src)
        self.assertNotIn("missing", src)

    def test_a_seat_entry_that_is_not_a_mapping_is_skipped_and_counted(self):
        # a ladder keyed "" is the track a non-mapping entry reads as; the
        # entry must be skipped and counted, never raise out of load_rungs
        self._home_with(json.dumps({"ladders": {"reader": ["input", "admitted"], "": ["input"]},
                                    "seats": [self._one(), "not-a-seat"]}))
        lad, seats, src = scorecard.load_rungs()
        self.assertEqual([s["seat"] for s in seats], ["qwen27"])
        self.assertIn("1 seat entry off its ladder skipped", src)

    def test_the_json_form_carries_the_source_line(self):
        self._home_with(json.dumps({
            "ladders": {"reader": ["input", "admitted"]},
            "seats": [{"seat": "qwenlocal", "track": "reader", "rung": "input",
                       "step": "input", "model": "Ornith", "by": "m", "date": "2026-09-30",
                       "next": "n", "evidence": "e", "source": "s"}]
        }))
        tmp = tempfile.mkdtemp(prefix="helm-scorecard-")
        path = os.path.join(tmp, "dispatches.jsonl")
        with open(path, "w") as f:
            for row in ledger():
                f.write(json.dumps(row) + "\n")
        out = io.StringIO()
        with mock.patch.dict(scorecard._CACHE, clear=True), \
                mock.patch.object(scorecard, "ledger_path", return_value=path), \
                mock.patch.object(scorecard, "trunk_lanes", return_value=(TRUNK, None)), \
                mock.patch.object(scorecard.time, "time", return_value=NOW), redirect_stdout(out):
            from helm import evalpin
            rc = evalpin.cmd_eval(["board", "--json", "--window", "30d"])
        self.assertEqual(rc, 0)
        got = json.loads(out.getvalue())
        self.assertIn("seat-rungs.json", got["rungs"]["source_line"])


class CliTest(unittest.TestCase):
    def test_eval_board_prints_json_from_the_ledger_it_reads(self):
        tmp = tempfile.mkdtemp(prefix="helm-scorecard-")
        path = os.path.join(tmp, "dispatches.jsonl")
        with open(path, "w") as f:
            for row in ledger():
                f.write(json.dumps(row) + "\n")
            f.write("{not json\n")
        out = io.StringIO()
        with mock.patch.dict(scorecard._CACHE, clear=True), \
                mock.patch.object(scorecard, "ledger_path", return_value=path), \
                mock.patch.object(scorecard, "trunk_lanes", return_value=(TRUNK, None)), \
                mock.patch.object(scorecard.time, "time", return_value=NOW), redirect_stdout(out):
            from helm import evalpin
            rc = evalpin.cmd_eval(["board", "--json", "--window", "30d"])
        self.assertEqual(rc, 0)
        got = json.loads(out.getvalue())
        self.assertEqual(got["window"], "30d")
        self.assertEqual(got["sources"]["ledger"]["skipped"], 1)
        self.assertIn("qwen27", [m["model"] for m in got["models"]])

    def test_a_bad_window_is_refused(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(scorecard.cmd(["--window", "9y"]), 2)


if __name__ == "__main__":
    unittest.main()
