#!/usr/bin/env python3
"""What a seat NAME means after the seat stops answering to it.

An open row stores its holder as a plain string and every consumer compares
holders by exact canonical-token equality, so a rename or a departure leaves
rows addressed to a name with no reader. Three answers are possible and the
tree needs all three kept apart: a live row answers to the name (CURRENT), the
rename lineage names a successor (RENAMED), or nothing answers to it at all
(ORPHANED). Anything that could not be measured is UNKNOWN and never ORPHANED,
because a banner built on an unreadable roster is the loudest claim available
resting on the weakest evidence.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `helm.seat` rides along because `seat_launch_assets` is an IMPL module:
# the facade-injection rule is that nothing imports one without the
# facade in the same or an enclosing scope, so the patch door every
# sibling shares reaches this module too.
from helm import landreq, pk, seat, seat_launch_assets  # noqa: E402,F401
from helm import seat_reassign  # noqa: E402
from helm import seats_claims, seats_lineage, seats_roster  # noqa: E402

ENV_KEYS = ("HELM_HOME", "HELM_CHAT_DIR", "HELM_CHAT_NAME", "HELM_CHAT_ROOM",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_OWNER_NAMES", "HELM_ADOPTED_DIR",
            "MELD_HOME", "MELD_CHAT_DIR", "MELD_CHAT_NAME")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-lineage-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        self.addCleanup(self._restore)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_OWNER_NAMES"] = "owner"
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")

    def _restore(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def plant(self, rows):
        pk.write_json(seats_roster.roster_path(), rows)

    def key(self, name):
        from helm.seats_common import _seat_key
        return _seat_key(name)


class LineageTest(Base):
    def test_a_live_row_answers_to_its_own_name(self):
        self.plant({"alpha": {"session": "s-alpha"}})
        name, state, why = seats_lineage.seat_lineage("alpha")
        self.assertEqual((name, state), ("alpha", seats_lineage.SEAT_CURRENT))
        self.assertIn("live roster row", why)

    def test_lineage_names_the_successor_when_nothing_answers_to_the_name(self):
        self.plant({"beta": {"session": "s", "seat_keys": [self.key("alpha"),
                                                           self.key("beta")]}})
        name, state, why = seats_lineage.seat_lineage("alpha")
        self.assertEqual((name, state), ("beta", seats_lineage.SEAT_RENAMED))
        self.assertIn("beta", why)

    def test_a_live_seat_outranks_a_predecessors_lineage(self):
        """THE RECYCLING ARM, and the reason lineage is asked SECOND.

        A rename leaves the old key on the successor's row permanently, while
        the roster hands a released name back out to a later seat. Once that
        happens the same key is both a LIVE seat and a historical alias, and a
        resolver that walked lineage first would answer with the predecessor's
        successor while the live seat sat in front of it — pointing a relaunch,
        and every row this name holds, at a different agent entirely.
        """
        self.plant({"alpha": {"session": "s-new"},
                    "beta": {"session": "s", "seat_keys": [self.key("alpha"),
                                                           self.key("beta")]}})
        name, state, _why = seats_lineage.seat_lineage("alpha")
        self.assertEqual((name, state), ("alpha", seats_lineage.SEAT_CURRENT),
                         "a live seat was resolved to somebody else's row")

    def test_a_name_with_no_row_and_no_lineage_is_orphaned(self):
        self.plant({"alpha": {"session": "s"}})
        name, state, why = seats_lineage.seat_lineage("gamma")
        self.assertIsNone(name)
        self.assertEqual(state, seats_lineage.SEAT_ORPHANED)
        self.assertIn("gamma", why)

    def test_malformed_lineage_on_any_row_is_unknown_not_orphaned(self):
        """Fail closed on the row that could not be READ, not only on a row
        that matched: a walk that skipped it never proved the key absent from
        it."""
        self.plant({"alpha": {"session": "s", "seat_keys": "not-a-list"},
                    "beta": {"session": "t"}})
        name, state, why = seats_lineage.seat_lineage("gamma")
        self.assertIsNone(name)
        self.assertEqual(state, seats_lineage.SEAT_LINEAGE_UNKNOWN)
        self.assertIn("malformed", why)

    def test_a_key_claimed_by_two_rows_is_unknown(self):
        self.plant({"beta": {"session": "s",
                             "seat_keys": [self.key("alpha")]},
                    "gamma": {"session": "t",
                              "seat_keys": [self.key("alpha")]}})
        name, state, why = seats_lineage.seat_lineage("alpha")
        self.assertIsNone(name)
        self.assertEqual(state, seats_lineage.SEAT_LINEAGE_UNKNOWN)
        self.assertIn("may not guess", why)

    def test_an_unreadable_roster_is_unknown(self):
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            name, state, why = seats_lineage.seat_lineage("alpha")
        self.assertIsNone(name)
        self.assertEqual(state, seats_lineage.SEAT_LINEAGE_UNKNOWN)
        self.assertIn("unavailable", why)

    def test_an_empty_name_asks_nothing(self):
        name, state, _why = seats_lineage.seat_lineage("")
        self.assertIsNone(name)
        self.assertEqual(state, seats_lineage.SEAT_LINEAGE_UNKNOWN)


class LaunchIdentityTest(Base):
    """The launcher reads the SAME resolver, and two of its three states the
    same way — a live seat and an unknown name both launch as themselves."""

    def test_a_renamed_asset_launches_as_its_successor(self):
        self.plant({"beta": {"session": "s", "seat_keys": [self.key("alpha"),
                                                           self.key("beta")]}})
        name, err = seat_launch_assets._launch_identity("alpha")
        self.assertIsNone(err)
        self.assertEqual(name, "beta")

    def test_a_live_seat_launches_as_itself_even_beside_a_stale_alias(self):
        self.plant({"alpha": {"session": "s-new"},
                    "beta": {"session": "s", "seat_keys": [self.key("alpha"),
                                                           self.key("beta")]}})
        name, err = seat_launch_assets._launch_identity("alpha")
        self.assertIsNone(err)
        self.assertEqual(name, "alpha")

    def test_an_unknown_name_launches_as_itself(self):
        self.plant({"alpha": {"session": "s"}})
        name, err = seat_launch_assets._launch_identity("gamma")
        self.assertIsNone(err)
        self.assertEqual(name, "gamma")

    def test_an_unprovable_lineage_refuses(self):
        self.plant({"alpha": {"session": "s", "seat_keys": 7}})
        name, err = seat_launch_assets._launch_identity("gamma")
        self.assertIsNone(name)
        self.assertIn("malformed", err)


class RenameCarriesHoldingsTest(Base):
    """A rename moves the roster row; it has never moved the OBLIGATIONS that
    spell the old name. `seat reassign` is the door for those and nothing
    invoked it, so these arms are about the CALL: that it happens, what it is
    handed, and that it can never take the rename down with it."""

    def spy(self, rc=0, lines=("helm seat reassign: done",), boom=None):
        calls = []

        def fake(source, to, **kw):
            calls.append((source, to, kw))
            if boom:
                raise boom
            return rc, list(lines)
        return calls, fake

    def held(self, seat):
        """Make the source genuinely non-empty THROUGH THE SHIPPED PRODUCER,
        so the proven-empty shortcut cannot silently swallow the arm."""
        ok, msg, _lease = seats_claims.claim("resource-one", seat)
        self.assertTrue(ok, msg)
        man, unread = seat_reassign.holdings(seat)
        self.assertFalse(unread, unread)
        self.assertTrue(sum(len(v or ()) for v in man.values()),
                        "the fixture planted no holdings, so any later "
                        "assertion about carrying them proves nothing")

    def test_a_proven_empty_source_never_calls_the_door(self):
        """THE POSITIVE CONTROL IS THE SAME SPY ON THE SAME RENAME, because
        "the door was not called" is worth nothing from a fixture where it
        would never have been called. Holdings first: the spy fires. Then the
        empty source, where it must not."""
        calls, fake = self.spy()
        self.plant({"alpha": {"session": "s-alpha", "sessions": ["s-alpha"]}})
        self.held("alpha")
        with mock.patch.object(seat_reassign, "reassign", fake):
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 1, "the spy never fires at all, so the "
                                        "negative arm below proves nothing")
        # ONE SPY ACROSS BOTH RENAMES, so the list asserted unchanged is the
        # same list that just proved it can grow. A second spy would put the
        # control on a different object from the claim.
        self.plant({"gamma": {"session": "s-gamma", "sessions": ["s-gamma"]}})
        with mock.patch.object(seat_reassign, "reassign", fake):
            ok, msg = seats_roster.rename_seat("gamma", "delta")
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 1, "a four-ledger mutating verb ran to "
                                        "move zero rows")
        self.assertIn("Nothing open was still spelled gamma.", msg,
                      "the message names a token no holder field stores")

    def test_the_door_is_handed_the_old_NAME_and_never_the_session(self):
        """The silent no-op this wiring is one argument away from. The
        resolver behind the door asks SESSION BEFORE NAME, so a session would
        resolve to whoever holds it NOW — which after a rename is the TARGET —
        and the door would answer 'already holds these' and move nothing, at a
        failing exit code, on a rename that had work to carry."""
        self.plant({"alpha": {"session": "s-alpha", "sessions": ["s-alpha"]}})
        self.held("alpha")
        calls, fake = self.spy()
        with mock.patch.object(seat_reassign, "reassign", fake):
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 1, "the door was not invoked")
        source, to, kw = calls[0]
        # THE BARE NAME, and neither of the other two spellings this module
        # keeps for the same seat: not the SESSION (which resolves to whoever
        # holds it now — after a rename, the target) and not the seat KEY
        # (which is what `seat_keys` stores, and which no holder field on a
        # dispatch, task or lease has ever contained).
        self.assertEqual(source, "alpha")
        self.assertNotEqual(source, "s-alpha")
        self.assertNotEqual(source, self.key("alpha"))
        self.assertEqual(to, "beta")
        self.assertTrue(kw.get("apply"), "a dry run carries nothing")

    def test_the_door_is_never_forced(self):
        """Its one must-miss refuses a measurably LIVE source. An automatic
        caller that overrode it would turn that guard into a formality, so a
        refusal here stands and is reported instead."""
        self.plant({"alpha": {"session": "s-alpha", "sessions": ["s-alpha"]}})
        self.held("alpha")
        calls, fake = self.spy(rc=1, lines=["helm seat reassign: REFUSED — x"])
        with mock.patch.object(seat_reassign, "reassign", fake):
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, "a refused carry took the rename down with it")
        self.assertEqual(len(calls), 1, "the door was not invoked, so asking "
                                        "what it was handed proves nothing")
        # THE POSITIVE HALF OF THE SAME OBSERVABLE: these kwargs ARE passed, so
        # an empty kwargs dict cannot be what makes `force` read as absent.
        self.assertTrue(calls[0][2].get("apply"))
        self.assertIn("alpha", calls[0][2].get("reason") or "")
        self.assertFalse(calls[0][2].get("force"))
        self.assertIn("NOT fully carried", msg)
        self.assertIn("REFUSED", msg)
        self.assertIn("helm seat reassign", msg)

    def test_a_door_that_raises_leaves_the_rename_done_and_says_UNKNOWN(self):
        """The row has already moved and the lock is already released, so
        False here would be a lie about what happened — and 'nothing moved' is
        the one reading that would stop an operator from checking."""
        self.plant({"alpha": {"session": "s-alpha", "sessions": ["s-alpha"]}})
        _calls, fake = self.spy(boom=RuntimeError("ledger on fire"))
        with mock.patch.object(seat_reassign, "holdings",
                               side_effect=RuntimeError("ledger on fire")):
            with mock.patch.object(seat_reassign, "reassign", fake):
                ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        self.assertIn("beta", seats_roster.roster())
        self.assertIn("UNKNOWN", msg)
        self.assertIn("ledger on fire", msg)

    def test_a_clean_carry_reports_a_counted_number(self):
        """CONTROL: a real lease under the old name really moves, and the
        count the message prints is the one the mover recorded."""
        self.rostered("alpha", "s-alpha-2529")
        ok, why, _lease = seats_claims.claim("resource-one", "alpha")
        self.assertTrue(ok, why)
        ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        self.assertEqual(
            pk.read_json(seats_claims.claims_path(), {})["resource-one"]
            ["holder"], "beta", msg)
        audit = self.audit()
        self.assertIsNotNone(audit, "the carry never ran the verb: " + msg)
        self.assertEqual(len(audit["moved"]["leases"]), 1, audit)
        self.assertIn("Carried 1 open holding from alpha", msg)
        self.assertNotIn("counted", msg)
        self.assertNotIn("already belonged", msg)

    def test_a_door_that_reports_no_counts_reads_UNKNOWN(self):
        """CONTROL: rc 0 says nothing was left behind, not how much moved. A
        door that fills no result has not said, so no number is printed."""
        self.plant({"alpha": {"session": "s-alpha", "sessions": ["s-alpha"]}})
        self.held("alpha")
        calls, fake = self.spy(rc=0)
        with mock.patch.object(seat_reassign, "reassign", fake):
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 1, "the door was not invoked")
        self.assertIn("HOW MANY moved is UNKNOWN", msg)
        self.assertNotIn("Carried ", msg)

    def rostered(self, name, session):
        """A roster row written by the real roster writer, AS that seat."""
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": name}):
            seats_roster.write_roster(name, session=session, cwd=self.tmp,
                                      home_room="main")
        self.assertIn(name, seats_roster.roster(),
                      "fixture: the roster writer did not admit %r" % name)

    @staticmethod
    def audit():
        """The reassign event the rename's carry recorded, as its auditor
        reads it. Its presence proves the verb ran with apply rather than the
        proven-empty shortcut."""
        from helm import eventledger
        rows, unavailable = eventledger.checked_events(
            seat_reassign.ledger_path())
        return None if unavailable or not rows else list(rows)[-1]

    def dispatch_repo(self):
        """(repo, tip) — a one-commit git repo pinned as this fixture's home,
        so the real dispatch writer admits rows against it."""
        from tests._tmphome import pin_dispatch_home
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        for cmd in (("init", "-q"), ("config", "user.email", "t@t"),
                    ("config", "user.name", "t")):
            subprocess.run(("git",) + cmd, cwd=repo, capture_output=True)
        with open(os.path.join(repo, "a.txt"), "w") as fh:
            fh.write("a\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "a"], cwd=repo,
                       capture_output=True)
        tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                             capture_output=True, text=True).stdout.strip()
        self.assertTrue(tip, "fixture: the temp repo has no commit")
        pin_dispatch_home(self, repo)
        return repo, tip

    def test_a_carry_that_moved_nothing_does_not_report_the_census(self):
        """task/2529. The census matches the old name canonically, so a lease
        stored exactly as the NEW name is listed under the old one. The lease
        mover matches the stored holder byte for byte and moves nothing, and
        the verb exits 0 because that lease already belongs to the target. The
        rename message printed the census count as "Carried 1" about a carry
        that moved nothing."""
        self.rostered("Worker", "s-worker-2529")
        ok, why, _lease = seats_claims.claim("a-live-resource", "worker")
        self.assertTrue(ok, why)
        claims = seats_claims.claims_path()
        before = pk.read_json(claims, {})["a-live-resource"]
        self.assertEqual(before["holder"], "worker")
        man, unread = seat_reassign.holdings("Worker")
        self.assertEqual(unread, [])
        self.assertEqual([r["resource"] for r in man["leases"]],
                         ["a-live-resource"],
                         "fixture: the census does not count the lease under "
                         "the old name, so there is no count to overstate")
        ok, msg = seats_roster.rename_seat("Worker", "worker")
        self.assertTrue(ok, msg)
        after = pk.read_json(claims, {})["a-live-resource"]
        for field in ("holder", "lease", "fence"):
            self.assertEqual(after[field], before[field], msg)
        # THE MOVER'S OWN RECORD: it ran, moved no lease, refused nothing and
        # left nothing behind, so the verb exited 0 with nothing carried.
        audit = self.audit()
        self.assertIsNotNone(audit, "the carry never ran the verb: " + msg)
        self.assertEqual(audit["moved"]["leases"], [], audit)
        self.assertFalse(any(audit["refused"].values()), audit)
        self.assertEqual(set(audit["remaining"].values()), {0}, audit)
        self.assertNotIn("NOT fully carried", msg)
        self.assertNotIn("Carried 1 open holding", msg)
        self.assertIn("Carried 0 open holdings from Worker", msg)
        self.assertIn("counted 1 open under that spelling", msg)

    def test_an_unreadable_opening_census_refuses_to_report_a_count(self):
        """CONTROL: the rename's own census could not read a surface, so how
        much was open under the old name is UNKNOWN and no census number is
        printed. The verb takes its own census under the lock, so what it
        moved is still its count to report."""
        self.rostered("alpha", "s-alpha-2529")
        ok, why, _lease = seats_claims.claim("resource-one", "alpha")
        self.assertTrue(ok, why)
        real, seen = seat_reassign.holdings, []

        def census(seat, root=None):
            # ONLY THE RENAME'S OPENING CENSUS is unread; every census the
            # verb takes after it reads the real stores.
            seen.append(seat)
            if len(seen) == 1:
                return ({k: [] for k in seat_reassign.SURFACES},
                        [seat_reassign._unread(["tasks"], "planted unread")])
            return real(seat, root)
        with mock.patch.object(seat_reassign, "holdings", census):
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        self.assertGreater(len(seen), 1, "the verb never took its own census, "
                                         "so the unread answer was not the "
                                         "rename's alone: " + msg)
        audit = self.audit()
        self.assertIsNotNone(audit, "the carry never ran the verb: " + msg)
        self.assertEqual(len(audit["moved"]["leases"]), 1, audit)
        self.assertIn("Carried 1 open holding from alpha", msg)
        self.assertIn("before it is UNKNOWN", msg)
        self.assertNotIn("counted", msg)

    def test_a_partial_carry_never_reads_as_carried(self):
        """CONTROL: `WORKER` matches the old name canonically but the lease
        mover matches the stored holder exactly, so that lease stays and the
        verb exits 1 after moving the other one. A count of what moved is not
        a carry, so the message says NOT fully carried and prints no count."""
        self.rostered("Worker", "s-worker-2529")
        for resource, holder in (("a-live-resource", "Worker"),
                                 ("an-untouched-resource", "WORKER")):
            ok, why, _lease = seats_claims.claim(resource, holder)
            self.assertTrue(ok, why)
        ok, msg = seats_roster.rename_seat("Worker", "worker")
        self.assertTrue(ok, msg)
        audit = self.audit()
        self.assertIsNotNone(audit, "the carry never ran the verb: " + msg)
        self.assertEqual(len(audit["moved"]["leases"]), 1, audit)
        self.assertEqual(audit["remaining"]["leases"], 1, audit)
        self.assertIn("NOT fully carried", msg)
        self.assertNotIn("Carried ", msg)

    def test_a_row_already_addressed_to_the_new_name_is_not_carried(self):  # noqa: VACUOUS_ASSERTION — the flagged absences are the audit's empty refusals and the missing NOT-fully-carried line; their unconditional positives are the census listing the row before the rename, the audit listing that same row as moved with already set, and the message naming it as already belonging to worker
        """A dispatch row sent to `Worker` routes to `worker` by the canonical
        recipient, so the verb writes nothing for it and lists it as moved
        with `already` set, so that re-runs converge. It is not a carry."""
        from helm import dispatches
        repo, tip = self.dispatch_repo()
        self.rostered("Worker", "s-worker-2529")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "integrator"}):
            row, why, _s = dispatches.send(
                "Worker", "inbound-lane", "a brief", tip, kind="review",
                new_work=True, repo=repo)
        self.assertIsNotNone(row, "fixture: inbound send refused (%s)" % why)
        man, unread = seat_reassign.holdings("Worker")
        self.assertEqual(unread, [])
        self.assertEqual([r["id"] for r in man["dispatch_in"]], [row["id"]],
                         "fixture: the census does not list the row under "
                         "the old name, so there is no count to overstate")
        ok, msg = seats_roster.rename_seat("Worker", "worker")
        self.assertTrue(ok, msg)
        audit = self.audit()
        self.assertIsNotNone(audit, "the carry never ran the verb: " + msg)
        self.assertEqual([(r["id"], r.get("already"))
                          for r in audit["moved"]["dispatch"]],
                         [(row["id"], True)], audit)
        self.assertFalse(any(audit["refused"].values()), audit)
        self.assertNotIn("NOT fully carried", msg)
        self.assertIn("Carried 0 open holdings from Worker", msg)
        self.assertIn("1 more already belonged to worker", msg)

    def test_a_sent_row_whose_delivery_leg_is_already_the_new_names_is_not_carried(self):
        """A row `Worker` SENT is chased by its custodian, which is the sender
        while none is recorded. `worker` matches that canonically, so the
        custody writer returns the row unchanged and writes nothing. The
        custody mover counted that return as a move, and the rename printed
        "Carried 1" about a row it never wrote."""
        from helm import dispatches
        repo, tip = self.dispatch_repo()
        self.rostered("Worker", "s-worker-2529")
        self.rostered("integrator", "s-integrator-2529")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "Worker"}):
            row, why, _s = dispatches.send(
                "integrator", "outbound-lane", "a brief", tip, kind="review",
                new_work=True, repo=repo)
        self.assertIsNotNone(row, "fixture: outbound send refused (%s)" % why)
        man, unread = seat_reassign.holdings("Worker")
        self.assertEqual(unread, [])
        self.assertEqual([r["id"] for r in man["dispatch_out"]], [row["id"]],
                         "fixture: the census does not list the sent row "
                         "under the old name, so there is no count to "
                         "overstate")
        self.assertEqual(man["dispatch_in"], [], man)
        current, unavailable = dispatches.snapshot()
        self.assertFalse(unavailable, unavailable)
        before = current[row["id"]]
        ok, msg = seats_roster.rename_seat("Worker", "worker")
        self.assertTrue(ok, msg)
        self.assertIn("Carried 0 open holdings from Worker", msg)
        self.assertIn("1 more already belonged to worker", msg)
        self.assertNotIn("Carried 1", msg)
        current, unavailable = dispatches.snapshot()
        self.assertFalse(unavailable, unavailable)
        after = current[row["id"]]
        self.assertEqual((after["seq"], after.get("custodian")),
                         (before["seq"], before.get("custodian")),
                         "the rename wrote a custody event: " + msg)
        audit = self.audit()
        self.assertIsNotNone(audit, "the carry never ran the verb: " + msg)
        self.assertEqual([(r["id"], r.get("already"))
                          for r in audit["moved"]["dispatch_out"]],
                         [(row["id"], True)], audit)
        self.assertEqual(audit["moved"]["dispatch"], [], audit)
        self.assertFalse(any(audit["refused"].values()), audit)

    def test_a_sent_row_moved_to_a_third_seat_before_the_custody_step_refuses(self):
        """task/2529 r1: the carry's census snapshots the sent row
        with custodian `Worker`, which matches `worker` canonically. If another
        reassignment moves that row's delivery leg to a third seat before the
        custody step, the only thing that can see it is the custody writer's
        locked compare-and-swap. A shortcut that decides "already the
        target's" from the stale snapshot skipped that writer and reported a
        clean carry and a target audit for a row a third seat now chases."""
        from helm import dispatches, takeover
        repo, tip = self.dispatch_repo()
        self.rostered("Worker", "s-worker-2529b")
        self.rostered("integrator", "s-integrator-2529b")
        self.rostered("third", "s-third-2529b")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "Worker"}):
            row, why, _s = dispatches.send(
                "integrator", "outbound-lane", "a brief", tip, kind="review",
                new_work=True, repo=repo)
        self.assertIsNotNone(row, "fixture: outbound send refused (%s)" % why)
        man, unread = seat_reassign.holdings("Worker")
        self.assertEqual(unread, [])
        self.assertEqual([r["id"] for r in man["dispatch_out"]], [row["id"]],
                         "fixture: the census does not list the sent row "
                         "under the old name")
        real_move_custody = seat_reassign._move_custody
        raced = []

        def custody_step_after_a_concurrent_move(rows, *a, **kw):
            # THE OTHER REASSIGNMENT LANDS FIRST, through the real writer, in
            # the window between the carry's census (`rows` is that snapshot)
            # and its custody step: a real worker -> third capability and a
            # real custody write. Staged at the step's entry, which every
            # version of the step reaches, not at a mint a shortcut can skip.
            # Nothing here asserts: the rename's carry reports an exception as
            # an uncarried holding, which would hide a fixture failure behind
            # the very refusal this arm looks for. The outcome is recorded and
            # asserted after the rename returns.
            if not raced:
                disp, err = takeover.mint_source_disposition("worker")
                auth = None
                if not err:
                    auth, err = takeover.mint_reassign_custody(
                        "worker", "third", force=True,
                        reason="a concurrent reassignment", disposition=disp)
                moved_row = None
                if not err:
                    moved_row, err = dispatches.mark_custody(row["id"], auth)
                raced.append((moved_row, err))
            return real_move_custody(rows, *a, **kw)

        with mock.patch.object(seat_reassign, "_move_custody",
                               side_effect=custody_step_after_a_concurrent_move):
            ok, msg = seats_roster.rename_seat("Worker", "worker")
        self.assertTrue(ok, msg)
        self.assertEqual(len(raced), 1,
                         "fixture: the custody step never ran, so the "
                         "concurrent move was never staged: " + msg)
        self.assertIsNone(raced[0][1],
                          "fixture: the concurrent move refused, so nothing "
                          "raced the carry: %s" % (raced[0][1],))
        current, unavailable = dispatches.snapshot()
        self.assertFalse(unavailable, unavailable)
        self.assertEqual(dispatches.custodian_of(current[row["id"]]), "third",
                         "the concurrent move did not stick: " + msg)
        audit = self.audit()
        self.assertIsNotNone(audit, "the carry never ran the verb: " + msg)
        self.assertEqual(audit["moved"]["dispatch_out"], [],
                         "a row a third seat now chases was reported as the "
                         "target's: %r" % (audit,))
        self.assertEqual([r["id"] for r in audit["refused"]["dispatch_out"]],
                         [row["id"]], audit)
        self.assertIn("NOT fully carried", msg)
        self.assertNotIn("already belonged to worker", msg)

    def test_a_bounded_audit_row_leaves_the_carry_count_whole(self):
        """The audit bound degrades an oversized event by replacing a
        surface's per-row list with a count. It did that to the verb's own
        result, which the rename then read, so a dropped lease surface
        counted its two summary keys as two carried holdings."""
        self.rostered("alpha", "s-alpha-2529")
        ok, why, _lease = seats_claims.claim("resource-one", "alpha")
        self.assertTrue(ok, why)
        with mock.patch.object(seat_reassign, "AUDIT_EVENT_BUDGET", 64):
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        self.assertEqual(
            pk.read_json(seats_claims.claims_path(), {})["resource-one"]
            ["holder"], "beta", msg)
        audit = self.audit()
        self.assertIsNotNone(audit, "the carry never ran the verb: " + msg)
        self.assertIn("moved.leases (1)", audit.get("detail_dropped") or "",
                      "fixture: the audit row was not bounded, so the "
                      "result was never degraded: %r" % audit)
        self.assertIn("Carried 1 open holding from alpha", msg)
        self.assertNotIn("Carried 2", msg)

    def test_a_moved_surface_with_no_rows_to_count_reads_UNKNOWN(self):
        """CONTROL: a result whose moved surface is the bound's count summary
        rather than a list has no rows to count, so no number is printed. The
        summary comes from the real bound, not a hand-written shape."""
        self.plant({"alpha": {"session": "s-alpha", "sessions": ["s-alpha"]}})
        self.held("alpha")
        # A BUDGET NO EVENT FITS, so the bound degrades every listed surface.
        with mock.patch.object(seat_reassign, "AUDIT_EVENT_BUDGET", 1):
            bounded = seat_reassign._bounded_event(
                {"moved": {"leases": [{"resource": "resource-one"}]}})
        self.assertEqual(bounded["moved"]["leases"],
                         {"count": 1, "detail": "DROPPED"},
                         "fixture: the bound did not degrade the surface")
        calls = []

        def door(source, to, outcome=None, **kw):
            calls.append((source, to))
            outcome.update(moved=bounded["moved"])
            return 0, ["helm seat reassign: done"]
        with mock.patch.object(seat_reassign, "reassign", door):
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        self.assertEqual(calls, [("alpha", "beta")], "the door was not invoked")
        self.assertIn("HOW MANY moved is UNKNOWN", msg)
        self.assertNotIn("Carried ", msg)

    def test_the_lineage_the_rename_writes_is_what_answers_afterwards(self):
        """End to end: after the rename the old name is not orphaned, because
        the row it moved to carries its key."""
        self.plant({"alpha": {"session": "s-alpha", "sessions": ["s-alpha"]}})
        _calls, fake = self.spy()
        with mock.patch.object(seat_reassign, "reassign", fake):
            ok, msg = seats_roster.rename_seat("alpha", "beta")
        self.assertTrue(ok, msg)
        name, state, _why = seats_lineage.seat_lineage("alpha")
        self.assertEqual((name, state), ("beta", seats_lineage.SEAT_RENAMED))


class BoardStandingTest(Base):
    """The board's half: a row whose holder no longer exists must say so
    instead of reading as ordinary debt owed by a builder."""

    def row(self, **kw):
        """A row with every field `_line` actually indexes.

        DERIVED FROM THE RENDERER, NOT GUESSED: the first cut carried only the
        three fields `owed_seat_standing` reads, and `_line` raised KeyError on
        `stalled`, `id`, `lane`, `review_sha` and `state` in turn — a fixture
        smaller than its consumer is not a smaller fixture, it is a wrong one.
        Every field a renderer INDEXES with [] is one it treats as always
        present, so those are the fixture's contract, not decoration."""
        lr = {"owed_by": "author", "author": "alpha", "reviewer": "beta",
              "id": "aaaaaaaaaaaa1111", "lane": "a-lane", "state": "OPEN",
              "review_sha": "cccccccccccc2222", "stalled": False,
              "dwell_s": None, "dwell_known": False}
        lr.update(kw)
        return lr

    def test_a_live_holder_renders_as_before(self):
        self.plant({"alpha": {"session": "s"}})
        role, seat, state, _why = landreq.owed_seat_standing(self.row())
        self.assertEqual((role, seat), ("author", "alpha"))
        self.assertEqual(state, seats_lineage.SEAT_CURRENT)
        self.assertEqual(landreq.ball_holder(self.row()), ("author", "alpha"))

    def test_a_renamed_holder_resolves_to_the_successor_without_a_rewrite(self):
        self.plant({"delta": {"session": "s",
                              "seat_keys": [self.key("alpha")]}})
        role, seat, state, _why = landreq.owed_seat_standing(self.row())
        self.assertEqual((role, seat), ("author", "delta"))
        self.assertEqual(state, seats_lineage.SEAT_RENAMED)

    def test_a_holder_nothing_answers_to_is_orphaned(self):
        self.plant({"delta": {"session": "s"}})
        role, seat, state, why = landreq.owed_seat_standing(self.row())
        self.assertEqual((role, seat), ("author", "alpha"))
        self.assertEqual(state, seats_lineage.SEAT_ORPHANED)
        self.assertIn("alpha", why)

    def test_the_orphan_mark_names_the_cure_and_promises_no_expiry(self):
        self.plant({"delta": {"session": "s"}})
        line = landreq._line(self.row())
        self.assertIn("ORPHANED", line)
        self.assertIn("helm seat reassign", line)
        self.assertIn("NOT expired", line)

    def test_an_orphan_is_not_marked_stalled_away_or_closed(self):  # noqa: VACUOUS_ASSERTION — the claim IS an absence and there is no positive form of it: no input makes the display half mutate the row, so a control on THIS observable cannot exist. The control that can is below and on a different observable — the path ran and rendered an ORPHANED mark — which is what rules out an unchanged row that was simply never touched.
        """A rendering, never an enforcement act: the audit role and every
        field a sweep reads are the same before and after."""
        lr = self.row(stalled=True, state="OPEN")
        self.plant({"delta": {"session": "s"}})
        before = dict(lr)
        _r, _s, state, _w = landreq.owed_seat_standing(lr)
        line = landreq._line(lr)
        # THE CONTROL: the path under test RAN and had something to say. A row
        # that rendered no mark would satisfy the equality below without the
        # display half ever having been exercised.
        self.assertEqual(state, seats_lineage.SEAT_ORPHANED)
        self.assertIn("ORPHANED", line)
        self.assertEqual(lr, before, "the display half mutated the row")

    def test_a_role_with_no_single_holder_is_never_marked(self):
        """integrator, nobody and unknown render unchanged: inventing a name
        for them is the same defect pointing the other way, and an ORPHANED
        banner on one would be exactly that."""
        self.plant({"delta": {"session": "s"}})
        # THE CONTROL — a role that DOES name a seat gets a state off this
        # same roster, so the Nones below cannot be a resolver that is simply
        # answering nothing for everybody.
        _r, seat, state, _why = landreq.owed_seat_standing(self.row())
        self.assertEqual(seat, "alpha")
        self.assertEqual(state, seats_lineage.SEAT_ORPHANED)
        for role in ("integrator", "nobody", "unknown"):
            _r, seat, state, _why = landreq.owed_seat_standing(
                {"owed_by": role})
            self.assertIsNone(seat, role)
            self.assertIsNone(state, role)

    def test_an_unreadable_roster_marks_nothing(self):
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            role, seat, state, _why = landreq.owed_seat_standing(self.row())
            line = landreq._line(self.row())
        self.assertEqual((role, seat), ("author", "alpha"))
        self.assertEqual(state, seats_lineage.SEAT_LINEAGE_UNKNOWN)
        self.assertNotIn("ORPHANED", line)

    def test_the_standing_rides_the_wire_or_is_absent(self):
        """A field the projection computes and the card drops cannot reach the
        console. Present-and-null and absent must not mean two things, so the
        states with nothing to say are absent rather than an empty banner."""
        self.plant({"delta": {"session": "s"}})
        _r, _s, state, why = landreq.owed_seat_standing(self.row())
        orphan = landreq._card_standing(state, why)
        self.assertEqual(orphan["state"], seats_lineage.SEAT_ORPHANED)
        self.assertIn("alpha", orphan["why"])
        self.plant({"alpha": {"session": "s"}})
        _r, _s, state, why = landreq.owed_seat_standing(self.row())
        self.assertIsNone(landreq._card_standing(state, why),
                          "a live holder sent an empty banner to the console")


if __name__ == "__main__":
    unittest.main()
