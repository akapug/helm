#!/usr/bin/env python3
"""A corrected line never carries its refusal's cause back (task/3403).

SEEN LIVE on a reviewing seat: `helm dispatch verdict <row> <tip> --fix
--imperfect --no-patch-because R` is refused IMPERFECT IS NOT A BLOCK, and
the one `corrected:` line under the refusal (task/3382) repeated `--fix
--imperfect --no-patch-because R`. Pasting it was refused again, forever.

THE CLASS: a refusal whose cause is a value the seat typed (its shape, or its
pairing with the direction or another answer), and whose corrected line
carried that value back unchanged. EVERY ARM HERE IS ONE SUCH REFUSAL: the
refused call prints its line; the line is pasted back, each placeholder that
is NOT the refused choice filled with a valid value; the paste is never
refused for the same reason. Where the refused choice became a placeholder,
the paste is refused as unfilled, naming it, and a paste with it filled
records. The controls at the end pin lines this lane must NOT change.

A SEPARATE MODULE because tests/test_review_done.py is already 1,600 lines;
the fixtures are imported from it, as that module imports its own from
tests/test_dispatches.py.
"""
import os
import shlex
import subprocess
from unittest import mock

from helm import dispatches, review_done
from tests import test_review_done as trd

READER = trd.READER
#: The placeholders a line prints, spelled as a seat reads them.
REOPENED = "<--approve|--fix --imperfect --patch-tip SHA>"
CLEAN_OR_CURE = "<clean|fix --imperfect --patch-tip SHA>"
DOOR = "<--meld ROOM|--async-because REASON>"
PATH, REASON, SHA = "<PATH>", "<REASON>", "<FULL_SHA>"
#: A FIX with every answer the door owes, each a value it admits.
FIX = ("--fix", "--measured", "--finding-count", "1", "--prior-relation",
       "new", "--worse-than-main", "helm/x.py", "--no-patch-because", "a meld")


def swap(argv, flag, value):
    """`argv` with `flag`'s value replaced by `value`."""
    argv = list(argv)
    argv[argv.index(flag) + 1] = value
    return argv


class NoEchoBase(trd.ReviewDoneBase):

    def unlooped(self, refused, cause, fills=(), seat=READER):
        """(line, paste): the refused call's corrected line, and what pasting
        it back does with each (placeholder, value) of `fills` typed in. The
        call was refused for `cause`, and the paste is not."""
        rc, _out, err = refused
        self.assertNotEqual(rc, 0, err)
        self.assertIn(cause, err)
        line = pasted = self.corrected(err)
        for hole, value in fills:
            self.assertIn(shlex.quote(hole), pasted)
            pasted = pasted.replace(shlex.quote(hole), value, 1)
        paste = self.paste(pasted, seat=seat)
        self.assertNotIn(cause, paste[2], pasted)
        return line, paste

    def records(self, paste, rid, kind="verdict"):
        """The paste recorded one `kind` event on `rid`; that event."""
        rc, _out, err = paste
        self.assertEqual(rc, 0, err)
        [event] = self.events(rid, kind)
        return event

    def unfilled(self, paste, *names):
        """The paste was refused only for the placeholders it still carries."""
        rc, _out, err = paste
        self.assertEqual(rc, 2, err)
        self.assertIn("still a placeholder", err)
        for name in names:
            self.assertIn(shlex.quote(name), err)
        return err


class AnImperfectReadWithNoCureReopensItsDirectionTest(NoEchoBase):
    """THE REPORTED LOOP. `--imperfect` says the tip is no worse than main,
    and the door admits it only as the carrier of a committed cure, so the
    refused choice is the direction. The line names its two real ways out
    and carries neither the hand-back nor the no-cure answer."""

    ARGV = FIX[:6] + ("--imperfect", "--no-patch-because",
                      "a design finding for a meld")

    def test_fix_imperfect_with_a_no_cure_reason(self):  # noqa: VACUOUS_ASSERTION — the absent cause is paired with the paste's positive answer: `records` asserts rc 0 and the one event, `unfilled` asserts rc 2 naming each placeholder
        row = self.row()
        line, paste = self.unlooped(
            self.dispatch("verdict", row["id"], row["tip"], *self.ARGV,
                          "--", "the guard is inverted"),
            "IMPERFECT IS NOT A BLOCK")
        self.assertEqual(line, "helm dispatch verdict %s %s %s --measured "
                         "--finding-count 1 --prior-relation new -- 'the "
                         "guard is inverted'" % (row["id"], self.b,
                                                 shlex.quote(REOPENED)))
        # AS PRINTED: refused for the unfilled choice, and a re-paste prints
        # the same line.
        err = self.unfilled(paste, REOPENED)
        self.assertEqual(self.corrected(err), line)
        # THE WAY OUT THAT CARRIES A CURE records polarity fix, IMPERFECT and
        # the patch.
        event = self.records(self.paste(line.replace(
            shlex.quote(REOPENED), "--fix --imperfect --patch-tip "
            + self.c)), row["id"])
        self.assertEqual((event["polarity"], event["exit_answer"],
                          event["patch_tip"]), ("fix", "imperfect", self.c))

    def test_the_way_out_that_blocks_nothing(self):  # noqa: VACUOUS_ASSERTION — the absent cause is paired with the paste's positive answer: `records` asserts rc 0 and the one event, `unfilled` asserts rc 2 naming each placeholder
        """`--approve` with no gate token is the approve door's own repair,
        a source-clean hold, which records."""
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      *self.ARGV, "--", "not worse")
        line = self.corrected(err)
        rc, _out, err = self.paste(line.replace(shlex.quote(REOPENED),
                                                "--approve"))
        self.assertNotEqual(rc, 0, err)
        self.assertNotIn("IMPERFECT IS NOT A BLOCK", err)
        hold = self.corrected(err)
        self.assertEqual(hold, "helm dispatch hold %s --source-clean %s -- "
                         "'not worse'" % (row["id"], self.b))
        self.records(self.paste(hold), row["id"], "hold")
        self.assertEqual(self.folded(row["id"])["source_clean_tip"], self.b)

    def test_supersede_imperfect(self):  # noqa: VACUOUS_ASSERTION — the absent cause is paired with the paste's positive answer: `records` asserts rc 0 and the one event, `unfilled` asserts rc 2 naming each placeholder
        """A SUPERSEDE carries no cure at all, so its `--imperfect` is never
        admitted and the line looped on `--supersede --imperfect`."""
        row = self.row()
        line, paste = self.unlooped(
            self.dispatch("verdict", row["id"], row["tip"], "--supersede",
                          "--measured", "--finding-count", "1",
                          "--prior-relation", "new", "--imperfect", "--",
                          "replaced"), "IMPERFECT IS NOT A BLOCK")
        self.assertEqual(line, "helm dispatch verdict %s %s %s --measured "
                         "--finding-count 1 --prior-relation new -- replaced"
                         % (row["id"], self.b, shlex.quote(REOPENED)))
        self.unfilled(paste, REOPENED)
        event = self.records(self.paste(line.replace(
            shlex.quote(REOPENED), "--fix --imperfect --patch-tip "
            + self.c)), row["id"])
        self.assertEqual((event["polarity"], event["patch_tip"]),
                         ("fix", self.c))

    def test_review_done_fix_imperfect_with_a_no_cure_reason(self):  # noqa: VACUOUS_ASSERTION — the absent cause is paired with the paste's positive answer: `records` asserts rc 0 and the one event, `unfilled` asserts rc 2 naming each placeholder
        row = self.row()
        line, paste = self.unlooped(
            self.done(row["id"][:8], "fix", "--finding-count", "1",
                      "--prior-relation", "new", "--imperfect",
                      "--no-patch-because", "a meld", "--", "inverted"),
            "IMPERFECT IS NOT A BLOCK")
        self.assertEqual(line, "helm review done %s %s --finding-count 1 "
                         "--prior-relation new -- inverted"
                         % (row["id"], shlex.quote(CLEAN_OR_CURE)))
        err = self.unfilled(paste, CLEAN_OR_CURE)
        self.assertEqual(self.corrected(err), line)
        event = self.records(self.paste(line.replace(
            shlex.quote(CLEAN_OR_CURE), "fix --imperfect --patch-tip "
            + self.c)), row["id"])
        self.assertEqual((event["exit_answer"], event["patch_tip"]),
                         ("imperfect", self.c))


class AVerdictValueRefusedByItsShapeTest(NoEchoBase):
    """Each value below is refused by the verdict door for what it IS, so
    no change in the world admits it: the line carries its placeholder."""

    def verdict(self, row, argv, evidence="ev"):
        return self.dispatch("verdict", row["id"], row["tip"], *argv, "--",
                             evidence)

    def test_a_count_that_is_not_a_count(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty literal table, and every case asserts the pasted line's positive answer (`records`, `unfilled` or `minted`)
        for count in ("abc", "-1", "1234567890"):
            with self.subTest(count=count):
                row = self.row()
                _line, paste = self.unlooped(
                    self.verdict(row, swap(FIX, "--finding-count", count)),
                    "finding_count needs", (("<N>", "2"),))
                self.assertEqual(self.records(paste, row["id"])
                                 ["finding_count"], 2)

    def test_a_relation_that_is_not_a_relation(self):
        row = self.row()
        _line, paste = self.unlooped(
            self.verdict(row, swap(FIX, "--prior-relation", "old")),
            "prior_relation must be", (("<new|uncured|regression-of-cure>",
                                        "uncured"),))
        self.assertEqual(self.records(paste, row["id"])["prior_relation"],
                         "uncured")

    def test_a_relation_with_no_count(self):
        """A CONCUR is not asked for counts, but a relation it gives
        describes counted findings, and the door refuses one alone."""
        row = self.row()
        line, paste = self.unlooped(
            self.verdict(row, ("--concur", "--measured", "--prior-relation",
                               "new")),
            "prior_relation requires finding_count", (("<N>", "0"),))
        self.assertIn("--finding-count '<N>' --prior-relation new", line)
        self.assertEqual(self.records(paste, row["id"])["finding_count"], 0)

    def test_a_worse_than_main_path_outside_the_project(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty literal table, and every case asserts the pasted line's positive answer (`records`, `unfilled` or `minted`)
        for path in ("/etc/passwd", "../up"):
            with self.subTest(path=path):
                row = self.row()
                _line, paste = self.unlooped(
                    self.verdict(row, swap(FIX, "--worse-than-main", path)),
                    "must be project-relative", ((PATH, "helm/y.py"),))
                self.assertEqual(self.records(paste, row["id"])
                                 ["worse_than_main_paths"], ["helm/y.py"])

    def test_a_no_cure_reason_over_its_cap_or_on_two_lines(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty literal table, and every case asserts the pasted line's positive answer (`records`, `unfilled` or `minted`)
        for reason, hole in (("r" * 300, "<REASON of at most 256 chars>"),
                             ("one\ntwo", REASON)):
            with self.subTest(hole=hole):
                row = self.row()
                _line, paste = self.unlooped(
                    self.verdict(row, swap(FIX, "--no-patch-because",
                                           reason)),
                    "no-patch reason must be one printable line",
                    ((hole, "'a meld'"),))
                self.assertEqual(self.records(paste, row["id"])
                                 ["no_patch_because"], "a meld")

    def test_a_patch_tip_that_is_a_ref_name(self):  # noqa: VACUOUS_ASSERTION — the absent cause is paired with the paste's positive answer: `records` asserts rc 0 and the one event, `unfilled` asserts rc 2 naming each placeholder
        row = self.row()
        argv = FIX[:8] + ("--patch-tip", "HEAD")
        _line, paste = self.unlooped(self.verdict(row, argv),
                                     "is not one commit's id",
                                     ((SHA, self.c),))
        self.assertEqual(self.records(paste, row["id"])["patch_tip"], self.c)

    def test_a_patch_tip_that_does_not_descend_from_the_row(self):  # noqa: VACUOUS_ASSERTION — the absent cause is paired with the paste's positive answer: `records` asserts rc 0 and the one event, `unfilled` asserts rc 2 naming each placeholder
        row = self.row()
        argv = FIX[:8] + ("--patch-tip", self.side)
        line, paste = self.unlooped(self.verdict(row, argv),
                                    "does not descend from the reviewed tip",
                                    ((SHA, self.c),))
        self.assertNotIn(self.side, line)
        self.assertEqual(self.records(paste, row["id"])["patch_tip"], self.c)

    def test_evidence_the_door_refuses_whole(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty literal table, and every case asserts the pasted line's positive answer (`records`, `unfilled` or `minted`)
        for evidence, cause in (
                ("one\ntwo", "verdict evidence must be one printable line"),
                ("all good Subsumption verified x on trunk y",
                 "malformed subsumption")):
            with self.subTest(cause=cause):
                row = self.row()
                line, paste = self.unlooped(
                    self.verdict(row, ("--concur", "--measured"), evidence),
                    cause, (("<evidence>", "fine"),))
                self.assertTrue(line.endswith("-- '<evidence>'"), line)
                self.assertEqual(self.records(paste, row["id"])
                                 ["verdict_ref"], "fine")

    def test_a_design_finding_the_direction_cannot_carry(self):
        row = self.row()
        line, paste = self.unlooped(
            self.verdict(row, ("--concur", "--measured", "--design-finding",
                               "a seam")),
            "--design-finding names a finding a FIX hands back")
        self.assertNotIn("--design-finding", line)
        self.assertEqual(self.records(paste, row["id"])["polarity"], "concur")

    def test_a_design_finding_over_its_cap(self):
        row = self.row()
        _line, paste = self.unlooped(
            self.verdict(row, FIX + ("--design-finding", "d" * 300)),
            "design finding must be one printable line",
            (("<FINDING of at most 256 chars>", "'a seam'"),))
        self.assertEqual(self.records(paste, row["id"])["design_findings"],
                         ["a seam"])

    def test_a_meld_the_direction_cannot_carry(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty literal table, and every case asserts the pasted line's positive answer (`records`, `unfilled` or `minted`)
        for argv, cause in (
                (("--concur", "--measured"), "not on concur"),
                (FIX, "--meld on a FIX needs --patch-tip")):
            with self.subTest(cause=cause):
                row = self.row()
                line, paste = self.unlooped(
                    self.verdict(row, argv + ("--meld", "helm-meld-x")),
                    cause)
                self.assertNotIn("--meld", line)
                self.records(paste, row["id"])

    def test_a_meld_given_twice(self):
        row = self.row()
        line, paste = self.unlooped(
            self.verdict(row, FIX[:8] + ("--patch-tip", self.c, "--meld",
                                         "m-1", "--meld", "m-2")),
            "--meld needs the meld ROOM, once")
        self.assertIn("--meld '<ROOM>'", line)
        self.unfilled(paste, "<ROOM>")


class AModelRunsReadRefusedByItsShapeTest(NoEchoBase):
    """A seat records a model run's read with --reviewer-model and
    --reviewer-run (task/2948). Each refusal below is the argument half's
    (`_on_behalf_shape`), decided by the values alone."""

    RUN = ("--reviewer-model", "gpt-5", "--reviewer-run", "run-1")

    def swapped(self, flag, value):
        return tuple(swap(self.RUN, flag, value))

    def verdict(self, row, *argv):
        return self.dispatch("verdict", row["id"], row["tip"], *argv, "--",
                             "gate:abcd1234 fine")

    def test_each_shape(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty literal table, and every case asserts the pasted line's positive answer (`records`, `unfilled` or `minted`)
        cases = (
            (("--concur", "--measured") + self.swapped("--reviewer-model",
                                                       "claude-sonnet-4-5"),
             "never reviews anything", "<MODEL>"),
            (("--concur", "--measured", "--reviewer-model", "gpt-4")
             + self.RUN, "needs one value, once", "<MODEL>"),
            (("--concur", "--measured") + self.swapped("--reviewer-run",
                                                       "a b"),
             "must be one token", "<RUN>"),
            (("--concur", "--measured", "--author-model", "gpt-5"),
             "names BOTH", "<RUN>"),
            (("--approve", "--measured") + self.RUN, "does not APPROVE",
             "<--concur|--fix>"))
        for argv, cause, hole in cases:
            with self.subTest(cause=cause):
                row = self.row()
                line, paste = self.unlooped(self.verdict(row, *argv), cause)
                self.assertIn(shlex.quote(hole), line)
                self.unfilled(paste, hole)

    def test_a_meld_beside_a_model_run(self):
        row = self.row()
        line, _paste = self.unlooped(
            self.verdict(row, "--concur", "--measured", "--meld", "m-1",
                         *self.RUN), "a model run's advisory read did not")
        self.assertNotIn("--meld", line)
        self.assertIn("--reviewer-model gpt-5 --reviewer-run run-1", line)


class AReviewDoneOrHoldValueRefusedByItsShapeTest(NoEchoBase):

    def test_an_id_that_names_no_row(self):
        row = self.row()
        line, paste = self.unlooped(self.done("abcdef0123", "concur", "fine"),
                                    "no row id starts with abcdef0123")
        self.assertEqual(line, "helm review done '<row-id-prefix>' concur -- "
                         "fine")
        self.unfilled(paste, "<row-id-prefix>")
        self.records(self.paste(line.replace("'<row-id-prefix>'",
                                             row["id"][:12])), row["id"])

    def test_a_tip_that_is_not_hex_beside_an_id_that_is_not_one(self):
        row = self.row()
        line, paste = self.unlooped(
            self.done("zzzz", "concur", "--tip", "HEAD", "fine"),
            "'zzzz' is not a row id")
        self.assertEqual(line, "helm review done '<row-id-prefix>' concur "
                         "--tip '<TIP_YOU_READ>' -- fine")
        self.unfilled(paste, "<row-id-prefix>", "<TIP_YOU_READ>")
        self.records(self.paste(line.replace("'<row-id-prefix>'", row["id"])
                                .replace("'<TIP_YOU_READ>'", self.b)),
                     row["id"])

    def test_a_count_that_is_not_a_count(self):
        row = self.row()
        _line, paste = self.unlooped(
            self.done(row["id"][:8], "fix", "--finding-count", "abc",
                      "--prior-relation", "new", "--worse-than-main",
                      "helm/x.py", "--no-patch-because", "a meld", "--",
                      "inverted"),
            "finding_count needs", (("<N>", "3"),))
        self.assertEqual(self.records(paste, row["id"])["finding_count"], 3)

    def test_a_hold_reason_on_two_lines(self):
        row = self.row()
        _line, paste = self.unlooped(
            self.dispatch("hold", row["id"], "--source-clean", row["tip"],
                          "--", "one\ntwo"),
            "hold reason must be one printable line",
            (("<reason>", "'nothing found'"),))
        self.records(paste, row["id"], "hold")
        self.assertEqual(self.folded(row["id"])["hold_reason"],
                         "nothing found")


class ASendValueRefusedByItsShapeTest(NoEchoBase):
    """The send door's values and the answers it owes a brief. Each paste
    that fills the placeholder mints the row."""

    def send(self, *extra, lane="lane-echo", brief="build the parser",
             kind="build", arm=("--new-work",), recipient="integrator",
             repo=None, ref=None):
        return self.dispatch("send", recipient, lane, brief, "--ref",
                             ref or self.c, "--kind", kind, *arm, "--repo",
                             repo or self.repo, *extra, author=False)

    def minted(self, paste, lane):
        rc, _out, err = paste
        self.assertEqual(rc, 0, err)
        self.assertEqual(len([r for r in dispatches.snapshot()[0].values()
                              if r.get("lane") == lane]), 1, err)

    def test_each_value(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty literal table, and every case asserts the pasted line's positive answer (`records`, `unfilled` or `minted`)
        cases = (
            ("kind", dict(kind="bogus"), "kind must be one of",
             "<build|review>", "build"),
            ("deadline", dict(extra=("--deadline", "soon")),
             "deadline takes SECONDS", "<SECONDS>", "600"),
            ("note", dict(extra=("--note", "n" * 1001)),
             "note must be one printable line", "<note>", "'a note'"),
            ("key", dict(extra=("--key", "k" * 257)),
             "operation key must be one printable line", "<KEY>", "k-1"),
            ("lane", dict(lane="l" * 161), "lane must be one printable line",
             "<lane>", None),
            ("recipient", dict(recipient="no such seat"),
             "must be 1-64 chars", "<recipient>", "integrator"),
            ("brief", dict(brief="b" * 16001), "message must be 1-16000",
             "<brief>", "'build the parser'"))
        for name, kw, cause, hole, value in cases:
            with self.subTest(name=name):
                lane = "echo-" + name
                kw = dict(kw)
                extra = kw.pop("extra", ())
                kw.setdefault("lane", lane)
                lane = lane if value is not None else lane + "-filled"
                line, paste = self.unlooped(
                    self.send(*extra, **kw), cause,
                    ((hole, value if value is not None else lane),))
                self.assertIn(shlex.quote(hole), line)
                self.minted(paste, lane)

    def test_each_answer_the_brief_owes(self):  # noqa: VACUOUS_ASSERTION — the loop runs a fixed non-empty literal table, and every case asserts the pasted line's positive answer (`records`, `unfilled` or `minted`)
        cases = (
            ("read-only", dict(brief="review it, do not commit anything",
                               kind="review"),
             "this REVIEW brief tells the reader not to edit", REASON,
             "'a reason'"),
            ("posture", dict(brief="patch the orca pane handler"),
             "carries no posture", REASON, "'no seam change'"),
            ("irreversible", dict(brief="run the migration on staging"),
             "IRREVERSIBLE", DOOR,
             "--async-because 'staging only'"))
        for name, kw, cause, hole, value in cases:
            with self.subTest(name=name):
                lane = "owed-" + name
                line, paste = self.unlooped(self.send(lane=lane, **kw), cause)
                self.unfilled(paste, hole)
                self.minted(self.paste(line.replace(shlex.quote(hole), value)),
                            lane)

    def test_a_supersedes_that_names_no_row(self):
        sent = self.row(lane="lane-parent")
        line, paste = self.unlooped(
            self.send(lane="lane-parent", brief="the cure is on the lane",
                      arm=("--supersedes", "abcdef012345")),
            "abcdef012345")
        self.assertIn("--supersedes '<ROW_ID>'", line)
        self.unfilled(paste, "<ROW_ID>")
        rc, _out, err = self.paste(line.replace("'<ROW_ID>'", sent["id"]))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len([r for r in dispatches.snapshot()[0].values()
                              if r.get("supersedes") == sent["id"]]), 1)

    def test_a_supersedes_whose_repository_differs_from_parent(self):
        # 1. Other repository B with a commit tip
        other = os.path.join(self.tmp, "other-repo")
        os.makedirs(other)
        subprocess.run(["git", "-C", other, "init", "-q"], check=True)
        with open(os.path.join(other, "artifact.txt"), "w") as f:
            f.write("built artifact\n")
        subprocess.run(["git", "-C", other, "add", "artifact.txt"], check=True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "-C", other, "commit", "-qm", "artifact"],
                       check=True, env=env)
        other_tip = subprocess.run(["git", "-C", other, "rev-parse", "HEAD"],
                                   check=True, capture_output=True,
                                   text=True).stdout.strip()

        # Register both repositories so they are valid helm projects
        from helm import registry
        data = {"projects": {"proja": {"name": "proja", "path": os.path.realpath(self.repo)},
                             "projb": {"name": "projb", "path": os.path.realpath(other)}}}
        patcher = mock.patch.object(registry, "load", return_value=data)
        patcher.start()
        self.addCleanup(patcher.stop)

        # 2. Parent row keyed to repo A (self.repo)
        parent = self.row(lane="lane-parent")

        # 3. Attempting to send with --supersedes naming parent row across repos
        refused = self.send(
            lane="lane-parent", brief="built the artifact",
            arm=("--supersedes", parent["id"]),
            repo=other, ref=other_tip, kind="review")

        # 4. Check refusal and corrected line
        line, paste = self.unlooped(
            refused, "refusing foreign chain authority")

        # Acceptance 1: asserts the printed corrected line is NOT the refused command
        self.assertNotIn("--supersedes", line)
        self.assertIn("--new-work", line)
        self.assertIn(parent["id"][:12], line)

        # Acceptance 2: Running the printed corrected line in that fixture succeeds (rc 0)
        # and the parent row is answered or linked (cited in the brief).
        rc, out, err = paste
        self.assertEqual(rc, 0, err)

        # Verify the new row was created in repo B and links the parent row in its brief
        rows = [r for r in dispatches.snapshot()[0].values() if r.get("id") != parent["id"]]
        self.assertEqual(len(rows), 1)
        new_row = rows[0]
        self.assertIn(parent["id"][:12], new_row["message_body"])
        self.assertEqual(new_row["repo_id"], dispatches._repo_info(other)["repo_id"])


class WhatThisLaneDoesNotChangeTest(NoEchoBase):
    """THE CONTROLS: refusals whose cause the line never carried keep the
    line they printed before task/3403."""

    def test_an_imperfect_fix_with_no_cure_answer_asks_for_the_patch(self):
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      *FIX[:6], "--imperfect", "--", "ev")
        self.assertEqual(rc, 2, err)
        self.assertIn("IMPERFECT IS NOT A BLOCK", err)
        self.assertEqual(self.corrected(err), "helm dispatch verdict %s %s "
                         "--fix --measured --finding-count 1 --prior-relation "
                         "new --imperfect --patch-tip '<FULL_SHA>' -- ev"
                         % (row["id"], self.b))

    def test_a_missing_basis_keeps_every_value_it_was_given(self):
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      "--fix", *FIX[2:], "--", "ev")
        self.assertEqual(rc, 2, err)
        self.assertIn("DECLARE HOW YOU KNOW", err)
        self.assertEqual(self.corrected(err), "helm dispatch verdict %s %s "
                         "--fix '<--measured|--inferred>' --finding-count 1 "
                         "--prior-relation new --worse-than-main helm/x.py "
                         "--no-patch-because 'a meld' -- ev"
                         % (row["id"], self.b))

    def test_a_hex_patch_prefix_no_commit_answers_comes_back_as_typed(self):
        """Its cause is the world, not its shape: a cure committed in the
        seat's own clone resolves here once fetched, and the same line then
        records."""
        row = self.row()
        rc, _out, err = self.dispatch("verdict", row["id"], row["tip"],
                                      *FIX[:8], "--patch-tip", "1234567",
                                      "--", "ev")
        self.assertNotEqual(rc, 0, err)
        self.assertIn("--patch-tip 1234567 --", self.corrected(err))

    def test_an_ambiguous_prefix_with_one_row_of_yours_still_names_it(self):
        mine = "abcd" + "1" * 28
        rows = {rid: {"id": rid, "status": "open", "recipient": who,
                      "sender": "integrator", "lane": "l", "tip": "a" * 40,
                      "ts": "2026-09-26T00:00:00Z"}
                for rid, who in ((mine, READER), ("abcd" + "2" * 28, "x"))}
        with mock.patch.object(dispatches, "snapshot",
                               return_value=(rows, None)):
            rc, _out, err = self.done("abcd", "concur", "fine")
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.corrected(err),
                         "helm review done %s concur -- fine" % mine)

    def test_a_brief_may_still_name_a_value_placeholder_in_prose(self):
        """The value placeholders this lane added are prose inside a brief;
        only the ones a send line prints in a word's own slot are refused
        there, as before. Pasted unfilled as a value, each is refused."""
        prose = "set <KEY> in the <note> tag after <SECONDS>"
        self.assertIsNone(review_done.brief_refusal(
            ["integrator", "lane-x", prose, "--ref", self.c]))
        self.assertIsNotNone(review_done.brief_refusal(
            ["integrator", "lane-x", "fix it " + DOOR, "--ref", self.c]))
        for hole in ("<KEY>", "<note>", "<SECONDS>"):
            self.assertIn(shlex.quote(hole), review_done.unfilled_refusal(
                ["--key", hole]) or "")
