#!/usr/bin/env python3
"""`helm seat reassign` — the dead-seat holdings mandate. Hermetic: HELM_HOME/HELM_CHAT_DIR/HELM_PROC
are tmpdirs, so the real ledgers and the LIVE FLEET'S .claims.json are never
touched. That last one is not paranoia: a prior suite reaped 24,001 live-fleet
chat files because one tmpdir redirect missed the chat seam, and `.claims.json`
lives in the chat dir.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helm import dispatches, seat_reassign, seats_roster, tasks

ENV_KEYS = ("HELM_HOME", "HELM_ADOPTED_DIR", "HELM_CHAT_DIR",
            "HELM_CHAT_NODE_URL", "HELM_CHAT_NAME", "HELM_CHAT_ROOM",
            "HELM_PROC", "HELM_CELL_BIN", "CLAUDECODE",
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID")


def run(fn, args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(args)
    return rc, out.getvalue(), err.getvalue()


class ReassignBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-reassign-")
        self.prior = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["HELM_HOME"] = os.path.join(self.tmp, "helm")
        os.environ["HELM_ADOPTED_DIR"] = os.path.join(self.tmp, "adopted")
        os.environ["HELM_CHAT_DIR"] = os.path.join(self.tmp, "chat")
        os.environ["HELM_CHAT_NODE_URL"] = ""
        os.environ["HELM_CHAT_NAME"] = "integrator"
        os.environ["HELM_CHAT_ROOM"] = "main"
        os.environ["HELM_PROC"] = os.path.join(self.tmp, "proc")
        for d in ("helm", "adopted", "chat", "proc"):
            os.makedirs(os.path.join(self.tmp, d), exist_ok=True)
        # THE ISOLATION IS ASSERTED, NOT ASSUMED. A redirect that silently
        # failed would point every write below at the live fleet, and the
        # arms would still pass.
        from helm import home, seats_common
        self.assertTrue(home.global_dir().startswith(self.tmp),
                        "HELM_HOME redirect did not take: %s"
                        % home.global_dir())
        self.assertTrue(seats_common.claims_path().startswith(self.tmp),
                        "the CHAT seam did not take, so .claims.json is the "
                        "LIVE FLEET'S: %s" % seats_common.claims_path())

    def tearDown(self):
        for k, v in self.prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def ledger_rows(path):
        """Rows of an audit ledger AS ITS AUDITOR SEES THEM.

        Through `eventledger.checked_events`, not a raw line read, and the
        difference is the whole point. The checked reader admits a row only
        when it is a complete newline-terminated line, under the size bound,
        valid JSON, an object, AND carrying a non-empty id — and this exact
        module once appended rows with no id, so the write SUCCEEDED, the
        writer reported True, and the audit trail was empty. A test that
        parses lines itself would confirm that write and see nothing wrong.
        Asserting through the auditor's own reader is what makes "the row was
        recorded" mean the thing the operator will later be able to read.
        """
        from helm import eventledger
        rows, unavailable = eventledger.checked_events(path)
        if unavailable:
            return []
        return list(rows)

    def seat(self, name, session=None):
        """Mint a roster row AS that seat.

        THE GUARD REFUSES A CROSS-SEAT SESSION STAMP AND IT IS RIGHT TO:
        "presence and delivery are per-PROCESS facts, so a seat may only ever
        stamp its own row". An earlier fixture wrote every row while declaring
        `integrator`, so every `session` was silently dropped and three arms
        failed against a substrate that was behaving correctly. Declaring the
        seat for the duration of its own write is what a real seat does."""
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = name
        try:
            seats_roster.write_roster(name, session=session, cwd=self.tmp,
                                      home_room="main")
        finally:
            if prior is None:
                os.environ.pop("HELM_CHAT_NAME", None)
            else:
                os.environ["HELM_CHAT_NAME"] = prior
        if session:
            # ASSERT THE FIXTURE TOOK. A row whose session was refused looks
            # exactly like a seat that never stamped one, and every arm below
            # would then be measuring the wrong world.
            row = seats_roster.roster().get(name) or {}
            self.assertEqual(row.get("session"), session,
                             "fixture: the session stamp did not land on %s "
                             "— this arm would measure nothing" % name)
        return name


class IdentityIsBoundAtTheMomentOfActionTest(ReassignBase):
    """The source and target were resolved ~45 lines before the lock existed.

    A rename landing in that window left the run holding a lock keyed on a
    name that had moved, about to write to seats nobody named — and every
    write would have succeeded, because each one is individually valid.

    Moving the resolve inside the lock only SHRINKS the window: the lock key
    is derived from the resolution, so it cannot protect the thing it is
    derived from. These arms are therefore about the compare-and-swap, not
    about ordering.
    """

    def _drifting(self, first, second):
        """A resolver whose answer CHANGES between the two reads — the input
        is actually moved rather than described as movable."""
        calls = []

        def resolver(_token):
            calls.append(1)
            return first if len(calls) == 1 else second
        return resolver

    def test_a_source_that_moves_under_the_lock_REFUSES(self):
        with mock.patch.object(
                seat_reassign, "resolve_source",
                self._drifting(("seat-a", "sess-1", "named"),
                               ("seat-b", "sess-2", "named"))), \
             mock.patch.object(seat_reassign, "resolve_target",
                               lambda _t: ("taker", "named")):
            rc, out = seat_reassign.reassign("seat-a", "taker", apply=True)
        text = "\n".join(out)
        self.assertEqual(rc, 1, "a moved source was accepted:\n%s" % text)
        self.assertIn("identity moved", text)
        self.assertIn("seat-b", text, "the refusal does not say what it "
                                      "found, so an operator cannot act on it")

    def test_a_target_that_moves_under_the_lock_REFUSES(self):
        with mock.patch.object(seat_reassign, "resolve_source",
                               lambda _t: ("seat-a", "sess-1", "named")), \
             mock.patch.object(
                 seat_reassign, "resolve_target",
                 self._drifting(("taker", "named"), ("someone-else", "named"))):
            rc, out = seat_reassign.reassign("seat-a", "taker", apply=True)
        text = "\n".join(out)
        self.assertEqual(rc, 1, "a moved target was accepted:\n%s" % text)
        self.assertIn("identity moved", text)
        self.assertIn("someone-else", text)

    def test_a_source_that_STOPS_resolving_REFUSES(self):
        with mock.patch.object(
                seat_reassign, "resolve_source",
                self._drifting(("seat-a", "sess-1", "named"),
                               (None, None, "no such seat"))), \
             mock.patch.object(seat_reassign, "resolve_target",
                               lambda _t: ("taker", "named")):
            rc, out = seat_reassign.reassign("seat-a", "taker", apply=True)
        self.assertEqual(rc, 1)
        self.assertIn("no longer resolves", "\n".join(out))

    def test_a_STABLE_identity_is_NOT_refused(self):
        """A drift check that refuses everything closes no race; it breaks the
        verb. So the sibling arms above need this one, and this one needs to
        prove the run REACHED the check rather than stopping short of it.

        Progress is read off the AUDIT ROW, not off the output prose. The row
        is written near the end of the apply path, after the identity
        compare-and-swap, so its arrival is proof this call got past the
        check — and unlike a printed phrase it does not change when someone
        rewords a message.
        """
        # THE TARGET IS A ROSTER ROW, because every mover asks the roster
        # about it: a target on no row is refused before the first write, and
        # this arm is about identity drift, not about an unaddressable target.
        self.seat("taker")
        before = len(self.ledger_rows(seat_reassign.ledger_path()))
        with mock.patch.object(seat_reassign, "resolve_source",
                               lambda _t: ("seat-a", "sess-1", "named")), \
             mock.patch.object(seat_reassign, "resolve_target",
                               lambda _t: ("taker", "named")):
            _rc, out = seat_reassign.reassign("seat-a", "taker", apply=True)

        rows = self.ledger_rows(seat_reassign.ledger_path())
        self.assertEqual(len(rows), before + 1,
                         "no audit row was written, so this call never "
                         "reached the end of the apply path and never "
                         "reached the identity check either")
        self.assertNotIn("identity moved", "\n".join(out),  # noqa: VACUOUS_ASSERTION — the audit-row assertion above is this arm's unconditional positive control; it is on a STRONGER observable than the output text, proving the run reached the end of the apply path rather than merely printing nothing
                         "a stable identity was reported as drifting, so the "
                         "check refuses everything and proves nothing")


class AnUnreadSurfaceNamesARealSurfaceTest(ReassignBase):
    """`remaining` reported a NUMBER for a surface it never read.

    The tri-state asked whether a surface key was in a list of MESSAGE
    STRINGS, so it never matched and every surface reported a count off a read
    that had failed — the false zero the tri-state exists to prevent, defeated
    by the shape of its own input. Parsing the prefix would not have saved it:
    entries said "dispatch" while the surfaces are `dispatch_in` and
    `dispatch_out`, so the one case with two halves was the one case a prefix
    could never name.

    """

    def test_a_failing_claims_read_darkens_the_leases_surface(self):
        from helm import seats_claims
        with mock.patch.object(seats_claims, "claims_list",
                               side_effect=OSError("boom")):
            _man, unread = seat_reassign.holdings("some-seat")
        self.assertTrue(unread, "a raising claims read reported NOTHING unread")
        self.assertIn("leases", seat_reassign.unread_surfaces(unread))

    def test_a_failing_dispatch_read_darkens_BOTH_halves(self):
        """One read produced both halves, so one failure darkens both. Naming
        a single surface would let the other report a confident zero off a
        read that never happened."""
        from helm import dispatches
        with mock.patch.object(dispatches, "snapshot",
                               side_effect=OSError("boom")):
            _man, unread = seat_reassign.holdings("some-seat")
        got = seat_reassign.unread_surfaces(unread)
        # EXACT, not a subset. `A - got == set()` is silent about anything
        # EXTRA in `got`, so a census that darkened every surface off one
        # failed dispatch read would satisfy it — and over-darkening is its
        # own defect: it refuses reassignments that could have proceeded.
        # Only the dispatch reader was made to fail, so only its two halves
        # may be unknown.
        self.assertEqual(got, {"dispatch_in", "dispatch_out"},
                         "a failing dispatch read darkened the wrong set of "
                         "surfaces: %s" % sorted(got))

    def test_every_surface_an_unread_entry_names_is_a_REAL_surface(self):
        """THE INVARIANT THAT WOULD HAVE CAUGHT THIS AT BIRTH, and the reason
        it is written against the PRODUCERS rather than against a literal: a
        name that is not in SURFACES can never mark anything UNKNOWN, and
        nothing else in the system objects to it."""
        from helm import dispatches, seats_claims, tasks
        seen = set()
        producers = ((seats_claims, "claims_list"),
                     (dispatches, "snapshot"),
                     (tasks, "snapshot"))
        for mod, attr in producers:
            # A NAMED PRODUCER THAT DOES NOT EXIST IS A BROKEN CONTROL, NEVER
            # A REASON TO CONTINUE. This loop skipped a missing attribute
            # silently, and it named `tasks.load`, which HAS NEVER EXISTED on
            # that module — so the "tasks" surface was never once exercised by
            # the arm whose own docstring calls it the invariant that would
            # have caught this at birth. It could not have: the producer it
            # reached for was absent and the guard turned that into a pass.
            # Measured while curing the defect it missed (task/2378).
            self.assertTrue(
                hasattr(mod, attr),
                "this arm names %s.%s as a producer and that attribute does "
                "not exist, so this surface is silently unexercised — point "
                "the arm at the real producer rather than skipping it"
                % (mod.__name__, attr))
            with mock.patch.object(mod, attr, side_effect=OSError("boom")):
                _man, unread = seat_reassign.holdings("some-seat")
            seen |= seat_reassign.unread_surfaces(unread)
        self.assertEqual(len(seen), len(set(seat_reassign.SURFACES)),
                         "the producers this arm drives did not between them "
                         "darken every declared surface, so some surface has "
                         "no failing-read arm at all: reached %s of %s"
                         % (sorted(seen), sorted(seat_reassign.SURFACES)))
        self.assertTrue(seen, "no producer named any surface — this arm "
                              "cannot see the defect it is about")
        self.assertEqual(seen - set(seat_reassign.SURFACES), set(),
                         "an unread entry named a surface that does not "
                         "exist, so it can never mark anything UNKNOWN")

    def test_an_UNREADABLE_task_ledger_darkens_tasks_and_never_reads_empty(self):
        """THE DEFECT THIS LANE CURES, DRIVEN.

        `ownership_census` states the rule in its own source: snapshot
        readers, not `rows()`, because `rows()` is `snapshot(path)[0]` and the
        subscript throws away the unavailable channel. This leg called
        `tasks.rows()`, so a task ledger nobody could read rendered as a
        manifest with NO tasks, NOTHING in `unread`, and rc 0 — and a seat
        reassigned on that answer leaves every task row behind on the dead
        seat it was moving away from.

        The unavailable channel is a RETURN VALUE, not an exception, which is
        why the sibling arms that raise could not see this: nothing raised.
        """
        from helm import tasks
        with mock.patch.object(tasks, "snapshot",
                               return_value=({}, "ledger unreadable: boom")):
            man, unread = seat_reassign.holdings("some-seat")
        self.assertIn("tasks", seat_reassign.unread_surfaces(unread),
                      "an unreadable task ledger left the tasks surface "
                      "reading CLEAN, which is the false zero this channel "
                      "exists to prevent")
        self.assertEqual(man.get("tasks"), [],
                         "control: the manifest must still be empty — the "
                         "defect was never a wrong count, it was a count "
                         "nobody could tell from an unread")

    def test_a_READABLE_empty_task_ledger_stays_QUIET(self):  # noqa: VACUOUS_ASSERTION — the contract IS the absence of an unread entry, and its control is the sibling arm above driving the same door to the opposite answer
        """The other half, and the one that keeps the cure from being a
        blanket darkening. A ledger that is READ and holds no rows for this
        seat is a measured zero, and marking it UNKNOWN would refuse
        reassignments that could safely proceed."""
        from helm import tasks
        with mock.patch.object(tasks, "snapshot", return_value=({}, None)):
            man, unread = seat_reassign.holdings("some-seat")
        self.assertNotIn("tasks", seat_reassign.unread_surfaces(unread),
                         "a ledger that WAS read reported UNKNOWN, so the "
                         "cure over-darkened: absence measured is not absence "
                         "unmeasured")
        self.assertEqual(man.get("tasks"), [])

    def test_a_MALFORMED_roster_is_UNKNOWN_not_a_rename_orphan(self):
        """THE GUARD WAS WRITTEN FOR A RAISE THAT CANNOT HAPPEN.

        `seats_common.roster()` is `pk.read_json(path, {}) or {}`, and that
        reader swallows missing, unreadable, malformed and wrong-shaped alike
        — measured: all three failures return `{}` with no exception. So an
        `except Exception` around it never fires for the case its own comment
        names, and a malformed roster reached the rename-orphan path, which
        treats the token as a bare holder key and MOVES HOLDINGS on the
        weakest resolution available. That is the precise behaviour the
        comment above the guard forbids.
        """
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            seat, holder, err = seat_reassign.resolve_source("some-seat")
        self.assertIsNone(seat)
        self.assertIsNone(holder)
        self.assertIn("UNKNOWN", err or "",
                      "a roster that could not be parsed resolved to a "
                      "rename-orphan instead of UNKNOWN: %r" % err)

    def test_a_MALFORMED_roster_does_not_decide_a_TARGET_either(self):
        """The receiving side is a truth consumer too: deciding a name can
        receive work off a roster nobody could parse is the false-negative
        twin of the source side's false weak resolution."""
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            seat, err = seat_reassign.resolve_target("some-seat")
        self.assertIsNone(seat)
        self.assertIn("UNKNOWN", err or "", err)

    def test_a_READABLE_roster_still_RESOLVES(self):
        """The control that keeps the tri-state from becoming a blanket
        refusal: a roster that WAS read resolves exactly as before."""
        rows = {"some-seat": {"session": "s-some-0001"}}
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=(rows, False)):
            seat, why = seat_reassign.resolve_target("some-seat")
        # THE SECOND VALUE IS A REASON, NOT AN ERROR, on the success path —
        # `resolve_target` answers (seat, why) and says "exact roster row".
        self.assertEqual(seat, "some-seat", why)
        self.assertIn("roster row", why or "")

    def test_the_RECORDED_row_survives_a_legacy_string_entry(self):
        """`unread_text` and `unread_surfaces` both accept a plain string,
        because rows written before the structured shape live in the audit
        ledger forever. The record builder did not: it built a dict from each
        entry, which raises on a string, so the one input the readers were
        built to survive would have crashed the write.

        This drives the SECOND census rather than the first, because a dirty
        first census refuses before anything moves and the branch is never
        reached.
        """
        clean = ({k: [] for k in seat_reassign.SURFACES}, [])
        legacy_detail = "a row written before the structured shape"
        legacy = ({k: [] for k in seat_reassign.SURFACES},
                  ["dispatch: " + legacy_detail])
        self.seat("taker")      # a target the lease mover can address
        before = len(self.ledger_rows(seat_reassign.ledger_path()))
        with mock.patch.object(seat_reassign, "resolve_source",
                               lambda _t: ("seat-a", "sess-1", "named")), \
             mock.patch.object(seat_reassign, "resolve_target",
                               lambda _t: ("taker", "named")), \
             mock.patch.object(seat_reassign, "holdings",
                               side_effect=[clean, legacy]):
            rc, out = seat_reassign.reassign("seat-a", "taker", apply=True)

        rows = self.ledger_rows(seat_reassign.ledger_path())
        self.assertEqual(len(rows), before + 1,
                         "no audit row was written, so 'the RECORDED row "
                         "survives' is a claim this arm never tested")
        row = rows[-1]
        # THE PAYLOAD, not merely the arrival. A row that was written without
        # the field under test satisfies an arrival check unchanged, so the
        # count above cannot stand alone.
        self.assertIsInstance(row, dict,
                              "the appended row is not parseable, so nothing "
                              "below is reading what was recorded")
        # THE EXACT STRUCTURED VALUE, not a substring of its json. A
        # containment probe passes on a row that also carries junk, on one
        # that normalised the entry into a different shape, and on one that
        # recorded the text under some other key entirely — every failure
        # this field could have, hidden behind a match that is technically
        # true. The legacy string must arrive normalised to the structured
        # shape, naming no surface, with its own text preserved.
        self.assertEqual(row.get("remaining_unread"),
                         [{"surfaces": [], "detail": "dispatch: " +
                           legacy_detail}],
                         "the recorded remaining_unread is not the exact "
                         "normalised entry: %r" % (row.get("remaining_unread"),))
        # THE OPERATOR MUST BE TOLD SOMETHING. The surfaces channel yields
        # nothing for a legacy entry, deliberately — inventing a surface from
        # prose is the mistake that produced an unsatisfiable membership test
        # once already. But the message was built from surfaces ALONE, so a
        # run whose only unread entry was legacy printed "could NOT read ,"
        # and named nothing at all, about the single condition this channel
        # exists to report. An entry that cannot name a surface must fall back
        # to its own text.
        text = "\n".join(out)
        self.assertIn(legacy_detail, text,
                      "the operator was told a census failed and NOT what it "
                      "could not read:\n%s" % text)
        self.assertNotIn("could NOT read ,", text,
                         "the unread line rendered an empty surface list")
        # AND THE OTHER FALSE SENTENCE. An unreadable census is not a residual
        # holding, but the remain-line was gated on "not clean" — true of both
        # — so it printed "holdings REMAIN on @seat ()": an empty list
        # contradicting the unread line directly above it. Nothing remained
        # here; a surface simply could not be read.
        self.assertNotIn("holdings REMAIN", text,
                         "an unreadable census was reported as leftover "
                         "holdings:\n%s" % text)
        # An unreadable final census is not a clean reassignment.
        self.assertEqual(rc, 1)

    def test_a_LEGACY_string_entry_still_prints_and_claims_nothing(self):
        """Rows written before the structured shape live in the audit ledger
        forever. They must not crash a reader, and must not have a surface
        invented for them."""
        self.assertEqual(seat_reassign.unread_text("dispatch: boom"),
                         "dispatch: boom")
        self.assertEqual(seat_reassign.unread_surfaces(["dispatch: boom"]),
                         set())


class AMalformedClaimIsUnknownNotAbsentTest(ReassignBase):
    """task/2455 — the CLAIMS half of the partial-move class.

    The TASK half was cured by teaching that surface's census to report what
    it could not read. This surface had the same hole through a different
    door: `claims_list` reads TOLERANTLY and `_sweep` ERASES every non-dict
    row, so a claims file holding a malformed row censused CLEAN. Nothing
    reached the manifest's `unread` channel, the anti-partial guard had
    nothing to refuse on, the move proceeded across dispatch, custody and
    tasks, and only then did `seats_claim_moves`' STRICT reader refuse. The
    verb exited 1 having already split the seat, and a nonzero rc does not
    undo a partial move.

    THE FIXTURE PLANTS THROUGH THE REAL PRODUCER. `seats_claims.claim` writes
    the live row; the corruption is then applied to the file on disk, which is
    what a truncated write or a half-flushed store actually leaves behind. An
    invented claims dict would have tested a shape nothing produces.
    """

    def _plant(self):
        """One LIVE claim written by the real verb, plus one malformed row."""
        from helm import seats_claims
        ok, why, _lease = seats_claims.claim("a-live-resource", "holder",
                                             ttl=7200)
        self.assertTrue(ok, "fixture: the claim was refused (%s)" % why)
        return seats_claims.claims_path()

    @staticmethod
    def _corrupt(path):
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["a-torn-resource"] = None      # what a torn row looks like on disk
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def test_the_TOLERANT_table_cannot_see_it_and_the_CENSUS_can(self):
        """THE DEFECT AND THE CURE IN ONE ARM, on one planted file.

        The first half is a MUST-HIT on the premise: if `claims_list` ever
        started reporting the torn row, the cure below would be measuring a
        hole that had closed somewhere else, and this arm would say so.
        """
        from helm import seats_claims
        path = self._plant()
        self._corrupt(path)

        rows = seats_claims.claims_list(gc=False) or []
        self.assertEqual([r["resource"] for r in rows], ["a-live-resource"],
                         "the tolerant table changed: it now reports the torn "
                         "row, so the premise behind this cure has moved")

        why = seats_claims.claims_unavailable()
        self.assertIsNotNone(
            why, "the census read the same file the MOVE will refuse and "
                 "called it clean — the false all-clear this exists to stop")
        self.assertIn("unreadable", why)

    def test_the_MANIFEST_carries_it_and_a_CLEAN_file_carries_nothing(self):
        """The channel the anti-partial guard actually keys on."""
        path = self._plant()

        man, unread = seat_reassign.holdings("holder")
        self.assertEqual(unread, [], "fixture: a clean claims file was already "
                                     "unread before anything was corrupted")
        self.assertEqual([r["resource"] for r in man["leases"]],
                         ["a-live-resource"],
                         "fixture: the planted claim was never censused, so "
                         "the corrupted run below would prove nothing")

        self._corrupt(path)
        _man2, unread2 = seat_reassign.holdings("holder")
        self.assertTrue(unread2, "a torn claim row reached NOTHING")
        self.assertIn("leases", seat_reassign.unread_surfaces(unread2),
                      "the entry did not name the surface it darkened: %r"
                      % unread2)

    def test_the_GUARD_REFUSES_BEFORE_THE_FIRST_WRITE(self):  # noqa: VACUOUS_ASSERTION — the flagged observable is the UNMOVED task owner and the byte-identical claims store; the unconditional positive control is the repaired run asserted on the SAME two observables (owner moved, bytes differ from a FRESH repaired baseline) at the end of this method
        """ORDERING IS THE CONTRACT: a surface that moves EARLIER must not.

        THE FIXTURE HAS TO BE POPULATED OR THIS ARM PROVES NOTHING. `reassign`
        moves dispatch, custody and tasks BEFORE `_move_leases`, so a byte
        comparison on the claims file alone measures "first write" only if
        something earlier COULD have been written. A fixture seeding just a
        roster and a claim leaves nothing ahead of the lease mover, and the
        assertion then holds for a reason that has nothing to do with the cure.

        So the real movable holding is a TASK, filed through the real producer
        and owned by the seat being moved away from. Its owner is the
        observable that would change first.
        """
        from helm import tasks as _tasks
        self.seat("holder", session="s-holder-0001")
        self.seat("taker", session="s-taker-0004")
        row, err = _tasks.add("a movable row", "holder", project="helm")
        self.assertIsNone(err, "fixture: the task was refused (%s)" % err)
        tid = row["id"]
        # MUST-HIT ON THE POPULATION: the census must SEE this task, or the
        # move below has nothing earlier to do and the ordering claim is empty.
        man, unread = seat_reassign.holdings("holder")
        self.assertEqual(unread, [], "fixture: the census is already unread")
        self.assertIn(tid, [t.get("id") for t in man["tasks"]],
                      "fixture: the planted task is not in the manifest, so "
                      "nothing precedes the lease mover: %r" % (man["tasks"],))

        path = self._plant()
        self._corrupt(path)
        with open(path, "rb") as fh:
            before = fh.read()

        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "gone")):
            rc, lines = seat_reassign.reassign("holder", "taker", apply=True)
        self.assertEqual(rc, 1, lines)
        self.assertTrue(any("PARTIAL" in l for l in lines),
                        "the verb did not name the partial it refused: %s"
                        % lines)
        self.assertTrue(any("nothing was moved" in l for l in lines), lines)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before,
                             "the claims store was WRITTEN on a run that says "
                             "nothing moved")
        # THE EARLIER SURFACE IS THE ONE THAT PROVES ORDERING. A task moved
        # ahead of the refusal is the split this verb exists to prevent, and
        # it is invisible in the claims file.
        self.assertEqual(_tasks.rows()[tid]["owner"], "holder",
                         "the task moved before the claims surface refused — "
                         "the seat is split, which is the whole defect")

        # UNCONDITIONAL POSITIVE ON BOTH OBSERVABLES, against a FRESH baseline.
        # Comparing a repaired run against the CORRUPT bytes proves nothing:
        # the repair itself makes them differ, so the assertion would hold over
        # a verb that never wrote. Snapshot AFTER repairing and BEFORE acting.
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw.pop("a-torn-resource")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        with open(path, "rb") as fh:
            repaired = fh.read()
        self.assertNotEqual(repaired, before,
                            "fixture: the repair changed nothing, so the "
                            "baseline below is the corrupt one again")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "gone")):
            rc2, lines2 = seat_reassign.reassign("holder", "taker", apply=True)
        self.assertEqual(rc2, 0, "\n".join(lines2))
        with open(path, "rb") as fh:
            self.assertNotEqual(fh.read(), repaired,
                                "the repaired run left the store untouched, "
                                "so the byte comparison above was measuring a "
                                "path that never writes")
        self.assertEqual(_tasks.rows()[tid]["owner"], "taker",
                         "the clean run did not move the task, so the unmoved "
                         "owner above says nothing about ordering")

    def test_a_CLEAN_claims_file_still_moves(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertFalse(PARTIAL in the output); its unconditional positive is the holder_id assertion on the moved lease two lines below, which the rung cannot pair with it because the printed lines and the claims table are different producers
        """THE CONTROL WITHOUT WHICH THE CURE IS A BLANKET REFUSAL."""
        from helm import seats_claims
        self.seat("holder", session="s-holder-0001")
        self.seat("taker", session="s-taker-0004")
        self._plant()

        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "gone")):
            rc, lines = seat_reassign.reassign("holder", "taker", apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertFalse(any("PARTIAL" in l for l in lines), lines)
        held = [r for r in (seats_claims.claims_list(gc=False) or [])
                if r["resource"] == "a-live-resource"]
        self.assertEqual(len(held), 1, "the planted claim vanished: %r" % held)
        self.assertEqual(held[0]["holder_id"], "taker",
                         "the lease did not move, so this control proves the "
                         "verb printed no refusal rather than that it acted")


class TheLockIsKeyedByWhatItProtectsTest(ReassignBase):
    """task/2454 — two spellings of one seat took two locks over one set of
    holdings, and the same spelling twice was excluded, which is exactly what
    made the lock look like it worked.

    `resolve_source` hands back the EXACT historical sid the operator named,
    and that is a landed cure worth keeping: the audit row must name the
    incarnation the operator named, not the seat's current binding. The lock
    key then inherited that sid, so one stable roster seat was reachable
    through as many lock files as it has remembered sessions, while
    `holdings(seat)` handed every one of those runs the same rows.
    """

    def sids(self):
        """One live roster row remembering TWO of its own sessions."""
        self.seat("holder", session="s-holder-old1")
        self.seat("holder", session="s-holder-new2")
        self.seat("taker", session="s-taker-0004")
        row = seats_roster.roster()["holder"]
        remembered = {str(s) for s in (row.get("sessions") or [])}
        self.assertLessEqual({"s-holder-old1", "s-holder-new2"}, remembered,
                             "fixture: the roster row does not remember both "
                             "sessions (%r), so there is no alias to collide"
                             % (sorted(remembered),))
        return "s-holder-old1", "s-holder-new2"

    def test_both_sids_still_resolve_to_the_SEAT_and_keep_their_own_sid(self):
        """THE PREMISE, pinned: the cure below may not reach resolution."""
        old, new = self.sids()
        # SPELLED OUT RATHER THAN LOOPED: a loop over a container that came
        # back empty asserts nothing, and these two calls are the whole claim.
        seat_o, session_o, how_o = seat_reassign.resolve_source(old)
        seat_n, session_n, how_n = seat_reassign.resolve_source(new)
        self.assertEqual((seat_o, seat_n), ("holder", "holder"),
                         "%s / %s" % (how_o, how_n))
        self.assertEqual((session_o, session_n), (old, new),
                         "the seat's CURRENT binding was substituted for the "
                         "incarnation the operator named (%s / %s)"
                         % (how_o, how_n))

    def test_the_OTHER_sid_of_the_SAME_seat_is_refused_while_a_run_holds(self):
        """A REAL RE-ENTRY UNDER A HELD LOCK, through the real verb.

        The second operator arrives while the first is between its movers —
        the window the lock exists to close — and names the same seat by its
        other sid. Nothing here reconstructs the key the verb computes.
        """
        old, new = self.sids()
        inner = {}
        real_leases = seat_reassign._move_leases

        def reenter(seat, to, out):
            with mock.patch.object(seat_reassign, "_move_leases", real_leases):
                inner["rc"], inner["lines"] = seat_reassign.reassign(
                    new, "taker", apply=True)
            return real_leases(seat, to, out)

        with mock.patch.object(seat_reassign, "_move_leases", reenter), \
                mock.patch.object(seat_reassign, "source_disposition",
                                  return_value=(seat_reassign.SOURCE_DEAD,
                                                "gone")):
            rc, lines = seat_reassign.reassign(old, "taker", apply=True)

        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertEqual(inner.get("rc"), 1,
                         "a second reassignment of the SAME seat ran while "
                         "the first held its lock: %s" % inner.get("lines"))
        self.assertTrue(
            any("another reassignment of this source" in l
                for l in inner.get("lines") or ()),
            "the refusal was not the concurrency one, so this arm proves "
            "nothing about the lock: %s" % inner.get("lines"))

    def test_TWO_DISTINCT_SEATS_whose_names_slug_alike_do_NOT_contend(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertFalse(the concurrency refusal fired); its unconditional positive control is the re-entrant rc 0 asserted two lines above on the same call, plus the three slug inequalities and the filename-safety must-hit at the top of the method
        """THE COST OF KEYING THE LOCK ON A NAME INSTEAD OF A SESSION.

        A session id is opaque hex and collides with nothing. A seat name is
        `[A-Za-z0-9._-]`, and the slug that makes it filename-safe mapped every
        non-`[alnum-_]` byte to `-` and then truncated — so `worker.a` and
        `worker-a`, two valid distinct canonical names, shared ONE lock file
        and excluded each other. That is an availability regression: a
        reassignment refused for a concurrency conflict that does not exist.
        """
        self.seat("worker.a", session="s-dot-0001")
        self.seat("worker-a", session="s-dash-0001")
        self.seat("taker", session="s-taker-0004")
        # THE SLUGS THEMSELVES, first: distinct names must not share a key.
        self.assertNotEqual(seat_reassign._lock_slug("worker.a"),
                            seat_reassign._lock_slug("worker-a"),
                            "two valid distinct seat names share one lock file")
        # AND A LONG PAIR SHARING A PREFIX, which a truncating slug merges.
        long_a, long_b = "w" * 80 + "A", "w" * 80 + "B"
        self.assertNotEqual(seat_reassign._lock_slug(long_a),
                            seat_reassign._lock_slug(long_b),
                            "truncation still loses identity")
        # MUST-HIT ON THE PRODUCER: the slug is still filename-safe, or this
        # arm would pass by making it unusable rather than injective.
        for name in ("worker.a", long_a):
            slug = seat_reassign._lock_slug(name)
            self.assertTrue(slug and "/" not in slug and "\x00" not in slug,
                            "the slug is not filename-safe: %r" % slug)

        # THE REAL OVERLAP, through the real verb: worker.a holds its lock and
        # worker-a runs INSIDE that window and must not be refused.
        inner = {}
        real_leases = seat_reassign._move_leases

        def reenter(seat, to, out):
            with mock.patch.object(seat_reassign, "_move_leases", real_leases):
                inner["rc"], inner["lines"] = seat_reassign.reassign(
                    "worker-a", "taker", apply=True)
            return real_leases(seat, to, out)

        with mock.patch.object(seat_reassign, "_move_leases", reenter), \
                mock.patch.object(seat_reassign, "source_disposition",
                                  return_value=(seat_reassign.SOURCE_DEAD,
                                                "gone")):
            rc, lines = seat_reassign.reassign("worker.a", "taker", apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertEqual(inner.get("rc"), 0,
                         "worker-a was refused while worker.a held the lock, "
                         "so the two still share a key: %s" % inner.get("lines"))
        self.assertFalse(
            any("another reassignment of this source" in l
                for l in inner.get("lines") or ()),
            "the concurrency refusal fired between DISTINCT seats: %s"
            % inner.get("lines"))

    def test_a_DIFFERENT_seat_still_runs_beside_it(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the re-entrant rc 0; its unconditional positive control is the claim asserted on @taker at the end of this method, a stronger observable the rung cannot pair with the rc because the printed lines and the claims table are different producers
        """THE CONTROL: the cure unifies aliases, it does not serialize the
        verb. Without this, keying every run on one constant would pass the
        arm above and break every concurrent reassignment on the fleet."""
        from helm import seats_claims
        old, _new = self.sids()
        self.seat("other", session="s-other-0007")
        # THE UNCONDITIONAL POSITIVE FOR THE RE-ENTRY: give the other seat
        # something to move, so "was not refused" is measured as "actually
        # moved" rather than as a quiet rc nobody can distinguish from a
        # re-entry that never ran.
        ok, whynot, _lease = seats_claims.claim("an-other-resource", "other",
                                                ttl=7200)
        self.assertTrue(ok, "fixture: the claim was refused (%s)" % whynot)
        inner = {}
        real_leases = seat_reassign._move_leases

        def reenter(seat, to, out):
            with mock.patch.object(seat_reassign, "_move_leases", real_leases):
                inner["rc"], inner["lines"] = seat_reassign.reassign(
                    "other", "taker", apply=True)
            return real_leases(seat, to, out)

        with mock.patch.object(seat_reassign, "_move_leases", reenter), \
                mock.patch.object(seat_reassign, "source_disposition",
                                  return_value=(seat_reassign.SOURCE_DEAD,
                                                "gone")):
            rc, lines = seat_reassign.reassign(old, "taker", apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertEqual(inner.get("rc"), 0,
                         "an unrelated seat was refused, so the lock now "
                         "serializes the whole verb: %s" % inner.get("lines"))
        held = [r for r in (seats_claims.claims_list(gc=False) or [])
                if r["resource"] == "an-other-resource"]
        self.assertEqual([r["holder_id"] for r in held], ["taker"],
                         "the re-entrant run moved nothing, so its rc 0 says "
                         "only that it printed no refusal: %r" % held)

    def lease_holders(self, resource):
        """Who holds one lease, through the census reader the verb uses."""
        from helm import seats_claims
        return [r.get("holder_id")
                for r in (seats_claims.claims_list(gc=False) or [])
                if isinstance(r, dict) and r.get("resource") == resource]

    def task_owners(self):
        """Owners of the task ledger AS THE VERB'S OWN READER SEES IT.

        `snapshot` answers a MAPPING, so iterating it yields ids; the verb
        takes `.values()` before it asks any row anything, and a check that
        skipped that step would read a string and say nothing about owners.
        """
        from helm import tasks
        raw, unavailable = tasks.snapshot(strict=True)
        self.assertFalse(unavailable, unavailable)
        rows = list(raw.values()) if isinstance(raw, dict) else list(raw or [])
        return [tasks.owner_of(r) for r in rows if isinstance(r, dict)]

    def _twin_overlap(self, outer, inner, resource):
        """Run `inner`'s apply INSIDE `outer`'s window and answer inner's rc.

        ONE HELPER FOR BOTH ARRIVAL ORDERS, because a scheme that excludes in
        one direction and passes in the other is exactly what a single-order
        arm cannot see."""
        from helm import seats_claims
        ok, message, _lease = seats_claims.claim(resource, inner, ttl=7200)
        self.assertTrue(ok, message)
        got = {}
        real_leases = seat_reassign._move_leases

        def reenter(seat, to, out):
            with mock.patch.object(seat_reassign, "_move_leases", real_leases):
                got["rc"], got["lines"] = seat_reassign.reassign(
                    inner, "taker", apply=True)
            return real_leases(seat, to, out)

        with mock.patch.object(seat_reassign, "_move_leases", reenter), \
                mock.patch.object(seat_reassign, "source_disposition",
                                  return_value=(seat_reassign.SOURCE_DEAD,
                                                "gone")):
            rc, lines = seat_reassign.reassign(outer, "taker", apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        return got

    def test_the_twin_names_coexist_in_BOTH_arrival_orders(self):  # noqa: VACUOUS_ASSERTION — the flagged absences are the two "no refusal fired" assertions; their unconditional positive control is the lease asserted on @taker after each overlap, a different producer the rung cannot pair with the printed lines
        """THE REVERSE DIRECTION, which one arm cannot see.

        `abc` and `abc-<abc's own digest>` are two valid distinct names that
        rendered ONE filename while the exclusive and compatibility spellings
        shared a prefix. With both older spellings taken SHARED they coexist
        whichever one arrives first — and an arm that only ever runs `abc`
        outermost would pass on a scheme that still refused the other way.
        """
        from helm import seats_claims
        twin = seat_reassign._lock_slug("abc")
        self.seat("abc", session="s-abc-00001")
        self.seat(twin, session="s-twin-0001")
        self.seat("taker", session="s-taker-0004")
        for outer, inner, resource in (("abc", twin, "twin-first"),
                                       (twin, "abc", "twin-second")):
            got = self._twin_overlap(outer, inner, resource)
            self.assertEqual(got.get("rc"), 0,
                             "%s was refused inside %s's window: %s"
                             % (inner, outer, got.get("lines")))
            self.assertFalse(
                any("exclusively" in line
                    or "another reassignment of this source" in line
                    for line in got.get("lines") or ()),
                "the two namespaces still meet (%s inside %s): %s"
                % (inner, outer, got.get("lines")))
            held = [r for r in (seats_claims.claims_list(gc=False) or [])
                    if isinstance(r, dict) and r.get("resource") == resource]
            self.assertEqual([r.get("holder_id") for r in held], ["taker"],
                             "the inner run moved nothing, so its rc 0 says "
                             "only that it printed no refusal: %r" % held)

    def test_a_seat_NAMED_like_another_seats_KEYED_lock_does_not_contend(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertFalse(a concurrency refusal fired); its unconditional positive control is the re-entrant rc 0 asserted beside it and the lease moved to @taker at the end of the method
        """THE NAMESPACE COLLISION A SECOND LOCK FILE CAN REINTRODUCE.

        A slug keeps only characters `str.isalnum()` accepts plus `-` and
        `_`, and a keyed slug is exactly that shape, so a seat may be
        LITERALLY NAMED the keyed spelling of another seat. Sharing one filename prefix, seat
        `abc` and a seat named `abc-<abc's own digest>` render the SAME path —
        one as its keyed spelling, the other as its legacy one — and then the
        first seat's EXCLUSIVE keyed take blocks the second seat's SHARED
        legacy take. That is cross-seat contention between two valid distinct
        names, which is the defect this lock key exists to end, arriving one
        namespace over from where it was cured.

        THE NAME IS DERIVED FROM THE PRODUCER, never transcribed: a hand-typed
        digest would stop modelling the collision the moment the slug changed.
        """
        from helm import seats_claims
        twin = seat_reassign._lock_slug("abc")
        self.assertNotEqual(twin, "abc")
        self.assertEqual(seat_reassign._legacy_lock_slug(twin), twin,
                         "fixture: the legacy slug must pass %r through "
                         "unchanged, or the two filenames were never equal "
                         "and this arm models nothing" % twin)
        self.seat("abc", session="s-abc-00001")
        self.seat(twin, session="s-twin-0001")
        self.seat("taker", session="s-taker-0004")
        ok, message, _lease = seats_claims.claim("twin-resource", twin,
                                                 ttl=7200)
        self.assertTrue(ok, message)

        inner = {}
        real_leases = seat_reassign._move_leases

        def reenter(seat, to, out):
            with mock.patch.object(seat_reassign, "_move_leases", real_leases):
                inner["rc"], inner["lines"] = seat_reassign.reassign(
                    twin, "taker", apply=True)
            return real_leases(seat, to, out)

        with mock.patch.object(seat_reassign, "_move_leases", reenter), \
                mock.patch.object(seat_reassign, "source_disposition",
                                  return_value=(seat_reassign.SOURCE_DEAD,
                                                "gone")):
            rc, lines = seat_reassign.reassign("abc", "taker", apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertEqual(inner.get("rc"), 0,
                         "a seat named like another seat's keyed lock was "
                         "refused: %s" % inner.get("lines"))
        self.assertFalse(
            any("exclusively" in line or "another reassignment of this source"
                in line for line in inner.get("lines") or ()),
            "the two namespaces still meet: %s" % inner.get("lines"))
        held = [r for r in (seats_claims.claims_list(gc=False) or [])
                if isinstance(r, dict) and r.get("resource") == "twin-resource"]
        self.assertEqual([r.get("holder_id") for r in held], ["taker"],
                         "the re-entrant run moved nothing, so its rc 0 says "
                         "only that it printed no refusal: %r" % held)

    def lock_files(self):
        """Every lock file the verb has actually created, by basename.

        THE FORMAT IS READ OFF THE PRODUCER RATHER THAN RETYPED. A hand-written
        filename stops modelling the scheme the moment the scheme changes, and
        the arms below hold these files EXCLUSIVELY to stand in for an older
        helm — so a stale spelling would make them hold a file nothing uses and
        pass while proving nothing.
        """
        here = os.path.dirname(seat_reassign.ledger_path())
        return sorted(n for n in os.listdir(here)
                      if n.startswith("reassign") and n.endswith(".lock"))

    def test_one_apply_creates_one_exclusive_and_two_rendezvous_files(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertNotIn('.', slug) over the hostile names; its unconditional positive control is the three-filename equality asserted on the same call, which fails loudly if the verb created nothing
        """THE PREMISE THE PREDECESSOR ARMS REST ON, asserted not assumed.

        Two older spellings are rendezvous points taken SHARED so each
        predecessor is excluded in both arrival orders, and the exclusive take
        lives in a namespace no seat name can spell — a slug emits only
        `[A-Za-z0-9_-]`, so `reassign.` is unreachable from any of them.
        """
        self.seat("holder", session="s-holder-0001")
        self.seat("taker", session="s-taker-0004")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "gone")):
            rc, lines = seat_reassign.reassign("holder", "taker", apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        names = self.lock_files()
        self.assertEqual(names, sorted([
            "reassign-%s.lock" % seat_reassign._legacy_lock_slug("holder"),
            "reassign-%s.lock" % seat_reassign._lock_slug("holder"),
            "reassign.%s.lock" % seat_reassign._lock_slug("holder")]), names)
        # AND THE NAMESPACES ARE DISJOINT BY CONSTRUCTION, not by inspection.
        # THE PROOF IS ABOUT THE DOT, NOT ABOUT ASCII: both slugs keep a
        # character only when `str.isalnum()` is true of it — which holds for
        # letters and digits in every script, so a slug may carry non-ASCII —
        # or when it is `-` or `_`. A dot is none of those and is always
        # substituted, so no seat name can produce the exclusive spelling.
        for hostile in ("abc", "a.b", "reassign.x", "." * 8, "x" * 90):
            for slug in (seat_reassign._legacy_lock_slug(hostile),
                         seat_reassign._lock_slug(hostile)):
                self.assertNotIn(".", slug,
                                 "a slug emitted a dot, so the exclusive "
                                 "namespace is reachable from a seat name")

    def _predecessor_blocks_us(self, path, target="other"):
        """Hold one lock file EXCLUSIVELY, as an older helm does, and run."""
        import fcntl
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream.seek(0)
            stream.truncate()
            stream.write(target)
            stream.flush()
            with mock.patch.object(seat_reassign, "source_disposition",
                                   return_value=(seat_reassign.SOURCE_DEAD,
                                                 "gone")):
                return seat_reassign.reassign("holder", "taker", apply=True)

    def test_a_KEYED_only_predecessor_is_excluded_too(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the task still owned by 'holder' under the refusal; its unconditional positive control is the second half of this same method, where the hold is released and the SAME call on the SAME fixture moves that task to 'taker'
        """THE PREDECESSOR THE FIRST CORRECTION WOULD HAVE LOST.

        An older helm that keyed the lock on the seat holds
        `reassign-<keyed>.lock` EXCLUSIVELY. Moving this version's exclusive
        take to its own namespace and keeping only the LEGACY rendezvous would
        have let that helm and this one run the same source at once — the
        exclusion would have been silently dropped while the namespace fix
        looked complete. It is a rendezvous too, held shared, so it excludes.
        """
        from helm import seats_claims, tasks
        self.seat("holder", session="s-holder-0001")
        self.seat("taker", session="s-taker-0004")
        self.seat("other", session="s-other-0007")
        row, err = tasks.add("a real holding", "holder")
        self.assertIsNone(err, err)
        ok, message, _lease = seats_claims.claim("res-held", "holder",
                                                 ttl=7200)
        self.assertTrue(ok, message)
        keyed = os.path.join(
            os.path.dirname(seat_reassign.ledger_path()),
            "reassign-%s.lock" % seat_reassign._lock_slug("holder"))
        rc, lines = self._predecessor_blocks_us(keyed)
        self.assertEqual(rc, 1, "\n".join(lines))
        self.assertTrue(any("holds %s exclusively" % os.path.basename(keyed)
                            in line for line in lines), lines)
        # BOTH SURFACES, because the brief claims both and one assertion
        # cannot support it: a refusal that stopped the task mover and not the
        # lease mover looks identical here unless the lease is read back too.
        self.assertEqual(self.task_owners(), ["holder"],
                         "the task moved under a refusal")
        self.assertEqual(self.lease_holders("res-held"), ["holder"],
                         "the lease moved under a refusal")
        # THE UNCONDITIONAL POSITIVE: the hold is gone, and the SAME call on
        # the SAME fixture moves the holding.
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "gone")):
            rc, lines = seat_reassign.reassign("holder", "taker", apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertEqual(self.task_owners(), ["taker"])
        self.assertEqual(self.lease_holders("res-held"), ["taker"])

    def test_an_OLDER_exclusive_holder_refuses_before_a_single_row_moves(self):  # noqa: VACUOUS_ASSERTION — the flagged absences are the two owners still reading 'holder' under the refusal; their unconditional positive control is the SECOND half of this same method, where the hold is released and the SAME call on the SAME fixture moves both surfaces to 'taker'
        """THE MIXED-VERSION OVERLAP, HELD FOR REAL.

        A helm that spells this source's lock the older way takes a file this
        one would otherwise never touch, so neither excludes the other: both
        census, both move what they find, both return 0, and one source's
        holdings end up split across two targets with nothing observable
        saying so — two empty final censuses and two zero exit codes.

        So the older spelling is taken SHARED here, and only an exclusive
        holder can block that. This arm is that holder: a real flock held on
        the real path the older spelling produces, naming a DIFFERENT target,
        across a real apply with a real task and a real live lease.
        """
        import fcntl
        from helm import seats_claims, tasks
        self.seat("holder", session="s-holder-0001")
        self.seat("taker", session="s-taker-0004")
        self.seat("other", session="s-other-0007")
        row, err = tasks.add("a real holding", "holder")
        self.assertIsNone(err, err)
        ok, message, _lease = seats_claims.claim("res-held", "holder",
                                                 ttl=7200)
        self.assertTrue(ok, message)

        legacy = os.path.join(
            os.path.dirname(seat_reassign.ledger_path()),
            "reassign-%s.lock" % seat_reassign._legacy_lock_slug("holder"))
        os.makedirs(os.path.dirname(legacy), exist_ok=True)
        with open(legacy, "a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream.seek(0)
            stream.truncate()
            stream.write("other")            # a DIFFERENT destination
            stream.flush()
            with mock.patch.object(seat_reassign, "source_disposition",
                                   return_value=(seat_reassign.SOURCE_DEAD,
                                                 "gone")):
                rc, lines = seat_reassign.reassign("holder", "taker",
                                                   apply=True)
        self.assertEqual(rc, 1, "\n".join(lines))
        self.assertTrue(
            any("holds %s exclusively" % os.path.basename(legacy) in line
                for line in lines),
            "the refusal did not come from the older spelling: %s" % lines)
        self.assertTrue(any("toward @other" in line for line in lines),
                        "the refusal did not name the holder's destination, "
                        "so it read nothing the holder wrote: %s" % lines)

        # AND NOTHING MOVED. A refusal that lands after the movers is worth
        # nothing, so both populated surfaces are read back on the source.
        self.assertEqual(self.task_owners(), ["holder"],
                         "the task moved under a refusal")
        leases = [r for r in (seats_claims.claims_list(gc=False) or [])
                  if isinstance(r, dict) and r.get("resource") == "res-held"]
        self.assertEqual([r.get("holder_id") for r in leases], ["holder"],
                         "the lease moved under a refusal")

        # THE UNCONDITIONAL POSITIVE ON BOTH OBSERVABLES: the exclusive hold
        # is gone with the `with`, and the SAME call on the SAME fixture now
        # moves both surfaces. Without this the two equalities above are
        # satisfied by a verb that can no longer move anything at all.
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "gone")):
            rc, lines = seat_reassign.reassign("holder", "taker", apply=True)
        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertEqual(self.task_owners(), ["taker"])
        leases = [r for r in (seats_claims.claims_list(gc=False) or [])
                  if isinstance(r, dict) and r.get("resource") == "res-held"]
        self.assertEqual([r.get("holder_id") for r in leases], ["taker"])


class TheLockHandlerCannotReachTheBodyTest(ReassignBase):
    """A failure while MOVING the work is not a failure to take the lock.

    The acquisition used to sit in a try/except OSError whose try-block also
    contained the yield. In a contextmanager the yield IS the with-body, so an
    OSError raised while moving rows was thrown back in at that point, caught
    by the lock's own handler, and answered with a SECOND yield — turning a
    real disk failure into "generator didn't stop after throw()" and reporting
    a lock problem for a lock that had been taken perfectly well.

    """

    def test_a_body_OSError_propagates_as_ITSELF(self):
        mine = OSError("the ledger write failed, not the lock")
        with self.assertRaises(OSError) as caught:  # noqa: VACUOUS_ASSERTION — not an absence: `held` is asserted True inside the body so the arm cannot pass by never entering the lock, and the assertIs below is a positive identity check against this exact object
            with seat_reassign.source_lock("src-seat", "taker") as (held, why):
                self.assertTrue(held, "the lock was not taken, so this arm "
                                      "would prove nothing about the body: %s"
                                      % why)
                raise mine
        self.assertIs(caught.exception, mine,
                      "the body's own exception did not come back out — the "
                      "lock's handler swallowed or replaced it")

    def test_the_lock_is_RELEASED_after_the_body_raises(self):
        """The half a narrower fix would miss. Releasing is what a lock owes on
        every exit, not only the clean one; if the finally stopped running, the
        next reassignment of the same source would refuse forever."""
        try:
            with seat_reassign.source_lock("src-seat", "taker") as (held, _):
                self.assertTrue(held)
                raise OSError("boom")
        except OSError:
            pass
        with seat_reassign.source_lock("src-seat", "taker") as (held, why):
            self.assertTrue(held,
                            "the lock survived the raising body, so this "
                            "source can never be reassigned again: %s" % why)

    def test_a_REAL_lock_conflict_is_still_reported(self):
        """THE CONTROL. Both arms above pass identically if source_lock stopped
        refusing anything at all, which would delete the concurrency guard
        rather than fix it."""
        with seat_reassign.source_lock("src-seat", "taker") as (held, _):
            self.assertTrue(held)
            with seat_reassign.source_lock("src-seat", "other") as (h2, why2):
                self.assertFalse(h2, "a second holder was admitted")
                self.assertIn("cannot both be right", why2 or "")


class TheCompositionIsItsOwnClaimTest(ReassignBase):
    """THE SEAM, AND IT IS A THIRD ARTIFACT.

    the dead-seat holdings mandate was built by three writers in one worktree: this verb, the
    send-BODY storage in dispatches.py, and the lease-holder rebind in
    seats_claims.py. EACH HALF BEING GREEN PROVES NOTHING ABOUT THE
    COMPOSITION — every half was verified against a tree the other two had not
    landed in, so all three can be individually correct and jointly wrong.

    THE CLAIM HERE IS THE OWNER'S BAR, VERBATIM: move a dead seat's holdings
    through the verb and have the recipient read the BRIEF OFF THE ROW, with no
    DM. That one sentence spans all three halves, and a green
    `tests.test_seat_reassign` plus a green `tests.test_dispatches` do not add
    up to it. This is where it is asserted as one thing.
    """

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)
        for cmd in (("init", "-q"), ("config", "user.email", "t@t"),
                    ("config", "user.name", "t")):
            subprocess.run(("git",) + cmd, cwd=self.repo, capture_output=True)
        open(os.path.join(self.repo, "a.txt"), "w").write("a\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "a"], cwd=self.repo,
                       capture_output=True)
        self.tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                  capture_output=True, text=True).stdout.strip()
        from tests._tmphome import pin_dispatch_home
        pin_dispatch_home(self, self.repo)

    def test_the_BRIEF_reaches_the_new_recipient_through_the_verb(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertNotIn(old-recipient) on the moved row; its unconditional positive is the byte-equal brief asserted on the SAME row two lines in this method, which the rung cannot match because the body reader and the recipient field are different producers
        """THE ACCEPTANCE TEST FOR THE WHOLE ROW, not for either half alone."""
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.seat("seat-a", session="s-seat-a-0001")
        self.seat("seat-b", session="s-seat-b-0001")
        brief = ("REVIEW the thing. Attack the loader identity first — it "
                 "reads code objects off bound callables and I have not "
                 "proven what it does with a decorated test.")
        row, why, _sent = dispatches.send(
            "seat-a", "composed-lane", brief, self.tip, kind="review",
            new_work=True, repo=self.repo)
        self.assertIsNotNone(row, "fixture: the send was refused (%s)" % why)

        # CONTROL BEFORE THE MOVE: the brief is readable on the ORIGINAL row,
        # so a byte-equal read after the move is about travel and not about a
        # reader that returns the same string for everything.
        before, absent = dispatches.body_of(row)
        self.assertIsNone(absent, "fixture: the brief was not stored at all")
        self.assertEqual(before, brief)

        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "pane gone")):
            rc, lines = seat_reassign.reassign(
                "seat-a", "seat-b", reason="dead seat", apply=True)
        # RE-DERIVED AFTER THE TRANSACTIONAL OBJECT WAS CUT, and the tripwire
        # that forced it worked: the old form asserted rc 1 AND asserted
        # CONSUMERS_WIRED was False, so deleting that flag made these arms fail
        # loudly instead of passing under a meaning that had changed beneath
        # them. rc is now 0 on a clean run — there is no operation left that
        # could be incomplete, so a run that moved everything it censused has
        # nothing to withhold.
        self.assertEqual(rc, 0, "\n".join(lines))

        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable, "the ledger became unreadable")
        moved = [r for r in snap.values()
                 if isinstance(r, dict) and r.get("recipient") == "seat-b"
                 and r.get("lane") == "composed-lane"]
        self.assertEqual(len(moved), 1, "expected exactly one moved row: %r"
                         % ([r.get("id") for r in moved],))

        # THE BAR ITSELF: the brief is on the row the new recipient reads.
        got, why_absent = dispatches.body_of(moved[0])
        self.assertIsNone(why_absent,
                          "the brief did NOT travel (%s) — the verb moved a "
                          "row nobody can read, which relocates the "
                          "re-briefing rather than removing it" % why_absent)
        self.assertEqual(got, brief, "the brief travelled but changed")
        self.assertNotEqual(moved[0].get("recipient"), "seat-a")

    def test_the_MOVE_does_not_re_author_the_row(self):
        """The half that is invisible until a land-request renders it: `add`
        stamps the ACTING seat as `sender`, and landreq projects `author` off
        that field. A move that re-authors silently rewrites whose work it is."""
        os.environ["HELM_CHAT_NAME"] = "integrator"
        self.seat("seat-a", session="s-seat-a-0002")
        self.seat("seat-b", session="s-seat-b-0002")
        row, why, _s = dispatches.send(
            "seat-a", "authorship-lane", "a brief with an author", self.tip,
            kind="review", new_work=True, repo=self.repo)
        self.assertIsNotNone(row, "fixture: send refused (%s)" % why)
        original_sender = row.get("sender")
        self.assertTrue(original_sender, "fixture: the row has no sender")

        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")):
            rc, lines = seat_reassign.reassign(
                "seat-a", "seat-b", reason="dead seat", apply=True)
        # RE-DERIVED AFTER THE TRANSACTIONAL OBJECT WAS CUT, and the tripwire
        # that forced it worked: the old form asserted rc 1 AND asserted
        # CONSUMERS_WIRED was False, so deleting that flag made these arms fail
        # loudly instead of passing under a meaning that had changed beneath
        # them. rc is now 0 on a clean run — there is no operation left that
        # could be incomplete, so a run that moved everything it censused has
        # nothing to withhold.
        self.assertEqual(rc, 0, "\n".join(lines))

        snap, _u = dispatches.snapshot()
        moved = [r for r in snap.values()
                 if isinstance(r, dict) and r.get("recipient") == "seat-b"
                 and r.get("lane") == "authorship-lane"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].get("sender"), original_sender,
                         "the move re-authored the row: a land-request reads "
                         "`author` off `sender`, so this would rewrite whose "
                         "work it is")


class TheSourceIsASessionBeforeItIsANameTest(ReassignBase):
    """SEAM 1. This verb's population is seats whose NAME stopped being a
    reliable key — a rename carries the session and orphans every row that
    spelled the old name. A resolver that asked for the name first would be
    correct only in the cases where the verb was not needed."""

    def test_a_session_id_resolves_to_its_seat(self):
        sid = "abcdef0123456789"
        self.seat("ghost", session=sid)
        seat, session, how = seat_reassign.resolve_source(sid)
        self.assertEqual(seat, "ghost")
        self.assertEqual(session, sid)
        self.assertIn("session", how)
        # MUST-MISS on the same producer: a token that is NOT a session and
        # NOT a roster key must not resolve to some near neighbour.
        other, _s, ohow = seat_reassign.resolve_source("ghos")
        self.assertEqual(other, "ghos", ohow)
        self.assertIn("ORPHANED", ohow)

    def test_a_name_with_NO_roster_row_is_the_ORPHAN_case_not_an_error(self):
        """The pure rename-orphan: the holdings exist, spelled with a name
        nothing answers to any more, and moving them is exactly the job.
        Refusing here would refuse the verb's whole reason to exist."""
        seat, _s, how = seat_reassign.resolve_source("was-hc2")
        self.assertEqual(seat, "was-hc2")
        self.assertIn("ORPHANED", how)
        # CONTROL, same producer: a name that DOES have a row reports the
        # other way, so "ORPHANED" is a measurement and not the only answer.
        self.seat("present")
        seat2, _s2, how2 = seat_reassign.resolve_source("present")
        self.assertEqual(seat2, "present")
        self.assertNotIn("ORPHANED", how2)

    def test_an_UNREADABLE_roster_refuses_rather_than_downgrading(self):
        """An unreadable roster is not an absent seat. Falling through to the
        orphan branch would silently downgrade every resolution to its weakest
        form at the exact moment the strongest evidence became unavailable."""
        self.seat("real")
        # THE PLANT IS THE TRI-STATE, NOT A RAISE, because the real reader
        # cannot raise. `seats_common.roster()` is `pk.read_json(path, {}) or
        # {}` and that swallows missing, unreadable, malformed and
        # wrong-shaped alike — measured: all three failures answer `{}` with
        # no exception, and a roster file holding a JSON array comes back as
        # that LIST. So a planted RuntimeError rehearsed a path production
        # never takes; `roster_checked` is the reader that reports the real
        # failure, and it reports it as a value.
        with mock.patch.object(seats_roster, "roster_checked",
                               return_value=({}, True)):
            seat, _s, why = seat_reassign.resolve_source("real")
        self.assertIsNone(seat)
        self.assertIn("UNKNOWN", why)
        # CONTROL on the same producer with the roster readable.
        ok, _s2, _h = seat_reassign.resolve_source("real")
        self.assertEqual(ok, "real")


class TheTargetResolvesThroughOneDoorTest(ReassignBase):
    """SEAM 2. Everything downstream stores whatever this returns, so when a
    ROLE becomes addressable this function learns it and the four ledgers do
    not change."""

    def test_an_exact_name_beats_a_family_of_the_same_spelling(self):
        """A family token can also be a real seat NAME — that collision is live  # noqa: SEAT_NAME — prose naming the shape, not a fixture value
        on this fleet, which is why the fixture models it with seat-a/seat-a-2.
        Resolving the family first would silently retarget a row addressed to
        the seat."""
        self.seat("seat-a", session="s-seat-a-0000")
        self.seat("seat-a-2", session="s-seat-a-2000")
        to, how = seat_reassign.resolve_target("seat-a")
        self.assertEqual(to, "seat-a")
        self.assertIn("exact", how)

    def test_a_family_with_ONE_live_seat_resolves_to_it(self):
        self.seat("seat-b-1", session="s-seat-b-1000")
        to, how = seat_reassign.resolve_target("seat-b")
        self.assertEqual(to, "seat-b-1")
        self.assertIn("family", how)

    def test_a_family_with_TWO_live_seats_REFUSES_rather_than_picking(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertIsNone(to) on the ambiguous call; its control is the SAME call after one member is disowned, which the rung cannot credit because a second resolve_target call is a different opaque call identity, and that the two calls differ IS the claim
        """MUST-MISS. Two is an ambiguity to report, never a tie to break
        silently — the same rule `seat_for_session` states for its own
        multi-match case."""
        self.seat("seat-c-1", session="s-seat-c-1000")
        self.seat("seat-c-2", session="s-seat-c-2000")
        to, why = seat_reassign.resolve_target("seat-c")
        self.assertIsNone(to)
        self.assertIn("FAMILY", why)
        # CONTROL on the same producer: drop one and it resolves, so the
        # refusal is about the ambiguity and not about the family form.
        seats_roster.disown_session("seat-c-2", "s-seat-c-2000")
        to2, _h = seat_reassign.resolve_target("seat-c")
        self.assertEqual(to2, "seat-c-1")

    def test_a_target_nothing_answers_to_REFUSES(self):
        """The ONE measured contradiction on the target side. Holdings may not
        be moved to a name that cannot receive them."""
        self.seat("real")
        to, why = seat_reassign.resolve_target("nobody")
        self.assertIsNone(to)
        self.assertIn("no seat", why)
        ok, _h = seat_reassign.resolve_target("real")
        self.assertEqual(ok, "real", "control: a real seat still resolves")


class AliveSeatIsNotReassignableTest(ReassignBase):
    """THE MUST-MISS THE ROW ASKED FOR: a live source with an active turn is
    not reassignable without --force --reason."""

    def test_a_LIVE_source_refuses_and_a_DEAD_one_proceeds(self):
        self.seat("worker", session="s-worker-0001")
        self.seat("taker", session="s-taker-0001")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_LIVE,
                                             "pane is live")):
            rc, lines = seat_reassign.reassign("worker", "taker")
        self.assertEqual(rc, 1)
        self.assertTrue(any("measurably LIVE" in l for l in lines), lines)
        # CONTROL, same call shape, only the disposition changed: a DEAD
        # source reaches the manifest. Without this the arm passes for a verb
        # that refuses everything.
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "pane gone")):
            rc2, lines2 = seat_reassign.reassign("worker", "taker")
        self.assertEqual(rc2, 0, lines2)
        self.assertTrue(any("DRY RUN" in l for l in lines2), lines2)

    def test_force_without_a_reason_refuses(self):
        self.seat("worker", session="s-worker-0002")
        self.seat("taker", session="s-taker-0002")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_LIVE, "up")):
            rc, lines = seat_reassign.reassign("worker", "taker", force=True)
        self.assertEqual(rc, 1)
        self.assertTrue(any("--force needs --reason" in l for l in lines), lines)
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_LIVE, "up")):
            rc2, lines2 = seat_reassign.reassign("worker", "taker", force=True,
                                                 reason="judgment call")
        self.assertEqual(rc2, 0, lines2)

    def test_a_NATIVE_seat_the_family_register_cannot_place_still_REFUSES(self):
        """THE MUST-MISS FAILED OPEN FOR EVERY NATIVE SEAT, measured on the
        live fleet before this arm existed.

        `prove_reboot_dead` resolves a seat through its FAMILY REGISTER —
        codex, gemini, grok, kimi and their -N instances — so for a native
        claude seat it answers "register sits under no known family" and
        returns UNKNOWN. UNKNOWN proceeds by design, so the guard protecting a
        LIVE source fired for proxy seats and waved through every native one.
        The verb was willing to move the holdings of a seat that was working.

        THE SECOND OPINION IS THE ROSTER SESSION AGAINST THE LIVE SID SET, and
        it is the third candidate — the first two were rejected BY
        MEASUREMENT. `claim_holder_liveness(<name>)` answers "live" for ANY
        string, including names that are not seats, so a refusal built on it
        refuses everything. The per-lease `liveness` field read "live" for all
        16 rows on the fleet at the time, which leaves it UNFALSIFIABLE rather
        than proven wrong — and a discriminator that cannot be made to say
        "no" is not one to gate on.

        CONSULTED ONLY TO REFUSE. It cannot prove death (a seat may be alive
        with no recorded session), so a negative leaves UNKNOWN standing."""
        from helm import sessions
        self.seat("seat-a", session="s-seat-a-live")
        self.seat("seat-b", session="s-seat-b-0009")

        # The family register cannot place either seat — that is the whole
        # premise, and it is what the fixture's bare names guarantee.
        with mock.patch.object(sessions, "live_sids",
                               return_value={"s-seat-a-live"}), \
                mock.patch.object(
                    seat_reassign, "source_disposition",
                    wraps=seat_reassign.source_disposition):
            rc, lines = seat_reassign.reassign("seat-a", "seat-b")
        self.assertEqual(rc, 1, "\n".join(lines))
        self.assertTrue(any("measurably LIVE" in l for l in lines), lines)
        self.assertTrue(any("second surface measured it working" in l
                            for l in lines),
                        "the refusal must name WHICH surface, or an operator "
                        "cannot check it: %r" % (lines,))

        # MUST-MISS ON THE SAME PRODUCER: a seat whose session is NOT in the
        # live set must NOT be refused — the second opinion may only refuse,
        # never authorize, and a version that refused on absence would make
        # this verb useless the morning after a reboot.
        with mock.patch.object(sessions, "live_sids", return_value=set()):
            rc2, lines2 = seat_reassign.reassign("seat-a", "seat-b")
        self.assertEqual(rc2, 0, "\n".join(lines2))
        self.assertTrue(any("DRY RUN" in l for l in lines2), lines2)

    def test_UNKNOWN_liveness_PROCEEDS_because_absence_is_not_a_contradiction(self):
        """Owner canon for this row: refuse only on measured contradiction,
        never on absence. This verb exists for the morning after a reboot,
        when nothing can be measured about a process that no longer exists —
        a version that refused on UNKNOWN would be useless exactly then."""
        self.seat("ghosted", session="s-ghost-0001")
        self.seat("taker", session="s-taker-0003")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_UNKNOWN,
                                             "census unreadable")):
            rc, lines = seat_reassign.reassign("ghosted", "taker")
        self.assertEqual(rc, 0, lines)
        self.assertTrue(any("unknown" in l for l in lines), lines)
        self.assertTrue(any("census unreadable" in l for l in lines),
                        "the unknown must be NAMED, not swallowed: %r" % lines)


class TheManifestCannotPrintAFalseZeroTest(ReassignBase):
    """The property this whole manifest exists to have, and the one place it
    already failed: an unreadable surface must never render as an empty one."""

    def test_a_task_ledger_of_NON_MAPPINGS_reads_UNREAD_not_zero(self):
        """MEASURED IN THE FIRST DOGFOOD RUN. `tasks.rows()` returns a MAPPING
        id -> row, so iterating it directly yields the KEYS — strings — and an
        `isinstance(row, dict)` filter dropped all 1710 live rows and printed
        "tasks 0" WITH NO EXCEPTION. The UNREAD channel could not see it,
        because nothing raised. So the leg asserts its SHAPE: a store holding
        rows none of which is a mapping is a reader that no longer fits."""
        self.seat("holder")
        # THE DOUBLE MUST SIT ON THE FUNCTION THE LEG ACTUALLY CALLS, and
        # that is `tasks.snapshot` — the leg reads the unavailable channel, so
        # a double on any other reader is inert. An inert double does not fail
        # loudly: the arm runs against the LIVE ledger and asserts whatever
        # that happens to hold. Whenever this leg's reader changes, this plant
        # moves with it or the arm silently stops testing anything.
        with mock.patch.object(
                tasks, "snapshot",
                return_value=({"a filed row": "not-a-mapping"}, None)):
            man, unread = seat_reassign.holdings("holder")
        self.assertEqual(man["tasks"], [])
        # THROUGH THE PUBLIC READER, not by substring on the entry. An unread
        # entry now carries its surfaces as data, so `"..." in u` tests dict
        # KEYS and silently answers False — which is how this arm went red
        # when the shape changed, and what it would have hidden if the text
        # had happened to match a key.
        self.assertTrue(
            any("no longer fits" in seat_reassign.unread_text(u)
                for u in unread),
            "a shape mismatch must report UNREAD: %r" % unread)
        self.assertIn("tasks", seat_reassign.unread_surfaces(unread),
                      "the entry did not name the surface it darkened")
        # CONTROL on the same producer: a well-shaped ledger with a matching
        # row is COUNTED, so this arm is about the shape and not about a
        # leg that reports UNREAD for everything.
        good = {"a filed row": {"id": "a filed row", "owner": "holder",
                           "status": "open", "title": "t"}}
        with mock.patch.object(tasks, "snapshot", return_value=(good, None)):
            man2, unread2 = seat_reassign.holdings("holder")
        self.assertEqual([r["id"] for r in man2["tasks"]], ["a filed row"])
        self.assertEqual(unread2, [])

    def test_the_ANTI_PARTIAL_GUARD_SEES_a_returned_unavailable_not_only_a_raise(self):
        """THE GUARD EXISTS AND THE DEFECT WAS THAT IT COULD NOT SEE THIS ONE.

        `reassign` refuses outright when any surface is unread — "a seat split
        across two owners is worse than a seat that has not moved" — and that
        refusal keys ENTIRELY on the `unread` channel. A task ledger read
        through `rows()` put nothing in that channel, because the unavailable
        value is dropped at the subscript and NOTHING RAISES. So the guard
        never fired, the move proceeded across the surfaces it could see, and
        every task row stayed behind on the seat being moved away from — the
        exact partial this verb exists to prevent.

        Its sibling above plants a RAISE. Production does not raise here; it
        RETURNS a reason. That is the whole shape of the defect, so it needs
        its own arm rather than sharing one.
        """
        self.seat("holder", session="s-holder-0001")
        self.seat("taker", session="s-taker-0004")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")), \
                mock.patch.object(tasks, "snapshot",
                                  return_value=({}, "ledger unreadable: boom")):
            rc, lines = seat_reassign.reassign("holder", "taker", apply=True)
        self.assertEqual(rc, 1, lines)
        self.assertTrue(any("PARTIAL" in l for l in lines),
                        "the verb did not name the partial it was refusing to "
                        "make: %s" % lines)
        self.assertTrue(any("nothing was moved" in l for l in lines),
                        "the refusal did not state that nothing moved, which "
                        "is the only sentence that tells an operator the seat "
                        "is not split: %s" % lines)
        # CONTROL: the same call with every surface readable is NOT refused,
        # so this arm measures the unread channel and not a verb that refuses
        # everything.
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")):
            rc2, lines2 = seat_reassign.reassign("holder", "taker")
        self.assertNotEqual(rc2, 1, lines2)

    def test_an_UNREAD_surface_makes_even_a_DRY_RUN_non_zero(self):
        """rc 0 is the only part of this output a script reads. A manifest
        that could not read one of the four ledgers is not a preview of the
        whole move."""
        self.seat("holder", session="s-holder-0001")
        self.seat("taker", session="s-taker-0004")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")), \
                mock.patch.object(tasks, "snapshot",
                                  side_effect=RuntimeError("ledger torn")):
            rc, lines = seat_reassign.reassign("holder", "taker")
        self.assertEqual(rc, 1, lines)
        self.assertTrue(any("UNREAD" in l for l in lines), lines)
        # CONTROL: the same call with every surface readable is rc 0.
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")):
            rc2, lines2 = seat_reassign.reassign("holder", "taker")
        self.assertEqual(rc2, 0, lines2)

    def test_the_manifest_never_folds_UNREAD_into_the_TOTAL(self):
        man = {k: [] for k in seat_reassign.SURFACES}
        man["tasks"] = [{"id": "a filed row"}]
        lines = seat_reassign.manifest_lines("a", "b", man, ["leases: boom"])
        self.assertTrue(any("TOTAL 1 holding" in l for l in lines), lines)
        self.assertTrue(any("not counted above" in l for l in lines), lines)


class TheSameSeatOnBothSidesMovesNothingTest(ReassignBase):
    def test_source_equals_target_refuses(self):
        self.seat("solo", session="s-solo-0001")
        rc, lines = seat_reassign.reassign("solo", "solo")
        self.assertEqual(rc, 1)
        self.assertTrue(any("already holds these" in l for l in lines), lines)
        # CONTROL: a different target gets past this gate.
        self.seat("other", session="s-other-0001")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")):
            rc2, lines2 = seat_reassign.reassign("solo", "other")
        self.assertEqual(rc2, 0, lines2)



if __name__ == "__main__":
    unittest.main()


class ASentRowIsNotAReceivedRowTest(ReassignBase):
    """EVERY dispatch row went through `dispatches.rebind`, which moves a
    row's RECIPIENT.

    For an INBOUND row that is exactly right. For one the seat SENT it hands a
    third party's review obligation to the successor and leaves the orphaned
    sender — the thing the verb was asked to repair — orphaned. The manifest
    had already stamped each outbound entry `role="sender"`, so the fact that
    separates them was in the rows the whole time.
    """

    def _manifest(self):
        return ({"leases": [], "tasks": [],
                 "dispatch_in": [{"id": "in0000000001", "lane": "lane-in",
                                  "recipient": "worker", "role": "recipient"}],
                 "dispatch_out": [{"id": "out000000002", "lane": "lane-out",
                                   "recipient": "someone-else",
                                   "role": "sender"}]}, [])

    def test_the_SENT_row_moves_by_CUSTODY_and_never_by_rebind(self):
        """Both halves of the rule in one arm.

        rebind moves the RECIPIENT, so pointing it at a sent row hijacks a
        reviewer who is doing nothing wrong. But merely REPORTING the row and
        moving on trades a loud failure for a quiet one — the obligation is
        still stranded, and ONE VERB is supposed to move EVERY holding. So the
        outbound row must reach `mark_custody` and must NOT reach `rebind`.
        """
        from helm import dispatches
        self.seat("worker", session="s-worker-0101")
        self.seat("taker", session="s-taker-0101")
        rebound, custodied = [], []

        def _rebind(rid, to, reason=None, force=False, **kw):
            rebound.append(rid)
            return {"new": {"id": "new" + str(rid)[:6]}}, None

        def _custody(rid, auth, outcome=None):
            # THE ARM READS THE PROOF, not a bare name: if the verb ever stops
            # minting one this mock raises instead of quietly recording None.
            #
            # THE COMPARE-AND-SWAP MOVED ONTO THE CAPABILITY, so this reads
            # `auth.source` where it used to read an `expected` argument. The
            # signature drops that parameter deliberately: a double WIDER than
            # production silently accepts an argument production no longer
            # sends, and would have kept recording None here while every
            # assertion below still passed.
            custodied.append((rid, auth.target, auth.source))
            return {"id": rid, "custodian": auth.target}, None

        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")), \
                mock.patch.object(seat_reassign, "holdings",
                                  return_value=self._manifest()), \
                mock.patch.object(dispatches, "rebind", _rebind), \
                mock.patch.object(dispatches, "mark_custody", _custody):
            rc, lines = seat_reassign.reassign("worker", "taker", apply=True)
        # POSITIVE CONTROL: the INBOUND row still goes through rebind. Without
        # it, a run where nothing was dispatched at all would satisfy the
        # must-miss below for the wrong reason.
        self.assertIn("in0000000001", rebound,
                      "the INBOUND row was not rebound, so this arm cannot "
                      "show the outbound one was routed differently")
        # THE MUST-MISS.
        self.assertNotIn("out000000002", rebound,
                         "a row the seat SENT was passed to rebind, which "
                         "moves the RECIPIENT — that hijacks its reviewer")
        # AND IT ACTUALLY MOVED, rather than being reported and abandoned.
        self.assertEqual(custodied, [("out000000002", "taker", "worker")],
                         "the sent row's delivery leg did not move, or moved "
                         "without the compare-and-swap naming its measured "
                         "holder on the proof: %r" % custodied)

    def test_a_refused_custody_is_not_counted_as_moved(self):
        from helm import dispatches
        self.seat("worker", session="s-worker-0102")
        self.seat("taker", session="s-taker-0102")
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")), \
                mock.patch.object(seat_reassign, "holdings",
                                  return_value=self._manifest()), \
                mock.patch.object(dispatches, "rebind",
                                  lambda rid, to, **kw: ({"new": {"id": "n"}}, None)), \
                mock.patch.object(dispatches, "mark_custody",
                                  lambda rid, auth, outcome=None: (
                                      None, "closed row")):
            rc, lines = seat_reassign.reassign("worker", "taker", apply=True)
        joined = "\n".join(lines)
        self.assertIn("REFUSED custody", joined,
                      "a refused custody transfer was not reported: %s"
                      % joined)
        self.assertNotIn("delivery leg now", joined,
                         "a refused transfer was announced as moved")


class TheCapabilityMeasuresLivenessItselfTest(ReassignBase):
    """The VERB refused a live source and the
    CAPABILITY did not, so anything reaching the mint directly could move a
    live incumbent's task. A safety property that lives in one of a
    capability's callers is not a property of the capability.
    """

    ROW = {"id": "a filed row", "owner": "worker", "status": "open"}

    def _mint(self, state, why):
        """Mint the DISPOSITION first, then the custody proof from it.

        These arms used to let the mint re-measure liveness itself. That
        fallback was deliberately REMOVED — one command must carry ONE
        measurement, so a seat that dies or revives mid-loop cannot split a
        single operator command across two verdicts — and "a caller without a
        proof is refused rather than quietly re-deriving one". The arms kept
        calling the old signature and went red, which is how this lane sat red
        at its own tip.

        The property under test is unchanged: does the CAPABILITY refuse a live
        incumbent. It is now reached the way production reaches it.
        """
        from helm import takeover
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(state, why)):
            disp, derr = takeover.mint_source_disposition("worker")
        self.assertIsNone(derr, derr)
        self.assertEqual(disp.state, state,
                         "the fixture disposition does not carry the state "
                         "this arm is about to attribute to the mint")
        return takeover.mint_seat_reassign(
            "a filed row", self.ROW, "worker", "taker",
            {"kind": "seat-reassign"}, disposition=disp)

    def test_the_MINT_itself_refuses_a_LIVE_incumbent(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertIsNone(auth) for the LIVE disposition; its unconditional positive is the SECOND _mint call below, asserting a DEAD incumbent DOES mint and carries state=SOURCE_DEAD. The rung cannot credit it because a second call is a different opaque call identity — that the two dispositions answer DIFFERENTLY through one mint IS the claim
        auth, err = self._mint(seat_reassign.SOURCE_LIVE, "pane is live")
        self.assertIsNone(auth,
                          "the capability minted custody for a LIVE incumbent")
        self.assertIn("measurably LIVE", err or "")
        # CONTROL: a mint that refused everything would pass the assertion
        # above without protecting anything.
        auth2, err2 = self._mint(seat_reassign.SOURCE_DEAD, "pane gone")
        self.assertIsNotNone(auth2, err2)
        self.assertEqual((auth2.disposition or {}).get("state"),
                         seat_reassign.SOURCE_DEAD)

    def test_an_UNMEASURABLE_incumbent_is_THE_PRIMARY_CASE_and_still_mints(self):
        """LIVE EVIDENCE IS A VETO, NEVER A REQUIREMENT.

        An earlier cure refused whenever liveness could not be MEASURED, which
        reads as caution and is the opposite: the seat with no roster row, or
        whose name stopped resolving, is exactly the seat nothing can measure
        AND exactly the population this verb exists to rescue. Requiring proof
        of death puts the burden on the only case that can never supply it.
        The refusal fires on a measured CONTRADICTION and on nothing else.
        """
        from helm import takeover
        with mock.patch.object(seat_reassign, "source_disposition",
                               side_effect=RuntimeError("no census")):
            disp, derr = takeover.mint_source_disposition("worker")
        self.assertIsNone(derr, derr)
        self.assertFalse(disp.measured,
                         "the fixture claims a MEASURED disposition, so this "
                         "arm would not be testing the unmeasurable case")
        auth, err = takeover.mint_seat_reassign(
            "a filed row", self.ROW, "worker", "taker",
            {"kind": "seat-reassign"}, disposition=disp)
        self.assertIsNotNone(
            auth, "an UNMEASURABLE incumbent was refused — that is the "
                  "orphaned-seat case the verb is for: %s" % err)
        # It still says what it is: unmeasured, not proven dead.
        self.assertFalse((auth.disposition or {}).get("measured"))
        self.assertEqual((auth.disposition or {}).get("state"), "unmeasured")

    def test_a_PROOF_WITHOUT_A_MEASUREMENT_cannot_authorize(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertIsNone(record2) after the disposition is stripped; its unconditional positive is assertIsNotNone(record) on the SAME auth immediately above, before the strip. One object, two states, and the difference between them is the property
        """The boundary half: minting is where liveness is measured, and this
        is where a proof that never carried one is refused."""
        from helm import takeover
        auth, err = self._mint(seat_reassign.SOURCE_DEAD, "pane gone")
        self.assertIsNotNone(auth, err)
        record, aerr = takeover.authorize_task_mutation(
            auth, "a filed row", self.ROW, {"owner": "taker"})
        self.assertIsNotNone(record, aerr)   # control: it normally authorizes
        auth.disposition = {}                # now strip the measurement
        record2, aerr2 = takeover.authorize_task_mutation(
            auth, "a filed row", self.ROW, {"owner": "taker"})
        self.assertIsNone(record2,
                          "a proof carrying no measured disposition "
                          "authorized a custody change")
        self.assertIn("no measured source disposition", aerr2 or "")


class ADisplayedSessionIsAPrefixTest(ReassignBase):
    """`seat_for_session` matches EXACTLY, but every surface that shows an
    operator a session id prints an 8+-char prefix.

    So the id the operator was shown resolved to nothing, fell through to the
    orphan branch, and the exact holder-key comparisons then found no holdings
    and printed a confident TOTAL 0 at rc 0 — a silent no-op wearing a success
    banner, on the verb whose whole job is to reach holdings a name cannot.
    """

    def test_the_PREFIX_an_operator_is_shown_resolves_to_its_seat(self):
        self.seat("worker", session="s-worker-abcdef01")
        # CONTROL: the FULL id resolves. Without it, a resolver broken for
        # everything would satisfy the prefix assertion by accident.
        seat_full, _s, how_full = seat_reassign.resolve_source("s-worker-abcdef01")
        self.assertEqual(seat_full, "worker", how_full)
        seat, _sess, how = seat_reassign.resolve_source("s-worker")
        self.assertEqual(seat, "worker",
                         "the 8-char prefix every surface prints did not "
                         "resolve: %s" % how)

    def test_an_AMBIGUOUS_prefix_REFUSES_rather_than_guessing(self):
        """THE MUST-MISS. The canonical resolver takes the FIRST hit, which is
        right for naming an agent and wrong here: handing one seat's holdings
        to another seat's successor is not recoverable by re-running."""
        self.seat("alpha", session="s-shared-0001")
        self.seat("beta", session="s-shared-0002")
        seat, _sess, why = seat_reassign.resolve_source("s-shared")
        self.assertIsNone(seat,
                          "an ambiguous prefix picked a seat; a custody move "
                          "may not guess (%s)" % why)
        self.assertIn("matches 2 seats", why or "")
        # CONTROL on the same predicate: one more character disambiguates and
        # the resolver answers, so the refusal above is ambiguity and not a
        # resolver that refuses every prefix.
        seat_ok, _s2, how = seat_reassign.resolve_source("s-shared-0002")
        self.assertEqual(seat_ok, "beta", how)


class ASelfAddressedRowIsONEHoldingTest(ReassignBase):
    """A row whose seat is BOTH custodian and recipient matched both legs of
    the census, and the move processes inbound FIRST: `rebind` mints a child
    and cancels the row, so the outbound leg then called `mark_custody` on a
    row that was already superseded — a guaranteed refusal, reported as a
    failure, on a holding that had in fact just moved successfully.
    """

    def test_it_appears_in_exactly_one_leg(self):
        from helm import dispatches
        self.seat("seat-a", session="s-seat-a-0001")
        rows = {
            "self00000001": {"id": "self00000001", "status": "open",
                             "lane": "l1", "recipient": "seat-a",
                             "sender": "seat-a", "kind": "review"},
            "in0000000002": {"id": "in0000000002", "status": "open",
                             "lane": "l2", "recipient": "seat-a",
                             "sender": "seat-b", "kind": "review"},
            "out000000003": {"id": "out000000003", "status": "open",
                             "lane": "l3", "recipient": "seat-b",
                             "sender": "seat-a", "kind": "review"},
        }
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)):
            man, _unread = seat_reassign.holdings("seat-a")
        ids_in = {e["id"] for e in man["dispatch_in"]}
        ids_out = {e["id"] for e in man["dispatch_out"]}
        # CONTROLS FIRST: both legs must actually be populated, or the
        # disjointness below would hold for a census that found nothing.
        self.assertIn("in0000000002", ids_in, "the inbound leg is empty")
        self.assertIn("out000000003", ids_out, "the outbound leg is empty")
        # THE FIX: the self-addressed row is counted ONCE, on the inbound leg,
        # because rebinding the recipient already carries the whole obligation.
        self.assertIn("self00000001", ids_in)
        self.assertEqual(ids_in & ids_out, set(),
                         "a self-addressed row was censused as TWO holdings; "
                         "the outbound leg would act on a row the inbound leg "
                         "had already superseded")


class AnUnreadSurfaceRefusesTheApplyTest(ReassignBase):
    """The dry run already returned non-zero for an unreadable ledger; APPLY
    did not. It moved every surface it could see and reported rc 1 at the END,
    so a run blind to one of the four ledgers left the seat SPLIT across two
    owners — the moved half done, the unread half neither moved nor listed.

    A partial reassignment is the one outcome this verb exists to prevent.
    """

    MANIFEST = {"leases": [{"resource": "r", "fence": 1}],
                "tasks": [{"id": "a filed row"}],
                "dispatch_in": [], "dispatch_out": []}

    def test_nothing_moves_when_a_surface_is_unread(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertEqual(called, []); its positive control is the sibling test_a_readable_manifest_still_moves, which drives the IDENTICAL mocks with a readable manifest and asserts a mover DID run. It cannot live in this method: the property is that one input moves nothing and another moves something, and a single call can only exhibit one
        from helm import dispatches
        self.seat("worker", session="s-worker-0301")
        self.seat("taker", session="s-taker-0301")
        called = []
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")), \
                mock.patch.object(seat_reassign, "holdings",
                                  return_value=(dict(self.MANIFEST),
                                                ["dispatch: unreadable"])), \
                mock.patch.object(seat_reassign, "_move_tasks",
                                  lambda *a, **k: called.append("tasks") or ([], [])), \
                mock.patch.object(seat_reassign, "_move_leases",
                                  lambda *a, **k: called.append("leases") or ([], [])), \
                mock.patch.object(dispatches, "rebind",
                                  lambda *a, **k: called.append("rebind") or (None, "x")):
            rc, lines = seat_reassign.reassign("worker", "taker", apply=True)
        self.assertEqual(rc, 1)
        # THE ASSERTION THAT MATTERS: not that rc is non-zero — the old code
        # returned non-zero too, AFTER moving — but that no mover RAN.
        self.assertEqual(called, [],
                         "an apply with an unreadable surface moved %s; the "
                         "seat would be split across two owners" % called)
        self.assertTrue(any("nothing was moved" in l for l in lines), lines)

    def test_a_readable_manifest_still_moves(self):
        """CONTROL. Without it, a verb that refused every apply would satisfy
        its sibling arm perfectly."""
        from helm import dispatches
        self.seat("worker", session="s-worker-0302")
        self.seat("taker", session="s-taker-0302")
        called = []
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD, "gone")), \
                mock.patch.object(seat_reassign, "holdings",
                                  return_value=(dict(self.MANIFEST), [])), \
                mock.patch.object(seat_reassign, "_move_tasks",
                                  lambda *a, **k: called.append("tasks") or ([], [])), \
                mock.patch.object(seat_reassign, "_move_leases",
                                  lambda *a, **k: called.append("leases") or ([], [])):
            seat_reassign.reassign("worker", "taker", apply=True)
        self.assertIn("tasks", called,
                      "the control did not move anything either, so the "
                      "refusal above proves nothing")


class AnAuditRowMustBeREADABLETest(ReassignBase):
    """`eventledger.checked_rows` admits a row only when
    `isinstance(row, dict) and row.get("id")`. `record` set v/event/ts and no
    id, so every reassignment audit row it wrote was INVISIBLE to the checked
    reader — the append succeeded, `record` returned True, and the trail was
    empty.

    A write that reports success and cannot be read back is the worst shape a
    ledger has: it satisfies its writer and lies to its auditor.
    """

    def test_a_recorded_event_is_visible_to_the_checked_reader(self):
        from helm import eventledger
        ok, err = seat_reassign.record(
            {"kind": "seat-reassign", "from": "seat-a", "to": "seat-b"})
        self.assertTrue(ok, err)
        rows, _bad = eventledger.checked_events(seat_reassign.ledger_path())
        rows = rows or []
        # THE ASSERTION IS THROUGH THE CHECKED READER, not through `record`'s
        # return value — the return value was True the whole time the row was
        # being dropped, which is exactly why it could not be the evidence.
        self.assertEqual(len(rows), 1,
                         "the audit row was dropped by the checked reader")
        self.assertTrue(rows[0].get("id"),
                        "the row carries no id, so a checked read discards it")
        self.assertEqual(rows[0].get("event"), "seat-reassign")

    def test_two_events_get_DISTINCT_ids(self):
        """An id that repeated would collapse two moves into one audit row,
        which is the same invisibility arriving by a different door."""
        from helm import eventledger
        seat_reassign.record({"kind": "seat-reassign", "from": "seat-a"})
        seat_reassign.record({"kind": "seat-reassign", "from": "seat-c"})
        rows, _bad = eventledger.checked_events(seat_reassign.ledger_path())
        ids = [r.get("id") for r in (rows or [])]
        self.assertEqual(len(ids), 2, ids)
        self.assertEqual(len(set(ids)), 2, "two moves share one audit id")


class ALargeMoveStillGETS_AnAuditRowTest(ReassignBase):
    """`eventledger` REFUSES any event over MAX_EVENT_BYTES, and this event
    embeds full per-row lists across four surfaces. So the LARGER the
    reassignment, the likelier its audit row is rejected outright — and the
    audit matters most exactly when the move is biggest.

    127 rows is not hypothetical: it is the measured rename-orphan population.
    """

    def _big(self):
        return {"source": "seat-a", "target": "seat-b",
                "moved": {"dispatch": [{"id": "x" * 32, "lane": "l" * 40,
                                        "new": "y" * 32} for _ in range(127)]},
                "refused": {"tasks": [{"id": "t/%d" % i, "why": "w" * 400}
                                      for i in range(127)]}}

    def test_the_row_survives_even_when_its_detail_cannot(self):
        import json
        from helm import eventledger
        big = self._big()
        # FIXTURE PROOF: this payload really does exceed the ledger's cap, so
        # the arm is about the bound and not about a small event passing.
        raw = len(json.dumps(dict(big, v=1, event="seat-reassign"),
                             ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8"))
        self.assertGreater(raw, eventledger.MAX_EVENT_BYTES,
                           "fixture: the payload must exceed the cap")
        ok, err = seat_reassign.record(dict(big))
        self.assertTrue(ok, err)
        rows, _bad = eventledger.checked_events(seat_reassign.ledger_path())
        self.assertEqual(len(rows or []), 1,
                         "the audit row for a large move was dropped")
        row = rows[0]
        # THE OP IS NO LONGER DEFAULTED ONTO EVERY ROW, and that is the point
        # of this line rather than an omission. Defaulting it to the row id
        # made a standalone row look like an operation whose completion never
        # arrived, so a pre-fence legacy intent fenced its incarnation
        # forever. Correlation now lives on the intent/completion PAIR that
        # `apply` writes explicitly; a lone audit row like this one is not
        # part of an operation and correctly carries none.
        self.assertIsNone(row.get("op"),
                          "a standalone audit row minted an op, which the "
                          "fence would read as an operation that never "
                          "completed")
        self.assertTrue(row.get("id"), "the row still needs its ledger id")
        self.assertTrue(row.get("detail_dropped"),
                        "detail was omitted without saying so")
        # COUNTS SURVIVE — a row that lost its counts too would make a 127-row
        # move indistinguishable from a no-op, which is the invisibility this
        # bound exists to prevent.
        self.assertEqual((row.get("refused") or {}).get("tasks", {}).get("count"),
                         127)

    def test_a_SMALL_move_keeps_its_full_detail(self):
        """CONTROL. A bound that degraded every event would satisfy the arm
        above while destroying the ordinary case."""
        from helm import eventledger
        ok, err = seat_reassign.record(
            {"source": "seat-a", "target": "seat-b",
             "moved": {"tasks": [{"id": "a filed row"}]}})
        self.assertTrue(ok, err)
        rows, _bad = eventledger.checked_events(seat_reassign.ledger_path())
        row = rows[0]
        self.assertIsNone(row.get("detail_dropped"),
                          "a small event was degraded for no reason")
        self.assertEqual(row["moved"]["tasks"], [{"id": "a filed row"}])


class AnAmbiguousExactSessionRefusesTest(ReassignBase):
    """The EXACT session path took the first hit.

    `seat_for_session` warns on a multi-row match and returns hits[0] by
    binding age — right for a caller that merely NAMES an agent, wrong for one
    that MOVES CUSTODY. The PREFIX branch already refused;
    this branch did not, so a shared session id could hand one seat's holdings
    to another seat's successor. A warning is not a refusal, and nobody reads
    a warning on a command that then reports success.

    THE ROSTER IS BUILT DIRECTLY HERE, and that is deliberate rather than
    lazy: the normal stamp path REFUSES a cross-seat session stamp ("presence
    and delivery are per-PROCESS facts"), so the fixture cannot manufacture
    this state the way a process would. The state is real anyway — it arises
    from a STALE HISTORICAL binding left beside a current one, which is
    exactly what `seat disown` exists to repair — so the fixture models the
    stored roster rather than the write that is guarded against.
    """

    SID = "abcdef0123456789abcdef0123456789"

    def _roster(self, *seats):
        """THE FIXTURE'S NAME IS ITS CONTRACT: this IS the roster, for every
        reader that asks. Two now do — `resolve_source` takes the tri-state
        `roster_checked`, and `seats_for_session` reads the fail-open
        `roster()` for itself — so a plant on one alone leaves the other
        reading the real file and the arm silently exercises a different
        branch than it names.
        """
        rows = {s: {"session": self.SID} for s in seats}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            seats_roster, "roster_checked", return_value=(rows, False)))
        stack.enter_context(mock.patch.object(
            seats_roster, "roster", return_value=rows))
        return stack

    def test_two_seats_remembering_ONE_session_REFUSE(self):
        with self._roster("twin-a", "twin-b"):
            seat, session, why = seat_reassign.resolve_source(self.SID)
        self.assertIsNone(seat, "custody resolved to one of two claimants")
        self.assertIsNone(session)
        self.assertIn("twin-a", why)
        self.assertIn("twin-b", why)
        self.assertIn("may not pick", why)

    def test_the_SAME_session_with_ONE_claimant_still_resolves(self):
        """THE CONTROL, one roster row apart. Without it its sibling passes
        against a resolver that refuses every session id it is handed, which
        would break the verb's primary population instead of protecting it."""
        with self._roster("only-one"):
            seat, session, how = seat_reassign.resolve_source(self.SID)
        self.assertEqual(seat, "only-one", how)
        self.assertEqual(session, self.SID)

    def test_the_refusal_names_the_repair(self):
        """A refusal that does not say how to clear it strands the operator on
        the one command whose job is to unstick things."""
        with self._roster("twin-a", "twin-b"):
            _s, _sess, why = seat_reassign.resolve_source(self.SID)
        self.assertIn("disown", why)


class TheSourceLockIsEphemeralTest(ReassignBase):
    """What replaced the transactional fence, and why it is smaller.

    The fence was DURABLE — an intent row with no completion — so a crash left
    a half-held operation and everything downstream had to answer questions
    about it: is it resumable, does it still fence, who may adopt it. Each
    answer was a defect. THE KERNEL RELEASES AN OS LOCK WHEN THE PROCESS DIES,
    so none of those questions exist: a crashed run leaves the stores as far as
    it got and the next run re-censuses from what is actually true.
    """

    def test_a_concurrent_DIFFERENT_target_is_refused(self):
        """Split work is the one genuine conflict — two runs sending one
        identity's holdings to two places. Late work converges on its own;
        split work does not."""
        with seat_reassign.source_lock("src", "taker-a") as (held, _why):
            self.assertTrue(held)
            with seat_reassign.source_lock("src", "taker-b") as (h2, why2):
                self.assertFalse(h2)
                self.assertIn("taker-a", why2)
                self.assertIn("cannot both be right", why2)

    def test_the_lock_is_RELEASED_when_the_holder_exits(self):
        """THE WHOLE REASON THIS IS NOT A LEDGER ROW. Without release-on-exit
        we are back to a durable marker and every recovery question it drags
        along."""
        with seat_reassign.source_lock("src", "taker-a") as (held, _w):
            self.assertTrue(held)
        with seat_reassign.source_lock("src", "taker-b") as (held2, why2):
            self.assertTrue(held2, why2)

    def test_two_DIFFERENT_sources_do_not_contend(self):
        """The lock is per-source. One reassignment must not serialise every
        other one on the fleet."""
        with seat_reassign.source_lock("src-one", "taker") as (a, _wa):
            with seat_reassign.source_lock("src-two", "taker") as (b, _wb):
                self.assertTrue(a)
                self.assertTrue(b)

    def test_the_slug_survives_a_hostile_source_name(self):
        """A seat name or session id may contain anything, and it becomes a
        FILENAME here. A slash would silently write outside the intended dir."""
        # UNCONDITIONAL FLOOR, and it must be POSITIVE. Every assertion below
        # is inside the loop, so an emptied hostile-input tuple would leave
        # this arm green. My first floor here was another assertNotIn — an
        # ABSENCE, which proves nothing about whether the slugger ran at all:
        # a function returning "" satisfies every "/" check in this arm.
        floor = seat_reassign._lock_slug("a/../../etc/passwd")
        self.assertTrue(floor, "the slugger returned nothing; every absence "
                               "assertion below would hold vacuously")
        self.assertLessEqual(len(floor), 64)
        for hostile in ("a/../../etc/passwd", "with spaces", "", "x" * 400):
            with self.subTest(source=hostile[:20]):
                slug = seat_reassign._lock_slug(hostile)
                self.assertNotIn("/", slug)
                self.assertTrue(slug)
                self.assertLessEqual(len(slug), 64)


class RerunningIsAlwaysSafeTest(ReassignBase):
    """The property the whole redesign buys: there is no operation to finish,
    so a second run is not a resume — it is the same query against whatever is
    true now. This is what a human team does with a departed colleague's work,
    and it is why none of the recovery machinery is needed."""

    def test_the_verb_carries_no_operation_state_at_all(self):
        """A STRUCTURAL arm. If any of these come back the transactional
        object is regrowing, and it regrows ONE CONCEPT AT A TIME rather than
        all at once — which is why the needles below are listed individually
        instead of as a single pattern."""
        import inspect
        src = inspect.getsource(seat_reassign)
        # EACH NEEDLE IS A CONSTRUCT, NEVER A BARE WORD. An earlier version
        # listed "resumed", which matched an unrelated `seat_resume_all` import
        # and a prose line saying reassignment never means work resumed — a
        # check scoped to the word in mind rather than to the thing it was
        # about. A shape-matching guard fails on innocent text and teaches
        # people to loosen it, which is how a guard dies.
        for gone in ("CONSUMERS_WIRED", "def open_op", '"phase": "intent"',
                     'phase = "complete"', '"resumed":'):
            self.assertNotIn(gone, src,
                             "%s is back — the transactional object is "
                             "regrowing" % gone)
        # AND THE CONTROL: the replacement IS present, so this arm cannot pass
        # by the module having been emptied.
        self.assertIn("def source_lock", src)
        self.assertIn("def holdings", src)


MOVERS = ("_move_dispatch", "_move_custody", "_move_tasks", "_move_leases")
LOCKED_HELPER = "_apply_locked"


def movers_called_in(source, movers=MOVERS, helper=LOCKED_HELPER):
    """The movers the locked helper actually CALLS.

    THE FLOOR AND THE ESCAPE RULE ASK DIFFERENT QUESTIONS and must not share a
    predicate. `mover_escapes` is about REACHABILITY, so it counts every
    reference — an alias and a dict entry are exactly how a mover gets reached
    without a visible call. The floor is about the arm not measuring an EMPTY
    WORLD, and a helper that merely NAMES a mover (`unused = _move_tasks`)
    without calling it satisfies a reference test while the mover is invoked
    nowhere at all — which is the vacuity the floor exists to refuse. The text
    scan this file replaced required a call site and so could not be fooled
    that way; asking only for a reference here was a WEAKENING introduced with
    the AST reader.
    """
    import ast
    tree = ast.parse(source)
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                or node.name != helper:
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if not isinstance(inner, ast.Call):
                    continue
                func = inner.func
                if isinstance(func, ast.Name) and func.id in movers:
                    called.add(func.id)
                elif isinstance(func, ast.Attribute) and func.attr in movers:
                    called.add(func.attr)
    return called


def mover_escapes(source, movers=MOVERS, helper=LOCKED_HELPER):
    """Every way a mover can be reached from outside the locked helper, read
    off the AST.

    THE PREDECESSOR WAS A TEXT SCAN and it answered the wrong question. It
    collected lines containing `<name>(`, so a direct call was seen and
    `m = _move_tasks; m(...)`, `getattr(mod, "_move_tasks")(...)` and a dict of
    movers were all INVISIBLE — measured against the helper verbatim, three of
    five synthetic bodies walked straight through. The arm's stated property is
    that no mover is reachable outside the lock, and reachability is not a
    property a per-line string match can decide: each spelling it learns leaves
    the next spelling outside the set.

    So this asks two whole-object questions instead of enumerating spellings:

      * every REFERENCE to a mover name must be inside the helper, whether it
        is spelled as a bare name or as an attribute. A call, an alias, a dict
        entry, a default argument and a decorator are all Name loads; a
        qualified `mod._move_tasks(...)` is an Attribute whose attr is the
        mover, and it carries NO Name node of that name anywhere. A reader
        that looks only at Name loads is blind to that spelling while looking
        strictly stronger than the text scan, which did match it — so the
        attribute rule is what keeps this reader from being a REGRESSION on
        `sys.modules[__name__]._move_tasks(x)` and `mod._move_tasks(x)`.
      * a call this reader CANNOT RESOLVE outside the helper is REPORTED
        rather than assumed innocent. A subscript, a returned callable and a
        getattr all defeat the proof; refusing honestly is the only answer
        that is not a guess, and the module has none today, so the cost of
        that strictness is currently zero.

    Returns a list of (kind, detail, lineno) — empty means proven.
    """
    import ast
    tree = ast.parse(source)
    # Definition-time expressions belong to the enclosing execution context,
    # not to the function being defined. Walking only body statements leaves
    # a top-level helper's decorators and defaults outside its protection.
    # Nested definitions inside a body retain that outer body's ownership.
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    owner.setdefault(inner, node.name)
    # The helper's own `def` line is not a call and must never count; excluding
    # it by name here is structural rather than a needle that has to be spelled
    # right per symbol, which is the error this file already records twice.
    defined = {node.name: node for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name) and node.id in movers:
            # The helper's own `def` is not a reference to itself.
            if defined.get(node.id) is not node:
                name = node.id
        elif isinstance(node, ast.Attribute) and node.attr in movers:
            # A QUALIFIED mover, which has no Name node of that name at all.
            name = node.attr
        if name is not None:
            if owner.get(node) != helper:
                found.append(("reference-outside-lock",
                              "%s referenced in %s" % (name,
                                                       owner.get(node) or "<module>"),
                              node.lineno))
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "getattr":
                if owner.get(node) != helper:
                    named = [a.value for a in node.args
                             if isinstance(a, ast.Constant)
                             and isinstance(a.value, str) and a.value in movers]
                    found.append(("unresolvable-reference-outside-lock",
                                  "getattr(%s) in %s"
                                  % (", ".join(named) or "dynamic name",
                                     owner.get(node) or "<module>"),
                                  node.lineno))
            elif not isinstance(func, (ast.Name, ast.Attribute)):
                if owner.get(node) != helper:
                    found.append(("unresolvable-call-outside-lock",
                                  "%s call in %s" % (type(func).__name__,
                                                     owner.get(node) or "<module>"),
                                  node.lineno))
    return sorted(found, key=lambda item: (item[2], item[0]))


class TheLockIsACTUALLYENTEREDTest(ReassignBase):
    """source_lock was DEAD: defined, tested, and never called.

    The arms around it proved the lock WORKS and none proved it is USED, so
    every one of them passed against a production path that never entered it.
    A mechanism nobody invokes reads as a guarantee and enforces nothing.

    BUILT-AND-NOT-WIRED IS A RECURRING CLASS here, which is why these arms
    assert the production path ENTERS the lock rather than that the lock
    functions when entered.
    """

    def setUp(self):
        super().setUp()
        # WITHOUT THESE ROWS THE VERB REFUSES AT resolve_target — "no seat
        # 'taker' is on the roster" — which is a THIRD gate earlier than the
        # lock. Each time the arm moved past one gate it stopped at the next,
        # which is what a fixture that was never built for this path does.
        self.seat("worker")
        self.seat("taker")

    def test_the_apply_path_ENTERS_the_lock(self):
        """Drives the real verb and asserts the lock was taken — not that the
        lock is correct, which is a different arm entirely."""
        entered = []
        real = seat_reassign.source_lock

        @contextlib.contextmanager
        def spy(key, target):
            entered.append((key, target))
            with real(key, target) as pair:
                yield pair
        seat_reassign.source_lock = spy
        try:
            # THE SOURCE MUST BE MEASURABLY DEAD OR THE LIVENESS VETO REFUSES
            # FIRST — an earlier version omitted this and the arm reported "the
            # lock is dead code again" when the truth was that the verb never
            # reached the lock at all. An arm that stops at an EARLIER gate
            # than the one it is about tells you nothing about that gate.
            with mock.patch.object(seat_reassign, "source_disposition",
                                   return_value=(seat_reassign.SOURCE_DEAD,
                                                 "pane gone")):
                rc, lines = seat_reassign.reassign(
                    "worker", "taker", reason="dead seat", apply=True)
            # PINNED POSITIVELY, on rc itself. "not 1" is an absence whose
            # only controls constrain `entered` — a different observable —
            # so it could not distinguish a clean apply from any other
            # non-1 outcome. A clean apply on this fixture returns 0.
            self.assertEqual(
                rc, 0, "the verb did not complete cleanly, so this arm may "
                       "have measured an earlier gate: %s" % "\n".join(lines))
        finally:
            seat_reassign.source_lock = real
        self.assertTrue(entered,
                        "reassign(apply=True) never entered source_lock — the "
                        "lock is dead code again")
        self.assertEqual(entered[0][1], "taker",
                         "the lock did not carry the TARGET, so a concurrent "
                         "different-target run could not be refused")

    def test_a_DRY_RUN_does_not_take_the_lock(self):
        """THE CONTROL, and a real property: a read-only preview must not be
        refused because a move is running, and it must not block one either."""
        entered = []
        real = seat_reassign.source_lock

        @contextlib.contextmanager
        def spy(key, target):
            entered.append(key)
            with real(key, target) as pair:
                yield pair
        seat_reassign.source_lock = spy
        try:
            with mock.patch.object(seat_reassign, "source_disposition",
                                   return_value=(seat_reassign.SOURCE_DEAD,
                                                 "pane gone")):
                seat_reassign.reassign("worker", "taker", reason="dead seat",
                                       apply=False)
        finally:
            seat_reassign.source_lock = real
        self.assertEqual(entered, [])

    def test_NO_MOVER_IS_REACHABLE_OUTSIDE_THE_LOCKED_HELPER(self):  # noqa: VACUOUS_ASSERTION — the unconditional positive control runs mover_escapes over THIS module's own source with one escape appended and asserts it is reported; by the rung's own provenance rule that is necessarily a second call and so a second observable, because a detector's liveness can only be shown on an input that HAS what it detects
        """STRUCTURAL, because the spy above proves ONE path enters the lock
        and says nothing about a second path that bypasses it.

        Read off the AST by `mover_escapes`, whose docstring records why the
        text scan this replaced could not decide the question.
        """
        import inspect
        src = inspect.getsource(seat_reassign)
        # UNCONDITIONAL FLOOR: the helper body must actually call every mover.
        # References and definition-time calls cannot establish this floor.
        called = movers_called_in(src)
        self.assertEqual(called, set(MOVERS),
                         "the locked helper does not CALL every mover, so the "
                         "escape check below would be measuring an empty "
                         "world; missing: %s" % sorted(set(MOVERS) - called))
        # UNCONDITIONAL POSITIVE ON THE SAME OBSERVABLE, against THIS module's
        # real source rather than a synthetic one: an escape appended to the
        # actual file must be reported. Without it the assertion below is an
        # absence claim with no evidence the reader can see anything here at
        # all — a detector that returned [] unconditionally would pass.
        self.assertTrue(
            mover_escapes(src + "\n\ndef _escape_control(row):\n"
                                "    return _move_tasks(row)\n"),
            "the reader finds no escape even when one is appended to this "
            "module's own source, so the empty result below means nothing")
        self.assertEqual(
            mover_escapes(src), [],
            "a mover is reachable from outside %s, which is a path around the "
            "lock" % LOCKED_HELPER)

    def test_definition_time_movers_are_not_locked_helper_body_calls(self):
        import inspect
        from unittest import mock

        src = "TRACE = []\n\n" + "\n".join(
            "def %s(x):\n    TRACE.append(%r)\n    return x\n" % (name, name)
            for name in MOVERS)
        src += "\ndef _apply_locked(x):\n" + "".join(
            "    %s(x)\n" % name for name in MOVERS)

        # The exact real arm must accept the clean module before its refusal
        # of the same module with a definition-time call has any meaning.
        with mock.patch.object(inspect, "getsource", return_value=src):
            self.test_NO_MOVER_IS_REACHABLE_OUTSIDE_THE_LOCKED_HELPER()

        for label, header in (
                ("decorator", "@_move_tasks(lambda fn: fn)\n"
                 "def _apply_locked(x):"),
                ("default", "def _apply_locked(\n"
                 "    x=_move_tasks('synthetic')\n):")):
            with self.subTest(shape=label):
                outside = src.replace("def _apply_locked(x):", header)
                scope = {}
                exec(compile(outside, "<synthetic-movers>", "exec"), scope)
                self.assertEqual(scope["TRACE"], ["_move_tasks"],
                                 "the fixture did not execute a mover while "
                                 "defining the helper")
                scope["TRACE"].clear()
                scope["_apply_locked"]("synthetic")
                self.assertEqual(scope["TRACE"], list(MOVERS),
                                 "the ordinary helper calls must remain live")
                with mock.patch.object(inspect, "getsource",
                                       return_value=outside):
                    with self.assertRaisesRegex(AssertionError,
                                                "a mover is reachable"):
                        self.test_NO_MOVER_IS_REACHABLE_OUTSIDE_THE_LOCKED_HELPER()

                # The same definition-time call cannot replace a body call
                # in the independent nonvacuity floor either.
                no_body_call = outside.replace("    _move_tasks(x)\n", "")
                self.assertEqual(movers_called_in(no_body_call),
                                 set(MOVERS) - {"_move_tasks"})

        # A nested definition is executed by the enclosing helper body.
        # Do not fix the outside case by banning all decorator spellings.
        inside = src.replace(
            "def _apply_locked(x):\n",
            "def _apply_locked(x):\n"
            "    @_move_tasks(lambda fn: fn)\n"
            "    def nested():\n        return x\n")
        with mock.patch.object(inspect, "getsource", return_value=inside):
            self.test_NO_MOVER_IS_REACHABLE_OUTSIDE_THE_LOCKED_HELPER()
        scope = {}
        exec(compile(inside, "<synthetic-movers>", "exec"), scope)
        self.assertEqual(scope["TRACE"], [])
        scope["_apply_locked"]("synthetic")
        self.assertEqual(scope["TRACE"], ["_move_tasks"] + list(MOVERS))

    def test_the_escape_reader_sees_every_indirection_a_text_scan_missed(self):
        """The detector's own controls, because an absence claim is only worth
        what its instrument can see — and this instrument's whole reason to
        exist is the four shapes the predecessor could not.

        Each case is a WHOLE synthetic module rather than a snippet, so it goes
        through the same parse, the same owner map and the same rules as the
        real arm above; a probe more forgiving than the arm it rehearses is a
        different test.
        """
        def module(body):
            return ("def _move_tasks(x):\n    return x\n\n\n"
                    "def _apply_locked(x):\n    return _move_tasks(x)\n\n\n"
                    + body)

        # MUST-MISS FIRST: the clean module, and the `def` line that this file
        # has twice been caught counting as a call.
        self.assertEqual(mover_escapes(module("")), [],
                         "the clean module reports an escape, so every "
                         "positive below would be unreadable")
        # AND THE PAIRED POSITIVE, UNCONDITIONALLY: the loop below asserts
        # inside subTests over a table, so an emptied table would leave this
        # arm green having checked nothing. This one case runs whatever the
        # table holds.
        self.assertTrue(
            mover_escapes(module("def elsewhere(x):\n"
                                 "    return _move_tasks(x)\n")),
            "the reader cannot even see a direct call outside the lock")

        for label, body, kind in (
                ("a direct call outside the lock",
                 "def elsewhere(x):\n    return _move_tasks(x)\n",
                 "reference-outside-lock"),
                ("an alias through a local name",
                 "def elsewhere(x):\n    m = _move_tasks\n    return m(x)\n",
                 "reference-outside-lock"),
                ("a dict of movers built at module level",
                 "TABLE = {'t': _move_tasks}\n\n\n"
                 "def elsewhere(x):\n    return TABLE['t'](x)\n",
                 "reference-outside-lock"),
                ("getattr by a constant string",
                 "import sys\n\n\ndef elsewhere(x):\n"
                 "    return getattr(sys.modules[__name__], '_move_tasks')(x)\n",
                 "unresolvable-reference-outside-lock"),
                ("getattr by a computed name",
                 "import sys\n\n\ndef elsewhere(x, n):\n"
                 "    return getattr(sys.modules[__name__], n)(x)\n",
                 "unresolvable-reference-outside-lock"),
                ("a subscripted callable",
                 "def elsewhere(x, table):\n    return table['t'](x)\n",
                 "unresolvable-call-outside-lock"),
                # THE TWO THE FIRST CUT OF THIS READER MISSED, and the only
                # shapes on this list the TEXT scan it replaces would have
                # caught. A qualified mover carries no Name node of its own
                # name, so a Name-only reader is blind to it while looking
                # strictly stronger than what it replaced.
                ("a qualified call through the module object",
                 "import sys\n\n\ndef elsewhere(x):\n"
                 "    return sys.modules[__name__]._move_tasks(x)\n",
                 "reference-outside-lock"),
                ("a qualified call through an imported name",
                 "import mod\n\n\ndef elsewhere(x):\n"
                 "    return mod._move_tasks(x)\n",
                 "reference-outside-lock")):
            with self.subTest(shape=label):
                found = mover_escapes(module(body))
                self.assertTrue(found, "%s was INVISIBLE to the reader" % label)
                self.assertIn(kind, [item[0] for item in found],
                              "%s was seen but classified as %s"
                              % (label, [item[0] for item in found]))

        # AND THE SAME INDIRECTIONS INSIDE THE LOCK ARE FINE, which is what
        # makes the rule about REACHABILITY rather than about spelling: a
        # reader that flagged these too would pass every case above while
        # being useless.
        inside = ("def _move_tasks(x):\n    return x\n\n\n"
                  "def _apply_locked(x):\n    m = _move_tasks\n"
                  "    return m(x)\n")
        self.assertEqual(mover_escapes(inside), [],
                         "an alias INSIDE the locked helper was reported as an "
                         "escape, so the rule is about spelling, not reach")

        # AND THE FLOOR IS A DIFFERENT QUESTION FROM THE ESCAPE RULE. A helper
        # that NAMES a mover without calling it reaches it nowhere, so the
        # escape check would be measuring a world in which nothing happens —
        # the exact vacuity the floor exists to refuse, and the one the text
        # scan this reader replaced could not be fooled by.
        names_only = ("def _move_tasks(x):\n    return x\n\n\n"
                      "def _apply_locked(x):\n    unused = _move_tasks\n"
                      "    return x\n")
        calls_it = ("def _move_tasks(x):\n    return x\n\n\n"
                    "def _apply_locked(x):\n    return _move_tasks(x)\n")
        movers = ("_move_tasks",)
        self.assertEqual(movers_called_in(calls_it, movers), {"_move_tasks"},
                         "the floor cannot see a plain call, so its refusal "
                         "below would be about nothing")
        self.assertEqual(movers_called_in(names_only, movers), set(),
                         "a helper that only NAMES a mover satisfies the "
                         "floor, so an arm could pass while the mover is "
                         "invoked nowhere at all")
        # A QUALIFIED call counts too, for the same reason it counts as an
        # escape outside the lock.
        qualified = ("def _move_tasks(x):\n    return x\n\n\nimport mod\n\n\n"
                     "def _apply_locked(x):\n    return mod._move_tasks(x)\n")
        self.assertEqual(movers_called_in(qualified, movers), {"_move_tasks"})


class TheCensusDoesNotMutateWhatItMeasuresTest(ReassignBase):
    """MEASURED: claims_list persists an expiry sweep when any row has
    aged out, so censusing the claims surface to decide what to move WROTE to
    the store the decision rested on. The plan then rests on a state the plan's
    own census created."""

    def test_claims_list_gc_False_does_not_write(self):
        """Observe writes and persisted bytes, not filesystem timestamp ticks.

        Force expiry without sleeping, but retain a live row so the returned
        tables cannot agree merely by both being empty. An unchanged mtime
        cannot distinguish no write from two writes within one clock tick;
        the real writer spy also detects a redundant same-content rewrite.
        """
        from helm import seats_claims, seats_common
        path = seats_claims.claims_path()

        def ledger():
            with open(path, "rb") as stream:
                return stream.read()

        for resource, ttl in (("res-expired", 1), ("res-live", 7200)):
            ok, message, _lease = seats_claims.claim(resource, "holder", ttl=ttl)
            self.assertTrue(ok, message)
        before = ledger()
        raw = json.loads(before)
        later = seats_common._now_mono() + 3600
        self.assertLess(raw["res-expired"]["exp_mono"], later)
        self.assertGreater(raw["res-live"]["exp_mono"], later)
        expected = {key: value for key, value in raw.items()
                    if key != "res-expired"}

        # _sweep resolves common's clock; the public remaining-time column
        # resolves claims' imported clock. Freeze both for exact table equality.
        with mock.patch.object(seats_common, "_now_mono", return_value=later), \
                mock.patch.object(seats_claims, "_now_mono", return_value=later), \
                mock.patch.object(seats_claims.pk, "write_json",
                                  wraps=seats_claims.pk.write_json) as write:
            rows_ro = seats_claims.claims_list(gc=False)
            write.assert_not_called()
            self.assertEqual(ledger(), before,
                             "a read-only claims poll rewrote the store")
            self.assertEqual([row["resource"] for row in rows_ro], ["res-live"])

            # Unconditional positive on the SAME ledger and writer: gc=False
            # left the expired row on disk, so gc=True must persist its removal.
            rows_gc = seats_claims.claims_list(gc=True)
            write.assert_called_once_with(path, expected)
            after = ledger()
            self.assertNotEqual(after, before)
            self.assertEqual(json.loads(after), expected)
            self.assertEqual(rows_gc, rows_ro)

    def test_the_DEFAULT_still_collects(self):
        """THE CONTROL. gc=False is only safe to add if the default is
        untouched — every existing caller relies on the sweep persisting, and
        an arm that only proves the new mode says nothing about them."""
        from helm import seats_claims
        import inspect
        # The DEFAULT is the property, not the signature's spelling: task/2541
        # added a keyword after it and the text pin read that as a change.
        gc = inspect.signature(seats_claims.claims_list).parameters["gc"]
        self.assertIs(gc.default, True,
                      "the default changed — existing callers silently stopped "
                      "collecting expired claims")

    def test_the_census_uses_the_read_only_form(self):
        """The seam that matters: it is not enough that a read-only mode
        EXISTS, the census has to be the thing that uses it. That distinction
        is exactly what made source_lock dead code one commit ago."""
        import inspect
        src = inspect.getsource(seat_reassign.holdings)
        self.assertIn("claims_list(gc=False)", src)
        self.assertNotIn("claims_list()", src,
                         "a second claims read in the census still collects")


class TheHistORICALIncarnationTravelsTest(ReassignBase):
    """The exact historical SID the operator names was replaced by the
    seat's CURRENT session, so the audit row claimed a move of the live
    incarnation when a historical one had been named — and the fence keyed on
    the wrong identity."""

    OLD = "1" * 32
    NEW = "2" * 32

    def _row_with_history(self):
        from helm import seats_roster
        return mock.patch.object(
            seats_roster, "roster_checked",
            return_value=({"mover": {"session": self.NEW,
                                     "sessions": [self.NEW, self.OLD]}},
                          False))

    def test_an_exact_historical_sid_is_returned_unchanged(self):
        from helm import seats_roster
        with self._row_with_history(), \
             mock.patch.object(seats_roster, "seats_for_session",
                               return_value=["mover"]):
            seat, session, how = seat_reassign.resolve_source(self.OLD)
        self.assertEqual(seat, "mover", how)
        self.assertEqual(session, self.OLD,
                         "the CURRENT session was substituted for the "
                         "historical incarnation the operator named")

    def test_the_CURRENT_sid_still_resolves_to_itself(self):
        """THE CONTROL: naming the live incarnation must still work, or the
        fix above would have broken the ordinary case to serve the rare one."""
        from helm import seats_roster
        with self._row_with_history(), \
             mock.patch.object(seats_roster, "seats_for_session",
                               return_value=["mover"]):
            _seat, session, _how = seat_reassign.resolve_source(self.NEW)
        self.assertEqual(session, self.NEW)

    def test_a_PREFIX_resolves_to_the_full_session_it_matched(self):
        from helm import seats_roster
        with self._row_with_history(), \
             mock.patch.object(seats_roster, "seats_for_session",
                               return_value=[]), \
             mock.patch.object(seats_roster, "seats_for_session_prefix",
                               return_value=["mover"]):
            _seat, session, how = seat_reassign.resolve_source(self.OLD[:10])
        self.assertEqual(session, self.OLD,
                         "a prefix resolved to the row's current binding "
                         "instead of the incarnation it actually named: %s"
                         % how)

    def test_an_AMBIGUOUS_prefix_refuses_rather_than_guessing(self):
        """Two stored sessions sharing a prefix is exactly the case a prefix
        cannot resolve, and picking either would move one incarnation's
        holdings under another's name."""
        from helm import seats_roster
        with mock.patch.object(
                seats_roster, "roster_checked",
                return_value=({"mover": {"session": "abc1" + "0" * 28,
                                         "sessions": ["abc1" + "0" * 28,
                                                      "abc1" + "9" * 28]}},
                              False)), \
             mock.patch.object(seats_roster, "seats_for_session",
                               return_value=[]), \
             mock.patch.object(seats_roster, "seats_for_session_prefix",
                               return_value=["mover"]):
            seat, session, why = seat_reassign.resolve_source("abc1")
        self.assertIsNone(seat, why)
        self.assertIsNone(session)
        self.assertIn("may not guess", why)


class AWriteAdvisoryReachesTheOperatorTest(ReassignBase):
    """Composing rebind is not byte-exact for legacy rows — a
    pre-2026-07-28 row carries sender=None, so the acting seat authors the
    child. That is DELIBERATE and documented; what was not deliberate is that
    rebind attaches an advisory saying so and this composer dropped it.

    dispatches.py states the stakes in its own words: the advisory "is a fact
    about THIS WRITE, not durable row state, and the caller is the only one who
    can act on it". We were that caller. A legacy row changed author under a
    line that read `moved`, and nothing told the operator.
    """

    def test_a_rebind_advisory_is_printed_and_recorded(self):
        from helm import dispatches
        note = "authorship could not be preserved: legacy row had no sender"

        def fake_rebind(rid, to, **kw):
            return ({"new": {"id": "new-row-id"},
                     dispatches._ADMISSION_NOTES: [note]}, None)
        out, rows = [], [{"id": "legacy-row-1", "lane": "old-lane"}]
        with mock.patch.object(dispatches, "rebind", fake_rebind):
            moved, refused = seat_reassign._move_dispatch(rows, "taker",
                                                          "why", out)
        self.assertEqual(refused, [])
        self.assertIn(note, moved[0].get("advisories") or [],
                      "the advisory was not recorded on the audit entry")
        self.assertTrue(any(note in line for line in out),
                        "the advisory never reached the operator's output: %s"
                        % out)

    def test_a_CLEAN_move_prints_no_note(self):
        """THE CONTROL. Without it its sibling passes against a composer
        that prints a NOTE line unconditionally, which would train operators
        to ignore the one that matters."""
        from helm import dispatches

        def fake_rebind(rid, to, **kw):
            return ({"new": {"id": "new-row-id"}}, None)
        out, rows = [], [{"id": "modern-row", "lane": "new-lane"}]
        with mock.patch.object(dispatches, "rebind", fake_rebind):
            moved, _refused = seat_reassign._move_dispatch(rows, "taker",
                                                           "why", out)
        # THE COMPOSER RAN. Both assertions below are absences over `out`
        # and `moved`, and both hold trivially if the mover returned
        # nothing at all — so the row and the output are pinned first.
        self.assertEqual(len(moved), 1,
                         "the row did not move, so an absent NOTE says "
                         "nothing about a clean move")
        self.assertTrue(out, "the mover produced no output at all, so the "
                             "absent NOTE line is not evidence")
        self.assertNotIn("advisories", moved[0])
        self.assertFalse([line for line in out if "NOTE" in line])


class TheAuthORIZINGMeasurementPrecedesEveryMoverTest(ReassignBase):
    """Liveness was measured pre-lock, and the fresh disposition was
    minted BETWEEN the inbound dispatch move and the custody move — so the
    reading that authorizes the operation was taken after part of the
    operation had happened. A seat reviving in that window produced an
    UNFORCED SPLIT: dispatch rows relocated, then a refusal for the rest.

    STRUCTURAL, because ordering is what broke and only ordering can pin it.
    A behavioural arm would need to win a race to observe the split.
    """

    def _body(self):
        import inspect
        src = inspect.getsource(seat_reassign)
        start = src.index("def _apply_locked(")
        rest = src[start + 1:]
        nxt = rest.find("\ndef ")
        return rest[:nxt] if nxt != -1 else rest

    def test_the_disposition_is_minted_before_every_mover(self):
        body = self._body()
        mint = body.index("_tk.mint_source_disposition(seat)")
        movers = {}
        # UNCONDITIONAL FLOOR: `body.index` above already raises if the mint
        # call is missing, but nothing outside the loop proves a MOVER exists,
        # so an emptied mover tuple would make the ordering census green
        # against a helper that moves nothing.
        self.assertIn("_move_dispatch(", body,
                      "the locked helper calls no dispatch mover; the "
                      "ordering census below would compare nothing")
        for name in ("_move_dispatch", "_move_custody", "_move_tasks",
                     "_move_leases"):
            sites = [i for i, ln in enumerate(body.splitlines())
                     if name + "(" in ln and not ln.lstrip().startswith("def ")]
            self.assertTrue(sites, "%s is not called in the locked helper — "
                                   "this census cannot report on a mover it "
                                   "cannot find" % name)
            movers[name] = min(sites)
        mint_line = body[:mint].count("\n")
        for name, line in sorted(movers.items()):
            self.assertLess(mint_line, line,
                            "%s runs BEFORE the authorizing disposition is "
                            "minted, so a revive mid-command splits the move"
                            % name)

    def test_the_mint_is_inside_the_locked_helper_not_before_it(self):
        """THE CONTROL on its sibling arm: an ordering proof means nothing if
        the mint sits outside the lock, where it can go stale in exactly the
        window the lock exists to close."""
        import inspect
        src = inspect.getsource(seat_reassign)
        self.assertEqual(src.count("_tk.mint_source_disposition(seat)"), 1,
                         "more than one disposition mint — one command must "
                         "carry ONE measurement")
        self.assertIn("_tk.mint_source_disposition(seat)", self._body(),
                      "the disposition is minted outside the locked helper")


class ATargetTheMoversCannotAddressTest(ReassignBase):
    """task/2455, custody refinement — the resolver is looser than the movers.

    `resolve_target` accepts an exact roster key, and a roster key is any
    string: the real roster producer admits a 65-character name, `UNOWNED`
    and `@x`. The movers are stricter and each refuses only at its own turn,
    so on a stable roster with nothing concurrent the task moved and then the
    lease mover refused the same target. Every arm drives the real verb over
    rows written by the real producers, and asserts on the surface that would
    have moved FIRST.
    """

    LONG = "t" * 65

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)
        for cmd in (("init", "-q"), ("config", "user.email", "t@t"),
                    ("config", "user.name", "t")):
            subprocess.run(("git",) + cmd, cwd=self.repo, capture_output=True)
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:
            fh.write("a\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "a"], cwd=self.repo,
                       capture_output=True)
        self.tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                  capture_output=True, text=True).stdout.strip()
        from tests._tmphome import pin_dispatch_home
        pin_dispatch_home(self, self.repo)

    def _populate(self, target, task=True, lease=True, inbound=True,
                  outbound=False):
        """The source holds what was asked for; `target` is a roster row."""
        from helm import seats_claims
        self.seat("holder", session="s-holder-2455")
        self.seat("bystander", session="s-bystander-2455")
        self.seat(target)
        self.assertIn(target, seats_roster.roster(),
                      "fixture: the roster producer did not admit %r, so no "
                      "arm below measures a stable bad target" % target)
        made = {}
        if task:
            row, err = tasks.add("a movable row", "holder", project="helm")
            self.assertIsNone(err, "fixture: task refused (%s)" % err)
            made["task"] = row["id"]
        if lease:
            ok, why, _lease = seats_claims.claim("a-live-resource", "holder",
                                                 ttl=7200)
            self.assertTrue(ok, "fixture: claim refused (%s)" % why)
        prior = os.environ.get("HELM_CHAT_NAME")
        if inbound:
            os.environ["HELM_CHAT_NAME"] = "integrator"
            row, why, _s = dispatches.send(
                "holder", "inbound-lane", "a brief", self.tip, kind="review",
                new_work=True, repo=self.repo)
            self.assertIsNotNone(row, "fixture: inbound send refused (%s)"
                                 % why)
            made["inbound"] = row["id"]
        if outbound:
            os.environ["HELM_CHAT_NAME"] = "holder"
            row, why, _s = dispatches.send(
                "bystander", "outbound-lane", "a brief", self.tip,
                kind="review", new_work=True, repo=self.repo)
            self.assertIsNotNone(row, "fixture: outbound send refused (%s)"
                                 % why)
            made["outbound"] = row["id"]
        os.environ["HELM_CHAT_NAME"] = prior or "integrator"
        # MUST-HIT ON THE POPULATION: the census sees every planted holding,
        # or "nothing moved" below would hold over a source holding nothing.
        man, unread = seat_reassign.holdings("holder")
        self.assertEqual(unread, [], "fixture: the census is unread")
        self.assertEqual(bool(man["tasks"]), task, man)
        self.assertEqual(bool(man["leases"]), lease, man)
        self.assertEqual(bool(man["dispatch_in"]), inbound, man)
        self.assertEqual(bool(man["dispatch_out"]), outbound, man)
        return made

    def _state(self, made):
        from helm import seats_claims
        snap, unavailable = dispatches.snapshot()
        self.assertIsNone(unavailable)
        claims = ""
        if os.path.exists(seats_claims.claims_path()):
            with open(seats_claims.claims_path(), "rb") as fh:
                claims = fh.read()
        dispatch = ""
        if os.path.exists(dispatches.ledger_path()):
            with open(dispatches.ledger_path(), "rb") as fh:
                dispatch = fh.read()
        return {"owner": tasks.rows()[made["task"]]["owner"]
                if "task" in made else None,
                "claims": claims, "dispatch": dispatch,
                "audit": len(self.ledger_rows(seat_reassign.ledger_path())),
                "open_rows": sorted(r["id"] for r in snap.values()
                                    if isinstance(r, dict)
                                    and r.get("status") == "open")}

    def _reassign(self, target, apply, reason="dead", why="gone"):
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             why)):
            return seat_reassign.reassign("holder", target, reason=reason,
                                          apply=apply)

    def _refused_unmoved(self, target, made, text, reason="dead",
                         header="cannot receive these holdings", named=None):
        """rc 1 in the dry run and the apply, every surface byte-identical."""
        before = self._state(made)
        rc, lines = self._reassign(target, apply=False, reason=reason)
        self.assertEqual(rc, 1, "\n".join(lines))
        self.assertEqual(self._state(made), before,
                         "the DRY RUN wrote: %s" % "\n".join(lines))
        rc, lines = self._reassign(target, apply=True, reason=reason)
        self.assertEqual(rc, 1, "\n".join(lines))
        after = self._state(made)
        # THE SURFACES THAT MOVE BEFORE THE LEASE MOVER, asserted first: a
        # task owner changed here is the split itself.
        self.assertEqual(after["owner"], before["owner"],
                         "the task moved to a target a later mover refused — "
                         "the seat is split:\n%s" % "\n".join(lines))
        self.assertEqual(after["dispatch"], before["dispatch"],
                         "the dispatch ledger was written:\n%s"
                         % "\n".join(lines))
        self.assertEqual(after["claims"], before["claims"],
                         "the claims store was written:\n%s" % "\n".join(lines))
        self.assertEqual(after["audit"], before["audit"],
                         "an audit row was written for a refused run")
        refusal = [l for l in lines if header in l]
        self.assertTrue(refusal, "\n".join(lines))
        self.assertTrue(all((named or target) in l for l in refusal), refusal)
        self.assertTrue(any(text in l for l in refusal), refusal)
        self.assertTrue(any("nothing was moved" in l for l in lines), lines)
        return before

    def _moved(self, target, made, reason="dead", why="gone"):
        before = self._state(made)
        rc, lines = self._reassign(target, apply=True, reason=reason, why=why)
        self.assertEqual(rc, 0, "\n".join(lines))
        return before, self._state(made), lines

    def test_a_LONG_target_holding_task_lease_and_dispatch_moves_nothing(self):
        from helm import seats_claims
        made = self._populate(self.LONG)
        self._refused_unmoved(self.LONG, made, "seat token")
        # POSITIVE CONTROL ON THE SAME FIXTURE AND THE SAME OBSERVABLES: a
        # valid target moves the task, the lease and the dispatch row, so the
        # unchanged state above is the refusal and not a fixture that cannot
        # move.
        before, after, lines = self._moved("bystander", made)
        self.assertEqual(after["owner"], "bystander", lines)
        self.assertNotEqual(after["claims"], before["claims"], lines)
        self.assertNotEqual(after["dispatch"], before["dispatch"], lines)
        self.assertNotIn(made["inbound"], after["open_rows"], lines)
        self.assertEqual(after["audit"], before["audit"] + 1)
        held = [r for r in seats_claims.claims_list(gc=False) or []
                if r["resource"] == "a-live-resource"]
        self.assertEqual([r["holder_id"] for r in held], ["bystander"])

    def test_a_PLACEHOLDER_target_holding_a_task_moves_nothing(self):
        """tasks.update refuses the word the ledger prints for absence; the
        dispatch rebind that runs before it accepts that word as a token."""
        made = self._populate("UNOWNED", lease=False)
        self._refused_unmoved("UNOWNED", made, "PRINTS for an absent owner")
        before, after, lines = self._moved("bystander", made)
        self.assertEqual(after["owner"], "bystander", lines)
        self.assertNotEqual(after["dispatch"], before["dispatch"], lines)

    def test_a_PLACEHOLDER_target_with_no_task_still_moves(self):
        """MUST-MISS: the task rung is asked only when a task will move, so a
        move that works today is not refused for a mover that never runs."""
        made = self._populate("UNOWNED", task=False)
        before, after, lines = self._moved("UNOWNED", made)
        self.assertNotEqual(after["claims"], before["claims"], lines)
        self.assertNotEqual(after["dispatch"], before["dispatch"], lines)
        self.assertNotIn(made["inbound"], after["open_rows"], lines)
        self.assertFalse(any("cannot receive" in l for l in lines), lines)

    def test_an_AT_SPELLED_target_holding_a_sent_row_moves_nothing(self):
        """mark_custody stores the target verbatim and admits no `@`; the
        lease listedness and the rebind operand both strip it."""
        made = self._populate("@x", inbound=False, outbound=True)
        self._refused_unmoved("@x", made, "custody writer")
        before, after, lines = self._moved("bystander", made)
        self.assertEqual(after["owner"], "bystander", lines)
        self.assertNotEqual(after["dispatch"], before["dispatch"], lines)
        custodian = dispatches.custodian_of(
            dispatches.snapshot()[0][made["outbound"]])
        self.assertEqual(custodian, "bystander", lines)

    def test_an_UNUSABLE_target_holding_an_inbound_row_moves_nothing(self):
        """add()'s usability gate, which rebind runs with no force. Unusable
        is a proxywatch reading, so the gate itself is substituted and records
        what it was asked; the move afterwards runs the real gate."""
        made = self._populate("taker", lease=False)
        asked = []

        def unusable(recipient, force):
            asked.append((str(recipient), force))
            return False, "recipient taker is UNUSABLE (substituted)", None
        with mock.patch.object(dispatches, "_validate_recipient_usable",
                               side_effect=unusable):
            self._refused_unmoved("taker", made, "UNUSABLE (substituted)")
        self.assertIn(("taker", False), asked)
        before, after, lines = self._moved("taker", made)
        self.assertEqual(after["owner"], "taker", lines)
        self.assertNotEqual(after["dispatch"], before["dispatch"], lines)
        self.assertNotIn(made["inbound"], after["open_rows"], lines)

    SPLIT = "a mover would split this move"

    def test_a_LONG_reason_holding_a_sent_row_moves_nothing(self):
        """The custody writer refuses a reason over 256 characters. It runs
        after the dispatch rebind, which clips, and before the task mover,
        which does not look — so the task moved and the sent row stayed."""
        made = self._populate("taker", outbound=True)
        self._refused_unmoved("taker", made, "at most 256 characters",
                              reason="r" * 300, header=self.SPLIT,
                              named="dispatch custody refuses the reason")
        before, after, lines = self._moved("taker", made)
        self.assertEqual(after["owner"], "taker", lines)
        self.assertNotEqual(after["claims"], before["claims"], lines)
        self.assertNotIn(made["inbound"], after["open_rows"], lines)
        self.assertEqual(dispatches.custodian_of(
            dispatches.snapshot()[0][made["outbound"]]), "taker", lines)

    def test_a_NEWLINE_reason_holding_an_inbound_row_moves_nothing(self):
        """rebind clips the reason but keeps the newline; add() appends the
        child, then mark_cancel refuses the cancel and a second write disowns
        the child — the dispatch ledger written, the row refused, and the task
        and lease moved anyway."""
        made = self._populate("taker")
        self._refused_unmoved("taker", made, "one printable line",
                              reason="dead\nreally", header=self.SPLIT,
                              named="dispatch rebind refuses the reason")
        before, after, lines = self._moved("taker", made)
        self.assertEqual(after["owner"], "taker", lines)
        self.assertNotIn(made["inbound"], after["open_rows"], lines)

    def test_a_LONG_reason_with_no_sent_row_still_moves(self):
        """MUST-MISS: rebind clips a long reason and the custody writer does
        not run, so a move that works today is not refused."""
        made = self._populate("taker")
        before, after, lines = self._moved("taker", made, reason="r" * 300)
        self.assertEqual(after["owner"], "taker", lines)
        self.assertNotEqual(after["claims"], before["claims"], lines)
        self.assertNotIn(made["inbound"], after["open_rows"], lines)
        self.assertFalse(any(self.SPLIT in l for l in lines), lines)

    def test_the_DEFAULT_reason_is_one_line_every_mover_accepts(self):
        """No --reason: the recorded reason embeds the disposition prose,
        which is unbounded. It is made one printable line of 256 characters or
        fewer, so a long disposition moves every surface."""
        made = self._populate("taker", outbound=True)
        why = "w" * 300 + "\nsecond line"
        before, after, lines = self._moved("taker", made, reason=None,
                                           why=why)
        self.assertEqual(after["owner"], "taker", lines)
        self.assertNotEqual(after["claims"], before["claims"], lines)
        self.assertNotIn(made["inbound"], after["open_rows"], lines)
        self.assertEqual(dispatches.custodian_of(
            dispatches.snapshot()[0][made["outbound"]]), "taker", lines)
        recorded = self.ledger_rows(seat_reassign.ledger_path())[-1]["reason"]
        self.assertLessEqual(len(recorded), 256, recorded)
        self.assertNotIn("\n", recorded)
        self.assertTrue(recorded.startswith("seat reassign: holder is"),
                        recorded)

    def test_a_process_with_NO_SEAT_IDENTITY_holding_an_inbound_row_moves_nothing(self):
        """add() refuses a process that declares no seat and has no roster
        binding. rebind runs first and refuses every row; custody, tasks and
        leases ask nobody, so they moved."""
        made = self._populate("taker")
        from helm import home
        for var in ("HELM_CHAT_NAME",) + tuple(home._SESSION_ENV):
            os.environ.pop(var, None)
        self.assertIsNotNone(dispatches._acting_author()[1],
                             "fixture: this process still has an identity")
        self._refused_unmoved("taker", made, "family floor",
                              header=self.SPLIT,
                              named="dispatch rebind refuses this process")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        before, after, lines = self._moved("taker", made)
        self.assertEqual(after["owner"], "taker", lines)
        self.assertNotIn(made["inbound"], after["open_rows"], lines)

    def test_a_CASE_VARIANT_source_converges_to_rc_0(self):
        """A source label that differs from its target only in case. The
        dispatch writer stores the casefolded recipient, so rows sent to
        `Worker` are stored as `worker` and already route to the rostered
        `worker`. rebind refuses them as "already addressed" and the census
        still lists them under `Worker`, so every run exited 1 and nothing
        could make it converge."""
        self.seat("worker", session="s-worker-2455")
        self.seat("bystander", session="s-bystander-2455")
        self.assertNotIn("Worker", seats_roster.roster())
        prior = os.environ.get("HELM_CHAT_NAME")
        os.environ["HELM_CHAT_NAME"] = "integrator"
        row, why, _s = dispatches.send(
            "Worker", "inbound-lane", "a brief", self.tip, kind="review",
            new_work=True, repo=self.repo)
        self.assertIsNotNone(row, "fixture: inbound send refused (%s)" % why)
        os.environ["HELM_CHAT_NAME"] = prior or "integrator"
        man, unread = seat_reassign.holdings("Worker")
        self.assertEqual(unread, [])
        self.assertEqual([r["id"] for r in man["dispatch_in"]], [row["id"]],
                         "fixture: the census does not list the row under the "
                         "case-variant label, so this arm measures nothing")
        with open(dispatches.ledger_path(), "rb") as fh:
            ledger = fh.read()
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "gone")):
            for _ in range(2):
                rc, lines = seat_reassign.reassign("Worker", "worker",
                                                   reason="dead", apply=True)
                self.assertEqual(rc, 0, "\n".join(lines))
        with open(dispatches.ledger_path(), "rb") as fh:
            self.assertEqual(fh.read(), ledger, "a row already addressed to "
                             "the target was rewritten")
        self.assertIn(row["id"], [
            r["id"] for r in dispatches.snapshot()[0].values()
            if isinstance(r, dict) and r.get("status") == "open"])
        self.assertTrue(any("already dispatch" in l for l in lines), lines)
        audit = self.ledger_rows(seat_reassign.ledger_path())[-1]
        self.assertEqual(audit["refused"]["dispatch"], [], audit)
        self.assertEqual(audit["remaining"]["dispatch_in"], 0, audit)

    def _claim(self, resource, holder):
        from helm import seats_claims
        ok, why, _lease = seats_claims.claim(resource, holder, ttl=7200)
        self.assertTrue(ok, "fixture: claim refused (%s)" % why)

    @staticmethod
    def _holders():
        from helm import seats_claims
        return {r["resource"]: r["holder_id"]
                for r in seats_claims.claims_list(gc=False) or []}

    @staticmethod
    def _claims_bytes():
        from helm import seats_claims
        with open(seats_claims.claims_path(), "rb") as fh:
            return fh.read()

    def _reassign_dead(self, source, target):
        with mock.patch.object(seat_reassign, "source_disposition",
                               return_value=(seat_reassign.SOURCE_DEAD,
                                             "gone")):
            rc, lines = seat_reassign.reassign(source, target, reason="dead",
                                               apply=True)
        return rc, lines, self.ledger_rows(seat_reassign.ledger_path())[-1]

    def test_a_CASE_VARIANT_lease_the_mover_transferred_converges_to_rc_0(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the re-run's empty moved-leases list; its unconditional positive is the first run on the SAME fixture asserting exactly one moved lease and the holder rewritten to worker, and the re-run's own positive is the byte-identical claims store beside it
        """The lease mover matches the holder byte for byte and stores the
        target verbatim, so a lease claimed as `Worker` really moves to the
        rostered `worker`. The final census matches the source canonically,
        still listed that lease under `Worker`, and counted it as remaining:
        rc 1 after a complete move, and rc 1 again on a re-run that had
        nothing left to move (task/2524)."""
        self.seat("worker", session="s-worker-2524")
        self.assertNotIn("Worker", seats_roster.roster())
        self._claim("a-live-resource", "Worker")
        man, unread = seat_reassign.holdings("Worker")
        self.assertEqual(unread, [])
        self.assertEqual([r["resource"] for r in man["leases"]],
                         ["a-live-resource"],
                         "fixture: the census does not list the lease under "
                         "the case-variant label, so this arm measures nothing")
        rc, lines, audit = self._reassign_dead("Worker", "worker")
        self.assertEqual(self._holders(), {"a-live-resource": "worker"},
                         "\n".join(lines))
        self.assertEqual(len(audit["moved"]["leases"]), 1, audit)
        self.assertEqual((rc, audit["remaining"]["leases"]), (0, 0),
                         "\n".join(lines))
        claims = self._claims_bytes()
        rc, lines, audit = self._reassign_dead("Worker", "worker")
        self.assertEqual((rc, audit["remaining"]["leases"]), (0, 0),
                         "\n".join(lines))
        self.assertEqual(audit["moved"]["leases"], [], audit)
        self.assertEqual(self._claims_bytes(), claims,
                         "a re-run with nothing to move rewrote the claims")

    def test_a_canonically_DISTINCT_source_lease_moves_and_leaves_the_census(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is the source's empty lease census after the move; its unconditional positive is the same census listing the lease before the move, and the holder rewritten to worker on the claims table
        """CONTROL: a source that shares no canonical token with the target.
        Only its own spelling holds the lease, so the move empties the census
        and the byte-for-byte rule changes nothing about this path."""
        self.seat("worker", session="s-worker-2524")
        self._claim("a-live-resource", "holder")
        man, unread = seat_reassign.holdings("holder")
        self.assertEqual(unread, [])
        self.assertEqual([r["resource"] for r in man["leases"]],
                         ["a-live-resource"], "fixture: the census is empty")
        rc, lines, audit = self._reassign_dead("holder", "worker")
        self.assertEqual(self._holders(), {"a-live-resource": "worker"},
                         "\n".join(lines))
        self.assertEqual((rc, audit["remaining"]["leases"]), (0, 0),
                         "\n".join(lines))
        man, unread = seat_reassign.holdings("holder")
        self.assertEqual(unread, [])
        self.assertEqual(man["leases"], [], lines)

    def test_a_lease_under_a_spelling_the_mover_does_not_match_STAYS_counted(self):
        """CONTROL: `WORKER` matches the source `Worker` canonically, so the
        census lists its lease, but the mover matches raw and leaves it. The
        target cannot release a lease stored as `WORKER` either, so it is a
        real holding left behind: counted, and the verb exits 1 on every
        run. Suppressing every canonical match of the target would hide it."""
        self.seat("worker", session="s-worker-2524")
        self._claim("a-live-resource", "Worker")
        self._claim("an-untouched-resource", "WORKER")
        man, unread = seat_reassign.holdings("Worker")
        self.assertEqual(unread, [])
        self.assertEqual(sorted(r["resource"] for r in man["leases"]),
                         ["a-live-resource", "an-untouched-resource"],
                         "fixture: the census does not list both spellings")
        for _ in range(2):
            rc, lines, audit = self._reassign_dead("Worker", "worker")
            self.assertEqual(self._holders(),
                             {"a-live-resource": "worker",
                              "an-untouched-resource": "WORKER"},
                             "\n".join(lines))
            self.assertEqual((rc, audit["remaining"]["leases"]), (1, 1),
                             "\n".join(lines))
            self.assertTrue(any("leases x1" in l for l in lines), lines)
        # THE REMEDY THE COUNT POINTS AT: naming the spelling that holds it
        # moves it, and that run exits 0.
        rc, lines, audit = self._reassign_dead("WORKER", "worker")
        self.assertEqual(self._holders(),
                         {"a-live-resource": "worker",
                          "an-untouched-resource": "worker"}, "\n".join(lines))
        self.assertEqual((rc, audit["remaining"]["leases"]), (0, 0),
                         "\n".join(lines))

    def test_a_lease_whose_stored_holder_only_SCRUBS_to_the_target_STAYS_counted(self):  # noqa: VACUOUS_ASSERTION — the flagged absences are the empty unread list and the target's refused bindings; their positives are the census listing all three leases before the loop and, on every pass of the fixed two-run loop, the stored holders read from the same claims file and a remaining count of 2
        """CONTROL: `worker ` and `worker` plus a zero-width space are stored
        verbatim by claim, and claims_list publishes both as holder_id
        `worker`. The lease mover compares the stored holder and does not move
        them; `_binding_ok` compares the stored holder, so `worker` cannot
        release them either. They are real holdings left on `Worker`. A final
        census that trusted the scrubbed name reported them clean at rc 0."""
        from helm import seats_claims
        padded = {"a-padded-resource": "worker ",
                  "a-format-char-resource": "worker​"}
        self.seat("worker", session="s-worker-2524")
        self._claim("a-live-resource", "Worker")
        leases = {}
        for resource, spelling in padded.items():
            ok, why, leases[resource] = seats_claims.claim(resource, spelling,
                                                           ttl=7200)
            self.assertTrue(ok, "fixture: claim refused (%s)" % why)
        # MUST-HITS: the scrub really folds both spellings into the target's
        # published name, and the census really lists them under the source.
        self.assertEqual(self._holders(),
                         {"a-live-resource": "Worker",
                          "a-padded-resource": "worker",
                          "a-format-char-resource": "worker"},
                         "fixture: the scrub does not fold the spellings, so "
                         "this arm measures nothing")
        man, unread = seat_reassign.holdings("Worker")
        self.assertEqual(unread, [])
        self.assertEqual(sorted(r["resource"] for r in man["leases"]),
                         sorted(["a-live-resource"] + list(padded)),
                         "fixture: the census does not list all three leases")
        for _ in range(2):
            rc, lines, audit = self._reassign_dead("Worker", "worker")
            with open(seats_claims.claims_path(), encoding="utf-8") as fh:
                stored = json.load(fh)
            self.assertEqual(
                {r: stored[r]["holder"]
                 for r in ["a-live-resource"] + list(padded)},
                dict(padded, **{"a-live-resource": "worker"}),
                "\n".join(lines))
            for resource in padded:
                self.assertEqual(
                    seats_claims._binding_ok(stored[resource], "worker",
                                             leases[resource], None)[0],
                    False, "fixture: the target can release %s, so it is "
                           "not left behind" % resource)
            self.assertEqual((rc, audit["remaining"]["leases"]), (1, 2),
                             "\n".join(lines))
            self.assertTrue(any("leases x2" in l for l in lines), lines)


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()
