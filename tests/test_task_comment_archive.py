#!/usr/bin/env python3
"""A task row outgrows the event cap no longer: its old comments move out whole.

THE DEFECT. Every task mutation appends the WHOLE row, comments included, and
one ledger event may be at most `eventledger.MAX_EVENT_BYTES`. A long-lived
task therefore grew toward the cap one comment at a time and then refused
every write — the next comment, an update, a close — and each refusal was the
same bare "task ledger refused the write", naming no cause. Measured on the
live ledger: one row at 65,511 bytes with 35 comments, whose 36th comment
could never be written.

THE CURE, ONE ARM PER CLAIM:
  (a) a row near the cap, and a row grown past it one comment at a time, keep
      taking comments, and `task show` prints EVERY comment in order;
  (b) a refusal names its cause, the row's size and the cap;
  (c) an archive that is deleted or altered is reported LOUDLY by every
      reader, never silently dropped;
  (d) the fold of the ledger still yields the latest row, and the archives
      plus the row hold every comment ever written, in order;
  F1  the archive is stored and fsynced BEFORE any row names it;
  F2  nothing reaps the store the archives live in;
  F5  a task holding 500 comments accepts the 501st.

ITS OWN MODULE, importing the task suite's fixture MODULE (never its names,
which would make unittest discovery run that suite twice here)."""
import contextlib
import errno
import io
import json
import os
import re
from unittest import mock

from tests import _tmphome           # noqa: F401 — must precede helm.*
from helm import eventledger, gc, refstore, registry, tasks, tasksmirror
from helm import web_core
from tests import test_tasks as tt

CAP = eventledger.MAX_EVENT_BYTES
BASE_TS = 1700000000.0
MARK = re.compile(r"\b([co]-\d{3})\b")


def _line_bytes(row):
    """What the ledger writes for `row`, computed here so the fixtures do not
    depend on the helper under test."""
    return len((json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                + "\n").encode("utf-8"))


class RowBase(tt.CliBase):
    """One ledger for the API and the CLI alike: `cmd_task` reads the default
    path, so every call here passes that same path."""

    def setUp(self):
        super().setUp()
        self.path = tasks.ledger_path()

    def file_task(self, title="a long-lived task that keeps taking comments"):
        return self.file(title, "seat-a")["id"]

    def plant(self, row):
        """Append `row` as a writer from before this cure would have: the
        whole row, comments inline, through the boolean door."""
        with eventledger.locked(self.path) as held:
            self.assertTrue(held, "the fixture could not lock the ledger")
            self.assertTrue(eventledger.append_unlocked(self.path, row),
                            "the fixture row did not fit the ledger")

    def near_cap_row(self, tid, count, slack=200):
        """The live shape: `count` comments whose row sits `slack` bytes under
        the cap, so ANY further comment of a few hundred bytes cannot fit."""
        prev = tasks.snapshot(self.path)[0][tid]
        comments = [{"ts": BASE_TS + i, "text": "c-%03d %s" % (i, "x" * 60),
                     "by": "seat-a" if i % 2 else "seat-b"}
                    for i in range(count)]
        row = dict(prev, comments=comments, last_updated=BASE_TS + count)
        pad = CAP - slack - _line_bytes(row)
        self.assertGreater(pad, 0, "%d comments do not fit under the cap"
                           % count)
        comments[0]["text"] += "p" * pad
        self.assertEqual(_line_bytes(row), CAP - slack)
        self.plant(row)
        return ["c-%03d" % i for i in range(count)]

    def say(self, tid, text, by="seat-a"):
        row, err = tasks.comment(tid, text, by=by, path=self.path)
        self.assertIsNone(err, "the comment was refused: %s" % err)
        return row

    def grow(self, tid, count, size=1000, start=0, by="seat-a"):
        """`count` comments through the real door, each `size` bytes of text."""
        marks = []
        for i in range(start, start + count):
            self.say(tid, "o-%03d %s" % (i, "y" * size), by=by)
            marks.append("o-%03d" % i)
        return marks

    def shown(self, tid, *flags):
        """(markers of every comment line `task show` printed, in order, the
        whole stdout, stderr)."""
        rc, out, err = self.cli("show", tid, *flags)
        self.assertEqual(rc, 0, err)
        marks = [m.group(1) for line in out.splitlines()
                 if line.startswith("    comment ")
                 for m in [MARK.search(line)] if m]
        return marks, out, err

    def latest(self, tid):
        rows, unavailable = tasks.snapshot(self.path, strict=True)
        self.assertIsNone(unavailable)
        return rows[tid]

    def ledger_bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def store_files(self):
        d = refstore.directory()
        return set(os.listdir(d)) if os.path.isdir(d) else set()


class ARowNearTheCapKeepsTakingCommentsTest(RowBase):
    """(a) and F5 — the live defect, reproduced in its own shape."""

    def test_a_a_row_just_under_the_cap_takes_its_36th_comment_and_show_prints_all_36_in_order(self):
        tid = self.file_task()
        before = self.near_cap_row(tid, 35)
        row = self.say(tid, "c-035 the thirty-sixth comment " + "z" * 300)
        self.assertLessEqual(_line_bytes(row), tasks.ROW_BUDGET,
                             "the row did not shed its old comments")
        self.assertEqual(tasks.comment_count(row), 36)
        marks, out, _err = self.shown(tid)
        self.assertIn("c-000", marks)
        self.assertEqual(marks, before + ["c-035"], out[-2000:])

    def test_a_a_row_grown_one_comment_at_a_time_never_meets_the_cap(self):
        tid = self.file_task()
        written = self.grow(tid, 120)
        self.assertGreater(len(self.latest(tid).get("comment_archives") or ()),
                           1, "120 comments of 1 KB never spilled twice")
        longest = max(len(line) + 1
                      for line in self.ledger_bytes().splitlines())
        self.assertGreater(longest - tasks.INLINE_KEEP, 0)
        self.assertLessEqual(longest, tasks.ROW_BUDGET,
                             "a row was written past its own budget")
        marks, out, _err = self.shown(tid)
        self.assertIn("o-119", marks)
        self.assertEqual(marks, written, out[-2000:])

    def test_F5_a_task_holding_500_comments_accepts_the_501st(self):
        tid = self.file_task()
        count = 500
        prev = tasks.snapshot(self.path)[0][tid]
        comments = [{"ts": BASE_TS + i, "text": "c-%03d " % (i % 1000),
                     "by": "seat-a"} for i in range(count)]
        row = dict(prev, comments=comments)
        comments[0]["text"] += "p" * (CAP - 150 - _line_bytes(row))
        self.assertEqual(_line_bytes(row), CAP - 150)  # noqa: VACUOUS_ASSERTION — fixture precondition: the planted row sits exactly 150 bytes under the cap; the 501 counts below are the positive controls
        self.plant(row)
        after = self.say(tid, "the five hundred and first comment " + "q" * 400)
        self.assertEqual(tasks.comment_count(after), 501)
        whole = tasks.comments_of(self.latest(tid))
        self.assertEqual(len(whole), 501)
        self.assertEqual([c["text"][:6] for c in whole[:3]],
                         ["c-000 ", "c-001 ", "c-002 "])
        self.assertTrue(whole[-1]["text"].startswith("the five hundred"))
        # AND IT KEEPS GOING: the 502nd onward land on the shed row.
        self.grow(tid, 40, start=0)
        self.assertEqual(tasks.comment_count(self.latest(tid)), 541)

    def test_a_an_update_and_a_close_on_a_row_at_the_cap_are_accepted(self):
        tid = self.file_task()
        self.near_cap_row(tid, 35, slack=40)
        row, err = tasks.update(tid, path=self.path,
                                note="a note long enough to cross the cap " * 4)
        self.assertIsNone(err, err)
        row, err = tasks.close(tid, "done, with all 35 comments kept",
                               path=self.path)
        self.assertIsNone(err, err)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(len(tasks.comments_of(row)), 35)


class ARefusalSaysWhyTest(RowBase):
    """(b) and F4 — every refusal names its cause, the row's size and the cap."""

    def test_b_the_ledger_refuses_an_over_cap_event_naming_its_size_and_the_cap(self):
        row = {"id": "x", "blob": "a" * (CAP + 10)}
        size = _line_bytes(row)
        with eventledger.locked(self.path) as held:
            self.assertTrue(held)
            ok, why = eventledger.append_unlocked_checked(self.path, row)
            self.assertFalse(eventledger.append_unlocked(self.path, row))
        self.assertFalse(ok)
        self.assertIn(str(size), why)
        self.assertIn(str(CAP), why)
        self.assertFalse(os.path.exists(self.path),
                         "a refused event still created the ledger")

    def test_b_a_short_write_is_named_and_rolled_back(self):
        real = os.write
        with eventledger.locked(self.path) as held:
            self.assertTrue(held)
            with mock.patch.object(eventledger.os, "write",
                                   side_effect=lambda fd, b: real(fd, b[:-1])):
                ok, why = eventledger.append_unlocked_checked(
                    self.path, {"id": "y", "v": 1})
        self.assertFalse(ok)
        self.assertIn("short write", why)
        self.assertIn("rolled back", why)
        self.assertEqual(os.path.getsize(self.path), 0)

    def test_F4_a_task_row_over_the_cap_is_refused_naming_size_cap_and_the_field(self):
        tid = self.file_task()
        before = self.ledger_bytes()
        self.assertTrue(before)
        files = self.store_files()
        row, err = tasks.update(tid, path=self.path, note="n" * (CAP + 500))
        self.assertIsNone(row)
        size = re.search(r"row is (\d+) bytes", err)
        self.assertIsNotNone(size, err)
        self.assertGreater(int(size.group(1)), CAP)
        self.assertIn("at most %d bytes" % CAP, err)
        self.assertIn("note", err)
        self.assertEqual(self.ledger_bytes(), before)
        self.assertEqual(self.store_files(), files,  # noqa: VACUOUS_ASSERTION — the store must be exactly as it was; the refusal text above is the positive control
                         "a refused row left an archive behind")

    def test_F4_a_failing_disk_is_named_with_the_row_size_and_the_cap(self):
        tid = self.file_task()
        before = self.ledger_bytes()
        self.assertTrue(before)
        with mock.patch.object(eventledger.os, "fsync",
                               side_effect=OSError(errno.EIO,
                                                   "Input/output error")):
            row, err = tasks.comment(tid, "a comment the disk will not keep",
                                     by="seat-a", path=self.path)
        self.assertIsNone(row)
        self.assertIn("Input/output error", err)
        self.assertRegex(err, r"row is \d+ bytes")
        self.assertIn("at most %d bytes" % CAP, err)
        self.assertEqual(self.ledger_bytes(), before)

    def test_F4_a_store_that_cannot_take_the_archive_is_named_and_nothing_is_appended(self):
        tid = self.file_task()
        self.near_cap_row(tid, 35)
        before = self.ledger_bytes()
        self.assertTrue(before)
        with mock.patch.object(refstore, "write",
                               return_value=(None, 0, "no space left (planted)")):
            row, err = tasks.comment(tid, "c-035 one too many", by="seat-a",
                                     path=self.path)
        self.assertIsNone(row)
        self.assertIn("no space left (planted)", err)
        self.assertRegex(err, r"row is \d+ bytes")
        self.assertIn("at most %d bytes" % CAP, err)
        self.assertEqual(self.ledger_bytes(), before)


class ALostArchiveIsLoudTest(RowBase):
    """(c) and F3 — every reader proves each archive, and says so when it can't."""

    def spilled(self):
        tid = self.file_task()
        written = self.grow(tid, 30, size=1500)
        row = self.latest(tid)
        self.assertTrue(row.get("comment_archives"), "30 comments never spilled")
        archived = sum(a["count"] for a in row["comment_archives"])
        return tid, written, row, archived

    def loud_everywhere(self, tid, kind, archived, inline):
        """The one LOUD line `task show` printed, after checking the same loss
        is loud on show --json, list --json and the web card too."""
        marks, out, err = self.shown(tid)
        loud = [ln for ln in out.splitlines() if tasks.ARCHIVE_BROKEN_MARK in ln]
        self.assertEqual(len(loud), 1, out[-3000:])
        self.assertIn(kind, loud[0])
        self.assertIn("%d comments" % archived, loud[0])
        self.assertIn("NOT complete", err)
        self.assertEqual(marks, inline, "the comments still on the row vanished")
        _m, js, _e = self.shown(tid, "--json")
        self.assertIn(kind, json.loads(js)["comments"][0]["text"])
        rc, listing, lerr = self.cli("list", "--json", "--all", "--all-projects")
        self.assertEqual(rc, 0, lerr)
        row = [r for r in json.loads(listing) if r["id"] == tid][0]
        self.assertIn(tasks.ARCHIVE_BROKEN_MARK, row["comments"][0]["text"])
        notes, status = web_core._api_task_notes({"id": [tid]})
        self.assertEqual(status, 200)
        self.assertTrue(notes["comments"][0].get("unreadable"))
        self.assertIn(kind, notes["comments"][0]["text"])
        return loud[0]

    def test_c_a_deleted_archive_is_reported_by_show_json_list_and_the_card(self):
        tid, written, row, archived = self.spilled()
        ref = row["comment_archives"][0]
        os.unlink(refstore.path_of(ref["digest"]))
        loud = self.loud_everywhere(tid, "MISSING", ref["count"],
                                    written[archived:])
        self.assertIn("could not be read", loud)

    def test_c_an_altered_archive_is_reported_as_a_digest_mismatch(self):
        tid, written, row, archived = self.spilled()
        ref = row["comment_archives"][0]
        path = refstore.path_of(ref["digest"])
        with open(path, encoding="utf-8") as f:
            body = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(body.replace("o-000", "o-999", 1))
        loud = self.loud_everywhere(tid, "DIGEST MISMATCH", ref["count"],
                                    written[archived:])
        self.assertIn("NOT the archive this row wrote", loud)

    def test_F3_an_intact_archive_resolves_inline_on_show_json_list_and_the_card(self):
        tid, written, _row, _archived = self.spilled()
        marks, out, err = self.shown(tid)
        self.assertIn("o-000", marks)
        self.assertEqual(marks, written)
        self.assertNotIn(tasks.ARCHIVE_BROKEN_MARK, out)
        self.assertEqual(err, "")
        _m, js, _e = self.shown(tid, "--json")
        self.assertIn("o-000", js)
        self.assertEqual([MARK.search(c["text"]).group(1)
                          for c in json.loads(js)["comments"]], written)
        rc, listing, lerr = self.cli("list", "--json", "--all", "--all-projects")
        self.assertEqual(rc, 0, lerr)
        row = [r for r in json.loads(listing) if r["id"] == tid][0]
        self.assertGreater(len(row["comments"]), 20)
        self.assertEqual(len(row["comments"]), len(written))
        notes, status = web_core._api_task_notes({"id": [tid]})
        self.assertEqual(status, 200)
        self.assertEqual([MARK.search(c["text"]).group(1)
                          for c in notes["comments"]], written)

    def test_c_one_comment_too_big_for_any_row_is_stored_whole_and_loud_when_lost(self):
        tid = self.file_task()
        tail = "THE-LAST-LINE-OF-A-LONG-COMMENT"
        text = "c-000 " + "w" * (tasks.COMMENT_TEXT_MAX + 5000) + tail
        row = self.say(tid, text)
        entry = row["comments"][-1]
        self.assertIn(tasks.COMMENT_REF_MARK, entry["text"])
        self.assertNotIn(tail, entry["text"])
        _m, out, _err = self.shown(tid)
        self.assertIn(tail, out)
        os.unlink(refstore.path_of(entry["text_digest"]))
        _m, out, err = self.shown(tid)
        self.assertNotIn(tail, out)
        self.assertIn(tasks.COMMENT_FILE_BROKEN_MARK, out)
        self.assertIn("BOUNDED copy", out)
        self.assertIn("NOT complete", err)

    def test_F3_the_mirror_still_sees_a_human_comment_that_was_archived(self):
        tid = self.file_task()
        self.say(tid, "o-000 a human said this first " + "h" * 9000,
                 by="seat-b")
        self.grow(tid, 40, size=1000, start=1, by=tasksmirror.MIRROR_ACTOR)
        row = self.latest(tid)
        self.assertEqual({c["by"] for c in row["comments"]},
                         {tasksmirror.MIRROR_ACTOR},
                         "the human comment is still inline, so this arm "
                         "would not be about an archived one")
        self.assertIn("commented", tasksmirror.touches(row))


class TheFoldStillAgreesTest(RowBase):
    """(d) — replay yields the same latest row, and nothing was lost on the way."""

    def test_d_replaying_the_ledger_yields_the_row_the_last_write_returned(self):
        tid = self.file_task()
        written = self.grow(tid, 80)
        last = self.say(tid, "o-080 the last word")
        written.append("o-080")
        folded = {}
        for line in self.ledger_bytes().splitlines():
            ev = json.loads(line)
            folded[ev["id"]] = ev
        self.assertEqual(folded[tid], last)
        self.assertEqual(self.latest(tid), last)
        self.assertGreater(len(last["comment_archives"]), 1)
        self.assertEqual([MARK.search(c["text"]).group(1)
                          for c in tasks.comments_of(last)], written)
        self.assertEqual(tasks.comment_count(last), len(written))


class TheArchiveIsDurableBeforeTheRowTest(RowBase):
    """F1 and F2 — the archive is on disk before any row names it, and nothing
    that reaps files ever reaches the store it lives in."""

    def test_F1_the_archive_is_fsynced_before_the_row_that_names_it_is_appended(self):
        tid = self.file_task()
        self.near_cap_row(tid, 35)
        events = []
        real_sync, real_append = refstore._fsync_path, \
            eventledger.append_unlocked_checked

        def sync(path, directory_=False):
            events.append(("fsync", path))
            return real_sync(path, directory_=directory_)

        def append(path, row):
            events.append(("append", row.get("id")))
            return real_append(path, row)

        with mock.patch.object(refstore, "_fsync_path", side_effect=sync), \
                mock.patch.object(eventledger, "append_unlocked_checked",
                                  side_effect=append):
            row = self.say(tid, "c-035 the comment that spills")
        self.assertTrue(row["comment_archives"])
        self.assertIn(("append", tid), events)
        path = refstore.path_of(row["comment_archives"][-1]["digest"])
        at = events.index(("append", tid))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600,
                         "an archive of a private ledger was left readable")
        self.assertLess(events.index(("fsync", path)), at, events)
        self.assertLess(events.index(("fsync", os.path.dirname(path))), at,
                        events)

    def test_F1_a_crash_before_the_row_leaves_only_an_unreferenced_orphan(self):
        tid = self.file_task()
        self.near_cap_row(tid, 35)
        before, files = self.ledger_bytes(), self.store_files()
        self.assertTrue(before)
        with mock.patch.object(eventledger, "append_unlocked_checked",
                               return_value=(False, "killed before the row")):
            row, err = tasks.comment(tid, "c-035 never lands", by="seat-a",
                                     path=self.path)
        self.assertIsNone(row)
        self.assertIn("killed before the row", err)
        self.assertEqual(self.ledger_bytes(), before)
        orphans = self.store_files() - files
        self.assertEqual(len(orphans), 1, orphans)
        self.assertNotIn(sorted(orphans)[0][:32].encode(), before)

    def test_F2_no_gc_stream_reaps_the_store_even_a_stale_orphan(self):
        tid = self.file_task()
        self.near_cap_row(tid, 35)
        row = self.say(tid, "c-035 the comment that spills")
        kept = refstore.path_of(row["comment_archives"][0]["digest"])
        orphan = refstore.path_of(refstore.write("an orphan nobody names")[0])
        stale = os.path.getmtime(kept) - 3 * gc.DAY
        for p in (kept, orphan):
            os.utime(p, (stale, stale))
        declared = [r for r in registry.projections()
                    if r["name"] == "dispatch-briefs"]
        self.assertEqual([r["kind"] for r in declared], ["authored"])
        cache = os.path.join(self.tmp, "cache")
        os.makedirs(cache)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.tmp)
        with mock.patch.dict(os.environ, {"HELM_CACHE_DIR": cache}), \
                mock.patch("helm.work.gc_orphans", return_value=[]):
            reached = [v for r in gc.scan() for v in r["victims"]
                       if str(v).startswith(refstore.directory())]
            out = io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                gc.cmd_gc(["--apply"])
        self.assertEqual(reached, [])
        self.assertTrue(os.path.exists(kept), "gc reaped a referenced archive")
        self.assertTrue(os.path.exists(orphan), "gc reaped the store")
        self.assertEqual(len(tasks.comments_of(self.latest(tid))), 36)


class TheArchiveListStaysBoundedTest(RowBase):
    """(e) THE LIST OF ARCHIVES IS BOUNDED TOO. `comment_archives` gained one
    entry per spill and nothing took one away, so a row that kept spilling
    carried a list that grew without end, back toward the cap the archives
    exist to keep it under. Every ARCHIVE_FANOUT adjacent entries of one
    level fold into ONE index entry a level up, whose file names them, so no
    level holds a full run; `comments_of` still reads every comment back, in
    order, through the indexes. Scaled down (a 4 KB row budget, a fan-out of
    3) so some 150 comments reach the second index level."""

    FANOUT = 3

    def setUp(self):
        super().setUp()
        for name, value in (("ROW_BUDGET", 4096), ("INLINE_KEEP", 1024),
                            ("ARCHIVE_FANOUT", self.FANOUT)):
            patch = mock.patch.object(tasks, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def level(ref):
        return ref.get("index", 0)

    def leaves(self, refs):
        """Every leaf archive under `refs`, oldest first, walked here straight
        off the store rather than through the reader under test."""
        out = []
        for ref in refs:
            if self.level(ref):
                with open(refstore.path_of(ref["digest"]),
                          encoding="utf-8") as f:
                    out += self.leaves(json.load(f)["archives"])
            else:
                out.append(ref)
        return out

    def assert_bounded(self, refs):
        levels = [self.level(r) for r in refs]
        self.assertEqual(levels, sorted(levels, reverse=True),
                         "an older entry sits below a newer one: %r" % levels)
        for lvl in set(levels):
            self.assertLess(levels.count(lvl), self.FANOUT,
                            "level %d holds a full run: %r" % (lvl, levels))

    def deep(self, count=150):
        """A row grown through the real comment door, its archive list
        checked after every write -> (tid, marks written, latest row)."""
        tid = self.file_task()
        written = []
        for i in range(count):
            written += self.grow(tid, 1, size=300, start=i,
                                 by="seat-a" if i % 2 else "seat-b")
            self.assert_bounded(self.latest(tid).get("comment_archives") or ())
        row = self.latest(tid)
        self.assertGreaterEqual(
            max(self.level(r) for r in row["comment_archives"]), 2,
            "the row never reached a second index level: %r"
            % row["comment_archives"])
        return tid, written, row

    def marks(self, comments):
        """Each comment's marker; a line with none (a loud one) reads as
        its opening words, so it fails the comparison instead of raising."""
        return [(MARK.search(c["text"]) or re.match(r"(.{0,40})", c["text"]))
                .group(1) for c in comments]

    def test_e_a_row_that_keeps_spilling_carries_a_bounded_archive_list(self):  # noqa: VACUOUS_ASSERTION — the empty stderr sits beside the unconditional marks == written on the same show call
        tid, written, row = self.deep()
        # EVERY COMMENT, IN THE ORDER WRITTEN, read back through the indexes
        self.assertEqual(self.marks(tasks.comments_of(row)), written)
        self.assertEqual(tasks.comment_count(row), len(written))
        self.assertEqual(tasks.comment_authors(row), {"seat-a", "seat-b"})
        # the leaves the indexes name hold every comment the row does not
        leaves = self.leaves(row["comment_archives"])
        self.assertGreater(len(leaves), len(row["comment_archives"]))
        self.assertEqual(sum(a["count"] for a in leaves)
                         + len(row["comments"]), len(written))
        marks, out, err = self.shown(tid)
        self.assertEqual(marks, written)
        self.assertEqual(err, "")
        self.assertIn("in %d files" % len(leaves), out)
        _m, js, _e = self.shown(tid, "--json")
        self.assertEqual(self.marks(json.loads(js)["comments"]), written)

    def test_e_a_lost_index_is_one_loud_entry_for_every_comment_under_it(self):
        tid, written, row = self.deep()
        top = row["comment_archives"][0]
        self.assertGreaterEqual(self.level(top), 2)
        os.unlink(refstore.path_of(top["digest"]))
        whole = tasks.comments_of(self.latest(tid))
        self.assertTrue(whole[0].get("unreadable"))
        self.assertIn("MISSING", whole[0]["text"])
        self.assertIn("%d comments" % top["count"], whole[0]["text"])
        self.assertEqual(self.marks(whole[1:]), written[top["count"]:])
        _m, _out, err = self.shown(tid)
        self.assertIn("NOT complete", err)

    def test_e_an_altered_index_is_a_digest_mismatch_never_a_silent_gap(self):
        tid, written, row = self.deep()
        top = row["comment_archives"][0]
        path = refstore.path_of(top["digest"])
        with open(path, encoding="utf-8") as f:
            body = json.load(f)
        body["archives"] = body["archives"][1:]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(body, f)
        whole = tasks.comments_of(self.latest(tid))
        self.assertTrue(whole[0].get("unreadable"))
        self.assertIn("DIGEST MISMATCH", whole[0]["text"])
        self.assertEqual(self.marks(whole[1:]), written[top["count"]:])

    def test_e_a_lost_leaf_under_an_index_is_loud_for_its_own_comments_only(self):
        tid, written, row = self.deep()
        leaves = self.leaves(row["comment_archives"])
        lost, before = leaves[1], leaves[0]["count"]
        os.unlink(refstore.path_of(lost["digest"]))
        whole = tasks.comments_of(self.latest(tid))
        self.assertEqual(self.marks(whole[:before]), written[:before])
        self.assertTrue(whole[before].get("unreadable"))
        self.assertIn("%d comments" % lost["count"], whole[before]["text"])
        self.assertEqual(self.marks(whole[before + 1:]),
                         written[before + lost["count"]:])
