"""`helm reviewers <row>` — the eligible seats, and for every other seat the
ONE conjunct that excludes it.

ONE ARM PER CONJUNCT, AND EVERY ARM CARRIES ITS OWN MUST-HIT. A test that only
asserts a seat is absent from the eligible list passes identically when the
fixture never reached the rung, when the join raised, and when the whole
candidate set came back empty — three ways of measuring nothing. So every
exclusion arm also asserts, unconditionally and in the same method and on the
SAME observable, that a sibling seat which passes that conjunct IS eligible.
The control is what proves the fixture produced a real answer.

Fixtures use the house convention — seat-a, seat-b — never a live seat
identity: tests/ is public-bound, and a neutral `family-x` stands in wherever
a family token is merely a token. The one real family word that appears is
the one the BUDGET rung keys on by name; a stand-in there would test a rung
that never fires.

EVERY READER IS INJECTED. `eligibility(..., seams=...)` takes one seam per
authority — the row, the roster, the usability join, the chain index, the
budget snapshot, the tier policy, the family evidence and the pane tail — so
no arm needs a live fleet, a proxy, a process census or a real ledger. That is
also what makes the UNREADABLE arms possible at all: an input that cannot be
made to fail cannot be proven to render UNKNOWN.
"""
import json
import unittest
import unittest.mock

from helm import dispatches, reviewer_eligibility as re_, seats


ROW = {"id": "row-1", "lane": "a-lane", "kind": "review", "state": "READY",
       "author": "seat-author", "reviewer": "seat-a",
       "pinned_tip": "0123456789abcdef", "chain_root": "root-1",
       "repo_id": "repo-1"}


def _get(row=None, err=None):
    return lambda rid: (row if err is None else None, err)


def _register(*names, **rows):
    reg = {n: {"home_room": "proj"} for n in names}
    reg.update(rows)
    return reg


_USABLE = {"family": "family-x", "can_take_work": True, "holding": 0,
           "verdict": "USABLE", "reason": "", "pane": True,
           "reachable": True, "upstream_dark": False, "upstream": "HEALTHY",
           "turn_state": "ok", "unknown": {}}


def _join(**overrides):
    def fn(seats=None, **_kw):
        out = {}
        for s in (seats or ()):
            row = dict(_USABLE, seat=s)
            row.update(overrides.get(s) or {})
            out[s] = row
        return out
    return fn


def _liveness(**states):
    def fn(seat, repair=False):
        return {"seat": seat, "state": states.get(seat, "IDLE"),
                "evidence": "pane-tail", "detail": "fixtured"}
    return fn


def _tier(**answers):
    return lambda seat: answers.get(seat, ("ok", None))


def _families(**answers):
    return lambda seat: answers.get(seat, ({"family-x"}, None))


def _seams(**over):
    base = {"get": _get(ROW), "project_for_cwd": lambda _cwd: "proj",
            "register": lambda: _register("seat-a", "seat-b"),
            "join": _join(), "chain_contributors": lambda lr: (set(), (), None),
            "cached_flags": lambda: ({}, None),
            "approval_tier": _tier(), "identity_families": _families(),
            "liveness": _liveness()}
    base.update(over)
    return base


def _flags(**per_family):
    """family -> burn flag, folded through burnflags' OWN derivation from
    pooled rows, so these arms assert the record the fleet actually writes
    rather than a dict shaped to match the assertion."""
    from helm import burnflags as bf
    out = {}
    for family, rows in per_family.items():
        axes = {"money": bf.derive_money(family, rows, ceiling=90.0,
                                         measured_at=1789000000.0)}
        out[family] = bf.compose(family, axes, now=1789000060.0)
    return out


def _by_seat(report, seat):
    for row in report["seats"]:
        if row["seat"] == seat:
            return row
    return None


class TierConjunct(unittest.TestCase):
    def test_outside_the_tier_excludes_and_names_the_valid_set(self):
        """A seat whose APPROVE cannot CLOSE the row is named under `tier`,
        carrying the policy's own sentence — which is where the valid set is.
        Paraphrasing it would be a second opinion wearing the first's
        authority."""
        report, err = re_.eligibility("row-1", seams=_seams(
            approval_tier=_tier(**{"seat-b": (
                "outside", "@seat-b is outside the current approval tier; "
                "reason: earned by review evidence; valid set: family:claude")})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertNotIn("seat-b", report["eligible"])
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["state"], re_.EXCLUDED)
        self.assertEqual(row["conjunct"], re_.TIER)
        self.assertIn("valid set: family:claude", row["reason"])

    def test_no_declared_tier_admits_rather_than_refusing(self):
        """Where no tier is declared there is nothing to be outside of. The
        land gate already reads it that way; a second reading that refused
        would brick every project that never declared one."""
        report, err = re_.eligibility("row-1", seams=_seams(
            approval_tier=_tier(**{"seat-b": ("none", "no policy declared")})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertIn("seat-b", report["eligible"])
        self.assertEqual(_by_seat(report, "seat-b")["unknown"], [])


class MintConjunct(unittest.TestCase):
    def test_contradictory_family_proof_excludes_and_carries_its_kind(self):
        """DAMAGED is a MEASURED contradiction — two stored proofs disagree and
        neither yields on a re-read — so it refuses. The kind travels because
        the kind is the difference between "re-ask" and "this never clears"."""
        damaged = dispatches.TierUnknown(
            dispatches.TIER_DAMAGED,
            "conflicting explicit families family-x, family-y")
        report, err = re_.eligibility("row-1", seams=_seams(
            identity_families=_families(**{"seat-b": (None, damaged)})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertNotIn("seat-b", report["eligible"])
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["conjunct"], re_.MINT)
        self.assertIn(dispatches.TIER_DAMAGED, row["reason"])

    def test_unmeasured_family_evidence_is_unknown_and_never_a_refusal(self):
        """A seat outside the minted-proxy census has no family evidence and
        helm cannot say why. That is not a contradiction, so the seat stays a
        candidate with the unread input named — the whole point of the rung."""
        unclassified = dispatches.TierUnknown(
            dispatches.TIER_UNCLASSIFIED, "no verified native runtime")
        report, err = re_.eligibility("row-1", seams=_seams(
            identity_families=_families(**{"seat-b": (None, unclassified)})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertIn("seat-b", report["eligible"])
        self.assertNotIn("seat-b", report["measured_eligible"])
        conjuncts = [c for c, _why in _by_seat(report, "seat-b")["unknown"]]
        self.assertIn(re_.MINT, conjuncts)


class ChainConjunct(unittest.TestCase):
    def test_a_recorded_chain_author_is_excluded_by_name(self):
        """A contributor's APPROVE is not the independent read the row needs,
        and until this verb that fact surfaced only in a refusal AFTER a
        reviewer turn had already been spent."""
        report, err = re_.eligibility("row-1", seams=_seams(
            chain_contributors=lambda lr: ({"seat-b"}, (), None)))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertNotIn("seat-b", report["eligible"])
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["conjunct"], re_.CHAIN)
        self.assertIn("seat-b", row["reason"])

    def test_an_unreadable_chain_never_becomes_an_empty_author_set(self):
        """An empty set says "nobody wrote this chain", which would make every
        stranger independent exactly when helm knows least. Unreadable is
        UNKNOWN on EVERY candidate, and it refuses none of them."""
        report, err = re_.eligibility("row-1", seams=_seams(
            chain_contributors=lambda lr: (set(), (), "chain identity is UNKNOWN")))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertIn("seat-b", report["eligible"])
        self.assertEqual(report["measured_eligible"], [])
        self.assertIn("chain", report["unreadable"])
        for name in ("seat-a", "seat-b"):
            conjuncts = [c for c, _why in _by_seat(report, name)["unknown"]]
            self.assertIn(re_.CHAIN, conjuncts)


class AwakeConjunct(unittest.TestCase):
    def test_a_deaf_seat_is_excluded_and_labelled_DEAF(self):
        """DEAF is the state no other axis can see: pane live, turn ok, vendor
        healthy, and no live beacon, so helm cannot wake it. It takes the
        obligation and never works it."""
        deaf = {"can_take_work": False, "reachable": False,
                "reason": "no live beacon: helm cannot wake it "
                          "(`helm chat wait --seat seat-b --follow` re-arms it)"}
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": deaf})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertNotIn("seat-b", report["eligible"])
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["conjunct"], re_.AWAKE)
        self.assertTrue(row["reason"].startswith(re_.DEAF))
        self.assertIn("no live beacon", row["reason"])

    def test_a_proxy_cooldown_is_labelled_WALL_and_kept_apart_from_DEAF(self):
        """PROXY-COOLDOWN is HELM'S OWN proxy refusing before a request leaves
        the box; it says nothing about the vendor and it is repairable. Showing
        it under the same word as DEAF would send the reader at the wrong
        repair."""
        walled = {"can_take_work": False, "upstream_dark": True,
                  "upstream": "PROXY-COOLDOWN",
                  "reason": "PROXY-COOLDOWN since then — HELM'S OWN PROXY "
                            "refused before any request left the box"}
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": walled})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertNotIn("seat-b", report["eligible"])
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["conjunct"], re_.AWAKE)
        self.assertIn("WALL/PROXY-COOLDOWN", row["reason"])
        self.assertNotIn(re_.DEAF, row["reason"])

    def test_can_take_work_None_is_unknown_and_admits(self):
        """UNKNOWN is helm saying it could not tell. Refusing on it would brick
        every box where proxywatch has never run — the same law the dispatch
        write door states one layer down."""
        blind = {"can_take_work": None,
                 "reason": "pane UNREADABLE (the census could not look)"}
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": blind})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertIn("seat-b", report["eligible"])
        self.assertNotIn("seat-b", report["measured_eligible"])
        conjuncts = [c for c, _why in _by_seat(report, "seat-b")["unknown"]]
        self.assertIn(re_.AWAKE, conjuncts)


class BudgetConjunct(unittest.TestCase):
    def test_a_fully_capped_pool_excludes_only_its_own_family(self):
        """`capped` requires that every pooled account was READ and every one
        is at or past the ceiling. It is scoped to the family that has a pool:
        a seat of another family is not walled by it."""
        rows = [{"account_id": "acct-1", "longest_pct": 95.0,
                 "source": "measured", "state": "ok", "windows": [{
                     "label": "7d", "seconds": 604800,
                     "used_percent": 95.0, "reset_at": 1789600000.0,
                     "unit": "requests", "source": "measured"}]}]
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": {"family": "codex"}}),
            identity_families=_families(**{"seat-b": ({"codex"}, None)}),
            cached_flags=lambda: (_flags(codex=rows), 0)))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertNotIn("seat-b", report["eligible"])
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["conjunct"], re_.BUDGET)
        self.assertIn("ceiling", row["reason"])

    def test_a_partly_over_pool_admits_because_an_account_can_still_pay(self):
        """A seat over the ceiling on SOME account is a COST decision, not a
        wall, and inventing a refusal from a partial reading is the
        guard-fires-on-absence class.

        THE READING CHANGED AND SO DID THIS ARM, deliberately. A pooled
        verdict says `mixed` whenever ANY account is over and prints that
        account; the flag asks whether the family can still pay, which is a
        question about the account with the MOST headroom — so a pool with one spent account and one at 12 percent is
        not "nearly spent", it is fine, and the cost note now fires where it
        means something: at ORANGE, the arm below.
        """
        rows = [{"account_id": "acct-1", "longest_pct": 100.0,
                 "state": "ok", "source": "measured", "windows": [{
                     "label": "7d", "seconds": 604800,
                     "used_percent": 100.0, "reset_at": 1789600000.0,
                     "unit": "requests", "source": "measured"}]},
                {"account_id": "acct-2", "longest_pct": 12.0,
                 "state": "ok", "source": "measured", "windows": [{
                     "label": "7d", "seconds": 604800,
                     "used_percent": 12.0, "reset_at": 1789600000.0,
                     "unit": "requests", "source": "measured"}]}]
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": {"family": "codex"}}),
            identity_families=_families(**{"seat-b": ({"codex"}, None)}),
            cached_flags=lambda: (_flags(codex=rows), 0)))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertIn("seat-b", report["eligible"])
        self.assertEqual(_by_seat(report, "seat-b")["unknown"], [])

    def test_a_nearly_spent_family_admits_AND_prints_its_cost(self):
        """The property the arm above carries no longer, at the colour where
        it means something: every readable account past the warning band but none at
        the ceiling. It PASSES, and the reader is told what they are
        spending before they spend it."""
        rows = [{"account_id": "acct-1", "longest_pct": 85.0,
                 "state": "ok", "source": "measured", "windows": [{
                     "label": "7d", "seconds": 604800,
                     "used_percent": 85.0, "reset_at": 1789600000.0,
                     "unit": "requests", "source": "measured"}]},
                {"account_id": "acct-2", "longest_pct": 88.0,
                 "state": "ok", "source": "measured", "windows": [{
                     "label": "7d", "seconds": 604800,
                     "used_percent": 88.0, "reset_at": 1789600000.0,
                     "unit": "requests", "source": "measured"}]}]
        flags = _flags(codex=rows)
        self.assertEqual(flags["codex"]["colour"], "ORANGE")  # the control
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": {"family": "codex"}}),
            identity_families=_families(**{"seat-b": ({"codex"}, None)}),
            cached_flags=lambda: (flags, 0)))
        self.assertIsNone(err)
        self.assertIn("seat-b", report["eligible"])
        notes = dict(_by_seat(report, "seat-b")["notes"])
        self.assertIn(re_.BUDGET, notes)
        self.assertIn("ORANGE", notes[re_.BUDGET])

    def test_an_unmeasured_family_is_unknown_and_never_refused(self):
        """The defect the widening exists to close, from the other side: the
        rung now speaks about every family, so it owes an honest silence for
        the ones no reader covers."""
        flags = _flags(codex=None)
        self.assertEqual(flags["codex"]["colour"], "GREY")    # the control
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": {"family": "codex"}}),
            identity_families=_families(**{"seat-b": ({"codex"}, None)}),
            cached_flags=lambda: (flags, 0)))
        self.assertIsNone(err)
        self.assertIn("seat-b", report["eligible"])
        conjuncts = [c for c, _why in _by_seat(report, "seat-b")["unknown"]]
        self.assertIn(re_.BUDGET, conjuncts)

    def test_an_absent_budget_snapshot_is_unknown_for_that_family_only(self):
        """No snapshot is not a clear pool. It is UNKNOWN for the seats it
        would have covered, and it must not touch a family it never described.
        """
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": {"family": "codex"}}),
            identity_families=_families(**{"seat-b": ({"codex"}, None)}),
            cached_flags=lambda: ({}, None)))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertIn("seat-b", report["eligible"])
        self.assertEqual(_by_seat(report, "seat-a")["unknown"], [])
        conjuncts = [c for c, _why in _by_seat(report, "seat-b")["unknown"]]
        self.assertIn(re_.BUDGET, conjuncts)


class PaneAndIdleConjuncts(unittest.TestCase):
    def test_a_mid_turn_seat_is_excluded_as_BUSY_not_as_broken(self):
        """The reader has to tell WAIT from REPAIR. A busy seat needs nothing
        done to it, and the reason says so in as many words."""
        report, err = re_.eligibility("row-1", seams=_seams(
            liveness=_liveness(**{"seat-b": "RUNNING"})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertNotIn("seat-b", report["eligible"])
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["conjunct"], re_.IDLE)
        self.assertIn("mid-turn", row["reason"])
        self.assertIn("frees itself", row["reason"])

    def test_a_gone_pane_is_excluded_under_its_own_conjunct(self):
        """GONE is a MEASURED absence with a different repair from BUSY, so it
        gets its own word. It is also the fact the fleet-wide census gives up
        on when two pieces of evidence contradict each other, recovered here at
        one seat's cost."""
        report, err = re_.eligibility("row-1", seams=_seams(
            liveness=_liveness(**{"seat-b": "GONE"})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertNotIn("seat-b", report["eligible"])
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["conjunct"], re_.PANE)
        self.assertIn("GONE", row["reason"])

    def test_an_unrecognised_pane_state_is_unknown_and_admits(self):
        """An unrecognised tail is a failed READ, never a claim about the
        seat. The allowlist is what keeps a new state from reading as idle and
        being recommended."""
        report, err = re_.eligibility("row-1", seams=_seams(
            liveness=_liveness(**{"seat-b": "WALLED"})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertIn("seat-b", report["eligible"])
        conjuncts = [c for c, _why in _by_seat(report, "seat-b")["unknown"]]
        self.assertIn(re_.PANE, conjuncts)


class UnreadableInputsNeverReadAsIneligible(unittest.TestCase):
    """THE ARM THAT MATTERS MOST. "No eligible reviewer" and "I could not read
    the roster" are different worlds with different repairs, and an incident
    where the second read as the first is why this verb exists."""

    def test_an_unreadable_roster_is_not_an_empty_fleet(self):
        def blows_up():
            raise OSError("permission denied")
        report, err = re_.eligibility("row-1", seams=_seams(register=blows_up))
        self.assertIsNone(err)
        self.assertIn("roster", report["unreadable"])
        self.assertIn("permission denied", report["unreadable"]["roster"])
        self.assertEqual(report["eligible"], [])
        text = "\n".join(re_.render(report))
        # THE POSITIVE CONTROL IS ON THE SAME OBSERVABLE: the rendered text
        # must CARRY the unreadable sentence, not merely omit the confident
        # one. An assertion that only checks a phrase is absent passes on an
        # empty string.
        self.assertIn("UNREADABLE", text)
        self.assertIn("This is NOT a measured", text)

    def test_an_unreadable_tier_policy_leaves_every_seat_a_candidate(self):
        """A conjunct that could not be measured must not exclude, and it must
        name the input it could not read — never the check that rejected it."""
        def raises(_seat):
            raise RuntimeError("the typed store did not open")
        report, err = re_.eligibility("row-1", seams=_seams(approval_tier=raises))
        self.assertIsNone(err)
        # POSITIVE CONTROL AND THE CLAIM ON ONE OBSERVABLE: both seats are
        # still eligible (nothing refused them) AND neither is fully measured.
        self.assertEqual(report["eligible"], ["seat-a", "seat-b"])
        self.assertEqual(report["measured_eligible"], [])
        for name in ("seat-a", "seat-b"):
            why = dict(_by_seat(report, name)["unknown"])[re_.TIER]
            self.assertIn("the typed store did not open", why)

    def test_an_unknown_seat_prints_in_its_own_block_not_beside_measured_ones(self):
        """A measured seat and a seat admitted only because something would not
        read are both eligible and are NOT the same answer. One list holding
        both would put an unmeasured seat at the top of a trusted list."""
        blind = {"can_take_work": None, "reason": "the census could not look"}
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": blind})))
        self.assertIsNone(err)
        self.assertEqual(report["measured_eligible"], ["seat-a"])
        text = "\n".join(re_.render(report))
        self.assertIn("ELIGIBLE (1), most idle first — every conjunct MEASURED",
                      text)
        self.assertIn("ELIGIBLE BUT NOT FULLY MEASURED (1)", text)

    def test_a_measured_empty_set_says_measured(self):
        """The other half of the pair, and it has to be provable too: when
        every candidate is genuinely refused, the surface must NOT hedge.

        THE CONTROLS COME FIRST AND THEY ARE THE POSITIVE FORM OF THE SAME
        OBSERVABLE: both candidates were REACHED and both were EXCLUDED on a
        named conjunct. An empty eligible list asserted alone is identical
        whether the rung refused two seats or the roster produced none, which
        is the failure this whole verb exists to end — so the emptiness is
        only ever asserted beside proof that the set it came from was real."""
        report, err = re_.eligibility("row-1", seams=_seams(
            approval_tier=_tier(**{
                "seat-a": ("outside", "@seat-a is outside the tier"),
                "seat-b": ("outside", "@seat-b is outside the tier")})))
        self.assertIsNone(err)
        self.assertEqual([(r["seat"], r["state"], r["conjunct"])
                          for r in report["seats"]],
                         [("seat-a", re_.EXCLUDED, re_.TIER),
                          ("seat-b", re_.EXCLUDED, re_.TIER)])
        self.assertEqual(report["eligible"], [])
        self.assertEqual(report["unreadable"], {})
        text = "\n".join(re_.render(report))
        self.assertIn("EXCLUDED (2)", text)
        self.assertIn("NO FULLY-MEASURED ELIGIBLE SEAT", text)
        self.assertNotIn("This is NOT a measured", text)


class LadderOrderAndScope(unittest.TestCase):
    def test_a_seat_failing_two_rungs_is_named_under_the_furthest_out_one(self):
        """The conjunct a seat is named under is the one a reader has to act
        on FIRST. Calling an out-of-tier seat BUSY would send someone to wait
        for an answer that could never arrive."""
        report, err = re_.eligibility("row-1", seams=_seams(
            approval_tier=_tier(**{"seat-b": ("outside", "@seat-b is outside")}),
            liveness=_liveness(**{"seat-b": "RUNNING"})))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertEqual(_by_seat(report, "seat-b")["conjunct"], re_.TIER)

    def test_a_seat_homed_elsewhere_is_counted_out_loud_never_dropped(self):
        """A silently narrowed candidate set is how a verb comes to print a
        confident "nobody". The count prints and --all-seats widens."""
        reg = _register("seat-a")
        reg["seat-far"] = {"home_room": "another"}
        report, err = re_.eligibility("row-1", seams=_seams(
            register=lambda: reg))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        self.assertIn("seat-far", report["out_of_scope"])
        self.assertIn("another", report["out_of_scope"]["seat-far"])
        wide, err = re_.eligibility("row-1", all_seats=True,
                                    seams=_seams(register=lambda: reg))
        self.assertIsNone(err)
        self.assertIn("seat-far", wide["eligible"])

    def test_the_mint_rungs_family_reaches_the_budget_rung(self):
        """proxywatch watches minted PROXY seats, so a native seat's family is
        None on the usability row while the verdict-time evidence resolver
        answers it exactly. The budget rung needs that family to know whether
        it applies at all; asking a second producer is how two rungs come to
        decide about different seats under one name."""
        rows = [{"account_id": "acct-1", "longest_pct": 100.0,
                 "source": "measured", "state": "ok", "windows": [{
                     "label": "7d", "seconds": 604800,
                     "used_percent": 100.0, "reset_at": 1789600000.0,
                     "unit": "requests", "source": "measured"}]}]
        report, err = re_.eligibility("row-1", seams=_seams(
            join=_join(**{"seat-b": {"family": None}}),
            identity_families=_families(**{"seat-b": ({"codex"}, None)}),
            cached_flags=lambda: (_flags(codex=rows), 0)))
        self.assertIsNone(err)
        self.assertIn("seat-a", report["eligible"])        # must-hit control
        row = _by_seat(report, "seat-b")
        self.assertEqual(row["family"], "codex")
        self.assertEqual(row["conjunct"], re_.BUDGET)


class TheVerb(unittest.TestCase):
    def test_a_bad_row_id_refuses_in_the_ledgers_own_words(self):
        report, err = re_.eligibility(
            "nope", seams=_seams(get=_get(err="no land request 'nope'")))
        self.assertIsNone(report)
        self.assertEqual(err, "no land request 'nope'")

    def test_the_headline_names_the_row_its_author_and_its_reviewer(self):
        """A router reading this line must not have to go and fetch the row it
        just asked about."""
        report, err = re_.eligibility("row-1", seams=_seams())
        self.assertIsNone(err)
        head = re_.render(report)[0]
        self.assertIn("row-1", head)
        self.assertIn("@seat-author", head)
        self.assertIn("@seat-a", head)
        self.assertIn("0123456789ab", head)


class TheRosterReaderIsTheTriStateOne(unittest.TestCase):
    """`seats.roster()` IS FAIL-OPEN AND THIS VERB CANNOT USE IT.

    It is a `read_json(path, {}) or {}`: a register that is unreadable,
    malformed or not a mapping comes back as an empty dict and raises nothing,
    so an `except` around it catches nothing and the candidate set is silently
    empty. This verb would then print "no candidate seats" and "nobody can
    review this row" for a register it simply could not parse — the inversion
    the module exists to end. `roster_checked` is the producer that separates
    the two, and these arms bind this reader to it: swapping back to `roster`
    makes the first one red, because patching `roster_checked` would no longer
    reach the code under test.
    """

    def test_a_failed_register_probe_is_UNREADABLE_not_an_empty_fleet(self):
        with unittest.mock.patch.object(seats, "roster_checked",
                                        lambda: ({}, True)):
            reg, why = re_._read_roster()
        self.assertIsNone(reg)
        self.assertIn("unreadable", why)
        self.assertIn("ABSENT register is a proven empty fleet", why)
        # THE POSITIVE CONTROL IS IN THIS METHOD AND ON THE SAME OBSERVABLE:
        # the same reader, reading the same producer, returns the rows and NO
        # why-not when the probe did not fail. Without it, a reader that
        # returned (None, "...") unconditionally would pass the arm above.
        with unittest.mock.patch.object(seats, "roster_checked",
                                        lambda: ({"seat-a": {}}, False)):
            reg, why = re_._read_roster()
        self.assertEqual(reg, {"seat-a": {}})
        self.assertIsNone(why)

    def test_a_failed_probe_reaches_the_report_as_an_unreadable_input(self):
        """The reader's tri-state is only worth something if the REPORT keeps
        it. This drives the whole verb with no `register` seam, so the real
        reader runs."""
        seams = _seams()
        seams.pop("register")
        with unittest.mock.patch.object(seats, "roster_checked",
                                        lambda: ({}, True)):
            report, err = re_.eligibility("row-1", seams=seams)
        self.assertIsNone(err)
        self.assertIn("roster", report["unreadable"])
        text = "\n".join(re_.render(report))
        self.assertIn("UNREADABLE", text)
        self.assertIn("This is NOT a measured", text)


class HostileRosterKeyNeverReshapesTheTerminal(unittest.TestCase):
    """THE CANDIDATE SET IS THE ROSTER'S KEYS, and a roster key is unvalidated
    at the join seam — a `HELM_CHAT_NAME` may carry a screen-clearing CSI or a
    right-to-left override. Every key reaches a sink here: the seat column, the
    EXCLUDED lines, a pasteable repair command and the whole `--json` body.

    This is the arm that stands in for the cross-module runtime sweep in
    tests/test_display_launder_tripwire.py, which drives named verbs against a
    table and cannot reach a verb that needs a land-request row plus six
    injected authorities. The allowlist entry for this module names it.
    """

    ESC, BIDI = "\x1b", "‮"
    HOSTILE = "lane\x1b[2J‮pwn"
    LAUNDERED = "lane[2Jpwn"

    def _liveness(self, gone):
        def fn(seat, repair=False):
            return {"seat": seat, "state": "GONE" if seat == gone else "IDLE",
                    "evidence": "pane-tail", "detail": "fixtured"}
        return fn

    def test_the_hostile_key_is_laundered_in_the_render_and_the_json(self):
        reg = _register("seat-a")
        reg[self.HOSTILE] = {"home_room": "proj"}
        report, err = re_.eligibility("row-1", seams=_seams(
            register=lambda: reg, liveness=self._liveness(self.HOSTILE)))
        self.assertIsNone(err)
        text = "\n".join(re_.render(report))
        body = json.dumps(report, ensure_ascii=False, default=str)

        # THE RAW KEY STILL DRIVES THE MATCH, and this proves it: the pane
        # seam answered GONE only for a seat compared by its RAW spelling, so
        # this row exists only because the unlaundered name reached the reader.
        row = _by_seat(report, self.LAUNDERED)
        self.assertIsNotNone(row, "the hostile seat never became a candidate")
        self.assertEqual(row["conjunct"], re_.PANE)

        # THE POSITIVE CONTROLS, unconditional and on the SAME observables:
        # the hostile row IS rendered (so the absence below is LAUNDERING and
        # not omission), its pasteable repair command IS emitted (the most
        # dangerous sink was exercised), and a legitimate seat beside it is
        # BYTE-IDENTICAL (so laundering costs nothing).
        self.assertIn(self.LAUNDERED, text)
        self.assertIn(self.LAUNDERED, body)
        self.assertIn("helm seat resume " + self.LAUNDERED, text)
        self.assertIn("seat-a", text)
        self.assertIn("seat-a", report["eligible"])

        # ASSERTED ON THE NAMED OBSERVABLES, NOT THROUGH A LOOP VARIABLE: the
        # two positive controls above are on `text` and `body` by name, and an
        # absence assertion is only worth the control that sits beside it on
        # the SAME name.
        self.assertNotIn(self.ESC, text,
                         "a screen-clearing CSI reached the render")
        self.assertNotIn(self.BIDI, text,
                         "a bidi override reached the render")
        self.assertNotIn(self.ESC, body,
                         "a screen-clearing CSI reached the json body")
        self.assertNotIn(self.BIDI, body,
                         "a bidi override reached the json body")

    def test_an_out_of_scope_key_is_laundered_too(self):
        """`out_of_scope` is keyed BY SEAT NAME and rides the --json body. It
        is the one roster key that never passes through a candidate row, so it
        is the one a per-field launder would forget."""
        reg = _register("seat-a")
        reg[self.HOSTILE] = {"home_room": "another"}
        report, err = re_.eligibility("row-1", seams=_seams(
            register=lambda: reg))
        self.assertIsNone(err)
        # POSITIVE CONTROL FIRST: the hostile seat IS counted out loud rather
        # than dropped, and a legitimate seat is still eligible — so the
        # absence of the payload below is laundering, not a missing row.
        self.assertIn(self.LAUNDERED, report["out_of_scope"])
        self.assertIn("seat-a", report["eligible"])
        body = json.dumps(report, ensure_ascii=False, default=str)
        # AND ON THE SAME OBSERVABLE THE ABSENCES ARE ASSERTED AGAINST: the
        # json body CARRIES the hostile seat's laundered key, so an empty or
        # truncated body cannot pass the two assertions below.
        self.assertIn(self.LAUNDERED, body)
        self.assertNotIn(self.ESC, body)
        self.assertNotIn(self.BIDI, body)


class AntigravityGroupSeatsAreFindingsOnlyTest(unittest.TestCase):
    """The two antigravity-group seats can READ but cannot CLOSE, and the
    evaluator says so through the shipped tier rather than through a second
    rule somebody remembers.

    THE POLICY IS DERIVED FROM THE SHIPPED TIER, NEVER TRANSCRIBED: its
    members are `route.APPROVAL_TIER` rendered as the `family:` selectors the
    real recorded policy carries, so an edit that admitted either family would
    turn the exclusions below green instead of leaving a stale literal behind.
    """

    def _policy(self):
        from helm import route
        return {"id": "tier-under-test", "class": "certain",
                "_policy_confidence_valid": True, "_policy_source_valid": True,
                "policy_kind": "approval-tier",
                "policy_reason": "independent final approval",
                "policy_members": ["family:%s" % f for f in route.APPROVAL_TIER]}

    def _state(self, family):
        from helm import verdict_tier
        return verdict_tier.evaluate(self._policy(), "seat-a", {family})

    def test_an_approve_from_either_family_is_recorded_outside_the_tier(self):
        """AND THE CONTROL IS A TIER FAMILY ON THE SAME EVALUATOR AND THE SAME
        POLICY, because "outside" is also what a malformed policy, an
        unresolvable recipient and an empty member list all produce."""
        from helm import route
        control = self._state(route.APPROVAL_TIER[0])
        self.assertEqual(control, ("ok", None),
                         "the control: a tier family's approve IS authorizing "
                         "under this same policy, so the refusals below are "
                         "about the families and not about the fixture")
        for family in ("opus46", "gptoss"):  # noqa: SEAT_NAME — catalog FAMILY keys, and which families are outside the tier IS this arm's subject
            state, why = self._state(family)
            self.assertEqual(state, "outside", family)
            self.assertIn("outside the recorded approval tier", why or "")

    def test_the_exclusion_says_the_findings_are_still_input(self):
        """A READ FROM THESE SEATS IS ADMITTED and the rendered reason is
        where a router learns that. "Outside the tier" is not "not a
        reviewer": a reader who needs a READ rather than a CLOSE must not be
        told to go away, and the rung's own sentence is the surface that
        carries the distinction."""
        from helm import route
        verdict, _why = re_._rung_tier(
            "seat-a", approval_tier=lambda _seat: self._state(route.APPROVAL_TIER[0]))
        self.assertEqual(verdict, "pass",
                         "the control: a tier family clears this rung, so the "
                         "exclusion below is the rung deciding and not the "
                         "rung failing")
        for family in ("opus46", "gptoss"):  # noqa: SEAT_NAME — catalog FAMILY keys; the rung's answer ABOUT them is the subject
            state = self._state(family)
            verdict, why = re_._rung_tier("seat-a",
                                          approval_tier=lambda _seat, s=state: s)
            self.assertEqual(verdict, "exclude", family)
            self.assertIn("cannot CLOSE this row", why)
            self.assertIn("findings", why)


if __name__ == "__main__":
    unittest.main()
