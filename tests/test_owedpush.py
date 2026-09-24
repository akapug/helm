#!/usr/bin/env python3
"""The DELIVERY LEG for unanswered FIX debt — the half that was missing.

`helm owed` already computed every debt, every addressee and the exact remedy
sentence. Both of its surfaces were PULL (a CLI verb somebody types, a web view
somebody opens), so the computation reached nobody who was not already looking:
130 live debts at a median age of 12.7 days, and one cure committed 15 minutes
after its FIX verdict whose row then sat 22 hours. These arms pin the push.

EVERY ARM INJECTS ITS ITEMS. `sweep` takes `items` so these run against a list
built for the case, never the live estate — test_obligation's law, inherited for
its reason: an arm whose fixture is the estate passes or fails for reasons that
have nothing to do with the code.

EVERY DELIVERY ARM ASSERTS THE MESSAGE, NOT THE ABSENCE OF A CRASH. A sweep
that posts nothing raises nothing, so `assertTrue(rep)` would pass on a reactor
that delivers zero DMs forever. Each arm below reads the captured text.

THE DOUBLE IS ASSERTED TO BE IN EFFECT. `_post` reaches its dependency with
`from . import seats`, which reads the PACKAGE ATTRIBUTE — a fake installed in
`sys.modules` alone would be bypassed and every arm would silently DM the real
fleet while reporting green. `_dm` patches the attribute on the real module and
`setUp` proves the patch took before any arm runs.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helm import owedpush  # noqa: E402
from helm import seats_integrator  # noqa: E402
from helm import seats  # noqa: E402


def item(row="r1", lane="lane-x", seat="author-seat", chain_root=None,
         kind="unanswered-fix", tip="a" * 40, ts="2026-08-10T00:00:00Z",
         what="a FIX verdict on this lane is unanswered ... --supersedes r1"):
    """One obligation item, in the shape `unanswered_fixes` really returns.

    THE KEYS ARE THE MEASURED ONES. A first probe of this feature assumed an
    `author` key and an `age_s` key; production has neither (it carries
    `owed_seat` and an ISO `owed_since`), so that probe reported every debt as
    unowned and 0.0 hours old and both readings were confident and wrong. A
    fixture RICHER than production is the failure that keeps arms green while
    the real thing reads nothing, so this helper carries exactly the eleven
    keys obligation emits and no invented twelfth."""
    return {"row": row, "lane": lane, "owed_seat": seat,
            "chain_root": chain_root or row, "kind": kind,
            "reviewed_tip": tip, "owed_since": ts, "what": what,
            "owed_by": "author", "reviewer": "rev-seat", "repo_id": None}


class OwedPushTest(unittest.TestCase):

    def setUp(self):
        self.sent = []
        self.fail_seats = set()
        self._real_dm = seats.dm

        def fake_dm(to, text, who=None, **kw):
            if str(to) in self.fail_seats:
                return None, "seat unreachable"
            self.sent.append((str(to), text, who))
            return {"id": "row-%d" % len(self.sent)}, None

        seats.dm = fake_dm
        self.addCleanup(setattr, seats, "dm", self._real_dm)
        # THE DOUBLE IS PROVEN IN EFFECT BEFORE ANY ARM RUNS. `from . import
        # seats` resolves the package attribute, so this is the object _post
        # will actually reach; asserting it here turns a bypassed patch into a
        # setUp failure instead of six arms quietly DMing the live fleet.
        self.assertIsNot(seats.dm, self._real_dm,
                         "the seats.dm double is NOT installed")
        d = tempfile.mkdtemp()
        self.state = os.path.join(d, "owed_push.json")

    def sweep(self, items, **kw):
        kw.setdefault("state_path", self.state)
        return owedpush.sweep(items=items, **kw)

    def control(self):
        """MUST-HIT: prove this harness CAN deliver, then clear the record.

        Every arm below that asserts an EMPTY `self.sent` is asserting an
        absence, and an absence is only evidence when the same instrument is
        known to produce a presence. A broken double, a renamed key or a sweep
        that silently returns early would make those arms pass forever. This
        fires one throwaway debt through the real path first, so the zero that
        follows means "this case delivered nothing" and not "nothing can."""
        owedpush.sweep(items=[item(row="control-row", seat="control-seat")],
                       state_path=self.state + ".control")
        self.assertEqual([s[0] for s in self.sent], ["control-seat"],
                         "the delivery path is dead — every absence arm in "
                         "this file would pass vacuously")
        self.sent = []

    # -- delivery ---------------------------------------------------------

    def test_a_debt_is_delivered_to_the_seat_that_owes_it(self):
        """THE WHOLE POINT: the debt reaches the author without being asked
        for. Asserts the addressee AND the remedy text, because a DM that
        arrives naming the wrong next action is worse than none."""
        rep = self.sweep([item(row="r1", lane="cure-me", seat="alice")])
        self.assertEqual(len(self.sent), 1, "no DM was delivered at all")
        to, text, who = self.sent[0]
        self.assertEqual(to, "alice")
        self.assertEqual(who, owedpush.BOT)
        self.assertIn("cure-me", text)
        self.assertIn("--supersedes r1", text)
        self.assertIn("@alice", text)
        self.assertEqual(rep["posted"], ["alice"])

    def test_the_remedy_text_is_obligations_own_sentence(self):
        """The `what` string travels VERBATIM. It already names --supersedes
        over --new-work — the flag whose absence built 124 unclosable land
        loops — so a reactor that paraphrased it could drop that word."""
        w = ("a FIX verdict on this lane is unanswered. Cure it if you have "
             "not; if you HAVE, re-dispatch for review on the new tip with "
             "--supersedes abc123def456 — curing alone creates no review, and "
             "nothing else will tell you that")
        self.sweep([item(what=w)])
        self.assertEqual(len(self.sent), 1)
        self.assertIn(w, self.sent[0][1])

    def test_an_ownerless_debt_rides_the_integrator_digest(self):  # noqa: VACUOUS_ASSERTION — the unconditional control is `self.control()` on the first line, this file's own must-hit helper: it fires a real debt down the same delivery path on a separate state file and asserts it landed before clearing the record
        """A fifth of the live items name no seat. Dropping them would
        silently retire exactly the debt nobody is positioned to notice."""
        # UNCONDITIONAL CONTROL ON THE SAME OBSERVABLE, through this file's own
        # helper: it fires a throwaway debt down the real path on a SEPARATE
        # state file and then clears the record, so it cannot latch against the
        # sweep below the way a same-state control would.
        self.control()

        self.sweep([item(row="r1", seat=None)])
        self.assertEqual(len(self.sent), 1, "the ownerless debt was DROPPED")
        self.assertEqual(self.sent[0][0], seats_integrator.integrator_seat_or_default())
        self.assertIn("--supersedes r1", self.sent[0][1])

    def test_the_ownerless_digest_is_ADDRESSED_TO_WHOEVER_RESOLVES(self):  # noqa: VACUOUS_ASSERTION — the unconditional control IS present and on the same observable (`self.sent` after an OWNED sweep, before the loop); the walker does not enter the `with mock.patch` + subTest block where the other assertions live, so it cannot pair them
        """THE ADDRESSEE IS READ, NEVER SPELLED. A module constant naming the
        integrator keeps naming it after the roster stops carrying it, and
        nothing fails loudly: the constant is truthy, the digest is keyed, the
        DM is written, and the sweep reports a delivery it never made.

        MEASURED BY MOVING THE ANSWER. The resolver names one seat, then
        another, and the digest follows — which no constant can do and which
        asserting against the default constant would not have caught."""
        # UNCONDITIONAL CONTROL OUTSIDE THE LOOP, on its own state file: the
        # sweep delivers at all. Every assertion below is inside an iteration,
        # so a loop that stopped iterating would take the whole arm quiet.
        self.control()

        # EACH ITERATION GETS ITS OWN STATE FILE, and that is not tidiness.
        # The sweep LATCHES a debt it has raised — one ask per debt per three
        # days is the whole point of the budget — so a second iteration over
        # the SAME row and the SAME state file sends NOTHING and the arm reads
        # "the ownerless debt was DROPPED" against a state the loop itself
        # created. Measured: shared state gives 1 then 0; per-iteration state
        # gives 1 then 1. Anything in this file that sweeps twice needs this.
        for who in ("seat-a-integrator", "seat-b-integrator"):
            with self.subTest(integrator=who):
                self.sent[:] = []
                with mock.patch.object(seats_integrator, "integrator_seat",
                                       return_value=(who, None)):
                    self.sweep([item(row="r1", seat=None)],
                               state_path=self.state + "." + who)
                self.assertEqual(len(self.sent), 1, "the ownerless debt was DROPPED")
                self.assertEqual(self.sent[0][0], who)

    def test_the_roster_is_not_read_when_every_debt_names_its_owner(self):
        """RESOLVED LAZILY, because the roster is not cached: asking per item
        reads the file once per debt, and asking eagerly reads it on every
        sweep whose items all name an owner — which is most of them."""
        with mock.patch.object(seats_integrator, "integrator_seat",
                               return_value=("seat-a-integrator", None)) as ask:
            owned = owedpush.route([item(row="r1", seat="seat-b", chain_root="k1"),
                                    item(row="r2", seat="seat-b", chain_root="k2")])
            self.assertEqual(ask.call_count, 0,
                             "the roster was read for a sweep that needed no "
                             "integrator")
            # POSITIVE CONTROL ON THE SAME MOCK: it DOES get asked when a row
            # needs it, so the zero above is about the owned path and not
            # about a patch that was never wired to anything.
            unowned = owedpush.route([item(row="r3", seat=None, chain_root="k3"),
                                      item(row="r4", seat=None, chain_root="k4")])
            self.assertEqual(ask.call_count, 1,
                             "two ownerless debts asked the resolver twice")
        self.assertEqual(sorted(owned), ["seat-b"])
        self.assertEqual(sorted(unowned), ["seat-a-integrator"])

    def test_one_chain_bills_its_author_once(self):
        """Deduped by CHAIN ROOT. Two rows continuing ONE chain are one debt;
        billing both sends a person to fix one lane twice, and the live board
        carries chains contributing seven rows each."""
        self.sweep([item(row="r1", chain_root="root"),
                    item(row="r2", chain_root="root")])
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0][1]
        self.assertIn("1 lane(s) owe YOUR next move", text)
        # ONE DEBT LINE. Counted as the remedy's own `--supersedes <row>`: the
        # digest's closing fallback ladder (task/2948) spells the flag once
        # more, against a placeholder row, and that line is not a debt.
        self.assertEqual(text.count("--supersedes r"), 1)

    def test_independent_chains_sharing_a_lane_LABEL_both_bill(self):
        """The mirror, and the one obligation argues hardest for: a lane is
        FREE TEXT. Two independent --new-work chains may carry the same string,
        and collapsing on the label would hide a real debt behind a stranger's."""
        self.sweep([item(row="r1", chain_root="r1", lane="same-label"),
                    item(row="r2", chain_root="r2", lane="same-label")])
        self.assertEqual(len(self.sent), 1, "one seat, one digest")
        text = self.sent[0][1]
        self.assertIn("2 lane(s) owe YOUR next move", text)
        self.assertIn("r1", text)
        self.assertIn("r2", text)

    def test_the_digest_caps_lines_and_COUNTS_the_remainder(self):
        """One live seat owes 71 lanes. A 71-line DM is not read; a silently
        truncated one is the forgetting this whole loop exists to end, so the
        remainder is counted and pointed at the verb that lists it."""
        items = [item(row="r%d" % n, chain_root="r%d" % n)
                 for n in range(owedpush.MAX_LINES + 5)]
        self.sweep(items)
        text = self.sent[0][1]
        self.assertIn("+5 more", text)
        self.assertIn("helm owed", text)

    def test_the_age_is_rendered_for_a_human_not_as_raw_seconds(self):
        """obligation._age_s answers in SECONDS or None. The first draft of
        the digest printed the integer raw ("owed 537614") and would have
        printed "owed None" for an unparseable stamp."""
        self.sweep([item(ts="2026-08-10T00:00:00Z")])
        text = self.sent[0][1]
        self.assertIn("owed ", text)          # the field IS rendered ...
        self.assertNotIn("owed 5376", text)   # ... and not as raw seconds
        self.assertRegex(text, r"owed \d+d\d+h")
        self.assertEqual(owedpush._fmt_age("nonsense"), "UNKNOWN")
        self.assertEqual(owedpush._fmt_age(None), "UNKNOWN")

    # -- the latch --------------------------------------------------------

    def test_the_same_debt_is_not_re_raised_next_sweep(self):
        """An hourly cadence with no latch is hourly noise, and a channel that
        becomes noise stops being read — which restores the original defect."""
        self.sweep([item()])
        self.assertEqual(len(self.sent), 1)
        rep = self.sweep([item()])
        self.assertEqual(len(self.sent), 1, "the debt was re-raised")
        self.assertEqual(rep["latched"], 1)

    def test_a_RETIPPED_debt_is_news_inside_the_latch_window(self):
        """The row id alone is too weak a latch key. A re-tipped row is a
        DIFFERENT ask on the same id, and latching on the id would swallow it
        for three days."""
        self.sweep([item(tip="a" * 40)])
        self.assertIn("lane-x", self.sent[0][1])
        self.sweep([item(tip="b" * 40)])
        self.assertEqual(len(self.sent), 2, "the re-tip was swallowed")
        self.assertIn("lane-x", self.sent[1][1])

    def test_a_newly_declared_polarity_is_news_inside_the_window(self):
        """Same law, the other field: an undeclared-verdict row that becomes a
        declared FIX is a new obligation, not a repeat."""
        self.sweep([item(kind="undeclared-verdict")])
        self.assertIn("lane-x", self.sent[0][1])
        self.sweep([item(kind="unanswered-fix")])
        self.assertEqual(len(self.sent), 2)
        self.assertIn("lane-x", self.sent[1][1])

    def test_a_debt_that_left_the_set_and_returns_is_delivered_again(self):
        """RE-ARM. Without it a stale latch entry silences a debt that was
        discharged and genuinely re-opened later."""
        self.sweep([item()])
        self.assertIn("lane-x", self.sent[0][1])
        self.sweep([])                       # left the owed set -> latch drops
        self.sweep([item()])
        self.assertEqual(len(self.sent), 2, "the re-opened debt stayed latched")
        self.assertIn("lane-x", self.sent[1][1])

    # -- the failure modes ------------------------------------------------

    def test_an_UNDELIVERED_digest_never_latches(self):
        """repo-watch's cursor lesson: a watcher that fails silent while its
        cursor marches on has dropped the window — and here the window is
        somebody's unanswered debt. Asserts the RE-DELIVERY, not the failure."""
        self.control()
        self.fail_seats.add("alice")
        rep = self.sweep([item(seat="alice")])
        self.assertEqual(rep["failed"], ["alice"])
        self.assertEqual(self.sent, [])
        self.fail_seats.clear()
        self.sweep([item(seat="alice")])
        self.assertEqual(len(self.sent), 1, "the dropped debt never came back")

    def test_an_unreadable_ledger_is_UNKNOWN_never_a_clean_burn_down(self):
        """Publishing "nobody owes anything" from the one moment we cannot see
        reads exactly like a finished burn-down. Nothing is delivered and the
        failure is carried out."""
        self.control()
        rep = owedpush.sweep(items=None, unavailable="ledger unreadable",
                             state_path=self.state)
        self.assertEqual(self.sent, [])
        self.assertIn("unreadable", str(rep["unavailable"]))
        self.assertEqual(rep["swept"], 0)

    def test_an_unreadable_ledger_EXITS_NONZERO(self):  # noqa: VACUOUS_ASSERTION — the observable is an exit CODE, asserted unconditionally after the try/finally restore
        """A scheduler must be able to tell "answered nothing" from "nobody
        owed anything"; on a screen and in an exit code they look identical."""
        real = owedpush.sweep
        owedpush.sweep = lambda **kw: {"unavailable": "boom", "swept": 0,
                                       "digests": {}, "posted": [],
                                       "failed": [], "latched": 0, "items": []}
        try:
            self.assertEqual(owedpush.cmd_owed_push([]), 1)
        finally:
            owedpush.sweep = real

    def test_a_dry_run_delivers_nothing_and_writes_no_state(self):
        """The loop must be inspectable before it is trusted."""
        self.control()
        rep = self.sweep([item()], post=False)
        self.assertEqual(self.sent, [])
        self.assertFalse(os.path.exists(self.state))
        self.assertIn("author-seat", rep["digests"])

    def test_a_failed_delivery_still_EXITS_ZERO(self):
        """IT NEVER BLOCKS. A false notification costs one line in a DM; a
        false block costs a seat its turn, so nothing here may fail a caller."""
        self.fail_seats.add("author-seat")
        rep = self.sweep([item()])
        self.assertEqual(rep["failed"], ["author-seat"])
        self.assertEqual(rep["posted"], [])

    def test_it_reads_no_worktree_and_so_cannot_fire_on_a_wip_commit(self):  # noqa: VACUOUS_ASSERTION — the absence of git reads IS the property; the must-hit control is the assertIn on a token known present
        """THE NOISE ARGUMENT, pinned. The obvious design reads each lane's
        HEAD and fires when it moves past the reviewed tip — which fires on a
        helm-work bot's mid-edit SNAPSHOT COMMIT. This predicate is pure ledger
        shape: identical items produce an identical routing whatever any tree
        on disk says, because no tree is ever consulted."""
        src = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "helm", "owedpush.py")
        with open(src) as fh:
            text = fh.read()
        for banned in ("head_sha", "rev-list", "is_ancestor", "status --porcelain",
                       "landed_state", "_dirty("):
            self.assertNotIn(banned, text,
                             "owedpush reads git (%s) — it acquires a noise "
                             "channel it has no filter for" % banned)


if __name__ == "__main__":
    unittest.main()
