"""A review row is never booked to a seat the catalog marks outside the review
bench (task/3855).

MEASURED (task/3855): task/3742b's door read went to the opus46 seat, sat on
that seat's quota wall for 55 minutes and was cancelled. The store premise
opus46-is-a-proof-of-life-seat-not-bench-capacity already said never book a
review there; nothing enforced it. The owner: "that opus 46 seed is just a toy
for council experimentation".

The catalog now marks such a family with a `bench_role`
(`seat_catalog.BENCH_ROLES`), and the one writer every `dispatch send` and
`dispatch add` reaches (`dispatches._base3`) refuses a REVIEW row to its seats,
naming the role, the premise and the reviewer ladder. `--force` does not open
it: force answers "this seat is offline", not "this seat reviews".
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import dispatches, seat, seat_catalog, seats  # noqa: E402,F401 — the facade first (seat_compat)
from tests.test_dispatches import DispatchBase  # noqa: E402

PREMISE = "opus46-is-a-proof-of-life-seat-not-bench-capacity"


class ProofOfLifeSeatTakesNoReviewTest(DispatchBase):
    """The send and add doors, end to end, on a temp home and repo."""

    def send(self, recipient, kind="review"):
        # One lane per send: a second open row on one lane is its own refusal.
        self._lanes = getattr(self, "_lanes", 0) + 1
        with mock.patch.object(seats, "dm",
                               return_value=({"id": "post-1"}, None)):
            return dispatches.send(
                recipient, "bench-role-%d" % self._lanes,
                "please read the tip and record a verdict", self.a,
                repo=self.repo, sign=False, kind=kind, new_work=True,
                task=self.review_task["id"] if kind == "review" else None,
                force=True)

    def test_the_catalog_marks_opus46_proof_of_life(self):
        entry = seat_catalog.FAMILIES["opus46"]  # noqa: SEAT_NAME — the catalog FAMILY key is this arm's subject
        self.assertEqual(entry["bench_role"], "proof-of-life")
        self.assertEqual(seat_catalog.BENCH_ROLES["proof-of-life"][0],
                         PREMISE)
        self.assertEqual(set(seat_catalog.BENCH_ROLES),
                         {"proof-of-life", "council-only"})

    def test_a_review_send_to_a_proof_of_life_seat_is_refused_even_forced(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present by its words for each seat name, and the empty ledger is read off the same home the CONTROL arm below proves a row lands in
        for name in ("opus46", "opus46-2"):  # noqa: SEAT_NAME — a seat named after its catalog family, and a numbered instance of it, are the subject
            with self.subTest(seat=name):
                row, why, _sent = self.send(name)
                self.assertIsNone(row, why)
                self.assertIn("proof-of-life", why)
                self.assertIn(PREMISE, why)
                self.assertIn("--force does not open it", why)
                # THE LADDER, fresh-context Opus first
                self.assertIn("fresh-context", why)
                self.assertIn("helm reviewers", why)
                self.assertLess(why.index("fresh-context"),
                                why.index("Fable"), why)
        self.assertEqual(dispatches.open_rows(), [])

    def test_a_review_add_to_a_proof_of_life_seat_is_refused(self):
        row, why = dispatches.add("opus46", "bench-add", ref=self.a,  # noqa: SEAT_NAME — the catalog FAMILY's seat is the subject
                                  repo=self.repo, kind="review",
                                  new_work=True, notify=False, _reason=True,
                                  task=self.review_task["id"])
        self.assertIsNone(row, why)
        self.assertIn("proof-of-life", why)
        self.assertEqual(dispatches.open_rows(), [])

    def test_CONTROL_a_build_to_the_same_seat_and_a_review_to_its_group_sibling_are_admitted(self):
        """Only the marking differs: gptoss rides the same credential and the
        same quota group, and a small build is what a proof-of-life seat is
        for."""
        row, why, _sent = self.send("opus46", kind="build")  # noqa: SEAT_NAME — the catalog FAMILY's seat is the subject
        self.assertIsNotNone(row, why)
        self.assertEqual(row["kind"], "build")
        row, why, _sent = self.send("gptoss")  # noqa: SEAT_NAME — the unmarked sibling FAMILY is the control
        self.assertIsNotNone(row, why)
        self.assertEqual(row["kind"], "review")

    def test_a_COUNCIL_ONLY_family_is_refused_the_same_way(self):  # noqa: VACUOUS_ASSERTION — the refusal is asserted present by its role and premise words, and the CONTROL arm above proves a review row to this same unplanted family lands in the ledger this arm reads empty
        """The other role, planted on the control family: the refusal names
        the role and its own premise."""
        with mock.patch.dict(seat_catalog.FAMILIES["gptoss"],  # noqa: SEAT_NAME — the catalog FAMILY key is the plant's target
                             {"bench_role": "council-only"}):
            row, why, _sent = self.send("gptoss")  # noqa: SEAT_NAME — the planted FAMILY's seat is the subject
        self.assertIsNone(row, why)
        self.assertIn("council-only", why)
        self.assertIn(seat_catalog.BENCH_ROLES["council-only"][0], why)
        self.assertEqual(dispatches.open_rows(), [])


if __name__ == "__main__":
    unittest.main()
