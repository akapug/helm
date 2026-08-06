#!/usr/bin/env python3
"""helmese seed register — the shipped L2 vocabulary (#216 step 1).

WHAT THESE PIN, and why each is worth a test rather than a code comment:

The register is CLOSED BY RULING. Council amendment 5 named the exact operator
and role sets and named what stays OUT. A closed set that nothing enforces
reopens the first time a symbol looks useful, so the conformance test asserts
membership in BOTH directions — every required entry present AND every excluded
one absent. A one-directional test would let the register grow silently.

The register is VERSIONED AND DIGESTED (amendment 3) because a decoder meeting an
unknown version must REFUSE rather than guess with a newer table. The digest
exists for the failure a version cannot see: an edit that forgot to bump.

Nothing here touches the store. The seed is what the GROWN instance looked like
on day zero, and the two must never write to each other.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import helmese  # noqa: E402


class SeedConformanceTest(unittest.TestCase):
    """Amendment 5, asserted in both directions."""

    REQUIRED_OPERATORS = ("→", "⊥", "⊳", "¬", "|", "·", ">", "μ", "∴")
    REQUIRED_ROLES = ("-lead", "-toward", "-from", "-with", "-against", "-as")
    EXCLUDED = ("Ω", "↻", "Θ", "Σ", "λ", "γ", "κ")

    def test_every_ruled_operator_is_seeded(self):
        seeded = {r["symbol"] for r in helmese.OPERATORS}
        self.assertTrue(seeded, "the operator table is empty")
        for sym in self.REQUIRED_OPERATORS:
            self.assertIn(sym, seeded, "council-ruled operator missing: %s" % sym)

    def test_no_excluded_symbol_crept_in(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertGreaterEqual on len(seeded) runs before the exclusion loop, so an empty table cannot pass by having nothing to exclude.
        """The other direction. A closed register that only checks presence
        grows silently, and every addition then arrives with a plausible
        argument attached."""
        seeded = {r["symbol"] for r in helmese.OPERATORS}
        # MUST-HIT CONTROL: the set is real and non-trivial, so an empty table
        # cannot pass this by having nothing to exclude.
        self.assertGreaterEqual(len(seeded), len(self.REQUIRED_OPERATORS))
        for sym in self.EXCLUDED:
            self.assertNotIn(sym, seeded, "excluded symbol seeded: %s" % sym)

    def test_the_transformation_arrow_is_distinct_from_sequence(self):
        """↦ was admitted ONLY after the sequence/transform conflation was
        named. Seeding it as an alias of → would re-create the confusion that
        earned it a separate slot."""
        by = {r["symbol"]: r for r in helmese.OPERATORS}
        self.assertIn("↦", by)
        self.assertNotEqual(by["↦"]["gloss"], by["→"]["gloss"])
        self.assertIn("transformation", by["↦"]["gloss"].lower())

    def test_every_ruled_role_is_seeded(self):  # noqa: VACUOUS_ASSERTION — this is set EQUALITY against the ruled set, not an absence assertion: an empty or drifted table fails it.
        seeded = {r["suffix"] for r in helmese.ROLES}
        self.assertEqual(seeded, set(self.REQUIRED_ROLES),
                         "the role set drifted from the council ruling")

    def test_every_entry_carries_its_provenance(self):
        """Amendment 4 applies to the register FIRST: a vocabulary that cannot
        say why a symbol exists cannot ask its users to attribute anything."""
        self.assertTrue(helmese.OPERATORS and helmese.ROLES and helmese.SAFETY,
                        "a table is empty, so the loops below assert nothing")
        for row in helmese.OPERATORS:
            self.assertTrue(row.get("earned", "").strip(),
                            "operator %s has no provenance" % row["symbol"])
        for row in helmese.ROLES:
            self.assertTrue(row.get("earned", "").strip(),
                            "role %s has no provenance" % row["suffix"])
        for row in helmese.SAFETY:
            self.assertTrue(row.get("earned", "").strip(),
                            "safety entry %s has no provenance" % row["dense"])

    def test_exclusions_are_recorded_with_reasons(self):
        """An exclusion nobody wrote down gets re-proposed. The reason is the
        artifact — without it the next reader re-litigates from scratch."""
        self.assertTrue(helmese.NOT_SEEDED, "no exclusions recorded at all")
        for row in helmese.NOT_SEEDED:
            self.assertTrue(row.get("why", "").strip(),
                            "exclusion %r carries no reason" % row.get("item"))

    def test_safety_entries_are_DUAL_FORM(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertTrue(helmese.SAFETY) runs before the loop, so an emptied table fails rather than skipping every assertion.
        """A dense safety string is permitted only because it is REGISTERED and
        carries its own plain expansion. A reader who does not know the register
        must still read the prohibition."""
        self.assertTrue(helmese.SAFETY, "no safety entries, loop is a no-op")
        for row in helmese.SAFETY:
            self.assertTrue(row["dense"].strip())
            self.assertTrue(row["plain"].strip())
            self.assertNotEqual(row["dense"], row["plain"])
            self.assertTrue(row["plain"].isascii(),
                            "the plain form must be readable without the "
                            "register: %r" % row["plain"])


class VersionStampTest(unittest.TestCase):
    """Amendment 3: an unknown version REFUSES rather than guesses."""

    def test_the_stamp_carries_both_version_and_digest(self):
        s = helmese.stamp()
        self.assertEqual(s["register"], helmese.VERSION)
        self.assertEqual(s["digest"], helmese.digest())
        self.assertTrue(s["digest"], "digest is empty")

    def test_the_digest_is_stable_across_calls(self):
        """A digest that varies per process cannot detect anything — every
        comparison would report drift and readers would learn to ignore it."""
        self.assertTrue(helmese.digest(), "digest is empty; equality below "
                        "would hold for two empty strings")
        self.assertEqual(helmese.digest(), helmese.digest())

    def test_the_digest_MOVES_when_a_seeded_entry_changes(self):  # noqa: VACUOUS_ASSERTION — asserts a CHANGE (assertNotEqual) and then that the table was restored; an inert digest fails both.
        """The whole point: it catches an edit that forgot to bump VERSION.
        Mutating the live table would leak into other tests, so this rebuilds
        the digest input the same way over a changed copy."""
        before = helmese.digest()
        original = helmese.OPERATORS
        try:
            helmese.OPERATORS = original[:-1] + (
                dict(original[-1], gloss="a deliberately different gloss"),)
            self.assertNotEqual(helmese.digest(), before,
                                "the digest ignored a changed gloss, so an "
                                "unversioned edit would pass unnoticed")
        finally:
            helmese.OPERATORS = original
        self.assertEqual(helmese.digest(), before, "the table was not restored")


class DecoderContractTest(unittest.TestCase):
    """accepts() — the REFUSAL half of the version law (amendment 3).

    The module docstring promised that a decoder meeting an unknown version
    refuses rather than guessing with a newer table. Promising a refusal is not
    shipping one, and until this existed the claim was prose."""

    def test_its_OWN_stamp_is_accepted(self):
        ok, why = helmese.accepts(helmese.stamp())
        self.assertTrue(ok, why)
        self.assertEqual(why, "")

    def test_an_UNKNOWN_version_refuses_and_says_what_to_do(self):
        ok, why = helmese.accepts({"register": "0.9.0",
                                   "digest": helmese.digest()})
        self.assertFalse(ok)
        self.assertIn("typed fields", why, "a refusal must name the fallback")

    def test_a_MATCHING_version_with_a_different_digest_refuses(self):
        """The more dangerous case, because it looks safe: same version number,
        different content. That is the edit a version cannot see."""
        ok, why = helmese.accepts({"register": helmese.VERSION,
                                   "digest": "0000000000000000"})
        self.assertFalse(ok)
        self.assertIn("digest", why)

    def test_a_missing_or_malformed_stamp_refuses(self):
        # MUST-HIT CONTROL: a real stamp IS accepted, so the refusals below
        # are decisions rather than a function that refuses everything.
        self.assertTrue(helmese.accepts(helmese.stamp())[0])
        for bad in (None, {}, "1.1.0", 42, {"register": helmese.VERSION}):
            ok, _why = helmese.accepts(bad)
            self.assertFalse(ok, repr(bad))

    def test_the_shipped_version_digest_is_PINNED(self):
        """The pin is the point: it fails on any content change, so an edit
        must consciously bump VERSION and update this line together."""
        self.assertEqual(helmese.VERSION, "1.2.0")
        self.assertEqual(helmese.digest(), "cc1529368df003d9",
                         "the register changed — bump VERSION and re-pin")


class DigestFramingTest(unittest.TestCase):
    """A SEPARATOR IS NOT FRAMING.

    Fields were colon-joined, so text could move ACROSS a field boundary while
    the joined bytes stayed identical — the register's MEANING changed and the
    digest did not, which is the one failure a digest exists to prevent. Worse,
    accepts() kept approving the stale stamp."""

    def test_moving_text_ACROSS_a_field_boundary_moves_the_digest(self):
        before = helmese.digest()
        self.assertTrue(before, "the digest is empty")
        original = helmese.OPERATORS
        row = original[0]
        head, sep, tail = row["gloss"].partition(":")
        self.assertTrue(sep, "this fixture needs a gloss containing a colon")
        try:
            # gloss keeps the head, earned absorbs the tail WITH the same
            # separator that used to join them: byte-identical when joined.
            helmese.OPERATORS = (dict(row, gloss=head,
                                      earned=tail + ":" + row["earned"]),) \
                + original[1:]
            self.assertNotEqual(helmese.digest(), before,
                                "text moved between fields and the digest did "
                                "not notice")
        finally:
            helmese.OPERATORS = original
        self.assertEqual(helmese.digest(), before, "the table was not restored")

    def test_framing_is_length_prefixed(self):
        self.assertEqual(helmese._framed("ab", "c"), b"2:ab1:c")
        # the discriminator: two different splits cannot frame identically
        self.assertNotEqual(helmese._framed("a:b", "c"),
                            helmese._framed("a", "b:c"))


class ProvenanceIsContentTest(unittest.TestCase):
    """Amendment 4 makes `earned` mandatory, so it is part of the register.

    digest() claimed to hash the whole seeded register and omitted provenance,
    which let a rewritten justification ship under an unchanged digest."""

    def test_the_digest_MOVES_when_only_PROVENANCE_changes(self):
        before = helmese.digest()
        self.assertTrue(before, "the digest is empty; every comparison below "
                        "would hold between two empty strings")
        original = helmese.OPERATORS
        self.assertTrue(original, "no operators, so nothing can be mutated")
        try:
            helmese.OPERATORS = original[:-1] + (
                dict(original[-1], earned="a completely different reason"),)
            self.assertNotEqual(helmese.digest(), before,
                                "provenance is not covered by the digest")
        finally:
            helmese.OPERATORS = original
        self.assertEqual(helmese.digest(), before, "the table was not restored")


class GlossTest(unittest.TestCase):
    """Deterministic by construction — a lookup, never a model call."""

    def test_it_glosses_each_seeded_kind(self):
        self.assertIn("sequence", helmese.gloss("→"))
        self.assertIn("owns", helmese.gloss("-lead"))
        self.assertEqual(helmese.gloss("禁推main"), "never push to main")

    def test_an_unseeded_token_glosses_to_None_not_a_guess(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertIsNotNone(gloss("→")) proves the lookup is alive before the None assertions.
        """A gloss that invented an expansion for an unknown symbol would be
        exactly the freehanding the register exists to prevent."""
        # MUST-HIT CONTROL first: gloss is alive on a known token, so the None
        # below means "not seeded" rather than "the lookup is broken".
        self.assertIsNotNone(helmese.gloss("→"))
        for unknown in ("Ω", "↻", "", None, 42, "not-a-role"):
            self.assertIsNone(helmese.gloss(unknown), repr(unknown))

    def test_a_role_SUFFIX_glosses_on_a_full_token(self):  # noqa: VACUOUS_ASSERTION — an unconditional assertIsNotNone(gloss("-against")) runs first, so the equality below cannot pass on two Nones.
        """Roles attach to a name — 'codex-2-against' must gloss, or the rule
        only works on bare suffixes nobody writes."""
        # both must be REAL glosses — two Nones are equal and prove nothing
        self.assertIsNotNone(helmese.gloss("-against"))
        self.assertEqual(helmese.gloss("codex-2-against"),
                         helmese.gloss("-against"))


class SeedIsNotTheStoreTest(unittest.TestCase):
    """The seed ships; the grown instance lives in the store. Two artifacts,
    one lineage, and neither writes to the other."""

    def test_the_module_performs_no_store_io(self):
        with open(helmese.__file__, encoding="utf-8") as fh:
            src = fh.read()
        # an empty read would satisfy every assertNotIn below
        self.assertGreater(len(src), 2000, "the module source did not load")
        for forbidden in ("write_lexicon", "load_all", "store.", "open("):
            self.assertNotIn(forbidden, src,
                             "the seed register touches the store or the "
                             "filesystem: %r" % forbidden)


if __name__ == "__main__":
    unittest.main()
