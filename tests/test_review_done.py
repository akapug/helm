#!/usr/bin/env python3
"""`helm review done`, the corrected command, and the two misroutes (task/3382).

Measured over 40.5 hours of the three local seats: 62% of their hand-backs
and verdicts FAILED (156 of 250), almost all on typing what the row already
knew, and 14 verdicts landed on the seat's own outgoing row while 7
hand-backs were addressed to their sender.

EVERY CELL OF THE SURFACE IS AN ARM, and each refusal arm carries its
positive control in the same method: the corrected command it prints is
PASTED — split exactly as a shell would split it and run — and must record.
A corrected line that cannot be pasted is the failure this lane exists to
end, so "the line names the id" is never the whole assertion.

The fixtures come from `tests.test_dispatches`: `DispatchBase` is the shared
scratch home, scratch repository and author-proof fixture every dispatch arm
uses. Rows are sent by `integrator` to `seat-b`; the read is recorded AS
`seat-b`, the row's recipient.
"""
import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import mock

from helm import cli, delegate_grant, dispatches, eventledger, review_done, tasks
from tests import test_dispatches as td

run = td.run
READER = "seat-b"
#: A value that is shell in every way a paste could run it.
EVIL = "x'y \"z\" $(touch pwned1) `touch pwned2`; touch pwned3 & |p >w <r"


def pasted(case, line):
    """(argv, created): what BASH hands `helm` when `line` is pasted, run in
    a scratch directory with `helm` stubbed to print its arguments, and the
    files the paste created there. A line of quoted words runs one command
    and creates nothing."""
    tmp = tempfile.mkdtemp(prefix="helm-test-paste-")
    case.addCleanup(shutil.rmtree, tmp, True)
    p = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c",
         'helm() { printf "%s\\0" "$@"; }\n' + line + "\n"],
        cwd=tmp, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    case.assertEqual(p.returncode, 0, p.stderr)
    return p.stdout.split("\0")[:-1], sorted(os.listdir(tmp))


HOLD_FIELDS = ("event", "v", "reason", "owner_gated", "source_clean_tip",
               "hold_actor")
VERDICT_FIELDS = ("event", "v", "reviewed_tip", "verdict_ref", "polarity",
                  "basis", "gate", "gate_caps", "finding_count",
                  "prior_relation", "declared_unknown", "exit_answer",
                  "worse_than_main_paths", "patch_tip", "patch_author",
                  "no_patch_because", "verdict_author_session")


class ReviewDoneFindingForwardingTest(td.DispatchBase):
    def test_fix_forwards_every_finding_carried_id_and_note(self):
        read = SimpleNamespace(
            row={"id": "a" * 32, "tip": self.b}, outcome="fix",
            evidence="the read found work",
            opts={"--finding-count": ["2"], "--prior-relation": ["new"],
                  "--finding": ["first defect", "second defect"],
                  "--finding-carried": ["task/77"],
                  "--note": ["an observation", "another observation"],
                  "--no-patch-because": ["design needs the task meld"]})
        argv = review_done._door_argv(read, self.b)
        for flag, values in (("--finding", ["first defect", "second defect"]),
                             ("--finding-carried", ["task/77"]),
                             ("--note", ["an observation", "another observation"])):
            self.assertTrue(review_done.VALUED[flag])
            self.assertIn(flag, review_done.NOT_CLEAN)
            self.assertIn(flag, review_done.USAGE)
            self.assertEqual([argv[i + 1] for i, word in enumerate(argv[:-1])
                              if word == flag], values)
        line, _note = review_done._Read(
            read.row["id"], "fix", read.evidence, read.opts).line()
        self.assertIn("--finding 'first defect'", line)
        self.assertIn("--finding-carried task/77", line)
        self.assertIn("--note 'another observation'", line)


class ReviewDoneBase(td.DispatchBase):

    def setUp(self):
        super().setUp()
        self.review_task, err = tasks.add("fixture reviewed work", "author",
                                          project="helm-test", force_new=True)
        self.assertIsNone(err, err)

    def test_done_files_two_findings_then_carries_one_with_a_note(self):
        first = self.row()
        rc, _out, err = self.done(
            first["id"], "fix", "the read found two defects",
            "--finding", "the retry drops the lock",
            "--finding", "the cap is off by one",
            "--prior-relation", "new", "--worse-than-main", "helm/x.py",
            "--no-patch-because", "design needs a meld")
        self.assertEqual(rc, 0, err)
        filed = sorted((r for r in tasks.rows().values()
                        if r.get("found_in") == first["id"]),
                       key=lambda r: r["title"])
        self.assertEqual([r["title"] for r in filed],
                         ["the cap is off by one", "the retry drops the lock"])
        self.assertTrue(all(r["continues"] == self.review_task["id"]
                            for r in filed))
        second = self.row(ref=self.c, supersedes=first["id"])
        rc, _out, err = self.done(
            second["id"], "fix", "the retry still loses the lock",
            "--finding-carried", filed[1]["id"],
            "--prior-relation", "uncured", "--note", "the cap is now right",
            "--worse-than-main", "helm/x.py", "--no-patch-because",
            "design needs a meld")
        self.assertEqual(rc, 0, err)
        self.assertEqual(dispatches.snapshot()[0][second["id"]][
            "findings_carried"], [filed[1]["id"]])
        self.assertIn("the cap is now right", tasks.rows()[
            self.review_task["id"]]["comments"][-1]["text"])

    def row(self, **kwargs):
        kwargs.setdefault("recipient", READER)
        kwargs.setdefault("ref", self.b)
        if "supersedes" not in kwargs:
            kwargs.setdefault("task", self.review_task["id"])
        return self.add(**kwargs)

    def as_seat(self, seat=READER):
        return mock.patch.dict(os.environ, {"HELM_CHAT_NAME": seat})

    def call(self, fn, argv, seat=READER, author=True):
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.as_seat(seat))
            if author:
                stack.enter_context(self.verdict_author())
            return run(fn, list(argv))

    def done(self, *argv, seat=READER, author=True):
        return self.call(review_done.cmd_review, ["done", *argv], seat, author)

    def dispatch(self, *argv, seat=READER, author=True):
        return self.call(dispatches.cmd_dispatch, argv, seat, author)

    def paste(self, line, seat=READER):
        """Run a corrected line exactly as a shell would split it."""
        argv = shlex.split(line)
        self.assertEqual(argv[0], "helm", line)
        verb = {"dispatch": dispatches.cmd_dispatch,
                "review": review_done.cmd_review}[argv[1]]
        return self.call(verb, argv[2:], seat)

    def corrected(self, err):
        """The ONE corrected line, which must be the last line printed."""
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertTrue(lines, "nothing was printed")
        self.assertTrue(lines[-1].startswith(review_done.CORRECTED), err)
        self.assertEqual(
            sum(line.startswith(review_done.CORRECTED) for line in lines), 1,
            "a refusal ends with exactly ONE corrected line: %s" % err)
        return lines[-1][len(review_done.CORRECTED):]

    def note(self, err):
        """The line printed just above the corrected line."""
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertGreater(len(lines), 1, err)
        return lines[-2].strip()

    def paste_refused(self, line, rid, *names, seat=READER):
        """Paste `line` AS PRINTED: refused, naming each placeholder it still
        carries, and nothing written to the row."""
        before = len([e for e in eventledger.events(dispatches.ledger_path())
                      if e.get("id") == rid])
        rc, _out, err = self.paste(line, seat=seat)
        self.assertEqual(rc, 2, err)
        self.assertIn("still a placeholder", err)
        for name in names:
            self.assertIn(shlex.quote(name), err)
        self.assertEqual(len([e for e in eventledger.events(
            dispatches.ledger_path()) if e.get("id") == rid]), before, err)
        return err

    def events(self, rid, kind):
        return [e for e in eventledger.events(dispatches.ledger_path())
                if e.get("id") == rid and e.get("event") == kind]

    def folded(self, rid):
        return dispatches.snapshot()[0][rid]

    def same_event(self, mine, theirs, kind, fields):
        """The one event each row carries, equal field for field."""
        left, right = self.events(mine, kind), self.events(theirs, kind)
        self.assertEqual((len(left), len(right)), (1, 1), (left, right))
        self.assertEqual(sorted(left[0]), sorted(right[0]))
        self.assertEqual({k: left[0].get(k) for k in fields},
                         {k: right[0].get(k) for k in fields})
        return left[0]


class DoneRecordsThroughTheExistingDoorsTest(ReviewDoneBase):
    """clean, concur and fix write the SAME event the existing door writes."""

    def test_clean_is_the_source_clean_hold_the_hold_door_writes(self):
        mine, theirs = self.row(), self.row()
        rc, out, err = self.done(mine["id"][:6], "clean",
                                 "read the delta; nothing found, Ran 5 tests OK")
        self.assertEqual(rc, 0, err)
        self.assertIn("SOURCE-CLEAN at %s" % self.b, out)
        rc, _out, err = self.dispatch("hold", theirs["id"], "--source-clean",
                                      self.b, "read the delta; nothing found, Ran 5 tests OK")
        self.assertEqual(rc, 0, err)
        event = self.same_event(mine["id"], theirs["id"], "hold", HOLD_FIELDS)
        self.assertEqual((event["source_clean_tip"], event["hold_actor"]),
                         (self.b, READER))
        folded = self.folded(mine["id"])
        self.assertEqual((folded["status"], folded["source_clean_tip"],
                          folded["hold_reason"]),
                         ("held", self.b, "read the delta; nothing found, Ran 5 tests OK"))

    def test_concur_is_the_verdict_door_concur_measured(self):
        mine, theirs = self.row(), self.row()
        rc, out, err = self.done(mine["id"][:6], "concur", "the design is sound")
        self.assertEqual(rc, 0, err)
        self.assertIn("VERDICT (CONCUR/MEASURED)", out)
        rc, _out, err = self.dispatch("verdict", theirs["id"], self.b,
                                      "--concur", "--measured",
                                      "the design is sound")
        self.assertEqual(rc, 0, err)
        event = self.same_event(mine["id"], theirs["id"], "verdict",
                                VERDICT_FIELDS)
        self.assertEqual((event["polarity"], event["basis"],
                          event["reviewed_tip"]), ("concur", "measured", self.b))

    def test_fix_without_a_patch_is_the_verdict_door_fix(self):  # noqa: VACUOUS_ASSERTION — the event is asserted equal field for field to the verdict door's own, and its polarity, counts and reason positively
        # THIS ARM PINNED `fix` FILLING `--finding-count 1 --prior-relation
        # new` (task/3382 F10): counts the seat never declared. It now
        # declares them, and the refusal without them is its own arm.
        mine, theirs = self.row(), self.row()
        rc, _out, err = self.done(
            mine["id"][:6], "fix", "the guard is inverted",
            "--finding-count", "1", "--prior-relation", "new",
            "--worse-than-main", "helm/x.py",
            "--finding", "the guard is inverted",
            "--no-patch-because", "a design finding for a meld")
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.dispatch(
            "verdict", theirs["id"], self.b, "--fix", "--measured",
            "--finding-count", "1", "--prior-relation", "new",
            "--worse-than-main", "helm/x.py",
            "--finding", "the guard is inverted",
            "--no-patch-because", "a design finding for a meld",
            "the guard is inverted")
        self.assertEqual(rc, 0, err)
        event = self.same_event(mine["id"], theirs["id"], "verdict",
                                VERDICT_FIELDS)
        self.assertEqual((event["polarity"], event["finding_count"],
                          event["prior_relation"], event["no_patch_because"]),
                         ("fix", 1, "new", "a design finding for a meld"))

    def test_fix_with_a_patch_prefix_records_the_full_cure(self):
        # `--prior-relation new` was filled beside the seat's own count
        # (task/3382 F10); the seat now declares it.
        mine, theirs = self.row(), self.row()
        rc, out, err = self.done(
            mine["id"][:6], "fix", "moved the port off a live socket",
            "--worse-than-main", "helm/x.py", "--patch-tip", self.c[:10],
            "--finding-count", "2", "--prior-relation", "new")
        self.assertEqual(rc, 0, err)
        self.assertIn("reviewer patch: %s" % self.c[:12], out)
        rc, _out, err = self.dispatch(
            "verdict", theirs["id"], self.b, "--fix", "--measured",
            "--finding-count", "2", "--prior-relation", "new",
            "--worse-than-main", "helm/x.py", "--patch-tip", self.c,
            "moved the port off a live socket")
        self.assertEqual(rc, 0, err)
        event = self.same_event(mine["id"], theirs["id"], "verdict",
                                VERDICT_FIELDS)
        self.assertEqual((event["patch_tip"], event["patch_author"],
                          event["finding_count"]), (self.c, READER, 2))

    def test_a_fix_the_door_refuses_is_refused_in_the_door_s_words(self):
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "fix", "the guard is inverted",
                                  "--worse-than-main", "helm/x.py")
        self.assertEqual(rc, 2, err)
        self.assertIn("A READER FIXES WHAT IT FINDS", err)
        self.assertEqual(self.folded(row["id"])["status"], "open")
        # THIS ARM PINNED `--no-patch-because '<REASON>'` (task/3382 F7): the
        # line chose "no cure" over a patch for the seat. Which cure answer,
        # and the counts (F10), are the seat's.
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s fix --finding-count '<N>' "
                         "--prior-relation '<new|uncured|regression-of-cure>' "
                         "--worse-than-main helm/x.py '<--patch-tip SHA|"
                         "--no-patch-because REASON>' -- 'the guard is "
                         "inverted'" % row["id"])
        rc, _out, err = self.paste(
            line.replace("'<N>'", "1")
            .replace("'<new|uncured|regression-of-cure>'", "new")
            .replace("'<--patch-tip SHA|--no-patch-because REASON>'",
                     "--no-patch-because 'a meld' --finding 'the guard is inverted'"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["status"], "verdict")

    def test_clean_takes_no_fix_flag(self):
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "clean", "nothing found, Ran 5 tests OK",
                                  "--patch-tip", self.c)
        self.assertEqual(rc, 2, err)
        self.assertIn("carries no --patch-tip", err)
        # REFUSED BEFORE THE ROW WAS READ: the line keeps the prefix typed.
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s clean -- 'nothing found, Ran 5 tests OK'"
                         % row["id"][:8])
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["status"], "held")


class ARowIdPrefixTest(ReviewDoneBase):
    """unique -> resolved; ambiguous -> refused listing the candidates;
    unknown -> refused."""

    def fake(self, *ids):
        return {rid: {"id": rid, "status": "open", "recipient": READER,
                      "sender": "integrator", "lane": "lane-%d" % n,
                      "tip": "a" * 40, "ts": "2026-09-26T00:00:0%dZ" % n}
                for n, rid in enumerate(ids)}

    def test_resolve_answers_every_shape(self):
        one, two, other = "abcd" + "1" * 28, "abcd" + "2" * 28, "ef01" + "3" * 28
        current = self.fake(one, two, other)
        row, why, _c = review_done.resolve(current, "EF01")
        self.assertEqual((row["id"], why), (other, None))
        row, why, candidates = review_done.resolve(current, "abcd")
        self.assertIsNone(row)
        self.assertIn("ambiguous row id prefix abcd: 2 rows", why)
        self.assertEqual([c["id"] for c in candidates], [two, one])
        row, why, candidates = review_done.resolve(current, "0123")
        self.assertEqual((row, why, candidates),
                         (None, "no row id starts with 0123", []))
        padded = other[:12] + "0" * 28
        row, why, candidates = review_done.resolve(current, padded)
        self.assertIsNone(row)
        self.assertIn(other, why)
        self.assertEqual([c["id"] for c in candidates], [other])
        for bad in ("abc", "xyz12345", ""):
            row, why, candidates = review_done.resolve(current, bad)
            self.assertEqual((row, candidates), (None, []), bad)
            self.assertIn("is not a row id", why)

    def test_an_ambiguous_prefix_is_refused_with_its_candidates(self):
        one, two = "abcd" + "1" * 28, "abcd" + "2" * 28
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(self.fake(one, two), None)):
            rc, _out, err = self.done("abcd", "concur", "the design is sound")
        self.assertEqual(rc, 2, err)
        self.assertIn("ambiguous row id prefix abcd", err)
        self.assertIn(one[:12], err)
        self.assertIn(two[:12], err)
        # THIS ARM PINNED `helm review done abcd concur ...` (task/3403): the
        # ambiguous prefix is the refusal's cause, so that line was refused
        # again for it, forever. The slot is its placeholder; the candidates
        # are named above the line.
        self.assertEqual(self.corrected(err),
                         "helm review done '<row-id-prefix>' concur -- 'the "
                         "design is sound'")

    def test_an_unknown_id_names_the_row_its_prefix_does(self):
        # THIS ARM PINNED THE CANDIDATE'S FULL ID IN THE COMMAND (task/3382
        # F8): an id that names no row is not evidence of which row was meant,
        # so the candidate is named ABOVE the line and the command carries
        # '<ROW_ID>'.
        row = self.row()
        pad = "1" if row["id"][12] == "0" else "0"
        rc, _out, err = self.done(row["id"][:12] + pad * 20, "concur", "fine")
        self.assertEqual(rc, 2, err)
        self.assertIn("no row id starts with", err)
        self.assertIn("was the id padded", err)
        self.assertEqual(self.folded(row["id"])["status"], "open")
        line = self.corrected(err)
        self.assertEqual(line, "helm review done '<ROW_ID>' concur -- fine")
        self.assertIn("did you mean %s" % row["id"], self.note(err))
        self.paste_refused(line, row["id"], "<ROW_ID>")
        rc, _out, err = self.paste(line.replace("'<ROW_ID>'", row["id"]))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["polarity"], "concur")


class EvidenceOverTheCapTest(ReviewDoneBase):
    """Refused with the count and the cap; never truncated."""

    def test_clean_evidence_over_the_cap_is_refused_whole(self):  # noqa: VACUOUS_ASSERTION — the same row's hold_reason is asserted RECORDED at the end of this method
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "clean", "x" * 300)
        self.assertEqual(rc, 1, err)
        self.assertIn("300 chars, 44 over the cap of at most 256", err)
        self.assertIn("nothing was truncated", err)
        self.assertEqual(self.events(row["id"], "hold"), [])
        self.assertEqual(self.corrected(err),
                         "helm review done %s clean -- '<evidence of at most "
                         "256 chars>'" % row["id"])
        full = "y" * 241 + " Ran 5 tests OK"
        self.assertEqual(len(full), 256)
        rc, _out, err = self.done(row["id"][:8], "clean", full)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["hold_reason"], full)

    def test_verdict_evidence_over_the_budget_is_refused_whole(self):  # noqa: VACUOUS_ASSERTION — the same row's verdict_ref is asserted RECORDED at the end of this method
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "concur", "z" * 300)
        self.assertEqual(rc, 1, err)
        self.assertIn("44 chars over the 256 budget", err)
        self.assertIn("300 chars of statement", err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        self.assertTrue(self.corrected(err).endswith(
            "concur -- '<evidence of at most 256 chars>'"), err)
        rc, _out, err = self.done(row["id"][:8], "concur", "z" * 256)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["verdict_ref"], "z" * 256)


class ARowYouSentTest(ReviewDoneBase):
    """A read of a row the seat SENT is refused and names the row on that
    chain addressed to it."""

    def chain(self):
        sent = self.row()
        with self.as_seat(READER):
            back = self.add(recipient="integrator", ref=self.c,
                            supersedes=sent["id"])
        return sent, back

    def test_done_on_a_row_you_sent_names_the_row_addressed_to_you(self):
        sent, back = self.chain()
        rc, _out, err = self.done(sent["id"][:8], "concur", "fine",
                                  seat="integrator")
        self.assertEqual(rc, 1, err)
        self.assertIn("you SENT dispatch %s" % sent["id"][:12], err)
        self.assertIn(back["id"][:12], err)
        self.assertEqual(self.events(sent["id"], "verdict"), [])
        # THE CHAIN SETTLES THE ROW, NEVER THE TREE (task/3382 F2). This arm
        # pinned `review done <back> concur fine`, whose paste bound the hand
        # back's tip c, a tree the seat's own command never named: its read
        # was of the row sent at b. The tip is now the seat's to type.
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s concur --tip "
                         "'<TIP_YOU_READ>' -- fine" % back["id"])
        self.assertIn("is now at %s" % self.c, self.note(err))
        self.paste_refused(line, back["id"], "<TIP_YOU_READ>",
                           seat="integrator")
        rc, _out, err = self.paste(line.replace("'<TIP_YOU_READ>'", self.c),
                                   seat="integrator")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(back["id"])["status"], "verdict")
        self.assertEqual(self.folded(back["id"])["reviewed_tip"], self.c)
        self.assertEqual(self.folded(sent["id"])["status"], "open")

    def test_the_verdict_door_names_it_too(self):
        sent, back = self.chain()
        rc, _out, err = self.dispatch("verdict", sent["id"], self.b,
                                      "--concur", "--measured", "fine",
                                      seat="integrator")
        self.assertEqual(rc, 1, err)
        self.assertIn("you SENT dispatch %s" % sent["id"][:12], err)
        self.assertIn(back["id"][:12], err)
        self.assertEqual(self.events(sent["id"], "verdict"), [])
        # The seat typed b, the tip of the row it SENT; the row addressed to
        # it is at c, so the line names no tip for it (task/3382 F2: this arm
        # pinned the row's tip c in the seat's place).
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s '<TIP_YOU_READ>' "
                         "--concur --measured -- fine" % back["id"])
        self.assertIn("is now at %s" % self.c, self.note(err))
        self.paste_refused(line, back["id"], "<TIP_YOU_READ>",
                           seat="integrator")
        rc, _out, err = self.paste(line.replace("'<TIP_YOU_READ>'", self.c),
                                   seat="integrator")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(back["id"])["polarity"], "concur")

    def test_with_nothing_addressed_to_you_the_proof_still_decides(self):
        """THE POSITIVE CONTROL for the misroute rung: with no row on the
        chain addressed to the sender, the verdict door is unchanged and the
        author proof (mocked here) decides, exactly as before."""
        sent = self.row()
        rc, _out, err = self.dispatch("verdict", sent["id"], self.b,
                                      "--concur", "--measured", "fine",
                                      seat="integrator")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(sent["id"])["polarity"], "concur")

    def test_a_third_seat_with_no_row_is_sent_to_its_own_list(self):
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "concur", "fine",
                                  seat="seat-c")
        self.assertEqual(rc, 1, err)
        self.assertIn("addressed to @%s, not to you" % READER, err)
        self.assertEqual(self.corrected(err), "helm dispatch list --mine --open")
        self.assertEqual(self.folded(row["id"])["status"], "open")


class TheTipMovedTest(ReviewDoneBase):
    """A row retipped since it was sent refuses as stale, with the verdict
    door's own sentence. The corrected command carries `<TIP_YOU_READ>` and
    the line above it names the row's current tip; it never binds that tip in
    the seat's place (task/3382 F2)."""

    def test_a_retipped_row_refuses_until_the_reader_names_the_tip(self):  # noqa: VACUOUS_ASSERTION — the same row's reviewed_tip is asserted RECORDED at the tip typed at the end of this method
        row = self.row(kind="build")
        moved, why = dispatches.retip(row["id"], self.c, reason="cured",
                                      repo=self.repo, notify=False)
        self.assertIsNone(why, why)
        self.assertEqual(moved["tip"], self.c)
        rc, _out, err = self.done(row["id"][:8], "concur", "fine")
        self.assertEqual(rc, 1, err)
        self.assertIn(dispatches.stale_tip_refusal(self.b, self.c), err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        # THIS ARM PINNED `--tip <c>`: a paste recorded a read of c, the tree
        # the seat had just been told it never read.
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s concur --tip "
                         "'<TIP_YOU_READ>' -- fine" % row["id"])
        self.assertIn("dispatch %s is now at %s" % (row["id"][:12], self.c),
                      self.note(err))
        self.paste_refused(line, row["id"], "<TIP_YOU_READ>")
        # the tip it once was is still stale ...
        rc, _out, err = self.paste(line.replace("'<TIP_YOU_READ>'", self.b))
        self.assertEqual(rc, 1, err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        # ... and the tip the reader read, once typed, is the one recorded.
        rc, _out, err = self.paste(line.replace("'<TIP_YOU_READ>'", self.c))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["reviewed_tip"], self.c)

    def test_a_tip_this_row_never_had_is_refused(self):
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "concur", "fine",
                                  "--tip", self.side[:10])
        self.assertEqual(rc, 2, err)
        self.assertIn("is no tip dispatch %s was ever at" % row["id"][:12], err)
        rc, _out, err = self.done(row["id"][:8], "concur", "fine",
                                  "--tip", self.b[:10])
        self.assertEqual(rc, 0, err)

    def test_the_verdict_door_stale_refusal_ends_at_a_tip_you_read(self):  # noqa: VACUOUS_ASSERTION — the same row's polarity and reviewed_tip are asserted RECORDED after the refused paste
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], self.c,
                                      "--concur", "--measured", "fine")
        self.assertEqual(rc, 1, err)
        self.assertIn(dispatches.stale_tip_refusal(self.c, self.b), err)
        # THIS ARM PINNED THE ROW'S TIP b IN THE SEAT'S c: the paste recorded
        # a verdict at a tip the seat never read.
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s '<TIP_YOU_READ>' "
                         "--concur --measured -- fine" % row["id"])
        self.assertIn("is now at %s" % self.b, self.note(err))
        self.paste_refused(line, row["id"], "<TIP_YOU_READ>")
        rc, _out, err = self.paste(line.replace("'<TIP_YOU_READ>'", self.b))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.folded(row["id"])["polarity"],
                          self.folded(row["id"])["reviewed_tip"]),
                         ("concur", self.b))

    def test_a_typed_prefix_of_the_row_tip_is_written_out_in_full(self):  # noqa: VACUOUS_ASSERTION — the same row's reviewed_tip is asserted RECORDED at the full tip after the paste
        """The one tip a corrected line fills: the seat's own, completed."""
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "concur", "z" * 300,
                                  "--tip", self.b[:9])
        self.assertEqual(rc, 1, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s concur --tip %s -- '<evidence "
                         "of at most 256 chars>'" % (row["id"], self.b))
        rc, _out, err = self.paste(line.replace(
            "'<evidence of at most 256 chars>'", "fine"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["reviewed_tip"], self.b)

    def test_a_source_clean_tip_outside_the_row_is_never_replaced(self):  # noqa: VACUOUS_ASSERTION — the same row's source_clean_tip is asserted RECORDED by the control paste at the end
        """hold: the typed tip is kept when the door would take it (a
        DESCENDANT of the row's tip is), and is `<TIP_YOU_READ>` when it is
        not — never the row's tip in its place."""
        row = self.row()
        rc, _out, err = self.dispatch("hold", row["id"], "--source-clean",
                                      self.side, "nothing found, Ran 5 tests OK")
        self.assertEqual(rc, 1, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch hold %s --source-clean "
                         "'<TIP_YOU_READ>' -- 'nothing found, Ran 5 tests OK'" % row["id"])
        self.assertIn("is now at %s" % self.b, self.note(err))
        self.paste_refused(line, row["id"], "<TIP_YOU_READ>")
        # THE CONTROL: a descendant the seat typed is kept, resolved in full.
        rc, _out, err = self.dispatch("hold", row["id"], "--source-clean",
                                      self.c[:10], "r" * 300)
        self.assertEqual(rc, 1, err)
        line = self.corrected(err)
        self.assertIn("--source-clean %s --" % self.c, line)
        rc, _out, err = self.paste(line.replace(
            "'<reason of at most 256 chars>'", "'nothing found, Ran 5 tests OK'"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["source_clean_tip"], self.c)


class EveryRefusalEndsWithTheCorrectedCommandTest(ReviewDoneBase):
    """usage, polarity, full sha, family proof: still refused, and each ends
    with ONE line that pastes."""

    def test_send_flags_mixed_into_a_verdict(self):
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"][:8], row["tip"],
                                      "--concur", "--measured", "--kind",
                                      "review", "fine")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s %s --concur "
                         "--measured -- fine" % (row["id"], self.b))
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["polarity"], "concur")

    def test_a_missing_polarity(self):
        """THE LINE NEVER PICKS A DIRECTION (task/3382 F2). It filled `--fix`
        and `--finding-count 1 --prior-relation new`, so a paste recorded a
        hand-back and a count the seat never declared."""
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"][:8], row["tip"],
                                      "--measured", "the guard is inverted")
        self.assertEqual(rc, 2, err)
        self.assertIn("DECLARE the polarity", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s %s '<--concur|--fix>' "
                         "--measured -- 'the guard is inverted'"
                         % (row["id"], self.b))
        self.paste_refused(line, row["id"], "<--concur|--fix>")
        rc, _out, err = self.paste(line.replace("'<--concur|--fix>'",
                                                "--concur"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["polarity"], "concur")

    def test_a_fix_owes_answers_only_the_seat_can_give(self):  # noqa: VACUOUS_ASSERTION — the same row's verdict event is asserted RECORDED field for field after the refused paste
        """--fix chosen: the counts, the exit answer and the cure answer the
        seat has not given are placeholders, and a paste that keeps any of
        them is refused naming each. THIS ARM PINNED `--worse-than-main
        '<PATH>' --no-patch-because '<REASON>'` (task/3382 F7): the line
        picked the blocking exit answer and "no cure" for the seat."""
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      "--fix", "--measured", "inverted")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s %s --fix --measured "
                         "--finding-count '<N>' --prior-relation "
                         "'<new|uncured|regression-of-cure>' "
                         "'<--worse-than-main PATH|--imperfect>' "
                         "'<--patch-tip SHA|--no-patch-because REASON>' -- "
                         "inverted" % (row["id"], self.b))
        self.paste_refused(line, row["id"], "<N>",
                           "<new|uncured|regression-of-cure>",
                           "<--worse-than-main PATH|--imperfect>",
                           "<--patch-tip SHA|--no-patch-because REASON>")
        rc, _out, err = self.paste(
            line.replace("'<N>'", "2")
            .replace("'<new|uncured|regression-of-cure>'", "new")
            .replace("'<--worse-than-main PATH|--imperfect>'",
                     "--worse-than-main helm/x.py")
            .replace("'<--patch-tip SHA|--no-patch-because REASON>'",
                     "--no-patch-because 'a meld' --finding 'the guard is inverted' "
                     "--finding 'the inversion bypasses the lock'"))
        self.assertEqual(rc, 0, err)
        event = self.events(row["id"], "verdict")[0]
        self.assertEqual((event["polarity"], event["finding_count"],
                          event["prior_relation"], event["no_patch_because"]),
                         ("fix", 2, "new", "a meld"))

    def test_a_missing_basis(self):
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      "--concur", "the design is sound")
        self.assertEqual(rc, 2, err)
        self.assertIn("DECLARE HOW YOU KNOW", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s %s --concur "
                         "'<--measured|--inferred>' -- 'the design is sound'"
                         % (row["id"], self.b))
        self.paste_refused(line, row["id"], "<--measured|--inferred>")
        rc, _out, err = self.paste(line.replace("'<--measured|--inferred>'",
                                                "--inferred"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.events(row["id"], "verdict")[0]["basis"],
                         "inferred")

    def test_a_short_reviewed_sha(self):
        # Composed with task/3382 L3 (typed ids): a unique prefix of the
        # row's tip RESOLVES at the verdict door and is bound as the full
        # sha, so it is no longer a refusal and prints no corrected line.
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"][:12],
                                      "--concur", "--measured", "fine")
        self.assertEqual(rc, 0, err)
        self.assertNotIn(review_done.CORRECTED, err)
        folded = self.folded(row["id"])
        self.assertEqual(folded["polarity"], "concur")
        self.assertEqual(folded["reviewed_tip"], self.b)

    def test_the_family_proof_unavailable(self):
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      "--concur", "--measured", "fine",
                                      author=False)
        self.assertEqual(rc, 1, err)
        self.assertIn("family proof is unavailable", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s %s --concur "
                         "--measured -- fine" % (row["id"], self.b))
        # THE SAME LINE records once the proof exists (mocked here).
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["polarity"], "concur")

    def test_verdict_evidence_over_the_budget(self):
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      "--concur", "--measured", "e" * 300)
        self.assertEqual(rc, 1, err)
        self.assertIn("44 chars over the 256 budget", err)
        self.assertTrue(self.corrected(err).endswith(
            "-- '<evidence of at most 256 chars>'"), err)

    def test_a_hold_reason_over_the_cap(self):
        row = self.row()
        rc, _out, err = self.dispatch("hold", row["id"][:8], "--source-clean",
                                      row["tip"][:12], "r" * 300)
        self.assertEqual(rc, 1, err)
        self.assertIn("300 chars, 44 over the cap of at most 256", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch hold %s --source-clean %s -- "
                         "'<reason of at most 256 chars>'" % (row["id"], self.b))
        rc, _out, err = self.paste(line.replace(
            "'<reason of at most 256 chars>'", "'nothing found, Ran 5 tests OK'"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["source_clean_tip"], self.b)

    def test_a_send_missing_its_kind(self):
        rc, _out, err = self.dispatch("send", READER, "lane/kindless",
                                      "cure it", "--ref", self.b[:12],
                                      "--new-work", "--repo", self.repo,
                                      seat="integrator")
        self.assertEqual(rc, 2, err)
        self.assertIn("requires --kind", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch send %s lane/kindless 'cure it' "
                         "--ref %s --kind '<build|review>' --new-work --repo %s"
                         % (READER, self.b, shlex.quote(self.repo)))
        rc, _out, err = self.paste(line.replace("'<build|review>'", "build"),
                                   seat="integrator")
        self.assertEqual(rc, 0, err)
        minted = [r for r in dispatches.snapshot()[0].values()
                  if r.get("lane") == "kindless"]
        self.assertEqual([(r["recipient"], r["kind"], r["tip"]) for r in minted],
                         [(READER, "build", self.b)])

    def test_a_send_missing_work_identity_does_not_choose_new_work(self):
        before = sorted(dispatches.snapshot()[0])
        rc, _out, err = self.dispatch(
            "send", READER, "lane/identity", "cure it", "--ref", self.b,
            "--kind", "build", "--repo", self.repo, seat="integrator")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertIn("'<--new-work|--supersedes ROW>'", line)
        self.paste_refused(line, "not-a-row",
                           "<--new-work|--supersedes ROW>", seat="integrator")
        self.assertEqual(sorted(dispatches.snapshot()[0]), before)
        rc, _out, err = self.paste(
            line.replace("'<--new-work|--supersedes ROW>'", "--new-work"),
            seat="integrator")
        self.assertEqual(rc, 0, err)
        minted = [r for r in dispatches.snapshot()[0].values()
                  if r.get("lane") == "identity"]
        self.assertEqual([(r["recipient"], r.get("supersedes")) for r in minted],
                         [(READER, None)])

    def test_a_bodyless_send_line_leaves_a_refused_brief_placeholder(self):  # noqa: VACUOUS_ASSERTION — the paste's positive argv equality proves the placeholder reaches Helm as one inert word; created=[] proves no shell side effect
        with self.as_seat("integrator"):
            line = review_done.send_line([
                READER, "lane/bodyless", "--ref", self.b, "--kind", "build",
                "--new-work", "--repo", self.repo])
        self.assertIn("'<brief>'", line)
        self.assertIn("'<brief>' is still a placeholder",
                      review_done.unfilled_refusal(shlex.split(line)[3:]))
        argv, created = pasted(self, line)
        self.assertEqual((argv, created), (shlex.split(line)[1:], []))

    def test_the_send_line_carries_the_task_and_whole(self):
        with self.as_seat("integrator"):
            line = review_done.send_line([
                READER, "lane/carried-task", "cure it", "--ref", self.b,
                "--kind", "build", "--new-work", "--repo", self.repo,
                "--task", "task/7", "--whole"])
        words = shlex.split(line)
        self.assertEqual(words[words.index("--task") + 1], "task/7")
        self.assertEqual(words[-1], "--whole")

    def test_the_corrected_send_line_carries_a_part_flag(self):
        # (task/3938 round 3) the corrected line for a refused send that
        # carried --part must carry --part back, so the pasted line still
        # names the path the seat chose.
        with self.as_seat("integrator"):
            line = review_done.send_line([
                READER, "lane/parted-task", "cure the piece",
                "--ref", self.b,
                "--kind", "build", "--new-work", "--repo", self.repo,
                "--task", "task/7", "--part"])
        words = shlex.split(line)
        self.assertEqual(words[words.index("--task") + 1], "task/7")
        self.assertEqual(words[-1], "--part")

    def test_a_long_send_body_stays_one_quoted_argument(self):
        body = "x" * 301 + EVIL
        rc, _out, err = self.dispatch(
            "send", READER, "lane/long-body", body, "--ref", self.b,
            "--new-work", "--repo", self.repo, seat="integrator")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        words = shlex.split(line)
        self.assertIn(body, words)
        argv, created = pasted(self, line)
        self.assertEqual((argv, created), (words[1:], []))
        rc, _out, err = self.paste(
            line.replace("'<build|review>'", "build"), seat="integrator")
        self.assertEqual(rc, 0, err)
        row = next(r for r in dispatches.snapshot()[0].values()
                   if r.get("lane") == "long-body")
        self.assertIn("x" * 301, row.get("message_body") or "")

    def test_a_send_already_recorded_prints_no_corrected_line(self):  # noqa: VACUOUS_ASSERTION — the absence is paired in this method with an unconditional exact count of the one row both sends name
        argv = ("send", READER, "lane/twice", "build it", "--ref", self.b,
                "--kind", "build", "--new-work", "--repo", self.repo)
        first = self.dispatch(*argv, seat="integrator")
        second = self.dispatch(*argv, seat="integrator")
        for rc, _out, err in (first, second):
            self.assertNotIn(review_done.CORRECTED, err)
        rows = [r for r in dispatches.snapshot()[0].values()
                if r.get("lane") == "twice"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(second[0], 1, second)

    def test_an_unresolved_patch_tip_is_one_quoted_shell_word(self):  # noqa: VACUOUS_ASSERTION — the bash paste's argv is asserted EQUAL to the line's words and to carry the value before the absences
        # THE ONE SEAT-TYPED VALUE THE LINE DID NOT QUOTE: a --patch-tip that
        # does not resolve is echoed as given, so `;` ran a second command on
        # paste and the line's own '<FULL_SHA>' came back as a redirect.
        row = self.row()
        bad = "abc;touch pwned"
        rc, _out, err = self.dispatch(
            "verdict", row["id"], row["tip"], "--fix", "--measured",
            "--finding-count", "1", "--prior-relation", "new",
            "--worse-than-main", "helm/x.py", "--patch-tip", bad, "--", "ev")
        self.assertEqual(rc, 1, err)
        line = self.corrected(err)
        # THIS ARM PINNED THE VALUE ECHOED, quoted (task/3403): it is not hex,
        # so the door refuses it for what it is and the echoed line was
        # refused again, forever. The line now carries the quoted
        # placeholder, and never the value.
        self.assertIn("--patch-tip '<FULL_SHA>' ", line)
        # PASTED INTO BASH, it runs one command and creates nothing.
        argv, created = pasted(self, line)
        self.assertEqual((argv, created), (shlex.split(line)[1:], []))
        self.assertIn("<FULL_SHA>", argv)
        self.assertNotIn(bad, argv)
        # A placeholder pasted as a value is refused naming it (task/3382
        # F2): this arm pinned `<FULL_SHA>` reaching the door, which refused
        # it only because it resolved to no commit.
        rc, _out, err = self.done(row["id"][:8], "fix", "ev",
                                  "--worse-than-main", "helm/x.py",
                                  "--patch-tip", "<FULL_SHA>")
        self.assertEqual(rc, 2, err)
        self.assertIn("'<FULL_SHA>' is still a placeholder", err)
        self.assertIn("--patch-tip '<FULL_SHA>' -- ev", self.corrected(err))
        self.assertEqual(self.events(row["id"], "verdict"), [])

    def test_only_the_three_doors_are_corrected(self):
        self.assertIsNone(review_done.corrected(["cancel", "abcd1234", "r"]))
        self.assertEqual(review_done.CORRECTED_VERBS,
                         ("send", "verdict", "hold"))


class SendToYourselfTest(ReviewDoneBase):
    """send refuses recipient == the sending seat and names the row's
    sender as the intended recipient."""

    def test_a_hand_back_to_yourself_names_the_sender(self):
        sent = self.row(lane="lane-hand")
        before = sorted(dispatches.snapshot()[0])
        rc, _out, err = self.dispatch(
            "send", READER, "lane-hand", "the cure is on the lane",
            "--ref", self.c, "--kind", "build", "--supersedes",
            sent["id"][:12], "--repo", self.repo, author=False)
        self.assertEqual(rc, 1, err)
        self.assertIn("recipient @%s is the sending seat" % READER, err)
        self.assertIn("intended recipient is @integrator", err)
        self.assertEqual(sorted(dispatches.snapshot()[0]), before)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch send integrator lane-hand 'the "
                         "cure is on the lane' --ref %s --kind build "
                         "--supersedes %s --repo %s"
                         % (self.c, sent["id"], shlex.quote(self.repo)))
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 0, err)
        minted = [r for r in dispatches.snapshot()[0].values()
                  if r.get("supersedes") == sent["id"]]
        self.assertEqual([(r["recipient"], r["sender"]) for r in minted],
                         [("integrator", READER)])

    def test_the_lane_names_the_row_when_nothing_is_superseded(self):  # noqa: VACUOUS_ASSERTION — the corrected line is asserted EQUAL to the whole expected command and the note above it to name the row and its sender
        # THIS ARM PINNED `--supersedes <the lane's row>` IN THE COMMAND
        # (task/3382 F5): the send named no work arm, and which row it
        # answers — so who it is for — is the seat's choice. The refusal and
        # the line above the command name the row and its sender.
        sent = self.row(lane="lane-bare")
        rc, _out, err = self.dispatch(
            "send", READER, "lane-bare", "the cure is on the lane",
            "--ref", self.c, "--kind", "build", author=False)
        self.assertEqual(rc, 1, err)
        self.assertIn("intended recipient is @integrator", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch send '<recipient>' lane-bare "
                         "'the cure is on the lane' --ref %s --kind build "
                         "'<--new-work|--supersedes ROW>'" % self.c)
        self.assertIn(sent["id"], self.note(err))
        self.assertIn("@integrator", self.note(err))

    def test_the_library_door_refuses_too(self):
        with self.as_seat("integrator"):
            row, why, sent = dispatches.send(
                "integrator", "lane-self", "a note to myself", self.b,
                repo=self.repo, kind="build", new_work=True, sign=False)
            self.assertIsNone(row)
            self.assertFalse(sent)
            self.assertIn("is the sending seat", why)
            self.assertIn("no open row on lane lane-self", why)
            # THE POSITIVE CONTROL: the same call to another seat records.
            row, why, _sent = dispatches.send(
                READER, "lane-self", "a note to the reader", self.b,
                repo=self.repo, kind="build", new_work=True, sign=False)
        self.assertEqual(row["recipient"], READER)


class ADelegateIsRefusedTheVerbTest(ReviewDoneBase):
    """`review done` writes a verdict or a source-clean hold, so the
    argv-guard refuses it to a delegate exactly as it refuses the doors it
    fronts; without this it is a way around both."""

    def test_it_is_in_the_refused_table_and_its_usage_writes_nothing(self):
        from helm import chat, delegate_grant
        self.assertIn(("review", "done"), delegate_grant.REFUSED)
        self.assertIn(("review", "done"), delegate_grant.GRANTABLE)
        command = "helm review done 0123abcd fix 'x' --no-patch-because r"
        self.assertEqual(chat.sidechain_authority_verbs(command),
                         ("review done",))
        self.assertEqual(chat.sidechain_authority_verbs(
            "helm dispatch verdict 0123abcd %s --fix x" % ("a" * 40)),
            ("dispatch verdict",))
        self.assertTrue(delegate_grant.writes_nothing("review", "done", []))
        self.assertTrue(delegate_grant.writes_nothing("review", "done",
                                                      ["--help"]))
        self.assertTrue(delegate_grant.writes_nothing(
            "review", "done", ["0123abcd", "concur", "-h"]))
        self.assertFalse(delegate_grant.writes_nothing(
            "review", "done", ["0123abcd", "fix", "finding",
                               "--no-patch-because", "-h"]))
        self.assertFalse(delegate_grant.writes_nothing(
            "dispatch", "hold", ["0123abcd", "waiting", "--help"]))
        self.assertFalse(delegate_grant.writes_nothing(
            "review", "done", ["0123abcd", "concur", "fine"]))


class TheVerbIsDocumentedTest(ReviewDoneBase):
    """docs/VERBS.md and the CLI help name the verb, its subverb and every
    flag it accepts."""

    def test_the_verb_is_wired_and_every_flag_is_named(self):
        from helm import cli_help
        self.assertIn("review", cli.VERBS)
        entry = cli_help._VERB_HELP["review"]
        self.assertTrue(entry.startswith("review done <row-id-prefix> "), entry)
        for flag in list(review_done.VALUED) + list(review_done.BARE):
            self.assertIn(flag, entry)
            self.assertIn(flag, review_done.USAGE)
        doc = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "docs", "VERBS.md")
        with open(doc, encoding="utf-8") as fh:
            self.assertIn("### `helm review done <row-id-prefix>", fh.read())

    def test_help_and_an_unknown_subverb(self):
        rc, out, _err = run(review_done.cmd_review, ["done", "--help"])
        self.assertEqual((rc, out.strip()), (0, review_done.USAGE))
        rc, _out, err = run(review_done.cmd_review, ["zz-no-such"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown subverb 'zz-no-such'", err)

    def test_an_unknown_flag_is_refused_and_corrected(self):
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "concur", "--bogus", "fine")
        self.assertEqual(rc, 2, err)
        self.assertIn("'--bogus' is not the evidence", err)
        self.assertTrue(self.corrected(err).startswith(
            "helm review done %s concur" % row["id"][:8]), err)
        self.assertEqual(self.folded(row["id"])["status"], "open")


class APlaceholderIsNeverAValueTest(ReviewDoneBase):
    """task/3382 F2, the door half: `<PATH>`, `<REASON>`, `<FULL_SHA>`, a
    placeholder tip and every other `<...>` a corrected line leaves are
    REFUSED at the verdict, hold, send and review-done doors, naming each —
    measured before the cure, `--worse-than-main '<PATH>'` recorded
    worse_than_main_paths=('<PATH>',)."""

    def test_the_verdict_door_refuses_a_path_and_a_reason_left_unfilled(self):
        row = self.row()
        argv = ["verdict", row["id"], row["tip"], "--fix", "--measured",
                "--finding-count", "1", "--prior-relation", "new",
                "--worse-than-main", "<PATH>", "--no-patch-because",
                "<REASON>", "--", "the guard is inverted"]
        rc, _out, err = self.dispatch(*argv)
        self.assertEqual(rc, 2, err)
        self.assertIn("'<PATH>', '<REASON>' are still a placeholder", err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        self.assertIn("--worse-than-main '<PATH>' --no-patch-because "
                      "'<REASON>' --", self.corrected(err))
        # THE CONTROL, same argv with the two values typed: it records.
        rc, _out, err = self.dispatch(*[{"<PATH>": "helm/x.py",
                                         "<REASON>": "a meld"}.get(a, a)
                                        for a in argv[:-2]], "--finding",
                                      "the guard is inverted", *argv[-2:])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.events(row["id"], "verdict")[0]
                         ["worse_than_main_paths"], ["helm/x.py"])

    def test_the_verdict_door_refuses_a_full_sha_left_unfilled(self):
        row = self.row()
        rc, _out, err = self.dispatch(
            "verdict", row["id"], row["tip"], "--fix", "--measured",
            "--finding-count", "1", "--prior-relation", "new", "--imperfect",
            "--patch-tip", "<FULL_SHA>", "--", "cured in place")
        self.assertEqual(rc, 2, err)
        self.assertIn("'<FULL_SHA>' is still a placeholder", err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        # THE CONTROL, the cure's own commit typed: it records.
        rc, _out, err = self.dispatch(
            "verdict", row["id"], row["tip"], "--fix", "--measured",
            "--finding-count", "1", "--prior-relation", "new", "--imperfect",
            "--patch-tip", self.c, "--", "cured in place")
        self.assertEqual(rc, 0, err)
        self.assertEqual([e["patch_tip"] for e in
                          self.events(row["id"], "verdict")], [self.c])

    def test_review_done_refuses_them_and_names_the_row_in_full(self):  # noqa: VACUOUS_ASSERTION — the same row's reviewed_tip is asserted RECORDED after the filled paste
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "fix", "--worse-than-main",
                                  "<PATH>", "--no-patch-because", "<REASON>",
                                  "--tip", "<TIP_YOU_READ>", "--", "inverted")
        self.assertEqual(rc, 2, err)
        self.assertIn("'<PATH>', '<REASON>', '<TIP_YOU_READ>' are still a "
                      "placeholder", err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        # The counts are owed placeholders too (task/3382 F10: this arm
        # pinned a line without them, because `fix` filled 1 and new).
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s fix --finding-count '<N>' "
                         "--prior-relation '<new|uncured|regression-of-cure>' "
                         "--worse-than-main '<PATH>' --no-patch-because "
                         "'<REASON>' --tip '<TIP_YOU_READ>' -- inverted"
                         % row["id"])
        self.assertIn("is now at %s" % self.b, self.note(err))
        rc, _out, err = self.paste(line.replace("'<PATH>'", "helm/x.py")
                                   .replace("'<N>'", "1")
                                   .replace("'<new|uncured|regression-of-"
                                            "cure>'", "new")
                                   .replace("'<REASON>'", "'a meld'")
                                   .replace("'<TIP_YOU_READ>'", self.b)
                                   .replace(" -- inverted", " --finding inverted -- inverted"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["reviewed_tip"], self.b)

    def test_an_unfilled_outcome_is_refused(self):
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "maybe", "fine")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s '<clean|concur|fix>' -- "
                         "fine" % row["id"][:8])
        self.paste_refused(line, row["id"], "<clean|concur|fix>")
        rc, _out, err = self.paste(line.replace("'<clean|concur|fix>'",
                                                "concur"))
        self.assertEqual(rc, 0, err)
        self.assertEqual([e["polarity"] for e in
                          self.events(row["id"], "verdict")], ["concur"])

    def test_the_hold_and_send_doors_refuse_them_too(self):  # noqa: VACUOUS_ASSERTION — the control hold on the same row is asserted to record (rc 0) at the end of this method
        row = self.row()
        rc, _out, err = self.dispatch("hold", row["id"], "--source-clean",
                                      row["tip"], "--", "<reason>")
        self.assertEqual(rc, 2, err)
        self.assertIn("'<reason>' is still a placeholder", err)
        self.assertEqual(self.folded(row["id"])["status"], "open")
        before = sorted(dispatches.snapshot()[0])
        rc, _out, err = self.dispatch(
            "send", "<recipient>", "lane/ph", "build it", "--ref", self.b,
            "--kind", "<build|review>", "--new-work", "--repo", self.repo,
            "--force", "--reason", "a deliberate fork", seat="integrator")
        self.assertEqual(rc, 2, err)
        self.assertIn("'<recipient>', '<build|review>' are still a "
                      "placeholder", err)
        self.assertEqual(sorted(dispatches.snapshot()[0]), before)
        # THE CONTROL on the hold door: the reason typed, it holds.
        rc, _out, err = self.dispatch("hold", row["id"], "--source-clean",
                                      row["tip"], "--", "nothing found, Ran 5 tests OK")
        self.assertEqual(rc, 0, err)

    def test_a_placeholder_is_a_whole_argument_never_a_word_in_prose(self):
        self.assertTrue(review_done.placeholder("<TIP_YOU_READ>"))
        self.assertTrue(review_done.placeholder(
            "<evidence of at most 256 chars>"))
        for text in ("see <PATH> for it", "a < b > c", "<>", "< PATH>",
                     "<a><b>", "<T>", "<expected>", "-> x", ""):
            self.assertFalse(review_done.placeholder(text), text)
        self.assertIsNone(review_done.unfilled_refusal(
            ["abc123", "--", "see <PATH> for it"]))
        # Angle-bracketed evidence is ordinary technical prose, not a
        # corrected-line choice. The door records it unchanged.
        row = self.row()
        rc, _out, err = self.done(row["id"], "concur", "--", "<expected>")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["verdict_ref"], "<expected>")


class EvidenceThatLooksLikeAFlagTest(ReviewDoneBase):
    """task/3382 F4: every corrected line puts `--` before the free text,
    and every door honours it, so evidence that starts `-h` is recorded and
    never loops through the same refusal."""

    def test_review_done_evidence_starting_dash_h(self):
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "concur", "-h foo")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s concur -- '-h foo'"
                         % row["id"][:8])
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["verdict_ref"], "-h foo")

    def test_the_verdict_door_evidence_starting_dash_h(self):
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      "--concur", "--measured", "-h foo")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s %s --concur "
                         "--measured -- '-h foo'" % (row["id"], self.b))
        # THE DOOR NOW HONOURS ITS OWN ESCAPE: it consumed the `--` and then
        # refused the evidence as a flag, so this paste looped.
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["verdict_ref"], "-h foo")

    def test_a_flag_word_after_the_terminator_is_evidence(self):
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      "--concur", "--measured", "--",
                                      "--unverified", "at", "best")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["verdict_ref"],
                         "--unverified at best")

    def test_the_hold_door_reason_starting_dash_h(self):
        row = self.row()
        rc, _out, err = self.dispatch("hold", row["id"], "--source-clean",
                                      row["tip"], "-h foo Ran 5 tests OK")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch hold %s --source-clean %s -- "
                         "'-h foo Ran 5 tests OK'" % (row["id"], self.b))
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["hold_reason"],
                         "-h foo Ran 5 tests OK")

    def test_dash_h_after_the_terminator_is_a_write_to_the_delegate_guard(self):
        """`-h` after `--` is recorded, so the argv-guard may not wave it
        through as a help call (it did for `dispatch hold <id> -- -h`)."""
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "concur", "--", "-h")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["verdict_ref"], "-h")
        self.assertFalse(delegate_grant.writes_nothing(
            "review", "done", [row["id"][:8], "concur", "--", "-h"]))
        self.assertFalse(delegate_grant.writes_nothing(
            "dispatch", "hold", ["d-1", "--", "--help"]))
        # THE CONTROL: before the terminator it is still the help it was.
        self.assertTrue(delegate_grant.writes_nothing(
            "review", "done", [row["id"][:8], "concur", "-h"]))
        self.assertTrue(delegate_grant.writes_nothing(
            "dispatch", "verdict", ["--help", "--", "x"]))


class EveryValueIsOneQuotedShellWordTest(ReviewDoneBase):
    """task/3382 F1, pinned for EVERY field: each value a corrected line
    carries, placeholders included, is one `shlex.quote`d word, so bash
    pasting it runs one command, creates nothing, and hands `helm` the
    value whole."""

    def assert_one_command(self, err, carried):
        line = self.corrected(err)
        words = shlex.split(line)
        self.assertEqual(" ".join(shlex.quote(w) for w in words), line)
        argv, created = pasted(self, line)
        self.assertEqual((argv, created), (words[1:], []), line)
        self.assertEqual(words.count(EVIL), carried, line)
        return line

    def test_the_verdict_line(self):  # noqa: VACUOUS_ASSERTION — every iteration asserts the refused line's words positively (argv equality, the value counted); the absent verdict is the refusal's product law
        row = self.row()
        # THE PATCH-TIP ARM PINNED 4 (task/3403): an EVIL patch tip is not
        # hex, so the door refuses it for what it is and the line carries
        # '<FULL_SHA>' in its place, never the value.
        for cure, carried in (
                (["--no-patch-because", EVIL, "--design-finding", EVIL], 4),
                (["--patch-tip", EVIL, "--meld", EVIL], 3)):
            with self.subTest(cure=cure[0]):
                rc, _out, err = self.dispatch(
                    "verdict", row["id"], self.c, "--fix", "--measured",
                    "--finding-count", "1", "--prior-relation", "new",
                    "--worse-than-main", EVIL, *cure, "--", EVIL)
                self.assertNotEqual(rc, 0, err)
                line = self.assert_one_command(err, carried)
                self.assertIn("'<TIP_YOU_READ>'", line)
        self.assertEqual(self.events(row["id"], "verdict"), [])

    def test_the_hold_line(self):
        row = self.row()
        rc, _out, err = self.dispatch("hold", row["id"], "--source-clean",
                                      EVIL, "--meld", EVIL, "--", EVIL)
        self.assertEqual(rc, 1, err)
        self.assertIn("--source-clean '<TIP_YOU_READ>'",
                      self.assert_one_command(err, 2))

    def test_the_send_line(self):
        rc, _out, err = self.dispatch(
            "send", EVIL, EVIL, EVIL, "--ref", EVIL, "--kind", "build",
            "--new-work", "--note", EVIL, "--key", EVIL, "--repo", self.repo,
            seat="integrator")
        self.assertNotEqual(rc, 0, err)
        # THIS ARM PINNED 6 (task/3403): an EVIL recipient is no seat token,
        # which is what the door refused, so the line carries '<recipient>'
        # in its place and never the address that looped.
        self.assertIn("'<recipient>'", self.assert_one_command(err, 5))

    def test_the_review_done_line(self):  # noqa: VACUOUS_ASSERTION — every iteration asserts the refused line's words positively (argv equality, the value counted)
        # THIS ARM PINNED 7 (task/3403): the row id, the count, the relation,
        # the tip and a patch tip that are EVIL are each refused by the door
        # for what they are, so the line carries their placeholders, quoted
        # like every other word, and never the values.
        for cure, carried in ((["--no-patch-because", EVIL], 3),
                              (["--patch-tip", EVIL], 2)):
            with self.subTest(cure=cure[0]):
                rc, _out, err = self.done(
                    EVIL, "fix", "--finding-count", EVIL, "--prior-relation",
                    EVIL, "--worse-than-main", EVIL, *cure, "--tip", EVIL,
                    "--", EVIL)
                self.assertEqual(rc, 2, err)
                line = self.assert_one_command(err, carried)
                for hole in ("<row-id-prefix>", "<N>", "<TIP_YOU_READ>",
                             "<new|uncured|regression-of-cure>"):
                    self.assertIn(shlex.quote(hole), line)


WORK = "<--new-work|--supersedes ROW>"
EXIT = "<--worse-than-main PATH|--imperfect>"
CURE = "<--patch-tip SHA|--no-patch-because REASON>"
HOLDER = "<--owner-gated|--source-clean TIP>"
RELATION = "<new|uncured|regression-of-cure>"


def padded(rid):
    """An id whose first 12 characters are `rid`'s and which names no row."""
    return rid[:12] + ("1" if rid[12] == "0" else "0") * 20


class TwoAnswersToOneChoiceTest(ReviewDoneBase):
    """task/3382 F1, F2: the doors refuse two answers to one choice, and the
    corrected line carries THAT CHOICE'S placeholder. It never keeps one of
    the two for the seat: at 760fb5a4f446 `--fix --approve` became a
    source-clean hold, `--concur --fix` a FIX, `--measured --inferred`
    inferred, a hold on both holders source-clean, and a send naming both
    work arms `--new-work` with its row dropped."""

    def test_two_polarities(self):  # noqa: VACUOUS_ASSERTION — the same row's polarity is asserted RECORDED after the refused pastes
        row = self.row()
        for pair in (("--fix", "--approve"), ("--concur", "--fix")):
            with self.subTest(pair=pair):
                rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                              *pair, "--measured", "inverted")
                self.assertEqual(rc, 2, err)
                self.assertIn("(at most one)", err)
                line = self.corrected(err)
                self.assertEqual(line, "helm dispatch verdict %s %s "
                                 "'<--concur|--fix>' --measured -- inverted"
                                 % (row["id"], self.b))
                self.paste_refused(line, row["id"], "<--concur|--fix>")
        rc, _out, err = self.paste(line.replace("'<--concur|--fix>'",
                                                "--concur"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["polarity"], "concur")

    def test_two_bases(self):  # noqa: VACUOUS_ASSERTION — the same row's basis is asserted RECORDED after the refused paste
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      "--concur", "--measured", "--inferred",
                                      "fine")
        self.assertEqual(rc, 2, err)
        self.assertIn("basis is one of", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s %s --concur "
                         "'<--measured|--inferred>' -- fine"
                         % (row["id"], self.b))
        self.paste_refused(line, row["id"], "<--measured|--inferred>")
        rc, _out, err = self.paste(line.replace("'<--measured|--inferred>'",
                                                "--measured"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.events(row["id"], "verdict")[0]["basis"],
                         "measured")

    def test_both_exit_answers_and_both_cure_answers(self):  # noqa: VACUOUS_ASSERTION — the same row's exit and cure answers are asserted RECORDED after the refused paste
        row = self.row()
        rc, _out, err = self.dispatch(
            "verdict", row["id"], row["tip"], "--fix", "--measured",
            "--finding-count", "1", "--prior-relation", "new",
            "--worse-than-main", "helm/x.py", "--imperfect",
            "--patch-tip", self.c, "--no-patch-because", "a meld", "--", "ev")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict %s %s --fix --measured "
                         "--finding-count 1 --prior-relation new %s %s -- ev"
                         % (row["id"], self.b, shlex.quote(EXIT),
                            shlex.quote(CURE)))
        self.paste_refused(line, row["id"], EXIT, CURE)
        rc, _out, err = self.paste(
            line.replace(shlex.quote(EXIT), "--imperfect")
            .replace(shlex.quote(CURE), "--patch-tip " + self.c))
        self.assertEqual(rc, 0, err)
        event = self.events(row["id"], "verdict")[0]
        self.assertEqual((event["exit_answer"], event["patch_tip"]),
                         ("imperfect", self.c))

    def test_one_flag_given_two_values(self):  # noqa: VACUOUS_ASSERTION — a refused read writing nothing is product law; each corrected line is asserted to carry the placeholder
        """The same choice answered twice: the door refuses the second
        value, and at 760fb5a4f446 the line kept the first."""
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "fix", "--finding-count",
                                  "1", "--finding-count", "2", "--",
                                  "inverted")
        self.assertEqual(rc, 2, err)
        self.assertIn("may be given once", err)
        self.assertIn("--finding-count '<N>' ", self.corrected(err))
        rc, _out, err = self.dispatch(
            "verdict", row["id"], row["tip"], "--fix", "--measured",
            "--finding-count", "1", "--prior-relation", "new",
            "--worse-than-main", "helm/x.py", "--patch-tip", self.c,
            "--patch-tip", self.side, "--", "ev")
        self.assertEqual(rc, 2, err)
        self.assertIn("--patch-tip '<FULL_SHA>' -- ev", self.corrected(err))
        self.assertEqual(self.events(row["id"], "verdict"), [])

    def test_a_hold_on_both_holders(self):  # noqa: VACUOUS_ASSERTION — the same row's source_clean_tip is asserted RECORDED after the refused paste
        row = self.row()
        rc, _out, err = self.dispatch("hold", row["id"], "--owner-gated",
                                      "--source-clean", row["tip"],
                                      "nothing found, Ran 5 tests OK")
        self.assertEqual(rc, 1, err)
        self.assertIn("a hold is owed by ONE holder", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch hold %s %s -- 'nothing found, Ran 5 tests OK'"
                         % (row["id"], shlex.quote(HOLDER)))
        # A RE-PASTE PRINTS THE SAME LINE: the placeholder is never read as
        # the first word of the reason.
        err = self.paste_refused(line, row["id"], HOLDER)
        self.assertEqual(self.corrected(err), line)
        rc, _out, err = self.paste(line.replace(shlex.quote(HOLDER),
                                                "--source-clean " + self.b))
        self.assertEqual(rc, 0, err)
        folded = self.folded(row["id"])
        self.assertEqual((folded["source_clean_tip"],
                          bool(folded.get("owner_gated")),
                          folded["hold_reason"]),
                         (self.b, False, "nothing found, Ran 5 tests OK"))

    def test_a_send_naming_both_work_arms(self):  # noqa: VACUOUS_ASSERTION — the refused send writing nothing is product law; the filled paste's minted row is asserted positively at the end
        sent = self.row(lane="lane-both")
        before = sorted(dispatches.snapshot()[0])
        rc, _out, err = self.dispatch(
            "send", "integrator", "lane-both", "the cure is on the lane",
            "--ref", self.c, "--kind", "build", "--new-work",
            "--supersedes", sent["id"], "--repo", self.repo, author=False)
        self.assertNotEqual(rc, 0, err)
        self.assertIn("exclusive", err)
        self.assertEqual(sorted(dispatches.snapshot()[0]), before)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch send integrator lane-both 'the "
                         "cure is on the lane' --ref %s --kind build %s "
                         "--repo %s" % (self.c, shlex.quote(WORK),
                                        shlex.quote(self.repo)))
        self.paste_refused(line, "not-a-row", WORK)
        rc, _out, err = self.paste(line.replace(
            shlex.quote(WORK), "--supersedes " + sent["id"]))
        self.assertEqual(rc, 0, err)
        minted = [r for r in dispatches.snapshot()[0].values()
                  if r.get("supersedes") == sent["id"]]
        self.assertEqual([r["recipient"] for r in minted], ["integrator"])


class SendProseStaysProseTest(ReviewDoneBase):
    """task/3382 F3: the corrected send reads argv through the send door's
    OWN partition, so a word the door keeps as prose stays prose, verbatim.
    At 760fb5a4f446 a `--force` inside the brief became a real trailing
    `--force` and left the brief, and an unknown `--x` in it was dropped."""

    def test_a_force_inside_the_brief_stays_in_the_brief(self):  # noqa: VACUOUS_ASSERTION — the minted row's brief is asserted to carry the prose verbatim, after the line is asserted EQUAL to the whole command
        rc, _out, err = self.dispatch(
            "send", READER, "lane/forceprose", "please", "use", "--force",
            "carefully", "--ref", self.b, "--new-work", "--repo", self.repo,
            seat="integrator")
        self.assertEqual(rc, 2, err)
        self.assertIn("requires --kind", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch send %s lane/forceprose 'please "
                         "use --force carefully' --ref %s --kind "
                         "'<build|review>' --new-work --repo %s"
                         % (READER, self.b, shlex.quote(self.repo)))
        rc, _out, err = self.paste(line.replace("'<build|review>'", "build"),
                                   seat="integrator")
        self.assertEqual(rc, 0, err)
        row = next(r for r in dispatches.snapshot()[0].values()
                   if r.get("lane") == "forceprose")
        self.assertIn("please use --force carefully",
                      row.get("message_body") or "")

    def test_an_unknown_option_word_inside_the_brief_is_kept(self):  # noqa: VACUOUS_ASSERTION — the minted row's brief is asserted to carry the prose verbatim, after the line is asserted EQUAL to the whole command
        rc, _out, err = self.dispatch(
            "send", READER, "lane/unknownprose", "fix", "the", "--x", "path",
            "--ref", self.b, "--kind", "build", "--new-work", "--repo",
            self.repo, seat="integrator")
        self.assertEqual(rc, 2, err)
        self.assertIn("unknown option --x", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch send %s lane/unknownprose 'fix "
                         "the --x path' --ref %s --kind build --new-work "
                         "--repo %s" % (READER, self.b, shlex.quote(self.repo)))
        rc, _out, err = self.paste(line, seat="integrator")
        self.assertEqual(rc, 0, err)
        row = next(r for r in dispatches.snapshot()[0].values()
                   if r.get("lane") == "unknownprose")
        self.assertIn("fix the --x path", row.get("message_body") or "")


class APatchTipIsACommitIdTest(ReviewDoneBase):
    """task/3382 F4: a patch tip is hex. A unique prefix is written out in
    full; a ref name — HEAD, the trunk, a branch — is refused as on main,
    and never resolved in the row's checkout, where it names whatever that
    checkout has there now. At 760fb5a4f446 `review done ... --patch-tip
    HEAD` recorded the row checkout's HEAD as the reviewer's cure."""

    def test_review_done_refuses_a_ref_name(self):  # noqa: VACUOUS_ASSERTION — the control records the cure's full id on the same row
        row = self.row()
        for name in ("HEAD", self.main, "side"):
            with self.subTest(name=name):
                rc, _out, err = self.done(
                    row["id"][:8], "fix", "cured it", "--finding-count", "1",
                    "--prior-relation", "new", "--worse-than-main", "helm/x.py",
                    "--patch-tip", name)
                self.assertNotEqual(rc, 0, err)
                # Composed with task/3382 L3 (typed ids): the verdict door
                # refuses a ref name at its typed-tip check, in its words.
                self.assertIn("is not one commit's id or unique prefix", err)
                # THIS ARM PINNED THE NAME ECHOED (task/3403), which the door
                # refused again, forever: the slot is its placeholder.
                self.assertIn("--patch-tip '<FULL_SHA>' -- ",
                              self.corrected(err))
                self.assertEqual(self.events(row["id"], "verdict"), [])
        # THE CONTROL: a hex prefix of the cure is written out in full.
        rc, _out, err = self.done(
            row["id"][:8], "fix", "cured it", "--finding-count", "1",
            "--prior-relation", "new", "--worse-than-main", "helm/x.py",
            "--patch-tip", self.c[:10])
        self.assertEqual(rc, 0, err)
        self.assertEqual([e["patch_tip"] for e in
                          self.events(row["id"], "verdict")], [self.c])

    def test_the_verdict_line_never_resolves_a_ref_name(self):  # noqa: VACUOUS_ASSERTION — the control paste records the cure's full id on the same row
        row = self.row()
        argv = ["verdict", row["id"], row["tip"], "--fix", "--measured",
                "--finding-count", "1", "--prior-relation", "new",
                "--worse-than-main", "helm/x.py", "--patch-tip", "HEAD", "--",
                "cured it"]
        rc, _out, err = self.dispatch(*argv)
        self.assertEqual(rc, 1, err)
        # Composed with task/3382 L3: the typed-tip check refuses HEAD first.
        self.assertIn("is not one commit's id or unique prefix", err)
        line = self.corrected(err)
        # THIS ARM PINNED `--patch-tip HEAD` ECHOED, and its paste refused
        # again for HEAD (task/3403): the line now carries the placeholder,
        # and its paste is refused only as unfilled.
        self.assertIn("--patch-tip '<FULL_SHA>' --", line)
        self.assertNotIn(self.c, line)
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 2, err)
        self.assertIn("'<FULL_SHA>' is still a placeholder", err)
        self.assertNotIn("is not one commit's id", err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        # THE CONTROL: composed with task/3382 L3, the verdict door itself
        # resolves a hex prefix of the cure and records the full id, so no
        # refusal and no corrected line are printed for it.
        rc, _out, err = self.dispatch(*[self.c[:10] if a == "HEAD" else a
                                        for a in argv])
        self.assertEqual(rc, 0, err)
        self.assertEqual([e["patch_tip"] for e in
                          self.events(row["id"], "verdict")], [self.c])


class AWorkArmIsNeverChosenForYouTest(ReviewDoneBase):
    """task/3382 F5: a send that names no work arm keeps the placeholder;
    the open row it may answer is named on the line above, never filled in.
    At 760fb5a4f446 the line carried `--supersedes <that row>`."""

    def test_the_candidate_row_is_named_above_the_line(self):  # noqa: VACUOUS_ASSERTION — the filled paste records a row superseding the candidate
        sent = self.row(lane="lane-cand")
        before = sorted(dispatches.snapshot()[0])
        rc, _out, err = self.dispatch(
            "send", "integrator", "lane-cand", "the cure is on the lane",
            "--ref", self.c, "--kind", "build", "--repo", self.repo,
            author=False)
        self.assertNotEqual(rc, 0, err)
        self.assertEqual(sorted(dispatches.snapshot()[0]), before)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch send integrator lane-cand 'the "
                         "cure is on the lane' --ref %s --kind build %s "
                         "--repo %s" % (self.c, shlex.quote(WORK),
                                        shlex.quote(self.repo)))
        self.assertNotIn(sent["id"], line)
        self.assertIn(sent["id"], self.note(err))
        self.paste_refused(line, "not-a-row", WORK)
        rc, _out, err = self.paste(line.replace(
            shlex.quote(WORK), "--supersedes " + sent["id"]))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len([r for r in dispatches.snapshot()[0].values()
                              if r.get("supersedes") == sent["id"]]), 1)


class ARePasteNeverGrowsTheBriefTest(ReviewDoneBase):
    """task/3382 F6: a placeholder is recognized by its slot AND by its exact
    spelling anywhere in argv, so it never becomes a word of the brief; and a
    brief that carries one is refused. At 760fb5a4f446 each unfilled re-paste
    folded the work placeholder into the brief, and a brief so grown passed
    every door."""

    def test_an_unfilled_line_pasted_back_prints_itself(self):  # noqa: VACUOUS_ASSERTION — the line is asserted EQUAL to the whole expected command, and the refused re-paste's line EQUAL to it
        with self.as_seat("integrator"):
            line = review_done.send_line([
                READER, "lane/regrow", "--ref", self.b, "--kind", "build",
                "--repo", self.repo])
        self.assertEqual(line, "helm dispatch send %s lane/regrow '<brief>' "
                         "--ref %s --kind build %s --repo %s"
                         % (READER, self.b, shlex.quote(WORK),
                            shlex.quote(self.repo)))
        err = self.paste_refused(line, "not-a-row", "<brief>", WORK,
                                 seat="integrator")
        self.assertEqual(self.corrected(err), line)

    def test_a_brief_that_carries_a_placeholder_is_refused(self):  # noqa: VACUOUS_ASSERTION — a refused brief writing nothing is product law; its corrected line is asserted positively to carry the brief placeholder
        before = sorted(dispatches.snapshot()[0])
        grown = "<brief> " + WORK
        rc, _out, err = self.dispatch(
            "send", READER, "lane/grown", grown, "--ref", self.b, "--kind",
            "build", "--new-work", "--repo", self.repo, seat="integrator")
        self.assertEqual(rc, 2, err)
        self.assertIn("'<brief>'", err)
        self.assertEqual(sorted(dispatches.snapshot()[0]), before)
        line = self.corrected(err)
        self.assertNotIn(shlex.quote(grown), line)
        self.assertIn(" '<brief>' ", line)


class TheExitAndCureAnswersAreTheSeatsTest(ReviewDoneBase):
    """task/3382 F7: a FIX that has not answered the exit question or the
    cure question keeps BOTH as placeholders naming the two answers. At
    760fb5a4f446 the line chose `--worse-than-main` and `--no-patch-because`
    and left only their values open."""

    def test_review_done_fix(self):  # noqa: VACUOUS_ASSERTION — the filled paste is asserted RECORDED on the same row
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "fix", "inverted",
                                  "--finding-count", "1", "--prior-relation",
                                  "new")
        self.assertEqual(rc, 2, err)
        self.assertIn("ANSWER THE EXIT QUESTION", err)
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s fix --finding-count 1 "
                         "--prior-relation new %s %s -- inverted"
                         % (row["id"], shlex.quote(EXIT), shlex.quote(CURE)))
        err = self.paste_refused(line, row["id"], EXIT, CURE)
        self.assertEqual(self.corrected(err), line)
        rc, _out, err = self.paste(
            line.replace(shlex.quote(EXIT), "--worse-than-main helm/x.py")
            .replace(shlex.quote(CURE),
                     "--no-patch-because 'a meld' --finding inverted"))
        self.assertEqual(rc, 0, err)
        event = self.events(row["id"], "verdict")[0]
        self.assertEqual((event["exit_answer"], event["no_patch_because"]),
                         ("worse-than-main", "a meld"))


class ARowTheSeatDidNotNameTest(ReviewDoneBase):
    """task/3382 F8: an id that names no row offers its candidate on the line
    above, and the command carries '<ROW_ID>'. At 760fb5a4f446 the verdict
    and send lines carried the candidate's full id."""

    def test_the_verdict_line(self):  # noqa: VACUOUS_ASSERTION — the filled paste is asserted RECORDED on the candidate row
        row = self.row()
        rc, _out, err = self.dispatch("verdict", padded(row["id"]), row["tip"],
                                      "--concur", "--measured", "fine")
        self.assertNotEqual(rc, 0, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch verdict '<ROW_ID>' %s --concur "
                         "--measured -- fine" % self.b)
        self.assertIn("did you mean %s" % row["id"], self.note(err))
        self.paste_refused(line, row["id"], "<ROW_ID>")
        rc, _out, err = self.paste(line.replace("'<ROW_ID>'", row["id"]))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["polarity"], "concur")

    def test_the_send_line(self):  # noqa: VACUOUS_ASSERTION — the line is asserted EQUAL to the whole expected command and the note above it to name the candidate row
        sent = self.row(lane="lane-pad")
        rc, _out, err = self.dispatch(
            "send", "integrator", "lane-pad", "the cure", "--ref", self.c,
            "--kind", "build", "--supersedes", padded(sent["id"]), "--repo",
            self.repo, author=False)
        self.assertNotEqual(rc, 0, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm dispatch send integrator lane-pad 'the "
                         "cure' --ref %s --kind build --supersedes '<ROW_ID>' "
                         "--repo %s" % (self.c, shlex.quote(self.repo)))
        self.assertIn("did you mean %s" % sent["id"], self.note(err))


class AnOwnerGatedHoldIsNeverLiftedByAPasteTest(ReviewDoneBase):
    """task/3382 F9: a refusal on a row held ON THE OWNER never ends with
    `helm dispatch release`, which would lift a hold only he may lift; the
    line above says whose move it is. An ordinary hold keeps the release
    line. At 760fb5a4f446 every held row ended with the release line."""

    def test_no_line_releases_an_owner_gated_hold(self):  # noqa: VACUOUS_ASSERTION — each of three fixed refused calls has its line asserted EQUAL to the listing command, and the held row is asserted positively after them
        row = self.row()
        rc, _out, err = self.dispatch("hold", row["id"], "--owner-gated",
                                      "waiting on the owner's call")
        self.assertEqual(rc, 0, err)
        refused = (
            lambda: self.dispatch("verdict", row["id"], row["tip"], "--concur",
                                  "--measured", "fine"),
            lambda: self.dispatch("hold", row["id"], "--source-clean",
                                  row["tip"], "nothing found, Ran 5 tests OK"),
            lambda: self.done(row["id"][:8], "concur", "fine"))
        for n, call in enumerate(refused):
            with self.subTest(call=n):
                rc, _out, err = call()
                self.assertNotEqual(rc, 0, err)
                line = self.corrected(err)
                self.assertNotIn("release", line)
                self.assertEqual(line, "helm dispatch list --mine --open")
                self.assertIn("OWNER", self.note(err))
        folded = self.folded(row["id"])
        self.assertEqual((folded["status"], folded["owner_gated"]),
                         ("held", True))
        # THE CONTROL: an ordinary hold still ends with its release line.
        other = self.row()
        rc, _out, err = self.dispatch("hold", other["id"], "waiting on a dep")
        self.assertEqual(rc, 0, err)
        rc, _out, err = self.dispatch("verdict", other["id"], other["tip"],
                                      "--concur", "--measured", "fine")
        self.assertNotEqual(rc, 0, err)
        self.assertEqual(self.corrected(err),
                         "helm dispatch release %s" % other["id"])


class AFixDeclaresItsOwnCountsTest(ReviewDoneBase):
    """task/3382 F10 (the integrator's ruling, the same rule): `review done
    <row> fix` without counts is refused and its line carries '<N>' and the
    relation placeholder; counts the seat gives pass through. At 760fb5a4f446
    it recorded `--finding-count 1 --prior-relation new` undeclared."""

    def test_a_fix_with_no_counts_is_refused(self):  # noqa: VACUOUS_ASSERTION — the filled paste is asserted RECORDED with the counts typed
        row = self.row()
        rc, _out, err = self.done(
            row["id"][:8], "fix", "the guard is inverted",
            "--worse-than-main", "helm/x.py",
            "--no-patch-because", "a design finding for a meld")
        self.assertEqual(rc, 2, err)
        self.assertIn("A FIX COUNTS WHAT IT FOUND", err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s fix --finding-count '<N>' "
                         "--prior-relation %s --worse-than-main helm/x.py "
                         "--no-patch-because 'a design finding for a meld' -- "
                         "'the guard is inverted'"
                         % (row["id"], shlex.quote(RELATION)))
        self.paste_refused(line, row["id"], "<N>", RELATION)
        rc, _out, err = self.paste(line.replace("'<N>'", "3").replace(
            shlex.quote(RELATION), "uncured").replace(
            " -- 'the guard is inverted'",
            " --finding 'the guard is inverted' "
            "--finding 'the retry loses the lock' "
            "--finding 'the guard misses one branch' -- 'the guard is inverted'"))
        self.assertEqual(rc, 0, err)
        event = self.events(row["id"], "verdict")[0]
        self.assertEqual((event["finding_count"], event["prior_relation"]),
                         (3, "uncured"))

    def test_a_count_without_its_relation_is_refused(self):  # noqa: VACUOUS_ASSERTION — a refused read writing nothing is product law; its corrected line is asserted to carry the seat's count beside the relation placeholder
        row = self.row()
        rc, _out, err = self.done(
            row["id"][:8], "fix", "the guard is inverted",
            "--finding-count", "2", "--worse-than-main", "helm/x.py",
            "--no-patch-because", "a meld")
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.events(row["id"], "verdict"), [])
        self.assertIn("--finding-count 2 --prior-relation %s "
                      % shlex.quote(RELATION), self.corrected(err))


class TheFirstTerminatorEndsTheFlagsTest(ReviewDoneBase):
    """The first bare `--` ends the flags and the rest is the text, verbatim,
    at every door and in every corrected line: `-- -- x` records `-- x`, and
    a `--` inside refused evidence is kept. At 760fb5a4f446 the refused
    `review done` evidence `foo -- bar` came back as `foo bar`."""

    def test_the_rest_is_text_verbatim(self):  # noqa: VACUOUS_ASSERTION — every door's record is asserted equal to the text
        rows = [self.row() for _ in range(4)]
        # A source-clean hold names its fab run (task/4103), so every text
        # carries one; the `--` it opens with is what this arm reads.
        x = "x Ran 5 tests OK"
        for rc, _out, err in (
                self.dispatch("verdict", rows[0]["id"], self.b, "--concur",
                              "--measured", "--", "--", x),
                self.dispatch("hold", rows[1]["id"], "--source-clean", self.b,
                              "--", "--", x),
                self.done(rows[2]["id"][:8], "concur", "--", "--", x),
                self.done(rows[3]["id"][:8], "clean", "--", "--", x)):
            self.assertEqual(rc, 0, err)
        self.assertEqual([self.folded(r["id"]).get("verdict_ref")
                          or self.folded(r["id"]).get("hold_reason")
                          for r in rows], ["-- " + x] * 4)
        row = self.row()
        rc, _out, err = self.done(row["id"][:8], "concur", "--bogus", "foo",
                                  "--", "bar")
        self.assertEqual(rc, 2, err)
        line = self.corrected(err)
        self.assertEqual(line, "helm review done %s concur -- 'foo -- bar'"
                         % row["id"][:8])
        rc, _out, err = self.paste(line)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.folded(row["id"])["verdict_ref"], "foo -- bar")


class OneCapPerTextTest(ReviewDoneBase):
    """A text over its cap becomes a placeholder naming THAT text's cap, and
    the doors recognize exactly the spelling the line emits. At 760fb5a4f446
    a clean read's placeholder named the hold cap while the recognized set
    listed the verdict budget, so the two agreed only while both were 256."""

    def test_the_clean_placeholder_is_refused_at_its_own_cap(self):  # noqa: VACUOUS_ASSERTION — a refused paste writing nothing is product law; the corrected line is asserted EQUAL to the whole command naming the cap
        row = self.row()
        with mock.patch.object(dispatches, "HOLD_REASON_CAP", 200):
            rc, _out, err = self.done(row["id"][:8], "clean", "x" * 230)
            self.assertEqual(rc, 1, err)
            line = self.corrected(err)
            self.assertEqual(line, "helm review done %s clean -- '<evidence "
                             "of at most 200 chars>'" % row["id"])
            self.paste_refused(line, row["id"],
                               "<evidence of at most 200 chars>")
        self.assertEqual(self.events(row["id"], "hold"), [])


def setUpModule():
    """No dispatch row this module writes walks the host's process table
    (task/3039; see tests._tmphome.pin_live_seats)."""
    from tests._tmphome import pin_live_seats
    pin_live_seats()
