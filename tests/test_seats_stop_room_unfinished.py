#!/usr/bin/env python3
"""Is there UNFINISHED WORK BOUND TO THIS ROOM — and does the advice name it?

The release advice under a held lane lease must name the work that holds it,
and DELEGATION is the wrong basis for that answer twice over. It accounts for
one of four measured live holds -- an outstanding review, a gate in flight and
a cure awaiting re-review after a FIX verdict are all correct holds it cannot
see -- and it is not answerable from outside the seat at all, since no subagent
probe reaches those three. "Is work still bound to this room" is
deterministically computable from state helm already keeps, through the four
primitives that already own it: `_room_status`, `_merge_state`,
`dispatches.owed` and `gate.inflight`. Every arm plants REAL state in a REAL
scratch repo and asserts the advice NAMES it, so a guard that holds for the
right reason and a guard that holds by luck read differently.

MOVED WHOLE OUT OF `tests/test_seats.py`, which stood 56,594 bytes under the
1 MiB never-track ceiling with arms still landing in it. This class alone was
85,297 of those bytes. No body was rewritten on the way; the class is the
byte-identical text it had there.

THE FIXTURE IS REUSED BY REFERENCE, NEVER COPIED. `SeatsBase` is imported from
the module these arms came from, so one fixture serves both files and the two
cannot drift.
"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from unittest import mock

from tests.test_seats import SeatsBase

from helm import (chat, dispatches, seats, seats_room_advice,
                  seats_stop_budget, seats_stop_claims)

# THE FIXTURE THAT PROTECTS THESE ARMS LIVES IN ANOTHER FILE, and two
# source-driven audits read THIS one. `SeatsBase.setUp` snapshots every key in
# `ENV_KEYS`, sets `HELM_SCRATCH_GC=0` so the stop hook's silent-mechanical
# lane cannot reap real `/tmp/claude-*` host scratch, and restores the lot in
# tearDown. tests/test_env_hygiene.py and tests/test_scratch.py each parse a
# module ON ITS OWN, so neither can follow an imported base class -- and moving
# these arms out of tests/test_seats.py moved them out of the only file where
# those two audits could see the guarantee. Both went red on the whole-suite
# gate for exactly that reason; neither was wrong to.
#
# SO IT IS RESTATED HERE AS SOMETHING THAT RUNS, not as a comment and not as an
# allowlist entry. This module snapshots the keys its own arms write, puts them
# back when the module is done, and disables the scratch reaper ITSELF rather
# than trusting that it inherited the setting. If SeatsBase ever stops doing
# either, this module is still safe instead of quietly deleting host scratch.
_ENV_PRIOR = {}


def setUpModule():
    _ENV_PRIOR["HELM_SCRATCH_GC"] = os.environ.get("HELM_SCRATCH_GC")
    os.environ["HELM_SCRATCH_GC"] = "0"
    # No dispatch row this module writes walks the host's process table
    # (task/3039; see tests._tmphome.pin_live_seats).
    from tests._tmphome import pin_live_seats
    pin_live_seats()


def tearDownModule():
    for key, was in _ENV_PRIOR.items():
        if was is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = was


class StopGuardRoomUnfinishedTest(SeatsBase):
    """#112. The release advice under a held LANE lease used to ask about
    DELEGATION — and delegation was the wrong question AND an unanswerable
    one. Four live holds measured in one night: an outstanding review, a
    gate in flight, a cure awaiting re-review after a FIX verdict, and one
    genuine subagent build. The correct answer was HOLD all four times and
    delegation explained exactly ONE, so the guard's basis was right 1 time
    in 4 — and the other three were correct holds for reasons no subagent
    probe can see.

    The wider question — IS THERE UNFINISHED WORK BOUND TO THIS ROOM? — is
    deterministically computable from state helm already keeps, through the
    four primitives that already own it (`_room_status`, `_merge_state`,
    `dispatches.owed`, `gate.inflight`). Every case here plants REAL state
    in a REAL scratch repo and asserts the ADVICE NAMES IT; every one has
    its negative, because a sentence that appears whatever the room holds
    would be decoration, not a measurement."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        # THIS FIXTURE IS ITS OWN PROJECT: the dispatch write door would
        # otherwise refuse every row here as FOREIGN.
        from tests._tmphome import pin_dispatch_home
        self._real_home_repo_id = pin_dispatch_home(self, self.root)
        self.commit("tip", self.root)
        self.head = self.rev(self.root)
        self.wt = self.root + "-wt/lane-u"
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-u", self.wt], check=True,
                       capture_output=True, timeout=30)
        self.res = "worktree:proj:lane-u"
        self.sid = "s-room-" + os.urandom(6).hex()
        self._cwd = os.getcwd()
        os.chdir(self.root)             # the guard anchors the project from cwd
        self._ledger = dispatches.ledger_path
        self.ledger = os.path.join(self.tmp, "dispatches.jsonl")
        dispatches.ledger_path = lambda: self.ledger

    def tearDown(self):
        dispatches.ledger_path = self._ledger
        os.chdir(self._cwd)
        super().tearDown()

    def commit(self, msg, cwd):
        subprocess.run(["git", "-C", cwd, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", msg], check=True, capture_output=True,
                       timeout=30)

    def rev(self, cwd, ref="HEAD"):
        return subprocess.run(["git", "-C", cwd, "rev-parse", ref],
                              capture_output=True, text=True, check=True,
                              timeout=30).stdout.strip()

    def plant(self, ref=None, recipient="ds4pro", lane="lane-u", repo=None):  # noqa: SEAT_NAME — moved whole; this exact line is already tracked in tests/test_seats.py, so the move is not a new copy
        """A REAL review row through the REAL writer — hand-planted JSONL is
        a fiction the replay discards."""
        # PLANT AS THE REPOSITORY THE ROW BINDS — three arms here plant into a
        # deliberately FOREIGN repo to prove the stop-guard tells repositories
        # apart, and the write door refuses a foreign ref by design. Being that
        # repo for the length of the write is how such a row honestly exists.
        from tests._tmphome import dispatch_home
        target = repo or self.root
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "room-fixture"}), \
                dispatch_home(target):
            row = dispatches.add(recipient, lane, ref=ref or self.head,
                                 kind="review", notify=False,
                                 repo=target, new_work=True)
        self.assertIsNotNone(row, "plant: the real writer refused")
        dispatches._mark_delivered(row["id"], ref or self.head)
        return row

    def other_repo(self, name="other-project"):
        """A SECOND real repository — the foreign half of the review's
        two-repo fixture. Its head is a valid ref for the REAL writer, so
        a foreign row is stamped with a repo_id the writer actually emits,
        never a hand-typed shape."""
        other = os.path.join(self.tmp, name)
        os.makedirs(other)
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=other,
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.commit("tip", other)
        return other

    def corrupt_ledger_repo_id(self, value):
        """Rewrite the ledger's `repo_id` to a NON-CANONICAL value, leaving
        every other byte the real writer emitted. The repro used the
        integer 123: truthy, unequal to the room's, and validated by
        nothing. As with `age_ledger_to_pre_repo_id`, the caller must
        re-prove the row still rides the REAL owed replay before reading."""
        with open(self.ledger) as fh:
            lines = fh.readlines()
        with open(self.ledger, "w") as fh:
            for ln in lines:
                d = json.loads(ln)
                d["repo_id"] = value
                fh.write(json.dumps(d) + "\n")

    def test_a_MALFORMED_repo_id_is_UNREADABLE_never_silently_foreign(self):
        """A `repo_id` that is truthy but not a canonical path is NOT proof
        of another project — it is a value nothing validated, and treating
        it as foreign EXCLUDES the row leaving no trace at all.

        The exact repro: rewrite a real row's `repo_id` to the
        integer 123. Replay still owes it, and the first cut answered
        findings=[] unknowns=[] — a false clean assembled from an
        unvalidated field. Exclusion is the one outcome with no residue,
        so it must be earned by a POSITIVE canonical identity rather than
        by mere inequality."""
        self.ledger = os.path.join(self.tmp, "d-malformed.jsonl")
        row = self.plant(ref=self.head)
        self.corrupt_ledger_repo_id(123)
        # The row must still ride the REAL owed replay, or this arm would be
        # asserting about a row the reader never saw — same re-proof the
        # legacy arm makes, not assumed.
        snap, note = dispatches.snapshot()
        self.assertFalse(note, note)
        carried = [r for r in dispatches.owed(snap) if r.get("id") == row["id"]]
        self.assertEqual(len(carried), 1,
                         "fixture: replay must still owe the corrupted row")
        self.assertEqual(carried[0].get("repo_id"), 123)
        findings, unknowns = seats._room_unfinished(self.res)
        reviews = [x for x in findings if "review dispatch" in x]
        unread = [x for x in unknowns if x.startswith("review:")]
        self.assertEqual(reviews, [], "a row helm cannot identify must not "
                                      "be reported as this room's work")
        self.assertEqual(len(unread), 1,
                         "a malformed repo_id must contribute exactly ONE "
                         "review UNKNOWN, not silence and not N entries")
        self.assertIn("READABLE", unread[0])

    def test_MALFORMED_and_KNOWN_FOREIGN_do_not_share_an_outcome(self):
        """The discrimination that matters is malformed vs FOREIGN, because
        only foreign earns silent exclusion. Malformed shares its signature
        with legacy-unscoped ON PURPOSE — both are unreadable — so the pair
        this arm separates is the one where being wrong is invisible."""
        other = self.other_repo()

        def owes(row_id):
            snap, note = dispatches.snapshot()
            self.assertFalse(note, note)
            return len([r for r in dispatches.owed(snap)
                        if r.get("id") == row_id])

        self.ledger = os.path.join(self.tmp, "d-mal.jsonl")
        mal_row = self.plant(ref=self.head)
        self.corrupt_ledger_repo_id(123)
        self.assertEqual(owes(mal_row["id"]), 1,
                         "fixture: replay must still owe the corrupted row")
        f_mal, u_mal = seats._room_unfinished(self.res)
        mal = (len([x for x in f_mal if "review dispatch" in x]),
               len([x for x in u_mal if x.startswith("review:")]))
        self.ledger = os.path.join(self.tmp, "d-for.jsonl")
        for_row = self.plant(ref=self.rev(other), repo=other)
        self.assertEqual(owes(for_row["id"]), 1,
                         "fixture: replay must still owe the foreign row")
        f_for, u_for = seats._room_unfinished(self.res)
        foreign = (len([x for x in f_for if "review dispatch" in x]),
                   len([x for x in u_for if x.startswith("review:")]))
        self.assertEqual(mal, (0, 1))       # unreadable -> named as UNKNOWN
        self.assertEqual(foreign, (0, 0))   # positively elsewhere -> excluded
        self.assertNotEqual(mal, foreign,
                            "an unvalidated repo_id was treated as proof of "
                            "another project — the silent-exclusion hole")

    def test_the_SAME_repo_spelled_ODDLY_is_never_silently_excluded(self):
        """The room's own work, written with a non-normalised `repo_id`, is
        still the room's own work. `room_id` comes through `_repo_info`'s
        `os.path.realpath`; the row side was merely stripped, so the two
        halves of the `==` normalised DIFFERENTLY and the loser was dropped
        with no trace — an exclusion earned by INEQUALITY, which is the one
        thing this lane's own law forbids (the T2 finding).

        Every spelling below resolves to the room's canonical id and is
        DERIVED from it, never typed, so the arm cannot drift from whatever
        the writer actually stamps. The canonical spelling rides the SAME
        assertions as a control: if the reader stopped seeing same-room work
        at all, the control fails first and the two findings cannot pass by
        being uniformly invisible."""
        canon = (dispatches._repo_info(self.root) or {}).get("repo_id")
        self.assertTrue(canon and canon.startswith(os.sep),
                        "fixture: the room must have a canonical repo_id")

        def read_under(i, spelling):
            """Plant a REAL row, rewrite ONLY its `repo_id` to `spelling`,
            re-prove the replay still owes it, and return what the room read
            said. Every case travels this one path, so the controls and the
            findings differ in the spelling and in nothing else."""
            self.ledger = os.path.join(self.tmp, "d-spell-%d.jsonl" % i)
            row = self.plant(ref=self.head)
            self.corrupt_ledger_repo_id(spelling)
            snap, note = dispatches.snapshot()
            self.assertFalse(note, note)
            carried = [r for r in dispatches.owed(snap)
                       if r.get("id") == row["id"]]
            self.assertEqual(len(carried), 1,
                             "fixture: replay must still owe the row")
            self.assertEqual(carried[0].get("repo_id"), spelling)
            findings, unknowns = seats._room_unfinished(self.res)
            return ([f for f in findings if "OPEN review dispatch" in f],
                    [x for x in unknowns if x.startswith("review:")])

        # THE CONTROL RUNS UNCONDITIONALLY, ahead of any loop. A reader that
        # stopped seeing same-room work AT ALL would fail both spellings
        # below with the identical `0 != 1` and then go identically green on
        # any change that restored nothing — so the claim "the odd spellings
        # were excluded" is only measurable against a canonical row proven
        # visible on the same code path, in the same test, every run.
        named, unread = read_under(0, canon)
        self.assertEqual(len(named), 1,
                         "control: a canonically-spelled same-room review "
                         "must be NAMED, or this arm measures nothing")
        self.assertEqual(unread, [],
                         "control: a canonical id is readable")

        # SECOND CONTROL, on the OTHER observable. Every `unread == []` in
        # this arm is an ABSENCE, and an absence is only a measurement once
        # something is shown to reach that list on the same path — otherwise
        # a reader whose UNKNOWNs never carried the `review:` prefix would
        # satisfy every one of them forever. The integer repro is the
        # cheapest thing that must land there.
        named, unread = read_under(1, 123)
        self.assertEqual(named, [],
                         "control: an unreadable id is not this room's work")
        self.assertEqual(len(unread), 1,
                         "control: an unreadable id MUST reach the review "
                         "UNKNOWN channel, or `unread == []` proves nothing")

        # The two spellings run UNROLLED, not in a loop. A loop body's
        # absence assertion is covered only by that same body's positive
        # one, so an empty sequence silences BOTH and the arm passes having
        # measured nothing — the vacuity this file's rung exists to catch.
        # Two cases do not need a loop to hide behind.
        parent, base = os.path.split(canon)
        doubled = parent + os.sep + os.sep + base
        self.assertNotEqual(doubled, canon, "fixture: a doubled separator "
                                            "must differ from the canonical")
        self.assertEqual(os.path.realpath(doubled), canon,
                         "fixture: it must still name the room's own repo")
        named, unread = read_under(2, doubled)
        self.assertEqual(len(named), 1,
                         "a DOUBLED SEPARATOR names this room's open review "
                         "and must not be dropped over a spelling comparison")
        self.assertEqual(unread, [],
                         "a resolvable same-room spelling is READABLE — it "
                         "must not be laundered into an UNKNOWN either")

        dotdot = os.sep.join([parent, os.pardir,
                              os.path.basename(parent), base])
        self.assertNotEqual(dotdot, canon, "fixture: a dot-dot round trip "
                                           "must differ from the canonical")
        self.assertEqual(os.path.realpath(dotdot), canon,
                         "fixture: it must still name the room's own repo")
        named, unread = read_under(3, dotdot)
        self.assertEqual(len(named), 1,
                         "a DOT-DOT ROUND TRIP names this room's open review "
                         "and must not be dropped over a spelling comparison")
        self.assertEqual(unread, [],
                         "a resolvable same-room spelling is READABLE — it "
                         "must not be laundered into an UNKNOWN either")

    def test_a_BARE_SLASH_repo_id_stays_UNREADABLE_after_normalising(self):
        """The negative that keeps the cure honest. Resolving the row side
        makes `/` a perfectly valid path — the filesystem root — so a naive
        `realpath` BEFORE the absolute-path guard would promote a junk value
        into a readable identity and silently exclude it as foreign. The
        `rstrip` stays ahead of the guard, so a bare separator still collapses
        to unreadable and the row rides `unknowns` where it belongs."""
        self.ledger = os.path.join(self.tmp, "d-bare-slash.jsonl")
        row = self.plant(ref=self.head)
        self.corrupt_ledger_repo_id(os.sep)
        snap, note = dispatches.snapshot()
        self.assertFalse(note, note)
        carried = [r for r in dispatches.owed(snap)
                   if r.get("id") == row["id"]]
        self.assertEqual(len(carried), 1,
                         "fixture: replay must still owe the row")
        self.assertEqual(carried[0].get("repo_id"), os.sep)
        findings, unknowns = seats._room_unfinished(self.res)
        named = [f for f in findings if "OPEN review dispatch" in f]
        unread = [x for x in unknowns if x.startswith("review:")]
        self.assertEqual(named, [], "a bare separator identifies nothing")
        self.assertEqual(len(unread), 1,
                         "a bare separator must contribute exactly ONE review "
                         "UNKNOWN — not silence, and not N entries")
        self.assertIn("READABLE", unread[0])

    def age_ledger_to_pre_repo_id(self):
        """Rewrite the ledger's rows to the bytes a PRE-repo_id writer
        emitted: today's real-writer rows minus the one field that did not
        exist yet. NOT hand-built JSON — every row was written by the real
        writer first, and the caller must re-prove the aged row still rides
        the REAL snapshot/owed replay before reading anything from it."""
        with open(self.ledger) as fh:
            lines = fh.readlines()
        with open(self.ledger, "w") as fh:
            for ln in lines:
                d = json.loads(ln)
                d.pop("repo_id", None)
                fh.write(json.dumps(d) + "\n")

    # --- read 1: a live worker's uncommitted bytes ------------------------
    def test_uncommitted_bytes_in_the_room_are_NAMED(self):
        """The read that covers the ONE specimen delegation got right — a
        delegate genuinely building leaves files behind, and those files are
        measurable where the delegate is not."""
        clean, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [], "a live scratch room must read clean")
        self.assertEqual([f for f in clean if "UNCOMMITTED" in f], [],
                         "the negative control: a clean room claims nothing")
        with open(os.path.join(self.wt, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        dirty, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [])
        named = [f for f in dirty if f.startswith("UNCOMMITTED changes")]
        self.assertEqual(len(named), 1, dirty)
        self.assertRegex(named[0],
                         r"^UNCOMMITTED changes in the room \(newest write \d+s ago\)$")

    # --- read 2: committed work the trunk does not have -------------------
    def test_a_committed_but_UNLANDED_tip_is_NAMED(self):
        """Specimens 1-3 share this shape: the lane is CLEAN and the work is
        real, sitting in commits the trunk has never seen. Delegation
        answers NO to all three and would have advised opening the room."""
        landed, _u = seats._room_unfinished(self.res)
        self.assertEqual([f for f in landed if "NOT landed" in f], [],
                         "a room at the branch point owes the trunk nothing")
        self.commit("lane work", self.wt)
        tip = self.rev(self.wt)
        unlanded, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [])
        self.assertIn("the room's tip %s is NOT landed by ancestry or patch "
                      "identity vs the trunk" % tip[:12], unlanded)

    # --- read 3: somebody else holds the verdict --------------------------
    def test_an_open_review_is_NAMED_at_the_exact_tip_the_exemption_cannot_use(self):
        """WIDER THAN THE EXEMPTION, on purpose. `_gate_pending` demands
        ref == HEAD because it grants an UN-GUARDING; this only NAMES what
        is outstanding, so a review row whose ref the holder has since
        committed past is still named — and that is exactly the specimen
        (cure-on-top-of-review) the exemption is built to refuse."""
        row = self.plant(ref=self.head)
        self.commit("cure", self.wt)
        self.assertIsNone(seats._gate_pending(self.res),
                          "precondition: the exemption must NOT fire here")
        findings, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [])
        named = [f for f in findings if "OPEN review dispatch" in f]
        self.assertEqual(len(named), 1, findings)
        self.assertIn(row["id"][:12], named[0])
        self.assertIn("ds4pro", named[0])  # noqa: SEAT_NAME — moved whole; this exact line is already tracked in tests/test_seats.py, so the move is not a new copy
        self.assertIn("NOT the room's current tip", named[0])
        # the negative: a CLOSED obligation is not outstanding work
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "room-fixture"}):
            done, err = dispatches.mark_cancel(row["id"], "withdrawn")
        self.assertIsNone(err, err)
        self.assertTrue(done)
        after, _u = seats._room_unfinished(self.res)
        self.assertEqual([f for f in after if "review dispatch" in f], [],
                         "a cancelled row must stop being named")

    def test_an_open_review_AT_the_tip_says_so_rather_than_guessing(self):
        """The ref DOES match here, so the wording must change with the
        fact. (This shape normally exits earlier through the exemption; the
        read is asserted directly because the sentence has to be right
        wherever it is reached — a moved lane stem, an ambiguous pair.)"""
        row = self.plant(ref=self.head)
        findings, _u = seats._room_unfinished(self.res)
        named = [f for f in findings if "OPEN review dispatch" in f]
        self.assertEqual(len(named), 1, findings)
        self.assertIn("at this exact tip", named[0])
        self.assertNotIn("NOT the room's current tip", named[0])
        self.assertIn(row["id"][:12], named[0])

    def test_a_same_lane_review_in_ANOTHER_repo_is_NOT_this_rooms_work(self):
        """The exact two-repo fixture, real writer + real owed
        frontier. `_room_unfinished` derived only the lane STEM, so the one
        open review named `lane-u` — living in a DIFFERENT project — was
        printed as UNFINISHED WORK BOUND TO THIS ROOM with unknowns=[]. A
        lane name is free text shared across every project on the box; the
        row's writer-stamped repo_id against `dispatches._repo_info(room)`
        is the identity, and a row POSITIVELY scoped elsewhere is not this
        room's work in any sense — excluded, not UNKNOWN."""
        other = self.other_repo()
        foreign = self.plant(ref=self.rev(other), repo=other)
        # the fixture's own premise, measured: the writer stamped a real,
        # DIFFERENT canonical repo_id on the foreign row
        snap, note = dispatches.snapshot()
        self.assertFalse(note, note)
        frow = snap[foreign["id"]]
        self.assertTrue(frow.get("repo_id"), "the real writer stamps repo_id")
        self.assertNotEqual(frow["repo_id"],
                            dispatches._repo_info(self.wt)["repo_id"])
        findings, unknowns = seats._room_unfinished(self.res)
        self.assertEqual([f for f in findings if "review dispatch" in f], [],
                         findings)
        self.assertEqual(unknowns, [],
                         "a KNOWN foreign row is excluded, never UNKNOWN")
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep)
        self.assertIn("found nothing bound to this room YET", text)
        # POSITIVE CONTROL on the same observable: the same lane stem in
        # THIS repo IS named, and the foreign row still is not
        mine = self.plant(ref=self.head)
        findings2, unknowns2 = seats._room_unfinished(self.res)
        named = [f for f in findings2 if "OPEN review dispatch" in f]
        self.assertEqual(len(named), 1, findings2)
        self.assertIn(mine["id"][:12], named[0])
        self.assertNotIn(foreign["id"][:12], "\n".join(findings2))
        self.assertEqual(unknowns2, [])

    def test_a_LEGACY_unscoped_same_lane_row_is_UNKNOWN_never_same_project_proof(self):
        """The third arm, the one it is tempting to collapse either way. A
        row predating the repo_id field is RELEVANT (same lane family) but
        UNPROVABLE (scoped to nothing), so it may neither count as
        same-project proof nor vanish like a known-foreign row: it rides
        `unknowns` — task/142's settled Stop rule (unscoped relevant rows
        contribute UNKNOWN), applied at this read."""
        row = self.plant(ref=self.head)
        self.age_ledger_to_pre_repo_id()
        # the aging is honest only if the REAL replay still owes the row —
        # re-proven here, not assumed
        snap, note = dispatches.snapshot()
        self.assertFalse(note, note)
        carried = [r for r in dispatches.owed(snap)
                   if r.get("id") == row["id"]]
        self.assertEqual(len(carried), 1,
                         "fixture: replay must still owe the aged row")
        self.assertFalse(carried[0].get("repo_id"))
        findings, unknowns = seats._room_unfinished(self.res)
        self.assertEqual([f for f in findings if "review dispatch" in f], [],
                         "an unprovable row may not be named as this room's")
        named = [u for u in unknowns if u.startswith("review:")]
        self.assertEqual(len(named), 1, unknowns)
        # The contract BROADENED when malformed joined absent: the entry no
        # longer says "no repo identity" (which named only the missing case)
        # but "no READABLE repo identity", which covers absent AND
        # non-canonical. Asserting the OLD substring would pin a narrower
        # promise than production makes — review caught this arm stale on
        # gate:5ea3b7210678a500, red on wording alone with every behavioural
        # assertion around it passing.
        self.assertIn("no READABLE repo identity", named[0])
        self.assertIn("UNKNOWN", named[0])
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep, "an UNKNOWN review may not authorise a release")
        self.assertIn("1 of 4 reads could not be made", text)
        self.assertIn("NO release command is offered", text)

    def test_review_repo_scope_stays_THREE_WAY_distinct_at_this_consumer(self):  # noqa: VACUOUS_ASSERTION — sig["same-repo"] == (1, 0) and sig["legacy"] == (0, 1) are unconditional positive controls on the same observables (the review finding, the review: unknown); the foreign empty discriminates against them
        """SAME-REPO / KNOWN-FOREIGN / LEGACY-UNSCOPED as (findings,
        unknowns) signatures over the review read: (1,0) / (0,0) / (0,1).
        Pairwise distinctness is the property — collapsing foreign into
        legacy manufactures uncertainty about rows helm can positively
        exclude, and collapsing legacy into either neighbour launders an
        unprovable row into proof or into silence. One arm per ledger, so
        no arm's rows shadow another's."""
        other = self.other_repo()
        sig = {}

        def read():
            f, u = seats._room_unfinished(self.res)
            return (len([x for x in f if "review dispatch" in x]),
                    len([x for x in u if x.startswith("review:")]))
        self.ledger = os.path.join(self.tmp, "d-mine.jsonl")
        self.plant(ref=self.head)
        sig["same-repo"] = read()
        self.ledger = os.path.join(self.tmp, "d-foreign.jsonl")
        self.plant(ref=self.rev(other), repo=other)
        sig["foreign"] = read()
        self.ledger = os.path.join(self.tmp, "d-legacy.jsonl")
        self.plant(ref=self.head)
        self.age_ledger_to_pre_repo_id()
        sig["legacy"] = read()
        self.assertEqual(sig["same-repo"], (1, 0))
        self.assertEqual(sig["foreign"], (0, 0))
        self.assertEqual(sig["legacy"], (0, 1))
        self.assertEqual(len(set(sig.values())), 3,
                         "two repo-scope states collapsed at the consumer")

    def test_an_UNREADABLE_room_identity_degrades_same_lane_rows_to_UNKNOWN(self):
        """When the room's own repo identity cannot be read there is no
        basis to classify ANY same-family row — same-repo and foreign are
        indistinguishable — so the whole review read degrades, in ONE
        counted entry (the `_missed` law: entries are per READ, and a
        partially-made read may not inflate the N-of-4 count). The missing
        checkout drives it here: three reads already degrade on the absent
        room, and the review read joins them the moment it holds rows it
        cannot tie down — 4 of 4, not a laundered clean."""
        self.plant(ref=self.head)
        shutil.rmtree(self.wt)
        findings, unknowns = seats._room_unfinished(self.res)
        self.assertEqual([f for f in findings if "review dispatch" in f], [],
                         findings)
        named = [u for u in unknowns if u.startswith("review:")]
        self.assertEqual(len(named), 1, unknowns)
        self.assertIn("repo identity is unreadable", named[0])
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep)
        self.assertIn("4 of 4 reads could not be made", text)

    # --- the scope the four reads cannot leave ----------------------------
    def test_a_lease_in_ANOTHER_repository_NAMES_it_instead_of_denying_it(self):
        """ALL FOUR READS ARE CWD-SCOPED, so a lease held in another
        repository is unreadable from here at EVERY future stop — not once,
        and not transiently. Rendered as "this lease resolves to no lane
        room" it sends the holder looking for a missing directory and repeats
        for the lease's whole TTL with nowhere to record the by-hand answer
        it asks for. The room is ordinary; this process is somewhere else.

        THE CURE READS NOTHING NEW. The reads stay cwd-scoped and the verdict
        stays UNPROVEN with no release command; only the SENTENCE learns to
        name its own scope limit, and the instruction points at the one place
        that can answer.

        THE DISCRIMINATION IS IN THIS ARM, on both sides. This fixture's own
        lease resolves (so the resolver is not answering None to everything),
        and a MISSING room in THIS repository keeps the old sentence
        unchanged (so the cure did not rename an absence)."""
        from helm import seats_delegation
        # CONTROL ONE, UNCONDITIONAL: the fixture's own lease is NOT foreign
        # and DOES resolve to a room.
        self.assertIsNone(seats_delegation.lease_foreign_project(self.res))
        self.assertIsNotNone(seats_delegation._lease_worktree(self.res))
        self.assertIsNone(seats_delegation.lease_foreign_project("not-lane-shaped"))

        foreign = "worktree:otherproj:lane-u"
        self.assertEqual(seats_delegation.lease_foreign_project(foreign),
                         "otherproj")
        self.assertIsNone(seats_delegation._lease_worktree(foreign),
                          "the reads are cwd-scoped; a foreign lease must "
                          "not resolve to a room in THIS repository")
        keep, text = seats._room_advice(foreign)
        self.assertFalse(keep, "an unreadable lease authorised a release")
        self.assertIn("4 of 4 reads could not be made", text)
        self.assertIn("in repository otherproj, not this one", text)
        self.assertIn("from a checkout of otherproj", text)
        # AND IT MAY NOT INSTRUCT A RELEASE IT IS WITHHOLDING. This branch
        # withholds the command because an IDLE room is UNPROVEN, so an
        # instruction to release contradicts the verdict it rides on. The
        # count pins it wider than any one wording: the ONLY occurrence of
        # the word in this sentence is the clause that withholds the command,
        # so an imperative phrased differently reddens this too — though a
        # paraphrase avoiding the word entirely would not, which is the
        # limit of a word-count control.
        self.assertIn("Inspect and confirm the lane", text)
        self.assertNotIn("release it", text)
        self.assertEqual(1, text.count("release"),
                         "the advice names a release it has no standing to "
                         "offer")
        self.assertIn("NO release command is offered", text)
        self.assertNotIn("resolves to no lane room", text)
        self.assertNotIn("Confirm the lane by hand", text)

        # CONTROL TWO: a MISSING room in THIS repository is still an absence,
        # still asks for a by-hand confirmation, and names no repository.
        keep2, text2 = seats._room_advice("worktree:proj:no-such-lane")
        self.assertFalse(keep2)
        self.assertIn("the claimed room is not on disk", text2)
        self.assertIn("Confirm the lane by hand", text2)
        self.assertNotIn("in repository", text2)

    # --- read 4: a suite is running in this room --------------------------
    def test_a_LIVE_gate_in_this_room_is_NAMED(self):  # noqa: VACUOUS_ASSERTION — len(named)==1 plus the pid assertIn on the SAME observable (the GATE IS RUNNING findings) is the unconditional positive control; the before/after empties are the discrimination
        """A LIVENESS PROOF, not a receipt's mtime: `gate.inflight` reports
        an owner file whose (boot, pid, start) still resolves to a running
        process. The owner registered here is this very test process, so
        the pid it prints is verifiable from inside the assertion."""
        from helm import gate as gatemod
        quiet, unknowns = seats._room_unfinished(self.res)
        self.assertEqual(unknowns, [])
        self.assertEqual([f for f in quiet if "GATE IS RUNNING" in f], [])
        nonce = gatemod._inflight_open(self.wt)
        self.assertIsNotNone(nonce, "the fixture could not register an owner")
        try:
            gating, unknowns = seats._room_unfinished(self.res)
        finally:
            gatemod._inflight_close(self.wt, nonce)
        self.assertEqual(unknowns, [])
        named = [f for f in gating if "GATE IS RUNNING" in f]
        self.assertEqual(len(named), 1, gating)
        self.assertIn("pid %d" % os.getpid(), named[0])
        after, _u = seats._room_unfinished(self.res)
        self.assertEqual([f for f in after if "GATE IS RUNNING" in f], [],
                         "a closed owner file must stop reading as a gate")

    def test_an_UNREADABLE_gate_dir_is_COUNTED_not_laundered_into_clean(self):
        """The reproduction, pinned as the regression it was. Before
        the typed census, `gate.inflight()` swallowed a PermissionError on
        the marker dir into None and this read treated that None as a
        successful empty census: findings=[], unknowns=[], and the advice
        printed "no gate running" about a directory helm never saw — the
        function's own EVERY-READ-DEGRADES-TO-UNKNOWN contract, falsified
        by its narrowest read. Read 4 now consumes `gate.inflight_census`,
        so blindness lands in `unknowns` with its reason and the advice
        says COULD NOT MEASURE rather than asserting an absence.

        THE PATCH, NOT chmod 000: chmod does not bind root, so a root-run
        suite would make a chmod arm vacuous. The patch is scoped to the
        room's own marker dir; every other listdir answers honestly."""
        from helm import gate as gatemod
        d = gatemod.inflight_dir(self.wt)
        self.assertTrue(d, "the fixture room must resolve a marker dir")
        real = os.listdir

        def deny(path):
            if str(path) == str(d):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)
        with mock.patch.object(gatemod.os, "listdir", side_effect=deny):
            findings, unknowns = seats._room_unfinished(self.res)
            keep, text = seats._room_advice(self.res)
        self.assertEqual([f for f in findings if "GATE IS RUNNING" in f], [],
                         "an unmeasured gate may not be asserted either way")
        named = [u for u in unknowns if u.startswith("gate:")]
        self.assertEqual(len(named), 1, unknowns)
        self.assertIn("Permission denied", named[0])
        self.assertFalse(keep, "a blind census may not authorise a release")
        self.assertIn("could not be made", text)
        self.assertIn("NO release command is offered", text)
        self.assertNotIn("no gate running", text)
        self.assertNotIn("found nothing bound to this room", text)

    def test_gate_census_states_stay_THREE_WAY_distinct_at_this_consumer(self):  # noqa: VACUOUS_ASSERTION — sig["live"] == (1, 0) and sig["blind"] == (0, 1) are unconditional positive controls on the same observables (the GATE IS RUNNING finding, the gate: unknown); the empties discriminate against them
        """EMPTY / LIVE / UNREADABLE-owner-file, as (findings, unknowns)
        signatures on the SAME room: (0,0) / (1,0) / (0,1). Pairwise
        distinctness is the property — a census that collapsed any two
        would merge two of these signatures, and this arm is the one that
        goes red when it does. The unreadable arm here is the OWNER FILE
        (the dir lists fine); the dir arm has its own test above, so both
        blind shapes named in review are pinned at this consumer."""
        from helm import gate as gatemod
        sig = {}

        def read():
            f, u = seats._room_unfinished(self.res)
            return (len([x for x in f if "GATE IS RUNNING" in x]),
                    len([x for x in u if x.startswith("gate:")]))
        sig["empty"] = read()
        nonce = gatemod._inflight_open(self.wt)
        self.assertIsNotNone(nonce, "the fixture could not register an owner")
        try:
            sig["live"] = read()
            target = os.path.join(gatemod.inflight_dir(self.wt),
                                  nonce + ".json")
            real = open

            def deny(file, *args, **kwargs):
                if str(file) == str(target):
                    raise PermissionError(13, "Permission denied", str(target))
                return real(file, *args, **kwargs)
            with mock.patch("builtins.open", side_effect=deny):
                sig["blind"] = read()
        finally:
            gatemod._inflight_close(self.wt, nonce)
        self.assertEqual(sig["empty"], (0, 0))
        self.assertEqual(sig["live"], (1, 0))
        self.assertEqual(sig["blind"], (0, 1))
        self.assertEqual(len(set(sig.values())), 3,
                         "two gate census states collapsed at the consumer")

    # --- the composed advice ---------------------------------------------
    def test_an_ALL_CLEAN_room_REPORTS_but_offers_NO_RELEASE_COMMAND(self):
        """THE READS ARE DIAGNOSTIC, NEVER PERMISSIVE — the owner's ruling,
        against my first draft, on evidence I did not have.

        I had this branch print "4 reads found no unfinished work" and
        invite the release. Two measurements killed it. 2026-08-05: the
        guard fired on THREE lease-held rooms at once while a live
        Agent-tool subagent was building in ALL THREE, and
        `_delegated_build` fired for NONE of them — two of the three were
        caught by a SINGLE read (one by unlanded commits at uncommitted=0,
        one by uncommitted delta at ahead=0). Then the case that is not an
        extrapolation at all: a room 12 minutes old with a live spawned
        subagent measured uncommitted=0, ahead=0, no bound dispatch, no
        gate — CLEAN ON ALL FOUR.

        THIS FIXTURE IS THAT ROOM, synthetically: a lane at HEAD with no
        commits, no dirt, no dispatch and no gate marker. The advice must
        REPORT and must NOT hand anyone a runnable release."""
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep, "an all-clean room may not authorise a release")
        self.assertIn("4 reads found nothing bound to this room YET", text)
        self.assertIn("NO release command is offered", text)
        self.assertIn("CANNOT SEE a subagent that has not written a file yet",
                      text)
        self.assertIn("Lease retained", text)
        # it may not tell anyone the room is free
        self.assertNotIn("no unfinished work", text)
        self.assertNotIn("do NOT run that", text)

    def drive_four_situations(self, probe):
        """Drive the four situations `_room_advice` distinguishes, calling
        `probe(label, thunk)` for each.

        SHARED so the coverage arm and the distinctness arm cannot drift
        onto different fixtures — two arms that claim to be about the same
        four situations, silently exercising three and five, would make the
        cross-check between them meaningless.

        ORDERED, because the situations are made of real room state and the
        changes are one-way: clean -> commit -> remove. The last is driven
        by making the measurement itself raise, which is the only way to
        reach that branch without stubbing the function under test."""
        probe("all clean", lambda: seats._room_advice(self.res))
        self.commit("lane work", self.wt)
        probe("findings", lambda: seats._room_advice(self.res))
        shutil.rmtree(self.wt)
        probe("unknowns", lambda: seats._room_advice(self.res))
        with mock.patch.object(seats, "_room_unfinished",
                               side_effect=RuntimeError("unreadable")):
            probe("measure raised", lambda: seats._room_advice(self.res))

    def test_FOUR_SITUATIONS_get_FOUR_DISTINCT_ANSWERS(self):
        """THE MERGER CATCH, which the coverage arm structurally cannot do.

        That arm keys branch identity on RETURN LINE. Line identity is
        coarser than semantic identity, so branches MERGING onto one line —
        or collapsing into a dispatch table's single shared `return` —
        shrink BOTH sides of its `len(outcomes) == len(branches)` control at
        once and it stays green while covering less. It catches ADDITION,
        never MERGER.

        The cure is NOT a finer line identity. Bytecode offsets or
        `sys.monitoring` would chase the implementation's layout, which is
        exactly the thing a refactor is allowed to change. The property
        actually worth having is simpler and layout-free: DIFFERENT
        SITUATIONS GET DIFFERENT ANSWERS.

        That is refactor-proof in both directions. Two branches merging onto
        one line while still returning different values loses nothing, and
        this arm stays green — correctly. Two branches merging such that a
        CLEAN room and an UNREADABLE room now give the same answer is a real
        defect, and two fixtures collide, the set shrinks, and this goes red
        — caught by its consequence rather than by its syntax. A dispatch
        table with one shared `return` passes, as it should, so long as the
        answers still differ.

        Keyed on the WHOLE outcome `(keep, sentence)`, not the sentence
        alone: two situations that agree on the words but disagree on
        whether a command may print are still different answers."""
        outcomes = []
        self.drive_four_situations(
            lambda label, call: outcomes.append((label, *call())))
        self.assertEqual(len(outcomes), 4, "the driver stopped short")
        answers = {(keep, text) for _label, keep, text in outcomes}
        self.assertEqual(
            len(answers), len(outcomes),
            "two of the four situations gave the SAME (keep, sentence): %s"
            % sorted((label, keep, text[:60]) for label, keep, text in outcomes))

    def test_EVERY_advice_branch_either_OFFERS_or_EXPLAINS(self):  # noqa: VACUOUS_ASSERTION — len(outcomes)==len(branches) and missed==set() are unconditional and prove the loop runs over every branch, so no assertion in it can pass on an empty set; the rung cannot see that through the loop
        """WHAT MAKES THE HEADER'S PROSE SAFE RATHER THAN LUCKY.

        The header tells the reader that a lane helm could measure names
        its exact command and a lane it could not prove idle says why and
        offers none. That sentence is not a claim about any one line — it
        is an EXHAUSTIVENESS claim about `_room_advice`: EVERY branch
        either offers a command or states why none is offered. It happened
        to be true because the four branches partition that way, and a
        fifth branch that did neither would falsify the header without
        tripping any other arm here.

        DRIVEN FROM THE BRANCH SET ITSELF, not from a hand-written list of
        four fixtures — a hand-listed four is correct today and silent the
        day someone adds a fifth, which is the same defect as a hardcoded
        module pin. The branches are read out of `_room_advice`'s OWN AST
        (top-level `return`s, nested helpers excluded), a line tracer
        records which of them each fixture actually reaches, and the arm
        fails unless EVERY one was reached AND every reached one satisfies
        the disjunction. Add a branch no fixture covers -> red. Add a
        branch that neither offers nor explains -> red. Either way the
        failure lands at the moment of introduction, which is when it is
        still cheap to fix.

        THE DISJUNCTION, stated where it is actually decidable: this
        function returns (may_print_the_command, sentence) and the CALLER
        attaches the runnable command, so the property is
            keep is True  XOR  the sentence says no command is offered.

        NO BRANCH CURRENTLY EARNS A TRUE, because none of the four reads
        sees PRESENCE and every one of them is being asked about it. That
        makes one arm of the XOR unexercised HERE, which is exactly why the
        arm is kept rather than rewritten into "every branch withholds": the
        day a branch acquires a real liveness fact and returns True, this is
        the line that demands it also stops saying no command is offered.
        The other arm of the pair — that the command CHANNEL still works —
        lives in `test_the_command_channel_still_prints_for_a_NON_room_lease`,
        where a lease that strands nothing keeps its exact command."""
        import ast as _ast
        import inspect
        import textwrap

        fn = _ast.parse(textwrap.dedent(
            inspect.getsource(seats._room_advice))).body[0]
        base = seats._room_advice.__code__.co_firstlineno

        def top_returns(node):
            """Returns belonging to THIS function — a nested helper's return
            lives in a different code object the tracer never sees, so
            counting it would make the arm unsatisfiable rather than
            strict."""
            for child in _ast.iter_child_nodes(node):
                if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                                      _ast.Lambda)):
                    continue
                if isinstance(child, _ast.Return):
                    yield child
                yield from top_returns(child)

        branches = {base + r.lineno - 1 for r in top_returns(fn)}
        self.assertGreaterEqual(len(branches), 4, "AST read found no branches")

        code = seats._room_advice.__code__
        reached, outcomes = set(), []

        def localtrace(frame, event, _arg):
            if event == "line":
                reached.add(frame.f_lineno)
            return localtrace

        def globaltrace(frame, _event, _arg):
            return localtrace if frame.f_code is code else None

        def probe(label, fn_call):
            sys.settrace(globaltrace)
            try:
                keep, text = fn_call()
            finally:
                sys.settrace(None)
            outcomes.append((label, keep, text))

        self.drive_four_situations(probe)

        # THE COUNT CONTROL, unconditional and load-bearing twice over: it
        # ties the FIXTURE set to the BRANCH set, so adding a fifth branch
        # without adding a fifth fixture is red on this line alone — before
        # coverage is even consulted — and it guarantees the loop below runs
        # over a non-empty set rather than passing on zero outcomes.
        self.assertEqual(len(outcomes), len(branches),
                         "%d branches in _room_advice but %d fixtures here — "
                         "every branch needs one that reaches it"
                         % (len(branches), len(outcomes)))
        missed = branches - reached
        self.assertEqual(missed, set(),
                         "advice branch(es) at line(s) %s are not covered by "
                         "any fixture here — a branch nobody exercises is a "
                         "branch nobody has checked offers-or-explains"
                         % sorted(missed))

        for label, keep, text in outcomes:
            self.assertTrue(text.strip(), "%s: advice may never be empty" % label)
            says_none = "NO release command is offered" in text
            self.assertNotEqual(
                bool(keep), says_none,
                "%s: branch must EITHER keep its command OR say none is "
                "offered, never both and never neither — got keep=%r, "
                "text=%r" % (label, keep, text))
        # DISTINCTNESS LIVES IN ITS OWN ARM, deliberately. Asserting it here
        # too would make this arm red on a MERGER as well, and then it could
        # not demonstrate what it structurally cannot see — the whole point
        # of the pair is that a merger leaves THIS arm green and reddens the
        # other one.

    def test_a_MIXED_stop_lets_every_lane_speak_for_itself(self):
        """THE HETEROGENEOUS SERMON — the normal case for an orchestrating
        seat, and the one with no coverage until now. Eight lane leases were
        held in one session while this was written, across at least three
        room states; a stop of that session prints a mixed block.

        The defect CLASS this pins is not "the header over-promises" or
        "the header under-promises" — it is THE HEADER ASSERTING ANYTHING
        ABOUT CONTENT IT DOES NOT OWN. That has now appeared twice one
        level apart (unconditional, then would-be per-stop), so the fix is
        structural: the header says why the stop is held and sends the
        reader down; each LINE owns its own lane. Nothing can be inherited,
        so a mixed sermon cannot lie in either direction.

        TWO ROOMS, ONE STOP: lane-u carries an UNLANDED COMMIT (measured →
        its exact command), lane-v is untouched (all clean → no command,
        and the caveat instead)."""
        second = self.root + "-wt/lane-v"
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-v", second], check=True,
                       capture_output=True, timeout=30)
        self.commit("lane work", self.wt)          # lane-u: MEASURED
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        seats.claim("worktree:proj:lane-v", "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        sermon = next(b for b in blocks if "act per line, then stop:" in b)
        head, *rows = sermon.split("\n")
        by_lane = {}
        for row in rows:
            for lane in ("lane-u", "lane-v"):
                if row.strip().startswith("worktree:proj:" + lane):
                    by_lane[lane] = row
        self.assertEqual(sorted(by_lane), ["lane-u", "lane-v"], rows)

        # THE MEASURED LANE: what is bound, NAMED — and no command, because
        # naming work is the strongest reason to withhold the string that
        # opens the lane on it.
        self.assertIn("unfinished work is bound to this room",
                      by_lane["lane-u"])
        self.assertNotIn("helm work release lane-u", by_lane["lane-u"])
        self.assertNotIn("--lease", by_lane["lane-u"])
        # WHAT is unfinished is the long form's job — one lane line per lane
        # is the block's whole shape, and `helm chat stop-guard --detail`
        # prints the four reads by name.
        self.assertIn("NOT landed by ancestry or patch identity",
                      seats._room_advice(self.res)[1])

        # THE CLEAN LANE: no command at all, and the reason in its place.
        self.assertIn("nothing bound to this room YET", by_lane["lane-v"])
        self.assertIn("a subagent that has not written a file",
                      by_lane["lane-v"])
        self.assertNotIn("helm work release lane-v", by_lane["lane-v"])
        self.assertNotIn("--lease", by_lane["lane-v"])
        # AND THE TWO LANES STILL READ DIFFERENTLY. Both withhold now, so the
        # sermon's whole value is that a measured room and an unmeasurable one
        # do not say the same thing — a mixed stop that printed one sentence
        # twice would satisfy every assertion above.
        self.assertNotEqual(by_lane["lane-u"].split("—", 1)[-1],
                            by_lane["lane-v"].split("—", 1)[-1],
                            "two room states collapsed onto one sentence")

        # THE HEADER: promises NEITHER, so neither line inherits anything —
        # and it is TWELVE WORDS, because a header nobody finishes reading
        # promises nothing in practice either.
        self.assertIn("act per line", head)
        self.assertLessEqual(len(head.split()) - 2, 12, head)
        self.assertNotIn("run the EXACT command shown", head)
        self.assertNotIn("NO release command is offered for any of them", head)
        self.assertNotIn("--lease", head)

    def test_the_all_clean_stop_prints_NO_runnable_release_for_that_lease(self):
        """The witness the ruling asked for, through the guard the owner
        actually reads: the suppression has to survive composition, not
        just live in the helper's return value. A clean lane lease's sermon
        line must carry the caveat and NO `helm work release`."""
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        text = " ".join(blocks)
        self.assertIn(self.res, text)              # the lease IS reported
        self.assertIn("check the lane by hand", text)
        self.assertNotIn("helm work release", text)
        self.assertNotIn("--lease", text)

    def test_the_advice_never_speaks_the_RESERVED_token_delegated(self):  # noqa: VACUOUS_ASSERTION — each of the three texts has an UNCONDITIONAL assertIn on its own content before the loop is entered, so no branch can pass on an empty string; the rung cannot pair them across the loop
        """A FORGED SIGNAL, caught by the regression sweep rather than by
        me. Two StopGuardDelegationTest cases read the ABSENCE of the token
        "delegated" from a stop's stderr as proof the delegation EXEMPTION
        did not fire. My all-clean sentence said "every delegated build",
        which put that token in front of them from a rung that had not
        fired — the same forged-token defect the sermon's own comment warns
        about for "auto-claimed", one string over.

        Pinned across EVERY branch, not just the one that broke, because
        the next edit will be to a different branch."""
        clean = seats._room_advice(self.res)[1]
        # UNCONDITIONAL POSITIVE CONTROL on the same observable, outside the
        # loop: this is the string the absences below are read from, so if it
        # were ever empty every assertNotIn would pass for free.
        self.assertIn("4 reads found nothing bound to this room YET", clean)
        cases = [("all clean", clean)]
        shutil.rmtree(self.wt)
        missing = seats._room_advice(self.res)[1]
        self.assertIn("3 of 4 reads could not be made", missing)
        cases.append(("missing room", missing))
        nolane = seats._room_advice("worktree:nosuchproj:x")[1]
        self.assertIn("4 of 4 reads could not be made", nolane)
        cases.append(("no lane room", nolane))
        for label, text in cases:
            self.assertNotIn("delegated", text, label)
        # the findings branch too, from a room that still exists
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-w", self.root + "-wt/lane-w"],
                       check=True, capture_output=True, timeout=30)
        self.commit("lane work", self.root + "-wt/lane-w")
        found = seats._room_advice("worktree:proj:lane-w")[1]
        self.assertIn("NOT landed by ancestry or patch identity", found)
        self.assertNotIn("delegated", found)

    def test_RECENT_FILE_MTIMES_ARE_NOT_A_SIGNAL_and_must_never_become_one(self):
        """NO MTIME RUNG. This is a tripwire against the fifth read the
        clean case invites someone to add.

        MEASURED 2026-08-05 on a 12-minute-old room with a live delegate:
        mtime fails in BOTH directions. Read naively it reported 510
        authored files touched in 15 minutes — pure CHECKOUT NOISE, every
        one stamped with the room's creation second, README.md and
        docs/VERBS.md included — which screams activity for a room nobody
        has touched. Read correctly as the newest AUTHORED write it said
        idle for a room that had a live subagent in it. It cannot rescue
        the clean case and an mtime rung would make it worse.

        So: touching every file to NOW, with no content change, must move
        NOTHING. The second half is the unconditional positive control —
        the same room with one byte of real content does report — so this
        is discrimination, not an absence assertion standing alone."""
        now = time.time()
        touched = 0
        for dirpath, _dirs, files in os.walk(self.wt):
            if ".git" in dirpath:
                continue
            for name in files:
                os.utime(os.path.join(dirpath, name), (now, now))
                touched += 1
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep)
        self.assertIn("4 reads found nothing bound to this room YET", text)
        # POSITIVE CONTROL on the same observable: real content DOES move it
        with open(os.path.join(self.wt, "actually_written.py"), "w") as fh:
            fh.write("x = 1\n")
        keep2, text2 = seats._room_advice(self.res)
        self.assertFalse(keep2, "no room read earns a release command")
        # THE DISCRIMINATION IS THE SENTENCE, not the boolean: the two rulings
        # agree (withhold) and the two reports do not, which is the whole
        # point of an mtime rung changing nothing and a written byte changing
        # what is NAMED.
        self.assertIn("UNCOMMITTED changes in the room", text2)
        self.assertNotEqual(text, text2,
                            "a written byte must move the REPORT even though "
                            "it does not move the ruling")

    def test_a_room_with_FINDINGS_gets_NO_release_command_either(self):
        """THE INVERSION, and the reason the suppression is not a blanket
        even though every ROOM branch now withholds.

        THE ARGUMENT FOR LETTING THIS BRANCH PRINT THE COMMAND is that helm
        MEASURED what is in the room and can therefore hand a fully-informed
        holder a decision. Two things refute it. The sentence and the command
        would point opposite ways in one line -- "do NOT run that blind"
        beside the string to run -- and a reader pastes the half that fits on
        a line. And the evidence is backwards: a room with findings has
        PROVEN there
        is work bound to it, which is a stronger reason to withhold than the
        clean room's merely-unproven idleness.

        WHAT KEEPS THIS FROM BEING A BLANKET is asserted in
        `test_the_command_channel_still_prints_for_a_NON_room_lease`: the
        same stop, the same sermon, a lease whose release strands nothing --
        and its exact command still prints. Suppressing everything fails that
        arm, so these absences are discriminations rather than silence."""
        self.commit("lane work", self.wt)
        with open(os.path.join(self.wt, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep, "a room with PROVEN bound work may not offer "
                               "the command that opens the lane on it")
        # the naming is untouched -- withholding the command is not
        # withholding the report, and both findings are still named
        self.assertIn("UNFINISHED WORK BOUND TO IT", text)
        self.assertIn("UNCOMMITTED changes in the room", text)
        self.assertIn("NOT landed by ancestry or patch identity", text)
        self.assertIn("NO release command is offered", text)
        self.assertNotIn("4 reads found nothing bound to this room YET", text)
        # THE ONE-LINE SELF-CONTRADICTION, pinned gone: the sentence no
        # longer tells a reader not to run a thing it is handing them.
        self.assertNotIn("do NOT run that blind", text)
        # AND THE LEASE DOES NOT ROT. Clearing a lane that really IS finished
        # is the case this hint was built for; withholding the composed
        # command must not withhold the TOKEN too, so the sentence names the
        # census that carries it. One read stands between a holder and a
        # destructive act -- that is the cost, and it is the whole cost.
        self.assertIn("If you KNOW it is finished", text)
        self.assertIn("carries this lane\'s lease", text)
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        joined = " ".join(blocks)
        # POSITIVE POLE, unconditional: the guard DID reach the claims rung
        # and DID speak about this lease, so the absence below is a measured
        # absence rather than a rung that never ran.
        self.assertIn(self.res, joined)
        self.assertIn("unfinished work is bound to this room", joined)
        self.assertNotIn("helm work release lane-u", joined)
        self.assertNotIn("--lease", joined)

    def test_the_command_channel_still_prints_for_a_NON_room_lease(self):
        """THE POSITIVE CONTROL FOR EVERY WITHHOLDING ARM IN THIS FILE, and
        the reason they are measurements rather than a mute guard.

        A `landlock:` lease names no room, strands no delegate and is
        discharged by dropping it -- so the guard has standing there and must
        still print its exact command. One stop drives BOTH: the lane lease
        (findings, command withheld) and the non-room lease (command kept).
        A cure that silenced the command globally passes every absence
        assertion in this file and fails this one line."""
        self.commit("lane work", self.wt)
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        seats.claim("landlock:proj", "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        joined = " ".join(blocks)
        # BOTH DIRECTIONS, ONE CALL.
        self.assertIn("helm chat release landlock:proj", joined,
                      "the command channel went mute: %s" % joined)
        self.assertIn("unfinished work is bound to this room", joined)
        self.assertNotIn("helm work release lane-u", joined)

    def test_a_MISSING_room_reports_three_missed_reads_and_withholds(self):
        """UNKNOWN-AND-SAY-SO, counted honestly. The count is derived from
        one entry PER READ, so the missing checkout cannot report itself as
        a single blind spot when it blinds three — and a room helm could
        not read has no more standing to authorise than a clean one."""
        shutil.rmtree(self.wt)
        keep, text = seats._room_advice(self.res)
        self.assertFalse(keep, "an unmeasured room may not authorise")
        self.assertIn("3 of 4 reads could not be made", text)
        for read in ("working tree", "landedness", "gate"):
            self.assertIn("%s: the claimed room is not on disk" % read, text)
        self.assertIn("an IDLE room is UNPROVEN", text)
        self.assertIn("NO release command is offered", text)
        self.assertNotIn("found no unfinished work", text)

    def test_an_unreadable_ledger_degrades_the_review_read_to_UNKNOWN(self):
        """The ledger read is the one that can fail without the checkout
        failing, and its failure must never collapse to 'no reviews'."""
        os.unlink(self.ledger) if os.path.exists(self.ledger) else None
        os.makedirs(self.ledger)         # a directory: unreadable as a ledger
        snap, note = dispatches.snapshot()
        self.assertTrue(note, "fixture: the ledger must read as unavailable")
        findings, unknowns = seats._room_unfinished(self.res, snap, note)
        self.assertEqual(findings, [])
        self.assertEqual(len(unknowns), 1, unknowns)
        self.assertTrue(unknowns[0].startswith(
            "review: the dispatch ledger could not be read"), unknowns)
        keep, text = seats._room_advice(self.res, snap, note)
        self.assertFalse(keep)
        self.assertIn("1 of 4 reads could not be made", text)

    def test_the_brief_line_carries_the_producers_reason(self):  # noqa: VACUOUS_ASSERTION — the positive controls on the same observable are unconditional and run FIRST: the lease's own resource and the guard's constant must BOTH appear in the block before the reason assertion is read, so a guard that never reached the rung reddens this arm before any reason check
        """THE SEAM train16 PROVED RED, pinned shut here.

        The terse per-lane line compressed the unknowns to a COUNT, and the
        producer's reason — including the expired-clock lane's attribution,
        which lives in the reason and nowhere else — never reached the owner.
        The contract is the CONJUNCTION: the line names the lease AND quotes
        the producer's own sentence for the read that failed, bounded, never
        rephrased. Pinned against the producer's CONSTANT so a rewording
        moves both sides together instead of silently unpinning the arm."""
        from helm import stopfacts_resident
        seats.claim(self.res, "alice", session=self.sid)

        def raised():
            raise RuntimeError("probe")

        import unittest.mock as _m
        # THE PRODUCER'S OWN DOOR: the stop facts are the RESIDENT'S fold, so
        # its reason is what a failed fold leaves in the facts the guard
        # reads — patched where the resident calls it, never one module over.
        with _m.patch.object(dispatches, "snapshot", side_effect=raised):
            blocks, warns = seats.stop_guard(session=self.sid, room="main",
                                             seat="alice", cwd=self.root)
        text = " ".join(blocks + warns)
        # POSITIVE POLES, both unconditional: the guard DID reach the claims
        # rung and DID print this lease's line, so the reason's presence
        # below is a measured property and not a rung that never ran.
        self.assertIn(self.res, text)
        self.assertIn(stopfacts_resident.LEDGER_RAISED, text,
                      "the brief line must carry the producer's reason "
                      "verbatim: %s" % text)

    # --- THE CLOCK IS AN INPUT, AND A SPENT CLOCK IS NOT AN UNREADABLE ROOM
    #
    # Every read above runs under the Stop guard's cooperative budget, and the
    # primitives they reach spend against it BEFORE they do any work
    # (`vcs.Backend.run` opens with `projscope.spend_or_raise("git memo
    # lookup")`). A bare `except Exception` caught that expiry and filed it
    # under the reason ITS author had in mind, so the guard told five seats at
    # once that rooms with resolvable HEADs and clean trees could not be read.
    # Each arm below carries the control that makes it a measurement: the SAME
    # room, in the SAME process, with the budget intact.

    def _spent_budget(self):
        """The state a stop enters whenever the dispatch-ledger rung has just
        spent its whole reserve: the guard's own probe log shows that rung
        ending at its 7.5s wall on every ladder of a busy run, so these reads
        begin with the clock already gone."""
        from helm import projscope
        return projscope.scope(deadline=time.monotonic() - 1.0)

    def test_a_spent_budget_is_named_as_the_BUDGET_never_as_the_ROOM(self):  # noqa: VACUOUS_ASSERTION — the empty `findings` has its unconditional positive pole on the SAME observable at the foot of the arm: the same room is made dirty and read again with time on the clock, and an UNCOMMITTED finding must appear, so a reader that never speaks reddens this arm; the unknowns half is asserted POSITIVELY (four entries, one per named read, each carrying the budget sentence)
        """The defect, and its control one line above it.

        The control is the whole arm: an assertion about what a spent budget
        prints proves nothing unless the same room, read with time on the
        clock, is demonstrably readable and clean."""
        control = seats._room_unfinished(self.res, {}, None)
        self.assertEqual(control, ([], []),
                         "control: this room is CLEAN and READABLE, so any "
                         "unknown below is about the clock and nothing else")
        with self._spent_budget():
            findings, unknowns = seats._room_unfinished(self.res, {}, None)
        self.assertEqual(findings, [],
                         "nothing was measured, so nothing may be claimed")
        # `_missed`'s law: one entry PER READ, and the denominator never
        # shrinks to whatever happened to be attempted.
        self.assertEqual(len(unknowns), len(seats._ROOM_READS), unknowns)
        self.assertEqual(sorted(u.split(":")[0] for u in unknowns),
                         sorted(seats._ROOM_READS), unknowns)
        for entry in unknowns:
            self.assertIn("the stop guard's budget expired", entry)
        # THE POLES THE OLD SENTENCES OCCUPIED. A room nothing opened may not
        # be described at all — these two strings were the live falsehood.
        joined = " ".join(unknowns)
        self.assertNotIn("the room status read failed", joined)
        self.assertNotIn("HEAD could not be resolved", joined)
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLE, unconditional: this
        # reader DOES produce findings, so the empty list above is a measured
        # silence and not a reader that never speaks.
        with open(os.path.join(self.wt, "positive_control.py"), "w") as fh:
            fh.write("x = 1\n")
        loud, _u = seats._room_unfinished(self.res, {}, None)
        self.assertTrue(any("UNCOMMITTED changes" in f for f in loud), loud)

    def test_the_budget_expiry_widens_what_is_EXPLAINED_never_what_is_ALLOWED(self):
        """Attribution is the only thing that moves. An unmeasured room is
        still UNPROVEN, still carries no release command, and still sends the
        holder to confirm by hand — a guard that could not read must never
        hand out the one command that opens a lane."""
        with self._spent_budget():
            keep, text = seats._room_advice(self.res, {}, None)
        self.assertFalse(keep, "an unmeasured room may not authorise")
        self.assertNotIn("helm chat release", text)
        self.assertIn("an IDLE room is UNPROVEN", text)
        self.assertIn("4 of 4 reads could not be made", text)
        self.assertIn("the stop guard's budget expired", text)
        # and the same room, with time on the clock, is measured and silent
        control_keep, control_text = seats._room_advice(self.res, {}, None)
        self.assertFalse(control_keep)
        self.assertIn("found nothing bound to this room YET", control_text)

    def test_a_read_ALREADY_MADE_survives_the_expiry_that_kills_its_successors(self):
        """The bookkeeping arm. An expiry reports the reads it did not reach,
        never the reads that already answered — so a room proven DIRTY before
        the clock died is still reported dirty, and exactly three unknowns
        follow it. A handler that blanketed all four would erase a measured
        finding, which is the same information loss one direction over."""
        from helm import projscope, vcs
        with open(os.path.join(self.wt, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        real_backend = vcs.backend

        class _ClockDiesAtHead:
            """The REAL backend, with the raise `spend_or_raise` makes at the
            next git read — injected at the seam that raises it, never
            reconstructed as a different exception."""

            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def head_sha(self, *a, **k):
                raise projscope.Expired("budget spent before git memo lookup")

        with mock.patch.object(vcs, "backend",
                               side_effect=lambda *a, **k:
                               _ClockDiesAtHead(real_backend(*a, **k))):
            findings, unknowns = seats._room_unfinished(self.res, {}, None)
        self.assertTrue(any("UNCOMMITTED changes" in f for f in findings),
                        "the read that ANSWERED must survive: %r" % findings)
        self.assertEqual(sorted(u.split(":")[0] for u in unknowns),
                         ["gate", "landedness", "review"], unknowns)
        for entry in unknowns:
            self.assertIn("the stop guard's budget expired", entry)

    def test_a_PARTIAL_read_with_FINDINGS_reaches_the_UNKNOWN_exit(self):  # noqa: VACUOUS_ASSERTION — every absence here is paired with an unconditional positive control on the SAME observable in the same call: `assertNotIn("UNFINISHED WORK BOUND TO IT", total)` against `assertIn("UNFINISHED WORK BOUND TO IT", whole)`, and `assertNotIn("reads could not be made", whole)` against `assertIn("3 of 4 reads could not be made", partial)`
        """THE COMPOSITION WITH NO ARM, and the one the lane was opened
        on: a room that answered on SOME axes and went dark on the rest.

        Routed to the findings exit it takes the confident rendering, so
        three-of-four reads failing reads MORE assertively than four-of-four
        on strictly weaker evidence. Both exits withhold, so what this arm
        also pins is the OTHER half: a partial failure must not
        share a sentence with a total one, and it must still name WHICH read
        went dark, because "1 of 4 failed, the gate read" and "4 of 4 failed"
        are different situations a reader acts on differently.

        THREE SITUATIONS, THREE SENTENCES, read in one call so none of them
        can be an absence standing alone."""
        from helm import projscope, vcs
        with open(os.path.join(self.wt, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        real_backend = vcs.backend

        class _ClockDiesAtHead:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def head_sha(self, *a, **k):
                raise projscope.Expired("budget spent before git memo lookup")

        # SITUATION ONE: findings measured, three reads lost to the budget.
        with mock.patch.object(vcs, "backend",
                               side_effect=lambda *a, **k:
                               _ClockDiesAtHead(real_backend(*a, **k))):
            partial_keep, partial = seats._room_advice(self.res, {}, None)
            partial_brief_keep, partial_brief = seats._room_advice(
                self.res, {}, None, brief=True)
        # SITUATION TWO: every read lost, nothing measured.
        with self._spent_budget():
            total_keep, total = seats._room_advice(self.res, {}, None)
            _tbk, total_brief = seats._room_advice(self.res, {}, None,
                                                   brief=True)
        # SITUATION THREE: the same findings with all four reads made.
        whole_keep, whole = seats._room_advice(self.res, {}, None)

        # THE RULING IS THE SAME EVERYWHERE, and that is the cure: no room
        # read has standing to offer the command, least of all the one that
        # proved there is work to strand.
        for label, keep in (("partial", partial_keep),
                            ("partial brief", partial_brief_keep),
                            ("total", total_keep), ("whole", whole_keep)):
            self.assertFalse(keep, "%s offered a release command" % label)
        for label, text in (("partial", partial), ("total", total),
                            ("whole", whole)):
            self.assertIn("NO release command is offered", text, label)

        # THE PARTIAL LINE IS STILL USEFUL: it names the work AND the reads.
        self.assertIn("UNCOMMITTED changes in the room", partial)
        self.assertIn("3 of 4 reads could not be made", partial)
        for read in ("landedness", "review", "gate"):
            self.assertIn("%s: %s" % (read, seats_room_advice._BUDGET_SPENT),
                          partial)
        self.assertIn("WHAT ELSE is bound to it is UNKNOWN", partial)
        self.assertIn("Confirm the lane by hand.", partial)
        # ... and the brief spelling carries both halves too, because that is
        # the one the owner reads at a blocked Stop.
        self.assertIn("unfinished work is bound to this room and",
                      partial_brief)
        self.assertIn("3 of 4 reads failed", partial_brief)
        self.assertIn("check the lane by hand", partial_brief)

        # THE TOTAL LINE IS A DIFFERENT SENTENCE. A partial and a total
        # failure sharing a value is the same collapse one level up from the
        # one this lane cures, so it is pinned on BOTH spellings.
        self.assertIn("4 of 4 reads could not be made", total)
        self.assertNotIn("UNFINISHED WORK BOUND TO IT", total)
        self.assertIn("an IDLE room is UNPROVEN", total)
        self.assertNotEqual(partial, total)
        self.assertNotEqual(partial_brief, total_brief)
        # ... and neither is the all-four-made sentence, which names the work
        # without any count at all.
        self.assertIn("UNFINISHED WORK BOUND TO IT", whole)
        self.assertNotIn("reads could not be made", whole)
        self.assertEqual(len({partial, total, whole}), 3,
                         "two of the three read states gave one sentence")

    def test_what_the_WITHHELD_command_would_actually_have_done(self):  # noqa: VACUOUS_ASSERTION — every drive asserts a POSITIVE rc and a POSITIVE line from the real door, and the two room-survives checks are discriminated in the same call by a third drive where the same door DOES retire a room
        """THE SEVERITY, MEASURED AT THE DOOR rather than assumed from the
        surface. The printed line was reported as one that "destroys
        uncommitted work"; `work._claims.release_lane` refuses that outright.
        What a pasted release really costs is the LEASE, and — in exactly one
        shape — the room.

        THE SHAPE WHERE THE ROOM GOES IS THE ONE WITH NO FINDINGS: clean tree,
        tip proven landed. That is the all-clean branch, which has withheld
        the command since the owner's first ruling. So the surface and the
        door pointed opposite ways in BOTH directions at once — the command
        printed where the door refuses or preserves, and was withheld where
        the door deletes.

        EVERY LANE HERE IS BUILT WITH `lane_branch`'s OWN SPELLING. A branch
        the door cannot read takes its unreadable-branch exit and keeps the
        room for a reason that has nothing to do with landedness, which would
        make two of these three drives pass without measuring anything."""
        from helm.work import _claims

        def lane(name):
            path = self.root + "-wt/" + name
            subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                            "-b", "lane/" + name, path], check=True,
                           capture_output=True, timeout=30)
            return path, "worktree:proj:" + name

        # ONE: a DIRTY room. The door refuses before it touches the lease.
        dirty_path, _res = lane("door-dirty")
        with open(os.path.join(dirty_path, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        rc, lines = _claims.release_lane(self.root, "door-dirty", "alice")
        self.assertEqual(rc, 1, lines)
        self.assertIn("is DIRTY", " ".join(lines))
        self.assertTrue(
            os.path.exists(os.path.join(dirty_path, "half_built.py")),
            "the door discarded uncommitted bytes")

        # TWO: UNLANDED commits, branch readable. The lease goes; the room and
        # the branch stay, and the door says WHY.
        un_path, un_res = lane("door-unlanded")
        self.commit("lane work", un_path)
        ok, msg, lease = seats.claim(un_res, "alice", ttl=600,
                                     session=self.sid)
        self.assertTrue(ok, msg)
        rc, lines = _claims.release_lane(self.root, "door-unlanded", "alice",
                                         lease=lease, session=self.sid)
        self.assertEqual(rc, 0, lines)
        joined = " ".join(lines)
        self.assertIn("not proven landed", joined)
        self.assertNotIn("unreadable/missing", joined,
                         "the branch spelling made this drive vacuous")
        self.assertTrue(os.path.exists(un_path),
                        "an unlanded lane lost its room: %s" % joined)

        # THREE: PROVEN LANDED and clean — the door retires the room. The
        # discrimination that makes the two survivals above measurements.
        landed_path, landed_res = lane("door-landed")
        ok, msg, lease2 = seats.claim(landed_res, "alice", ttl=600,
                                      session=self.sid)
        self.assertTrue(ok, msg)
        # and the guard WITHHOLDS here, which is the inversion in one line
        keep, advice = seats._room_advice(landed_res)
        self.assertFalse(keep)
        self.assertIn("nothing bound to this room YET", advice)
        rc, lines = _claims.release_lane(self.root, "door-landed", "alice",
                                         lease=lease2, session=self.sid)
        self.assertEqual(rc, 0, lines)
        self.assertIn("removed", " ".join(lines))
        self.assertFalse(os.path.exists(landed_path),
                         "the landed room was not retired: %s" % lines)

    def test_no_read_is_named_TWICE_when_an_expiry_follows_an_ordinary_failure(self):
        """The denominator law, at the one composition that can break it.

        A read that failed ORDINARILY has already been named, and the expiry
        handler reports only what it did not reach — so a read struck by one
        path and reported by the other would inflate "N of 4" past the reads
        that exist. That is `_missed`'s own stated hazard, in the direction of
        ALARM rather than reassurance, and it is invisible to every arm where
        the clock dies first. Found by mutation: dropping one strike from the
        HEAD-failure path left every other arm green.
        """
        from helm import projscope, seats_room_advice, vcs
        real_backend = vcs.backend

        class _HeadIsSimplyAbsent:
            """HEAD returns nothing WITHOUT raising — the ordinary failure the
            expiry handler must not re-report."""

            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def head_sha(self, *a, **k):
                return None

        def _ledger_expires():
            raise projscope.Expired("budget spent before dispatch ledger read")

        with mock.patch.object(vcs, "backend",
                               side_effect=lambda *a, **k:
                               _HeadIsSimplyAbsent(real_backend(*a, **k))), \
                mock.patch.object(dispatches, "snapshot",
                                  side_effect=_ledger_expires):
            _findings, unknowns = seats._room_unfinished(self.res)
        named = [u.split(":")[0] for u in unknowns]
        self.assertEqual(sorted(named), sorted(set(named)),
                         "a read may be named ONCE: %r" % (unknowns,))
        self.assertLessEqual(len(unknowns), len(seats._ROOM_READS), unknowns)
        # POSITIVE POLE, unconditional and on the SAME observable: both halves
        # are present, each carrying ITS OWN reason, so this is not an arm a
        # reader that says nothing could pass.
        self.assertIn("landedness: the room's HEAD could not be resolved",
                      unknowns)
        self.assertIn("review: " + seats_room_advice._BUDGET_SPENT, unknowns)

    def test_the_claims_rung_opens_NO_ledger_fold_whatever_the_facts_say(self):  # noqa: VACUOUS_ASSERTION — the zero-fold rows have their unconditional positive pole on the SAME observable in the FIRST row of the same loop: the resident's own compute must count at least ONE fold through the same spy, so a broken spy or an unreached rung reddens this arm before either zero is read
        """THE CLAIMS RUNG READS THE LEDGER NOWHERE: not one shared fold
        per stop, and not a second whole fold per held lane lease on the
        unreadable-ledger shape, because the gate exemption and the room
        advice are the resident's facts (helm/stopfacts_resident.py).

        The ruling does not move with the fold count: facts that are ABSENT
        earn no exemption and the claims block stands, and facts computed over
        an unreadable ledger earn none either."""
        from helm import stopfacts, stopfacts_resident
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        folds = []
        real_snapshot, real_rows = dispatches.snapshot, dispatches.rows

        def _counting_snapshot(*a, **k):
            folds.append("snapshot")
            return real_snapshot(*a, **k)

        def _counting_rows(*a, **k):
            folds.append("rows")
            return real_rows(*a, **k)

        # THE FIRST ROW IS THE POSITIVE CONTROL: the resident's compute folds
        # through the same spy, so a spy that counts nothing reddens here.
        with mock.patch.object(dispatches, "snapshot",
                               side_effect=_counting_snapshot), \
                mock.patch.object(dispatches, "rows",
                                  side_effect=_counting_rows):
            stopfacts_resident.write(stopfacts_resident.compute())
        self.assertGreaterEqual(len(folds), 1, "the spy counted nothing")
        self.fresh_resident.off()
        for label, prepare in (
                ("facts the resident wrote", lambda: None),
                ("NO facts at all",
                 lambda: os.unlink(stopfacts.path()))):
            prepare()
            folds.clear()
            # EACH ARM IS A FIRST STOP: an unchanged held set would compress
            # to one line that names no lease, which is the latch doing its
            # job and not this arm's question.
            for name in os.listdir(chat.chat_dir()):
                if seats.LEASE_LATCH in name:
                    os.unlink(os.path.join(chat.chat_dir(), name))
            blocks, warns = [], []
            with mock.patch.object(dispatches, "snapshot",
                                   side_effect=_counting_snapshot), \
                    mock.patch.object(dispatches, "rows",
                                      side_effect=_counting_rows):
                seats_stop_claims.claims_rung(
                    self.sid, "a-room", "alice", blocks=blocks, warns=warns,
                    facts=stopfacts.Lazy())
            text = " ".join(blocks + warns)
            self.assertIn(self.res, text, label)
            self.assertEqual(folds, [], "%s: the claims rung folded the "
                             "ledger: %r" % (label, folds))
            self.assertNotIn("lease retained", text,
                             "%s: an unexempted lease is not exempted" % label)

    def test_THE_GUARD_ITSELF_carries_the_producers_failure_reason(self):  # noqa: VACUOUS_ASSERTION — the zero-fold assertion has two unconditional positive poles on the same stop it measures: the lease resource and the producer's reason must BOTH appear in the guard's own output, so a rung that never ran, a lease never seen, or a guard that died early reddens this arm before the zero is read; the fold counter itself is proven live by the sibling arm, whose first row must count at least ONE
        """The producer half, bound end to end, for both ways the facts can
        fail to carry a reading of this lane.

        THE RESIDENT'S FOLD RAISED: the lease facts carry its reason
        (`stopfacts_resident.LEDGER_RAISED`) into the room advice, and the
        guard prints it. NO RESIDENT AT ALL: there are no facts, the lease is
        held, and the guard says the stop facts are absent. In both, the
        guard itself opens no fold of its own — the second-fold shape
        `_gate_pending` once took is unreachable from the stop path."""
        from helm import stopfacts, stopfacts_resident
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        folds = []
        real_rows = dispatches.rows

        def _counting_rows(*a, **k):
            folds.append(1)
            return real_rows(*a, **k)

        def _ledger_is_gone(*_a, **_k):
            raise RuntimeError("the ledger read blew up")

        def _no_resident():
            self.fresh_resident.off()
            try:
                os.unlink(stopfacts.path())
            except FileNotFoundError:
                pass
            return contextlib.nullcontext()

        arms = (
            ("the resident's fold RAISED",
             lambda: mock.patch.object(dispatches, "snapshot",
                                       side_effect=_ledger_is_gone),
             stopfacts_resident.LEDGER_RAISED),
            ("NO resident", _no_resident, "stop-facts ABSENT"),
        )
        for label, failure, reason in arms:
            folds.clear()
            # THE SECOND ARM IS A SECOND STOP ON AN UNCHANGED HELD SET, which
            # the lease latch legitimately compresses to one line — so the
            # latch is cleared between arms and each stop is a first stop.
            for name in os.listdir(chat.chat_dir()):
                if seats.LEASE_LATCH in name:
                    os.unlink(os.path.join(chat.chat_dir(), name))
            with failure(), mock.patch.object(dispatches, "rows",
                                              side_effect=_counting_rows):
                blocks, warns = seats.stop_guard(session=self.sid,
                                                 seat="alice")
            text = " ".join(blocks + warns)
            self.assertIn(self.res, text, "%s: %s" % (label, text))
            self.assertIn(reason, text,
                          "%s: the guard's block must carry the PRODUCER's "
                          "reason, however the line is worded: %s"
                          % (label, text))
            self.assertEqual(folds, [], "%s: the guard opened a fold of its "
                             "own" % label)
    def test_the_release_advice_NAMES_THE_WORK_and_no_longer_asks_about_delegation(self):
        """The deliverable. Two real facts in the room, both named in the
        block that carries the release command — and the retired question
        gone from it."""
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        self.commit("lane work", self.wt)
        with open(os.path.join(self.wt, "half_built.py"), "w") as fh:
            fh.write("x = 1\n")
        blocks, _warns = seats.stop_guard(session=self.sid, seat="alice")
        text = " ".join(blocks)
        # THE NAMING SURVIVED THE WITHHOLDING, which is this arm's whole
        # deliverable: the advice REPLACES the command instead of trailing it,
        # and what it says about the room is unchanged.
        self.assertIn("unfinished work is bound to this room", text)
        self.assertIn("NOT blind", text)
        self.assertNotIn("helm work release lane-u --lease", text)
        # WHAT is unfinished is the long form's job: the same drive through
        # `_room_advice` below names the uncommitted bytes and the unlanded
        # tip, and `helm chat stop-guard --detail` prints it at the block.
        keep, detail = seats._room_advice(self.res)
        self.assertFalse(keep)
        self.assertIn("UNCOMMITTED changes in the room", detail)
        self.assertIn("NOT landed by ancestry or patch identity", detail)
        self.assertIn("releasing the lane opens it on that work", detail)
        # THE RETIRED QUESTION, pinned gone
        self.assertNotIn("delegation is UNKNOWN", text)
        self.assertNotIn("SUBAGENT is building in this lane", text)

    def test_the_claims_block_reads_the_190ms_ledger_ONCE_for_N_lane_leases(self):  # noqa: VACUOUS_ASSERTION — len(folds)==1 counts the SAME instrumented read the rows_reads==[] assertion is about, and the advice assertIn proves the rungs ran at all
        """COST, asserted rather than hoped for.

        The ledger fold measured 190ms on a 1,577-event file and the claims
        block asks it TWICE PER LANE LEASE — once for the gate-pending
        exemption, once for the review read. Unthreaded that is 2N reads on
        every stop; threaded it is one, whatever N is. TWO rooms here, so a
        per-lease read cannot hide behind a count of one.

        `dispatches.rows` is asserted UNCALLED because that is the exact call
        the exemption used to make: it is the mutation site, and a revert of
        the threading turns this red rather than merely slower."""
        second = self.root + "-wt/lane-v"
        subprocess.run(["git", "-C", self.root, "worktree", "add", "-q",
                        "-b", "lane-v", second], check=True,
                       capture_output=True, timeout=30)
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        seats.claim("worktree:proj:lane-v", "alice", ttl=600, session=self.sid)
        self.commit("lane work", self.wt)
        folds, rows_reads = [], []
        real_snapshot, real_rows = dispatches.snapshot, dispatches.rows

        def counted_snapshot():
            folds.append(1)
            return real_snapshot()

        def counted_rows():
            rows_reads.append(1)
            return real_rows()

        with mock.patch.object(dispatches, "snapshot", counted_snapshot), \
                mock.patch.object(dispatches, "rows", counted_rows):
            blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        self.assertIn("unfinished work is bound to this room", " ".join(blocks))
        self.assertEqual(rows_reads, [],
                         "the gate-pending exemption re-read the ledger")
        # Claims, beacon, review-spiral and stop-whisper share this ONE stop
        # snapshot. The number is pinned so a NEW fold cannot appear unremarked.
        self.assertEqual(len(folds), 1,
                         "the ledger was folded %d times in one stop"
                         % len(folds))

    def test_NO_stop_makes_the_four_reads_the_resident_made_them(self):  # noqa: VACUOUS_ASSERTION — the resident's own refresh through the same counter must count exactly ONE read first, and the first stop must print that read's finding, so a counter that counts nothing or a stop that never read the facts reddens this arm before either zero is read
        """The cost story, finished. The four reads once ran on the stop that
        printed them (and the latch kept them off the compressed re-stop);
        now they run in the `helm web` resident when an input moves, and NO
        stop runs them — the first prints the resident's finding and the
        re-stop compresses, both without one read of their own."""
        from helm import stopfacts_resident
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        self.commit("lane work", self.wt)
        calls, real = [], seats._room_unfinished

        def counted(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        with mock.patch.object(seats, "_room_unfinished", counted):
            stopfacts_resident.write(stopfacts_resident.compute())
            self.assertEqual(len(calls), 1, "the resident must measure")
            self.fresh_resident.off()
            calls.clear()
            blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
            self.assertIn("unfinished work is bound to this room",
                          " ".join(blocks))
            _b2, warns2 = seats.stop_guard(session=self.sid, seat="alice")
        self.assertIn("unchanged. Reprint:", " ".join(warns2))
        self.assertEqual(calls, [], "a stop paid for the four git reads")

    def test_a_LATCHED_second_stop_promises_nothing_the_first_did_not_print(self):
        """THE HEADER RULING, EXTENDED TO THE LATCH PATH — the one path the
        heterogeneous mixed-lease witness never drives. The latched
        one-liner said "the full detail (exact release commands) printed
        then" UNCONDITIONALLY, but a first stop on a room helm could not
        prove idle withholds EVERY command — so the second stop pointed its
        reader back at commands that were never printed: the header/line
        coupling surviving one path over. The ruling is structural and
        already applied to the sermon: a summary owns the sermon's SHAPE
        (each line carried its own instruction, or said why none is
        offered — the exhaustiveness the branch arm pins), never its
        CONTENT. This drive is a first stop that WITHHELD, then a second on
        the same state."""
        seats.claim(self.res, "alice", ttl=600, session=self.sid)
        blocks, _w = seats.stop_guard(session=self.sid, seat="alice")
        first = " ".join(blocks)
        # the control's premise, MEASURED rather than assumed: the first
        # stop really did withhold every release command (all-clean room)
        self.assertIn(self.res, first)
        self.assertIn("check the lane by hand", first)
        self.assertNotIn("helm work release", first)
        _b2, warns2 = seats.stop_guard(session=self.sid, seat="alice")
        latched = [w for w in warns2
                   if "unchanged. Reprint:" in w]
        self.assertEqual(len(latched), 1, warns2)
        line = latched[0]
        # the old unconditional claim, pinned gone — it was FALSE in this
        # exact drive
        self.assertNotIn("exact release commands", line)
        self.assertNotIn("helm work release", line)
        self.assertNotIn("--lease", line)
        # what it may say instead: the count, and the VERB that reprints —
        # a name, not a description of what the reprint will contain.
        self.assertIn("helm chat stop-guard --detail", line)
        self.assertIn("1 lease(s) held", line)
