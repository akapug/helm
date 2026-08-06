#!/usr/bin/env python3
"""The native append-only chain MUST be concurrency-safe. It is the fleet's
PRIMARY proof: two agents capturing at once must not fork it (both reading the
same head and appending records with the same prev + index). _append_record
holds an exclusive inter-process lock (fcntl.flock) across read-head + construct
+ append, so real concurrent writers serialize into ONE intact chain.

This test uses REAL forked processes (not threads) hammering the same chain file
behind a barrier — the honest reproduction of the race. Without the lock the
chain forks and verify_chain() reports it broken; with the lock it is intact."""
import os
import shutil
import tempfile
import unittest

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tests._tmphome import home as _tmp_home  # noqa: E402
_tmp_home(prefix="helm-test-home-", var="HELM_HOME")

from helm import premise  # noqa: E402

DEAD = "http://127.0.0.1:1"
PER_WORKER = 50
WORKERS = 2


def _worker(home_dir, node_url, worker_id, n, barrier):
    """Append `n` records to the shared native chain, starting in lockstep with
    the sibling (barrier) so the read-head+append windows genuinely overlap."""
    os.environ["HELM_HOME"] = home_dir
    os.environ["HELM_NODE_URL"] = node_url
    from helm import premise as p, pk
    barrier.wait()
    for i in range(n):
        p._append_record(
            "create", "w%d-law-%d" % (worker_id, i),
            p.digest_payload("truth %d %d" % (worker_id, i)),
            root="global", project="", ts=pk.now_ts(), source="human",
            attest_by="t", supersedes="", supersedes_record="")


@unittest.skipIf(premise.fcntl is None, "POSIX fcntl unavailable (non-Linux)")
class ConcurrentAppendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-premconc-")
        self.prev = os.environ.get("HELM_HOME")
        self.prev_node = os.environ.get("HELM_NODE_URL")
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_NODE_URL"] = DEAD

    def tearDown(self):
        for k, v in (("HELM_HOME", self.prev), ("HELM_NODE_URL", self.prev_node)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_concurrent_writers_one_intact_chain(self):
        import multiprocessing as mp
        ctx = mp.get_context("fork")   # inherit env + HELM_HOME; Linux fleet
        barrier = ctx.Barrier(WORKERS)
        procs = [ctx.Process(target=_worker,
                             args=(os.environ["HELM_HOME"], DEAD, w,
                                   PER_WORKER, barrier))
                 for w in range(WORKERS)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0, "worker crashed")

        recs = premise.chain_records()
        total = WORKERS * PER_WORKER
        self.assertEqual(len(recs), total, "lost or forked appends")
        # every record hash is unique (no two writers minted the same record)
        self.assertEqual(len({r["rec_hash"] for r in recs}), total)
        # chain_index is a contiguous 0..N-1 with no duplicates (no fork)
        self.assertEqual(sorted(r["chain_index"] for r in recs),
                         list(range(total)))
        # every prev links to its immediate predecessor; every hash recomputes
        ok, detail = premise.verify_chain()
        self.assertTrue(ok, detail)
        self.assertIn("%d records" % total, detail)


if __name__ == "__main__":
    unittest.main()
