#!/usr/bin/env python3
"""task/910 — a whole-file roster write must not destroy what it cannot parse.

THE DEFECT, PROBE-PROVEN BEFORE A LINE WAS WRITTEN: write alpha, write beta,
corrupt the file, write a third seat, and the roster holds ONLY the third.
roster() fail-opens to {} for unparseable bytes exactly as for a missing file,
and every whole-file writer follows that read with pk.write_json of the
WHOLE dict — so one transient parse failure overwrites every row the reader
could not parse. That is how a fleet of live seats becomes unlisted between
one join and the next (task/866 Defect A; the live roster carried no row older
than the 20:44:45Z join that followed the loss).

WHAT THIS LANE DOES AND DELIBERATELY DOES NOT DO. It makes the loss LOUD and
EVIDENCED: the unparseable bytes are preserved beside the roster and the write
says so. It does NOT restore the lost rows — unparseable bytes may be
truncated, and a real restore path (a .prev snapshot, or carrying the roster
in the log-flush write-behind that already exists for chat) is a durability
design that deserves its own review rather than an addition here.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helm import seats_common as sc  # noqa: E402
from helm import seats_roster as sr  # noqa: E402

CORRUPT = "{ this is not json\n"


class RosterWriteGuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-test-rosterwrite-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HELM_HOME": self.dir,
                                           "HELM_CHAT_DIR": self.dir,
                                           "HELM_CHAT_NAME": "zz-writer"})
        env.start(); self.addCleanup(env.stop)
        self.err = mock.patch.object(sys, "stderr", new_callable=_Capture)
        self.err.start(); self.addCleanup(self.err.stop)

    def _quarantined(self):
        d = os.path.dirname(sr.roster_path())
        base = os.path.basename(sr.roster_path()) + ".unreadable."
        return [f for f in os.listdir(d) if f.startswith(base)]

    def _scan_can_see_a_quarantine_file(self):
        """A sentinel, so an arm asserting NO quarantine has an unconditional
        positive control on that exact observable. Without it a helper with
        the wrong prefix returns [] forever and three arms below pass on
        nothing — which is precisely what the rung flagged, correctly."""
        sentinel = sr.roster_path() + ".unreadable.0"
        open(sentinel, "w").close()
        seen = self._quarantined()
        os.remove(sentinel)
        return seen

    def test_an_unparseable_roster_is_PRESERVED_rather_than_overwritten(self):
        """THE PROBE, PROMOTED TO AN ARM. Before the guard this sequence left
        the roster holding one key and alpha and beta simply gone, with
        nothing on disk and nothing on stderr to say a fleet had been
        delisted."""
        sr.write_roster("zz-alpha")
        sr.write_roster("zz-beta")
        # CONTROL, unconditional and on the same observable: an ORDINARY
        # second write keeps the rows that were already there. Without this
        # the arm below could pass on a roster that never held them.
        self.assertIn("zz-alpha", sr.roster(), "control: rows accumulate")
        self.assertIn("zz-beta", sr.roster(), "control: rows accumulate")

        with open(sr.roster_path(), "w", encoding="utf-8") as f:
            f.write(CORRUPT)
        sr.write_roster("zz-writer")

        kept = self._quarantined()
        self.assertEqual(len(kept), 1,
                         "the unparseable bytes were not preserved: %r" % kept)
        with open(os.path.join(self.dir, kept[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), CORRUPT,
                             "the preserved file is not the original bytes")
        self.assertIn("could not be PARSED", sys.stderr.text,
                      "the destruction was silent")

    def test_a_FAILED_preservation_REFUSES_instead_of_deleting_the_only_copy(self):  # noqa: VACUOUS_ASSERTION — task/912: the assertRaises absence roots at the exception class name and cannot intersect any control; the unconditional structural control is the assertIn on the surviving bytes below
        """THE SECOND SURVIVOR, found in review on the first cut and
        the reason this arm exists at all.

        That version did os.replace inside a try, wrote NOT PRESERVED into the
        message on failure, and RETURNED — so the caller went on to overwrite
        the file. No copy, no refusal, and an outcome bit-for-bit identical to
        the task/910 bug this lane cures, differing only by a stderr line that
        a hook context swallows. No arm covered the branch, so the mutation
        matrix could not see it either.

        PRESERVE-OVER-REFUSE WEIGHS BYTES WE KEPT. It has nothing to say about
        bytes we are about to delete with no copy, so this branch fails
        CLOSED — and a read-only or full filesystem is exactly when the
        replace fails AND nobody can recover by hand."""
        with open(sr.roster_path(), "w", encoding="utf-8") as f:
            f.write(CORRUPT)

        def no_replace(src, dst):
            raise OSError(13, "Permission denied")

        with mock.patch.object(sc.os, "replace", no_replace):
            with self.assertRaises(OSError):
                sc.roster_for_write()

        # THE CONTROL IS THE POINT OF THE ARM: the original bytes are STILL
        # THERE. A refusal that lost them anyway would satisfy assertRaises
        # and defeat the whole finding.
        with open(sr.roster_path(), encoding="utf-8") as f:
            surviving = f.read()
        self.assertIn("this is not json", surviving,
                      "the refusal did not save the bytes it refused for")
        self.assertEqual(self._quarantined(), [],
                         "a quarantine file appeared despite replace failing")

    def test_a_MISSING_roster_is_an_ordinary_first_join(self):  # noqa: VACUOUS_ASSERTION — MEASURED against the analyzer: it gives each opaque helper call its own producer id (positive call#1, absence call#2), so a control and an absence sourced from two helper invocations can never intersect no matter how they are written. The control is the sentinel assertion directly above. Filed against the instrument, not worked around
        """A file that is not there is PROVEN empty, and a first-ever join
        must not be treated as a loss. This is the arm that keeps the guard
        from firing on the most common state in a fresh home."""
        quarantined = self._scan_can_see_a_quarantine_file()
        self.assertEqual(quarantined, [".roster.json.unreadable.0"],
                         "control: the quarantine scan can see a file at all")
        sr.write_roster("zz-writer")
        self.assertIn("zz-writer", sr.roster(),
                      "a first join into a missing roster must succeed")
        quarantined = self._quarantined()
        self.assertEqual(quarantined, [],
                         "a missing roster was treated as unparseable")
        # AND THE WARNING IS THE OBSERVABLE THAT ACTUALLY BINDS THIS. The
        # first cut asserted only the absence of a quarantine FILE and a
        # mutation SURVIVED it: with the narrowness removed a missing path
        # still creates no file, because os.replace on a path that is not
        # there just fails and the message says NOT PRESERVED. The arm was
        # green about the wrong thing until the matrix said so.
        self.assertEqual(sys.stderr.text, "",
                         "a healthy first join announced a data loss")

    def test_an_EMPTY_but_parseable_roster_is_an_ordinary_write(self):  # noqa: VACUOUS_ASSERTION — MEASURED against the analyzer: it gives each opaque helper call its own producer id (positive call#1, absence call#2), so a control and an absence sourced from two helper invocations can never intersect no matter how they are written. The control is the sentinel assertion directly above. Filed against the instrument, not worked around
        """The discrimination the guard turns on: {} PARSES, so it is a
        genuinely empty roster and there is nothing to destroy. Reading the
        bytes as 'empty result therefore corrupt' would quarantine a healthy
        file on every fresh home."""
        quarantined = self._scan_can_see_a_quarantine_file()
        self.assertEqual(quarantined, [".roster.json.unreadable.0"],
                         "control: the quarantine scan can see a file at all")
        with open(sr.roster_path(), "w", encoding="utf-8") as f:
            f.write("{}")
        sr.write_roster("zz-writer")
        self.assertIn("zz-writer", sr.roster(),
                      "a parseable empty roster must accept a write")
        quarantined = self._quarantined()
        self.assertEqual(quarantined, [],
                         "a parseable empty roster was quarantined")
        self.assertEqual(sys.stderr.text, "",
                         "a parseable empty roster announced a data loss")

    def test_wrong_shaped_but_parseable_rows_SURVIVE_the_write(self):  # noqa: VACUOUS_ASSERTION — MEASURED against the analyzer: it gives each opaque helper call its own producer id (positive call#1, absence call#2), so a control and an absence sourced from two helper invocations can never intersect no matter how they are written. The control is the sentinel assertion directly above. Filed against the instrument, not worked around
        """THE TOLERANCE THAT MUST NOT REGRESS, and it has a price tag.
        Demanding roster_checked's stricter VALID of every write cost 142
        failures and 63 errors, because the shapes it rejects are shapes the
        fleet is USING. Wrong-shaped JSON still parses, so roster() returns it
        intact and it is never seen as empty — this arm pins that the guard
        did not quietly widen into that rule."""
        quarantined = self._scan_can_see_a_quarantine_file()
        self.assertEqual(quarantined, [".roster.json.unreadable.0"],
                         "control: the quarantine scan can see a file at all")
        with open(sr.roster_path(), "w", encoding="utf-8") as f:
            f.write('{"zz-other": {"session": 5}}')
        sr.write_roster("zz-writer")
        after = sr.roster()
        self.assertIn("zz-writer", after, "the write did not land")
        self.assertIn("zz-other", after,
                      "a wrong-shaped row the fleet may be using was dropped")
        quarantined = self._quarantined()
        self.assertEqual(quarantined, [], "parseable JSON was quarantined")
        self.assertEqual(sys.stderr.text, "",
                         "wrong-shaped but parseable JSON announced a loss")

    def test_the_guard_RAISES_on_a_path_it_cannot_resolve(self):  # noqa: VACUOUS_ASSERTION — task/912, measured: an assertRaises absence roots at the EXCEPTION CLASS NAME, so no control on the code under test can intersect it. The unconditional structural control is the assertIn below
        """THE DOCSTRING SAID 'NEVER RAISES' AND THAT WAS FALSE. roster()
        resolves roster_path() first, and under a relative HELM_HOME with the
        process cwd removed that raises FileNotFoundError — the same hazard
        roster_checked was fixed for tonight, one call down from a sentence I
        wrote without following it.

        Pinned rather than papered over, because it decides what a caller
        must write. Every caller meets it one line earlier at
        _flocked(roster_path() + '.lock'), so nothing new reaches them; an
        arm is what keeps that true if the lock line ever moves."""
        gone = tempfile.mkdtemp(prefix="helm-test-rosterpath-")
        prior_cwd = os.getcwd()
        prior = {k: os.environ.get(k) for k in ("HELM_HOME", "HELM_CHAT_DIR")}

        def restore():
            try:
                os.chdir(prior_cwd)
            except OSError:
                os.chdir(tempfile.gettempdir())
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)

        # CONTROL on the same call, unconditional and STRUCTURAL: it returns
        # a real row while the cwd still exists, so the raise below is the
        # unresolvable path and not a function that never worked. The first
        # draft asserted the result was EMPTY, which is another absence
        # wearing a control's name.
        sr.write_roster("zz-writer")
        rows = sc.roster_for_write()
        self.assertIn("zz-writer", rows,
                      "control: it reads real rows while the path resolves")

        os.chdir(gone)
        os.rmdir(gone)
        os.environ["HELM_HOME"] = "relative-home"
        os.environ.pop("HELM_CHAT_DIR", None)
        with self.assertRaises(OSError):
            sc.roster_for_write()
        shutil.rmtree(gone, ignore_errors=True)

    def test_EVERY_whole_file_writer_reads_through_the_guard(self):
        """THE SHAPE ARM, and the reason this lane is not a one-line fix.

        Whole-roster writers span the roster, runtime, and report owners. The
        rule therefore scans every extracted seats module and recognizes the
        actual write target, rather than trusting one module to contain every
        writer forever. A writer of ``roster_path()`` may not bind the fail-open
        reader, wherever the owner lives.
        """
        import ast
        root = os.path.join(os.path.dirname(__file__), "..", "helm")
        offenders, guarded = [], []
        for f in sorted(os.listdir(root)):
            if not f.startswith("seats_") or not f.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(root, f),
                                  encoding="utf-8").read())
            for d in [n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef)]:
                writes = any(
                    isinstance(n, ast.Call)
                    and getattr(n.func, "attr", None) == "write_json"
                    and n.args and isinstance(n.args[0], ast.Call)
                    and getattr(n.args[0].func, "id", None) == "roster_path"
                    for n in ast.walk(d))
                if not writes:
                    continue
                for n in ast.walk(d):
                    if isinstance(n, ast.Assign) \
                            and isinstance(n.value, ast.Call):
                        name = getattr(n.value.func, "id", None)
                        if name == "roster":
                            offenders.append((d.name, n.lineno))
                        elif name == "roster_for_write":
                            guarded.append(d.name)
        # ONE SCAN, BOTH READINGS, bound to one name deliberately. An empty
        # scan would satisfy the offender assertion vacuously, and reading the
        # control off a separate variable hides from any checker that the two
        # answers share a source — they must rise and fall together.
        scan = {"guarded": sorted(guarded), "offenders": offenders}
        self.assertEqual(
            scan["guarded"],
            # `migrate_incarnations` joined them with the identity-generation
            # stamp: it publishes the whole roster to mark rows written before
            # the generation marker existed, and it reads through
            # roster_for_write like every other writer here — a fail-open read
            # would let it stamp a partial roster and publish the loss.
            ["bind_lifecycle_runtime", "disown_session", "gc_roster",
             "migrate_incarnations", "rehome_seat", "rename_seat", "set_mute",
             "set_status", "stamp_proxy_runtime", "write_roster"],
            "the scan did not find every known whole-roster writer, so it "
            "cannot speak about offenders: %r" % scan["guarded"])
        self.assertEqual(scan["offenders"], [],
                         "a whole-file roster writer still binds the "
                         "fail-open reader: %r" % scan["offenders"])


class _Capture:
    """A stderr stand-in that keeps what was written, so an arm can assert the
    warning FIRED rather than assert that nothing complained."""

    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s
        return len(s)

    def flush(self):
        pass


if __name__ == "__main__":
    unittest.main()


class RosterAcquiredTest(unittest.TestCase):
    """ONE ACQUISITION, TWO VIEWS — and the contract that must not move.

    A roster render needs the rows to list the seats AND a verdict on those
    rows to decide whether a claim's holder can honestly be called unlisted.
    Those came from two functions, so the two halves of one screen were two
    reads with a window between them, and a roster changing inside that window
    let a single render contradict itself. `roster_acquired` answers both from
    one open.

    THE ARM THAT GUARDS THE REST OF THE TREE is the all-or-nothing pin below.
    `roster_checked` now DELEGATES here, and the create-only mint and the
    whole-file write guard both refuse on its tri-state — so a well-meaning
    "why does the narrowing throw away rows we parsed fine" would widen what
    those two accept without touching either file. That arm goes red first.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="helm-test-acquired-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HELM_HOME": self.dir,
                                           "HELM_CHAT_DIR": self.dir,
                                           "HELM_CHAT_NAME": "zz-acquirer"})
        env.start(); self.addCleanup(env.stop)

    def _write(self, text):
        os.makedirs(os.path.dirname(sr.roster_path()), exist_ok=True)
        with open(sr.roster_path(), "w", encoding="utf-8") as f:
            f.write(text)

    def test_a_MISSING_roster_is_PROVEN_empty_for_both_doors(self):
        """Missing is not a failure — it is a roster with no seats in it, and
        collapsing that into 'unreadable' makes every genuinely unlisted holder
        render UNKNOWN and the marker never fire."""
        path = sr.roster_path()
        if os.path.exists(path):
            os.remove(path)
        self.assertEqual(sr.roster_acquired(), ({}, False))
        self.assertEqual(sr.roster_checked(), ({}, False))

    def test_a_VALID_roster_reads_identically_through_both(self):
        """The pole. Without it, a reader that returned ({}, True) for
        everything would satisfy every other arm in this class."""
        self._write('{"alice": {"session": "s"}}')
        self.assertEqual(sr.roster_acquired(), ({"alice": {"session": "s"}}, False))
        self.assertEqual(sr.roster_checked(), ({"alice": {"session": "s"}}, False))

    def test_UNPARSEABLE_bytes_yield_no_rows_and_a_FAILED_verdict(self):
        """There is nothing to hand back fail-open here: json.load produced
        nothing, so the two doors necessarily agree."""
        self._write("{ this is not json")
        self.assertEqual(sr.roster_acquired(), ({}, True))
        self.assertEqual(sr.roster_checked(), ({}, True))

    def test_ONE_BAD_ROW_keeps_the_rows_here_and_drops_them_THERE(self):
        """THE WHOLE POINT OF TWO DOORS, in one file state.

        A row whose `session` is not a string fails validation. The renderer
        must still draw every seat — blanking the owner's roster on one
        malformed row trades a contradiction for an outage, which is the
        regression that made 'just point the renderer at roster_checked' the
        wrong one-line fix. The listedness marker must NOT see those rows,
        because it would be asserting membership from a file it just judged
        untrustworthy.

        So: same bytes, same instant, two answers, on purpose."""
        self._write('{"alice": {"session": "s"}, "bob": {"session": 12345}}')
        rows, failed = sr.roster_acquired()
        self.assertTrue(failed, "a wrong-shaped row did not fail the verdict")
        self.assertEqual(sorted(rows), ["alice", "bob"],
                         "the fail-open door dropped rows — the seat list goes "
                         "dark on one malformed row, which is an outage where "
                         "there was a contradiction")
        self.assertEqual(sr.roster_checked(), ({}, True),
                         "roster_checked stopped being ALL-OR-NOTHING. The "
                         "create-only mint and the whole-file write guard both "
                         "refuse on that contract; widening it here widens "
                         "what they accept, in a file neither of them names")

    def test_the_two_doors_answer_from_the_SAME_read(self):
        """roster_checked is a NARROWING of roster_acquired, not a second
        reader. If it ever does its own open again, the pair can disagree about
        one file — which is the class of defect this whole change removes."""
        self._write('{"alice": {"session": "s"}}')
        seen = []
        real = sr.roster_acquired

        def counting():
            seen.append(1)
            return real()

        with mock.patch.object(sr, "roster_acquired", counting):
            sr.roster_checked()
        self.assertEqual(len(seen), 1,
                         "roster_checked did not go through roster_acquired — "
                         "it is reading the file itself again")
