#!/usr/bin/env python3
"""helm.multiplayer_demo — the demo LWW CRDT client. Helm's relay stays blind;
this is the reference materializer that folds its opaque log into a keyed map.
The browser mirrors this fold in web_ui.html. Hermetic: pure functions, no
filesystem, no env."""
import unittest

from helm import multiplayer_demo as d


def _env(update, ts, ident, actor="x"):
    """A relay envelope shaped like LocalRelay.updates rows carry."""
    return {"update": update, "ts": ts, "id": ident, "actor": actor}


class DemoCrdtTest(unittest.TestCase):
    def test_encode_roundtrips_through_materialize(self):
        out = d.materialize([_env(d.encode("greeting", "hello", "daria"), 1.0, "a")])
        self.assertEqual(out["board"], [{"key": "greeting", "value": "hello",
                                         "actor": "daria", "ts": 1.0}])
        self.assertEqual((out["cells"], out["foreign"]), (1, 0))

    def test_lww_later_timestamp_wins_in_either_arrival_order(self):
        early = _env(d.encode("k", "old", "daria"), 1.0, "aaa")
        late = _env(d.encode("k", "new", "codex"), 2.0, "bbb")
        for order in ([early, late], [late, early]):
            out = d.materialize(order)
            self.assertEqual([c["value"] for c in out["board"]], ["new"], order)
            self.assertEqual(out["board"][0]["actor"], "codex")

    def test_equal_timestamp_breaks_on_id_deterministically(self):
        # a real tie is astronomically unlikely, but the winner must still be a
        # total order — the greater id — so every client converges identically.
        a = _env(d.encode("k", "A", "x"), 5.0, "id-aaa")
        b = _env(d.encode("k", "B", "y"), 5.0, "id-bbb")
        for order in ([a, b], [b, a]):
            self.assertEqual(d.materialize(order)["board"][0]["value"], "B", order)

    def test_independent_keys_all_survive_sorted_by_key(self):
        out = d.materialize([_env(d.encode("b", "2", "x"), 2.0, "i2"),
                             _env(d.encode("a", "1", "y"), 1.0, "i1")])
        self.assertEqual([(c["key"], c["value"]) for c in out["board"]],
                         [("a", "1"), ("b", "2")])

    def test_foreign_updates_are_counted_never_decoded(self):
        out = d.materialize([
            _env("not json at all", 1.0, "i1"),
            _env('{"k":"other.type","key":"x","value":"y","actor":"z"}', 2.0, "i2"),
            _env('{"k":"helm.demo.lww","key":"real","value":"v","actor":"a"}', 3.0, "i3")])
        self.assertEqual([c["key"] for c in out["board"]], ["real"])
        self.assertEqual(out["foreign"], 2)  # the transport carried strangers' bytes

    def test_encode_rejects_bad_fields(self):
        for bad in (("", "v", "a"), ("k", "v", ""), ("k\x1b", "v", "a"),
                    ("k\x1bx", "v", "a")):
            with self.assertRaises(ValueError):
                d.encode(*bad)
        with self.assertRaises(ValueError):
            d.encode("k", 7, "a")           # value must be a string
        with self.assertRaises(ValueError):
            d.encode("k" * 300, "v", "a")   # over the key byte cap

    def test_materialize_tolerates_empty_and_none(self):
        self.assertEqual(d.materialize([])["board"], [])
        self.assertEqual(d.materialize(None), {"board": [], "cells": 0, "foreign": 0})


if __name__ == "__main__":
    unittest.main()
