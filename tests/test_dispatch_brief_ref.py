#!/usr/bin/env python3
"""A dispatch brief travels WHOLE, by reference, and the send door says its size.

ITS OWN MODULE BECAUSE `tests/test_dispatches.py` IS AT ITS BLOB CEILING. The
never-track rung refuses a staged source file over 1.0 MiB and that file was
1,032,765 bytes before this lane; these arms would have pushed it past. Adding
them here keeps the fixture class they build on — the ledger, the roster and
the temp home are all `DispatchBase`'s — and costs one import.

THE MODULE IS IMPORTED, NEVER ITS NAMES. `from tests.test_dispatches import
DispatchBase` would bind every TestCase in that module into THIS module's
namespace and unittest discovery would run the whole dispatch suite twice.
Importing the module and reaching through it binds one module object."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import dispatches, seats
from tests import test_dispatches as td


class BriefTravelsWholeByReferenceTest(td.DispatchBase):
    """THE ROW IS BOUNDED; THE BRIEF IS WHOLE. Two facts, two places.

    WHAT WAS MEASURED. `MESSAGE_BODY_CAP` was sized from ten recovered briefs
    whose largest was 3212 bytes. Briefs now carry coverage lists and run 5 to
    11 KB: on the live ledger, 259 rows carry the truncation notice and 28 of
    them are still OPEN, the largest declaring 12879 bytes sent against 3805
    kept. Every one of those cuts lands before or inside the coverage list the
    brief exists to carry, and the stored notice tells the reader the tail is
    not recoverable here — so the reviewer who picked that row up read half an
    instruction and the review it produced was scoped to half the work.

    THE CURE IS TWO-SIDED, and an arm for each side. The ledger row keeps its
    cap, because every list read parses every line. The brief is written WHOLE
    to one content-addressed file BEFORE the row that names it, and every
    reader that renders a brief reads that file and PROVES it. A file that is
    missing, unreadable or not the brief falls back to the row's bounded copy
    with a loud line, because a bounded copy shown silently is the defect.

    THE FIXTURE ROSTERS EVERY NAME IT USES, for the reason
    `BriefAndAuthorTravelWithTheObligationTest` states: a non-empty roster
    turns the recipient guard from UNKNOWN-proceed into "absent = REFUSED"."""

    _send = td.BriefAndAuthorTravelWithTheObligationTest._send
    _starve = td.RebindTest._starve

    # A 10 KB brief in the shape the defect was measured on: prose, then the
    # COVERAGE LIST, which is the part the cut ate. The tail marker is what
    # every whole-read arm below looks for, so a cure that keeps a prefix
    # cannot pass by keeping a longer one.
    TAIL = "LAST-LINE-OF-THE-COVERAGE-LIST"

    @classmethod
    def big_brief(cls, nbytes=10240):
        head = ("REVIEW REQUEST — the cure, its arms, and the coverage list "
                "the reviewer must run.\n\nCOVERAGE:\n")
        # NO TRAILING NEWLINE: `send` STRIPS the message before storing it, so
        # a fixture ending in one is not the text that gets stored and every
        # byte-identical assertion below would be comparing against a value
        # the writer never saw.
        tail = "\n" + cls.TAIL
        filler = "  - tests/test_placeholder_%04d.py::an_arm_that_must_run\n"
        body = head
        i = 0
        while len(body.encode("utf-8")) + len(tail.encode("utf-8")) < nbytes:
            body += filler % i
            i += 1
        return body + tail

    def frozen_cut_row(self):
        """One of the live ledger's OPEN cut rows, FROZEN, identities redacted.

        NOT INVENTED. It is the real row a re-dispatch of an already-cut brief
        produced — the same field set, the same `message_hash` over the
        original 4861-byte message, and the same stored body of exactly 4000
        bytes carrying the notice "3806 of 4861". Seat names, the session id
        and the repository paths were replaced with house-convention ones, and
        every substitution is the SAME LENGTH as the token it replaced, so the
        stored byte length the arms assert on is the ledger's, not mine.

        An invented row would agree with whatever the reader happens to do; a
        frozen one disagrees when the reader changes, which is the whole point
        of a capture."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "dispatch_cut_brief_row.json")
        with open(path, encoding="utf-8") as f:
            row = json.load(f)
        # STAMPED INTO THIS FIXTURE'S REPOSITORY, and that is plumbing rather
        # than invention: `triage` scopes rows by the project their ref lives
        # in, so a capture carrying its original repository would land in the
        # UNPLACEABLE bucket and never reach the brief renderer this arm is
        # about. The BODY, the byte lengths, the notice, the message_hash and
        # the field SET — everything the arms assert on — are untouched.
        row["repo_id"] = dispatches._repo_info(self.repo)["repo_id"]
        row["repo_root"] = self.repo
        row["tip"] = self.a
        row["ref"] = self.a
        return row

    def setUp(self):
        super().setUp()
        for seat_name in ("source-seat", "target-seat"):
            seats.write_roster(seat_name, presence_beat=False)
        # RESTORED HERE AND NOT ONLY BY THE BASE CLASS. `DispatchBase.tearDown`
        # does put every ENV_KEY back, but `tests/test_env_hygiene.py` reads
        # the MODULE, statically, and cannot follow a cross-module base class —
        # so the module that sets a var registers its own removal.
        self.addCleanup(os.environ.pop, "HELM_CHAT_NAME", None)
        os.environ["HELM_CHAT_NAME"] = "author-seat"

    def _plant(self, row):
        """Append one frozen creation to the ledger as if it had been sent by
        an older binary. The writer can no longer produce this row — it has no
        `brief_ref` — which is exactly why it must be planted rather than
        made."""
        path = dispatches.ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    # ------------------------------------------------------- stored whole

    def test_a_brief_with_an_internal_CRLF_still_reads_back_whole(self):
        """The file reader must not translate newlines: the digest is over the
        sender's bytes, so a CRLF folded to LF by a universal-newline reader
        made an INTACT file report DIGEST MISMATCH. The control is the same
        brief with LF only, which has to read back whole either way."""
        base = self.big_brief()
        seen_cr = False
        for lane, tail in (("crlf-lane", "\r\nSecond scope line, CRLF."),
                           ("lf-lane", "\nSecond scope line, LF.")):
            brief = base + tail
            row = self._send(brief, lane=lane)
            stored = dispatches.rows()[row["id"]]
            whole, why, problem = dispatches.brief_of(stored)
            self.assertIsNone(problem, "%r: %s" % (tail, problem))
            self.assertIsNone(why)
            self.assertEqual(whole, brief, "%r did not survive whole" % tail)
            seen_cr = seen_cr or "\r\n" in whole
        self.assertTrue(seen_cr, "the CRLF arm never carried a CR through "
                                 "the reader, so it tested nothing")

    def test_a_10KB_brief_is_stored_WHOLE_and_read_back_byte_identical(self):  # noqa: VACUOUS_ASSERTION — the flagged absence is assertNotIn(TAIL, bounded); its unconditional positive is assertEqual(whole, brief) plus assertIn(TAIL, whole) on the SAME brief three lines below, which no empty reader could satisfy
        """THE CENTRAL CLAIM, with the must-hit control beside it: the ROW's
        own copy of this brief is CUT, so a reader that answered from the row
        would lose the coverage list, and an arm that only checked `brief_of`
        could not tell the two apart."""
        brief = self.big_brief()
        self.assertGreater(len(brief.encode("utf-8")),
                           2 * dispatches.MESSAGE_BODY_CAP,
                           "the fixture must exceed the row cap or it proves "
                           "nothing about storing whole")
        row = self._send(brief)
        stored = dispatches.rows()[row["id"]]

        # THE MUST-HIT CONTROL: the row copy alone LOST the coverage list.
        bounded, absent = dispatches.body_of(stored)
        self.assertIsNone(absent)
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, bounded)
        self.assertNotIn(self.TAIL, bounded,
                         "the row's bounded copy still holds the tail, so "
                         "this fixture never reproduced the defect")

        # ...and the whole brief comes back through the reference, byte for
        # byte, with no problem reported.
        whole, why, problem = dispatches.brief_of(stored)
        self.assertIsNone(problem, problem)
        self.assertIsNone(why)
        self.assertEqual(whole, brief, "the brief did not survive whole")
        self.assertIn(self.TAIL, whole)

        # THE ROW STAYS UNDER BOTH CAPS, which is the other half of the cure.
        self.assertLessEqual(len(bounded.encode("utf-8")),
                             dispatches.MESSAGE_BODY_CAP)
        self.assertLessEqual(dispatches._serialized_len(bounded),
                             dispatches.MESSAGE_BODY_SERIALIZED_CAP)

        # THE REFERENCE IS SELF-PROVING: length and digest, both on the row.
        self.assertEqual(stored["brief_bytes"], len(brief.encode("utf-8")))
        self.assertEqual(stored["brief_ref"], dispatches.brief_digest(brief))
        with open(dispatches.brief_file_path(stored["brief_ref"]),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), brief)

    def test_TRIAGE_renders_the_whole_brief_not_the_bounded_copy(self):
        """THE SURFACE THE DEFECT WAS MEASURED ON. A seat picks work up here."""
        brief = self.big_brief()
        row = self._send(brief)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = dispatches.cmd_dispatch(["triage", row["id"]])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn(self.TAIL, text,
                      "triage printed a brief with no coverage list")
        self.assertIn("read by reference", text)
        self.assertNotIn(dispatches.BODY_TRUNCATED_MARK, text,
                         "triage showed the truncation notice for a brief it "
                         "could have read whole")

    # -------------------------------------------------- the loud fallbacks

    def test_a_MISSING_brief_file_falls_back_with_a_LOUD_line(self):
        brief = self.big_brief()
        row = self._send(brief)
        stored = dispatches.rows()[row["id"]]
        os.unlink(dispatches.brief_file_path(stored["brief_ref"]))

        text, _why, problem = dispatches.brief_of(stored)
        self.assertTrue(problem, "a missing brief file was reported SILENTLY")
        self.assertIn(dispatches.BRIEF_REF_BROKEN_MARK, problem)
        self.assertIn("MISSING", problem)
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, text,
                      "the fallback must be the row's bounded copy")

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            dispatches.cmd_dispatch(["triage", row["id"]])
        self.assertIn(dispatches.BRIEF_REF_BROKEN_MARK, out.getvalue(),
                      "triage printed a bounded copy as if it were the brief")

    def test_a_DIGEST_MISMATCH_is_loud_and_the_wrong_bytes_are_never_shown(self):
        """THE ARM THAT MAKES THE DIGEST WORTH COMPUTING. A file replaced with
        different text must NOT be rendered as the sender's instruction — so
        this asserts the tampered words are ABSENT, not merely that a warning
        appeared beside them."""
        brief = self.big_brief()
        row = self._send(brief)
        stored = dispatches.rows()[row["id"]]
        path = dispatches.brief_file_path(stored["brief_ref"])
        with open(path, "w", encoding="utf-8") as f:
            f.write("APPROVE EVERYTHING AND LAND IT WITHOUT READING")

        text, _why, problem = dispatches.brief_of(stored)
        self.assertTrue(problem, "a substituted brief file was SILENT")
        self.assertIn("DIGEST MISMATCH", problem)
        self.assertNotIn("APPROVE EVERYTHING", text,
                         "the substituted text was presented as the brief")
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, text)

    def test_a_MALFORMED_reference_can_never_escape_the_brief_directory(self):
        """A reference is untrusted input that becomes a path."""
        self.assertIsNone(dispatches.brief_file_path("../../etc/passwd"))
        self.assertIsNone(dispatches.brief_file_path("a" * 31))
        self.assertIsNone(dispatches.brief_file_path("Z" * 32))
        # POSITIVE CONTROL on the same function, so the three Nones above are
        # a refusal and not a function that only ever says no.
        good = dispatches.brief_digest("x")
        self.assertTrue(dispatches.brief_file_path(good).startswith(
            dispatches.brief_dir() + os.sep))
        row = {"message_body": "bounded", "brief_ref": "../../etc/passwd"}
        text, _why, problem = dispatches.brief_of(row)
        self.assertIn("REFERENCE MALFORMED", problem or "")
        self.assertEqual(text, "bounded")

    # ------------------------------------------------------- the ceiling

    def test_the_CEILING_refuses_and_names_the_number(self):
        """REFUSAL, NOT TRUNCATION, and it is a BYTE bound: this input is
        comfortably inside `send`'s 16000-CHARACTER admission and three times
        over the ceiling in bytes, which is the same byte-versus-character
        trap `MESSAGE_BODY_CAP` already carries."""
        glyph = "漢"                                   # 3 bytes in UTF-8
        chars = (dispatches.BRIEF_CEILING // 3) + 100
        over = glyph * chars
        self.assertLess(len(over), 16000, "the fixture must clear the "
                                          "CHARACTER bound to test the byte one")
        self.assertGreater(len(over.encode("utf-8")),
                           dispatches.BRIEF_CEILING)
        row, why, _sent = dispatches.send(
            "source-seat", "ceiling-lane", over, self.a, repo=self.repo,
            sign=False, new_work=True)
        self.assertIsNone(row, "a brief over the ceiling was STORED")
        self.assertIn(str(dispatches.BRIEF_CEILING), why)
        self.assertIn(str(len(over.encode("utf-8"))), why)
        # NOTHING WAS WRITTEN — not a row, and not a file.
        self.assertFalse(os.path.exists(
            dispatches.brief_file_path(dispatches.brief_digest(over)) or ""))
        # UNCONDITIONAL POSITIVE CONTROL, AT THE BOUNDARY: the largest brief
        # the ceiling admits is ACCEPTED and stored whole, so the refusal
        # above is about the bound and not about multibyte text. It goes
        # through the library rather than `_send` because that helper asserts
        # DELIVERY, and whether a 32 KB DM is deliverable is a different
        # question from whether this door admits 32 KB.
        under = glyph * (dispatches.BRIEF_CEILING // 3)
        self.assertLessEqual(len(under.encode("utf-8")),
                             dispatches.BRIEF_CEILING)
        from tests._tmphome import dispatch_home
        with dispatch_home(self.repo):
            ok, why, _sent = dispatches.send(
                "source-seat", "ceiling-lane-ok", under, self.a,
                repo=self.repo, sign=False, new_work=True)
        self.assertIsNone(why, why)
        whole, _why, problem = dispatches.brief_of(dispatches.rows()[ok["id"]])
        self.assertIsNone(problem, problem)
        self.assertEqual(whole, under)

    # ------------------------------------------------- the send door's line

    def test_the_send_door_names_the_BYTES_on_every_send(self):  # noqa: VACUOUS_ASSERTION — both arms of the loop assert a PRESENT string (the byte count) unconditionally; the boolean equality on the over-cap clause is a two-sided pin, not an absence
        for brief, expect_over in (("a short brief that fits the row", False),
                                   (self.big_brief(), True)):
            out, err = io.StringIO(), io.StringIO()
            from tests._tmphome import dispatch_home
            with dispatch_home(self.repo), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                rc = dispatches.cmd_dispatch(
                    ["send", "source-seat", "size-lane-%s" % expect_over,
                     brief, "--ref", self.a, "--kind", "review",
                     "--new-work", "--repo", self.repo])
            self.assertEqual(rc, 0, err.getvalue())
            text = out.getvalue()
            self.assertIn("brief %d bytes" % len(brief.encode("utf-8")), text,
                          "the send door did not name the size: %r" % text)
            self.assertEqual("over the %d-byte row cap"
                             % dispatches.MESSAGE_BODY_CAP in text,
                             expect_over,
                             "the over-cap clause fired on the wrong brief")
            if expect_over:
                self.assertIn("the recipient sees the first", text,
                              "send receipt must state what recipient sees")
                self.assertIn("bytes unless it follows the reference", text)

    def test_triage_labels_truncated_bounded_copy_when_falling_back(self):
        """A bounded fallback render must be labelled TRUNCATED bounded copy,
        not 'as sent'."""
        brief = self.big_brief()
        row = self._send(brief)
        stored = dispatches.rows()[row["id"]]
        os.unlink(dispatches.brief_file_path(stored["brief_ref"]))

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            dispatches.cmd_dispatch(["triage", row["id"]])
        text = out.getvalue()
        self.assertIn("TRUNCATED bounded copy", text,
                      "triage fallback did not label the truncated bounded copy")

    def test_stored_notice_names_triage_command_when_reference_exists(self):
        """When a brief_ref exists, the truncation notice must name helm dispatch triage
        and NOT claim the tail is unrecoverable."""
        brief = self.big_brief()
        row = self._send(brief)
        stored = dispatches.rows()[row["id"]]
        bounded, _absent = dispatches.body_of(stored)
        self.assertIn("helm dispatch triage", bounded,
                      "the notice did not name helm dispatch triage")
        self.assertNotIn("NOT recoverable", bounded,
                         "the notice claimed the tail is not recoverable when brief_ref exists")

    def test_triage_labels_undercap_brief_missing_ref_as_stored_on_row(self):
        """An under-cap brief whose reference file is missing must be labelled
        'as sent, stored on the row', not 'TRUNCATED'."""
        brief = "short brief under cap"
        row = self._send(brief)
        stored = dispatches.rows()[row["id"]]
        os.unlink(dispatches.brief_file_path(stored["brief_ref"]))

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            dispatches.cmd_dispatch(["triage", row["id"]])
        text = out.getvalue()
        self.assertIn("BRIEF (as sent, stored on the row):", text,
                      "triage did not label under-cap missing ref as stored on the row")
        self.assertNotIn("BRIEF (TRUNCATED", text,
                         "triage incorrectly labelled an under-cap brief header as TRUNCATED")

    def test_triage_fills_the_notice_id_and_never_rewrites_the_brief(self):
        """The row id replaces the `<id>` in helm's notice only; a sender's
        brief that quotes `<id>` as an instruction reaches the reader intact."""
        brief = "Run `helm dispatch verdict <id> <tip>` when done.\n" + self.big_brief()
        row = self._send(brief)
        stored = dispatches.rows()[row["id"]]
        os.unlink(dispatches.brief_file_path(stored["brief_ref"]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            dispatches.cmd_dispatch(["triage", row["id"]])
        text = out.getvalue()
        self.assertIn("helm dispatch verdict <id> <tip>", text,
                      "triage rewrote the sender's own <id> placeholder")
        self.assertIn("helm dispatch triage %s" % row["id"][:12], text,
                      "triage did not fill the notice's row id")

    # --------------------------------------------------- write order + census

    def test_the_FILE_is_written_BEFORE_the_row_so_nothing_dangles(self):
        """A kill between the two must leave an ORPHAN FILE, never a row
        promising a brief that does not exist. Proven by failing the APPEND
        and measuring both sides: the file is there, the row is not."""
        brief = self.big_brief()
        digest = dispatches.brief_digest(brief)
        from tests._tmphome import dispatch_home
        with dispatch_home(self.repo), \
                mock.patch.object(dispatches, "_append_dispatch",
                                  return_value=(None, "killed here", False)):
            row, why, _sent = dispatches.send(
                "source-seat", "order-lane", brief, self.a, repo=self.repo,
                sign=False, new_work=True)
        self.assertIsNone(row)
        self.assertEqual(why, "killed here")
        self.assertTrue(os.path.exists(dispatches.brief_file_path(digest)),
                        "the brief file was written AFTER the row, so a kill "
                        "in the window leaves a dangling reference")
        for stored in dispatches.rows().values():
            self.assertNotEqual(stored.get("brief_ref"), digest,
                                "a row reached the ledger for a send that "
                                "never appended")

    def test_the_CENSUS_lists_a_cut_row_and_NOT_one_stored_by_reference(self):
        frozen = self.frozen_cut_row()
        self._plant(frozen)
        modern = self._send(self.big_brief(), lane="modern-lane")

        rows, unavailable = dispatches.brief_census()
        self.assertIsNone(unavailable, unavailable)
        listed = {r["id"] for r in rows}
        self.assertIn(frozen["id"], listed,
                      "the census missed a row whose brief was cut")
        self.assertNotIn(modern["id"], listed,
                         "the census listed a row whose brief is stored "
                         "whole; its bounded copy is not a loss")
        hit = next(r for r in rows if r["id"] == frozen["id"])
        # THE NUMBERS ARE RECOVERED FROM THE ROW'S OWN NOTICE, so the
        # integrator learns how much of each brief is gone.
        self.assertEqual(hit["stored_bytes"], 4000)
        self.assertEqual(hit["sent_bytes"], 4861)

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = dispatches.cmd_dispatch(["briefs"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn(frozen["id"][:12], text)
        self.assertNotIn(modern["id"][:12], text)
        self.assertIn("4861", text)
        # THE DENOMINATOR RIDES THE BARE VERB and not --cut, which is the
        # table alone. Four cut rows out of five and four out of four hundred
        # call for different actions and print the same table.
        self.assertIn("stored WHOLE by reference", text)
        cut_only = io.StringIO()
        with contextlib.redirect_stdout(cut_only), \
                contextlib.redirect_stderr(io.StringIO()):
            dispatches.cmd_dispatch(["briefs", "--cut"])
        self.assertNotIn("stored WHOLE by reference", cut_only.getvalue())
        self.assertIn(frozen["id"][:12], cut_only.getvalue())

    def test_briefs_refuses_an_unknown_flag(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = dispatches.cmd_dispatch(["briefs", "--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err.getvalue())

    # --------------------------------------------------------- migration

    def test_a_PRE_LANE_row_still_reads_exactly_as_it_did(self):
        """THE CONTROL ON THE MIGRATION. The frozen row carries no reference,
        so every reader must take the OLD path — the bounded copy, its notice
        intact, and NO loud line, because a fallback that is loud for every
        historical row stops meaning anything on the one row it matters for."""
        frozen = self.frozen_cut_row()
        self._plant(frozen)
        stored = dispatches.rows()[frozen["id"]]
        self.assertIsNone(stored.get("brief_ref"),
                          "the frozen capture must predate the reference")

        body, absent = dispatches.body_of(stored)
        self.assertIsNone(absent)
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, body)
        self.assertEqual(len(body.encode("utf-8")), 4000)

        text, why, problem = dispatches.brief_of(stored)
        self.assertIsNone(problem,
                          "a row with no reference was reported as broken")
        self.assertIsNone(why)
        self.assertEqual(text, body,
                         "the pre-lane reading changed under the new reader")
        # ...and the surface says the same thing it always said.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            dispatches.cmd_dispatch(["triage", frozen["id"]])
        text = out.getvalue()
        self.assertIn("stored on the row", text)
        self.assertNotIn(dispatches.BRIEF_REF_BROKEN_MARK, text)

    def test_the_reference_TRAVELS_so_a_rebound_brief_is_still_whole(self):
        """A rebind is the move whose whole purpose is that the instruction
        arrives with the obligation — so it is the last place that may hand a
        bounded copy to the one seat least able to ask for the tail."""
        brief = self.big_brief()
        row = self._send(brief)
        self._starve("source-seat", streak=True)
        out, err = dispatches.rebind(row["id"], "target-seat", force=True,
                                     reason="the seat is dark", repo=self.repo,
                                     notify=False)
        self.assertIsNone(err, err)
        child = dispatches.rows()[out["new"]["id"]]
        self.assertEqual(child.get("brief_ref"),
                         dispatches.brief_digest(brief),
                         "the rebound row lost the reference, so its "
                         "recipient inherits the cut")
        whole, _why, problem = dispatches.brief_of(child)
        self.assertIsNone(problem, problem)
        self.assertEqual(whole, brief)
        self.assertIn(self.TAIL, whole)
        # THE MUST-HIT: the child's own row copy is still bounded, so this is
        # the reference doing the work and not a cap that quietly moved.
        bounded, _absent = dispatches.body_of(child)
        self.assertIn(dispatches.BODY_TRUNCATED_MARK, bounded)
        self.assertNotIn(self.TAIL, bounded)
        self.assertIn("TRAVELED WHOLE",
                      dispatches._brief_travel_note(
                          dispatches.rows()[row["id"]], child))


if __name__ == "__main__":
    unittest.main()
