#!/usr/bin/env python3
"""A seat rename ORPHANS every worktree lease, and the ledger has no undo.

THE DEFECT, in the ledger's own terms. A lease is one row in `.claims.json`
and its `holder` is a seat NAME. `_binding_ok` compares that name with a raw
`!=` — deliberately, so a display spelling can never open a grant — which
means the instant a seat is renamed:

  * `own_leases(new-name)` answers {} — the holder cannot find their own token;
  * `release`/`refresh_claim` under the new name refuse with "the holding seat
    (<old-name>)", naming a seat that no longer exists;
  * `claim_holder_listedness` calls the holder UNLISTED, so the roster prints
    a lease held by nobody it lists;
  * and the git worktree stays locked for the whole remaining TTL.

The escape hatch is closed too, and that is the part that makes it a strand
rather than an inconvenience: `release_stale` releases only on a liveness
verdict of exactly "stale", but after a RENAME the recorded session usually
belongs to a process that is still running, so the verdict is "live" and
nothing — not the new name, not the old one, not another seat — can release
it. `claim` was the only writer of `holder` that ever existed.

`rebind_claim_sessions` already solved the same shape one field over: it walks
every row and rewrites ONE field in place, preserving "every grant byte except
session — nonce, fence, expiry, holder and timestamp do not become a fresh
lease merely because its process resumed". `rebind_claim_holder` is its
counterpart, and this module is the pin for every clause of it.

WHY THE SESSION IS CLEARED, at length (the docstring states it; here is the
argument). A recorded session binds a lease to a PROCESS. A reassignment hands
the lease to a DIFFERENT actor, so after it the recorded session names a
process that is not the holder, and both consumers of that field then answer
about the wrong one:

  1. `_binding_ok` refuses a caller whose session disagrees with the recorded
     one. Keeping it would mean the seat we just made the holder cannot
     release or refresh what it holds — the SAME strand as the defect, rebuilt
     by its cure, one field over. test_the_new_holder_can_release_what_the_old
     _name_cannot is red the moment that clearing stops.
  2. `claim_holder_liveness` reads it to decide live/stale/unknown. A DEAD
     source session on a live holder's row reads "stale", and stale is the one
     verdict that authorizes ANOTHER seat to release the row. Keeping the
     session would therefore hand every reassigned lease to the first passing
     `release_stale`.

Cleared, the row reads "unknown" instead — "no session recorded; cannot prove
liveness" — which is deliberately NOT actionable, so the failure direction is
refuse-to-release rather than release-a-live-holder's-lane. The cost is real
and is stated rather than hidden: liveness stops being provable for that row
until the new holder's next `claim` records their own session (`refresh_claim`
does not write the field; `claim` does). The dropped id leaves in the
manifest's `session_was`, which is also what makes the rollback an inverse
rather than half of one.

ISOLATION. Every test here writes `.claims.json` and `.roster.json`. Under an
unset HELM_CHAT_DIR both resolve to the RUNNING FLEET's tmpfs bus — the
measured 24,001-victim near-miss recorded in tests/__init__.py and
tests/test_gc.py. setUp plants tmp values for both AND asserts the resolved
paths land under its own tmpdir, because a redirect that missed one seam is
exactly the failure that incident was made of.
"""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from helm import pk, seats_claims, seats_common, seats_roster  # noqa: E402
from helm.seats_common import claims_path, roster_path  # noqa: E402

ENV_KEYS = ("HELM_PROC", "HELM_HOME", "MELD_HOME", "HELM_CHAT_DIR",
            "MELD_CHAT_DIR", "HELM_CHAT_NAME", "MELD_CHAT_NAME",
            "HELM_CHAT_NODE_URL", "MELD_CHAT_NODE_URL", "HELM_CHAT_LOG",
            "MELD_CHAT_LOG", "HELM_CHAT_OWNER_NAMES", "HELM_CHAT_ROOM",
            "HELM_ADOPTED_DIR", "HELM_SCRATCH_GC")

# The three seats and two lanes are this file's own vocabulary; nothing here
# transcribes a constant out of the code under test (leases, fences, expiries
# and refusal wordings are all read back from the producer instead).
ALICE, BOB, CAROL = "alice-r1", "bob-r2", "carol-r3"
RES_A, RES_B = "worktree:proj:lane-a", "worktree:proj:lane-b"
SID_A, SID_B = "alice-session-0001", "bob-session-0002"


class HolderRebindBase(unittest.TestCase):
    """Hermetic: tmp HELM_HOME + HELM_CHAT_DIR + HELM_PROC, roster planted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-holderbind-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.chat = os.path.join(self.tmp, "chat")
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = self.chat
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        # A DETERMINISTIC PROCESS TABLE. The holder-name rung of
        # claim_holder_liveness scans every same-uid cmdline for the holder
        # string, so against the real /proc a seat name typed into any shell
        # in this session answers "live" and the test measures the box.
        os.environ["HELM_PROC"] = self.proc
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_LOG"] = "0"
        os.environ["HELM_CHAT_OWNER_NAMES"] = "daria"
        os.environ["HELM_SCRATCH_GC"] = "0"
        pk.write_json(roster_path(), {ALICE: {}, BOB: {}, CAROL: {}})
        # THE ISOLATION IS ASSERTED, NOT ASSUMED — see the module docstring.
        # Both surfaces, because HELM_HOME alone historically left the chat
        # seam (roster, claims, cursors) pointed at the live fleet.
        for path in (claims_path(), roster_path()):
            self.assertTrue(path.startswith(self.tmp + os.sep),
                            "NOT ISOLATED — %s is outside %s and this test "
                            "would read or write the live fleet"
                            % (path, self.tmp))

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def claim(self, resource, seat, session=None, ttl=600):
        ok, msg, lease = seats_claims.claim(resource, seat, ttl=ttl,
                                            session=session)
        self.assertTrue(ok, msg)
        return lease

    def ledger(self):
        with open(claims_path(), "rb") as f:
            return f.read()

    def row(self, resource):
        return json.loads(self.ledger().decode("utf-8"))[resource]

    def resources(self, manifest):
        return sorted(e["resource"] for e in manifest)

    def holders(self):
        return {c["resource"]: c["holder_id"] for c in seats_claims.claims_list()}


class HolderMoveExtractionTest(HolderRebindBase):
    """Extraction must keep both established patch surfaces live."""

    def test_public_signatures_and_facade_exports_stay_compatible(self):
        import inspect
        from helm import seats, seats_claim_moves
        for name, signature in (
                ("rebind_claim_holder", "(source, target, snap=None, restore=None)"),
                ("rollback_claim_holder", "(source, target, manifest)")):
            self.assertIs(getattr(seats, name), getattr(seats_claims, name))
            self.assertEqual(str(inspect.signature(getattr(seats_claims, name))), signature)
            self.assertEqual(str(inspect.signature(getattr(seats_claim_moves, name))), signature)

    def test_direct_and_facade_read_patches_still_block_the_real_transfer(self):
        from unittest import mock
        from helm import seats
        self.claim(RES_A, ALICE, SID_A)
        before = self.ledger()
        for module in (seats_claims, seats):
            with self.subTest(module=module.__name__), mock.patch.object(
                    module, "_claims_read", side_effect=OSError("synthetic unreadable ledger")) as read:
                ok, message, moved = seats.rebind_claim_holder(ALICE, BOB)
                self.assertEqual((ok, moved), (False, []))
                self.assertIn("synthetic unreadable ledger", message)
                read.assert_called_once_with(True)
            self.assertEqual(self.ledger(), before)
        # Unconditional real positive after both patches restore.
        ok, message, moved = seats.rebind_claim_holder(ALICE, BOB)
        self.assertTrue(ok, message)
        self.assertEqual(self.resources(moved), [RES_A])
        self.assertEqual(self.row(RES_A)["holder"], BOB)

    def test_rollback_reads_the_original_rebind_patch_seam(self):
        from unittest import mock
        from helm import seats
        expected = (True, "synthetic rollback", [])
        manifest = [{"resource": RES_A, "lease": "synthetic-lease"}]
        for module in (seats_claims, seats):
            with self.subTest(module=module.__name__), mock.patch.object(
                    module, "rebind_claim_holder", return_value=expected) as rebind:
                self.assertIs(seats.rollback_claim_holder(ALICE, BOB, manifest), expected)
                rebind.assert_called_once_with(BOB, ALICE, restore=manifest)


class RebindHolderTest(HolderRebindBase):

    def test_the_new_holder_can_release_what_the_old_name_cannot(self):
        """THE property, end to end through the real doors — not by reading
        back bytes, which would prove the file changed and nothing about
        whether the BINDING moved with it.

        Both refresh calls supply the session their own process would supply,
        which is the whole point: BOB's succeeds only because the rebind
        cleared the session `_binding_ok` would otherwise have compared it
        against."""
        lease = self.claim(RES_A, ALICE, SID_A)
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, self.resources(moved)], [True, [RES_A]], msg)

        old = seats_claims.refresh_claim(RES_A, ALICE, lease=lease,
                                         session=SID_A)
        new = seats_claims.refresh_claim(RES_A, BOB, lease=lease,
                                         session=SID_B)
        # THE POSITIVE CONTROL for the empty table below, on the same
        # producer: the row is really there, really BOB's, and really visible
        # on the public surface before the release takes it away.
        self.assertEqual(self.holders(), {RES_A: BOB})
        gone = seats_claims.release(RES_A, BOB, lease=lease, session=SID_B)
        self.assertEqual([old[0], new[0], gone[0], self.holders()],
                         [False, True, True, {}],
                         "old=%r new=%r release=%r" % (old, new, gone))
        # A REFUSAL MUST NAME ITS ORIGIN: the old name is told who holds it now.
        self.assertIn(BOB, old[1])

    def test_a_reassignment_is_not_a_fresh_grant(self):
        """Which FIELDS moved, derived rather than enumerated. Listing the
        preserved keys by hand would pass a mutation that stopped copying a
        key this test never thought to name."""
        lease = self.claim(RES_A, ALICE, SID_A)
        before = self.row(RES_A)
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertTrue(ok, msg)
        after = self.row(RES_A)
        changed = {k for k in set(before) | set(after)
                   if before.get(k) != after.get(k)}
        self.assertEqual(changed, {"holder", "session"},
                         "before=%r after=%r" % (before, after))
        self.assertEqual([after["holder"], after["session"], after["lease"],
                          moved[0]["session_was"]],
                         [BOB, None, lease, SID_A])

    def test_only_the_source_seats_rows_move(self):
        """One exact mapping over the WHOLE ledger: a row that moved when it
        should not have, and one that failed to move, are the same assertion."""
        self.claim(RES_A, ALICE, SID_A)
        self.claim(RES_B, CAROL, SID_B)
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, self.resources(moved)], [True, [RES_A]], msg)
        self.assertEqual(self.holders(), {RES_A: BOB, RES_B: CAROL})

    def test_the_manifest_hands_over_the_token_and_launders_what_it_shows(self):
        """The manifest is what `helm seat reassign` prints AND what the new
        holder acts on, so it carries both halves of claims_list's split:
        `resource` is the STORED key release takes, `shown` is that key
        laundered for a terminal."""
        hostile = "worktree:proj:\x1b[2Jlane"
        lease = self.claim(RES_A, ALICE, SID_A)
        lease_h = self.claim(hostile, ALICE, SID_A)
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, self.resources(moved)],
                         [True, sorted([RES_A, hostile])], msg)
        entry = {e["resource"]: e for e in moved}
        self.assertEqual(
            [entry[RES_A]["lease"], entry[RES_A]["session_was"],
             entry[RES_A]["remaining"] > 0, entry[RES_A]["shown"],
             "\x1b" in hostile, "\x1b" in entry[hostile]["shown"]],
            [lease, SID_A, True, RES_A, True, False])
        # THE TOKEN IS THE ONE THE LEDGER NOW HANDS ITS HOLDER, and the old
        # name's column is empty in the same breath, from one call each.
        self.assertEqual({"target": seats_claims.own_leases(BOB),
                          "source": seats_claims.own_leases(ALICE)},
                         {"target": {RES_A: lease, hostile: lease_h},
                          "source": {}})

    def test_a_source_with_no_leases_is_an_empty_result_that_writes_nothing(self):
        """Owner canon for this task: refuse on a measured contradiction,
        never on absence. Holding nothing is an absence."""
        self.claim(RES_B, CAROL, SID_B)
        before = self.ledger()
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, moved, ALICE in msg, self.ledger() == before],
                         [True, [], True, True], msg)
        # POSITIVE CONTROL, same producer, unconditional: the only difference
        # is a source that HOLDS something, and that one moves. Without this
        # the empty manifest above is satisfied by a verb that never works.
        ok2, msg2, moved2 = seats_claims.rebind_claim_holder(CAROL, BOB)
        self.assertEqual([ok2, self.resources(moved2)], [True, [RES_B]], msg2)

    def test_only_a_byte_identical_pair_is_the_no_op(self):
        """A case-VARIANT is a REAL move and must not be refused as a no-op:
        routing casefolds (`recipient_matches`), `_binding_ok` and
        `own_leases` do not, so `alice` -> `Alice` orphans exactly as hard."""
        lease = self.claim(RES_A, ALICE, SID_A)
        same = seats_claims.rebind_claim_holder(ALICE, ALICE)
        variant = seats_claims.rebind_claim_holder(ALICE, ALICE.upper())
        self.assertEqual([same[0], same[2], variant[0],
                          self.resources(variant[2])],
                         [False, [], True, [RES_A]],
                         "same=%r variant=%r" % (same[1], variant[1]))
        lower = seats_claims.refresh_claim(RES_A, ALICE, lease=lease)
        upper = seats_claims.refresh_claim(RES_A, ALICE.upper(), lease=lease)
        self.assertEqual([lower[0], upper[0]], [False, True],
                         "lower=%r upper=%r" % (lower, upper))

    def test_the_cleared_session_reads_unknown_and_never_stale(self):
        """The safety half of the session decision. `unknown` is the state
        `release_stale` refuses; `stale` is the one it acts on."""
        self.claim(RES_A, ALICE, SID_A)
        ok, msg, _moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertTrue(ok, msg)
        # AN UNCONDITIONAL POSITIVE CONTROL ON THE SAME PRODUCER: a planted
        # process really can make this rung answer "live", so BOB's "unknown"
        # is a verdict about BOB and not a dead instrument.
        os.makedirs(os.path.join(self.proc, "4242"))
        with open(os.path.join(self.proc, "4242", "cmdline"), "wb") as f:
            f.write(b"claude\x00--seat\x00" + CAROL.encode("utf-8") + b"\x00")
        self.assertEqual(
            {seat: seats_claims.claim_holder_liveness(seat, None)[0]
             for seat in (CAROL, BOB)},
            {CAROL: "live", BOB: "unknown"})
        self.assertEqual([self.row(RES_A)["holder"],
                          self.row(RES_A)["session"]], [BOB, None])


class RebindRefusalTest(HolderRebindBase):
    """The MUST-MISS arms: inputs this verb has to REJECT, each paired in the
    same test with the unconditional positive control that proves the verb
    was otherwise willing and able to move the row."""

    def test_an_unlisted_target_is_refused_and_nothing_is_written(self):
        """A target the roster WAS READ and does not list is a measured
        contradiction: nothing can dispatch to a seat with no row, so moving
        a lease there rebuilds the defect this verb exists to cure."""
        self.claim(RES_A, ALICE, SID_A)
        before = self.ledger()
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, "ghost-seat")
        self.assertEqual([ok, moved, "ghost-seat" in msg,
                          self.ledger() == before], [False, [], True, True],
                         msg)
        ok2, msg2, moved2 = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok2, self.resources(moved2)], [True, [RES_A]], msg2)

    def test_a_target_no_mention_can_address_is_refused_distinctly(self):
        """Two contradictions, two sentences. An operator who cannot tell
        WHICH one fired is being handed `_lock_unavailable`'s old defect."""
        self.claim(RES_A, ALICE, SID_A)
        unaddressable = "not a seat token"        # a space: no @mention says it
        bad = seats_claims.rebind_claim_holder(ALICE, unaddressable)
        ghost = seats_claims.rebind_claim_holder(ALICE, "ghost-seat")
        self.assertEqual([bad[0], ghost[0], bad[1] == ghost[1],
                          unaddressable in bad[1]],
                         [False, False, False, True],
                         "bad=%r ghost=%r" % (bad[1], ghost[1]))
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, self.resources(moved)], [True, [RES_A]], msg)

    def test_an_unreadable_roster_proceeds_because_absence_is_not_evidence(self):
        """The other half of the canon. A roster that could not be READ says
        nothing about the target; refusing there would downgrade every
        legitimate reassignment to whatever state the roster file is in."""
        self.claim(RES_A, ALICE, SID_A)
        with open(roster_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        # The instrument really is dark — asserted, because a roster that
        # merely LISTS bob would make this arm pass for the wrong reason.
        self.assertEqual(seats_roster.roster_checked(), ({}, True))
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, self.resources(moved)], [True, [RES_A]], msg)

    def test_an_unparseable_ledger_refuses_instead_of_replacing_it(self):
        """The write is WHOLE-FILE. A fail-open read of an unparseable ledger
        followed by a whole-dict write does not lose A row, it loses EVERY
        row — the probe-proven roster_for_write defect, one store over."""
        self.claim(RES_A, ALICE, SID_A)
        broken = b"{ this is not json"
        with open(claims_path(), "wb") as f:
            f.write(broken)
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, moved, self.ledger() == broken],
                         [False, [], True], msg)
        # POSITIVE CONTROL: the same call against a PARSEABLE ledger lands.
        self.claim(RES_A, ALICE, SID_A)
        ok2, msg2, moved2 = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok2, self.resources(moved2)], [True, [RES_A]], msg2)

    def test_a_lock_it_cannot_open_refuses_rather_than_writing_unlocked(self):
        """Fail atomically or not at all. `snap` supplies the roster verdict
        so the listedness rung cannot fire first and answer a DIFFERENT
        question than the one this arm asks."""
        self.claim(RES_A, ALICE, SID_A)
        listed = ({BOB: {}}, False)
        gone = os.path.join(self.tmp, "never-created")
        os.environ["HELM_CHAT_DIR"] = gone
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB,
                                                          snap=listed)
        self.assertEqual([ok, moved, os.path.isdir(gone)],
                         [False, [], False], msg)
        os.environ["HELM_CHAT_DIR"] = self.chat
        ok2, msg2, moved2 = seats_claims.rebind_claim_holder(ALICE, BOB,
                                                            snap=listed)
        self.assertEqual([ok2, self.resources(moved2)], [True, [RES_A]], msg2)


class RollbackHolderTest(HolderRebindBase):

    def test_rollback_restores_the_session_and_refuses_a_re_minted_grant(self):
        """The inverse, and the ABA case in one ledger. RES_B's grant is
        released and re-minted under BOB between the rebind and the rollback,
        so its nonce is no longer the one the manifest names: handing it back
        would give ALICE a lease BOB was separately granted."""
        self.claim(RES_A, ALICE, SID_A)
        self.claim(RES_B, ALICE, SID_A)
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, self.resources(moved)],
                         [True, sorted([RES_A, RES_B])], msg)

        row_b = {e["resource"]: e for e in moved}[RES_B]
        self.assertTrue(seats_claims.release(RES_B, BOB,
                                             lease=row_b["lease"])[0])
        reminted = self.claim(RES_B, BOB, SID_B)
        self.assertNotEqual(reminted, row_b["lease"])

        ok2, msg2, back = seats_claims.rollback_claim_holder(ALICE, BOB, moved)
        self.assertEqual([ok2, self.resources(back), self.holders()],
                         [True, [RES_A], {RES_A: ALICE, RES_B: BOB}], msg2)
        self.assertEqual(self.row(RES_A)["session"], SID_A)

    def test_rollback_of_an_empty_manifest_is_not_an_error(self):
        """Nothing moved, so nothing comes back — the same canon as the empty
        rebind. A False here would make every clean no-op rebind print a
        rollback failure at the caller."""
        self.claim(RES_A, ALICE, SID_A)
        ok, msg, back = seats_claims.rollback_claim_holder(ALICE, BOB, [])
        self.assertEqual([ok, back], [True, []], msg)
        # POSITIVE CONTROL, same producer: a real manifest DOES come back.
        _ok, _msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        ok2, msg2, back2 = seats_claims.rollback_claim_holder(ALICE, BOB, moved)
        self.assertEqual([ok2, self.resources(back2), self.holders()],
                         [True, [RES_A], {RES_A: ALICE}], msg2)

    def test_rollback_names_the_same_pair_or_refuses(self):
        """A rollback whose pair does not name a rebind is a call nobody can
        undo; it is refused rather than half-applied."""
        self.claim(RES_A, ALICE, SID_A)
        _ok, _msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        same = seats_claims.rollback_claim_holder(BOB, BOB, moved)
        blank = seats_claims.rollback_claim_holder("", BOB, moved)
        self.assertEqual([same[0], same[2], blank[0], blank[2],
                          self.holders()],
                         [False, [], False, [], {RES_A: BOB}],
                         "same=%r blank=%r" % (same[1], blank[1]))
        ok, msg, back = seats_claims.rollback_claim_holder(ALICE, BOB, moved)
        self.assertEqual([ok, self.resources(back)], [True, [RES_A]], msg)


class HolderExactTest(HolderRebindBase):
    """`holder_exact` says whether the stored holder is the published
    `holder_id` itself. The holder mover decides on the stored holder, so the
    flag is measured against the mover as well as against the bytes."""

    def test_holder_exact_is_false_where_the_scrub_changed_the_stored_holder(self):
        spellings = {RES_A: ALICE, RES_B: ALICE + " ",
                     "worktree:proj:lane-c": ALICE + "​"}
        for resource, spelling in spellings.items():
            self.claim(resource, spelling, SID_A)
        rows = {c["resource"]: c for c in seats_claims.claims_list()}
        self.assertEqual(
            {r: (c["holder_id"], c["holder_exact"]) for r, c in rows.items()},
            {RES_A: (ALICE, True), RES_B: (ALICE, False),
             "worktree:proj:lane-c": (ALICE, False)})
        # THE MOVER AGREES: only the exact row is ALICE's to move.
        ok, msg, moved = seats_claims.rebind_claim_holder(ALICE, BOB)
        self.assertEqual([ok, self.resources(moved)], [True, [RES_A]], msg)
        self.assertEqual([self.row(r)["holder"] for r in spellings],
                         [BOB, ALICE + " ", ALICE + "​"])

    def test_resource_exact_is_false_where_the_scrub_changed_the_stored_resource(self):
        """`resource_exact` is the same signal for the resource. `claim`
        stores a resource as given, so `lane-a ` is a row of its own that
        publishes as `lane-a`, and a join on a stored resource (the
        `helm work list` board) must not read that row."""
        tokens = {r: self.claim(r, ALICE, SID_A)
                  for r in (RES_A, RES_A + " ", RES_B + "​")}
        self.assertEqual(len(set(tokens.values())), 3, "three stored rows")
        self.assertEqual(
            sorted((c["resource"], c["resource_exact"], c["holder_exact"])
                   for c in seats_claims.claims_list()),
            [(RES_A, False, True), (RES_A, True, True), (RES_B, False, True)])


class OwnLeaseJoinTest(HolderRebindBase):
    """`helm chat claims` puts the caller's token on the row that token
    belongs to, and on no other row (task/2528).

    THE DEFECT. Two stored rows can publish one resource and one holder:
    `lane-a` held by `alice-r1`, and `lane-a ` held by `alice-r1 `. The scrub
    and strip make both rows read `lane-a -> alice-r1`. The second row's
    stored holder is not `alice-r1`, so its lease is not the caller's. But the
    table joined the caller's tokens on the published pair, so both rows got
    the first row's token and "(yours)".

    THE DESIGN THESE ARMS PIN. A row carries the caller's token only when its
    stored holder is the caller (`holder_exact`) AND it is the only such row
    under its published resource. Where two of the caller's own rows publish
    one resource, neither row carries a token: the table cannot tell which
    token belongs to which row, so it gives no token. Every row stays in the
    table with its listedness; only the token and "(yours)" are withheld.

    Every arm drives the real doors: `claim` writes the rows, and the
    `helm chat claims` handler renders them in both forms."""

    def setUp(self):
        super().setUp()
        os.environ["HELM_CHAT_NAME"] = ALICE

    def render(self):
        from helm import seats
        out, text, err = io.StringIO(), io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc_json = seats._cmd_claims(["--json"])
        with contextlib.redirect_stdout(text), contextlib.redirect_stderr(err):
            rc_text = seats._cmd_claims([])
        self.assertEqual([rc_json, rc_text], [0, 0], err.getvalue())
        return json.loads(out.getvalue()), text.getvalue()

    @staticmethod
    def shape(rows):
        return sorted((r["resource"], r["holder_id"], r["holder_exact"],
                       r["lease"] or "", r["listedness"]) for r in rows)

    def test_a_padded_row_that_publishes_the_callers_pair_gets_no_token(self):
        mine = self.claim(RES_A, ALICE, SID_A)
        theirs = self.claim(RES_A + " ", ALICE + " ", SID_B)
        rows, text = self.render()
        # MUST-HIT: the two rows really publish one resource and one holder.
        self.assertEqual([(r["resource"], r["holder_id"]) for r in rows],
                         [(RES_A, ALICE)] * 2, "no collision was produced")
        self.assertEqual(self.shape(rows),
                         [(RES_A, ALICE, False, "", "listed"),
                          (RES_A, ALICE, True, mine, "listed")])
        self.assertEqual([text.count(" -> "), text.count("(yours)"),
                          text.count("lease %s (yours)" % mine),
                          text.count(theirs)], [2, 1, 1, 0], text)

    def test_two_of_the_callers_rows_under_one_published_resource_get_none(self):  # noqa: VACUOUS_ASSERTION — the own_leases must-hit and the two rendered rows are unconditional positive controls on the same table
        first = self.claim(RES_A, ALICE, SID_A)
        second = self.claim(RES_A + " ", ALICE, SID_A)
        # MUST-HIT: both rows are really the caller's, each with its own token.
        self.assertEqual(sorted(seats_claims.own_leases(ALICE).items()),
                         sorted({RES_A: first, RES_A + " ": second}.items()))
        rows, text = self.render()
        self.assertEqual(self.shape(rows),
                         [(RES_A, ALICE, True, "", "listed")] * 2)
        self.assertEqual([text.count(" -> "), text.count("(yours)"),
                          text.count(first), text.count(second)],
                         [2, 0, 0, 0], text)

    def test_without_the_callers_own_row_nothing_is_stamped(self):
        theirs = self.claim(RES_A + " ", ALICE + " ", SID_B)
        rows, text = self.render()
        self.assertEqual(self.shape(rows),
                         [(RES_A, ALICE, False, "", "listed")])
        self.assertEqual([text.count(" -> "), text.count("(yours)"),
                          text.count(theirs)], [1, 0, 0], text)

    def test_a_padded_row_under_another_resource_leaves_the_exact_row_stamped(self):
        mine = self.claim(RES_A, ALICE, SID_A)
        self.claim(RES_B + " ", ALICE + " ", SID_B)
        rows, text = self.render()
        self.assertEqual(self.shape(rows),
                         [(RES_A, ALICE, True, mine, "listed"),
                          (RES_B, ALICE, False, "", "listed")])
        self.assertEqual([text.count("(yours)"),
                          text.count("lease %s (yours)" % mine)], [1, 1], text)

    def test_a_publicly_distinct_holder_under_the_same_resource_is_unaffected(self):
        mine = self.claim(RES_A, ALICE, SID_A)
        self.claim(RES_A + " ", BOB, SID_B)
        rows, text = self.render()
        self.assertEqual(self.shape(rows),
                         [(RES_A, ALICE, True, mine, "listed"),
                          (RES_A, BOB, True, "", "listed")])
        self.assertEqual([text.count("(yours)"),
                          text.count("lease %s (yours)" % mine)], [1, 1], text)

    def after_first_claims_read(self, change):
        """Run `change` once, right after the first read of the claims file
        returns, so everything read after it sees the changed ledger. The
        change goes through the real `claim`/`release` doors, whose own reads
        pass straight through."""
        from unittest import mock
        real, fired = pk.read_json, []

        def read_json(path, *args, **kwargs):
            value = real(path, *args, **kwargs)
            if not fired and path == claims_path():
                fired.append(True)
                change()
            return value
        patcher = mock.patch.object(pk, "read_json", side_effect=read_json)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fired

    def test_a_release_and_alias_claim_between_the_reads_cannot_move_a_token(self):  # noqa: VACUOUS_ASSERTION — the absent alias token sits beside the rendered row asserted EQUAL to the caller's own token, an unconditional positive control on the same output
        """task/2541 (review on task/2528): the table's rows and the caller's
        tokens were two reads of the claims file. Between them the caller
        released `lane-a` and claimed `lane-a `, so the rows still showed
        `lane-a` while the tokens held only the alias's, and the join stamped
        the alias's token on a row it never belonged to. Both come from one
        read now, so a row carries the token its own ledger row held."""
        mine = self.claim(RES_A, ALICE, SID_A)
        alias = []

        def change():
            ok, msg = seats_claims.release(RES_A, ALICE, lease=mine,
                                           session=SID_A)
            self.assertTrue(ok, msg)
            alias.append(self.claim(RES_A + " ", ALICE, SID_A))
        fired = self.after_first_claims_read(change)
        out, err = io.StringIO(), io.StringIO()
        from helm import seats
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(seats._cmd_claims(["--json"]), 0, err.getvalue())
        # MUST-HIT: the change ran inside the render and minted a new token.
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(alias), 1)
        self.assertNotEqual(alias[0], mine)
        rows = json.loads(out.getvalue())
        self.assertEqual(self.shape(rows),
                         [(RES_A, ALICE, True, mine, "listed")])
        self.assertNotIn(alias[0], out.getvalue())

    def test_a_single_own_lease_is_stamped_with_its_token(self):
        mine = self.claim(RES_A, ALICE, SID_A)
        rows, text = self.render()
        self.assertEqual(self.shape(rows),
                         [(RES_A, ALICE, True, mine, "listed")])
        self.assertIn("%s -> %s" % (RES_A, ALICE), text)
        self.assertEqual(text.count("lease %s (yours)" % mine), 1, text)


# A process that takes LOCK_EX on the claims lock, says so, and holds it until
# a release file appears. The stopped arm SIGSTOPs it instead, so it never
# looks for the file.
_HOLDER = """import fcntl, os, sys, time
f = open(sys.argv[1], "a")
fcntl.flock(f.fileno(), fcntl.LOCK_EX)
open(sys.argv[2], "w").close()
while not os.path.exists(sys.argv[3]):
    time.sleep(0.01)
"""

# One claims operation in its own process: import, say "armed", wait for "go"
# so every operation starts at the same instant, then time the one call. A
# process and not a thread, because a call stuck in flock cannot be stopped
# from inside the interpreter that is stuck.
_OPERATION = """import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from helm import seats, seats_common
go, armed, call, bound = sys.argv[2:6]
# THE BOUND IS SET HERE, IN THE CHILD: a parent-side patch never reaches it.
seats_common.CLAIM_LOCK_WAIT_S = float(bound)
open(armed, "w").close()
while not os.path.exists(go):
    time.sleep(0.01)
start = time.monotonic()
try:
    out = {"result": eval(call)}
except Exception as exc:
    out = {"error": "%s: %s" % (type(exc).__name__, exc)}
out["elapsed"] = time.monotonic() - start
print(json.dumps(out, default=str))
"""


class ClaimsLockWaitIsBoundedTest(HolderRebindBase):
    """task/3001: every claims-lock caller waits a bounded time, then REFUSES
    and names the holder. No caller ever proceeds without the lock.

    MEASURED on a fab build host before the cure: a real process took LOCK_EX on
    the claims lock and was SIGSTOPped. The strict claim raised in 2.02s, but
    non-strict claim, non-strict release and rebind_claim_sessions were still
    blocked after 20s, because the non-strict door called flock LOCK_EX with
    no deadline. The gate launcher's legacy renewer holds this lock across a
    refresh every 5s, so a stopped launcher hung `helm work claim` and
    `release`.

    THE RESIDUE IS PLANTED, NOT WAITED FOR: a real child takes the lock and is
    stopped, and /proc/locks is read to prove it holds the lock in state T
    before any operation starts. Every operation runs in its own process
    under a timeout well above the bound, so before the cure the arm reports
    BLOCKED instead of hanging the suite.

    THE CONTROL is a LIVE holder that keeps the lock for one full renewer
    period. Every operation must wait for it and succeed. A bound shorter
    than the renewer cadence refuses that holder, which is why the strict
    path's old 2s bound is red here.

    THE ARM RUNS AT A TENTH OF THE PRODUCT'S TIMES. The renewer period and
    the bound are both the product's constants times SCALE, so the bound is
    still two renewer periods; only the wall the arm waits shrinks."""

    SCALE = 0.1

    RES_REFRESH, RES_REBIND = "worktree:proj:refresh", "worktree:proj:rebind"
    RES_ROLLBACK, RES_OLD = "worktree:proj:rollback", "worktree:proj:expired"
    RES_NEW, RES_STRICT = "worktree:proj:new", "worktree:proj:strict"
    SID_C, SID_C2, SID_OLD, SID_NEW = ("carol-session-1", "carol-session-2",
                                       "bob-session-old", "bob-session-new")

    def setUp(self):
        super().setUp()
        from helm import gate
        # The renewer cadence comes from the gate, never a copied number.
        self.renew_s = gate._GATE_LEGACY_RENEW_S
        self.period = self.renew_s * self.SCALE
        self.bound = seats_common.CLAIM_LOCK_WAIT_S * self.SCALE
        self.timeout = 6 * self.period
        self.lock = claims_path() + ".lock"
        self.release_lease = self.claim(RES_A, ALICE, SID_A)
        self.refresh_lease = self.claim(self.RES_REFRESH, BOB, SID_B)
        self.claim(self.RES_REBIND, CAROL, self.SID_C)
        self.claim(self.RES_ROLLBACK, BOB, self.SID_NEW)
        # An EXPIRED row, so claims_list takes its GC leg, the one place a
        # reader takes the lock.
        ledger = pk.read_json(claims_path(), {})
        old = dict(ledger[RES_A])
        old["exp_mono"] = seats_common._now_mono() - 60
        ledger[self.RES_OLD] = old
        pk.write_json(claims_path(), ledger)

    def wait_for(self, predicate, what):
        end = time.monotonic() + self.timeout
        while not predicate():
            if time.monotonic() > end:
                self.fail("timed out after %ds waiting for %s"
                          % (self.timeout, what))
            time.sleep(0.01)

    def spawn_holder(self):
        ready = os.path.join(self.tmp, "holder-ready")
        release = os.path.join(self.tmp, "holder-release")
        holder = subprocess.Popen(
            (sys.executable, "-c", _HOLDER, self.lock, ready, release))
        self.addCleanup(self.reap, holder)
        self.wait_for(lambda: os.path.exists(ready), "the holder to lock")
        return holder, release

    @staticmethod
    def reap(process):
        if process.poll() is None:
            process.kill()
        process.wait()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    def operations(self):
        return {
            "claim": "seats.claim(%r, %r, ttl=600, session='fresh')"
                     % (self.RES_NEW, CAROL),
            "strict-claim": "seats.claim(%r, %r, ttl=600, session='fresh', "
                            "strict=True)" % (self.RES_STRICT, CAROL),
            "refresh": "seats.refresh_claim(%r, %r, lease=%r, session=%r, "
                       "ttl=600)" % (self.RES_REFRESH, BOB, self.refresh_lease,
                                     SID_B),
            "release": "seats.release(%r, %r, lease=%r, session=%r)"
                       % (RES_A, ALICE, self.release_lease, SID_A),
            "rebind": "seats.rebind_claim_sessions(%r, %r, %r)"
                      % (CAROL, self.SID_C, self.SID_C2),
            "rollback": "seats.rollback_claim_sessions(%r, %r, %r, [%r])"
                        % (BOB, self.SID_OLD, self.SID_NEW, self.RES_ROLLBACK),
        }

    def start(self, calls):
        """Start every call in its own process; return once all are running."""
        go = os.path.join(self.tmp, "go")
        procs = {}
        for name, call in calls.items():
            armed = os.path.join(self.tmp, "armed-" + name)
            procs[name] = subprocess.Popen(
                (sys.executable, "-c", _OPERATION, ROOT, go, armed, call,
                 repr(self.bound)),
                env=dict(os.environ), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True)
            self.addCleanup(self.reap, procs[name])
        self.wait_for(lambda: all(
            os.path.exists(os.path.join(self.tmp, "armed-" + name))
            for name in calls), "every operation to import")
        open(go, "w").close()
        return procs

    def collect(self, procs):
        """{name: outcome}. BLOCKED means still inside the call at the arm's
        own timeout, which is the defect."""
        end = time.monotonic() + self.timeout
        out = {}
        for name, proc in procs.items():
            try:
                stdout, stderr = proc.communicate(
                    timeout=max(0.1, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                self.reap(proc)
                out[name] = "BLOCKED"
                continue
            lines = stdout.strip().splitlines()
            out[name] = json.loads(lines[-1]) if proc.returncode == 0 \
                and lines else {"crash": stderr[-2000:]}
        return out

    def test_a_stopped_holder_is_refused_by_name_within_the_bound(self):  # noqa: VACUOUS_ASSERTION — the empty BLOCKED and crash sets sit before unconditional positive controls on the same outcomes: /proc/locks names the stopped holder, and every refusal must contain its pid and the remedy
        holder, _release = self.spawn_holder()
        os.kill(holder.pid, signal.SIGSTOP)
        self.wait_for(lambda: seats_claims._flock_holder(self.lock)
                      == (holder.pid, "T"), "the holder to stop")
        # CONTROL: the residue is really there. A stopped process cannot
        # change its locks, so this reading holds for the whole arm.
        self.assertEqual(seats_claims._flock_holder(self.lock),
                         (holder.pid, "T"))
        before = self.ledger()
        calls = self.operations()
        calls["claims-list"] = "[r['resource'] for r in seats.claims_list()]"
        out = self.collect(self.start(calls))

        self.assertEqual(
            sorted(name for name, got in out.items() if got == "BLOCKED"), [],
            "still blocked after %ds behind STOPPED pid %d: %r"
            % (self.timeout, holder.pid, out))
        self.assertEqual(
            {name: got for name, got in out.items() if "crash" in got}, {})
        named = "STOPPED pid %d (state T)" % holder.pid
        remedy = "kill -CONT %d" % holder.pid
        bound = self.bound
        # The strict door and the two session movers REFUSE BY RAISING; the
        # tuple doors refuse with (False, message), as for any other refusal.
        raising = ("strict-claim", "rebind", "rollback")
        for name in ("claim", "refresh", "release") + raising:
            with self.subTest(operation=name):
                got = out[name]
                if name in raising:
                    text = got.get("error", "")
                    self.assertTrue(text.startswith("OSError: "), got)
                else:
                    self.assertIs(got.get("result", [True])[0], False, got)
                    text = got["result"][1]
                self.assertIn(named, text)
                self.assertIn(remedy, text)
                # It WAITED the bound (a live holder gets that long) and then
                # stopped waiting.
                self.assertGreaterEqual(got["elapsed"], bound * 0.99)
                self.assertLess(got["elapsed"], bound + self.period)
        self.assertIsNone(out["claim"]["result"][2])
        # The GC leg only tidies: every writer persists its own sweep. So it
        # does not wait, and it never writes without the lock.
        self.assertIn(RES_A, out["claims-list"]["result"])
        self.assertNotIn(self.RES_OLD, out["claims-list"]["result"])
        self.assertLess(out["claims-list"]["elapsed"], bound / 2)

        # NOTHING PROCEEDED WITHOUT THE LOCK: once the holder is gone the
        # ledger is byte-identical, so the refused release left its lease.
        self.reap(holder)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(seats_claims.own_leases(ALICE),
                         {RES_A: self.release_lease})

    def test_a_live_holder_for_one_renewer_period_is_waited_for(self):  # noqa: VACUOUS_ASSERTION — the empty failure set is followed by unconditional positive controls on the same outcomes: four True results, the moved rebind row, one rollback, and an elapsed above the renewer period
        holder, release = self.spawn_holder()
        procs = self.start(self.operations())
        # Every operation is now inside its call, behind a holder that is
        # running and keeps the lock for one full renewer period.
        time.sleep(self.period)
        self.assertIsNone(holder.poll(), "the holder let go early")
        held = seats_claims._flock_holder(self.lock)
        self.assertEqual(held[0], holder.pid)
        self.assertNotIn(held[1], ("T", "t", "D", "Z"))
        open(release, "w").close()
        out = self.collect(procs)
        self.assertEqual(
            {name: got for name, got in out.items()
             if got == "BLOCKED" or "crash" in got or "error" in got}, {},
            "a LIVE holder was refused or waited on forever: %r" % out)
        self.assertEqual(
            [out[name]["result"][0] for name in
             ("claim", "strict-claim", "refresh", "release")],
            [True] * 4, "a LIVE holder was refused: %r" % out)
        self.assertEqual(out["rebind"]["result"], [self.RES_REBIND])
        self.assertEqual(out["rollback"]["result"], 1)
        for name, got in out.items():
            with self.subTest(operation=name):
                # MUST-HIT: it really waited behind the holder.
                self.assertGreater(got["elapsed"], self.period * 0.9)
        # THE BOUND, stated as the relation that keeps a live holder safe:
        # on the product's own constants, and on the scaled pair this arm ran.
        self.assertGreaterEqual(seats_common.CLAIM_LOCK_WAIT_S,
                                2 * self.renew_s)
        self.assertGreaterEqual(self.bound, 2 * self.period)


if __name__ == "__main__":
    unittest.main()
