#!/usr/bin/env python3
"""premise backfill tests — --attest-existing / --attest-sweep (attesting the
imported corpus IN PLACE). Hermetic: HELM_HOME + the adopted root are tempdirs,
cell.send_self / premise.refuel / premise._post_json are mocked — the real
node, ~/.dregg and ~/.helm are never touched."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("HELM_HOME", tempfile.mkdtemp(prefix="helm-test-home-"))

from helm import cell, home, premise, store  # noqa: E402

TURN = "ee" * 32

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_ADOPTED_DIR", "HELM_CELL_BIN",
            "MELD_CELL_BIN", "HELM_CELL_PROFILE", "MELD_AGENT_PROFILE",
            "HELM_NODE_TOKEN", "MELD_NODE_TOKEN",
            "HELM_NODE_PASSPHRASE", "MELD_NODE_PASSPHRASE",
            "HELM_NODE_URL", "MELD_NODE_URL")

# A hand-authored adopted-corpus entry: quirky spacing, inner quotes, a body a
# writer rewrite would reflow — the byte-diff law's worst reasonable customer.
ADOPTED_RAW = """---
name: prior-corpus-law
description: "premise: corpus-law - The corpus   truth"
metadata:
  node_type: memory
  type: prior
  id: corpus-law
  statement: The corpus   truth with "quotes"  inside
  confidence: 1.0
  class: certain
  status: live
  keywords: corpus,lawful
---

PREMISE: hand-written body   with odd	whitespace a rewrite would destroy
"""


def ok_info(turn=TURN, chain=9):
    return ({"turn_hash": turn, "receipt_hash": "rb", "chain_index": chain}, None)


class BackfillBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-backfill-")
        self.env_prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.makedirs(os.environ["HELM_ADOPTED_DIR"])
        os.environ["HELM_CELL_PROFILE"] = "stub-prof"
        self.pause_prior = premise.RETRY_PAUSE_S
        premise.RETRY_PAUSE_S = 0  # the retry pause is a knob, not a law

    def tearDown(self):
        premise.RETRY_PAUSE_S = self.pause_prior
        for k, v in self.env_prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verb(self, args, fn=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = (fn or premise.cmd_premise)(list(args))
        return rc, out.getvalue(), err.getvalue()

    def write_adopted(self, raw=ADOPTED_RAW, name="prior-corpus-law.md"):
        p = os.path.join(os.environ["HELM_ADOPTED_DIR"], name)
        with open(p, "w") as f:
            f.write(raw)
        return p

    def write_certain(self, pid, statement, project=None, **extra):
        e = dict({"id": pid, "statement": statement, "confidence": 1.0,
                  "stated_ts": "2026-07-01T00:00:00Z", "source": "human"},
                 **extra)
        d = os.path.join(home.project_dir(project) if project
                         else home.global_dir(), "premises")
        return store.write_prior(e, root_dir=d)

    def raw(self, path):
        with open(path) as f:
            return f.read()

    def queue_rows(self):
        try:
            with open(premise._queue_path()) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []


class AttestExistingTest(BackfillBase):
    def test_annotates_in_place_byte_diff_is_attest_keys_only(self):
        p = self.write_adopted()
        before = self.raw(p)
        with mock.patch.object(cell, "send_self", return_value=ok_info()):
            rc, out, _ = self.run_verb(["--attest-existing", "corpus-law"])
        self.assertEqual(rc, 0)
        self.assertIn("attested existing 'corpus-law' [adopted]", out)
        after = self.raw(p)
        added = [l for l in after.split("\n") if l not in before.split("\n")]
        self.assertEqual([l.split(":")[0] for l in added],
                         ["  attest_payload", "  attest_ts", "  attest_by",
                          "  attest_turn", "  attest_receipt",
                          "  attest_chain_index"])
        # removing exactly the inserted lines reproduces the original bytes —
        # statement/keywords/confidence/body untouched, no reflow
        stripped = "\n".join(l for l in after.split("\n") if l not in added)
        self.assertEqual(stripped, before)
        self.assertIn("attest_payload: " + premise.digest_payload(
            'The corpus   truth with "quotes"  inside'), after)
        self.assertIn("attest_turn: " + TURN, after)
        self.assertIn("attest_by: stub-prof", after)

    def test_never_creates_a_twin_never_trips_the_add_guard(self):
        self.write_adopted()
        n_before = len(store.load_all(types=("prior",)))
        with mock.patch.object(cell, "send_self", return_value=ok_info()):
            rc, _, err = self.run_verb(["--attest-existing", "corpus-law"])
        self.assertEqual(rc, 0)
        self.assertNotIn("supersede", err)  # the add guard never ran
        gdir = os.path.join(home.global_dir(), "premises")
        self.assertFalse(os.path.exists(os.path.join(gdir, "prior-corpus-law.md")))
        self.assertEqual(len(store.load_all(types=("prior",))), n_before)
        e = store._find("corpus-law")
        self.assertEqual(e["root"], "adopted")
        self.assertEqual(e["attest_turn"], TURN)

    def test_digest_matches_premise_check_after_backfill(self):
        self.write_adopted()
        with mock.patch.object(cell, "send_self", return_value=ok_info()):
            self.run_verb(["--attest-existing", "corpus-law"])
        prior = premise._fetch_turn_status
        premise._fetch_turn_status = lambda turn: {
            "attested_height": 7, "consensus_final": True,
            "receipt_present": True, "turn_hash": TURN}
        try:
            rc, out, _ = self.run_verb(["corpus-law"], fn=premise.cmd_premise_check)
        finally:
            premise._fetch_turn_status = prior
        self.assertEqual(rc, 0)
        self.assertIn("digest: MATCH prem:b2b:", out)
        self.assertIn("finality tier: attested-after-next-height", out)

    def test_guards_belief_attested_ghost(self):
        store.write_prior({"id": "a-belief", "statement": "maybe so",
                           "confidence": 0.6},
                          root_dir=os.path.join(home.global_dir(), "premises"))
        self.write_certain("done-law", "already signed", attest_payload="x",
                           attest_turn="tt", attest_by="stub-prof")
        with mock.patch.object(cell, "send_self",
                               side_effect=AssertionError("must not send")):
            rc, _, err = self.run_verb(["--attest-existing", "a-belief"])
            self.assertEqual(rc, 1)
            self.assertIn("confidence 0.60", err)
            rc, out, _ = self.run_verb(["--attest-existing", "done-law"])
            self.assertEqual(rc, 0)  # idempotent skip, no re-send
            self.assertIn("already attested", out)
            rc, _, err = self.run_verb(["--attest-existing", "ghost"])
            self.assertEqual(rc, 1)
            self.assertIn("not found", err)
        rc, _, err = self.run_verb(["--attest-existing"])
        self.assertEqual(rc, 2)

    def test_failure_falls_to_queue_after_one_retry(self):
        self.write_adopted()
        calls = []

        def down(payload, profile):
            calls.append(payload)
            return None, "meld send failed (rc 1): node gone"

        with mock.patch.object(cell, "send_self", down):
            rc, out, _ = self.run_verb(["--attest-existing", "corpus-law"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 2)  # one retry, then the queue
        self.assertIn("attestation pending", out)
        rows = self.queue_rows()
        self.assertEqual([r["id"] for r in rows], ["corpus-law"])
        self.assertIn("node gone", rows[0]["reason"])
        self.assertNotIn("attest_turn", self.raw(os.path.join(
            os.environ["HELM_ADOPTED_DIR"], "prior-corpus-law.md")))

    def test_insufficient_balance_refuels_then_resends(self):
        self.write_adopted()
        state = {"n": 0}

        def broke_then_ok(payload, profile):
            state["n"] += 1
            if state["n"] == 1:
                return None, ("meld send failed (rc 1): rejected: insufficient "
                              "balance on cell ab12: need 1442, have 794")
            return ok_info()

        with mock.patch.object(cell, "send_self", broke_then_ok), \
                mock.patch.object(premise, "refuel",
                                  return_value=(True, None)) as refuel:
            rc, out, _ = self.run_verb(["--attest-existing", "corpus-law"])
        self.assertEqual(rc, 0)
        refuel.assert_called_once_with("stub-prof")
        self.assertEqual(state["n"], 2)
        self.assertIn("attested existing 'corpus-law'", out)

    def test_rate_limited_refuel_waits_out_the_faucet_window(self):
        # the faucet grants 1/cell/60s — a rate-limited grant is WAITED OUT
        # once (the faucet cadence is the sweep's sustainable pace)
        self.write_adopted()
        e = store._find("corpus-law", types=("prior",))
        broke = (None, "Error: rejected: insufficient balance on cell x: "
                       "need 1442, have 700")
        sends = {"n": 0}

        def send(payload, profile):
            sends["n"] += 1
            return broke if sends["n"] == 1 else ok_info()

        pauses = []
        with mock.patch.object(cell, "send_self", send), \
                mock.patch.object(premise, "refuel", side_effect=[
                    (False, "faucet refused: rate limited: 1 request per "
                            "cell per minute"),
                    (True, None)]) as refuel:
            info, err = premise.attest_existing(e, profile="stub-prof",
                                                pause=pauses.append)
        self.assertIsNone(err)
        self.assertEqual(refuel.call_count, 2)
        self.assertIn(premise.FAUCET_WINDOW_S, pauses)
        self.assertEqual(sends["n"], 2)

    def test_refuel_reads_the_faucet_body_verdict(self):
        # HTTP 200 + success:false IS a refusal (the starved-sweep bug)
        with mock.patch.object(cell, "own_cell", return_value=("ab" * 32, None)), \
                mock.patch.object(cell, "profiles_dir",
                                  return_value=self.tmp):
            with open(os.path.join(self.tmp, "p.json"), "w") as f:
                json.dump({"public_key_hex": "cd" * 32}, f)
            with mock.patch.object(premise, "_post_json", return_value=(
                    {"success": False,
                     "error": "rate limited: 1 request per cell per minute"},
                    None)):
                ok, why = premise.refuel("p")
            self.assertFalse(ok)
            self.assertIn("rate limited", why)
            with mock.patch.object(premise, "_post_json", return_value=(
                    {"success": True, "tx_hash": "t", "amount": 10000}, None)):
                ok, why = premise.refuel("p")
            self.assertTrue(ok)
            self.assertIsNone(why)


class SweepTest(BackfillBase):
    def seed_corpus(self):
        """adopted + global + project certains (unattested), one belief, one
        already-attested certain — the sweep must attest exactly three."""
        self.write_adopted()
        self.write_certain("global-law", "the global truth")
        self.write_certain("proj-law", "the project truth", project="p1")
        store.write_prior({"id": "a-belief", "statement": "maybe", "confidence": 0.6},
                          root_dir=os.path.join(home.global_dir(), "premises"))
        self.write_certain("done-law", "already signed", attest_payload="x",
                           attest_turn="tt", attest_by="stub-prof")

    def test_sweep_attests_all_roots_then_is_idempotent(self):
        self.seed_corpus()
        sent = []

        def working(payload, profile):
            sent.append(payload)
            return ok_info(chain=len(sent))

        with mock.patch.object(cell, "send_self", working):
            rc, out, _ = self.run_verb(["--attest-sweep"])
        self.assertEqual(rc, 0)
        self.assertIn("4 live certain entries — 1 attested, 3 to attest", out)
        self.assertEqual(len(sent), 3)
        self.assertIn("attested 'corpus-law' [adopted]", out)
        self.assertIn("attested 'global-law' [helm-global]", out)
        self.assertIn("attested 'proj-law' [project]", out)
        self.assertIn("3 attested, 0 queued", out)
        for pid, proj in (("corpus-law", None), ("global-law", None),
                          ("proj-law", "p1")):
            e = store._find(pid, project=proj, types=("prior",))
            self.assertEqual(e["attest_turn"], TURN, pid)
        # the belief was never a candidate
        self.assertNotIn(premise.digest_payload("maybe"), sent)
        # idempotence: a second sweep sends NOTHING
        with mock.patch.object(cell, "send_self",
                               side_effect=AssertionError("must not send")):
            rc, out, _ = self.run_verb(["--attest-sweep"])
        self.assertEqual(rc, 0)
        self.assertIn("4 attested, 0 to attest", out)
        self.assertIn("nothing to attest", out)

    def test_sweep_queue_fallback_never_crashes(self):
        self.seed_corpus()
        bad = premise.digest_payload("the project truth")
        calls = []

        def half_up(payload, profile):
            calls.append(payload)
            if payload == bad:
                return None, "meld send failed (rc 1): parse unlock response"
            return ok_info()

        with mock.patch.object(cell, "send_self", half_up):
            rc, out, _ = self.run_verb(["--attest-sweep"])
        self.assertEqual(rc, 1)  # something queued -> nonzero, but no crash
        self.assertIn("2 attested, 1 queued", out)
        self.assertIn("QUEUED 'proj-law'", out)
        self.assertEqual(calls.count(bad), 2)  # exactly one 2s-pause retry
        rows = self.queue_rows()
        self.assertEqual([(r["id"], r["project"]) for r in rows],
                         [("proj-law", "p1")])
        self.assertIn("parse unlock response", rows[0]["reason"])
        e = store._find("proj-law", project="p1", types=("prior",))
        self.assertEqual(e.get("attest_turn"), "")

    def test_sweep_dry_run_sends_nothing_and_estimates(self):
        self.seed_corpus()
        with mock.patch.object(cell, "send_self",
                               side_effect=AssertionError("must not send")):
            rc, out, _ = self.run_verb(["--attest-sweep", "--dry"])
        self.assertEqual(rc, 0)
        self.assertIn("3 to attest", out)
        self.assertIn("estimated ~%d computrons"
                      % (3 * premise.ATTEST_COST_HINT), out)

    def test_sweep_limit(self):
        self.seed_corpus()
        sent = []

        def working(payload, profile):
            sent.append(payload)
            return ok_info()

        with mock.patch.object(cell, "send_self", working):
            rc, out, _ = self.run_verb(["--attest-sweep", "--limit", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("1 attested, 0 queued", out)
        rc, _, _ = self.run_verb(["--attest-sweep", "--limit", "nope"])
        self.assertEqual(rc, 2)

    def test_sweep_prunes_stale_queue_rows(self):
        self.write_certain("global-law", "the global truth")
        premise._enqueue({"ts": "t", "id": "global-law", "project": None,
                          "payload": premise.digest_payload("the global truth"),
                          "profile": "stub-prof", "reason": "was down"})
        with mock.patch.object(cell, "send_self", return_value=ok_info()):
            rc, out, _ = self.run_verb(["--attest-sweep"])
        self.assertEqual(rc, 0)
        self.assertIn("1 stale queue row pruned", out)
        self.assertEqual(self.queue_rows(), [])

    def test_sweep_mints_one_bearer_from_the_passphrase(self):
        # emberian/dregg#60: per-send passphrase unlocks 429 at #6 — the sweep
        # must unlock ONCE and ride MELD_NODE_TOKEN for every send.
        self.write_certain("global-law", "the global truth")
        os.environ["HELM_NODE_PASSPHRASE"] = "pp"
        posts = []

        def unlock(url, payload, timeout=10):
            posts.append((url, payload))
            return {"success": True, "bearer_token": "tok-once"}, None

        with mock.patch.object(premise, "_post_json", unlock), \
                mock.patch.object(cell, "send_self", return_value=ok_info()):
            rc, out, _ = self.run_verb(["--attest-sweep"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][0].endswith("/api/cipherclerk/unlock"))
        self.assertEqual(posts[0][1], {"passphrase": "pp"})
        self.assertIn("minted once", out)
        self.assertEqual(os.environ.get("MELD_NODE_TOKEN"), "tok-once")
        self.assertNotIn("HELM_NODE_PASSPHRASE", os.environ)  # meld rides the token


if __name__ == "__main__":
    unittest.main()
