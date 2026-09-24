"""Readers must tolerate other threads extending the shared proof memo."""
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from helm import landreq, projscope
from tests.test_land_ledger import LedgerBase


class ConcurrentProofReaders(LedgerBase):
    def exercise(self, kind):
        base = self.commit("base.txt")
        first = self.commit("first.txt")
        second = self.commit("second.txt")
        newest = self.commit("newest.txt")
        with projscope.scope():
            landreq._keep_landing_proof(self.gitdir, base, first, "ancestor")
        landreq._land_proof_ledger()
        hit = []

        def append():
            with projscope.scope():
                tip = first if kind == "outer" else base
                landreq._keep_landing_proof(self.gitdir, tip, second, "ancestor")

        class Pairs(dict):
            def interleave(self, key):
                if not hit:
                    hit.append(key)
                    # Complete a real writer while the reader holds its iterator.
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(append).result(timeout=3)

            def get(self, key, default=None):
                self.interleave(key)
                return super().get(key, default)

            def __contains__(self, key):
                self.interleave(key)
                return super().__contains__(key)

        pairs = Pairs({(first, newest): True} if kind == "positive" else {})
        try:
            with mock.patch.object(landreq, "_ancestry_ledger", return_value=pairs):
                if kind in ("lookup", "positive"):
                    self.assertEqual(
                        landreq._kept_landing_proof(self.gitdir, base, newest),
                        "ancestor" if kind == "positive" else None)
                else:
                    with mock.patch.object(landreq, "_ancestry_append") as teach:
                        with projscope.scope():
                            landreq._teach_trunk_pairs(self.gitdir, newest)
                    teach.assert_called_once_with(self.gitdir, [(first, newest)])
        finally:
            self.assertEqual(len(hit), 1, "the interleaved append did not fire")
            # The new answer is durably readable, not just a mutation of a mock.
            landreq._LAND_PROOF_MEMO.clear()
            self.assertEqual(landreq._kept_landing_proof(
                self.gitdir, first if kind == "outer" else base, second), "ancestor")

    def test_lookup_survives_concurrent_new_trunk(self):
        self.exercise("lookup")

    def test_teach_survives_concurrent_new_trunk(self):
        self.exercise("nested")

    def test_teach_survives_concurrent_new_tip(self):
        self.exercise("outer")

    def test_carried_positive_survives_concurrent_append(self):
        self.exercise("positive")
