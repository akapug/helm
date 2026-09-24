#!/usr/bin/env python3
"""promptcensus — how often a word reaches a turn (task/2978).

The store's stem gate measured commonness on the STATEMENT corpus while the
probes fire on PROMPTS: chat wakes, task notices, subagent hand-backs. The
census is the prompt side of that measurement. Its unit is a TURN (a word in
three turns is in three turns however often each repeats it), and its word
forms are exactly the ones a single-word probe matches under the resolver's
own boundary and inflection law, so a fraction it reports is the fraction of
turns that probe would have fired on.

Every arm imports the module inside itself, so on a tree without it each arm
fails on its own line."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def census():
    from helm import promptcensus
    return promptcensus


class CensusBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-census-")
        self.prior = os.environ.get("HELM_HOME")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        # FormsTest's word list names "stop-guard", which the scratch-reaper
        # tripwire (test_scratch.HostMutationTripwireTest) reads as driving the
        # stop hook. Nothing here drives it; the switch is set anyway so a
        # future test that does can never reap real host scratch.
        self.prior_gc = os.environ.get("HELM_SCRATCH_GC")
        os.environ["HELM_SCRATCH_GC"] = "0"

    def tearDown(self):
        if self.prior is None:
            os.environ.pop("HELM_HOME", None)
        else:
            os.environ["HELM_HOME"] = self.prior
        if self.prior_gc is None:
            os.environ.pop("HELM_SCRATCH_GC", None)
        else:
            os.environ["HELM_SCRATCH_GC"] = self.prior_gc
        shutil.rmtree(self.tmp, ignore_errors=True)


class FormsTest(unittest.TestCase):
    """The census counts what the resolver matches, never a second law."""

    TEXT = ("three events and a merge-tree stop-guard at 30m; the caps "
            "were processed, band was lagging")

    def test_forms_agree_with_the_resolver_probe_law(self):
        from helm.store.resolve import _probe_re
        got = census().forms(self.TEXT)
        probes = ("event", "events", "merge", "tree", "merge-tree", "stop",
                  "stop-guard", "30m", "cap", "caps", "process", "processed",
                  "band", "ban", "lag", "lagg", "lagging", "three", "tre")
        low = self.TEXT.lower()
        hits = [p for p in probes if re.search(_probe_re(p), low)]
        self.assertIn("event", hits)                  # the law does inflect
        self.assertNotIn("ban", hits)                 # ...and does not stem
        for p in probes:
            with self.subTest(probe=p):
                self.assertEqual(p in got, p in hits)

    def test_forms_of_nothing(self):  # noqa: VACUOUS_ASSERTION — an empty set IS the claim; test_forms_agree_with_the_resolver_probe_law is the positive twin on the same function
        self.assertEqual(census().forms(""), set())
        self.assertEqual(census().forms(None), set())


class RecordLoadTest(CensusBase):
    def test_a_turn_counts_each_form_once(self):
        c = census()
        self.assertTrue(c.record("the fluxcap and the fluxcap again"))
        self.assertTrue(c.record("no flux here"))
        got = c.load()
        self.assertEqual(got.turns, 2)
        self.assertEqual(got.df["fluxcap"], 1)
        self.assertAlmostEqual(got.frac("fluxcap"), 0.5)
        self.assertAlmostEqual(got.frac("FluxCap"), 0.5)
        self.assertEqual(got.frac("absent"), 0.0)

    def test_an_empty_turn_is_not_a_turn(self):
        c = census()
        self.assertFalse(c.record("   "))
        self.assertFalse(c.record(None))
        c.record("one real turn")
        self.assertEqual(c.load().turns, 1)

    def test_nothing_recorded_is_an_unmeasured_census(self):  # noqa: VACUOUS_ASSERTION — an empty census IS the claim; test_measured_only_from_min_turns is the positive twin on the same common() read
        c = census()
        got = c.load()
        self.assertEqual((got.turns, got.measured), (0, False))
        self.assertEqual(got.common(0.01), frozenset())

    def test_measured_only_from_min_turns(self):
        c = census()
        state = {"v": 1, "turns": c.MIN_TURNS - 1,
                 "df": {"please": c.MIN_TURNS - 1, "fluxcap": 1}}
        os.makedirs(os.path.dirname(c.path()), exist_ok=True)
        with open(c.path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
        self.assertFalse(c.load().measured)
        self.assertEqual(c.load().common(0.5), frozenset())
        c.record("please")
        got = c.load()
        self.assertTrue(got.measured)
        self.assertEqual(got.common(0.5), frozenset({"please"}))

    def test_the_window_halves_counts_and_turns(self):  # noqa: VACUOUS_ASSERTION — turns and the kept word's count are asserted EQUAL to non-zero values on the same load before the shed word's absence
        c = census()
        os.makedirs(os.path.dirname(c.path()), exist_ok=True)
        with open(c.path(), "w", encoding="utf-8") as f:
            json.dump({"v": 1, "turns": c.WINDOW - 1,
                       "df": {"please": 2000, "once": 1}}, f)
        c.record("please")
        got = c.load()
        self.assertEqual(got.turns, c.WINDOW // 2)
        self.assertEqual(got.df["please"], 2001 // 2)
        self.assertNotIn("once", got.df)              # 1 // 2 == 0: shed

    def test_the_table_is_bounded_and_keeps_the_frequent(self):
        c = census()
        df = {"w%05d" % i: 1 for i in range(c.CAP + 50)}
        df["please"] = 900
        os.makedirs(os.path.dirname(c.path()), exist_ok=True)
        with open(c.path(), "w", encoding="utf-8") as f:
            json.dump({"v": 1, "turns": 1000, "df": df}, f)
        c.record("please")
        got = c.load()
        self.assertLessEqual(len(got.df), c.CAP)
        self.assertEqual(got.df["please"], 901)

    def test_a_garbled_census_reads_empty_and_is_rebuilt(self):
        c = census()
        os.makedirs(os.path.dirname(c.path()), exist_ok=True)
        with open(c.path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(c.load().turns, 0)
        self.assertTrue(c.record("fluxcap"))
        self.assertEqual(c.load().turns, 1)

    def test_an_alien_shaped_census_reads_empty(self):
        c = census()
        os.makedirs(os.path.dirname(c.path()), exist_ok=True)
        for alien in ([], {"turns": "many", "df": {}},
                      {"turns": 5, "df": {"a": "x"}}, {"turns": 5, "df": []}):
            with self.subTest(alien=alien):
                with open(c.path(), "w", encoding="utf-8") as f:
                    json.dump(alien, f)
                self.assertEqual(c.load().turns, 0)
        with open(c.path(), "w", encoding="utf-8") as f:
            json.dump({"v": 1, "turns": 5, "df": {"a": 2}}, f)
        self.assertEqual(c.load().turns, 5)            # control: a sane one reads


if __name__ == "__main__":
    unittest.main()
