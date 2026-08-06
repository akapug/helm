#!/usr/bin/env python3
"""The lease arm latches per held-set state instead of re-sermoning forever.

OWNER-SURFACED 2026-08-04: the stop-guard's LEASE arm re-printed its full
multi-line warning on EVERY stop while the held set was unchanged — the owner
watched the identical wall twice in a row and asked "is that perfect or does
it need adjusting?". Every sibling rung already latched per state (inbox on
the pending fingerprint, beacon on armed|missing, spiral on chain|rounds);
this arm was the one publisher without a memory.

The contract these tests pin, arm by arm:
  * the FIRST stop on a held set prints the full sermon and BLOCKS;
  * a re-stop on the SAME set (same resources, same lease ids, same TTL
    band) compresses to ONE warn line and the stop passes — dropping the
    latch turns this test red;
  * ANY change to the set — a new lease, a release, a re-claim (new lease
    id under the old resource), or a remainder crossing LEASE_TTL_ALARM_S —
    re-prints the full block: the latch compresses repetition, never
    severity escalation;
  * an UNWRITABLE latch degrades the sermon to a WARN, never an
    unconditional re-block (the refusal-honesty law: a gate that cannot
    remember must never become a wall);
  * no state word is spoken without a measurement: the arm never claims a
    live delegate it did not prove (#112's computable half was probed
    2026-08-04 and no reliable in-guard signal exists — the transcript-dir
    subagent records carry no liveness marker and record the parent's cwd,
    never the lane room).
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import pk, seats  # noqa: E402

ENV_KEYS = ("HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR", "MELD_CHAT_DIR",
            "HELM_CHAT_NAME", "MELD_CHAT_NAME", "HELM_CHAT_NODE_URL",
            "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG", "MELD_CHAT_LOG",
            "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_ROOM", "HELM_ADOPTED_DIR",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
            "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HELM_PRIVATE_NEEDLES",
            "HELM_SCRATCH_GC")

ME = "latch-seat"
SESSION = "aaaa1111-2222-3333-4444-555566667777"

FULL = "live claim lease(s) held by this session"     # the sermon's key
COMPRESSED = "unchanged since the last warning"       # the one-liner's key


class LeaseLatchBase(unittest.TestCase):
    """Hermetic tmp HELM_HOME/HELM_CHAT_DIR; non-lane resources on purpose so
    the delegation and gate-pending exemptions resolve to None fast and the
    latch is the only variable under test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-leaselatch-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"
        os.environ["HELM_SCRATCH_GC"] = "0"
        os.environ["HELM_CHAT_NAME"] = ME

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def claim(self, resource, ttl=600):
        ok, msg, lease = seats.claim(resource, ME, ttl=ttl, session=SESSION)
        self.assertTrue(ok, msg)
        return lease

    def drop(self, resource):
        """Remove a row the way a release/expiry leaves the ledger."""
        c = pk.read_json(seats.claims_path(), {}) or {}
        del c[resource]
        pk.write_json(seats.claims_path(), c)

    def decay(self, resource, left):
        """Rewrite one row's remaining TTL — the crossing under test."""
        c = pk.read_json(seats.claims_path(), {}) or {}
        c[resource]["exp_mono"] = seats._now_mono() + left
        pk.write_json(seats.claims_path(), c)

    def guard(self):
        return seats.stop_guard(session=SESSION, room="main", seat=ME)

    def sermons(self, lines):
        return [x for x in lines if FULL in x]

    def compressed(self, lines):
        return [x for x in lines if COMPRESSED in x]


class SameSetLatchTest(LeaseLatchBase):
    def test_the_first_stop_blocks_with_the_full_sermon(self):  # noqa: VACUOUS_ASSERTION — len(hit)==1 + the assertIns are the unconditional positive controls; the compressed-line absence is the finding
        self.claim("db-migration")
        blocks, warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("db-migration", hit[0])
        self.assertIn("helm chat release db-migration", hit[0])
        self.assertEqual(self.compressed(warns), [])

    def test_a_restop_on_the_same_set_compresses_to_one_warn_line(self):  # noqa: VACUOUS_ASSERTION — the compressed line (len==1 + content) is the unconditional positive control on the same stop's output; the sermon absence is the finding
        """THE LATCH ITSELF — drop it and this test is red on the re-block."""
        self.claim("db-migration")
        self.guard()
        blocks, warns = self.guard()
        self.assertEqual(self.sermons(blocks), [],
                         "the identical wall printed twice — the owner's "
                         "exact defect")
        line = self.compressed(warns)
        self.assertEqual(len(line), 1, warns)
        self.assertIn("1 lease(s) held", line[0])
        self.assertNotIn("\n", line[0])           # ONE line, not a wall
        self.assertNotIn("--lease", line[0])      # detail lives in the sermon
        self.assertIn("Leases retained", line[0])

    def test_a_new_lease_reprints_the_full_block(self):
        self.claim("db-migration")
        self.guard()
        self.claim("cache-rebuild")
        blocks, _warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("db-migration", hit[0])
        self.assertIn("cache-rebuild", hit[0])

    def test_a_release_reprints_the_full_block_for_the_remainder(self):
        self.claim("db-migration")
        self.claim("cache-rebuild")
        self.guard()
        self.drop("cache-rebuild")
        blocks, _warns = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("db-migration", hit[0])
        self.assertNotIn("cache-rebuild", hit[0])

    def test_an_all_exempt_stop_rearms_the_full_sermon(self):  # noqa: VACUOUS_ASSERTION — the giver warn and the final len(sermons)==1 are the unconditional positive controls; the mid-test sermon absence is the exempt state itself
        """sermon (latched) -> the row turns exempt for one stop (the giver
        shape: holder flipped to a rostered peer) -> back to held. Without
        the exempt-stop clear, the return fingerprints identically to the
        latched sermon and a lapsed exemption — a delegate's death, in the
        delegation arm's version of this shape — would compress into the
        one-liner. The stop after an exemption lapses must be LOUD."""
        self.claim("db-migration")
        self.guard()                                   # sermon, latched
        c = pk.read_json(seats.claims_path(), {}) or {}
        c["db-migration"]["holder"] = "peer-seat"
        pk.write_json(seats.claims_path(), c)
        r = pk.read_json(seats.roster_path(), {}) or {}
        r.setdefault("peer-seat", {})["session"] = (
            "99998888-7777-6666-5555-444433332222")
        pk.write_json(seats.roster_path(), r)
        blocks, warns = self.guard()                   # giver: warn, allowed
        self.assertEqual(self.sermons(blocks), [], blocks)
        self.assertTrue([w for w in warns if "not yours to release" in w],
                        warns)
        c = pk.read_json(seats.claims_path(), {}) or {}
        c["db-migration"]["holder"] = ME
        pk.write_json(seats.claims_path(), c)
        blocks, _warns = self.guard()
        self.assertEqual(len(self.sermons(blocks)), 1,
                         "the return to held after an exempt stop was "
                         "compressed — the lapse was silent")

    def test_a_reclaim_is_a_new_lease_id_and_reprints(self):  # noqa: VACUOUS_ASSERTION — len(sermons)==1 on the reclaim stop is the unconditional positive control
        """The fingerprint keys on the LEASE ID, not the resource name alone:
        release-then-reclaim of the same resource is a fresh obligation."""
        first = self.claim("db-migration")
        self.guard()
        self.drop("db-migration")
        second = self.claim("db-migration")
        self.assertNotEqual(first, second)
        blocks, _warns = self.guard()
        self.assertEqual(len(self.sermons(blocks)), 1, blocks)


class TTLEscalationTest(LeaseLatchBase):
    def test_crossing_the_alarm_escalates_through_the_latch(self):  # noqa: VACUOUS_ASSERTION — the post-crossing block (len==1 + EXPIRING) is the unconditional positive control; the latched quiet stop is the state under test
        """A latched set whose remainder crosses LEASE_TTL_ALARM_S is a
        CHANGED state: the full block re-fires and says EXPIRING. Severity is
        never compressed."""
        self.claim("db-migration", ttl=600)
        self.guard()
        blocks, _w = self.guard()
        self.assertEqual(self.sermons(blocks), [])       # latched, quiet
        self.decay("db-migration", seats.LEASE_TTL_ALARM_S - 30)
        blocks, _w = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("EXPIRING", hit[0])

    def test_a_lease_born_below_the_alarm_is_loud_at_first_sight(self):
        self.claim("db-migration", ttl=60)
        blocks, _w = self.guard()
        hit = self.sermons(blocks)
        self.assertEqual(len(hit), 1, blocks)
        self.assertIn("EXPIRING", hit[0])

    def test_repetition_in_the_expiring_band_still_compresses_but_counts(self):  # noqa: VACUOUS_ASSERTION — the compressed line (len==1, '1 EXPIRING') is the unconditional positive control
        """After the crossing fired once, an unchanged expiring set is
        repetition again — but the one-liner carries the EXPIRING tally, so
        the state stays visible without the wall."""
        self.claim("db-migration", ttl=60)
        self.guard()
        blocks, warns = self.guard()
        self.assertEqual(self.sermons(blocks), [])
        line = self.compressed(warns)
        self.assertEqual(len(line), 1, warns)
        self.assertIn("1 EXPIRING", line[0])


class UnwritableLatchTest(LeaseLatchBase):
    def test_an_unwritable_latch_degrades_to_the_warn_and_never_walls(self):  # noqa: VACUOUS_ASSERTION — the loop body runs unconditionally twice and asserts len(sermons(warns))==1 each pass
        """The refusal-honesty law: a latch that cannot remember must degrade
        to warn semantics, never to an unconditional re-block — the exact bug
        the beacon/spiral rungs fixed ('no latch, no block')."""
        self.claim("db-migration")
        with mock.patch.object(seats.pk, "atomic_write",
                               side_effect=OSError("read-only")):
            for _ in range(2):        # every stop, not just the first
                blocks, warns = self.guard()
                self.assertEqual(self.sermons(blocks), [],
                                 "an unwritable latch became a wall")
                self.assertEqual(len(self.sermons(warns)), 1, warns)


class NoForgedDelegationSignalTest(LeaseLatchBase):
    def test_no_delegate_claim_is_ever_spoken_without_a_measurement(self):  # noqa: VACUOUS_ASSERTION — the two len==1 must-hits prove the scanned corpus is the armed guard speaking about this lease before the absences are read
        """#112's computable half, probed 2026-08-04: no reliable in-guard
        read of live subagents exists (no liveness marker in the transcript
        records, parent-cwd only, tasklist rows are declarations). So the arm
        must never render a HELD-FOR-LIVE-DELEGATE state it cannot measure —
        in either the sermon or the compressed line."""
        self.claim("db-migration")
        b1, w1 = self.guard()
        b2, w2 = self.guard()
        # MUST-HIT first (absence-of-a-match is not a finding): the corpus
        # under scan really is the armed guard talking about this lease.
        self.assertEqual(len(self.sermons(b1)), 1, b1)
        self.assertEqual(len(self.compressed(w2)), 1, w2)
        everything = "\n".join(b1 + w1 + b2 + w2)
        self.assertNotIn("HELD FOR LIVE DELEGATE", everything)
        self.assertNotIn("live delegated build", everything)
