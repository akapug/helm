"""The independence check: context, then model; family only ranks."""
import unittest

from helm import review_independence as ri


def side(seat, session, model, family="family-a"):
    return {"seat": seat, "session": session, "model": model,
            "family": family}


AUTHOR = side("seat-a", "sess-a", "vendor-a-small-1")


class TestIndependence(unittest.TestCase):

    def test_same_family_different_model_fresh_session_is_admitted(self):
        """The owner's case: a smarter same-family reader, fresh context."""
        state, why, facts = ri.independence(
            AUTHOR, side("seat-c", "sess-b", "vendor-a-large-1"))
        self.assertEqual(state, ri.OK)
        self.assertIsNone(why)
        self.assertEqual(facts["family"], ri.SAME_FAMILY)

    def test_other_family_is_still_admitted(self):
        """The control: the pairing the old rule admitted still admits."""
        state, why, _facts = ri.independence(
            AUTHOR, side("seat-e", "sess-b", "vendor-b-large-1", family="family-b"))
        self.assertEqual(state, ri.OK)
        self.assertIsNone(why)

    def test_same_resolved_model_refuses_naming_the_model(self):
        state, why, _facts = ri.independence(
            AUTHOR, side("seat-b", "sess-b", "vendor-a-small-1"))
        self.assertEqual(state, ri.SHARED)
        self.assertIn("model", why)
        self.assertIn("vendor-a-small-1", why)

    def test_same_session_refuses_naming_the_session(self):
        """A delegate under the author's own seat carries the author's turn."""
        state, why, _facts = ri.independence(
            AUTHOR, side("seat-c", "sess-a", "vendor-a-large-1"))
        self.assertEqual(state, ri.SHARED)
        self.assertIn("session", why)
        self.assertIn("sess-a", why)

    def test_same_seat_refuses_naming_the_seat(self):
        state, why, _facts = ri.independence(
            AUTHOR, side("seat-a", "sess-b", "vendor-a-large-1"))
        self.assertEqual(state, ri.SHARED)
        self.assertIn("seat", why)

    def test_unknown_reviewer_model_refuses_naming_the_side(self):
        state, why, _facts = ri.independence(
            AUTHOR, side("seat-c", "sess-b", None))
        self.assertEqual(state, ri.UNKNOWN)
        self.assertIn("model", why)
        self.assertIn("reviewer", why)
        self.assertNotIn("author", why)

    def test_unknown_author_model_refuses_naming_the_author(self):
        """The measured native case: the author's own model is not recorded."""
        state, why, _facts = ri.independence(
            side("seat-d", "sess-a", None),
            side("seat-c", "sess-b", "vendor-a-large-1"))
        self.assertEqual(state, ri.UNKNOWN)
        self.assertIn("author", why)

    def test_both_models_unknown_names_both_sides(self):
        state, why, _facts = ri.independence(
            side("seat-d", "sess-a", None), side("seat-c", "sess-b", None))
        self.assertEqual(state, ri.UNKNOWN)
        self.assertIn("author", why)
        self.assertIn("reviewer", why)

    def test_an_alias_is_not_a_resolved_model_the_caller_must_resolve(self):
        """Two turns both labelled "opus" are the SHARED case, not admitted.

        The module cannot tell an alias from a resolved id, and the point of
        the arm is that it does not try: equal strings refuse. A caller that
        passes aliases gets refusals, which is the safe direction.
        """
        state, _why, _facts = ri.independence(
            side("seat-a", "sess-a", "opus"),
            side("seat-c", "sess-b", "opus"))
        self.assertEqual(state, ri.SHARED)

    def test_blank_and_nonstring_models_read_as_unknown_not_as_equal(self):
        control, _why, _f = ri.independence(
            side("seat-a", "sess-a", "vendor-a-small-1"),
            side("seat-c", "sess-b", "vendor-a-large-1"))
        self.assertEqual(control, ri.OK)
        for empty in ("", "   ", None, 7, ["alias"]):
            state, _why, _facts = ri.independence(
                side("seat-a", "sess-a", empty),
                side("seat-c", "sess-b", empty))
            self.assertEqual(state, ri.UNKNOWN, empty)

    def test_seat_is_checked_before_session_before_model(self):
        """One pairing sharing all three names the SEAT, the outermost."""
        state, why, _facts = ri.independence(AUTHOR, dict(AUTHOR))
        self.assertEqual(state, ri.SHARED)
        self.assertIn("seat", why)
        self.assertNotIn("session", why)


class TestFamilyIsAPreference(unittest.TestCase):

    def test_family_never_appears_in_the_state(self):
        """Same family admits and other family admits, on the same inputs."""
        same = ri.independence(
            AUTHOR, side("seat-c", "sess-b", "vendor-a-large-1"))
        other = ri.independence(
            AUTHOR, side("seat-c", "sess-b", "vendor-a-large-1",
                         family="family-b"))
        self.assertEqual(same[0], other[0])
        self.assertEqual(same[0], ri.OK)
        self.assertNotEqual(same[2]["family"], other[2]["family"])

    def test_family_relation_reads_the_two_sides(self):
        self.assertEqual(ri.family_relation(AUTHOR, AUTHOR), ri.SAME_FAMILY)
        self.assertEqual(
            ri.family_relation(AUTHOR, side("c", "d", "e", family="family-b")),
            ri.OTHER_FAMILY)
        self.assertEqual(
            ri.family_relation(AUTHOR, side("c", "d", "e", family=None)),
            ri.UNKNOWN_FAMILY)

    def test_line_prints_the_family_beside_the_answer(self):
        out = ri.line(AUTHOR,
                      side("seat-c", "sess-b", "vendor-a-large-1"))
        self.assertIn("ADMITTED", out)
        self.assertIn("family: same", out)
        refused = ri.line(AUTHOR, dict(AUTHOR))
        self.assertIn("REFUSED", refused)


class TestRank(unittest.TestCase):

    def test_the_order_is_context_then_model_then_family_then_cost(self):
        fresh_other = ri.rank(
            AUTHOR, side("seat-e", "sess-b", "vendor-b-large-1", family="family-b"))
        fresh_same_family = ri.rank(
            AUTHOR, side("seat-c", "sess-b", "vendor-a-large-1"))
        fresh_same_model = ri.rank(
            AUTHOR, side("seat-b", "sess-b", "vendor-a-small-1"))
        shares_session = ri.rank(
            AUTHOR, side("seat-c", "sess-a", "vendor-a-large-1"))
        self.assertEqual(fresh_other[0], (0, 0, 0, 0))
        self.assertEqual(fresh_same_family[0], (0, 0, 2, 0))
        self.assertEqual(fresh_same_model[0], (0, 2, 2, 0))
        self.assertEqual(shares_session[0], (2, 2, 2, 0))

    def test_a_same_family_fresh_reader_outranks_a_same_model_one(self):
        """The demotion that replaces the refusal: ranked down, not walled."""
        same_family, _r = ri.rank(
            AUTHOR, side("seat-c", "sess-b", "vendor-a-large-1"))
        same_model, _r2 = ri.rank(
            AUTHOR, side("seat-b", "sess-b", "vendor-a-small-1"))
        self.assertEqual(same_family, (0, 0, 2, 0))
        self.assertEqual(same_model, (0, 2, 2, 0))
        self.assertLess(same_family, same_model)

    def test_cost_breaks_a_tie_and_never_outranks_the_model(self):
        cheap_same_model, _r = ri.rank(
            AUTHOR, side("seat-b", "sess-b", "vendor-a-small-1"),
            cost=0)
        dear_other_model, _r2 = ri.rank(
            AUTHOR, side("seat-c", "sess-b", "vendor-a-large-1"),
            cost=99)
        self.assertEqual(cheap_same_model, (0, 2, 2, 0))
        self.assertEqual(dear_other_model, (0, 0, 2, 99))
        self.assertLess(dear_other_model, cheap_same_model)
        dearer, _r3 = ri.rank(
            AUTHOR, side("seat-c", "sess-b", "vendor-a-large-1"),
            cost=100)
        self.assertLess(dear_other_model, dearer)

    def test_unproven_context_sorts_between_fresh_and_shared(self):  # noqa: VACUOUS_ASSERTION — the three key tuples ARE the unconditional positive control; the observable is a rank term, not an absence
        fresh, _r = ri.rank(
            AUTHOR, side("seat-c", "sess-b", "vendor-a-large-1"))
        unproven, _r2 = ri.rank(
            AUTHOR, side("seat-c", None, "vendor-a-large-1"))
        shared, _r3 = ri.rank(
            AUTHOR, side("seat-c", "sess-a", "vendor-a-large-1"))
        self.assertEqual((fresh[0], unproven[0], shared[0]), (0, 1, 2))
        self.assertLess(fresh[0], unproven[0])
        self.assertLess(unproven[0], shared[0])

    def test_a_refused_candidate_still_ranks_rather_than_raising(self):
        """`independence` returns early, so the facts it built are partial."""
        key, reason = ri.rank(AUTHOR, dict(AUTHOR))
        self.assertEqual(len(key), 4)
        self.assertIn("shares the author's seat or session", reason)

    def test_the_reason_names_all_three_axes(self):
        _key, reason = ri.rank(
            AUTHOR, side("seat-e", "sess-b", "vendor-b-large-1", family="family-b"))
        self.assertIn("fresh context", reason)
        self.assertIn("different model", reason)
        self.assertIn("other family", reason)

    def test_an_unreadable_cost_is_zero_and_does_not_raise(self):
        key, _reason = ri.rank(
            AUTHOR, side("seat-e", "sess-b", "vendor-b-large-1", family="family-b"),
            cost="expensive")
        self.assertEqual(key, (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
