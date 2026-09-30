#!/usr/bin/env python3
"""helm task release — the HOLDER hands its own row back to the pool (task/3141).

THE DEFECT THIS MEASURES. `helm task update <id> --owner ''` is refused by the
incumbent guard even when the caller IS the holder, and no other door cleared
an owner. Measured: 40 rows moved from a dead seat to one steward seat for
triage could not be returned to the pool, so the steward nominally held 40
rows it was not working. The owner's model is a person on GitHub unassigning
themselves: the holder says "not mine", and the row is claimable again.

Each arm names the wrong build it kills. The ones that matter most: a
non-holder is REFUSED by name (release must never become a way to take
another seat's row), an already-unowned row is a LOUD no-op (never a silent
success), and the holder is resolved through the SAME door `list --mine` reads
(so the rows a seat sees as its own are exactly the rows it may release).

EVERY "NOTHING WAS WRITTEN" IS MEASURED ON A COUNTER PROVEN TO COUNT. Each arm
pins the ledger's event count to the number of rows its fixture filed before
it reads that count again, so an unchanged count cannot be satisfied by an
instrument that reads zero forever.
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import eventledger, home, tasks  # noqa: E402
from tests.test_tasks import CliBase, TasksBase  # noqa: E402


class ReleaseLibraryTest(TasksBase):
    """tasks.release() — the API door, which the CLI only forwards to."""

    def test_the_holder_releases_and_every_other_answer_writes_nothing(self):
        row = self.file("inherited steward row", "seat-a",
                        status="in_progress")
        # MUST-HIT FIRST: the release writes. Every refusal below is free if
        # the function is simply broken.
        got, err = tasks.release(row["id"], "seat-a", note="not mine",
                                 path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual((got["owner"], got["status"]), (None, "open"))
        self.assertEqual(got["released"]["by"], "seat-a")
        self.assertEqual(len(self.lines()), 2)

        # A SECOND RELEASE IS THE THIRD ANSWER, NOT SUCCESS: the row was read
        # and there was nothing to write.
        again, why = tasks.release(row["id"], "seat-a", path=self.path)
        self.assertIs(again, tasks.SKIPPED)
        self.assertIn("already UNOWNED", why)

        for token, releaser, want in (
                (row["id"], "", "releaser"),
                (row["id"], "UNOWNED", "PRINTS for an absent owner"),
                ("not an id!", "seat-a", "unparseable"),
                ("task/99999", "seat-a", "does not exist")):
            with self.subTest(token=token, releaser=releaser):
                none, err = tasks.release(token, releaser, path=self.path)
                self.assertIsNone(none)
                self.assertIn(want, err)
        # the add + the one release; every other answer appended nothing
        self.assertEqual(len(self.lines()), 2)


class ReleaseCliTest(CliBase):
    """`helm task release <id> [--note TEXT]`, as a seat runs it."""

    def held(self, title, owner="seat-a", **kw):
        """File a row under the CLI's ledger -> its row. `force_new`: the
        titles in one arm are a fixture, not a backlog to dedupe."""
        row, err = tasks.add(title, owner, path=tasks.ledger_path(),
                             force_new=True, **kw)
        self.assertIsNone(err, "fixture add refused: %s" % err)
        return row

    def now(self, tid):
        return tasks.get(tid, path=tasks.ledger_path())

    def test_the_holder_puts_an_in_progress_row_back_in_the_open_pool(self):
        free = self.held("a free row the pool already offers", owner=None)
        tid = self.held("the triage row nobody is building",
                        status="in_progress")["id"]
        quiet_since = self.now(tid)["last_updated"]
        # CONTROL ON THE POOL OBSERVABLE: while seat-a is live and holds the
        # row, another seat is offered the free row and NOT this one. Without
        # it, "offered after the release" is satisfied by a rung that offers
        # everything.
        live = ("seat-a", "seat-b")

        def pool():
            return sorted(o[5]["id"] for o in tasks.offer_rows(
                tasks.ledger_path(), seat="seat-b", live=live)
                if not (o[5].get("owner") or ""))

        self.assertEqual(pool(), [free["id"]])
        before = self.ledger_lines()
        self.assertEqual(before, 2)

        rc, out, err = self.cli("release", tid, "--note",
                                "triaged: not mine to build")
        self.assertEqual(rc, 0, err)
        self.assertIn(tid, out)
        self.assertIn("UNOWNED", out)

        got = self.now(tid)
        self.assertEqual(got["status"], "open")
        self.assertIsNone(got["owner"])
        self.assertEqual(got["released"], {
            "by": "seat-a", "ts": got["released"]["ts"], "session": None,
            "pid": os.getpid(), "ppid": os.getppid(), "owner_was": "seat-a",
            "status_was": "in_progress",
            "note": "triaged: not mine to build"})
        # ONE audited event, and it is a CUSTODY move rather than activity:
        # stalebot reads last_updated, so a release that refreshed it would
        # make every released row look freshly worked (the reassign lesson).
        self.assertEqual(self.ledger_lines(), 3)
        self.assertEqual(got["last_updated"], quiet_since)
        self.assertIn("custody_updated", got)
        # THE CONSEQUENCE THE VERB EXISTS FOR: another seat is offered it.
        self.assertEqual(pool(), sorted([free["id"], tid]))
        # and `show` says who released it and why
        rc, out, err = self.cli("show", tid)
        self.assertEqual(rc, 0, err)
        self.assertIn("released", out)
        self.assertIn("by seat-a", out)
        self.assertIn("triaged: not mine to build", out)

    def test_an_open_row_the_seat_holds_releases_and_stays_open(self):
        tid = self.held("an assigned row never started")["id"]
        rc, _out, err = self.cli("release", tid)
        self.assertEqual(rc, 0, err)
        got = self.now(tid)
        self.assertEqual((got["owner"], got["status"]), (None, "open"))
        self.assertEqual(got["released"]["status_was"], "open")
        self.assertEqual(got["released"]["by"], "seat-a")
        self.assertIsNone(got["released"]["note"])

    def test_a_non_holder_is_refused_by_name_and_nothing_moves(self):
        # THE FILER IS NOT THE HOLDER. seat-a filed this row FOR kimi, so its
        # `source` is seat-a — the first of the three killed reclaim designs
        # (filing spent as custody) must stay dead through this door too.
        rc, _o, err = self.cli("add", "work", "filed", "for", "kimi",
                               "--owner", "kimi")
        self.assertEqual(rc, 0, err)
        row = self.filed("work filed for kimi")
        self.assertEqual(row["source"], "seat-a")
        before = self.ledger_lines()
        self.assertEqual(before, 1)

        rc, out, err = self.cli("release", row["id"])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("held by kimi", err)
        self.assertIn("seat-a", err)
        self.assertEqual(self.ledger_lines(), before)
        kept = self.now(row["id"])
        self.assertEqual((kept["owner"], kept["status"]), ("kimi", "open"))

        # POSITIVE CONTROL ON THE SAME ROW: the holder can. Without it the
        # refusal above is indistinguishable from a verb that always refuses.
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "kimi"}):
            rc, _out, err = self.cli("release", row["id"])
        self.assertEqual(rc, 0, err)
        freed = self.now(row["id"])
        self.assertEqual((freed["owner"], freed["released"]["by"]),
                         (None, "kimi"))

    def test_an_unowned_row_is_a_loud_no_op_never_a_silent_success(self):
        tid = self.held("already in the pool", owner=None)["id"]
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        rc, out, err = self.cli("release", tid)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("already UNOWNED", err)
        self.assertIn("NOTHING", err)
        self.assertEqual(self.ledger_lines(), before)

    def test_a_closed_row_is_refused(self):
        tid = self.held("finished long ago", status="closed",
                        closed_reason="landed")["id"]
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        rc, _out, err = self.cli("release", tid)
        self.assertEqual(rc, 2)
        self.assertIn("CLOSED", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.now(tid)["owner"], "seat-a")

    def test_an_unresolvable_or_disputed_identity_releases_nothing(self):
        from helm import seats
        tid = self.held("a row with a careful holder",
                        status="in_progress")["id"]
        before = self.ledger_lines()
        self.assertEqual(before, 1)

        self.unidentify()
        rc, _out, err = self.cli("release", tid)
        self.assertEqual(rc, 2)
        self.assertIn("refusing to release", err)
        self.assertIn("NOTHING WAS RELEASED", err)

        # DISPUTED: the process DECLARES seat-a while its session is rostered
        # to seat-b. `own_name()` would hand seat-a over; the --mine door
        # refuses, and release must refuse with it.
        self.admit("seat-b")
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-a"}):
            self.assertEqual(seats.own_name(), "seat-a")
            rc, _out, err = self.cli("release", tid)
        self.assertEqual(rc, 2)
        self.assertIn("DISPUTED", err)
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.now(tid)["owner"], "seat-a")

        # POSITIVE CONTROL: the same row releases once the identity is clean.
        with mock.patch.dict(os.environ, {"HELM_CHAT_NAME": "seat-a"}):
            for key in home._SESSION_ENV:
                os.environ.pop(key, None)
            rc, _out, err = self.cli("release", tid)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ledger_lines(), 2)

    def test_the_cli_refuses_what_it_does_not_implement(self):
        tid = self.held("a row for argv abuse")["id"]
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        for argv in (("release",),
                     ("release", "--note", "x"),
                     ("release", tid, "--owner", "seat-b"),
                     ("release", tid, "--note"),
                     ("release", tid, "stray", "words"),
                     ("release", tid, "--project", "helm"),
                     ("release", tid, "--all-mine"),
                     ("release", tid, "--force")):
            with self.subTest(argv=argv):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 2)
                self.assertIn("helm task", err)
                self.assertEqual(out, "")  # noqa: VACUOUS_ASSERTION — err on the same call is pinned non-empty one line up
        self.assertEqual(self.ledger_lines(), before)
        self.assertEqual(self.now(tid)["owner"], "seat-a")
        # the `=` spelling is the literal escape for a dash-leading note
        rc, _out, err = self.cli("release", tid, "--note=--not-a-flag")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.now(tid)["released"]["note"], "--not-a-flag")

    def test_the_rows_list_mine_shows_are_the_rows_release_accepts(self):
        # ONE DOOR FOR "WHICH ROWS ARE MINE". The owner field keeps whatever
        # spelling a seat typed, and --mine matches casefolded; a release that
        # compared raw would refuse a row its own seat lists as its own.
        mine = self.held("a row filed under a capitalised name",
                         owner="Seat-A")["id"]
        theirs = self.held("a row somebody else holds", owner="kimi")["id"]
        rc, out, err = self.cli("list", "--mine", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["id"] for r in json.loads(out)], [mine])
        rc, _out, err = self.cli("release", mine)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("release", theirs)
        self.assertEqual(rc, 2)
        self.assertIn("held by kimi", err)

    def test_the_clear_refusal_names_the_release_door(self):
        # THE MEASURED FAILURE: the holder typed `update --owner ''` and was
        # told nothing it could run would help. A transfer refusal is left as
        # it was — release is not a transfer and must not be offered as one.
        tid = self.held("the row a holder tried to clear")["id"]
        before = self.ledger_lines()
        self.assertEqual(before, 1)
        rc, _out, err = self.cli("update", tid, "--owner", "")
        self.assertEqual(rc, 2)
        self.assertIn("helm task release %s" % tid, err)
        rc, _out, err = self.cli("update", tid, "--owner", "seat-b")
        self.assertEqual(rc, 2)
        self.assertIn("held by seat-a", err)
        self.assertNotIn("helm task release", err)
        self.assertEqual(self.ledger_lines(), before)

    def test_the_release_event_traces_the_acting_process_and_reverses(self):
        # THE INTEGRATOR'S FIRST CONDITION: a forged release must be
        # TRACEABLE and REVERSIBLE from the ledger alone. The event records
        # the acting seat, the session it declared and the pids that ran it,
        # and it keeps the holder's exact spelling so the row can be put back.
        self.admit("seat-a")
        tid = self.held("a row held under its own spelling", owner="Seat-A",
                        status="in_progress")["id"]
        rc, _out, err = self.cli("release", tid, "--note", "handing back")
        self.assertEqual(rc, 0, err)
        raw = [ev for ev in self.lines(tasks.ledger_path())
               if ev["id"] == tid][-1]["released"]
        self.assertEqual(
            {k: raw[k] for k in ("by", "session", "pid", "ppid", "owner_was",
                                 "status_was", "note")},
            {"by": "seat-a", "session": "sess-for-seat-a",
             "pid": os.getpid(), "ppid": os.getppid(), "owner_was": "Seat-A",
             "status_was": "in_progress", "note": "handing back"})

        # THE SESSION IS AUDIT, NOT WIRE. A session id backs a declared
        # identity, so publishing it hands out the bearer the killed reclaim
        # designs were forged with; the store keeps it, the wire drops it —
        # the rule public_row already applies to `reported_session`.
        rc, out, err = self.cli("show", tid, "--json")
        self.assertEqual(rc, 0, err)
        self.assertIn('"owner_was": "Seat-A"', out)
        self.assertNotIn("sess-for-seat-a", out)
        wire = json.loads(out)["released"]
        self.assertEqual(wire["by"], "seat-a")
        self.assertEqual(wire["pid"], os.getpid())
        self.assertNotIn("session", wire)

        # REVERSIBLE FROM THE RECORD: the row is unowned now, so the ordinary
        # update door puts the recorded holder and status straight back.
        back, err = tasks.update(tid, path=tasks.ledger_path(),
                                 owner=raw["owner_was"],
                                 status=raw["status_was"])
        self.assertIsNone(err, err)
        self.assertEqual(back["owner"], "Seat-A")
        self.assertEqual(back["status"], "in_progress")

    def test_a_live_holder_is_refused_to_every_other_seat_as_is_a_dead_one(self):
        # THE INTEGRATOR'S THIRD CONDITION: a release of a row whose holder is
        # LIVE, by a DIFFERENT acting seat, is refused naming the holder.
        # HOLDER-ONLY ALREADY MAKES THAT UNREACHABLE, and this arm proves it
        # rather than asserting it: kimi is live by the fleet's own liveness
        # read (the one the offer rung's stranded-work rescue trusts), ghost
        # is not, and seat-a is refused on both — so the refusal does not
        # depend on liveness at all, and no liveness read can open it.
        from helm import pk, seats, seats_work_offer
        pk.atomic_write(seats.roster_path(), json.dumps(
            {"kimi": {"session": "sess-kimi", "last_seen": time.time()}}))
        live = seats_work_offer._live_seats()
        self.assertIn("kimi", live)
        self.assertNotIn("ghost", live)
        rows = {who: self.held("work %s is holding" % who, owner=who,
                               status="in_progress")["id"]
                for who in ("kimi", "ghost")}
        before = self.ledger_lines()
        self.assertEqual(before, 2)
        rc, out, err = self.cli("release", rows["kimi"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("held by kimi, not by seat-a", err)
        rc, out, err = self.cli("release", rows["ghost"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("held by ghost, not by seat-a", err)
        self.assertEqual(self.ledger_lines(), before)

    def test_the_second_leg_of_the_two_step_can_name_any_seat(self):
        # THE TWO-STEP THE DOCSTRING STATES, MEASURED RATHER THAN ASSERTED.
        # Release names nobody, but the row it leaves is UNOWNED, and both
        # `claim --owner SEAT` and `update --owner SEAT` put an unowned row on
        # whatever seat they name without resolving who is asking. So a
        # release followed by either is a hand-off to a NAMED seat (the
        # task/2026 harm) in two steps; only the release leg records its
        # actor. The identity lane that binds these doors flips this arm.
        tid = self.held("a row the two-step moves", status="in_progress")["id"]
        rc, _out, err = self.cli("claim", tid, "--owner", "kimi")
        self.assertEqual(rc, 2, "control: a held row refuses the claim")
        self.assertIn("held by seat-a", err)
        rc, _out, err = self.cli("release", tid)
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.cli("claim", tid, "--owner", "kimi")
        self.assertEqual(rc, 0, err)
        got = self.now(tid)
        self.assertEqual(got["owner"], "kimi")
        # the trace survives on the row until the next release overwrites it
        self.assertEqual((got["released"]["by"],
                          got["released"]["owner_was"]), ("seat-a", "seat-a"))

    # ---------------------------------------------- through the one write door
    #
    # THE ROW A HOLDER MOST NEEDS TO HAND BACK IS A LONG-LIVED ONE, and a
    # long-lived row is the one near the event cap. `tasks._commit` is the one
    # write door for a task row: it moves the oldest comments into an archive
    # and names the size and the cap when it still refuses. A release that
    # appended bare got "task ledger refused the write" for that row, and
    # nothing else. Both arms measure the door, not the verb's fields.

    CAP = eventledger.MAX_EVENT_BYTES

    def at_the_cap(self, tid, slack, comments=0, note_pad=False):
        """Plant `tid` as a writer before the archive cure would have left
        it: `slack` bytes under the cap, its bulk in `comments` comments or,
        with `note_pad`, in its NOTE, where no archive can reach it."""
        path = tasks.ledger_path()
        row = dict(self.now(tid), comments=[
            {"ts": 1700000000.0 + i, "text": "c-%03d %s" % (i, "x" * 60),
             "by": "seat-a" if i % 2 else "seat-b"} for i in range(comments)])
        if note_pad:
            # a string BEFORE measuring: padding a null note would swap its
            # four bytes for two quotes and miss the size by two
            row["note"] = row.get("note") or ""
        pad = self.CAP - slack - _line_bytes(row)
        self.assertGreater(pad, 0, "the fixture row does not fit the cap")
        if note_pad:
            row["note"] += "n" * pad
        else:
            row["comments"][0]["text"] += "p" * pad
        self.assertEqual(_line_bytes(row), self.CAP - slack)
        with eventledger.locked(path) as held:
            self.assertTrue(held, "the fixture could not lock the ledger")
            self.assertTrue(eventledger.append_unlocked(path, row),
                            "the fixture row did not fit the ledger")
        return row

    def bare_release(self, row, note):
        """The row the pre-cure release appended for `row`, unfitted."""
        now = time.time()
        return dict(row, owner=None, status="open", custody_updated=now,
                    released={"by": "seat-a", "ts": now, "session": None,
                              "pid": os.getpid(), "ppid": os.getppid(),
                              "owner_was": row.get("owner"),
                              "status_was": row.get("status"), "note": note})

    def test_a_row_at_the_cap_is_released_with_its_oldest_comments_archived(self):
        tid = self.held("a long-lived row the holder hands back",
                        status="in_progress")["id"]
        planted = self.at_the_cap(tid, slack=200, comments=35)
        self.assertEqual(planted["owner"], "seat-a")
        note = "triaged, not mine to build: " + "r" * 300
        # CONTROL: appended bare, the released row does not fit one event, so
        # a pass below is the archive making room and never a row that fit.
        self.assertGreater(_line_bytes(self.bare_release(planted, note)),
                           self.CAP)
        # the add and the planted row: a counter proven to count
        self.assertEqual(self.ledger_lines(), 2)

        rc, out, err = self.cli("release", tid, "--note", note)
        self.assertEqual(rc, 0, err)
        self.assertIn("UNOWNED", out)
        self.assertEqual(self.ledger_lines(), 3)
        got = self.now(tid)
        self.assertEqual((got["owner"], got["status"],
                          got["released"]["by"], got["released"]["note"]),
                         (None, "open", "seat-a", note))
        self.assertEqual(len(got.get("comment_archives") or ()), 1,
                         "the release did not move the oldest comments off")
        self.assertLessEqual(_line_bytes(got), tasks.ROW_BUDGET)
        # EVERY COMMENT SURVIVES, IN ORDER, read back through the one reader.
        self.assertEqual([c["text"][:5] for c in tasks.comments_of(got)],
                         ["c-%03d" % i for i in range(35)])

    def test_a_row_whose_bulk_is_its_note_is_refused_naming_its_size_and_the_cap(self):
        tid = self.held("a row whose note outgrew the ledger",
                        status="in_progress")["id"]
        planted = self.at_the_cap(tid, slack=200, note_pad=True)
        self.assertEqual(planted["owner"], "seat-a")
        note = "handing it back: " + "r" * 300
        self.assertGreater(_line_bytes(self.bare_release(planted, note)),
                           self.CAP)
        self.assertEqual(self.ledger_lines(), 2)

        rc, out, err = self.cli("release", tid, "--note", note)
        self.assertEqual((rc, out), (2, ""))
        # NOT THE BARE REFUSAL: the cause, the row's size, the cap, and the
        # field to shorten, which is what `_commit` says and a bare append
        # never did.
        self.assertIn("task ledger refused the write: %s's row is " % tid,
                      err)
        self.assertIn("one ledger event may be at most %d bytes" % self.CAP,
                      err)
        self.assertRegex(err, r"the largest are: note \d+ bytes")
        # the ROW'S note is the bulk, so the door that shortens it is update
        self.assertIn("`helm task update %s --note <shorter>`" % tid, err)
        self.assertEqual(self.ledger_lines(), 2)
        got = self.now(tid)
        self.assertEqual(got["owner"], "seat-a")
        self.assertEqual(got["status"], "in_progress")
        self.assertNotIn("released", got)

    # ------------------------------------------------- the release note's cap
    #
    # A RELEASE NOTE IS A SHORT HANDBACK REASON, AND IT STAYS ON THE ROW: the
    # `released` block rides every later claim, update, comment and close
    # unedited, and only the row's next release replaces it, so an unbounded
    # note could pin a row near the event cap until then. It is capped at
    # 2,000 characters, counted on the text that is stored, and a longer one
    # is refused whole: never cut, nothing written.

    def released_with(self, note):
        """Release a fresh held row with `note` through the CLI -> its row."""
        tid = self.held("a row handed back with a full reason",
                        status="in_progress")["id"]
        rc, out, err = self.cli("release", tid, "--note", note)
        self.assertEqual(rc, 0, err)
        self.assertIn("UNOWNED", out)
        return self.now(tid)

    def test_a_release_note_at_the_cap_is_admitted_and_kept_whole(self):
        got = self.released_with("r" * 2000)
        self.assertEqual(len(got["released"]["note"]), 2000)
        self.assertEqual(got["released"]["note"], "r" * 2000)
        self.assertEqual(got["released"]["by"], "seat-a")
        self.assertIsNone(got["owner"])
        # CHARACTERS, NOT BYTES: 2,000 of these is 4,000 UTF-8 bytes
        got = self.released_with("\u00e9" * 2000)
        self.assertEqual(len(got["released"]["note"]), 2000)
        self.assertEqual(got["released"]["note"], "\u00e9" * 2000)
        # the cap counts what is STORED, and the surrounding blanks are
        # stripped before anything is stored
        got = self.released_with("  %s\n" % ("s" * 2000))
        self.assertEqual(len(got["released"]["note"]), 2000)
        self.assertEqual(got["released"]["note"], "s" * 2000)

    def test_a_release_note_one_past_the_cap_is_refused_whole_and_nothing_moves(self):
        tid = self.held("a row handed back with too long a reason",
                        status="in_progress")["id"]
        self.assertEqual(self.ledger_lines(), 1)
        note = "r" * 2001

        rc, out, err = self.cli("release", tid, "--note", note)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("REFUSED", err)
        # THE CAP AND THE LENGTH, and the door that shortens it: the
        # release's own --note, never update --note
        self.assertIn("2001 characters", err)
        self.assertIn("at most 2000", err)
        self.assertIn("helm task release %s --note" % tid, err)
        self.assertNotIn("helm task update", err)
        self.assertEqual(self.ledger_lines(), 1)
        got = self.now(tid)
        self.assertEqual(got["owner"], "seat-a")
        self.assertEqual(got["status"], "in_progress")
        self.assertNotIn("released", got)

        # THE API DOOR REFUSES THE SAME: other helm code calls release()
        # without the CLI, and a cap that lives only in the CLI is no cap.
        none, why = tasks.release(tid, "seat-a", note=note,
                                  path=tasks.ledger_path())
        self.assertIsNone(none)
        self.assertIn("2001 characters", why)
        self.assertEqual(self.ledger_lines(), 1)

    def test_a_long_note_never_hides_the_answer_about_the_row(self):
        """THE ROW IS JUDGED BEFORE THE NOTE. A missing row, another seat's
        row, a closed row and an unowned one each get their own answer when
        the note is also past the cap: "note too long" would send the caller
        to shorten a note for a release that could never happen."""
        long = "r" * (tasks.RELEASE_NOTE_MAX + 1)
        path = tasks.ledger_path()
        mine = self.held("the caller's own row", status="in_progress")["id"]
        other = self.held("another seat's row", owner="seat-b",
                          status="in_progress")["id"]
        closed = self.held("a finished row", status="closed",
                           closed_reason="landed")["id"]
        free = self.held("a row already in the pool", owner=None)["id"]
        self.assertEqual(self.ledger_lines(), 4)
        # CONTROL: on the caller's own row the same note meets the cap.
        none, why = tasks.release(mine, "seat-a", note=long, path=path)
        self.assertIsNone(none)
        self.assertIn("%d characters" % len(long), why)
        for token, want in (("task/99999", "does not exist"),
                            (other, "held by seat-b, not by seat-a"),
                            (closed, "is CLOSED")):
            with self.subTest(token=token):
                none, why = tasks.release(token, "seat-a", note=long,
                                          path=path)
                self.assertIsNone(none)
                self.assertIn(want, why)
                self.assertNotIn("characters", why)
        skipped, why = tasks.release(free, "seat-a", note=long, path=path)
        self.assertIs(skipped, tasks.SKIPPED)
        self.assertIn("already UNOWNED", why)
        # and at the CLI, the door a seat runs
        rc, out, err = self.cli("release", other, "--note", long)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("held by seat-b", err)
        self.assertNotIn("characters", err)
        self.assertEqual(self.ledger_lines(), 4)

    def test_a_row_with_several_archives_keeps_every_comment_in_order_through_a_release(self):
        # THE ARM ABOVE ARCHIVES ONE COMMENT, so it pins archive-before-inline
        # and nothing about the order AMONG archived comments. Here the row
        # already carries two archives of many comments each, written through
        # the real comment door, and the release itself mints a third that
        # holds several more. Every comment reads back in the order written.
        tid = self.held("a long-lived row with a deep comment history",
                        status="in_progress")["id"]
        path = tasks.ledger_path()
        written = []
        row = self.now(tid)
        # grow until two archives exist and the row sits within one comment
        # of ROW_BUDGET, never past it, so the release note is what spills
        while (len(row.get("comment_archives") or ()) < 2
               or _line_bytes(row) + 1200 < tasks.ROW_BUDGET):
            mark = "c-%03d" % len(written)
            row, err = tasks.comment(
                tid, "%s %s" % (mark, "y" * 1000), path=path,
                by="seat-a" if len(written) % 2 else "seat-b")
            self.assertIsNone(err, err)
            written.append(mark)
        before = list(row["comment_archives"])
        self.assertEqual(len(before), 2)
        self.assertTrue(all(a["count"] > 1 for a in before), before)
        self.assertLessEqual(_line_bytes(row), tasks.ROW_BUDGET)
        note = "handing back a deep row: " + "r" * (
            tasks.ROW_BUDGET - _line_bytes(row) + 64)
        self.assertLessEqual(len(note), tasks.RELEASE_NOTE_MAX)
        # CONTROL: the released row is past ROW_BUDGET, so a third archive
        # below is the release spilling and never a row that already had one.
        self.assertGreater(_line_bytes(self.bare_release(row, note)),
                           tasks.ROW_BUDGET)

        rc, out, err = self.cli("release", tid, "--note", note)
        self.assertEqual(rc, 0, err)
        self.assertIn("UNOWNED", out)
        got = self.now(tid)
        self.assertEqual((got["owner"], got["status"], got["released"]["note"]),
                         (None, "open", note))
        archives = got["comment_archives"]
        self.assertEqual(archives[:2], before,
                         "the release rewrote an archive it did not mint")
        self.assertEqual(len(archives), 3,
                         "the release did not move its oldest comments off")
        self.assertGreater(archives[2]["count"], 1)
        self.assertLessEqual(_line_bytes(got), tasks.ROW_BUDGET)
        self.assertEqual(tasks.comment_count(got), len(written))
        # EVERY COMMENT, IN THE ORDER WRITTEN, across all three archives and
        # the comments still inline, read back through the one reader.
        self.assertEqual([c["text"][:5] for c in tasks.comments_of(got)],
                         written)


def _line_bytes(row):
    """What the ledger writes for `row`, terminator included, computed here
    so the arms do not measure with the door they test."""
    return len((json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                + "\n").encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
