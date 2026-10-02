#!/usr/bin/env python3
"""helm task handoff — the HOLDER gives its own row to a known seat (task/2309).

THE DEFECT THIS MEASURES. A live holder could not hand its OWN row to another
seat: `helm task claim` by the recipient is refused (an incumbent cannot be
displaced), and `helm task update --owner` by the holder is refused by the
same incumbent guard. The guard could not tell a GIFT (the holder gives) from
a SEIZURE (somebody else takes). Measured live on 09-11, 09-14 (18 rows of a
retiring seat), 09-23 and twice on 09-30.

THE DOOR IS ONE CAPABILITY AT THE ONE GUARD. `handoff` mints a holder-only
authorization (takeover.HolderHandoffAuthorization) and the incumbent guard in
tasks.update admits it, beside the BUILD takeover and seat-reassign proofs.
The caller's identity is task/1918's act-door rule: a declared name counts
only when seats_roster.seat_for_session resolves the presented session to the
SAME seat. It is never keyed on a session id recorded on a row.

EVERY "NOTHING WAS WRITTEN" IS MEASURED ON A COUNTER PROVEN TO COUNT: each arm
pins the ledger's event count to the rows its fixture filed before it reads
the count again.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import home, pk, seats, takeover, tasks  # noqa: E402
from tests.test_tasks import CliBase  # noqa: E402


class HandoffBase(CliBase):
    """A ledger, a holder, and a roster that knows the recipient."""

    def rostered(self, *names):
        """Put roster rows for `names` WITHOUT touching this process's
        identity, so a recipient is a known seat while the caller stays
        whoever the arm made it."""
        rpath = seats.roster_path()
        rows = pk.read_json(rpath, {}) or {}
        for name in names:
            rows[name] = dict(rows.get(name) or {},
                              session="sess-for-%s" % name)
        os.makedirs(os.path.dirname(rpath), exist_ok=True)
        pk.atomic_write(rpath, json.dumps(rows))

    def held(self, title, owner="seat-a", **kw):
        row, err = tasks.add(title, owner, path=tasks.ledger_path(),
                             force_new=True, **kw)
        self.assertIsNone(err, "fixture add refused: %s" % err)
        return row

    def now(self, tid):
        return tasks.get(tid, path=tasks.ledger_path())

    def events(self, tid):
        """Every snapshot the ledger holds for `tid`, oldest first — the
        row's history, read raw rather than through the fold."""
        return [ev for ev in self.lines(tasks.ledger_path())
                if ev.get("id") == tid]


class HandoffGiftTest(HandoffBase):

    def test_the_holders_gift_moves_the_row_and_its_history_records_it(self):
        self.admit("seat-a")
        self.rostered("seat-b")
        tid = self.held("a row the integrator filed and then delegated",
                        status="in_progress")["id"]
        quiet_since = self.now(tid)["last_updated"]
        self.assertEqual(self.ledger_lines(), 1)

        rc, out, err = self.cli("handoff", tid, "--to", "seat-b",
                                "--note", "seat-b built the detector")
        self.assertEqual(rc, 0, err)
        self.assertIn(tid, out)
        self.assertIn("seat-b", out)

        got = self.now(tid)
        self.assertEqual(got["owner"], "seat-b")
        # AN OWNER MOVE ONLY: a gift says who holds the row, never that the
        # recipient has started it, so the status stays what it was.
        self.assertEqual(got["status"], "in_progress")
        # ONE event, and it is custody rather than activity (the reassign
        # and release rule): stalebot reads last_updated.
        self.assertEqual(self.ledger_lines(), 2)
        self.assertEqual(got["last_updated"], quiet_since)
        self.assertIn("custody_updated", got)

        # THE ROW'S HISTORY SHOWS THE GIFT (task/305): the snapshot before
        # it names the holder, the one after it carries the event.
        history = self.events(tid)
        self.assertEqual([ev["owner"] for ev in history], ["seat-a", "seat-b"])
        gift = history[-1]["takeover"]
        self.assertEqual(
            {k: gift[k] for k in ("kind", "from", "to", "by", "session",
                                  "owner_was", "status_was", "note", "pid",
                                  "ppid")},
            {"kind": "holder-handoff", "from": "seat-a", "to": "seat-b",
             "by": "seat-a", "session": "sess-for-seat-a",
             "owner_was": "seat-a", "status_was": "in_progress",
             "note": "seat-b built the detector", "pid": os.getpid(),
             "ppid": os.getppid()})

        # `show` says who gave it to whom, from what status, and why.
        rc, out, err = self.cli("show", tid)
        self.assertEqual(rc, 0, err)
        self.assertIn("handed off", out)
        self.assertIn("seat-a -> seat-b", out)
        self.assertIn("seat-b built the detector", out)

        # THE SESSION IS AUDIT, NOT WIRE: it is the bearer that corroborates
        # a declared name, so the store keeps it and every JSON door drops it.
        rc, out, err = self.cli("show", tid, "--json")
        self.assertEqual(rc, 0, err)
        self.assertIn('"kind": "holder-handoff"', out)
        self.assertNotIn("sess-for-seat-a", out)
        self.assertNotIn("session", json.loads(out)["takeover"])

        # THE GIVER IS NOT A HOLDER ANY MORE (the second killed reclaim
        # design: an author taking a row back after an authorized transfer).
        rc, out, err = self.cli("handoff", tid, "--to", "seat-a")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("held by seat-b", err)
        self.assertEqual(self.ledger_lines(), 2)
        self.assertEqual(self.now(tid)["owner"], "seat-b")

    def test_an_open_row_stays_open_and_goes_to_the_named_seat(self):
        self.admit("seat-a")
        self.rostered("seat-b")
        tid = self.held("assigned, never started")["id"]
        rc, _out, err = self.cli("handoff", tid, "--to", "seat-b")
        self.assertEqual(rc, 0, err)
        got = self.now(tid)
        self.assertEqual((got["owner"], got["status"]), ("seat-b", "open"))
        self.assertIsNone(got["takeover"]["note"])
        self.assertEqual(got["takeover"]["status_was"], "open")


class HandoffRefusalTest(HandoffBase):

    def test_a_non_holder_is_refused_and_a_seizure_stays_refused(self):
        # THE FILER IS NOT THE HOLDER, and the filer's recorded session is
        # not custody: seat-a filed this row for kimi, from seat-a's own
        # session. Two of the three killed reclaim designs keyed on exactly
        # those facts, so a door keyed on them would hand this row over.
        self.admit("seat-a")
        self.rostered("seat-b", "kimi")
        row = self.held("work filed for kimi", owner="kimi",
                        source="seat-a")
        self.assertEqual(row["source"], "seat-a")
        before = self.ledger_lines()
        self.assertEqual(before, 1)

        rc, out, err = self.cli("handoff", row["id"], "--to", "seat-b")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("held by kimi", err)
        self.assertIn("not by seat-a", err)
        self.assertIn("seizure", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.now(row["id"])["owner"], "kimi")

        # POSITIVE CONTROL ON THE SAME ROW: its holder can give it.
        self.admit("kimi")
        rc, _out, err = self.cli("handoff", row["id"], "--to", "seat-b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.now(row["id"])["owner"], "seat-b")

    def test_an_unowned_row_is_refused_with_the_claim_door(self):
        self.admit("seat-a")
        self.rostered("seat-b")
        tid = self.held("in the pool", owner=None)["id"]
        rc, out, err = self.cli("handoff", tid, "--to", "seat-b")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("UNOWNED", err)
        self.assertIn("nothing to hand off", err)
        self.assertIn("helm task claim", err)
        self.assertEqual(self.ledger_lines(), 1)

    def test_an_unknown_seat_is_refused(self):
        self.admit("seat-a")
        tid = self.held("a row for a seat nobody rostered")["id"]
        self.assertEqual(self.ledger_lines(), 1)
        rc, out, err = self.cli("handoff", tid, "--to", "ghost-seat")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("no seat 'ghost-seat' is on the roster", err)
        self.assertEqual(self.ledger_lines(), 1)
        self.assertEqual(self.now(tid)["owner"], "seat-a")
        # POSITIVE CONTROL: the same row goes once the seat is rostered.
        self.rostered("ghost-seat")
        rc, _out, err = self.cli("handoff", tid, "--to", "ghost-seat")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.now(tid)["owner"], "ghost-seat")

    def test_a_closed_row_is_refused(self):
        self.admit("seat-a")
        self.rostered("seat-b")
        tid = self.held("finished long ago", status="closed",
                        closed_reason="landed")["id"]
        self.assertEqual(self.ledger_lines(), 1)
        rc, out, err = self.cli("handoff", tid, "--to", "seat-b")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("CLOSED", err)
        self.assertEqual(self.ledger_lines(), 1)
        self.assertEqual(self.now(tid)["owner"], "seat-a")

    def test_an_identity_the_roster_does_not_resolve_hands_off_nothing(self):  # noqa: VACUOUS_ASSERTION — every empty stdout sits beside rc 2 and a named refusal on the same call, and the positive control at the end lands on the same row and counter
        self.rostered("seat-b")
        tid = self.held("a row with a careful holder",
                        status="in_progress")["id"]
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        refusals = []

        # NO IDENTITY AT ALL.
        self.unidentify()
        refusals.append(self.cli("handoff", tid, "--to", "seat-b"))
        # DECLARED, NO SESSION: a name any process can export. The release
        # door admits this (acting_author); a GIFT to a named seat does not.
        os.environ["HELM_CHAT_NAME"] = "seat-a"
        refusals.append(self.cli("handoff", tid, "--to", "seat-b"))
        # DECLARED WITH A SESSION NO ROSTER HAS SEEN: an invented value.
        for key in home._SESSION_ENV:
            os.environ[key] = "sess-invented-on-the-spot"
        refusals.append(self.cli("handoff", tid, "--to", "seat-b"))
        # DISPUTED: seat-a declared, the session rostered to seat-b.
        self.admit("seat-b")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-a"}):
            refusals.append(self.cli("handoff", tid, "--to", "seat-b"))

        for rc, out, err in refusals:
            with self.subTest(err=err):
                self.assertEqual(rc, 2)
                self.assertEqual(out, "")
                self.assertIn("refusing to hand off", err)
                self.assertIn("NOTHING WAS HANDED OFF", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.now(tid)["owner"], "seat-a")

        # POSITIVE CONTROL: the same row goes once the roster resolves the
        # session to the holder.
        self.admit("seat-a")
        rc, _out, err = self.cli("handoff", tid, "--to", "seat-b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ledger_lines(), 2)

    def test_giving_a_row_to_its_own_holder_is_a_loud_no_op(self):
        self.admit("seat-a")
        tid = self.held("already mine")["id"]
        rc, out, err = self.cli("handoff", tid, "--to", "seat-a")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("already held by seat-a", err)
        self.assertEqual(self.ledger_lines(), 1)

    def test_the_cli_refuses_what_it_does_not_implement(self):
        self.admit("seat-a")
        self.rostered("seat-b")
        tid = self.held("a row for argv abuse")["id"]
        self.assertEqual(self.ledger_lines(), 1)
        for argv in (("handoff",),
                     ("handoff", tid),
                     ("handoff", tid, "--to"),
                     ("handoff", "--to", "seat-b"),
                     ("handoff", tid, "--to", "seat-b", "--note"),
                     ("handoff", tid, "--to", "seat-b", "stray"),
                     ("handoff", tid, "--to", "seat-b", "--force"),
                     ("handoff", tid, "--to", "seat-b", "--owner", "x"),
                     ("handoff", tid, "--to", "seat-b", "--to", "seat-c"),
                     ("handoff", tid, "--to", "seat-b", "--project", "p")):
            with self.subTest(argv=argv):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 2)
                # THE VERB'S OWN REFUSAL, not the unknown-verb usage text,
                # which would pass this arm on a tree with no hand-off door.
                self.assertIn("helm task handoff", err)
                self.assertEqual(out, "")  # noqa: VACUOUS_ASSERTION — err on the same call is pinned non-empty one line up
        self.assertEqual(self.ledger_lines(), 1)
        self.assertEqual(self.now(tid)["owner"], "seat-a")


class ExistingDoorsTest(HandoffBase):

    def test_claim_and_update_owner_still_refuse_and_name_the_handoff(self):  # noqa: VACUOUS_ASSERTION — each empty stdout sits beside rc 2 and the incumbent refusal text on the same call; the handoff arms are the positive control for the row moving
        """THE SEIZURE DOORS DO NOT MOVE. The holder asking through `claim
        --owner` or `update --owner`, and another seat claiming, are refused
        as before; the refusal now names the holder's own door."""
        self.admit("seat-a")
        self.rostered("seat-b")
        tid = self.held("a row its holder wants to give",
                        status="in_progress")["id"]
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        for argv in (("claim", tid, "--owner", "seat-b"),
                     ("update", tid, "--owner", "seat-b")):
            with self.subTest(argv=argv):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 2)
                self.assertEqual(out, "")
                self.assertIn("held by seat-a", err)
                self.assertIn("raw force cannot transfer or clear an "
                              "incumbent", err)
                self.assertIn("helm task handoff", err)
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-b"}):
            for key in home._SESSION_ENV:
                os.environ[key] = "sess-for-seat-b"
            rc, _out, err = self.cli("claim", tid)
        self.assertEqual(rc, 2)
        self.assertIn("held by seat-a", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.now(tid)["owner"], "seat-a")


class HandoffCapabilityTest(HandoffBase):
    """takeover.HolderHandoffAuthorization — the capability the door mints."""

    def test_the_capability_is_minted_never_built(self):
        with self.assertRaises(takeover.TakeoverRefused):
            takeover.HolderHandoffAuthorization()
        with self.assertRaises(takeover.TakeoverRefused):
            takeover.HolderHandoffAuthorization(mint=object())

    def test_a_name_is_not_an_actor_and_a_non_holder_cannot_mint(self):
        actor = self.admit("seat-a")
        held = self.held("mine")
        theirs = self.held("theirs", owner="kimi")
        auth, err = takeover.mint_holder_handoff(
            held["id"], held, "seat-a", "seat-b", {})
        self.assertIsNone(auth)
        self.assertIn("admitted actor", err)
        auth, err = takeover.mint_holder_handoff(
            theirs["id"], theirs, actor, "seat-b", {})
        self.assertIsNone(auth)
        self.assertIn("held by kimi", err)
        # POSITIVE CONTROL: the holder's own actor mints for its own row.
        auth, err = takeover.mint_holder_handoff(
            held["id"], held, actor, "seat-b", {"kind": "holder-handoff"})
        self.assertIsNone(err, err)
        self.assertIsInstance(auth, takeover.HolderHandoffAuthorization)

    def test_one_proof_moves_one_row_to_one_seat_from_one_snapshot(self):
        actor = self.admit("seat-a")
        path = tasks.ledger_path()
        row = self.held("the row the proof was minted for")
        other = self.held("a different row the same seat holds")

        def mint(prev):
            auth, err = takeover.mint_holder_handoff(
                prev["id"], prev, actor, "seat-b", {"kind": "holder-handoff"})
            self.assertIsNone(err, err)
            return auth

        auth = mint(row)
        for tid, fields in ((other["id"], {"owner": "seat-b"}),
                            (row["id"], {"owner": "seat-c"}),
                            (row["id"], {"owner": "seat-b",
                                         "status": "in_progress"})):
            with self.subTest(tid=tid, fields=fields):
                got, err = tasks.update(tid, path=path, takeover_auth=auth,
                                        **fields)
                self.assertIsNone(got)
                self.assertIn("compare-and-swap", err)
        # A ROW THAT MOVED AFTER THE MINT is not the row the proof names.
        _c, err = tasks.comment(row["id"], "moved on", by="seat-a",
                                path=path)
        self.assertIsNone(err, err)
        got, err = tasks.update(row["id"], path=path, takeover_auth=auth,
                                owner="seat-b")
        self.assertIsNone(got)
        self.assertIn("compare-and-swap", err)
        # POSITIVE CONTROL: a fresh proof over the current row lands.
        got, err = tasks.update(row["id"], path=path,
                                takeover_auth=mint(self.now(row["id"])),
                                owner="seat-b")
        self.assertIsNone(err, err)
        self.assertEqual(got["owner"], "seat-b")


if __name__ == "__main__":
    unittest.main()
